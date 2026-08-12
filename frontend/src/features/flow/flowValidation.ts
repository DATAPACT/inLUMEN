import type { Edge, Node } from "reactflow";

import {
  getStepTypeLabel,
  getNodeFileName,
  getNodeFileRole,
  normalizeImplementationKind,
  normalizeNodePorts,
  normalizeType,
  type NodeFileReference,
} from "@/features/nodes/nodeSchema";
import { findTemplateForType } from "@/features/nodes/templateCatalog";
import { normalizeSubpipelineInterface } from "@/features/flow/subpipeline";

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

export type GraphValidationMode = "draft" | "complete";

export type ValidationIssueSubject = {
  label: string;
  context: string;
};

const objectValue = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};

const validationNodeSubject = (node: Node): ValidationIssueSubject => {
  const data = objectValue(node.data);
  const kind = normalizeType(data.type);
  const kindLabel = getStepTypeLabel(kind);
  const label = String(data.label || "").trim() || `Untitled ${kindLabel}`;
  const template = String(objectValue(data.template).name || data.template_label || "").trim();
  return {
    label,
    context: template && template.toLowerCase() !== kindLabel.toLowerCase()
      ? `${kindLabel} · ${template}`
      : kindLabel,
  };
};

export const getValidationIssueSubject = (
  issue: ValidationIssue,
  nodes: Node[],
  edges: Edge[],
): ValidationIssueSubject => {
  const node = issue.nodeId
    ? nodes.find((candidate) => String(candidate.id) === String(issue.nodeId))
    : undefined;
  if (node) return validationNodeSubject(node);

  const edge = issue.edgeId
    ? edges.find((candidate) => String(candidate.id || "") === String(issue.edgeId))
    : undefined;
  if (edge) {
    const source = nodes.find((candidate) => String(candidate.id) === String(edge.source));
    const target = nodes.find((candidate) => String(candidate.id) === String(edge.target));
    const sourceLabel = source ? validationNodeSubject(source).label : String(edge.source || "Unknown node");
    const targetLabel = target ? validationNodeSubject(target).label : String(edge.target || "Unknown node");
    return { label: `${sourceLabel} → ${targetLabel}`, context: "Connection" };
  }

  if (issue.nodeId) return { label: `Node ${issue.nodeId}`, context: "Component" };
  return { label: "Pipeline", context: "Graph" };
};

const portTypesCompatible = (source: string, target: string) => {
  const left = source.trim().toLowerCase();
  const right = target.trim().toLowerCase();
  return !left || !right || ["any", "unknown", "*"].includes(left) || ["any", "unknown", "*"].includes(right) || left === right;
};

