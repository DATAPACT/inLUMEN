import { describe, expect, it } from "vitest";

import { normalizeGraph } from "@/features/flow/flowGraph";
import {
  PROJECT_IR_SCHEMA_VERSION,
  createProjectDocument,
  projectDocumentToGraph,
} from "@/features/flow/projectIr";

describe("Project JSON Pipeline IR", () => {
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
});
