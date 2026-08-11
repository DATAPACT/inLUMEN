import type { Edge, Node } from "reactflow";

import {
  getNodeFileName,
  normalizeNodeImplementation,
  normalizeNodePorts,
  normalizeType,
  type NodeFileReference,
  type NodePort,
  type StepType,
} from "@/features/nodes/nodeSchema";
import { defaultTemplateForType, findTemplateForType } from "@/features/nodes/templateCatalog";
import { normalizeGraph, type NormalizedGraph } from "@/features/flow/flowGraph";
import {
  type SubpipelineInterface,
  type SubpipelineReference,
} from "@/features/flow/subpipeline";

export const PROJECT_IR_SCHEMA_VERSION = "inlumen.project@2" as const;

export type TemplateReference = {
  id: string;
  name: string;
  version?: number;
};

export type PipelineIrConnection = {
  id: string;
  from: { node: string; port: string };
  to: { node: string; port: string };
};

export type PipelineIrNode = {
  id: string;
  kind: StepType;
  template: TemplateReference;
  label: string;
  description: string;
  position: { x: number; y: number };
  inputs: NodePort[];
  outputs: NodePort[];
  parameters: Record<string, unknown>;
  implementation?: Record<string, unknown>;
  source_configuration?: Record<string, unknown>;
  sample_data?: unknown[];
  subpipeline?: {
    version: 2;
    reference: SubpipelineReference;
    interface: SubpipelineInterface;
  };
};

export type PipelineIr = {
  nodes: PipelineIrNode[];
  connections: PipelineIrConnection[];
};

export type ProjectDocument = {
  schema_version: typeof PROJECT_IR_SCHEMA_VERSION;
  project: {
    name: string;
    description: string;
    exported_at: string;
  };
  pipeline: PipelineIr;
};

const objectValue = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};

const templateReference = (data: Record<string, unknown>, kind: StepType): TemplateReference => {
  const structured = objectValue(data.template);
  const name = String(
    structured.name || data.template_label || defaultTemplateForType(kind),
  ).trim() || defaultTemplateForType(kind);
  const catalogTemplate = findTemplateForType(kind, name);
  const id = String(structured.id || catalogTemplate?.id || `custom.${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`);
  const version = Number(structured.version);
  return {
    id,
    name,
    ...(Number.isInteger(version) && version > 0 ? { version } : {}),
  };
};

const graphToPipelineIr = (graph: NormalizedGraph): PipelineIr => ({
  nodes: graph.nodes.map((node) => {
    const data = objectValue(node.data);
    const kind = normalizeType(data.type);
    const ports = normalizeNodePorts(data.ports, kind);
    const files = Array.isArray(data.files)
      ? data.files.filter((file) => Boolean(getNodeFileName(file as NodeFileReference)))
      : [];
    const implementation = normalizeNodeImplementation(data.implementation);
    const subpipelineData = objectValue(data.subpipeline);
    const reference = objectValue(subpipelineData.reference);
    return {
      id: String(node.id),
      kind,
      template: templateReference(data, kind),
      label: String(data.label || ""),
      description: String(data.description || ""),
      position: {
        x: Number.isFinite(Number(node.position?.x)) ? Number(node.position.x) : 0,
        y: Number.isFinite(Number(node.position?.y)) ? Number(node.position.y) : 0,
      },
      inputs: ports.inputs,
      outputs: ports.outputs,
      parameters: objectValue(data.param || data.parameters),
      ...(kind === "task" ? { implementation } : {}),
      ...(kind === "source" ? { source_configuration: objectValue(data.source_config || data.source_configuration) } : {}),
      ...(files.length > 0 ? { sample_data: files } : {}),
      ...(kind === "subpipeline" && reference.pipeline_uid && reference.version_uid ? {
        subpipeline: {
          version: 2 as const,
          reference: {
            pipeline_uid: String(reference.pipeline_uid),
            pipeline_name: String(reference.pipeline_name || ""),
            version_uid: String(reference.version_uid),
            version_name: String(reference.version_name || ""),
          },
          interface: subpipelineData.interface as SubpipelineInterface,
        },
      } : {}),
    };
  }),
  connections: graph.edges.map((edge) => ({
    id: String(edge.id || `${edge.source}:${edge.sourceHandle}->${edge.target}:${edge.targetHandle}`),
    from: { node: String(edge.source), port: String(edge.sourceHandle || "") },
    to: { node: String(edge.target), port: String(edge.targetHandle || "") },
  })),
});

export const createProjectDocument = (
  graph: NormalizedGraph,
  metadata: { name?: string; description?: string } = {},
): ProjectDocument => ({
  schema_version: PROJECT_IR_SCHEMA_VERSION,
  project: {
    name: String(metadata.name || "inLUMEN Project"),
    description: String(metadata.description || ""),
    exported_at: new Date().toISOString(),
  },
  pipeline: graphToPipelineIr(graph),
});

const pipelineIrToGraphData = (pipeline: PipelineIr): { nodes: Node[]; edges: Edge[] } => {
  const nodes: Node[] = pipeline.nodes.map((node) => ({
    id: String(node.id),
    type: "custom",
    position: node.position || { x: 0, y: 0 },
    data: {
      label: node.label,
      description: node.description,
      type: normalizeType(node.kind),
      template: node.template,
      template_label: node.template?.name,
      ports: { inputs: node.inputs, outputs: node.outputs },
      param: objectValue(node.parameters),
      ...(node.kind === "task" ? { implementation: objectValue(node.implementation) } : {}),
      ...(node.kind === "source" ? { source_config: objectValue(node.source_configuration) } : {}),
      ...(Array.isArray(node.sample_data) ? { files: node.sample_data } : {}),
      ...(node.kind === "subpipeline" && node.subpipeline ? {
        subpipeline: {
          version: 2,
          reference: node.subpipeline.reference,
          interface: node.subpipeline.interface,
        },
      } : {}),
    },
  }));
  const edges: Edge[] = pipeline.connections.map((connection) => ({
    id: String(connection.id || `e-${connection.from.node}-${connection.to.node}`),
    source: String(connection.from.node),
    target: String(connection.to.node),
    sourceHandle: String(connection.from.port || ""),
    targetHandle: String(connection.to.port || ""),
  }));
  return { nodes, edges };
};

export const projectDocumentToGraph = (value: unknown): NormalizedGraph => {
  const candidate = objectValue(value);
  if (candidate.schema_version === PROJECT_IR_SCHEMA_VERSION) {
    const pipeline = candidate.pipeline as PipelineIr | undefined;
    if (!pipeline || !Array.isArray(pipeline.nodes) || !Array.isArray(pipeline.connections)) {
      throw new Error("Project JSON is missing a valid pipeline IR.");
    }
    return normalizeGraph(pipelineIrToGraphData(pipeline));
  }

  // Compatibility migration for pre-IR Project JSON / raw React Flow exports.
  const legacyGraph = objectValue(candidate.graph);
  const source = Array.isArray(candidate.nodes) && Array.isArray(candidate.edges)
    ? candidate
    : legacyGraph;
  if (!Array.isArray(source.nodes) || !Array.isArray(source.edges)) {
    throw new Error("The selected JSON is not an inLUMEN Project JSON document.");
  }
  return normalizeGraph(source);
};
