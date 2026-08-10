import { describe, expect, it } from "vitest";

import { normalizeGraph } from "@/features/flow/flowGraph";
import { validateGraph } from "@/features/flow/flowValidation";

describe("pipeline design validation", () => {
  it("reports missing required inputs, parameters, outputs, and implementation issues", () => {
    const graph = normalizeGraph({
      nodes: [{
        id: "task",
        position: { x: 0, y: 0 },
        data: {
          type: "task",
          template_label: "LLM",
          implementation: { kind: "container" },
          ports: { inputs: [{ id: "prompt", name: "prompt", type: "Text", required: true, description: "" }], outputs: [] },
          param: { temperature: "" },
        },
      }],
      edges: [],
    });

    const codes = validateGraph(graph.nodes, graph.edges).issues.map((issue) => issue.code);
    expect(codes).toEqual(expect.arrayContaining([
      "missing-required-input",
      "missing-output",
      "missing-parameter-value",
      "missing-required-parameter",
      "missing-container-image",
    ]));
  });

  it("accepts valid explicit connections and rejects incompatible contracts", () => {
    const graph = normalizeGraph({
      nodes: [
        {
          id: "source",
          position: { x: 0, y: 0 },
          data: { type: "source", ports: { outputs: [{ id: "audio", name: "audio", type: "Audio", required: true, description: "" }] } },
        },
        {
          id: "task",
          position: { x: 1, y: 0 },
          data: {
            type: "task",
            implementation: { kind: "generated-code" },
            ports: {
              inputs: [{ id: "records", name: "records", type: "Dataset", required: true, description: "" }],
              outputs: [{ id: "result", name: "result", type: "Dataset", required: true, description: "" }],
            },
          },
        },
      ],
      edges: [{ source: "source", target: "task", sourceHandle: "audio", targetHandle: "records" }],
    });

    const report = validateGraph(graph.nodes, graph.edges);
    expect(report.valid).toBe(false);
    expect(report.issues).toEqual(expect.arrayContaining([
      expect.objectContaining({ code: "incompatible-port-types" }),
    ]));
  });
});
