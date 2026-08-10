import type { Edge, Node } from "reactflow";

import { normalizeImplementationKind, normalizeNodePorts, normalizeType } from "@/features/nodes/nodeSchema";
import { findTemplateForType } from "@/features/nodes/templateCatalog";

export type ValidationCategory =
  | "configuration"
  | "ports"
  | "implementation"
  | "graph";

export type ValidationIssue = {
  severity: "error" | "warning";
  category: ValidationCategory;
  code: string;
  message: string;
  nodeId?: string;
  edgeId?: string;
};

export type GraphValidationReport = {
  valid: boolean;
  issues: ValidationIssue[];
  byNode: Record<string, ValidationIssue[]>;
};

const objectValue = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};

const portTypesCompatible = (source: string, target: string) => {
  const left = source.trim().toLowerCase();
  const right = target.trim().toLowerCase();
  return !left || !right || ["any", "unknown", "*"].includes(left) || ["any", "unknown", "*"].includes(right) || left === right;
};

export const validateGraph = (nodes: Node[], edges: Edge[]): GraphValidationReport => {
  const issues: ValidationIssue[] = [];
  const nodeById = new Map(nodes.map((node) => [String(node.id), node]));
  const portsByNode = new Map(nodes.map((node) => {
    const kind = normalizeType(node.data?.type);
    return [String(node.id), normalizeNodePorts(node.data?.ports, kind)] as const;
  }));
  const add = (issue: ValidationIssue) => issues.push(issue);

  nodes.forEach((node) => {
    const nodeId = String(node.id);
    const data = objectValue(node.data);
    const kind = normalizeType(data.type);
    const ports = portsByNode.get(nodeId)!;
    const parameters = objectValue(data.param || data.parameters);
    const templateName = String(objectValue(data.template).name || data.template_label || "");
    const template = findTemplateForType(kind, templateName);

    ports.inputs.filter((port) => port.required).forEach((port) => {
      const connected = edges.some((edge) =>
        String(edge.target) === nodeId && String(edge.targetHandle || "") === port.id
      );
      if (!connected) add({
        severity: "error",
        category: "ports",
        code: "missing-required-input",
        nodeId,
        message: `Required input “${port.name}” is not connected.`,
      });
    });

    if (kind !== "destination" && ports.outputs.length === 0) add({
      severity: "error",
      category: "ports",
      code: "missing-output",
      nodeId,
      message: "The component does not define an output port.",
    });

    Object.entries(parameters).forEach(([name, value]) => {
      if (value == null || (typeof value === "string" && !value.trim())) add({
        severity: "error",
        category: "configuration",
        code: "missing-parameter-value",
        nodeId,
        message: `Parameter “${name}” has no value.`,
      });
    });
    (template?.requiredParameters || []).forEach((name) => {
      if (!(name in parameters) || parameters[name] == null || String(parameters[name]).trim() === "") add({
        severity: "error",
        category: "configuration",
        code: "missing-required-parameter",
        nodeId,
        message: `Template “${template.label}” requires parameter “${name}”.`,
      });
    });

    if (kind === "task") {
      const implementation = objectValue(data.implementation);
      const kindValue = normalizeImplementationKind(implementation.kind);
      if (!implementation.kind) add({
        severity: "error",
        category: "implementation",
        code: "missing-implementation",
        nodeId,
        message: "Task implementation is not configured.",
      });
      if (["python", "sql", "shell"].includes(kindValue)) {
        const files = Array.isArray(data.files) ? data.files : [];
        if (!String(implementation.entrypoint || "").trim() && files.length === 0) add({
          severity: "warning",
          category: "implementation",
          code: "missing-entrypoint",
          nodeId,
          message: "Implementation has no entrypoint or attached code file.",
        });
      }
      if (kindValue === "container" && !String(implementation.image || "").trim()) add({
        severity: "error",
        category: "implementation",
        code: "missing-container-image",
        nodeId,
        message: "Container implementation requires an image.",
      });
      if (kindValue === "repository" && !String(implementation.repository || implementation.url || "").trim()) add({
        severity: "error",
        category: "implementation",
        code: "missing-repository",
        nodeId,
        message: "Repository implementation requires a repository URL.",
      });
      if (kindValue === "rest-api" && !String(implementation.endpoint || "").trim()) add({
        severity: "error",
        category: "implementation",
        code: "missing-endpoint",
        nodeId,
        message: "REST API implementation requires an endpoint.",
      });
    }

    if (kind === "subpipeline") {
      const nested = objectValue(objectValue(data.subpipeline).graph);
      const nestedNodes = Array.isArray(nested.nodes) ? nested.nodes as Node[] : [];
      const nestedEdges = Array.isArray(nested.edges) ? nested.edges as Edge[] : [];
      if (nestedNodes.length === 0) {
        add({
          severity: "warning",
          category: "graph",
          code: "empty-subpipeline",
          nodeId,
          message: "Subpipeline does not contain an internal graph yet.",
        });
      } else {
        validateGraph(nestedNodes, nestedEdges).issues.forEach((issue) => add({
          ...issue,
          nodeId,
          message: `Nested graph: ${issue.message}`,
        }));
      }
    }
  });

  const seenConnections = new Set<string>();
  edges.forEach((edge) => {
    const edgeId = String(edge.id || "");
    const sourceId = String(edge.source || "");
    const targetId = String(edge.target || "");
    const sourcePortId = String(edge.sourceHandle || "");
    const targetPortId = String(edge.targetHandle || "");
    const connectionKey = `${sourceId}:${sourcePortId}->${targetId}:${targetPortId}`;
    if (!nodeById.has(sourceId) || !nodeById.has(targetId)) {
      add({ severity: "error", category: "graph", code: "orphan-edge", edgeId, message: "Connection references a missing component." });
      return;
    }
    if (sourceId === targetId) add({ severity: "error", category: "graph", code: "self-edge", edgeId, nodeId: sourceId, message: "A component cannot connect to itself." });
    if (seenConnections.has(connectionKey)) add({ severity: "error", category: "graph", code: "duplicate-edge", edgeId, message: "Duplicate port connection." });
    seenConnections.add(connectionKey);
    if (!sourcePortId || !targetPortId) {
      add({ severity: "error", category: "graph", code: "missing-edge-port", edgeId, nodeId: targetId, message: "Connection must identify both source and target ports." });
      return;
    }
    const sourcePort = portsByNode.get(sourceId)?.outputs.find((port) => port.id === sourcePortId);
    const targetPort = portsByNode.get(targetId)?.inputs.find((port) => port.id === targetPortId);
    if (!sourcePort || !targetPort) {
      add({ severity: "error", category: "graph", code: "unknown-edge-port", edgeId, nodeId: targetId, message: "Connection references a port that does not exist." });
      return;
    }
    if (!portTypesCompatible(sourcePort.type, targetPort.type)) add({
      severity: "error",
      category: "ports",
      code: "incompatible-port-types",
      edgeId,
      nodeId: targetId,
      message: `Port types are incompatible: ${sourcePort.type} → ${targetPort.type}.`,
    });
  });

  const byNode: Record<string, ValidationIssue[]> = {};
  issues.forEach((issue) => {
    if (!issue.nodeId) return;
    (byNode[issue.nodeId] ||= []).push(issue);
  });
  return {
    valid: !issues.some((issue) => issue.severity === "error"),
    issues,
    byNode,
  };
};
