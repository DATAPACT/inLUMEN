import JSZip from "jszip";
import { describe, expect, it } from "vitest";

import { buildDeploymentZip, safeZipPath } from "@/features/deployment/deploymentBundleZip";

describe("deployment bundle zip", () => {
  it("packs the whole Argo bundle with script modes and binary inputs", async () => {
    const blob = await buildDeploymentZip([
      { path: "argo/workflow.yaml", content: "kind: Workflow\n" },
      { path: "argo/submit.sh", content: "#!/usr/bin/env bash\n", executable: true },
      { path: "inputs/node-1/a.bin", content: "AAE=", content_encoding: "base64" },
    ]);

    const zip = await JSZip.loadAsync(blob);
    expect(Object.keys(zip.files).filter((name) => !zip.files[name].dir).sort()).toEqual([
      "argo/submit.sh",
      "argo/workflow.yaml",
      "inputs/node-1/a.bin",
    ]);
    expect(await zip.file("argo/workflow.yaml")?.async("string")).toBe("kind: Workflow\n");
    expect(Array.from(await zip.file("inputs/node-1/a.bin")!.async("uint8array"))).toEqual([0, 1]);
    expect((zip.file("argo/submit.sh")!.unixPermissions as number) & 0o777).toBe(0o755);
    expect((zip.file("argo/workflow.yaml")!.unixPermissions as number) & 0o777).toBe(0o644);
  });

  it("never writes outside the bundle root", () => {
    expect(safeZipPath({ path: "../etc/passwd" }, 0)).toBe("file/etc/passwd");
    expect(safeZipPath({ path: "/abs/file.txt" }, 0)).toBe("abs/file.txt");
    expect(safeZipPath({ flow_id: "step 1", filename: "main.py" }, 0)).toBe("nodes/step-1/main.py");
  });
});
