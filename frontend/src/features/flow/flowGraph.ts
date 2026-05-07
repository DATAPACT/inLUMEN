import { Edge, Node } from 'reactflow';
import { normalizeType } from '@/features/nodes/nodeSchema';

export type NormalizedGraph = {
  updated_at: string | null;
  nodes: Node[];
  edges: Edge[];
};

export const normalizeGraph = (data: unknown): NormalizedGraph => {
  const parsedGraph = (data && typeof data === "object" ? data : {}) as {
    nodes?: unknown[];
    edges?: unknown[];
    updated_at?: string | null;
  };
  const incomingNodes = Array.isArray(parsedGraph.nodes) ? parsedGraph.nodes : [];
  const incomingEdges = Array.isArray(parsedGraph.edges) ? parsedGraph.edges : [];

  const nodes: Node[] = incomingNodes.map((nodeEntry) => {
    const node = nodeEntry as Node;
    return {
      ...node,
      id: String(node.id),
      data: {
        ...node.data,
        type: normalizeType(node.data?.type),
      },
    };
  });

  const edges: Edge[] = incomingEdges.map((edgeEntry) => {
    const edge = edgeEntry as Edge;
    return {
      ...edge,
      id: edge?.id ? String(edge.id) : `e-${String(edge.source)}-${String(edge.target)}`,
      source: String(edge.source),
      target: String(edge.target),
    };
  });

  return {
    updated_at: parsedGraph.updated_at ?? null,
    nodes,
    edges,
  };
};

export const getNextNumericNodeId = (nodes: Node[], fallback = 1) => {
  const numericIds = nodes
    .map((node) => parseInt(String(node.id), 10))
    .filter((value) => Number.isFinite(value));

  return numericIds.length > 0 ? Math.max(...numericIds) + 1 : fallback;
};

export const downloadTextFile = (content: string, fileName: string, mimeType: string) => {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
};

export const downloadJsonFile = (data: unknown, fileName: string) => {
  const dataStr = JSON.stringify(data);
  const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(dataStr);
  const linkElement = document.createElement('a');
  linkElement.setAttribute('href', dataUri);
  linkElement.setAttribute('download', fileName);
  linkElement.click();
};
