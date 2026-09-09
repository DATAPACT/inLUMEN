import { test, expect, type Page } from '@playwright/test';
import type { Node, Edge } from 'reactflow';

const port = { id: 'data', name: 'data', type: 'Text', required: true };
const childGraph = {
  nodes: [
    { id: 'input', type: 'custom', position: { x: 100, y: 100 }, data: { type: 'source', label: 'Input text', files: [{ filename: 'data.txt', bucket: 'files-step-id-input', snapshot_bucket: 'snapshots', snapshot_object: 'input/data.txt' }], ports: { inputs: [], outputs: [port] } } },
    { id: 'output', type: 'custom', position: { x: 400, y: 100 }, data: { type: 'destination', label: 'Output text', ports: { inputs: [port], outputs: [] } } },
  ],
  edges: [{ id: 'input-output', source: 'input', target: 'output', sourceHandle: 'data', targetHandle: 'data' }],
};
const contract = { inputs: [{ ...port, internal: { node: 'input', port: 'data' } }], outputs: [{ ...port, internal: { node: 'output', port: 'data' } }] };
const reference = { pipeline_uid: 'reusable-1', pipeline_name: 'Text adapter' };

async function setup(page: Page, savedInitially = false) {
  let revision = 1;
  let saved = savedInitially;
  const mutations: string[] = [];
  let graph: { nodes: Node[]; edges: Edge[]; updated_at?: string } = structuredClone(childGraph);
  const catalog = () => saved ? [{ uid: reference.pipeline_uid, name: reference.pipeline_name, description: '', interface: contract, node_count: 2, edge_count: 1 }] : [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const headers = { ETag: `"${revision}"` };
    if (request.method() === 'GET') {
      if (path === '/api/pipeline/graph') return route.fulfill({ json: graph, headers });
      if (path === '/api/pipeline/updated-at') return route.fulfill({ json: { updated_at: 'initial' } });
      if (path === '/api/reusable-pipelines') return route.fulfill({ json: { pipelines: catalog() } });
      if (path === '/api/reusable-pipelines/definition') return route.fulfill({ json: { reference, graph: childGraph, interface: contract } });
      return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
    mutations.push(path);
    const body = request.postDataJSON();
    if (path === '/api/reusable-pipelines') {
      expect(body.pipeline_uid).toBeUndefined();
      expect(body.version_name).toBeUndefined();
      expect(body.graph.nodes[0].data.files[0]).toMatchObject({ snapshot_bucket: 'snapshots', snapshot_object: 'input/data.txt' });
      saved = true;
    }
    if (body?.graph) graph = structuredClone(body.graph);
    if (path === '/api/graph/nodes') {
      const data = body.properties;
      graph.nodes.push({ id: String(data.flow_id), type: 'custom', position: { x: data.x, y: data.y }, data });
    }
    revision++;
    return route.fulfill({ json: path === '/api/reusable-pipelines'
      ? { reference, interface: contract, graph: childGraph }
      : { ok: true, version: { uid: 'main', name: 'Main' } }, headers: { ETag: `"${revision}"` } });
  });
  return { graph: () => graph, mutations };
}

