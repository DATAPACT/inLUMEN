import { chromium, expect } from '@playwright/test';
import { ensure, positiveInteger, validateSessions, validateGraph, validateAudioGraph, safeFailure, summarize, DEFAULT_PROMPT, AUDIO_PROMPTS } from './core.mjs';
import { loadSessionAssets, runAudioSession } from './audio-session.mjs';

const WS_HEADER = 'X-InLumen-Workspace-Id';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export async function runLoadTest({ baseURL, issuer, accounts, rounds = 1, timeoutMs = 180000, rampMs = 0, preflight = false, headed = false, prompt = DEFAULT_PROMPT, scenario = 'design', codeZip, audioFile, runTimeoutMs = 1800000, maxRunCpus = 2, maxRunMemoryGiB = 4, pollMs = 5000, reviewAIChanges, onProgress = console.log }) {
  ensure(['design', 'audio-session'].includes(scenario), 'invalid_scenario');
  ensure(reviewAIChanges === undefined || typeof reviewAIChanges === 'boolean', 'invalid_review_ai_changes');
  const allocationLimits = scenario === 'audio-session' ? {
    cpu: positiveInteger(maxRunCpus, 'max_run_cpus'),
    memory_bytes: positiveInteger(maxRunMemoryGiB, 'max_run_memory_gib') * 1024 ** 3,
  } : null;
  const assets = scenario === 'audio-session' && !preflight ? await loadSessionAssets(codeZip, audioFile) : null;
  const runID = `loadtest-${new Date().toISOString().replace(/\D/g, '')}`;
  const browser = await chromium.launch({ headless: !headed });
  const actors = [], results = [], workspaces = [];
  let phase = 'login', failure = null;
  const origin = new URL(baseURL).origin;
  const issuerURL = new URL(issuer);
  const appPath = url => new URL(url).origin === origin;

  async function api(actor, path, { method = 'GET', data, multipart, headers = {}, workspace = actor.workspace } = {}) {
    ensure(actor.token, 'missing_authenticated_token');
    const response = await actor.context.request.fetch(`${baseURL}${path}`, {
      method, data, multipart, headers: { Authorization: actor.token, ...(workspace ? { [WS_HEADER]: workspace } : {}), ...headers },
      timeout: 30000, maxRedirects: 0, maxRetries: 0,
    });
    return response;
  }

  async function setup(account, index) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' });
    const actor = { context, page: await context.newPage(), index, token: '', workspace: null, session: null };
    actors.push(actor);
    onProgress(`User ${index + 1}: signing in.`);
    const { page } = actor;
    page.setDefaultTimeout(30000);
    // Observe only this app's authenticated requests; tokens stay in memory.
    page.on('request', request => {
      if (appPath(request.url()) && request.headers().authorization?.startsWith('Bearer ')) actor.token = request.headers().authorization;
    });
    await page.route('**/*', async route => {
      const request = route.request();
      const headers = request.headers();
      if (appPath(request.url()) && new URL(request.url()).pathname === '/simple_chat') {
        let credential;
        try { credential = request.postDataJSON()?.llm_config?.credential_id; } catch { /* Block malformed requests. */ }
        if (credential !== 'application-llm') {
          actor.wrongLLMConfiguration = true;
          actor.onBlockedChat?.();
          await route.abort('blockedbyclient');
          return;
        }
      }
      if (appPath(request.url()) && actor.workspace && headers.authorization) {
        // Real requests are forwarded unchanged except workspace selection.
        // The server still verifies membership. No responses are mocked.
        headers[WS_HEADER.toLowerCase()] = actor.workspace;
      }
      await route.continue({ headers });
    });
    const sessionResponse = page.waitForResponse(r => appPath(r.url()) && new URL(r.url()).pathname === '/api/session', { timeout: 60000 })
      .then(async response => { ensure(response.ok(), `session_http_${response.status()}`); return response.json(); })
      .catch(error => ({ setup_error: safeFailure(error) }));
    await page.goto(baseURL);
    await page.locator('#username').waitFor();
    const loginURL = new URL(page.url());
    ensure(loginURL.origin === issuerURL.origin && loginURL.pathname.startsWith(`${issuerURL.pathname.replace(/\/$/, '')}/`), 'unexpected_login_origin_or_realm');
    await page.locator('#username').fill(account.username);
    await page.locator('#password').fill(account.password);
    await page.locator('#kc-login').click();
    actor.session = await sessionResponse;
    ensure(!actor.session.setup_error, actor.session.setup_error || 'login_failed');
    ensure(actor.token, 'missing_authenticated_token');
    return actor;
  }

  async function prepare(actor, round) {
    workspaces.push({ user_index: actor.index + 1, user_id: actor.session.user.id, round, workspace_id: actor.workspace });
    const freshSession = actor.page.waitForResponse(r => appPath(r.url()) && new URL(r.url()).pathname === '/api/session', { timeout: 60000 })
      .then(r => r.json()).catch(() => null);
    await actor.page.goto(baseURL);
    const selected = await freshSession;
    ensure(selected?.active_workspace_id === actor.workspace && selected.user.id === actor.session.user.id, 'workspace_selection_failed');
    const configs = await api(actor, '/api/chatbot-configs');
    ensure(configs.ok(), `configs_http_${configs.status()}`);
    const shared = (await configs.json()).configs?.find(c => c.id === 'application-llm');
    ensure(shared?.has_api_key, 'shared_llm_not_enabled');
    actor.model = shared.model;
    if (!preflight) {
      onProgress(`User ${actor.index + 1}, round ${round}: clearing default workspace.`);
      // The toolbar opens a confirmation dialog; only its action sends the request.
      const cleared = actor.page.waitForResponse(r => appPath(r.url()) && new URL(r.url()).pathname === '/api/workspace/clear-all', { timeout: timeoutMs }).catch(() => null);
      await actor.page.getByRole('button', { name: 'Clear all', exact: true }).click();
      const confirmation = actor.page.getByRole('alertdialog', { name: 'Clear the entire workspace?' });
      await confirmation.getByRole('button', { name: 'Clear workspace', exact: true }).click();
      const response = await cleared;
      ensure(response?.status() === 200, response ? `clear_all_http_${response.status()}` : 'clear_all_timeout_outcome_unknown');
      ensure((await response.json()).status !== 'partial', 'clear_all_incomplete');
      await expect(actor.page.getByRole('button', { name: 'Clear all', exact: true })).toBeEnabled();
    }
    const graph = await api(actor, '/api/pipeline/graph');
    ensure(graph.ok(), `graph_http_${graph.status()}`);
    const initialGraph = await graph.json();
    ensure(Array.isArray(initialGraph.nodes) && (preflight || initialGraph.nodes.length === 0), 'workspace_not_empty');
    await actor.page.getByRole('button', { name: 'Settings', exact: true }).click();
    const dialog = actor.page.getByRole('dialog');
    await dialog.locator('button[aria-haspopup="menu"]').click();
    await actor.page.getByRole('menuitem').filter({ hasText: 'Application-provided LLM' }).click();
    onProgress(`User ${actor.index + 1}, round ${round}: selecting Application-provided LLM.`);
    await expect(dialog.getByText('Application-provided LLM · Managed by your administrator. No API key needed.', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(dialog).not.toBeVisible();
    if (!await actor.page.getByPlaceholder('Describe the pipeline...').isVisible()) {
      await actor.page.getByRole('button', { name: 'Chat', exact: true }).click();
    }
    if (reviewAIChanges !== undefined) {
      const review = actor.page.getByRole('switch', { name: 'Preview AI graph changes before applying', exact: true });
      if (await review.getAttribute('aria-checked') !== String(reviewAIChanges)) await review.click();
      await expect(review).toHaveAttribute('aria-checked', String(reviewAIChanges));
    }
    await actor.page.getByPlaceholder('Describe the pipeline...').fill(scenario === 'audio-session' ? AUDIO_PROMPTS[0] : `${prompt}\nName this test pipeline ${runID}-u${actor.index + 1}-r${round}.`);
    await expect(actor.page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  }

  async function design(actor, round, stage = 'design') {
    const result = { user_index: actor.index + 1, round, workspace_id: actor.workspace, model: actor.model, ok: false, phase: stage };
    const { page } = actor;
    let started;
    actor.wrongLLMConfiguration = false;
    const blockedChat = new Promise(resolve => { actor.onBlockedChat = () => resolve(null); });
    const onRequest = request => {
      if (appPath(request.url()) && new URL(request.url()).pathname === '/simple_chat') {
        started = request;
        result.request_started_ms = Date.now();
      }
    };
    page.on('request', onRequest);
    if (rampMs) await sleep(actor.index * rampMs / Math.max(1, accounts.length - 1));
    const begin = Date.now();
    try {
      const responsePromise = page.waitForResponse(r => r.request() === started, { timeout: timeoutMs })
        .catch(() => null);
      await page.getByRole('button', { name: 'Send', exact: true }).click();
      const response = await Promise.race([responsePromise, blockedChat]);
      result.request_finished_ms = Date.now();
      ensure(!actor.wrongLLMConfiguration, 'wrong_llm_configuration_blocked');
      ensure(response, 'chat_timeout_outcome_unknown');
      result.llm_credential_id = 'application-llm';
      result.http_status = response.status();
      result.request_id = response.headers()['x-request-id'] || null;
      ensure(response.ok(), `chat_http_${response.status()}`);
      const payload = await response.json();
      ensure(started.postDataJSON()?.llm_config?.credential_id === 'application-llm', 'wrong_llm_configuration');
      if (reviewAIChanges !== undefined) {
        ensure(started.postDataJSON()?.preview_changes === reviewAIChanges, 'review_ai_changes_preference_mismatch');
        if (!reviewAIChanges) ensure(!payload.sync?.preview_pending, 'unexpected_graph_preview');
      }
      ensure(payload.sync?.guardrail_passed === true && payload.sync?.graph_safe_to_apply !== false, 'graph_guardrail_failed');
      validateGraph(payload.graph);
      if (scenario === 'audio-session') validateAudioGraph(payload.graph, stage === 'extend_design');
      if (payload.sync?.preview_pending) {
        const preview = page.getByRole('dialog', { name: 'Review proposed graph', exact: true });
        await expect(preview).toBeVisible({ timeout: 30000 });
        const applied = page.waitForResponse(r => appPath(r.url()) && r.request().method() === 'POST'
          && new URL(r.url()).pathname === '/api/pipeline/graph', { timeout: 30000 }).catch(() => null);
        await preview.getByRole('button', { name: 'Apply to canvas', exact: true }).click();
        const saved = await applied;
        ensure(saved, 'graph_preview_apply_timeout_outcome_unknown');
        ensure(saved.ok(), `graph_preview_apply_http_${saved.status()}`);
        await expect(preview).toBeHidden();
        result.preview_applied = true;
      }
      await expect(page.locator('.react-flow__node')).toHaveCount(payload.graph.nodes.length, { timeout: 30000 })
        .catch(() => { ensure(false, 'canvas_graph_sync_timeout'); });
      await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 30000 });
      await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible({ timeout: 30000 });
      const persisted = await api(actor, '/api/pipeline/graph');
      ensure(persisted.ok(), `persisted_graph_http_${persisted.status()}`);
      const graph = await persisted.json();
      validateGraph(graph);
      ensure(graph.nodes.length === payload.graph.nodes.length, 'persisted_graph_mismatch');
      if (scenario === 'audio-session') actor.roles = validateAudioGraph(graph, stage === 'extend_design');
      result.nodes = graph.nodes.length;
      result.edges = graph.edges.length;
      result.ok = true;
    } catch (error) {
      result.failure = safeFailure(error);
      result.request_finished_ms ??= Date.now();
      // No replay: a timeout/524 may leave the original operation executing.
      result.outcome_may_be_running = Boolean(started) && !actor.wrongLLMConfiguration && !result.ok;
    } finally {
      actor.onBlockedChat = null;
      page.off('request', onRequest);
      result.elapsed_ms = Date.now() - begin;
      onProgress(`User ${actor.index + 1}, round ${round}, ${stage}: ${result.ok ? 'passed' : result.failure} (${result.elapsed_ms} ms)`);
    }
    return result;
  }

  try {
    const logins = await Promise.allSettled(accounts.map(setup));
    const rejected = logins.find(r => r.status === 'rejected');
    if (rejected) throw rejected.reason;
    actors.sort((a, b) => a.index - b.index);
    validateSessions(actors.map(a => a.session));
    for (const actor of actors) actor.workspace = actor.session.active_workspace_id;
    // Check denied cross-workspace reads before any paid workload.
    if (actors.length > 1) for (const [i, actor] of actors.entries()) {
      const denied = await api(actor, '/api/pipeline/graph', { workspace: actors[(i + 1) % actors.length].workspace });
      ensure(denied.status() === 404, 'cross_workspace_access_not_denied');
    }

    for (let round = 1; round <= rounds; round++) {
      phase = 'prepare';
      const prepared = await Promise.allSettled(actors.map(actor => prepare(actor, round)));
      const failed = prepared.find(r => r.status === 'rejected');
      if (failed) throw failed.reason;
      ensure(new Set(actors.map(a => a.workspace)).size === accounts.length, 'duplicate_test_workspaces');
      onProgress(`Round ${round}: ${actors.length} distinct users ready in their default workspaces.`);
      if (preflight) break;
      phase = 'design';
      if (scenario === 'design') results.push(...await Promise.all(actors.map(actor => design(actor, round))));
      else {
        const begin = Date.now();
        const sessions = actors.map(actor => ({ user_index: actor.index + 1, round, workspace_id: actor.workspace,
          model: actor.model, ok: false, phase: 'audio_session', stages: [] }));
        const first = await Promise.all(actors.map(actor => design(actor, round, 'initial_design')));
        first.forEach((stage, i) => sessions[i].stages.push(stage));
        const extendedActors = actors.filter((_, i) => first[i].ok);
        phase = 'extend_design';
        await Promise.all(extendedActors.map(actor => actor.page.getByPlaceholder('Describe the pipeline...').fill(AUDIO_PROMPTS[1])));
        await Promise.all(extendedActors.map(async actor => {
          sessions[actor.index].stages.push(await design(actor, round, 'extend_design'));
        }));
        phase = 'import_and_execute';
        await Promise.all(actors.map(async (actor, i) => {
          if (sessions[i].stages.length !== 2 || !sessions[i].stages.every(stage => stage.ok)) return;
          actor.runId = null;
          actor.runOutcomeMayBeRunning = false;
          try {
            await runAudioSession({ actor, round, assets, api, runTimeoutMs, pollMs, allocationLimits,
              onStage: stage => sessions[i].stages.push(stage), onProgress,
              isAppResponse: response => appPath(response.url()) });
            sessions[i].ok = true;
          } catch (error) {
            sessions[i].failure = safeFailure(error);
            sessions[i].run_id = actor.runId;
            sessions[i].outcome_may_be_running = Boolean(actor.runOutcomeMayBeRunning);
          } finally {
            sessions[i].elapsed_ms = Date.now() - begin;
          }
        }));
        for (const result of sessions) {
          result.elapsed_ms ??= Date.now() - begin;
          if (!result.ok) {
            result.failure ??= result.stages.find(stage => !stage.ok)?.failure || 'session_stopped_after_design_failure';
            result.outcome_may_be_running ||= result.stages.some(stage => stage.outcome_may_be_running);
          }
        }
        results.push(...sessions);
      }
      if (results.some(r => !r.ok)) break;
    }
  } catch (error) {
    failure = { phase, code: safeFailure(error) };
  } finally {
    await browser.close();
  }
  return { schema_version: 2, scenario, review_ai_changes: reviewAIChanges ?? null, allocation_limits: allocationLimits, shared_assets: assets?.report || null, run_id: runID, base_url: baseURL, finished_at: new Date().toISOString(),
    workspace_mode: 'default', clear_all_before_each_round: !preflight, preflight, requested_rounds: rounds, failure, workspaces, results,
    summary: summarize(results, accounts.length),
    passed: !failure && (preflight || results.length === accounts.length * rounds && results.every(r => r.ok)),
  };
}
