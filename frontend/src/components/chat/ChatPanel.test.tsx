import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ChatPanel } from './ChatPanel';
import { CHAT_PROMPT_SUGGESTIONS } from '@/features/chat/promptSuggestions';

const renderPanel = (
  isProcessing: boolean,
  isStopping = false,
  promptSuggestions: string[] = [],
) =>
  renderToStaticMarkup(
    <ChatPanel
      activeConfig={{
        name: 'Test',
        provider: 'openrouter',
        model: 'test/model',
        baseUrl: 'https://example.test/v1',
      }}
      conversation={[]}
      conversationEndRef={React.createRef<HTMLDivElement>()}
      canvasSyncStatus={{ state: 'idle', message: 'Ready' }}
      isProcessing={isProcessing}
      isStopping={isStopping}
      userInput="Build a pipeline"
      promptSuggestions={promptSuggestions}
      formatConfigDescription={() => 'Test'}
      onUserInputChange={vi.fn()}
      onSendMessage={vi.fn()}
      onStopProcessing={vi.fn()}
      onClearConversation={vi.fn()}
      onSaveConversation={vi.fn()}
      onExportConversation={vi.fn()}
      onSuggestionClick={vi.fn()}
    />,
  );

describe('ChatPanel cancellation controls', () => {
  it('shows the audio pipeline among the three default design examples', () => {
    const html = renderPanel(false, false, CHAT_PROMPT_SUGGESTIONS);

    expect(html).toContain('audio transcription and sentiment analysis');
    expect(html).toContain('remote patient monitoring');
    expect(html).toContain('document retrieval pipeline');
    expect(html).not.toContain('fraud detection workflow');
  });

  it('replaces Send with an enabled Stop button while the agent is running', () => {
    const html = renderPanel(true);

    expect(html).toContain('>Stop</button>');
    expect(html).not.toContain('>Send</button>');
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*>[^<]*Stop/);
  });

  it('shows a disabled stopping state after cancellation is requested', () => {
    const html = renderPanel(true, true);

    expect(html).toContain('Stopping…');
    expect(html).toMatch(/<button[^>]*disabled=""/);
  });
});
