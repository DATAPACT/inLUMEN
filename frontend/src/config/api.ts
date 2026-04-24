const browserHost = window.location.hostname || "localhost";

const minioApiPort = (import.meta.env.VITE_MINIO_API_PORT as string) || "5003";
const neo4jApiPort = (import.meta.env.VITE_NEO4J_API_PORT as string) || "5001";
const llmApiPort = (import.meta.env.VITE_LLM_API_PORT as string) || "5002";

export const MINIO_API_URL =
  (import.meta.env.VITE_MINIO_API_URL as string) || `http://${browserHost}:${minioApiPort}`;
export const NEO4J_API_URL =
  (import.meta.env.VITE_NEO4J_API_URL as string) || `http://${browserHost}:${neo4jApiPort}`;
export const LLM_API_URL =
  (import.meta.env.VITE_LLM_API_URL as string) || `http://${browserHost}:${llmApiPort}`;
