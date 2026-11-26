import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";


describe("apiFetch", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    globalThis.fetch = originalFetch;
  });

  it("leaves requests untouched when authentication is disabled", async () => {
    vi.stubEnv("VITE_AUTH_ENABLED", "false");
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock;
    const { apiFetch, setAuthToken } = await import("@/utils/apiFetch");
    const init = { method: "GET", headers: { Accept: "application/json" } };

    setAuthToken("token-that-must-not-be-sent");
    await apiFetch("https://example.test/health", init);

    expect(fetchMock).toHaveBeenCalledWith("https://example.test/health", init);
  });

  it("injects the bearer token without discarding existing headers", async () => {
    vi.stubEnv("VITE_AUTH_ENABLED", "true");
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock;
    const { apiFetch, setAuthToken } = await import("@/utils/apiFetch");

    setAuthToken("sso-token");
    await apiFetch("https://example.test/pipelines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    const [, requestInit] = fetchMock.mock.calls[0];
    const headers = new Headers(requestInit.headers);
    expect(requestInit.method).toBe("POST");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Authorization")).toBe("Bearer sso-token");
  });

  it("injects the active workspace selector", async () => {
    vi.stubEnv("VITE_AUTH_ENABLED", "true");
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock;
    const { apiFetch, setActiveWorkspaceId } = await import("@/utils/apiFetch");

    setActiveWorkspaceId("workspace-123");
    await apiFetch("https://example.test/pipelines");

    const [, requestInit] = fetchMock.mock.calls[0];
    const headers = new Headers(requestInit.headers);
    expect(headers.get("X-InLumen-Workspace-Id")).toBe("workspace-123");
  });
});

it('serializes Overview and reusable-catalog updates with canvas saves', async () => {
  vi.resetModules();
  const { apiFetch } = await import('@/utils/apiFetch');
  const { captureGraphRevision, resetPersistence } = await import('@/features/flow/persistenceState');
  resetPersistence();
  captureGraphRevision(new Response('{}', { headers: { ETag: '"1"' } }));
  const seen: (string | null)[] = [];
  const mock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    seen.push(new Headers(init?.headers).get('If-Match'));
    return new Response('{}', { headers: { ETag: `"${seen.length + 1}"` } });
  });
  try {
    await Promise.all([
      apiFetch('/api/pipeline/overview', { method: 'POST', body: '{}' }),
      apiFetch('/api/reusable-pipelines', { method: 'POST', body: '{}' }),
      apiFetch('/api/graph/nodes/position', { method: 'POST', body: '{}' }),
    ]);
    expect(seen).toEqual(['"1"', '"2"', '"3"']);
  } finally { mock.mockRestore(); }
});

it('serializes file upload, removal and text edits with the next canvas save', async () => {
  vi.resetModules();
  const { uploadNodeFile, removeNodeFile, updateNodeTextFile, updateNodePropertiesInBackend } = await import('@/features/nodes/nodePersistence');
  const { captureGraphRevision, resetPersistence, getPersistenceState } = await import('@/features/flow/persistenceState');
  resetPersistence();
  captureGraphRevision(new Response('{}', { headers: { ETag: '"10"' } }));
  const seen: (string | null)[] = [];
  const mock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    seen.push(new Headers(init?.headers).get('If-Match'));
    return new Response('{}', { headers: { ETag: `"${10 + seen.length}"` } });
  });
  try {
    await Promise.all([
      uploadNodeFile('source/1', new File(['input'], 'input.txt'), 'data'),
      removeNodeFile('source/1', 'old.txt'),
      updateNodeTextFile('source/1', 'input.txt', 'edited'),
      updateNodePropertiesInBackend('source/1', { label: 'Source' }),
    ]);
    expect(seen).toEqual(['"10"', '"11"', '"12"', '"13"']);
    expect(getPersistenceState()).toEqual({ pending: 0, error: null, conflict: false });
  } finally { mock.mockRestore(); }
});

