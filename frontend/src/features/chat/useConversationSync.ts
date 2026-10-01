import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import type { ChatMessage } from './chatTypes';
import { fetchConversation, mergeMessages } from './conversationService';
import { startPolling } from '@/utils/polling';

export function useConversationSync(paused: boolean) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState('');
  const [pendingTurnId, setPendingTurnId] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const state = useRef({ id: '', revision: 0, messages: [] as ChatMessage[], paused, epoch: 0, failed: false });
  state.current.paused = paused;
  const inFlight = useRef(false);
  const live = useRef(true);
  const controller = useRef<AbortController | null>(null);

  const setLocalMessages: Dispatch<SetStateAction<ChatMessage[]>> = useCallback(update => {
    state.current.epoch++;
    setMessages(current => {
      const next = typeof update === 'function' ? update(current) : update;
      state.current.messages = next;
      return next;
    });
  }, []);

  const reset = useCallback(() => {
    state.current.epoch++;
    state.current.id = '';
    state.current.revision = 0;
    state.current.messages = [];
    setMessages([]); setConversationId(''); setPendingTurnId(null); setHasOlder(false);
  }, []);

  const refresh = useCallback(async () => {
    if (inFlight.current || state.current.paused || !live.current) return false;
    inFlight.current = true;
    const epoch = state.current.epoch;
    controller.current = new AbortController();
    try {
      const page = await fetchConversation({ after: state.current.revision, conversationId: state.current.id,
        signal: controller.current.signal });
      if (!live.current || state.current.paused || epoch !== state.current.epoch) return false;
      const resetPage = page.reset || page.conversation?.id !== state.current.id;
      const next = mergeMessages(state.current.messages, page.messages, resetPage);
      state.current.messages = next;
      state.current.id = page.conversation?.id || '';
      state.current.revision = page.conversation?.revision || 0;
      setMessages(next); setConversationId(state.current.id);
      setPendingTurnId(page.conversation?.pending_turn_id || null);
      if (resetPage) setHasOlder(page.has_older);
      setReady(true); setError('');
      state.current.failed = false;
      return page.has_more;
    } catch (failure) {
      if (live.current && !(failure instanceof DOMException && failure.name === 'AbortError')) {
        state.current.failed = true;
        setError(failure instanceof Error ? failure.message : 'Conversation history is unavailable.');
      }
      return false;
    } finally { inFlight.current = false; }
  }, []);

  const loadOlder = useCallback(async () => {
    if (inFlight.current || state.current.paused || !state.current.id || !state.current.messages.length) return;
    const first = state.current.messages[0].sequence;
    if (!first) return;
    inFlight.current = true; setLoadingOlder(true);
    const epoch = state.current.epoch;
    try {
      const page = await fetchConversation({ conversationId: state.current.id, before: first });
      if (!live.current || epoch !== state.current.epoch || page.conversation?.id !== state.current.id) return;
      const next = mergeMessages(state.current.messages, page.messages);
      state.current.messages = next;
      setMessages(next); setHasOlder(page.has_older); setError('');
    } catch (failure) {
      if (live.current) setError(failure instanceof Error ? failure.message : 'Older messages are unavailable.');
    } finally { inFlight.current = false; if (live.current) setLoadingOlder(false); }
  }, []);

  useEffect(() => {
    live.current = true;
    const snapshot = state.current;
    const stop = startPolling(async () => {
      const more = await refresh();
      if (state.current.failed) throw new Error('History refresh failed.');
      return more ? 100 : undefined;
    });
    return () => { stop(); live.current = false; snapshot.epoch++; controller.current?.abort(); };
  }, [refresh]);

  const setActiveId = useCallback((id: string) => { state.current.id = id; setConversationId(id); }, []);
  return { messages, setMessages: setLocalMessages, conversationId, setConversationId: setActiveId,
    pendingTurnId, ready, error, hasOlder, loadingOlder, loadOlder, refresh, reset };
}
