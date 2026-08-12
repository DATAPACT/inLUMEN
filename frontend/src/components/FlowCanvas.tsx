import React, { useState, useCallback, useRef, useEffect, forwardRef, useImperativeHandle, useMemo } from 'react';
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
  clearPipelineScriptGenerationRuns,
  deleteEdgeFromBackend,
  deleteNodeFromBackend,
  fetchPipelineScriptGenerationRun,
  listPipelineScriptGenerationRuns,
  preparePipelineScriptGeneration,
  fetchPipelineVersions,
  fetchPipelineGraph,
  fetchPipelineUpdatedAt,
  restoreBackendGraphHistory,
  resumePipelineScriptGenerationRun,
  restorePipelineVersion,
  startPipelineScriptGenerationRun,
  type PipelineVersionGraph,
  type PipelineVersionSummary,
  type PipelineGenerationJob,
  type PipelineGenerationPreflight,
  type PipelineScriptGenerationMode,
  type PipelineScriptGenerationScope,
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
import { createProjectDocument, projectDocumentToGraph } from '@/features/flow/projectIr';
import {
  effectiveGenerationStatus,
  GENERATION_TERMINAL_STATUSES,
  generationRunId,
  isRestorableGenerationRun,
} from '@/features/flow/generationState';
import {
  getValidationIssueSubject,
  validateGraph,
  type ValidationIssue,
} from '@/features/flow/flowValidation';
import { normalizeNodePorts, normalizeType } from '@/features/nodes/nodeSchema';
import { remapSubpipelineParentEdges } from '@/features/flow/subpipeline';
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
  onDisplayModeChange?: (advanced: boolean) => void;
  workspaceResetKey?: number;
}

export interface FlowCanvasRef {
  updateNode: (
    id: string,
    data: Record<string, unknown>,
    options?: { remapSubpipeline?: boolean },
  ) => void;
  syncFromBackend: (graphData?: unknown) => Promise<NormalizedGraph>;
  getCurrentGraph: () => AgentGraphSnapshot;
  getCurrentVersionGraph: () => PipelineVersionGraph;
  openCodeGeneration: (selectedFlowIds?: string[]) => void;
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
    value: "draft",
    title: "Draft",
    description: "Generate packages and run static syntax, dependency, manifest, and contract checks.",
    icon: Zap,
  },
  {
    value: "validated",
    title: "Validated",
    description: "Execute with attached sample inputs and automatically repair validation failures.",
    icon: Database,
  },
];

const modeToGenerationOptions = (
  mode: PipelineScriptGenerationMode,
  hasInputFiles: boolean,
) => {
  if (mode === "draft") {
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
    includeSampleData: hasInputFiles,
    validationMode: "pipeline_sample" as const,
    generationStrategy: "single_pass" as const,
    allowDeterministicFallback: false,
    repairAttempts: 4,
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

const ACTIVE_GENERATION_RUN_STORAGE_KEY = "inlumen-active-codegen-run-id";

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
    reused_validated_bundle: "reused validated package",
    validated_cache_hit: "reused validated result",
    complete: "complete",
    cancelled: "cancelled",
  };
  return labels[key] || key.replace(/_/g, " ");
};

const generationStatusLabel = (status: unknown) => {
  const key = String(status || "running").toLowerCase();
  return {
    queued: "Queued",
    running: "In progress",
    valid: "Validated",
    invalid: "Validation failed",
    failed: "Failed",
    cancelled: "Stopped",
  }[key] || key.replace(/_/g, " ");
};

const formatGenerationCost = (cost: number | null | undefined) => {
  if (typeof cost !== "number" || !Number.isFinite(cost)) return "Not reported";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: cost > 0 && cost < 0.01 ? 4 : 2,
    maximumFractionDigits: cost > 0 && cost < 0.0001 ? 8 : cost > 0 && cost < 0.01 ? 6 : 2,
  }).format(cost);
};

