import { ReusablePipelineViewerDialog } from "./ReusablePipelineViewerDialog";
import { useState } from "react";
import { toast } from "sonner";

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
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { normalizeGraph, type NormalizedGraph } from "@/features/flow/flowGraph";
import { validateGraph } from "@/features/flow/flowValidation";
import { reusablePipelineNestingError } from "@/features/flow/subpipeline";
import {
  deleteReusablePipeline,
  saveReusablePipeline,
  type ReusablePipelineSummary,
} from "@/features/flow/subpipelinePersistence";

type Props = {
  open: boolean;
  pipelines: ReusablePipelineSummary[];
  onOpenChange: (open: boolean) => void;
  onRefresh: () => Promise<ReusablePipelineSummary[]>;
  getCurrentGraph?: () => unknown;
  currentPipelineName?: string;
  currentPipelineDescription?: string;
};

type CurrentCanvasDraft = {
  name: string;
  description: string;
  graph: NormalizedGraph;
};

export function ReusablePipelineManagerDialog({
  open,
  pipelines,
  onOpenChange,
  onRefresh,
  getCurrentGraph,
  currentPipelineName,
  currentPipelineDescription,
}: Props) {
  const [pipelineToView, setPipelineToView] = useState<ReusablePipelineSummary | null>(null);
  const [pipelineToDelete, setPipelineToDelete] = useState<ReusablePipelineSummary | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [currentCanvasDraft, setCurrentCanvasDraft] = useState<CurrentCanvasDraft | null>(null);
  const [currentCanvasError, setCurrentCanvasError] = useState("");
  const [isSavingCurrentCanvas, setIsSavingCurrentCanvas] = useState(false);

  const prepareCurrentCanvas = () => {
    const graph = normalizeGraph(getCurrentGraph?.() || {});
    setCurrentCanvasDraft({
      name: (
        currentPipelineName?.trim() && currentPipelineName.trim() !== "Main"
          ? currentPipelineName.trim()
          : "Reusable Pipeline"
      ),
      description: currentPipelineDescription?.trim() || "",
      graph,
    });
    setCurrentCanvasError("");
  };

  const saveCurrentCanvas = async () => {
    if (!currentCanvasDraft) return;
    const name = currentCanvasDraft.name.trim();
    if (!name) {
      setCurrentCanvasError("Pipeline name is required.");
      return;
    }
    if (currentCanvasDraft.graph.nodes.length === 0) {
      setCurrentCanvasError("Add components to the main canvas before saving it for reuse.");
      return;
    }
    const nestingError = reusablePipelineNestingError(currentCanvasDraft.graph);
    if (nestingError) { setCurrentCanvasError(nestingError); return; }
    const validation = validateGraph(
      currentCanvasDraft.graph.nodes,
      currentCanvasDraft.graph.edges,
      { mode: "complete", requireRuntime: false, reusable: true },
    );
    if (!validation.valid) {
      const firstError = validation.issues.find((issue) => issue.severity === "error");
      setCurrentCanvasError(firstError?.message || "Resolve pipeline validation errors before saving.");
      return;
    }
    try {
      setIsSavingCurrentCanvas(true);
      setCurrentCanvasError("");
      const saved = await saveReusablePipeline({
        name,
        description: currentCanvasDraft.description.trim(),
        graph: currentCanvasDraft.graph,
      });
      await onRefresh();
      setCurrentCanvasDraft(null);
      toast.success("Current canvas saved for reuse", {
        description: saved.reference.pipeline_name,
      });
    } catch (error) {
      setCurrentCanvasError(error instanceof Error ? error.message : "Failed to save the current canvas.");
    } finally {
      setIsSavingCurrentCanvas(false);
    }
  };

  const confirmDelete = async () => {
    if (!pipelineToDelete) return;
    try {
      setIsDeleting(true);
      await deleteReusablePipeline(pipelineToDelete.uid);
      await onRefresh();
      toast.success("Reusable pipeline deleted", { description: pipelineToDelete.name });
      setPipelineToDelete(null);
    } catch (error) {
      toast.error("Could not delete reusable pipeline", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <>
      <Dialog open={open && !currentCanvasDraft && !pipelineToView} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[86vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Reusable pipelines</DialogTitle>
            <DialogDescription>
              Design on the main canvas, save it here, then drag the saved pipeline from Reusable pipelines into another pipeline. Reusable pipelines cannot contain Subpipeline components.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-xs text-muted-foreground sm:grid-cols-3">
            <p><strong className="text-foreground">1. Design</strong><br />Build and test the pipeline on the main canvas.</p>
            <p><strong className="text-foreground">2. Save</strong><br />Save a reusable pipeline that cannot be edited.</p>
            <p><strong className="text-foreground">3. Reuse</strong><br />Drag the saved pipeline from the Lab into another pipeline.</p>
          </div>
          <div className="flex justify-end">
            <Button onClick={() => prepareCurrentCanvas()} disabled={!getCurrentGraph}>
              Save current canvas
            </Button>
          </div>
          {pipelines.length === 0 ? (
            <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
              No reusable pipelines have been saved yet. Save the current canvas to create one directly.
            </div>
          ) : (
            <div className="space-y-4">
              {pipelines.map((pipeline) => (
                <section key={pipeline.uid} className="rounded-lg border p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="font-medium">{pipeline.name}</h3>
                      <p className="mt-1 text-sm text-muted-foreground">
                        {pipeline.description || "No description provided."}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button size="sm" variant="outline" onClick={() => setPipelineToView(pipeline)}>View pipeline</Button>
                      <Button
                        size="sm"
                        variant="destructive"
                        onClick={() => setPipelineToDelete(pipeline)}
                      >
                        Delete
                      </Button>
                    </div>
                  </div>
                  <div className="mt-4 flex items-center justify-between gap-3 rounded-md bg-muted/50 p-3">
                    <div className="min-w-0 text-xs text-muted-foreground">
                      {pipeline.interface.inputs.length} inputs · {pipeline.interface.outputs.length} outputs · {pipeline.node_count} components
                      {pipeline.unavailable_reason && <p className="mt-1 text-destructive">{pipeline.unavailable_reason}</p>}
                    </div>

                  </div>
                </section>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(currentCanvasDraft)}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && !isSavingCurrentCanvas) setCurrentCanvasDraft(null);
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              Save current canvas for reuse
            </DialogTitle>
            <DialogDescription>
              This saves the graph and attached files currently shown on the main canvas. Saved reusable pipelines cannot be edited. To change the definition, save a new reusable pipeline with a different name.
            </DialogDescription>
          </DialogHeader>
          {currentCanvasDraft && (
            <div className="space-y-4">
              <div className="space-y-3">
                <div className="space-y-2">
                  <Label htmlFor="current-canvas-pipeline-name">Name</Label>
                  <Input
                    id="current-canvas-pipeline-name"
                    value={currentCanvasDraft.name}
                    onChange={(event) => setCurrentCanvasDraft((current) => current && ({ ...current, name: event.target.value }))}
                  />
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="current-canvas-description">Description</Label>
                <Textarea
                  id="current-canvas-description"
                  value={currentCanvasDraft.description}
                  onChange={(event) => setCurrentCanvasDraft((current) => current && ({ ...current, description: event.target.value }))}
                  placeholder="What does this pipeline do?"
                />
              </div>
              <p className="text-xs text-muted-foreground">
                {currentCanvasDraft.graph.nodes.length} component{currentCanvasDraft.graph.nodes.length === 1 ? "" : "s"}
                {" · "}{currentCanvasDraft.graph.edges.length} connection{currentCanvasDraft.graph.edges.length === 1 ? "" : "s"}
              </p>
              {currentCanvasError && (
                <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                  {currentCanvasError}
                </p>
              )}
              <div className="flex justify-end gap-2">
                <Button
                  variant="outline"
                  disabled={isSavingCurrentCanvas}
                  onClick={() => setCurrentCanvasDraft(null)}
                >
                  Cancel
                </Button>
                <Button
                  disabled={isSavingCurrentCanvas}
                  onClick={() => { void saveCurrentCanvas(); }}
                >
                  {isSavingCurrentCanvas ? "Saving…" : "Save reusable pipeline"}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <ReusablePipelineViewerDialog
        reference={open && pipelineToView ? { pipeline_uid: pipelineToView.uid, pipeline_name: pipelineToView.name } : null}
        onClose={() => setPipelineToView(null)}
      />

      <AlertDialog open={Boolean(pipelineToDelete)} onOpenChange={(nextOpen) => {
        if (!nextOpen && !isDeleting) setPipelineToDelete(null);
      }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete reusable pipeline?</AlertDialogTitle>
            <AlertDialogDescription>
              This removes “{pipelineToDelete?.name}”. Deletion is blocked while any parent pipeline references it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={isDeleting}
              onClick={(event) => {
                event.preventDefault();
                void confirmDelete();
              }}
            >
              {isDeleting ? "Deleting…" : "Delete pipeline"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>


    </>
  );
}
