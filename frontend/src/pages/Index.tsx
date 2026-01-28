import React, { useState, useCallback, useEffect, useRef } from 'react';
import { Sidebar } from '@/components/Sidebar';
import { PropertiesPanel } from '@/components/PropertiesPanel';
import { Toolbar } from '@/components/Toolbar';
import { WrappedFlowCanvas, FlowCanvasRef } from '@/components/FlowCanvas';
import { toast } from 'sonner';
import { 
  PlayCircle, 
  Save, 
  MessageSquare, 
  Settings,
  Send,
  PlusCircle,
  ChevronDown,
  Edit,
  Trash2
} from 'lucide-react';
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { callAzureAI } from '@/utils/azureAI';
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

const Index = () => {
  const [selectedNode, setSelectedNode] = useState<any>(null);
  const [activeTab, setActiveTab] = useState('lab'); // 'lab' or 'test'
  const [userInput, setUserInput] = useState('');
  const [aiResponse, setAiResponse] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [githubToken, setGithubToken] = useState('');
  const [flowNodes, setFlowNodes] = useState<Node[]>([]);
  const [conversation, setConversation] = useState<{role: 'user' | 'assistant', content: string}[]>([]);
  const [isLightMode, setIsLightMode] = useState(false);
  const flowCanvasRef = useRef<FlowCanvasRef>(null);
  
  const [configs, setConfigs] = useState<ChatbotConfig[]>([]);
  const [selectedConfig, setSelectedConfig] = useState<ChatbotConfig | null>(null);
  const [isConfigFormOpen, setIsConfigFormOpen] = useState(false);
  const [configToEdit, setConfigToEdit] = useState<ChatbotConfig | undefined>(undefined);

  useEffect(() => {
    const savedToken = localStorage.getItem('github_token');
    if (savedToken) {
      setGithubToken(savedToken);
    }
    
    loadConfigurations();
  }, []);

  const loadConfigurations = async () => {
    try {
      const configsList = await fetchChatbotConfigs();
      setConfigs(configsList);
      
      if (configsList.length > 0 && !selectedConfig) {
        setSelectedConfig(configsList[0]);
      }
    } catch (error) {
      console.error("Error loading configurations:", error);
      toast.error("Failed to load configurations");
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
    // Update both the local flowNodes state AND the FlowCanvas internal state via ref
    setFlowNodes(prev => prev.map(node => 
      node.id === id ? { ...node, data: { ...node.data, ...data } } : node
    ));
    // Also update the FlowCanvas internal nodes via ref
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
    setActiveTab('test');
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
      const response = await callAzureAI(
        updatedConversation.map(msg => ({ 
          role: msg.role, 
          content: msg.content 
        })), 
        githubToken, 
        flowNodes,
        selectedConfig || undefined
      );
      
      setConversation(prev => [...prev, { role: 'assistant', content: response }]);
      setAiResponse(response);
      setUserInput('');
    } catch (error) {
      console.error("Error processing request:", error);
      toast.error("An error occurred while processing your request");
    } finally {
      setIsProcessing(false);
    }
  };

  const handleClearConversation = () => {
    setConversation([]);
    setAiResponse('');
    toast.success("Conversation cleared", {
      description: "Your conversation history has been reset",
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
            setSelectedConfig(updatedConfigs.length > 0 ? updatedConfigs[0] : null);
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
      description: `Using ${config.model} with temperature ${config.temperature}`
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
    localStorage.setItem('saved-pipeline-nodes', JSON.stringify(flowNodes));
    localStorage.setItem('saved-pipeline-timestamp', new Date().toISOString());
    toast.success("Pipeline saved", {
      description: "Your pipeline will persist on next visit"
    });
  };

  const handleExportPipeline = () => {
    const pipelineData = {
      nodes: flowNodes,
      timestamp: new Date().toISOString()
    };
    const dataStr = JSON.stringify(pipelineData, null, 2);
    const dataBlob = new Blob([dataStr], { type: 'application/json' });
    const url = URL.createObjectURL(dataBlob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `pipeline-${Date.now()}.json`;
    link.click();
    URL.revokeObjectURL(url);
    toast.success("Pipeline exported");
  };

  const handleImportPipeline = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (file) {
        const reader = new FileReader();
        reader.onload = (e) => {
          try {
            const data = JSON.parse(e.target?.result as string);
            if (data.nodes) {
              setFlowNodes(data.nodes);
              toast.success("Pipeline imported");
            }
          } catch (error) {
            toast.error("Invalid pipeline file");
          }
        };
        reader.readAsText(file);
      }
    };
    input.click();
  };

  const handleSaveToYAML = () => {
    const workflow = {
      apiVersion: 'argoproj.io/v1alpha1',
      kind: 'Workflow',
      metadata: {
        generateName: 'pipeline-',
      },
      spec: {
        entrypoint: 'pipeline',
        templates: flowNodes.map(node => ({
          name: node.id,
          container: {
            image: 'alpine:latest',
            command: ['echo'],
            args: [node.data.label || 'Node'],
          },
        })),
      },
    };
    
    const yamlStr = `# Argo Workflow generated from inLUMEN pipeline
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: pipeline-
spec:
  entrypoint: pipeline
  templates:
${flowNodes.map(node => `  - name: ${node.id}
    container:
      image: alpine:latest
      command: [echo]
      args: ["${node.data.label || 'Node'}"]`).join('\n')}
`;
    
    const dataBlob = new Blob([yamlStr], { type: 'text/yaml' });
    const url = URL.createObjectURL(dataBlob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `argo-workflow-${Date.now()}.yaml`;
    link.click();
    URL.revokeObjectURL(url);
    toast.success("Workflow saved to YAML");
  };

  const handleRemoveNode = (nodeId: string) => {
    setFlowNodes(prev => prev.filter(node => node.id !== nodeId));
    if (selectedNode?.id === nodeId) {
      setSelectedNode(null);
    }
    toast.success("Node removed");
  };

  const handleRemoveEdge = (edgeId: string) => {
    // This will be implemented in FlowCanvas
    toast.success("Connection removed");
  };

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
          onExportPipeline={handleExportPipeline}
          onImportPipeline={handleImportPipeline}
          onSaveToYAML={handleSaveToYAML}
        />
        
        {activeTab === 'lab' ? (
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
                    <div className="p-4 border-b border-border">
                      <h3 className="text-sm font-medium text-gray-900">AI-assisted Pipeline Design Chat</h3>
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
                                className={`max-w-[80%] p-3 rounded-lg ${
                                  msg.role === 'user' 
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
          <ResizablePanelGroup direction="horizontal" className="flex-1">
            <ResizablePanel defaultSize={75} minSize={50}>
              <div className="h-full bg-canvas-DEFAULT flex items-center justify-center">
                <div className="max-w-2xl w-full h-[80vh] p-6 bg-card/30 backdrop-blur-sm border border-border rounded-lg flex flex-col">
                  <div className="flex items-center justify-between mb-4">
                    <h2 className="text-2xl font-bold flex items-center gap-2">
                      <MessageSquare className="w-5 h-5" />
                      AI-assisted Pipeline Design Chat
                    </h2>
                <div className="flex items-center gap-2">
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="outline" className="gap-1">
                        <Settings className="w-4 h-4" />
                        {selectedConfig ? (
                          <span className="hidden md:inline-flex">
                            {selectedConfig.name}
                          </span>
                        ) : (
                          <span className="hidden md:inline-flex">
                            Configuration
                          </span>
                        )}
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
                            {config.name}
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
                              <Trash2 className="h-3 w-3" />
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
                </div>
              </div>
              
              <div className="flex-1 overflow-y-auto mb-4 p-4 bg-gradient-to-b from-gray-900 to-gray-950 rounded-md border border-border">
                {conversation.length > 0 ? (
                  <div className="space-y-3">
                    {conversation.map((msg, index) => (
                      <div 
                        key={index}
                        className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                      >
                        <div 
                          className={`max-w-[80%] p-3 rounded-lg ${
                            msg.role === 'user' 
                              ? 'bg-emerald-700 text-white rounded-tr-none' 
                              : 'bg-gray-800 text-white rounded-tl-none'
                          }`}
                        >
                          <div className="text-sm whitespace-pre-wrap">
                            {msg.content}
                          </div>
                          <div className="text-[10px] opacity-70 text-right mt-1">
                            {new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="h-full flex flex-col items-center justify-center text-center text-muted-foreground">
                    <MessageSquare className="h-12 w-12 mb-4 opacity-20" />
                    <p className="text-lg font-medium">Your chat is empty</p>
                    <p className="max-w-xs mx-auto mt-2 text-sm">
                      Send a message to start chatting with your AI assistant
                    </p>
                  </div>
                )}
              </div>
              
              <div className="relative">
                <Textarea 
                  className="w-full pr-12 py-3 rounded-full text-sm bg-gray-800 border-gray-700 placeholder:text-gray-500 resize-none" 
                  placeholder="Type a message..."
                  value={userInput}
                  onChange={(e) => setUserInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleSendMessage();
                    }
                  }}
                  rows={1}
                />
                <Button 
                  className="absolute right-1 bottom-1 p-2 h-9 w-9 rounded-full bg-emerald-600 hover:bg-emerald-700 text-white"
                  onClick={handleSendMessage}
                  disabled={isProcessing || !githubToken || !userInput.trim()}
                >
                  <Send className="h-4 w-4" />
                </Button>
              </div>
              
              <div className="text-xs text-muted-foreground pt-4 flex justify-between items-center mt-2">
                <div>
                  <p>
                    {selectedConfig 
                      ? `Using: ${selectedConfig.name} (${selectedConfig.model}, temp: ${selectedConfig.temperature})`
                      : 'No configuration selected'}
                  </p>
                </div>
                <Button 
                  variant="outline"
                  size="sm"
                  onClick={handleSaveWorkflow}
                  className="text-xs flex items-center gap-1"
                >
                  <Save className="w-3 h-3" />
                  Save Workflow
                </Button>
              </div>
            </div>
          </div>
        </ResizablePanel>
        <ResizableHandle />
        <ResizablePanel defaultSize={25} minSize={20} maxSize={50}>
          <div className="h-full p-4 bg-muted/30">
            <h3 className="text-sm font-medium mb-2">Pipeline Overview</h3>
            <div className="text-xs text-muted-foreground">
              Additional tools and information can be added here
            </div>
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
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