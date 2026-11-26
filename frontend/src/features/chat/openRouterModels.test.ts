import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearOpenRouterModelCacheForTests,
  fetchOpenRouterModels,
  formatContextLength,
  formatTokenPrice,
} from "@/services/openRouterModels";

describe("OpenRouter model catalog", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearOpenRouterModelCacheForTests();
  });

  it("combines live pricing with Cerebras availability", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: [{
          id: "openai/gpt-oss-120b",
          name: "OpenAI: GPT OSS 120B",
          context_length: 131072,
          pricing: { prompt: "0.00000025", completion: "0.00000069" },
          supported_parameters: ["tools"],
        }],
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: [{ id: "openai/gpt-oss-120b" }],
      }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const models = await fetchOpenRouterModels();

    expect(models).toEqual([expect.objectContaining({
      id: "openai/gpt-oss-120b",
      promptPrice: 0.00000025,
      completionPrice: 0.00000069,
      supportsTools: true,
      cerebrasAvailable: true,
    })]);
    expect(String(fetchMock.mock.calls[1][0])).toContain("providers=Cerebras");
  });

  it("formats token pricing and context for compact display", () => {
    expect(formatTokenPrice(0.00000025)).toBe("$0.25");
    expect(formatTokenPrice(0)).toBe("Free");
    expect(formatContextLength(131072)).toBe("131K context");
  });
});
