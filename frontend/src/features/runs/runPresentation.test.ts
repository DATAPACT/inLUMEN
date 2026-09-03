import { describe, expect, it } from 'vitest';

import {
  formatOutputSize,
  presentRunOutputs,
  summarizeNodeEvents,
} from '@/features/runs/runPresentation';

describe('run result presentation', () => {
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
