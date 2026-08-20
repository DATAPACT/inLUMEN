import type { Edge, Node } from "reactflow";

import { normalizeGraph, type NormalizedGraph } from "@/features/flow/flowGraph";
import {
  normalizeNodePorts,
  normalizeType,
  type NodePort,
  type NodePorts,
} from "@/features/nodes/nodeSchema";

export type SubpipelinePortBinding = NodePort & {
  internal: { node: string; port: string };
};

export type SubpipelineInterface = {
  inputs: SubpipelinePortBinding[];
  outputs: SubpipelinePortBinding[];
};

export type SubpipelineReference = {
  pipeline_uid: string;
  pipeline_name: string;
  version_uid: string;
  version_name: string;
};

export type SubpipelineDefinition = {
  version: 2;
  reference: SubpipelineReference;
  interface: SubpipelineInterface;
  resolved_graph?: NormalizedGraph;
  resolution_error?: string;
  expanded?: boolean;
};

export type ReusablePipelineDraft = {
  graph: NormalizedGraph;
  interface: SubpipelineInterface;
};

export type ReusablePipelineSaveDraft = ReusablePipelineDraft & {
  name: string;
  description: string;
  versionName: string;
};

const clonePort = (port: NodePort): NodePort => ({
  ...port,
  ...(port.schema ? { schema: { ...port.schema } } : {}),
});

const publicPortId = (
  node: Node,
  port: NodePort,
  collisions: Map<string, number>,
) => {
  if ((collisions.get(port.id) || 0) < 2) return port.id;
  const nodeStem = String(node.data?.label || node.id)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "-")
    .replace(/^-+|-+$/g, "") || String(node.id);
  return `${nodeStem}.${port.id}`;
};

const boundaryBindings = (
  nodes: Node[],
  kind: "source" | "destination",
): SubpipelinePortBinding[] => {
  const boundaries = nodes.filter((node) => normalizeType(node.data?.type) === kind);
  const entries = boundaries.flatMap((node) => {
    const ports = normalizeNodePorts(node.data?.ports, kind);
    const boundaryPorts = kind === "source" ? ports.outputs : ports.inputs;
    return boundaryPorts.map((port) => ({ node, port }));
  });
  const collisions = entries.reduce((counts, { port }) => {
    counts.set(port.id, (counts.get(port.id) || 0) + 1);
    return counts;
  }, new Map<string, number>());
  return entries.map(({ node, port }) => ({
    ...clonePort(port),
    id: publicPortId(node, port, collisions),
    internal: { node: String(node.id), port: port.id },
  }));
};

export const deriveSubpipelineInterface = (graph: unknown): SubpipelineInterface => {
  const normalized = normalizeGraph(graph);
  return {
    inputs: boundaryBindings(normalized.nodes, "source"),
    outputs: boundaryBindings(normalized.nodes, "destination"),
  };
};

const normalizeBindings = (value: unknown): SubpipelinePortBinding[] => {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const candidate = entry as Record<string, unknown>;
    const internal = candidate.internal && typeof candidate.internal === "object" && !Array.isArray(candidate.internal)
      ? candidate.internal as Record<string, unknown>
      : {};
    const id = String(candidate.id || "").trim();
    const node = String(internal.node || "").trim();
    const port = String(internal.port || "").trim();
    if (!id || !node || !port) return [];
    return [{
      id,
      name: String(candidate.name || id),
      type: String(candidate.type || "any"),
      required: candidate.required !== false,
      description: String(candidate.description || ""),
      ...(String(candidate.format || "").trim() ? { format: String(candidate.format) } : {}),
      ...(candidate.schema && typeof candidate.schema === "object" && !Array.isArray(candidate.schema)
        ? { schema: candidate.schema as Record<string, unknown> }
        : {}),
      internal: { node, port },
    }];
  });
};

export const normalizeSubpipelineInterface = (
  value: unknown,
  graph: unknown,
): SubpipelineInterface => {
  const candidate = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const inputs = normalizeBindings(candidate.inputs);
  const outputs = normalizeBindings(candidate.outputs);
  if (inputs.length > 0 || outputs.length > 0) return { inputs, outputs };
  return deriveSubpipelineInterface(graph);
};

export const publicPortsForSubpipeline = (
  subpipeline: Pick<SubpipelineDefinition, "interface">,
): NodePorts => ({
  inputs: subpipeline.interface.inputs.map(({ internal: _internal, ...port }) => clonePort(port)),
  outputs: subpipeline.interface.outputs.map(({ internal: _internal, ...port }) => clonePort(port)),
});

export const remapSubpipelineParentEdges = (
  nodeId: string,
  edges: Edge[],
  previousPorts: NodePorts,
  nextPorts: NodePorts,
): Edge[] => {
  const inputMap = new Map(
    previousPorts.inputs.flatMap((port, index) => (
      nextPorts.inputs[index] ? [[port.id, nextPorts.inputs[index].id] as const] : []
    )),
  );
  const outputMap = new Map(
    previousPorts.outputs.flatMap((port, index) => (
      nextPorts.outputs[index] ? [[port.id, nextPorts.outputs[index].id] as const] : []
    )),
  );
  return edges.map((edge) => ({
    ...edge,
    ...(String(edge.target) === String(nodeId) && edge.targetHandle && inputMap.has(edge.targetHandle)
      ? { targetHandle: inputMap.get(edge.targetHandle) }
      : {}),
    ...(String(edge.source) === String(nodeId) && edge.sourceHandle && outputMap.has(edge.sourceHandle)
      ? { sourceHandle: outputMap.get(edge.sourceHandle) }
      : {}),
  }));
};

