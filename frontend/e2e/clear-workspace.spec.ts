import { test, expect } from '@playwright/test';

test('Clear all requires the confirmation used by the load runner', async ({ page }) => {
  let clears = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/workspace/clear-all') {
      clears++;
      await route.fulfill({ json: { status: 'ok', graph: { nodes: [], edges: [] }, version: { uid: 'main', name: 'Main' } } });
    } else if (path === '/api/pipeline/graph') {
      await route.fulfill({ json: { nodes: [], edges: [], updated_at: '2026-01-01' } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], nodes: [], edges: [] } });
    }
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Clear all', exact: true }).click();
  const confirmation = page.getByRole('alertdialog', { name: 'Clear the entire workspace?' });
  await expect(confirmation).toBeVisible();
  expect(clears).toBe(0);
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/workspace/clear-all');
  await confirmation.getByRole('button', { name: 'Clear workspace', exact: true }).click();
  expect((await response).status()).toBe(200);
  await expect(confirmation).not.toBeVisible();
  await expect(page.getByRole('button', { name: 'Clear all', exact: true })).toBeEnabled();
  expect(clears).toBe(1);
});
