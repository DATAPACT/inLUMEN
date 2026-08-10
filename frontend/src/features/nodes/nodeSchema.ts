export type StepType =
  | "source"
  | "task"
  | "sink"
  | "flow"
  | "subpipeline";

export const STEP_TYPE_LABELS: Record<StepType, string> = {
  source: "Source",
  task: "Task",
  sink: "Destination",
  flow: "Flow",
  subpipeline: "Subpipeline",
};

export const getStepTypeLabel = (type: StepType) => STEP_TYPE_LABELS[type];

export type NodePort = {
  id: string;
  label: string;
  data_type?: string;
};

export type NodePorts = {
  inputs: NodePort[];
  outputs: NodePort[];
};

export const DEFAULT_NODE_PORTS: Record<StepType, NodePorts> = {
  source: {
    inputs: [],
    outputs: [{ id: "data", label: "data" }],
  },
  task: {
    inputs: [{ id: "input", label: "input" }],
    outputs: [{ id: "output", label: "output" }],
  },
  sink: {
    inputs: [{ id: "data", label: "data" }],
    outputs: [],
  },
  flow: {
    inputs: [{ id: "input", label: "input" }],
    outputs: [{ id: "output", label: "output" }],
  },
  subpipeline: {
    inputs: [{ id: "input", label: "input" }],
    outputs: [{ id: "output", label: "output" }],
  },
};

const normalizePortList = (value: unknown, fallback: NodePort[]) => {
  if (!Array.isArray(value)) return fallback.map((port) => ({ ...port }));
  const ids = new Set<string>();
  return value.flatMap((entry, index) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const candidate = entry as Record<string, unknown>;
    const label = String(candidate.label ?? candidate.id ?? "").trim();
    const baseId = String(candidate.id ?? label ?? `port-${index + 1}`)
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_.-]+/g, "-")
      .replace(/^-+|-+$/g, "") || `port-${index + 1}`;
    let id = baseId;
    let suffix = 2;
    while (ids.has(id)) {
      id = `${baseId}-${suffix}`;
      suffix += 1;
    }
    ids.add(id);
    return [{
      id,
      label: label || id,
      ...(typeof candidate.data_type === "string" && candidate.data_type.trim()
        ? { data_type: candidate.data_type.trim() }
        : {}),
    }];
  });
};

export const normalizeNodePorts = (value: unknown, type: StepType): NodePorts => {
  const candidate = value && typeof value === "object" && !Array.isArray(value)
    ? value as Partial<NodePorts>
    : {};
  return {
    inputs: type === "source"
      ? []
      : normalizePortList(candidate.inputs, DEFAULT_NODE_PORTS[type].inputs),
    outputs: type === "sink"
      ? []
      : normalizePortList(candidate.outputs, DEFAULT_NODE_PORTS[type].outputs),
  };
};

export const IMPLEMENTATION_KIND_OPTIONS = [
  { value: "generated-code", label: "Generated code" },
  { value: "python", label: "Python" },
  { value: "sql", label: "SQL" },
  { value: "container", label: "Container" },
  { value: "git-repository", label: "Git repository" },
  { value: "rest-api", label: "REST API" },
  { value: "shell", label: "Shell" },
  { value: "custom", label: "Custom" },
] as const;

export type ImplementationKind = (typeof IMPLEMENTATION_KIND_OPTIONS)[number]["value"];

export const normalizeImplementationKind = (value: unknown): ImplementationKind => {
  const normalized = String(value ?? "").trim().toLowerCase().replace(/\s+/g, "-");
  return IMPLEMENTATION_KIND_OPTIONS.some((option) => option.value === normalized)
    ? normalized as ImplementationKind
    : "generated-code";
};

export type NodeImplementation = Record<string, unknown>;

const SENSITIVE_PARAMETER_PATTERN =
  /(^|[_\-.])(api[_\-.]?key|access[_\-.]?key|client[_\-.]?secret|private[_\-.]?key|password|passphrase|secret|token|credential|authorization)($|[_\-.])/i;

export const isSensitiveParameterName = (value: unknown) => {
  const name = String(value ?? "").trim();
  const compact = name.toLowerCase().replace(/[^a-z0-9]/g, "");
  return SENSITIVE_PARAMETER_PATTERN.test(name) || [
    "apikey",
    "accesskey",
    "clientsecret",
    "privatekey",
    "password",
    "passphrase",
    "secret",
    "token",
    "credential",
    "authorization",
  ].includes(compact);
};

