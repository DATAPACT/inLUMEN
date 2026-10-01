import { readFile } from 'node:fs/promises';
import { basename } from 'node:path';
import { createHash } from 'node:crypto';
import JSZip from 'jszip';
import { expect } from '@playwright/test';
import { ensure, graphRevision, safeFailure } from './core.mjs';

const outputRoles = { 'transcription.json': 'transcription', 'entities.json': 'ner',
  'anonymized.json': 'anonymization', 'sentiment.json': 'sentiment' };

export async function loadSessionAssets(codeZip, audioFile) {
  ensure(codeZip && audioFile, 'audio_session_requires_code_zip_and_audio_file');
  const [code, audio] = await Promise.all([readFile(codeZip), readFile(audioFile)]);
  ensure(code.length < 50 * 1024 * 1024, 'code_zip_too_large');
  ensure(basename(audioFile).toLowerCase().endsWith('.wav') && audio.length <= 50 * 1024 * 1024,
    'use_session_wav_under_50_mb');
  const archive = await JSZip.loadAsync(code);
  const folders = {};
  const manifests = Object.values(archive.files).filter(f => f.name.endsWith('/inlumen.task.json'));
  ensure(manifests.length === 4, 'session_zip_requires_four_tasks');
  for (const file of manifests) {
    const metadata = JSON.parse(await file.async('string'));
    const role = outputRoles[metadata.output?.path];
    ensure(role && !Object.values(folders).includes(role), 'unrecognized_session_task_output');
    const folder = file.name.slice(0, -'/inlumen.task.json'.length);
    ensure(archive.file(`${folder}/main.py`), 'session_task_missing_main');
    folders[folder] = role;
  }
  const sha256 = buffer => createHash('sha256').update(buffer).digest('hex');
  return { code, audio, folders, audioName: basename(audioFile),
    report: { code_zip_sha256: sha256(code), audio_sha256: sha256(audio),
      code_zip_bytes: code.length, audio_bytes: audio.length } };
}

