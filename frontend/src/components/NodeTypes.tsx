import React, { useContext } from 'react';
import { Handle, Position } from 'reactflow';
import { cn } from '@/lib/utils';
import {
  Boxes,
  FileInput,
  FileOutput,
  GitBranch,
  PanelLeft,
  Zap,
} from 'lucide-react';
import {
  getStepTypeLabel,
  normalizeNodePorts,
  normalizeType,
  type NodePorts,
} from '@/features/nodes/nodeSchema';
import { PortDisplayContext } from '@/features/nodes/PortDisplayContext';

interface NodeProps {
  id: string;
  data: {
    label: string;
    description?: string;
    type: string;
    content?: string;
    active?: boolean;
    ports?: Partial<NodePorts>;
    template_label?: string;
    implementation?: Record<string, unknown>;
    param?: Record<string, unknown>;
    subpipeline?: {
      reference?: {
        pipeline_name?: string;
        version_name?: string;
      };
    };
  };
  selected: boolean;
}

const icons = {
  source: FileInput,
  task: Zap,
  destination: FileOutput,
  flow: GitBranch,
  subpipeline: Boxes,
};

const TYPE_STYLES = {
  source: {
    accent: 'bg-blue-400',
    icon: 'bg-blue-500/15 text-blue-300 ring-blue-400/20',
    border: 'border-blue-400/20',
    selected: 'border-blue-400/70 shadow-[0_0_0_1px_rgba(96,165,250,0.35),0_8px_30px_rgba(37,99,235,0.14)]',
  },
  task: {
    accent: 'bg-amber-400',
    icon: 'bg-amber-500/15 text-amber-300 ring-amber-400/20',
    border: 'border-amber-400/20',
    selected: 'border-amber-400/70 shadow-[0_0_0_1px_rgba(251,191,36,0.35),0_8px_30px_rgba(217,119,6,0.14)]',
  },
  destination: {
    accent: 'bg-emerald-400',
    icon: 'bg-emerald-500/15 text-emerald-300 ring-emerald-400/20',
    border: 'border-emerald-400/20',
    selected: 'border-emerald-400/70 shadow-[0_0_0_1px_rgba(52,211,153,0.35),0_8px_30px_rgba(5,150,105,0.14)]',
  },
  flow: {
    accent: 'bg-purple-400',
    icon: 'bg-purple-500/15 text-purple-300 ring-purple-400/20',
    border: 'border-purple-400/20',
    selected: 'border-purple-400/70 shadow-[0_0_0_1px_rgba(192,132,252,0.35),0_8px_30px_rgba(147,51,234,0.14)]',
  },
  subpipeline: {
    accent: 'bg-cyan-400',
    icon: 'bg-cyan-500/15 text-cyan-300 ring-cyan-400/20',
    border: 'border-cyan-400/20',
    selected: 'border-cyan-400/70 shadow-[0_0_0_1px_rgba(34,211,238,0.35),0_8px_30px_rgba(8,145,178,0.14)]',
  },
};

const portPosition = (index: number, count: number) =>
  `${((index + 1) / (count + 1)) * 100}%`;

const PortList = ({
  title,
  ports,
  align,
}: {
  title: string;
  ports: NodePorts['inputs'];
  align: 'left' | 'right';
}) => (
  <div className={cn('min-w-0', align === 'right' && 'text-right')}>
    <div className="mb-0.5 text-[8px] font-semibold uppercase tracking-[0.14em] text-slate-500">
      {title}
    </div>
    <div className="space-y-0.5">
      {ports.map((port) => (
        <div
          key={port.id}
          className={cn(
            'flex min-w-0 items-baseline gap-1 text-[9px] leading-3',
            align === 'right' && 'justify-end',
          )}
        >
          <span className="truncate font-medium text-slate-300">{port.name}</span>
          {port.type !== 'any' && (
            <span className="shrink-0 text-slate-500">{port.type}</span>
          )}
          {port.required && <span className="shrink-0 text-amber-400" title="Required">*</span>}
        </div>
      ))}
    </div>
  </div>
);

