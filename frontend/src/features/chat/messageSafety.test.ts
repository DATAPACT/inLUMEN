import { describe, expect, it } from "vitest";

import {
  INTERNAL_AGENT_MESSAGE_FALLBACK,
  containsInternalAgentData,
  sanitizeAssistantMessage,
} from "@/features/chat/messageSafety";


describe("assistant message safety", () => {
  it("blocks concatenated tool calls, results, and persisted graph records", () => {
    const leaked = [
      '{"tool":"delete_step","params":"{\\"step_uid\\":\\"secret-step\\"}"}',
      '[{"deleted_step":{"pipeline_updated_at":"2026-08-11T13:25:51Z"}}]',
      '{"tool":"overview","params":"{}"}',
      '[{"step":{"ports_json":"{...}","implementation_json":"{...}"}}]',
      "The pipeline is complete.",
    ].join("");

    expect(containsInternalAgentData(leaked)).toBe(true);
    expect(sanitizeAssistantMessage(leaked)).toBe(INTERNAL_AGENT_MESSAGE_FALLBACK);
    expect(sanitizeAssistantMessage(leaked)).not.toContain("secret-step");
  });

  it("preserves normal assistant prose and harmless JSON examples", () => {
    expect(sanitizeAssistantMessage("The pipeline is ready.")).toBe(
      "The pipeline is ready.",
    );
    expect(sanitizeAssistantMessage('Use {"threshold": 0.8}.')).toBe(
      'Use {"threshold": 0.8}.',
    );
  });
});
