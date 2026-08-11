import React, { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Connection,
  ConnectionLineType,
  Controls,
  Edge,
  MarkerType,
  MiniMap,
  Node,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type EdgeChange,
  type NodeChange,
} from "reactflow";

import { CustomNode } from "@/components/NodeTypes";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { PortDisplayContext } from "@/features/nodes/PortDisplayContext";
import {
  DEFAULT_NODE_PORTS,
  normalizeNodePorts,
  normalizeType,
  type NodePort,
  type NodePorts,
  type StepType,
} from "@/features/nodes/nodeSchema";
import { normalizeGraph } from "@/features/flow/flowGraph";
import {
  conversationUnderstandingSubpipeline,
  deriveSubpipelineInterface,
  publicPortsForSubpipeline,
  type ReusablePipelineSaveDraft,
} from "@/features/flow/subpipeline";
import {
  fetchReusablePipelineVersion,
  type ReusablePipelineSummary,
} from "@/features/flow/subpipelinePersistence";
import { validateGraph } from "@/features/flow/flowValidation";

const nodeTypes = { custom: CustomNode };

const PORT_TYPES = [
  "Text", "Number", "Boolean", "Object", "Object[]", "Dataset", "File", "File[]",
  "Image", "Image[]", "Audio", "Message", "Message[]", "Document", "Document[]",
  "Vector", "Vector[]", "FeatureSet", "Model", "Prediction[]",
] as const;

const cloneDefaultPorts = (kind: StepType): NodePorts => ({
  inputs: DEFAULT_NODE_PORTS[kind].inputs.map((port) => ({ ...port, type: "Object" })),
  outputs: DEFAULT_NODE_PORTS[kind].outputs.map((port) => ({ ...port, type: "Object" })),
});

const portsForNewKind = (kind: StepType): NodePorts => {
  if (kind === "flow") {
    return {
      inputs: [{ id: "value", name: "value", type: "Object", required: true, description: "Value to evaluate." }],
      outputs: [
        { id: "when_true", name: "when_true", type: "Object", required: true, description: "True branch value." },
        { id: "when_false", name: "when_false", type: "Object", required: false, description: "False branch value." },
      ],
    };
  }
  return cloneDefaultPorts(kind);
};

const newNestedNode = (kind: StepType, index: number): Node => ({
  id: `nested-${kind}-${Date.now()}-${index}`,
  type: "custom",
  position: { x: 80 + index * 260, y: 180 },
  data: {
    type: kind,
    label: kind === "source"
      ? "Pipeline Input"
      : kind === "destination"
        ? "Pipeline Output"
        : kind === "flow"
          ? "Condition"
          : kind === "subpipeline"
            ? "Referenced Pipeline"
            : "Processing Step",
    description: kind === "source"
      ? "Logical input supplied by the parent pipeline."
      : kind === "destination"
        ? "Logical output returned to the parent pipeline."
        : kind === "flow"
          ? "Route data based on an explicit condition."
          : kind === "subpipeline"
            ? "Invoke another pinned reusable pipeline version."
            : "Process data inside the reusable pipeline.",
    template_label: kind === "source"
      ? "Subpipeline Input"
      : kind === "destination"
        ? "Subpipeline Output"
        : kind === "flow"
          ? "Condition"
          : kind === "subpipeline"
            ? "Subpipeline"
            : "Custom Logic",
    ports: portsForNewKind(kind),
    param: kind === "flow" ? { expression: 'value.status == "active"' } : {},
    ...(kind === "task" ? {
      implementation: { kind: "generated-code", task: "nested_pipeline_step", execution_profile: "deterministic" },
    } : {}),
  },
});

type Props = {
  open: boolean;
  pipelineUid?: string;
  name: string;
  description?: string;
  suggestedVersionName?: string;
  reusablePipelines?: ReusablePipelineSummary[];
  graph?: { nodes?: unknown[]; edges?: unknown[] };
  onOpenChange: (open: boolean) => void;
  onSave: (definition: ReusablePipelineSaveDraft) => void | Promise<void>;
};

