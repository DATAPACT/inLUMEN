import React, { useState, useCallback, useRef, useEffect } from 'react';
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

let nodeId = 1;

export function FlowCanvas({ onNodeSelect, onNodesChange, onRemoveNode, onRemoveEdge, isLightMode }: FlowCanvasProps) {
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
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const triggerImport = () => {
    fileInputRef.current?.click();
  };

  // Persist state to localStorage
  useEffect(() => {
    localStorage.setItem('ai-flow-nodes', JSON.stringify(nodes));
    localStorage.setItem('ai-flow-edges', JSON.stringify(edges));
  }, [nodes, edges]);

  // Notify parent of node changes
  useEffect(() => {
    if (onNodesChange) onNodesChange(nodes);
  }, [nodes, onNodesChange]);

  const onNodesChangeInternal = useCallback(
    (changes: NodeChange[]) => {
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
    (changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)),
    []
  );

  const onConnect = useCallback(
    (params: Connection) => {
      if (params.source === params.target) {
        toast("Cannot connect a node to itself", { description: "Please connect to a different node" });
        return;
      }

      const connectionExists = edges.some(
        edge => edge.source === params.source && edge.target === params.target
      );
      if (connectionExists) {
        toast("Connection already exists", { description: "This connection is already in place" });
        return;
      }

      setEdges((eds) => addEdge(params, eds));
    },
    [edges]
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

      setNodes((nds) => nds.concat(newNode));
    },
    [reactFlowInstance]
  );

  // Save flow to localStorage
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

  // Export as JSON
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

  // Import from JSON
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

  // 🧹 Clear Canvas Function
  const clearCanvas = () => {
    setNodes([]);
    setEdges([]);
    localStorage.removeItem('ai-flow');
    localStorage.removeItem('ai-flow-nodes');
    localStorage.removeItem('ai-flow-edges');
    nodeId = 1;

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

        {/* ✅ Updated MiniMap color mapping */}
        <MiniMap 
          nodeColor={n => {
            switch (n.data.type) {
              case 'config': return '#0EA5E9';          // sky
              case 'input': return '#3B82F6';           // blue
              case 'action': return '#84CC16';          // lime/yellow for processing steps
              case 'output': return '#10B981';          // green
              case 'api': return '#F43F5E';             // rose
              case 'storage': return '#14B8A6';         // teal
              case 'custom': return '#8B5CF6';          // violet
              default: return '#6B7280';                // gray
            }
          }}
          maskColor="rgba(0, 0, 0, 0.1)"
          className="bg-card/70 border border-border rounded-md"
        />

        {/* Top Toolbar */}
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
}

export const WrappedFlowCanvas = ({ onNodeSelect, onNodesChange, isLightMode }: FlowCanvasProps) => (
  <ReactFlowProvider>
    <FlowCanvas onNodeSelect={onNodeSelect} onNodesChange={onNodesChange} isLightMode={isLightMode} />
  </ReactFlowProvider>
);
