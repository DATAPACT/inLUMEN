import React, { useState, useCallback, useRef, useEffect, forwardRef, useImperativeHandle } from 'react';
import { ChatbotConfig } from '@/services/chatbotService';
import ReactFlow, {
  Node,
  Edge,
  Controls,
  MiniMap,
  ReactFlowInstance,
  ReactFlowProvider,
  NodeChange,
  EdgeChange,
  Connection,
  ConnectionLineType,
  MarkerType,
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { nodeTypes } from './NodeTypes';
import { PortDisplayContext } from '@/features/nodes/PortDisplayContext';
import { toast } from 'sonner';
import { cn } from "@/lib/utils";
import { FlowCanvasActionsPanel } from '@/components/flow/FlowCanvasActionsPanel';
import {
  addEdgeToBackend,
  addNodeToBackend,
  cancelPipelineScriptGenerationRun,
  deleteEdgeFromBackend,
  deleteNodeFromBackend,
  fetchPipelineScriptGenerationRun,
  fetchPipelineVersions,
  fetchPipelineGraph,
  fetchPipelineUpdatedAt,
  generatePipelineScripts,
  prepareExternalRuntimePrompt,
  restoreBackendGraphHistory,
  resumePipelineScriptGenerationRun,
  startPipelineScriptGenerationRun,
  type PipelineVersionGraph,
  type PipelineVersionSummary,
  type PipelineGenerationJob,
  type PipelineScriptGenerationMode,
  rebuildBackendFromFlow,
  savePipelineVersion,
  updateNodePositionInBackend,
} from '@/features/flow/flowPersistence';
import {
  createAgentGraphSnapshot,
  downloadJsonFile,
  getNextNumericNodeId,
  normalizeGraph,
  type AgentGraphSnapshot,
  type NormalizedGraph,
} from '@/features/flow/flowGraph';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Progress } from '@/components/ui/progress';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Textarea } from '@/components/ui/textarea';
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  Database,
  Loader2,
  Zap,
} from 'lucide-react';

interface FlowCanvasProps {
  onNodeSelect: (node: Node | null, options?: { openInspector?: boolean }) => void;
  onNodesChange?: (nodes: Node[]) => void;
  onRemoveNode?: (nodeId: string) => void;
  onRemoveEdge?: (edgeId: string) => void;
  isLightMode?: boolean;
  activeChatbotConfig?: ChatbotConfig;
  pipelinePrompt?: string;
  onVersionSaved?: (version: PipelineVersionSummary) => void;
  onCanvasEdited?: () => void;
  onActiveVersionChange?: (versionUid: string) => void;
  onActiveVersionNameChange?: (versionName: string) => void;
  onPipelineDescriptionChange?: (description: string) => void;
}

export interface FlowCanvasRef {
  updateNode: (id: string, data: Record<string, unknown>) => void;
  syncFromBackend: (graphData?: unknown) => Promise<NormalizedGraph>;
  getCurrentGraph: () => AgentGraphSnapshot;
  getCurrentVersionGraph: () => PipelineVersionGraph;
}

let nodeId = 1;

const CODEGEN_RUNTIME_FILENAMES = new Set([
  "main.py",
  "requirements.txt",
  "node-manifest.json",
  "validation-report.json",
]);

const isCodegenRuntimeFile = (filename: unknown) => {
  const normalized = String(filename || "").trim();
  const lower = normalized.toLowerCase();
  return CODEGEN_RUNTIME_FILENAMES.has(lower)
    || lower.startsWith("dockerfile.")
    || [".py", ".pyi", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".tsx"]
      .some((suffix) => lower.endsWith(suffix));
};

const nodeFiles = (node: Node): Array<{ filename?: string; name?: string } | string> => {
  const raw = Array.isArray(node.data?.file_buckets)
    ? node.data.file_buckets
    : Array.isArray(node.data?.files)
      ? node.data.files
      : [];
  return raw as Array<{ filename?: string; name?: string } | string>;
};

const hasUploadedSampleData = (nodes: Node[]) =>
  nodes.some((node) =>
    nodeFiles(node).some((file) => {
      const filename = typeof file === "string" ? file : file.filename || file.name;
      return Boolean(filename && !isCodegenRuntimeFile(filename));
    }),
  );

const generationModeOptions: Array<{
  value: PipelineScriptGenerationMode;
  title: string;
  description: string;
  icon: typeof Zap;
}> = [
  {
    value: "fast",
    title: "Fast draft",
    description: "Generate quickly with static checks. Sample inputs are optional.",
    icon: Zap,
  },
  {
    value: "generic",
    title: "Generic draft",
    description: "Generate reusable scripts without reading uploaded sample data.",
    icon: AlertCircle,
  },
  {
    value: "full",
    title: "Full data-aware",
    description: "Run with uploaded inputs, validate each node, and repair failures.",
    icon: Database,
  },
];

const modeToGenerationOptions = (
  mode: PipelineScriptGenerationMode,
  hasInputFiles: boolean,
) => {
  if (mode === "fast") {
    return {
      mode,
      includeSampleData: hasInputFiles,
      validationMode: "static" as const,
      generationStrategy: "single_pass" as const,
      allowDeterministicFallback: false,
      repairAttempts: 0,
    };
  }
  if (mode === "generic") {
    return {
      mode,
      includeSampleData: false,
      validationMode: "static" as const,
      generationStrategy: "single_pass" as const,
      allowDeterministicFallback: false,
      repairAttempts: 0,
    };
  }
  return {
    mode,
    includeSampleData: true,
    validationMode: "pipeline_sample" as const,
    generationStrategy: "single_pass" as const,
    allowDeterministicFallback: false,
    repairAttempts: 7,
  };
};

const generationProgressPercent = (job: PipelineGenerationJob | null) => {
  const steps = job?.generation_run?.steps || [];
  if (steps.length === 0) return job?.status === "queued" ? 5 : 10;
  const completed = steps.filter((step) =>
    ["valid", "invalid", "failed", "skipped"].includes(String(step.status || "")),
  ).length;
  const status = effectiveGenerationStatus(job);
  if (
    status === "valid" ||
    status === "invalid" ||
    status === "failed" ||
    status === "cancelled"
  ) return 100;
  return Math.max(10, Math.min(95, Math.round((completed / steps.length) * 100)));
};

const GENERATION_TERMINAL_STATUSES = new Set([
  "valid",
  "invalid",
  "failed",
  "cancelled",
]);

const effectiveGenerationStatus = (job: PipelineGenerationJob | null) => {
  const outer = String(job?.status || "").toLowerCase();
  if (GENERATION_TERMINAL_STATUSES.has(outer)) return outer;
  const nested = String(job?.generation_run?.status || "").toLowerCase();
  return nested || outer || "running";
};

