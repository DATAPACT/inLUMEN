import { normalizeImplementationKind, type GeneratedArtifact, type StepType } from "@/features/nodes/nodeSchema";

export type TaskImplementationStatus = "missing" | "generating" | "current" | "stale" | "invalid";

const MANAGED_PYTHON_IMPLEMENTATION_KINDS = new Set(["python", "generated-code"]);
const objectValue = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};

export const taskImplementationMigrationError = (value: unknown): string => {
  const implementation = objectValue(value);
  const rawKind = String(implementation.kind || "").trim();
  const kind = rawKind ? normalizeImplementationKind(rawKind) : "";
  const language = String(implementation.language || "").trim().toLowerCase();
  if (kind && !MANAGED_PYTHON_IMPLEMENTATION_KINDS.has(kind)) {
    return `Legacy ${rawKind} Task implementations are unsupported. Replace this Task with generated or uploaded Python code.`;
  }
  if (language && language !== "python" && language !== "python3") {
    return `Legacy ${implementation.language} Task implementations are unsupported. Migrate this Task to Python.`;
  }
  return "";
};

export const nodeSupportsInputFiles = (nodeType: StepType, template: unknown) => {
  // A Source may combine a configured connector with local seed files. The
  // bundle resolves which inputs apply; the inspector stays deliberately light.
  void template;
  return nodeType === "source";
};

export const taskImplementationStatus = ({
  implementation,
  artifact,
  hasPythonPackage,
  isGenerating,
  hasImplementationErrors,
}: {
  implementation: unknown;
  artifact?: GeneratedArtifact;
  hasPythonPackage: boolean;
  isGenerating: boolean;
  hasImplementationErrors: boolean;
}): TaskImplementationStatus => {
  if (isGenerating) return "generating";
  if (taskImplementationMigrationError(implementation) || hasImplementationErrors) return "invalid";
  const report = artifact?.validation_report;
  if (
    (Array.isArray(report?.errors) && report.errors.length > 0)
    || ["invalid", "failed", "error"].includes(String(report?.status || "").toLowerCase())
  ) return "invalid";
  if (artifact?.status === "stale") return "stale";
  if (hasPythonPackage) return "current";
  return "missing";
};

export const visiblePropertySections = ({
  nodeType,
  template,
  configurationFieldCount,
  validationIssueCount,
}: {
  nodeType: StepType;
  template: unknown;
  configurationFieldCount: number;
  validationIssueCount: number;
}) => ({
  general: true,
  configuration: nodeType === "flow" && configurationFieldCount > 0,
  implementation: nodeType === "task",
  inputFiles: nodeSupportsInputFiles(nodeType, template),
  validation: validationIssueCount > 0,
  advanced: false,
});
