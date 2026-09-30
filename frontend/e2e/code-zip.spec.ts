import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import JSZip from 'jszip';

const nodes = ['2', '3'].map((id, index) => ({ id, type: 'custom', position: { x: 100 + index * 300, y: 100 }, data: { type: 'task', label: index ? 'Sentiment' : 'Transcribe' } }));
const issues = (id: string) => [
  { task: `nodes/${id}`, filename: 'inlumen.task.json', field: 'models/0/model_revision', message: 'Model revision main must be an immutable commit hash.', hint: 'Resolve the model repository revision to a real commit SHA.' },
  { task: `nodes/${id}`, filename: 'inlumen.task.json', field: 'output/kind', message: 'Unsupported output kind data.', hint: 'Use kind json or omit optional kind.' },
];
function report(valid: boolean, revision = '"1"') {
  return { digest: 'reviewed-digest', graph_revision: revision, valid, packages: nodes.map(n => ({ folder: `nodes/${n.id}`, files: ['main.py', 'requirements.txt', 'inlumen.task.json'], node_id: n.id, errors: valid ? [] : issues(n.id), warnings: [] })), errors: valid ? [] : nodes.flatMap(n => issues(n.id)) };
}
async function openUpload(page: Page) {
  await page.goto('/');
  const library = page.getByRole('button', { name: 'Library', exact: true });
  if (await library.getAttribute('aria-pressed') !== 'true') await library.click();
  await page.getByRole('tab', { name: 'Run', exact: true }).click();
  await page.getByRole('button', { name: 'Upload code ZIP', exact: true }).click();
}

test('invalid ZIP keeps matches, shows issues once, and supports repair and replacement', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  let valid = false;
  let imported = false;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph') return route.fulfill({ json: { nodes, edges: [], updated_at: '1' }, headers: { ETag: imported ? '"2"' : '"1"' } });
    if (path === '/api/pipeline/external-runtime-prompt') return route.fulfill({ json: { prompt: 'AUTHORITATIVE SCHEMA AND GRAPH' } });
    if (path.endsWith('/task-packages/validate')) return route.fulfill({ json: report(valid) });
    if (path.endsWith('/task-packages/import')) {
      imported = true;
      expect(route.request().headers()['if-match']).toBe('"1"');
      expect(route.request().postData()).toContain('reviewed-digest');
      return route.fulfill({ json: { imported: 2 }, headers: { ETag: '"2"' } });
    }
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
  });
  await openUpload(page);
  await page.getByRole('button', { name: 'Copy external-generation prompt', exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('AUTHORITATIVE SCHEMA AND GRAPH');
  const archive = new JSZip();
  for (const id of ['2', '3']) {
    for (const filename of ['main.py', 'requirements.txt', 'inlumen.task.json']) {
      const path = `nodes/${id}/${filename}`;
      archive.file(path, await readFile(new URL(`../../backend/tests/fixtures/task-packages/audio-sentiment-unpinned/${path}`, import.meta.url)));
    }
  }
  const bytes = await archive.generateAsync({ type: 'nodebuffer' });
  await page.locator('input[accept=".zip,application/zip"]').setInputFiles({ name: 'code.zip', mimeType: 'application/zip', buffer: bytes });
  const dialog = page.getByRole('dialog', { name: 'Review code ZIP' });
  await expect(dialog.getByRole('combobox', { name: 'Target Task for nodes/2' })).toHaveValue('2');
  await expect(dialog.getByRole('combobox', { name: 'Target Task for nodes/3' })).toHaveValue('3');
  await expect(dialog.getByRole('alert')).toHaveCount(4);
  await expect(dialog.getByRole('button', { name: 'Import 2 Task packages' })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Copy repair prompt' }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('models/0/model_revision');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('attach the original ZIP');
  await page.screenshot({ path: 'test-results/code-zip-review.png' });
  valid = true;
  const chooser = page.waitForEvent('filechooser');
  await dialog.getByRole('button', { name: 'Choose replacement ZIP' }).click();
  await (await chooser).setFiles({ name: 'fixed.zip', mimeType: 'application/zip', buffer: bytes });
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await dialog.getByRole('button', { name: 'Import 2 Task packages' }).click();
  await expect(dialog).toBeHidden();
  expect(imported).toBe(true);
});

test('stale review requires revalidation before import', async ({ page }) => {
  let stale = false;
  let imported = false;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph') return route.fulfill({ json: { nodes, edges: [], updated_at: '1' }, headers: { ETag: imported ? '"3"' : stale ? '"2"' : '"1"' } });
    if (path.endsWith('/task-packages/validate')) return route.fulfill({ json: report(true, stale ? '"2"' : '"1"') });
    if (path.endsWith('/task-packages/import')) {
      if (!stale) { stale = true; return route.fulfill({ status: 409, json: { error: 'Graph changed. Validate the packages again.' } }); }
      expect(route.request().headers()['if-match']).toBe('"2"');
      imported = true;
      return route.fulfill({ json: { imported: 2 }, headers: { ETag: '"3"' } });
    }
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
  });
  await openUpload(page);
  await page.locator('input[accept=".zip,application/zip"]').setInputFiles({ name: 'code.zip', mimeType: 'application/zip', buffer: Buffer.from('test ZIP transport') });
  const dialog = page.getByRole('dialog', { name: 'Review code ZIP' });
  await dialog.getByRole('button', { name: 'Import 2 Task packages' }).click();
  await expect(dialog.getByRole('button', { name: 'Import 2 Task packages' })).toBeDisabled();
  await expect(dialog.getByRole('alert')).toContainText('Graph changed');
  await dialog.getByRole('button', { name: 'Revalidate' }).click();
  await expect(dialog.getByRole('button', { name: 'Import 2 Task packages' })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Import 2 Task packages' }).click();
  await expect(dialog).toBeHidden();
});

