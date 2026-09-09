import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test('editor recovers from a conflicting save and remains keyboard accessible', async ({ page }) => {
  let conflict = false;
  const writes: { path: string; revision?: string }[] = [];
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    if ((url.pathname.startsWith('/api/graph/') || url.pathname === '/api/pipeline/graph') && route.request().method() === 'POST') {
      writes.push({ path: url.pathname, revision: route.request().headers()["if-match"] });
      await route.fulfill({ status: conflict ? 409 : 200, json: conflict ? { code: 'graph_conflict' } : { ok: true }, headers: { ETag: '"2"' } });
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
  await expect(page.getByText('Couldn’t save', { exact: true })).toBeVisible();
  expect(writes[0].revision).toBe('"1"');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download draft' }).click();
  expect((await download).suggestedFilename()).toBe('inlumen-draft.json');
  conflict = false;
  await page.getByRole('button', { name: 'Reload saved graph' }).click();
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
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

test('agent changes refresh the revision before autosave and the next canvas edit', async ({ page }) => {
  let revision = 1;
  let agentFinished = false;
  const writes: string[] = [];
  const graph = () => ({ nodes: [{ id: '1', type: 'source', position: { x: 150, y: 150 }, data: { type: 'source', label: agentFinished ? 'Agent updated source' : 'Original source' } }], edges: [], updated_at: '2026-01-01' });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'GET' && (path.startsWith('/api/graph/') || path.startsWith('/api/pipeline/'))) {
      const expected = route.request().headers()['if-match'];
      writes.push(expected);
      if (expected !== `"${revision}"`) {
        await route.fulfill({ status: 409, json: { code: 'graph_conflict' } });
        return;
      }
      revision++;
      await route.fulfill({ json: { ok: true, version: { uid: 'main', name: 'Main' } }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/graph') {
      await route.fulfill({ json: graph(), headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [{ id: 'test-config', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
  });
  await page.route('**/simple_chat', async route => {
    agentFinished = true;
    revision++;
    await route.fulfill({ json: { assistant_message: 'Updated the source.', graph: graph(), sync: { guardrail_passed: true, graph_safe_to_apply: true } } });
  });
  await page.goto('/');
  await expect(page.getByText('Original source', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Rename the source');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByText('Agent updated source', { exact: true }).first()).toBeVisible();
  await expect.poll(() => writes.length).toBeGreaterThan(0);
  expect(writes[0]).toBe('"2"');
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
  const count = writes.length;
  const node = page.locator('.react-flow__node').first();
  await node.focus();
  await node.press('Enter');
  await node.press('ArrowRight');
  await expect.poll(() => writes.length).toBeGreaterThan(count);
  await expect(page.getByText('Couldn’t save', { exact: true })).toHaveCount(0);
});
