import { Edge, Node } from 'reactflow';
import {
  normalizeConfigurationStatus,
  normalizeDefinitionId,
  normalizeDefinitionVersion,
  normalizeGeneratedArtifact,
  normalizeNodeImplementation,
  normalizeNodePorts,
  normalizeSecretParamKeys,
  withoutSensitiveParameterValues,
  normalizeType,
} from '@/features/nodes/nodeSchema';
import {
  defaultParametersForTemplate,
  findTemplateForType,
} from '@/features/nodes/templateCatalog';

export type NormalizedGraph = {
  updated_at: string | null;
  nodes: Node[];
  edges: Edge[];
  settings?: Record<string, unknown>;
};

export type AgentGraphSnapshot = {
  updated_at: string | null;
  settings?: Record<string, unknown>;
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
    secret_params?: string[];
    definition_id?: string;
    definition_version?: number;
    implementation?: Record<string, unknown>;
    template?: string;
    ports?: ReturnType<typeof normalizeNodePorts>;
    configuration_status?: string;
    generated_artifact?: Record<string, unknown>;
  }>;
  edges: Array<{
    source: string;
    target: string;
    source_port?: string;
    target_port?: string;
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

const objectValue = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};

const templateNameFromData = (data: Record<string, unknown>) =>
  String(
    (typeof data.template === "string" ? data.template : objectValue(data.template).name)
    || data.template_label
    || "",
  );

const migrateFlowPorts = (data: Record<string, unknown>, nodeType: ReturnType<typeof normalizeType>) => {
  const current = normalizeNodePorts(data.ports, nodeType);
  if (nodeType !== "flow") return current;
  const templateName = templateNameFromData(data);
  const template = findTemplateForType("flow", templateName);
  if (!template?.ports) return current;
  const usesLegacyGenericContract =
    current.inputs.length === 1 && current.inputs[0].id === "input"
    && current.outputs.length === 1 && current.outputs[0].id === "output";
  return usesLegacyGenericContract
    ? normalizeNodePorts(template.ports, "flow")
    : current;
};

