import React, { useState, useCallback, useEffect, useRef } from 'react';
import { apiFetch } from '@/utils/apiFetch';
import { LLM_API_URL } from '@/config/api';
import { Sidebar } from '@/components/Sidebar';
import { PropertiesPanel, PropertyNodeData } from '@/components/PropertiesPanel';
import { Toolbar } from '@/components/Toolbar';
import { WrappedFlowCanvas, FlowCanvasRef } from '@/components/FlowCanvas';
import { ChatPanel } from '@/components/chat/ChatPanel';
import { CanvasSyncStatus, ChatMessage } from '@/features/chat/chatTypes';
import { toast } from 'sonner';
import {
  Settings,
  PanelLeft,
  SlidersHorizontal,
  MessageSquare,
  RotateCcw,
  Sun,
  Moon,
  Keyboard,
  HelpCircle,
} from 'lucide-react';
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
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

const CHAT_SESSION_KEY = "chat-session-id";
const CHAT_TRANSCRIPT_KEY = "inlumen-chat-transcript";
const PANEL_STATE_KEY = "inlumen-panel-preferences";
const THEME_KEY = "inlumen-theme";
const CHAT_PROMPT_SUGGESTIONS = [
  "Design a remote patient monitoring pipeline with ingestion, preprocessing, model training, and alerting.",
  "Create a document retrieval pipeline that ingests PDFs, chunks content, stores embeddings, and answers questions.",
  "Build a fraud detection workflow with batch feature engineering, real-time scoring, and monitoring.",
];

type RightPanel = 'inspector' | 'chat' | null;

type PanelPreferences = {
  libraryOpen: boolean;
  rightPanel: RightPanel;
};

const DEFAULT_PANEL_PREFERENCES: PanelPreferences = {
  libraryOpen: false,
  rightPanel: null,
};

const readPanelPreferences = (): PanelPreferences => {
  try {
    const saved = localStorage.getItem(PANEL_STATE_KEY);
    if (!saved) return DEFAULT_PANEL_PREFERENCES;
    const parsed = JSON.parse(saved) as Partial<PanelPreferences>;
    const rightPanel =
      parsed.rightPanel === 'inspector' || parsed.rightPanel === 'chat'
        ? parsed.rightPanel
        : null;
    return {
      libraryOpen: typeof parsed.libraryOpen === 'boolean'
        ? parsed.libraryOpen
        : DEFAULT_PANEL_PREFERENCES.libraryOpen,
      rightPanel,
    };
  } catch {
    return DEFAULT_PANEL_PREFERENCES;
  }
};

const readSavedTheme = () => {
  try {
    return localStorage.getItem(THEME_KEY) === "light";
  } catch {
    return false;
  }
};

const createDownloadTimestamp = () =>
  new Date().toISOString().replace(/[:.]/g, "-");

type FlowNodeData = PropertyNodeData;

type FlowNode = Node<FlowNodeData>;

type DragNodeType = {
  type: string;
  data: FlowNodeData;
};

type ChatApiResponse = {
  session_id?: string;
  assistant_message?: string;
  graph?: unknown;
  sync?: {
    status?: string;
    guardrail_passed?: boolean;
    expected_graph_change?: boolean;
    graph_changed?: boolean;
    message?: string;
    node_count?: number;
    edge_count?: number;
    updated_at?: string | null;
  };
};

