import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { normalizeGraph } from "@/features/flow/flowGraph";
import {
  PROJECT_IR_SCHEMA_VERSION,
  createProjectDocument,
  projectDocumentToGraph,
} from "@/features/flow/projectIr";
import { conversationUnderstandingSubpipeline, publicPortsForSubpipeline } from "@/features/flow/subpipeline";

describe("Project JSON Pipeline IR", () => {
  it("uses the published Project JSON schema version", () => {
    const schema = JSON.parse(readFileSync(resolve(
      process.cwd(),
      "../contracts/v2/project.schema.json",
    ), "utf8"));
    expect(PROJECT_IR_SCHEMA_VERSION).toBe(
      schema.properties.schema_version.const,
    );
  });

  it("exports structural kind, template, implementation, and contracts separately", () => {
    const graph = normalizeGraph({
      nodes: [{
        id: "1",
        position: { x: 10, y: 20 },
        data: {
          type: "task",
          label: "Transcribe",
          template_label: "Speech-to-Text",
          implementation: { kind: "container", image: "example/asr:1" },
          ports: {
            inputs: [{ id: "audio", name: "audio", type: "Audio", required: true, description: "Recording" }],
            outputs: [{ id: "transcript", name: "transcript", type: "Text", required: true, description: "Transcript" }],
          },
          param: { language: "en" },
        },
      }],
      edges: [],
    });

    const project = createProjectDocument(graph, { name: "ASR" });

    expect(project.schema_version).toBe(PROJECT_IR_SCHEMA_VERSION);
    expect(project.pipeline.nodes[0]).toMatchObject({
      kind: "task",
      template: { id: "task.speech-to-text", name: "Speech-to-Text" },
      implementation: { kind: "container", image: "example/asr:1" },
      parameters: { language: "en" },
      inputs: [{ name: "audio", type: "Audio", required: true }],
    });
  });

  it("round-trips canonical Project JSON and migrates legacy sink graphs", () => {
    const migrated = projectDocumentToGraph({
      nodes: [
        { id: "1", position: { x: 0, y: 0 }, data: { type: "source" } },
        { id: "2", position: { x: 100, y: 0 }, data: { type: "sink" } },
      ],
      edges: [{ source: "1", target: "2" }],
    });

    expect(migrated.nodes[1].data.type).toBe("destination");
    expect(migrated.edges[0]).toMatchObject({ sourceHandle: "data", targetHandle: "data" });

    const roundTrip = projectDocumentToGraph(createProjectDocument(migrated));
    expect(roundTrip.nodes.map((node) => node.data.type)).toEqual(["source", "destination"]);
    expect(roundTrip.edges[0]).toMatchObject({ sourceHandle: "data", targetHandle: "data" });
  });

  it("rejects an explicitly unsupported project schema version", () => {
    expect(() => projectDocumentToGraph({
      schema_version: "inlumen.project@99",
      pipeline: { nodes: [], connections: [] },
    })).toThrow("Unsupported Project JSON schema version: inlumen.project@99");
  });

  it("does not export or restore obsolete source configuration", () => {
    const graph = normalizeGraph({
      nodes: [{
        id: "source",
        position: { x: 0, y: 0 },
        data: {
          type: "source",
          source_config: { base_url: "https://legacy.test" },
        },
      }],
      edges: [],
    });

    const document = createProjectDocument(graph);
    expect(document.pipeline.nodes[0]).not.toHaveProperty("source_configuration");

    const legacyDocument = {
      ...document,
      pipeline: {
        ...document.pipeline,
        nodes: document.pipeline.nodes.map((node) => ({
          ...node,
          source_configuration: { base_url: "https://legacy.test" },
        })),
      },
    };
    const restored = projectDocumentToGraph(legacyDocument);
    expect(restored.nodes[0].data).not.toHaveProperty("source_config");
  });

  it("round-trips a pinned reusable-pipeline reference and interface", () => {
    const reusable = conversationUnderstandingSubpipeline();
    const definition = {
      version: 2 as const,
      reference: {
        pipeline_uid: "conversation-pipeline",
        pipeline_name: "Conversation Understanding",
        version_uid: "conversation-v1",
        version_name: "Version 1",
      },
      interface: reusable.interface,
      resolved_graph: reusable.graph,
    };
    const graph = normalizeGraph({
      nodes: [{
        id: "conversation",
        position: { x: 10, y: 20 },
        data: {
          type: "subpipeline",
          label: "Conversation Understanding",
          ports: publicPortsForSubpipeline(definition),
          subpipeline: definition,
        },
      }],
      edges: [],
    });

    const document = createProjectDocument(graph);
    expect(document.pipeline.nodes[0].subpipeline).toMatchObject({
      version: 2,
      reference: {
        pipeline_uid: "conversation-pipeline",
        version_uid: "conversation-v1",
      },
      interface: {
        inputs: [{ id: "audio", internal: { node: "conversation-input", port: "audio" } }],
      },
    });

    const restored = projectDocumentToGraph(document);
    expect(restored.nodes[0].data.subpipeline).not.toHaveProperty("graph");
    expect(restored.nodes[0].data.subpipeline.reference).toMatchObject({
      pipeline_uid: "conversation-pipeline",
      version_uid: "conversation-v1",
    });
    expect(restored.nodes[0].data.subpipeline.interface.outputs[0]).toMatchObject({
      id: "conversation_analysis",
      internal: { node: "conversation-output", port: "conversation_analysis" },
    });
  });
});
