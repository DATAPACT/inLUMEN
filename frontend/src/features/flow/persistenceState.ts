export type PersistenceState = { pending: number; error: string | null };
let state: PersistenceState = { pending: 0, error: null };
let revision: string | null = null;
let serial: Promise<unknown> = Promise.resolve();
let generation = 0;
let writeGeneration = 0;
const listeners = new Set<() => void>();
const publish = (next: PersistenceState) => { state = next; listeners.forEach((listener) => listener()); };
export const subscribePersistence = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const getPersistenceState = () => state;
export const resetPersistence = () => { generation += 1; revision = null; serial = Promise.resolve(); publish({ pending: 0, error: null }); };
export const clearPersistenceError = () => publish({ ...state, error: null });
export const reportPersistenceError = (error: unknown) => publish({ ...state, error: error instanceof Error ? error.message : String(error) });
export const persistenceEpoch = () => generation;
export const graphReadTicket = () => `${generation}:${writeGeneration}`;
export const captureGraphRevision = (response: Response, epoch = generation) => {
  const incoming = response.headers.get('ETag');
  if (epoch !== generation || !response.ok || !incoming || !/^"\d+"$/.test(incoming)) return;
  if (revision === null || BigInt(incoming.slice(1, -1)) >= BigInt(revision.slice(1, -1))) revision = incoming;
};
export const graphWrite = (send: (revision: string | null) => Promise<Response>): Promise<Response> => {
  const epoch = generation;
  writeGeneration += 1;
  publish({ ...state, pending: state.pending + 1 });
  const task = serial.then(async () => {
    if (epoch !== generation) throw new Error('The workspace changed.');
    if (state.error) throw new Error(state.error);
    try {
      const response = await send(revision);
      if (epoch !== generation) throw new Error('The workspace changed.');
      const detail = response.ok ? "" : (await response.clone().text()).slice(0, 500);
      if (!response.ok) throw new Error(response.status === 409
        ? 'Another session changed this pipeline. Download your draft, then reload the saved graph.'
        : `The edit could not be saved (${response.status}). ${detail} Download your draft before reloading.`);
      captureGraphRevision(response);
      return response;
    } catch (error) {
      if (epoch === generation) reportPersistenceError(error);
      throw error;
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
    if (metadata) captureGraphRevision(metadata.response, metadata.epoch);
  }
};
