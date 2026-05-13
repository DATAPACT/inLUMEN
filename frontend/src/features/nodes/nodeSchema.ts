export type StepType =
  | "action"
  | "input"
  | "output"
  | "config"
  | "storage"
  | "api"
  | "custom";

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

export const typeHasFiles = (type: StepType) =>
  type === "input" || type === "output" || type === "action" || type === "custom";

export const typeHasContent = (type: StepType) =>
  type === "input" || type === "output";

export const typeHasEndpoint = (type: StepType) =>
  type === "storage" || type === "api";

export const toDatabaseValue = (uiValue: unknown) =>
  String(uiValue ?? "").toLowerCase().trim();

export const pickNeo4jUpdatableProps = (
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
