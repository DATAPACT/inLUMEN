import type { ComponentTemplate } from '@/features/nodes/templateCatalog';
import type { NodePorts } from '@/features/nodes/nodeSchema';
import type { InstalledPlugin, PluginManifest, PluginParameter } from './pluginApi';

export const PLUGIN_TEMPLATE_PREFIX = 'plugin:';

export const pluginTemplateValue = (pluginId: string) => `${PLUGIN_TEMPLATE_PREFIX}${pluginId}`;

export const pluginIdFromTemplate = (value: unknown): string | null => {
  const text = String(value ?? '').trim();
  if (!text.toLowerCase().startsWith(PLUGIN_TEMPLATE_PREFIX)) return null;
  const id = text.slice(PLUGIN_TEMPLATE_PREFIX.length).trim().toLowerCase();
  return id || null;
};

export const isPluginTemplate = (value: unknown) => pluginIdFromTemplate(value) !== null;

const portId = (name: string) =>
  name.toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^-+|-+$/g, '') || 'port';

export const pluginPorts = (manifest: PluginManifest): NodePorts => ({
  inputs: (manifest.ports?.inputs || []).map((port) => ({
    id: portId(port.name),
    name: port.name,
    type: port.type || 'any',
    required: true,
    description: port.description || '',
  })),
  outputs: (manifest.ports?.outputs || []).map((port) => ({
    id: portId(port.name),
    name: port.name,
    type: port.type || 'any',
    required: true,
    description: port.description || '',
  })),
});

export const parametersForOperation = (manifest: PluginManifest, operationId?: unknown) => {
  const operation = String(operationId || manifest.operations[0]?.id || '');
  return (manifest.parameters || []).filter(
    (parameter) => !parameter.operations?.length || parameter.operations.includes(operation),
  );
};

export const isRequiredParameter = (parameter: PluginParameter) =>
  Boolean(parameter.required) && parameter.default === undefined;

export const pluginDefaultParameters = (manifest: PluginManifest): Record<string, unknown> => ({
  operation: manifest.operations[0]?.id || '',
  ...Object.fromEntries((manifest.parameters || []).map((p) => [p.name, p.default ?? ''])),
  // Optional secrets are added only when the user enters a value; an
  // unconfigured sensitive parameter would fail the run.
  ...Object.fromEntries(
    (manifest.secrets || []).filter((s) => s.required !== false).map((s) => [s.name, '']),
  ),
});

export const pluginSecretNames = (manifest: PluginManifest) =>
  (manifest.secrets || []).map((secret) => secret.name);

/** Present an installed plugin through the existing component-template model. */
export const pluginToTemplate = (plugin: InstalledPlugin): ComponentTemplate => {
  const manifest = plugin.manifest;
  return {
    id: `plugin.${manifest.id}`,
    value: pluginTemplateValue(manifest.id),
    label: manifest.name,
    category: 'Plugins',
    description: manifest.description,
    ports: pluginPorts(manifest),
    requiredParameters: parametersForOperation(manifest)
      .filter(isRequiredParameter)
      .map((parameter) => parameter.name),
    defaultParameters: pluginDefaultParameters(manifest),
    configurationFields: [
      ...(manifest.parameters || []).map((parameter) => ({
        name: parameter.name,
        label: parameter.label || parameter.name,
        placeholder: parameter.description || '',
      })),
      ...(manifest.secrets || []).map((secret) => ({
        name: secret.name,
        label: secret.label || secret.name,
        placeholder: secret.description || '',
        secret: true,
      })),
    ],
    plugin: manifest,
  };
};