test('Download code ZIP preserves the portable server archive', async ({ page }) => {
  const archive = new JSZip();
  archive.file('nodes/2/main.py', 'print("ok")\n');
  archive.file('nodes/2/inlumen.task.json', JSON.stringify({ version: 1, output: { type: 'file', path: 'result.json' } }));
  const bytes = await archive.generateAsync({ type: 'nodebuffer' });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph') return route.fulfill({ json: { nodes, edges: [] }, headers: { ETag: '"1"' } });
    if (path.endsWith('/task-packages/download')) return route.fulfill({ body: bytes, contentType: 'application/zip' });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
  });
  await openUpload(page);
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download code ZIP', exact: true }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe('pipeline-code.zip');
  expect(await readFile((await download.path())!)).toEqual(bytes);
});

test('parallel Tasks show readable folders, distinct duplicate labels, and graph connections', async ({ page }) => {
  const parallelNodes = [
    { id: 'audio-en', type: 'custom', position: { x: 0, y: 0 }, data: { type: 'source', label: 'English audio' } },
    { id: 'audio-fr', type: 'custom', position: { x: 0, y: 250 }, data: { type: 'source', label: 'French audio' } },
    ...nodes.map(node => ({ ...node, data: { type: 'task', label: 'Analyze' } })),
    { id: 'result', type: 'custom', position: { x: 700, y: 0 }, data: { type: 'destination', label: 'Combined result' } },
  ];
  const edges = [
    { id: 'en', source: 'audio-en', target: '2' },
    { id: 'fr', source: 'audio-fr', target: '3' },
    { id: 'left-result', source: '2', target: 'result' },
    { id: 'right-result', source: '3', target: 'result' },
  ];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph') return route.fulfill({ json: { nodes: parallelNodes, edges }, headers: { ETag: '"1"' } });
    if (path.endsWith('/task-packages/validate')) return route.fulfill({ json: {
      ...report(true), packages: report(true).packages.map(pkg => ({ ...pkg, folder: `nodes/Analyze--${pkg.node_id}` })),
    } });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
  });
  await openUpload(page);
  const guide = page.getByRole('dialog', { name: 'Upload code ZIP', exact: true });
  await expect(guide).toContainText('Analyze--2/');
  await expect(guide).toContainText('Analyze--3/');
  await expect(guide).toContainText('IDs are not step numbers');
  await page.locator('input[accept=".zip,application/zip"]').setInputFiles({ name: 'parallel.zip', mimeType: 'application/zip', buffer: Buffer.from('test ZIP transport') });
  const dialog = page.getByRole('dialog', { name: 'Review code ZIP' });
  const left = dialog.getByRole('combobox', { name: 'Target Task for nodes/Analyze--2' });
  const right = dialog.getByRole('combobox', { name: 'Target Task for nodes/Analyze--3' });
  await expect(left).toHaveValue('2');
  await expect(right).toHaveValue('3');
  await expect(left.locator('option:checked')).toHaveText('Analyze (ID: 2)');
  await expect(right.locator('option:checked')).toHaveText('Analyze (ID: 3)');
  await expect(dialog.getByText('Receives from: English audio', { exact: true })).toBeVisible();
  await expect(dialog.getByText('Receives from: French audio', { exact: true })).toBeVisible();
  await expect(dialog.getByText('Sends to: Combined result', { exact: true })).toHaveCount(2);
});
