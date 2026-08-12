import type { PipelineGenerationJob } from "@/features/flow/flowPersistence";

export const GENERATION_TERMINAL_STATUSES = new Set([
  "valid",
  "invalid",
  "failed",
  "cancelled",
]);

export const effectiveGenerationStatus = (job: PipelineGenerationJob | null) => {
  const outer = String(job?.status || "").toLowerCase();
  if (GENERATION_TERMINAL_STATUSES.has(outer)) return outer;
  const nested = String(job?.generation_run?.status || "").toLowerCase();
  return nested || outer || "running";
};

export const generationRunId = (job: PipelineGenerationJob | null) =>
  String(job?.run_id || job?.generation_run?.run_id || "").trim();

export const isRestorableGenerationRun = (job: PipelineGenerationJob | null) =>
  Boolean(generationRunId(job))
  && (
    !GENERATION_TERMINAL_STATUSES.has(effectiveGenerationStatus(job))
    || (
      effectiveGenerationStatus(job) === "valid"
      && job?.persistence?.status === "pending"
    )
  );
