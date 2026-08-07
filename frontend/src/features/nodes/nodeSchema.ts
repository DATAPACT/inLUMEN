export type StepType =
  | "action"
  | "input"
  | "output"
  | "config"
  | "storage"
  | "api"
  | "custom";

export type NodeImplementation = Record<string, unknown>;

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

export const STORAGE_DATABASE_OPTIONS = ["MinIO", "SQLite", "ChromaDB"] as const;

export type StorageDatabaseOption = (typeof STORAGE_DATABASE_OPTIONS)[number];

export type NodeFileMetadata = {
  filename?: string;
  name?: string;
  bucket?: string;
  added_at?: string;
  [key: string]: unknown;
};

export type NodeFileReference = File | string | NodeFileMetadata;

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

export const normalizeStorageDatabaseOption = (value: unknown): StorageDatabaseOption => {
  const candidate = String(value ?? "").trim().toLowerCase();
  return (
    STORAGE_DATABASE_OPTIONS.find((option) => option.toLowerCase() === candidate) ??
    "MinIO"
  );
};

export const normalizeType = (type: unknown): StepType => {
  const normalized = String(type ?? "").toLowerCase().trim().replace(/\s+/g, "_");
  if (
    normalized === "action" ||
    normalized === "input" ||
    normalized === "output" ||
    normalized === "config" ||
    normalized === "storage" ||
    normalized === "api" ||
    normalized === "custom"
  ) {
    return normalized;
  }

  const aliases: Record<string, StepType> = {
    data_ingestion: "input",
    "data-source": "input",
    data_source: "input",
    ingest: "input",
    ingestion: "input",
    source: "input",
    sensor: "input",
    sensors: "input",
    collect: "input",
    collection: "input",
    preprocess: "action",
    preprocessing: "action",
    processing: "action",
    transform: "action",
    transformation: "action",
    feature_engineering: "action",
    "feature-engineering": "action",
    training: "action",
    model_training: "action",
    "model-training": "action",
    evaluation: "action",
    model_evaluation: "action",
    "model-evaluation": "action",
    inference: "action",
    scoring: "action",
    alert: "output",
    alerting: "output",
    notification: "output",
    notify: "output",
    report: "output",
    reporting: "output",
    dashboard: "output",
    result: "output",
    results: "output",
    database: "storage",
    db: "storage",
    clipboard: "storage",
    endpoint: "api",
    api_call: "api",
    "api-call": "api",
    model_config: "config",
    "model-config": "config",
    configuration: "config",
  };
  if (aliases[normalized]) return aliases[normalized];
  if (normalized.includes("ingest") || normalized.includes("input") || normalized.includes("source")) return "input";
  if (normalized.includes("alert") || normalized.includes("output") || normalized.includes("report")) return "output";
  if (normalized.includes("storage") || normalized.includes("database") || normalized.includes("clipboard")) return "storage";
  if (normalized.includes("api") || normalized.includes("endpoint")) return "api";
  if (normalized.includes("config")) return "config";
  return "action";
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

// Dagster treats every graph node as an executable step, so every node type
// must offer the same simple script/input attachment workflow.
export const typeHasFiles = (_type: StepType) => true;

export const typeHasContent = (type: StepType) =>
  type === "input" || type === "output";

export const typeHasEndpoint = (type: StepType) =>
  type === "storage" || type === "api";

export const toDatabaseValue = (uiValue: unknown) =>
  String(uiValue ?? "").toLowerCase().trim();

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
  };

  const definitionId = normalizeDefinitionId(nodeData.definition_id);
  const definitionVersion = normalizeDefinitionVersion(nodeData.definition_version);
  if (definitionId) {
    props.definition_id = definitionId;
    props.definition_version = definitionVersion ?? 1;
    props.implementation = normalizeNodeImplementation(nodeData.implementation);
    const configurationStatus = normalizeConfigurationStatus(nodeData.configuration_status);
    if (configurationStatus) {
      props.configuration_status = configurationStatus;
    }
    const generatedArtifact = normalizeGeneratedArtifact(nodeData.generated_artifact);
    if (generatedArtifact) {
      props.generated_artifact = generatedArtifact;
    }
  }

  if (typeHasContent(nodeType)) {
    props.content = nodeData.content ?? "";
  }

  if (typeHasFiles(nodeType)) {
    props.has_files = nodeData.has_files ?? "no";
  }

  if (nodeType === "config") {
    props.param = nodeData.param ?? {};
  }

  if (typeHasEndpoint(nodeType)) {
    props.endpoint = nodeData.endpoint ?? "";
  }

  if (nodeType === "storage") {
    props.database = toDatabaseValue(nodeData.database ?? "MinIO");
  }

  return props;
};

const TEXT_PREVIEW_EXTENSIONS = [
  '.txt',
  '.csv',
  '.tsv',
  '.json',
  '.xml',
  '.yaml',
  '.yml',
  '.md',
  '.js',
  '.ts',
  '.tsx',
  '.jsx',
  '.css',
  '.html',
  '.py',
  '.java',
  '.cpp',
  '.c',
  '.h',
  '.sh',
  '.sql',
  '.dockerfile',
  '.env',
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
