import React from 'react';
import {
  Brain,
  Clipboard,
  Database,
  FileText,
  MessageCircle,
  PlusCircle,
  Settings,
  Zap,
} from 'lucide-react';

export const getTypeIcon = (type: string) => {
  switch (type) {
    case 'system':
      return <Brain className="w-4 h-4" />;
    case 'input':
      return <FileText className="w-4 h-4" />;
    case 'output':
      return <MessageCircle className="w-4 h-4" />;
    case 'action':
      return <Zap className="w-4 h-4" />;
    case 'storage':
      return <Database className="w-4 h-4" />;
    case 'api':
      return <Database className="w-4 h-4" />;
    case 'config':
      return <Settings className="w-4 h-4" />;
    case 'clipboard':
      return <Clipboard className="w-4 h-4" />;
    case 'custom':
      return <PlusCircle className="w-4 h-4" />;
    default:
      return null;
  }
};

export const getTypeColor = (type: string) => {
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
    case 'storage':
      return 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
    case 'config':
      return 'bg-sky-500/20 text-sky-300 border-sky-500/30';
    case 'clipboard':
      return 'bg-teal-500/20 text-teal-300 border-teal-500/30';
    case 'custom':
      return 'bg-violet-500/20 text-violet-300 border-violet-500/30';
    default:
      return 'bg-gray-500/20 text-gray-300 border-gray-500/30';
  }
};
