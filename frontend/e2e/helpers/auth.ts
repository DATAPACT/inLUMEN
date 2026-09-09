import { expect, type Page } from '@playwright/test';

// Exercise the real embedded SSO bootstrap; only the host and API are mocked.
export async function authenticatedApp(page: Page, isAdmin: boolean) {
  await page.route('**/api/session', async route => {
    expect(route.request().headers()['authorization']).toBe('Bearer browser-test-token');
    await route.fulfill({ json: {
      is_application_admin: isAdmin,
      user: { id: 'browser-test-user', subject: 'browser-test-user', display_name: 'Browser test user' },
      active_workspace_id: 'browser-test-workspace',
      workspaces: [{ id: 'browser-test-workspace', name: 'Test workspace', role: 'owner' }],
    } });
  });
  await page.route('**/__test_auth_host', route => route.fulfill({
    contentType: 'text/html',
    body: `<!doctype html><html><head><title>Test SSO host</title>
      <style>html,body,iframe { width:100%; height:100%; margin:0; border:0; }</style>
      </head><body><script>
        window.addEventListener('message', event => {
          const frame = document.querySelector('iframe');
          if (event.origin === location.origin && event.source === frame?.contentWindow && event.data?.type === 'IFRAME_READY') {
            frame.contentWindow.postMessage({ type: 'SSO_TOKEN', token: 'browser-test-token' }, location.origin);
          }
        });
      </script><iframe title="inLUMEN" src="/"></iframe></body></html>`,
  }));
  return page.frameLocator('iframe[title="inLUMEN"]');
}
