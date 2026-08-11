import { describe, expect, it, vi } from "vitest";

import {
  notifyReusablePipelineCatalogChanged,
  REUSABLE_PIPELINE_CATALOG_CHANGED_EVENT,
} from "@/features/flow/subpipelinePersistence";

describe("reusable pipeline catalog notifications", () => {
  it("notifies mounted catalog consumers after an agent or manual mutation", () => {
    const listener = vi.fn();
    window.addEventListener(REUSABLE_PIPELINE_CATALOG_CHANGED_EVENT, listener);

    notifyReusablePipelineCatalogChanged();

    expect(listener).toHaveBeenCalledOnce();
    expect(listener.mock.calls[0][0]).toBeInstanceOf(Event);
    window.removeEventListener(REUSABLE_PIPELINE_CATALOG_CHANGED_EVENT, listener);
  });
});
