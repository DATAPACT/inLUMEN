import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import * as service from './conversationService';
import { useConversationSync } from './useConversationSync';

vi.mock('./conversationService', async importOriginal => ({
  ...await importOriginal<typeof import('./conversationService')>(), fetchConversation: vi.fn(),
}));
afterEach(() => vi.useRealTimers());

it('a late history response cannot replace an optimistic send or a local reset', async () => {
  vi.useFakeTimers();
  const reactEnvironment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  reactEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  let current!: ReturnType<typeof useConversationSync>;
  let release!: (value: service.ConversationPage) => void;
  const empty: service.ConversationPage = { conversation: { id: 'owned', revision: 1, pending_turn_id: null },
    messages: [], reset: true, has_older: false, has_more: false };
  vi.mocked(service.fetchConversation).mockResolvedValueOnce(empty)
    .mockImplementationOnce(() => new Promise(resolve => { release = resolve; }))
    .mockResolvedValue(empty);
  function View({ paused }: { paused: boolean }) { current = useConversationSync(paused); return null; }
  const root = createRoot(document.createElement('div'));
  try {
    await act(async () => root.render(<View paused={false} />));
    expect(current.ready).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(service.fetchConversation).toHaveBeenCalledTimes(2);
    await act(async () => {
      current.setMessages([{ role: 'user', content: 'New local request' }]);
      root.render(<View paused={true} />);
    });
    await act(async () => { release(empty); });
    expect(current.messages.map(message => message.content)).toEqual(['New local request']);
    await act(async () => { current.reset(); root.render(<View paused={false} />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(current.messages).toEqual([]);
  } finally {
    await act(async () => root.unmount());
    reactEnvironment.IS_REACT_ACT_ENVIRONMENT = false;
  }
});
