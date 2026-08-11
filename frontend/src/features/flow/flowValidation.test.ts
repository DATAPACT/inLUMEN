import { describe, expect, it } from "vitest";

import { normalizeGraph } from "@/features/flow/flowGraph";
import { getValidationIssueSubject, validateGraph } from "@/features/flow/flowValidation";
import { conversationUnderstandingSubpipeline, publicPortsForSubpipeline } from "@/features/flow/subpipeline";

describe("pipeline design validation", () => {
  it("describes every issue with its node or connection subject", () => {
    const nodes = [
      {
        id: "transcription",
        position: { x: 0, y: 0 },
        data: { type: "task", label: "Transcription", template_label: "Speech-to-Text" },
      },
      {
        id: "sentiment",
        position: { x: 1, y: 0 },
        data: { type: "task", label: "Sentiment Analysis", template_label: "Sentiment Analysis" },
      },
    ];
    const edges = [{ id: "transcription-sentiment", source: "transcription", target: "sentiment" }];

    expect(getValidationIssueSubject({
      severity: "warning",
      category: "implementation",
      code: "missing-code",
      nodeId: "transcription",
      message: "Missing code.",
    }, nodes, edges)).toEqual({ label: "Transcription", context: "Task · Speech-to-Text" });

    expect(getValidationIssueSubject({
      severity: "error",
      category: "graph",
      code: "duplicate-edge",
      edgeId: "transcription-sentiment",
      message: "Duplicate connection.",
    }, nodes, edges)).toEqual({ label: "Transcription → Sentiment Analysis", context: "Connection" });
  });

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

  it("keeps sample inputs separate from executable Task code", () => {
    const graph = normalizeGraph({
      nodes: [{
        id: "task",
        position: { x: 0, y: 0 },
        data: {
          type: "task",
          implementation: { kind: "python", language: "python" },
          files: [
            { filename: "records.csv", role: "data" },
            { filename: "requirements.txt", role: "code" },
          ],
        },
      }],
      edges: [],
    });

    const draftIssue = validateGraph(graph.nodes, graph.edges, { mode: "draft" }).issues
      .find((issue) => issue.code === "missing-code");
    const completeIssue = validateGraph(graph.nodes, graph.edges).issues
      .find((issue) => issue.code === "missing-code");

    expect(draftIssue?.severity).toBe("warning");
    expect(completeIssue?.severity).toBe("error");

    graph.nodes[0].data.files = [
      { filename: "records.csv", role: "data" },
      { filename: "main.py", role: "code" },
    ];
    const missingEntrypoint = validateGraph(graph.nodes, graph.edges, { mode: "draft" }).issues
      .find((issue) => issue.code === "missing-entrypoint");
    expect(missingEntrypoint?.severity).toBe("warning");

    graph.nodes[0].data.implementation = {
      ...graph.nodes[0].data.implementation,
      entrypoint: "main.py",
    };
    expect(validateGraph(graph.nodes, graph.edges).issues).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ category: "implementation" }),
      ]),
    );
  });

  it("reports missing code independently on every code-backed Task", () => {
    const graph = normalizeGraph({
      nodes: [
        {
          id: "transcription",
          position: { x: 0, y: 0 },
          data: {
            type: "task",
            implementation: { kind: "python", language: "python" },
          },
        },
        {
          id: "sentiment",
          position: { x: 1, y: 0 },
          data: {
            type: "task",
            implementation: {
              kind: "generated-code",
              task: "text-classification",
              execution_profile: "trusted_heavy_model",
            },
          },
        },
      ],
      edges: [],
    });

    const report = validateGraph(graph.nodes, graph.edges, { mode: "draft" });
    expect(report.byNode.transcription).toEqual(expect.arrayContaining([
      expect.objectContaining({ code: "missing-code", severity: "warning" }),
    ]));
    expect(report.byNode.sentiment).toEqual(expect.arrayContaining([
      expect.objectContaining({ code: "missing-code", severity: "warning" }),
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

  it("validates a pinned reusable-pipeline reference and its cached interface", () => {
    const reusable = conversationUnderstandingSubpipeline();
    const definition = {
      version: 2 as const,
      reference: {
        pipeline_uid: "conversation-pipeline",
        pipeline_name: "Conversation Understanding",
        version_uid: "version-1",
        version_name: "Version 1",
      },
      interface: reusable.interface,
      resolved_graph: reusable.graph,
    };
    const graph = normalizeGraph({
      nodes: [
        { id: "source", position: { x: 0, y: 0 }, data: { type: "source" } },
        {
          id: "subpipeline",
          position: { x: 1, y: 0 },
          data: {
            type: "subpipeline",
            ports: publicPortsForSubpipeline(definition),
            subpipeline: definition,
          },
        },
        { id: "destination", position: { x: 2, y: 0 }, data: { type: "destination" } },
      ],
      edges: [
        { source: "source", target: "subpipeline", sourceHandle: "data", targetHandle: "audio" },
        { source: "subpipeline", target: "destination", sourceHandle: "conversation_analysis", targetHandle: "data" },
      ],
    });

    expect(validateGraph(graph.nodes, graph.edges).valid).toBe(true);
    graph.nodes[1].data.ports.outputs = [];
    expect(validateGraph(graph.nodes, graph.edges).issues).toEqual(
      expect.arrayContaining([expect.objectContaining({ code: "invalid-subpipeline-interface" })]),
    );
  });

  it("treats incomplete wiring as a draft warning without hiding completion errors", () => {
    const reusable = conversationUnderstandingSubpipeline();
    const definition = {
      version: 2 as const,
      reference: {
        pipeline_uid: "conversation-pipeline",
        pipeline_name: "Conversation Understanding",
        version_uid: "version-1",
        version_name: "Version 1",
      },
      interface: reusable.interface,
    };
    const graph = normalizeGraph({
      nodes: [{
        id: "subpipeline",
        position: { x: 0, y: 0 },
        data: {
          type: "subpipeline",
          ports: publicPortsForSubpipeline(definition),
          subpipeline: definition,
        },
      }],
      edges: [],
    });

    const draftIssue = validateGraph(graph.nodes, graph.edges, { mode: "draft" }).issues
      .find((issue) => issue.code === "missing-required-input");
    const completeIssue = validateGraph(graph.nodes, graph.edges).issues
      .find((issue) => issue.code === "missing-required-input");

    expect(draftIssue?.severity).toBe("warning");
    expect(completeIssue?.severity).toBe("error");
  });
});
