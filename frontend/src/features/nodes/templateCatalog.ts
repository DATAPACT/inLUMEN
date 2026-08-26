import type { NodePorts, StepType } from '@/features/nodes/nodeSchema';

export type ComponentTemplate = {
  id: string;
  value: string;
  label: string;
  category: string;
  description?: string;
  ports?: NodePorts;
  requiredParameters?: string[];
  defaultParameters?: Record<string, unknown>;
  configurationFields?: Array<{
    name: string;
    label: string;
    placeholder?: string;
    secret?: boolean;
  }>;
};

const field = (name: string, label: string, placeholder = "", secret = false) => ({
  name,
  label,
  placeholder,
  secret,
});

const port = (name: string, type: string, description: string, required = true) => ({
  id: name.toLowerCase().replace(/[^a-z0-9_.-]+/g, "-"),
  name,
  type,
  required,
  description,
});

const taskPorts = (
  inputName: string,
  inputType: string,
  outputName: string,
  outputType: string,
): NodePorts => ({
  inputs: [port(inputName, inputType, `Input ${inputName.replace(/_/g, " ")}.`)],
  outputs: [port(outputName, outputType, `Produced ${outputName.replace(/_/g, " ")}.`)],
});

const sourcePorts = (outputName: string, outputType: string): NodePorts => ({
  inputs: [],
  outputs: [port(outputName, outputType, `Data emitted by this source.`)],
});

const destinationPorts = (inputName: string, inputType: string): NodePorts => ({
  inputs: [port(inputName, inputType, `Data consumed by this destination.`)],
  outputs: [],
});

const categorized = (
  category: string,
  templates: Array<Omit<ComponentTemplate, "category">>,
): ComponentTemplate[] => templates.map((template) => ({ ...template, category }));

