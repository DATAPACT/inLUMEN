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
  const [requestedRevision, setRequestedRevision] = useState(0);
  const completedRevisionRef = useRef(0);

  const requestFit = useCallback(() => {
    setRequestedRevision((revision) => revision + 1);
  }, []);

  useEffect(() => {
    if (!instance || completedRevisionRef.current === requestedRevision) return;

    if (nodeCount === 0) {
      completedRevisionRef.current = requestedRevision;
      instance.setViewport(EMPTY_GRAPH_VIEWPORT, {
        duration: GRAPH_FIT_VIEW_OPTIONS.duration,
      });
      return;
    }

    // React Flow cannot calculate bounds until every newly loaded node has
    // dimensions. Keep the request pending until its internal measurements are
    // ready instead of fitting the previous version's graph.
    if (!nodesInitialized) return;

    completedRevisionRef.current = requestedRevision;
    instance.fitView(GRAPH_FIT_VIEW_OPTIONS);
  }, [instance, nodeCount, nodesInitialized, requestedRevision]);

  return requestFit;
};
