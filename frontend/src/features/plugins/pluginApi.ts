import { INLUMEN_API_URL } from '@/config/api';
import { apiFetch } from '@/utils/apiFetch';

export type PluginStepType = 'source' | 'task' | 'destination';

export type PluginParameter = {
  name: string;
  label?: string;
  description?: string;
  type?: 'string' | 'integer' | 'number' | 'boolean' | 'object' | 'array';
  required?: boolean;
  default?: unknown;
  enum?: unknown[];
  location?: 'query' | 'header' | 'path' | 'body';
  api_name?: string;
  operations?: string[];
};

export type PluginSecret = {
  name: string;
  label?: string;
  description?: string;
  required?: boolean;
  header?: string;
};

export type PluginOperation = {
  id: string;
  label?: string;
  description?: string;
  method: string;
  path: string;
};

export type PluginPort = { name: string; type?: string; description?: string };

/** Normalized ``inlumen.plugin@1`` manifest as returned by the backend. */
export type PluginManifest = {
  schema_version: 'inlumen.plugin@1';
  id: string;
  version: string;
  name: string;
  description: string;
  publisher?: string;
  step_type: PluginStepType;
  endpoint: { base_url: string; health_path?: string; openapi_url?: string };
  operations: PluginOperation[];
  parameters: PluginParameter[];
  secrets: PluginSecret[];
  auth?: { type: 'none' | 'api_key' | 'bearer'; secret?: string };
  ports: { inputs: PluginPort[]; outputs: PluginPort[] };
  instructions?: string;
  tags?: string[];
};

export type InstalledPlugin = {
  id: string;
  version: string;
  name: string;
  description: string;
  publisher: string;
  step_type: PluginStepType;
  template: string;
  source: string;
  installed_at: string;
  manifest: PluginManifest;
};

export type InstalledPluginsResponse = {
  plugins: InstalledPlugin[];
  can_manage: boolean;
};

export type EndpointCheck = {
  reachable: boolean;
  healthy?: boolean;
  status?: number;
  latency_ms?: number;
  url: string;
  detail?: string;
};

const readJson = async <T>(response: Response, fallback: string): Promise<T> => {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const details = Array.isArray(payload?.details) ? ` ${payload.details.join('; ')}` : '';
    throw new Error(`${payload?.error || fallback}${details}`);
  }
  return payload as T;
};

export const fetchInstalledPlugins = async (signal?: AbortSignal): Promise<InstalledPluginsResponse> => {
  const response = await readJson<Partial<InstalledPluginsResponse>>(
    await apiFetch(`${INLUMEN_API_URL}/api/plugins`, { signal }),
    'Could not load plugins.',
  );
  // A malformed payload must not crash the editor; treat it as "no plugins".
  return {
    ...response,
    plugins: Array.isArray(response?.plugins) ? response.plugins : [],
    can_manage: response?.can_manage === true,
  } as InstalledPluginsResponse;
};

export const installPluginManifest = async (manifest: unknown) =>
  readJson<InstalledPlugin>(
    await apiFetch(`${INLUMEN_API_URL}/api/admin/plugins`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest }),
    }),
    'Could not add the plugin.',
  );

export const uninstallPlugin = async (id: string) =>
  readJson<{ uninstalled: string }>(
    await apiFetch(`${INLUMEN_API_URL}/api/admin/plugins/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
    'Could not remove the plugin.',
  );

export const checkPluginEndpoint = async (id: string) =>
  readJson<EndpointCheck>(
    await apiFetch(`${INLUMEN_API_URL}/api/plugins/${encodeURIComponent(id)}/check`, {
      method: 'POST',
    }),
    'Could not check the plugin endpoint.',
  );
