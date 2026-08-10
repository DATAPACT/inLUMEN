import type { Edge, Node } from "reactflow";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("edge persistence", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_AUTH_ENABLED", "false");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    globalThis.fetch = originalFetch;
  });

  it("sends the displayed port identity when deleting a connection", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      deleted_count: 1,
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    globalThis.fetch = fetchMock;
    const { deleteEdgeFromBackend } = await import("@/features/flow/flowPersistence");
    const source = { id: "source" } as Node;
    const target = { id: "target" } as Node;
    const edge = { sourceHandle: "file", targetHandle: "input" } as Edge;

    await deleteEdgeFromBackend(source, target, edge);

    const [, request] = fetchMock.mock.calls[0];
    expect(request.method).toBe("DELETE");
    expect(JSON.parse(request.body)).toEqual({
      properties: {
        flow_id_source: "source",
        flow_id_target: "target",
        source_port: "file",
        target_port: "input",
      },
    });
  });

  it("surfaces backend deletion failures so the canvas can recover", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("legacy edge was not deleted", {
      status: 500,
    }));
    const { deleteEdgeFromBackend } = await import("@/features/flow/flowPersistence");

    await expect(deleteEdgeFromBackend(
      { id: "source" } as Node,
      { id: "target" } as Node,
      { sourceHandle: "file", targetHandle: "input" } as Edge,
    )).rejects.toThrow("legacy edge was not deleted");
  });

  it("rejects a successful response that removed no connection", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      deleted_count: 0,
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    const { deleteEdgeFromBackend } = await import("@/features/flow/flowPersistence");

    await expect(deleteEdgeFromBackend(
      { id: "source" } as Node,
      { id: "target" } as Node,
      { sourceHandle: "file", targetHandle: "input" } as Edge,
    )).rejects.toThrow("did not find the selected connection");
  });
});

describe("background code generation", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_AUTH_ENABLED", "false");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    globalThis.fetch = originalFetch;
  });

  it("lists durable runs that can be reattached after a reload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      runs: [{ run_id: "run-1", status: "running" }],
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    globalThis.fetch = fetchMock;
    const { listPipelineScriptGenerationRuns } = await import(
      "@/features/flow/flowPersistence"
    );

    await expect(listPipelineScriptGenerationRuns(5)).resolves.toEqual([
      { run_id: "run-1", status: "running" },
    ]);
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/pipeline/generation-runs?limit=5",
    );
  });

  it("sends the dedicated code model instead of the chat model", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      run_id: "run-code",
      status: "queued",
    }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    }));
    globalThis.fetch = fetchMock;
    const { startPipelineScriptGenerationRun } = await import(
      "@/features/flow/flowPersistence"
    );

    await startPipelineScriptGenerationRun({
      name: "Split models",
      provider: "openrouter",
      model: "openai/gpt-oss-120b",
      codegenModel: "openai/gpt-5.2-codex",
      baseUrl: "https://openrouter.ai/api/v1",
      apiKey: "provider-secret",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.llm_config.model).toBe("openai/gpt-5.2-codex");
    expect(body.llm_config.model).not.toBe("openai/gpt-oss-120b");
    expect(body.llm_config.api_key).toBe("provider-secret");
  });
});