export function SubpipelineEditorDialog({
  open,
  pipelineUid = "",
  name,
  description = "",
  suggestedVersionName = "Version 1",
  reusablePipelines = [],
  graph,
  onOpenChange,
  onSave,
}: Props) {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [saveError, setSaveError] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [isDirty, setIsDirty] = useState(false);
  const [isDiscardOpen, setIsDiscardOpen] = useState(false);
  const [pipelineName, setPipelineName] = useState("");
  const [pipelineDescription, setPipelineDescription] = useState("");
  const [versionName, setVersionName] = useState("");
  const [parameterDraft, setParameterDraft] = useState("{}");
  const [parameterError, setParameterError] = useState("");
  const [isResolvingNestedReference, setIsResolvingNestedReference] = useState(false);

  useEffect(() => {
    if (!open) return;
    const normalized = normalizeGraph({
      nodes: Array.isArray(graph?.nodes) ? graph.nodes : [],
      edges: Array.isArray(graph?.edges) ? graph.edges : [],
    });
    setNodes(normalized.nodes);
    setEdges(normalized.edges);
    setSelectedNodeId("");
    setSaveError("");
    setPipelineName(name || "Reusable Pipeline");
    setPipelineDescription(description || "");
    setVersionName(suggestedVersionName || "Version 1");
    setIsDirty(false);
    setIsDiscardOpen(false);
  }, [description, graph, name, open, suggestedVersionName]);

  const validation = useMemo(() => validateGraph(nodes, edges), [edges, nodes]);
  const nestedInterface = useMemo(
    () => deriveSubpipelineInterface({ nodes, edges }),
    [edges, nodes],
  );
  const selectedNode = nodes.find((node) => node.id === selectedNodeId) || null;

  useEffect(() => {
    setParameterDraft(JSON.stringify(selectedNode?.data?.param || {}, null, 2));
    setParameterError("");
  }, [selectedNode?.data?.param, selectedNodeId]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      if (changes.some((change) => !["select", "dimensions"].includes(change.type))) setIsDirty(true);
      setNodes((current) => applyNodeChanges(changes, current));
    },
    [],
  );
  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      if (changes.some((change) => change.type !== "select")) setIsDirty(true);
      setEdges((current) => applyEdgeChanges(changes, current));
    },
    [],
  );
  const onConnect = useCallback((connection: Connection) => {
    if (!connection.sourceHandle || !connection.targetHandle) return;
    setIsDirty(true);
    setEdges((current) => addEdge({
      ...connection,
      id: `nested-${connection.source}-${connection.sourceHandle}-${connection.target}-${connection.targetHandle}`,
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed },
    }, current));
  }, []);

  const addNode = (kind: StepType) => {
    setIsDirty(true);
    setNodes((current) => [...current, newNestedNode(kind, current.length)]);
  };

  const updateSelectedNode = (patch: Record<string, unknown>) => {
    if (!selectedNodeId) return;
    setIsDirty(true);
    setNodes((current) => current.map((node) => node.id === selectedNodeId
      ? { ...node, data: { ...node.data, ...patch } }
      : node));
  };

  const changeSelectedKind = (kind: StepType) => {
    if (!selectedNode) return;
    const previousKind = normalizeType(selectedNode.data?.type);
    updateSelectedNode({
      type: kind,
      ports: previousKind === kind
        ? normalizeNodePorts(selectedNode.data?.ports, kind)
        : portsForNewKind(kind),
      template_label: previousKind === kind
        ? selectedNode.data?.template_label
        : kind === "flow"
          ? "Condition"
          : kind === "subpipeline"
            ? "Subpipeline"
            : selectedNode.data?.template_label,
      param: kind === "flow" ? { expression: 'value.status == "active"' } : {},
      ...(kind === "task" && !selectedNode.data?.implementation ? {
        implementation: { kind: "generated-code", task: "nested_pipeline_step", execution_profile: "deterministic" },
      } : {}),
    });
  };

  const pushPorts = (nextPorts: NodePorts) => updateSelectedNode({ ports: nextPorts });

  const updatePort = (
    direction: keyof NodePorts,
    portId: string,
    patch: Partial<NodePort>,
  ) => {
    if (!selectedNode) return;
    const current = normalizeNodePorts(selectedNode.data?.ports, normalizeType(selectedNode.data?.type));
    const nextId = String(patch.id || portId).trim() || portId;
    pushPorts({
      ...current,
      [direction]: current[direction].map((port) => port.id === portId
        ? { ...port, ...patch, id: nextId, name: String(patch.name ?? port.name) }
        : port),
    });
    if (nextId !== portId) {
      setEdges((currentEdges) => currentEdges.map((edge) => ({
        ...edge,
        ...(direction === "outputs" && edge.source === selectedNode.id && edge.sourceHandle === portId
          ? { sourceHandle: nextId }
          : {}),
        ...(direction === "inputs" && edge.target === selectedNode.id && edge.targetHandle === portId
          ? { targetHandle: nextId }
          : {}),
      })));
    }
  };

  const addPort = (direction: keyof NodePorts) => {
    if (!selectedNode) return;
    const current = normalizeNodePorts(selectedNode.data?.ports, normalizeType(selectedNode.data?.type));
    let index = current[direction].length + 1;
    let id = `${direction === "inputs" ? "input" : "output"}_${index}`;
    while (current[direction].some((port) => port.id === id)) {
      index += 1;
      id = `${direction === "inputs" ? "input" : "output"}_${index}`;
    }
    pushPorts({
      ...current,
      [direction]: [...current[direction], {
        id,
        name: id,
        type: "Object",
        required: true,
        description: "",
      }],
    });
  };

  const removePort = (direction: keyof NodePorts, portId: string) => {
    if (!selectedNode) return;
    const current = normalizeNodePorts(selectedNode.data?.ports, normalizeType(selectedNode.data?.type));
    pushPorts({
      ...current,
      [direction]: current[direction].filter((port) => port.id !== portId),
    });
    setEdges((currentEdges) => currentEdges.filter((edge) => !(
      (direction === "outputs" && edge.source === selectedNode.id && edge.sourceHandle === portId)
      || (direction === "inputs" && edge.target === selectedNode.id && edge.targetHandle === portId)
    )));
  };

  const commitParameters = () => {
    try {
      const parsed = JSON.parse(parameterDraft);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Parameters must be a JSON object.");
      }
      updateSelectedNode({ param: parsed });
      setParameterError("");
    } catch (error) {
      setParameterError(error instanceof Error ? error.message : "Invalid JSON parameters.");
    }
  };

  const attachNestedReference = async (value: string) => {
    if (!selectedNode || !value) return;
    const [selectedPipelineUid, selectedVersionUid] = value.split("::");
    if (!selectedPipelineUid || !selectedVersionUid) return;
    try {
      setIsResolvingNestedReference(true);
      const version = await fetchReusablePipelineVersion(selectedPipelineUid, selectedVersionUid);
      updateSelectedNode({
        label: version.reference.pipeline_name,
        template_label: "Subpipeline",
        ports: publicPortsForSubpipeline({ interface: version.interface }),
        subpipeline: {
          version: 2,
          reference: version.reference,
          interface: version.interface,
          resolved_graph: version.graph,
          expanded: false,
        },
      });
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Could not attach nested reusable pipeline.");
    } finally {
      setIsResolvingNestedReference(false);
    }
  };

  const requestClose = () => {
    if (isDirty) setIsDiscardOpen(true);
    else onOpenChange(false);
  };

  const loadConversationExample = () => {
    const example = conversationUnderstandingSubpipeline();
    setNodes(example.graph.nodes);
    setEdges(example.graph.edges);
    setSelectedNodeId("");
    setSaveError("");
    setIsDirty(true);
  };

  const save = async () => {
    if (!pipelineName.trim() || !versionName.trim()) {
      setSaveError("Pipeline name and version name are required.");
      return;
    }
    if (nodes.length === 0) {
      setSaveError("Add nested components or load the Conversation Understanding example.");
      return;
    }
    if (nestedInterface.inputs.length === 0 || nestedInterface.outputs.length === 0) {
      setSaveError("A Subpipeline needs at least one Source boundary and one Destination boundary.");
      return;
    }
    if (!validation.valid) {
      setSaveError("Resolve the pipeline validation errors before saving.");
      return;
    }
    const untypedNodes = nodes.filter((node) => {
      const nodePorts = normalizeNodePorts(node.data?.ports, normalizeType(node.data?.type));
      return [...nodePorts.inputs, ...nodePorts.outputs].some((port) =>
        !port.id.trim() || ["", "any", "unknown", "*"].includes(String(port.type || "").trim().toLowerCase()));
    });
    if (untypedNodes.length > 0) {
      setSaveError(`Define explicit non-generic port types for: ${untypedNodes.map((node) => node.data?.label || node.id).join(", ")}.`);
      return;
    }
    try {
      setIsSaving(true);
      await onSave({
        name: pipelineName.trim(),
        description: pipelineDescription.trim(),
        versionName: versionName.trim(),
        graph: { updated_at: null, nodes, edges },
        interface: nestedInterface,
      });
      setIsDirty(false);
      onOpenChange(false);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Failed to save reusable pipeline.");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <>
    <Dialog open={open} onOpenChange={(nextOpen) => nextOpen ? onOpenChange(true) : requestClose()}>
      <DialogContent className="h-[92vh] max-w-[96vw] grid-rows-[auto_1fr_auto] overflow-hidden p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle>{name || "Reusable pipeline"} · Pipeline editor</DialogTitle>
          <DialogDescription>
            This is a separately saved pipeline. Source outputs become its public inputs; Destination inputs become its public outputs.
          </DialogDescription>
        </DialogHeader>

        <div className="grid min-h-0 grid-cols-[minmax(0,1fr)_300px]">
          <div className="relative min-h-0 border-r bg-slate-950">
            <div className="absolute left-3 top-3 z-10 flex flex-wrap gap-2 rounded-lg border bg-background/95 p-2 shadow">
              <Button size="sm" variant="outline" onClick={() => addNode("source")}>+ Input boundary</Button>
              <Button size="sm" variant="outline" onClick={() => addNode("task")}>+ Task</Button>
              <Button size="sm" variant="outline" onClick={() => addNode("flow")}>+ Flow</Button>
              <Button size="sm" variant="outline" onClick={() => addNode("subpipeline")}>+ Subpipeline</Button>
              <Button size="sm" variant="outline" onClick={() => addNode("destination")}>+ Output boundary</Button>
              <Button size="sm" onClick={loadConversationExample}>Load conversation example</Button>
            </div>
            <PortDisplayContext.Provider value={{ advanced: true, validationByNode: validation.byNode }}>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeClick={(_event, node) => setSelectedNodeId(node.id)}
                fitView
                fitViewOptions={{ padding: 0.2 }}
                minZoom={0.25}
                connectionLineType={ConnectionLineType.SmoothStep}
              >
                <Background gap={20} size={1} />
                <Controls />
                <MiniMap pannable zoomable />
              </ReactFlow>
            </PortDisplayContext.Provider>
          </div>

          <aside className="min-h-0 space-y-5 overflow-y-auto p-4">
            <section className="space-y-3 rounded-lg border p-3">
              <div className="text-sm font-semibold">Pipeline details</div>
              <div className="space-y-1.5">
                <Label htmlFor="reusable-pipeline-name">Name</Label>
                <Input
                  id="reusable-pipeline-name"
                  value={pipelineName}
                  onChange={(event) => { setPipelineName(event.target.value); setIsDirty(true); }}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reusable-version-name">New version</Label>
                <Input
                  id="reusable-version-name"
                  value={versionName}
                  onChange={(event) => { setVersionName(event.target.value); setIsDirty(true); }}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reusable-pipeline-description">Description</Label>
                <Textarea
                  id="reusable-pipeline-description"
                  value={pipelineDescription}
                  onChange={(event) => { setPipelineDescription(event.target.value); setIsDirty(true); }}
                />
              </div>
            </section>
            <section className="space-y-2 rounded-lg border p-3">
              <div className="text-sm font-semibold">Public contract</div>
              <div className="text-xs text-muted-foreground">Inputs</div>
              {nestedInterface.inputs.length > 0 ? nestedInterface.inputs.map((port) => (
                <div key={`input-${port.id}`} className="rounded bg-muted px-2 py-1 text-xs">
                  <span className="font-medium">{port.name}</span> · {port.type}
                </div>
              )) : <div className="text-xs text-amber-500">Add a Source boundary.</div>}
              <div className="pt-1 text-xs text-muted-foreground">Outputs</div>
              {nestedInterface.outputs.length > 0 ? nestedInterface.outputs.map((port) => (
                <div key={`output-${port.id}`} className="rounded bg-muted px-2 py-1 text-xs">
                  <span className="font-medium">{port.name}</span> · {port.type}
                </div>
              )) : <div className="text-xs text-amber-500">Add a Destination boundary.</div>}
            </section>

            <section className="space-y-3 rounded-lg border p-3">
              <div className="text-sm font-semibold">Selected component</div>
              {selectedNode ? (
                <>
                  <div className="space-y-1.5">
                    <Label htmlFor="nested-kind">Kind</Label>
                    <select
                      id="nested-kind"
                      value={normalizeType(selectedNode.data?.type)}
                      onChange={(event) => changeSelectedKind(event.target.value as StepType)}
                      className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                    >
                      <option value="source">Input boundary</option>
                      <option value="task">Task</option>
                      <option value="flow">Flow</option>
                      <option value="subpipeline">Subpipeline</option>
                      <option value="destination">Output boundary</option>
                    </select>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="nested-label">Label</Label>
                    <Input
                      id="nested-label"
                      value={String(selectedNode.data?.label || "")}
                      onChange={(event) => updateSelectedNode({ label: event.target.value })}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="nested-template">Template</Label>
                    <Input
                      id="nested-template"
                      value={String(selectedNode.data?.template_label || "")}
                      onChange={(event) => updateSelectedNode({ template_label: event.target.value })}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="nested-description">Description</Label>
                    <Textarea
                      id="nested-description"
                      value={String(selectedNode.data?.description || "")}
                      onChange={(event) => updateSelectedNode({ description: event.target.value })}
                    />
                  </div>
                  {normalizeType(selectedNode.data?.type) === "subpipeline" && (
                    <div className="space-y-1.5">
                      <Label htmlFor="nested-reference">Pinned reusable version</Label>
                      <select
                        id="nested-reference"
                        value={selectedNode.data?.subpipeline?.reference
                          ? `${selectedNode.data.subpipeline.reference.pipeline_uid}::${selectedNode.data.subpipeline.reference.version_uid}`
                          : ""}
                        disabled={isResolvingNestedReference}
                        onChange={(event) => { void attachNestedReference(event.target.value); }}
                        className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                      >
                        <option value="">Select a reusable pipeline…</option>
                        {reusablePipelines
                          .filter((pipeline) => pipeline.uid !== pipelineUid)
                          .flatMap((pipeline) => pipeline.versions.map((version) => (
                            <option key={`${pipeline.uid}::${version.uid}`} value={`${pipeline.uid}::${version.uid}`}>
                              {pipeline.name} · {version.name}
                            </option>
                          )))}
                      </select>
                    </div>
                  )}
                  {normalizeType(selectedNode.data?.type) === "task" && (
                    <div className="space-y-2 rounded-md border p-2">
                      <div className="text-xs font-semibold uppercase text-muted-foreground">Implementation</div>
                      <Input
                        aria-label="Implementation kind"
                        value={String(selectedNode.data?.implementation?.kind || "generated-code")}
                        onChange={(event) => updateSelectedNode({
                          implementation: { ...(selectedNode.data?.implementation || {}), kind: event.target.value },
                        })}
                        placeholder="generated-code"
                      />
                      <Input
                        aria-label="Implementation task"
                        value={String(selectedNode.data?.implementation?.task || "")}
                        onChange={(event) => updateSelectedNode({
                          implementation: { ...(selectedNode.data?.implementation || {}), task: event.target.value },
                        })}
                        placeholder="task identifier"
                      />
                      <Input
                        aria-label="Execution profile"
                        value={String(selectedNode.data?.implementation?.execution_profile || "")}
                        onChange={(event) => updateSelectedNode({
                          implementation: { ...(selectedNode.data?.implementation || {}), execution_profile: event.target.value },
                        })}
                        placeholder="deterministic"
                      />
                    </div>
                  )}
                  {normalizeType(selectedNode.data?.type) === "flow" && (
                    <div className="space-y-2 rounded-md border p-2">
                      <div className="text-xs font-semibold uppercase text-muted-foreground">Flow behavior</div>
                      <select
                        value={String(selectedNode.data?.template_label || "Condition")}
                        onChange={(event) => {
                          const behavior = event.target.value;
                          updateSelectedNode({
                            template_label: behavior,
                            ports: behavior === "Condition"
                              ? portsForNewKind("flow")
                              : {
                                  inputs: [{ id: "items", name: "items", type: "Object[]", required: true, description: "Items to process." }],
                                  outputs: [{ id: "item", name: "item", type: "Object", required: true, description: "One mapped item." }],
                                },
                            param: behavior === "Condition"
                              ? { expression: 'value.status == "active"' }
                              : { max_concurrency: 4, failure_policy: "stop" },
                          });
                        }}
                        className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                      >
                        <option value="Condition">Condition</option>
                        <option value="Parallel Map">Parallel Map</option>
                      </select>
                    </div>
                  )}
                  {!(["source", "destination", "subpipeline"] as StepType[]).includes(normalizeType(selectedNode.data?.type)) && (
                    <div className="space-y-1.5">
                      <Label htmlFor="nested-parameters">Parameters (JSON)</Label>
                      <Textarea
                        id="nested-parameters"
                        value={parameterDraft}
                        onChange={(event) => { setParameterDraft(event.target.value); setIsDirty(true); }}
                        onBlur={commitParameters}
                        className="min-h-24 font-mono text-xs"
                      />
                      {parameterError && <p className="text-xs text-red-500">{parameterError}</p>}
                    </div>
                  )}
                  {(["inputs", "outputs"] as Array<keyof NodePorts>).map((direction) => {
                    const kind = normalizeType(selectedNode.data?.type);
                    const blocked = (kind === "source" && direction === "inputs")
                      || (kind === "destination" && direction === "outputs");
                    const selectedPorts = normalizeNodePorts(selectedNode.data?.ports, kind)[direction];
                    return (
                      <div key={direction} className="space-y-2 rounded-md border p-2">
                        <div className="flex items-center justify-between">
                          <div className="text-xs font-semibold uppercase text-muted-foreground">{direction}</div>
                          {!blocked && kind !== "subpipeline" && (
                            <Button size="sm" variant="outline" onClick={() => addPort(direction)}>Add</Button>
                          )}
                        </div>
                        {selectedPorts.map((port) => (
                          <div key={port.id} className="space-y-2 rounded bg-muted/40 p-2">
                            <div className="grid grid-cols-2 gap-2">
                              <Input
                                aria-label={`${direction} port id`}
                                value={port.id}
                                disabled={kind === "subpipeline"}
                                onChange={(event) => updatePort(direction, port.id, { id: event.target.value })}
                                placeholder="port_id"
                              />
                              <Input
                                aria-label={`${direction} port name`}
                                value={port.name}
                                disabled={kind === "subpipeline"}
                                onChange={(event) => updatePort(direction, port.id, { name: event.target.value })}
                                placeholder="Display name"
                              />
                            </div>
                            <select
                              aria-label={`${direction} port type`}
                              value={port.type}
                              disabled={kind === "subpipeline"}
                              onChange={(event) => updatePort(direction, port.id, { type: event.target.value })}
                              className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                            >
                              <option value="any">Choose a type…</option>
                              {PORT_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
                            </select>
                            <Input
                              aria-label={`${direction} port description`}
                              value={port.description || ""}
                              disabled={kind === "subpipeline"}
                              onChange={(event) => updatePort(direction, port.id, { description: event.target.value })}
                              placeholder="Contract description"
                            />
                            <div className="flex items-center justify-between">
                              <label className="flex items-center gap-2 text-xs">
                                <input
                                  type="checkbox"
                                  checked={port.required !== false}
                                  disabled={kind === "subpipeline"}
                                  onChange={(event) => updatePort(direction, port.id, { required: event.target.checked })}
                                />
                                Required
                              </label>
                              {!blocked && kind !== "subpipeline" && (
                                <Button size="sm" variant="ghost" onClick={() => removePort(direction, port.id)}>Remove</Button>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    );
                  })}
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => {
                      setIsDirty(true);
                      setNodes((current) => current.filter((node) => node.id !== selectedNode.id));
                      setEdges((current) => current.filter((edge) => edge.source !== selectedNode.id && edge.target !== selectedNode.id));
                      setSelectedNodeId("");
                    }}
                  >
                    Remove component
                  </Button>
                </>
              ) : (
                <div className="text-xs text-muted-foreground">Select a nested component to edit it.</div>
              )}
            </section>

            <section className="rounded-lg border p-3 text-xs">
              <div className={nodes.length === 0 ? "text-amber-500" : validation.valid ? "text-emerald-500" : "text-red-500"}>
                {nodes.length === 0
                  ? "No pipeline steps yet"
                  : validation.valid
                  ? `Valid pipeline graph · ${nodes.length} components`
                  : `${validation.issues.filter((issue) => issue.severity === "error").length} validation error(s)`}
              </div>
              {validation.issues.slice(0, 5).map((issue, index) => (
                <div key={`${issue.code}-${index}`} className="mt-1 text-muted-foreground">{issue.message}</div>
              ))}
            </section>
          </aside>
        </div>

        <DialogFooter className="border-t px-6 py-4">
          {saveError && <div className="mr-auto text-sm text-red-500">{saveError}</div>}
          <Button variant="outline" onClick={requestClose}>Cancel</Button>
          <Button onClick={() => { void save(); }} disabled={isSaving}>
            {isSaving ? "Saving reusable pipeline…" : "Save reusable pipeline version"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <AlertDialog open={isDiscardOpen} onOpenChange={setIsDiscardOpen}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Discard unsaved pipeline changes?</AlertDialogTitle>
          <AlertDialogDescription>
            The reusable pipeline draft has changes that have not been saved as a version.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Continue editing</AlertDialogCancel>
          <AlertDialogAction
            onClick={() => {
              setIsDirty(false);
              setIsDiscardOpen(false);
              onOpenChange(false);
            }}
          >
            Discard changes
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    </>
  );
}