const wait = (ms: number) =>
  new Promise<void>((resolve) => {
    window.setTimeout(resolve, ms);
  });

const copyTextToClipboard = async (value: string) => {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) {
    throw new Error("The browser did not allow clipboard access.");
  }
};

const generationFailureMessage = (job: PipelineGenerationJob) => {
  const runError = Array.isArray(job.generation_run?.errors)
    ? job.generation_run?.errors.find((item) => typeof item === "string")
    : undefined;
  return (
    job.error ||
    runError ||
    job.persistence?.reason ||
    "Pipeline script generation did not complete successfully."
  );
};

const failedGenerationStep = (job: PipelineGenerationJob | null) => {
  const steps = job?.generation_run?.steps || [];
  const failed = steps.filter((step) =>
    ["invalid", "failed"].includes(String(step.status || "").toLowerCase()),
  );
  return failed.length === 1 ? failed[0] : undefined;
};

const generationStageLabel = (stage: unknown) => {
  const key = String(stage || "pending").toLowerCase();
  const labels: Record<string, string> = {
    pending: "waiting",
    pipeline_planning: "planning pipeline",
    pipeline_generation: "generating canonical pipeline",
    pipeline_validation: "validating canonical pipeline",
    pipeline_repair: "repairing canonical pipeline",
    dependency_validation: "validating dependencies",
    dependency_installation: "installing dependencies",
    sandbox_execution: "executing sample in sandbox",
    node_compilation: "extracting independent nodes",
    compiled_independent_bundle: "complete",
    validated_cache_hit: "reused validated result",
    complete: "complete",
    cancelled: "cancelled",
  };
  return labels[key] || key.replace(/_/g, " ");
};

const getSnapshotFileRef = (file: unknown, nodeIdValue: string) => {
  if (typeof file === "string") return file;
  if (typeof File !== "undefined" && file instanceof File) return file.name;
  if (file && typeof file === "object") {
    const entry = file as { filename?: unknown; name?: unknown; bucket?: unknown; role?: unknown };
    const filename = typeof entry.filename === "string"
      ? entry.filename
      : typeof entry.name === "string"
        ? entry.name
        : "";
    if (!filename) return null;
    const bucket = typeof entry.bucket === "string" && entry.bucket.trim()
      ? entry.bucket.trim()
      : `files-step-id-${nodeIdValue}`.toLowerCase();
    const role = entry.role === "code" || entry.role === "data" ? entry.role : undefined;
    return { filename, bucket, ...(role ? { role } : {}) };
  }
  return null;
};

const GRAPH_HISTORY_LIMIT = 25;
const GRAPH_HISTORY_COALESCE_MS = 1200;

type GraphViewport = { x: number; y: number; zoom: number };

type GraphHistorySnapshot = {
  nodes: Node[];
  edges: Edge[];
  viewport: GraphViewport;
  updated_at: string | null;
  signature: string;
  coalesceKey?: string;
  timestamp: number;
};

const cloneGraphValue = <T,>(value: T): T => {
  if (typeof globalThis.structuredClone === "function") {
    return globalThis.structuredClone(value);
  }
  return JSON.parse(JSON.stringify(value)) as T;
};

const normalizeViewport = (viewport: unknown): GraphViewport => {
  const candidate = viewport && typeof viewport === "object"
    ? viewport as Partial<GraphViewport>
    : {};
  return {
    x: Number.isFinite(Number(candidate.x)) ? Number(candidate.x) : 0,
    y: Number.isFinite(Number(candidate.y)) ? Number(candidate.y) : 0,
    zoom: Number.isFinite(Number(candidate.zoom)) ? Number(candidate.zoom) : 1,
  };
};

const normalizeForHistorySignature = (value: unknown): unknown => {
  if (typeof File !== "undefined" && value instanceof File) {
    return {
      name: value.name,
      size: value.size,
      type: value.type,
      lastModified: value.lastModified,
    };
  }
  if (Array.isArray(value)) {
    return value.map(normalizeForHistorySignature);
  }
  if (value && typeof value === "object") {
    return Object.keys(value as Record<string, unknown>)
      .sort()
      .reduce<Record<string, unknown>>((acc, key) => {
        acc[key] = normalizeForHistorySignature((value as Record<string, unknown>)[key]);
        return acc;
      }, {});
  }
  return value ?? null;
};

const cleanHistoryNodes = (nodes: Node[]): Node[] =>
  cloneGraphValue(nodes).map((node) => ({
    ...node,
    selected: false,
    dragging: false,
  }));

const cleanHistoryEdges = (edges: Edge[]): Edge[] =>
  cloneGraphValue(edges).map((edge) => ({
    ...edge,
    selected: false,
  }));

const graphHistorySignature = (nodes: Node[], edges: Edge[]) => JSON.stringify({
  nodes: nodes.map((node) => ({
    id: String(node.id),
    type: node.type ?? null,
    position: {
      x: Number.isFinite(Number(node.position?.x)) ? Number(node.position?.x) : 0,
      y: Number.isFinite(Number(node.position?.y)) ? Number(node.position?.y) : 0,
    },
    data: normalizeForHistorySignature(node.data || {}),
  })),
  edges: edges.map((edge) => ({
    id: edge.id ?? "",
    source: String(edge.source || ""),
    target: String(edge.target || ""),
    sourceHandle: edge.sourceHandle ?? null,
    targetHandle: edge.targetHandle ?? null,
    type: edge.type ?? null,
    data: normalizeForHistorySignature(edge.data || {}),
  })),
});

