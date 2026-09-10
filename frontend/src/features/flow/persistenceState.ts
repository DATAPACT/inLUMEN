export type PersistenceState = { pending: number; error: string | null; conflict: boolean };
let state: PersistenceState = { pending: 0, error: null, conflict: false };
let revision: string | null = null;
let serial: Promise<unknown> = Promise.resolve();
let generation = 0;
let writeGeneration = 0;
let acknowledgedGraph: { signature: string; revision: string } | null = null;
const listeners = new Set<() => void>();
const publish = (next: PersistenceState) => { state = next; listeners.forEach((listener) => listener()); };

class GraphSaveError extends Error {
  constructor(message: string, readonly conflict = false, readonly epoch = generation) { super(message); }
}

export const subscribePersistence = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const getPersistenceState = () => state;
export const getGraphRevision = () => revision;
export const resetPersistence = () => {
  generation += 1;
  revision = null;
  acknowledgedGraph = null;
  serial = Promise.resolve();
  publish({ pending: 0, error: null, conflict: false });
};
export const clearPersistenceError = () => publish({ ...state, error: null, conflict: false });
export const reportPersistenceError = (error: unknown) => {
  if (error instanceof GraphSaveError && error.epoch !== generation) return;
  publish({ ...state, error: error instanceof Error ? error.message : String(error), conflict: error instanceof GraphSaveError && error.conflict });
};
export const persistenceEpoch = () => generation;
export const graphReadTicket = () => `${generation}:${writeGeneration}`;
const responseRevision = (response: Response) => {
  // Compression proxies may weaken HTTP ETags. The application revision is
  // independent of representation encoding and must survive that transformation.
  const explicit = response.headers.get('X-InLumen-Graph-Revision');
  if (explicit !== null) return /^\d+$/.test(explicit) ? `"${explicit}"` : null;
  // Compatibility with older servers: only our numeric graph validator is
  // accepted, never an arbitrary weak ETag or an object-storage checksum.
  const match = /^(?:W\/)?"(\d+)"$/.exec(response.headers.get('ETag') ?? '');
  return match ? `"${match[1]}"` : null;
};
export const captureGraphRevision = (response: Response, epoch = generation) => {
  const incoming = responseRevision(response);
  if (epoch !== generation || !response.ok || !incoming) return;
  if (revision === null || BigInt(incoming.slice(1, -1)) >= BigInt(revision.slice(1, -1))) revision = incoming;
};

// Compare saved content, not JSON key order or the workspace's update timestamp.
// Retain all node/edge fields and pipeline metadata: a changed version, parameter,
// file, or generated artifact must not be acknowledged without reaching the canvas.
const stableValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, stableValue(item)]),
  );
  return value;
};
const graphSignature = (value: unknown): string | null => {
  if (!value || typeof value !== 'object') return null;
  const graph = value as Record<string, unknown>;
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return null;
  const pipeline = graph.pipeline && typeof graph.pipeline === 'object'
    ? Object.fromEntries(Object.entries(graph.pipeline).filter(([key]) => key !== 'updated_at'))
    : null;
  const sortedItems = (items: unknown[]) => items.map(stableValue).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  return JSON.stringify(stableValue({ nodes: sortedItems(graph.nodes), edges: sortedItems(graph.edges), settings: graph.settings ?? null, pipeline }));
};

type WriteOptions = {
  readGraph?: () => Promise<Response>;
  // Catalog-only mutations advance the workspace revision without editing the canvas.
  preservesGraph?: boolean;
  validationStatuses?: readonly number[];
};

export const graphWrite = (send: (revision: string | null) => Promise<Response>, options: WriteOptions = {}): Promise<Response> => {
  const epoch = generation;
  writeGeneration += 1;
  publish({ ...state, pending: state.pending + 1 });
  const task = serial.then(async () => {
    if (epoch !== generation) throw new GraphSaveError('The workspace changed.', false, epoch);
    if (state.error) throw new GraphSaveError(state.error, state.conflict);
    try {
      let response = await send(revision);
      if (epoch !== generation) throw new Error('The workspace changed.');
      let problem = response.ok ? null : await response.clone().json().catch(() => null);
      if (epoch !== generation) throw new Error('The workspace changed.');
      if (response.status === 409 && problem?.code === 'graph_conflict') {
        // A revision can advance for catalog/snapshot bookkeeping. Retry only
        // when the entire acknowledged graph is still unchanged, never merely
        // by accepting the ETag from a rejected write.
        if (options.readGraph && acknowledgedGraph?.revision === revision) {
          const current = await options.readGraph();
          const graph = current.ok ? await current.json() : null;
          if (epoch !== generation) throw new Error('The workspace changed.');
          const incoming = responseRevision(current);
          if (incoming && revision && BigInt(incoming.slice(1, -1)) >= BigInt(revision.slice(1, -1))
            && graphSignature(graph) === acknowledgedGraph.signature) {
            captureGraphRevision(current, epoch);
            acknowledgedGraph = { ...acknowledgedGraph, revision: incoming };
            response = await send(revision);
            if (epoch !== generation) throw new Error('The workspace changed.');
            problem = response.ok ? null : await response.clone().json().catch(() => null);
            if (epoch !== generation) throw new Error('The workspace changed.');
          }
        }
        if (response.status === 409 && problem?.code === 'graph_conflict') {
          throw new GraphSaveError('The saved graph has changed. Your edits are still here.', true);
        }
      }
      // Validation/business conflicts (for example a duplicate version name)
      // belong to the calling form and must not freeze unrelated canvas saves.
      if (response.status === 409 || options.validationStatuses?.includes(response.status)) return response;
      if (!response.ok) {
        const detail = (typeof problem?.error === 'string' ? problem.error : await response.clone().text()).slice(0, 500);
        throw new GraphSaveError(`Changes could not be saved (${response.status}). ${detail} Try saving again.`, false, epoch);
      }
      const previousRevision = revision;
      captureGraphRevision(response, epoch);
      if (options.preservesGraph && acknowledgedGraph?.revision === previousRevision && revision) {
        acknowledgedGraph = { ...acknowledgedGraph, revision };
      } else {
        acknowledgedGraph = null;
      }
      return response;
    } catch (error) {
      const failure = error instanceof GraphSaveError ? error
        : new GraphSaveError(error instanceof Error ? error.message : String(error), false, epoch);
      reportPersistenceError(failure);
      throw failure;
    }
  }).finally(() => {
    if (epoch === generation) publish({ ...state, pending: Math.max(0, state.pending - 1) });
  });
  serial = task.catch(() => undefined);
  return task;
};

// A background graph read is not an acknowledgement until the canvas applies it.
const graphResponses = new WeakMap<object, { response: Response; epoch: number }>();
export const rememberGraphRead = (graph: object, response: Response, epoch: number) => {
  graphResponses.set(graph, { response, epoch });
};
export const acknowledgeGraphRead = (graph: unknown) => {
  if (graph && typeof graph === 'object') {
    const metadata = graphResponses.get(graph);
    if (metadata) {
      const incoming = responseRevision(metadata.response);
      if (metadata.epoch !== generation || (incoming && revision && BigInt(incoming.slice(1, -1)) < BigInt(revision.slice(1, -1)))) return false;
      captureGraphRevision(metadata.response, metadata.epoch);
      const signature = graphSignature(graph);
      if (signature && incoming === revision && incoming) acknowledgedGraph = { signature, revision: incoming };
    }
  }
  return true;
};
