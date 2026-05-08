import { Edge, Node } from 'reactflow';
import { normalizeType } from '@/features/nodes/nodeSchema';

export type NormalizedGraph = {
  updated_at: string | null;
  nodes: Node[];
  edges: Edge[];
};

export type AgentGraphSnapshot = {
  updated_at: string | null;
  nodes: Array<{
    id: string;
    type: string;
    label: string;
    description: string;
    position: { x: number; y: number };
    content?: string;
    endpoint?: string;
    database?: string;
    files?: string[];
    param?: Record<string, unknown>;
  }>;
  edges: Array<{
    source: string;
    target: string;
  }>;
};

const fileNameFromUnknown = (file: unknown) => {
  if (typeof file === "string") return file;
  if (file && typeof file === "object") {
    const candidate = file as { filename?: unknown; name?: unknown };
    if (typeof candidate.filename === "string") return candidate.filename;
    if (typeof candidate.name === "string") return candidate.name;
  }
  return "";
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

export const createAgentGraphSnapshot = (graph: NormalizedGraph): AgentGraphSnapshot => ({
  updated_at: graph.updated_at,
  nodes: graph.nodes.map((node) => {
    const data = node.data || {};
    const files = Array.isArray(data.files)
      ? data.files.map(fileNameFromUnknown).filter(Boolean)
      : undefined;
    return {
      id: String(node.id),
      type: normalizeType(data.type),
      label: String(data.label || ""),
      description: String(data.description || ""),
      position: {
        x: Number.isFinite(Number(node.position?.x)) ? Number(node.position?.x) : 0,
        y: Number.isFinite(Number(node.position?.y)) ? Number(node.position?.y) : 0,
      },
      ...(typeof data.content === "string" ? { content: data.content } : {}),
      ...(typeof data.endpoint === "string" ? { endpoint: data.endpoint } : {}),
      ...(typeof data.database === "string" ? { database: data.database } : {}),
      ...(files && files.length > 0 ? { files } : {}),
      ...(data.param && typeof data.param === "object" && !Array.isArray(data.param)
        ? { param: data.param as Record<string, unknown> }
        : {}),
    };
  }),
  edges: graph.edges.map((edge) => ({
    source: String(edge.source),
    target: String(edge.target),
  })),
});

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
