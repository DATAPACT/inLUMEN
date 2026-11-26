export interface OpenRouterModel {
  id: string;
  name: string;
  description: string;
  contextLength: number | null;
  promptPrice: number | null;
  completionPrice: number | null;
  supportsTools: boolean;
  cerebrasAvailable: boolean;
}

interface OpenRouterModelPayload {
  id?: unknown;
  name?: unknown;
  description?: unknown;
  context_length?: unknown;
  pricing?: {
    prompt?: unknown;
    completion?: unknown;
  };
  supported_parameters?: unknown;
}

const OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models";
const CATALOG_TTL_MS = 15 * 60 * 1000;

let cachedCatalog: { models: OpenRouterModel[]; expiresAt: number } | null = null;
let pendingCatalog: Promise<OpenRouterModel[]> | null = null;

const optionalNumber = (value: unknown): number | null => {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
};

const readModels = async (url: string, signal?: AbortSignal): Promise<OpenRouterModelPayload[]> => {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`OpenRouter model catalog returned ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload?.data) ? payload.data : [];
};

const loadCatalog = async (signal?: AbortSignal): Promise<OpenRouterModel[]> => {
  const query = new URLSearchParams({ output_modalities: "text" });
  const cerebrasQuery = new URLSearchParams({
    output_modalities: "text",
    providers: "Cerebras",
  });
  const [allModels, cerebrasModels] = await Promise.all([
    readModels(`${OPENROUTER_MODELS_URL}?${query}`, signal),
    readModels(`${OPENROUTER_MODELS_URL}?${cerebrasQuery}`, signal),
  ]);
  const cerebrasIds = new Set(
    cerebrasModels.flatMap((model) => typeof model.id === "string" ? [model.id] : []),
  );

  return allModels.flatMap((model) => {
    if (typeof model.id !== "string" || !model.id.trim()) return [];
    const supportedParameters = Array.isArray(model.supported_parameters)
      ? model.supported_parameters
      : [];
    return [{
      id: model.id,
      name: typeof model.name === "string" && model.name.trim() ? model.name : model.id,
      description: typeof model.description === "string" ? model.description : "",
      contextLength: optionalNumber(model.context_length),
      promptPrice: optionalNumber(model.pricing?.prompt),
      completionPrice: optionalNumber(model.pricing?.completion),
      supportsTools: supportedParameters.includes("tools"),
      cerebrasAvailable: cerebrasIds.has(model.id),
    }];
  });
};

export const fetchOpenRouterModels = async (signal?: AbortSignal): Promise<OpenRouterModel[]> => {
  if (cachedCatalog && cachedCatalog.expiresAt > Date.now()) return cachedCatalog.models;
  if (pendingCatalog) return pendingCatalog;

  pendingCatalog = loadCatalog(signal)
    .then((models) => {
      cachedCatalog = { models, expiresAt: Date.now() + CATALOG_TTL_MS };
      return models;
    })
    .finally(() => {
      pendingCatalog = null;
    });
  return pendingCatalog;
};

export const formatTokenPrice = (pricePerToken: number | null): string => {
  if (pricePerToken === null) return "Not listed";
  const pricePerMillion = pricePerToken * 1_000_000;
  if (pricePerMillion === 0) return "Free";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: pricePerMillion < 0.01 ? 3 : 2,
    maximumFractionDigits: pricePerMillion < 1 ? 3 : 2,
  }).format(pricePerMillion);
};

export const formatContextLength = (tokens: number | null): string => {
  if (tokens === null) return "Context not listed";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens % 1_000_000 ? 1 : 0)}M context`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K context`;
  return `${tokens} context`;
};

export const clearOpenRouterModelCacheForTests = () => {
  cachedCatalog = null;
  pendingCatalog = null;
};