const nestedNode = (
  id: string,
  kind: "source" | "task" | "destination",
  label: string,
  description: string,
  x: number,
  ports: NodePorts,
  template: string,
  implementation?: Record<string, unknown>,
): Node => ({
  id,
  type: "custom",
  position: { x, y: 120 },
  data: {
    type: kind,
    label,
    description,
    template_label: template,
    ports,
    param: {},
    ...(implementation ? { implementation } : {}),
  },
});

const nestedEdge = (
  source: string,
  sourceHandle: string,
  target: string,
  targetHandle: string,
): Edge => ({
  id: `nested-${source}-${sourceHandle}-${target}-${targetHandle}`,
  source,
  sourceHandle,
  target,
  targetHandle,
  type: "smoothstep",
});

export const conversationUnderstandingSubpipeline = (): ReusablePipelineDraft => {
  const graph: NormalizedGraph = {
    updated_at: null,
    nodes: [
      nestedNode(
        "conversation-input",
        "source",
        "Audio Input",
        "Logical audio input supplied by the parent pipeline.",
        0,
        { inputs: [], outputs: [{ id: "audio", name: "audio", type: "Audio", required: true, description: "Customer-support call audio." }] },
        "Subpipeline Input",
      ),
      nestedNode(
        "transcription",
        "task",
        "Transcription",
        "Transcribe the complete customer-support call.",
        280,
        {
          inputs: [{ id: "audio", name: "audio", type: "Audio", required: true, description: "Call audio." }],
          outputs: [{ id: "transcript", name: "transcript", type: "Text", required: true, description: "Timestamped transcript." }],
        },
        "Speech-to-Text",
        { kind: "generated-code", task: "automatic_speech_recognition", execution_profile: "trusted_heavy_model" },
      ),
      nestedNode(
        "pii-redaction",
        "task",
        "PII Redaction",
        "Redact personal and sensitive identifiers before further analysis.",
        560,
        {
          inputs: [{ id: "transcript", name: "transcript", type: "Text", required: true, description: "Original transcript." }],
          outputs: [{ id: "redacted_transcript", name: "redacted_transcript", type: "Text", required: true, description: "Privacy-safe transcript." }],
        },
        "PII Redaction",
        { kind: "generated-code", task: "pii_redaction", execution_profile: "deterministic" },
      ),
      nestedNode(
        "sentiment-analysis",
        "task",
        "Sentiment Analysis",
        "Classify sentiment and retain confidence scores.",
        840,
        {
          inputs: [{ id: "text", name: "text", type: "Text", required: true, description: "Redacted transcript." }],
          outputs: [{ id: "sentiment", name: "sentiment", type: "Object", required: true, description: "Sentiment label and confidence." }],
        },
        "Sentiment Analysis",
        { kind: "generated-code", task: "text-classification", execution_profile: "trusted_heavy_model" },
      ),
      nestedNode(
        "conversation-summary",
        "task",
        "Conversation Summary",
        "Assemble the transcript, redacted text, sentiment, confidence, and summary.",
        1120,
        {
          inputs: [
            { id: "transcript", name: "transcript", type: "Text", required: true, description: "Privacy-safe transcript." },
            { id: "sentiment", name: "sentiment", type: "Object", required: true, description: "Sentiment label and confidence." },
          ],
          outputs: [{ id: "conversation_analysis", name: "conversation_analysis", type: "Object", required: true, description: "Structured reusable conversation analysis." }],
        },
        "Conversation Summary",
        { kind: "generated-code", task: "conversation_summary", execution_profile: "custom_model" },
      ),
      nestedNode(
        "conversation-output",
        "destination",
        "Structured Analysis Output",
        "Logical output returned to the parent pipeline.",
        1400,
        { inputs: [{ id: "conversation_analysis", name: "conversation_analysis", type: "Object", required: true, description: "Structured conversation analysis." }], outputs: [] },
        "Subpipeline Output",
      ),
    ],
    edges: [
      nestedEdge("conversation-input", "audio", "transcription", "audio"),
      nestedEdge("transcription", "transcript", "pii-redaction", "transcript"),
      nestedEdge("pii-redaction", "redacted_transcript", "sentiment-analysis", "text"),
      nestedEdge("pii-redaction", "redacted_transcript", "conversation-summary", "transcript"),
      nestedEdge("sentiment-analysis", "sentiment", "conversation-summary", "sentiment"),
      nestedEdge("conversation-summary", "conversation_analysis", "conversation-output", "conversation_analysis"),
    ],
  };
  return {
    graph,
    interface: deriveSubpipelineInterface(graph),
  };
};
