import { test, expect } from '@playwright/test';

test('package export, preview, explicit replacement, and reload', async ({ page }) => {
  let revision = 1;
  let imports = 0;
  let graph = { nodes: [{ id: 'before', type: 'custom', position: { x: 100, y: 100 }, data: { type: 'task', label: 'Original design' } }], edges: [], updated_at: 'initial' };
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.pathname === '/api/pipeline/package') {
      if (method === 'GET') return route.fulfill({ body: 'test-package-bytes', contentType: 'application/zip' });
      if (url.searchParams.has('preview')) return route.fulfill({ json: { name: 'Portable example', nodes: 1, files: 2, definitions: 0, secret_parameters: 1 } });
      expect(route.request().headers()['if-match']).toBe(`"${revision}"`);
      imports++;
      graph = { ...graph, nodes: [{ id: 'imported', type: 'custom', position: { x: 100, y: 100 }, data: { type: 'task', label: 'Imported design' } }], updated_at: 'imported' };
      return route.fulfill({ json: { imported: true }, headers: { ETag: `"${++revision}"` } });
    }
    if (url.pathname === '/api/pipeline/graph') return route.fulfill({ json: graph, headers: { ETag: `"${revision}"` } });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], updated_at: graph.updated_at }, headers: { ETag: `"${revision}"` } });
  });
  await page.goto('/');
  await expect(page.locator('.react-flow__node')).toContainText('Original design');
  await page.getByRole('button', { name: 'Package', exact: true }).click();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export package' }).click();
  expect((await download).suggestedFilename()).toBe('inlumen-project.zip');
  await page.getByLabel('Choose project package').setInputFiles({ name: 'pipeline.zip', mimeType: 'application/zip', buffer: Buffer.from('test-package-bytes') });
  await expect(page.getByText('Portable example', { exact: true })).toBeVisible();
  expect(imports).toBe(0);
  await expect(page.getByText('1 secret parameter will need a value after import.')).toBeVisible();
  await page.screenshot({ path: 'test-results/package-preview.png', fullPage: true });
  await page.getByRole('button', { name: 'Replace canvas and import' }).click();
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.locator('.react-flow__node')).toContainText('Imported design');
  expect(imports).toBe(1);
  await page.reload();
  await expect(page.locator('.react-flow__node')).toContainText('Imported design');
});

test('invalid package preview leaves the current design usable', async ({ page }) => {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/package') return route.fulfill({ status: 422, json: { error: 'Attachment checksum does not match' } });
    if (path === '/api/pipeline/graph') return route.fulfill({ json: { nodes: [], edges: [] }, headers: { ETag: '"1"' } });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Package', exact: true }).click();
  await page.getByLabel('Choose project package').setInputFiles({ name: 'broken.zip', mimeType: 'application/zip', buffer: Buffer.from('invalid') });
  await expect(page.getByRole('alert')).toHaveText('Attachment checksum does not match');
  await expect(page.getByRole('button', { name: 'Replace canvas and import' })).toHaveCount(0);
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
});

test('a preview cannot replace a design changed by another editor', async ({ page }) => {
  let revision = 1;
  let imported = false;
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/pipeline/package') {
      if (!url.searchParams.has('preview')) imported = true;
      return route.fulfill({ json: { name: 'Portable', nodes: 1, files: 0, definitions: 0, secret_parameters: 0 } });
    }
    if (url.pathname === '/api/pipeline/graph') return route.fulfill({ json: {
      nodes: [{ id: '1', position: { x: 100, y: 100 }, data: { type: 'task', label: `Design ${revision}` } }],
      edges: [], updated_at: `revision-${revision}`,
    }, headers: { ETag: `"${revision}"` } });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], updated_at: `revision-${revision}` } });
  });
  await page.goto('/');
  await expect(page.locator('.react-flow__node')).toContainText('Design 1');
  await page.getByRole('button', { name: 'Package', exact: true }).click();
  await page.getByLabel('Choose project package').setInputFiles({ name: 'pipeline.zip', mimeType: 'application/zip', buffer: Buffer.from('fixture') });
  await expect(page.getByText('Portable', { exact: true })).toBeVisible();
  revision = 2;
  await expect(page.locator('.react-flow__node')).toContainText('Design 2', { timeout: 10000 });
  await page.getByRole('button', { name: 'Replace canvas and import' }).click();
  await expect(page.getByRole('alert')).toContainText('The workspace changed');
  expect(imported).toBe(false);
});
