import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import JSZip from 'jszip';

test('Download code ZIP sits beside upload and produces upload-compatible Task folders', async ({ page }) => {
  let attached = true;
  const script = 'print("original code")\n';
  const reads: string[] = [];
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/files/content') {
      const name = url.searchParams.get('filename')!;
      reads.push(name);
      return route.fulfill({ body: name === 'main.py' ? script : 'requests==2.32.0\n' });
    }
    if (url.pathname === '/api/pipeline/graph') return route.fulfill({ json: {
      nodes: [{ id: 'task-1', type: 'custom', position: { x: 100, y: 100 }, data: {
        type: 'task', label: 'Summarize orders', files: attached ? [
          { filename: 'main.py', role: 'code' }, { filename: 'requirements.txt', role: 'code' },
          { filename: 'orders.json', role: 'data' },
        ] : [],
      } }], edges: [], updated_at: attached ? 'with-code' : 'without-code',
    }, headers: { ETag: '"1"' } });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
  });
  const openRun = async () => {
    const library = page.getByRole('button', { name: 'Library', exact: true });
    if (await library.getAttribute('aria-pressed') !== 'true') await library.click();
    await page.getByRole('tab', { name: 'Run', exact: true }).click();
  };
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Package', exact: true })).toHaveCount(0);
  await openRun();
  await expect(page.getByRole('button', { name: 'Upload code ZIP', exact: true })).toBeVisible();
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download code ZIP', exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe('pipeline-code.zip');
  const bytes = await readFile((await download.path())!);
  const zip = await JSZip.loadAsync(bytes);
  expect(Object.values(zip.files).filter(file => !file.dir).map(file => file.name)).toEqual([
    'Summarize orders--task-1/main.py', 'Summarize orders--task-1/requirements.txt',
  ]);
  expect(await zip.file('Summarize orders--task-1/main.py')!.async('string')).toBe(script);
  expect(reads).toEqual(['main.py', 'requirements.txt']);
  attached = false;
  await page.reload();
  await openRun();
  await page.getByRole('button', { name: 'Upload code ZIP', exact: true }).click();
  await page.locator('input[accept=".zip,application/zip"]').setInputFiles({ name: 'pipeline-code.zip', mimeType: 'application/zip', buffer: bytes });
  await expect(page.getByText('Ready to upload (1)', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toContainText('main.py, requirements.txt');
});
