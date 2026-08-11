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

  it("requires a configured and connected Condition behavior", () => {
    const graph = normalizeGraph({
      nodes: [{
        id: "condition",
        position: { x: 0, y: 0 },
        data: {
          type: "flow",
          template_label: "Condition",
          param: { expression: "" },
          ports: {
            inputs: [{ id: "value", name: "value", type: "any", required: true, description: "" }],
            outputs: [
              { id: "when_true", name: "when_true", type: "any", required: true, description: "" },
              { id: "when_false", name: "when_false", type: "any", required: false, description: "" },
            ],
          },
        },
      }],
      edges: [],
    });

    const codes = validateGraph(graph.nodes, graph.edges).issues.map((issue) => issue.code);
    expect(codes).toEqual(expect.arrayContaining([
      "missing-required-input",
      "missing-required-parameter",
      "missing-required-flow-output",
    ]));
  });

  it("accepts a complete Condition and validates Parallel Map scheduling", () => {
    const conditionGraph = normalizeGraph({
      nodes: [
        { id: "source", position: { x: 0, y: 0 }, data: { type: "source" } },
        {
          id: "condition",
          position: { x: 1, y: 0 },
          data: {
            type: "flow",
            template_label: "Condition",
            param: { expression: "value.score >= 0.8" },
            ports: {
              inputs: [{ id: "value", name: "value", type: "any", required: true, description: "" }],
              outputs: [
                { id: "when_true", name: "when_true", type: "any", required: true, description: "" },
                { id: "when_false", name: "when_false", type: "any", required: false, description: "" },
              ],
            },
          },
        },
        { id: "destination", position: { x: 2, y: 0 }, data: { type: "destination" } },
      ],
      edges: [
        { source: "source", target: "condition", sourceHandle: "data", targetHandle: "value" },
        { source: "condition", target: "destination", sourceHandle: "when_true", targetHandle: "data" },
      ],
    });
    expect(validateGraph(conditionGraph.nodes, conditionGraph.edges).valid).toBe(true);

    conditionGraph.nodes[1].data.param = { expression: "run arbitrary code()" };
    expect(validateGraph(conditionGraph.nodes, conditionGraph.edges).issues).toEqual(
      expect.arrayContaining([expect.objectContaining({ code: "invalid-flow-expression" })]),
    );

    const parallelGraph = normalizeGraph({
      nodes: [{
        id: "map",
        position: { x: 0, y: 0 },
        data: {
          type: "flow",
          template_label: "Parallel Map",
          param: { max_concurrency: 0, failure_policy: "ignore" },
          ports: {
            inputs: [{ id: "items", name: "items", type: "any[]", required: true, description: "" }],
            outputs: [{ id: "item", name: "item", type: "any", required: true, description: "" }],
          },
        },
      }],
      edges: [],
    });
    const parallelCodes = validateGraph(parallelGraph.nodes, parallelGraph.edges).issues.map((issue) => issue.code);
    expect(parallelCodes).toEqual(expect.arrayContaining([
      "invalid-flow-concurrency",
      "invalid-flow-failure-policy",
      "missing-required-flow-output",
    ]));
  });
});
