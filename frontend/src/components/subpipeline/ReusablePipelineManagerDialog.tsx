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
import { Badge } from "@/components/ui/badge";
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
import {
  deleteReusablePipeline,
  fetchReusablePipelineVersion,
  saveReusablePipeline,
  type ReusablePipelineSummary,
  type ReusablePipelineVersionSummary,
} from "@/features/flow/subpipelinePersistence";

type Props = {
  open: boolean;
  pipelines: ReusablePipelineSummary[];
  onOpenChange: (open: boolean) => void;
  onRefresh: () => Promise<ReusablePipelineSummary[]>;
  getCurrentGraph?: () => unknown;
  replaceCurrentGraph?: (graph: unknown) => Promise<unknown> | unknown;
  currentPipelineName?: string;
  currentPipelineDescription?: string;
};

type CurrentCanvasDraft = {
  pipelineUid?: string;
  name: string;
  description: string;
  versionName: string;
  graph: NormalizedGraph;
};

type VersionToEdit = {
  pipeline: ReusablePipelineSummary;
  version: ReusablePipelineVersionSummary;
};

export function ReusablePipelineManagerDialog({
  open,
  pipelines,
  onOpenChange,
  onRefresh,
  getCurrentGraph,
  replaceCurrentGraph,
  currentPipelineName,
  currentPipelineDescription,
}: Props) {
  const [pipelineToDelete, setPipelineToDelete] = useState<ReusablePipelineSummary | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [currentCanvasDraft, setCurrentCanvasDraft] = useState<CurrentCanvasDraft | null>(null);
  const [currentCanvasError, setCurrentCanvasError] = useState("");
  const [isSavingCurrentCanvas, setIsSavingCurrentCanvas] = useState(false);
  const [versionToEdit, setVersionToEdit] = useState<VersionToEdit | null>(null);
  const [isLoadingVersion, setIsLoadingVersion] = useState(false);

  const prepareCurrentCanvas = (pipeline?: ReusablePipelineSummary) => {
    const graph = normalizeGraph(getCurrentGraph?.() || {});
    setCurrentCanvasDraft({
      pipelineUid: pipeline?.uid,
      name: pipeline?.name || (
        currentPipelineName?.trim() && currentPipelineName.trim() !== "Main"
          ? currentPipelineName.trim()
          : "Reusable Pipeline"
      ),
      description: pipeline?.description || currentPipelineDescription?.trim() || "",
      versionName: pipeline ? `Version ${pipeline.versions.length + 1}` : "Version 1",
      graph,
    });
    setCurrentCanvasError("");
  };

  const saveCurrentCanvas = async () => {
    if (!currentCanvasDraft) return;
    const name = currentCanvasDraft.name.trim();
    const versionName = currentCanvasDraft.versionName.trim();
    if (!name || !versionName) {
      setCurrentCanvasError("Pipeline name and version name are required.");
      return;
    }
    if (currentCanvasDraft.graph.nodes.length === 0) {
      setCurrentCanvasError("Add components to the main canvas before saving it for reuse.");
      return;
    }
    const validation = validateGraph(
      currentCanvasDraft.graph.nodes,
      currentCanvasDraft.graph.edges,
      { mode: "complete", requireRuntime: false },
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
        pipelineUid: currentCanvasDraft.pipelineUid,
        name,
        description: currentCanvasDraft.description.trim(),
        versionName,
        graph: currentCanvasDraft.graph,
      });
      await onRefresh();
      setCurrentCanvasDraft(null);
      toast.success(currentCanvasDraft.pipelineUid ? "Reusable pipeline version saved" : "Current canvas saved for reuse", {
        description: `${saved.reference.pipeline_name} · ${saved.reference.version_name}`,
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

  const editVersionOnMainCanvas = async () => {
    if (!versionToEdit || !replaceCurrentGraph) return;
    try {
      setIsLoadingVersion(true);
      const loaded = await fetchReusablePipelineVersion(
        versionToEdit.pipeline.uid,
        versionToEdit.version.uid,
      );
      await replaceCurrentGraph(loaded.graph);
      setVersionToEdit(null);
      onOpenChange(false);
      toast.success("Reusable pipeline loaded on the main canvas", {
        description: `${loaded.reference.pipeline_name} · ${loaded.reference.version_name}. Edit it there, then save a new version.`,
      });
    } catch (error) {
      toast.error("Could not load reusable pipeline", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsLoadingVersion(false);
    }
  };

  return (
    <>
      <Dialog open={open && !currentCanvasDraft} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[86vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Reusable pipelines</DialogTitle>
            <DialogDescription>
              Design on the main canvas, save it here, then attach the saved version from a Subpipeline component.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-xs text-muted-foreground sm:grid-cols-3">
            <p><strong className="text-foreground">1. Design</strong><br />Build and test the pipeline on the main canvas.</p>
            <p><strong className="text-foreground">2. Save</strong><br />Create an immutable reusable version here.</p>
            <p><strong className="text-foreground">3. Attach</strong><br />Select it from a Subpipeline component.</p>
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
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!getCurrentGraph}
                        onClick={() => prepareCurrentCanvas(pipeline)}
                      >
                        Save current as new version
                      </Button>
                      <Button
                        size="sm"
                        variant="destructive"
                        onClick={() => setPipelineToDelete(pipeline)}
                      >
                        Delete
                      </Button>
                    </div>
                  </div>
                  <div className="mt-4 space-y-2">
                    {pipeline.versions.map((version) => (
                      <div
                        key={version.uid}
                        className="flex items-center justify-between gap-3 rounded-md bg-muted/50 p-3"
                      >
                        <div className="min-w-0 text-sm">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">{version.name}</span>
                            {pipeline.active_version_uid === version.uid && <Badge variant="secondary">Latest</Badge>}
                          </div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {version.interface.inputs.length} input{version.interface.inputs.length === 1 ? "" : "s"}
                            {" · "}{version.interface.outputs.length} output{version.interface.outputs.length === 1 ? "" : "s"}
                            {" · "}{version.node_count} components
                          </div>
                        </div>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!replaceCurrentGraph}
                          onClick={() => setVersionToEdit({ pipeline, version })}
                        >
                          Edit on main canvas
                        </Button>
                      </div>
                    ))}
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
              {currentCanvasDraft?.pipelineUid ? "Save current canvas as a new version" : "Save current canvas for reuse"}
            </DialogTitle>
            <DialogDescription>
              This saves the graph currently shown on the main canvas. No separate pipeline editor is needed.
            </DialogDescription>
          </DialogHeader>
          {currentCanvasDraft && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-2">
                  <Label htmlFor="current-canvas-pipeline-name">Name</Label>
                  <Input
                    id="current-canvas-pipeline-name"
                    value={currentCanvasDraft.name}
                    disabled={Boolean(currentCanvasDraft.pipelineUid)}
                    onChange={(event) => setCurrentCanvasDraft((current) => current && ({ ...current, name: event.target.value }))}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="current-canvas-version-name">Version</Label>
                  <Input
                    id="current-canvas-version-name"
                    value={currentCanvasDraft.versionName}
                    onChange={(event) => setCurrentCanvasDraft((current) => current && ({ ...current, versionName: event.target.value }))}
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

      <AlertDialog open={Boolean(pipelineToDelete)} onOpenChange={(nextOpen) => {
        if (!nextOpen && !isDeleting) setPipelineToDelete(null);
      }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete reusable pipeline?</AlertDialogTitle>
            <AlertDialogDescription>
              This removes “{pipelineToDelete?.name}” and all of its immutable versions. Deletion is blocked while any parent pipeline references it.
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

      <AlertDialog open={Boolean(versionToEdit)} onOpenChange={(nextOpen) => {
        if (!nextOpen && !isLoadingVersion) setVersionToEdit(null);
      }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Load this version on the main canvas?</AlertDialogTitle>
            <AlertDialogDescription>
              The current main canvas will be replaced with “{versionToEdit?.pipeline.name} · {versionToEdit?.version.name}”. You can use Undo to restore the previous canvas.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isLoadingVersion}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={isLoadingVersion || !replaceCurrentGraph}
              onClick={(event) => {
                event.preventDefault();
                void editVersionOnMainCanvas();
              }}
            >
              {isLoadingVersion ? "Loading…" : "Load on main canvas"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
