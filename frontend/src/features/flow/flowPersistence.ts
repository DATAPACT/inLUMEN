import { Edge, Node } from 'reactflow';
import { apiFetch } from '@/utils/apiFetch';
import { INLUMEN_API_URL } from '@/config/api';
import {
  ChatbotConfig,
  buildCodegenLLMRequestConfig,
  buildLLMRequestConfig,
} from '@/services/chatbotService';
import {
  normalizeType,
  pickBackendUpdatableProps,
} from '@/features/nodes/nodeSchema';

export const MAIN_PIPELINE_VERSION_UID = 'main';

export type PipelineVersionGraph = {
  updated_at?: string | null;
  nodes?: Node[];
  edges?: Edge[];
  viewport?: unknown;
  [key: string]: unknown;
};

export type PipelineVersionSummary = {
  uid: string;
  name: string;
  version?: string;
  description?: string | null;
  version_index?: number;
  is_main?: boolean;
  node_count?: number;
  edge_count?: number;
  file_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
  pipeline_updated_at?: string | null;
};

export type PipelineVersionRestore = {
  version: PipelineVersionSummary;
  graph: PipelineVersionGraph;
  file_restore?: Array<Record<string, unknown>>;
};

export type PipelineVersionSetMainResult = PipelineVersionRestore & {
  source_version?: PipelineVersionSummary;
};

export type PipelineWorkspaceClearResult = {
  status?: string;
  message?: string;
  deleted_step_flow_ids?: string[];
  deleted_version_uids?: string[];
  deleted_version_count?: number;
  deleted_provenance_event_count?: number;
  provenance_cleared?: boolean;
  version: PipelineVersionSummary;
  graph: PipelineVersionGraph;
  chat_reset?: boolean;
  storage_cleanup?: Array<Record<string, unknown>>;
};

export type PipelineOverviewMetadata = {
  version?: string;
  description?: string;
  active_version_uid?: string;
  created_at?: string | null;
  updated_at?: string | null;
};

export type PipelineScriptGenerationMode = "fast" | "generic" | "full";

export type PipelineScriptGenerationOptions = {
  mode?: PipelineScriptGenerationMode;
  includeSampleData?: boolean;
  validationMode?: "static" | "unit" | "edge" | "pipeline_sample";
  generationStrategy?: "auto" | "single_pass" | "per_node";
  allowDeterministicFallback?: boolean;
  repairAttempts?: number;
  userInstruction?: string;
};

export type PipelineGenerationRunStep = {
  flow_id?: string;
  status?: string;
  stage?: string;
  attempts?: number;
  validation_report?: {
    status?: string;
    errors?: string[];
    warnings?: string[];
  } | null;
};

export type PipelineGenerationRun = {
  run_id?: string;
  status?: string;
  mode?: string;
  steps?: PipelineGenerationRunStep[];
  errors?: string[];
  warnings?: string[];
};

export type PipelineGenerationJob = {
  run_id?: string;
  status?: string;
  resumed_from_run_id?: string | null;
  resume_from_flow_id?: string | null;
  generation_run?: PipelineGenerationRun | null;
  result?: unknown;
  error?: string | null;
  mode?: string;
  data_awareness?: {
    has_sample_data?: boolean;
    sample_file_count?: number;
    sample_nodes?: Array<Record<string, unknown>>;
  };
  persistence?: {
    status?: string;
    reason?: string;
    result?: unknown;
    warning?: string;
  };
};

export type ExternalRuntimePrompt = {
  prompt: string;
  filename?: string;
  node_count?: number;
  input_policy?: "user_supplied" | string;
};