test('empty catalog guides creation; immutable pipelines drag with a reference and survive reopening', async ({ page }) => {
  const server = await setup(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Library', exact: true }).click();
  await expect(page.getByText('No reusable pipelines yet.', { exact: true })).toBeVisible();
  await expect(page.locator('[draggable="true"]').filter({ has: page.getByRole('heading', { name: 'Subpipeline', exact: true }) })).toHaveCount(0);
  await page.getByRole('button', { name: 'Save a pipeline for reuse' }).click();
  await page.getByRole('button', { name: 'Save current canvas', exact: true }).click();
  await expect(page.getByLabel('Version name', { exact: true })).toHaveCount(0);
  await page.getByLabel('Name', { exact: true }).fill('Text adapter');
  await page.getByRole('button', { name: 'Save reusable pipeline', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Reusable pipelines', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /Edit on main canvas|Update from canvas/ })).toHaveCount(0);
  await page.getByRole('dialog', { name: 'Reusable pipelines', exact: true }).getByRole('button', { name: 'Close', exact: true }).click();
  const card = page.locator('[draggable="true"]').filter({ has: page.getByRole('heading', { name: 'Text adapter', exact: true }) });
  const transfer = await page.evaluateHandle(() => new DataTransfer());
  await card.dispatchEvent('dragstart', { dataTransfer: transfer });
  await page.locator('.react-flow').dispatchEvent('drop', { dataTransfer: transfer, clientX: 650, clientY: 350 });
  await expect.poll(() => server.graph().nodes.some(node => node.data.type === 'subpipeline')).toBe(true);
  const subpipeline = page.locator('.react-flow__node').filter({ hasText: 'Text adapter' });
  await expect(subpipeline).toBeVisible();
  await subpipeline.click();
  await expect(page.getByText('No reusable pipeline attached', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Select or create a saved reusable pipeline version.', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/^ID: /)).toBeVisible();
  await page.getByRole('button', { name: 'View pipeline', exact: true }).last().click();
  const preview = page.getByRole('dialog', { name: 'Text adapter — Read only', exact: true });
  await expect(preview.locator('.react-flow__node')).toHaveCount(2);
  await preview.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
  await page.reload();
  await expect(page.locator('.react-flow__node').filter({ hasText: 'Text adapter' })).toBeVisible();
  await page.getByRole('button', { name: 'Manage', exact: true }).click();
  await page.getByRole('button', { name: 'Save current canvas', exact: true }).click();
  await page.getByRole('button', { name: 'Save reusable pipeline', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Only one subpipeline level is supported');
  await page.screenshot({ path: 'test-results/subpipeline-depth-limit.png', fullPage: true });
});


test('reusable pipelines can be inspected without editing or saving either canvas', async ({ page }) => {
  const server = await setup(page, true);
  await page.goto('/');
  await page.getByRole('button', { name: 'Library', exact: true }).click();
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
  const before = structuredClone(server.graph());
  const writes = server.mutations.length;
  const card = page.locator('[draggable="true"]').filter({ has: page.getByRole('heading', { name: 'Text adapter', exact: true }) });
  await card.getByRole('button', { name: 'View pipeline', exact: true }).click();
  const viewer = page.getByRole('dialog', { name: 'Text adapter — Read only', exact: true });
  await expect(viewer.locator('.react-flow__node')).toHaveCount(2);
  await expect(viewer.locator('.react-flow__edge')).toHaveCount(1);
  await viewer.getByLabel('Component', { exact: true }).selectOption('input');
  await expect(viewer.getByLabel('Component details')).toContainText('data.txt');
  await expect(viewer.getByLabel('Component details')).toContainText('Text · Required');
  const node = viewer.locator('.react-flow__node').filter({ hasText: 'Input text' });
  const position = await node.getAttribute('style');
  const box = await node.boundingBox();
  if (!box) throw new Error('Viewer node is not visible');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 80, box.y + box.height / 2 + 60, { steps: 5 });
  await page.mouse.up();
  await expect(node).toHaveAttribute('style', position!);
  await node.focus();
  await page.keyboard.press('Delete');
  await page.keyboard.press('Backspace');
  await expect(viewer.locator('.react-flow__node')).toHaveCount(2);
  await expect(viewer.getByRole('button', { name: /save|edit|interactive/i })).toHaveCount(0);
  await page.screenshot({ path: 'test-results/reusable-pipeline-viewer.png', fullPage: true });
  await viewer.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Manage', exact: true }).click();
  await page.getByRole('dialog', { name: 'Reusable pipelines', exact: true }).getByRole('button', { name: 'View pipeline' }).click();
  await expect(viewer.locator('.react-flow__node')).toHaveCount(2);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Reusable pipelines', exact: true })).toBeVisible();
  expect(server.graph()).toEqual(before);
  expect(server.mutations).toHaveLength(writes);
});

test('a failed read-only preview can retry without changing the main canvas', async ({ page }) => {
  const server = await setup(page, true);
  let attempts = 0;
  await page.route('**/api/reusable-pipelines/definition?*', async route => {
    attempts++;
    return route.fulfill(attempts === 1
      ? { status: 503, json: { error: 'Preview temporarily unavailable' } }
      : { json: { reference, graph: childGraph, interface: contract } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Library', exact: true }).click();
  await page.getByRole('button', { name: 'View pipeline', exact: true }).click();
  const viewer = page.getByRole('dialog', { name: 'Text adapter — Read only', exact: true });
  await expect(viewer.getByRole('alert')).toContainText('Preview temporarily unavailable');
  await viewer.getByRole('button', { name: 'Try again' }).click();
  await expect(viewer.locator('.react-flow__node')).toHaveCount(2);
  expect(server.graph().nodes.map(node => node.id)).toEqual(['input', 'output']);
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
});
