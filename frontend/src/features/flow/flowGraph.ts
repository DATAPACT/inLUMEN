import { Edge, Node } from 'reactflow';
import {
  normalizeConfigurationStatus,
  normalizeDefinitionId,
  normalizeDefinitionVersion,
  normalizeGeneratedArtifact,
  normalizeNodeImplementation,
  normalizeNodePorts,
  normalizeSecretParamKeys,
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
        Object.entries(rawNode).filter(([key]) => !["id", "position", "data"].includes(key)),
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
      type: hasNestedData ? String(rawNode.type || "custom") : "custom",
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
          ? { param: { ...defaultParametersForTemplate("flow", templateName), ...objectValue(nodeData.param) } }
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
        ? { param: data.param as Record<string, unknown> }
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

export const downloadJsonFile = (data: unknown, fileName: string) => {
  const dataStr = JSON.stringify(data, null, 2);
  const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(dataStr);
  const linkElement = document.createElement('a');
  linkElement.setAttribute('href', dataUri);
  linkElement.setAttribute('download', fileName);
  linkElement.click();
};