export const addNodeToBackend = async (node: Node) => {
  try {
    const nodeType = normalizeType(node.data?.type);
    const response = await apiFetch(`${INLUMEN_API_URL}/api/graph/nodes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          ...pickBackendUpdatableProps(node.id, node.data ?? {}, nodeType),
          x: node.position?.x ?? 0,
          y: node.position?.y ?? 0,
        },
      }),
    });

    if (!response.ok) throw new Error('Failed to add node');
    const result = await response.json();
    console.log("[flowPersistence.ts] Graph add_node:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] Graph add node error:", err);
  }
};

export const updateNodePositionInBackend = async (node: Node) => {
  try {
    await apiFetch(`${INLUMEN_API_URL}/api/graph/nodes/position`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        flow_id: node.id,
        x: node.position.x,
        y: node.position.y,
      }),
    });
  } catch (e) {
    console.warn("[flowPersistence.ts] Failed to update node position:", e);
  }
};

export const addEdgeToBackend = async (sourceNode: Node, targetNode: Node) => {
  try {
    const response = await apiFetch(`${INLUMEN_API_URL}/api/graph/edges`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id_source: sourceNode.id,
          flow_id_target: targetNode.id,
        },
      }),
    });

    if (!response.ok) throw new Error('Failed to add edge');
    const result = await response.json();
    console.log("[flowPersistence.ts] Graph adding edge:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] Graph adding edge error:", err);
  }
};

export const deleteEdgeFromBackend = async (sourceNode: Node, targetNode: Node) => {
  try {
    const response = await apiFetch(`${INLUMEN_API_URL}/api/graph/edges`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id_source: sourceNode.id,
          flow_id_target: targetNode.id,
        },
      }),
    });

    if (!response.ok) throw new Error('Failed to delete edge');
    const result = await response.json();
    console.log("[flowPersistence.ts] Graph deleting edge:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] Graph delete edge error:", err);
  }
};

export const deleteNodeFromBackend = async (nodeId: string) => {
  try {
    const response = await apiFetch(
      `${INLUMEN_API_URL}/api/graph/nodes/${encodeURIComponent(nodeId)}`,
      { method: 'DELETE' },
    );
    if (!response.ok) throw new Error('Failed to delete node');
    const result = await response.json();
    console.log("[flowPersistence.ts] Graph delete_node:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] deleteNodeFromBackend error:", err);
  }
};

export const clearBackendGraph = async () => {
  try {
    const response = await apiFetch(`${INLUMEN_API_URL}/api/graph/nodes`, {
      method: 'DELETE',
    });
    if (!response.ok) throw new Error('Failed to clear graph');
    const result = await response.json();
    console.log("Backend graph cleared:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] clearBackendGraph error:", err);
  }
};

export const fetchPipelineUpdatedAt = async (): Promise<string | null> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/updated-at`, { method: "GET" });
  if (!res.ok) throw new Error("Failed to fetch pipeline updated_at");
  const data = await res.json();
  return data?.updated_at ?? null;
};

export const fetchPipelineGraph = async () => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/graph`, { method: "GET" });
  if (!res.ok) throw new Error("Failed to fetch pipeline graph");
  return res.json();
};

export const fetchPipelineVersions = async (): Promise<PipelineVersionSummary[]> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/versions`, { method: "GET" });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to fetch pipeline versions (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  return Array.isArray(data?.versions) ? data.versions : [];
};

export const savePipelineVersion = async (
  name: string,
  graph: PipelineVersionGraph,
): Promise<PipelineVersionSummary> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, graph }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to save pipeline version (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  if (!data?.version?.uid) {
    throw new Error("Saved version response did not include a version id.");
  }
  return data.version;
};

export const savePipelineMain = async (
  graph: PipelineVersionGraph,
): Promise<PipelineVersionSummary> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/versions/main`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to save main pipeline version (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  if (!data?.version?.uid) {
    throw new Error("Main version response did not include a version id.");
  }
  return data.version;
};

export const savePipelineActiveVersion = async (
  graph: PipelineVersionGraph,
  versionUid: string,
  versionName: string,
): Promise<PipelineVersionSummary> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/versions/active`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph, uid: versionUid, name: versionName }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to save active pipeline version (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  if (!data?.version?.uid) {
    throw new Error("Active version response did not include a version id.");
  }
  return data.version;
};

export const updatePipelineOverviewMetadata = async (
  metadata: {
    version: string;
    description: string;
    activeVersionUid?: string;
  },
): Promise<PipelineOverviewMetadata> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/overview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      version: metadata.version,
      description: metadata.description,
      active_version_uid: metadata.activeVersionUid,
    }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to update pipeline overview (${res.status}): ${txt}`);
  }
  return res.json().catch(() => ({}));
};

export const restorePipelineVersion = async (
  versionUid: string,
): Promise<PipelineVersionRestore> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/versions/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uid: versionUid }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to restore pipeline version (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  if (!data?.version?.uid || !data?.graph) {
    throw new Error("Restore response did not include a version graph.");
  }
  return data as PipelineVersionRestore;
};

export const setPipelineVersionAsMain = async (
  versionUid: string,
): Promise<PipelineVersionSetMainResult> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/versions/set-main`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uid: versionUid }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to set pipeline version as Main (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  if (!data?.version?.uid || !data?.graph) {
    throw new Error("Set Main response did not include a version graph.");
  }
  return data as PipelineVersionSetMainResult;
};

