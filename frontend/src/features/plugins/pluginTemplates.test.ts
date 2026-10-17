import { afterEach, describe, expect, it } from "vitest";

import {
  findTemplateForType,
  setPluginTemplates,
  templateOptionsForType,
} from "@/features/nodes/templateCatalog";
import { createNodeDataFromPluginTemplate, getFallbackNodeDefinitions } from "@/features/nodes/registry/nodeRegistry";
import { nodeSupportsInputFiles } from "@/features/nodes/propertyPanelPolicy";
import type { InstalledPlugin, PluginManifest } from "./pluginApi";
import { isPluginTemplate, pluginIdFromTemplate, pluginToTemplate } from "./pluginTemplates";

const manifest: PluginManifest = {
  schema_version: "inlumen.plugin@1",
  id: "datapact.text-anonymizer",
  version: "1.0.0",
  name: "Text Anonymizer",
  description: "Removes personal identifiers.",
  publisher: "DataPACT",
  step_type: "task",
  endpoint: { base_url: "http://tool.example", health_path: "/health" },
  operations: [{ id: "anonymize", method: "POST", path: "/v1/anonymize" }],
  parameters: [
    { name: "entities", type: "string", enum: ["email", "all"], default: "all", location: "query" },
    { name: "language", type: "string", required: true, location: "query" },
  ],
  secrets: [{ name: "api_key", label: "API key", required: true }],
  auth: { type: "api_key", secret: "api_key" },
  ports: { inputs: [{ name: "text", type: "Text" }], outputs: [{ name: "anonymized", type: "Object" }] },
};

const installed: InstalledPlugin = {
  id: manifest.id,
  version: manifest.version,
  name: manifest.name,
  description: manifest.description,
  publisher: "DataPACT",
  step_type: "task",
  template: "plugin:datapact.text-anonymizer",
  source: "manual",
  installed_at: "",
  manifest,
};

afterEach(() => setPluginTemplates([]));

describe("plugin templates", () => {
  it("parses plugin template values", () => {
    expect(pluginIdFromTemplate(" Plugin:datapact.text-anonymizer ")).toBe("datapact.text-anonymizer");
    expect(isPluginTemplate("REST API")).toBe(false);
  });

  it("starts annotation plugins with annotations-only output", () => {
    const annotator: PluginManifest = {
      ...manifest,
      operations: [{ id: "annotate", method: "POST", path: "/annotate", response: { merge: { key: "wikifier" } } }],
    };
    expect(pluginToTemplate({ ...installed, manifest: annotator }).defaultParameters).toMatchObject({
      operation: "annotate", output_mode: "annotations",
    });
    expect(pluginToTemplate(installed).defaultParameters).not.toHaveProperty("output_mode");
  });

  it("maps a manifest onto the component template model", () => {
    const template = pluginToTemplate(installed);
    expect(template.value).toBe("plugin:datapact.text-anonymizer");
    expect(template.category).toBe("Plugins");
    expect(template.requiredParameters).toEqual(["language"]);
    expect(template.defaultParameters).toEqual({
      operation: "anonymize", entities: "all", language: "", api_key: "",
    });
    expect(template.configurationFields?.find((field) => field.name === "api_key")?.secret).toBe(true);
    expect(template.ports?.inputs.map((port) => port.id)).toEqual(["text"]);
  });

  it("extends the catalog only for the plugin's step type", () => {
    setPluginTemplates([pluginToTemplate(installed)]);
    expect(findTemplateForType("task", "plugin:datapact.text-anonymizer")?.label).toBe("Text Anonymizer");
    expect(findTemplateForType("source", "plugin:datapact.text-anonymizer")).toBeUndefined();
    setPluginTemplates([]);
    const options = templateOptionsForType("task", "plugin:datapact.text-anonymizer");
    expect(options[0].label).toContain("not installed");
  });

  it("creates palette node data with secrets flagged", () => {
    const definition = getFallbackNodeDefinitions().find((item) => item.base_type === "task")!;
    const data = createNodeDataFromPluginTemplate(definition, pluginToTemplate(installed));
    expect(data.template_label).toBe("plugin:datapact.text-anonymizer");
    expect(data.secret_params).toEqual(["api_key"]);
    expect(data.ports.outputs.map((port) => port.id)).toEqual(["anonymized"]);
  });

  it("does not offer input uploads on plugin sources", () => {
    expect(nodeSupportsInputFiles("source", "plugin:acme.weather")).toBe(false);
    expect(nodeSupportsInputFiles("source", "Custom")).toBe(true);
  });
});
