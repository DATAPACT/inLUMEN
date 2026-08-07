import { beforeEach, describe, expect, it } from "vitest";

import {
  buildCodegenLLMRequestConfig,
  buildLLMRequestConfig,
  getDefaultChatbotConfig,
  readSelectedChatbotConfigId,
  writeSelectedChatbotConfigId,
  type ChatbotConfig,
} from "@/services/chatbotService";


const completeConfig = (overrides: Partial<ChatbotConfig> = {}): ChatbotConfig => ({
  name: "OpenRouter",
  provider: "openrouter",
  model: "gpt-oss:120b",
  codegenModel: "openai/gpt-5.2-codex",
  baseUrl: "https://openrouter.ai/api/v1",
  apiKey: "secret-key",
  ...overrides,
});

const createStorage = (): Storage => {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  };
};


describe("chatbot configuration contracts", () => {
  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: createStorage(),
    });
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: createStorage(),
    });
  });

  it("provides a usable OpenRouter default without inventing a key", () => {
    expect(getDefaultChatbotConfig()).toEqual({
      name: "OpenRouter",
      provider: "openrouter",
      model: "gpt-oss-120b",
      baseUrl: "https://openrouter.ai/api/v1",
      apiKey: "",
    });
  });

  it("builds a normalized chat request contract", () => {
    expect(buildLLMRequestConfig(completeConfig())).toEqual({
      provider: "openrouter",
      model: "openai/gpt-oss-120b",
      base_url: "https://openrouter.ai/api/v1",
      api_key: "secret-key",
      model_family: "unknown",
      supports_function_calling: true,
      supports_json_output: true,
      supports_structured_output: true,
      supports_vision: false,
    });
  });

  it("uses the dedicated code generation model and timeout", () => {
    expect(buildCodegenLLMRequestConfig(completeConfig())).toMatchObject({
      provider: "openrouter",
      model: "openai/gpt-5.2-codex",
      base_url: "https://openrouter.ai/api/v1",
      api_key: "secret-key",
      timeout_seconds: 90,
    });
  });

  it("rejects LLM requests without locally supplied credentials", () => {
    expect(() => buildLLMRequestConfig(completeConfig({ apiKey: "" })))
      .toThrow("Enter an LLM API key");
    expect(() => buildCodegenLLMRequestConfig(completeConfig({ codegenModel: "" })))
      .toThrow("Code Generation Model is required");
  });

  it("persists and clears the selected configuration", () => {
    expect(readSelectedChatbotConfigId()).toBeNull();
    writeSelectedChatbotConfigId("config-7");
    expect(readSelectedChatbotConfigId()).toBe("config-7");
    writeSelectedChatbotConfigId(null);
    expect(readSelectedChatbotConfigId()).toBeNull();
  });
});
