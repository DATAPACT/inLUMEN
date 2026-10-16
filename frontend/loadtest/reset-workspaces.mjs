import { chromium } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { ensure, validateAccounts, validateSessions, validatedURL, safeFailure } from './core.mjs';

const ACTIVE_RUNS = new Set(['queued', 'preparing', 'running', 'cancelling']);
const ACTIVE_GENERATION = new Set(['queued', 'running', 'cancelling']);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// Resolve the entire allowlist before making changes. Never use an admin token or
// enumerate global workspaces: every operation uses the participant's own access.
export async function planWorkspaceReset(actors, api, { onProgress = () => {} } = {}) {
  validateSessions(actors.map(actor => actor.session));
  const plan = [], seen = new Set();
  for (const actor of actors) {
    onProgress(`Checking owned workspaces (${actors.indexOf(actor) + 1}/${actors.length}): ${actor.username}`);
    const payload = await api(actor, '/api/workspaces');
    ensure(Array.isArray(payload.workspaces) && payload.workspaces.length > 0, 'missing_workspace_memberships');
    for (const workspace of payload.workspaces) {
      ensure(workspace.role === 'owner', 'non_owned_workspace_requires_separate_review');
      ensure(workspace.id && workspace.id !== 'local-workspace', 'invalid_reset_workspace');
      if (seen.has(workspace.id)) continue;
      seen.add(workspace.id);
      plan.push({ actor, workspace_id: workspace.id, name: workspace.name });
    }
  }
  return plan;
}

export async function applyWorkspaceReset(plan, api, { timeoutMs = 120000, pollMs = 2000, onProgress = () => {} } = {}) {
  const results = [];
  for (const entry of plan) {
    const { actor, workspace_id } = entry;
    const label = `(${results.length + 1}/${plan.length}) ${actor.username} / ${workspace_id}`;
    onProgress(`Resetting workspace ${label}`);
    const options = { workspace: workspace_id };
    const runs = await api(actor, '/api/pipeline-runs?limit=100', options);
    const generations = await api(actor, '/api/pipeline/generation-runs?limit=100', options);
    const active = (runs.runs || []).filter(run => ACTIVE_RUNS.has(run.status));
    const generating = (generations.runs || []).filter(run => ACTIVE_GENERATION.has(run.status));
    if (active.length || generating.length) onProgress(`Cancelling ${active.length} pipeline runs and ${generating.length} generation runs: ${label}`);
    for (const run of active) await api(actor, `/api/pipeline-runs/${encodeURIComponent(run.run_id)}`, { ...options, method: 'DELETE' });
    for (const run of generating) await api(actor, `/api/pipeline/generation-runs/${encodeURIComponent(run.run_id)}`, { ...options, method: 'DELETE' });
    const deadline = Date.now() + timeoutMs;
    let nextWaitingUpdate = 0;
    while (true) {
      const execution = await api(actor, '/api/pipeline-runs?limit=100', options);
      const generation = await api(actor, '/api/pipeline/generation-runs?limit=100', options);
      if (!(execution.runs || []).some(run => ACTIVE_RUNS.has(run.status)) &&
          !(generation.runs || []).some(run => ACTIVE_GENERATION.has(run.status))) break;
      ensure(Date.now() < deadline, 'workspace_still_busy_reset_not_performed');
      if (Date.now() >= nextWaitingUpdate) {
        onProgress(`Waiting for active work to stop: ${label}`);
        nextWaitingUpdate = Date.now() + 10000;
      }
      await sleep(pollMs);
    }
    onProgress(`Clearing workspace content: ${label}`);
    const result = await api(actor, '/api/workspace/clear-all', { ...options, method: 'POST', data: {} });
    ensure(result.status !== 'partial' && !result.generation_cleanup?.warning && result.generation_cleanup?.ok !== false, 'workspace_reset_incomplete');
    const graph = await api(actor, '/api/pipeline/graph', options);
    ensure(Array.isArray(graph.nodes) && graph.nodes.length === 0, 'workspace_reset_verification_failed');
    results.push({ username: actor.username, workspace_id, reset: true });
    onProgress(`Workspace cleared and verified: ${label}`);
  }
  return results;
}