export const COMPONENT_TEMPLATE_CATALOG: Record<StepType, ComponentTemplate[]> = {
  source: categorized("Sources", [
    { id: "source.custom", value: "Custom", label: "Custom" },
    { id: "source.file", value: "File", label: "File", ports: sourcePorts("file", "File") },
    { id: "source.folder", value: "Folder", label: "Folder", ports: sourcePorts("files", "File[]") },
    {
      id: "source.database", value: "Database", label: "Database", ports: sourcePorts("rows", "Dataset"),
      requiredParameters: ["connection_url", "query"],
      defaultParameters: { connection_url: "", query: "", output_format: "csv" },
      configurationFields: [field("connection_url", "Connection URL", "postgresql://…", true), field("query", "Query", "SELECT …"), field("output_format", "Output format", "csv or parquet")],
    },
    {
      id: "source.object-storage", value: "Object Storage", label: "Object Storage", ports: sourcePorts("objects", "Object[]"),
      requiredParameters: ["bucket"],
      defaultParameters: { endpoint: "", bucket: "", access_key: "", secret_key: "" },
      configurationFields: [field("endpoint", "Endpoint"), field("bucket", "Bucket"), field("access_key", "Access key", "", true), field("secret_key", "Secret key", "", true)],
    },
    {
      id: "source.rest-api", value: "REST API", label: "REST API", ports: sourcePorts("response", "Object"),
      requiredParameters: ["url"],
      defaultParameters: { url: "", method: "GET" },
      configurationFields: [field("url", "URL", "https://api.example.com/…"), field("method", "Method", "GET")],
    },
    { id: "source.user-upload", value: "User Upload", label: "User Upload", ports: sourcePorts("uploaded_files", "File[]") },
  ]),
  task: [
    ...categorized("General", [
      { id: "task.blank", value: "Blank Task", label: "Blank Task" },
    ]),
    ...categorized("Data", [
      { id: "task.data-cleaning", value: "Data Cleaning", label: "Data Cleaning", ports: taskPorts("data", "Dataset", "cleaned_data", "Dataset") },
      { id: "task.validation", value: "Validation", label: "Validation", ports: taskPorts("data", "Dataset", "validated_data", "Dataset") },
      { id: "task.aggregation", value: "Aggregation", label: "Aggregation", ports: taskPorts("records", "Dataset", "aggregates", "Dataset") },
      { id: "task.feature-engineering", value: "Feature Engineering", label: "Feature Engineering", ports: taskPorts("records", "Dataset", "features", "FeatureSet") },
    ]),
    ...categorized("Document & media", [
      { id: "task.ocr", value: "OCR", label: "OCR", ports: taskPorts("document_image", "Image", "extracted_text", "Text") },
      { id: "task.speech-to-text", value: "Speech-to-Text", label: "Speech-to-Text", ports: taskPorts("audio", "Audio", "transcript", "Text") },
      { id: "task.image-processing", value: "Image Processing", label: "Image Processing", ports: taskPorts("images", "Image[]", "processed_images", "Image[]") },
    ]),
    ...categorized("AI & machine learning", [
      { id: "task.sentiment-analysis", value: "Sentiment Analysis", label: "Sentiment Analysis", ports: taskPorts("text", "Text", "sentiment", "Classification") },
      { id: "task.embeddings", value: "Embeddings", label: "Embeddings", ports: taskPorts("documents", "Document[]", "embeddings", "Vector[]") },
      { id: "task.entity-linking", value: "Entity Linking", label: "Entity Linking", ports: taskPorts("documents", "Document[]", "linked_entities", "Entity[]") },
      { id: "task.classification", value: "Classification", label: "Classification", ports: taskPorts("features", "FeatureSet", "predictions", "Prediction[]") },
      { id: "task.model-training", value: "Model Training", label: "Model Training", ports: taskPorts("training_data", "Dataset", "model_artifact", "Model") },
      {
        id: "task.llm", value: "LLM", label: "LLM", ports: taskPorts("prompt", "Text", "response", "Text"),
        requiredParameters: ["model"], defaultParameters: { model: "" },
        configurationFields: [field("model", "Model", "Provider model name")],
      },
    ]),
    ...categorized("Integration", [
      {
        id: "task.api-call", value: "API Call", label: "API Call", ports: taskPorts("request", "Object", "response", "Object"),
        requiredParameters: ["url"], defaultParameters: { url: "", method: "POST" },
        configurationFields: [field("url", "URL", "https://api.example.com/…"), field("method", "Method", "POST")],
      },
    ]),
  ],
  destination: categorized("Destinations", [
    { id: "destination.custom", value: "Custom", label: "Custom" },
    {
      id: "destination.file", value: "File", label: "File", ports: destinationPorts("data", "any"),
      requiredParameters: ["filename"], defaultParameters: { filename: "output.json" },
      configurationFields: [field("filename", "Output filename", "output.json")],
    },
    {
      id: "destination.object-storage", value: "Object Storage", label: "Object Storage", ports: destinationPorts("objects", "Object[]"),
      requiredParameters: ["bucket"], defaultParameters: { endpoint: "", bucket: "" },
      configurationFields: [field("endpoint", "Endpoint"), field("bucket", "Bucket"), field("prefix", "Object prefix"), field("object_name", "Object name")],
    },
    {
      id: "destination.rest-api", value: "REST API", label: "REST API", ports: destinationPorts("request", "Object"),
      requiredParameters: ["url"], defaultParameters: { url: "", method: "POST" },
      configurationFields: [field("url", "URL", "https://api.example.com/…"), field("method", "Method", "POST")],
    },
  ]),
  flow: [
    ...categorized("Flow control", [
      {
        id: "flow.condition",
        value: "Condition",
        label: "Condition",
        description: "Evaluate an expression and route the input through the true or false branch.",
        ports: {
          inputs: [port("value", "any", "Value evaluated by the condition.")],
          outputs: [
            port("when_true", "any", "Value routed when the condition is true."),
            port("when_false", "any", "Value routed when the condition is false.", false),
          ],
        },
        requiredParameters: ["expression"],
        defaultParameters: { expression: "" },
      },
      {
        id: "flow.parallel-map",
        value: "Parallel Map",
        label: "Parallel Map",
        description: "Fan out an array so the downstream branch runs once for each item.",
        ports: taskPorts("items", "any[]", "item", "any"),
        requiredParameters: ["max_concurrency"],
        defaultParameters: { max_concurrency: 4, failure_policy: "stop" },
      },
    ]),
    ...categorized("Legacy", [
      {
        id: "flow.generic",
        value: "Flow",
        label: "Generic flow (choose a behavior)",
        description: "Compatibility template for older projects; select Condition or Parallel Map before using it.",
      },
    ]),
  ],
  subpipeline: categorized("Subpipelines", [
    { id: "subpipeline.pipeline", value: "Subpipeline", label: "Subpipeline" },
  ]),
};

export const LEGACY_TASK_TEMPLATE_NAMES = new Set([
  "Preprocessing",
  "Document Processing",
  "Custom Logic",
]);

export const defaultTemplateForType = (type: StepType) =>
  COMPONENT_TEMPLATE_CATALOG[type][0].value;

export const templateOptionsForType = (type: StepType, current?: unknown) => {
  const options = COMPONENT_TEMPLATE_CATALOG[type];
  const currentValue = typeof current === "string" ? current.trim() : "";
  if (!currentValue || options.some((option) => option.value === currentValue)) return options;
  const legacy = type === "task" && LEGACY_TASK_TEMPLATE_NAMES.has(currentValue);
  return [{
    id: `${legacy ? "legacy" : "custom"}.${currentValue.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`,
    value: currentValue,
    label: `${currentValue} (${legacy ? "legacy" : "custom"})`,
    category: legacy ? "Legacy" : "Custom",
  }, ...options];
};

export const findTemplateForType = (type: StepType, value: unknown) => {
  const normalized = String(value ?? "").trim();
  return COMPONENT_TEMPLATE_CATALOG[type].find((template) =>
    template.value === normalized || template.id === normalized
  );
};

export const defaultParametersForTemplate = (type: StepType, value: unknown) => ({
  ...(findTemplateForType(type, value)?.defaultParameters || {}),
});
