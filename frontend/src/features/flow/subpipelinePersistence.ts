import { INLUMEN_API_URL } from "@/config/api";
import type { NormalizedGraph } from "@/features/flow/flowGraph";
import type {
  SubpipelineInterface,
  SubpipelineReference,
} from "@/features/flow/subpipeline";
import { apiFetch } from "@/utils/apiFetch";

export type ReusablePipelineSummary = {
  uid: string;
  name: string;
  description: string;
  interface: SubpipelineInterface;
  node_count: number;
  edge_count: number;
  unavailable_reason?: string;
  created_at?: string | null;
  updated_at?: string | null;
};

export type ReusablePipelineDefinition = {
  reference: SubpipelineReference;
  description?: string;
  interface: SubpipelineInterface;
  graph: NormalizedGraph;
  created_at?: string | null;
  updated_at?: string | null;
};

export type SubpipelineCompatibilityConflict = {
  direction: "inputs" | "outputs";
  port: string;
  reason: string;
  candidates: Array<string | { id: string; name: string; type: string }>;
};

export type ReusablePipelineAttachment = ReusablePipelineDefinition & {
  ports: { inputs: Array<Record<string, unknown>>; outputs: Array<Record<string, unknown>> };
  compatibility: {
    compatible: boolean;
    input_mapping: Record<string, string>;
    output_mapping: Record<string, string>;
    conflicts: SubpipelineCompatibilityConflict[];
  };
  attached?: boolean;
};

const responseError = async (response: Response, fallback: string) => {
  const payload = await response.json().catch(() => ({}));
  return String(payload?.error || fallback);
};

export const REUSABLE_PIPELINE_CATALOG_CHANGED_EVENT = "inlumen:reusable-pipelines-changed";

export const notifyReusablePipelineCatalogChanged = () => {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(REUSABLE_PIPELINE_CATALOG_CHANGED_EVENT));
  }
};

export const fetchReusablePipelines = async (): Promise<ReusablePipelineSummary[]> => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/reusable-pipelines`, { method: "GET" });
  if (!response.ok) throw new Error(await responseError(response, "Failed to load reusable pipelines"));
  const payload = await response.json().catch(() => ({}));
  return Array.isArray(payload?.pipelines) ? payload.pipelines : [];
};

export const fetchReusablePipeline = async (
  pipelineUid: string,
  legacyVersionUid?: string,
): Promise<ReusablePipelineDefinition> => {
  const query = new URLSearchParams({ pipeline_uid: pipelineUid });
  if (legacyVersionUid) query.set("version_uid", legacyVersionUid);
  const response = await apiFetch(
    `${INLUMEN_API_URL}/api/reusable-pipelines/definition?${query.toString()}`,
    { method: "GET" },
  );
  if (!response.ok) throw new Error(await responseError(response, "Failed to load reusable pipeline"));
  return response.json();
};

export const saveReusablePipeline = async ({
  name,
  description,
  graph,
}: {
  name: string;
  description: string;
  graph: NormalizedGraph;
}): Promise<ReusablePipelineDefinition> => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/reusable-pipelines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      description,
      graph,
    }),
  });
  if (!response.ok) throw new Error(await responseError(response, "Failed to save reusable pipeline"));
  const saved = await response.json();
  notifyReusablePipelineCatalogChanged();
  return saved;
};

export const deleteReusablePipeline = async (pipelineUid: string): Promise<void> => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/reusable-pipelines`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pipeline_uid: pipelineUid }),
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "Failed to delete reusable pipeline"));
  }
  notifyReusablePipelineCatalogChanged();
};

const reusablePipelineAttachment = async ({
  flowId,
  pipelineUid,
  dryRun,
  inputMapping,
  outputMapping,
}: {
  flowId: string;
  pipelineUid: string;
  dryRun: boolean;
  inputMapping?: Record<string, string>;
  outputMapping?: Record<string, string>;
}): Promise<ReusablePipelineAttachment> => {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/reusable-pipelines/attach`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      flow_id: flowId,
      pipeline_uid: pipelineUid,
      dry_run: dryRun,
      input_mapping: inputMapping,
      output_mapping: outputMapping,
    }),
  });
  if (!response.ok) throw new Error(await responseError(response, "Failed to attach reusable pipeline"));
  return response.json();
};

export const previewReusablePipelineAttachment = (args: {
  flowId: string;
  pipelineUid: string;
}) => reusablePipelineAttachment({ ...args, dryRun: true });

export const attachReusablePipeline = (args: {
  flowId: string;
  pipelineUid: string;
  inputMapping?: Record<string, string>;
  outputMapping?: Record<string, string>;
}) => reusablePipelineAttachment({ ...args, dryRun: false });
