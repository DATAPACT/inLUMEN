import type { PipelineGenerationJob } from "@/features/flow/flowPersistence";
import { effectiveGenerationStatus } from "@/features/flow/generationState";

const TERMINAL_STEP_STATUSES = new Set(["valid", "invalid", "failed", "skipped"]);

const STAGE_PROGRESS: Record<string, number> = {
  pending: 3,
  preparing_nodes: 5,
  pipeline_planning: 8,
  generating: 18,
  pipeline_generation: 28,
  static_validation: 45,
  pipeline_validation: 56,
  pipeline_repair: 64,
  dependency_validation: 70,
  dependency_installation: 76,
  sandbox_execution: 82,
  replaying: 84,
  node_compilation: 88,
  compiled_independent_bundle: 96,
  reused_validated_bundle: 96,
  validated_cache_hit: 99,
  complete: 100,
  failed: 100,
  cancelled: 100,
};

const runTargetCount = (job: PipelineGenerationJob | null) =>
  job?.target_flow_ids?.length
  || job?.preflight?.target_count
  || job?.generation_run?.steps?.length
  || 0;

export const generationCurrentStage = (job: PipelineGenerationJob | null) => {
  const reported = String(job?.generation_run?.current_stage || "").trim();
  if (reported) return reported;
  const runningStep = [...(job?.generation_run?.steps || [])]
    .reverse()
    .find((step) => String(step.status || "").toLowerCase() === "running");
  return String(runningStep?.stage || effectiveGenerationStatus(job) || "pending");
};

export const generationProgressPercent = (job: PipelineGenerationJob | null) => {
  const status = effectiveGenerationStatus(job);
  if (["valid", "invalid", "failed", "cancelled"].includes(status)) return 100;
  const steps = job?.generation_run?.steps || [];
  const completed = steps.filter((step) => (
    TERMINAL_STEP_STATUSES.has(String(step.status || "").toLowerCase())
  )).length;
  const total = Math.max(runTargetCount(job), steps.length);
  const completionProgress = total > 0
    ? Math.round((completed / total) * 95)
    : 0;
  const stage = generationCurrentStage(job).toLowerCase();
  const stageProgress = STAGE_PROGRESS[stage] ?? 10;
  return Math.max(3, Math.min(99, Math.max(completionProgress, stageProgress)));
};

export type GenerationLiveProgress = {
  completedSteps: number;
  activeSteps: number;
  totalSteps: number;
  attempt: number;
};

export const generationLiveProgress = (
  job: PipelineGenerationJob | null,
): GenerationLiveProgress => {
  const steps = job?.generation_run?.steps || [];
  const completedSteps = steps.filter((step) => (
    TERMINAL_STEP_STATUSES.has(String(step.status || "").toLowerCase())
  )).length;
  return {
    completedSteps,
    activeSteps: steps.filter((step) => (
      String(step.status || "").toLowerCase() === "running"
    )).length,
    totalSteps: Math.max(job ? runTargetCount(job) : 0, steps.length),
    attempt: steps.reduce((maximum, step) => (
      Math.max(maximum, Number(step.attempts) || 0)
    ), 0),
  };
};

export const generationElapsedMs = (
  job: PipelineGenerationJob | null,
  nowMs: number,
) => {
  const parsed = typeof job?.created_at === "string"
    ? Date.parse(job.created_at)
    : Number.NaN;
  return Number.isFinite(parsed) ? Math.max(0, nowMs - parsed) : 0;
};

export const formatGenerationDuration = (durationMs: number) => {
  const seconds = Math.max(0, Math.round(durationMs / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder === 0 ? `${minutes}m` : `${minutes}m ${remainder}s`;
};
