import React, { useState, useCallback, useEffect, useRef } from 'react';
import { apiFetch } from '@/utils/apiFetch';
import { MINIO_API_URL, NEO4J_API_URL, LLM_API_URL } from '@/config/api';
import { Sidebar } from '@/components/Sidebar';
import { PropertiesPanel } from '@/components/PropertiesPanel';
import { Toolbar } from '@/components/Toolbar';
import { WrappedFlowCanvas, FlowCanvasRef } from '@/components/FlowCanvas';
import { toast } from 'sonner';
import {
  Send,
  PlusCircle,
  ChevronDown,
  Edit,
  Trash2,
  Settings,
} from 'lucide-react';
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Node } from 'reactflow';
import {
  ChatbotConfig,
  buildLLMRequestConfig,
  fetchChatbotConfigs,
  deleteChatbotConfig,
  formatProviderLabel,
  getDefaultChatbotConfig
} from '@/services/chatbotService';
import { ChatbotConfigForm } from '@/components/ChatbotConfigForm';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from '@/lib/utils';

const CHAT_SESSION_KEY = "chat-session-id";
const CHAT_PROMPT_SUGGESTIONS = [
  "Design a remote patient monitoring pipeline with ingestion, preprocessing, model training, and alerting.",
  "Create a document retrieval pipeline that ingests PDFs, chunks content, stores embeddings, and answers questions.",
  "Build a fraud detection workflow with batch feature engineering, real-time scoring, and monitoring.",
];

type FlowNodeData = {
  label?: string;
  description?: string;
  type?: string;
  files?: unknown[];
  [key: string]: unknown;
};

type FlowNode = Node<FlowNodeData>;

type DragNodeType = {
  type: string;
  data: FlowNodeData;
};

