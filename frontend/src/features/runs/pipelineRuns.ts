import { INLUMEN_API_URL } from '@/config/api';
import { apiFetch } from '@/utils/apiFetch';


export type PipelineRunStatus =
  | 'queued'
  | 'preparing'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled';

export type PipelineRunRecord = {
  schema_version: 'inlumen.pipeline-run@1';
  run_id: string;
  status: PipelineRunStatus;
  engine: string;
  execution_mode: string;
  snapshot: {
    snapshot_id: string;
    graph_sha256: string;
    pipeline_id?: string | null;
    pipeline_version?: string | null;
    active_version_uid?: string | null;
    node_count: number;
    edge_count: number;
  };
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  cancel_requested_at?: string | null;
  event_cursor: number;
  progress?: {
    phase?: string | null;
    message?: string | null;
    active_node_id?: string | null;
    active_node_name?: string | null;
    node_elapsed_seconds?: number | null;
    heartbeat_at?: string | null;
    resource_profile?: string | null;
    resource_cpu?: number | null;
    resource_memory_bytes?: number | null;
    resource_reason?: string | null;
    queue_position?: number | null;
  } | null;
  error?: { code?: string; message?: string; details?: unknown } | null;
  result?: {
    status?: string;
    outputs?: Array<Record<string, unknown>>;
  } | null;
};

export type PipelineRunEvent = {
  id: number;
  timestamp: string;
  type: string;
  status?: PipelineRunStatus | null;
  message?: string | null;
  node_id?: string | null;
};

export type RunnerCapabilities = {
  background_runs: boolean;
  execution_available: boolean;
  adapter: string;
  execution_mode: string;
  max_outstanding_runs: number;
  outstanding_run_count: number;
  available_run_slots: number;
  summary_persistence: boolean;
  message?: string | null;
};

const responseJson = async <T>(response: Response, fallback: string): Promise<T> => {
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(String(payload.error || payload.detail || fallback));
  }
  return payload as T;
};

export const fetchRunnerCapabilities = async (): Promise<RunnerCapabilities> => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/pipeline-runs/capabilities`);
  return responseJson<RunnerCapabilities>(response, 'Failed to load pipeline runner capabilities.');
};

export const listPipelineRuns = async (limit = 20): Promise<PipelineRunRecord[]> => {
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/pipeline-runs?limit=${encodeURIComponent(limit)}`,
  );
  const payload = await responseJson<{ runs?: PipelineRunRecord[] }>(
    response,
    'Failed to load pipeline runs.',
  );
  return Array.isArray(payload.runs) ? payload.runs : [];
};

export const startPipelineRun = async (
  idempotencyKey: string,
): Promise<PipelineRunRecord> => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/pipeline-runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
  return responseJson<PipelineRunRecord>(response, 'Failed to start the pipeline run.');
};

export const getPipelineRun = async (runId: string): Promise<PipelineRunRecord> => {
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/pipeline-runs/${encodeURIComponent(runId)}`,
  );
  return responseJson<PipelineRunRecord>(response, 'Failed to refresh the pipeline run.');
};

export const cancelPipelineRun = async (runId: string): Promise<PipelineRunRecord> => {
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/pipeline-runs/${encodeURIComponent(runId)}`,
    { method: 'DELETE' },
  );
  return responseJson<PipelineRunRecord>(response, 'Failed to cancel the pipeline run.');
};

export const fetchPipelineRunEvents = async (
  runId: string,
  after = 0,
): Promise<{ events: PipelineRunEvent[]; next_cursor: number }> => {
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/pipeline-runs/${encodeURIComponent(runId)}/events?after=${encodeURIComponent(after)}`,
  );
  return responseJson<{ events: PipelineRunEvent[]; next_cursor: number }>(
    response,
    'Failed to load pipeline run events.',
  );
};

const downloadRunFile = async (url: string, fallbackName: string): Promise<void> => {
  const response = await apiFetch(url);
  if (!response.ok) {
    await responseJson(response, 'Failed to download run artifact.');
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const disposition = response.headers.get('Content-Disposition') || '';
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallbackName;
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(objectUrl);
};

export const downloadPipelineRunBundle = (runId: string): Promise<void> =>
  downloadRunFile(
    `${INLUMEN_API_URL}/api/pipeline-runs/${encodeURIComponent(runId)}/bundle`,
    `inlumen-dagster-run-${runId}.zip`,
  );

export const downloadPipelineRunOutput = (
  runId: string,
  path: string,
  filename: string,
): Promise<void> => downloadRunFile(
  `${INLUMEN_API_URL}/api/pipeline-runs/${encodeURIComponent(runId)}/outputs/${path.split('/').map(encodeURIComponent).join('/')}`,
  filename,
);

export const isActivePipelineRun = (status: PipelineRunStatus): boolean =>
  ['queued', 'preparing', 'running', 'cancelling'].includes(status);
