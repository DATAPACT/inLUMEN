import { describe, expect, it } from 'vitest';

import {
  formatOutputSize,
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
