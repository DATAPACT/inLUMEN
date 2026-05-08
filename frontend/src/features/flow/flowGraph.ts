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

  const nodes: Node[] = incomingNodes.flatMap((nodeEntry) => {
    const node = (nodeEntry && typeof nodeEntry === "object" ? nodeEntry : {}) as Node;
    if (node.id == null || String(node.id).trim() === "") return [];
    const position = node.position || { x: 0, y: 0 };
    return [{
      ...node,
      id: String(node.id),
      position: {
        x: Number.isFinite(Number(position.x)) ? Number(position.x) : 0,
        y: Number.isFinite(Number(position.y)) ? Number(position.y) : 0,
      },
      data: {
        ...node.data,
        label: node.data?.label || "",
        description: node.data?.description || "",
        type: normalizeType(node.data?.type),
      },
    }];
  });

  const nodeIds = new Set(nodes.map((node) => node.id));
  const seenEdgeKeys = new Set<string>();
  const edges: Edge[] = [];

  incomingEdges.forEach((edgeEntry) => {
    const edge = (edgeEntry && typeof edgeEntry === "object" ? edgeEntry : {}) as Edge;
    const source = String(edge.source || "");
    const target = String(edge.target || "");
    const edgeKey = `${source}->${target}`;

    if (!source || !target || source === target) return;
    if (!nodeIds.has(source) || !nodeIds.has(target)) return;
    if (seenEdgeKeys.has(edgeKey)) return;
    seenEdgeKeys.add(edgeKey);

    edges.push({
      ...edge,
      id: edge?.id ? String(edge.id) : `e-${String(edge.source)}-${String(edge.target)}`,
      source,
      target,
    });
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
