const browserHost = window.location.hostname || "localhost";

const inlumenApiPort = (import.meta.env.VITE_INLUMEN_API_PORT as string) || "5000";

const normalizeApiUrl = (url: string): string => {
  const trimmedUrl = url.trim().replace(/\/$/, "");
  if (!trimmedUrl) {
    return "";
  }
  if (trimmedUrl.startsWith("//")) {
    return `${window.location.protocol}${trimmedUrl}`;
  }
  if (/^https?:\/\//i.test(trimmedUrl)) {
    return trimmedUrl;
  }
  return `${window.location.protocol}//${trimmedUrl}`;
};

export const INLUMEN_API_URL =
  normalizeApiUrl((import.meta.env.VITE_INLUMEN_API_URL as string) || "") ||
  `${window.location.protocol}//${browserHost}:${inlumenApiPort}`;
