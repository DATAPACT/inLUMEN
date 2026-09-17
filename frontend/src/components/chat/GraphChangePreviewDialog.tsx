import React, { useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  Edge,
  MarkerType,
  Node,
  ReactFlowProvider,
} from "reactflow";
import "reactflow/dist/style.css";
import { ArrowRight, Check, CircleAlert, Minus, Plus, RefreshCw } from "lucide-react";
import { nodeTypes } from "@/components/NodeTypes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PortDisplayContext } from "@/features/nodes/PortDisplayContext";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { normalizeGraph } from "@/features/flow/flowGraph";
import { cn } from "@/lib/utils";

type GraphChangePreviewDialogProps = {
  open: boolean;
  baseline: unknown;
  proposal: unknown;
  stale?: boolean;
  isApplying?: boolean;
  onApply: () => void;
  onDiscard: () => void;
};

type ChangeItem = { id: string; label: string };

const stableValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !["selected", "dragging", "positionAbsolute", "width", "height", "measured", "validation_issues", "connected_ports"].includes(key))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, stableValue(item)]),
  );
};

const nodeSignature = (node: Node) => {
  const data = { ...(node.data || {}) };
  // Backend and canvas snapshots expose file references under different
  // compatibility fields. Compare only their durable identifying properties.
  const files: unknown[] = Array.isArray(data.file_buckets)
    ? data.file_buckets as unknown[]
    : Array.isArray(data.files) ? data.files as unknown[] : [];
  const normalizedFiles: Array<Record<string, unknown>> = files.flatMap((file: unknown) => {
    if (typeof file === "string") return [{ filename: file }];
    if (!file || typeof file !== "object") return [];
    const entry = file as Record<string, unknown>;
    const filename = String(entry.filename || entry.name || "").trim();
    if (!filename) return [];
    return [{
      filename,
      ...(entry.bucket ? { bucket: entry.bucket } : {}),
      ...(entry.role ? { role: entry.role } : {}),
      ...(entry.snapshot_bucket ? { snapshot_bucket: entry.snapshot_bucket } : {}),
      ...(entry.snapshot_object ? { snapshot_object: entry.snapshot_object } : {}),
    }];
  });
  data.files = normalizedFiles.sort((a, b) => String(a.filename).localeCompare(String(b.filename)));
  delete data.file_buckets;
  // Layout-only differences are not pipeline changes and should not appear as
  // every step being updated after an informational/no-op request.
  return JSON.stringify(stableValue({ data }));
};

const edgeKey = (edge: Edge) => [
  edge.source,
  edge.sourceHandle || "",
  edge.target,
  edge.targetHandle || "",
].join("|");

const nodeLabel = (node: Node) => String(node.data?.label || node.id || "Untitled step");

const buildChanges = (baseline: unknown, proposal: unknown) => {
  const before = normalizeGraph(baseline);
  const after = normalizeGraph(proposal);
  const beforeNodes = new Map(before.nodes.map((node) => [node.id, node]));
  const afterNodes = new Map(after.nodes.map((node) => [node.id, node]));
  const addedNodes: ChangeItem[] = [];
  const updatedNodes: ChangeItem[] = [];
  const removedNodes: ChangeItem[] = [];

  after.nodes.forEach((node) => {
    const previous = beforeNodes.get(node.id);
    if (!previous) addedNodes.push({ id: node.id, label: nodeLabel(node) });
    else if (nodeSignature(previous) !== nodeSignature(node)) {
      updatedNodes.push({ id: node.id, label: nodeLabel(node) });
    }
  });
  before.nodes.forEach((node) => {
    if (!afterNodes.has(node.id)) removedNodes.push({ id: node.id, label: nodeLabel(node) });
  });

  const beforeEdges = new Map(before.edges.map((edge) => [edgeKey(edge), edge]));
  const afterEdges = new Map(after.edges.map((edge) => [edgeKey(edge), edge]));
  const addedEdges = after.edges.filter((edge) => !beforeEdges.has(edgeKey(edge)));
  const removedEdges = before.edges.filter((edge) => !afterEdges.has(edgeKey(edge)));

  return { before, after, addedNodes, updatedNodes, removedNodes, addedEdges, removedEdges };
};

