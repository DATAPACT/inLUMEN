import { test, expect, type BrowserContext, type Page } from '@playwright/test';

type Graph = { nodes: Array<{ id: string; type: string; position: { x: number; y: number }; data: { type: string; label: string } }>; edges: unknown[]; updated_at: string };

async function savedPipeline(context: BrowserContext, mode: 'metadata' | 'network' | 'concurrent') {
  let revision = 1;
  let injectFailure = mode !== 'concurrent';
  let graph: Graph = { nodes: [{ id: '1', type: 'custom', position: { x: 150, y: 150 }, data: { type: 'source', label: 'Input records' } }], edges: [], updated_at: 'initial' };
  const positions: Array<string | undefined> = [];
  await context.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const mutation = request.method() !== 'GET';
    if (mutation && (path.startsWith('/api/graph/') || path === '/api/pipeline/graph' || path === '/api/pipeline/versions/active')) {
      if (path.endsWith('/position')) {
        positions.push(request.headers()['if-match']);
        if (injectFailure) {
          injectFailure = false;
          if (mode === 'network') {
            await route.fulfill({ status: 503, json: { error: 'Temporary failure' } });
            return;
          }
          // A catalog/snapshot update changes the workspace revision, but not the graph.
          revision++;
        }
      }
      if (request.headers()['if-match'] !== `"${revision}"`) {
        await route.fulfill({ status: 409, json: { code: 'graph_conflict', revision }, headers: { ETag: `"${revision}"` } });
        return;
      }
      const body = request.postDataJSON();
      if (path.endsWith('/position')) graph.nodes[0].position = { x: body.x, y: body.y };
      if (body.graph) graph = { ...graph, nodes: body.graph.nodes, edges: body.graph.edges };
      revision++;
      await route.fulfill({ json: { ok: true, version: { uid: 'main', name: 'Main' } }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/graph') {
      await route.fulfill({ json: graph, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/updated-at') {
      // Model a tab which has not yet observed a changed polling timestamp.
      await route.fulfill({ json: { updated_at: 'initial' } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], nodes: [], edges: [] } });
    }
  });
  return { positions, graph: () => structuredClone(graph) };
}

async function moveNode(page: Page, direction: 'ArrowRight' | 'ArrowDown') {
  const node = page.locator('.react-flow__node').first();
  await node.focus();
  await node.press('Enter');
  await node.press(direction);
}

async function open(page: Page) {
  await page.goto('/');
  await expect(page.getByText('Input records', { exact: true }).first()).toBeVisible();
}

const saveStatus = (page: Page) => page.getByLabel('Pipeline save status');

test('metadata-only revision drift recovers automatically and persists after reopening', async ({ page, context }) => {
  const server = await savedPipeline(context, 'metadata');
  await open(page);
  await moveNode(page, 'ArrowRight');
  await expect.poll(() => server.positions.length).toBe(2);
  expect(server.positions).toEqual(['"1"', '"2"']);
  await expect(saveStatus(page).getByText('Saved', { exact: true })).toBeVisible();
  await expect(page.getByText('Couldn’t save', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/Another session changed/)).toHaveCount(0);
  const position = server.graph().nodes[0].position;
  expect(position.x).toBeGreaterThan(150);
  await page.reload();
  await expect(page.locator('.react-flow__node').first()).toHaveCSS('transform', `matrix(1, 0, 0, 1, ${position.x}, ${position.y})`);
  await expect(page.locator('.react-flow__panel.top.center').getByLabel('Pipeline save status')).toBeVisible();
  await page.screenshot({ path: 'test-results/save-status-desktop.png', fullPage: true, animations: 'disabled' });
});

