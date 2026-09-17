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

test('agent graph changes stay in preview until applied, then refresh the revision', async ({ page }) => {
  let revision = 1;
  let applied = false;
  const writes: string[] = [];
  const graph = () => ({ nodes: [{ id: '1', type: 'source', position: { x: 150, y: 150 }, data: { type: 'source', label: applied ? 'Agent updated source' : 'Original source' } }], edges: [], updated_at: '2026-01-01' });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'GET' && (path.startsWith('/api/graph/') || path.startsWith('/api/pipeline/'))) {
      const expected = route.request().headers()['if-match'];
      writes.push(expected);
      if (expected !== `"${revision}"`) {
        await route.fulfill({ status: 409, json: { code: 'graph_conflict' } });
        return;
      }
      if (path === '/api/pipeline/graph') applied = true;
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
    const proposal = { nodes: [{ id: '1', type: 'source', position: { x: 150, y: 150 }, data: { type: 'source', label: 'Agent updated source' } }], edges: [], updated_at: '2026-01-02' };
    await route.fulfill({ json: { assistant_message: 'Updated the source.', graph: proposal, sync: { status: 'preview', guardrail_passed: true, graph_safe_to_apply: true, preview_pending: true } } });
  });
  await page.goto('/');
  await expect(page.getByText('Original source', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Rename the source');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toBeVisible();
  await expect(page.getByText(/Updated steps/)).toBeVisible();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Original source');
  await expect(page.getByText('Agent updated source', { exact: true }).first()).toBeVisible();
  expect(writes).toHaveLength(0);
  await page.getByRole('button', { name: 'Apply to canvas' }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toHaveCount(0);
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Agent updated source');
  await expect.poll(() => writes.length).toBeGreaterThan(0);
  expect(writes[0]).toBe('"1"');
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
  const count = writes.length;
  const node = page.locator('#canvas-panel .react-flow__node').first();
  await node.focus();
  await node.press('Enter');
  await node.press('ArrowRight');
  await expect.poll(() => writes.length).toBeGreaterThan(count);
  await expect(page.getByText('Couldn’t save', { exact: true })).toHaveCount(0);
});

test('graph preview also works for a proposal on an empty canvas', async ({ page }) => {
  let revision = 1;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph' && route.request().method() === 'GET') {
      await route.fulfill({ json: { nodes: [], edges: [], updated_at: '2026-01-01' }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/graph' && route.request().method() === 'POST') {
      expect(route.request().headers()['if-match']).toBe(`"${revision}"`);
      revision++;
      await route.fulfill({ json: { ok: true }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [{ id: 'test-config', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
  });
  await page.route('**/simple_chat', async route => {
    await route.fulfill({ json: {
      assistant_message: 'I drafted a starter graph.',
      graph: {
        nodes: [
          { id: 'source', type: 'custom', position: { x: 100, y: 120 }, data: { type: 'source', label: 'New input' } },
          { id: 'destination', type: 'custom', position: { x: 400, y: 120 }, data: { type: 'destination', label: 'New output' } },
        ],
        edges: [{ id: 'edge-1', source: 'source', target: 'destination', sourceHandle: 'data', targetHandle: 'data' }],
      },
      sync: {
        status: 'preview',
        guardrail_passed: true,
        graph_safe_to_apply: true,
        preview_pending: true,
      },
    } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Create a simple input to output pipeline');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toBeVisible();
  await expect(page.getByText('New steps (2)')).toBeVisible();
  await expect(page.getByText('Connections added (1)')).toBeVisible();
  await expect(page.getByText('Flow direction', { exact: true })).toBeVisible();
  await expect(page.locator('.react-flow__edge path[marker-end]')).toHaveCount(1);
  await expect(page.getByText('Draft proposal · awaiting review', { exact: true })).toBeVisible();
  await expect(page.getByText('New input', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('New output', { exact: true }).first()).toBeVisible();
  await expect(page.locator('#canvas-panel .react-flow__node')).toHaveCount(0);
  await page.getByRole('button', { name: 'Discard proposal' }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toHaveCount(0);
  await expect(page.getByText('Proposal discarded · canvas unchanged', { exact: true })).toBeVisible();
  await expect(page.locator('#canvas-panel .react-flow__node')).toHaveCount(0);
});

test('a discarded preview is clearly labeled and the next request uses the unchanged canvas', async ({ page }) => {
  let turn = 0;
  let nextRequestCanvasLabel: string | undefined;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph' && route.request().method() === 'GET') {
      await route.fulfill({ json: {
        nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: 'Original source' } }],
        edges: [],
      }, headers: { ETag: '"1"' } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [{ id: 'test-config', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
  });
  await page.route('**/simple_chat', async route => {
    turn += 1;
    const body = await route.request().postDataJSON();
    if (turn === 2) nextRequestCanvasLabel = body.canvas_graph.nodes[0].label;
    await route.fulfill({ json: {
      assistant_message: turn === 1 ? 'I drafted the requested change.' : 'I drafted the follow-up change.',
      graph: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: turn === 1 ? 'Proposed source' : 'Follow-up source' } }], edges: [] },
      sync: {
        status: 'preview',
        guardrail_passed: true,
        graph_safe_to_apply: true,
        preview_pending: true,
      },
    } });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Make a first change');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toBeVisible();
  await page.getByRole('button', { name: 'Discard proposal' }).click();
  await expect(page.getByText('Proposal discarded · canvas unchanged', { exact: true })).toBeVisible();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Original source');

  await page.getByPlaceholder('Describe the pipeline...').fill('Make a follow-up change');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toBeVisible();
  await expect.poll(() => nextRequestCanvasLabel).toBe('Original source');
});

test('a no-op response does not open a graph preview when Review AI is enabled', async ({ page }) => {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph' && route.request().method() === 'GET') {
      await route.fulfill({ json: {
        nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: 'Original source' } }],
        edges: [],
        updated_at: '2026-01-01',
      }, headers: { ETag: '"1"' } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [{ id: 'test-config', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
  });
  await page.route('**/simple_chat', async route => {
    await route.fulfill({ json: {
      assistant_message: 'The current pipeline is unchanged.',
      graph: {
        // The real agent response uses the persisted graph shape, where node
        // coordinates are flattened as x/y instead of React Flow position.
        nodes: [{ id: '1', type: 'source', label: 'Original source', description: '', x: 420, y: 220 }],
        edges: [],
        updated_at: '2026-01-02',
      },
      sync: {
        status: 'unchanged',
        graph_changed: false,
        guardrail_passed: true,
        graph_safe_to_apply: true,
        preview_pending: true,
      },
    } });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Describe the current pipeline without making any changes.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toHaveCount(0);
  await expect(page.getByText('No graph changes to review', { exact: true })).toBeVisible();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Original source');
});

test('preview can be disabled for direct assistant edits', async ({ page }) => {
  let persistedLabel = 'Original';
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph' && route.request().method() === 'GET') {
      await route.fulfill({ json: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: persistedLabel } }], edges: [] }, headers: { ETag: '"1"' } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [{ id: 'test-config', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
    } else if (path === '/api/pipeline/versions/active') {
      await route.fulfill({ json: { version: { uid: 'main', name: 'Main' } }, headers: { ETag: '"2"' } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
  });
  await page.route('**/simple_chat', async route => {
    expect((await route.request().postDataJSON()).preview_changes).toBe(false);
    persistedLabel = 'Updated directly';
    await route.fulfill({ json: {
      assistant_message: 'Renamed the source.',
      graph: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: 'Updated directly' } }], edges: [] },
      sync: { guardrail_passed: true, graph_safe_to_apply: true },
    } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByRole('switch', { name: 'Preview AI graph changes before applying' }).click();
  await expect(page.getByRole('switch', { name: 'Preview AI graph changes before applying' })).toHaveAttribute('aria-checked', 'false');
  await page.getByPlaceholder('Describe the pipeline...').fill('Rename the source');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Updated directly');
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toHaveCount(0);
});

test('a follow-up direct edit works after applying a graph preview', async ({ page }) => {
  let revision = 1;
  let persistedLabel = 'Original';
  let followUpCanvasLabel: string | undefined;
  let turn = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph' && route.request().method() === 'GET') {
      await route.fulfill({ json: {
        nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: persistedLabel } }],
        edges: [],
      }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/graph' && route.request().method() === 'POST') {
      expect(route.request().headers()['if-match']).toBe(`"${revision}"`);
      const body = route.request().postDataJSON();
      persistedLabel = body.graph.nodes[0].data.label;
      revision++;
      await route.fulfill({ json: { ok: true }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/versions/active' && route.request().method() === 'POST') {
      revision++;
      await route.fulfill({ json: { version: { uid: 'main', name: 'Main', updated_at: '2026-01-02' } }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [{ id: 'test-config', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
  });
  await page.route('**/simple_chat', async route => {
    const body = await route.request().postDataJSON();
    turn++;
    if (turn === 1) {
      expect(body.preview_changes).toBe(true);
      await route.fulfill({ json: {
        assistant_message: 'I updated the source.',
        graph: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: 'First applied edit' } }], edges: [] },
        sync: { status: 'preview', guardrail_passed: true, graph_safe_to_apply: true, preview_pending: true },
      } });
      return;
    }
    if (turn === 2) {
      expect(body.preview_changes).toBe(false);
      followUpCanvasLabel = body.canvas_graph.nodes[0].label;
      persistedLabel = 'Second direct edit';
      await route.fulfill({ json: {
        assistant_message: 'I made the follow-up edit.',
        graph: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: 'Second direct edit' } }], edges: [] },
        sync: { guardrail_passed: true, graph_safe_to_apply: true },
      } });
      return;
    }
    expect(body.preview_changes).toBe(true);
    expect(body.canvas_graph.nodes[0].label).toBe('Second direct edit');
    await route.fulfill({ json: {
      assistant_message: 'I drafted another follow-up edit.',
      graph: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: 'Third preview edit' } }], edges: [] },
      sync: { status: 'preview', guardrail_passed: true, graph_safe_to_apply: true, preview_pending: true },
    } });
  });

  await page.goto('/');
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Original');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Rename this source');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toBeVisible();
  await page.getByRole('button', { name: 'Apply to canvas' }).click();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('First applied edit');
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');

  await page.getByRole('switch', { name: 'Preview AI graph changes before applying' }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Change its follow-up label');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Second direct edit');
  expect(followUpCanvasLabel).toBe('First applied edit');
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toHaveCount(0);
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');

  await page.getByRole('switch', { name: 'Preview AI graph changes before applying' }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Make another change with preview enabled');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toBeVisible();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Second direct edit');
  await page.getByRole('button', { name: 'Apply to canvas' }).click();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Third preview edit');
});

