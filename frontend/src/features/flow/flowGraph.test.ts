import type { Node } from "reactflow";
import { describe, expect, it } from "vitest";

import {
  createAgentGraphSnapshot,
  getNextNumericNodeId,
  normalizeGraph,
} from "@/features/flow/flowGraph";


describe("flow graph normalization", () => {
  it("normalizes nodes and removes invalid or duplicate edges", () => {
    const graph = normalizeGraph({
      updated_at: "2026-08-07T10:00:00Z",
      settings: { version: "1.0" },
      nodes: [
        {
          id: 1,
          position: { x: "12.5", y: "invalid" },
          data: { label: "Source", description: "", type: "data source" },
        },
        {
          id: "2",
          position: { x: 30, y: 40 },
          data: { label: "Result", type: "reporting" },
        },
        { id: " ", position: { x: 0, y: 0 }, data: {} },
      ],
      edges: [
        { source: 1, target: 2 },
        { id: "duplicate", source: "1", target: "2" },
        { source: "2", target: "2" },
        { source: "2", target: "missing" },
      ],
    });

    expect(graph.nodes).toHaveLength(2);
    expect(graph.nodes[0]).toMatchObject({
      id: "1",
      position: { x: 12.5, y: 0 },
      data: { type: "input" },
    });
    expect(graph.nodes[1].data.type).toBe("output");
    expect(graph.edges).toEqual([
      expect.objectContaining({ id: "e-1-2", source: "1", target: "2" }),
    ]);
    expect(graph.settings).toEqual({ version: "1.0" });
  });

  it("creates the compact graph contract consumed by agents", () => {
    const graph = normalizeGraph({
      nodes: [
        {
          id: "4",
          position: { x: 1, y: 2 },
          data: {
            label: "Input",
            description: "Load records",
            type: "input",
            files: [
              new File(["a,b"], "records.csv", { type: "text/csv" }),
              { filename: "schema.json", bucket: "files-step-id-4" },
              "legacy.txt",
            ],
            definition_id: "core.input-data",
            definition_version: 1,
            implementation: { parser: "csv" },
            configuration_status: "valid",
            generated_artifact: { status: "current" },
          },
        },
      ],
      edges: [],
    });

    expect(createAgentGraphSnapshot(graph)).toEqual({
      updated_at: null,
      nodes: [
        {
          id: "4",
          type: "input",
          label: "Input",
          description: "Load records",
          position: { x: 1, y: 2 },
          files: ["records.csv", "schema.json", "legacy.txt"],
          definition_id: "core.input-data",
          definition_version: 1,
          implementation: { parser: "csv" },
          configuration_status: "valid",
          generated_artifact: { status: "current" },
        },
      ],
      edges: [],
    });
  });

  it("increments the largest numeric node id and supports an empty graph", () => {
    const nodes = [
      { id: "2" },
      { id: "10" },
      { id: "generated-node" },
    ] as Node[];

    expect(getNextNumericNodeId(nodes)).toBe(11);
    expect(getNextNumericNodeId([], 5)).toBe(5);
  });
});
