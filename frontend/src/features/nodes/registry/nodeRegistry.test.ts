import { describe, expect, it } from "vitest";

import {
  getFallbackNodeDefinitions,
  groupNodeDefinitions,
} from "@/features/nodes/registry/nodeRegistry";

describe("core node registry", () => {
  it("offers generic pipeline roles in lifecycle order", () => {
    const definitions = getFallbackNodeDefinitions();

    expect(definitions.map((definition) => definition.id)).toEqual([
      "core.source",
      "core.task",
      "core.sink",
      "core.flow",
      "core.subpipeline",
    ]);
    expect(definitions.map((definition) => definition.palette.label)).toEqual([
      "Source",
      "Blank Task",
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
});
