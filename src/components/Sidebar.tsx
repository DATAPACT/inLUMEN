import React, { useState } from 'react';
import { cn } from "@/lib/utils";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import {
  Brain,
  MessageCircle,
  FileText,
  Zap,
  Settings,
  Settings2,
  PanelLeft,
  Clipboard,
  Database,
  Plus,
  Info,
  LayoutGrid,
  Beaker,
  PlayCircle,
  Key,
  PlusCircle,
  Save,
  Upload,
  FileDown
} from 'lucide-react';
import { Separator } from "@/components/ui/separator";

interface SidebarProps {
  className?: string;
  onDragStart: (event: React.DragEvent, nodeType: any) => void;
  activeTab: string;
  onTabChange: (value: string) => void;
  githubToken: string;
  setGithubToken: (token: string) => void;
  onBlankPipeline?: () => void;
  onSavePipeline?: () => void;
  onExportPipeline?: () => void;
  onImportPipeline?: () => void;
  onSaveToYAML?: () => void;
}

interface NodeTypeItem {
  type: string;
  label: string;
  description: string;
  icon: React.ReactNode;
  color: string;
}

const nodeTypes: NodeTypeItem[] = [
  {
    type: 'config',
    label: 'Model Configuration',
    description: 'Adjust model parameters, system prompt and more',
    icon: <Settings className="w-4 h-4" />,
    color: 'bg-sky-500/20 text-sky-300 border-sky-500/30'
  },
  {
    type: 'input',
    label: 'Input Data',
    description: 'Raw data from sensors, APIs, files or user message.',
    icon: <Database className="w-4 h-4" />,
    color: 'bg-blue-500/20 text-blue-300 border-blue-500/30'
  },
  {
    type: 'action',
    label: 'Data Preprocessing',
    description: 'Clean, normalize, and transform input data',
    icon: <FileText className="w-4 h-4" />,
    color: 'bg-lime-500/20 text-lime-300 border-lime-500/30'
  },
  {
    type: 'action',
    label: 'Feature Engineering',
    description: 'Generate or select features for model input',
    icon: <Info className="w-4 h-4" />,
    color: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30'
  },
  {
    type: 'action',
    label: 'Model Training',
    description: 'Train machine learning or deep learning models',
    icon: <Brain className="w-4 h-4" />,
    color: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30'
  },
  {
    type: 'action',
    label: 'Model Evaluation',
    description: 'Assess model performance and metrics',
    icon: <Zap className="w-4 h-4" />,
    color: 'bg-purple-500/20 text-purple-300 border-purple-500/30'
  },
  { type: 'output',
    label: 'AI/ML Output',
    description: 'AI/ML pipeline results',
    icon: <MessageCircle className="w-4 h-4" />,
    color: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' },
  {
    type: 'api',
    label: 'API Call',
    description: 'Connect to external services',
    icon: <Database className="w-4 h-4" />,
    color: 'bg-rose-500/20 text-rose-300 border-rose-500/30'
  },
  {
    type: 'storage',
    label: 'Clipboard',
    description: 'Store and retrieve content',
    icon: <Clipboard className="w-4 h-4" />,
    color: 'bg-teal-500/20 text-teal-300 border-teal-500/30'
  },
  {
    type: 'custom',
    label: 'Custom Node',
    description: 'Add custom label and description',
    icon: <PlusCircle className="w-4 h-4" />,
    color: 'bg-violet-500/20 text-violet-300 border-violet-500/30'
  }
];


export function Sidebar({ 
  className, 
  onDragStart, 
  activeTab, 
  onTabChange, 
  githubToken, 
  setGithubToken,
  onBlankPipeline,
  onSavePipeline,
  onExportPipeline,
  onImportPipeline,
  onSaveToYAML
}: SidebarProps) {
  const [showToken, setShowToken] = useState(false);

  return (
    <div className={cn("w-64 border-r border-border bg-card flex flex-col", className)}>
      
      <Tabs value={activeTab} onValueChange={onTabChange} className="w-full">
        <TabsList className="grid grid-cols-2 w-full rounded-none border-b border-border">
          <TabsTrigger value="lab" className="flex items-center gap-1.5">
            <Beaker className="w-4 h-4" />
            Lab
          </TabsTrigger>
          <TabsTrigger value="simulate" className="flex items-center gap-1.5">
            <PlayCircle className="w-4 h-4" />
            Simulate
          </TabsTrigger>
        </TabsList>
      </Tabs>
      
      <ScrollArea className="flex-1 px-4">
        {activeTab === "lab" && (
          <div className="py-4 space-y-6">
            <div>
              <h3 className="text-sm font-medium mb-3 flex items-center gap-2">
                <Plus className="w-4 h-4" />
                Node Types
              </h3>
              <div className="space-y-2">
                {nodeTypes.map((nodeType) => (
                  <div
                    key={nodeType.type}
                    draggable
                    onDragStart={(event) => onDragStart(event, { 
                      type: 'custom', 
                      data: { 
                        label: nodeType.label,
                        description: nodeType.description,
                        type: nodeType.type
                      } 
                    })}
                    className="flex items-start gap-3 p-2.5 rounded-md border border-border cursor-move hover:bg-muted/50 transition-colors"
                  >
                    <div className={cn("p-1.5 rounded-md", nodeType.color.split(' ')[0])}>
                      {nodeType.icon}
                    </div>
                    <div>
                      <h4 className="text-sm font-medium">{nodeType.label}</h4>
                      <p className="text-xs text-muted-foreground mt-0.5">{nodeType.description}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
        
        {activeTab === "simulate" && (
          <div className="py-4">
            <div className="flex flex-col space-y-4">
              <div className="p-4 border rounded-lg border-border">
                <h3 className="text-sm font-medium mb-2">GitHub Token</h3>
                <p className="text-xs text-muted-foreground mb-4">
                  A GitHub token is required to authenticate with Azure AI.
                </p>
                <div className="flex items-center gap-2 mb-2">
                  <Input
                    type={showToken ? "text" : "password"}
                    value={githubToken}
                    onChange={(e) => setGithubToken(e.target.value)}
                    placeholder="Enter GitHub token"
                    className="flex-1"
                  />
                  <Button 
                    variant="outline" 
                    size="icon" 
                    onClick={() => setShowToken(!showToken)}
                    title={showToken ? "Hide token" : "Show token"}
                  >
                    <Key className="w-4 h-4" />
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Your token is stored in your browser's localStorage.
                </p>
              </div>
              
              <div className="p-4 border rounded-lg border-border">
                <h3 className="text-sm font-medium mb-2">Test Your AI Model</h3>
                <p className="text-xs text-muted-foreground mb-4">
                  Execute your AI thinking flow and test its responses.
                </p>
                <p className="text-xs text-muted-foreground">
                  Use the main test panel to run your AI model.
                </p>
              </div>
            </div>
          </div>
        )}
      </ScrollArea>
    </div>
  );
}