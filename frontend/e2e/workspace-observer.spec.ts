import { test, expect } from '@playwright/test';

// Independent browser contexts observe one mocked workspace without invoking a
// model, deployed API, or pipeline job.
test('open tabs recover persisted chat, graph changes, external runs and clears', async ({ browser }) => {
  const contexts = await Promise.all([browser.newContext({ baseURL: 'http://127.0.0.1:4175' }), browser.newContext({ baseURL: 'http://127.0.0.1:4175' })]);
  let revision = 0, resetRevision = 0, pending: string | null = null;
  let updatedAt: string | null = 'initial';
  let label = 'Initial saved source';
  let hasRun = false;
  let messages: Record<string, unknown>[] = [];
  const record = () => ({ run_id: 'external-run', status: 'running',
    snapshot: { pipeline_version: 'main', node_count: 1 }, created_at: '2026-10-01T12:00:00Z',
    progress: { phase: 'waiting_for_capacity', queue_position: 3, observed_at: new Date().toISOString() } });
  const addMessage = (role: string, content: string) => messages.push({ id: `message-${++revision}`,
    turn_id: 'external-turn', role, content, sequence: revision, revision, status: 'completed', created_at: '2026-10-01' });
  try {
    for (const context of contexts) {
      await context.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        const path = url.pathname;
        if (path === '/api/chat/conversation') {
          if (route.request().method() === 'DELETE') { messages = []; resetRevision = ++revision; }
          const after = Number(url.searchParams.get('after') || 0);
          const reset = after === 0 || after < resetRevision;
          return route.fulfill({ json: { conversation: { id: 'shared-conversation', revision, pending_turn_id: pending },
            messages: reset ? messages : messages.filter(m => Number(m.revision) > after), reset, has_older: false, has_more: false } });
        }
        if (path === '/api/pipeline/graph') return route.fulfill({ json: { nodes: label ? [{ id: 'source', type: 'custom', position: { x: 150, y: 120 }, data: { type: 'source', label } }] : [], edges: [], updated_at: updatedAt }, headers: { ETag: '"1"' } });
        if (path === '/api/pipeline/updated-at') return route.fulfill({ json: { updated_at: updatedAt } });
        if (path === '/api/chatbot-configs') return route.fulfill({ json: { configs: [{ id: 'test-model', name: 'Test model', provider: 'openrouter', model: 'test/model', has_api_key: true }] } });
        if (path.endsWith('/capabilities')) return route.fulfill({ json: { execution_available: true, max_outstanding_runs: 4 } });
        if (path.endsWith('/workload')) return route.fulfill({ json: { outstanding_runs: hasRun ? 1 : 0, max_outstanding_runs: 50, worker_available: true, active_runs: 0, max_active_runs: 3, queued_runs: hasRun ? 1 : 0 } });
        if (path === '/api/pipeline-runs') return route.fulfill({ json: { runs: hasRun ? [record()] : [] } });
        if (path.endsWith('/external-run')) return route.fulfill({ json: record() });
        if (path.endsWith('/events')) return route.fulfill({ json: { events: [], next_cursor: 0 } });
        return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
      });
    }
    const pages = await Promise.all(contexts.map(context => context.newPage()));
    for (const page of pages) {
      await page.goto('/');
      await page.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(page.locator('#canvas-panel .react-flow__node')).toContainText('Initial saved source');
    }
    pending = 'external-turn';
    addMessage('user', 'Create an audio transcription pipeline');
    for (const page of pages) {
      await expect(page.getByText('Create an audio transcription pipeline', { exact: true })).toBeVisible({ timeout: 10000 });
      await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    }
    label = 'Transcription created elsewhere'; updatedAt = 'saved-update';
    addMessage('assistant', 'The transcription pipeline is ready.'); pending = null;
    for (const page of pages) {
      await expect(page.getByText('The transcription pipeline is ready.', { exact: true })).toBeVisible({ timeout: 10000 });
      await expect(page.locator('#canvas-panel .react-flow__node')).toContainText(label, { timeout: 10000 });
    }
    await pages[1].reload();
    await expect(pages[1].getByText('The transcription pipeline is ready.', { exact: true })).toBeVisible();
    const library = pages[0].getByRole('button', { name: 'Library', exact: true });
    if (await library.getAttribute('aria-pressed') !== 'true') await library.click();
    await pages[0].getByRole('tab', { name: 'Run', exact: true }).click();
    await expect(pages[0].getByRole('button', { name: 'Run current pipeline', exact: true })).toBeEnabled();
    hasRun = true;
    await expect(pages[0].getByText(/Waiting for an execution slot · position 3/)).toBeVisible({ timeout: 10000 });
    await pages[1].getByRole('button', { name: 'Clear', exact: true }).click();
    for (const page of pages) await expect(page.getByText('The transcription pipeline is ready.', { exact: true })).toHaveCount(0, { timeout: 10000 });
    label = ''; updatedAt = null;
    for (const page of pages) await expect(page.locator('#canvas-panel .react-flow__node')).toHaveCount(0, { timeout: 10000 });
  } finally { await Promise.all(contexts.map(context => context.close())); }
});
