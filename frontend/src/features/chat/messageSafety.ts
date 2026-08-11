const INTERNAL_AGENT_PATTERNS = [
  /(?:^|[\s`])(?:call\s*:\s*)?(?:create|configure|connect|disconnect|insert|delete|list|get|inspect|overview)_[a-z0-9_]+\s*\(/i,
  /\{\s*["']tool["']\s*:\s*["'](?:create|configure|connect|disconnect|insert|delete|list|get|inspect|overview)_[a-z0-9_]+["']\s*,\s*["']params["']\s*:/i,
  /["'](?:name|tool_name)["']\s*:\s*["'](?:create|configure|connect|disconnect|insert|delete|list|get|inspect|overview)_[a-z0-9_]+["']\s*,\s*["'](?:arguments|params)["']\s*:/i,
  /<(?:tool_call|function_call)\b/i,
  /["'](?:implementation_json|param_json|ports_json|secret_params_json|pipeline_updated_at|step_link|files_linked_to_step)["']\s*:/i,
  /\[\s*\{\s*["'](?:connection|deleted_step|disconnected|flow_step|reusable_pipeline|subpipeline_step)["']\s*:/i,
];

export const INTERNAL_AGENT_MESSAGE_FALLBACK =
  "I couldn't display that response because it contained internal operation data.";

export const containsInternalAgentData = (value: unknown): boolean => {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) return false;
  return INTERNAL_AGENT_PATTERNS.some((pattern) => pattern.test(text));
};

export const sanitizeAssistantMessage = (value: unknown): string => {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text || containsInternalAgentData(text)) {
    return INTERNAL_AGENT_MESSAGE_FALLBACK;
  }
  return text;
};
