import React, { useEffect, useState } from 'react';
import { apiFetch } from '@/utils/apiFetch';
import { NEO4J_API_URL, LLM_API_URL } from '@/config/api';
import { cn } from "@/lib/utils";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import {
  Brain,
  MessageCircle,
  FileText,
  Zap,
  Settings,
  Clipboard,
  Database,
  Plus,
  Info,
  LayoutGrid,
  Beaker,
  PlayCircle,
  Key,
  PlusCircle,
  BarChart3,
  Calendar,
  Hash,
  Paperclip,
  Download
} from 'lucide-react';

interface PipelineOverview {
  version: string;
  lastUpdate: string;
  createdAt: string; 
  stepCount: number;
  fileCount: number;
}

interface SidebarProps {
  className?: string;
  onDragStart: (event: React.DragEvent, nodeType: any) => void;
  activeTab: string;
  onTabChange: (value: string) => void;
  githubToken: string;
  setGithubToken: (token: string) => void;
  onBlankPipeline?: () => void;
  onSavePipeline?: () => void;
  pipelineOverview?: PipelineOverview;
}

interface NodeTypeItem {
  type: string;
  label: string;
  description: string;
  icon: React.ReactNode;
  color: string;
}

const nodeTypes: NodeTypeItem[] = [
  {
    type: 'config',
    label: 'Model Configuration',
    description: 'Adjust model parameters, system prompt and more',
    icon: <Settings className="w-4 h-4" />,
    color: 'bg-sky-500/20 text-sky-300 border-sky-500/30'
  },
  {
    type: 'input',
    label: 'Input Data',
    description: 'Raw data from sensors, APIs, files or user message.',
    icon: <Database className="w-4 h-4" />,
    color: 'bg-blue-500/20 text-blue-300 border-blue-500/30'
  },
  {
    type: 'action',
    label: 'Data Preprocessing',
    description: 'Clean, normalize, and transform input data',
    icon: <FileText className="w-4 h-4" />,
    color: 'bg-lime-500/20 text-lime-300 border-lime-500/30'
  },
  {
    type: 'action',
    label: 'Feature Engineering',
    description: 'Generate or select features for model input',
    icon: <Info className="w-4 h-4" />,
    color: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30'
  },
  {
    type: 'action',
    label: 'Model Training',
    description: 'Train machine learning or deep learning models',
    icon: <Brain className="w-4 h-4" />,
    color: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30'
  },
  {
    type: 'action',
    label: 'Model Evaluation',
    description: 'Assess model performance and metrics',
    icon: <Zap className="w-4 h-4" />,
    color: 'bg-purple-500/20 text-purple-300 border-purple-500/30'
  },
  {
    type: 'output',
    label: 'AI/ML Output',
    description: 'AI/ML pipeline results',
    icon: <MessageCircle className="w-4 h-4" />,
    color: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
  },
  {
    type: 'api',
    label: 'API Call',
    description: 'Connect to external services',
    icon: <Database className="w-4 h-4" />,
    color: 'bg-rose-500/20 text-rose-300 border-rose-500/30'
  },
  {
    type: 'storage',
    label: 'Clipboard',
    description: 'Store and retrieve content',
    icon: <Clipboard className="w-4 h-4" />,
    color: 'bg-teal-500/20 text-teal-300 border-teal-500/30'
  },
  {
    type: 'custom',
    label: 'Custom Node',
    description: 'Add custom label and description',
    icon: <PlusCircle className="w-4 h-4" />,
    color: 'bg-violet-500/20 text-violet-300 border-violet-500/30'
  }
];

type DockerfileDownload = { name: string; url: string };
type YamlDownload = { name: string; url: string };

