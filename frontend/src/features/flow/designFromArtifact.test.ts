import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "@/lib/apiFetch";
import { artifactKindLabel, designFromArtifact, IMPORT_ACCEPT, isArtifactFile, isArtifactJson } from "./designFromArtifact";
import { projectDocumentToGraph } from "./projectIr";

vi.mock("@/lib/apiFetch", () => ({ apiFetch: vi.fn() }));

const response = (status: number, body: unknown) =>
  ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;

describe("design from a deployment artifact", () => {
  beforeEach(() => vi.mocked(apiFetch).mockReset());

  it("sends ZIP and YAML files to the backend and reads Project JSON here", () => {
    expect(isArtifactFile({ name: "bundle.ZIP" })).toBe(true);
    expect(isArtifactFile({ name: "workflow.yaml" })).toBe(true);
    expect(isArtifactFile({ name: "workflow.yml" })).toBe(true);
    expect(isArtifactFile({ name: "project.json" })).toBe(false);
    expect(IMPORT_ACCEPT.split(",")).toEqual([".json", ".zip", ".yaml", ".yml"]);
  });

  it("tells a bundle manifest and an Argo workflow in JSON from Project JSON", () => {
    expect(isArtifactJson({ schema_version: "inlumen.deployment-bundle@3" })).toBe(true);
    expect(isArtifactJson({ apiVersion: "argoproj.io/v1alpha1", kind: "Workflow" })).toBe(true);
    expect(isArtifactJson({ schema_version: "inlumen.project@2" })).toBe(false);
    expect(isArtifactJson({ nodes: [], edges: [] })).toBe(false);
    expect(isArtifactJson(null)).toBe(false);
  });

  it("returns a design that the canvas import accepts", async () => {
    const project = {
      schema_version: "inlumen.project@2",
      project: { name: "Train model", description: "", exported_at: "2026-11-27T00:00:00.000Z" },
      pipeline: {
        nodes: [
          { id: "1", kind: "source", template: { id: "custom.custom", name: "Custom" }, label: "Fetch data",
            description: "", position: { x: 150, y: 150 }, inputs: [],
            outputs: [{ id: "data", name: "data", type: "any", required: true, description: "" }], parameters: {} },
          { id: "2", kind: "destination", template: { id: "custom.custom", name: "Custom" }, label: "Publish",
            description: "", position: { x: 450, y: 150 },
            inputs: [{ id: "data", name: "data", type: "any", required: true, description: "" }], outputs: [], parameters: {} },
        ],
        connections: [{ id: "1:data->2:data", from: { node: "1", port: "data" }, to: { node: "2", port: "data" } }],
      },
    };
    vi.mocked(apiFetch).mockResolvedValue(response(200, {
      kind: "argo-workflow", project, notes: ["Built from tasks."], step_count: 2, connection_count: 1,
    }));

    const design = await designFromArtifact(new File(["kind: Workflow"], "train.yaml"));

    const [url, init] = vi.mocked(apiFetch).mock.calls[0];
    expect(String(url)).toMatch(/\/api\/pipeline\/design-from-artifact$/);
    expect((init as RequestInit).method).toBe("POST");
    expect(((init as RequestInit).body as FormData).get("file")).toBeInstanceOf(File);
    expect(artifactKindLabel(design.kind)).toBe("Argo workflow");
    const graph = projectDocumentToGraph(design.project);
    expect(graph.nodes.map((node) => node.data.label)).toEqual(["Fetch data", "Publish"]);
    expect(graph.edges).toHaveLength(1);
  });

  it("reports the backend's reason when the file is refused", async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(422, {
      error: "This file cannot be turned into a design", details: "The ZIP holds no bundle-manifest.json",
    }));
    await expect(designFromArtifact(new File(["x"], "code.zip"))).rejects.toThrow("no bundle-manifest.json");
  });
});
