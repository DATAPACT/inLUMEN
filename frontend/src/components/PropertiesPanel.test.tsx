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
    expect(container.textContent).toContain("Parameters");
    expect(container.textContent).toContain("language");
    expect(container.textContent).toContain("Implementation");
    expect(container.textContent).toContain("main.py");
    expect(container.textContent).not.toContain("Advanced");
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
    await act(async () => root.unmount());
  });
});
