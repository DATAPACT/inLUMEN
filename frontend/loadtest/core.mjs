export const DEFAULT_PROMPT = 'Design a pipeline that reads an uploaded CSV of orders, removes duplicate rows, filters out rows with missing order IDs, calculates total sales per product, and writes a summary CSV. Create the connected pipeline on the canvas now. Do not generate code, run the pipeline, or call external data services.';

export class LoadTestError extends Error {
  constructor(code) { super(code); this.code = code; }
}
export function ensure(condition, code) { if (!condition) throw new LoadTestError(code); }

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
  const events = results.filter(r => r.request_started_ms != null && r.request_finished_ms != null)
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
  };
}

export function safeFailure(error) {
  return error instanceof LoadTestError ? error.code : error?.name === 'TimeoutError' ? 'timeout' : 'browser_or_network_error';
}
