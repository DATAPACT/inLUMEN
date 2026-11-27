import type { ArtifactDesign } from '@/features/flow/designFromArtifact';

/** An imported design that waits for the user to apply or discard it. */
export type ImportReview = {
  flowData: unknown;
  artifact: ArtifactDesign | null;
  /** Runs once the design is saved; returns notes on what it attached. */
  afterSave?: () => Promise<string[]>;
  /** The ZIP of code behind a design from the model, kept so it can be built again. */
  codeFile?: File;
  baseline: { nodes: unknown[]; edges: unknown[] };
  proposal: { nodes: unknown[]; edges: unknown[] };
};

/**
 * An import replaces the canvas. It is reviewed first when there is something
 * to lose, or when the model inferred the design, because its result varies.
 * A rule-based import into an empty canvas is applied directly.
 */
export const importNeedsReview = (canvasStepCount: number, artifact: ArtifactDesign | null): boolean =>
  canvasStepCount > 0 || artifact?.kind === 'code-zip';

/** What the review dialog says about an import before it is applied. */
export const importReviewNotes = (review: Pick<ImportReview, 'artifact' | 'afterSave' | 'codeFile'>): string[] => {
  if (!review.artifact) return ['The design comes from a Project JSON file.'];
  const notes = [...review.artifact.notes];
  if (review.afterSave) {
    notes.unshift('When you apply it, the code of the Tasks and the data of the Sources are attached from the bundle.');
  }
  if (review.codeFile) {
    notes.push('The model can read the same code differently each time. Import again asks it for a new design.');
  }
  return notes;
};