export const deletePipelineVersion = async (
  versionUid: string,
): Promise<{ deleted_uid?: string; remaining_count?: number; pipeline_updated_at?: string | null }> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/versions`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uid: versionUid }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to delete pipeline version (${res.status}): ${txt}`);
  }
  return res.json().catch(() => ({}));
};

export const clearPipelineWorkspace = async (
  chatSessionId?: string | null,
): Promise<PipelineWorkspaceClearResult> => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/workspace/clear-all`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: chatSessionId || null }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to clear workspace (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  if (!data?.version?.uid || !data?.graph) {
    throw new Error("Clear all response did not include the Main graph.");
  }
  return data as PipelineWorkspaceClearResult;
};

export const fetchProvenanceReport = async (
  versionUid?: string | null,
): Promise<Blob> => {
  const params = new URLSearchParams();
  if (versionUid) params.set("version_uid", versionUid);
  const query = params.toString();
  const res = await apiFetch(`${INLUMEN_API_URL}/api/provenance/report${query ? `?${query}` : ""}`, {
    method: "GET",
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to generate provenance report (${res.status}): ${txt}`);
  }
  return res.blob();
};

export const fetchProvenanceProvO = async (
  versionUid?: string | null,
): Promise<Blob> => {
  const params = new URLSearchParams();
  if (versionUid) params.set("version_uid", versionUid);
  const query = params.toString();
  const res = await apiFetch(`${INLUMEN_API_URL}/api/provenance/prov-o${query ? `?${query}` : ""}`, {
    method: "GET",
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to generate PROV-O provenance (${res.status}): ${txt}`);
  }
  return res.blob();
};

export const restoreBackendGraphHistory = async (
  graph: PipelineVersionGraph,
  direction: "undo" | "redo",
  details: Record<string, unknown>,
) => {
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/history/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph, direction, details }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to restore graph history (${res.status}): ${txt}`);
  }
  return res.json().catch(() => ({}));
};

