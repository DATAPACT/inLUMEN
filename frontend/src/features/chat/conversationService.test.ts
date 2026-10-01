import { describe, expect, it } from 'vitest';
import { mergeMessages, type StoredMessage } from './conversationService';

const message = (sequence: number, content = `Message ${sequence}`): StoredMessage => ({
  id: `message-${sequence}`, turn_id: 'turn', role: sequence % 2 ? 'user' : 'assistant',
  sequence, content, revision: sequence, status: 'completed', created_at: '2026-10-01',
});

describe('durable conversation reconciliation', () => {
  it('prepends older history and updates existing IDs without duplicates or reordering', () => {
    const current = mergeMessages([], [message(3), message(4)]);
    const older = mergeMessages(current, [message(1), message(2)]);
    const updated = mergeMessages(older, [{ ...message(2), proposal_status: 'applied', revision: 5 }]);
    expect(updated.map(m => m.id)).toEqual(['message-1', 'message-2', 'message-3', 'message-4']);
    expect(updated[1].graphProposalStatus).toBe('applied');
    expect(mergeMessages(updated, [])).toBe(updated);
  });
  it('server reset erases earlier history, including local optimistic messages', () => {
    const current = mergeMessages([], [message(1), message(2)]);
    expect(mergeMessages([...current, { role: 'user', content: 'draft' }], [], true)).toEqual([]);
  });
});
