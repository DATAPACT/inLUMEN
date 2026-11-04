import { describe, expect, it } from "vitest";

import { mapScopes, type ScopeConnection } from "./mapScope";

// Same graphs as backend/tests/test_map_scope.py.
const FLOWS = { map: "Parallel Map", collect: "Collect" };
const link = (source: string, sourcePort: string, target: string, targetPort: string): ScopeConnection =>
  ({ source, sourcePort, target, targetPort });
const codes = (edges: ScopeConnection[], templates: Record<string, string> = FLOWS) =>
  mapScopes(templates, edges).issues.map((issue) => issue.code);

describe("mapScopes", () => {
  it("puts every step between map and collect in the body, in order", () => {
    const { scopes, issues } = mapScopes(FLOWS, [
      link("src", "data", "map", "items"), link("map", "item", "extract", "input"),
      link("extract", "output", "parse", "input"), link("parse", "output", "collect", "item"),
      link("collect", "items", "aggregate", "input"),
    ]);
    expect(issues).toEqual([]);
    expect(scopes).toEqual([{ map: "map", collect: "collect", body: ["extract", "parse"] }]);
  });

  it("reports structural errors", () => {
    expect(codes([link("map", "item", "a", "input"), link("a", "output", "dest", "data")], { map: "Parallel Map" }))
      .toEqual(["map-without-collect"]);
    expect(codes([link("map", "item", "a", "input"), link("lookup", "output", "a", "input"), link("a", "output", "collect", "item")]))
      .toContain("map-side-input");
    expect(codes([link("map", "item", "a", "input"), link("a", "output", "collect", "item"), link("a", "output", "dest", "data")]))
      .toContain("map-scope-leak");
    expect(codes([link("a", "output", "collect", "item")], { collect: "Collect" })).toEqual(["collect-without-map"]);
    expect(codes([link("map", "item", "map2", "items"), link("map2", "item", "collect", "item")],
      { map: "Parallel Map", map2: "Parallel Map", collect: "Collect" })).toContain("nested-map");
  });
});