export function Sidebar({
  className,
  onDragStart,
  activeTab,
  onTabChange,
  githubToken,
  setGithubToken,
  onBlankPipeline,
  onSavePipeline,
  pipelineOverview
}: SidebarProps) {
  const [showKey, setShowKey] = useState(false);

  // --- overview state (fetched when Overview tab is opened)
  const [overviewData, setOverviewData] = useState<Partial<PipelineOverview> | null>(null);
  const [overviewError, setOverviewError] = useState<string>("");
  const [isLoadingOverview, setIsLoadingOverview] = useState(false);

  // --- OpenAI API key
  const [openaiKey, setOpenaiKey] = useState<string>(() => {
    return localStorage.getItem("openai_api_key") || "";
  });

  // --- Dockerfiles state
  const [isGeneratingDockerfiles, setIsGeneratingDockerfiles] = useState(false);
  const [dockerfileDownloads, setDockerfileDownloads] = useState<DockerfileDownload[]>([]);
  const [dockerfileError, setDockerfileError] = useState<string>("");

  // --- YAML state
  const [isGeneratingYaml, setIsGeneratingYaml] = useState(false);
  const [yamlDownload, setYamlDownload] = useState<YamlDownload | null>(null);
  const [yamlError, setYamlError] = useState<string>("");

  // Cleanup blob URLs on unmount
  useEffect(() => {
    return () => {
      dockerfileDownloads.forEach((d) => URL.revokeObjectURL(d.url));
      if (yamlDownload?.url) URL.revokeObjectURL(yamlDownload.url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleOpenaiKeyChange = (val: string) => {
    setOpenaiKey(val);
    localStorage.setItem("openai_api_key", val);
  };

  const clearDockerfileDownloads = () => {
    setDockerfileDownloads((prev) => {
      prev.forEach((d) => URL.revokeObjectURL(d.url));
      return [];
    });
    setDockerfileError("");
  };

  const clearYamlDownload = () => {
    setYamlDownload((prev) => {
      if (prev?.url) URL.revokeObjectURL(prev.url);
      return null;
    });
    setYamlError("");
  };

  const fetchNeo4jFiles = async () => {
    const filesRes = await apiFetch(`${NEO4J_API_URL}/neo4j_get_all_files`, { method: "GET" });
    if (!filesRes.ok) {
      const errText = await filesRes.text().catch(() => "");
      throw new Error(`Failed to fetch files: ${filesRes.status} ${filesRes.statusText} ${errText}`);
    }
    return await filesRes.json(); // expected: [{filename,bucket}, ...]
  };

  const generateDockerfiles = async (files: any) => {
    const genRes = await apiFetch(`${LLM_API_URL}/agentic_generate_dockerfiles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        files,
        // openai_api_key: openaiKey, // enable when backend supports it
      }),
    });

    if (!genRes.ok) {
      const errText = await genRes.text().catch(() => "");
      throw new Error(`Failed to generate Dockerfiles: ${genRes.status} ${genRes.statusText} ${errText}`);
    }

    return await genRes.json(); // expected: { dockerfiles: [{dockerfile_filename, content}, ...] }
  };

  // fetch overview properties when opening Overview tab
  const fetchPipelineOverview = async () => {
    const res = await apiFetch(`${NEO4J_API_URL}/neo4j_get_overview_properties`, { method: "GET" });
    if (!res.ok) {
      const errText = await res.text().catch(() => "");
      throw new Error(`Failed to fetch overview: ${res.status} ${res.statusText} ${errText}`);
    }
    return await res.json(); // expected: { version, created_at, updated_at }
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
          createdAt: data?.created_at ?? "",
          lastUpdate: data?.updated_at ?? "",
        });
      } catch (e: any) {
        if (isCancelled) return;
        console.error("[Sidebar.tsx] Overview fetch error:", e);
        setOverviewError(e?.message || "Failed to fetch overview.");
      } finally {
        if (!isCancelled) setIsLoadingOverview(false);
      }
    })();
    return () => {
      isCancelled = true;
    };
  }, [activeTab]);

  const handleGenerateDockerfiles = async () => {
    try {
      setDockerfileError("");
      setIsGeneratingDockerfiles(true);
      clearDockerfileDownloads();

      const files = await fetchNeo4jFiles();
      const dockerfile_json = await generateDockerfiles(files);

      const dockerfiles = dockerfile_json?.dockerfiles ?? [];
      if (!Array.isArray(dockerfiles) || dockerfiles.length === 0) {
        setDockerfileError("No Dockerfiles were generated (dockerfiles array is empty).");
        return;
      }

      const links: DockerfileDownload[] = dockerfiles.map(
        (df: { dockerfile_filename: string; content: string }, idx: number) => {
          const name = df?.dockerfile_filename || `Dockerfile_${idx + 1}`;
          const blob = new Blob([df?.content ?? ""], { type: "text/plain;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          return { name, url };
        }
      );

      setDockerfileDownloads(links);
    } catch (e: any) {
      console.error("[Sidebar.tsx] Generate Dockerfiles error:", e);
      setDockerfileError(e?.message || "Failed to generate Dockerfiles.");
    } finally {
      setIsGeneratingDockerfiles(false);
    }
  };

  const handleGenerateYaml = async () => {
    try {
      setYamlError("");
      setIsGeneratingYaml(true);
      clearYamlDownload();

      // Get files -> dockerfiles -> YAML
      const files = await fetchNeo4jFiles();
      const dockerfile_json = await generateDockerfiles(files);

      const yamlRes = await apiFetch(`${LLM_API_URL}/agentic_generate_yaml`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dockerfile_json,
          // openai_api_key: openaiKey, // enable when backend supports it
        }),
      });

      if (!yamlRes.ok) {
        const errText = await yamlRes.text().catch(() => "");
        throw new Error(`Failed to generate YAML: ${yamlRes.status} ${yamlRes.statusText} ${errText}`);
      }

      const yamlText = await yamlRes.text();
      const blob = new Blob([yamlText], { type: "application/x-yaml;charset=utf-8" });
      const url = URL.createObjectURL(blob);

      setYamlDownload({ name: `ai-pipeline-${Date.now()}.yaml`, url });
    } catch (e: any) {
      console.error("[Sidebar.tsx] Generate YAML error:", e);
      setYamlError(e?.message || "Failed to generate YAML.");
    } finally {
      setIsGeneratingYaml(false);
    }
  };

  // Choose fetched overview first, fall back to prop if still pass it in
  const overview = {
    ...pipelineOverview,
    ...overviewData,
  } as PipelineOverview;

  return (
    <div className={cn("w-64 border-r border-border bg-card flex flex-col", className)}>
      <Tabs value={activeTab} onValueChange={onTabChange} className="w-full">
        <TabsList className="grid grid-cols-3 w-full rounded-none border-b border-border">
          <TabsTrigger value="lab" className="flex items-center gap-1.5 text-xs">
            <Beaker className="w-4 h-4" />
            Lab
          </TabsTrigger>
          <TabsTrigger value="overview" className="flex items-center gap-1.5 text-xs">
            <BarChart3 className="w-4 h-4" />
            Overview
          </TabsTrigger>
          <TabsTrigger value="simulate" className="flex items-center gap-1.5 text-xs">
            <PlayCircle className="w-4 h-4" />
            Simulate
          </TabsTrigger>
        </TabsList>
      </Tabs>

      <ScrollArea className="flex-1 px-4">
        {activeTab === "lab" && (
          <div className="py-4 space-y-6">
            <div>
              <h3 className="text-sm font-medium mb-3 flex items-center gap-2">
                <Plus className="w-4 h-4" />
                Node Types
              </h3>
              <div className="space-y-2">
                {nodeTypes.map((nodeType) => (
                  <div
                    key={nodeType.label}
                    draggable
                    onDragStart={(event) =>
                      onDragStart(event, {
                        type: 'custom',
                        data: {
                          label: nodeType.label,
                          description: nodeType.description,
                          type: nodeType.type
                        }
                      })
                    }
                    className="flex items-start gap-3 p-2.5 rounded-md border border-border cursor-move hover:bg-muted/50 transition-colors"
                  >
                    <div className={cn("p-1.5 rounded-md", nodeType.color.split(' ')[0])}>
                      {nodeType.icon}
                    </div>
                    <div>
                      <h4 className="text-sm font-medium">{nodeType.label}</h4>
                      <p className="text-xs text-muted-foreground mt-0.5">{nodeType.description}</p>
                    </div>
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

              <div className="space-y-3">
                <div className="p-3 rounded-lg border border-border bg-muted/30">
                  <div className="flex items-center gap-2 text-muted-foreground mb-1">
                    <Hash className="w-3.5 h-3.5" />
                    <span className="text-xs font-medium">Pipeline Version</span>
                  </div>
                  <p className="text-sm font-semibold">{overview?.version || '0.0.0'}</p>
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
            {/* OpenAI key */}
            <div className="p-4 border rounded-lg border-border">
              <h3 className="text-sm font-medium mb-2 flex items-center gap-2">
                <Key className="w-4 h-4" />
                OpenAI API Key
              </h3>
              <p className="text-xs text-muted-foreground mb-3">
                Used by the LLM agents. Stored in your browser localStorage.
              </p>

              <div className="flex items-center gap-2">
                <Input
                  type={showKey ? "text" : "password"}
                  value={openaiKey}
                  onChange={(e) => handleOpenaiKeyChange(e.target.value)}
                  placeholder="sk-..."
                  className="flex-1"
                />
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => setShowKey(!showKey)}
                  title={showKey ? "Hide key" : "Show key"}
                >
                  <Key className="w-4 h-4" />
                </Button>
              </div>
            </div>

            {/* Dockerfiles */}
            <div className="p-4 border rounded-lg border-border">
              <h3 className="text-sm font-medium mb-2">Generate Dockerfiles</h3>
              <p className="text-xs text-muted-foreground mb-3">
                Fetches FILE nodes from Neo4j and generates Dockerfiles via the agent service.
              </p>

              <Button
                className="w-full"
                onClick={handleGenerateDockerfiles}
                disabled={isGeneratingDockerfiles || isGeneratingYaml}
              >
                {isGeneratingDockerfiles ? "Generating..." : "Generate Dockerfiles"}
              </Button>

              {dockerfileError && (
                <div className="mt-3 text-xs text-red-400">
                  {dockerfileError}
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
            </div>

            {/* YAML */}
            <div className="p-4 border rounded-lg border-border">
              <h3 className="text-sm font-medium mb-2">Generate YAML</h3>
              <p className="text-xs text-muted-foreground mb-3">
                Generates YAML via the agent service (uses generated Dockerfiles as input).
              </p>

              <Button
                className="w-full"
                onClick={handleGenerateYaml}
                disabled={isGeneratingYaml || isGeneratingDockerfiles}
              >
                {isGeneratingYaml ? "Generating..." : "Generate YAML"}
              </Button>

              {yamlError && (
                <div className="mt-3 text-xs text-red-400">
                  {yamlError}
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
