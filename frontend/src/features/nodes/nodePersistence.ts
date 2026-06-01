import { apiFetch } from '@/utils/apiFetch';
import { INLUMEN_API_URL } from '@/config/api';
import {
  getNodeFileBucket,
  getNodeFileName,
  NodeFileReference,
} from '@/features/nodes/nodeSchema';

export const updateNodePropertiesInBackend = async (
  nodeId: string,
  properties: Record<string, unknown>,
) => {
  try {
    const response = await apiFetch(`${INLUMEN_API_URL}/api/graph/nodes/properties`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        flow_id: nodeId,
        properties,
      }),
    });

    if (!response.ok) throw new Error('Failed to update node');
    const result = await response.json();
    console.log("[nodePersistence.ts] Backend update_node:", result);
  } catch (err) {
    console.error("[nodePersistence.ts] Backend update node error:", err);
  }
};

export const uploadNodeFile = async (nodeId: string, file: File) => {
  try {
    const form = new FormData();
    form.append("file", file);
    const res = await apiFetch(`${INLUMEN_API_URL}/api/nodes/${encodeURIComponent(nodeId)}/files`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`File upload failed (${res.status}): ${txt}`);
    }
    const json = await res.json().catch(() => null);
    console.log("[nodePersistence.ts] File upload ok:", {
      nodeId,
      fileName: file.name,
      response: json,
    });
    return json;
  } catch (err) {
    console.error("[nodePersistence.ts] File upload error:", err);
    throw err;
  }
};

export const removeNodeFile = async (nodeId: string, file: NodeFileReference) => {
  const fileName = getNodeFileName(file);
  if (!fileName) {
    throw new Error("Cannot remove a file without a filename.");
  }

  try {
    const res = await apiFetch(`${INLUMEN_API_URL}/api/nodes/${encodeURIComponent(nodeId)}/files`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: fileName }),
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`File removal failed (${res.status}): ${txt}`);
    }
    const json = await res.json().catch(() => null);
    console.log("[nodePersistence.ts] File removal ok:", {
      nodeId,
      fileName,
      response: json,
    });
    return json;
  } catch (err) {
    console.error("[nodePersistence.ts] File removal error:", err);
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
    container_id: bucketId,
    filename: fileName,
  });
  const res = await apiFetch(`${INLUMEN_API_URL}/api/files/content?${params.toString()}`, {
    method: "GET",
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`File read failed (${res.status}): ${txt}`);
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
  const res = await apiFetch(`${INLUMEN_API_URL}/api/nodes/${encodeURIComponent(nodeId)}/files/text`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      container_id: bucketId,
      filename: fileName,
      content,
    }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`File text update failed (${res.status}): ${txt}`);
  }
  return res.json().catch(() => null);
};
