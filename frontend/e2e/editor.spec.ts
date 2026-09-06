import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test('editor recovers from a conflicting save and remains keyboard accessible', async ({ page }) => {
  let conflict = false;
  const writes: { path: string; revision?: string }[] = [];
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    if ((url.pathname.startsWith('/api/graph/') || url.pathname === '/api/pipeline/graph') && route.request().method() === 'POST') {
      writes.push({ path: url.pathname, revision: route.request().headers()["if-match"] });
      await route.fulfill({ status: conflict ? 409 : 200, json: { ok: true }, headers: { ETag: '"2"' } });
    } else if (url.pathname === '/api/pipeline/graph') {
      await route.fulfill({ json: { nodes: [{ id: '1', type: 'source', position: { x: 150, y: 150 }, data: { type: 'source', label: 'Input records' } }], edges: [], updated_at: '2026-01-01' }, headers: { ETag: '"1"' } });
    } else if (url.pathname === '/api/pipeline/versions/active') {
      await route.fulfill({ json: { version: { uid: 'main', name: 'Main' } }, headers: { ETag: '"2"' } });
    } else if (url.pathname === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], nodes: [], edges: [] } });
    }
  });
  await page.goto('/');
  await expect(page.getByText('Input records', { exact: true }).first()).toBeVisible();
  conflict = true;
  await page.locator('input[accept*="json"]').setInputFiles({
    name: 'pipeline.json', mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify({ nodes: [], edges: [] })),
  });
  await expect(page.getByText(/Not saved:/)).toBeVisible();
  expect(writes[0].revision).toBe('"1"');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download draft' }).click();
  expect((await download).suggestedFilename()).toBe('inlumen-draft.json');
  conflict = false;
  await page.getByRole('button', { name: 'Reload saved graph' }).click();
  await expect(page.getByText('Changes saved', { exact: true })).toBeVisible();
  const node = page.locator('.react-flow__node').first();
  await node.focus();
  await node.press('Enter');
  await expect(node).toHaveClass(/selected/);
  const previousWrites = writes.length;
  await node.press('ArrowRight');
  await expect.poll(() => writes.length).toBeGreaterThan(previousWrites);
  await page.keyboard.press('Tab');
  expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe('BODY');
  const accessibility = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(accessibility.violations.filter((item) => ['critical', 'serious'].includes(item.impact || '')).map(({ id, nodes }) => ({ id, nodes: nodes.map(({ target, failureSummary }) => ({ target, failureSummary })) }))).toEqual([]);
  await page.screenshot({ path: 'test-results/editor-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 768, height: 1024 });
  await expect(page.locator('.react-flow')).toBeVisible();
  await page.screenshot({ path: 'test-results/editor-tablet.png', fullPage: true });
});
