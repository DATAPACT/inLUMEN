import { INLUMEN_API_URL } from "@/config/api";
import { apiFetch } from "@/utils/apiFetch";
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
const SESSION_API_KEYS_KEY = "inlumen-chatbot-session-api-keys";
const REMOTE_CONFIG_SYNC_ENABLED =
  String(import.meta.env.VITE_ENABLE_REMOTE_CHATBOT_CONFIG_SYNC ?? "true").trim().toLowerCase() !==
  "false";

type StoredConfigValues = Partial<
  Pick<ChatbotConfig, "provider" | "baseUrl">
>;

const canUseLocalStorage = () => typeof window !== "undefined" && Boolean(window.localStorage);
const canUseSessionStorage = () => typeof window !== "undefined" && Boolean(window.sessionStorage);

const readStoredConfigValues = (): Record<string, StoredConfigValues> => {
  if (!canUseLocalStorage()) return {};
  try {
    const raw = JSON.parse(localStorage.getItem(LOCAL_CONFIG_KEY) || "{}");
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    return Object.fromEntries(
      Object.entries(raw as Record<string, unknown>).map(([key, value]) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) {
          return [key, {}];
        }
        const item = value as Record<string, unknown>;
        return [
          key,
          {
            provider: typeof item.provider === "string" ? item.provider as LLMProvider : undefined,
            baseUrl: typeof item.baseUrl === "string" ? item.baseUrl : undefined,
          },
        ];
      }),
    );
  } catch {
    return {};
  }
};

const writeStoredConfigValues = (values: Record<string, StoredConfigValues>) => {
  if (!canUseLocalStorage()) return;
  localStorage.setItem(LOCAL_CONFIG_KEY, JSON.stringify(values));
};

const readSessionApiKeys = (): Record<string, string> => {
  if (!canUseSessionStorage()) return {};
  try {
    return JSON.parse(sessionStorage.getItem(SESSION_API_KEYS_KEY) || "{}");
  } catch {
    return {};
  }
};

const writeSessionApiKey = (storageKey: string, apiKey: string) => {
  if (!canUseSessionStorage()) return;
  const values = readSessionApiKeys();
  if (apiKey) {
    values[storageKey] = apiKey;
  } else {
    delete values[storageKey];
  }
  sessionStorage.setItem(SESSION_API_KEYS_KEY, JSON.stringify(values));
};

const deleteSessionApiKey = (storageKey: string) => {
  writeSessionApiKey(storageKey, "");
};

const stripConfigSecret = (config: ChatbotConfig) => {
  const { apiKey: _apiKey, ...safeConfig } = config;
  return safeConfig;
};

const stripRawConfigSecret = (item: Record<string, unknown>) => {
  const { apiKey: _apiKey, api_key: _apiKeySnake, ...safeConfig } = item;
  return safeConfig;
};

