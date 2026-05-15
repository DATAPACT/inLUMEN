import { Edge, Node } from 'reactflow';
import { apiFetch } from '@/utils/apiFetch';
import { MINIO_API_URL, NEO4J_API_URL, LLM_API_URL } from '@/config/api';
import { ChatbotConfig, buildLLMRequestConfig } from '@/services/chatbotService';

export type PipelineVersionSummary = {
  uid: string;
  name: string;
  version?: string;
  version_index?: number;
  node_count?: number;
  edge_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
  pipeline_updated_at?: string | null;
};

export type PipelineVersionRestore = {
  version: PipelineVersionSummary;
  graph: {
    updated_at?: string | null;
    nodes?: Node[];
    edges?: Edge[];
    viewport?: unknown;
    [key: string]: unknown;
  };
};

export const addNodeToNeo4j = async (node: Node) => {
  try {
    const response = await apiFetch(`${NEO4J_API_URL}/neo4j_add_node`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id: node.id,
          label: node.data.label,
          type: node.data?.type,
          description: node.data?.description || "",
          x: node.position?.x ?? 0,
          y: node.position?.y ?? 0,
        },
      }),
    });

    if (!response.ok) throw new Error('Failed to add node to Neo4j');
    const result = await response.json();
    console.log("[flowPersistence.ts] Neo4j add_node:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] Neo4j add node error:", err);
  }
};

export const updateNodePositionInNeo4j = async (node: Node) => {
  try {
    await apiFetch(`${NEO4J_API_URL}/neo4j_update_node_position`, {
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

export const addEdgeToNeo4j = async (sourceNode: Node, targetNode: Node) => {
  try {
    const response = await apiFetch(`${NEO4J_API_URL}/neo4j_add_edge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id_source: sourceNode.id,
          flow_id_target: targetNode.id,
        },
      }),
    });

    if (!response.ok) throw new Error('Failed to add edge to Neo4j');
    const result = await response.json();
    console.log("[flowPersistence.ts] Neo4j adding edge:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] Neo4j adding edge error:", err);
  }
};

export const deleteEdgeToNeo4j = async (sourceNode: Node, targetNode: Node) => {
  try {
    const response = await apiFetch(`${NEO4J_API_URL}/neo4j_delete_edge`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id_source: sourceNode.id,
          flow_id_target: targetNode.id,
        },
      }),
    });

    if (!response.ok) throw new Error('Failed to delete edge to Neo4j');
    const result = await response.json();
    console.log("[flowPersistence.ts] Neo4j deleting edge:", result);
  } catch (err) {
    console.error("[flowPersistence.ts] Neo4j delete edge error:", err);
  }
};

export const deleteNodeFromNeo4jAndMinIO = async (nodeId: string) => {
  try {
    const response = await apiFetch(
      `${NEO4J_API_URL}/neo4j_delete_node/${nodeId}`,
      { method: 'DELETE' },
    );
    if (!response.ok) throw new Error('Failed to delete node from Neo4j');
    const result = await response.json();
    console.log("[flowPersistence.ts] Neo4j delete_node:", result);

    try {
      const minioResponse = await apiFetch(
        `${MINIO_API_URL}/minio_clear_bucket?bucket_id=${nodeId}`,
        { method: 'DELETE' },
      );
      if (!minioResponse.ok) throw new Error('Failed to clear MinIO bucket');
      const minioResult = await minioResponse.json().catch(() => null);
      console.log(
        `[flowPersistence.ts] MinIO bucket cleared for nodeId=${nodeId}`,
        minioResult,
      );
    } catch (minioErr) {
      console.warn(
        `[flowPersistence.ts] Neo4j node deleted, but MinIO cleanup failed for nodeId=${nodeId}`,
        minioErr,
      );
    }
  } catch (err) {
    console.error("[flowPersistence.ts] deleteNodeFromNeo4jAndMinIO error:", err);
  }
};

export const clearNeo4jAndMinIO = async () => {
  try {
    const neoResponse = await apiFetch(`${NEO4J_API_URL}/neo4j_clear_nodes`, {
      method: 'DELETE',
    });
    if (!neoResponse.ok) throw new Error('Failed to clear Neo4j');
    const result = await neoResponse.json();
    const ids: string[] = result?.deleted_step_flow_ids ?? [];
    console.log("Neo4j cleared:", result);

    for (const id of ids) {
      try {
        const minioResponse = await apiFetch(
          `${MINIO_API_URL}/minio_clear_bucket?bucket_id=${id}`,
          { method: 'DELETE' },
        );
        if (!minioResponse.ok) {
          const txt = await minioResponse.text().catch(() => "");
          throw new Error(`MinIO clear failed (${minioResponse.status}): ${txt}`);
        }
        const minioResult = await minioResponse.json().catch(() => null);
        console.log(`[flowPersistence.ts] MinIO bucket cleared for flow_id=${id}`, minioResult);
      } catch (minioErr) {
        console.warn(`[flowPersistence.ts] Failed to clear MinIO bucket for flow_id=${id}`, minioErr);
      }
    }
  } catch (err) {
    console.error("[flowPersistence.ts] clearNeo4jAndMinIO error:", err);
  }
};

export const fetchPipelineUpdatedAt = async (): Promise<string | null> => {
  const res = await apiFetch(`${NEO4J_API_URL}/neo4j_get_pipeline_updated_at`, { method: "GET" });
  if (!res.ok) throw new Error("Failed to fetch pipeline updated_at");
  const data = await res.json();
  return data?.updated_at ?? null;
};

export const fetchPipelineGraph = async () => {
  const res = await apiFetch(`${NEO4J_API_URL}/neo4j_get_graph`, { method: "GET" });
  if (!res.ok) throw new Error("Failed to fetch neo4j_get_graph");
  return res.json();
};

export const fetchPipelineVersions = async (): Promise<PipelineVersionSummary[]> => {
  const res = await apiFetch(`${NEO4J_API_URL}/neo4j_list_pipeline_versions`, { method: "GET" });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Failed to fetch pipeline versions (${res.status}): ${txt}`);
  }
  const data = await res.json().catch(() => ({}));
  return Array.isArray(data?.versions) ? data.versions : [];
};

export const savePipelineVersion = async (
  name: string,
  graph: PipelineVersionRestore["graph"],
): Promise<PipelineVersionSummary> => {
  const res = await apiFetch(`${NEO4J_API_URL}/neo4j_save_pipeline_version`, {
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

export const restorePipelineVersion = async (
  versionUid: string,
): Promise<PipelineVersionRestore> => {
  const res = await apiFetch(`${NEO4J_API_URL}/neo4j_restore_pipeline_version`, {
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

export const deletePipelineVersion = async (
  versionUid: string,
): Promise<{ deleted_uid?: string; remaining_count?: number; pipeline_updated_at?: string | null }> => {
  const res = await apiFetch(`${NEO4J_API_URL}/neo4j_delete_pipeline_version`, {
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

export const rebuildBackendFromFlow = async (nodes: Node[], edges: Edge[]) => {
  await clearNeo4jAndMinIO();

  for (const node of nodes) {
    await addNodeToNeo4j(node);
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
    await addEdgeToNeo4j(sourceNode, targetNode);
  }
};

export const generatePipelineYaml = async (activeChatbotConfig?: ChatbotConfig) => {
  const filesRes = await apiFetch(`${NEO4J_API_URL}/neo4j_get_all_files`, {
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
    `${LLM_API_URL}/agentic_generate_dockerfiles`,
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
    `${LLM_API_URL}/agentic_generate_yaml`,
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