export const normalizeGraph = (data: unknown): NormalizedGraph => {
  const parsedGraph = (data && typeof data === "object" ? data : {}) as {
    nodes?: unknown[];
    edges?: unknown[];
    updated_at?: string | null;
    settings?: unknown;
  };
  const incomingNodes = Array.isArray(parsedGraph.nodes) ? parsedGraph.nodes : [];
  const incomingEdges = Array.isArray(parsedGraph.edges) ? parsedGraph.edges : [];

  const nodes: Node[] = incomingNodes.flatMap((nodeEntry, index) => {
    const rawNode = objectValue(nodeEntry);
    if (rawNode.id == null || String(rawNode.id).trim() === "") return [];
    const nestedData = objectValue(rawNode.data);
    const hasNestedData = Object.keys(nestedData).length > 0;
    const nodeData = hasNestedData
      ? nestedData
      : Object.fromEntries(
        // Agent snapshots use `position`, while the persisted graph endpoint
        // flattens coordinates as `x`/`y`. Coordinates belong to the React
        // Flow position, never to the durable node content used for change
        // detection.
        Object.entries(rawNode).filter(([key]) => !["id", "position", "data", "x", "y"].includes(key)),
      );
    const rawPosition = objectValue(rawNode.position);
    const position = {
      x: rawPosition.x ?? index * 280,
      y: rawPosition.y ?? 120,
    };
    const nodeType = normalizeType(nodeData.type);
    const templateName = templateNameFromData(nodeData);
    const normalizedFiles = Array.isArray(nodeData.file_buckets)
      ? nodeData.file_buckets
      : Array.isArray(nodeData.files)
        ? nodeData.files
        : [];
    return [{
      ...rawNode,
      id: String(rawNode.id),
      type: "custom",
      position: {
        x: Number.isFinite(Number(position.x)) ? Number(position.x) : 0,
        y: Number.isFinite(Number(position.y)) ? Number(position.y) : 0,
      },
      data: {
        ...nodeData,
        label: nodeData.label || "",
        description: nodeData.description || "",
        type: nodeType,
        ...(templateName ? { template_label: templateName } : {}),
        ports: migrateFlowPorts(nodeData, nodeType),
        ...(nodeType === "flow" && findTemplateForType("flow", templateName)
          ? {
              param: nodeData.configuration_status === "unconfigured"
                ? objectValue(nodeData.param)
                : {
                    ...defaultParametersForTemplate("flow", templateName),
                    ...objectValue(nodeData.param),
                  },
            }
          : {}),
        files: normalizedFiles,
      },
    }];
  });

  const nodeIds = new Set(nodes.map((node) => node.id));
  const seenEdgeKeys = new Set<string>();
  const edges: Edge[] = [];

  incomingEdges.forEach((edgeEntry) => {
    const edge = (edgeEntry && typeof edgeEntry === "object" ? edgeEntry : {}) as Edge & {
      source_port?: unknown;
      target_port?: unknown;
    };
    const source = String(edge.source || "");
    const target = String(edge.target || "");
    const sourceNode = nodes.find((node) => node.id === source);
    const targetNode = nodes.find((node) => node.id === target);
    const sourcePorts = sourceNode
      ? normalizeNodePorts(sourceNode.data?.ports, normalizeType(sourceNode.data?.type))
      : null;
    const targetPorts = targetNode
      ? normalizeNodePorts(targetNode.data?.ports, normalizeType(targetNode.data?.type))
      : null;
    const requestedSourceHandle = String(edge.sourceHandle || edge.source_port || "");
    const requestedTargetHandle = String(edge.targetHandle || edge.target_port || "");
    const sourceHandle = requestedSourceHandle === "output"
      && !sourcePorts?.outputs.some((port) => port.id === requestedSourceHandle)
      ? String(sourcePorts?.outputs[0]?.id || "")
      : String(requestedSourceHandle || sourcePorts?.outputs[0]?.id || "");
    const targetHandle = requestedTargetHandle === "input"
      && !targetPorts?.inputs.some((port) => port.id === requestedTargetHandle)
      ? String(targetPorts?.inputs[0]?.id || "")
      : String(requestedTargetHandle || targetPorts?.inputs[0]?.id || "");
    const edgeKey = `${source}:${sourceHandle}->${target}:${targetHandle}`;
    const conditionBranch = sourceHandle === "when_true"
      ? { label: "true", color: "#8b5cf6" }
      : sourceHandle === "when_false"
        ? { label: "false", color: "#0891b2" }
        : null;

    if (!source || !target || source === target) return;
    if (!nodeIds.has(source) || !nodeIds.has(target)) return;
    if (seenEdgeKeys.has(edgeKey)) return;
    seenEdgeKeys.add(edgeKey);

    edges.push({
      ...edge,
      id: edge?.id
        ? String(edge.id)
        : `e-${source}-${sourceHandle || "default"}-${target}-${targetHandle || "default"}`,
      source,
      target,
      sourceHandle,
      targetHandle,
      ...(conditionBranch
        ? {
          label: edge.label || conditionBranch.label,
          labelStyle: {
            fill: conditionBranch.color,
            fontSize: 10,
            fontWeight: 700,
            ...(edge.labelStyle || {}),
          },
          labelBgStyle: {
            fill: "hsl(var(--card))",
            fillOpacity: 0.94,
            ...(edge.labelBgStyle || {}),
          },
          labelBgPadding: edge.labelBgPadding || [5, 3],
          labelBgBorderRadius: edge.labelBgBorderRadius ?? 5,
          style: {
            stroke: conditionBranch.color,
            strokeWidth: 2,
            ...(edge.style || {}),
          },
          data: {
            ...objectValue(edge.data),
            conditionBranch: sourceHandle,
          },
        }
        : {}),
    });
  });

  return {
    updated_at: parsedGraph.updated_at ?? null,
    nodes,
    edges,
    ...(parsedGraph.settings &&
    typeof parsedGraph.settings === "object" &&
    !Array.isArray(parsedGraph.settings)
      ? { settings: parsedGraph.settings as Record<string, unknown> }
      : {}),
  };
};

const GRAPH_UI_FIELDS = new Set([
  "selected",
  "dragging",
  "positionAbsolute",
  "width",
  "height",
  "measured",
  "validation_issues",
  "connected_ports",
]);

const stableGraphValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(stableGraphValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !GRAPH_UI_FIELDS.has(key))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, stableGraphValue(item)]),
  );
};