const readLocalOnlyConfigs = (): ChatbotConfig[] => {
  if (!canUseLocalStorage()) return [];
  try {
    const raw = JSON.parse(localStorage.getItem(LOCAL_ONLY_CONFIGS_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => normalizeConfig(stripRawConfigSecret(item as Record<string, unknown>)));
  } catch {
    return [];
  }
};

const writeLocalOnlyConfigs = (configs: ChatbotConfig[]) => {
  if (!canUseLocalStorage()) return;
  localStorage.setItem(LOCAL_ONLY_CONFIGS_KEY, JSON.stringify(configs.map(stripConfigSecret)));
};

const readCachedRemoteConfigs = (): ChatbotConfig[] => {
  if (!canUseLocalStorage()) return [];
  try {
    const raw = JSON.parse(localStorage.getItem(REMOTE_CONFIG_CACHE_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => normalizeConfig(stripRawConfigSecret(item as Record<string, unknown>)));
  } catch {
    return [];
  }
};

const writeCachedRemoteConfigs = (configs: ChatbotConfig[]) => {
  if (!canUseLocalStorage()) return;
  localStorage.setItem(REMOTE_CONFIG_CACHE_KEY, JSON.stringify(configs.map(stripConfigSecret)));
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
  const storageKey = configStorageKey({
    id: typeof config.id === "string" ? config.id : undefined,
    name: String(config.name || "OpenRouter"),
    model: String(config.model || LLM_PROVIDER_DETAILS.openrouter.defaultModel),
  });
  const stored = readStoredConfigValues()[storageKey] || {};
  const sessionApiKey = readSessionApiKeys()[storageKey] || "";

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
    apiKey: String((config.apiKey as string | undefined) || sessionApiKey || ""),
    system_prompt: typeof config.system_prompt === "string" ? config.system_prompt : "",
    temperature: typeof config.temperature === "number" ? config.temperature : 0.7,
  };
};

const persistLocalConfigValues = (config: ChatbotConfig) => {
  const values = readStoredConfigValues();
  const storageKey = configStorageKey(config);
  values[storageKey] = {
    provider: config.provider,
    baseUrl: config.baseUrl,
  };
  writeStoredConfigValues(values);
  writeSessionApiKey(storageKey, config.apiKey || "");
};

const deleteLocalConfigValues = (id: string) => {
  const values = readStoredConfigValues();
  delete values[`id:${id}`];
  writeStoredConfigValues(values);
  deleteSessionApiKey(`id:${id}`);
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

const chatbotConfigUrl = (id?: string) =>
  id
    ? `${INLUMEN_API_URL}/api/chatbot-configs/${encodeURIComponent(id)}`
    : `${INLUMEN_API_URL}/api/chatbot-configs`;

const backendConfigPayload = (config: ChatbotConfig) => ({
  name: config.name,
  provider: config.provider,
  model: config.model,
  baseUrl: config.baseUrl,
  system_prompt: config.system_prompt || "",
  temperature: config.temperature ?? 0.7,
});

const readBackendError = async (response: Response) => {
  const text = await response.text().catch(() => "");
  if (!text) return `${response.status} ${response.statusText}`;
  try {
    const payload = JSON.parse(text);
    return payload?.error?.message || payload?.error || text;
  } catch {
    return text;
  }
};

const configFromBackendPayload = (
  payload: unknown,
  localValues?: Pick<ChatbotConfig, "apiKey" | "baseUrl" | "provider">,
): ChatbotConfig => {
  const raw = (payload && typeof payload === "object" && "config" in payload)
    ? (payload as { config?: unknown }).config
    : payload;
  const config = normalizeConfig((raw || {}) as Partial<ChatbotConfig> & Record<string, unknown>);
  return {
    ...config,
    provider: localValues?.provider || config.provider,
    baseUrl: localValues?.baseUrl || config.baseUrl,
    apiKey: localValues?.apiKey || config.apiKey || "",
  };
};

const fetchBackendConfigs = async (): Promise<ChatbotConfig[]> => {
  const response = await apiFetch(chatbotConfigUrl(), { method: "GET" });
  if (!response.ok) throw new Error(await readBackendError(response));
  const payload = await response.json().catch(() => ({}));
  const configs = Array.isArray(payload?.configs) ? payload.configs : [];
  return configs.map((item: Record<string, unknown>) => normalizeConfig(item));
};

const fetchBackendConfig = async (id: string): Promise<ChatbotConfig> => {
  const response = await apiFetch(chatbotConfigUrl(id), { method: "GET" });
  if (!response.ok) throw new Error(await readBackendError(response));
  return configFromBackendPayload(await response.json().catch(() => ({})));
};

const createBackendConfig = async (config: ChatbotConfig): Promise<ChatbotConfig> => {
  const response = await apiFetch(chatbotConfigUrl(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(backendConfigPayload(config)),
  });
  if (!response.ok) throw new Error(await readBackendError(response));
  return configFromBackendPayload(await response.json().catch(() => ({})), config);
};

const updateBackendConfig = async (config: ChatbotConfig): Promise<ChatbotConfig> => {
  if (!config.id) throw new Error("Missing configuration id");
  const response = await apiFetch(chatbotConfigUrl(config.id), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(backendConfigPayload(config)),
  });
  if (!response.ok) throw new Error(await readBackendError(response));
  return configFromBackendPayload(await response.json().catch(() => ({})), config);
};

const deleteBackendConfig = async (id: string): Promise<void> => {
  const response = await apiFetch(chatbotConfigUrl(id), { method: "DELETE" });
  if (!response.ok) throw new Error(await readBackendError(response));
};

const getAvailableConfigs = (remoteConfigs: ChatbotConfig[] = readCachedRemoteConfigs()) => [
  ...readLocalOnlyConfigs(),
  ...remoteConfigs,
];

export const buildLLMRequestConfig = (config: ChatbotConfig): LLMRequestConfig => {
  const normalizedConfig = normalizeConfig(config as Partial<ChatbotConfig> & Record<string, unknown>);
  if (!normalizedConfig.provider || !normalizedConfig.model || !normalizedConfig.baseUrl) {
    throw new Error("Complete the LLM provider, model, and base URL in Settings before using LLM features.");
  }
  if (!normalizedConfig.apiKey) {
    throw new Error("Enter an LLM API key in Settings before using chat or artifact generation.");
  }
  return {
    provider: normalizedConfig.provider,
    model: normalizedConfig.model,
    base_url: normalizedConfig.baseUrl,
    api_key: normalizedConfig.apiKey,
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
    const remoteConfigs = await fetchBackendConfigs();
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
    const savedConfig = upsertCachedRemoteConfig(await fetchBackendConfig(id));
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
    const savedConfig = await createBackendConfig(config);
    upsertCachedRemoteConfig(savedConfig);
    persistLocalConfigValues(savedConfig);
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
    const savedConfig = await updateBackendConfig(config);
    upsertCachedRemoteConfig(savedConfig);
    persistLocalConfigValues(savedConfig);
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
    await deleteBackendConfig(id);
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
