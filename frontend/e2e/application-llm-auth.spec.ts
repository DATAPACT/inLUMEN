import { test, expect } from '@playwright/test';
import { authenticatedApp } from './helpers/auth';

test('admin saves, disables and re-enables the shared LLM without retaining its key in browser storage', async ({ page }) => {
  let config: Record<string, unknown> | null = null;
  let enabled = false;
  let revision = 0;
  const writes: Array<Record<string, unknown>> = [];
  let failSave = false;
  await page.route('https://openrouter.ai/api/v1/models**', route => route.fulfill({ json: { data: [] } }));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/admin/application-llm') {
      expect(route.request().headers()['authorization']).toBe('Bearer browser-test-token');
      expect(route.request().headers()['x-inlumen-workspace-id']).toBe('browser-test-workspace');
      if (route.request().method() === 'PUT') {
        const payload = route.request().postDataJSON();
        writes.push(payload);
        if (failSave) {
          await route.fulfill({ status: 503, json: { error: 'Settings unavailable. Try again.' } });
          return;
        }
        expect(payload.revision).toBe(revision);
        const { api_key: _key, ...metadata } = payload.config;
        config = { ...metadata, has_api_key: true, readOnly: true, applicationProvided: true };
        enabled = payload.enabled;
        revision++;
      }
      await route.fulfill({ json: { config, enabled, revision } });
    } else if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: enabled ? [config] : [] } });
    } else if (path === '/api/pipeline/graph') {
      await route.fulfill({ json: { nodes: [], edges: [], updated_at: '2026-01-01' }, headers: { ETag: '"1"' } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], nodes: [], edges: [] } });
    }
  });
  const app = await authenticatedApp(page, true);
  const openAdmin = async () => {
    await app.getByRole('button', { name: 'Settings', exact: true }).click();
    await app.getByRole('button', { name: 'Manage shared LLM', exact: true }).click();
    await expect(app.getByRole('switch', { name: 'Enable for all users' })).toBeVisible();
  };
  const save = async () => {
    await app.getByRole('button', { name: 'Save Configuration' }).click();
    await expect(app.getByRole('dialog')).not.toBeVisible();
  };
  await page.goto('/__test_auth_host');
  await openAdmin();
  await app.getByRole('combobox', { name: 'Provider', exact: true }).click();
  await app.getByRole('option', { name: 'Custom / On premise' }).click();
  await app.getByLabel('OpenAI-Compatible Base URL').fill('https://llm.test/v1');
  await app.getByLabel('Model', { exact: true }).fill('shared-chat');
  await app.getByLabel('Code Generation Model', { exact: true }).fill('shared-code');
  await app.getByLabel('API Key', { exact: true }).fill('shared-browser-test-key');
  await app.getByRole('switch', { name: 'Enable for all users' }).click();
  // Toggling enabled must not reset edited values or erase the entered key.
  await expect(app.getByLabel('Model', { exact: true })).toHaveValue('shared-chat');
  await save();
  expect(writes[0]).toMatchObject({ enabled: true, config: { model: 'shared-chat', api_key: 'shared-browser-test-key' } });

  await openAdmin();
  await expect(app.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(app.getByRole('switch', { name: 'Enable for all users' })).toBeChecked();
  await page.screenshot({ path: 'test-results/shared-llm-admin.png', fullPage: true, animations: 'disabled' });
  await app.getByRole('switch', { name: 'Enable for all users' }).click();
  await save();
  expect(writes[1]).toMatchObject({ enabled: false, config: { api_key: '' } });
  await page.reload();
  await openAdmin();
  await expect(app.getByLabel('Model', { exact: true })).toHaveValue('shared-chat');
  await expect(app.getByRole('switch', { name: 'Enable for all users' })).not.toBeChecked();
  await app.getByRole('switch', { name: 'Enable for all users' }).click();
  await save();
  expect(writes[2]).toMatchObject({ enabled: true, config: { api_key: '' } });

  await openAdmin();
  await app.getByLabel('API Key', { exact: true }).fill('failed-save-test-key');
  failSave = true;
  await app.getByRole('button', { name: 'Save Configuration' }).click();
  await expect(app.getByText('Settings unavailable. Try again.')).toBeVisible();
  await expect(app.getByRole('dialog')).toBeVisible();
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain('shared-browser-test-key');
  expect(storage).not.toContain('failed-save-test-key');
  await app.getByRole('button', { name: 'Close', exact: true }).click();
  await openAdmin();
  await expect(app.getByLabel('API Key', { exact: true })).toHaveValue('');
});


test('authenticated non-admin cannot manage the shared LLM', async ({ page }) => {
  let adminRequests = 0;
  await page.route('**/api/**', async route => {
    if (new URL(route.request().url()).pathname.startsWith('/api/admin/')) adminRequests++;
    await route.fulfill({ json: { configs: [], nodes: [], edges: [], runs: [], versions: [], definitions: [] } });
  });
  const app = await authenticatedApp(page, false);
  await page.goto('/__test_auth_host');
  await app.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(app.getByRole('dialog')).toBeVisible();
  await expect(app.getByRole('button', { name: 'Manage shared LLM', exact: true })).toHaveCount(0);
  expect(adminRequests).toBe(0);
});
