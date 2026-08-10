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
      data: {
        type: "source",
        ports: {
          inputs: [],
          outputs: [{ id: "data", name: "data", type: "any", required: true, description: "Source data." }],
        },
      },
    });
    expect(graph.nodes[1].data.type).toBe("destination");
    expect(graph.edges).toEqual([
      expect.objectContaining({ id: "e-1-data-2-data", source: "1", target: "2" }),
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
            template_label: "File",
            implementation: { parser: "csv" },
            secret_params: [],
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
          type: "source",
          label: "Input",
          description: "Load records",
          position: { x: 1, y: 2 },
          files: ["records.csv", "schema.json", "legacy.txt"],
          definition_id: "core.input-data",
          definition_version: 1,
          template: "File",
          ports: {
            inputs: [],
            outputs: [{ id: "data", name: "data", type: "any", required: true, description: "Source data." }],
          },
          implementation: { parser: "csv" },
          secret_params: [],
          configuration_status: "valid",
          generated_artifact: { status: "current" },
        },
      ],
      edges: [],
    });
  });

  it("preserves explicit port connections in agent snapshots", () => {
    const graph = normalizeGraph({
      nodes: [
        { id: "1", position: { x: 0, y: 0 }, data: { type: "source" } },
        { id: "2", position: { x: 1, y: 1 }, data: { type: "task" } },
      ],
      edges: [
        {
          source: "1",
          target: "2",
          sourceHandle: "documents",
          targetHandle: "records",
        },
      ],
    });

    expect(createAgentGraphSnapshot(graph).edges).toEqual([
      {
        source: "1",
        target: "2",
        source_port: "documents",
        target_port: "records",
      },
    ]);
  });

  it("prefers persisted file metadata so code and data roles survive polling", () => {
    const graph = normalizeGraph({
      nodes: [{
        id: "7",
        position: { x: 0, y: 0 },
        data: {
          type: "task",
          files: ["main.py", "records.csv"],
          file_buckets: [
            { filename: "main.py", bucket: "files-step-id-7", role: "code" },
            { filename: "records.csv", bucket: "files-step-id-7", role: "data" },
          ],
        },
      }],
      edges: [],
    });

    expect(graph.nodes[0].data.files).toEqual([
      { filename: "main.py", bucket: "files-step-id-7", role: "code" },
      { filename: "records.csv", bucket: "files-step-id-7", role: "data" },
    ]);
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
