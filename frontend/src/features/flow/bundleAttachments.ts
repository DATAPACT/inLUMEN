import JSZip from 'jszip';
import {
  checkTaskPackages,
  importTaskPackages,
  taskPackageFolder,
  type PackageReport,
} from '@/features/flow/taskPackages';
import { uploadNodeFile } from '@/features/nodes/nodePersistence';

/**
 * Attach what an inLUMEN bundle ZIP holds for the steps of its design.
 *
 * A bundle keeps the code of each step under `nodes/<folder>/` and the input
 * files of each Source under `inputs/<folder>/`. After the design is on the
 * canvas, the code of the Tasks goes through the same validation and import
 * as "Upload code ZIP", and the input files through the same upload as the
 * inspector. Sources, Destinations and Flow steps run platform code that
 * Build Bundle writes again, so their folders are not attached.
 */

const BUNDLE_MANIFEST = 'bundle-manifest.json';
// Written by the export; a portable Task package must not contain them.
const PLATFORM_FILES = new Set(['node-manifest.json', 'validation-report.json', 'inlumen_item_runner.py']);
const IGNORED_DATA = new Set(['.gitkeep', '.DS_Store']);
const MAX_FILES = 2000;
const MAX_BYTES = 200 * 1024 * 1024;

type Entry = { name: string; read: () => Promise<ArrayBuffer> };
export type BundleTask = { nodeId: string; label: string; files: Entry[] };
export type BundleSource = { nodeId: string; label: string; files: Entry[] };
export type BundlePlan = { tasks: BundleTask[]; sources: BundleSource[]; notes: string[] };
export type BundleAttachmentResult = { codeTasks: string[]; dataFiles: number; dataSources: string[]; notes: string[] };

type ManifestNode = { flow_id?: unknown; label?: unknown; type?: unknown; path?: unknown };

const basename = (path: string) => path.replace(/\/+$/, '').split('/').pop() || '';

/** Read the bundle and decide what belongs to which step. Null when the ZIP is not a bundle. */
export async function readBundlePlan(file: Blob): Promise<BundlePlan | null> {
  const archive = await JSZip.loadAsync(file);
  const manifestPath = Object.keys(archive.files)
    .filter((path) => !archive.files[path].dir && basename(path) === BUNDLE_MANIFEST && path.split('/').length <= 2)
    .sort((a, b) => a.split('/').length - b.split('/').length)[0];
  if (!manifestPath) return null;
  const prefix = manifestPath.slice(0, manifestPath.length - BUNDLE_MANIFEST.length);
  let manifest: { nodes?: ManifestNode[] };
  try {
    manifest = JSON.parse(await archive.files[manifestPath].async('string'));
  } catch {
    return null;
  }

  const paths = Object.keys(archive.files).filter((path) => !archive.files[path].dir);
  if (paths.length > MAX_FILES) throw new Error(`The bundle has more than ${MAX_FILES} files.`);
  const under = (folder: string): Entry[] =>
    paths
      .filter((path) => path.startsWith(folder))
      .map((path) => ({ name: path.slice(folder.length), read: () => archive.files[path].async('arraybuffer') }));

  const plan: BundlePlan = { tasks: [], sources: [], notes: [] };
  for (const node of Array.isArray(manifest.nodes) ? manifest.nodes : []) {
    const nodeId = String(node.flow_id ?? '').trim();
    const folder = String(node.path ?? '').replace(/^\/+|\/+$/g, '');
    if (!nodeId || !folder || folder.includes('..')) continue;
    const label = String(node.label || nodeId);
    const kind = String(node.type || '').toLowerCase();
    if (kind === 'task') {
      const files = under(`${prefix}${folder}/`).filter((entry) =>
        !PLATFORM_FILES.has(basename(entry.name)) && !/^dockerfile/i.test(basename(entry.name)));
      if (files.some((entry) => entry.name === 'main.py')) plan.tasks.push({ nodeId, label, files });
      else plan.notes.push(`${label}: the bundle holds no main.py for this Task, so no code was attached.`);
    } else if (kind === 'source') {
      const all = under(`${prefix}inputs/${basename(folder)}/`).filter((entry) => !IGNORED_DATA.has(basename(entry.name)));
      const nested = all.filter((entry) => entry.name.includes('/'));
      if (nested.length > 0) {
        plan.notes.push(`${label}: ${nested.length} input file(s) in subfolders were not attached.`);
      }
      const files = all.filter((entry) => !entry.name.includes('/'));
      if (files.length > 0) plan.sources.push({ nodeId, label, files });
    }
  }
  return plan;
}

