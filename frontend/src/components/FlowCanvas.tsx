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
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { nodeTypes } from './NodeTypes';
import { toast } from 'sonner';
import { cn } from "@/lib/utils";
import { FlowCanvasActionsPanel } from '@/components/flow/FlowCanvasActionsPanel';
import {
  addEdgeToBackend,
  addNodeToBackend,
  deleteEdgeFromBackend,
  deleteNodeFromBackend,
  fetchPipelineVersions,
  fetchPipelineGraph,
  fetchPipelineUpdatedAt,
  generatePipelineYaml,
  type PipelineVersionGraph,
  type PipelineVersionSummary,
  rebuildBackendFromFlow,
  savePipelineVersion,
  updateNodePositionInBackend,
  clearBackendGraph,
} from '@/features/flow/flowPersistence';
import {
  createAgentGraphSnapshot,
  downloadJsonFile,
  downloadTextFile,
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

interface FlowCanvasProps {
  onNodeSelect: (node: Node | null, options?: { openInspector?: boolean }) => void;
  onNodesChange?: (nodes: Node[]) => void;
  onRemoveNode?: (nodeId: string) => void;
  onRemoveEdge?: (edgeId: string) => void;
  isLightMode?: boolean;
  activeChatbotConfig?: ChatbotConfig;
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

const getSnapshotFileRef = (file: unknown, nodeIdValue: string) => {
  if (typeof file === "string") return file;
  if (typeof File !== "undefined" && file instanceof File) return file.name;
  if (file && typeof file === "object") {
    const entry = file as { filename?: unknown; name?: unknown; bucket?: unknown };
    const filename = typeof entry.filename === "string"
      ? entry.filename
      : typeof entry.name === "string"
        ? entry.name
        : "";
    if (!filename) return null;
    const bucket = typeof entry.bucket === "string" && entry.bucket.trim()
      ? entry.bucket.trim()
      : `files-step-id-${nodeIdValue}`.toLowerCase();
    return { filename, bucket };
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

  const applyHistorySnapshot = useCallback(async (snapshot: GraphHistorySnapshot) => {
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

    await rebuildBackendFromFlow(nextNodes, nextEdges);
    onCanvasEdited?.();
  }, [markLocalWrite, onCanvasEdited, onNodeSelect, reactFlowInstance]);

  const undoGraphChange = useCallback(async () => {
    const snapshot = undoStackRef.current.pop();
    if (!snapshot) return;

    redoStackRef.current = [
      ...redoStackRef.current,
      createHistorySnapshot(),
    ].slice(-GRAPH_HISTORY_LIMIT);
    syncHistoryAvailability();

    try {
      setIsHistoryRestoring(true);
      await applyHistorySnapshot(snapshot);
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

    undoStackRef.current = [
      ...undoStackRef.current,
      createHistorySnapshot(),
    ].slice(-GRAPH_HISTORY_LIMIT);
    syncHistoryAvailability();

    try {
      setIsHistoryRestoring(true);
      await applyHistorySnapshot(snapshot);
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
            deleteEdgeFromBackend(sourceNode, targetNode);
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

      const duplicate = edges.some((edge) => edge.source === params.source && edge.target === params.target);
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
      await addEdgeToBackend(sourceNode, targetNode);
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
          content: nodeData.data.type === 'input' ? '{input}' : '',
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

  const exportFlowYAML = async () => {
    try {
      const yamlText = await generatePipelineYaml(activeChatbotConfig);
      downloadTextFile(
        yamlText,
        `ai-pipeline-${Date.now()}.yaml`,
        "application/x-yaml;charset=utf-8",
      );
      toast.success("Flow exported successfully", {
        description: "Your AI pipeline has been exported as YAML",
      });
    } catch (error) {
      console.error("Error exporting flow:", error);
      toast.error("Failed to export flow", {
        description: "There was an error exporting your pipeline",
      });
    }
  };

  const importFlow = async (e: React.ChangeEvent<HTMLInputElement>) => {
    try {
      const file = e.target.files?.[0];
      if (!file) return;
      const text = await file.text();
      const flowData = JSON.parse(text) as { nodes?: Node[]; edges?: Edge[] };
      if (!Array.isArray(flowData.nodes) || !Array.isArray(flowData.edges)) {
        toast.error('Invalid flow file', {
          description: 'The selected file does not contain a valid flow',
        });
        return;
      }
      const importedNodes = flowData.nodes;
      const importedEdges = flowData.edges;
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
    await clearBackendGraph();
    toast.success('Canvas cleared', {
      description: 'All nodes and edges have been removed',
    });
  };

  return (
    <div ref={reactFlowWrapper} className="h-full w-full">
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
        fitView
        className={cn(
          "flow-canvas transition-colors duration-300",
          isLightMode ? "bg-stone-50" : "bg-[#0F1C0F]"
        )}
      >
        <Controls className="bg-card border border-border rounded-md p-1" />

        <MiniMap
          nodeColor={n => {
            switch (n.data.type) {
              case 'config': return '#0EA5E9';
              case 'input': return '#3B82F6';
              case 'action': return '#84CC16';
              case 'output': return '#10B981';
              case 'api': return '#F43F5E';
              case 'storage': return '#14B8A6';
              case 'custom': return '#8B5CF6';
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
          onExportYaml={exportFlowYAML}
          onImportClick={triggerImport}
          onImport={importFlow}
          onClear={clearCanvas}
          canUndo={historyAvailability.canUndo}
          canRedo={historyAvailability.canRedo}
          isHistoryRestoring={isHistoryRestoring}
        />
      </ReactFlow>

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
      onVersionSaved={onVersionSaved}
      onCanvasEdited={onCanvasEdited}
      onActiveVersionChange={onActiveVersionChange}
      onActiveVersionNameChange={onActiveVersionNameChange}
      onPipelineDescriptionChange={onPipelineDescriptionChange}
    />
  </ReactFlowProvider>
);
