import { test, expect, type BrowserContext, type Page } from '@playwright/test';

type Graph = { nodes: Array<{ id: string; type: string; position: { x: number; y: number }; data: { type: string; label: string; files?: unknown[] } }>; edges: unknown[]; settings: Record<string, unknown>; updated_at: string };

async function savedPipeline(context: BrowserContext, mode: 'metadata' | 'network' | 'concurrent') {
  let revision = 1;
  let injectFailure = mode !== 'concurrent';
  let graph: Graph = { nodes: [{ id: '1', type: 'custom', position: { x: 150, y: 150 }, data: { type: 'source', label: 'Input records' } }], edges: [], settings: { locale: 'en', retries: 2 }, updated_at: 'initial' };
  const positions: Array<string | undefined> = [];
  const writes: string[] = [];
  await context.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const mutation = request.method() !== 'GET';
    if (mutation) writes.push(path);
    if (mutation && path === '/api/nodes/1/files') {
      const expected = request.headers()['if-match'];
      if (expected && expected !== `"${revision}"`) {
        await route.fulfill({ status: 409, json: { code: 'graph_conflict', revision }, headers: { ETag: `"${revision}"` } });
        return;
      }
      graph.nodes[0].data.files = request.method() === 'POST' ? [{ filename: 'input.csv', bucket: 'files-step-id-1', role: 'data' }] : [];
      revision++;
      await route.fulfill({ json: { file: {}, graph: {} }, headers: { ETag: `"${revision}"` } });
    } else if (mutation && (path.startsWith('/api/graph/') || path === '/api/pipeline/graph' || path === '/api/pipeline/versions/active')) {
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
      if (path.endsWith('/properties')) Object.assign(graph.nodes[0].data, body.properties);
      if (body.graph) graph = { ...graph, nodes: body.graph.nodes, edges: body.graph.edges, settings: body.graph.settings ?? {} };
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
  return { positions, writes, graph: () => structuredClone(graph) };
}

async function moveNode(page: Page, direction: 'ArrowRight' | 'ArrowDown') {
  const node = page.locator('.react-flow__node').first();
  await node.focus();
  await node.press('Enter');
  await expect(node).toHaveClass(/selected/);
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
  await expect(saveStatus(page)).toHaveAttribute('data-save-state', 'saved');
  await expect(page.getByText('Couldn’t save', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/Another session changed/)).toHaveCount(0);
  const position = server.graph().nodes[0].position;
  expect(position.x).toBeGreaterThan(150);
  expect(server.graph().settings).toEqual({ locale: 'en', retries: 2 });
  await page.reload();
  await expect(page.locator('.react-flow__node').first()).toHaveCSS('transform', `matrix(1, 0, 0, 1, ${position.x}, ${position.y})`);
  await expect(page.locator('.react-flow__panel.top.center').getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
  await page.screenshot({ path: 'test-results/save-status-desktop.png', fullPage: true, animations: 'disabled' });
});

test('a transient failure can retry the current draft and settings without reloading', async ({ page, context }) => {
  const server = await savedPipeline(context, 'network');
  await open(page);
  await moveNode(page, 'ArrowRight');
  await expect(saveStatus(page).getByText('Couldn’t save', { exact: true })).toBeVisible();
  expect(server.graph().nodes[0].position.x).toBe(150);
  await saveStatus(page).getByRole('button', { name: 'Retry save' }).click();
  await expect(saveStatus(page)).toHaveAttribute('data-save-state', 'saved');
  const position = server.graph().nodes[0].position;
  expect(position.x).toBeGreaterThan(150);
  expect(server.graph().settings).toEqual({ locale: 'en', retries: 2 });
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
  // Finish the first tab's debounced version save before the second tab recovers.
  await expect.poll(() => server.writes.filter(path => path === '/api/pipeline/versions/active').length).toBe(1);
  await expect(saveStatus(page)).toHaveAttribute('data-save-state', 'saved');
  const savedPosition = server.graph().nodes[0].position;
  await moveNode(second, 'ArrowDown');
  await expect(saveStatus(second).getByText('Couldn’t save', { exact: true })).toBeVisible();
  expect(server.graph().nodes[0].position).toEqual(savedPosition);
  await expect(second.getByText(/Another session changed/)).toHaveCount(0);
  await expect(saveStatus(second).getByRole('button', { name: 'Retry save' })).toHaveCount(0);
  await saveStatus(second).getByRole('button', { name: 'Reload saved graph' }).click();
  await expect(saveStatus(second)).toHaveAttribute('data-save-state', 'saved');
  await expect(second.locator('.react-flow__node').first()).toHaveCSS('transform', `matrix(1, 0, 0, 1, ${savedPosition.x}, ${savedPosition.y})`);
  const retained = await second.evaluate(() => JSON.parse(localStorage.getItem('ai-flow-recovery-draft')!));
  expect(retained.nodes[0].position.y).toBeGreaterThan(150);
  expect(retained.nodes[0].position.x).toBe(150);
  const download = second.waitForEvent('download');
  await saveStatus(second).getByRole('button', { name: 'Previous draft' }).click();
  expect((await download).suggestedFilename()).toBe('inlumen-draft.json');
  await moveNode(second, 'ArrowDown');
  await expect.poll(() => server.graph().nodes[0].position.y).toBeGreaterThan(savedPosition.y);
  await expect(saveStatus(second)).toHaveAttribute('data-save-state', 'saved');
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
  await expect.poll(() => server.writes.filter(path => path === '/api/pipeline/versions/active').length).toBe(1);
  await expect(saveStatus(page)).toHaveAttribute('data-save-state', 'saved');
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
  await expect(saveStatus(second)).toHaveAttribute('data-save-state', 'saved');
});


test('uploading and removing a source input file keeps subsequent edits saved', async ({ page, context }) => {
  const server = await savedPipeline(context, 'concurrent');
  await open(page);
  await page.locator('.react-flow__node').first().click();
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'Upload Input Files', exact: true }).click();
  await (await chooser).setFiles({ name: 'input.csv', mimeType: 'text/csv', buffer: Buffer.from('name,value\nexample,1\n') });
  await expect(page.getByRole('button', { name: 'Preview input.csv', exact: true })).toBeVisible();
  await moveNode(page, 'ArrowRight');
  await expect.poll(() => server.graph().nodes[0].position.x).toBeGreaterThan(150);
  await expect(saveStatus(page)).toHaveAttribute('data-save-state', 'saved');
  await page.reload();
  await page.locator('.react-flow__node').first().click();
  await expect(page.getByRole('button', { name: 'Preview input.csv', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Remove input.csv', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Preview input.csv', exact: true })).toHaveCount(0);
  await moveNode(page, 'ArrowDown');
  await expect.poll(() => server.graph().nodes[0].position.y).toBeGreaterThan(150);
  await expect(saveStatus(page)).toHaveAttribute('data-save-state', 'saved');
  await page.reload();
  await page.locator('.react-flow__node').first().click();
  await expect(page.getByRole('button', { name: 'Preview input.csv', exact: true })).toHaveCount(0);
  expect(server.graph().nodes[0].data.files).toEqual([]);
});


