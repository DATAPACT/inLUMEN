import { INLUMEN_API_URL } from '@/config/api';
import { apiFetch } from '@/utils/apiFetch';
import type { ChatMessage } from './chatTypes';
import { sanitizeAssistantMessage } from './messageSafety';

export type StoredMessage = {
  id: string; turn_id: string; role: 'user' | 'assistant'; content: string;
  sequence: number; revision: number; status: string; created_at: string;
  proposal_status?: 'pending' | 'applied' | 'discarded' | null;
};
export type ConversationPage = {
  conversation: { id: string; revision: number; pending_turn_id: string | null; cancelling?: boolean } | null;
  messages: StoredMessage[]; reset: boolean; has_older: boolean; has_more: boolean;
};

export async function fetchConversation({ after = 0, before, conversationId, signal }: {
  after?: number; before?: number; conversationId?: string; signal?: AbortSignal;
} = {}): Promise<ConversationPage> {
  const query = new URLSearchParams({ after: String(after), limit: '100' });
  if (before !== undefined) query.set('before', String(before));
  if (conversationId) query.set('conversation_id', conversationId);
  const response = await apiFetch(`${INLUMEN_API_URL}/api/chat/conversation?${query}`, { signal });
  if (!response.ok) throw new Error('Conversation history could not be refreshed. Your messages are still here.');
  const page = await response.json();
  if (!('conversation' in page) || !Array.isArray(page.messages)) throw new Error('Conversation history returned an invalid response. Your messages are still here.');
  return { conversation: page.conversation ?? null, messages: page.messages ?? [],
    reset: page.reset ?? true, has_older: page.has_older ?? false, has_more: page.has_more ?? false };
}

export function mergeMessages(current: ChatMessage[], incoming: StoredMessage[], reset = false): ChatMessage[] {
  const messages = new Map<string, ChatMessage>();
  if (!reset) for (const message of current) if (message.id) messages.set(message.id, message);
  for (const message of incoming) messages.set(message.id, {
    id: message.id, turnId: message.turn_id, role: message.role, content: message.role === 'assistant' ? sanitizeAssistantMessage(message.content) : message.content,
    sequence: message.sequence, status: message.status,
    ...(message.proposal_status ? { graphProposalStatus: message.proposal_status } : {}),
  });
  const next = [...messages.values()].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
  return next.length === current.length && next.every((message, index) => JSON.stringify(message) === JSON.stringify(current[index])) ? current : next;
}

export async function clearConversation(conversationId: string) {
  const response = await apiFetch(`${INLUMEN_API_URL}/api/chat/conversation`, {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: conversationId || undefined }),
  });
  if (!response.ok) throw new Error('The conversation could not be cleared. Stop any running request and try again.');
}

export async function updateProposal(messageId: string | undefined, status: 'applied' | 'discarded') {
  if (!messageId) return;
  const response = await apiFetch(`${INLUMEN_API_URL}/api/chat/messages/${encodeURIComponent(messageId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ proposal_status: status }),
  });
  if (!response.ok) throw new Error('The proposal status could not be saved.');
}
