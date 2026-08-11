import { describe, expect, it } from "vitest";

import { CHAT_PROMPT_SUGGESTIONS } from "@/features/chat/promptSuggestions";

describe("pipeline prompt suggestions", () => {
  it("offers an uploaded-audio transcription and sentiment analysis example", () => {
    expect(CHAT_PROMPT_SUGGESTIONS[0]).toContain("audio transcription and sentiment analysis");
    expect(CHAT_PROMPT_SUGGESTIONS[0]).toContain("uploaded audio source");
  });
});