const formatGenerationTokens = (tokens: number | null | undefined) => {
  if (typeof tokens !== "number" || !Number.isFinite(tokens)) return "Not reported";
  return new Intl.NumberFormat("en-US").format(tokens);
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
  onDisplayModeChange,
  workspaceResetKey = 0,
}, ref) => {
  const [nodes, setNodes] = useState<Node[]>(() => {
    const savedNodes = localStorage.getItem('ai-flow-nodes');
    return savedNodes ? JSON.parse(savedNodes) : [];
  });
  const [edges, setEdges] = useState<Edge[]>(() => {
    const savedEdges = localStorage.getItem('ai-flow-edges');
    return savedEdges ? JSON.parse(savedEdges) : [];
  });
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
  const handledGenerationRunsRef = useRef(new Set<string>());
  const [isScriptGenerationOpen, setIsScriptGenerationOpen] = useState(false);
  const [isValidationOpen, setIsValidationOpen] = useState(false);
  const [scriptGenerationPrompt, setScriptGenerationPrompt] = useState("");
  const [scriptGenerationMode, setScriptGenerationMode] =
    useState<PipelineScriptGenerationMode>("validated");
  const [scriptGenerationScope, setScriptGenerationScope] =
    useState<PipelineScriptGenerationScope>("missing_changed");
  const [scriptGenerationSelectedFlowIds, setScriptGenerationSelectedFlowIds] =
    useState<string[]>([]);
  const [generationPreflight, setGenerationPreflight] =
    useState<PipelineGenerationPreflight | null>(null);
  const [isGenerationPreflightLoading, setIsGenerationPreflightLoading] =
    useState(false);
  const generationPreflightRequestRef = useRef(0);
  const [generationPreflightError, setGenerationPreflightError] = useState("");
  const [overwriteProtectedCode, setOverwriteProtectedCode] = useState(false);
  const [recentGenerationRuns, setRecentGenerationRuns] =
    useState<PipelineGenerationJob[]>([]);
  const [isCreatingGenerationCheckpoint, setIsCreatingGenerationCheckpoint] =
    useState(false);
  const [isRestoringGenerationCheckpoint, setIsRestoringGenerationCheckpoint] =
    useState(false);
  const [generationJob, setGenerationJob] = useState<PipelineGenerationJob | null>(null);
  const generationHistoryResetVersionRef = useRef(0);
  const resetGenerationHistoryState = useCallback(() => {
    generationHistoryResetVersionRef.current += 1;
    generationCancelRequestedRef.current = false;
    handledGenerationRunsRef.current.clear();
    generationPreflightRequestRef.current += 1;
    localStorage.removeItem(ACTIVE_GENERATION_RUN_STORAGE_KEY);
    setGenerationJob(null);
    setRecentGenerationRuns([]);
    setIsGeneratingScripts(false);
    setIsCancellingScripts(false);
    setIsScriptGenerationOpen(false);
    setGenerationPreflight(null);
    setGenerationPreflightError("");
    setOverwriteProtectedCode(false);
  }, []);
  const previousWorkspaceResetKeyRef = useRef(workspaceResetKey);
  useEffect(() => {
    if (previousWorkspaceResetKeyRef.current === workspaceResetKey) return;
    previousWorkspaceResetKeyRef.current = workspaceResetKey;
    resetGenerationHistoryState();
  }, [resetGenerationHistoryState, workspaceResetKey]);
  const rememberGenerationJob = useCallback((job: PipelineGenerationJob) => {
    setGenerationJob(job);
    const runId = generationRunId(job);
    const status = effectiveGenerationStatus(job);
    const awaitingPersistence = status === "valid" && job.persistence?.status === "pending";
    const complete = GENERATION_TERMINAL_STATUSES.has(status) && !awaitingPersistence;
    if (runId) {
      setRecentGenerationRuns((current) => [
        job,
        ...current.filter((item) => generationRunId(item) !== runId),
      ].slice(0, 8));
    }
    if (runId) {
      if (complete) {
        localStorage.removeItem(ACTIVE_GENERATION_RUN_STORAGE_KEY);
      } else {
        localStorage.setItem(ACTIVE_GENERATION_RUN_STORAGE_KEY, runId);
      }
    }
    setIsGeneratingScripts(!complete);
  }, []);
  const activeGenerationRunId = String(
    generationJob?.run_id || generationJob?.generation_run?.run_id || "",
  ).trim();
  const activeGenerationStatus = effectiveGenerationStatus(generationJob);
  const activeGenerationPersistenceStatus = generationJob?.persistence?.status;
  const uploadedSampleDataAvailable = hasUploadedSampleData(nodes);
  const designValidation = useMemo(
    () => validateGraph(nodes, edges, { mode: "draft" }),
    [edges, nodes],
  );
  const displayedNodes = useMemo(() => nodes.map((node) => ({
    ...node,
    data: {
      ...node.data,
      validation_issues: designValidation.byNode[String(node.id)] || [],
      connected_ports: {
        inputs: edges
          .filter((edge) => String(edge.target) === String(node.id) && edge.targetHandle)
          .map((edge) => String(edge.targetHandle)),
        outputs: edges
          .filter((edge) => String(edge.source) === String(node.id) && edge.sourceHandle)
          .map((edge) => String(edge.sourceHandle)),
      },
    },
  })), [designValidation.byNode, edges, nodes]);
  const displayedNodesRef = useRef(displayedNodes);
  displayedNodesRef.current = displayedNodes;
  const displayedEdges = useMemo(() => edges.map((edge) => {
    const issues = designValidation.issues.filter((issue) => issue.edgeId === String(edge.id || ""));
    const hasError = issues.some((issue) => issue.severity === "error");
    const hasWarning = issues.some((issue) => issue.severity === "warning");
    if (!hasError && !hasWarning) return edge;
    const color = hasError ? "#ef4444" : "#f59e0b";
    return {
      ...edge,
      style: { ...edge.style, stroke: color, strokeWidth: 2.5 },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 12,
        height: 12,
        color,
      },
    };
  }), [designValidation.issues, edges]);
  const validationErrors = designValidation.issues.filter((issue) => issue.severity === "error").length;
  const validationWarnings = designValidation.issues.filter((issue) => issue.severity === "warning").length;
  const selectedNodeFeedbackSignature = (() => {
    const selectedNodeId = selectedNodeIdRef.current;
    if (!selectedNodeId) return "";
    const selected = displayedNodes.find((node) => node.id === selectedNodeId);
    return selected ? JSON.stringify({
      validation_issues: selected.data.validation_issues,
      connected_ports: selected.data.connected_ports,
    }) : "missing";
  })();

  // Keep inspector feedback current without replacing its editable node data on
  // every keystroke. This only runs when validation or connection state changes.
  useEffect(() => {
    const selectedNodeId = selectedNodeIdRef.current;
    if (!selectedNodeId || !selectedNodeFeedbackSignature) return;
    const refreshedSelection = displayedNodesRef.current.find((node) => node.id === selectedNodeId) || null;
    onNodeSelect(refreshedSelection, { openInspector: false });
  }, [onNodeSelect, selectedNodeFeedbackSignature]);

  useEffect(() => {
    if (!uploadedSampleDataAvailable && scriptGenerationMode === "validated") {
      setScriptGenerationMode("draft");
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

  useEffect(() => {
    let disposed = false;
    const restoreGenerationRun = async () => {
      const resetVersion = generationHistoryResetVersionRef.current;
      const rememberedRunId = String(
        localStorage.getItem(ACTIVE_GENERATION_RUN_STORAGE_KEY) || "",
      ).trim();
      let restored: PipelineGenerationJob | null = null;
      if (rememberedRunId) {
        try {
          restored = await fetchPipelineScriptGenerationRun(rememberedRunId);
        } catch (error) {
          console.warn(
            "[FlowCanvas.tsx] Could not restore remembered generation run:",
            error,
          );
        }
      }
      if (restored && !isRestorableGenerationRun(restored)) {
        localStorage.removeItem(ACTIVE_GENERATION_RUN_STORAGE_KEY);
        restored = null;
      }
      if (!restored) {
        try {
          const runs = await listPipelineScriptGenerationRuns(20);
          restored = runs.find(isRestorableGenerationRun) || null;
        } catch (error) {
          console.warn(
            "[FlowCanvas.tsx] Could not discover background generation runs:",
            error,
          );
        }
      }
      if (
        !disposed
        && restored
        && resetVersion === generationHistoryResetVersionRef.current
      ) {
        rememberGenerationJob(restored);
      }
    };
    void restoreGenerationRun();
    return () => {
      disposed = true;
    };
  }, [rememberGenerationJob]);

  useEffect(() => {
    const runId = activeGenerationRunId;
    if (!runId) return;
    const status = activeGenerationStatus;
    const needsFinalization =
      status === "valid" && activeGenerationPersistenceStatus === "pending";
    if (GENERATION_TERMINAL_STATUSES.has(status) && !needsFinalization) return;

    let disposed = false;
    let requestInFlight = false;
    const resetVersion = generationHistoryResetVersionRef.current;
    const refresh = async () => {
      if (requestInFlight) return;
      requestInFlight = true;
      try {
        const latest = await fetchPipelineScriptGenerationRun(runId);
        if (
          !disposed
          && resetVersion === generationHistoryResetVersionRef.current
        ) {
          rememberGenerationJob(latest);
        }
      } catch (error) {
        if (!disposed) {
          console.warn("[FlowCanvas.tsx] Generation progress refresh failed:", error);
        }
      } finally {
        requestInFlight = false;
      }
    };
    void refresh();
    const intervalId = window.setInterval(() => {
      void refresh();
    }, 3000);
    return () => {
      disposed = true;
      window.clearInterval(intervalId);
    };
  }, [
    activeGenerationPersistenceStatus,
    activeGenerationRunId,
    activeGenerationStatus,
    rememberGenerationJob,
  ]);

  useEffect(() => {
    if (!generationJob) return;
    const runId = String(
      generationJob.run_id || generationJob.generation_run?.run_id || "",
    ).trim();
    const status = effectiveGenerationStatus(generationJob);
    if (!runId || !GENERATION_TERMINAL_STATUSES.has(status)) return;
    if (status === "valid" && generationJob.persistence?.status === "pending") return;
    setIsGeneratingScripts(false);
    if (handledGenerationRunsRef.current.has(runId)) return;
    handledGenerationRunsRef.current.add(runId);

    if (status === "valid" && generationJob.persistence?.status === "persisted") {
      const persistedResult = generationJob.persistence?.result as
        | { nodes?: unknown[] }
        | undefined;
      const generatedCount = Array.isArray(persistedResult?.nodes)
        ? persistedResult.nodes.length
        : 0;
      markLocalWrite(5000);
      void fetchGraphAndApply()
        .then(() => {
          toast.success("Runtime scripts generated", {
            description: `${generatedCount} node bundle${generatedCount === 1 ? "" : "s"} generated in the background.`,
          });
        })
        .catch((error) => {
          console.error("[FlowCanvas.tsx] Generated graph refresh failed:", error);
          toast.error("Scripts generated, but the graph could not be refreshed", {
            description: error instanceof Error ? error.message : "Unknown error",
          });
        });
      return;
    }
    if (status !== "cancelled") {
      toast.error("Script generation failed", {
        description: generationFailureMessage(generationJob),
      });
    }
  }, [fetchGraphAndApply, generationJob, markLocalWrite]);

  // Expose updateNode 
  const updateNode = useCallback((
    id: string,
    data: Record<string, unknown>,
    options?: { remapSubpipeline?: boolean },
  ) => {
    pushHistorySnapshot(undefined, { coalesceKey: `node:${id}:properties` });
    onCanvasEdited?.();
    markLocalWrite(1200);
    const existingNode = nodes.find((node) => String(node.id) === String(id));
    const isConfiguredSubpipeline = existingNode
      && normalizeType(data.type ?? existingNode.data?.type) === "subpipeline"
      && data.ports;
    const nextNode = existingNode
      ? { ...existingNode, data: { ...existingNode.data, ...data } }
      : null;
    setNodes((nds) =>
      nds.map((node) => {
        if (node.id === id) {
          return { ...node, data: { ...node.data, ...data } };
        }
        return node;
      })
    );
    if (isConfiguredSubpipeline && nextNode && options?.remapSubpipeline !== false) {
      const previousPorts = normalizeNodePorts(existingNode.data?.ports, "subpipeline");
      const nextPorts = normalizeNodePorts(data.ports, "subpipeline");
      const remappedEdges = remapSubpipelineParentEdges(id, edges, previousPorts, nextPorts);
      const changedEdges = remappedEdges.flatMap((edge, index) => (
        edge.sourceHandle !== edges[index].sourceHandle
        || edge.targetHandle !== edges[index].targetHandle
          ? [{ previous: edges[index], next: edge }]
          : []
      ));
      if (changedEdges.length > 0) {
        setEdges(remappedEdges);
        changedEdges.forEach(({ previous, next }) => {
          const sourceNode = String(next.source) === String(id)
            ? nextNode
            : nodes.find((node) => String(node.id) === String(next.source));
          const targetNode = String(next.target) === String(id)
            ? nextNode
            : nodes.find((node) => String(node.id) === String(next.target));
          if (!sourceNode || !targetNode) return;
          void deleteEdgeFromBackend(sourceNode, targetNode, previous)
            .then(() => addEdgeToBackend(sourceNode, targetNode, next))
            .catch((error) => {
            console.error("[FlowCanvas.tsx] Failed to remap Subpipeline connection:", error);
            toast.error("Subpipeline contract saved, but a connection could not be remapped");
          });
        });
      }
    }
  }, [edges, markLocalWrite, nodes, onCanvasEdited, pushHistorySnapshot]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const triggerImport = () => fileInputRef.current?.click();

  const createSerializableFlow = useCallback((): PipelineVersionGraph => {
    const viewport = reactFlowInstance?.toObject().viewport ?? { x: 0, y: 0, zoom: 1 };
    return {
      updated_at: lastSeenUpdatedAtRef.current,
      nodes: nodes.map((node) => {
        const data = { ...(node.data || {}) };
        delete data.file_buckets;
        delete data.validation_issues;
        delete data.connected_ports;
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

  const loadGenerationPreflight = useCallback(async (
    scope: PipelineScriptGenerationScope,
    selectedFlowIds: string[],
  ) => {
    const requestId = ++generationPreflightRequestRef.current;
    setIsGenerationPreflightLoading(true);
    setGenerationPreflightError("");
    try {
      const prepared = await preparePipelineScriptGeneration({
        scope,
        selectedFlowIds,
      });
      if (requestId !== generationPreflightRequestRef.current) return;
      setGenerationPreflight(prepared);
    } catch (error) {
      if (requestId !== generationPreflightRequestRef.current) return;
      setGenerationPreflight(null);
      setGenerationPreflightError(
        error instanceof Error ? error.message : "Could not inspect the pipeline.",
      );
    } finally {
      if (requestId === generationPreflightRequestRef.current) {
        setIsGenerationPreflightLoading(false);
      }
    }
  }, []);

  const loadRecentGenerationRuns = useCallback(async () => {
    const resetVersion = generationHistoryResetVersionRef.current;
    try {
      const runs = await listPipelineScriptGenerationRuns(8);
      if (resetVersion === generationHistoryResetVersionRef.current) {
        setRecentGenerationRuns(runs);
      }
    } catch (error) {
      console.warn("[FlowCanvas.tsx] Could not load generation history:", error);
    }
  }, []);

  const openCodeGeneration = useCallback((selectedFlowIds: string[] = []) => {
    if (isGeneratingScripts) {
      setIsScriptGenerationOpen(true);
      return;
    }
    const normalizedSelectedIds = Array.from(new Set(
      selectedFlowIds.map((flowId) => String(flowId).trim()).filter(Boolean),
    ));
    const nextScope: PipelineScriptGenerationScope = normalizedSelectedIds.length > 0
      ? "selected"
      : "missing_changed";
    setScriptGenerationSelectedFlowIds(normalizedSelectedIds);
    setScriptGenerationScope(nextScope);
    setScriptGenerationPrompt(String(pipelinePrompt || "").trim());
    setScriptGenerationMode(uploadedSampleDataAvailable ? "validated" : "draft");
    setOverwriteProtectedCode(false);
    setGenerationJob(null);
    generationPreflightRequestRef.current += 1;
    setGenerationPreflight(null);
    setIsGenerationPreflightLoading(true);
    setGenerationPreflightError("");
    setIsScriptGenerationOpen(true);
    void loadRecentGenerationRuns();
  }, [
    isGeneratingScripts,
    loadRecentGenerationRuns,
    pipelinePrompt,
    uploadedSampleDataAvailable,
  ]);

  useEffect(() => {
    if (!isScriptGenerationOpen || isGeneratingScripts || generationJob) return;
    void loadGenerationPreflight(
      scriptGenerationScope,
      scriptGenerationSelectedFlowIds,
    );
  }, [
    generationJob,
    isGeneratingScripts,
    isScriptGenerationOpen,
    loadGenerationPreflight,
    scriptGenerationScope,
    scriptGenerationSelectedFlowIds,
  ]);

  useImperativeHandle(ref, () => ({
    updateNode,
    syncFromBackend,
    getCurrentGraph,
    getCurrentVersionGraph: createSerializableFlow,
    openCodeGeneration,
  }), [
    createSerializableFlow,
    getCurrentGraph,
    openCodeGeneration,
    syncFromBackend,
    updateNode,
  ]);

  useEffect(() => {
    localStorage.setItem('ai-flow-nodes', JSON.stringify(nodes));
    localStorage.setItem('ai-flow-edges', JSON.stringify(edges));
  }, [nodes, edges]);

  useEffect(() => {
    localStorage.setItem('inlumen-show-port-details', String(showPortDetails));
    onDisplayModeChange?.(showPortDetails);
  }, [onDisplayModeChange, showPortDetails]);

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

      const selectedNodeId = selectedNodeIdRef.current;
      if (selectedNodeId && removedNodeIds.includes(selectedNodeId)) {
        selectedNodeIdRef.current = null;
        onNodeSelect(null, { openInspector: false });
      }
    },
    [nodes, onNodeSelect, markLocalWrite, onCanvasEdited, pushHistorySnapshot]
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
        const removedEdges = edges.filter((edge) => removedEdgeIds.includes(edge.id));
        setEdges((currentEdges) => applyEdgeChanges(changes, currentEdges));
        markLocalWrite(5000);
        void Promise.all(
          removedEdges.map(async (edge) => {
            const sourceNode = nodes.find((n) => n.id === edge.source);
            const targetNode = nodes.find((n) => n.id === edge.target);
            if (!sourceNode || !targetNode) {
              throw new Error(`Could not resolve the endpoints for connection ${edge.id}.`);
            }
            await deleteEdgeFromBackend(sourceNode, targetNode, edge);
            onRemoveEdge?.(edge.id);
          }),
        ).then(() => {
          markLocalWrite(1000);
        }).catch(async (error) => {
          console.error("[FlowCanvas.tsx] Connection deletion failed:", error);
          toast.error("Could not remove connection", {
            description: error instanceof Error ? error.message : "The backend did not accept the deletion.",
          });
          try {
            await fetchGraphAndApply();
          } catch (syncError) {
            scheduleSyncRetry("Connection deletion recovery failed", syncError);
          }
        });
        return;
      }
      setEdges((eds) => applyEdgeChanges(changes, eds));
    },
    [
      edges,
      fetchGraphAndApply,
      markLocalWrite,
      nodes,
      onCanvasEdited,
      onRemoveEdge,
      pushHistorySnapshot,
      scheduleSyncRetry,
    ]
  );

  const onConnect = useCallback(
    async (params: Connection) => {
      if (!params.source || !params.target) return;

      if (params.source === params.target) {
        toast("Cannot connect a node to itself", { description: "Please connect to a different node" });
        return;
      }

      if (!params.sourceHandle || !params.targetHandle) {
        toast.error("Select explicit ports", {
          description: "Connections must link an output port to an input port.",
        });
        return;
      }

      const sourceNode = nodes.find((node) => node.id === params.source);
      const targetNode = nodes.find((node) => node.id === params.target);
      const sourcePort = sourceNode
        ? normalizeNodePorts(sourceNode.data?.ports, normalizeType(sourceNode.data?.type)).outputs
          .find((port) => port.id === params.sourceHandle)
        : undefined;
      const targetPort = targetNode
        ? normalizeNodePorts(targetNode.data?.ports, normalizeType(targetNode.data?.type)).inputs
          .find((port) => port.id === params.targetHandle)
        : undefined;
      if (!sourcePort || !targetPort) {
        toast.error("Invalid port connection", {
          description: "The selected source or target port no longer exists.",
        });
        return;
      }
      const sourceType = sourcePort.type.toLowerCase();
      const targetType = targetPort.type.toLowerCase();
      const wildcard = (value: string) => ["", "any", "unknown", "*"].includes(value);
      if (!wildcard(sourceType) && !wildcard(targetType) && sourceType !== targetType) {
        toast.error("Incompatible port contracts", {
          description: `${sourcePort.name} (${sourcePort.type}) cannot connect to ${targetPort.name} (${targetPort.type}).`,
        });
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
    onNodeSelect(node);
  }, [onNodeSelect]);

  const openValidationIssue = useCallback((issue: ValidationIssue) => {
    const edge = issue.edgeId
      ? edges.find((candidate) => String(candidate.id || "") === issue.edgeId)
      : undefined;
    const nodeId = issue.nodeId || edge?.target || edge?.source;
    const node = displayedNodes.find((candidate) => String(candidate.id) === String(nodeId || ""));
    if (node) {
      selectedNodeIdRef.current = node.id;
      onNodeSelect(node);
      window.setTimeout(() => {
        const section = ["unknown-edge-port", "missing-edge-port"].includes(issue.code)
          ? "ports"
          : issue.category;
        document.getElementById(`inspector-${section}`)?.scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
      }, 150);
    }
    setIsValidationOpen(false);
  }, [displayedNodes, edges, onNodeSelect]);

  const onPaneClick = useCallback(() => {
    selectedNodeIdRef.current = null;
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
        const project = createProjectDocument(normalizeGraph(createSerializableFlow()));
        downloadJsonFile(project, 'inlumen-project.json');

        toast.success('Project JSON exported', {
          description: designValidation.valid
            ? 'The canonical Pipeline IR is valid.'
            : `Exported with ${designValidation.issues.length} validation issue${designValidation.issues.length === 1 ? '' : 's'}.`,
        });
      }
    } catch (error) {
      console.error('Error exporting flow:', error);
      toast.error('Failed to export flow', {
        description: 'There was an error exporting your pipeline',
      });
    }
  };

  const handleGeneratePipelineScripts = () => openCodeGeneration();

  const handleRunPipelineScriptGeneration = async () => {
    if (
      isGeneratingScripts
      || isCreatingGenerationCheckpoint
      || !generationPreflight
      || generationPreflight.target_count === 0
    ) return;
    const sampleDataAvailable = Boolean(
      generationPreflight.sample_data?.has_sample_data,
    );
    const mode = scriptGenerationMode === "validated" && !sampleDataAvailable
      ? "draft"
      : scriptGenerationMode;
    if (generationPreflight.protected_count > 0 && !overwriteProtectedCode) {
      toast.error("Replacement approval required", {
        description: "Confirm replacement of user-owned runtime code before generating.",
      });
      return;
    }
    if (validationErrors > 0) {
      toast.error("Resolve pipeline errors first", {
        description: `${validationErrors} design-time error${validationErrors === 1 ? "" : "s"} block safe code generation.`,
      });
      return;
    }
    let checkpointVersionUid = "";
    if (generationPreflight.replacement_count > 0) {
      try {
        setIsCreatingGenerationCheckpoint(true);
        const checkpoint = await savePipelineVersion(
          `Before code generation ${new Date().toLocaleString()}`,
          createSerializableFlow(),
        );
        checkpointVersionUid = checkpoint.uid;
        onVersionSaved?.(checkpoint);
      } catch (error) {
        toast.error("Could not create a restore point", {
          description: error instanceof Error ? error.message : "Generation was not started.",
        });
        return;
      } finally {
        setIsCreatingGenerationCheckpoint(false);
      }
    }
    const options = {
      ...modeToGenerationOptions(mode, sampleDataAvailable),
      scope: scriptGenerationScope,
      selectedFlowIds: scriptGenerationSelectedFlowIds,
      overwriteManualCode: overwriteProtectedCode,
      checkpointVersionUid,
      userInstruction: scriptGenerationPrompt.trim(),
    };
    generationCancelRequestedRef.current = false;
    setIsGeneratingScripts(true);
    setGenerationJob(null);
    try {
      const started = await startPipelineScriptGenerationRun(
        activeChatbotConfig,
        options,
      );
      if (!String(started.run_id || "").trim()) {
        throw new Error("Pipeline generation run did not return a run id.");
      }
      rememberGenerationJob(started);
      void loadRecentGenerationRuns();
      toast.info("Runtime generation started", {
        description: "This runs in the background. You can close this panel and return at any time.",
      });
    } catch (error) {
      if (generationCancelRequestedRef.current) {
        return;
      }
      console.error("[FlowCanvas.tsx] Generate pipeline scripts error:", error);
      toast.error("Script generation failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
      setIsGeneratingScripts(false);
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
      const started = await resumePipelineScriptGenerationRun(
        currentRunId,
        activeChatbotConfig,
        {
          flowId: failedFlowId,
          repairAttempts: 4,
        },
      );
      rememberGenerationJob(started);
      void loadRecentGenerationRuns();
      toast.info("Node repair started", {
        description: "The repair is running in the background.",
      });
    } catch (error) {
      if (generationCancelRequestedRef.current) {
        return;
      }
      console.error("[FlowCanvas.tsx] Repair pipeline script error:", error);
      toast.error("Node repair failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
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
      rememberGenerationJob(cancelled);
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

  const handleViewGenerationRun = async (run: PipelineGenerationJob) => {
    const runId = generationRunId(run);
    if (!runId) return;
    try {
      const loaded = await fetchPipelineScriptGenerationRun(runId);
      rememberGenerationJob(loaded);
      setIsScriptGenerationOpen(true);
    } catch (error) {
      toast.error("Could not open generation run", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    }
  };

  const handleViewGeneratedCode = () => {
    const persistedResult = generationJob?.persistence?.result as
      | {
        nodes?: Array<{ flow_id?: string }>;
        attached_flow_ids?: string[];
      }
      | undefined;
    const targetFlowIds = Array.from(new Set([
      ...(generationJob?.target_flow_ids || []),
      ...(generationJob?.preflight?.target_flow_ids || []),
      ...(persistedResult?.attached_flow_ids || []),
      ...(persistedResult?.nodes || []).map((node) => node.flow_id || ""),
      ...(generationJob?.generation_run?.steps || []).map((step) => step.flow_id || ""),
    ].map((flowId) => String(flowId).trim()).filter(Boolean)));
    const taskTarget = targetFlowIds.find((flowId) => {
      const node = displayedNodesRef.current.find(
        (candidate) => String(candidate.id) === String(flowId),
      );
      return node && normalizeType(node.data?.type) === "task";
    });
    const flowId = String(taskTarget || targetFlowIds[0] || "").trim();
    if (!flowId) return;
    const node = displayedNodesRef.current.find(
      (candidate) => String(candidate.id) === flowId,
    );
    if (!node) return;
    selectedNodeIdRef.current = node.id;
    onNodeSelect(node, { openInspector: true });
    setIsScriptGenerationOpen(false);
  };

  const handleRestoreGenerationCheckpoint = async () => {
    const checkpointVersionUid = String(
      generationJob?.checkpoint_version_uid || "",
    ).trim();
    if (!checkpointVersionUid || isRestoringGenerationCheckpoint) return;
    try {
      setIsRestoringGenerationCheckpoint(true);
      const restored = await restorePipelineVersion(checkpointVersionUid);
      await syncFromBackend(restored.graph);
      onActiveVersionChange?.(restored.version.uid);
      onActiveVersionNameChange?.(restored.version.name);
      onPipelineDescriptionChange?.(restored.version.description || "");
      toast.success("Generated packages were undone", {
        description: `Restored ${restored.version.name}.`,
      });
      setIsScriptGenerationOpen(false);
    } catch (error) {
      toast.error("Could not restore the generation checkpoint", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsRestoringGenerationCheckpoint(false);
    }
  };

  const importFlow = async (e: React.ChangeEvent<HTMLInputElement>) => {
    try {
      const file = e.target.files?.[0];
      if (!file) return;
      const text = await file.text();
      const flowData = JSON.parse(text);
      const normalizedImport = projectDocumentToGraph(flowData);
      const importedNodes = normalizedImport.nodes;
      const importedEdges = normalizedImport.edges;
      pushHistorySnapshot();
      onCanvasEdited?.();
      markLocalWrite(1200); // avoid immediate poll-refresh
      await rebuildBackendFromFlow(importedNodes, importedEdges);
      setNodes(importedNodes);
      setEdges(importedEdges);
      nodeId = getNextNumericNodeId(importedNodes, 1);
      toast.success('Project JSON imported', {
        description: 'The Pipeline IR was migrated and the design state was reconstructed.',
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
    try {
      await clearPipelineScriptGenerationRuns();
      resetGenerationHistoryState();
      pushHistorySnapshot();
      onCanvasEdited?.();
      setNodes([]);
      setEdges([]);
      selectedNodeIdRef.current = null;
      onNodeSelect(null);
      localStorage.removeItem('ai-flow');
      localStorage.removeItem('ai-flow-nodes');
      localStorage.removeItem('ai-flow-edges');
      nodeId = 1;
      markLocalWrite(1200);
      await rebuildBackendFromFlow([], []);
      toast.success('Canvas cleared', {
        description: 'Nodes, edges, and generation history have been removed',
      });
    } catch (error) {
      console.error('[FlowCanvas.tsx] Canvas cleanup failed:', error);
      toast.error('Could not clear canvas', {
        description: error instanceof Error ? error.message : 'Workspace cleanup failed.',
      });
    }
  };

  const generationSteps = generationJob?.generation_run?.steps || [];
  const generationProgress = generationProgressPercent(generationJob);
  const generationStatus = effectiveGenerationStatus(generationJob);
  const generationUsage = generationJob?.generation_run?.generation_usage;
  const generationFailed = ["invalid", "failed"].includes(generationStatus);
  const repairableFailedStep = failedGenerationStep(generationJob);
  const canRepairFailedNode = Boolean(
    generationJob &&
      generationFailed &&
      repairableFailedStep?.flow_id &&
      generationJob.result,
  );
  const generationPersistenceResult = generationJob?.persistence?.result as
    | {
      nodes?: Array<{ flow_id?: string }>;
      attached_flow_ids?: string[];
      reused_flow_ids?: string[];
    }
    | undefined;
  const attachedGenerationCount = Array.isArray(
    generationPersistenceResult?.attached_flow_ids,
  )
    ? generationPersistenceResult.attached_flow_ids.length
    : Array.isArray(generationPersistenceResult?.nodes)
      ? generationPersistenceResult.nodes.length
      : 0;
  const reusedGenerationCount = Array.isArray(
    generationPersistenceResult?.reused_flow_ids,
  )
    ? generationPersistenceResult.reused_flow_ids.length
    : generationJob?.reusable_flow_ids?.length || 0;
  const generationCompleted = Boolean(
    generationJob && GENERATION_TERMINAL_STATUSES.has(generationStatus),
  );
  const generationSucceeded = Boolean(
    generationCompleted
    && generationStatus === "valid"
    && generationJob?.persistence?.status === "persisted",
  );
  const generationSampleDataAvailable = Boolean(
    generationPreflight?.sample_data?.has_sample_data
    ?? uploadedSampleDataAvailable,
  );
  const selectedGenerationLabel = scriptGenerationSelectedFlowIds.length === 1
    ? displayedNodes.find(
      (node) => String(node.id) === scriptGenerationSelectedFlowIds[0],
    )?.data?.label || `Node ${scriptGenerationSelectedFlowIds[0]}`
    : `${scriptGenerationSelectedFlowIds.length} selected nodes`;
  const codegenModelName = activeChatbotConfig?.codegenModel?.trim() || "";
  const canStartGeneration = Boolean(
    generationPreflight
    && generationPreflight.target_count > 0
    && !isGenerationPreflightLoading
    && !generationPreflightError
    && validationErrors === 0
    && codegenModelName
    && (
      generationPreflight.protected_count === 0
      || overwriteProtectedCode
    )
    && (
      scriptGenerationMode !== "validated"
      || generationSampleDataAvailable
    )
  );

  return (
    <div ref={reactFlowWrapper} className="h-full w-full">
      <PortDisplayContext.Provider value={{
        advanced: showPortDetails,
        validationByNode: designValidation.byNode,
      }}>
      <ReactFlow
        nodes={displayedNodes}
        edges={displayedEdges}
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
              case 'destination': return '#10B981';
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
          validationErrors={validationErrors}
          validationWarnings={validationWarnings}
          onValidationClick={() => setIsValidationOpen(true)}
          onClear={clearCanvas}
          canUndo={historyAvailability.canUndo}
          canRedo={historyAvailability.canRedo}
          isHistoryRestoring={isHistoryRestoring}
        />
      </ReactFlow>
      </PortDisplayContext.Provider>

      <Dialog open={isValidationOpen} onOpenChange={setIsValidationOpen}>
        <DialogContent className="max-h-[80vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Pipeline validation</DialogTitle>
            <DialogDescription>
              {designValidation.issues.length === 0
                ? "The pipeline contract is valid."
                : `${validationErrors} error${validationErrors === 1 ? "" : "s"} and ${validationWarnings} warning${validationWarnings === 1 ? "" : "s"} need attention.`}
            </DialogDescription>
          </DialogHeader>
          {designValidation.issues.length === 0 ? (
            <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-600">
              <CheckCircle2 className="h-4 w-4" />
              No validation issues found.
            </div>
          ) : (
            <div className="space-y-2">
              {designValidation.issues.map((issue, index) => {
                const subject = getValidationIssueSubject(issue, displayedNodes, edges);
                return (
                  <button
                    key={`${issue.code}-${issue.nodeId || issue.edgeId || index}`}
                    type="button"
                    onClick={() => openValidationIssue(issue)}
                    aria-label={`${subject.label}: ${issue.message}`}
                    className={cn(
                      "flex w-full items-start gap-2 rounded-lg border p-3 text-left text-sm transition-colors hover:bg-muted/60",
                      issue.severity === "error"
                        ? "border-red-500/30 bg-red-500/5"
                        : "border-amber-500/30 bg-amber-500/5",
                    )}
                  >
                    <AlertCircle className={cn(
                      "mt-0.5 h-4 w-4 shrink-0",
                      issue.severity === "error" ? "text-red-500" : "text-amber-500",
                    )} />
                    <span className="min-w-0">
                      <span className="block truncate font-semibold text-foreground">{subject.label}</span>
                      <span className="block text-xs font-medium capitalize text-muted-foreground">
                        {subject.context} · {issue.severity} · {issue.category}
                      </span>
                      <span className="mt-1 block text-muted-foreground">{issue.message}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={isScriptGenerationOpen}
        onOpenChange={setIsScriptGenerationOpen}
      >
        <DialogContent className="flex max-h-[90vh] flex-col overflow-hidden p-0 sm:max-w-2xl">
          <DialogHeader className="px-6 pt-6">
            <DialogTitle>
              {!generationJob
                ? "Generate runtime code"
                : isGeneratingScripts
                  ? "Generating runtime code"
                  : generationSucceeded
                    ? "Runtime code attached"
                    : generationStatus === "cancelled"
                      ? "Generation stopped"
                      : "Generation needs attention"}
            </DialogTitle>
            <DialogDescription>
              {!generationJob
                ? "Choose the scope and validation level. inLUMEN validates generated packages before attaching them."
                : isGeneratingScripts
                  ? "The run continues in the background when this panel is closed."
                  : generationSucceeded
                    ? "Validated packages were attached automatically to the selected scope."
                    : "No invalid candidate packages were attached."}
            </DialogDescription>
          </DialogHeader>

          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
            {!generationJob ? (
              <div className="space-y-5">
                {isGenerationPreflightLoading && !generationPreflight ? (
                  <div className="flex items-center gap-2 rounded-md border border-border p-4 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Inspecting runtime packages and sample inputs…
                  </div>
                ) : generationPreflightError ? (
                  <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                    {generationPreflightError}
                  </div>
                ) : generationPreflight && (
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div className="rounded-md border border-border bg-muted/20 p-3">
                      <p className="text-xs text-muted-foreground">Packages to attach</p>
                      <p className="mt-1 font-semibold">{generationPreflight.target_count}</p>
                      <p className="text-xs text-muted-foreground">
                        {(generationPreflight.candidate_count || 0) > generationPreflight.target_count
                          ? `${generationPreflight.candidate_count} candidates generated for pipeline validation`
                          : `${generationPreflight.reused_count} validated package${generationPreflight.reused_count === 1 ? "" : "s"} reused`}
                      </p>
                    </div>
                    <div className="rounded-md border border-border bg-muted/20 p-3">
                      <p className="text-xs text-muted-foreground">Code model</p>
                      <p className={cn("mt-1 truncate font-semibold", !codegenModelName && "text-destructive")}>
                        {codegenModelName || "Not configured"}
                      </p>
                      <p className="text-xs text-muted-foreground">{activeChatbotConfig?.provider || "Open Settings to configure"}</p>
                    </div>
                    <div className="rounded-md border border-border bg-muted/20 p-3">
                      <p className="text-xs text-muted-foreground">Sample inputs</p>
                      <p className="mt-1 font-semibold">
                        {generationPreflight.sample_data?.sample_file_count || 0} attached
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {generationSampleDataAvailable ? "Available for execution" : "Static validation only"}
                      </p>
                    </div>
                    <button
                      type="button"
                      className={cn(
                        "rounded-md border border-border bg-muted/20 p-3 text-left",
                        validationErrors > 0 && "border-destructive/40 bg-destructive/5",
                      )}
                      onClick={() => {
                        setIsScriptGenerationOpen(false);
                        setIsValidationOpen(true);
                      }}
                    >
                      <p className="text-xs text-muted-foreground">Pipeline design</p>
                      <p className="mt-1 font-semibold">
                        {validationErrors > 0
                          ? `${validationErrors} blocking error${validationErrors === 1 ? "" : "s"}`
                          : validationWarnings > 0
                            ? `${validationWarnings} warning${validationWarnings === 1 ? "" : "s"}`
                            : "Ready"}
                      </p>
                      <p className="text-xs text-muted-foreground">Open validation</p>
                    </button>
                  </div>
                )}

                <div className="space-y-2">
                  <Label>Scope</Label>
                  <RadioGroup
                    value={scriptGenerationScope}
                    onValueChange={(value) => {
                      generationPreflightRequestRef.current += 1;
                      setScriptGenerationScope(value as PipelineScriptGenerationScope);
                      setGenerationPreflight(null);
                      setIsGenerationPreflightLoading(true);
                      setGenerationPreflightError("");
                      setOverwriteProtectedCode(false);
                    }}
                    className="grid gap-2"
                  >
                    {[
                      {
                        value: "missing_changed",
                        title: "Missing or changed",
                        description: "Generate only packages that are absent, stale, or invalid. Reuse validated packages.",
                        disabled: false,
                      },
                      {
                        value: "selected",
                        title: scriptGenerationSelectedFlowIds.length > 0
                          ? `Selected: ${selectedGenerationLabel}`
                          : "Selected nodes",
                        description: "Attach only the node scope opened from the Inspector. A full plan may be generated for pipeline validation.",
                        disabled: scriptGenerationSelectedFlowIds.length === 0,
                      },
                      {
                        value: "all",
                        title: "Entire pipeline",
                        description: "Regenerate every runtime package from one coherent pipeline plan.",
                        disabled: false,
                      },
                    ].map((option) => (
                      <Label
                        key={option.value}
                        htmlFor={`script-scope-${option.value}`}
                        className={cn(
                          "flex cursor-pointer items-start gap-3 rounded-md border border-border p-3 transition-colors",
                          scriptGenerationScope === option.value && "border-primary bg-primary/10",
                          option.disabled && "cursor-not-allowed opacity-50",
                        )}
                      >
                        <RadioGroupItem
                          id={`script-scope-${option.value}`}
                          value={option.value}
                          disabled={option.disabled || isGenerationPreflightLoading}
                          className="mt-1"
                        />
                        <span className="grid gap-1">
                          <span className="font-medium">{option.title}</span>
                          <span className="text-sm font-normal text-muted-foreground">{option.description}</span>
                        </span>
                      </Label>
                    ))}
                  </RadioGroup>
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="pipeline-runtime-prompt">Additional generation instructions <span className="font-normal text-muted-foreground">(optional)</span></Label>
                  <Textarea
                    id="pipeline-runtime-prompt"
                    value={scriptGenerationPrompt}
                    onChange={(event) => setScriptGenerationPrompt(event.target.value)}
                    placeholder="Add implementation constraints, preferred libraries, or performance requirements."
                    className="min-h-20"
                  />
                </div>

                <div className="space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <Label>Validation</Label>
                    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      {generationSampleDataAvailable ? (
                        <><CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" /> Sample execution available</>
                      ) : (
                        <><AlertCircle className="h-3.5 w-3.5 text-amber-500" /> No sample inputs</>
                      )}
                    </span>
                  </div>
                  <RadioGroup
                    value={scriptGenerationMode}
                    onValueChange={(value) => setScriptGenerationMode(value as PipelineScriptGenerationMode)}
                    className="grid gap-2"
                  >
                    {generationModeOptions.map((option) => {
                      const Icon = option.icon;
                      const disabled = option.value === "validated" && !generationSampleDataAvailable;
                      return (
                        <Label
                          key={option.value}
                          htmlFor={`script-generation-${option.value}`}
                          className={cn(
                            "flex cursor-pointer items-start gap-3 rounded-md border border-border p-3 transition-colors",
                            scriptGenerationMode === option.value && "border-primary bg-primary/10",
                            disabled && "cursor-not-allowed opacity-50",
                          )}
                        >
                          <RadioGroupItem
                            id={`script-generation-${option.value}`}
                            value={option.value}
                            disabled={disabled}
                            className="mt-1"
                          />
                          <Icon className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                          <span className="grid gap-1">
                            <span className="font-medium">{option.title}</span>
                            <span className="text-sm font-normal text-muted-foreground">{option.description}</span>
                          </span>
                        </Label>
                      );
                    })}
                  </RadioGroup>
                </div>

                {generationPreflight && generationPreflight.protected_count > 0 && (
                  <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
                    <p className="font-medium text-amber-600 dark:text-amber-300">
                      {generationPreflight.protected_count} package{generationPreflight.protected_count === 1 ? " contains" : "s contain"} user-owned code
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      inLUMEN will not replace uploaded or manually edited runtime code without explicit approval.
                    </p>
                    <label className="mt-3 flex items-start gap-2">
                      <input
                        type="checkbox"
                        checked={overwriteProtectedCode}
                        onChange={(event) => setOverwriteProtectedCode(event.target.checked)}
                        className="mt-0.5"
                      />
                      <span>I understand and want to replace the protected packages. A restore point will be created first.</span>
                    </label>
                  </div>
                )}

                {generationPreflight?.target_count === 0 && !isGenerationPreflightLoading && (
                  <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-600">
                    All runtime packages in this scope are current and validated.
                  </div>
                )}

                <details className="rounded-md border border-border bg-muted/10 p-3 text-sm">
                  <summary className="cursor-pointer font-medium">What will be created and validated?</summary>
                  <div className="mt-3 space-y-2 text-xs text-muted-foreground">
                    <p>Each generated package contains <code className="text-foreground">main.py</code>, an optional <code className="text-foreground">requirements.txt</code>, <code className="text-foreground">node-manifest.json</code>, and a validation report.</p>
                    <p>Packages are attached only after the selected pipeline scope passes contract validation. Deployment Dockerfiles remain exporter-managed.</p>
                    {generationPreflight?.sample_data?.sample_nodes?.map((sampleNode) => (
                      <p key={sampleNode.flow_id}>
                        <span className="font-medium text-foreground">{sampleNode.label || sampleNode.flow_id}:</span>{" "}
                        {(sampleNode.files || []).map((file) => file.filename).filter(Boolean).join(", ")}
                      </p>
                    ))}
                  </div>
                </details>

                {recentGenerationRuns.length > 0 && (
                  <div className="space-y-2">
                    <Label>Recent runs</Label>
                    <div className="divide-y divide-border rounded-md border border-border">
                      {recentGenerationRuns.slice(0, 4).map((run) => (
                        <button
                          key={generationRunId(run)}
                          type="button"
                          onClick={() => { void handleViewGenerationRun(run); }}
                          className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:bg-muted/50"
                        >
                          <span className="min-w-0">
                            <span className="block truncate font-medium">
                              {run.target_flow_ids?.length || run.preflight?.target_count || "Pipeline"} package scope
                            </span>
                            <span className="block text-xs text-muted-foreground">
                              {run.created_at ? new Date(run.created_at).toLocaleString() : `Run ${generationRunId(run).slice(0, 8)}`}
                            </span>
                          </span>
                          <span className="shrink-0 text-xs text-muted-foreground">{generationStatusLabel(effectiveGenerationStatus(run))}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="space-y-4">
                <div className={cn(
                  "rounded-lg border p-4",
                  generationSucceeded
                    ? "border-emerald-500/30 bg-emerald-500/10"
                    : generationFailed
                      ? "border-destructive/40 bg-destructive/10"
                      : "border-border bg-muted/20",
                )}>
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="font-semibold">{generationStatusLabel(generationStatus)}</p>
                      <p className="text-xs text-muted-foreground">Run {generationRunId(generationJob).slice(0, 8)}</p>
                    </div>
                    <span className="rounded-full border border-border bg-background/70 px-2 py-1 text-xs capitalize">
                      {generationJob.generation_scope?.replace(/_/g, " ") || generationJob.mode || "pipeline"}
                    </span>
                  </div>
                  {isGeneratingScripts && <Progress value={generationProgress} className="mt-4" />}
                  <div className="mt-4 grid grid-cols-2 gap-2 text-sm">
                    <div className="rounded-md bg-background/60 p-3">
                      <p className="text-xs text-muted-foreground">Generation cost</p>
                      <p className="font-semibold">{formatGenerationCost(generationUsage?.cost_usd)}</p>
                      <p className="text-xs text-muted-foreground">
                        {generationUsage?.request_count || 0} model request{generationUsage?.request_count === 1 ? "" : "s"}
                      </p>
                    </div>
                    <div className="rounded-md bg-background/60 p-3">
                      <p className="text-xs text-muted-foreground">Model tokens</p>
                      <p className="font-semibold">{formatGenerationTokens(generationUsage?.total_tokens)}</p>
                      <p className="text-xs text-muted-foreground">Provider-reported usage</p>
                    </div>
                  </div>
                  {generationSucceeded && (
                    <div className="mt-4 grid grid-cols-2 gap-2 text-sm">
                      <div className="rounded-md bg-background/60 p-3">
                        <p className="text-xs text-muted-foreground">Attached</p>
                        <p className="font-semibold">{attachedGenerationCount} package{attachedGenerationCount === 1 ? "" : "s"}</p>
                      </div>
                      <div className="rounded-md bg-background/60 p-3">
                        <p className="text-xs text-muted-foreground">Reused</p>
                        <p className="font-semibold">{reusedGenerationCount} package{reusedGenerationCount === 1 ? "" : "s"}</p>
                      </div>
                    </div>
                  )}
                </div>

                {generationSteps.length > 0 && (
                  <div className="space-y-2">
                    <Label>{isGeneratingScripts ? "Progress" : "Node results"}</Label>
                    <div className="divide-y divide-border rounded-md border border-border">
                      {generationSteps.map((step) => {
                        const nodeLabel = displayedNodes.find((node) => String(node.id) === String(step.flow_id))?.data?.label;
                        return (
                          <div key={`${step.flow_id}-${step.stage}`} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                            <span className="min-w-0 truncate">
                              <span className="font-medium">{nodeLabel || `Node ${step.flow_id || "?"}`}</span>
                              <span className="ml-2 text-xs text-muted-foreground">{generationStageLabel(step.stage)}</span>
                            </span>
                            <span className={cn(
                              "shrink-0 text-xs",
                              step.status === "valid" ? "text-emerald-500" : step.status === "invalid" || step.status === "failed" ? "text-destructive" : "text-muted-foreground",
                            )}>
                              {generationStatusLabel(step.status || "running")}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {(generationFailed || (
                  generationCompleted
                  && !generationSucceeded
                  && generationStatus !== "cancelled"
                )) && (
                  <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    {generationFailureMessage(generationJob)}
                  </div>
                )}

                {isGeneratingScripts && (
                  <p className="text-sm text-muted-foreground">
                    You can close this panel and continue working. Use Generate code → View run to return.
                  </p>
                )}
              </div>
            )}
          </div>

          <DialogFooter className="border-t border-border px-6 pb-6 pt-4">
            {!generationJob ? (
              <>
                <Button variant="outline" onClick={() => setIsScriptGenerationOpen(false)}>Close</Button>
                <Button
                  onClick={() => { void handleRunPipelineScriptGeneration(); }}
                  disabled={!canStartGeneration || isCreatingGenerationCheckpoint}
                >
                  {(isGenerationPreflightLoading || isCreatingGenerationCheckpoint) && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {isCreatingGenerationCheckpoint ? "Creating restore point" : "Generate and attach"}
                </Button>
              </>
            ) : isGeneratingScripts ? (
              <>
                <Button variant="outline" onClick={() => setIsScriptGenerationOpen(false)}>Close and continue</Button>
                <Button
                  variant="destructive"
                  onClick={() => { void handleCancelPipelineScriptGeneration(); }}
                  disabled={isCancellingScripts}
                >
                  {isCancellingScripts && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Stop generation
                </Button>
              </>
            ) : (
              <>
                <Button variant="outline" onClick={() => setIsScriptGenerationOpen(false)}>Close</Button>
                {generationSucceeded && generationJob.checkpoint_version_uid && (
                  <Button
                    variant="outline"
                    onClick={() => { void handleRestoreGenerationCheckpoint(); }}
                    disabled={isRestoringGenerationCheckpoint}
                  >
                    {isRestoringGenerationCheckpoint && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    Undo attachment
                  </Button>
                )}
                {canRepairFailedNode && (
                  <Button variant="outline" onClick={() => { void handleRepairFailedPipelineNode(); }}>
                    Retry {displayedNodes.find((node) => String(node.id) === String(repairableFailedStep?.flow_id))?.data?.label || `Node ${repairableFailedStep?.flow_id}`}
                  </Button>
                )}
                {generationSucceeded ? (
                  <Button onClick={handleViewGeneratedCode}>View generated code</Button>
                ) : generationStatus !== "cancelled" ? (
                  <Button onClick={() => {
                    const previousScope = generationJob.generation_scope || "missing_changed";
                    const selectedIds = previousScope === "selected" ? generationJob.target_flow_ids || [] : [];
                    generationPreflightRequestRef.current += 1;
                    setScriptGenerationScope(previousScope);
                    setScriptGenerationSelectedFlowIds(selectedIds);
                    setGenerationPreflight(null);
                    setIsGenerationPreflightLoading(true);
                    setGenerationPreflightError("");
                    setOverwriteProtectedCode(false);
                    setGenerationJob(null);
                  }}>
                    Adjust and retry
                  </Button>
                ) : null}
              </>
            )}
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
  onDisplayModeChange,
  workspaceResetKey,
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
      onDisplayModeChange={onDisplayModeChange}
      workspaceResetKey={workspaceResetKey}
    />
  </ReactFlowProvider>
);
