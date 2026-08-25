import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Node } from "reactflow";
import { describe, expect, it, vi } from "vitest";

import { PropertiesPanel, type PropertyNodeData } from "@/components/PropertiesPanel";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("PropertiesPanel", () => {
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
    expect(container.textContent).toContain("Advanced connection settings");
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
