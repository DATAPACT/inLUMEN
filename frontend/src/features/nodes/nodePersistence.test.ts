import { describe, expect, it, vi } from "vitest";
import { apiFetch } from "@/utils/apiFetch";
import { readNodeFile, updateNodeTextFile, uploadNodeFile } from "./nodePersistence";

vi.mock("@/utils/apiFetch", () => ({ apiFetch: vi.fn() }));

describe("file persistence in authenticated workspaces", () => {
  const ref = { filename: "real.csv", bucket: "files-ws-opaque-1-hash", snapshot_bucket: "pipeline-snapshots-ws-opaque", snapshot_object: "old/real.csv" };
  it("reads by node ID so the backend resolves the current immutable snapshot", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response("actual bytes"));
    await readNodeFile("1", ref);
    const [url] = vi.mocked(apiFetch).mock.lastCall!;
    expect(String(url)).toContain("container_id=1&filename=real.csv");
    expect(String(url)).not.toContain("files-ws");
  });
  it("edits the node live bucket and returns its new canonical reference", async () => {
    const canonical = { filename: ref.filename, bucket: ref.bucket };
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response(JSON.stringify({ file_reference: canonical })));
    expect(await updateNodeTextFile("1", ref, "new bytes")).toEqual({ file_reference: canonical });
    const [, init] = vi.mocked(apiFetch).mock.lastCall!;
    expect(JSON.parse(String(init?.body))).toEqual({ container_id: "1", filename: ref.filename, content: "new bytes" });
  });
  it("preserves the upload response without deriving a bucket from a node ID", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response(JSON.stringify({ file_reference: ref })));
    expect(await uploadNodeFile("1", new File(["bytes"], ref.filename), "data")).toEqual({ file_reference: ref });
  });
});
