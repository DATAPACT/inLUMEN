import { describe, expect, it } from 'vitest';

import {
  formatOutputSize,
  groupRunOutputsByStep,
  outputsForNode,
  presentRunOutputs,
  presentRunFailure,
  summarizeNodeEvents,
} from '@/features/runs/runPresentation';

describe('run result presentation', () => {
  it('shows storage exhaustion hidden by an old model-loading error', () => {
    const failure = presentRunFailure(
      "OSError: Can't load model from huggingface.co/models",
      ['RuntimeError: File reconstruction error: No space left on device (os error 28)'],
    );
    expect(failure.message).toContain('ran out of space');
    expect(failure.hint).toContain('temporary-storage capacity');
    expect(failure.hint).not.toContain('pinned');
  });

  it('does not assume every model-loading failure means the model is unavailable', () => {
    expect(presentRunFailure('OSError: huggingface model load failed').hint)
      .toContain('download, storage, or model-file errors');
  });

  it('keeps internal artifacts out of primary results and consolidates repeated names', () => {
    const presented = presentRunOutputs([
      { path: 'outputs/one/result.wav', filename: 'result.wav', size_bytes: 2048 },
      { path: 'outputs/two/result.wav', filename: 'result.wav', size_bytes: 2048 },
      { path: 'outputs/two/output_manifest.json', filename: 'output_manifest.json' },
      { path: 'outputs/two/:memory:.ses', filename: ':memory:.ses' },
      { path: 'outputs/one/result.wav', filename: 'result.wav', size_bytes: 2048 },
    ]);

    expect(presented.primary).toHaveLength(1);
    expect(presented.primary[0]).toMatchObject({ filename: 'result.wav', copies: 2 });
    expect(presented.all).toHaveLength(4);
    expect(presented.hiddenCount).toBe(3);
  });

  it('keeps only the latest activity for each identified node', () => {
    const events = summarizeNodeEvents([
      { id: 1, timestamp: '', type: 'node.started', node_id: 'a' },
      { id: 2, timestamp: '', type: 'node.started', node_id: 'b' },
      { id: 3, timestamp: '', type: 'node.succeeded', node_id: 'a' },
    ]);

    expect(events.map((event) => event.id)).toEqual([2, 3]);
  });

  it('formats output sizes for compact metadata', () => {
    expect(formatOutputSize(512)).toBe('512 B');
    expect(formatOutputSize(2048)).toBe('2.0 KB');
    expect(formatOutputSize(null)).toBe('');
  });
});

it('explains artifact boundary failures without hiding the connection', () => {
  const message = 'Artifact contract violation at transcription: missing transcript.json';
  const result = presentRunFailure(message);
  expect(result.message).toBe(message);
  expect(result.hint).toContain('declared artifact contract');
});

import { isWaitingForExecution, presentRunPhase } from './runPresentation';
import type { PipelineRunRecord } from './pipelineRuns';
it('shows a waiting worker as queued even if the lifecycle status is running', () => {
  const run = { status: 'running', progress: { phase: 'waiting_for_capacity', queue_position: 12 } } as PipelineRunRecord;
  expect(isWaitingForExecution(run)).toBe(true);
  expect(presentRunPhase(run)).toBe('Queued');
  run.progress = { phase: 'prefetching_models', admitted_at: '2026-10-01T12:00:00Z' };
  expect(isWaitingForExecution(run)).toBe(false);
  expect(presentRunPhase(run)).toBe('Loading models');
  run.status = 'cancelled';
  expect(presentRunPhase(run)).toBe('Cancelled');
});

describe('run outputs per step', () => {
  const outputs = [
    { path: 'outputs/node-1/clean.csv', filename: 'clean.csv', source_node: '1', node_label: 'Clean', output_role: 'intermediate' },
    { path: 'outputs/node-2/result.csv', filename: 'result.csv', source_node: '2', node_label: 'Report', output_role: 'final' },
    { path: 'outputs/node-3/result.csv', filename: 'result.csv', source_node: '3', node_label: 'Archive', output_role: 'final' },
    { path: 'outputs/node-2/output_manifest.json', filename: 'output_manifest.json', source_node: '2' },
  ];

  it('keeps same-named files from different steps apart', () => {
    const presented = presentRunOutputs(outputs);
    expect(presented.primary.map((output) => output.path)).toEqual([
      'outputs/node-1/clean.csv',
      'outputs/node-2/result.csv',
      'outputs/node-3/result.csv',
    ]);
    expect(presented.primary[1]).toMatchObject({ sourceNode: '2', nodeLabel: 'Report', role: 'final', copies: 1 });
  });

  it('groups outputs by producing step with final results first', () => {
    const groups = groupRunOutputsByStep(presentRunOutputs(outputs).all);
    expect(groups.map((group) => [group.label, group.role, group.outputs.length])).toEqual([
      ['Report', 'final', 2],
      ['Archive', 'final', 1],
      ['Clean', 'intermediate', 1],
    ]);
  });

  it('selects a canvas node outputs without runtime manifests', () => {
    expect(outputsForNode(outputs, '2').map((output) => output.filename)).toEqual(['result.csv']);
    expect(outputsForNode(outputs, 'missing')).toEqual([]);
  });
});

describe('reused steps', () => {
  it('keeps saying a step reused an earlier result after it completes', () => {
    const events = summarizeNodeEvents([
      { id: 1, timestamp: '', type: 'node.started', node_id: 'a' },
      { id: 2, timestamp: '', type: 'node.cached', node_id: 'a', message: 'A reused an earlier result.' },
      { id: 3, timestamp: '', type: 'node.completed', node_id: 'a' },
      { id: 4, timestamp: '', type: 'node.completed', node_id: 'b' },
    ]);
    expect(events.map((event) => event.type)).toEqual(['node.cached', 'node.completed']);
  });
});