const graphHistoryFingerprint = (signature: string) => {
  let hash = 2166136261;
  for (let index = 0; index < signature.length; index += 1) {
    hash ^= signature.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `fnv1a-${(hash >>> 0).toString(16).padStart(8, "0")}`;
};

const buildGraphHistorySnapshot = (
  nodes: Node[],
  edges: Edge[],
  viewport: unknown,
  updatedAt: string | null,
): GraphHistorySnapshot => {
  const cleanNodes = cleanHistoryNodes(nodes);
  const cleanEdges = cleanHistoryEdges(edges);
  return {
    nodes: cleanNodes,
    edges: cleanEdges,
    viewport: normalizeViewport(viewport),
    updated_at: updatedAt,
    signature: graphHistorySignature(cleanNodes, cleanEdges),
    timestamp: Date.now(),
  };
};

export const FlowCanvas = forwardRef<FlowCanvasRef, FlowCanvasProps>(({
  onNodeSelect,
  onNodesChange,
  onRemoveNode,
  onRemoveEdge,
  isLightMode,
  activeChatbotConfig,
  pipelinePrompt,
  onVersionSaved,
  onCanvasEdited,
  onActiveVersionChange,
  onActiveVersionNameChange,
  onPipelineDescriptionChange,
}, ref) => {
  const [nodes, setNodes] = useState<Node[]>(() => {
    const savedNodes = localStorage.getItem('ai-flow-nodes');
    return savedNodes ? JSON.parse(savedNodes) : [];
  });
  const [edges, setEdges] = useState<Edge[]>(() => {
    const savedEdges = localStorage.getItem('ai-flow-edges');
    return savedEdges ? JSON.parse(savedEdges) : [];
  });
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const [showPortDetails, setShowPortDetails] = useState(
    () => localStorage.getItem('inlumen-show-port-details') === 'true',
  );
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = useState<ReactFlowInstance | null>(null);
  const lastSeenUpdatedAtRef = useRef<string | null>(null);
  const refreshCooldownUntilRef = useRef<number>(0);
  const syncBackoffUntilRef = useRef<number>(0);
  const syncFailureLoggedRef = useRef(false);
  const selectedNodeIdRef = useRef<string | null>(null);
  const [isSaveVersionOpen, setIsSaveVersionOpen] = useState(false);
  const [versionName, setVersionName] = useState("");
  const [isSavingVersion, setIsSavingVersion] = useState(false);
  const undoStackRef = useRef<GraphHistorySnapshot[]>([]);
  const redoStackRef = useRef<GraphHistorySnapshot[]>([]);
  const dragStartSnapshotRef = useRef<GraphHistorySnapshot | null>(null);
  const [historyAvailability, setHistoryAvailability] = useState({
    canUndo: false,
    canRedo: false,
  });
  const [isHistoryRestoring, setIsHistoryRestoring] = useState(false);
  const [isGeneratingScripts, setIsGeneratingScripts] = useState(false);
  const [isCancellingScripts, setIsCancellingScripts] = useState(false);
  const generationCancelRequestedRef = useRef(false);
  const [isScriptGenerationOpen, setIsScriptGenerationOpen] = useState(false);
  const [scriptGenerationPrompt, setScriptGenerationPrompt] = useState("");
  const [scriptGenerationMode, setScriptGenerationMode] =
    useState<PipelineScriptGenerationMode>("full");
  const [isPreparingExternalPrompt, setIsPreparingExternalPrompt] = useState(false);
  const [generationJob, setGenerationJob] = useState<PipelineGenerationJob | null>(null);
  const uploadedSampleDataAvailable = hasUploadedSampleData(nodes);

  useEffect(() => {
    if (!uploadedSampleDataAvailable && scriptGenerationMode === "full") {
      setScriptGenerationMode("generic");
    }
  }, [scriptGenerationMode, uploadedSampleDataAvailable]);

  const markLocalWrite = useCallback((ms = 800) => {
    refreshCooldownUntilRef.current = Date.now() + ms;
  }, []);

  const scheduleSyncRetry = useCallback((label: string, error: unknown) => {
    if (!syncFailureLoggedRef.current) {
      console.warn(`[FlowCanvas.tsx] ${label}:`, error);
      syncFailureLoggedRef.current = true;
    }
    syncBackoffUntilRef.current = Date.now() + 15000;
  }, []);

  const markSyncHealthy = useCallback(() => {
    syncFailureLoggedRef.current = false;
    syncBackoffUntilRef.current = 0;
  }, []);

  const syncHistoryAvailability = useCallback(() => {
    setHistoryAvailability({
      canUndo: undoStackRef.current.length > 0,
      canRedo: redoStackRef.current.length > 0,
    });
  }, []);

  const currentViewport = useCallback(
    () => normalizeViewport(reactFlowInstance?.toObject().viewport),
    [reactFlowInstance],
  );

  const createHistorySnapshot = useCallback(
    () => buildGraphHistorySnapshot(
      nodes,
      edges,
      currentViewport(),
      lastSeenUpdatedAtRef.current,
    ),
    [currentViewport, edges, nodes],
  );

  const pushHistorySnapshot = useCallback((
    snapshot?: GraphHistorySnapshot,
    options?: { coalesceKey?: string },
  ) => {
    const now = Date.now();
    const entry = {
      ...(snapshot ?? createHistorySnapshot()),
      coalesceKey: options?.coalesceKey,
      timestamp: now,
    };
    const last = undoStackRef.current[undoStackRef.current.length - 1];

    if (last?.signature === entry.signature) {
      last.timestamp = now;
      syncHistoryAvailability();
      return;
    }

    if (
      options?.coalesceKey
      && last?.coalesceKey === options.coalesceKey
      && now - last.timestamp < GRAPH_HISTORY_COALESCE_MS
    ) {
      last.timestamp = now;
      return;
    }

    undoStackRef.current = [
      ...undoStackRef.current,
      entry,
    ].slice(-GRAPH_HISTORY_LIMIT);
    redoStackRef.current = [];
    syncHistoryAvailability();
  }, [createHistorySnapshot, syncHistoryAvailability]);

  const applyGraph = useCallback((data: unknown, normalizedGraph?: NormalizedGraph) => {
    const g = normalizedGraph ?? normalizeGraph(data);
    const pipeline = data && typeof data === "object"
      ? (data as { pipeline?: { active_version_uid?: unknown; active_version_name?: unknown; description?: unknown } }).pipeline
      : null;
    if (typeof pipeline?.active_version_uid === "string" && pipeline.active_version_uid.trim()) {
      onActiveVersionChange?.(pipeline.active_version_uid);
    }
    if (typeof pipeline?.active_version_name === "string" && pipeline.active_version_name.trim()) {
      onActiveVersionNameChange?.(pipeline.active_version_name);
    }
    if (typeof pipeline?.description === "string") {
      onPipelineDescriptionChange?.(pipeline.description);
    }
    setNodes(g.nodes);
    setEdges(g.edges);
    lastSeenUpdatedAtRef.current = g.updated_at;
    nodeId = getNextNumericNodeId(g.nodes, nodeId);

    const selectedNodeId = selectedNodeIdRef.current;
    if (selectedNodeId) {
      const refreshedSelection = g.nodes.find((node) => node.id === selectedNodeId) || null;
      selectedNodeIdRef.current = refreshedSelection?.id ?? null;
      setSelectedNode(refreshedSelection);
      onNodeSelect(refreshedSelection, { openInspector: false });
    }

    return g;
  }, [onActiveVersionChange, onActiveVersionNameChange, onNodeSelect, onPipelineDescriptionChange]);

  const fetchGraphAndApply = useCallback(async () => {
    const data = await fetchPipelineGraph();
    return applyGraph(data);
  }, [applyGraph]);

  const syncFromBackend = useCallback(async (graphData?: unknown) => {
    try {
      let graph: NormalizedGraph;
      if (graphData == null) {
        graph = await fetchGraphAndApply();
      } else {
        const normalizedGraph = normalizeGraph(graphData);
        const incomingSignature = graphHistorySignature(normalizedGraph.nodes, normalizedGraph.edges);
        const currentSnapshot = createHistorySnapshot();
        if (incomingSignature !== currentSnapshot.signature) {
          pushHistorySnapshot(currentSnapshot);
        }
        graph = applyGraph(graphData, normalizedGraph);
      }
      markSyncHealthy();
      return graph;
    } catch (error) {
      scheduleSyncRetry("Explicit graph sync failed", error);
      throw error;
    }
  }, [
    applyGraph,
    createHistorySnapshot,
    fetchGraphAndApply,
    markSyncHealthy,
    pushHistorySnapshot,
    scheduleSyncRetry,
  ]);

  const getCurrentGraph = useCallback(() => {
    return createAgentGraphSnapshot(normalizeGraph({
      updated_at: lastSeenUpdatedAtRef.current,
      nodes,
      edges,
    }));
  }, [edges, nodes]);

  useEffect(() => {
    let cancelled = false;
    const initialLoad = async () => {
      try {
        await fetchGraphAndApply();
        markSyncHealthy();
      } catch (e) {
        scheduleSyncRetry("Initial pipeline graph fetch failed", e);
      }
    };
    const tick = async () => {
      try {
        if (
          Date.now() < refreshCooldownUntilRef.current ||
          Date.now() < syncBackoffUntilRef.current
        ) {
          return;
        }
        const updatedAt = await fetchPipelineUpdatedAt();
        if (cancelled) return;
        markSyncHealthy();
        if (lastSeenUpdatedAtRef.current === null) {
          if (updatedAt) {
            await fetchGraphAndApply();
          }
          return;
        }
        if (updatedAt && updatedAt !== lastSeenUpdatedAtRef.current) {
          await fetchGraphAndApply();
        }
      } catch (e) {
        scheduleSyncRetry("Backend poll tick failed", e);
      }
    };
    // Load once at mount, then poll
    initialLoad();
    const id = window.setInterval(tick, 1500);
    tick();
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [fetchGraphAndApply, markSyncHealthy, scheduleSyncRetry]);

  // Expose updateNode 
  const updateNode = useCallback((id: string, data: Record<string, unknown>) => {
    pushHistorySnapshot(undefined, { coalesceKey: `node:${id}:properties` });
    onCanvasEdited?.();
    setNodes((nds) =>
      nds.map((node) => {
        if (node.id === id) {
          const updatedNode = { ...node, data: { ...node.data, ...data } };
          return updatedNode;
        }
        return node;
      })
    );
    // Also update selected node 
    setSelectedNode((prev) => {
      if (prev?.id === id) {
        return { ...prev, data: { ...prev.data, ...data } };
      }
      return prev;
    });
  }, [onCanvasEdited, pushHistorySnapshot]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const triggerImport = () => fileInputRef.current?.click();

  const createSerializableFlow = useCallback((): PipelineVersionGraph => {
    const viewport = reactFlowInstance?.toObject().viewport ?? { x: 0, y: 0, zoom: 1 };
    return {
      updated_at: lastSeenUpdatedAtRef.current,
      nodes: nodes.map((node) => {
        const data = { ...(node.data || {}) };
        delete data.file_buckets;
        if (Array.isArray(data.files)) {
          data.files = data.files
            .map((file) => getSnapshotFileRef(file, node.id))
            .filter(Boolean);
        }
        return {
          ...node,
          data,
        };
      }),
      edges,
      viewport,
    };
  }, [edges, nodes, reactFlowInstance]);

  const applyHistorySnapshot = useCallback(async (
    snapshot: GraphHistorySnapshot,
    direction: "undo" | "redo",
    sourceSnapshot: GraphHistorySnapshot,
  ) => {
    const nextNodes = cleanHistoryNodes(snapshot.nodes);
    const nextEdges = cleanHistoryEdges(snapshot.edges);

    markLocalWrite(1500);
    setNodes(nextNodes);
    setEdges(nextEdges);
    lastSeenUpdatedAtRef.current = snapshot.updated_at;
    nodeId = getNextNumericNodeId(nextNodes, 1);

    const selectedNodeId = selectedNodeIdRef.current;
    const refreshedSelection = selectedNodeId
      ? nextNodes.find((node) => node.id === selectedNodeId) || null
      : null;
    selectedNodeIdRef.current = refreshedSelection?.id ?? null;
    setSelectedNode(refreshedSelection);
    onNodeSelect(refreshedSelection, { openInspector: false });

    if (reactFlowInstance) {
      reactFlowInstance.setViewport(snapshot.viewport);
    }

    await restoreBackendGraphHistory(
      {
        nodes: nextNodes,
        edges: nextEdges,
        viewport: snapshot.viewport,
      },
      direction,
      {
        source_snapshot_fingerprint: graphHistoryFingerprint(sourceSnapshot.signature),
        target_snapshot_fingerprint: graphHistoryFingerprint(snapshot.signature),
        source_node_count: sourceSnapshot.nodes.length,
        source_edge_count: sourceSnapshot.edges.length,
        target_node_count: nextNodes.length,
        target_edge_count: nextEdges.length,
        source_snapshot_timestamp: new Date(sourceSnapshot.timestamp).toISOString(),
        target_snapshot_timestamp: new Date(snapshot.timestamp).toISOString(),
      },
    );
    onCanvasEdited?.();
  }, [markLocalWrite, onCanvasEdited, onNodeSelect, reactFlowInstance]);

  const undoGraphChange = useCallback(async () => {
    const snapshot = undoStackRef.current.pop();
    if (!snapshot) return;

    const sourceSnapshot = createHistorySnapshot();
    redoStackRef.current = [
      ...redoStackRef.current,
      sourceSnapshot,
    ].slice(-GRAPH_HISTORY_LIMIT);
    syncHistoryAvailability();

    try {
      setIsHistoryRestoring(true);
      await applyHistorySnapshot(snapshot, "undo", sourceSnapshot);
      toast.success("Undo applied", {
        description: "The previous graph snapshot has been restored.",
      });
    } catch (error) {
      console.error("[FlowCanvas.tsx] Undo failed:", error);
      toast.error("Undo failed", {
        description: error instanceof Error ? error.message : "Could not restore the previous graph.",
      });
    } finally {
      setIsHistoryRestoring(false);
      syncHistoryAvailability();
    }
  }, [applyHistorySnapshot, createHistorySnapshot, syncHistoryAvailability]);

  const redoGraphChange = useCallback(async () => {
    const snapshot = redoStackRef.current.pop();
    if (!snapshot) return;

    const sourceSnapshot = createHistorySnapshot();
    undoStackRef.current = [
      ...undoStackRef.current,
      sourceSnapshot,
    ].slice(-GRAPH_HISTORY_LIMIT);
    syncHistoryAvailability();

    try {
      setIsHistoryRestoring(true);
      await applyHistorySnapshot(snapshot, "redo", sourceSnapshot);
      toast.success("Redo applied", {
        description: "The next graph snapshot has been restored.",
      });
    } catch (error) {
      console.error("[FlowCanvas.tsx] Redo failed:", error);
      toast.error("Redo failed", {
        description: error instanceof Error ? error.message : "Could not restore the next graph.",
      });
    } finally {
      setIsHistoryRestoring(false);
      syncHistoryAvailability();
    }
  }, [applyHistorySnapshot, createHistorySnapshot, syncHistoryAvailability]);

  useImperativeHandle(ref, () => ({
    updateNode,
    syncFromBackend,
    getCurrentGraph,
    getCurrentVersionGraph: createSerializableFlow,
  }), [createSerializableFlow, getCurrentGraph, syncFromBackend, updateNode]);

  useEffect(() => {
    localStorage.setItem('ai-flow-nodes', JSON.stringify(nodes));
    localStorage.setItem('ai-flow-edges', JSON.stringify(edges));
  }, [nodes, edges]);

  useEffect(() => {
    localStorage.setItem('inlumen-show-port-details', String(showPortDetails));
  }, [showPortDetails]);

  useEffect(() => {
    if (onNodesChange) onNodesChange(nodes);
  }, [nodes, onNodesChange]);

  const onNodesChangeInternal = useCallback(
    (changes: NodeChange[]) => {
      const hasGraphEdit = changes.some((change) => (
        change.type !== 'select'
        && change.type !== 'dimensions'
        && change.type !== 'position'
      ));
      if (hasGraphEdit) {
        pushHistorySnapshot();
      }
      if (changes.some((change) => change.type !== 'select' && change.type !== 'dimensions')) {
        onCanvasEdited?.();
      }
      const removedNodeIds = changes
        .filter(change => change.type === 'remove')
        .map(change => change.id);

      removedNodeIds.forEach((id) => {
        markLocalWrite(800);
        deleteNodeFromBackend(id);
      });

      const newNodes = applyNodeChanges(changes, nodes);
      setNodes(newNodes);

      if (selectedNode) {
        const updatedSelectedNode = newNodes.find(n => n.id === selectedNode.id);
        if (updatedSelectedNode) {
          selectedNodeIdRef.current = updatedSelectedNode.id;
          setSelectedNode(updatedSelectedNode);
          onNodeSelect(updatedSelectedNode, { openInspector: false });
        }
      }
    },
    [nodes, selectedNode, onNodeSelect, markLocalWrite, onCanvasEdited, pushHistorySnapshot]
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      if (changes.some((change) => change.type !== 'select')) {
        onCanvasEdited?.();
      }
      const removedEdgeIds = changes
        .filter((c) => c.type === "remove")
        .map((c) => c.id);
      if (removedEdgeIds.length > 0) {
        pushHistorySnapshot();
        setEdges((eds) => {
          const removedEdges = eds.filter((e) => removedEdgeIds.includes(e.id));
          removedEdges.forEach((edge) => {
            const sourceNode = nodes.find((n) => n.id === edge.source);
            const targetNode = nodes.find((n) => n.id === edge.target);
            if (!sourceNode || !targetNode) {
              console.warn(
                "[FlowCanvas.tsx] Could not find source/target nodes for edge removal:",
                edge.id
              );
              return;
            }
            markLocalWrite(800);
            deleteEdgeFromBackend(sourceNode, targetNode, edge);
          });
          return applyEdgeChanges(changes, eds);
        });
        return;
      }
      setEdges((eds) => applyEdgeChanges(changes, eds));
    },
    [nodes, markLocalWrite, onCanvasEdited, pushHistorySnapshot]
  );

  const onConnect = useCallback(
    async (params: Connection) => {
      if (!params.source || !params.target) return;

      if (params.source === params.target) {
        toast("Cannot connect a node to itself", { description: "Please connect to a different node" });
        return;
      }

      const duplicate = edges.some((edge) =>
        edge.source === params.source &&
        edge.target === params.target &&
        (edge.sourceHandle ?? null) === (params.sourceHandle ?? null) &&
        (edge.targetHandle ?? null) === (params.targetHandle ?? null)
      );
      if (duplicate) {
        toast("Connection already exists", { description: "This connection is already in place" });
        return;
      }
      pushHistorySnapshot();
      setEdges((eds) => addEdge(params, eds));
      onCanvasEdited?.();

      // Find the actual Node objects
      const sourceNode = nodes.find((n) => n.id === params.source);
      const targetNode = nodes.find((n) => n.id === params.target);
      if (!sourceNode || !targetNode) {
        console.warn("[FlowCanvas.tsx] Could not find source/target nodes for edge creation.");
        return;
      }
      markLocalWrite(800);
      await addEdgeToBackend(sourceNode, targetNode, params);
    },
    [edges, nodes, markLocalWrite, onCanvasEdited, pushHistorySnapshot]
  );

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    selectedNodeIdRef.current = node.id;
    setSelectedNode(node);
    onNodeSelect(node);
  }, [onNodeSelect]);

  const onPaneClick = useCallback(() => {
    selectedNodeIdRef.current = null;
    setSelectedNode(null);
    onNodeSelect(null);
  }, [onNodeSelect]);

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();

      const reactFlowBounds = reactFlowWrapper.current?.getBoundingClientRect();
      const nodeData = JSON.parse(event.dataTransfer.getData('application/reactflow'));

      if (!reactFlowBounds || !reactFlowInstance) return;

      const position = reactFlowInstance.project({
        x: event.clientX - reactFlowBounds.left,
        y: event.clientY - reactFlowBounds.top,
      });

      const newNode = {
        id: `${nodeId++}`,
        type: nodeData.type,
        position,
        data: {
          ...nodeData.data,
          ...(nodeData.data.type === 'source' ? { content: '{input}' } : {}),
        },
      };

      pushHistorySnapshot();
      onCanvasEdited?.();
      setNodes((nds) => {
        const updated = nds.concat(newNode);
        markLocalWrite(800);
        addNodeToBackend(newNode);
        return updated;
      });
    },
    [reactFlowInstance, markLocalWrite, onCanvasEdited, pushHistorySnapshot]
  );

  const openSaveVersionDialog = async () => {
    try {
      const versions = await fetchPipelineVersions();
      const savedVersionCount = versions.filter((version) => !version.is_main).length;
      setVersionName(`Version ${savedVersionCount + 1}`);
    } catch {
      setVersionName(`Version ${new Date().toISOString().slice(0, 19).replace("T", " ")}`);
    }
    setIsSaveVersionOpen(true);
  };

  const saveFlow = async () => {
    try {
      if (!reactFlowInstance) return;
      const trimmedName = versionName.trim();
      if (!trimmedName) {
        toast.error("Version name is required");
        return;
      }
      setIsSavingVersion(true);
      const flow = createSerializableFlow();
      const savedVersion = await savePipelineVersion(trimmedName, flow);
      localStorage.setItem('ai-flow', JSON.stringify(flow));
      markLocalWrite(1200);
      setIsSaveVersionOpen(false);
      onVersionSaved?.(savedVersion);
      toast.success('Version saved', {
        description: savedVersion.name,
      });
    } catch (error) {
      console.error('Error saving flow:', error);
      toast.error('Failed to save flow', {
        description: 'There was an error saving your pipeline',
      });
    } finally {
      setIsSavingVersion(false);
    }
  };

  const exportFlow = () => {
    try {
      if (reactFlowInstance) {
        const flow = reactFlowInstance.toObject();
        downloadJsonFile(flow, 'inlumen-flow.json');

        toast.success('Flow exported successfully', {
          description: 'Your AI pipeline has been exported as JSON',
        });
      }
    } catch (error) {
      console.error('Error exporting flow:', error);
      toast.error('Failed to export flow', {
        description: 'There was an error exporting your pipeline',
      });
    }
  };

  const handleGeneratePipelineScripts = () => {
    if (isGeneratingScripts) return;
    setScriptGenerationPrompt(String(pipelinePrompt || "").trim());
    setScriptGenerationMode(uploadedSampleDataAvailable ? "full" : "generic");
    setIsScriptGenerationOpen(true);
  };

  const pollPipelineGenerationRun = async (started: PipelineGenerationJob) => {
    setGenerationJob(started);
    const runId = String(started.run_id || "").trim();
    if (!runId) {
      throw new Error("Pipeline generation run did not return a run id.");
    }
    let latest = started;
    while (
      !GENERATION_TERMINAL_STATUSES.has(effectiveGenerationStatus(latest))
    ) {
      await wait(3000);
      latest = await fetchPipelineScriptGenerationRun(runId);
      setGenerationJob(latest);
    }
    return latest;
  };

  const handleRunPipelineScriptGeneration = async () => {
    if (isGeneratingScripts) return;
    const mode =
      scriptGenerationMode === "full" && !uploadedSampleDataAvailable
        ? "generic"
        : scriptGenerationMode;
    const options = {
      ...modeToGenerationOptions(mode, uploadedSampleDataAvailable),
      userInstruction: scriptGenerationPrompt.trim(),
    };
    generationCancelRequestedRef.current = false;
    setIsGeneratingScripts(true);
    setGenerationJob(null);
    try {
      markLocalWrite(5000);
      let generatedCount = 0;
      if (mode === "full") {
        const started = await startPipelineScriptGenerationRun(
          activeChatbotConfig,
          options,
        );
        const latest = await pollPipelineGenerationRun(started);
        if (
          latest.status !== "valid" ||
          latest.persistence?.status !== "persisted"
        ) {
          throw new Error(generationFailureMessage(latest));
        }
        const persistedResult = latest.persistence?.result as { nodes?: unknown[] } | undefined;
        generatedCount = Array.isArray(persistedResult?.nodes)
          ? persistedResult.nodes.length
          : 0;
      } else {
        const result = await generatePipelineScripts(activeChatbotConfig, options);
        generatedCount = Array.isArray(result?.nodes) ? result.nodes.length : 0;
      }
      await fetchGraphAndApply();
      setIsScriptGenerationOpen(false);
      toast.success("Runtime scripts generated", {
        description: `${generatedCount} node bundle${generatedCount === 1 ? "" : "s"} generated.`,
      });
    } catch (error) {
      if (generationCancelRequestedRef.current) {
        return;
      }
      console.error("[FlowCanvas.tsx] Generate pipeline scripts error:", error);
      toast.error("Script generation failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsGeneratingScripts(false);
    }
  };

  const handleExternalRuntimePrompt = async () => {
    if (isPreparingExternalPrompt) return;
    setIsPreparingExternalPrompt(true);
    try {
      const result = await prepareExternalRuntimePrompt(
        scriptGenerationPrompt.trim(),
      );
      const prompt = String(result.prompt || "").trim();
      if (!prompt) {
        throw new Error("The prepared external AI prompt was empty.");
      }
      await copyTextToClipboard(prompt);
      toast.success("Prompt copied", {
        description: `Paste it into any AI for ${result.node_count || nodes.length} pipeline node${(result.node_count || nodes.length) === 1 ? "" : "s"}.`,
      });
    } catch (error) {
      console.error("[FlowCanvas.tsx] Prepare external AI prompt error:", error);
      toast.error("Could not prepare external AI prompt", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsPreparingExternalPrompt(false);
    }
  };

  const handleRepairFailedPipelineNode = async () => {
    if (isGeneratingScripts) return;
    const currentRunId = String(generationJob?.run_id || "").trim();
    const failedStep = failedGenerationStep(generationJob);
    const failedFlowId = String(failedStep?.flow_id || "").trim();
    if (!currentRunId || !failedFlowId) {
      toast.error("Repair unavailable", {
        description: "No failed node was found for this generation run.",
      });
      return;
    }
    setIsGeneratingScripts(true);
    generationCancelRequestedRef.current = false;
    try {
      markLocalWrite(5000);
      const started = await resumePipelineScriptGenerationRun(
        currentRunId,
        activeChatbotConfig,
        {
          flowId: failedFlowId,
          repairAttempts: 7,
        },
      );
      const latest = await pollPipelineGenerationRun(started);
      if (
        latest.status !== "valid" ||
        latest.persistence?.status !== "persisted"
      ) {
        throw new Error(generationFailureMessage(latest));
      }
      const persistedResult = latest.persistence?.result as { nodes?: unknown[] } | undefined;
      const generatedCount = Array.isArray(persistedResult?.nodes)
        ? persistedResult.nodes.length
        : 0;
      await fetchGraphAndApply();
      setIsScriptGenerationOpen(false);
      toast.success("Failed node repaired", {
        description: `${generatedCount} node bundle${generatedCount === 1 ? "" : "s"} persisted.`,
      });
    } catch (error) {
      if (generationCancelRequestedRef.current) {
        return;
      }
      console.error("[FlowCanvas.tsx] Repair pipeline script error:", error);
      toast.error("Node repair failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsGeneratingScripts(false);
    }
  };

  const handleCancelPipelineScriptGeneration = async () => {
    if (!isGeneratingScripts) {
      setIsScriptGenerationOpen(false);
      return;
    }
    const runId = String(
      generationJob?.run_id || generationJob?.generation_run?.run_id || "",
    ).trim();
    if (!runId || isCancellingScripts) return;

    generationCancelRequestedRef.current = true;
    setIsCancellingScripts(true);
    try {
      const cancelled = await cancelPipelineScriptGenerationRun(runId);
      setGenerationJob(cancelled);
      setIsGeneratingScripts(false);
      toast.info("Runtime script generation stopped", {
        description: `Run ${runId.slice(0, 8)} was cancelled.`,
      });
    } catch (error) {
      generationCancelRequestedRef.current = false;
      toast.error("Could not stop generation", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsCancellingScripts(false);
    }
  };

  const importFlow = async (e: React.ChangeEvent<HTMLInputElement>) => {
    try {
      const file = e.target.files?.[0];
      if (!file) return;
      const text = await file.text();
      const flowData = JSON.parse(text) as {
        nodes?: Node[];
        edges?: Edge[];
      };
      if (!Array.isArray(flowData.nodes) || !Array.isArray(flowData.edges)) {
        toast.error('Invalid flow file', {
          description: 'The selected file does not contain a valid flow',
        });
        return;
      }
      const normalizedImport = normalizeGraph(flowData);
      const importedNodes = normalizedImport.nodes;
      const importedEdges = normalizedImport.edges;
      pushHistorySnapshot();
      onCanvasEdited?.();
      markLocalWrite(1200); // avoid immediate poll-refresh
      await rebuildBackendFromFlow(importedNodes, importedEdges);
      setNodes(importedNodes);
      setEdges(importedEdges);
      nodeId = getNextNumericNodeId(importedNodes, 1);
      toast.success('Flow imported successfully', {
        description: 'Imported flow and backend state reconstructed',
      });
    } catch (error) {
      console.error('Error importing flow:', error);
      toast.error('Failed to import flow', {
        description: 'There was an error importing your pipeline',
      });
    } finally {
      if (e.target) e.target.value = '';
    }
  };

  const clearCanvas = async () => {
    pushHistorySnapshot();
    onCanvasEdited?.();
    setNodes([]);
    setEdges([]);
    selectedNodeIdRef.current = null;
    setSelectedNode(null);
    onNodeSelect(null);
    localStorage.removeItem('ai-flow');
    localStorage.removeItem('ai-flow-nodes');
    localStorage.removeItem('ai-flow-edges');
    nodeId = 1;
    markLocalWrite(1200);
    await rebuildBackendFromFlow([], []);
    toast.success('Canvas cleared', {
      description: 'All nodes and edges have been removed',
    });
  };

  const generationSteps = generationJob?.generation_run?.steps || [];
  const generationProgress = generationProgressPercent(generationJob);
  const generationStatus = effectiveGenerationStatus(generationJob);
  const generationFailed = ["invalid", "failed"].includes(generationStatus);
  const repairableFailedStep = failedGenerationStep(generationJob);
  const canRepairFailedNode = Boolean(
    generationJob &&
      generationFailed &&
      repairableFailedStep?.flow_id &&
      generationJob.result,
  );

  return (
    <div ref={reactFlowWrapper} className="h-full w-full">
      <PortDisplayContext.Provider value={showPortDetails}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChangeInternal}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        onInit={setReactFlowInstance}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onNodeDragStart={() => {
          dragStartSnapshotRef.current = createHistorySnapshot();
        }}
        onNodeDragStop={(_, node) => {
          const dragStartSnapshot = dragStartSnapshotRef.current;
          dragStartSnapshotRef.current = null;
          if (dragStartSnapshot) {
            const finalNodes = nodes.map((currentNode) => (
              currentNode.id === node.id
                ? { ...currentNode, position: node.position }
                : currentNode
            ));
            if (dragStartSnapshot.signature !== graphHistorySignature(finalNodes, edges)) {
              pushHistorySnapshot(dragStartSnapshot);
            }
          }
          onCanvasEdited?.();
          markLocalWrite(800);
          updateNodePositionInBackend(node);
        }}
        nodeTypes={nodeTypes}
        defaultEdgeOptions={{
          type: 'smoothstep',
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 12,
            height: 12,
            color: 'hsl(var(--muted-foreground))',
          },
        }}
        connectionLineType={ConnectionLineType.SmoothStep}
        fitView
        fitViewOptions={{ padding: 0.28 }}
        className={cn(
          "flow-canvas transition-colors duration-300",
          isLightMode ? "bg-stone-50" : "bg-[#0F1C0F]"
        )}
      >
        <Controls className="bg-card border border-border rounded-md p-1" />

        <MiniMap
          nodeColor={n => {
            switch (n.data.type) {
              case 'source': return '#3B82F6';
              case 'task': return '#F59E0B';
              case 'sink': return '#10B981';
              case 'flow': return '#A855F7';
              case 'subpipeline': return '#06B6D4';
              default: return '#6B7280';
            }
          }}
          maskColor="rgba(0, 0, 0, 0.1)"
          className="bg-card/70 border border-border rounded-md"
        />

        <FlowCanvasActionsPanel
          fileInputRef={fileInputRef}
          onSave={openSaveVersionDialog}
          onUndo={() => { void undoGraphChange(); }}
          onRedo={() => { void redoGraphChange(); }}
          onExportJson={exportFlow}
          onImportClick={triggerImport}
          onImport={importFlow}
          onGenerateScripts={handleGeneratePipelineScripts}
          isGeneratingScripts={isGeneratingScripts}
          showPortDetails={showPortDetails}
          onTogglePortDetails={() => setShowPortDetails((current) => !current)}
          onClear={clearCanvas}
          canUndo={historyAvailability.canUndo}
          canRedo={historyAvailability.canRedo}
          isHistoryRestoring={isHistoryRestoring}
        />
      </ReactFlow>
      </PortDisplayContext.Provider>

      <Dialog
        open={isScriptGenerationOpen}
        onOpenChange={(open) => {
          if (!isGeneratingScripts) setIsScriptGenerationOpen(open);
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Add Node Scripts</DialogTitle>
            <DialogDescription>
              Every node follows the same simple file rule, whether the files
              come from you, another AI, or inLUMEN.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-2 rounded-md border border-border bg-muted/20 p-3 text-sm">
              <p className="font-medium">Files to attach to each node</p>
              <div className="grid gap-1 text-muted-foreground">
                <p><code className="text-foreground">main.py</code> — the node script</p>
                <p><code className="text-foreground">requirements.txt</code> — only when packages are needed</p>
                <p><span className="text-foreground">Input files</span> — upload them to the first node that reads them</p>
              </div>
              <p className="text-xs text-muted-foreground">
                That is all. inLUMEN builds the Dagster setup and passes files
                between connected nodes automatically.
              </p>
              <p className="text-xs text-muted-foreground">
                To upload: select a node, open Inspector, and choose Upload Files.
              </p>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="pipeline-runtime-prompt">
                What should the pipeline do?
              </Label>
              <Textarea
                id="pipeline-runtime-prompt"
                value={scriptGenerationPrompt}
                onChange={(event) => setScriptGenerationPrompt(event.target.value)}
                placeholder="Describe the result you want."
                disabled={isGeneratingScripts}
                className="min-h-24"
              />
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between gap-3">
                <Label>Generation mode</Label>
                <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  {uploadedSampleDataAvailable ? (
                    <>
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                      Input data detected
                    </>
                  ) : (
                    <>
                      <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
                      No input data attached
                    </>
                  )}
                </span>
              </div>
              <RadioGroup
                value={scriptGenerationMode}
                onValueChange={(value) =>
                  setScriptGenerationMode(value as PipelineScriptGenerationMode)
                }
                className="grid gap-2"
              >
                {generationModeOptions.map((option) => {
                  const Icon = option.icon;
                  const disabled =
                    option.value === "full" && !uploadedSampleDataAvailable;
                  return (
                    <Label
                      key={option.value}
                      htmlFor={`script-generation-${option.value}`}
                      className={cn(
                        "flex cursor-pointer items-start gap-3 rounded-md border border-border p-3 transition-colors",
                        scriptGenerationMode === option.value &&
                          "border-primary bg-primary/10",
                        disabled && "cursor-not-allowed opacity-50",
                      )}
                    >
                      <RadioGroupItem
                        id={`script-generation-${option.value}`}
                        value={option.value}
                        disabled={disabled || isGeneratingScripts}
                        className="mt-1"
                      />
                      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                      <span className="grid gap-1">
                        <span className="font-medium">{option.title}</span>
                        <span className="text-sm font-normal text-muted-foreground">
                          {option.description}
                        </span>
                      </span>
                    </Label>
                  );
                })}
              </RadioGroup>
              {!uploadedSampleDataAvailable && (
                <p className="text-xs text-muted-foreground">
                  Attach an input file to its first consumer node to enable Full data-aware.
                </p>
              )}
            </div>

            <div className="space-y-3 rounded-md border border-border bg-muted/20 p-3">
              <div className="space-y-1">
                <p className="text-sm font-medium">Using ChatGPT or another AI?</p>
                <p className="text-xs text-muted-foreground">
                  Copy one ready-made prompt, paste it into the AI, then upload
                  the returned files to the matching nodes. The response also
                  tells you exactly which node receives each real input file.
                </p>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => { void handleExternalRuntimePrompt(); }}
                disabled={isPreparingExternalPrompt || isGeneratingScripts}
              >
                {isPreparingExternalPrompt ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Copy className="mr-2 h-4 w-4" />
                )}
                Copy prompt
              </Button>
            </div>

            {generationJob && (
              <div className="space-y-3 rounded-md border border-border p-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="font-medium">
                    Run {String(generationJob.run_id || "").slice(0, 8)}
                  </span>
                  <span className="text-muted-foreground">
                    {generationStatus}
                  </span>
                </div>
                <Progress value={generationProgress} />
                {generationSteps.length > 0 && (
                  <div className="max-h-40 space-y-2 overflow-auto pr-1">
                    {generationSteps.map((step) => (
                      <div
                        key={`${step.flow_id}-${step.stage}`}
                        className="flex items-center justify-between gap-3 text-sm"
                      >
                        <span className="truncate">
                          Node {step.flow_id || "?"} - {generationStageLabel(step.stage)}
                        </span>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {step.status || "pending"}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {generationFailed && (
                  <div
                    role="alert"
                    className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                  >
                    {generationFailureMessage(generationJob)}
                  </div>
                )}
              </div>
            )}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => { void handleCancelPipelineScriptGeneration(); }}
              disabled={isCancellingScripts}
            >
              {isCancellingScripts && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              )}
              {isGeneratingScripts ? "Stop generation" : "Cancel"}
            </Button>
            {canRepairFailedNode && (
              <Button
                variant="outline"
                onClick={() => { void handleRepairFailedPipelineNode(); }}
                disabled={isGeneratingScripts}
              >
                {isGeneratingScripts && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                Retry Node {repairableFailedStep?.flow_id}
              </Button>
            )}
            <Button
              onClick={() => { void handleRunPipelineScriptGeneration(); }}
              disabled={
                isGeneratingScripts ||
                (scriptGenerationMode === "full" && !uploadedSampleDataAvailable)
              }
            >
              {isGeneratingScripts && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {isGeneratingScripts
                ? "Generating"
                : generationFailed
                  ? "Retry all"
                  : "Generate and attach"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isSaveVersionOpen} onOpenChange={setIsSaveVersionOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Save Version</DialogTitle>
            <DialogDescription>
              Name this pipeline snapshot before saving it.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-2">
            <Label htmlFor="pipeline-version-name">Version name</Label>
            <Input
              id="pipeline-version-name"
              value={versionName}
              onChange={(event) => setVersionName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void saveFlow();
                }
              }}
              autoFocus
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setIsSaveVersionOpen(false)}
              disabled={isSavingVersion}
            >
              Cancel
            </Button>
            <Button
              onClick={() => { void saveFlow(); }}
              disabled={isSavingVersion || !versionName.trim()}
            >
              {isSavingVersion ? "Saving..." : "Save Version"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
});

interface WrappedFlowCanvasProps extends FlowCanvasProps {
  flowCanvasRef?: React.RefObject<FlowCanvasRef>;
}

export const WrappedFlowCanvas = ({
  onNodeSelect,
  onNodesChange,
  onRemoveNode,
  onRemoveEdge,
  isLightMode,
  activeChatbotConfig,
  pipelinePrompt,
  onVersionSaved,
  onCanvasEdited,
  onActiveVersionChange,
  onActiveVersionNameChange,
  onPipelineDescriptionChange,
  flowCanvasRef,
}: WrappedFlowCanvasProps) => (
  <ReactFlowProvider>
    <FlowCanvas
      ref={flowCanvasRef}
      onNodeSelect={onNodeSelect}
      onNodesChange={onNodesChange}
      onRemoveNode={onRemoveNode}
      onRemoveEdge={onRemoveEdge}
      isLightMode={isLightMode}
      activeChatbotConfig={activeChatbotConfig}
      pipelinePrompt={pipelinePrompt}
      onVersionSaved={onVersionSaved}
      onCanvasEdited={onCanvasEdited}
      onActiveVersionChange={onActiveVersionChange}
      onActiveVersionNameChange={onActiveVersionNameChange}
      onPipelineDescriptionChange={onPipelineDescriptionChange}
    />
  </ReactFlowProvider>
);
