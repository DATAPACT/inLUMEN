import React, { useEffect, useState } from 'react';
import JSZip from 'jszip';
import { apiFetch } from '@/utils/apiFetch';
import { INLUMEN_API_URL } from '@/config/api';
import type { ChatbotConfig } from '@/services/chatbotService';
import { cn } from "@/lib/utils";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  MAIN_PIPELINE_VERSION_UID,
  updatePipelineOverviewMetadata,
} from '@/features/flow/flowPersistence';
import {
  Plus,
  LayoutGrid,
  Beaker,
  PlayCircle,
  BarChart3,
  Calendar,
  FileText,
  Hash,
  Paperclip,
  Download,
  ShieldCheck
} from 'lucide-react';
import {
  createNodeDataFromDefinition,
  fetchNodeDefinitions,
  getFallbackNodeDefinitions,
  groupNodeDefinitions,
} from '@/features/nodes/registry/nodeRegistry';
import {
  getNodeDefinitionColorClasses,
  getNodeDefinitionIcon,
} from '@/features/nodes/registry/iconRegistry';
import type { NodeDefinition } from '@/features/nodes/registry/types';

interface PipelineOverview {
  version: string;
  description: string;
  lastUpdate: string;
  createdAt: string; 
  stepCount: number;
  fileCount: number;
}

type PipelineOverviewUpdate = {
  version?: string;
  description?: string;
  activeVersionUid?: string;
  updatedAt?: string | null;
  createdAt?: string | null;
};

interface SidebarProps {
  className?: string;
  onDragStart: (event: React.DragEvent, nodeType: DragNodeType) => void;
  activeTab: string;
  onTabChange: (value: string) => void;
  onBlankPipeline?: () => void;
  onSavePipeline?: () => void;
  pipelineOverview?: PipelineOverview;
  activeVersionUid?: string;
  onOverviewUpdated?: (overview: PipelineOverviewUpdate) => void;
  activeChatbotConfig?: ChatbotConfig;
  workspaceResetKey?: number;
}

type DragNodeType = {
  type: string;
  data: ReturnType<typeof createNodeDataFromDefinition>;
};

type DockerfileGenerationResponse = {
  dockerfiles?: Array<{
    dockerfile_filename?: string;
    content?: string;
    flow_id?: string;
  }>;
  runtime_artifacts?: Array<{
    flow_id?: string;
    files?: Array<{
      filename?: string;
      content?: string;
      content_type?: string;
    }>;
  }>;
  deployment_files?: DeploymentBundleFile[];
  input_files?: DeploymentBundleFile[];
};

type DeploymentValidationReport = {
  ok?: boolean;
  errors?: string[];
  argo?: { ok?: boolean; errors?: string[] } | null;
  dagster?: { ok?: boolean; errors?: string[] } | null;
};

type DeploymentRepairReport = {
  ok?: boolean;
  changed?: boolean;
  actions?: string[];
};

type DeploymentBundleGenerationResponse = {
  files?: DeploymentBundleFile[];
  manifest?: Record<string, unknown>;
  validation_report?: DeploymentValidationReport | null;
  repair_report?: DeploymentRepairReport | null;
};

type DeploymentBundleFile = {
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
};

type PipelineOverviewResponse = {
  version?: string;
  description?: string;
  active_version_uid?: string;
  created_at?: string;
  updated_at?: string;
};

const errorToMessage = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

type DockerfileDownload = { name: string; url: string };
type RuntimeArtifactDownload = { name: string; url: string };
type YamlDownload = { name: string; url: string };
type DeploymentBundleDownload = { name: string; url: string };
type DeploymentTargets = { argo: boolean; dagster: boolean };
type DeploymentValidationMode = "fast" | "validate" | "repair";

const FAMILY_LABELS: Record<string, string> = {
  sources: "Sources",
  tasks: "Tasks",
  destinations: "Destinations",
  flow: "Flow",
  subpipeline: "Subpipeline",
};

