import React from 'react';
import { Handle, Position } from 'reactflow';
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { 
  Brain, 
  MessageCircle, 
  FileText, 
  Zap, 
  Settings, 
  PanelLeft, 
  Clipboard,
  Database,
  PlusCircle 
} from 'lucide-react';
import { normalizeType } from '@/features/nodes/nodeSchema';

interface NodeProps {
  data: {
    label: string;
    description?: string;
    type: string;
    content?: string;
    active?: boolean;
  };
  selected: boolean;
}

const icons = {
  system: <Brain className="w-4 h-4" />,
  input: <FileText className="w-4 h-4" />,
  output: <MessageCircle className="w-4 h-4" />,
  action: <Zap className="w-4 h-4" />,
  api: <Database className="w-4 h-4" />,
  config: <Settings className="w-4 h-4" />,
  storage: <Clipboard className="w-4 h-4" />,
  custom: <PlusCircle className="w-4 h-4" />,
};

const getTypeColor = (type: string) => {
  switch (type) {
    case 'system':
      return 'bg-purple-500/20 text-purple-300 border-purple-500/30';
    case 'input':
      return 'bg-blue-500/20 text-blue-300 border-blue-500/30';
    case 'output':
      return 'bg-green-500/20 text-green-300 border-green-500/30';
    case 'action':
      return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
    case 'api':
      return 'bg-rose-500/20 text-rose-300 border-rose-500/30';
    case 'config':
      return 'bg-sky-500/20 text-sky-300 border-sky-500/30';
    case 'storage':
      return 'bg-teal-500/20 text-teal-300 border-teal-500/30';
    case 'custom':
      return 'bg-gray-500/20 text-gray-300 border-gray-500/30';
    default:
      return 'bg-gray-500/20 text-gray-300 border-gray-500/30';
  }
};

export const CustomNode: React.FC<NodeProps> = ({ data, selected }) => {
  const visualType = data.type === 'system' ? 'system' : normalizeType(data.type);
  const icon = icons[visualType as keyof typeof icons] || <PanelLeft className="w-4 h-4" />;
  const typeColor = getTypeColor(visualType);
  
  return (
    <div 
      className={cn(
        "node-custom px-3 py-2 rounded-lg border min-w-[160px] max-w-[240px] animate-fade-in text-slate-100",
        selected ? "border-accent/80 shadow-[0_0_0_1px_hsl(var(--accent))]" : "border-border",
        data.active ? "animate-pulse border-purple-500" : "",
        visualType === 'system' ? "bg-purple-950/40" :
        visualType === 'input' ? "bg-blue-950/40" :
        visualType === 'output' ? "bg-green-950/40" :
        visualType === 'action' ? "bg-amber-950/40" :
        visualType === 'api' ? "bg-rose-950/40" :
        visualType === 'config' ? "bg-sky-950/40" :
        visualType === 'custom' ? "bg-gray-950/40" :
        visualType === 'storage' ? "bg-teal-950/40" : "bg-gray-950/40"
      )}
    >
      {/* Input handle on the top */}
      <Handle 
        type="target" 
        position={Position.Top} 
        className="w-2 h-2 bg-node-connector" 
      />
      
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <Badge 
            variant="outline" 
            className={cn("text-xs font-normal flex items-center gap-1", typeColor)}
          >
            {icon}
            {visualType}
          </Badge>
        </div>
        
        <div className="text-sm font-medium">{data.label}</div>
        
        {data.description && (
          <div className="text-xs text-slate-300">{data.description}</div>
        )}

        {data.content && (
          <div className="text-xs bg-slate-950/30 p-2 rounded border border-white/10 mt-1 max-h-20 overflow-y-auto">
            <p className="line-clamp-3">{data.content}</p>
          </div>
        )}
      </div>
      
      {/* Output handle on the bottom */}
      <Handle 
        type="source" 
        position={Position.Bottom} 
        className="w-2 h-2 bg-node-connector" 
      />
    </div>
  );
};

export const nodeTypes = {
  custom: CustomNode,
};