export const normalizeSecretParamKeys = (
  value: unknown,
  parameters: unknown,
) => {
  const paramKeys = parameters && typeof parameters === "object" && !Array.isArray(parameters)
    ? new Set(Object.keys(parameters as Record<string, unknown>))
    : new Set<string>();
  const candidates = Array.isArray(value)
    ? value.map((entry) => String(entry ?? "").trim())
    : Array.from(paramKeys).filter(isSensitiveParameterName);
  return Array.from(new Set(candidates.filter((key) => key && paramKeys.has(key))));
};

export type NodeConfigurationStatus = "unconfigured" | "valid" | "invalid";

export type GeneratedArtifact = {
  status?: "current" | "stale";
  generator?: string;
  generator_version?: string;
  configuration_hash?: string;
  entrypoint?: string[];
  files?: Array<{
    filename?: string;
    bucket?: string;
    content_type?: string;
  }>;
  data_contract?: Record<string, unknown>;
  validation_report?: {
    status?: string;
    errors?: string[];
    warnings?: string[];
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export type NodeFileMetadata = {
  filename?: string;
  name?: string;
  bucket?: string;
  role?: NodeFileRole;
  added_at?: string;
  [key: string]: unknown;
};

export type NodeFileReference = File | string | NodeFileMetadata;
export type NodeFileRole = "code" | "data";

export const isBrowserFile = (file: NodeFileReference): file is File =>
  typeof File !== "undefined" && file instanceof File;

export const getNodeFileName = (file: NodeFileReference) => {
  if (typeof file === "string") return file;
  if (isBrowserFile(file)) return file.name;
  if (file && typeof file === "object") {
    const name = file.filename ?? file.name;
    return typeof name === "string" ? name : "";
  }
  return "";
};

export const getNodeFileBucket = (file: NodeFileReference, nodeId: string) => {
  if (file && typeof file === "object" && !isBrowserFile(file)) {
    const bucket = file.bucket;
    if (typeof bucket === "string" && bucket.trim()) return bucket.trim();
  }
  return `files-step-id-${nodeId}`.toLowerCase();
};

const CODE_FILE_PATTERN = /(^|\/)(dockerfile(?:\.[^/]*)?|makefile|requirements(?:\.[^/]*)?\.txt|pyproject\.toml|package(?:-lock)?\.json|.*\.(?:py|pyi|sql|sh|bash|zsh|js|jsx|ts|tsx|java|c|cc|cpp|h|hpp|go|rs|rb|php|r|scala|kt|kts|swift|lua|pl|ex|exs))$/i;

export const getNodeFileRole = (file: NodeFileReference): NodeFileRole => {
  if (file && typeof file === "object" && !isBrowserFile(file)) {
    if (file.role === "code" || file.role === "data") return file.role;
  }
  return CODE_FILE_PATTERN.test(getNodeFileName(file)) ? "code" : "data";
};

const CANONICAL_STEP_TYPES = new Set<StepType>([
  "source",
  "task",
  "sink",
  "flow",
  "subpipeline",
]);

const STEP_TYPE_ALIASES: Record<string, StepType> = {
  input: "source",
  data_ingestion: "source",
  "data-source": "source",
  data_source: "source",
  ingest: "source",
  ingestion: "source",
  sensor: "source",
  collect: "source",
  output: "sink",
  destination: "sink",
  alert: "sink",
  notification: "sink",
  report: "sink",
  reporting: "sink",
  result: "sink",
  action: "task",
  config: "task",
  configuration: "task",
  custom: "task",
  api: "task",
  api_call: "task",
  "api-call": "task",
  integration: "task",
  storage: "task",
  database: "task",
  clipboard: "task",
  parameters: "task",
  process: "task",
  processing: "task",
  processing_step: "task",
  operation: "task",
  operator: "task",
  step: "task",
  preprocessing: "task",
  transformation: "task",
  feature_engineering: "task",
  "feature-engineering": "task",
  training: "task",
  model_training: "task",
  evaluation: "task",
  model_evaluation: "task",
  inference: "task",
  scoring: "task",
  control: "flow",
  condition: "flow",
  branch: "flow",
  parallel: "flow",
  parallel_map: "flow",
  merge: "flow",
  retry: "flow",
  wait: "flow",
  human_approval: "flow",
  sub_pipeline: "subpipeline",
  "sub-pipeline": "subpipeline",
  nested_pipeline: "subpipeline",
  reusable_pipeline: "subpipeline",
};

export const normalizeType = (type: unknown): StepType => {
  const normalized = String(type ?? "").toLowerCase().trim().replace(/\s+/g, "_");
  if (CANONICAL_STEP_TYPES.has(normalized as StepType)) return normalized as StepType;
  if (STEP_TYPE_ALIASES[normalized]) return STEP_TYPE_ALIASES[normalized];
  if (normalized.includes("subpipeline") || normalized.includes("sub_pipeline") || normalized.includes("nested_pipeline")) return "subpipeline";
  if (["condition", "branch", "parallel", "merge", "retry", "wait", "approval"].some((token) => normalized.includes(token))) return "flow";
  if (["ingest", "input", "source", "sensor", "upload"].some((token) => normalized.includes(token))) return "source";
  if (["sink", "destination", "alert", "output", "report", "publish", "notification"].some((token) => normalized.includes(token))) return "sink";
  return "task";
};

export const normalizeDefinitionId = (value: unknown) =>
  typeof value === "string" ? value.trim() : "";

export const normalizeDefinitionVersion = (value: unknown) => {
  const version = Number(value);
  return Number.isInteger(version) && version > 0 ? version : undefined;
};

export const normalizeNodeImplementation = (value: unknown): NodeImplementation =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as NodeImplementation
    : {};

export const normalizeGeneratedArtifact = (
  value: unknown,
): GeneratedArtifact | undefined =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as GeneratedArtifact
    : undefined;

export const normalizeConfigurationStatus = (
  value: unknown,
): NodeConfigurationStatus | undefined =>
  value === "unconfigured" || value === "valid" || value === "invalid"
    ? value
    : undefined;

// Every graph component may carry artifacts; implementation type is expressed
// in implementation metadata rather than by inventing another graph kind.
export const typeHasFiles = (_type: StepType) => true;

export const typeHasContent = (type: StepType) =>
  type === "source" || type === "sink";

export const pickBackendUpdatableProps = (
  nodeId: string,
  nodeData: Record<string, unknown>,
  nodeType: StepType,
) => {
  const props: Record<string, unknown> = {
    flow_id: nodeId,
    label: nodeData.label ?? "",
    type: nodeType,
    description: nodeData.description ?? "",
    param: nodeData.param && typeof nodeData.param === "object" && !Array.isArray(nodeData.param)
      ? nodeData.param
      : {},
    ports: normalizeNodePorts(nodeData.ports, nodeType),
    has_files: nodeData.has_files ?? "no",
  };
  props.secret_params = normalizeSecretParamKeys(nodeData.secret_params, props.param);

  if (typeof nodeData.template_label === "string" && nodeData.template_label.trim()) {
    props.template_label = nodeData.template_label.trim();
  }

  const definitionId = normalizeDefinitionId(nodeData.definition_id);
  const definitionVersion = normalizeDefinitionVersion(nodeData.definition_version);
  if (definitionId) {
    props.definition_id = definitionId;
    props.definition_version = definitionVersion ?? 1;
  }
  const implementation = normalizeNodeImplementation(nodeData.implementation);
  if (Object.keys(implementation).length > 0) props.implementation = implementation;
  const configurationStatus = normalizeConfigurationStatus(nodeData.configuration_status);
  if (configurationStatus) props.configuration_status = configurationStatus;
  const generatedArtifact = normalizeGeneratedArtifact(nodeData.generated_artifact);
  if (generatedArtifact) props.generated_artifact = generatedArtifact;

  if (typeHasContent(nodeType)) props.content = nodeData.content ?? "";

  return props;
};

const TEXT_PREVIEW_EXTENSIONS = [
  '.txt', '.csv', '.tsv', '.json', '.xml', '.yaml', '.yml', '.md', '.js',
  '.ts', '.tsx', '.jsx', '.css', '.html', '.py', '.java', '.cpp', '.c',
  '.h', '.sh', '.sql', '.dockerfile', '.env',
];

export const isTextPreviewName = (name: string) => {
  const normalized = name.toLowerCase();
  return TEXT_PREVIEW_EXTENSIONS.some((extension) => normalized.endsWith(extension)) ||
    normalized === 'dockerfile' ||
    normalized.startsWith('dockerfile.');
};

export const isImagePreviewName = (name: string) =>
  /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(name);

export const isTextPreviewFile = (file: File) =>
  file.type.startsWith('text/') || isTextPreviewName(file.name);
