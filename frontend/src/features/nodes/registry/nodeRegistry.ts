import coreManifest from "./core.generated.json";
import { apiFetch } from "@/utils/apiFetch";
import { INLUMEN_API_URL } from "@/config/api";
import {
  nodeDefinitionResponseSchema,
  nodeDefinitionSchema,
  type NodeDefinition,
  type NodeDefinitionData,
} from "@/features/nodes/registry/types";
import { normalizeNodePorts } from "@/features/nodes/nodeSchema";
import {
  defaultParametersForTemplate,
  defaultTemplateForType,
  findTemplateForType,
} from "@/features/nodes/templateCatalog";

// Generated from the backend manifest; scripts/sync_shared.py --check enforces parity.
const CORE_FALLBACK_DEFINITIONS = coreManifest.definitions.map((definition) => nodeDefinitionSchema.parse(definition)).filter((definition) => definition.enabled);

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