export async function resetParticipantWorkspaces({ baseURL, issuer, accounts, apply = false, onProgress = console.log }) {
  onProgress(`Starting ${apply ? 'reset' : 'preview'} for ${accounts.length} accounts at ${baseURL}. Authenticating accounts sequentially before clearing content.`);
  const browser = await chromium.launch();
  const actors = [];
  const appOrigin = new URL(baseURL).origin, identity = new URL(issuer);
  try {
    // Sequential login keeps identity/control-plane load low during maintenance.
    for (const account of accounts) {
      onProgress(`Signing in (${actors.length + 1}/${accounts.length}): ${account.username}`);
      const context = await browser.newContext({ serviceWorkers: 'block' });
      const page = await context.newPage();
      const actor = { context, username: account.username, token: '', session: null };
      actors.push(actor);
      page.on('request', request => {
        if (new URL(request.url()).origin === appOrigin && request.headers().authorization?.startsWith('Bearer ')) actor.token = request.headers().authorization;
      });
      const session = page.waitForResponse(r => new URL(r.url()).origin === appOrigin && new URL(r.url()).pathname === '/api/session', { timeout: 60000 });
      // Navigation/login can fail before the response is awaited. Keep its
      // rejection handled while preserving the error when awaiting it below.
      session.catch(() => {});
      await page.goto(baseURL);
      await page.locator('#username').waitFor();
      const login = new URL(page.url());
      ensure(login.origin === identity.origin && login.pathname.startsWith(`${identity.pathname}/`), 'unexpected_login_origin_or_realm');
      await page.locator('#username').fill(account.username);
      await page.locator('#password').fill(account.password);
      await page.locator('#kc-login').click();
      const response = await session;
      ensure(response.ok(), 'participant_login_failed');
      actor.session = await response.json();
      ensure(actor.token, 'missing_authenticated_token');
      // Keep contexts for authenticated API requests, close pages to prevent autosaves.
      await page.close();
      onProgress(`Signed in (${actors.length}/${accounts.length}): ${account.username}`);
    }
    const api = async (actor, path, { workspace, method = 'GET', data } = {}) => {
      const response = await actor.context.request.fetch(`${baseURL}${path}`, { method, data,
        headers: { Authorization: actor.token, ...(workspace ? { 'X-InLumen-Workspace-Id': workspace } : {}) },
        maxRetries: 0, maxRedirects: 0, timeout: 30000 });
      ensure(response.status() === 200, `reset_api_http_${response.status()}`);
      return response.json();
    };
    const plan = await planWorkspaceReset(actors, api, { onProgress });
    const preview = plan.map(entry => ({ username: entry.actor.username, workspace_id: entry.workspace_id, name: entry.name }));
    onProgress(JSON.stringify({ mode: apply ? 'apply' : 'dry-run', users: accounts.length, workspaces: preview }, null, 2));
    if (!apply) return { applied: false, plan: preview };
    return { applied: true, results: await applyWorkspaceReset(plan, api, { onProgress }) };
  } finally { await browser.close(); }
}

async function main() {
  const { values } = parseArgs({ options: { url: { type: 'string' }, issuer: { type: 'string' },
    accounts: { type: 'string' }, apply: { type: 'boolean', default: false }, help: { type: 'boolean', default: false } } });
  if (values.help) {
    console.log('node loadtest/reset-workspaces.mjs --url https://inlumen.example --issuer https://identity.example/realms/inlumen --accounts loadtest/accounts.participants.local.json [--apply]\nDefault: preview only. --apply resets ALL owned workspaces for exactly the accounts in the file, cancelling active work first. Accounts, memberships and model caches are retained. Pause the session, wait for AI edits/uploads to finish, close inLUMEN tabs before applying, and reload afterwards.');
    return;
  }
  ensure(values.accounts, 'accounts_file_required');
  const raw = JSON.parse(await readFile(values.accounts, 'utf8'));
  const accounts = validateAccounts(raw, raw.length);
  const result = await resetParticipantWorkspaces({ baseURL: validatedURL(values.url), issuer: validatedURL(values.issuer), accounts, apply: values.apply });
  if (result.applied) console.log(`Reset completed: ${result.results.length} workspaces.`);
  else console.log('Preview complete. No workspace content was changed. Add --apply to reset this allowlist.');
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch(error => {
  console.error(`Reset stopped: ${safeFailure(error)}. Completed resets are not rolled back; rerun a preview before continuing. Credentials and server bodies are not logged.`);
  process.exitCode = 1;
});
