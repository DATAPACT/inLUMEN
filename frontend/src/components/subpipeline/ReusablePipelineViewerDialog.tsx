import { useEffect, useId, useMemo, useState } from "react";
import ReactFlow, { Background, Controls, ReactFlowProvider } from "reactflow";
import "reactflow/dist/style.css";

import { nodeTypes } from "@/components/NodeTypes";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { normalizeGraph } from "@/features/flow/flowGraph";
import { reusablePipelineNestingError, type SubpipelineReference } from "@/features/flow/subpipeline";
import { fetchReusablePipeline, type ReusablePipelineDefinition } from "@/features/flow/subpipelinePersistence";
import { getStepTypeLabel, normalizeNodePorts, normalizeType } from "@/features/nodes/nodeSchema";
import { PortDisplayContext } from "@/features/nodes/PortDisplayContext";

type Props = {
  reference: SubpipelineReference | null;
  onClose: () => void;
};

export function ReusablePipelineViewerDialog({ reference, onClose }: Props) {
  // Mount a fresh viewer for each reference so late requests cannot show another graph.
  return (
    <Dialog open={Boolean(reference)} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="flex h-[86vh] max-w-[95vw] flex-col sm:max-w-6xl" onKeyDown={(event) => {
        if (event.key !== "Escape") event.stopPropagation();
      }}>
        <DialogHeader>
          <DialogTitle>{reference?.pipeline_name || "Reusable pipeline"} — Read only</DialogTitle>
          <DialogDescription>Pan and zoom to explore. Select a component to inspect its details.</DialogDescription>
        </DialogHeader>
        {reference && <Viewer key={`${reference.pipeline_uid}:${reference.version_uid || ""}`} reference={reference} />}
      </DialogContent>
    </Dialog>
  );
}

function Viewer({ reference }: { reference: SubpipelineReference }) {
  const componentSelectId = useId();
  const [definition, setDefinition] = useState<ReusablePipelineDefinition | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [selectedId, setSelectedId] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    fetchReusablePipeline(reference.pipeline_uid, reference.version_uid)
      .then((result) => { if (!cancelled) setDefinition(result); })
      .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "Could not load reusable pipeline."); });
    return () => { cancelled = true; };
  }, [reference.pipeline_uid, reference.version_uid, attempt]);

  const graph = useMemo(() => normalizeGraph(definition?.graph || {}), [definition]);
  const nodes = useMemo(() => graph.nodes.map((node) => ({
    ...node, draggable: false, connectable: false, deletable: false,
    selected: node.id === selectedId,
  })), [graph.nodes, selectedId]);
  const edges = useMemo(() => graph.edges.map((edge) => ({ ...edge, updatable: false, deletable: false })), [graph.edges]);
  const selected = graph.nodes.find((node) => node.id === selectedId);
  const ports = normalizeNodePorts(selected?.data.ports, normalizeType(selected?.data.type));
  const nestingError = reusablePipelineNestingError(graph);

  if (error) return <div role="alert" className="space-y-3 p-4">
    <p>{error}</p><Button variant="outline" onClick={() => setAttempt((value) => value + 1)}>Try again</Button>
  </div>;
  if (!definition) return <p role="status" className="p-4 text-sm text-muted-foreground">Loading reusable pipeline…</p>;

  return <>
    {definition.description && <p className="text-sm text-muted-foreground">{definition.description}</p>}
    {nestingError && <p role="alert" className="text-sm text-amber-600">{nestingError} This saved definition is available for inspection only.</p>}
    <div className="grid min-h-0 flex-1 grid-rows-[minmax(200px,1fr)_minmax(120px,1fr)] gap-4 md:grid-cols-[minmax(0,1fr)_280px] md:grid-rows-1">
      <div className="min-h-0 overflow-hidden rounded-lg border" aria-label="Read-only pipeline canvas">
        <ReactFlowProvider>
          <PortDisplayContext.Provider value={{ advanced: true, validationByNode: {} }}>
            <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.1}
              nodesDraggable={false} nodesConnectable={false} edgesUpdatable={false}
              deleteKeyCode={null} onNodeClick={(_, node) => setSelectedId(node.id)}
              onPaneClick={() => setSelectedId("")}>
              <Background />
              <Controls showInteractive={false} />
            </ReactFlow>
          </PortDisplayContext.Provider>
        </ReactFlowProvider>
      </div>
      <aside className="min-h-0 space-y-4 overflow-y-auto rounded-lg border p-4" aria-label="Component details">
        <p className="text-xs text-muted-foreground">{graph.nodes.length} components · {graph.edges.length} connections</p>
        <div className="space-y-2 text-sm font-medium">
          <label htmlFor={componentSelectId}>Component</label>
          <select id={componentSelectId} className="h-9 w-full rounded-md border bg-background px-2 text-sm" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
            <option value="">Select a component…</option>
            {graph.nodes.map((node) => <option key={node.id} value={node.id}>{node.data.label || node.id}</option>)}
          </select>
        </div>
        {selected ? <div className="space-y-4 break-words text-sm">
          <div><h3 className="font-semibold">{selected.data.label || selected.id}</h3>
            <p className="text-xs text-muted-foreground">{getStepTypeLabel(selected.data.type)} · ID: {selected.id}</p></div>
          {selected.data.description && <p className="whitespace-pre-wrap">{selected.data.description}</p>}
          {selected.data.template_label && <p>Template: {selected.data.template_label}</p>}
          {(["inputs", "outputs"] as const).map((direction) => <div key={direction}>
            <h4 className="mb-1 font-medium">{direction === "inputs" ? "Inputs" : "Outputs"}</h4>
            {ports[direction].length ? <ul className="space-y-1 text-xs text-muted-foreground">
              {ports[direction].map((port) => <li key={port.id}>{port.name || port.id} · {port.type}{port.required ? " · Required" : ""}</li>)}
            </ul> : <p className="text-xs text-muted-foreground">None</p>}
          </div>)}
          <div><h4 className="mb-1 font-medium">Attached files</h4>
            {selected.data.files?.length ? <ul className="space-y-1 text-xs text-muted-foreground">
              {selected.data.files.map((file: string | { filename?: string }, index: number) => <li key={index}>{typeof file === "string" ? file : file.filename}</li>)}
            </ul> : <p className="text-xs text-muted-foreground">None</p>}
          </div>
        </div> : <p className="text-sm text-muted-foreground">Select a component on the canvas or from the list.</p>}
      </aside>
    </div>
  </>;
}
