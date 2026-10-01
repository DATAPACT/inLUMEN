export const DEFAULT_PROMPT = 'Design a pipeline that reads an uploaded CSV of orders, removes duplicate rows, filters out rows with missing order IDs, calculates total sales per product, and writes a summary CSV. Create the connected pipeline on the canvas now. Do not generate code, run the pipeline, or call external data services.';
export const AUDIO_PROMPTS = [
  'Create a pipeline that transcribes an uploaded audio recording, analyzes its sentiment, and outputs the results.',
  'Extend the pipeline by adding named entity recognition after transcription, followed by data anonymization before sentiment analysis.',
];

export function validateAudioGraph(graph, extended = false) {
  validateGraph(graph);
  const roles = {};
  for (const node of graph.nodes) {
    const kind = String(node.data?.type || node.type || '').toLowerCase();
    const classify = text => [
      /transcrib|transcript|speech.?to.?text|\basr\b/.test(text) && 'transcription',
      /named.?entity|entity.?recognition|\bner\b/.test(text) && 'ner',
      /anonym|redact/.test(text) && 'anonymization',
      /sentiment/.test(text) && 'sentiment',
    ].filter(Boolean);
    const labelRoles = classify(String(node.data?.label || '').toLowerCase());
    const candidates = ['source', 'input'].includes(kind) ? ['source']
      : ['destination', 'output', 'sink'].includes(kind) ? ['destination']
      : kind === 'task' ? (labelRoles.length ? labelRoles : classify(String(node.data?.description || '').toLowerCase())) : [];
    ensure(candidates.length === 1 && !roles[candidates[0]], 'ambiguous_audio_pipeline_roles');
    roles[candidates[0]] = String(node.id);
  }
  const chain = extended ? ['source', 'transcription', 'ner', 'anonymization', 'sentiment', 'destination']
    : ['source', 'transcription', 'sentiment', 'destination'];
  ensure(graph.nodes.length === chain.length && graph.edges.length === chain.length - 1, 'unexpected_audio_pipeline_shape');
  ensure(chain.every(role => roles[role]), 'missing_audio_pipeline_task');
  ensure(chain.slice(1).every((role, i) => graph.edges.some(edge =>
    String(edge.source) === roles[chain[i]] && String(edge.target) === roles[role])), 'incorrect_audio_pipeline_order');
  return roles;
}

export class LoadTestError extends Error {
  constructor(code) { super(code); this.code = code; }
}
export function ensure(condition, code) { if (!condition) throw new LoadTestError(code); }

export function graphRevision(headers) {
  const explicit = headers['x-inlumen-graph-revision'];
  if (explicit != null) {
    ensure(/^\d+$/.test(explicit), 'invalid_graph_revision');
    return `"${explicit}"`;
  }
  const match = /^(?:W\/)?"(\d+)"$/.exec(headers.etag || '');
  ensure(match, 'missing_or_invalid_graph_revision');
  return `"${match[1]}"`;
}

export function positiveInteger(value, name, max = 10000) {
  const parsed = Number(value);
  ensure(Number.isInteger(parsed) && parsed > 0 && parsed <= max, `invalid_${name}`);
  return parsed;
}

export function validatedURL(value) {
  let url;
  try { url = new URL(value); } catch { throw new LoadTestError('invalid_url'); }
  ensure(url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)), 'https_required');
  ensure(!url.username && !url.password && !url.search && !url.hash, 'invalid_url');
  return url.href.replace(/\/$/, '');
}

export function validateAccounts(accounts, count) {
  ensure(Array.isArray(accounts) && accounts.length >= count, 'not_enough_accounts');
  const selected = accounts.slice(0, count);
  ensure(selected.every(a => a && typeof a.username === 'string' && a.username.trim() && typeof a.password === 'string' && a.password), 'invalid_account');
  ensure(new Set(selected.map(a => a.username.trim().toLowerCase())).size === count, 'duplicate_accounts');
  return selected;
}

export function validateSessions(sessions) {
  ensure(sessions.length > 0 && sessions.every(s => s?.user?.id && s.user.id !== 'local-user' && s.active_workspace_id && s.active_workspace_id !== 'local-workspace'), 'authenticated_users_required');
  ensure(sessions.every(s => !s.is_application_admin), 'use_non_admin_test_accounts');
  ensure(new Set(sessions.map(s => s.user.id)).size === sessions.length, 'duplicate_identities');
  ensure(new Set(sessions.map(s => s.active_workspace_id)).size === sessions.length, 'duplicate_initial_workspaces');
}

export function validateGraph(graph) {
  ensure(Array.isArray(graph?.nodes) && graph.nodes.length >= 2, 'empty_or_incomplete_graph');
  ensure(Array.isArray(graph.edges) && graph.edges.length > 0, 'unconnected_graph');
  const ids = new Set(graph.nodes.map(n => String(n.id)));
  ensure(ids.size === graph.nodes.length && graph.nodes.every(n => n.id != null), 'invalid_node_ids');
  ensure(graph.edges.every(e => ids.has(String(e.source)) && ids.has(String(e.target))), 'invalid_edge_reference');
}

export function summarize(results, requestedUsers) {
  const successful = results.filter(r => r.ok);
  const times = successful.map(r => r.elapsed_ms).sort((a, b) => a - b);
  const percentile = p => times.length ? times[Math.max(0, Math.ceil(times.length * p) - 1)] : null;
  const requests = results.flatMap(r => r.stages || [r]);
  const events = requests.filter(r => r.request_started_ms != null && r.request_finished_ms != null)
    .flatMap(r => [[r.request_started_ms, 1], [r.request_finished_ms, -1]])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let concurrent = 0, peak = 0;
  for (const [, delta] of events) { concurrent += delta; peak = Math.max(peak, concurrent); }
  return {
    requested_users: requestedUsers, completed_scenarios: results.length,
    successful: successful.length, failed: results.length - successful.length,
    success_rate: results.length ? successful.length / results.length : null,
    successful_latency_ms: { p50: percentile(0.5), p95: percentile(0.95), max: times.at(-1) ?? null },
    peak_observed_chat_requests: peak,
    stages: Object.fromEntries([...new Set(requests.map(r => r.phase).filter(Boolean))].map(phase => {
      const matching = requests.filter(r => r.phase === phase);
      const durations = matching.filter(r => r.ok).map(r => r.elapsed_ms).sort((a, b) => a - b);
      return [phase, { completed: matching.length, successful: matching.filter(r => r.ok).length,
        p50_ms: durations.length ? durations[Math.ceil(durations.length * .5) - 1] : null,
        p95_ms: durations.length ? durations[Math.ceil(durations.length * .95) - 1] : null }];
    })),
  };
}

export function safeFailure(error) {
  return error instanceof LoadTestError ? error.code : error?.name === 'TimeoutError' ? 'timeout' : 'browser_or_network_error';
}
