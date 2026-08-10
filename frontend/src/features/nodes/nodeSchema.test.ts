import { describe, expect, it } from "vitest";

import {
  getNodeFileBucket,
  getNodeFileName,
  getNodeFileRole,
  getStepTypeLabel,
  isImagePreviewName,
  isTextPreviewFile,
  isTextPreviewName,
  normalizeImplementationKind,
  normalizeNodePorts,
  normalizeSecretParamKeys,
  normalizeType,
  pickBackendUpdatableProps,
} from "@/features/nodes/nodeSchema";


describe("node schema compatibility", () => {
  it("normalizes legacy and domain vocabulary into five structural kinds", () => {
    expect(normalizeType(" SOURCE ")).toBe("source");
    expect(normalizeType("data source")).toBe("source");
    expect(normalizeType("input")).toBe("source");
    expect(normalizeType("processing step")).toBe("task");
    expect(normalizeType("feature-engineering")).toBe("task");
    expect(normalizeType("storage")).toBe("task");
    expect(normalizeType("API call")).toBe("task");
    expect(normalizeType("quality_report_writer")).toBe("destination");
    expect(normalizeType("human approval")).toBe("flow");
    expect(normalizeType("nested pipeline")).toBe("subpipeline");
    expect(normalizeType("unrecognized step")).toBe("task");
  });

  it("uses structural, implementation-neutral labels", () => {
    expect(getStepTypeLabel("source")).toBe("Source");
    expect(getStepTypeLabel("task")).toBe("Task");
    expect(getStepTypeLabel("destination")).toBe("Destination");
    expect(getStepTypeLabel("flow")).toBe("Flow");
    expect(getStepTypeLabel("subpipeline")).toBe("Subpipeline");
  });

  it("normalizes explicit port contracts and enforces source/destination directionality", () => {
    expect(normalizeNodePorts(undefined, "source")).toEqual({
      inputs: [],
      outputs: [{ id: "data", name: "data", type: "any", required: true, description: "Source data." }],
    });
    expect(normalizeNodePorts({
      inputs: [{ id: "ignored", label: "ignored" }],
      outputs: [
        { id: "Embeddings", label: "embeddings", data_type: "Collection<Vector>" },
        { id: "Embeddings", label: "scores" },
      ],
    }, "source")).toEqual({
      inputs: [],
      outputs: [
        { id: "embeddings", name: "embeddings", type: "Collection<Vector>", required: true, description: "" },
        { id: "embeddings-2", name: "scores", type: "any", required: true, description: "" },
      ],
    });
    expect(normalizeNodePorts(undefined, "destination").outputs).toEqual([]);
    expect(normalizeNodePorts({
      outputs: [{ id: "response", name: "response", description: "Data emitted by this adapter." }],
    }, "source").outputs[0].description).toBe("Data emitted by this source.");
  });

  it("keeps runtime implementation selection independent", () => {
    expect(normalizeImplementationKind("Python")).toBe("python");
    expect(normalizeImplementationKind("git repository")).toBe("repository");
    expect(normalizeImplementationKind("future-runtime")).toBe("python");
  });

  it("infers secret parameters while preserving explicit visibility choices", () => {
    const parameters = { api_key: "secret", threshold: 0.8, accessToken: "token" };

    expect(normalizeSecretParamKeys(undefined, parameters)).toEqual(["api_key"]);
    expect(normalizeSecretParamKeys(["accessToken"], parameters)).toEqual(["accessToken"]);
    expect(normalizeSecretParamKeys([], parameters)).toEqual([]);
  });

  it("reads browser, persisted, and legacy file references", () => {
    const browserFile = new File(["content"], "input.csv", { type: "text/csv" });

    expect(getNodeFileName(browserFile)).toBe("input.csv");
    expect(getNodeFileName({ filename: "persisted.json" })).toBe("persisted.json");
    expect(getNodeFileName("legacy.txt")).toBe("legacy.txt");
    expect(getNodeFileBucket({ filename: "x", bucket: " Custom-Bucket " }, "7"))
      .toBe("Custom-Bucket");
    expect(getNodeFileBucket("legacy.txt", "7")).toBe("files-step-id-7");
    expect(getNodeFileRole({ filename: "records.py", role: "data" })).toBe("data");
    expect(getNodeFileRole({ filename: "main.py" })).toBe("code");
    expect(getNodeFileRole("observations.csv")).toBe("data");
  });

  it("persists structural metadata without technology-specific graph types", () => {
    const properties = pickBackendUpdatableProps(
      "9",
      {
        label: "Speech transcription",
        description: "Transcribe uploaded recordings",
        param: { language: "en" },
        secret_params: [],
        ports: {
          inputs: [{ id: "audio", label: "audio", data_type: "Audio" }],
          outputs: [{ id: "transcript", label: "transcript", data_type: "Document" }],
        },
        definition_id: "core.speech-to-text",
        definition_version: 2,
        template_label: "Speech-to-Text",
        implementation: { kind: "container", image: "example/asr:1" },
        endpoint: "legacy-field-is-not-structural",
        database: "legacy-field-is-not-structural",
      },
      "task",
    );

    expect(properties).toEqual({
      flow_id: "9",
      label: "Speech transcription",
      type: "task",
      description: "Transcribe uploaded recordings",
      param: { language: "en" },
      secret_params: [],
      ports: {
        inputs: [{ id: "audio", name: "audio", type: "Audio", required: true, description: "" }],
        outputs: [{ id: "transcript", name: "transcript", type: "Document", required: true, description: "" }],
      },
      has_files: "no",
      template_label: "Speech-to-Text",
      definition_id: "core.speech-to-text",
      definition_version: 2,
      implementation: { kind: "container", image: "example/asr:1" },
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
