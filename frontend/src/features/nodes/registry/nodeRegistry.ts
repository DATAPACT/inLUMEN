import { apiFetch } from "@/utils/apiFetch";
import { INLUMEN_API_URL } from "@/config/api";
import {
  nodeDefinitionResponseSchema,
  type NodeDefinition,
  type NodeDefinitionData,
} from "@/features/nodes/registry/types";

const CORE_FALLBACK_DEFINITIONS: NodeDefinition[] = [
  {
    id: "core.model-configuration",
    version: 1,
    base_type: "config",
    family: "core",
    enabled: true,
    palette: {
      label: "Model Configuration",
      description: "Adjust model parameters, system prompt and more",
      icon: "settings",
      color: "sky",
      order: 10,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.input-data",
    version: 1,
    base_type: "input",
    family: "core",
    enabled: true,
    palette: {
      label: "Input Data",
      description: "Raw data from sensors, APIs, files or user message.",
      icon: "database",
      color: "blue",
      order: 20,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.data-preprocessing",
    version: 1,
    base_type: "action",
    family: "core",
    enabled: true,
    palette: {
      label: "Data Preprocessing",
      description: "Clean, normalize, and transform input data",
      icon: "file-text",
      color: "lime",
      order: 30,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.feature-engineering",
    version: 1,
    base_type: "action",
    family: "core",
    enabled: true,
    palette: {
      label: "Feature Engineering",
      description: "Generate or select features for model input",
      icon: "info",
      color: "yellow",
      order: 40,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.model-training",
    version: 1,
    base_type: "action",
    family: "core",
    enabled: true,
    palette: {
      label: "Model Training",
      description: "Train machine learning or deep learning models",
      icon: "brain",
      color: "indigo",
      order: 50,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.model-evaluation",
    version: 1,
    base_type: "action",
    family: "core",
    enabled: true,
    palette: {
      label: "Model Evaluation",
      description: "Assess model performance and metrics",
      icon: "zap",
      color: "purple",
      order: 60,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.output",
    version: 1,
    base_type: "output",
    family: "core",
    enabled: true,
    palette: {
      label: "AI/ML Output",
      description: "AI/ML pipeline results",
      icon: "message-circle",
      color: "emerald",
      order: 70,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.api-call",
    version: 1,
    base_type: "api",
    family: "core",
    enabled: true,
    palette: {
      label: "API Call",
      description: "Connect to external services",
      icon: "network",
      color: "rose",
      order: 80,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.clipboard",
    version: 1,
    base_type: "storage",
    family: "core",
    enabled: true,
    palette: {
      label: "Clipboard",
      description: "Store and retrieve content",
      icon: "clipboard",
      color: "teal",
      order: 90,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.custom",
    version: 1,
    base_type: "custom",
    family: "core",
    enabled: true,
    palette: {
      label: "Custom Node",
      description: "Add custom label and description",
      icon: "plus-circle",
      color: "violet",
      order: 100,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
];

let definitionsPromise: Promise<NodeDefinition[]> | null = null;
let definitionsById = new Map(
  CORE_FALLBACK_DEFINITIONS.map((definition) => [definition.id, definition]),
);

const cloneImplementation = (value: Record<string, unknown>) =>
  JSON.parse(JSON.stringify(value ?? {})) as Record<string, unknown>;

export const getFallbackNodeDefinitions = () =>
  CORE_FALLBACK_DEFINITIONS.map((definition) => ({
    ...definition,
    palette: { ...definition.palette },
    editor: { ...definition.editor },
    runtime: { ...definition.runtime },
    default_implementation: cloneImplementation(definition.default_implementation),
  }));

export const fetchNodeDefinitions = async (force = false): Promise<NodeDefinition[]> => {
  if (!force && definitionsPromise) return definitionsPromise;

  definitionsPromise = (async () => {
    const response = await apiFetch(`${INLUMEN_API_URL}/api/node-definitions`, {
      method: "GET",
    });
    if (!response.ok) {
      throw new Error(`Failed to load node definitions (${response.status})`);
    }
    const parsed = nodeDefinitionResponseSchema.parse(await response.json());
    const definitions = parsed.definitions
      .filter((definition) => definition.enabled)
      .sort((left, right) =>
        left.palette.order - right.palette.order ||
        left.palette.label.localeCompare(right.palette.label)
      );
    definitionsById = new Map(
      definitions.map((definition) => [definition.id, definition]),
    );
    return definitions;
  })();

  try {
    return await definitionsPromise;
  } catch (error) {
    definitionsPromise = null;
    throw error;
  }
};

export const createNodeDataFromDefinition = (
  definition: NodeDefinition,
): NodeDefinitionData => ({
  label: definition.palette.label,
  description: definition.palette.description,
  type: definition.base_type,
  definition_id: definition.id,
  definition_version: definition.version,
  implementation: cloneImplementation(definition.default_implementation),
  ...(definition.editor.kind !== "default"
    ? { configuration_status: "unconfigured" as const }
    : {}),
});

export const getNodeDefinitionEditorKind = (definitionId: string | undefined) => {
  if (!definitionId) return "default";
  const registeredKind = definitionsById.get(definitionId)?.editor.kind;
  if (registeredKind) return registeredKind;
  return "default";
};

export const groupNodeDefinitions = (definitions: NodeDefinition[]) => {
  const grouped = new Map<string, NodeDefinition[]>();
  definitions.forEach((definition) => {
    const familyDefinitions = grouped.get(definition.family) ?? [];
    familyDefinitions.push(definition);
    grouped.set(definition.family, familyDefinitions);
  });
  return Array.from(grouped.entries());
};
