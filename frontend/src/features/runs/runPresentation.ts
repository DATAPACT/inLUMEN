import type { PipelineRunEvent, PipelineRunRecord } from '@/features/runs/pipelineRuns';

export const isWaitingForExecution = (run: PipelineRunRecord) =>
  ['queued', 'preparing', 'running'].includes(run.status)
  && !run.progress?.admitted_at
  && ['queued', 'accepted', 'pending', 'selecting_resources', 'waiting_for_capacity'].includes(run.progress?.phase || 'queued');

export const presentRunPhase = (run: PipelineRunRecord) => {
  if (run.status === 'succeeded') return 'Completed';
  if (run.status === 'failed' || run.status === 'partial') return 'Failed';
  if (run.status === 'cancelled') return 'Cancelled';
  if (run.status === 'cancelling') return 'Cancelling';
  if (isWaitingForExecution(run)) return 'Queued';
  if (run.progress?.phase === 'building_runtime') return 'Preparing runtime';
  if (run.progress?.phase === 'prefetching_models') return 'Loading models';
  if (run.progress?.active_node_name) return run.progress.active_node_name;
  if (run.progress?.phase === 'completed') return 'Collecting results';
  return 'Processing pipeline';
};

export type RunOutput = Record<string, unknown>;

export type PresentedRunOutput = {
  path: string;
  filename: string;
  kind: string;
  contentType: string;
  sizeBytes: number | null;
  copies: number;
  source: RunOutput;
};

const INTERNAL_OUTPUT_NAMES = new Set([
  'output_manifest.json',
  'node-output-manifest.json',
  'validation-report.json',
]);

const normalizeOutput = (output: RunOutput, index: number): PresentedRunOutput | null => {
  const path = String(output.path || '').trim();
  if (!path) return null;
  const filename = String(
    output.filename || path.split('/').pop() || `output-${index + 1}`,
  ).trim();
  const rawSize = Number(output.size_bytes);
  return {
    path,
    filename,
    kind: String(output.kind || output.format || '').trim(),
    contentType: String(output.content_type || '').trim(),
    sizeBytes: Number.isFinite(rawSize) && rawSize >= 0 ? rawSize : null,
    copies: 1,
    source: output,
  };
};

export const isInternalRunOutput = (output: PresentedRunOutput) => {
  const filename = output.filename.toLowerCase();
  return INTERNAL_OUTPUT_NAMES.has(filename)
    || filename.startsWith(':memory:')
    || filename.endsWith('.ses')
    || filename.endsWith('.sqlite')
    || filename.endsWith('.sqlite3');
};

export const presentRunOutputs = (outputs: RunOutput[]) => {
  const uniqueByPath = new Map<string, PresentedRunOutput>();
  outputs.forEach((output, index) => {
    const normalized = normalizeOutput(output, index);
    if (normalized && !uniqueByPath.has(normalized.path)) {
      uniqueByPath.set(normalized.path, normalized);
    }
  });
  const all = [...uniqueByPath.values()];
  const primaryByName = new Map<string, PresentedRunOutput>();
  all.filter((output) => !isInternalRunOutput(output)).forEach((output) => {
    const key = output.filename.toLocaleLowerCase();
    const existing = primaryByName.get(key);
    if (existing) {
      existing.copies += 1;
      return;
    }
    primaryByName.set(key, { ...output });
  });
  return {
    primary: [...primaryByName.values()],
    all,
    hiddenCount: Math.max(0, all.length - primaryByName.size),
  };
};

export const summarizeNodeEvents = (events: PipelineRunEvent[]) => {
  const latestByNode = new Map<string, PipelineRunEvent>();
  const unscoped: PipelineRunEvent[] = [];
  events.forEach((event) => {
    const nodeId = String(event.node_id || '').trim();
    if (nodeId) latestByNode.set(nodeId, event);
    else unscoped.push(event);
  });
  return [...unscoped, ...latestByNode.values()]
    .sort((left, right) => left.id - right.id)
    .slice(-8);
};

export const formatOutputSize = (sizeBytes: number | null) => {
  if (sizeBytes == null) return '';
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 ** 2) return `${(sizeBytes / 1024).toFixed(1)} KB`;
  return `${(sizeBytes / (1024 ** 2)).toFixed(1)} MB`;
};


export const presentRunFailure = (message: string, logs: string[] = []) => {
  const details = [message, ...logs].join('\n').toLowerCase();
  if (/no space left on device|disk quota exceeded|runtime storage is full/.test(details)) {
    return {
      message: 'Runtime storage ran out of space while writing downloaded files or temporary data.',
      hint: 'Check the runtime cache and temporary-storage capacity, then start a new run.',
    };
  }
  const normalized = message.toLowerCase();
  if (/artifact contract|artifactcontracterror|artifact declaration|incompatible artifact connection/.test(details)) {
    return {
      message,
      hint: 'A step did not satisfy its declared artifact contract. Check the producer and connection named above. Regenerate older task code before starting a new run.',
    };
  }
  if (normalized.includes('huggingface') || normalized.includes('cached files')) {
    return { message, hint: 'Model loading failed. Check Technical logs for download, storage, or model-file errors.' };
  }
  if (normalized.includes('no csv') || normalized.includes('no .wav') || normalized.includes('pipeline_input_dir')) {
    return { message, hint: 'Check that the source node has the expected input file and that the task reads it directly from PIPELINE_INPUT_DIR.' };
  }
  if (normalized.includes('environment variable') || normalized.includes('keyerror')) {
    return { message, hint: 'Open the task Inspector and configure the runtime environment value reported by the script.' };
  }
  return { message, hint: 'Open Technical logs for the full Dagster trace. The tested snapshot is also available below for local debugging.' };
};