const Index = () => {
  const [selectedNode, setSelectedNode] = useState<FlowNode | null>(null);
  const [activeTab, setActiveTab] = useState('lab'); // 'lab', 'overview', or 'simulate'
  const [userInput, setUserInput] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [githubToken, setGithubToken] = useState('');
  const [flowNodes, setFlowNodes] = useState<FlowNode[]>([]);
  const [conversation, setConversation] = useState<{ role: 'user' | 'assistant', content: string }[]>([]);
  const [isLightMode, setIsLightMode] = useState(false);
  const flowCanvasRef = useRef<FlowCanvasRef>(null);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const [pipelineLastUpdate, setPipelineLastUpdate] = useState<string>('Never');
  const [pipelineCreatedAt, setPipelineCreatedAt] = useState<string>('Never');
  const [configs, setConfigs] = useState<ChatbotConfig[]>([]);
  const [selectedConfig, setSelectedConfig] = useState<ChatbotConfig | null>(null);
  const [isConfigFormOpen, setIsConfigFormOpen] = useState(false);
  const [configToEdit, setConfigToEdit] = useState<ChatbotConfig | undefined>(undefined);
  const defaultConfig = React.useMemo(() => getDefaultChatbotConfig(), []);

  // Backend session id
  const [chatSessionId, setChatSessionId] = useState<string>(() => {
    return localStorage.getItem(CHAT_SESSION_KEY) || "";
  });

  useEffect(() => {
    if (chatSessionId) {
      localStorage.setItem(CHAT_SESSION_KEY, chatSessionId);
    }
  }, [chatSessionId]);

  const formatConfigDescription = (config: ChatbotConfig) =>
    `${formatProviderLabel(config.provider)} / ${config.model}`;

  const pickPreferredConfig = useCallback(
    (configsList: ChatbotConfig[]) =>
      configsList.find((config) => config.provider === "openrouter") || configsList[0] || defaultConfig,
    [defaultConfig]
  );

  const loadConfigurations = useCallback(async () => {
    try {
      const configsList = await fetchChatbotConfigs();
      setConfigs(configsList);
      setSelectedConfig((currentSelection) => {
        if (currentSelection?.id) {
          return configsList.find((config) => config.id === currentSelection.id) || currentSelection;
        }
        return pickPreferredConfig(configsList);
      });
    } catch (error) {
      console.error("Error loading configurations:", error);
      setConfigs([]);
      setSelectedConfig((currentSelection) => currentSelection || defaultConfig);
    }
  }, [defaultConfig, pickPreferredConfig]);

  // Compute pipeline overview from flowNodes
  const pipelineOverview = React.useMemo(() => {
    const fileCount = flowNodes.reduce((count, node) => {
      const files = node.data?.files || [];
      return count + files.length;
    }, 0);

    return {
      version: '1.0.0',
      lastUpdate: pipelineLastUpdate,
      createdAt: pipelineCreatedAt,
      stepCount: flowNodes.length,
      fileCount
    };
  }, [flowNodes, pipelineLastUpdate, pipelineCreatedAt]);

  useEffect(() => {
    const savedToken = localStorage.getItem('github_token');
    if (savedToken) {
      setGithubToken(savedToken);
    }

    // Load saved pipeline timestamp (last update)
    const savedTimestamp = localStorage.getItem('saved-pipeline-timestamp');
    if (savedTimestamp) {
      setPipelineLastUpdate(new Date(savedTimestamp).toLocaleString());
    }

    // Load created-at (if you have it)
    const savedCreatedAt = localStorage.getItem('saved-pipeline-createdAt');
    if (savedCreatedAt) {
      setPipelineCreatedAt(new Date(savedCreatedAt).toLocaleString());
    }

    loadConfigurations();
  }, [loadConfigurations]);

  useEffect(() => {
    if (githubToken) {
      localStorage.setItem('github_token', githubToken);
    }
  }, [githubToken]);

  useEffect(() => {
    conversationEndRef.current?.scrollIntoView({
      behavior: conversation.length > 1 || isProcessing ? "smooth" : "auto",
      block: "end",
    });
  }, [conversation, isProcessing]);

  const onNodeSelect = useCallback((node: FlowNode | null) => {
    setSelectedNode(node);
  }, []);

  const onNodeUpdate = useCallback((id: string, data: FlowNodeData) => {
    setFlowNodes(prev => prev.map(node =>
      node.id === id ? { ...node, data: { ...node.data, ...data } } : node
    ));
    flowCanvasRef.current?.updateNode(id, data);
  }, []);

  const onNodesChange = useCallback((nodes: Node[]) => {
    setFlowNodes(nodes);
  }, []);

  const onDragStart = (event: React.DragEvent, nodeType: DragNodeType) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify(nodeType));
    event.dataTransfer.effectAllowed = 'move';
  };

  const handleRunFlow = () => {
    toast.success("Running AI Flow", {
      description: "Executing your custom thinking model",
    });
    setActiveTab('simulate');
  };

  const handleTabChange = (value: string) => {
    setActiveTab(value);
  };

  const handleSendMessage = async () => {
    if (!userInput.trim()) {
      toast.error("Please enter a message", {
        description: "Your input is empty",
      });
      return;
    }

    setIsProcessing(true);
    toast("Processing your input", {
      description: "AI is thinking...",
    });

    const newUserMessage = { role: 'user' as const, content: userInput };
    const updatedConversation = [...conversation, newUserMessage];
    setConversation(updatedConversation);

    try {
      const activeCfg = selectedConfig || defaultConfig;

      const res = await apiFetch(`${LLM_API_URL}/simple_chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: chatSessionId || null,
          user_message: userInput,
          model: activeCfg.model,
          llm_config: buildLLMRequestConfig(activeCfg),
        }),
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`Chat failed (${res.status}): ${errText}`);
      }

      const data = await res.json();

      if (data.session_id && data.session_id !== chatSessionId) {
        setChatSessionId(data.session_id);
      }

      const responseText = data.assistant_message ?? "";
      setConversation(prev => [...prev, { role: 'assistant', content: responseText }]);
      setUserInput('');
    } catch (error) {
      console.error("Error processing request:", error);
      toast.error("An error occurred while processing your request", {
        description: error instanceof Error ? error.message : "Unknown error occurred",
      });
    } finally {
      setIsProcessing(false);
    }
  };

  const handleClearConversation = async () => {
    setConversation([]);
    toast.success("Conversation cleared", {
      description: "Your conversation history has been reset",
    });

    if (chatSessionId) {
      try {
        await apiFetch(`${LLM_API_URL}/simple_chat/reset`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: chatSessionId }),
        });
      } catch (e) {
        console.warn("Failed to reset backend chat session:", e);
      }
    }

    setChatSessionId("");
    localStorage.removeItem(CHAT_SESSION_KEY);
  };

  const handleSaveWorkflow = () => {
    localStorage.setItem('ai-workflow-nodes', JSON.stringify(flowNodes));
    toast.success("Workflow saved", {
      description: "Your AI workflow has been saved",
    });
  };

  const handleCreateConfig = () => {
    setConfigToEdit(undefined);
    setIsConfigFormOpen(true);
  };

  const handleEditConfig = (config: ChatbotConfig) => {
    setConfigToEdit(config);
    setIsConfigFormOpen(true);
  };

  const handleDeleteConfig = async (id: string) => {
    if (window.confirm("Are you sure you want to delete this configuration?")) {
      try {
        const success = await deleteChatbotConfig(id);
        if (success) {
          const updatedConfigs = await fetchChatbotConfigs();
          setConfigs(updatedConfigs);

          if (selectedConfig?.id === id) {
            setSelectedConfig(pickPreferredConfig(updatedConfigs));
          }

          toast.success("Configuration deleted successfully");
        }
      } catch (error) {
        console.error("Error deleting configuration:", error);
        toast.error("Failed to delete configuration");
      }
    }
  };

  const handleConfigSaved = (config: ChatbotConfig) => {
    loadConfigurations();
    setSelectedConfig(config);
    toast.success("Configuration saved successfully");
  };

  const handleSelectConfig = (config: ChatbotConfig) => {
    setSelectedConfig(config);
    toast.info(`Activated: ${config.name}`, {
      description: `Using ${formatConfigDescription(config)}`
    });
  };

  const handleSuggestionClick = (prompt: string) => {
    setUserInput(prompt);
  };

  const handleBlankPipeline = () => {
    setFlowNodes([]);
    setConversation([]);
    setSelectedNode(null);
    localStorage.removeItem('ai-flow-nodes');
    localStorage.removeItem('ai-flow-edges');
    toast.success("Blank pipeline created");
  };

  const handleSavePipeline = () => {
    const timestamp = new Date().toISOString();
    const existingCreatedAt = localStorage.getItem('saved-pipeline-createdAt');
    if (!existingCreatedAt) {
      localStorage.setItem('saved-pipeline-createdAt', timestamp);
      setPipelineCreatedAt(new Date(timestamp).toLocaleString());
    } else {
      setPipelineCreatedAt(new Date(existingCreatedAt).toLocaleString());
    }
    localStorage.setItem('saved-pipeline-nodes', JSON.stringify(flowNodes));
    localStorage.setItem('saved-pipeline-timestamp', timestamp);
    setPipelineLastUpdate(new Date(timestamp).toLocaleString());
    toast.success("Pipeline saved", {
      description: "Your pipeline will persist on next visit"
    });
  };

  const handleRemoveNode = (nodeId: string) => {
    setFlowNodes(prev => prev.filter(node => node.id !== nodeId));
    if (selectedNode?.id === nodeId) {
      setSelectedNode(null);
    }
    toast.success("Node removed");
  };

  const handleRemoveEdge = (edgeId: string) => {
    toast.success("Connection removed");
  };

  const showFlowLayout = activeTab === 'lab' || activeTab === 'overview' || activeTab === 'simulate';

  // label for main configuration button
  const activeConfig = selectedConfig || defaultConfig;
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
    <div className="h-screen w-screen flex flex-col overflow-hidden animate-fade-in bg-[#1A1A1D]">
      <Toolbar
        onRunFlow={handleRunFlow}
        isLightMode={isLightMode}
        onToggleLightMode={() => setIsLightMode(!isLightMode)}
      />

      <div className="flex-1 flex overflow-hidden">
        <Sidebar
          onDragStart={onDragStart}
          activeTab={activeTab}
          onTabChange={handleTabChange}
          githubToken={githubToken}
          setGithubToken={setGithubToken}
          onBlankPipeline={handleBlankPipeline}
          onSavePipeline={handleSavePipeline}
          pipelineOverview={pipelineOverview}
          activeChatbotConfig={activeConfig}
        />

        {showFlowLayout ? (
          <ResizablePanelGroup direction="horizontal" className="flex-1">
            <ResizablePanel defaultSize={60} minSize={38}>
              <div className={`h-full ${isLightMode ? 'bg-gray-50' : 'bg-canvas-DEFAULT'}`}>
                <WrappedFlowCanvas
                  onNodeSelect={onNodeSelect}
                  onNodesChange={onNodesChange}
                  onRemoveNode={handleRemoveNode}
                  onRemoveEdge={handleRemoveEdge}
                  isLightMode={isLightMode}
                  activeChatbotConfig={activeConfig}
                  flowCanvasRef={flowCanvasRef}
                />
              </div>
            </ResizablePanel>

            <ResizableHandle withHandle />

            <ResizablePanel defaultSize={40} minSize={26}>
              <ResizablePanelGroup direction="horizontal">
                <ResizablePanel defaultSize={64} minSize={30}>
                  <PropertiesPanel
                    selectedNode={selectedNode}
                    onNodeUpdate={onNodeUpdate}
                    onRemoveNode={handleRemoveNode}
                  />
                </ResizablePanel>

                <ResizableHandle withHandle />

                <ResizablePanel defaultSize={36} minSize={18} maxSize={44}>
                  <div className="flex h-full flex-col overflow-hidden border-l border-white/10 bg-[linear-gradient(180deg,#081018_0%,#0b1118_100%)] text-slate-100">
                    <div className="border-b border-white/10 px-3 py-3">
                      <div className="flex items-start gap-2">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="h-2 w-2 rounded-full bg-emerald-300 shadow-[0_0_14px_rgba(110,231,183,0.85)]" />
                            <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-emerald-100/80">
                              Pipeline Chat
                            </p>
                          </div>
                          <p className="mt-1 text-sm font-medium text-white">
                            {conversationStatus}
                          </p>
                          <p className="mt-1 truncate text-xs text-slate-400">
                            Using {formatProviderLabel(activeConfig.provider)} / {activeConfig.model}
                          </p>
                        </div>

                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-9 max-w-full gap-2 rounded-xl border-white/10 bg-slate-900/80 px-3 text-slate-100 hover:bg-slate-900 hover:text-white"
                            >
                              <Settings className="h-4 w-4 text-emerald-200" />
                              <span className="max-w-[140px] truncate text-left text-xs font-medium">
                                {compactConfigLabel}
                              </span>
                              <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-60" />
                            </Button>
                          </DropdownMenuTrigger>

                          <DropdownMenuContent
                            align="end"
                            className="w-[320px] rounded-2xl border-white/10 bg-slate-950/95 p-2 text-slate-100 shadow-[0_24px_60px_rgba(2,6,23,0.55)] backdrop-blur-xl"
                          >
                            <DropdownMenuLabel className="px-3 pt-2 text-xs uppercase tracking-[0.22em] text-slate-400">
                              Chatbot Configurations
                            </DropdownMenuLabel>
                            <DropdownMenuSeparator className="bg-white/10" />

                            {configs.length > 0 ? (
                              configs.map((config) => (
                                <DropdownMenuItem
                                  key={config.id}
                                  className="flex cursor-pointer items-start justify-between gap-2 rounded-xl px-3 py-3 focus:bg-emerald-500/10 focus:text-white data-[highlighted]:bg-emerald-500/10"
                                  onClick={() => handleSelectConfig(config)}
                                >
                                  <div className="min-w-0 flex-1">
                                    <div className="truncate text-sm font-medium text-slate-100">
                                      {config.name}
                                    </div>
                                    <div
                                      className={cn(
                                        "truncate text-xs text-slate-400",
                                        selectedConfig?.id === config.id && "text-emerald-200"
                                      )}
                                    >
                                      {formatConfigDescription(config)}
                                    </div>
                                  </div>
                                  <div className="flex items-center gap-1">
                                    <Button
                                      variant="ghost"
                                      size="icon"
                                      className="h-7 w-7 rounded-full text-slate-300 hover:bg-white/10 hover:text-white"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        handleEditConfig(config);
                                      }}
                                    >
                                      <Edit className="h-3.5 w-3.5" />
                                    </Button>
                                    <Button
                                      variant="ghost"
                                      size="icon"
                                      className="h-7 w-7 rounded-full text-rose-300 hover:bg-rose-500/10 hover:text-rose-200"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        if (config.id) handleDeleteConfig(config.id);
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
                                className="rounded-xl px-3 py-3 text-xs text-slate-400 opacity-100"
                              >
                                No saved browser configurations yet.
                              </DropdownMenuItem>
                            )}

                            <DropdownMenuSeparator className="bg-white/10" />
                            <DropdownMenuItem
                              className="flex cursor-pointer items-center gap-2 rounded-xl px-3 py-3 text-emerald-100 focus:bg-emerald-500/10 focus:text-white data-[highlighted]:bg-emerald-500/10"
                              onClick={handleCreateConfig}
                            >
                              <PlusCircle className="h-4 w-4" />
                              <span>New Configuration</span>
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>

                      <div className="mt-3 flex items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={handleClearConversation}
                          className="h-8 rounded-xl border-white/10 bg-slate-900/60 px-3 text-xs text-slate-200 hover:bg-slate-900 hover:text-white"
                        >
                          Clear
                        </Button>
                        <p className="truncate text-[11px] text-slate-500">
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
                                        msg.role === 'user' ? "justify-end text-emerald-100/80" : "text-slate-400"
                                      )}
                                    >
                                      <span
                                        className={cn(
                                          "h-2 w-2 rounded-full",
                                          msg.role === 'user' ? "bg-emerald-300" : "bg-sky-300"
                                        )}
                                      />
                                      {msg.role === 'user' ? "You" : "Pipeline Copilot"}
                                    </div>
                                    <div
                                      className={cn(
                                        "rounded-[18px] border px-3 py-2.5 text-sm leading-6 shadow-lg",
                                        msg.role === 'user'
                                          ? "border-emerald-400/25 bg-[linear-gradient(135deg,rgba(16,185,129,0.28),rgba(14,116,144,0.3))] text-white shadow-emerald-950/30"
                                          : "border-white/10 bg-slate-900/80 text-slate-100 shadow-slate-950/40"
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
                                    <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-400">
                                      <span className="h-2 w-2 rounded-full bg-sky-300" />
                                      Pipeline Copilot
                                    </div>
                                    <div className="rounded-[18px] border border-white/10 bg-slate-900/80 px-3 py-2.5 text-sm text-slate-300 shadow-lg shadow-slate-950/40">
                                      <div className="flex items-center gap-3">
                                        <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-emerald-300" />
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
                            <p className="text-sm font-medium text-white">
                              Describe the pipeline you want to build.
                            </p>
                            <p className="mt-1 text-xs leading-5 text-slate-400">
                              Use the chat to add steps, refine the graph, or ask for deployment artifacts.
                            </p>

                            <div className="mt-4 space-y-2">
                              {CHAT_PROMPT_SUGGESTIONS.slice(0, 2).map((prompt) => (
                                <button
                                  key={prompt}
                                  type="button"
                                  onClick={() => handleSuggestionClick(prompt)}
                                  className="w-full rounded-xl border border-white/10 bg-slate-900/70 px-3 py-2.5 text-left text-xs leading-5 text-slate-200 transition-colors hover:border-emerald-400/25 hover:bg-slate-900"
                                >
                                  {prompt}
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                      </ScrollArea>

                      <div className="border-t border-white/10 p-3">
                        <div className="rounded-[20px] border border-white/10 bg-slate-950/70 p-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
                            <Textarea
                              className="min-h-[72px] resize-none border-0 bg-transparent px-1 text-sm leading-6 text-slate-100 shadow-none placeholder:text-slate-500 focus-visible:ring-0"
                              placeholder="Describe the pipeline..."
                              value={userInput}
                              onChange={(e) => setUserInput(e.target.value)}
                              rows={3}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && !e.shiftKey) {
                                  e.preventDefault();
                                  handleSendMessage();
                                }
                              }}
                            />

                            <div className="mt-2 flex items-center justify-end border-t border-white/10 pt-2">
                              <Button
                                onClick={handleSendMessage}
                                disabled={isProcessing || !userInput.trim()}
                                className="h-9 rounded-xl bg-[linear-gradient(135deg,#34d399,#0f766e)] px-3.5 font-semibold text-slate-950 shadow-[0_18px_40px_rgba(16,185,129,0.3)] hover:opacity-95"
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
                </ResizablePanel>
              </ResizablePanelGroup>
            </ResizablePanel>
          </ResizablePanelGroup>
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground">
            Select a tab.
          </div>
        )}
      </div>

      <ChatbotConfigForm
        isOpen={isConfigFormOpen}
        onClose={() => setIsConfigFormOpen(false)}
        initialConfig={configToEdit}
        onConfigSaved={handleConfigSaved}
      />
    </div>
  );
};

export default Index;
