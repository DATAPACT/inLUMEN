import { isActivePipelineRun, type PipelineRunEvent, type PipelineRunRecord, type PipelineRunStatus } from '@/features/runs/pipelineRuns';

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
  sourceNode: string;
  nodeLabel: string;
  port: string;
  role: 'final' | 'intermediate' | '';
  source: RunOutput;
};

export type RunOutputStepGroup = {
  key: string;
  label: string;
  role: PresentedRunOutput['role'];
  outputs: PresentedRunOutput[];
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
  const role = String(output.output_role || '').trim();
  return {
    path,
    filename,
    kind: String(output.kind || output.format || '').trim(),
    contentType: String(output.content_type || '').trim(),
    sizeBytes: Number.isFinite(rawSize) && rawSize >= 0 ? rawSize : null,
    copies: 1,
    sourceNode: String(output.source_node || '').trim(),
    nodeLabel: String(output.node_label || '').trim(),
    port: String(output.port || '').trim(),
    role: role === 'final' || role === 'intermediate' ? role : '',
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
    // Same-named files from different steps are different results.
    const key = `${output.sourceNode}\u0000${output.filename.toLocaleLowerCase()}`;
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

const stepLabel = (output: PresentedRunOutput) =>
  output.nodeLabel || (output.sourceNode ? `Step ${output.sourceNode}` : 'Other outputs');

/** Group outputs by the step that produced them, final results first. */
export const groupRunOutputsByStep = (outputs: PresentedRunOutput[]): RunOutputStepGroup[] => {
  const groups = new Map<string, RunOutputStepGroup>();
  outputs.forEach((output) => {
    const key = output.sourceNode || '';
    const group = groups.get(key) || {
      key,
      label: stepLabel(output),
      role: output.role,
      outputs: [],
    };
    group.outputs.push(output);
    groups.set(key, group);
  });
  const rank = (group: RunOutputStepGroup) => (
    group.role === 'final' ? 0 : group.role === 'intermediate' ? 1 : 2
  );
  return [...groups.values()].sort((left, right) => rank(left) - rank(right));
};

export const outputsForNode = (outputs: RunOutput[], nodeId: string) =>
  presentRunOutputs(outputs).all.filter(
    (output) => output.sourceNode === nodeId && !isInternalRunOutput(output),
  );

export const summarizeNodeEvents = (events: PipelineRunEvent[], runStatus?: PipelineRunStatus) => {
  const latestByNode = new Map<string, PipelineRunEvent>();
  const cachedNodes = new Set<string>();
  const unscoped: PipelineRunEvent[] = [];
  events.forEach((event) => {
    const nodeId = String(event.node_id || '').trim();
    if (!nodeId) {
      unscoped.push(event);
      return;
    }
    if (event.type === 'node.cached') cachedNodes.add(nodeId);
    // A reused step also reports completion; keep saying it was reused.
    const keep = cachedNodes.has(nodeId) && event.type === 'node.completed'
      ? latestByNode.get(nodeId) || event
      : event;
    latestByNode.set(nodeId, keep);
  });
  // Once the run has finished, a step still shown as started can no longer be
  // running. A succeeded run means every step finished. Otherwise we cannot
  // tell which unfinished steps failed (a failing step has its own failed
  // event), so show them with the neutral cancelled state instead of guessing.
  const settled = (event: PipelineRunEvent): PipelineRunEvent => {
    if (event.status !== 'running' || !event.node_id || !runStatus || isActivePipelineRun(runStatus)) {
      return event;
    }
    const name = event.message?.replace(/\s+started\.?$/i, '').trim() || String(event.node_id);
    if (runStatus === 'succeeded') {
      return { ...event, type: 'node.completed', status: 'succeeded', message: `${name} completed.` };
    }
    return { ...event, type: 'node.cancelled', status: 'cancelled', message: `${name} did not finish.` };
  };
  return [...unscoped, ...[...latestByNode.values()].map(settled)]
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
