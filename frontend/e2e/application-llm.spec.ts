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
  await expect(settings.getByRole('button', { name: 'Manage shared LLM', exact: true })).toHaveCount(0);
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
