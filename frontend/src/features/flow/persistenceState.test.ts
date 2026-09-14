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
  expect(getPersistenceState()).toEqual({ pending: 0, error: null, conflict: false });
});
it('blocks further writes after a conflict until recovery', async () => {
  await expect(graphWrite(async () => Response.json({ code: 'graph_conflict' }, { status: 409 }))).rejects.toThrow('The saved graph has changed');
  const send = vi.fn();
  await expect(graphWrite(send)).rejects.toThrow('The saved graph has changed');
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

const savedGraph = () => ({ nodes: [{ id: '1', position: { x: 10, y: 20 }, data: { label: 'Original' } }], edges: [] });
const acceptGraph = (graph = savedGraph(), revision = '"1"') => {
  rememberGraphRead(graph, Response.json(graph, { headers: { ETag: revision } }), persistenceEpoch());
  acknowledgeGraphRead(graph);
};
const conflict = () => Response.json({ code: 'graph_conflict' }, { status: 409 });

it('recovers a bookkeeping-only revision change without discarding local edits', async () => {
  acceptGraph();
  const send = vi.fn().mockResolvedValueOnce(conflict()).mockResolvedValueOnce(new Response('{}', { headers: { ETag: '"3"' } }));
  const readGraph = vi.fn().mockResolvedValue(Response.json({ ...savedGraph(), updated_at: 'later' }, { headers: { ETag: '"2"' } }));
  await graphWrite(send, { readGraph });
  expect(send.mock.calls.map(([revision]) => revision)).toEqual(['"1"', '"2"']);
  expect(getPersistenceState().error).toBeNull();
});

it('does not retry over a real competing edit or acknowledge its revision', async () => {
  acceptGraph();
  const remote = savedGraph();
  remote.nodes[0].data.label = 'Changed in another tab';
  const send = vi.fn().mockResolvedValue(conflict());
  await expect(graphWrite(send, { readGraph: async () => Response.json(remote, { headers: { ETag: '"2"' } }) })).rejects.toThrow('Your edits are still here');
  expect(send).toHaveBeenCalledTimes(1);
  expect(getPersistenceState().conflict).toBe(true);
});

it('does not retry after an intervening local write invalidates the saved baseline', async () => {
  acceptGraph();
  await graphWrite(async () => new Response('{}', { headers: { ETag: '"2"' } }));
  const readGraph = vi.fn();
  await expect(graphWrite(async () => conflict(), { readGraph })).rejects.toThrow('saved graph');
  expect(readGraph).not.toHaveBeenCalled();
});

it('does not freeze editing after a duplicate-name or other business conflict', async () => {
  const response = await graphWrite(async () => Response.json({ error: 'Version name already exists' }, { status: 409 }));
  expect(response.status).toBe(409);
  const send = vi.fn().mockResolvedValue(new Response('{}'));
  await graphWrite(send);
  expect(send).toHaveBeenCalledOnce();
  expect(getPersistenceState().error).toBeNull();
});

it('bounds automatic conflict recovery to one retry', async () => {
  acceptGraph();
  const send = vi.fn().mockResolvedValue(conflict());
  const readGraph = vi.fn().mockResolvedValue(Response.json(savedGraph(), { headers: { ETag: '"2"' } }));
  await expect(graphWrite(send, { readGraph })).rejects.toThrow('saved graph');
  expect(send).toHaveBeenCalledTimes(2);
  expect(readGraph).toHaveBeenCalledOnce();
});

it('does not acknowledge a changed active version even when its nodes match', async () => {
  const graph = { ...savedGraph(), pipeline: { active_version_uid: 'a' } };
  rememberGraphRead(graph, Response.json(graph, { headers: { ETag: '"1"' } }), persistenceEpoch());
  acknowledgeGraphRead(graph);
  const readGraph = async () => Response.json({ ...graph, pipeline: { active_version_uid: 'b' } }, { headers: { ETag: '"2"' } });
  const send = vi.fn().mockResolvedValue(conflict());
  await expect(graphWrite(send, { readGraph })).rejects.toThrow('saved graph');
  expect(send).toHaveBeenCalledOnce();
});

it('ignores recovery responses and pending counters from a previous workspace', async () => {
  acceptGraph();
  let resolveRead!: (value: Response) => void;
  const readGraph = vi.fn(() => new Promise<Response>(resolve => { resolveRead = resolve; }));
  const oldWrite = graphWrite(async () => conflict(), { readGraph });
  const rejected = expect(oldWrite).rejects.toThrow('workspace changed');
  await vi.waitFor(() => expect(readGraph).toHaveBeenCalledOnce());
  resetPersistence();
  let resolveWrite!: (value: Response) => void;
  const newWrite = graphWrite(() => new Promise<Response>(resolve => { resolveWrite = resolve; }));
  resolveRead(Response.json(savedGraph(), { headers: { ETag: '"99"' } }));
  await rejected;
  expect(getPersistenceState()).toEqual({ pending: 1, error: null, conflict: false });
  resolveWrite(new Response('{}'));
  await newWrite;
});

it('rejects an older background graph before it can replace the current canvas', () => {
  acceptGraph(savedGraph(), '"3"');
  const stale = savedGraph();
  rememberGraphRead(stale, Response.json(stale, { headers: { ETag: '"2"' } }), persistenceEpoch());
  expect(acknowledgeGraphRead(stale)).toBe(false);
});

it('uses the dedicated graph revision instead of an encoding-specific ETag', async () => {
  captureGraphRevision(new Response('{}', { headers: { ETag: 'W/"encoded-representation"', 'X-InLumen-Graph-Revision': '42' } }));
  await graphWrite(async (etag) => {
    expect(etag).toBe('"42"');
    return new Response('{}');
  });
});

it.each(['W/"object-checksum"', '"-1"', 'W/"1.5"'])('does not treat arbitrary validators as graph revisions: %s', async (etag) => {
  captureGraphRevision(new Response('{}', { headers: { ETag: etag } }));
  await graphWrite(async (revision) => { expect(revision).toBeNull(); return new Response('{}'); });
});
