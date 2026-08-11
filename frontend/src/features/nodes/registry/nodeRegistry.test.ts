import { describe, expect, it } from "vitest";

import {
  createNodeDataFromDefinition,
  getFallbackNodeDefinitions,
  groupNodeDefinitions,
} from "@/features/nodes/registry/nodeRegistry";

describe("core node registry", () => {
  it("offers generic pipeline roles in lifecycle order", () => {
    const definitions = getFallbackNodeDefinitions();

    expect(definitions.map((definition) => definition.id)).toEqual([
      "core.source",
      "core.task",
      "core.destination",
      "core.flow",
      "core.subpipeline",
    ]);
    expect(definitions.map((definition) => definition.palette.label)).toEqual([
      "Source",
      "Task",
      "Destination",
      "Flow",
      "Subpipeline",
    ]);
    expect(new Set(definitions.map((definition) => definition.runtime.generator)))
      .toEqual(new Set(["generic"]));
  });

  it("groups the five structural component families separately", () => {
    const groups = groupNodeDefinitions(getFallbackNodeDefinitions());

    expect(groups.map(([family]) => family)).toEqual([
      "sources",
      "tasks",
      "destinations",
      "flow",
      "subpipeline",
    ]);
  });

  it("creates a usable Condition when a Flow component is dragged in", () => {
    const flowDefinition = getFallbackNodeDefinitions().find((definition) => definition.base_type === "flow")!;

    expect(createNodeDataFromDefinition(flowDefinition)).toMatchObject({
      label: "Condition",
      template_label: "Condition",
      param: { expression: "" },
      ports: {
        inputs: [{ id: "value" }],
        outputs: [{ id: "when_true" }, { id: "when_false" }],
      },
    });
  });
});
