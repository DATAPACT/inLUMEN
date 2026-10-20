import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/utils/apiFetch", () => ({ apiFetch: vi.fn() }));

import { PaletteGroup } from "@/components/Sidebar";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const render = async (count: number, open = true, filter = "", matchCount = count) => {
  const container = document.createElement("div");
  const root = createRoot(container);
  const onOpenChange = vi.fn();
  await act(async () => root.render(
    <PaletteGroup title="Plugins" count={count} open={open} onOpenChange={onOpenChange}
      actionLabel="Manage" onAction={vi.fn()} filter={filter} onFilterChange={vi.fn()} matchCount={matchCount}>
      <span data-testid="row">row</span>
    </PaletteGroup>,
  ));
  return { container, root, onOpenChange };
};

describe("PaletteGroup", () => {
  it("shows the count and offers a filter only once the group grows long", async () => {
    const short = await render(5);
    expect(short.container.textContent).toContain("(5)");
    expect(short.container.querySelector('input[aria-label="Filter plugins"]')).toBeNull();
    await act(async () => short.root.unmount());

    const long = await render(6);
    expect(long.container.querySelector('input[aria-label="Filter plugins"]')).not.toBeNull();
    await act(async () => long.root.unmount());
  });

  it("collapses its rows and reports when a filter matches nothing", async () => {
    const closed = await render(8, false);
    expect(closed.container.querySelector('[data-testid="row"]')).toBeNull();
    const toggle = closed.container.querySelector("button[aria-expanded]") as HTMLButtonElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await act(async () => toggle.click());
    expect(closed.onOpenChange).toHaveBeenCalledWith(true);
    await act(async () => closed.root.unmount());

    const empty = await render(8, true, "zzz", 0);
    expect(empty.container.textContent).toContain("No plugins match “zzz”.");
    await act(async () => empty.root.unmount());
  });
});
