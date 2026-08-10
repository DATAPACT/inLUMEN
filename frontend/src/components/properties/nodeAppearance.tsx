import React from 'react';
import { Boxes, FileInput, FileOutput, GitBranch, Zap } from 'lucide-react';
import { normalizeType } from '@/features/nodes/nodeSchema';

export const getTypeIcon = (type: string) => {
  switch (normalizeType(type)) {
    case 'source':
      return <FileInput className="w-4 h-4" />;
    case 'task':
      return <Zap className="w-4 h-4" />;
    case 'sink':
      return <FileOutput className="w-4 h-4" />;
    case 'flow':
      return <GitBranch className="w-4 h-4" />;
    case 'subpipeline':
      return <Boxes className="w-4 h-4" />;
  }
};

export const getTypeColor = (type: string) => {
  switch (normalizeType(type)) {
    case 'source':
      return 'bg-blue-500/20 text-blue-300 border-blue-500/30';
    case 'task':
      return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
    case 'sink':
      return 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
    case 'flow':
      return 'bg-purple-500/20 text-purple-300 border-purple-500/30';
    case 'subpipeline':
      return 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30';
  }
};
