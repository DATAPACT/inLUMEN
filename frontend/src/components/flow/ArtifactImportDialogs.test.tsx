import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CodeImportConsentDialog, ImportSummaryDialog } from "./ArtifactImportDialogs";
import type { ArtifactDesign, CodeZipConsentRequest } from "@/features/flow/designFromArtifact";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const request: CodeZipConsentRequest = {
  kind: "code-zip", needs_model: true, name: "Sensor pipeline",
  files_to_send: [
    { path: "sensor_pipeline/README.md", part: "whole file", chars: 1318 },
    { path: "sensor_pipeline/main.py", part: "whole file", chars: 2900 },
    { path: "sensor_pipeline/sample_input/sensor_readings.csv", part: "first line only", chars: 33 },
  ],
  files_not_sent: [{ path: "sensor_pipeline/.env", size_bytes: 20 }],
  total_chars: 4251,
};

let root: Root | null = null;

// The dialogs render into document.body through a portal.
const show = async (element: ReactElement) => {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root!.render(element));
};
const text = () => document.body.textContent ?? "";
const button = (label: RegExp) =>
  [...document.body.querySelectorAll("button")].find((item) => label.test(item.textContent ?? ""))!;
const click = (label: RegExp) => act(async () => button(label).click());

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("code import consent dialog", () => {
  const showDialog = async (props: Partial<Parameters<typeof CodeImportConsentDialog>[0]> = {}) => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    await show(
      <CodeImportConsentDialog
        request={request} fileName="sensor_pipeline.zip" modelName="openrouter / openai/gpt-oss-120b"
        busy={false} onConfirm={onConfirm} onCancel={onCancel} {...props}
      />,
    );
    return { onConfirm, onCancel };
  };

  it("names the model and lists what is sent of each file", async () => {
    await showDialog();
    expect(text()).toContain("Build a design from this code?");
    expect(text()).toContain("sensor_pipeline.zip");
    expect(text()).toContain("openrouter / openai/gpt-oss-120b");
    const rows = [...document.body.querySelectorAll("li")].map((item) => item.textContent ?? "");
    expect(rows.find((row) => row.includes("main.py"))).toContain("whole file");
    expect(rows.find((row) => row.includes("sensor_readings.csv"))).toContain("first line only");
    expect(text()).toContain("1 other file is not sent");
    // An empty canvas loses nothing, so there is no warning.
    expect(text()).not.toContain("replaces");
  });

  it("says how many steps the design replaces and that Undo brings them back", async () => {
    await showDialog({ replacedSteps: 4 });
    expect(text()).toContain("The design replaces the 4 steps on the canvas. Undo brings them back.");
  });

  it("sends only on the confirm button and can be cancelled", async () => {
    const { onConfirm, onCancel } = await showDialog();
    await click(/^Cancel$/);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
    await click(/Send and build design/);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("locks both buttons while the model reads the code", async () => {
    await showDialog({ busy: true });
    expect(button(/Building the design/).disabled).toBe(true);
    expect(button(/^Cancel$/).disabled).toBe(true);
  });

  it("is closed without a request", async () => {
    await showDialog({ request: null });
    expect(text()).not.toContain("Build a design from this code?");
  });
});

describe("import summary dialog", () => {
  const design = (kind: ArtifactDesign["kind"], notes: string[]): ArtifactDesign =>
    ({ kind, project: {}, notes, step_count: 5, connection_count: 4 });

  it("shows the caution once for a design inferred from code", async () => {
    await show(<ImportSummaryDialog design={design("code-zip", ["Caution from the backend.", "One task reads the file."])} onClose={() => {}} />);
    expect(text()).toContain("Design imported from code");
    expect(text()).toContain("5 steps and 4 connections are on the canvas.");
    expect(text()).toContain("Check each step against the code.");
    expect(text()).not.toContain("Caution from the backend.");
    expect(text()).toContain("One task reads the file.");
  });

  it("lists every note for a bundle or a workflow", async () => {
    const onClose = vi.fn();
    await show(<ImportSummaryDialog design={design("argo-workflow", ["Built from tasks.", "Ports have the type any."])} onClose={onClose} />);
    expect(text()).toContain("Design imported from Argo workflow");
    expect(text()).toContain("Built from tasks.");
    expect(text()).not.toContain("Check each step against the code.");
    await click(/^Close$/);
    expect(onClose).toHaveBeenCalled();
  });
});
