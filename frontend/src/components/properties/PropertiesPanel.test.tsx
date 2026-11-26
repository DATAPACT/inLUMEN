import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Node } from "reactflow";
import { describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/utils/apiFetch";

vi.mock("@/utils/apiFetch", () => ({ apiFetch: vi.fn() }));

import { PropertiesPanel, type PropertyNodeData } from "@/components/PropertiesPanel";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("PropertiesPanel", () => {
  it("identifies the selected node and updates the header when selection changes", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    for (const [id, label, expected] of [["source-a", "Input records", "Input records"], ["source-b", "", "Untitled Source"]]) {
      await act(async () => root.render(<PropertiesPanel selectedNode={{
        id, type: "custom", position: { x: 0, y: 0 },
        data: { type: "source", label },
      }} onNodeUpdate={vi.fn()} />));
      const header = container.querySelector("h2")?.parentElement;
      expect(header?.textContent).toContain(expected);
      expect(header?.textContent).toContain(`ID: ${id}`);
      if (id === "source-b") expect(header?.textContent).not.toContain("Input records");
    }
    await act(async () => root.unmount());
  });

  it("uses canonical upload and replacement references without retaining old snapshots", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const onNodeUpdate = vi.fn();
    const bucket = "files-ws-opaque-1-hash";
    await act(async () => root.render(<PropertiesPanel selectedNode={{
      id: "1", type: "custom", position: { x: 0, y: 0 },
      data: { type: "source", label: "Input", files: [] },
    }} onNodeUpdate={onNodeUpdate} />));
    for (const content of ["first", "replacement"]) {
      vi.mocked(apiFetch).mockResolvedValueOnce(new Response(JSON.stringify({
        file_reference: { filename: "input.csv", bucket, role: "data" },
      })));
      const input = container.querySelector('input[type="file"]') as HTMLInputElement;
      Object.defineProperty(input, "files", { configurable: true, value: [new File([content], "input.csv")] });
      await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));
      const update = onNodeUpdate.mock.lastCall!;
      expect(JSON.stringify(update)).toContain(bucket);
      expect(JSON.stringify(update)).not.toContain("files-step-id");
      expect(container.textContent?.match(/input.csv/g)).toHaveLength(1);
    }
    await act(async () => root.unmount());
  });

  it("keeps edits made while a file upload is pending", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const onNodeUpdate = vi.fn();
    const original: Node<PropertyNodeData> = {
      id: "1", type: "custom", position: { x: 0, y: 0 },
      data: { type: "source", label: "Before upload", files: [] },
    };
    let finishUpload!: (response: Response) => void;
    const pending = new Promise<Response>(resolve => { finishUpload = resolve; });
    vi.mocked(apiFetch).mockImplementation(async (_url, init) =>
      init?.body instanceof FormData ? pending : Response.json({ names: [] }));
    await act(async () => root.render(<PropertiesPanel selectedNode={original} onNodeUpdate={onNodeUpdate} />));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, "files", { configurable: true, value: [new File(["value\n1\n"], "input.csv")] });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
    const edited = { ...original, data: { ...original.data, label: "Typed during upload", description: "New description" } };
    await act(async () => root.render(<PropertiesPanel selectedNode={edited} onNodeUpdate={onNodeUpdate} />));
    await act(async () => finishUpload(Response.json({ file_reference: {
      filename: "input.csv", bucket: "files-ws-isolated-1", role: "data",
    } })));
    expect(onNodeUpdate.mock.lastCall?.[1]).toMatchObject({
      label: "Typed during upload", description: "New description",
      files: [{ filename: "input.csv", bucket: "files-ws-isolated-1", role: "data" }],
    });
    await act(async () => root.unmount());
  });

  it("renders an uploaded Source file without crashing", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const sourceNode: Node<PropertyNodeData> = {
      id: "1",
      type: "custom",
      position: { x: 0, y: 0 },
      data: {
        type: "source",
        label: "Audio Upload",
        description: "Receives audio recordings.",
        param: { language: "en" },
        files: [
          {
            filename: "customer_complaint.wav",
            bucket: "files-step-id-1",
            role: "data",
          },
          {
            filename: "main.py",
            bucket: "files-step-id-1",
            role: "code",
          },
        ],
      },
    };

    await act(async () => {
      root.render(
        <PropertiesPanel
          selectedNode={sourceNode}
          onNodeUpdate={vi.fn()}
        />,
      );
    });

    expect(container.textContent).toContain("customer_complaint.wav");
    expect(container.textContent).toContain("Input Files");
    expect(container.textContent).not.toContain("Sample Inputs");
    expect(container.textContent).toContain("Connection");
    expect(container.textContent).toContain("No setup is needed for most pipelines");
    expect(container.textContent).toContain("Advanced settings");
    expect(container.textContent).not.toContain("Default boundary");
    expect(container.textContent).toContain("language");
    expect(container.textContent).not.toContain("Implementation");
    expect(container.textContent).not.toContain("main.py");
    expect(container.textContent).not.toContain("Source override");
    expect(container.textContent).not.toContain("Implementation override");
    expect(container.querySelector('[aria-label="Remove customer_complaint.wav"]')).not.toBeNull();

    await act(async () => root.unmount());
  });

  it("does not expose input uploads on Task nodes", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <PropertiesPanel
          selectedNode={{
            id: "task-1",
            type: "custom",
            position: { x: 0, y: 0 },
            data: {
              type: "task",
              label: "Transform",
              implementation: { kind: "python" },
            },
          }}
          onNodeUpdate={vi.fn()}
        />,
      );
    });

    expect(container.textContent).not.toContain("Input Files");
    expect(container.textContent).not.toContain("Sample Inputs");
    expect(container.textContent).toContain("Implementation");
    expect(container.textContent).toContain("Task runtime contract");
    expect(container.textContent).toContain("PIPELINE_INPUT_DIR");
    expect(container.textContent).toContain("Port names never create implicit subdirectories");
    expect(container.textContent).toContain("PIPELINE_OUTPUT_DIR");
    await act(async () => root.unmount());
  });

  it("adds detected environment variables without inventing values or duplicating parameters", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <PropertiesPanel
          selectedNode={{
            id: "task-weather",
            type: "custom",
            position: { x: 0, y: 0 },
            data: {
              type: "task",
              label: "Fetch Weather",
              files: [{ filename: "main.py", role: "code" }],
              generated_artifact: {
                status: "current",
                runtime_environment: [
                  { name: "API_ENDPOINT", required: true, secret: false },
                  { name: "API_KEY", required: false, secret: true },
                ],
              },
            },
          }}
          onNodeUpdate={vi.fn()}
        />,
      );
    });

    expect(container.textContent).toContain("Environment variables detected");
    expect(container.textContent).toContain("API_ENDPOINT");
    expect(container.textContent).toContain("API_KEY");
    expect(container.textContent).toContain("Required");
    expect(container.textContent).toContain("Optional");
    expect(container.textContent).toContain("Sensitive");
    expect(container.textContent).toContain("Click a variable to add it to Runtime parameters");
    expect(container.textContent).toContain("The pipeline assistant never fills this section");
    expect(container.textContent).toContain("No parameters added");
    document.body.appendChild(container);
    const add = container.querySelector('[aria-label="Add API_ENDPOINT to Runtime parameters"]') as HTMLButtonElement;
    await act(async () => add.click());
    const value = container.querySelector('#parameter-API_ENDPOINT') as HTMLInputElement;
    expect(value.value).toBe("");
    expect(document.activeElement).toBe(value);
    expect(add.disabled).toBe(true);
    expect(add.textContent).toContain("Added");
    await act(async () => add.click());
    expect(container.querySelectorAll('#parameter-API_ENDPOINT')).toHaveLength(1);
    await act(async () => (container.querySelector('[aria-label="Add API_KEY to Runtime parameters"]') as HTMLButtonElement).click());
    const secret = container.querySelector('#parameter-API_KEY') as HTMLInputElement;
    expect(secret.type).toBe("password");
    expect(secret.value).toBe("");
    expect(document.activeElement).toBe(secret);
    await act(async () => (container.querySelector('[aria-label="Remove parameter API_ENDPOINT"]') as HTMLButtonElement).click());
    expect(add.disabled).toBe(false);
    await act(async () => root.unmount());
    container.remove();
  });

  it("configures a plugin-backed Task instead of uploaded code", async () => {
    const { setPluginRegistryState } = await import("@/features/plugins/pluginRegistry");
    const manifest = {
      schema_version: "inlumen.plugin@1" as const,
      id: "datapact.text-anonymizer",
      version: "1.0.0",
      name: "Text Anonymizer",
      description: "Removes personal identifiers.",
      step_type: "task" as const,
      endpoint: { base_url: "http://tool.example" },
      operations: [{ id: "anonymize", method: "POST", path: "/v1/anonymize" }],
      parameters: [{ name: "entities", type: "string" as const, enum: ["email", "all"], default: "all" }],
      secrets: [{ name: "api_key", label: "API key", required: true }],
      ports: { inputs: [{ name: "text" }], outputs: [{ name: "anonymized" }] },
    };
    setPluginRegistryState({
      plugins: [{
        id: manifest.id, version: "1.0.0", name: manifest.name, description: manifest.description,
        publisher: "", step_type: "task", template: "plugin:datapact.text-anonymizer",
        source: "manual", installed_at: "", manifest,
      }],
      canManage: false,
      error: "",
    });
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => root.render(<PropertiesPanel selectedNode={{
      id: "task-plugin", type: "custom", position: { x: 0, y: 0 },
      data: {
        type: "task", label: "Anonymize", template_label: "plugin:datapact.text-anonymizer",
        param: { operation: "anonymize", entities: "email", api_key: "" }, secret_params: ["api_key"],
      },
    }} onNodeUpdate={vi.fn()} />));
    expect(container.textContent).toContain("Text Anonymizer");
    expect(container.textContent).toContain("http://tool.example/v1/anonymize");
    expect(container.textContent).not.toContain("Generate with AI");
    expect((container.querySelector("#plugin-parameter-entities") as HTMLSelectElement).value).toBe("email");
    expect((container.querySelector("#plugin-parameter-api_key") as HTMLInputElement).type).toBe("password");
    expect(container.textContent).not.toContain("Add output");
    await act(async () => root.unmount());
    setPluginRegistryState({ plugins: [] });
  });

  it("stores Task outputs as a hidden parameter and keeps connected outputs locked", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const onNodeUpdate = vi.fn();
    const task = (data: Record<string, unknown>): Node<PropertyNodeData> => ({
      id: "split", type: "custom", position: { x: 0, y: 0 },
      data: { type: "task", label: "Split", param: { threshold: "2" }, ...data },
    });
    await act(async () => root.render(<PropertiesPanel selectedNode={task({
      connected_ports: { inputs: [], outputs: ["output"] },
    })} onNodeUpdate={onNodeUpdate} />));
    const button = (label: string) => [...container.querySelectorAll("button")].find((item) => item.textContent?.includes(label))!;
    await act(async () => button("Add output").dispatchEvent(new MouseEvent("click", { bubbles: true })));
    const file = container.querySelector('input[aria-label="File published on output_2"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(file, "report.json");
      file.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const update = onNodeUpdate.mock.lastCall![1];
    expect(update.ports.outputs.map((port: { id: string }) => port.id)).toEqual(["output", "output_2"]);
    expect(update.param).toEqual({
      threshold: "2",
      output_artifacts: [{ name: "output_2", port: "output_2", filename: "report.json", kind: "json", format: "json" }],
    });
    expect(update.implementation?.output_artifacts).toBeUndefined();

    // The canvas hands back the updated node without connected_ports.
    await act(async () => root.render(<PropertiesPanel selectedNode={task({ ports: update.ports, param: update.param })} onNodeUpdate={onNodeUpdate} />));
    expect((container.querySelector('input[aria-label="Output 1 name"]') as HTMLInputElement).disabled).toBe(true);
    expect([...container.querySelectorAll("input")].some((input) => input.value === "output_artifacts")).toBe(false);
    await act(async () => root.unmount());
  });
});
