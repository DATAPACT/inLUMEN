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
      "destination",
      "flow",
      "subpipeline",
    ]);
    expect(COMPONENT_TEMPLATE_CATALOG.source.map(({ value }) => value)).toContain("Kafka");
    expect(COMPONENT_TEMPLATE_CATALOG.task.map(({ value }) => value)).toContain("Speech-to-Text");
    expect(COMPONENT_TEMPLATE_CATALOG.task.map(({ value }) => value)).not.toContain("Preprocessing");
    expect(COMPONENT_TEMPLATE_CATALOG.task.map(({ value }) => value)).not.toContain("Document Processing");
    expect(COMPONENT_TEMPLATE_CATALOG.task.map(({ value }) => value)).not.toContain("Custom Logic");
    expect(new Set(COMPONENT_TEMPLATE_CATALOG.task.map(({ category }) => category))).toEqual(new Set([
      "General",
      "Data",
      "Document & media",
      "AI & machine learning",
      "Integration",
    ]));
    expect(COMPONENT_TEMPLATE_CATALOG.destination.map(({ value }) => value)).toContain("Notification");
    expect(COMPONENT_TEMPLATE_CATALOG.flow.map(({ value }) => value)).toEqual([
      "Flow",
      "Condition",
      "Parallel Map",
    ]);
  });

  it("keeps imported custom templates selectable without changing the catalog", () => {
    expect(defaultTemplateForType("task")).toBe("Blank Task");
    expect(templateOptionsForType("task", "Remote Patient Scoring")[0]).toEqual({
      id: "custom.remote-patient-scoring",
      value: "Remote Patient Scoring",
      label: "Remote Patient Scoring (custom)",
      category: "Custom",
    });
    expect(templateOptionsForType("task", "Preprocessing")[0]).toMatchObject({
      id: "legacy.preprocessing",
      label: "Preprocessing (legacy)",
      category: "Legacy",
    });
    expect(COMPONENT_TEMPLATE_CATALOG.task[0].value).toBe("Blank Task");
  });
});