// Both uploads use the normal application paths. Code review and Run are driven
// through the UI; audio uses the authenticated Source attachment API.
export async function runAudioSession({ actor, round, assets, api, runTimeoutMs, pollMs, allocationLimits, onStage, onProgress, isAppResponse, beforeRun }) {
  const { page } = actor;
  let runId;
  let terminal = false;
  async function stage(phase, operation) {
    const result = { phase, ok: false };
    const begin = Date.now();
    try { await operation(result); result.ok = true; }
    catch (error) { result.failure = safeFailure(error); throw error; }
    finally { result.elapsed_ms = Date.now() - begin; onStage(result); }
  }
  const responseFor = path => page.waitForResponse(r => isAppResponse(r) && r.request().method() === 'POST' && new URL(r.url()).pathname === path,
    { timeout: 30000 }).catch(() => null);
  await stage('import_code_zip', async result => {
    const library = page.getByRole('button', { name: 'Library', exact: true });
    if (await library.getAttribute('aria-pressed') !== 'true') await library.click();
    await page.getByRole('tab', { name: 'Run', exact: true }).click();
    await page.getByRole('button', { name: 'Upload code ZIP', exact: true }).click();
    const checked = responseFor('/api/pipeline/task-packages/validate');
    await page.locator('input[accept=".zip,application/zip"]').setInputFiles({ name: 'session-code.zip', mimeType: 'application/zip', buffer: assets.code });
    const initial = await checked;
    ensure(initial, 'zip_validation_timeout');
    ensure(initial.ok(), `zip_validation_http_${initial.status()}`);
    const initialReport = await initial.json();
    ensure(initialReport.packages?.length === 4, 'incorrect_import_task_count');
    const dialog = page.getByRole('dialog', { name: 'Review code ZIP' });
    for (const [folder, role] of Object.entries(assets.folders)) {
      const target = actor.roles[role];
      const selector = dialog.getByRole('combobox', { name: `Target Task for ${folder}`, exact: true });
      if (await selector.inputValue() !== target) {
        const rechecked = responseFor('/api/pipeline/task-packages/validate');
        await selector.selectOption(target);
        ensure((await rechecked)?.ok(), 'zip_mapping_validation_failed');
        await expect(selector).toHaveValue(target);
      }
    }
    const reviewed = responseFor('/api/pipeline/task-packages/validate');
    await dialog.getByRole('button', { name: 'Revalidate', exact: true }).click();
    const reviewedResponse = await reviewed;
    ensure(reviewedResponse?.ok(), 'zip_revalidation_failed');
    const review = await reviewedResponse.json();
    ensure(review.valid === true && review.packages.every(pkg => pkg.node_id === actor.roles[assets.folders[pkg.folder]]), 'zip_review_failed');
    const imported = responseFor('/api/pipeline/task-packages/import');
    await dialog.getByRole('button', { name: 'Import 4 Task packages', exact: true }).click();
    const response = await imported;
    ensure(response, 'zip_import_timeout_outcome_unknown');
    ensure(response.ok(), `zip_import_http_${response.status()}`);
    await expect(dialog).toBeHidden();
    result.package_digest = review.digest;
    result.mappings = Object.fromEntries(Object.entries(assets.folders).map(([folder, role]) => [folder, actor.roles[role]]));
  });
  await stage('upload_audio', async result => {
    const graph = await api(actor, '/api/pipeline/graph');
    ensure(graph.ok(), 'audio_upload_requires_graph_revision');
    const revision = graphRevision(graph.headers());
    const response = await api(actor, `/api/nodes/${encodeURIComponent(actor.roles.source)}/files`, {
      method: 'POST', headers: { 'If-Match': revision },
      multipart: { role: 'data', file: { name: assets.audioName, mimeType: 'audio/wav', buffer: assets.audio } },
    });
    ensure(response.ok(), `audio_upload_http_${response.status()}`);
    result.bytes = assets.audio.length;
    // Refresh the Run panel after its Source attachment changes.
    await page.reload();
    await page.getByRole('button', { name: 'Library', exact: true }).waitFor();
    const library = page.getByRole('button', { name: 'Library', exact: true });
    if (await library.getAttribute('aria-pressed') !== 'true') await library.click();
    await page.getByRole('tab', { name: 'Run', exact: true }).click();
  });
  if (beforeRun) await beforeRun();
  await stage('execute_pipeline', async result => {
    const begin = Date.now();
    const accepted = responseFor('/api/pipeline-runs');
    await page.getByRole('button', { name: 'Run current pipeline', exact: true }).click();
    const response = await accepted;
    ensure(response, 'run_submission_timeout_outcome_unknown');
    result.http_status = response.status();
    if (response.status() !== 202) terminal = response.status() >= 400 && response.status() < 500;
    ensure(response.status() === 202, `run_submission_http_${response.status()}`);
    const record = await response.json();
    ensure(record.run_id, 'missing_run_id');
    runId = result.run_id = record.run_id;
    result.observed_queue_ms = 0;
    let current = record, lastSample = Date.now();
    const statuses = new Set();
    let previousPhase;
    while (true) {
      statuses.add(current.status);
      if (['succeeded', 'partial', 'failed', 'cancelled'].includes(current.status)) break;
      ensure(Date.now() - begin < runTimeoutMs, 'pipeline_timeout_outcome_unknown');
      ensure(['queued', 'preparing', 'running', 'cancelling'].includes(current.status), 'unknown_run_status');
      const phase = current.progress?.phase || current.status;
      if (phase !== previousPhase) onProgress(`User ${actor.index + 1}, round ${round}: ${phase}.`);
      previousPhase = phase;
      await new Promise(resolve => setTimeout(resolve, pollMs));
      const refreshed = await api(actor, `/api/pipeline-runs/${encodeURIComponent(runId)}`);
      ensure(refreshed.ok(), `run_poll_http_${refreshed.status()}`);
      const now = Date.now();
      if (current.status === 'queued' || current.progress?.phase === 'waiting_for_capacity') result.observed_queue_ms += now - lastSample;
      lastSample = now;
      current = await refreshed.json();
      result.resource_cpu = current.progress?.resource_cpu ?? result.resource_cpu;
      result.resource_memory_bytes = current.progress?.resource_memory_bytes ?? result.resource_memory_bytes;
    }
    result.created_at = current.created_at;
    result.admitted_at = current.progress?.admitted_at || null;
    result.finished_at = current.finished_at;
    result.queue_wait_ms = result.admitted_at ? Math.max(0, Date.parse(result.admitted_at) - Date.parse(current.created_at)) : null;
    result.worker_execution_ms = result.admitted_at ? Math.max(0, Date.parse(current.finished_at) - Date.parse(result.admitted_at)) : null;
    result.observed_statuses = [...statuses];
    terminal = true;
    result.status = current.status;
    ensure(current.status === 'succeeded', `pipeline_${current.status}`);
    ensure(result.resource_cpu != null && result.resource_memory_bytes != null, 'missing_execution_allocation');
    ensure(result.resource_cpu <= allocationLimits.cpu && result.resource_memory_bytes <= allocationLimits.memory_bytes, 'execution_allocation_exceeds_configured_limits');
    const artifacts = {};
    for (const filename of Object.keys(outputRoles)) {
      const outputs = (current.result?.outputs || []).filter(o => o.filename === filename || o.path?.split('/').at(-1) === filename);
      ensure(outputs.length > 0 && outputs.every(output => output.path)
        && new Set(outputs.map(output => output.path)).size === outputs.length, `missing_or_duplicate_${filename.replace('.', '_')}`);
      let canonical;
      for (const entry of outputs) {
        const output = await api(actor, `/api/pipeline-runs/${encodeURIComponent(runId)}/outputs/${entry.path.split('/').map(encodeURIComponent).join('/')}`);
        ensure(output.ok(), 'artifact_download_failed');
        const bytes = await output.body();
        // Destinations may publish a copy of the upstream Task artifact.
        ensure(!canonical || bytes.equals(canonical), `conflicting_${filename.replace('.', '_')}`);
        canonical = bytes;
      }
      artifacts[filename] = JSON.parse(canonical.toString('utf8'));
    }
    const transcript = artifacts['transcription.json'], entities = artifacts['entities.json'];
    const anonymized = artifacts['anonymized.json'], sentiment = artifacts['sentiment.json'];
    ensure(typeof transcript.text === 'string' && transcript.text.trim() && Array.isArray(transcript.chunks), 'invalid_transcription_output');
    ensure(entities.text === transcript.text && Array.isArray(entities.entities), 'invalid_ner_handoff');
    ensure(typeof anonymized.text === 'string' && Array.isArray(anonymized.redactions) && anonymized.entity_count === entities.entities.length, 'invalid_anonymization_output');
    ensure(!('transcription' in anonymized) && !('chunks' in anonymized) && !('entities' in anonymized), 'original_text_retained_after_anonymization');
    ensure(sentiment.anonymized_text === anonymized.text && ['positive', 'neutral', 'negative'].includes(sentiment.sentiment?.label), 'invalid_sentiment_handoff');
    result.artifacts_verified = Object.keys(artifacts);
    result.entity_count = entities.entities.length;
    result.redaction_count = anonymized.redactions.length;
  }).catch(error => { actor.runOutcomeMayBeRunning = !terminal; actor.runId = runId; throw error; });
}
