import { supabase } from "@/integrations/supabase/client";
import { toast } from "sonner";

export type LLMProvider = "openrouter" | "ollama_cloud" | "custom";

export interface ChatbotConfig {
  id?: string;
  name: string;
  provider: LLMProvider;
  model: string;
  baseUrl: string;
  apiKey?: string;
  system_prompt?: string;
  temperature?: number;
}

export interface LLMRequestConfig {
  provider: LLMProvider;
  model: string;
  base_url: string;
  api_key?: string;
  model_family: string;
  supports_function_calling: boolean;
  supports_json_output: boolean;
  supports_structured_output: boolean;
  supports_vision: boolean;
}

export const LLM_PROVIDER_DETAILS: Record<
  LLMProvider,
  { label: string; baseUrl: string; defaultModel: string; description: string }
> = {
  openrouter: {
    label: "OpenRouter",
    baseUrl: "https://openrouter.ai/api/v1",
    defaultModel: "gpt-oss-120b",
    description: "OpenAI-compatible router for multiple hosted model providers.",
  },
  ollama_cloud: {
    label: "Ollama Cloud",
    baseUrl: "https://ollama.com/v1",
    defaultModel: "gpt-oss:120b",
    description: "Ollama-hosted cloud models through the OpenAI-compatible endpoint.",
  },
  custom: {
    label: "Custom / On premise",
    baseUrl: "",
    defaultModel: "",
    description: "Any OpenAI-compatible endpoint exposed by your deployment.",
  },
};

const LOCAL_CONFIG_KEY = "inlumen-chatbot-config-overrides";
const LOCAL_ONLY_CONFIGS_KEY = "inlumen-chatbot-local-configs";
const REMOTE_CONFIG_CACHE_KEY = "inlumen-chatbot-remote-config-cache";
const REMOTE_CONFIG_SYNC_ENABLED =
  String(import.meta.env.VITE_ENABLE_REMOTE_CHATBOT_CONFIG_SYNC || "").trim().toLowerCase() ===
  "true";

type StoredConfigValues = Partial<
  Pick<ChatbotConfig, "provider" | "baseUrl" | "apiKey">
>;

const canUseLocalStorage = () => typeof window !== "undefined" && Boolean(window.localStorage);

const readStoredConfigValues = (): Record<string, StoredConfigValues> => {
  if (!canUseLocalStorage()) return {};
  try {
    return JSON.parse(localStorage.getItem(LOCAL_CONFIG_KEY) || "{}");
  } catch {
    return {};
  }
};

const writeStoredConfigValues = (values: Record<string, StoredConfigValues>) => {
  if (!canUseLocalStorage()) return;
  localStorage.setItem(LOCAL_CONFIG_KEY, JSON.stringify(values));
};

