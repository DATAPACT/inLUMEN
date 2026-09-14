import { test, expect } from '@playwright/test';

test('detected parameters can be added by keyboard, filled and reopened without exposing secrets', async ({ page }) => {
  let revision = 1;
  let data: Record<string, unknown> = {
    type: 'task', label: 'Speech-to-Text', param: {}, implementation: { kind: 'python' },
    files: [{ filename: 'main.py', role: 'code' }],
    generated_artifact: { status: 'current', runtime_environment: [
      { name: 'INLUMEN_ASR_DEVICE', required: false, secret: false },
      { name: 'CREDENTIAL', required: true, secret: true },
    ] },
  };
  const secretValues: Record<string, string> = {};
  const graphWrites: unknown[] = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === '/api/graph/nodes/properties' && method === 'POST') {
      const payload = route.request().postDataJSON();
      graphWrites.push(payload);
      data = { ...data, ...payload.properties };
      return route.fulfill({ json: { ok: true }, headers: { ETag: `"${++revision}"` } });
    }
    if (path === '/api/pipeline/graph') {
      if (method === 'POST') {
        const payload = route.request().postDataJSON();
        graphWrites.push(payload);
        if (payload.graph?.nodes?.[0]) data = { ...data, ...payload.graph.nodes[0].data };
        revision++;
      }
      return route.fulfill({ json: { nodes: [{ id: '1', type: 'custom', position: { x: 150, y: 150 }, data }], edges: [], updated_at: 'initial' }, headers: { ETag: `"${revision}"` } });
    }
    if (path.startsWith('/api/nodes/1/secrets/')) {
      const name = decodeURIComponent(path.split('/').at(-1)!);
      if (method === 'PUT') secretValues[name] = route.request().postDataJSON().value;
      if (method === 'DELETE') delete secretValues[name];
      return route.fulfill({ json: { ok: true } });
    }
    if (path === '/api/nodes/1/secrets') return route.fulfill({ json: { configured: Object.keys(secretValues) } });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], updated_at: 'initial' }, headers: { ETag: `"${revision}"` } });
  });
  const select = async () => {
    await page.locator('.react-flow__node').first().click();
    await expect(page.getByRole('heading', { name: 'Properties', exact: true })).toBeVisible();
  };
  await page.goto('/');
  await select();
  const suggestion = page.getByRole('button', { name: 'Add INLUMEN_ASR_DEVICE to Runtime parameters', exact: true });
  await suggestion.focus();
  await suggestion.press('Enter');
  const value = page.locator('#parameter-INLUMEN_ASR_DEVICE');
  await expect(value).toBeFocused();
  await expect(value).toHaveValue('');
  await value.fill('cpu');
  await expect(suggestion).toBeDisabled();
  await page.getByRole('button', { name: 'Add CREDENTIAL to Runtime parameters', exact: true }).click();
  const secret = page.locator('#parameter-CREDENTIAL');
  await expect(secret).toBeFocused();
  await expect(secret).toHaveAttribute('type', 'password');
  await secret.fill('test-only-sensitive-value');
  await secret.press('Tab');
  await expect.poll(() => secretValues.CREDENTIAL).toBe('test-only-sensitive-value');
  await expect.poll(() => (data.param as Record<string, string>)?.INLUMEN_ASR_DEVICE).toBe('cpu');
  await expect(page.getByLabel('Pipeline save status')).toHaveAttribute('data-save-state', 'saved');
  await page.reload();
  await select();
  await expect(value).toHaveValue('cpu');
  await expect(suggestion).toBeDisabled();
  await expect(secret).toHaveValue('');
  await expect(page.getByText('Stored securely', { exact: true })).toBeVisible();
  expect(JSON.stringify(graphWrites)).not.toContain('test-only-sensitive-value');
  await page.locator('#inspector-runtime-environment').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/beta4-parameters.png', fullPage: true });
});

test('completed generation duration remains visible in history after reload', async ({ page }) => {
  const job = {
    run_id: 'measured-run', status: 'valid', target_flow_ids: ['1'],
    created_at: '2026-09-14T10:00:00Z', started_at: '2026-09-14T10:00:02Z',
    finished_at: '2026-09-14T10:01:17Z', duration_ms: 75000, queue_duration_ms: 2000,
    persistence: { status: 'persisted' },
    generation_run: { run_id: 'measured-run', status: 'valid', steps: [], generation_usage: { total_tokens: 1200, cost_usd: 0.012, request_count: 1 } },
  };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pipeline/generation-runs') return route.fulfill({ json: { runs: [job] } });
    if (path === '/api/pipeline/generation-runs/measured-run') return route.fulfill({ json: job });
    if (path === '/api/pipeline/graph') return route.fulfill({ json: { nodes: [], edges: [] }, headers: { ETag: '"1"' } });
    return route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], target_count: 0 } });
  });
  await page.goto('/');
  for (let attempt = 0; attempt < 2; attempt++) {
    const library = page.getByRole('button', { name: 'Library', exact: true });
    if (await library.getAttribute('aria-pressed') !== 'true') await library.click();
    await page.getByRole('tab', { name: 'Run', exact: true }).click();
    await page.getByRole('button', { name: 'Generate runtime code', exact: true }).click();
    const row = page.getByRole('button').filter({ hasText: '1 package scope' });
    await expect(row).toContainText('1m 15s');
    await row.click();
    await expect(page.getByText('Generation duration', { exact: true }).locator('..')).toContainText('1m 15s');
    await expect(page.getByText('Queue time', { exact: true }).locator('..')).toContainText('2s');
    await expect(page.getByText('Model tokens', { exact: true }).locator('..')).toContainText('1,200');
    await page.screenshot({ path: 'test-results/beta4-duration.png', fullPage: true });
    if (attempt === 0) await page.reload();
  }
});
