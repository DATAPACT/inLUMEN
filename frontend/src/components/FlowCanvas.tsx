import React, { useState, useCallback, useRef, useEffect, forwardRef, useImperativeHandle } from 'react';
import ReactFlow, {
  Node,
  Edge,
  Controls,
  MiniMap,
  ReactFlowProvider,
  NodeChange,
  EdgeChange,
  Connection,
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
  Panel,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { nodeTypes } from './NodeTypes';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Save, Download, Upload, Trash2 } from 'lucide-react';
import { cn } from "@/lib/utils";

interface FlowCanvasProps {
  onNodeSelect: (node: Node | null) => void;
  onNodesChange?: (nodes: Node[]) => void;
  onRemoveNode?: (nodeId: string) => void;
  onRemoveEdge?: (edgeId: string) => void;
  isLightMode?: boolean;
}

export interface FlowCanvasRef {
  updateNode: (id: string, data: any) => void;
}

let nodeId = 1;

const addNodeToNeo4j = async (node: Node) => {
  try {
    const response = await fetch('http://localhost:5001/neo4j_add_node', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id: node.id,
          label: node.data.label,
          type: node.data?.type,
          description: node.data?.description || ""
        }
      })
    });

    if (!response.ok) throw new Error('Failed to add node to Neo4j');
    const result = await response.json();
    console.log("[FlowCanvas.tsx] Neo4j add_node:", result);
  } catch (err) {
    console.error("[FlowCanvas.tsx] Neo4j add node error:", err);
  }
};

const addEdgeToNeo4j = async (source_node: Node, target_node: Node) => {
  try {
    const response = await fetch('http://localhost:5001/neo4j_add_edge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id_source: source_node.id,
          flow_id_target: target_node.id
        }
      })
    });

    if (!response.ok) throw new Error('Failed to add edge to Neo4j');
    const result = await response.json();
    console.log("[FlowCanvas.tsx] Neo4j adding edge:", result);
  } catch (err) {
    console.error("[FlowCanvas.tsx] Neo4j adding edge error:", err);
  }
};

const deleteEdgeToNeo4j = async (source_node: Node, target_node: Node) => {
  try {
    const response = await fetch('http://localhost:5001/neo4j_delete_edge', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        properties: {
          flow_id_source: source_node.id,
          flow_id_target: target_node.id
        }
      })
    });

    if (!response.ok) throw new Error('Failed to delete edge to Neo4j');
    const result = await response.json();
    console.log("[FlowCanvas.tsx] Neo4j deleting edge:", result);
  } catch (err) {
    console.error("[FlowCanvas.tsx] Neo4j delete edge error:", err);
  }
};

const deleteNodeFromNeo4j = async (nodeId: string) => {
  try {
    const response = await fetch(`http://localhost:5001/neo4j_delete_node/${nodeId}`, {
      method: 'DELETE'
    });

    if (!response.ok) throw new Error('Failed to delete node from Neo4j');
    const result = await response.json();
    console.log("Neo4j delete_node:", result);
  } catch (err) {
    console.error("Neo4j delete node error:", err);
  }
};

const clearNeo4j = async () => {
  try {
    const response = await fetch('http://localhost:5001/neo4j_clear_nodes', {
      method: 'DELETE',
    });

    if (!response.ok) throw new Error('Failed to clear Neo4j');
    const result = await response.json();
    console.log("Neo4j cleared:", result);
  } catch (err) {
    console.error("Neo4j clear error:", err);
  }
};

export const FlowCanvas = forwardRef<FlowCanvasRef, FlowCanvasProps>(({ onNodeSelect, onNodesChange, onRemoveNode, onRemoveEdge, isLightMode }, ref) => {
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
  const [reactFlowInstance, setReactFlowInstance] = useState<any>(null);

  // Expose updateNode 
  const updateNode = useCallback((id: string, data: any) => {
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

      removedNodeIds.forEach(deleteNodeFromNeo4j);

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
    [nodes, selectedNode, onNodeSelect]
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
            deleteEdgeToNeo4j(sourceNode, targetNode);
          });
          return applyEdgeChanges(changes, eds);
        });
        return;
      }
      setEdges((eds) => applyEdgeChanges(changes, eds));
    },
    [nodes] 
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
      await addEdgeToNeo4j(sourceNode, targetNode);
    },
    [nodes] 
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
        addNodeToNeo4j(newNode); // 🔁 Sync to Neo4j
        return updated;
      });
    },
    [reactFlowInstance]
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
        const dataStr = JSON.stringify(flow);
        const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(dataStr);
        const exportFileDefaultName = 'ai-flow.json';
        const linkElement = document.createElement('a');
        linkElement.setAttribute('href', dataUri);
        linkElement.setAttribute('download', exportFileDefaultName);
        linkElement.click();

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

  const importFlow = (e: React.ChangeEvent<HTMLInputElement>) => {
    try {
      const fileReader = new FileReader();
      fileReader.onload = (event) => {
        if (event.target && event.target.result) {
          const flowData = JSON.parse(event.target.result as string);

          if (flowData.nodes && flowData.edges) {
            setNodes(flowData.nodes);
            setEdges(flowData.edges);

            const maxNodeId = Math.max(...flowData.nodes.map((node: Node) => parseInt(node.id.toString(), 10)));
            nodeId = maxNodeId + 1;

            toast.success('Flow imported successfully', {
              description: 'Your AI pipeline has been imported',
            });
          } else {
            toast.error('Invalid flow file', {
              description: 'The selected file does not contain a valid flow',
            });
          }
        }
      };

      if (e.target.files && e.target.files.length > 0) {
        fileReader.readAsText(e.target.files[0]);
      }
    } catch (error) {
      console.error('Error importing flow:', error);
      toast.error('Failed to import flow', {
        description: 'There was an error importing your pipeline',
      });
    }
  };

  const clearCanvas = async () => {
    setNodes([]);
    setEdges([]);
    localStorage.removeItem('ai-flow');
    localStorage.removeItem('ai-flow-nodes');
    localStorage.removeItem('ai-flow-edges');
    nodeId = 1;

    await clearNeo4j(); 

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

        <Panel position="top-center" className="mt-2">
          <div className="bg-card/90 backdrop-blur-sm border border-border rounded-lg py-1.5 px-3 text-xs flex items-center gap-2">
            <Button size="sm" variant="outline" onClick={saveFlow} className="flex items-center gap-1 h-7">
              <Save className="h-3.5 w-3.5" />
              Save
            </Button>
            <Button size="sm" variant="outline" onClick={exportFlow} className="flex items-center gap-1 h-7">
              <Download className="h-3.5 w-3.5" />
              Export
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="flex items-center gap-1 h-7"
              onClick={triggerImport}
            >
              <Upload className="h-3.5 w-3.5" />
              Import
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".json"
              className="hidden"
              onChange={importFlow}
            />
            <Button
              size="sm"
              variant="destructive"
              onClick={clearCanvas}
              className="flex items-center gap-1 h-7 bg-red-600 hover:bg-red-700 text-white"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Clear
            </Button>
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
});

interface WrappedFlowCanvasProps extends FlowCanvasProps {
  flowCanvasRef?: React.RefObject<FlowCanvasRef>;
}

export const WrappedFlowCanvas = ({ onNodeSelect, onNodesChange, isLightMode, flowCanvasRef }: WrappedFlowCanvasProps) => (
  <ReactFlowProvider>
    <FlowCanvas ref={flowCanvasRef} onNodeSelect={onNodeSelect} onNodesChange={onNodesChange} isLightMode={isLightMode} />
  </ReactFlowProvider>
);
