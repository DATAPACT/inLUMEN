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
  addEdgeToNeo4j,
  addNodeToNeo4j,
  deleteEdgeToNeo4j,
  deleteNodeFromNeo4jAndMinIO,
  fetchPipelineGraph,
  fetchPipelineUpdatedAt,
  generatePipelineYaml,
  rebuildBackendFromFlow,
  updateNodePositionInNeo4j,
  clearNeo4jAndMinIO,
} from '@/features/flow/flowPersistence';
import {
  downloadJsonFile,
  downloadTextFile,
  getNextNumericNodeId,
  normalizeGraph,
} from '@/features/flow/flowGraph';

interface FlowCanvasProps {
  onNodeSelect: (node: Node | null) => void;
  onNodesChange?: (nodes: Node[]) => void;
  onRemoveNode?: (nodeId: string) => void;
  onRemoveEdge?: (edgeId: string) => void;
  isLightMode?: boolean;
  activeChatbotConfig?: ChatbotConfig;
}

export interface FlowCanvasRef {
  updateNode: (id: string, data: Record<string, unknown>) => void;
}

let nodeId = 1;

export const FlowCanvas = forwardRef<FlowCanvasRef, FlowCanvasProps>(({
  onNodeSelect,
  onNodesChange,
  onRemoveNode,
  onRemoveEdge,
  isLightMode,
  activeChatbotConfig,
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

  const fetchGraphAndApply = useCallback(async () => {
    const data = await fetchPipelineGraph();
    const g = normalizeGraph(data);
    setNodes(g.nodes);
    setEdges(g.edges);
    lastSeenUpdatedAtRef.current = g.updated_at;
    nodeId = getNextNumericNodeId(g.nodes, nodeId);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const initialLoad = async () => {
      try {
        await fetchGraphAndApply();
        markSyncHealthy();
      } catch (e) {
        scheduleSyncRetry("Initial neo4j_get_graph failed", e);
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
        scheduleSyncRetry("Neo4j poll tick failed", e);
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
  }, []);
  useImperativeHandle(ref, () => ({
    updateNode,
  }), [updateNode]);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const triggerImport = () => fileInputRef.current?.click();

  useEffect(() => {
    localStorage.setItem('ai-flow-nodes', JSON.stringify(nodes));
    localStorage.setItem('ai-flow-edges', JSON.stringify(edges));
  }, [nodes, edges]);

  useEffect(() => {
    if (onNodesChange) onNodesChange(nodes);
  }, [nodes, onNodesChange]);

  const onNodesChangeInternal = useCallback(
    (changes: NodeChange[]) => {
      const removedNodeIds = changes
        .filter(change => change.type === 'remove')
        .map(change => change.id);

      removedNodeIds.forEach((id) => {
        markLocalWrite(800);
        deleteNodeFromNeo4jAndMinIO(id);
      });

      const newNodes = applyNodeChanges(changes, nodes);
      setNodes(newNodes);

      if (selectedNode) {
        const updatedSelectedNode = newNodes.find(n => n.id === selectedNode.id);
        if (updatedSelectedNode) {
          setSelectedNode(updatedSelectedNode);
          onNodeSelect(updatedSelectedNode);
        }
      }
    },
    [nodes, selectedNode, onNodeSelect, markLocalWrite]
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const removedEdgeIds = changes
        .filter((c) => c.type === "remove")
        .map((c) => c.id);
      if (removedEdgeIds.length > 0) {
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
            deleteEdgeToNeo4j(sourceNode, targetNode);
          });
          return applyEdgeChanges(changes, eds);
        });
        return;
      }
      setEdges((eds) => applyEdgeChanges(changes, eds));
    },
    [nodes, markLocalWrite]
  );

  const onConnect = useCallback(
    async (params: Connection) => {
      if (!params.source || !params.target) return;

      if (params.source === params.target) {
        toast("Cannot connect a node to itself", { description: "Please connect to a different node" });
        return;
      }

      // prevent duplicates 
      let duplicate = false;
      setEdges((eds) => {
        duplicate = eds.some((e) => e.source === params.source && e.target === params.target);
        if (duplicate) return eds;
        return addEdge(params, eds);
      });

      if (duplicate) {
        toast("Connection already exists", { description: "This connection is already in place" });
        return;
      }

      // Find the actual Node objects
      const sourceNode = nodes.find((n) => n.id === params.source);
      const targetNode = nodes.find((n) => n.id === params.target);
      if (!sourceNode || !targetNode) {
        console.warn("[FlowCanvas.tsx] Could not find source/target nodes for Neo4j edge creation.");
        return;
      }
      markLocalWrite(800);
      await addEdgeToNeo4j(sourceNode, targetNode);
    },
    [nodes, markLocalWrite]
  );

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    setSelectedNode(node);
    onNodeSelect(node);
  }, [onNodeSelect]);

  const onPaneClick = useCallback(() => {
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

      setNodes((nds) => {
        const updated = nds.concat(newNode);
        markLocalWrite(800);
        addNodeToNeo4j(newNode); // Sync to Neo4j
        return updated;
      });
    },
    [reactFlowInstance, markLocalWrite]
  );

  const saveFlow = () => {
    try {
      if (reactFlowInstance) {
        const flow = reactFlowInstance.toObject();
        localStorage.setItem('ai-flow', JSON.stringify(flow));
        toast.success('Flow saved successfully', {
          description: 'Your AI pipeline has been saved',
        });
      }
    } catch (error) {
      console.error('Error saving flow:', error);
      toast.error('Failed to save flow', {
        description: 'There was an error saving your pipeline',
      });
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
      markLocalWrite(1200); // avoid immediate poll-refresh
      await rebuildBackendFromFlow(importedNodes, importedEdges);
      setNodes(importedNodes);
      setEdges(importedEdges);
      nodeId = getNextNumericNodeId(importedNodes, 1);
      toast.success('Flow imported successfully', {
        description: 'Imported flow + backend reconstructed (Neo4j/MinIO)',
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
    setNodes([]);
    setEdges([]);
    localStorage.removeItem('ai-flow');
    localStorage.removeItem('ai-flow-nodes');
    localStorage.removeItem('ai-flow-edges');
    nodeId = 1;
    markLocalWrite(1200);
    await clearNeo4jAndMinIO();
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
        onNodeDragStop={(_, node) => {
          markLocalWrite(800);
          updateNodePositionInNeo4j(node);
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
          onSave={saveFlow}
          onExportJson={exportFlow}
          onExportYaml={exportFlowYAML}
          onImportClick={triggerImport}
          onImport={importFlow}
          onClear={clearCanvas}
        />
      </ReactFlow>
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
    />
  </ReactFlowProvider>
);
