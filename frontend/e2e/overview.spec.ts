import { test, expect } from '@playwright/test';

test('Overview refreshes after saved edits and preserves typing during a late response', async ({ page }) => {
  let revision = 1;
  let overviewReads = 0;
  let releaseOverview: (() => void) | undefined;
  const firstOverview = new Promise<void>(resolve => { releaseOverview = resolve; });
  const graph = { nodes: [{ id: '1', type: 'custom', position: { x: 150, y: 150 }, data: { type: 'source', label: 'Input records' } }], edges: [] };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET') {
      if (path === '/api/pipeline/graph') return route.fulfill({ json: graph, headers: { ETag: `"${revision}"` } });
      if (path === '/api/pipeline/updated-at') return route.fulfill({ json: { updated_at: 'initial' } });
      if (path === '/api/pipeline/overview') {
        overviewReads++;
        if (overviewReads === 1) await firstOverview;
        return route.fulfill({ json: { version: 'Main', description: 'Server description', active_version_uid: 'main', updated_at: `2026-09-09T12:00:${String(revision).padStart(2, '0')}Z` } });
      }
      return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
    if (path.endsWith('/position')) {
      const body = request.postDataJSON();
      graph.nodes[0].position = { x: body.x, y: body.y };
    }
    revision++;
    return route.fulfill({ json: path === '/api/pipeline/overview'
      ? { version: 'Main', description: 'My unsaved notes', active_version_uid: 'main' }
      : { ok: true, version: { uid: 'main', name: 'Main' } }, headers: { ETag: `"${revision}"` } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Library', exact: true }).click();
  await page.getByRole('tab', { name: 'Overview', exact: true }).click();
  await expect.poll(() => overviewReads).toBe(1);
  await page.getByRole('textbox', { name: 'Pipeline description' }).fill('My unsaved notes');
  releaseOverview!();
  await expect(page.getByText('Refreshing overview…', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'Pipeline description' })).toHaveValue('My unsaved notes');
  const node = page.locator('.react-flow__node').first();
  await node.focus();
  await node.press('Enter');
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
  const previousReads = overviewReads;
  await node.press('ArrowRight');
  await expect.poll(() => overviewReads).toBeGreaterThan(previousReads);
});