export function Sidebar({
  className,
  onDragStart,
  activeTab,
  onTabChange,
  onBlankPipeline,
  onSavePipeline,
  pipelineOverview,
  activeVersionUid,
  onOverviewUpdated,
  activeChatbotConfig,
  workspaceResetKey = 0,
}: SidebarProps) {
  // --- overview state (fetched when Overview tab is opened)
  const [overviewData, setOverviewData] = useState<Partial<PipelineOverview> | null>(null);
  const [overviewError, setOverviewError] = useState<string>("");
  const [isLoadingOverview, setIsLoadingOverview] = useState(false);
  const [overviewVersionDraft, setOverviewVersionDraft] = useState("");
  const [overviewDescriptionDraft, setOverviewDescriptionDraft] = useState("");
  const [isSavingOverview, setIsSavingOverview] = useState(false);
  const [overviewSaveError, setOverviewSaveError] = useState("");

  // --- Dockerfiles state
  const [isGeneratingDeployment, setIsGeneratingDeployment] = useState(false);
  const [dockerfileDownloads, setDockerfileDownloads] = useState<DockerfileDownload[]>([]);
  const [runtimeArtifactDownloads, setRuntimeArtifactDownloads] = useState<RuntimeArtifactDownload[]>([]);
  const [yamlDownload, setYamlDownload] = useState<YamlDownload | null>(null);
  const [deploymentBundleDownload, setDeploymentBundleDownload] = useState<DeploymentBundleDownload | null>(null);
  const [deploymentError, setDeploymentError] = useState<string>("");
  const [deploymentValidationReport, setDeploymentValidationReport] = useState<DeploymentValidationReport | null>(null);
  const [deploymentRepairReport, setDeploymentRepairReport] = useState<DeploymentRepairReport | null>(null);
  const [deploymentValidationMode, setDeploymentValidationMode] = useState<DeploymentValidationMode>("validate");
  const [deploymentTargets, setDeploymentTargets] = useState<DeploymentTargets>({
    argo: true,
    dagster: false,
  });
  const [nodeDefinitions, setNodeDefinitions] = useState<NodeDefinition[]>(
    getFallbackNodeDefinitions,
  );

  useEffect(() => {
    let cancelled = false;
    fetchNodeDefinitions()
      .then((definitions) => {
        if (cancelled) return;
        setNodeDefinitions(definitions);
      })
      .catch((error) => {
        if (cancelled) return;
        console.warn("[Sidebar.tsx] Using fallback node definitions:", error);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Cleanup blob URLs on unmount
  useEffect(() => {
    return () => {
      dockerfileDownloads.forEach((d) => URL.revokeObjectURL(d.url));
      runtimeArtifactDownloads.forEach((d) => URL.revokeObjectURL(d.url));
      if (yamlDownload?.url) URL.revokeObjectURL(yamlDownload.url);
      if (deploymentBundleDownload?.url) URL.revokeObjectURL(deploymentBundleDownload.url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const clearDockerfileDownloads = () => {
    setDockerfileDownloads((prev) => {
      prev.forEach((d) => URL.revokeObjectURL(d.url));
      return [];
    });
  };

  const clearRuntimeArtifactDownloads = () => {
    setRuntimeArtifactDownloads((prev) => {
      prev.forEach((download) => URL.revokeObjectURL(download.url));
      return [];
    });
  };

  const clearYamlDownload = () => {
    setYamlDownload((prev) => {
      if (prev?.url) URL.revokeObjectURL(prev.url);
      return null;
    });
  };

  const clearDeploymentBundleDownload = () => {
    setDeploymentBundleDownload((prev) => {
      if (prev?.url) URL.revokeObjectURL(prev.url);
      return null;
    });
  };

  useEffect(() => {
    clearDockerfileDownloads();
    clearRuntimeArtifactDownloads();
    clearYamlDownload();
    clearDeploymentBundleDownload();
    setDeploymentError("");
    setDeploymentValidationReport(null);
    setDeploymentRepairReport(null);
    // The reset key changes only after a confirmed workspace-wide clear.
  }, [workspaceResetKey]);

  const sanitizeZipSegment = (value: unknown, fallback: string) => {
    const cleaned = String(value || "")
      .trim()
      .replace(/[^A-Za-z0-9_.-]+/g, "-")
      .replace(/^[-.]+|[-.]+$/g, "");
    return cleaned || fallback;
  };

  const safeZipPath = (file: DeploymentBundleFile, index: number) => {
    const rawPath = String(file.path || "").trim();
    if (rawPath) {
      const parts = rawPath.replace(/\\/g, "/")
        .split("/")
        .map((part) => part.trim())
        .filter((part) => part && part !== ".");
      if (parts.length > 0 && !rawPath.startsWith("/") && !parts.includes("..")) {
        return parts.join("/");
      }
    }
    const rawFallbackPath = String(file.path || "").trim();
    if (rawFallbackPath) {
      const parts = rawFallbackPath
        .replace(/\\/g, "/")
        .split("/")
        .map((part) => sanitizeZipSegment(part, "file"))
        .filter((part) => part && part !== "." && part !== "..");
      if (parts.length > 0) return parts.join("/");
    }
    const flowId = sanitizeZipSegment(file.flow_id, "node");
    const filename = sanitizeZipSegment(file.filename, `artifact-${index + 1}.txt`);
    return `nodes/${flowId}/${filename}`;
  };

  const buildDeploymentZip = async (
    files: DeploymentBundleFile[],
  ) => {
    const zip = new JSZip();
    const written = new Set<string>();
    const writeFile = (
      path: string,
      content: string,
      contentEncoding?: string,
    ) => {
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
      zip.file(
        nextPath,
        content,
        contentEncoding === "base64" ? { base64: true } : undefined,
      );
      return nextPath;
    };

    files.forEach((file, index) => {
      const path = safeZipPath(file, index);
      writeFile(
        path,
        file.content ?? "",
        file.content_encoding ?? file.encoding,
      );
    });

    return zip.generateAsync({
      type: "blob",
      compression: "DEFLATE",
    });
  };

  const generateDockerfiles = async (): Promise<DockerfileGenerationResponse> => {
    const genRes = await apiFetch(`${INLUMEN_API_URL}/agentic_generate_dockerfiles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        // Dagster exports are attachment-driven. A Python script is sufficient;
        // requirements, manifests, Dockerfiles, and input files are optional.
        require_attached_runtime: deploymentTargets.dagster,
      }),
    });

    if (!genRes.ok) {
      const payload = await genRes.json().catch(async () => ({
        error: await genRes.text().catch(() => ""),
      }));
      throw new Error(
        String(payload?.error || "").trim()
        || `Could not prepare deployment files (${genRes.status})`,
      );
    }

    return await genRes.json(); // expected: { dockerfiles: [{dockerfile_filename, content}, ...] }
  };

  const generateDeploymentBundle = async (
    dockerfileJson: DockerfileGenerationResponse,
  ): Promise<DeploymentBundleGenerationResponse> => {
    const bundleRes = await apiFetch(`${INLUMEN_API_URL}/agentic_generate_deployment_bundle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dockerfile_json: dockerfileJson,
        targets: deploymentTargets,
        validation_mode: deploymentValidationMode,
        validate_bundle: true,
        validation: {
          enabled: true,
          mode: deploymentValidationMode,
          materialize: deploymentValidationMode !== "fast" && deploymentTargets.dagster,
          validate_argo: deploymentValidationMode !== "fast" && deploymentTargets.argo,
          validate_dagster: deploymentValidationMode !== "fast" && deploymentTargets.dagster,
          argo_lint: false,
          argo_dry_run: false,
        },
      }),
    });

    if (!bundleRes.ok) {
      const errText = await bundleRes.text().catch(() => "");
      throw new Error(`Failed to generate deployment bundle: ${bundleRes.status} ${bundleRes.statusText} ${errText}`);
    }

    return await bundleRes.json();
  };

  // fetch overview properties when opening Overview tab
  const fetchPipelineOverview = async (): Promise<PipelineOverviewResponse> => {
    const res = await apiFetch(`${INLUMEN_API_URL}/api/pipeline/overview`, { method: "GET" });
    if (!res.ok) {
      const errText = await res.text().catch(() => "");
      throw new Error(`Failed to fetch overview: ${res.status} ${res.statusText} ${errText}`);
    }
    return await res.json(); // expected: { version, description, active_version_uid, created_at, updated_at }
  };

  useEffect(() => {
    if (activeTab !== "overview") return;
    let isCancelled = false;
    (async () => {
      try {
        setOverviewError("");
        setIsLoadingOverview(true);
        const data = await fetchPipelineOverview();
        if (isCancelled) return;
        setOverviewData({
          version: data?.version ?? "",
          description: data?.description ?? "",
          createdAt: data?.created_at ?? "",
          lastUpdate: data?.updated_at ?? "",
        });
        onOverviewUpdated?.({
          version: data?.version ?? "",
          description: data?.description ?? "",
          activeVersionUid: data?.active_version_uid,
          updatedAt: data?.updated_at ?? null,
          createdAt: data?.created_at ?? null,
        });
      } catch (e: unknown) {
        if (isCancelled) return;
        console.error("[Sidebar.tsx] Overview fetch error:", e);
        setOverviewError(errorToMessage(e, "Failed to fetch overview."));
      } finally {
        if (!isCancelled) setIsLoadingOverview(false);
      }
    })();
    return () => {
      isCancelled = true;
    };
  }, [activeTab, activeVersionUid, onOverviewUpdated]);

  const handleGenerateDeploymentArtifacts = async () => {
    try {
      setDeploymentError("");
      setDeploymentValidationReport(null);
      setDeploymentRepairReport(null);
      if (!deploymentTargets.argo && !deploymentTargets.dagster) {
        throw new Error("Select at least one deployment target.");
      }
      setIsGeneratingDeployment(true);
      clearDockerfileDownloads();
      clearRuntimeArtifactDownloads();
      clearYamlDownload();
      clearDeploymentBundleDownload();

      const dockerfile_json = await generateDockerfiles();

      const dockerfiles = dockerfile_json?.dockerfiles ?? [];
      if (!Array.isArray(dockerfiles) || dockerfiles.length === 0) {
        throw new Error("No Dockerfiles were generated (dockerfiles array is empty).");
      }

      const links: DockerfileDownload[] = dockerfiles.map(
        (df, idx: number) => {
          const name = df?.dockerfile_filename || `Dockerfile_${idx + 1}`;
          const blob = new Blob([df?.content ?? ""], { type: "text/plain;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          return { name, url };
        }
      );

      const runtimeLinks = (dockerfile_json.runtime_artifacts ?? []).flatMap((artifact) =>
        (artifact.files ?? [])
          .filter((file) => file.filename && !file.filename.startsWith("Dockerfile."))
          .map((file) => {
            const blob = new Blob([file.content ?? ""], {
              type: file.content_type || "text/plain;charset=utf-8",
            });
            return {
              name: `${artifact.flow_id ?? "node"}-${file.filename}`,
              url: URL.createObjectURL(blob),
            };
          }),
      );
      const bundle = await generateDeploymentBundle(dockerfile_json);
      const bundledFiles = Array.isArray(bundle.files) ? bundle.files : [];
      if (bundledFiles.length === 0) {
        throw new Error("Deployment bundle response did not include files.");
      }
      const deploymentRuntimeLinks = bundledFiles
        .filter((file) => file.role !== "dockerfile" && file.filename && file.path?.startsWith("nodes/"))
        .map((file, index) => {
          const blobContent = file.encoding === "base64"
            ? Uint8Array.from(atob(file.content ?? ""), (character) => character.charCodeAt(0))
            : file.content ?? "";
          const blob = new Blob([blobContent], {
            type: file.content_type || "text/plain;charset=utf-8",
          });
          return {
            name: safeZipPath(file, index).replace(/\//g, "__"),
            url: URL.createObjectURL(blob),
          };
        });
      const argoWorkflowFile = bundledFiles.find((file) => file.path === "argo/workflow.yaml");

      const yamlDownloadLink = argoWorkflowFile
        ? {
            name: argoWorkflowFile.filename || "workflow.yaml",
            url: URL.createObjectURL(new Blob([argoWorkflowFile.content ?? ""], { type: "application/x-yaml;charset=utf-8" })),
          }
        : null;
      const bundleBlob = await buildDeploymentZip(bundledFiles);
      const bundleUrl = URL.createObjectURL(bundleBlob);
      const targetName = [
        deploymentTargets.argo ? "argo" : "",
        deploymentTargets.dagster ? "dagster" : "",
      ].filter(Boolean).join("-");

      setDockerfileDownloads(links);
      setRuntimeArtifactDownloads(deploymentRuntimeLinks.length > 0 ? deploymentRuntimeLinks : runtimeLinks);
      setYamlDownload(yamlDownloadLink);
      setDeploymentValidationReport(bundle.validation_report || null);
      setDeploymentRepairReport(bundle.repair_report || null);
      setDeploymentBundleDownload({
        name: `inlumen-${targetName || "deployment"}-artifacts-${Date.now()}.zip`,
        url: bundleUrl,
      });
    } catch (e: unknown) {
      clearDockerfileDownloads();
      clearRuntimeArtifactDownloads();
      clearDeploymentBundleDownload();
      setDeploymentValidationReport(null);
      setDeploymentRepairReport(null);
      console.error("[Sidebar.tsx] Generate deployment artifacts error:", e);
      setDeploymentError(errorToMessage(e, "Failed to generate deployment artifacts."));
    } finally {
      setIsGeneratingDeployment(false);
    }
  };

  // Choose fetched overview first, fall back to prop if still pass it in
  const overview = {
    ...pipelineOverview,
    ...overviewData,
  } as PipelineOverview;
  const isMainVersion = activeVersionUid === MAIN_PIPELINE_VERSION_UID;
  const deploymentButtonLabel = deploymentValidationMode === "repair"
    ? "Generate, Repair & Validate"
    : deploymentValidationMode === "validate"
      ? "Generate & Validate"
      : "Generate Deployment Artifacts";

  useEffect(() => {
    setOverviewVersionDraft(overview?.version ?? "");
    setOverviewDescriptionDraft(overview?.description ?? "");
  }, [overview?.description, overview?.version]);

  const handleOverviewMetadataSave = async () => {
    const nextVersion = isMainVersion
      ? "Main"
      : overviewVersionDraft.trim() || overview?.version || "Main";
    const nextDescription = overviewDescriptionDraft.trim();
    const currentVersion = overview?.version || "";
    const currentDescription = overview?.description || "";

    if (nextVersion === currentVersion && nextDescription === currentDescription) return;

    try {
      setOverviewSaveError("");
      setIsSavingOverview(true);
      const saved = await updatePipelineOverviewMetadata({
        version: nextVersion,
        description: nextDescription,
        activeVersionUid,
      });
      const savedVersion = saved.version ?? nextVersion;
      const savedDescription = saved.description ?? nextDescription;
      setOverviewVersionDraft(savedVersion);
      setOverviewDescriptionDraft(savedDescription);
      setOverviewData((current) => ({
        ...current,
        version: savedVersion,
        description: savedDescription,
        lastUpdate: saved.updated_at ?? current?.lastUpdate ?? "",
        createdAt: saved.created_at ?? current?.createdAt ?? "",
      }));
      onOverviewUpdated?.({
        version: savedVersion,
        description: savedDescription,
        activeVersionUid: saved.active_version_uid,
        updatedAt: saved.updated_at ?? null,
        createdAt: saved.created_at ?? null,
      });
    } catch (e: unknown) {
      console.error("[Sidebar.tsx] Overview save error:", e);
      setOverviewSaveError(errorToMessage(e, "Failed to save overview."));
      setOverviewVersionDraft(currentVersion);
      setOverviewDescriptionDraft(currentDescription);
    } finally {
      setIsSavingOverview(false);
    }
  };

  return (
    <div className={cn("w-64 border-r border-border bg-card flex flex-col", className)}>
      <Tabs value={activeTab} onValueChange={onTabChange} className="w-full">
        <TabsList className="grid h-12 w-full grid-cols-3 rounded-none border-b border-border p-1">
          <TabsTrigger value="lab" className="min-w-0 gap-1 px-1.5 text-xs">
            <Beaker className="h-3.5 w-3.5 shrink-0" />
            Lab
          </TabsTrigger>
          <TabsTrigger value="overview" className="min-w-0 gap-1 px-1.5 text-xs">
            <BarChart3 className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">Overview</span>
          </TabsTrigger>
          <TabsTrigger value="simulate" className="min-w-0 gap-1 px-1.5 text-xs">
            <PlayCircle className="h-3.5 w-3.5 shrink-0" />
            Run
          </TabsTrigger>
        </TabsList>
      </Tabs>

      <ScrollArea className="flex-1 px-4">
        {activeTab === "lab" && (
          <div className="py-4 space-y-6">
            <div>
              <h3 className="text-sm font-medium mb-3 flex items-center gap-2">
                <Plus className="w-4 h-4" />
                Pipeline Components
              </h3>
              <div className="space-y-5">
                {groupNodeDefinitions(nodeDefinitions).map(([family, definitions]) => (
                  <div key={family} className="space-y-2">
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {FAMILY_LABELS[family] ?? family}
                    </p>
                    {definitions.map((definition) => {
                      const colorClasses = getNodeDefinitionColorClasses(definition.palette.color);
                      return (
                        <div
                          key={definition.id}
                          draggable
                          onDragStart={(event) =>
                            onDragStart(event, {
                              type: 'custom',
                              data: createNodeDataFromDefinition(definition),
                            })
                          }
                          className="flex items-start gap-3 p-2.5 rounded-md border border-border cursor-move hover:bg-muted/50 transition-colors"
                        >
                          <div className={cn("p-1.5 rounded-md", colorClasses.split(' ')[0])}>
                            {getNodeDefinitionIcon(definition.palette.icon)}
                          </div>
                          <div>
                            <h4 className="text-sm font-medium">{definition.palette.label}</h4>
                            <p className="text-xs text-muted-foreground mt-0.5">
                              {definition.palette.description}
                            </p>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {activeTab === "overview" && (
          <div className="py-4 space-y-4">
            <div>
              <h3 className="text-sm font-medium mb-3 flex items-center gap-2">
                <BarChart3 className="w-4 h-4" />
                Pipeline Overview
              </h3>

              {isLoadingOverview && (
                <div className="text-xs text-muted-foreground mb-2">Refreshing overview…</div>
              )}
              {overviewError && (
                <div className="text-xs text-red-400 mb-2">{overviewError}</div>
              )}
              {overviewSaveError && (
                <div className="text-xs text-red-400 mb-2">{overviewSaveError}</div>
              )}
              {isSavingOverview && (
                <div className="text-xs text-muted-foreground mb-2">Saving overview...</div>
              )}

              <div className="space-y-3">
                <div className="p-3 rounded-lg border border-border bg-muted/30">
                  <div className="flex items-center gap-2 text-muted-foreground mb-1">
                    <Hash className="w-3.5 h-3.5" />
                    <span className="text-xs font-medium">Pipeline Version</span>
                  </div>
                  <Input
                    aria-label="Pipeline version"
                    value={overviewVersionDraft}
                    disabled={isSavingOverview || isMainVersion}
                    onChange={(event) => setOverviewVersionDraft(event.target.value)}
                    onBlur={() => { void handleOverviewMetadataSave(); }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.currentTarget.blur();
                      }
                    }}
                    placeholder="Main"
                    className="h-8 px-2 text-sm font-semibold"
                  />
                </div>

                <div className="p-3 rounded-lg border border-border bg-muted/30">
                  <div className="flex items-center gap-2 text-muted-foreground mb-1">
                    <FileText className="w-3.5 h-3.5" />
                    <span className="text-xs font-medium">Pipeline Description</span>
                  </div>
                  <Textarea
                    aria-label="Pipeline description"
                    value={overviewDescriptionDraft}
                    disabled={isSavingOverview}
                    onChange={(event) => setOverviewDescriptionDraft(event.target.value)}
                    onBlur={() => { void handleOverviewMetadataSave(); }}
                    placeholder="None"
                    className="min-h-[76px] resize-none px-2 py-2 text-sm font-medium"
                  />
                </div>

                <div className="p-3 rounded-lg border border-border bg-muted/30">
                  <div className="flex items-center gap-2 text-muted-foreground mb-1">
                    <Calendar className="w-3.5 h-3.5" />
                    <span className="text-xs font-medium">Last Update</span>
                  </div>
                  <p className="text-sm font-semibold">{overview?.lastUpdate || 'Never'}</p>
                </div>

                <div className="p-3 rounded-lg border border-border bg-muted/30">
                  <div className="flex items-center gap-2 text-muted-foreground mb-1">
                    <Calendar className="w-3.5 h-3.5" />
                    <span className="text-xs font-medium">Created At</span>
                  </div>
                  <p className="text-sm font-semibold">{overview?.createdAt || 'Never'}</p>
                </div>

                <div className="p-3 rounded-lg border border-border bg-muted/30">
                  <div className="flex items-center gap-2 text-muted-foreground mb-1">
                    <LayoutGrid className="w-3.5 h-3.5" />
                    <span className="text-xs font-medium">Number of Steps</span>
                  </div>
                  <p className="text-sm font-semibold">{overview?.stepCount ?? 0}</p>
                </div>

                <div className="p-3 rounded-lg border border-border bg-muted/30">
                  <div className="flex items-center gap-2 text-muted-foreground mb-1">
                    <Paperclip className="w-3.5 h-3.5" />
                    <span className="text-xs font-medium">Number of Files</span>
                  </div>
                  <p className="text-sm font-semibold">{overview?.fileCount ?? 0}</p>
                </div>
              </div>
            </div>
          </div>
        )}

        {activeTab === "simulate" && (
          <div className="py-4 space-y-4">
            {/* Deployment artifacts */}
            <div className="p-4 border rounded-lg border-border">
              <h3 className="text-sm font-medium mb-2">Generate Deployment Artifacts</h3>
              <p className="text-xs text-muted-foreground mb-3">
                Builds from the scripts and input files attached to each node.
              </p>

              <div className="mb-3 rounded-md border border-border bg-muted/20 p-2">
                <div className="mb-2 text-xs font-medium">Deployment target</div>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={deploymentTargets.argo}
                    onChange={(event) =>
                      setDeploymentTargets((current) => ({
                        ...current,
                        argo: event.target.checked,
                      }))
                    }
                  />
                  <span>Argo Workflow</span>
                </label>
                <label className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={deploymentTargets.dagster}
                    onChange={(event) =>
                      setDeploymentTargets((current) => ({
                        ...current,
                        dagster: event.target.checked,
                      }))
                    }
                  />
                  <span>Dagster project</span>
                </label>
              </div>

              <div className="mb-3 rounded-md border border-border bg-muted/20 p-2">
                <div className="mb-2 flex items-center gap-2 text-xs font-medium">
                  <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  Deployment validation
                </div>
                <div className="grid grid-cols-3 gap-1">
                  {([
                    ["fast", "Fast"],
                    ["validate", "Validate"],
                    ["repair", "Repair"],
                  ] as const).map(([mode, label]) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => setDeploymentValidationMode(mode)}
                      className={cn(
                        "rounded-md border px-2 py-1.5 text-xs transition-colors",
                        deploymentValidationMode === mode
                          ? "border-primary bg-primary/15 text-primary"
                          : "border-border bg-background text-muted-foreground hover:bg-muted",
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <p className="mt-2 text-xs text-muted-foreground">
                  {deploymentValidationMode === "repair"
                    ? "Normalize the bundle layout, then validate before download."
                    : deploymentValidationMode === "validate"
                      ? "Validate contracts and materialize selected Dagster assets before download."
                      : "Validate bundle structure, required inputs, contracts, paths, sizes, and checksums without installing or materializing targets."}
                </p>
              </div>

              <Button
                className="h-auto min-h-10 w-full whitespace-normal px-3 py-2 text-center leading-snug"
                onClick={handleGenerateDeploymentArtifacts}
                disabled={isGeneratingDeployment || (!deploymentTargets.argo && !deploymentTargets.dagster)}
              >
                {isGeneratingDeployment ? "Generating..." : deploymentButtonLabel}
              </Button>

              {deploymentError && (
                <div className="mt-3 text-xs text-red-400">
                  {deploymentError}
                </div>
              )}

              {deploymentValidationReport && (
                <div className="mt-3 rounded-md border border-border bg-muted/20 p-2 text-xs">
                  <div className="font-medium">
                    Validation {deploymentValidationReport.ok ? "passed" : "failed"}
                  </div>
                  {deploymentValidationReport.errors && deploymentValidationReport.errors.length > 0 && (
                    <div className="mt-1 text-red-400">
                      {deploymentValidationReport.errors.slice(0, 2).join(" ")}
                    </div>
                  )}
                </div>
              )}

              {deploymentRepairReport && (
                <div className="mt-3 rounded-md border border-border bg-muted/20 p-2 text-xs">
                  <div className="font-medium">
                    Repair {deploymentRepairReport.changed ? "applied" : "checked"}
                  </div>
                  {deploymentRepairReport.actions && deploymentRepairReport.actions.length > 0 && (
                    <div className="mt-1 text-muted-foreground">
                      {deploymentRepairReport.actions.slice(0, 2).join(" ")}
                    </div>
                  )}
                </div>
              )}

              {deploymentBundleDownload && (
                <div className="mt-4">
                  <div className="text-xs font-medium mb-2">Deployment Bundle</div>
                  <a
                    href={deploymentBundleDownload.url}
                    download={deploymentBundleDownload.name}
                    className="flex items-center gap-2 text-xs underline"
                  >
                    <Download className="w-3.5 h-3.5" />
                    <span className="truncate">{deploymentBundleDownload.name}</span>
                  </a>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3 w-full"
                    onClick={clearDeploymentBundleDownload}
                  >
                    Clear Bundle Link
                  </Button>
                </div>
              )}

              {dockerfileDownloads.length > 0 && (
                <div className="mt-4">
                  <div className="text-xs font-medium mb-2">Dockerfile Downloads</div>
                  <div className="space-y-1">
                    {dockerfileDownloads.map((d) => (
                      <a
                        key={d.url}
                        href={d.url}
                        download={d.name}
                        className="flex items-center gap-2 text-xs underline"
                      >
                        <Download className="w-3.5 h-3.5" />
                        <span className="truncate">{d.name}</span>
                      </a>
                    ))}
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3 w-full"
                    onClick={clearDockerfileDownloads}
                  >
                    Clear Dockerfile Links
                  </Button>
                </div>
              )}

              {runtimeArtifactDownloads.length > 0 && (
                <div className="mt-4">
                  <div className="text-xs font-medium mb-2">Runtime Artifact Downloads</div>
                  <div className="space-y-1">
                    {runtimeArtifactDownloads.map((download) => (
                      <a
                        key={download.url}
                        href={download.url}
                        download={download.name}
                        className="flex items-center gap-2 text-xs underline"
                      >
                        <Download className="w-3.5 h-3.5" />
                        <span className="truncate">{download.name}</span>
                      </a>
                    ))}
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3 w-full"
                    onClick={clearRuntimeArtifactDownloads}
                  >
                    Clear Runtime Artifact Links
                  </Button>
                </div>
              )}

              {yamlDownload && (
                <div className="mt-4">
                  <div className="text-xs font-medium mb-2">YAML Download</div>
                  <a
                    href={yamlDownload.url}
                    download={yamlDownload.name}
                    className="flex items-center gap-2 text-xs underline"
                  >
                    <Download className="w-3.5 h-3.5" />
                    <span className="truncate">{yamlDownload.name}</span>
                  </a>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3 w-full"
                    onClick={clearYamlDownload}
                  >
                    Clear YAML Link
                  </Button>
                </div>
              )}
            </div>
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