const Index = () => {
  const [selectedNode, setSelectedNode] = useState<FlowNode | null>(null);
  const [activeTab, setActiveTab] = useState('lab'); // 'lab', 'overview', or 'simulate'
  const [userInput, setUserInput] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [githubToken, setGithubToken] = useState('');
  const [flowNodes, setFlowNodes] = useState<FlowNode[]>([]);
  const [conversation, setConversation] = useState<ChatMessage[]>([]);
  const [canvasSyncStatus, setCanvasSyncStatus] = useState<CanvasSyncStatus>({
    state: 'idle',
    message: 'Canvas is ready',
  });
  const [isLightMode, setIsLightMode] = useState(readSavedTheme);
  const [panelPreferences, setPanelPreferences] = useState<PanelPreferences>(readPanelPreferences);
  const [isHelpOpen, setIsHelpOpen] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const flowCanvasRef = useRef<FlowCanvasRef>(null);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const [pipelineLastUpdate, setPipelineLastUpdate] = useState<string>('Never');
  const [pipelineCreatedAt, setPipelineCreatedAt] = useState<string>('Never');
  const [configs, setConfigs] = useState<ChatbotConfig[]>([]);
  const [selectedConfig, setSelectedConfig] = useState<ChatbotConfig | null>(null);
  const [isConfigFormOpen, setIsConfigFormOpen] = useState(false);
  const [configToEdit, setConfigToEdit] = useState<ChatbotConfig | undefined>(undefined);
  const defaultConfig = React.useMemo(() => getDefaultChatbotConfig(), []);
  const isLibraryOpen = panelPreferences.libraryOpen;
  const rightPanel = panelPreferences.rightPanel;

  // Backend session id
  const [chatSessionId, setChatSessionId] = useState<string>(() => {
    return localStorage.getItem(CHAT_SESSION_KEY) || "";
  });

  useEffect(() => {
    if (chatSessionId) {
      localStorage.setItem(CHAT_SESSION_KEY, chatSessionId);
    }
  }, [chatSessionId]);

  useEffect(() => {
    document.documentElement.classList.toggle("light", isLightMode);
    localStorage.setItem(THEME_KEY, isLightMode ? "light" : "dark");
  }, [isLightMode]);

  useEffect(() => {
    localStorage.setItem(PANEL_STATE_KEY, JSON.stringify(panelPreferences));
  }, [panelPreferences]);

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
    if (node) {
      setPanelPreferences((current) => ({ ...current, rightPanel: 'inspector' }));
    }
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

  const handleTabChange = (value: string) => {
    setActiveTab(value);
  };

  const activeConfig = selectedConfig || defaultConfig;

  const handleSendMessage = async () => {
    const messageText = userInput;

    if (!messageText.trim()) {
      toast.error("Please enter a message", {
        description: "Your input is empty",
      });
      return;
    }

    setUserInput('');
    setIsProcessing(true);
    toast("Processing your input", {
      description: "AI is thinking...",
    });

    const newUserMessage = { role: 'user' as const, content: messageText };
    const updatedConversation = [...conversation, newUserMessage];
    setConversation(updatedConversation);

    try {
      const activeCfg = selectedConfig || defaultConfig;

      const res = await apiFetch(`${LLM_API_URL}/simple_chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: chatSessionId || null,
          user_message: messageText,
          model: activeCfg.model,
          llm_config: buildLLMRequestConfig(activeCfg),
        }),
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`Chat failed (${res.status}): ${errText}`);
      }

      const data = await res.json() as ChatApiResponse;

      if (data.session_id && data.session_id !== chatSessionId) {
        setChatSessionId(data.session_id);
      }

      const responseText = data.assistant_message ?? "";
      setConversation(prev => [...prev, { role: 'assistant', content: responseText }]);

      setCanvasSyncStatus({
        state: 'syncing',
        message: 'Applying agent graph changes to the canvas...',
      });

      if (!flowCanvasRef.current) {
        setCanvasSyncStatus({
          state: 'warning',
          message: 'Chat completed, but the canvas was not mounted for graph sync.',
        });
        return;
      }

      const syncedGraph = await flowCanvasRef.current.syncFromBackend(data.graph);
      const sync = data.sync;
      const nodeCount = sync?.node_count ?? syncedGraph.nodes.length;
      const edgeCount = sync?.edge_count ?? syncedGraph.edges.length;
      const updatedAt = sync?.updated_at ?? syncedGraph.updated_at;
      const guardrailPassed = sync?.guardrail_passed !== false;
      const syncMessage = sync?.message
        || `Canvas sync needs attention: ${nodeCount} node${nodeCount === 1 ? '' : 's'} and ${edgeCount} edge${edgeCount === 1 ? '' : 's'}.`;

      setCanvasSyncStatus({
        state: guardrailPassed ? 'idle' : 'warning',
        message: guardrailPassed ? '' : syncMessage,
        updatedAt,
      });

      if (!guardrailPassed) {
        toast.warning("Canvas sync guardrail", {
          description: syncMessage,
        });
      }
    } catch (error) {
      console.error("Error processing request:", error);
      setCanvasSyncStatus({
        state: 'error',
        message: error instanceof Error ? error.message : 'Chat or canvas sync failed.',
      });
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
    setCanvasSyncStatus({
      state: 'idle',
      message: 'Canvas is ready',
    });
  };

  const handleToggleLibrary = () => {
    setPanelPreferences((current) => ({
      ...current,
      libraryOpen: !current.libraryOpen,
    }));
  };

  const handleToggleRightPanel = (panel: Exclude<RightPanel, null>) => {
    setPanelPreferences((current) => ({
      ...current,
      rightPanel: current.rightPanel === panel ? null : panel,
    }));
  };

  const handleResetPanelLayout = () => {
    setPanelPreferences(DEFAULT_PANEL_PREFERENCES);
    toast.success("Layout reset", {
      description: "The canvas is focused and panels are back to their default state.",
    });
  };

  const buildConversationMarkdown = () => {
    const lines = [
      "# inLUMEN chat export",
      "",
      `Exported: ${new Date().toLocaleString()}`,
      `Model: ${formatConfigDescription(activeConfig)}`,
      "",
      ...conversation.flatMap((message) => [
        `## ${message.role === "user" ? "You" : "Pipeline Copilot"}`,
        "",
        message.content,
        "",
      ]),
    ];

    if (isProcessing) {
      lines.push("## Pipeline Copilot", "", "_Response in progress at export time._", "");
    }

    return lines.join("\n");
  };

  const handleSaveConversation = () => {
    if (conversation.length === 0) {
      toast.error("Nothing to save", {
        description: "Start a chat before saving the transcript.",
      });
      return;
    }

    localStorage.setItem(
      CHAT_TRANSCRIPT_KEY,
      JSON.stringify({
        savedAt: new Date().toISOString(),
        sessionId: chatSessionId || null,
        model: formatConfigDescription(activeConfig),
        conversation,
      })
    );

    toast.success("Chat saved", {
      description: "Transcript saved locally in this browser.",
    });
  };

  const handleExportConversation = () => {
    if (conversation.length === 0) {
      toast.error("Nothing to export", {
        description: "Start a chat before exporting the transcript.",
      });
      return;
    }

    const blob = new Blob([buildConversationMarkdown()], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `inlumen-chat-${createDownloadTimestamp()}.md`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);

    toast.success("Chat exported", {
      description: "Transcript downloaded as Markdown.",
    });
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

  return (
    <div className="h-screen w-screen flex flex-col overflow-hidden animate-fade-in bg-background text-foreground transition-colors">
      <Toolbar
        isLightMode={isLightMode}
        onToggleLightMode={() => setIsLightMode(!isLightMode)}
        isLibraryOpen={isLibraryOpen}
        isInspectorOpen={rightPanel === 'inspector'}
        isChatOpen={rightPanel === 'chat'}
        onToggleLibrary={handleToggleLibrary}
        onToggleInspector={() => handleToggleRightPanel('inspector')}
        onToggleChat={() => handleToggleRightPanel('chat')}
        onOpenHelp={() => setIsHelpOpen(true)}
        onOpenSettings={() => setIsSettingsOpen(true)}
      />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        {isLibraryOpen && (
          <Sidebar
            className="w-[17rem] shrink-0 bg-card/95"
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
        )}

        {showFlowLayout ? (
          <ResizablePanelGroup direction="horizontal" className="min-w-0 flex-1">
            <ResizablePanel defaultSize={rightPanel ? 72 : 100} minSize={45}>
              <div className="h-full bg-background">
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

            {rightPanel && (
              <>
                <ResizableHandle withHandle />
                <ResizablePanel defaultSize={28} minSize={24} maxSize={42} className="min-w-[320px]">
                  {rightPanel === 'inspector' ? (
                    <PropertiesPanel
                      className="bg-card/95"
                      selectedNode={selectedNode}
                      onNodeUpdate={onNodeUpdate}
                      onRemoveNode={handleRemoveNode}
                    />
                  ) : (
                    <ChatPanel
                      activeConfig={activeConfig}
                      configs={configs}
                      selectedConfig={selectedConfig}
                      conversation={conversation}
                      conversationEndRef={conversationEndRef}
                      canvasSyncStatus={canvasSyncStatus}
                      isProcessing={isProcessing}
                      userInput={userInput}
                      promptSuggestions={CHAT_PROMPT_SUGGESTIONS}
                      formatConfigDescription={formatConfigDescription}
                      onUserInputChange={setUserInput}
                      onSendMessage={handleSendMessage}
                      onClearConversation={handleClearConversation}
                      onSaveConversation={handleSaveConversation}
                      onExportConversation={handleExportConversation}
                      onSelectConfig={handleSelectConfig}
                      onCreateConfig={handleCreateConfig}
                      onEditConfig={handleEditConfig}
                      onDeleteConfig={handleDeleteConfig}
                      onSuggestionClick={handleSuggestionClick}
                    />
                  )}
                </ResizablePanel>
              </>
            )}
          </ResizablePanelGroup>
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground">
            Select a tab.
          </div>
        )}
      </div>

      <Dialog open={isHelpOpen} onOpenChange={setIsHelpOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <HelpCircle className="h-5 w-5 text-emerald-500" />
              inLUMEN Help
            </DialogTitle>
            <DialogDescription>
              A compact guide for working efficiently on laptop-sized screens.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3 text-sm">
            <div className="rounded-xl border border-border bg-muted/35 p-3">
              <div className="mb-1 flex items-center gap-2 font-medium">
                <PanelLeft className="h-4 w-4 text-emerald-500" />
                Panels
              </div>
              Use the header toggles to show only what you need: Library for dragging nodes, Inspector for editing selected nodes, and Chat for pipeline assistance.
            </div>
            <div className="rounded-xl border border-border bg-muted/35 p-3">
              <div className="mb-1 flex items-center gap-2 font-medium">
                <Keyboard className="h-4 w-4 text-emerald-500" />
                Chat shortcuts
              </div>
              Press Enter to send a message. Press Shift+Enter to add a new line. Save stores the transcript locally; Export downloads it as Markdown.
            </div>
            <div className="rounded-xl border border-border bg-muted/35 p-3">
              <div className="mb-1 flex items-center gap-2 font-medium">
                <SlidersHorizontal className="h-4 w-4 text-emerald-500" />
                Canvas workflow
              </div>
              Select a node to open the Inspector automatically. Use the canvas Save, Export, Import, and Clear controls for pipeline files.
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={isSettingsOpen} onOpenChange={setIsSettingsOpen}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Settings className="h-5 w-5 text-emerald-500" />
              Workspace Settings
            </DialogTitle>
            <DialogDescription>
              Control the workspace layout and appearance without leaving the canvas.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="rounded-xl border border-border bg-muted/30 p-3">
              <div className="mb-3 text-sm font-medium">Appearance</div>
              <Button
                variant="outline"
                className="w-full justify-start"
                onClick={() => setIsLightMode((current) => !current)}
              >
                {isLightMode ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
                {isLightMode ? "Switch to dark mode" : "Switch to light mode"}
              </Button>
            </div>

            <div className="rounded-xl border border-border bg-muted/30 p-3">
              <div className="mb-3 text-sm font-medium">Panel visibility</div>
              <div className="grid grid-cols-3 gap-2">
                <Button
                  variant={isLibraryOpen ? "default" : "outline"}
                  size="sm"
                  onClick={handleToggleLibrary}
                >
                  <PanelLeft className="h-4 w-4" />
                  Library
                </Button>
                <Button
                  variant={rightPanel === 'inspector' ? "default" : "outline"}
                  size="sm"
                  onClick={() => handleToggleRightPanel('inspector')}
                >
                  <SlidersHorizontal className="h-4 w-4" />
                  Inspector
                </Button>
                <Button
                  variant={rightPanel === 'chat' ? "default" : "outline"}
                  size="sm"
                  onClick={() => handleToggleRightPanel('chat')}
                >
                  <MessageSquare className="h-4 w-4" />
                  Chat
                </Button>
              </div>
              <Button
                variant="ghost"
                size="sm"
                className="mt-3 w-full justify-start text-muted-foreground"
                onClick={handleResetPanelLayout}
              >
                <RotateCcw className="h-4 w-4" />
                Reset to focused canvas layout
              </Button>
            </div>

            <div className="rounded-xl border border-border bg-muted/30 p-3">
              <div className="mb-2 text-sm font-medium">Chat model</div>
              <p className="mb-3 text-xs text-muted-foreground">
                Current model: {formatConfigDescription(activeConfig)}
              </p>
              <Button
                variant="outline"
                className="w-full justify-start"
                onClick={() => {
                  setPanelPreferences((current) => ({ ...current, rightPanel: 'chat' }));
                  setIsSettingsOpen(false);
                }}
              >
                <MessageSquare className="h-4 w-4" />
                Open chat configuration
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

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