export const rebuildBackendFromFlow = async (
  nodes: Node[],
  edges: Edge[],
  settings?: Record<string, unknown>,
) => {
  try {
    const response = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/graph`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        graph: {
          nodes,
          edges,
          ...(settings && Object.keys(settings).length > 0 ? { settings } : {}),
        },
      }),
    });
    if (response.ok) {
      const result = await response.json().catch(() => ({}));
      console.log("[flowPersistence.ts] Graph sync:", result);
      return;
    }
    const errText = await response.text().catch(() => "");
    console.warn(
      `[flowPersistence.ts] Whole graph sync unavailable (${response.status}): ${errText}`,
    );
  } catch (err) {
    console.warn("[flowPersistence.ts] Whole graph sync failed, falling back:", err);
  }

  await clearBackendGraph();

  for (const node of nodes) {
    await addNodeToBackend(node);
  }

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  for (const edge of edges) {
    const sourceNode = nodeById.get(edge.source);
    const targetNode = nodeById.get(edge.target);
    if (!sourceNode || !targetNode) {
      console.warn(
        `[flowPersistence.ts] Skipping edge; missing source/target node for edge id=${edge.id}`,
        edge,
      );
      continue;
    }
    await addEdgeToBackend(sourceNode, targetNode);
  }
};

export const generatePipelineYaml = async (activeChatbotConfig?: ChatbotConfig) => {
  const filesRes = await apiFetch(`${INLUMEN_API_URL}/api/files`, {
    method: "GET",
  });

  if (!filesRes.ok) {
    const errText = await filesRes.text().catch(() => "");
    throw new Error(
      `Failed to fetch files: ${filesRes.status} ${filesRes.statusText} ${errText}`,
    );
  }

  const files = await filesRes.json();
  console.log("[flowPersistence.ts] Fetched filenames.");

  const llm_config = activeChatbotConfig
    ? buildLLMRequestConfig(activeChatbotConfig)
    : undefined;

  const response = await apiFetch(
    `${INLUMEN_API_URL}/agentic_generate_dockerfiles`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        files,
        llm_config,
      }),
    },
  );

  if (!response.ok) {
    const errText = await response.text().catch(() => "");
    throw new Error(
      `Failed: ${response.status} ${response.statusText} ${errText}`,
    );
  }

  const dockerfiles_json = await response.json();
  console.log("[flowPersistence.ts] Agents generated Dockerfile(s):", dockerfiles_json);

  const responseYAML = await apiFetch(
    `${INLUMEN_API_URL}/agentic_generate_yaml`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        dockerfiles_json,
        llm_config,
      }),
    },
  );

  if (!responseYAML.ok) {
    const errText = await responseYAML.text().catch(() => "");
    throw new Error(
      `Failed: ${responseYAML.status} ${responseYAML.statusText} ${errText}`,
    );
  }

  return responseYAML.text();
};

const buildPipelineGenerationPayload = (
  activeChatbotConfig?: ChatbotConfig,
  options: PipelineScriptGenerationOptions = {},
) => {
  let llm_config: ReturnType<typeof buildLLMRequestConfig> | undefined;
  if (activeChatbotConfig) {
    llm_config = buildCodegenLLMRequestConfig(activeChatbotConfig);
  }
  return {
    include_sample_data: options.includeSampleData ?? true,
    validation_mode: options.validationMode ?? "pipeline_sample",
    generation_strategy: options.generationStrategy ?? "auto",
    generation_mode: options.mode ?? "full",
    allow_deterministic_fallback: options.allowDeterministicFallback ?? false,
    repair_attempts: options.repairAttempts ?? 2,
    high_level_prompt: options.userInstruction?.trim() || "",
    ...(llm_config ? { llm_config } : {}),
  };
};

export const prepareExternalRuntimePrompt = async (
  highLevelPrompt = "",
): Promise<ExternalRuntimePrompt> => {
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/pipeline/external-runtime-prompt`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        high_level_prompt: highLevelPrompt.trim(),
      }),
    },
  );
  if (!response.ok) {
    const errorPayload = await response.json().catch(async () => ({
      error: await response.text().catch(() => ""),
    }));
    throw new Error(
      formatPipelineGenerationError(
        errorPayload,
        response.status,
        response.statusText,
      ),
    );
  }
  return response.json();
};

export const generatePipelineScripts = async (
  activeChatbotConfig?: ChatbotConfig,
  options: PipelineScriptGenerationOptions = {},
) => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/generate-scripts`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildPipelineGenerationPayload(activeChatbotConfig, options)),
  });
  if (!response.ok) {
    const errorPayload = await response.json().catch(async () => ({
      error: await response.text().catch(() => ""),
    }));
    const message = formatPipelineGenerationError(
      errorPayload,
      response.status,
      response.statusText,
    );
    const error = new Error(message);
    (error as Error & { payload?: unknown }).payload = errorPayload;
    throw error;
  }
  return response.json();
};

export const startPipelineScriptGenerationRun = async (
  activeChatbotConfig?: ChatbotConfig,
  options: PipelineScriptGenerationOptions = {},
): Promise<PipelineGenerationJob> => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/generation-runs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildPipelineGenerationPayload(activeChatbotConfig, options)),
  });
  if (!response.ok) {
    const errorPayload = await response.json().catch(async () => ({
      error: await response.text().catch(() => ""),
    }));
    const message = formatPipelineGenerationError(
      errorPayload,
      response.status,
      response.statusText,
    );
    const error = new Error(message);
    (error as Error & { payload?: unknown }).payload = errorPayload;
    throw error;
  }
  return response.json();
};

export const fetchPipelineScriptGenerationRun = async (
  runId: string,
): Promise<PipelineGenerationJob> => {
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/pipeline/generation-runs/${encodeURIComponent(runId)}`,
    { method: "GET" },
  );
  if (!response.ok) {
    const errorPayload = await response.json().catch(async () => ({
      error: await response.text().catch(() => ""),
    }));
    const message = formatPipelineGenerationError(
      errorPayload,
      response.status,
      response.statusText,
    );
    const error = new Error(message);
    (error as Error & { payload?: unknown }).payload = errorPayload;
    throw error;
  }
  return response.json();
};