test('selecting a node leaves the save toolbar stable and makes no graph writes', async ({ page, context }) => {
  const server = await savedPipeline(context, 'concurrent');
  await open(page);
  const toolbar = page.locator('.react-flow__panel.top.center');
  await page.evaluate(() => document.fonts.ready);
  const originalBounds = await toolbar.boundingBox();
  const originalText = await toolbar.innerText();
  const initialWrites = server.writes.length;
  for (let i = 0; i < 3; i++) {
    await page.locator('.react-flow__node').first().click();
    await expect(page.getByRole('button', { name: 'Upload Input Files', exact: true })).toBeVisible();
    await page.locator('.react-flow__pane').click({ position: { x: 30, y: 400 } });
  }
  // Allow both property debounce (300ms) and version autosave (600ms) to fire.
  await page.waitForTimeout(800);
  expect(server.writes.slice(initialWrites)).toEqual([]);
  expect(await toolbar.innerText()).toBe(originalText);
  const finalBounds = await toolbar.boundingBox();
  expect(finalBounds?.width).toBe(originalBounds?.width);
  expect(finalBounds?.height).toBe(originalBounds?.height);
  await expect(toolbar).not.toContainText(/Saving|Saved/);
});


test('dragging a node still persists its position without routine save messages', async ({ page, context }) => {
  const server = await savedPipeline(context, 'concurrent');
  await open(page);
  const node = page.locator('.react-flow__node').first();
  await node.click();
  await page.evaluate(() => document.fonts.ready);
  // Let the Inspector resize debounce and viewport fit finish before measuring.
  await page.waitForTimeout(400);
  const box = await node.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + 60, box!.y + box!.height / 2 + 30, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => server.graph().nodes[0].position).not.toEqual({ x: 150, y: 150 });
  await expect(saveStatus(page)).toHaveAttribute('data-save-state', 'saved');
  await expect(page.locator('.react-flow__panel.top.center')).not.toContainText(/Saving|Saved/);
  const position = server.graph().nodes[0].position;
  await page.reload();
  const reopened = page.locator('.react-flow__node').first();
  await expect(reopened).toBeVisible();
  const saved = await reopened.evaluate(element => {
    const matrix = new DOMMatrix(getComputedStyle(element).transform);
    return { x: matrix.m41, y: matrix.m42 };
  });
  expect(saved.x).toBeCloseTo(position.x, 2);
  expect(saved.y).toBeCloseTo(position.y, 2);
});
