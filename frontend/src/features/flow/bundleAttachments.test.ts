import JSZip from "jszip";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/apiFetch", () => ({ apiFetch: vi.fn() }));

import { attachBundleFiles, readBundlePlan, taskPackagesZip } from "./bundleAttachments";
import type { PackageReport } from "./taskPackages";

const manifest = {
  schema_version: "inlumen.deployment-bundle@3",
  nodes: [
    { flow_id: "1", label: "Text Files", type: "source", path: "nodes/node-1-text-files" },
    { flow_id: "2", label: "Parallel Map", type: "flow", path: "nodes/node-2-parallel-map" },
    { flow_id: "3", label: "Word Count", type: "task", path: "nodes/node-3-word-count" },
    { flow_id: "4", label: "Collect", type: "flow", path: "nodes/node-4-collect" },
    { flow_id: "5", label: "Merge Summary", type: "task", path: "nodes/node-5-merge-summary" },
    { flow_id: "6", label: "JSON Output", type: "destination", path: "nodes/node-6-json-output" },
  ],
};

const bundle = async (prefix = "", extra: Record<string, string> = {}) => {
  const archive = new JSZip();
  const files: Record<string, string> = {
    "bundle-manifest.json": JSON.stringify(manifest),
    "nodes/node-1-text-files/main.py": "# platform adapter",
    "nodes/node-1-text-files/node-manifest.json": "{}",
    "nodes/node-2-parallel-map/main.py": "# platform flow code",
    "nodes/node-3-word-count/main.py": "print('count')",
    "nodes/node-3-word-count/requirements.txt": "",
    "nodes/node-3-word-count/inlumen.task.json": "{\"version\": 1}",
    "nodes/node-3-word-count/helpers/text.py": "def words(): ...",
    "nodes/node-3-word-count/node-manifest.json": "{}",
    "nodes/node-3-word-count/validation-report.json": "{}",
    "nodes/node-3-word-count/inlumen_item_runner.py": "# written by the export",
    "nodes/node-5-merge-summary/main.py": "print('merge')",
    "nodes/node-5-merge-summary/inlumen.task.json": "{\"version\": 1}",
    "nodes/node-6-json-output/main.py": "# platform adapter",
    "inputs/node-1-text-files/a.txt": "alpha",
    "inputs/node-1-text-files/b.txt": "beta",
    "inputs/node-1-text-files/.gitkeep": "",
    "inputs/node-1-text-files/archive/old.txt": "nested",
    "argo/workflow.yaml": "kind: Workflow",
    ...extra,
  };
  for (const [path, text] of Object.entries(files)) archive.file(prefix + path, text);
  return new Blob([await archive.generateAsync({ type: "arraybuffer" })]);
};

// jsdom's File has no text(); read it the way a browser's FileReader does.
const textOf = (file: Blob) => new Promise<string>((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result));
  reader.onerror = () => reject(reader.error);
  reader.readAsText(file);
});

const names = (entries: Array<{ name: string }>) => entries.map((entry) => entry.name).sort();

