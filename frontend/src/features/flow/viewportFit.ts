import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactFlowInstance } from "reactflow";

type ViewportController = Pick<ReactFlowInstance, "fitView" | "setViewport">;

export const GRAPH_FIT_VIEW_OPTIONS = {
  padding: 0.35,
  duration: 250,
  minZoom: 0.15,
  maxZoom: 0.9,
} as const;

export const GRAPH_MIN_ZOOM = GRAPH_FIT_VIEW_OPTIONS.minZoom;
export const ASSISTANT_GRAPH_FIT_DURATION = 80;
export const RESIZE_GRAPH_FIT_DURATION = 120;

export const EMPTY_GRAPH_VIEWPORT = {
  x: 0,
  y: 0,
  zoom: 1,
} as const;

export const useGraphViewportFit = ({
  instance,
  nodesInitialized,
  nodeCount,
}: {
  instance: ViewportController | null;
  nodesInitialized: boolean;
  nodeCount: number;
}) => {
  const [request, setRequest] = useState<{ revision: number; duration: number }>({
    revision: 0,
    duration: GRAPH_FIT_VIEW_OPTIONS.duration,
  });
  const completedRevisionRef = useRef(0);

  const requestFit = useCallback((options?: { duration?: number }) => {
    setRequest((current) => ({
      revision: current.revision + 1,
      duration: options?.duration ?? GRAPH_FIT_VIEW_OPTIONS.duration,
    }));
  }, []);

  useEffect(() => {
    if (!instance || completedRevisionRef.current === request.revision) return;

    if (nodeCount === 0) {
      completedRevisionRef.current = request.revision;
      instance.setViewport(EMPTY_GRAPH_VIEWPORT, {
        duration: request.duration,
      });
      return;
    }

    // React Flow cannot calculate bounds until every newly loaded node has
    // dimensions. Keep the request pending until its internal measurements are
    // ready instead of fitting the previous version's graph.
    if (!nodesInitialized) return;

    completedRevisionRef.current = request.revision;
    instance.fitView({
      ...GRAPH_FIT_VIEW_OPTIONS,
      duration: request.duration,
    });
  }, [instance, nodeCount, nodesInitialized, request]);

  return requestFit;
};