export const CustomNode: React.FC<NodeProps> = ({ id, data, selected }) => {
  const display = useContext(PortDisplayContext);
  const validationIssues = display.validationByNode[id] || [];
  const validationErrors = validationIssues.filter((issue) => issue.severity === 'error').length;
  const validationWarnings = validationIssues.filter((issue) => issue.severity === 'warning').length;
  const visualType = normalizeType(data.type);
  // Flow behavior is encoded by named handles, so those handles stay visible
  // and connectable even when the rest of the canvas is in Compact mode.
  const showPortDetails = display.advanced || visualType === 'flow';
  const ports = normalizeNodePorts(data.ports, visualType);
  const style = TYPE_STYLES[visualType];
  const Icon = icons[visualType] || PanelLeft;
  const templateLabel = String(data.template_label || '').trim();
  const structuralDefaults = new Set([
    'Source',
    'Task',
    'Blank Task',
    'Sink',
    'Destination',
    'Flow',
    'Subpipeline',
  ]);
  const showTemplate = templateLabel && !structuralDefaults.has(templateLabel);

  return (
    <div
      className={cn(
        'node-custom relative overflow-visible rounded-xl border bg-slate-950/90 text-slate-100',
        showPortDetails ? 'w-[218px] px-3 py-2.5' : 'w-[184px] px-3 py-2.5',
        style.border,
        selected ? style.selected : 'shadow-[0_5px_18px_rgba(0,0,0,0.2)]',
        validationErrors > 0 && 'border-red-500/80 shadow-[0_0_0_1px_rgba(239,68,68,0.35),0_8px_28px_rgba(239,68,68,0.16)]',
        validationErrors === 0 && validationWarnings > 0 && 'border-amber-400/80 shadow-[0_0_0_1px_rgba(251,191,36,0.3),0_8px_28px_rgba(245,158,11,0.12)]',
        data.active && 'animate-pulse',
      )}
    >
      <div className={cn('absolute inset-y-3 left-0 w-0.5 rounded-r-full opacity-80', style.accent)} />

      {ports.inputs.map((port, index) => (
        <Handle
          key={`input-${port.id}`}
          id={port.id}
          type="target"
          position={Position.Left}
          title={`${port.name}${port.type !== 'any' ? ` · ${port.type}` : ''}${port.description ? ` — ${port.description}` : ''}`}
          style={{ top: portPosition(index, ports.inputs.length) }}
          className={cn(
            "node-port-handle node-port-handle-input",
            !showPortDetails && "pointer-events-none opacity-0",
          )}
        />
      ))}

      <div className="flex items-start gap-2.5">
        <div className={cn('mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ring-1', style.icon)}>
          <Icon className="h-3.5 w-3.5" />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1.5 text-[9px] leading-3 text-slate-400">
            <span className="shrink-0 font-semibold uppercase tracking-[0.1em]">
              {getStepTypeLabel(visualType)}
            </span>
            {showTemplate && (
              <>
                <span className="text-slate-600">/</span>
                <span className="truncate">{templateLabel}</span>
              </>
            )}
          </div>

          <div className="mt-1 flex items-baseline justify-between gap-2">
            <div className="truncate text-[13px] font-semibold leading-4 text-slate-100">{data.label}</div>
          </div>

          {data.description && (
            <div className={cn(
              'mt-1 text-[10px] leading-3.5 text-slate-400',
              showPortDetails ? 'line-clamp-2' : 'line-clamp-1',
            )}>
              {data.description}
            </div>
          )}

          {visualType === 'flow' && (
            <div className="mt-1 rounded bg-purple-500/10 px-1.5 py-1 text-[9px] leading-3 text-purple-200">
              {templateLabel === 'Condition'
                ? `If ${String(data.param?.expression || '…set an expression')}`
                : templateLabel === 'Parallel Map'
                  ? `One run per item · max ${String(data.param?.max_concurrency || 4)} at once`
                  : 'Choose Condition or Parallel Map'}
            </div>
          )}

          {showPortDetails && validationIssues.length > 0 && (
            <div className={cn(
              'mt-1 text-[9px]',
              validationErrors > 0 ? 'text-red-300' : 'text-amber-300',
            )}>
              {validationErrors > 0 ? `${validationErrors} validation error${validationErrors === 1 ? '' : 's'}` : `${validationIssues.length} validation warning${validationIssues.length === 1 ? '' : 's'}`}
            </div>
          )}
        </div>
      </div>

      {showPortDetails && (ports.inputs.length > 0 || ports.outputs.length > 0) && (
        <div className="mt-2 grid grid-cols-2 gap-3 border-t border-white/[0.07] pt-1.5">
          {ports.inputs.length > 0 ? (
            <PortList title="In" ports={ports.inputs} align="left" />
          ) : <div />}
          {ports.outputs.length > 0 && (
            <PortList title="Out" ports={ports.outputs} align="right" />
          )}
        </div>
      )}

      {visualType === 'subpipeline' && data.subpipeline?.reference && (
        <div className="mt-2 rounded-md border border-cyan-400/15 bg-cyan-950/20 px-2 py-1.5 text-[9px] text-cyan-200">
          References {data.subpipeline.reference.pipeline_name || 'reusable pipeline'} · {data.subpipeline.reference.version_name || 'pinned version'}
        </div>
      )}

      {ports.outputs.map((port, index) => (
        <Handle
          key={`output-${port.id}`}
          id={port.id}
          type="source"
          position={Position.Right}
          title={`${port.name}${port.type !== 'any' ? ` · ${port.type}` : ''}${port.description ? ` — ${port.description}` : ''}`}
          style={{ top: portPosition(index, ports.outputs.length) }}
          className={cn(
            "node-port-handle node-port-handle-output",
            !showPortDetails && "pointer-events-none opacity-0",
          )}
        />
      ))}
    </div>
  );
};

export const nodeTypes = {
  custom: CustomNode,
};
