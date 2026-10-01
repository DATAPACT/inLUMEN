import { test, expect } from '@playwright/test';

test('Run shows queue position and workload, recovers after reload, and cancels without resubmitting', async ({ page }) => {
  let cancelled = false, launches = 0;
  const record = () => ({ run_id: 'own-run', status: cancelled ? 'cancelled' : 'running',
    snapshot: { pipeline_version: 'main', node_count: 6 }, created_at: new Date(Date.now() - 12000).toISOString(),
    progress: { phase: 'waiting_for_capacity', queue_position: 7, observed_at: new Date().toISOString() } });
  await page.route('**/api/**', async route => {
    const { pathname } = new URL(route.request().url());
    if (pathname === '/api/pipeline/graph') return route.fulfill({ json: { nodes: [], edges: [] } });
    if (pathname.endsWith('/capabilities')) return route.fulfill({ json: { execution_available: true, max_outstanding_runs: 4 } });
    if (pathname.endsWith('/workload')) return route.fulfill({ json: { outstanding_runs: 12, max_outstanding_runs: 50, worker_available: true, active_runs: 2, max_active_runs: 2, queued_runs: 10 } });
    if (pathname === '/api/pipeline-runs') {
      if (route.request().method() === 'POST') launches++;
      return route.fulfill({ json: { runs: [record()] } });
    }
    if (pathname.endsWith('/events')) return route.fulfill({ json: { events: [], next_cursor: 0 } });
    if (pathname.endsWith('/own-run')) {
      if (route.request().method() === 'DELETE') cancelled = true;
      return route.fulfill({ json: record() });
    }
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [] } });
  });
  const open = async () => {
    await page.goto('/');
    const library = page.getByRole('button', { name: 'Library', exact: true });
    if (await library.getAttribute('aria-pressed') !== 'true') await library.click();
    await page.getByRole('tab', { name: 'Run', exact: true }).click();
  };
  await open();
  await expect(page.getByRole('status').filter({ hasText: 'Queued' })).toBeVisible();
  await expect(page.getByText(/Waiting for an execution slot · position 7/)).toBeVisible();
  await expect(page.getByText('2 running / 2 slots · 10 queued across inLUMEN')).toBeVisible();
  await expect(page.getByText(/Queue wait:/)).toBeVisible();
  await page.screenshot({ path: 'test-results/run-queue.png' });
  await open();
  await expect(page.getByText(/Waiting for an execution slot · position 7/)).toBeVisible();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toHaveCount(0);
  expect(cancelled).toBe(true);
  expect(launches).toBe(0);
});