const readLocalOnlyConfigs = (): ChatbotConfig[] => {
  if (!canUseLocalStorage()) return [];
  try {
    const raw = JSON.parse(localStorage.getItem(LOCAL_ONLY_CONFIGS_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => normalizeConfig(item as Record<string, unknown>));
  } catch {
    return [];
  }
};

const writeLocalOnlyConfigs = (configs: ChatbotConfig[]) => {
  if (!canUseLocalStorage()) return;
  localStorage.setItem(LOCAL_ONLY_CONFIGS_KEY, JSON.stringify(configs));
};

const readCachedRemoteConfigs = (): ChatbotConfig[] => {
  if (!canUseLocalStorage()) return [];
  try {
    const raw = JSON.parse(localStorage.getItem(REMOTE_CONFIG_CACHE_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => normalizeConfig(item as Record<string, unknown>));
  } catch {
    return [];
  }
};

const writeCachedRemoteConfigs = (configs: ChatbotConfig[]) => {
  if (!canUseLocalStorage()) return;
  localStorage.setItem(REMOTE_CONFIG_CACHE_KEY, JSON.stringify(configs));
};

const makeLocalConfigId = () => {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `local:${crypto.randomUUID()}`;
  }
  return `local:${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

const isLocalConfigId = (id?: string) => Boolean(id?.startsWith("local:"));

const configStorageKey = (config: Pick<ChatbotConfig, "name" | "model"> & { id?: string }) =>
  config.id ? `id:${config.id}` : `draft:${config.name}:${config.model}`;

const normalizeProvider = (provider?: string | null): LLMProvider => {
  const normalized = (provider || "").toLowerCase().replace("-", "_");
  if (normalized === "openrouter" || normalized === "open_router") return "openrouter";
  if (normalized === "ollama_cloud" || normalized === "ollama") return "ollama_cloud";
  return "custom";
};

const normalizeOpenRouterModel = (model: string) => {
  const aliases: Record<string, string> = {
    "gpt-4o": "openai/gpt-4o",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "gpt-oss:120b": "openai/gpt-oss-120b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "gpt-oss:20b": "openai/gpt-oss-20b",
  };
  return aliases[model.toLowerCase()] || model;
};

const errorToMessage = (error: unknown) => {
  if (error instanceof Error) return error.message;
  if (error && typeof error === "object" && "message" in error) {
    return String((error as { message?: unknown }).message || "Unknown error occurred");
  }
  return "Unknown error occurred";
};

export const getDefaultChatbotConfig = (): ChatbotConfig => ({
  name: "OpenRouter",
  provider: "openrouter",
  model: LLM_PROVIDER_DETAILS.openrouter.defaultModel,
  baseUrl: LLM_PROVIDER_DETAILS.openrouter.baseUrl,
  apiKey: "",
});

const normalizeConfig = (config: Partial<ChatbotConfig> & Record<string, unknown>): ChatbotConfig => {
  const stored = readStoredConfigValues()[configStorageKey({
    id: typeof config.id === "string" ? config.id : undefined,
    name: String(config.name || "OpenRouter"),
    model: String(config.model || LLM_PROVIDER_DETAILS.openrouter.defaultModel),
  })] || {};

  const provider = normalizeProvider(
    (config.provider as string | undefined) || stored.provider || "openrouter"
  );
  const providerDefaults = LLM_PROVIDER_DETAILS[provider];
  const rawModel = String(config.model || providerDefaults.defaultModel || "");
  const model =
    provider === "openrouter" && rawModel.toLowerCase() === "llama3.1"
      ? providerDefaults.defaultModel
      : provider === "openrouter"
        ? normalizeOpenRouterModel(rawModel)
        : rawModel;
  const baseUrl = String(
    (config.baseUrl as string | undefined) ||
      (config.base_url as string | undefined) ||
      stored.baseUrl ||
      providerDefaults.baseUrl
  );

  return {
    id: typeof config.id === "string" ? config.id : undefined,
    name: String(config.name || providerDefaults.label),
    provider,
    model,
    baseUrl,
    apiKey: String((config.apiKey as string | undefined) || stored.apiKey || ""),
    system_prompt: typeof config.system_prompt === "string" ? config.system_prompt : "",
    temperature: typeof config.temperature === "number" ? config.temperature : 0.7,
  };
};

const persistLocalConfigValues = (config: ChatbotConfig) => {
  const values = readStoredConfigValues();
  values[configStorageKey(config)] = {
    provider: config.provider,
    baseUrl: config.baseUrl,
    apiKey: config.apiKey || "",
  };
  writeStoredConfigValues(values);
};

const deleteLocalConfigValues = (id: string) => {
  const values = readStoredConfigValues();
  delete values[`id:${id}`];
  writeStoredConfigValues(values);
};

const createLocalOnlyConfig = (config: ChatbotConfig): ChatbotConfig => {
  const savedConfig = normalizeConfig({
    ...config,
    id: makeLocalConfigId(),
  });
  writeLocalOnlyConfigs([savedConfig, ...readLocalOnlyConfigs()]);
  persistLocalConfigValues(savedConfig);
  return savedConfig;
};

const updateLocalOnlyConfig = (config: ChatbotConfig): ChatbotConfig => {
  const savedConfig = normalizeConfig(config as Partial<ChatbotConfig> & Record<string, unknown>);
  const configs = readLocalOnlyConfigs();
  const nextConfigs = configs.some((item) => item.id === savedConfig.id)
    ? configs.map((item) => (item.id === savedConfig.id ? savedConfig : item))
    : [savedConfig, ...configs];
  writeLocalOnlyConfigs(nextConfigs);
  persistLocalConfigValues(savedConfig);
  return savedConfig;
};

const deleteLocalOnlyConfig = (id: string) => {
  writeLocalOnlyConfigs(readLocalOnlyConfigs().filter((config) => config.id !== id));
  deleteLocalConfigValues(id);
};

const upsertCachedRemoteConfig = (config: ChatbotConfig): ChatbotConfig => {
  const savedConfig = normalizeConfig(config as Partial<ChatbotConfig> & Record<string, unknown>);
  const configs = readCachedRemoteConfigs();
  const nextConfigs = configs.some((item) => item.id === savedConfig.id)
    ? configs.map((item) => (item.id === savedConfig.id ? savedConfig : item))
    : [savedConfig, ...configs];
  writeCachedRemoteConfigs(nextConfigs);
  persistLocalConfigValues(savedConfig);
  return savedConfig;
};

const deleteCachedRemoteConfig = (id: string) => {
  writeCachedRemoteConfigs(readCachedRemoteConfigs().filter((config) => config.id !== id));
  deleteLocalConfigValues(id);
};

const getAvailableConfigs = (remoteConfigs: ChatbotConfig[] = readCachedRemoteConfigs()) => [
  ...readLocalOnlyConfigs(),
  ...remoteConfigs,
];

export const buildLLMRequestConfig = (config: ChatbotConfig): LLMRequestConfig => {
  const normalizedConfig = normalizeConfig(config as Partial<ChatbotConfig> & Record<string, unknown>);
  return {
    provider: normalizedConfig.provider,
    model: normalizedConfig.model,
    base_url: normalizedConfig.baseUrl,
    api_key: normalizedConfig.apiKey || undefined,
    model_family: "unknown",
    supports_function_calling: true,
    supports_json_output: true,
    supports_structured_output: true,
    supports_vision: false,
  };
};

export const formatProviderLabel = (provider: LLMProvider) => LLM_PROVIDER_DETAILS[provider].label;

export const fetchChatbotConfigs = async (): Promise<ChatbotConfig[]> => {
  const cachedRemoteConfigs = readCachedRemoteConfigs();
  if (!REMOTE_CONFIG_SYNC_ENABLED) {
    return getAvailableConfigs(cachedRemoteConfigs);
  }

  try {
    const { data, error } = await supabase
      .from("chatbot_configurations")
      .select("*")
      .order("created_at", { ascending: false });

    if (error) throw error;
    const remoteConfigs = ((data || []) as Array<Record<string, unknown>>).map(normalizeConfig);
    writeCachedRemoteConfigs(remoteConfigs);
    return getAvailableConfigs(remoteConfigs);
  } catch (error) {
    console.warn("Falling back to locally cached chatbot configurations:", error);
    return getAvailableConfigs(cachedRemoteConfigs);
  }
};

export const fetchChatbotConfig = async (id: string): Promise<ChatbotConfig | null> => {
  if (isLocalConfigId(id)) {
    return readLocalOnlyConfigs().find((config) => config.id === id) || null;
  }

  const cachedRemoteConfig = readCachedRemoteConfigs().find((config) => config.id === id) || null;
  if (!REMOTE_CONFIG_SYNC_ENABLED) {
    return cachedRemoteConfig;
  }

  try {
    const { data, error } = await supabase
      .from("chatbot_configurations")
      .select("*")
      .eq("id", id)
      .single();

    if (error) throw error;
    const savedConfig = upsertCachedRemoteConfig(normalizeConfig(data as Record<string, unknown>));
    return savedConfig;
  } catch (error) {
    console.error("Error fetching chatbot configuration:", error);
    return cachedRemoteConfig;
  }
};

export const createChatbotConfig = async (config: ChatbotConfig): Promise<ChatbotConfig | null> => {
  if (!config.name || !config.model || !config.baseUrl) {
    throw new Error("Missing required configuration fields");
  }

  if (!REMOTE_CONFIG_SYNC_ENABLED) {
    const savedConfig = createLocalOnlyConfig(config);
    toast.success("Configuration saved successfully");
    return savedConfig;
  }

  try {
    const payload = {
      name: config.name,
      model: config.model,
    };

    const { data, error } = await supabase
      .from("chatbot_configurations")
      .insert(payload)
      .select()
      .single();

    if (error) throw error;
    const savedConfig = normalizeConfig({
      ...(data as Record<string, unknown>),
      provider: config.provider,
      baseUrl: config.baseUrl,
      apiKey: config.apiKey || "",
    });
    upsertCachedRemoteConfig(savedConfig);
    toast.success("Configuration saved successfully");
    return savedConfig;
  } catch (error) {
    console.error("Error creating chatbot configuration:", error);
    const savedConfig = createLocalOnlyConfig(config);
    toast.success("Configuration saved locally", {
      description: `Remote save failed: ${errorToMessage(error)}`,
    });
    return savedConfig;
  }
};

export const updateChatbotConfig = async (config: ChatbotConfig): Promise<ChatbotConfig | null> => {
  if (!config.id) return null;
  if (!config.name || !config.model || !config.baseUrl) {
    throw new Error("Missing required configuration fields");
  }

  if (isLocalConfigId(config.id)) {
    const savedConfig = updateLocalOnlyConfig(config);
    toast.success("Configuration updated locally");
    return savedConfig;
  }

  if (!REMOTE_CONFIG_SYNC_ENABLED) {
    const savedConfig = upsertCachedRemoteConfig(config);
    toast.success("Configuration updated successfully");
    return savedConfig;
  }

  try {
    const { data, error } = await supabase
      .from("chatbot_configurations")
      .update({
        name: config.name,
        model: config.model,
        updated_at: new Date().toISOString(),
      })
      .eq("id", config.id)
      .select()
      .single();

    if (error) throw error;
    const savedConfig = normalizeConfig({
      ...(data as Record<string, unknown>),
      provider: config.provider,
      baseUrl: config.baseUrl,
      apiKey: config.apiKey || "",
    });
    upsertCachedRemoteConfig(savedConfig);
    toast.success("Configuration updated successfully");
    return savedConfig;
  } catch (error) {
    console.error("Error updating chatbot configuration:", error);
    const savedConfig = updateLocalOnlyConfig(config);
    toast.success("Configuration updated locally", {
      description: `Remote update failed: ${errorToMessage(error)}`,
    });
    return savedConfig;
  }
};

export const deleteChatbotConfig = async (id: string): Promise<boolean> => {
  if (isLocalConfigId(id)) {
    deleteLocalOnlyConfig(id);
    toast.success("Configuration deleted successfully");
    return true;
  }

  if (!REMOTE_CONFIG_SYNC_ENABLED) {
    deleteCachedRemoteConfig(id);
    toast.success("Configuration deleted successfully");
    return true;
  }

  try {
    const { error } = await supabase
      .from("chatbot_configurations")
      .delete()
      .eq("id", id);

    if (error) throw error;
    deleteCachedRemoteConfig(id);
    toast.success("Configuration deleted successfully");
    return true;
  } catch (error) {
    console.error("Error deleting chatbot configuration:", error);
    toast.error("Failed to delete configuration", {
      description: errorToMessage(error),
    });
    return false;
  }
};
