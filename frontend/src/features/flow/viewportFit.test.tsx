import { act } from "react";
import { createRoot } from "react-dom/client";
import type { ReactFlowInstance } from "reactflow";
import { describe, expect, it, vi } from "vitest";

import {
  EMPTY_GRAPH_VIEWPORT,
  GRAPH_FIT_VIEW_OPTIONS,
  useGraphViewportFit,
} from "@/features/flow/viewportFit";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const Harness = ({
  instance,
  nodesInitialized,
  nodeCount,
}: {
  instance: Pick<ReactFlowInstance, "fitView" | "setViewport">;
  nodesInitialized: boolean;
  nodeCount: number;
}) => {
  const requestFit = useGraphViewportFit({
    instance,
    nodesInitialized,
    nodeCount,
  });
  return <button onClick={requestFit}>Fit loaded graph</button>;
};

describe("version graph viewport fitting", () => {
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

    expect(instance.fitView).toHaveBeenCalledOnce();
    expect(instance.fitView).toHaveBeenCalledWith(GRAPH_FIT_VIEW_OPTIONS);
    expect(instance.setViewport).not.toHaveBeenCalled();
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
    await act(async () => {
      root.render(<Harness instance={instance} nodesInitialized nodeCount={4} />);
      container.querySelector("button")?.click();
    });

    expect(instance.fitView).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
  });
});
