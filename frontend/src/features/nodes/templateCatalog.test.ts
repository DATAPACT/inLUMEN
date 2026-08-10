import { describe, expect, it } from "vitest";

import {
  COMPONENT_TEMPLATE_CATALOG,
  defaultTemplateForType,
  templateOptionsForType,
} from "@/features/nodes/templateCatalog";

describe("component template catalog", () => {
  it("keeps templates underneath exactly five structural kinds", () => {
    expect(Object.keys(COMPONENT_TEMPLATE_CATALOG)).toEqual([
      "source",
      "task",
      "sink",
      "flow",
      "subpipeline",
    ]);
    expect(COMPONENT_TEMPLATE_CATALOG.source.map(({ value }) => value)).toContain("Kafka");
    expect(COMPONENT_TEMPLATE_CATALOG.task.map(({ value }) => value)).toContain("Speech-to-Text");
    expect(COMPONENT_TEMPLATE_CATALOG.sink.map(({ value }) => value)).toContain("Notification");
    expect(COMPONENT_TEMPLATE_CATALOG.flow.map(({ value }) => value)).toContain("Human Approval");
  });

  it("keeps imported custom templates selectable without changing the catalog", () => {
    expect(defaultTemplateForType("task")).toBe("Blank Task");
    expect(templateOptionsForType("task", "Remote Patient Scoring")[0]).toEqual({
      value: "Remote Patient Scoring",
      label: "Remote Patient Scoring (custom)",
    });
    expect(COMPONENT_TEMPLATE_CATALOG.task[0].value).toBe("Blank Task");
  });
});
