export type DeploymentBundleFile = {
  path?: string;
  filename?: string;
  flow_id?: string;
  content?: string;
  content_encoding?: "utf-8" | "base64" | string;
  content_type?: string;
  size_bytes?: number;
  sha256?: string;
  role?: string;
  encoding?: "base64";
  executable?: boolean;
};

const sanitizeZipSegment = (value: unknown, fallback: string) => {
  const cleaned = String(value || "")
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "");
  return cleaned || fallback;
};

export const safeZipPath = (file: DeploymentBundleFile, index: number) => {
  const rawPath = String(file.path || "").trim();
  if (rawPath) {
    const parts = rawPath.replace(/\\/g, "/")
      .split("/")
      .map((part) => part.trim())
      .filter((part) => part && part !== ".");
    if (parts.length > 0 && !rawPath.startsWith("/") && !parts.includes("..")) {
      return parts.join("/");
    }
    const sanitized = parts
      .map((part) => sanitizeZipSegment(part, "file"))
      .filter((part) => part && part !== "." && part !== "..");
    if (sanitized.length > 0) return sanitized.join("/");
  }
  const flowId = sanitizeZipSegment(file.flow_id, "node");
  const filename = sanitizeZipSegment(file.filename, `artifact-${index + 1}.txt`);
  return `nodes/${flowId}/${filename}`;
};

// Scripts keep their executable bit, so `./argo/submit.sh` works after unzip.
const EXECUTABLE_MODE = 0o755;
const FILE_MODE = 0o644;

export const buildDeploymentZip = async (files: DeploymentBundleFile[]) => {
  const { default: JSZip } = await import("jszip");
  const zip = new JSZip();
  const written = new Set<string>();

  files.forEach((file, index) => {
    const path = safeZipPath(file, index);
    let nextPath = path;
    let suffix = 2;
    while (written.has(nextPath)) {
      const dot = path.lastIndexOf(".");
      nextPath = dot > 0
        ? `${path.slice(0, dot)}-${suffix}${path.slice(dot)}`
        : `${path}-${suffix}`;
      suffix += 1;
    }
    written.add(nextPath);
    zip.file(nextPath, file.content ?? "", {
      base64: (file.content_encoding ?? file.encoding) === "base64",
      unixPermissions: file.executable ? EXECUTABLE_MODE : FILE_MODE,
    });
  });

  return zip.generateAsync({
    type: "blob",
    compression: "DEFLATE",
    platform: "UNIX",
  });
};
