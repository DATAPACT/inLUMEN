import type { Page } from '@playwright/test';

// Existing editor fixtures stub the model response. Provide the corresponding
// durable history API too, so polling/reloads exercise the production contract.
export async function installConversationFixture(page: Page) {
  let revision = 0;
  let sequence = 0;
  let messages: Record<string, unknown>[] = [];
  const id = 'fixture-conversation';
  page.on('response', async response => {
    if (new URL(response.url()).pathname !== '/simple_chat' || !response.ok()) return;
    const request = response.request().postDataJSON();
    const result = await response.json();
    for (const [role, content] of [['user', request.user_message], ['assistant', result.assistant_message]]) {
      if (typeof content !== 'string') continue;
      messages.push({ id: `fixture-${++sequence}`, sequence, revision: ++revision,
        turn_id: request.turn_id, role, content, status: 'completed', created_at: '2026-10-01',
        proposal_status: role === 'assistant' && result.sync?.preview_pending ? 'pending' : null });
    }
  });
  await page.route('**/api/chat/**', async route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'DELETE') { messages = []; revision++; }
    if (route.request().method() === 'PATCH') {
      const message = messages.find(m => m.id === url.pathname.split('/').pop());
      if (message) { message.proposal_status = route.request().postDataJSON().proposal_status; message.revision = ++revision; }
    }
    const after = Number(url.searchParams.get('after') || 0);
    return route.fulfill({ json: { conversation: { id, revision, pending_turn_id: null },
      messages: messages.filter(m => Number(m.revision) > after), reset: after === 0,
      has_older: false, has_more: false } });
  });
}
