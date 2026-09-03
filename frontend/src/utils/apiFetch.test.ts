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
