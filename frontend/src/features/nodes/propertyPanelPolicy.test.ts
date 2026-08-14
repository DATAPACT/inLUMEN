import { describe, expect, it } from "vitest";

import {
  nodeSupportsInputFiles,
  taskImplementationMigrationError,
  taskImplementationStatus,
  visiblePropertySections,
} from "@/features/nodes/propertyPanelPolicy";

describe("Properties panel policy", () => {
  it("shows only contextual default sections", () => {
    expect(visiblePropertySections({
      nodeType: "task",
      template: "Speech-to-Text",
      configurationFieldCount: 0,
      validationIssueCount: 0,
    })).toEqual({
      general: true,
      configuration: false,
      implementation: true,
      inputFiles: false,
      validation: false,
      advanced: false,
    });
  });

  it("shows one simple implementation section without an Advanced section", () => {
    expect(visiblePropertySections({
      nodeType: "task",
      template: "LLM",
      configurationFieldCount: 1,
      validationIssueCount: 0,
    })).toMatchObject({
      configuration: false,
      implementation: true,
      advanced: false,
    });
  });

  it("offers input uploads only to file-based Sources", () => {
    expect(nodeSupportsInputFiles("source", "File")).toBe(true);
    expect(nodeSupportsInputFiles("source", "Folder")).toBe(true);
    expect(nodeSupportsInputFiles("source", "User Upload")).toBe(true);
    expect(nodeSupportsInputFiles("source", "Source")).toBe(true);
    expect(nodeSupportsInputFiles("source", "Audio Upload")).toBe(true);
    expect(nodeSupportsInputFiles("source", "Document Input")).toBe(true);
    expect(nodeSupportsInputFiles("source", "Database")).toBe(true);
    expect(nodeSupportsInputFiles("source", "REST API")).toBe(true);
    expect(nodeSupportsInputFiles("task", "Speech-to-Text")).toBe(false);
    expect(nodeSupportsInputFiles("destination", "File")).toBe(false);
  });

  it("accepts managed Python implementations and flags preserved legacy metadata", () => {
    expect(taskImplementationMigrationError({ kind: "python", language: "python" })).toBe("");
    expect(taskImplementationMigrationError({ kind: "generated-code" })).toBe("");
    expect(taskImplementationMigrationError({ kind: "container", image: "legacy:1" }))
      .toContain("unsupported");
    expect(taskImplementationMigrationError({ kind: "python", language: "R" }))
      .toContain("Migrate this Task to Python");
  });

  it("derives the simple managed-package status", () => {
    expect(taskImplementationStatus({
      implementation: { kind: "python" },
      hasPythonPackage: false,
      isGenerating: false,
      hasImplementationErrors: false,
    })).toBe("missing");
    expect(taskImplementationStatus({
      implementation: { kind: "python" },
      artifact: { status: "stale" },
      hasPythonPackage: true,
      isGenerating: false,
      hasImplementationErrors: false,
    })).toBe("stale");
    expect(taskImplementationStatus({
      implementation: { kind: "container", image: "legacy:1" },
      hasPythonPackage: true,
      isGenerating: false,
      hasImplementationErrors: false,
    })).toBe("invalid");
  });
});
