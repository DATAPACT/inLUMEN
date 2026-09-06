import { beforeEach, expect, it, vi } from 'vitest';
import { captureGraphRevision, getPersistenceState, graphWrite, resetPersistence, persistenceEpoch, rememberGraphRead, acknowledgeGraphRead } from './persistenceState';

beforeEach(resetPersistence);
it('serializes writes and advances the acknowledged revision', async () => {
  captureGraphRevision(new Response('{}', { headers: { ETag: '"1"' } }));
  const seen: (string | null)[] = [];
  await Promise.all([2, 3].map((revision) => graphWrite(async (etag) => {
    seen.push(etag);
    return new Response('{}', { headers: { ETag: `"${revision}"` } });
  })));
  expect(seen).toEqual(['"1"', '"2"']);
  expect(getPersistenceState()).toEqual({ pending: 0, error: null });
});
it('blocks further writes after a conflict until recovery', async () => {
  await expect(graphWrite(async () => new Response('{}', { status: 409 }))).rejects.toThrow('Another session');
  const send = vi.fn();
  await expect(graphWrite(send)).rejects.toThrow('Another session');
  expect(send).not.toHaveBeenCalled();
  expect(getPersistenceState().pending).toBe(0);
});

it('ignores stale revisions and responses from previous workspaces', async () => {
  const oldEpoch = persistenceEpoch();
  resetPersistence();
  captureGraphRevision(new Response('{}', { headers: { ETag: '"90"' } }), oldEpoch);
  captureGraphRevision(new Response('{}', { headers: { ETag: '"3"' } }));
  captureGraphRevision(new Response('{}', { headers: { ETag: '"2"' } }));
  await graphWrite(async (etag) => {
    expect(etag).toBe('"3"');
    return new Response('{}');
  });
});

it('advances read revisions only after the canvas accepts the graph', async () => {
  captureGraphRevision(new Response('{}', { headers: { ETag: '"1"' } }));
  const graph = {};
  rememberGraphRead(graph, new Response('{}', { headers: { ETag: '"4"' } }), persistenceEpoch());
  await graphWrite(async (etag) => { expect(etag).toBe('"1"'); return new Response('{}'); });
  acknowledgeGraphRead(graph);
  await graphWrite(async (etag) => { expect(etag).toBe('"4"'); return new Response('{}'); });
});
