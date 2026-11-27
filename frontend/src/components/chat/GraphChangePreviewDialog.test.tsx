import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("reactflow", async () => {
  const actual = await vi.importActual<typeof import("reactflow")>("reactflow");
  // The graph itself needs a real layout engine; the texts and buttons do not.
  return { ...actual, default: () => null, Background: () => null, Controls: () => null };
});

import { GraphChangePreviewDialog } from "./GraphChangePreviewDialog";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const node = (id: string, label: string) => ({ id, type: "custom", position: { x: 0, y: 0 }, data: { type: "task", label } });
const baseline = { nodes: [node("1", "Old step")], edges: [] };
const proposal = { nodes: [node("7", "Readings"), node("8", "Validate")], edges: [{ id: "e", source: "7", target: "8" }] };

let root: Root | null = null;
const show = async (element: ReactElement) => {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root!.render(element));
};
const text = () => document.body.textContent ?? "";
const button = (label: RegExp) =>
  [...document.body.querySelectorAll("button")].find((item) => label.test(item.textContent ?? ""))!;

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("graph change preview dialog", () => {
  it("keeps its texts for a proposal of the assistant", async () => {
    await show(<GraphChangePreviewDialog open baseline={baseline} proposal={proposal} onApply={() => {}} onDiscard={() => {}} />);
    expect(text()).toContain("Review proposed graph");
    expect(text()).toContain("Your saved pipeline is unchanged.");
    expect(text()).not.toContain("About this proposal");
    expect(button(/Import again/)).toBeUndefined();
    expect(text()).toContain("Readings");
    expect(text()).toContain("Old step"); // listed as removed
  });

  it("shows an import with its own texts, its notes and a second action", async () => {
    const onApply = vi.fn();
    const onDiscard = vi.fn();
    const again = vi.fn();
    await show(
      <GraphChangePreviewDialog
        open baseline={baseline} proposal={proposal} onApply={onApply} onDiscard={onDiscard}
        title="Review the imported design (from code)"
        description="Applying it replaces the current canvas."
        notes={["Check each step against the code.", "3 file(s) were read."]}
        secondaryAction={{ label: "Import again", onClick: again }}
        replacesCanvas
      />,
    );
    expect(text()).toContain("Review the imported design (from code)");
    expect(text()).toContain("Applying it replaces the current canvas.");
    expect(text()).toContain("About this proposal");
    expect(text()).toContain("Check each step against the code.");
    await act(async () => button(/Import again/).click());
    expect(again).toHaveBeenCalledTimes(1);
    await act(async () => button(/Apply to canvas/).click());
    expect(onApply).toHaveBeenCalledTimes(1);
    await act(async () => button(/Discard proposal/).click());
    expect(onDiscard).toHaveBeenCalledTimes(1);
  });

  it("locks applying while the proposal is built again, and allows an identical design to replace the canvas", async () => {
    await show(
      <GraphChangePreviewDialog
        open baseline={proposal} proposal={proposal} onApply={() => {}} onDiscard={() => {}}
        secondaryAction={{ label: "Import again", onClick: () => {}, busy: true }} replacesCanvas
      />,
    );
    expect(button(/Import again/).disabled).toBe(true);
    expect(button(/Apply to canvas/).disabled).toBe(true);

    await act(async () => root?.unmount());
    document.body.innerHTML = "";
    await show(<GraphChangePreviewDialog open baseline={proposal} proposal={proposal} onApply={() => {}} onDiscard={() => {}} replacesCanvas />);
    expect(button(/Apply to canvas/).disabled).toBe(false);
    await act(async () => root?.unmount());
    document.body.innerHTML = "";
    await show(<GraphChangePreviewDialog open baseline={proposal} proposal={proposal} onApply={() => {}} onDiscard={() => {}} />);
    expect(button(/Apply to canvas/).disabled).toBe(true);
  });
});
