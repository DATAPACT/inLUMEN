import { describe, expect, it } from "vitest";

import {
  estimateGenerationTiming,
  generationCurrentStage,
  generationProgressPercent,
} from "@/features/flow/generationProgress";
import type { PipelineGenerationJob } from "@/features/flow/flowPersistence";

const completedRun = (runId: string, durationMs: number): PipelineGenerationJob => ({
  run_id: runId,
  status: "valid",
  target_flow_ids: ["1", "2"],
  generation_run: {
    run_id: runId,
    status: "valid",
    mode: "pipeline_first_single_script",
    stage_timings_ms: { pipeline_generation: durationMs },
  },
});

describe("generation progress", () => {
  it("uses the reported live stage instead of waiting for node completion", () => {
    const job: PipelineGenerationJob = {
      status: "running",
      generation_run: {
        current_stage: "pipeline_validation",
        steps: [{ flow_id: "1", status: "running", stage: "pipeline_validation" }],
      },
    };

    expect(generationCurrentStage(job)).toBe("pipeline_validation");
    expect(generationProgressPercent(job)).toBe(56);
  });

  it("withholds ETA until two comparable completed runs exist", () => {
    const job: PipelineGenerationJob = {
      run_id: "active",
      status: "running",
      created_at: "2026-08-14T10:00:00Z",
      target_flow_ids: ["1", "2"],
      generation_run: { mode: "pipeline_first_single_script" },
    };

    const estimate = estimateGenerationTiming(
      job,
      [completedRun("one", 60_000)],
      Date.parse("2026-08-14T10:00:10Z"),
    );

    expect(estimate.confidence).toBe("learning");
    expect(estimate.remainingMs).toBeNull();
  });

  it("uses the median of comparable history and reports a range", () => {
    const job: PipelineGenerationJob = {
      run_id: "active",
      status: "running",
      created_at: "2026-08-14T10:00:00Z",
      target_flow_ids: ["1", "2"],
      generation_run: { mode: "pipeline_first_single_script" },
    };

    const estimate = estimateGenerationTiming(
      job,
      [completedRun("one", 50_000), completedRun("two", 70_000)],
      Date.parse("2026-08-14T10:00:10Z"),
    );

    expect(estimate.remainingMs).toBe(50_000);
    expect(estimate.lowerRemainingMs).toBe(32_500);
    expect(estimate.upperRemainingMs).toBe(67_500);
    expect(estimate.confidence).toBe("medium");
  });
});