/** Compare pipeline content while ignoring timestamps and canvas layout. */
export const graphContentSignature = (data: unknown) => {
  const graph = normalizeGraph(data);
  const nodes = graph.nodes
    .map((node) => ({
      id: String(node.id),
      data: stableGraphValue(node.data || {}),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const edges = graph.edges
    .map((edge) => ({
      source: String(edge.source),
      sourceHandle: String(edge.sourceHandle || ""),
      target: String(edge.target),
      targetHandle: String(edge.targetHandle || ""),
    }))
    .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  return JSON.stringify({ nodes, edges });
};

export const hasGraphContentChanges = (before: unknown, after: unknown) =>
  graphContentSignature(before) !== graphContentSignature(after);

export const createAgentGraphSnapshot = (graph: NormalizedGraph): AgentGraphSnapshot => ({
  updated_at: graph.updated_at,
  ...(graph.settings ? { settings: graph.settings } : {}),
  nodes: graph.nodes.map((node) => {
    const data = node.data || {};
    const files = Array.isArray(data.files)
      ? data.files.map(fileNameFromUnknown).filter(Boolean)
      : undefined;
    const definitionId = normalizeDefinitionId(data.definition_id);
    const definitionVersion = normalizeDefinitionVersion(data.definition_version);
    const configurationStatus = normalizeConfigurationStatus(data.configuration_status);
    const generatedArtifact = normalizeGeneratedArtifact(data.generated_artifact);
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
        ? { param: withoutSensitiveParameterValues(data.param, data.secret_params) }
        : {}),
      ...(Array.isArray(data.secret_params)
        ? { secret_params: normalizeSecretParamKeys(data.secret_params, data.param) }
        : {}),
      ...(typeof data.template_label === "string" && data.template_label.trim()
        ? { template: data.template_label.trim() }
        : {}),
      ports: normalizeNodePorts(data.ports, normalizeType(data.type)),
      ...(definitionId ? { definition_id: definitionId } : {}),
      ...(definitionId && definitionVersion ? { definition_version: definitionVersion } : {}),
      ...(Object.keys(normalizeNodeImplementation(data.implementation)).length > 0
        ? { implementation: normalizeNodeImplementation(data.implementation) }
        : {}),
      ...(configurationStatus ? { configuration_status: configurationStatus } : {}),
      ...(generatedArtifact ? { generated_artifact: generatedArtifact } : {}),
    };
  }),
  edges: graph.edges.map((edge) => ({
    source: String(edge.source),
    target: String(edge.target),
    ...(typeof edge.sourceHandle === "string" && edge.sourceHandle
      ? { source_port: edge.sourceHandle }
      : {}),
    ...(typeof edge.targetHandle === "string" && edge.targetHandle
      ? { target_port: edge.targetHandle }
      : {}),
  })),
});

export const getNextNumericNodeId = (nodes: Node[], fallback = 1) => {
  const numericIds = nodes
    .map((node) => parseInt(String(node.id), 10))
    .filter((value) => Number.isFinite(value));

  return numericIds.length > 0 ? Math.max(...numericIds) + 1 : fallback;
};

/**
 * Close the horizontal gap left by deleting a node from a simple chain.
 * Branches and merges are left untouched because their layout needs an
 * explicit branch-aware arrangement rather than a blind horizontal shift.
 */
export const compactGraphAfterNodeRemoval = (
  nodes: Node[],
  edges: Edge[],
  removedNodeIds: string[],
): Node[] => {
  const removed = new Set(removedNodeIds.map(String));
  if (removed.size === 0) return nodes;

  const activeEdges = edges.filter(
    (edge) => !removed.has(String(edge.source)) && !removed.has(String(edge.target)),
  );
  const positions = new Map(nodes.map((node) => [String(node.id), node.position]));
  const compactedIds = new Set<string>();

  for (const removedId of removed) {
    const sourceEdges = edges.filter((edge) => String(edge.target) === removedId);
    const targetEdges = edges.filter((edge) => String(edge.source) === removedId);
    if (sourceEdges.length > 1 || targetEdges.length !== 1) continue;

    const removedNode = nodes.find((node) => String(node.id) === removedId);
    const nextId = String(targetEdges[0]?.target || "");
    const nextNode = nodes.find((node) => String(node.id) === nextId);
    if (!removedNode || !nextNode || removed.has(nextId)) continue;

    const shift = Number(nextNode.position.x) - Number(removedNode.position.x);
    if (!Number.isFinite(shift) || shift <= 0) continue;

    const queue = [nextId];
    const downstream = new Set<string>();
    while (queue.length > 0) {
      const currentId = queue.shift()!;
      if (downstream.has(currentId)) continue;
      downstream.add(currentId);
      activeEdges
        .filter((edge) => String(edge.source) === currentId)
        .forEach((edge) => {
          const childId = String(edge.target);
          if (!downstream.has(childId)) queue.push(childId);
        });
    }

    downstream.forEach((nodeId) => {
      const position = positions.get(nodeId);
      if (position) positions.set(nodeId, { ...position, x: position.x - shift });
      compactedIds.add(nodeId);
    });
  }

  if (compactedIds.size === 0) return nodes;
  return nodes.map((node) => {
    const position = positions.get(String(node.id));
    return position ? { ...node, position } : node;
  });
};

export const downloadJsonFile = (data: unknown, fileName: string) => {
  const dataStr = JSON.stringify(data, null, 2);
  const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(dataStr);
  const linkElement = document.createElement('a');
  linkElement.setAttribute('href', dataUri);
  linkElement.setAttribute('download', fileName);
  linkElement.click();
};
