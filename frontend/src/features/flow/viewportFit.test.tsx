import { act } from "react";
import { createRoot } from "react-dom/client";
import type { ReactFlowInstance } from "reactflow";
import { describe, expect, it, vi } from "vitest";

import {
  ASSISTANT_GRAPH_FIT_DURATION,
  EMPTY_GRAPH_VIEWPORT,
  GRAPH_FIT_VIEW_OPTIONS,
  GRAPH_MIN_ZOOM,
  useGraphViewportFit,
} from "@/features/flow/viewportFit";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const waitForViewportFit = () => new Promise<void>((resolve) => {
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve()));
});

const Harness = ({
  instance,
  nodesInitialized,
  nodeCount,
  duration,
}: {
  instance: Pick<ReactFlowInstance, "fitView" | "setViewport">;
  nodesInitialized: boolean;
  nodeCount: number;
  duration?: number;
}) => {
  const requestFit = useGraphViewportFit({
    instance,
    nodesInitialized,
    nodeCount,
  });
  return <button onClick={() => requestFit(duration == null ? undefined : { duration })}>Fit loaded graph</button>;
};

describe("version graph viewport fitting", () => {
  it("allows assistant updates to zoom out enough to keep the full graph visible", () => {
    expect(GRAPH_MIN_ZOOM).toBe(0.15);
    expect(GRAPH_FIT_VIEW_OPTIONS).toMatchObject({
      padding: 0.35,
      minZoom: 0.15,
      maxZoom: 0.9,
    });
  });

  it("waits for newly loaded nodes before fitting their bounds", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const instance = {
      fitView: vi.fn(() => true),
      setViewport: vi.fn(),
    };

    await act(async () => {
      root.render(
        <Harness instance={instance} nodesInitialized={false} nodeCount={3} />,
      );
    });
    await act(async () => {
      container.querySelector("button")?.click();
    });

    expect(instance.fitView).not.toHaveBeenCalled();

    await act(async () => {
      root.render(
        <Harness instance={instance} nodesInitialized nodeCount={3} />,
      );
    });
    await act(waitForViewportFit);

    expect(instance.fitView).toHaveBeenCalledOnce();
    expect(instance.fitView).toHaveBeenCalledWith(GRAPH_FIT_VIEW_OPTIONS);
    expect(instance.setViewport).not.toHaveBeenCalled();
    await act(async () => root.unmount());
  });

  it("does not complete a fit against stale bounds while streamed nodes mount", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const instance = {
      fitView: vi.fn(() => true),
      setViewport: vi.fn(),
    };

    await act(async () => {
      root.render(<Harness instance={instance} nodesInitialized nodeCount={2} />);
    });
    await act(async () => {
      container.querySelector("button")?.click();
      root.render(<Harness instance={instance} nodesInitialized={false} nodeCount={4} />);
    });
    await act(waitForViewportFit);

    expect(instance.fitView).not.toHaveBeenCalled();

    await act(async () => {
      root.render(<Harness instance={instance} nodesInitialized nodeCount={4} />);
    });
    await act(waitForViewportFit);

    expect(instance.fitView).toHaveBeenCalledOnce();
    expect(instance.fitView).toHaveBeenCalledWith(GRAPH_FIT_VIEW_OPTIONS);
    await act(async () => root.unmount());
  });

  it("resets the viewport when Main contains an empty graph", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const instance = {
      fitView: vi.fn(() => true),
      setViewport: vi.fn(),
    };

    await act(async () => {
      root.render(
        <Harness instance={instance} nodesInitialized={false} nodeCount={0} />,
      );
    });
    await act(async () => {
      container.querySelector("button")?.click();
    });

    expect(instance.setViewport).toHaveBeenCalledOnce();
    expect(instance.setViewport).toHaveBeenCalledWith(EMPTY_GRAPH_VIEWPORT, {
      duration: GRAPH_FIT_VIEW_OPTIONS.duration,
    });
    expect(instance.fitView).not.toHaveBeenCalled();
    await act(async () => root.unmount());
  });

  it("refits after each requested assistant graph update", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const instance = {
      fitView: vi.fn(() => true),
      setViewport: vi.fn(),
    };

    await act(async () => {
      root.render(<Harness instance={instance} nodesInitialized nodeCount={2} />);
    });
    await act(async () => {
      container.querySelector("button")?.click();
    });
    await act(waitForViewportFit);
    await act(async () => {
      root.render(<Harness instance={instance} nodesInitialized nodeCount={4} />);
      container.querySelector("button")?.click();
    });
    await act(waitForViewportFit);

    expect(instance.fitView).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
  });

  it("uses a short fit transition for streamed assistant updates", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const instance = {
      fitView: vi.fn(() => true),
      setViewport: vi.fn(),
    };

    await act(async () => {
      root.render(
        <Harness
          instance={instance}
          nodesInitialized
          nodeCount={4}
          duration={ASSISTANT_GRAPH_FIT_DURATION}
        />,
      );
    });
    await act(async () => {
      container.querySelector("button")?.click();
    });
    await act(waitForViewportFit);

    expect(instance.fitView).toHaveBeenCalledWith({
      ...GRAPH_FIT_VIEW_OPTIONS,
      duration: ASSISTANT_GRAPH_FIT_DURATION,
    });
    await act(async () => root.unmount());
  });
});