const ChangeSection = ({
  title,
  items,
  tone,
  icon,
}: {
  title: string;
  items: ChangeItem[];
  tone: "green" | "amber" | "rose";
  icon: React.ReactNode;
}) => {
  if (items.length === 0) return null;
  const toneClass = {
    green: "text-emerald-500",
    amber: "text-amber-500",
    rose: "text-rose-500",
  }[tone];
  return (
    <section className="space-y-1.5">
      <h3 className={cn("flex items-center gap-1.5 text-xs font-semibold", toneClass)}>
        {icon}
        {title} <span className="text-muted-foreground">({items.length})</span>
      </h3>
      <ul className="space-y-1 pl-5 text-xs text-foreground">
        {items.map((item) => <li key={item.id} className="truncate" title={item.label}>{item.label}</li>)}
      </ul>
    </section>
  );
};

export const GraphChangePreviewDialog = ({
  open,
  baseline,
  proposal,
  stale = false,
  isApplying = false,
  onApply,
  onDiscard,
}: GraphChangePreviewDialogProps) => {
  const changes = useMemo(() => buildChanges(baseline, proposal), [baseline, proposal]);
  const nodeIds = useMemo(() => new Set([
    ...changes.addedNodes.map((item) => item.id),
    ...changes.updatedNodes.map((item) => item.id),
  ]), [changes.addedNodes, changes.updatedNodes]);
  const addedEdgeKeys = useMemo(() => new Set(changes.addedEdges.map(edgeKey)), [changes.addedEdges]);
  const previewNodes = useMemo(() => changes.after.nodes.map((node) => {
    const added = !changes.before.nodes.some((oldNode) => oldNode.id === node.id);
    const changed = nodeIds.has(node.id);
    return {
      ...node,
      style: {
        ...node.style,
        ...(changed ? {
          border: `2px solid ${added ? "#34d399" : "#fbbf24"}`,
          borderRadius: 12,
          boxShadow: `0 0 0 4px ${added ? "rgba(52,211,153,0.15)" : "rgba(251,191,36,0.14)"}`,
        } : {}),
      },
    };
  }), [changes.after.nodes, changes.before.nodes, nodeIds]);
  const previewEdges = useMemo(() => changes.after.edges.map((edge) => ({
    ...edge,
    type: edge.type || "smoothstep",
    style: {
      ...edge.style,
      ...(addedEdgeKeys.has(edgeKey(edge)) ? { stroke: "#34d399", strokeWidth: 3 } : {}),
    },
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 14,
      height: 14,
      color: addedEdgeKeys.has(edgeKey(edge))
        ? "#34d399"
        : "hsl(var(--muted-foreground))",
    },
  })), [addedEdgeKeys, changes.after.edges]);
  const totalChanges = changes.addedNodes.length + changes.updatedNodes.length
    + changes.removedNodes.length + changes.addedEdges.length + changes.removedEdges.length;

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!nextOpen) onDiscard(); }}>
      <DialogContent
        className="flex h-[86vh] max-h-[900px] w-[min(96vw,1440px)] max-w-none flex-col gap-0 p-0"
        onInteractOutside={(event) => event.preventDefault()}
      >
        <DialogHeader className="shrink-0 border-b border-border px-6 py-4 pr-12">
          <div className="flex flex-wrap items-center gap-2">
            <DialogTitle>Review proposed graph</DialogTitle>
            <Badge variant="outline" className="border-emerald-500/40 text-emerald-600">Preview</Badge>
          </div>
          <DialogDescription>
            Your saved pipeline is unchanged. Review the proposal, then apply it or discard it.
          </DialogDescription>
          {stale && (
            <p role="alert" className="mt-2 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
              <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
              This proposal is out of date because the saved graph changed. Ask the assistant for a fresh proposal before applying it.
            </p>
          )}
        </DialogHeader>

        <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[minmax(0,1fr)_minmax(0,0.65fr)] lg:grid-cols-[minmax(0,1fr)_300px] lg:grid-rows-1">
          <div className="relative min-h-0 bg-muted/20">
            <div className="absolute left-3 top-3 z-10 flex flex-wrap gap-2 rounded-lg border border-border bg-background/90 px-2.5 py-2 text-[11px] shadow-sm backdrop-blur">
              <span className="flex items-center gap-1.5"><i className="h-2.5 w-2.5 rounded-sm bg-emerald-400" />Added</span>
              <span className="flex items-center gap-1.5"><i className="h-2.5 w-2.5 rounded-sm bg-amber-400" />Updated</span>
              <span className="flex items-center gap-1.5"><ArrowRight className="h-3 w-3" />Flow direction</span>
              <span className="text-muted-foreground">Removed steps are listed at right</span>
            </div>
            <ReactFlowProvider>
              <PortDisplayContext.Provider value={{ advanced: true, validationByNode: {} }}>
                <ReactFlow
                  nodes={previewNodes}
                  edges={previewEdges}
                  nodeTypes={nodeTypes}
                  fitView
                  fitViewOptions={{ padding: 0.24, minZoom: 0.2, maxZoom: 1.15 }}
                  minZoom={0.15}
                  nodesDraggable={false}
                  nodesConnectable={false}
                  elementsSelectable={false}
                  panOnDrag
                  zoomOnScroll
                  proOptions={{ hideAttribution: true }}
                >
                  <Background gap={20} size={1} />
                  <Controls showInteractive={false} />
                </ReactFlow>
              </PortDisplayContext.Provider>
            </ReactFlowProvider>
            {previewNodes.length === 0 && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
                This proposal leaves the canvas empty.
              </div>
            )}
          </div>

          <aside className="flex min-h-0 flex-col border-t border-border bg-card lg:border-l lg:border-t-0">
            <div className="border-b border-border px-4 py-3">
              <p className="text-sm font-semibold">Change summary</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {totalChanges === 0 ? "No graph changes" : `${totalChanges} graph change${totalChanges === 1 ? "" : "s"}`}
              </p>
            </div>
            <ScrollArea className="min-h-0 flex-1">
              <div className="space-y-5 px-4 py-4">
                {totalChanges === 0 ? (
                  <p className="text-xs leading-5 text-muted-foreground">The assistant did not change any steps or connections.</p>
                ) : (
                  <>
                    <ChangeSection title="New steps" items={changes.addedNodes} tone="green" icon={<Plus className="h-3.5 w-3.5" />} />
                    <ChangeSection title="Updated steps" items={changes.updatedNodes} tone="amber" icon={<RefreshCw className="h-3.5 w-3.5" />} />
                    <ChangeSection title="Removed steps" items={changes.removedNodes} tone="rose" icon={<Minus className="h-3.5 w-3.5" />} />
                    <section className="space-y-1.5">
                      <h3 className="flex items-center gap-1.5 text-xs font-semibold text-emerald-600"><Plus className="h-3.5 w-3.5" />Connections added <span className="text-muted-foreground">({changes.addedEdges.length})</span></h3>
                      {changes.addedEdges.length === 0 ? <p className="pl-5 text-xs text-muted-foreground">None</p> : (
                        <ul className="space-y-1 pl-5 text-xs text-foreground">{changes.addedEdges.map((edge) => <li key={edgeKey(edge)}>{edge.source} → {edge.target}</li>)}</ul>
                      )}
                    </section>
                    <section className="space-y-1.5">
                      <h3 className="flex items-center gap-1.5 text-xs font-semibold text-rose-600"><Minus className="h-3.5 w-3.5" />Connections removed <span className="text-muted-foreground">({changes.removedEdges.length})</span></h3>
                      {changes.removedEdges.length === 0 ? <p className="pl-5 text-xs text-muted-foreground">None</p> : (
                        <ul className="space-y-1 pl-5 text-xs text-foreground">{changes.removedEdges.map((edge) => <li key={edgeKey(edge)}>{edge.source} → {edge.target}</li>)}</ul>
                      )}
                    </section>
                  </>
                )}
              </div>
            </ScrollArea>
          </aside>
        </div>

        <DialogFooter className="shrink-0 gap-2 border-t border-border px-6 py-3">
          <Button variant="outline" onClick={onDiscard} disabled={isApplying}>Discard proposal</Button>
          <Button onClick={onApply} disabled={isApplying || stale || totalChanges === 0}>
            {isApplying ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            {isApplying ? "Applying…" : "Apply to canvas"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