describe("bundle attachments", () => {
  it("takes the code of Tasks and the input files of Sources, and no platform file", async () => {
    for (const prefix of ["", "my-bundle/"]) {
      const plan = (await readBundlePlan(await bundle(prefix)))!;
      expect(plan.tasks.map((task) => [task.nodeId, task.label])).toEqual([["3", "Word Count"], ["5", "Merge Summary"]]);
      expect(names(plan.tasks[0].files)).toEqual(["helpers/text.py", "inlumen.task.json", "main.py", "requirements.txt"]);
      expect(plan.sources.map((source) => [source.nodeId, names(source.files)])).toEqual([["1", ["a.txt", "b.txt"]]]);
      expect(plan.notes).toEqual(["Text Files: 1 input file(s) in subfolders were not attached."]);
    }
  });

  it("is not a plan for a ZIP without a bundle manifest", async () => {
    const archive = new JSZip();
    archive.file("src/main.py", "print(1)");
    expect(await readBundlePlan(new Blob([await archive.generateAsync({ type: "arraybuffer" })]))).toBeNull();
  });

  it("writes the Task code as a ZIP that Upload code ZIP accepts", async () => {
    const plan = (await readBundlePlan(await bundle()))!;
    const packages = await JSZip.loadAsync(await taskPackagesZip(plan.tasks));
    const paths = Object.keys(packages.files).filter((path) => !packages.files[path].dir).sort();
    expect(paths).toEqual([
      "nodes/Merge Summary--5/inlumen.task.json",
      "nodes/Merge Summary--5/main.py",
      "nodes/Word Count--3/helpers/text.py",
      "nodes/Word Count--3/inlumen.task.json",
      "nodes/Word Count--3/main.py",
      "nodes/Word Count--3/requirements.txt",
    ]);
    expect(await packages.files["nodes/Word Count--3/main.py"].async("string")).toBe("print('count')");
  });

  const report = (valid: boolean, bad: string[] = []): PackageReport => ({
    digest: "d", graph_revision: "r", valid, errors: [],
    packages: ["nodes/Word Count--3", "nodes/Merge Summary--5"].map((folder) => ({
      folder, files: [], node_id: folder.endsWith("3") ? "3" : "5", warnings: [],
      errors: bad.includes(folder) ? [{ message: "output is not declared" }] : [],
    })),
  });

  it("imports the Task code, uploads the input files as data, and says what it did", async () => {
    const check = vi.fn().mockResolvedValue(report(true));
    const commit = vi.fn().mockResolvedValue(undefined);
    const upload = vi.fn().mockResolvedValue({});

    const result = await attachBundleFiles(await bundle(), { check, commit, upload });

    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit.mock.calls[0][1]).toEqual(report(true));
    expect(upload.mock.calls.map(([nodeId, file, role]) => [nodeId, file.name, role]))
      .toEqual([["1", "a.txt", "data"], ["1", "b.txt", "data"]]);
    expect(await textOf(upload.mock.calls[0][1])).toBe("alpha");
    expect(result.notes).toEqual([
      "Code was attached to 2 Task(s): Word Count, Merge Summary.",
      "2 input file(s) were attached to: Text Files.",
      "Text Files: 1 input file(s) in subfolders were not attached.",
    ]);
  });

  it("leaves out a Task whose code fails validation and attaches the others", async () => {
    const check = vi.fn()
      .mockResolvedValueOnce(report(false, ["nodes/Word Count--3"]))
      .mockResolvedValueOnce({ ...report(true), packages: report(true).packages.slice(1) });
    const commit = vi.fn().mockResolvedValue(undefined);
    const upload = vi.fn().mockRejectedValueOnce(new Error("too large")).mockResolvedValue({});

    const result = await attachBundleFiles(await bundle(), { check, commit, upload });

    expect(check).toHaveBeenCalledTimes(2);
    const second = await JSZip.loadAsync(check.mock.calls[1][0] as File);
    expect(Object.keys(second.files).some((path) => path.includes("Word Count"))).toBe(false);
    expect(commit).toHaveBeenCalledTimes(1);
    expect(result.codeTasks).toEqual(["Merge Summary"]);
    expect(result.notes).toContain("Word Count: the code was not attached (output is not declared).");
    expect(result.notes).toContain("Text Files: a.txt was not attached (too large).");
    expect(result.notes).toContain("1 input file(s) were attached to: Text Files.");
  });

  it("reports a failed import without throwing", async () => {
    const result = await attachBundleFiles(await bundle(), {
      check: vi.fn().mockRejectedValue(new Error("Graph changed")),
      commit: vi.fn(),
      upload: vi.fn().mockResolvedValue({}),
    });
    expect(result.codeTasks).toEqual([]);
    expect(result.notes).toContain("The Task code was not attached (Graph changed).");
    expect(result.dataFiles).toBe(2);
  });
});
