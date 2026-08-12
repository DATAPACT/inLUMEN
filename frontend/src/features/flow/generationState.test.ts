import { describe, expect, it } from "vitest";

import {
  effectiveGenerationStatus,
  generationRunId,
  isRestorableGenerationRun,
} from "@/features/flow/generationState";

describe("background generation state", () => {
  it("restores only a genuinely active generation run", () => {
    expect(isRestorableGenerationRun({ run_id: "queued-1", status: "queued" })).toBe(true);
    expect(isRestorableGenerationRun({ run_id: "running-1", status: "running" })).toBe(true);
    expect(isRestorableGenerationRun({
      run_id: "persisting-1",
      status: "valid",
      persistence: { status: "pending" },
    })).toBe(true);
    expect(isRestorableGenerationRun({ run_id: "failed-1", status: "failed" })).toBe(false);
    expect(isRestorableGenerationRun({ run_id: "valid-1", status: "valid" })).toBe(false);
  });

  it("uses nested run status and identity when the outer job is incomplete", () => {
    const job = {
      status: "running",
      generation_run: {
        run_id: "nested-1",
        status: "invalid",
      },
    };

    expect(generationRunId(job)).toBe("nested-1");
    expect(effectiveGenerationStatus(job)).toBe("invalid");
    expect(isRestorableGenerationRun(job)).toBe(false);
  });

  it("does not restore an unidentified job", () => {
    expect(isRestorableGenerationRun({ status: "running" })).toBe(false);
  });
});
