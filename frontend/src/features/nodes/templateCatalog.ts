import type { StepType } from '@/features/nodes/nodeSchema';

export type ComponentTemplate = {
  value: string;
  label: string;
};

export const COMPONENT_TEMPLATE_CATALOG: Record<StepType, ComponentTemplate[]> = {
  source: [
    { value: "Source", label: "Generic source" },
    { value: "File", label: "File" },
    { value: "Folder", label: "Folder" },
    { value: "Database", label: "Database" },
    { value: "Object Storage", label: "Object Storage" },
    { value: "REST API", label: "REST API" },
    { value: "Kafka", label: "Kafka" },
    { value: "Message Queue", label: "Message Queue" },
    { value: "User Upload", label: "User Upload" },
  ],
  task: [
    { value: "Blank Task", label: "Blank Task" },
    { value: "Preprocessing", label: "Preprocessing" },
    { value: "Data Cleaning", label: "Data Cleaning" },
    { value: "Feature Engineering", label: "Feature Engineering" },
    { value: "Validation", label: "Validation" },
    { value: "Aggregation", label: "Aggregation" },
    { value: "Document Processing", label: "Document Processing" },
    { value: "OCR", label: "OCR" },
    { value: "Speech-to-Text", label: "Speech-to-Text" },
    { value: "Sentiment Analysis", label: "Sentiment Analysis" },
    { value: "Embeddings", label: "Embeddings" },
    { value: "Entity Linking", label: "Entity Linking" },
    { value: "Classification", label: "Classification" },
    { value: "Model Training", label: "Model Training" },
    { value: "Image Processing", label: "Image Processing" },
    { value: "LLM", label: "LLM" },
    { value: "API Call", label: "API Call" },
    { value: "Custom Logic", label: "Custom Logic" },
  ],
  sink: [
    { value: "Destination", label: "Generic destination" },
    { value: "File", label: "File" },
    { value: "Database", label: "Database" },
    { value: "Object Storage", label: "Object Storage" },
    { value: "REST API", label: "REST API" },
    { value: "Kafka", label: "Kafka" },
    { value: "Report", label: "Report" },
    { value: "Notification", label: "Notification" },
  ],
  flow: [
    { value: "Flow", label: "Generic flow" },
    { value: "Condition", label: "Condition" },
    { value: "Parallel Map", label: "Parallel Map" },
    { value: "Merge", label: "Merge" },
    { value: "Retry", label: "Retry" },
    { value: "Wait", label: "Wait" },
    { value: "Human Approval", label: "Human Approval" },
  ],
  subpipeline: [
    { value: "Subpipeline", label: "Subpipeline" },
  ],
};

export const defaultTemplateForType = (type: StepType) =>
  COMPONENT_TEMPLATE_CATALOG[type][0].value;

export const templateOptionsForType = (type: StepType, current?: unknown) => {
  const options = COMPONENT_TEMPLATE_CATALOG[type];
  const currentValue = typeof current === "string" ? current.trim() : "";
  if (!currentValue || options.some((option) => option.value === currentValue)) return options;
  return [{ value: currentValue, label: `${currentValue} (custom)` }, ...options];
};
