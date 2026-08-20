import { apiFetch } from "@/utils/apiFetch";
import { INLUMEN_API_URL } from "@/config/api";
import {
  nodeDefinitionResponseSchema,
  type NodeDefinition,
  type NodeDefinitionData,
} from "@/features/nodes/registry/types";
import { normalizeNodePorts } from "@/features/nodes/nodeSchema";
import {
  defaultParametersForTemplate,
  defaultTemplateForType,
  findTemplateForType,
} from "@/features/nodes/templateCatalog";

const CORE_FALLBACK_DEFINITIONS: NodeDefinition[] = [
  {
    id: "core.source",
    version: 1,
    base_type: "source",
    family: "sources",
    enabled: true,
    palette: {
      label: "Source",
      description: "Adapt an external system into logical pipeline data.",
      icon: "file-text",
      color: "blue",
      order: 10,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.task",
    version: 1,
    base_type: "task",
    family: "tasks",
    enabled: true,
    palette: {
      label: "Task",
      description: "Process, transform, validate, or analyze pipeline data.",
      icon: "zap",
      color: "amber",
      order: 20,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: { kind: "python", language: "python" },
  },
  {
    id: "core.destination",
    version: 1,
    base_type: "destination",
    family: "destinations",
    enabled: true,
    palette: {
      label: "Destination",
      description: "Write or publish pipeline results outside the pipeline.",
      icon: "file-output",
      color: "emerald",
      order: 30,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.flow",
    version: 1,
    base_type: "flow",
    family: "flow",
    enabled: true,
    palette: {
      label: "Flow",
      description: "Model conditions and parallel maps without an execution-engine-specific node.",
      icon: "git-branch",
      color: "purple",
      order: 40,
    },
    editor: { kind: "default" },
    runtime: { generator: "generic" },
    default_implementation: {},
  },
  {
    id: "core.subpipeline",
    version: 1,
    base_type: "subpipeline",
    family: "subpipeline",
    enabled: true,
    palette: {
      label: "Subpipeline",
      description: "Reuse another pipeline as one composable component.",
      icon: "boxes",
      color: "cyan",
      order: 50,
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
): NodeDefinitionData => {
  const templateName = defaultTemplateForType(definition.base_type);
  const template = findTemplateForType(definition.base_type, templateName);
  return {
    label: definition.base_type === "flow" ? (template?.label || definition.palette.label) : definition.palette.label,
    description: template?.description || definition.palette.description,
    type: definition.base_type,
    definition_id: definition.id,
    definition_version: definition.version,
    implementation: cloneImplementation(definition.default_implementation),
    template_label: templateName,
    template: {
      id: template?.id || `core.${definition.base_type}`,
      name: templateName,
    },
    ports: normalizeNodePorts(template?.ports, definition.base_type),
    param: defaultParametersForTemplate(definition.base_type, templateName),
    ...(definition.editor.kind !== "default"
      ? { configuration_status: "unconfigured" as const }
      : {}),
  };
};

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
