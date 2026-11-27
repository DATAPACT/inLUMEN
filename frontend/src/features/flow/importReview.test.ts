import { describe, expect, it } from "vitest";
import { importNeedsReview, importReviewNotes } from "./importReview";
import type { ArtifactDesign } from "./designFromArtifact";

const design = (kind: ArtifactDesign["kind"], notes: string[] = ["Built from the file."]): ArtifactDesign =>
  ({ kind, project: {}, notes, step_count: 3, connection_count: 2 });

describe("import review", () => {
  it("reviews an import when the canvas has steps or the model built the design", () => {
    expect(importNeedsReview(0, null)).toBe(false);                    // Project JSON, empty canvas
    expect(importNeedsReview(0, design("inlumen-bundle"))).toBe(false);
    expect(importNeedsReview(0, design("argo-workflow"))).toBe(false);
    expect(importNeedsReview(0, design("code-zip"))).toBe(true);       // the model's result varies
    expect(importNeedsReview(4, null)).toBe(true);
    expect(importNeedsReview(4, design("inlumen-bundle"))).toBe(true);
  });

  it("says where the design comes from and what applying it does", () => {
    expect(importReviewNotes({ artifact: null })).toEqual(["The design comes from a Project JSON file."]);
    expect(importReviewNotes({ artifact: design("argo-workflow") })).toEqual(["Built from the file."]);

    const bundle = importReviewNotes({ artifact: design("inlumen-bundle"), afterSave: async () => [] });
    expect(bundle[0]).toContain("When you apply it, the code of the Tasks");
    expect(bundle[1]).toBe("Built from the file.");

    const code = importReviewNotes({ artifact: design("code-zip", ["Check each step."]), codeFile: new File(["z"], "c.zip") });
    expect(code[0]).toBe("Check each step.");
    expect(code.at(-1)).toContain("Import again asks it for a new design.");
  });
});
