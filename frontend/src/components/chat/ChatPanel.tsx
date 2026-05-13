import React from 'react';
import {
  AlertTriangle,
  Download,
  Save,
  Send,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Textarea } from '@/components/ui/textarea';
import { ChatbotConfig, formatProviderLabel } from '@/services/chatbotService';
import { CanvasSyncStatus, ChatMessage } from '@/features/chat/chatTypes';
import { cn } from '@/lib/utils';

type ChatPanelProps = {
  activeConfig: ChatbotConfig;
  conversation: ChatMessage[];
  conversationEndRef: React.RefObject<HTMLDivElement>;
  canvasSyncStatus: CanvasSyncStatus;
  isProcessing: boolean;
  userInput: string;
  promptSuggestions: string[];
  formatConfigDescription: (config: ChatbotConfig) => string;
  onUserInputChange: (value: string) => void;
  onSendMessage: () => void;
  onClearConversation: () => void;
  onSaveConversation: () => void;
  onExportConversation: () => void;
  onSuggestionClick: (prompt: string) => void;
};

export const ChatPanel = ({
  activeConfig,
  conversation,
  conversationEndRef,
  canvasSyncStatus,
  isProcessing,
  userInput,
  promptSuggestions,
  formatConfigDescription,
  onUserInputChange,
  onSendMessage,
  onClearConversation,
  onSaveConversation,
  onExportConversation,
  onSuggestionClick,
}: ChatPanelProps) => {
  const conversationStatus = isProcessing
    ? "Thinking through your graph..."
    : conversation.length > 0
      ? `${conversation.length} message${conversation.length === 1 ? "" : "s"} in session`
      : "Ready to design";
  const hasConversation = conversation.length > 0 || isProcessing;
  const showSyncStatus =
    canvasSyncStatus.state === 'warning' || canvasSyncStatus.state === 'error';
  const syncStatusClass =
    canvasSyncStatus.state === 'error'
      ? 'border-rose-500/25 bg-rose-500/10 text-rose-600'
      : 'border-amber-500/25 bg-amber-500/10 text-amber-600';

  return (
    <div className="flex h-full flex-col overflow-hidden border-l border-border bg-card/95 text-card-foreground">
      <div className="border-b border-border px-3 py-3">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_14px_rgba(52,211,153,0.65)]" />
              <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-emerald-500">
                Pipeline Chat
              </p>
            </div>
            <p className="mt-1 text-sm font-medium text-foreground">
              {conversationStatus}
            </p>
            <p className="mt-1 truncate text-xs text-muted-foreground">
              Using {formatProviderLabel(activeConfig.provider)} / {activeConfig.model}
            </p>
            {showSyncStatus && (
              <div
                role="alert"
                className={cn(
                  "mt-2 flex items-start gap-2 rounded-md border px-2 py-1.5 text-[11px] leading-4",
                  syncStatusClass,
                )}
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span className="min-w-0 flex-1 break-words">
                  {canvasSyncStatus.message}
                </span>
              </div>
            )}
          </div>

        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onSaveConversation}
            disabled={conversation.length === 0}
            className="h-8 rounded-xl px-3 text-xs"
          >
            <Save className="h-3.5 w-3.5" />
            Save
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onExportConversation}
            disabled={conversation.length === 0}
            className="h-8 rounded-xl px-3 text-xs"
          >
            <Download className="h-3.5 w-3.5" />
            Export
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClearConversation}
            className="h-8 rounded-xl px-3 text-xs text-muted-foreground"
          >
            Clear
          </Button>
          <p className="min-w-[160px] flex-1 truncate text-[11px] text-muted-foreground">
            Enter sends. Shift+Enter adds a new line.
          </p>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col">
        <ScrollArea className="min-h-0 flex-1">
          {hasConversation ? (
            <div className="space-y-4 px-3 py-3">
              {conversation.map((msg, index) => (
                <div
                  key={index}
                  className={cn("flex", msg.role === 'user' ? "justify-end" : "justify-start")}
                >
                  <div className="max-w-[92%] space-y-1.5">
                    <div
                      className={cn(
                        "flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em]",
                        msg.role === 'user' ? "justify-end text-emerald-500" : "text-muted-foreground",
                      )}
                    >
                      <span
                        className={cn(
                          "h-2 w-2 rounded-full",
                          msg.role === 'user' ? "bg-emerald-400" : "bg-sky-400",
                        )}
                      />
                      {msg.role === 'user' ? "You" : "Pipeline Copilot"}
                    </div>
                    <div
                      className={cn(
                        "rounded-[18px] border px-3 py-2.5 text-sm leading-6 shadow-sm",
                        msg.role === 'user'
                          ? "border-emerald-400/25 bg-[linear-gradient(135deg,rgba(16,185,129,0.88),rgba(14,116,144,0.86))] text-white"
                          : "border-border bg-muted/55 text-foreground",
                      )}
                    >
                      <div className="whitespace-pre-wrap break-words">
                        {msg.content}
                      </div>
                    </div>
                  </div>
                </div>
              ))}

              {isProcessing && (
                <div className="flex justify-start">
                  <div className="max-w-[90%] space-y-1.5">
                    <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                      <span className="h-2 w-2 rounded-full bg-sky-400" />
                      Pipeline Copilot
                    </div>
                    <div className="rounded-[18px] border border-border bg-muted/55 px-3 py-2.5 text-sm text-muted-foreground shadow-sm">
                      <div className="flex items-center gap-3">
                        <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-emerald-400" />
                        Working through the next pipeline revision...
                      </div>
                    </div>
                  </div>
                </div>
              )}

              <div ref={conversationEndRef} />
            </div>
          ) : (
            <div className="flex h-full flex-col justify-center px-3 py-4">
              <p className="text-sm font-medium text-foreground">
                Describe the pipeline you want to build.
              </p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                Use the chat to add steps, refine the graph, or ask for deployment artifacts.
              </p>

              <div className="mt-4 space-y-2">
                {promptSuggestions.slice(0, 2).map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    onClick={() => onSuggestionClick(prompt)}
                    className="w-full rounded-xl border border-border bg-background/70 px-3 py-2.5 text-left text-xs leading-5 text-foreground transition-colors hover:border-emerald-400/35 hover:bg-muted"
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </div>
          )}
        </ScrollArea>

        <div className="border-t border-border p-3">
          <div className="rounded-[20px] border border-border bg-background/85 p-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
            <Textarea
              className="min-h-[72px] resize-none border-0 bg-transparent px-1 text-sm leading-6 text-foreground shadow-none placeholder:text-muted-foreground focus-visible:ring-0"
              placeholder="Describe the pipeline..."
              value={userInput}
              onChange={(e) => onUserInputChange(e.target.value)}
              rows={3}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  onSendMessage();
                }
              }}
            />

            <div className="mt-2 flex items-center justify-end border-t border-border pt-2">
              <Button
                onClick={onSendMessage}
                disabled={isProcessing || !userInput.trim()}
                className="h-9 rounded-xl bg-[linear-gradient(135deg,#34d399,#0f766e)] px-3.5 font-semibold text-slate-950 shadow-[0_18px_40px_rgba(16,185,129,0.22)] hover:opacity-95"
              >
                {isProcessing ? (
                  <>
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-950/30 border-t-slate-950" />
                    Thinking
                  </>
                ) : (
                  <>
                    <Send className="h-4 w-4" />
                    Send
                  </>
                )}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