test('a transient failure can retry the current draft without reloading', async ({ page, context }) => {
  const server = await savedPipeline(context, 'network');
  await open(page);
  await moveNode(page, 'ArrowRight');
  await expect(saveStatus(page).getByText('Couldn’t save', { exact: true })).toBeVisible();
  expect(server.graph().nodes[0].position.x).toBe(150);
  await saveStatus(page).getByRole('button', { name: 'Retry save' }).click();
  await expect(saveStatus(page).getByText('Saved', { exact: true })).toBeVisible();
  const position = server.graph().nodes[0].position;
  expect(position.x).toBeGreaterThan(150);
  await page.reload();
  await expect(page.locator('.react-flow__node').first()).toHaveCSS('transform', `matrix(1, 0, 0, 1, ${position.x}, ${position.y})`);
});

test('two editing tabs preserve competing drafts and recover inline', async ({ page, context }) => {
  const server = await savedPipeline(context, 'concurrent');
  const second = await context.newPage();
  await open(page);
  await open(second);
  await moveNode(page, 'ArrowRight');
  await expect.poll(() => server.graph().nodes[0].position.x).toBeGreaterThan(150);
  const savedPosition = server.graph().nodes[0].position;
  await moveNode(second, 'ArrowDown');
  await expect(saveStatus(second).getByText('Couldn’t save', { exact: true })).toBeVisible();
  expect(server.graph().nodes[0].position).toEqual(savedPosition);
  await expect(second.getByText(/Another session changed/)).toHaveCount(0);
  await expect(saveStatus(second).getByRole('button', { name: 'Retry save' })).toHaveCount(0);
  await saveStatus(second).getByRole('button', { name: 'Reload saved graph' }).click();
  await expect(saveStatus(second).getByText('Saved', { exact: true })).toBeVisible();
  await expect(second.locator('.react-flow__node').first()).toHaveCSS('transform', `matrix(1, 0, 0, 1, ${savedPosition.x}, ${savedPosition.y})`);
  const retained = await second.evaluate(() => JSON.parse(localStorage.getItem('ai-flow-recovery-draft')!));
  expect(retained.nodes[0].position.y).toBeGreaterThan(150);
  expect(retained.nodes[0].position.x).toBe(150);
  const download = second.waitForEvent('download');
  await saveStatus(second).getByRole('button', { name: 'Previous draft' }).click();
  expect((await download).suggestedFilename()).toBe('inlumen-draft.json');
  await moveNode(second, 'ArrowDown');
  await expect.poll(() => server.graph().nodes[0].position.y).toBeGreaterThan(savedPosition.y);
  await expect(saveStatus(second).getByText('Saved', { exact: true })).toBeVisible();
});

test('unavailable draft storage does not discard edits and allows a downloaded backup', async ({ page, context }) => {
  const server = await savedPipeline(context, 'concurrent');
  const second = await context.newPage();
  await second.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key === 'ai-flow-recovery-draft') throw new DOMException('Full', 'QuotaExceededError');
      return original.call(this, key, value);
    };
  });
  await open(page);
  await open(second);
  await moveNode(page, 'ArrowRight');
  await expect.poll(() => server.graph().nodes[0].position.x).toBeGreaterThan(150);
  await moveNode(second, 'ArrowDown');
  await expect(saveStatus(second).getByText('Couldn’t save', { exact: true })).toBeVisible();
  await saveStatus(second).getByRole('button', { name: 'Reload saved graph' }).click();
  await expect(saveStatus(second).getByText('Couldn’t save', { exact: true })).toHaveAttribute('title', /Could not keep a local copy/);
  const draftPosition = await second.locator('.react-flow__node').first().evaluate(node => getComputedStyle(node).transform);
  expect(draftPosition).not.toBe(`matrix(1, 0, 0, 1, ${server.graph().nodes[0].position.x}, 150)`);
  const download = second.waitForEvent('download');
  await saveStatus(second).getByRole('button', { name: 'Download draft' }).click();
  await download;
  await saveStatus(second).getByRole('button', { name: 'Reload saved graph' }).click();
  await expect(saveStatus(second).getByText('Saved', { exact: true })).toBeVisible();
});