/** The Task code of a plan as a ZIP that "Upload code ZIP" accepts. */
export async function taskPackagesZip(tasks: BundleTask[]): Promise<File> {
  const archive = new JSZip();
  let total = 0;
  for (const task of tasks) {
    const folder = taskPackageFolder(task.nodeId, task.label);
    for (const entry of task.files) {
      const body = await entry.read();
      total += body.byteLength;
      if (total > MAX_BYTES) throw new Error('The code in the bundle is larger than 200 MB.');
      archive.file(`${folder}/${entry.name}`, body);
    }
  }
  const blob = await archive.generateAsync({ type: 'blob', compression: 'DEFLATE' });
  return new File([blob], 'bundle-task-code.zip', { type: 'application/zip' });
}

const list = (labels: string[]) => labels.join(', ');

type Services = {
  check: typeof checkTaskPackages;
  commit: typeof importTaskPackages;
  upload: typeof uploadNodeFile;
};

/**
 * Attach the Task code and the Source data of a bundle to the design that
 * was just imported from it. Never throws for a single step: what could not
 * be attached is reported in `notes`.
 */
export async function attachBundleFiles(
  file: Blob,
  services: Services = { check: checkTaskPackages, commit: importTaskPackages, upload: uploadNodeFile },
): Promise<BundleAttachmentResult> {
  const result: BundleAttachmentResult = { codeTasks: [], dataFiles: 0, dataSources: [], notes: [] };
  const plan = await readBundlePlan(file);
  if (!plan) return result;
  result.notes.push(...plan.notes);

  // Task code: validate all packages, then import those that are valid.
  let tasks = plan.tasks;
  for (let attempt = 0; attempt < 2 && tasks.length > 0; attempt += 1) {
    try {
      const archive = await taskPackagesZip(tasks);
      const report: PackageReport = await services.check(archive);
      if (report.valid) {
        await services.commit(archive, report);
        result.codeTasks = tasks.map((task) => task.label);
        break;
      }
      const invalid = new Set(report.packages.filter((item) => item.errors.length > 0 || !item.node_id).map((item) => item.folder));
      const rejected = tasks.filter((task) => invalid.has(taskPackageFolder(task.nodeId, task.label)));
      for (const task of rejected) {
        const reason = report.packages.find((item) => item.folder === taskPackageFolder(task.nodeId, task.label))?.errors[0]?.message;
        result.notes.push(`${task.label}: the code was not attached${reason ? ` (${reason})` : ''}.`);
      }
      const remaining = tasks.filter((task) => !rejected.includes(task));
      if (rejected.length === 0 || remaining.length === tasks.length) {
        // The report is invalid for a reason that no single package explains.
        result.notes.push(`The Task code was not attached${report.errors[0]?.message ? ` (${report.errors[0].message})` : ''}.`);
        break;
      }
      tasks = remaining;
    } catch (error) {
      result.notes.push(`The Task code was not attached (${error instanceof Error ? error.message : 'the import failed'}).`);
      break;
    }
  }

  // Source data, one file at a time, through the inspector's upload.
  for (const source of plan.sources) {
    let attached = 0;
    for (const entry of source.files) {
      try {
        await services.upload(source.nodeId, new File([await entry.read()], entry.name), 'data');
        attached += 1;
      } catch (error) {
        result.notes.push(`${source.label}: ${entry.name} was not attached (${error instanceof Error ? error.message : 'upload failed'}).`);
      }
    }
    if (attached > 0) {
      result.dataFiles += attached;
      result.dataSources.push(source.label);
    }
  }

  const summary: string[] = [];
  if (result.codeTasks.length > 0) summary.push(`Code was attached to ${result.codeTasks.length} Task(s): ${list(result.codeTasks)}.`);
  if (result.dataFiles > 0) summary.push(`${result.dataFiles} input file(s) were attached to: ${list(result.dataSources)}.`);
  if (summary.length === 0 && result.notes.length === 0) summary.push('The bundle holds no Task code and no input files to attach.');
  result.notes = [...summary, ...result.notes];
  return result;
}
