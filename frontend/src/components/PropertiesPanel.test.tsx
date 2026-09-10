import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Node } from "reactflow";
import { describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/utils/apiFetch";

vi.mock("@/utils/apiFetch", () => ({ apiFetch: vi.fn() }));

import { PropertiesPanel, type PropertyNodeData } from "@/components/PropertiesPanel";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("PropertiesPanel", () => {
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

  it("shows detected environment variables as read-only script warnings", async () => {
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
    expect(container.textContent).toContain("does not create parameters or store values");
    expect(container.textContent).toContain("The pipeline assistant never fills this section");
    expect(container.textContent).toContain("No parameters added");
    await act(async () => root.unmount());
  });
});
