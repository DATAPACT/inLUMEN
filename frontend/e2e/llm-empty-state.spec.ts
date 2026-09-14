import { test, expect } from '@playwright/test';

test('settings show an empty state, then saved details, then empty after deleting the last configuration', async ({ page }) => {
  let configs: Record<string, unknown>[] = [];
  await page.route('https://openrouter.ai/api/v1/models**', route => route.fulfill({ json: { data: [] } }));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/chatbot-configs') {
      if (route.request().method() === 'POST') {
        const { api_key: _key, ...metadata } = route.request().postDataJSON();
        configs = [{ ...metadata, id: 'personal-test', has_api_key: true }];
        return route.fulfill({ json: { config: configs[0] } });
      }
      return route.fulfill({ json: { configs } });
    }
    if (path === '/api/chatbot-configs/personal-test' && route.request().method() === 'DELETE') {
      configs = [];
      return route.fulfill({ json: { success: true } });
    }
    return route.fulfill({ json: { configs: [], nodes: [], edges: [], versions: [], runs: [], definitions: [] }, headers: { ETag: '"1"' } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const settings = page.getByRole('dialog').filter({ hasText: 'LLM configuration' });
  const assertEmpty = async () => {
    await expect(settings.getByRole('button', { name: 'No LLM configured', exact: true })).toBeVisible();
    for (const label of ['Provider:', 'Model:', 'Code generation:', 'Base URL:']) {
      await expect(settings.getByText(label, { exact: true })).toHaveCount(0);
    }
    await expect(settings.getByText('gpt-oss-120b', { exact: false })).toHaveCount(0);
  };
  await assertEmpty();
  await settings.getByRole('button', { name: 'No LLM configured', exact: true }).click();
  await page.getByRole('menuitem', { name: 'New Configuration' }).click();
  const form = page.getByRole('dialog', { name: 'New LLM Configuration', exact: true });
  await form.getByLabel('Configuration Name').fill('Personal test');
  await form.getByRole('combobox', { name: 'Provider', exact: true }).click();
  await page.getByRole('option', { name: 'Custom / On premise' }).click();
  await form.getByLabel('OpenAI-Compatible Base URL').fill('https://llm.test/v1');
  await form.getByLabel('Model', { exact: true }).fill('personal-chat');
  await form.getByLabel('Code Generation Model', { exact: true }).fill('personal-code');
  await form.getByLabel('API Key', { exact: true }).fill('browser-test-key');
  await form.getByRole('button', { name: 'Save Configuration' }).click();
  await expect(form).not.toBeVisible();
  await expect(settings.getByText('Code generation: personal-code', { exact: true })).toBeVisible();
  await settings.getByRole('button', { name: 'Custom / On premise / personal-chat' }).click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('menuitem').filter({ hasText: 'Personal test' }).getByRole('button').last().click();
  await expect.poll(() => configs.length).toBe(0);
  await page.keyboard.press('Escape');
  await assertEmpty();
  await page.reload();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await assertEmpty();
});
