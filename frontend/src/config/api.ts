const browserHost = window.location.hostname || "localhost";

const inlumenApiPort = (import.meta.env.VITE_INLUMEN_API_PORT as string) || "5000";

export const INLUMEN_API_URL =
  (import.meta.env.VITE_INLUMEN_API_URL as string) || `http://${browserHost}:${inlumenApiPort}`;
