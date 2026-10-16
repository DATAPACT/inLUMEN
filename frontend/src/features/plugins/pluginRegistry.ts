import { useEffect, useSyncExternalStore } from 'react';
import { setPluginTemplates } from '@/features/nodes/templateCatalog';
import { fetchInstalledPlugins, type InstalledPlugin } from './pluginApi';
import { pluginToTemplate } from './pluginTemplates';

export type PluginRegistryState = {
  plugins: InstalledPlugin[];
  canManage: boolean;
  loaded: boolean;
  error: string;
};

let state: PluginRegistryState = {
  plugins: [],
  canManage: false,
  loaded: false,
  error: '',
};
let inFlight: Promise<void> | null = null;
const listeners = new Set<() => void>();

const publish = (next: PluginRegistryState) => {
  state = next;
  setPluginTemplates(next.plugins.map(pluginToTemplate));
  listeners.forEach((listener) => listener());
};

/** Load installed plugins once; pass force to refresh after install/uninstall. */
export const loadPlugins = (force = false): Promise<void> => {
  if (inFlight) return inFlight;
  if (state.loaded && !force) return Promise.resolve();
  inFlight = fetchInstalledPlugins()
    .then((response) => publish({
      plugins: response.plugins,
      canManage: response.can_manage,
      loaded: true,
      error: '',
    }))
    .catch((error: unknown) => publish({
      ...state,
      loaded: true,
      error: error instanceof Error ? error.message : 'Could not load plugins.',
    }))
    .finally(() => { inFlight = null; });
  return inFlight;
};

export const getPluginRegistryState = () => state;

/** Test hook: replace the registry contents without a network call. */
export const setPluginRegistryState = (next: Partial<PluginRegistryState>) =>
  publish({ ...state, loaded: true, ...next });

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export const usePluginRegistry = (): PluginRegistryState => {
  const snapshot = useSyncExternalStore(subscribe, getPluginRegistryState, getPluginRegistryState);
  useEffect(() => { void loadPlugins(); }, []);
  return snapshot;
};

export const findInstalledPlugin = (pluginId: string | null | undefined) =>
  pluginId ? state.plugins.find((plugin) => plugin.id === pluginId) : undefined;
