import { test, expect } from '@playwright/test';

test('application LLM is selected automatically, read-only, and sends only a credential reference', async ({ page }) => {
  const managed = {
    id: 'application-llm', name: 'Application-provided LLM', provider: 'custom',
    model: 'managed-chat', codegenModel: 'managed-code', baseUrl: 'https://llm.test/v1',
    has_api_key: true, readOnly: true, applicationProvided: true,
  };
  const personal = {
    id: 'personal', name: 'Personal OpenRouter', provider: 'openrouter',
    model: 'test/personal', codegenModel: 'test/code', baseUrl: 'https://openrouter.ai/api/v1',
    has_api_key: true,
  };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/chatbot-configs') {
      await route.fulfill({ json: { configs: [managed, personal] } });
    } else if (path === '/api/pipeline/graph') {
      await route.fulfill({ json: { nodes: [], edges: [], updated_at: '2026-01-01' }, headers: { ETag: '"1"' } });
    } else if (path === '/api/pipeline/updated-at') {
      await route.fulfill({ json: { updated_at: '2026-01-01' } });
    } else {
      await route.fulfill({ json: { configs: [], runs: [], versions: [], definitions: [], nodes: [], edges: [] } });
    }
  });
  let sentConfig: Record<string, unknown> | undefined;
  await page.route('**/simple_chat', async route => {
    sentConfig = route.request().postDataJSON().llm_config;
    await route.fulfill({ json: { assistant_message: 'Application model is ready.' } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const settings = page.getByRole('dialog');
  await expect(settings.getByText('Application-provided LLM · Managed by your administrator. No API key needed.')).toBeVisible();
  await expect(settings.getByText('Code generation: managed-code', { exact: true })).toBeVisible();
  await settings.getByRole('button', { name: 'Custom / On premise / managed-chat' }).click();
  const managedItem = page.getByRole('menuitem').filter({ hasText: 'Application-provided LLM' });
  await expect(managedItem).toBeVisible();
  await expect(managedItem.getByRole('button')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(managedItem).not.toBeVisible();
  await settings.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(settings).not.toBeVisible();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByPlaceholder('Describe the pipeline...').fill('Hello');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByText('Application model is ready.')).toBeVisible();
  expect(sentConfig).toMatchObject({ credential_id: 'application-llm', model: 'managed-chat' });
  expect(sentConfig).not.toHaveProperty('api_key');
  await page.reload();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('Code generation: managed-code', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/application-llm-settings.png' });
  await page.getByRole('dialog').getByRole('button', { name: 'Custom / On premise / managed-chat' }).click();
  await page.getByRole('menuitem').filter({ hasText: 'Personal OpenRouter' }).click();
  await expect(page.getByRole('dialog').getByText('Code generation: test/code', { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('Code generation: test/code', { exact: true })).toBeVisible();
});

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
  const openAdmin = async () => {
    await page.getByRole('button', { name: 'Settings', exact: true }).click();
    await page.getByRole('button', { name: 'Manage shared LLM', exact: true }).click();
    await expect(page.getByRole('switch', { name: 'Enable for all users' })).toBeVisible();
  };
  const save = async () => {
    await page.getByRole('button', { name: 'Save Configuration' }).click();
    await expect(page.getByRole('dialog')).not.toBeVisible();
  };
  await page.goto('/');
  await openAdmin();
  await page.getByRole('combobox', { name: 'Provider', exact: true }).click();
  await page.getByRole('option', { name: 'Custom / On premise' }).click();
  await page.getByLabel('OpenAI-Compatible Base URL').fill('https://llm.test/v1');
  await page.getByLabel('Model', { exact: true }).fill('shared-chat');
  await page.getByLabel('Code Generation Model', { exact: true }).fill('shared-code');
  await page.getByLabel('API Key', { exact: true }).fill('shared-browser-test-key');
  await page.getByRole('switch', { name: 'Enable for all users' }).click();
  // Toggling enabled must not reset edited values or erase the entered key.
  await expect(page.getByLabel('Model', { exact: true })).toHaveValue('shared-chat');
  await save();
  expect(writes[0]).toMatchObject({ enabled: true, config: { model: 'shared-chat', api_key: 'shared-browser-test-key' } });

  await openAdmin();
  await expect(page.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(page.getByRole('switch', { name: 'Enable for all users' })).toBeChecked();
  await page.screenshot({ path: 'test-results/shared-llm-admin.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('switch', { name: 'Enable for all users' }).click();
  await save();
  expect(writes[1]).toMatchObject({ enabled: false, config: { api_key: '' } });
  await page.reload();
  await openAdmin();
  await expect(page.getByLabel('Model', { exact: true })).toHaveValue('shared-chat');
  await expect(page.getByRole('switch', { name: 'Enable for all users' })).not.toBeChecked();
  await page.getByRole('switch', { name: 'Enable for all users' }).click();
  await save();
  expect(writes[2]).toMatchObject({ enabled: true, config: { api_key: '' } });

  await openAdmin();
  await page.getByLabel('API Key', { exact: true }).fill('failed-save-test-key');
  failSave = true;
  await page.getByRole('button', { name: 'Save Configuration' }).click();
  await expect(page.getByText('Settings unavailable. Try again.')).toBeVisible();
  await expect(page.getByRole('dialog')).toBeVisible();
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain('shared-browser-test-key');
  expect(storage).not.toContain('failed-save-test-key');
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await openAdmin();
  await expect(page.getByLabel('API Key', { exact: true })).toHaveValue('');
});
