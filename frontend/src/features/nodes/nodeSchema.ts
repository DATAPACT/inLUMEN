export type StepType =
  | "source"
  | "task"
  | "destination"
  | "flow"
  | "subpipeline";

export const STEP_TYPE_LABELS: Record<StepType, string> = {
  source: "Source",
  task: "Task",
  destination: "Destination",
  flow: "Flow",
  subpipeline: "Subpipeline",
};

export const getStepTypeLabel = (type: StepType) => STEP_TYPE_LABELS[type];

export type NodePort = {
  id: string;
  name: string;
  type: string;
  required: boolean;
  description: string;
  format?: string;
  schema?: Record<string, unknown>;
};

export type NodePorts = {
  inputs: NodePort[];
  outputs: NodePort[];
};

export const DEFAULT_NODE_PORTS: Record<StepType, NodePorts> = {
  source: {
    inputs: [],
    outputs: [{ id: "data", name: "data", type: "any", required: true, description: "Source data." }],
  },
  task: {
    inputs: [{ id: "input", name: "input", type: "any", required: true, description: "Task input." }],
    outputs: [{ id: "output", name: "output", type: "any", required: true, description: "Task output." }],
  },
  destination: {
    inputs: [{ id: "data", name: "data", type: "any", required: true, description: "Data to deliver." }],
    outputs: [],
  },
  flow: {
    inputs: [{ id: "input", name: "input", type: "any", required: true, description: "Flow input." }],
    outputs: [{ id: "output", name: "output", type: "any", required: true, description: "Flow output." }],
  },
  subpipeline: {
    inputs: [{ id: "input", name: "input", type: "any", required: true, description: "Nested pipeline input." }],
    outputs: [{ id: "output", name: "output", type: "any", required: true, description: "Nested pipeline output." }],
  },
};

const normalizePortList = (value: unknown, fallback: NodePort[]) => {
  if (!Array.isArray(value)) return fallback.map((port) => ({ ...port }));
  const ids = new Set<string>();
  return value.flatMap((entry, index) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const candidate = entry as Record<string, unknown>;
    const name = String(candidate.name ?? candidate.label ?? candidate.id ?? "").trim();
    const baseId = String(candidate.id ?? name ?? `port-${index + 1}`)
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
    const contractType = String(candidate.type ?? candidate.data_type ?? "any").trim() || "any";
    const description = String(candidate.description ?? "").trim();
    const format = String(candidate.format ?? "").trim();
    const schema = candidate.schema && typeof candidate.schema === "object" && !Array.isArray(candidate.schema)
      ? candidate.schema as Record<string, unknown>
      : undefined;
    return [{
      id,
      name: name || id,
      type: contractType,
      required: typeof candidate.required === "boolean" ? candidate.required : true,
      description,
      ...(format ? { format } : {}),
      ...(schema ? { schema } : {}),
    }];
  });
};

export const normalizeNodePorts = (value: unknown, type: StepType): NodePorts => {
  const candidate = value && typeof value === "object" && !Array.isArray(value)
    ? value as Partial<NodePorts>
    : {};
  const inputs = type === "source"
    ? []
    : normalizePortList(candidate.inputs, DEFAULT_NODE_PORTS[type].inputs);
  const outputs = type === "destination"
    ? []
    : normalizePortList(candidate.outputs, DEFAULT_NODE_PORTS[type].outputs);
  return {
    inputs: inputs.map((port) => ({
      ...port,
      description: type === "destination" && port.description === "Data consumed by this adapter."
        ? "Data consumed by this destination."
        : port.description,
    })),
    outputs: outputs.map((port) => ({
      ...port,
      description: type === "source" && port.description === "Data emitted by this adapter."
        ? "Data emitted by this source."
        : port.description,
    })),
  };
};

export const IMPLEMENTATION_KIND_OPTIONS = [
  { value: "python", label: "Python" },
  { value: "sql", label: "SQL" },
  { value: "container", label: "Container" },
  { value: "repository", label: "Repository" },
  { value: "rest-api", label: "REST API" },
  { value: "shell", label: "Shell" },
  { value: "generated-code", label: "Generated code" },
] as const;

export type ImplementationKind = (typeof IMPLEMENTATION_KIND_OPTIONS)[number]["value"];

export const normalizeImplementationKind = (value: unknown): ImplementationKind => {
  const raw = String(value ?? "").trim().toLowerCase().replace(/\s+/g, "-");
  const normalized = raw === "git-repository" ? "repository" : raw;
  return IMPLEMENTATION_KIND_OPTIONS.some((option) => option.value === normalized)
    ? normalized as ImplementationKind
    : "python";
};

export type NodeImplementation = Record<string, unknown> & {
  kind?: ImplementationKind;
  language?: string;
  dependencies?: string[];
  entrypoint?: string;
};

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
  "destination",
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
  sink: "destination",
  output: "destination",
  alert: "destination",
  notification: "destination",
  report: "destination",
  reporting: "destination",
  result: "destination",
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
  if (["sink", "destination", "alert", "output", "report", "publish", "notification"].some((token) => normalized.includes(token))) return "destination";
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
    ? {
        ...(value as NodeImplementation),
        kind: normalizeImplementationKind((value as NodeImplementation).kind),
      }
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
  type === "source" || type === "destination";

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
  if (nodeData.template && typeof nodeData.template === "object" && !Array.isArray(nodeData.template)) {
    props.template = nodeData.template;
  }
  if (nodeType === "source" && nodeData.source_config && typeof nodeData.source_config === "object" && !Array.isArray(nodeData.source_config)) {
    props.source_config = nodeData.source_config;
  }
  if (nodeType === "subpipeline" && nodeData.subpipeline && typeof nodeData.subpipeline === "object" && !Array.isArray(nodeData.subpipeline)) {
    props.subpipeline = nodeData.subpipeline;
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
