import React from 'react';
import {
  ChevronDown,
  Download,
  Edit,
  PlusCircle,
  Save,
  Send,
  Settings,
  Trash2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Textarea } from '@/components/ui/textarea';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ChatbotConfig, formatProviderLabel } from '@/services/chatbotService';
import { ChatMessage } from '@/features/chat/chatTypes';
import { cn } from '@/lib/utils';

type ChatPanelProps = {
  activeConfig: ChatbotConfig;
  configs: ChatbotConfig[];
  selectedConfig: ChatbotConfig | null;
  conversation: ChatMessage[];
  conversationEndRef: React.RefObject<HTMLDivElement>;
  isProcessing: boolean;
  userInput: string;
  promptSuggestions: string[];
  formatConfigDescription: (config: ChatbotConfig) => string;
  onUserInputChange: (value: string) => void;
  onSendMessage: () => void;
  onClearConversation: () => void;
  onSaveConversation: () => void;
  onExportConversation: () => void;
  onSelectConfig: (config: ChatbotConfig) => void;
  onCreateConfig: () => void;
  onEditConfig: (config: ChatbotConfig) => void;
  onDeleteConfig: (id: string) => void;
  onSuggestionClick: (prompt: string) => void;
};

export const ChatPanel = ({
  activeConfig,
  configs,
  selectedConfig,
  conversation,
  conversationEndRef,
  isProcessing,
  userInput,
  promptSuggestions,
  formatConfigDescription,
  onUserInputChange,
  onSendMessage,
  onClearConversation,
  onSaveConversation,
  onExportConversation,
  onSelectConfig,
  onCreateConfig,
  onEditConfig,
  onDeleteConfig,
  onSuggestionClick,
}: ChatPanelProps) => {
  const compactConfigLabel =
    activeConfig.name === formatProviderLabel(activeConfig.provider)
      ? `${formatProviderLabel(activeConfig.provider)} / ${activeConfig.model}`
      : `${activeConfig.name}`;
  const conversationStatus = isProcessing
    ? "Thinking through your graph..."
    : conversation.length > 0
      ? `${conversation.length} message${conversation.length === 1 ? "" : "s"} in session`
      : "Ready to design";
  const hasConversation = conversation.length > 0 || isProcessing;

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
          </div>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-9 max-w-full gap-2 rounded-xl bg-background/80 px-3 text-foreground hover:bg-muted"
              >
                <Settings className="h-4 w-4 text-emerald-500" />
                <span className="max-w-[140px] truncate text-left text-xs font-medium">
                  {compactConfigLabel}
                </span>
                <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-60" />
              </Button>
            </DropdownMenuTrigger>

            <DropdownMenuContent
              align="end"
              className="w-[320px] rounded-2xl border-border bg-popover p-2 text-popover-foreground shadow-[0_24px_60px_rgba(2,6,23,0.22)] backdrop-blur-xl"
            >
              <DropdownMenuLabel className="px-3 pt-2 text-xs uppercase tracking-[0.22em] text-muted-foreground">
                Chatbot Configurations
              </DropdownMenuLabel>
              <DropdownMenuSeparator />

              {configs.length > 0 ? (
                configs.map((config) => (
                  <DropdownMenuItem
                    key={config.id}
                    className="flex cursor-pointer items-start justify-between gap-2 rounded-xl px-3 py-3 focus:bg-emerald-500/10 data-[highlighted]:bg-emerald-500/10"
                    onClick={() => onSelectConfig(config)}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">
                        {config.name}
                      </div>
                      <div
                        className={cn(
                          "truncate text-xs text-muted-foreground",
                          selectedConfig?.id === config.id && "text-emerald-500",
                        )}
                      >
                        {formatConfigDescription(config)}
                      </div>
                    </div>
                    <div className="flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                        onClick={(e) => {
                          e.stopPropagation();
                          onEditConfig(config);
                        }}
                      >
                        <Edit className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 rounded-full text-rose-500 hover:bg-rose-500/10"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (config.id) onDeleteConfig(config.id);
                        }}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </DropdownMenuItem>
                ))
              ) : (
                <DropdownMenuItem
                  disabled
                  className="rounded-xl px-3 py-3 text-xs text-muted-foreground opacity-100"
                >
                  No saved browser configurations yet.
                </DropdownMenuItem>
              )}

              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="flex cursor-pointer items-center gap-2 rounded-xl px-3 py-3 text-emerald-600 focus:bg-emerald-500/10 data-[highlighted]:bg-emerald-500/10"
                onClick={onCreateConfig}
              >
                <PlusCircle className="h-4 w-4" />
                <span>New Configuration</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
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