it('does not freeze canvas saves when the upload form rejects an attachment', async () => {
  vi.resetModules();
  const { uploadNodeFile, updateNodePropertiesInBackend } = await import('@/features/nodes/nodePersistence');
  const { captureGraphRevision, resetPersistence, getPersistenceState } = await import('@/features/flow/persistenceState');
  resetPersistence();
  captureGraphRevision(new Response('{}', { headers: { ETag: '"10"' } }));
  const mock = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ error: 'Invalid input attachment' }), { status: 422 }))
    .mockResolvedValueOnce(new Response('{}', { headers: { ETag: '"11"' } }));
  try {
    await expect(uploadNodeFile('1', new File(['invalid'], 'bad.csv'), 'data')).rejects.toThrow('Invalid input attachment');
    await updateNodePropertiesInBackend('1', { label: 'Source' });
    expect(mock).toHaveBeenCalledTimes(2);
    expect(new Headers(mock.mock.calls[1][1]?.headers).get('If-Match')).toBe('"10"');
    expect(getPersistenceState().error).toBeNull();
  } finally { mock.mockRestore(); }
});

it.each(['/api/reusable-pipelines', '/api/reusable-pipelines/attach'])('keeps reusable validation failures local to the form: %s', async (path) => {
  vi.resetModules();
  const { apiFetch } = await import('@/utils/apiFetch');
  const { captureGraphRevision, resetPersistence, getPersistenceState } = await import('@/features/flow/persistenceState');
  resetPersistence();
  captureGraphRevision(new Response('{}', { headers: { ETag: '"10"' } }));
  const mock = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ error: 'Only one subpipeline level is supported.' }), { status: 422 }))
    .mockResolvedValueOnce(new Response('{}', { headers: { ETag: '"11"' } }));
  try {
    expect((await apiFetch(path, { method: 'POST' })).status).toBe(422);
    expect((await apiFetch('/api/graph/nodes/1/properties', { method: 'POST' })).ok).toBe(true);
    expect(new Headers(mock.mock.calls[1][1]?.headers).get('If-Match')).toBe('"10"');
    expect(getPersistenceState().error).toBeNull();
  } finally { mock.mockRestore(); }
});

it.each([false, true])('keeps authenticated saves and uploads synchronized through compressed responses (dedicated revision: %s)', async (dedicatedHeader) => {
  vi.resetModules();
  vi.stubEnv('VITE_AUTH_ENABLED', 'true');
  const { apiFetch, setAuthToken, setActiveWorkspaceId } = await import('@/utils/apiFetch');
  const { rememberGraphRead, acknowledgeGraphRead, persistenceEpoch, getPersistenceState } = await import('@/features/flow/persistenceState');
  setAuthToken('test-session');
  setActiveWorkspaceId('isolated-workspace');
  let serverRevision = 10;
  const graph = { nodes: [], edges: [] };
  const wireResponse = (compressed: boolean) => Response.json(graph, { headers: {
    ETag: `${compressed ? 'W/' : ''}"${serverRevision}"`,
    ...(dedicatedHeader ? { 'X-InLumen-Graph-Revision': String(serverRevision) } : {}),
  } });
  const initial = wireResponse(true);
  rememberGraphRead(graph, initial, persistenceEpoch());
  acknowledgeGraphRead(graph);
  const mock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    const headers = new Headers(init?.headers);
    expect(headers.get('Authorization')).toBe('Bearer test-session');
    expect(headers.get('X-InLumen-Workspace-Id')).toBe('isolated-workspace');
    if (headers.get('If-Match') !== `"${serverRevision}"`) {
      return Response.json({ code: 'graph_conflict' }, { status: 409 });
    }
    serverRevision++;
    return wireResponse(serverRevision !== 12);
  });
  try {
    for (const path of ['/api/graph/nodes', '/api/graph/nodes/position', '/api/pipeline/versions/active', '/api/nodes/1/files', '/api/pipeline/versions/active', '/api/nodes/1/files/text', '/api/pipeline/versions/active']) {
      expect((await apiFetch(path, { method: 'POST' })).ok).toBe(true);
    }
    expect(serverRevision).toBe(17);
    expect(getPersistenceState().error).toBeNull();
  } finally { mock.mockRestore(); vi.unstubAllEnvs(); }
});
