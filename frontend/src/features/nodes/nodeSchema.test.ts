import { describe, expect, it } from "vitest";

import {
  getNodeFileBucket,
  getNodeFileName,
  isImagePreviewName,
  isTextPreviewFile,
  isTextPreviewName,
  normalizeStorageDatabaseOption,
  normalizeType,
  pickBackendUpdatableProps,
} from "@/features/nodes/nodeSchema";


describe("node schema compatibility", () => {
  it("normalizes canonical, aliased, and generated step types", () => {
    expect(normalizeType(" INPUT ")).toBe("input");
    expect(normalizeType("data source")).toBe("input");
    expect(normalizeType("feature-engineering")).toBe("action");
    expect(normalizeType("quality_report_writer")).toBe("output");
    expect(normalizeType("external endpoint client")).toBe("api");
    expect(normalizeType("unrecognized step")).toBe("action");
  });

  it("normalizes supported storage database choices", () => {
    expect(normalizeStorageDatabaseOption("sqlite")).toBe("SQLite");
    expect(normalizeStorageDatabaseOption(" CHROMADB ")).toBe("ChromaDB");
    expect(normalizeStorageDatabaseOption("unknown")).toBe("MinIO");
  });

  it("reads browser, persisted, and legacy file references", () => {
    const browserFile = new File(["content"], "input.csv", { type: "text/csv" });

    expect(getNodeFileName(browserFile)).toBe("input.csv");
    expect(getNodeFileName({ filename: "persisted.json" })).toBe("persisted.json");
    expect(getNodeFileName("legacy.txt")).toBe("legacy.txt");
    expect(getNodeFileBucket({ filename: "x", bucket: " Custom-Bucket " }, "7"))
      .toBe("Custom-Bucket");
    expect(getNodeFileBucket("legacy.txt", "7")).toBe("files-step-id-7");
  });

  it("selects only properties supported by a storage node", () => {
    const properties = pickBackendUpdatableProps(
      "9",
      {
        label: "Vector store",
        description: "Persist embeddings",
        database: "SQLite",
        endpoint: "sqlite:///vectors.db",
        content: "not applicable",
        param: { ignored: true },
        definition_id: " core.clipboard ",
        definition_version: 2,
        implementation: { mode: "durable" },
        configuration_status: "valid",
        generated_artifact: { status: "current" },
      },
      "storage",
    );

    expect(properties).toEqual({
      flow_id: "9",
      label: "Vector store",
      type: "storage",
      description: "Persist embeddings",
      definition_id: "core.clipboard",
      definition_version: 2,
      implementation: { mode: "durable" },
      configuration_status: "valid",
      generated_artifact: { status: "current" },
      has_files: "no",
      endpoint: "sqlite:///vectors.db",
      database: "sqlite",
    });
  });

  it("detects text and image previews", () => {
    expect(isTextPreviewName("Dockerfile.runtime")).toBe(true);
    expect(isTextPreviewName("pipeline.YAML")).toBe(true);
    expect(isImagePreviewName("diagram.SVG")).toBe(true);
    expect(isImagePreviewName("archive.zip")).toBe(false);
    expect(isTextPreviewFile(new File(["hello"], "README", { type: "text/plain" })))
      .toBe(true);
  });
});
