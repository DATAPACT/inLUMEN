import React from 'react';
import { Boxes, FileInput, FileOutput, GitBranch, Zap } from 'lucide-react';
import { normalizeType } from '@/features/nodes/nodeSchema';

export const getTypeIcon = (type: string) => {
  switch (normalizeType(type)) {
    case 'source':
      return <FileInput className="w-4 h-4" />;
    case 'task':
      return <Zap className="w-4 h-4" />;
    case 'destination':
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
      return 'node-type-badge node-type-badge-source';
    case 'task':
      return 'node-type-badge node-type-badge-task';
    case 'destination':
      return 'node-type-badge node-type-badge-destination';
    case 'flow':
      return 'node-type-badge node-type-badge-flow';
    case 'subpipeline':
      return 'node-type-badge node-type-badge-subpipeline';
  }
};