const FLOW_EXPRESSION_PATTERN = /^value(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[(?:\d+|"[^"]+"|'[^']+')\]))*(?:\s*(?:==|!=|>=|<=|>|<)\s*(?:"[^"]*"|'[^']*'|-?\d+(?:\.\d+)?|true|false|null))?$/;

const executableFilePattern = (implementationKind: string) => {
  if (implementationKind === "python") return /\.py$/i;
  if (implementationKind === "sql") return /\.sql$/i;
  if (implementationKind === "shell") return /\.(?:sh|bash|zsh)$/i;
  return /\.(?:py|sql|sh|bash|zsh|js|jsx|ts|tsx|java|go|rs|rb|php|r|scala|kt|kts|swift|lua|pl|ex|exs)$/i;
};

const getEntrypointTokens = (value: unknown) => {
  if (Array.isArray(value)) {
    return value
      .map((entry) => String(entry ?? "").trim())
      .filter((entry) => entry.length > 0);
  }
  const single = String(value ?? "").trim();
  return single.length > 0 ? [single] : [];
};

export const validateGraph = (
  nodes: Node[],
  edges: Edge[],
  options: { mode?: GraphValidationMode } = {},
): GraphValidationReport => {
  const issues: ValidationIssue[] = [];
  const wiringSeverity: ValidationIssue["severity"] = options.mode === "draft" ? "warning" : "error";
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
    const requiredParameterNames = new Set(template?.requiredParameters || []);

    ports.inputs.filter((port) => port.required).forEach((port) => {
      const connected = edges.some((edge) =>
        String(edge.target) === nodeId && String(edge.targetHandle || "") === port.id
      );
      if (!connected) add({
        severity: wiringSeverity,
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
      if (!requiredParameterNames.has(name) && (value == null || (typeof value === "string" && !value.trim()))) add({
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

    if (kind === "flow") {
      if (!template || templateName === "Flow") add({
        severity: "error",
        category: "configuration",
        code: "missing-flow-behavior",
        nodeId,
        message: "Choose a Flow behavior: Condition or Parallel Map.",
      });

      if (
        templateName === "Condition"
        && String(parameters.expression || "").trim()
        && !FLOW_EXPRESSION_PATTERN.test(String(parameters.expression).trim())
      ) add({
        severity: "error",
        category: "configuration",
        code: "invalid-flow-expression",
        nodeId,
        message: "Condition expressions must compare value (or value.field) with a literal.",
      });

      ports.outputs.filter((port) => port.required).forEach((port) => {
        const connected = edges.some((edge) =>
          String(edge.source) === nodeId && String(edge.sourceHandle || "") === port.id
        );
        if (!connected) add({
          severity: wiringSeverity,
          category: "ports",
          code: "missing-required-flow-output",
          nodeId,
          message: `Required Flow output “${port.name}” is not connected.`,
        });
      });

      if (templateName === "Parallel Map") {
        const concurrency = Number(parameters.max_concurrency);
        if (!Number.isInteger(concurrency) || concurrency < 1) add({
          severity: "error",
          category: "configuration",
          code: "invalid-flow-concurrency",
          nodeId,
          message: "Parallel Map maximum concurrency must be a positive whole number.",
        });
        if (!["stop", "continue"].includes(String(parameters.failure_policy || ""))) add({
          severity: "error",
          category: "configuration",
          code: "invalid-flow-failure-policy",
          nodeId,
          message: "Parallel Map requires a valid item failure policy.",
        });
      }
    }

    if (kind === "task") {
      const implementation = objectValue(data.implementation);
      const kindValue = normalizeImplementationKind(implementation.kind);
      const generatedArtifact = objectValue(data.generated_artifact);
      if (!implementation.kind) add({
        severity: "error",
        category: "implementation",
        code: "missing-implementation",
        nodeId,
        message: "Task implementation is not configured.",
      });
      if (["python", "sql", "shell", "generated-code"].includes(kindValue)) {
        const codeFiles = Array.isArray(data.files)
          ? data.files.filter((file) =>
              getNodeFileRole(file as NodeFileReference) === "code"
              && executableFilePattern(kindValue).test(
                getNodeFileName(file as NodeFileReference),
              )
            )
          : [];
        if (codeFiles.length === 0) add({
          severity: wiringSeverity,
          category: "implementation",
          code: "missing-code",
          nodeId,
          message: "No implementation code is attached. Generate code or upload a runtime package.",
        });
        else if (
          getEntrypointTokens(implementation.entrypoint).length === 0
          && getEntrypointTokens(generatedArtifact.entrypoint).length === 0
        ) add({
          severity: wiringSeverity,
          category: "implementation",
          code: "missing-entrypoint",
          nodeId,
          message: "Select the runtime package entrypoint.",
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
      const subpipeline = objectValue(data.subpipeline);
      const reference = objectValue(subpipeline.reference);
      if (!String(reference.pipeline_uid || "").trim() || !String(reference.version_uid || "").trim()) {
        add({
          severity: "error",
          category: "configuration",
          code: "missing-subpipeline-reference",
          nodeId,
          message: "Select or create a saved reusable pipeline version.",
        });
      }
      if (String(subpipeline.resolution_error || "").trim()) {
        add({
          severity: "error",
          category: "configuration",
          code: "unresolved-subpipeline-reference",
          nodeId,
          message: String(subpipeline.resolution_error),
        });
      }
      const referencedInterface = normalizeSubpipelineInterface(subpipeline.interface, {});
      if (referencedInterface.inputs.length === 0 || referencedInterface.outputs.length === 0) {
        add({
          severity: "error",
          category: "configuration",
          code: "missing-subpipeline-interface",
          nodeId,
          message: "The referenced pipeline must expose at least one input and output.",
        });
      }
      (["inputs", "outputs"] as const).forEach((direction) => {
        referencedInterface[direction].forEach((binding) => {
          const publicPort = ports[direction].find((port) => port.id === binding.id);
          if (!publicPort) {
            add({
              severity: "error",
              category: "ports",
              code: "invalid-subpipeline-interface",
              nodeId,
              message: `Referenced pipeline ${direction.slice(0, -1)} “${binding.name}” is missing from this component.`,
            });
          } else if (!portTypesCompatible(publicPort.type, binding.type)) {
            add({
              severity: "error",
              category: "ports",
              code: "incompatible-subpipeline-interface",
              nodeId,
              message: `Referenced pipeline ${direction.slice(0, -1)} “${binding.name}” has an incompatible type.`,
            });
          }
          });
      });
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
