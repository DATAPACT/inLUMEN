import type { PipelineGenerationJob } from "@/features/flow/flowPersistence";
import { effectiveGenerationStatus, generationRunId } from "@/features/flow/generationState";

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

const finiteTimestamp = (value: unknown) => {
  const parsed = typeof value === "string" ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : null;
};

const median = (values: number[]) => {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
};

const runStageDuration = (job: PipelineGenerationJob) => {
  const timings = job.generation_run?.stage_timings_ms;
  if (!timings) return 0;
  return Object.values(timings).reduce((total, value) => (
    Number.isFinite(value) && value >= 0 ? total + value : total
  ), 0);
};

const runTargetCount = (job: PipelineGenerationJob) =>
  job.target_flow_ids?.length
  || job.preflight?.target_count
  || job.generation_run?.steps?.length
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
  const completionProgress = steps.length > 0
    ? Math.round((completed / steps.length) * 95)
    : 0;
  const stage = generationCurrentStage(job).toLowerCase();
  const stageProgress = STAGE_PROGRESS[stage] ?? 10;
  return Math.max(3, Math.min(99, Math.max(completionProgress, stageProgress)));
};

export type GenerationTimingEstimate = {
  elapsedMs: number;
  remainingMs: number | null;
  lowerRemainingMs: number | null;
  upperRemainingMs: number | null;
  sampleCount: number;
  confidence: "learning" | "medium" | "high";
};

export const estimateGenerationTiming = (
  job: PipelineGenerationJob | null,
  history: PipelineGenerationJob[],
  nowMs: number,
): GenerationTimingEstimate => {
  const startedAt = finiteTimestamp(job?.created_at);
  const elapsedMs = startedAt == null ? 0 : Math.max(0, nowMs - startedAt);
  if (!job) {
    return {
      elapsedMs,
      remainingMs: null,
      lowerRemainingMs: null,
      upperRemainingMs: null,
      sampleCount: 0,
      confidence: "learning",
    };
  }

  const runId = generationRunId(job);
  const mode = String(job.generation_run?.mode || "");
  const targetCount = runTargetCount(job);
  let comparable = history.filter((candidate) => {
    if (!generationRunId(candidate) || generationRunId(candidate) === runId) return false;
    if (!["valid", "invalid"].includes(effectiveGenerationStatus(candidate))) return false;
    if (mode && candidate.generation_run?.mode && candidate.generation_run.mode !== mode) return false;
    const candidateCount = runTargetCount(candidate);
    return !targetCount || !candidateCount || Math.abs(candidateCount - targetCount) <= 1;
  });
  if (comparable.length < 2) {
    comparable = history.filter((candidate) => (
      generationRunId(candidate) !== runId
      && ["valid", "invalid"].includes(effectiveGenerationStatus(candidate))
      && (!mode || !candidate.generation_run?.mode || candidate.generation_run.mode === mode)
    ));
  }
  const durations = comparable
    .map(runStageDuration)
    .filter((duration) => duration >= 500);
  if (durations.length < 2) {
    return {
      elapsedMs,
      remainingMs: null,
      lowerRemainingMs: null,
      upperRemainingMs: null,
      sampleCount: durations.length,
      confidence: "learning",
    };
  }

  const expectedMs = median(durations);
  const remainingMs = Math.max(0, expectedMs - elapsedMs);
  const confidence = durations.length >= 5 ? "high" : "medium";
  const margin = confidence === "high" ? 0.2 : 0.35;
  return {
    elapsedMs,
    remainingMs,
    lowerRemainingMs: Math.max(0, remainingMs * (1 - margin)),
    upperRemainingMs: remainingMs * (1 + margin),
    sampleCount: durations.length,
    confidence,
  };
};

export const formatGenerationDuration = (durationMs: number) => {
  const seconds = Math.max(0, Math.round(durationMs / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder === 0 ? `${minutes}m` : `${minutes}m ${remainder}s`;
};
