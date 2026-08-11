import { useState } from "react";
import { toast } from "sonner";

import { SubpipelineEditorDialog } from "@/components/subpipeline/SubpipelineEditorDialog";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { ReusablePipelineSaveDraft } from "@/features/flow/subpipeline";
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
};

type EditorDraft = {
  pipelineUid: string;
  name: string;
  description: string;
  versionName: string;
  graph: { nodes: unknown[]; edges: unknown[] };
};

const emptyEditorDraft = (): EditorDraft => ({
  pipelineUid: "",
  name: "Reusable Pipeline",
  description: "",
  versionName: "Version 1",
  graph: { nodes: [], edges: [] },
});

export function ReusablePipelineManagerDialog({
  open,
  pipelines,
  onOpenChange,
  onRefresh,
}: Props) {
  const [editorDraft, setEditorDraft] = useState<EditorDraft | null>(null);
  const [pipelineToDelete, setPipelineToDelete] = useState<ReusablePipelineSummary | null>(null);
  const [isOpeningVersion, setIsOpeningVersion] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);

  const openVersion = async (
    pipeline: ReusablePipelineSummary,
    version: ReusablePipelineVersionSummary,
  ) => {
    try {
      setIsOpeningVersion(true);
      const loaded = await fetchReusablePipelineVersion(pipeline.uid, version.uid);
      setEditorDraft({
        pipelineUid: pipeline.uid,
        name: pipeline.name,
        description: pipeline.description || loaded.description || "",
        versionName: `Version ${pipeline.versions.length + 1}`,
        graph: loaded.graph,
      });
    } catch (error) {
      toast.error("Could not open reusable pipeline", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsOpeningVersion(false);
    }
  };

  const saveVersion = async (draft: ReusablePipelineSaveDraft) => {
    const existingPipelineUid = editorDraft?.pipelineUid || undefined;
    const saved = await saveReusablePipeline({
      pipelineUid: existingPipelineUid,
      name: draft.name,
      description: draft.description,
      versionName: draft.versionName,
      graph: draft.graph,
    });
    await onRefresh();
    toast.success(existingPipelineUid ? "Reusable pipeline version saved" : "Reusable pipeline created", {
      description: `${saved.reference.pipeline_name} · ${saved.reference.version_name}`,
    });
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
      <Dialog open={open && !editorDraft} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[86vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Manage reusable pipelines</DialogTitle>
            <DialogDescription>
              Reusable pipelines are separate definitions. Saved versions are immutable; editing creates a new version.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end">
            <Button onClick={() => setEditorDraft(emptyEditorDraft())}>Create reusable pipeline</Button>
          </div>
          {pipelines.length === 0 ? (
            <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
              No reusable pipelines have been saved yet.
            </div>
          ) : (
            <div className="space-y-4">
              {pipelines.map((pipeline) => (
                <section key={pipeline.uid} className="rounded-lg border p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <h3 className="font-medium">{pipeline.name}</h3>
                      <p className="mt-1 text-sm text-muted-foreground">
                        {pipeline.description || "No description provided."}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      variant="destructive"
                      onClick={() => setPipelineToDelete(pipeline)}
                    >
                      Delete pipeline
                    </Button>
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
                          variant="outline"
                          disabled={isOpeningVersion}
                          onClick={() => { void openVersion(pipeline, version); }}
                        >
                          Open and create version
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

      {editorDraft && (
        <SubpipelineEditorDialog
          open
          pipelineUid={editorDraft.pipelineUid}
          name={editorDraft.name}
          description={editorDraft.description}
          suggestedVersionName={editorDraft.versionName}
          reusablePipelines={pipelines}
          graph={editorDraft.graph}
          onOpenChange={(nextOpen) => {
            if (!nextOpen) setEditorDraft(null);
          }}
          onSave={saveVersion}
        />
      )}

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
    </>
  );
}