test('a stale proposal cannot overwrite a graph changed while it was being reviewed', async ({ page }) => {
  let revision = 1;
  let persistedLabel = 'Saved source';
  let applyRevision: string | undefined;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/graph' && route.request().method() === 'GET') {
      await route.fulfill({ json: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: persistedLabel } }], edges: [] }, headers: { ETag: `"${revision}"` } });
    } else if (path === '/api/pipeline/graph' && route.request().method() === 'POST') {
      applyRevision = route.request().headers()['if-match'];
      await route.fulfill({ status: 409, json: { code: 'graph_conflict', error: 'The saved graph has changed.' } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [{ id: 'test-config', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
    }
  });
  await page.route('**/simple_chat', async route => {
    revision = 2; // Another tab saves an edit while this proposal is being generated.
    persistedLabel = 'Changed in another tab';
    await route.fulfill({ json: {
      assistant_message: 'I updated the source.',
      graph: { nodes: [{ id: '1', type: 'custom', position: { x: 120, y: 120 }, data: { type: 'source', label: 'Assistant proposal' } }], edges: [] },
      sync: { status: 'preview', guardrail_passed: true, graph_safe_to_apply: true, preview_pending: true },
    } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Rename the source');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review proposed graph' })).toBeVisible();
  await page.getByRole('button', { name: 'Apply to canvas' }).click();
  await expect(page.getByRole('alert')).toContainText('out of date');
  expect(applyRevision).toBe('"1"');
  await expect(page.getByRole('button', { name: 'Apply to canvas' })).toBeDisabled();
  await expect(page.locator('#canvas-panel .react-flow__node').first()).toContainText('Saved source');
  await expect(page.getByText('Assistant proposal', { exact: true }).first()).toBeVisible();
});
