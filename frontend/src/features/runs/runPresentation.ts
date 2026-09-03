import type { PipelineRunEvent } from '@/features/runs/pipelineRuns';

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
