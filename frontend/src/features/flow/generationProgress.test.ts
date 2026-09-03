import { describe, expect, it } from "vitest";

import {
  generationElapsedMs,
  generationCurrentStage,
  generationLiveProgress,
  generationProgressPercent,
} from "@/features/flow/generationProgress";
import type { PipelineGenerationJob } from "@/features/flow/flowPersistence";

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

  it("reports measurable progress from the current run only", () => {
    const job: PipelineGenerationJob = {
      run_id: "active",
      status: "running",
      created_at: "2026-08-14T10:00:00Z",
      target_flow_ids: ["1", "2", "3"],
      generation_run: {
        mode: "pipeline_first_single_script",
        steps: [
          { flow_id: "1", status: "valid", attempts: 1 },
          { flow_id: "2", status: "running", attempts: 2 },
        ],
      },
    };

    expect(generationLiveProgress(job)).toEqual({
      completedSteps: 1,
      activeSteps: 1,
      totalSteps: 3,
      attempt: 2,
    });
    expect(generationElapsedMs(job, Date.parse("2026-08-14T10:00:10Z"))).toBe(10_000);
  });

  it("uses the target count rather than emitted steps as the completion denominator", () => {
    const job: PipelineGenerationJob = {
      status: "running",
      target_flow_ids: ["1", "2", "3", "4"],
      generation_run: {
        current_stage: "generating",
        steps: [{ flow_id: "1", status: "valid" }],
      },
    };

    expect(generationProgressPercent(job)).toBe(24);
  });
});
