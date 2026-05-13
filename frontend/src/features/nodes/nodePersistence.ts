import { apiFetch } from '@/utils/apiFetch';
import { MINIO_API_URL, NEO4J_API_URL } from '@/config/api';
import {
  getNodeFileBucket,
  getNodeFileName,
  NodeFileReference,
} from '@/features/nodes/nodeSchema';

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

export const removeNodeFile = async (nodeId: string, file: NodeFileReference) => {
  const fileName = getNodeFileName(file);
  if (!fileName) {
    throw new Error("Cannot remove a file without a filename.");
  }

  try {
    const form = new FormData();
    form.append("filename", fileName);
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
      fileName,
      response: json,
    });

    const neoRes = await apiFetch(`${NEO4J_API_URL}/neo4j_delete_file`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        properties: {
          flow_id: nodeId,
          filename: fileName,
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

export const readNodeFile = async (nodeId: string, file: NodeFileReference) => {
  const fileName = getNodeFileName(file);
  if (!fileName) {
    throw new Error("Cannot read a file without a filename.");
  }

  const bucket = getNodeFileBucket(file, nodeId);
  const bucketId = bucket.replace(/^files-step-id-/i, "");
  const params = new URLSearchParams({
    bucket_id: bucketId,
    filename: fileName,
  });
  const res = await apiFetch(`${MINIO_API_URL}/minio_read_file?${params.toString()}`, {
    method: "GET",
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`MinIO read failed (${res.status}): ${txt}`);
  }
  return res;
};

export const updateNodeTextFile = async (
  nodeId: string,
  file: NodeFileReference,
  content: string,
) => {
  const fileName = getNodeFileName(file);
  if (!fileName) {
    throw new Error("Cannot update a file without a filename.");
  }

  const bucket = getNodeFileBucket(file, nodeId);
  const bucketId = bucket.replace(/^files-step-id-/i, "");
  const res = await apiFetch(`${MINIO_API_URL}/minio_update_text_file`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      bucket_id: bucketId,
      filename: fileName,
      content,
    }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`MinIO text update failed (${res.status}): ${txt}`);
  }
  return res.json().catch(() => null);
};
