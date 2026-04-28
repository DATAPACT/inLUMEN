import { apiFetch } from '@/utils/apiFetch';
import { MINIO_API_URL, NEO4J_API_URL } from '@/config/api';

export const updateNodePropertiesInNeo4j = async (
  nodeId: string,
  properties: Record<string, unknown>,
) => {
  try {
    const response = await apiFetch(`${NEO4J_API_URL}/neo4j_update_node`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        flow_id: nodeId,
        properties,
      }),
    });

    if (!response.ok) throw new Error('Failed to update node in Neo4j');
    const result = await response.json();
    console.log("[nodePersistence.ts] Neo4j update_node:", result);
  } catch (err) {
    console.error("[nodePersistence.ts] Neo4j update node error:", err);
  }
};

export const uploadNodeFile = async (nodeId: string, file: File) => {
  try {
    const form = new FormData();
    form.append("file", file);
    form.append("bucket_id", nodeId);
    const res = await apiFetch(`${MINIO_API_URL}/minio_upload_file`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`MinIO upload failed (${res.status}): ${txt}`);
    }
    const json = await res.json().catch(() => null);
    console.log("[nodePersistence.ts] MinIO upload ok:", {
      nodeId,
      fileName: file.name,
      response: json,
    });

    const neoRes = await apiFetch(`${NEO4J_API_URL}/neo4j_add_file`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        properties: {
          flow_id: nodeId,
          filename: file.name,
        },
      }),
    });

    if (!neoRes.ok) {
      const txt = await neoRes.text().catch(() => "");
      throw new Error(`Neo4j add file failed (${neoRes.status}): ${txt}`);
    }
    const neoJson = await neoRes.json().catch(() => null);
    console.log("[nodePersistence.ts] Neo4j add_file ok:", neoJson);
    return { minio: json, neo4j: neoJson };
  } catch (err) {
    console.error("[nodePersistence.ts] MinIO upload error:", err);
    throw err;
  }
};

export const removeNodeFile = async (nodeId: string, file: File) => {
  try {
    const form = new FormData();
    form.append("filename", file.name);
    form.append("bucket_id", nodeId);
    const res = await apiFetch(`${MINIO_API_URL}/minio_remove_file`, {
      method: "DELETE",
      body: form,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`MinIO removal failed (${res.status}): ${txt}`);
    }
    const json = await res.json().catch(() => null);
    console.log("[nodePersistence.ts] MinIO removal ok:", {
      nodeId,
      fileName: file.name,
      response: json,
    });

    const neoRes = await apiFetch(`${NEO4J_API_URL}/neo4j_delete_file`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        properties: {
          flow_id: nodeId,
          filename: file.name,
        },
      }),
    });
    if (!neoRes.ok) {
      const txt = await neoRes.text().catch(() => "");
      throw new Error(`Neo4j delete file failed (${neoRes.status}): ${txt}`);
    }
    const neoJson = await neoRes.json().catch(() => null);
    console.log("[nodePersistence.ts] Neo4j delete_file ok:", neoJson);
    return { minio: json, neo4j: neoJson };
  } catch (err) {
    console.error("[nodePersistence.ts] MinIO removal error:", err);
    throw err;
  }
};