export const resumePipelineScriptGenerationRun = async (
  runId: string,
  activeChatbotConfig?: ChatbotConfig,
  options: {
    flowId?: string;
    repairAttempts?: number;
    userInstruction?: string;
  } = {},
): Promise<PipelineGenerationJob> => {
  const llm_config = activeChatbotConfig
    ? buildCodegenLLMRequestConfig(activeChatbotConfig)
    : undefined;
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/pipeline/generation-runs/${encodeURIComponent(runId)}/resume`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        flow_id: options.flowId || "",
        repair_attempts: options.repairAttempts ?? 4,
        user_instruction: options.userInstruction || "",
        ...(llm_config ? { llm_config } : {}),
      }),
    },
  );
  if (!response.ok) {
    const errorPayload = await response.json().catch(async () => ({
      error: await response.text().catch(() => ""),
    }));
    const message = formatPipelineGenerationError(
      errorPayload,
      response.status,
      response.statusText,
    );
    const error = new Error(message);
    (error as Error & { payload?: unknown }).payload = errorPayload;
    throw error;
  }
  return response.json();
};

const formatPipelineGenerationError = (
  payload: unknown,
  status: number,
  statusText: string,
) => {
  if (!payload || typeof payload !== "object") {
    return `Failed to generate pipeline scripts: ${status} ${statusText}`;
  }
  const body = payload as {
    error?: unknown;
    details?: {
      invalid_nodes?: Array<{
        flow_id?: unknown;
        validation_report?: {
          errors?: unknown;
          warnings?: unknown;
        };
      }>;
      integration_validation?: {
        errors?: unknown;
        warnings?: unknown;
      };
      generation_run?: {
        run_id?: unknown;
        steps?: Array<{
          flow_id?: unknown;
          stage?: unknown;
          status?: unknown;
          validation_report?: {
            errors?: unknown;
          } | null;
        }>;
      };
    };
  };
  const lines: string[] = [
    typeof body.error === "string"
      ? body.error
      : `Failed to generate pipeline scripts: ${status} ${statusText}`,
  ];
  const invalidNodes = Array.isArray(body.details?.invalid_nodes)
    ? body.details.invalid_nodes
    : [];
  invalidNodes.slice(0, 4).forEach((node) => {
    const errors = Array.isArray(node.validation_report?.errors)
      ? node.validation_report.errors
      : [];
    const firstError = errors.find((item) => typeof item === "string");
    if (firstError) {
      lines.push(`Node ${String(node.flow_id || "?")}: ${firstError}`);
    }
  });
  const integrationErrors = Array.isArray(body.details?.integration_validation?.errors)
    ? body.details?.integration_validation?.errors
    : [];
  const firstIntegrationError = integrationErrors.find((item) => typeof item === "string");
  if (firstIntegrationError && lines.length < 5) {
    lines.push(String(firstIntegrationError));
  }
  const runId = body.details?.generation_run?.run_id;
  if (typeof runId === "string" && runId.trim()) {
    lines.push(`Generation run: ${runId.trim()}`);
  }
  const failedStep = Array.isArray(body.details?.generation_run?.steps)
    ? body.details.generation_run.steps.find((step) => step.status !== "valid")
    : undefined;
  if (failedStep && lines.length < 7) {
    const stepErrors = Array.isArray(failedStep.validation_report?.errors)
      ? failedStep.validation_report?.errors
      : [];
    const firstStepError = stepErrors.find((item) => typeof item === "string");
    const stage = typeof failedStep.stage === "string" ? failedStep.stage : "unknown";
    lines.push(
      `Run step ${String(failedStep.flow_id || "?")} (${stage}): ${
        firstStepError || String(failedStep.status || "invalid")
      }`,
    );
  }
  return lines.join("\n");
};

export const uploadPipelineSourceFile = async (file: File): Promise<void> => {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/source`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    console.warn(
      `[flowPersistence.ts] Pipeline source upload not available (${res.status}): ${txt}`,
    );
    return;
  }
  console.log("[flowPersistence.ts] Pipeline source saved to MinIO:", file.name);
};
