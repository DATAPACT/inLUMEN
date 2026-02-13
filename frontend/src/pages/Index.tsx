import React, { useState, useCallback, useEffect, useRef } from 'react';
import { Sidebar } from '@/components/Sidebar';
import { PropertiesPanel } from '@/components/PropertiesPanel';
import { Toolbar } from '@/components/Toolbar';
import { WrappedFlowCanvas, FlowCanvasRef } from '@/components/FlowCanvas';
import { toast } from 'sonner';
import {
  Save,
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
import { Node } from 'reactflow';
import {
  ChatbotConfig,
  fetchChatbotConfigs,
  deleteChatbotConfig
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

const SIMPLE_CHAT_SESSION_KEY = "simple-chat-session-id";

const Index = () => {
  const [selectedNode, setSelectedNode] = useState<any>(null);
  const [activeTab, setActiveTab] = useState('lab'); // 'lab', 'overview', or 'simulate'
  const [userInput, setUserInput] = useState('');
  const [aiResponse, setAiResponse] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [githubToken, setGithubToken] = useState('');
  const [flowNodes, setFlowNodes] = useState<Node[]>([]);
  const [conversation, setConversation] = useState<{ role: 'user' | 'assistant', content: string }[]>([]);
  const [isLightMode, setIsLightMode] = useState(false);
  const flowCanvasRef = useRef<FlowCanvasRef>(null);
  const [pipelineLastUpdate, setPipelineLastUpdate] = useState<string>('Never');
  const [pipelineCreatedAt, setPipelineCreatedAt] = useState<string>('Never');
  const [configs, setConfigs] = useState<ChatbotConfig[]>([]);
  const [selectedConfig, setSelectedConfig] = useState<ChatbotConfig | null>(null);
  const [isConfigFormOpen, setIsConfigFormOpen] = useState(false);
  const [configToEdit, setConfigToEdit] = useState<ChatbotConfig | undefined>(undefined);

  // Backend session id
  const [chatSessionId, setChatSessionId] = useState<string>(() => {
    return localStorage.getItem(SIMPLE_CHAT_SESSION_KEY) || "";
  });

  useEffect(() => {
    if (chatSessionId) {
      localStorage.setItem(SIMPLE_CHAT_SESSION_KEY, chatSessionId);
    }
  }, [chatSessionId]);

  // Helper to display model labels
  const formatModelLabel = (model?: string) => {
    if (!model) return "Llama 3.1";
    const m = model.toLowerCase();
    if (m === "llama3.1") return "Llama 3.1";
    if (m === "gpt-4o") return "GPT-4o";
    return model;
  };

  // Local fallback config so UI shows default model even before configs exist
  const defaultConfig: ChatbotConfig = {
    name: "Configuration",
    model: "llama3.1",
  };

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
  }, []);

  const loadConfigurations = async () => {
    try {
      const configsList = await fetchChatbotConfigs();
      setConfigs(configsList);

      if (!selectedConfig) {
        if (configsList.length > 0) {
          const llamaPreferred =
            configsList.find(c => (c.model || "").toLowerCase() === "llama3.1") || configsList[0];
          setSelectedConfig(llamaPreferred);
        } else {
          setSelectedConfig(defaultConfig);
        }
      }
    } catch (error) {
      console.error("Error loading configurations:", error);
      toast.error("Failed to load configurations");
      if (!selectedConfig) setSelectedConfig(defaultConfig);
    }
  };

  useEffect(() => {
    if (githubToken) {
      localStorage.setItem('github_token', githubToken);
    }
  }, [githubToken]);

  const onNodeSelect = useCallback((node: any) => {
    setSelectedNode(node);
  }, []);

  const onNodeUpdate = useCallback((id: string, data: any) => {
    setFlowNodes(prev => prev.map(node =>
      node.id === id ? { ...node, data: { ...node.data, ...data } } : node
    ));
    flowCanvasRef.current?.updateNode(id, data);
  }, []);

  const onNodesChange = useCallback((nodes: Node[]) => {
    setFlowNodes(nodes);
  }, []);

  const onDragStart = (event: React.DragEvent, nodeType: any) => {
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

      const res = await fetch("http://localhost:5002/simple_chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: chatSessionId || null,
          user_message: userInput,
          model: activeCfg.model || "llama3.1",
        }),
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`simple_chat failed (${res.status}): ${errText}`);
      }

      const data = await res.json();

      if (data.session_id && data.session_id !== chatSessionId) {
        setChatSessionId(data.session_id);
      }

      const responseText = data.assistant_message ?? "";
      setConversation(prev => [...prev, { role: 'assistant', content: responseText }]);
      setAiResponse(responseText);
      setUserInput('');
    } catch (error) {
      console.error("Error processing request:", error);
      toast.error("An error occurred while processing your request");
    } finally {
      setIsProcessing(false);
    }
  };

  const handleClearConversation = async () => {
    setConversation([]);
    setAiResponse('');
    toast.success("Conversation cleared", {
      description: "Your conversation history has been reset",
    });

    if (chatSessionId) {
      try {
        await fetch("http://localhost:5002/simple_chat/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: chatSessionId }),
        });
      } catch (e) {
        console.warn("Failed to reset backend simple_chat session:", e);
      }
    }

    setChatSessionId("");
    localStorage.removeItem(SIMPLE_CHAT_SESSION_KEY);
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
            if (updatedConfigs.length > 0) {
              const llamaPreferred =
                updatedConfigs.find(c => (c.model || "").toLowerCase() === "llama3.1") || updatedConfigs[0];
              setSelectedConfig(llamaPreferred);
            } else {
              setSelectedConfig(defaultConfig);
            }
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
      description: `Using ${formatModelLabel(config.model)}`
    });
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
  const configButtonLabel = `${activeConfig.name} (${formatModelLabel(activeConfig.model)})`;

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
        />

        {showFlowLayout ? (
          <ResizablePanelGroup direction="horizontal" className="flex-1">
            <ResizablePanel defaultSize={60} minSize={40}>
              <div className={`h-full ${isLightMode ? 'bg-gray-50' : 'bg-canvas-DEFAULT'}`}>
                <WrappedFlowCanvas
                  onNodeSelect={onNodeSelect}
                  onNodesChange={onNodesChange}
                  onRemoveNode={handleRemoveNode}
                  onRemoveEdge={handleRemoveEdge}
                  isLightMode={isLightMode}
                  flowCanvasRef={flowCanvasRef}
                />
              </div>
            </ResizablePanel>

            <ResizableHandle withHandle />

            <ResizablePanel defaultSize={40} minSize={30}>
              <ResizablePanelGroup direction="horizontal">
                <ResizablePanel defaultSize={60} minSize={30}>
                  <PropertiesPanel
                    selectedNode={selectedNode}
                    onNodeUpdate={onNodeUpdate}
                    onRemoveNode={handleRemoveNode}
                  />
                </ResizablePanel>

                <ResizableHandle withHandle />

                <ResizablePanel defaultSize={40} minSize={20} maxSize={60}>
                  <div className="h-full bg-white border-l border-border flex flex-col">
                    <div className="p-4 border-b border-border flex items-center justify-between gap-2">
                      <h3 className="text-sm font-medium text-gray-900">
                        AI-assisted Pipeline Design Chat
                      </h3>

                      <div className="flex items-center gap-2">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="outline" className="gap-1" size="sm">
                              <Settings className="w-4 h-4" />
                              <span className="hidden md:inline-flex">
                                {configButtonLabel}
                              </span>
                              <ChevronDown className="w-3 h-3 opacity-50" />
                            </Button>
                          </DropdownMenuTrigger>

                          <DropdownMenuContent align="end" className="w-56">
                            <DropdownMenuLabel>Chatbot Configurations</DropdownMenuLabel>
                            <DropdownMenuSeparator />

                            {configs.map((config) => (
                              <DropdownMenuItem
                                key={config.id}
                                className="flex justify-between cursor-pointer"
                                onClick={() => handleSelectConfig(config)}
                              >
                                <span className={selectedConfig?.id === config.id ? "font-bold" : ""}>
                                  {config.name} ({formatModelLabel(config.model)})
                                </span>
                                <div className="flex items-center gap-1">
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-6 w-6"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      handleEditConfig(config);
                                    }}
                                  >
                                    <Edit className="h-3 w-3" />
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-6 w-6 text-destructive"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      if (config.id) handleDeleteConfig(config.id);
                                    }}
                                  >
                                    <Trash2 className="h-3 h-3" />
                                  </Button>
                                </div>
                              </DropdownMenuItem>
                            ))}

                            <DropdownMenuSeparator />
                            <DropdownMenuItem
                              className="flex items-center gap-2 cursor-pointer"
                              onClick={handleCreateConfig}
                            >
                              <PlusCircle className="h-4 w-4" />
                              <span>New Configuration</span>
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>

                        <Button
                          variant="outline"
                          size="sm"
                          onClick={handleClearConversation}
                          className="text-xs"
                        >
                          Clear Chat
                        </Button>

                        <Button
                          variant="outline"
                          size="sm"
                          onClick={handleSaveWorkflow}
                          className="text-xs flex items-center gap-1"
                        >
                          <Save className="w-3 h-3" />
                          Save
                        </Button>
                      </div>
                    </div>

                    <div className="flex-1 overflow-y-auto p-4">
                      {conversation.length > 0 ? (
                        <div className="space-y-3">
                          {conversation.map((msg, index) => (
                            <div
                              key={index}
                              className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                            >
                              <div
                                className={`max-w-[80%] p-3 rounded-lg ${msg.role === 'user'
                                  ? 'bg-blue-600 text-white'
                                  : 'bg-gray-100 text-gray-900'
                                  }`}
                              >
                                <div className="text-sm whitespace-pre-wrap">
                                  {msg.content}
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="h-full flex items-center justify-center">
                          <p className="text-gray-400 text-sm">Describe your pipeline</p>
                        </div>
                      )}
                    </div>

                    <div className="p-4 border-t border-border">
                      <div className="flex gap-2">
                        <Textarea
                          className="flex-1 text-gray-900 border-gray-300 bg-white"
                          placeholder="Describe your pipeline"
                          value={userInput}
                          onChange={(e) => setUserInput(e.target.value)}
                          rows={1}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                              e.preventDefault();
                              handleSendMessage();
                            }
                          }}
                        />
                        <Button
                          onClick={handleSendMessage}
                          disabled={isProcessing || !userInput.trim()}
                          className="bg-blue-600 hover:bg-blue-700 text-white"
                        >
                          <Send className="h-4 w-4" />
                        </Button>
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
