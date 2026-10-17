import { useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { NodePort } from '@/features/nodes/nodeSchema';
import {
  artifactForPath,
  portIdError,
  type OutputArtifactDeclaration,
} from '@/features/nodes/outputArtifacts';

type TaskOutputsEditorProps = {
  outputs: NodePort[];
  artifacts: OutputArtifactDeclaration[];
  connectedPorts: string[];
  onChange: (outputs: NodePort[], artifacts: OutputArtifactDeclaration[]) => void;
};

const nextPortId = (outputs: NodePort[]) => {
  let index = outputs.length + 1;
  while (outputs.some((port) => port.id === `output_${index}`)) index += 1;
  return `output_${index}`;
};

/**
 * Output ports of a Task and the file each one publishes.
 *
 * A Task with one output keeps the existing single-result behaviour. With
 * several outputs, every port names the file the Task writes for it, and the
 * runtime hands each connected step only that file. Connected ports can't be
 * renamed or removed, so no connection is ever lost silently.
 */
export const TaskOutputsEditor = ({
  outputs,
  artifacts,
  connectedPorts,
  onChange,
}: TaskOutputsEditorProps) => {
  const artifactFor = (port: string) => artifacts.find((item) => item.port === port);
  const several = outputs.length > 1;
  // Names being typed stay local until valid, so the graph never stores them.
  const [drafts, setDrafts] = useState<Record<number, string>>({});

  const update = (nextOutputs: NodePort[], nextArtifacts: OutputArtifactDeclaration[]) => {
    const ports = new Set(nextOutputs.map((port) => port.id));
    onChange(nextOutputs, nextArtifacts.filter((item) => ports.has(item.port) && item.filename.trim()));
  };

  const rename = (index: number, id: string) => {
    const previous = outputs[index].id;
    const others = outputs.filter((_port, position) => position !== index).map((port) => port.id);
    if (portIdError(id, others)) {
      setDrafts((current) => ({ ...current, [index]: id }));
      return;
    }
    setDrafts((current) => {
      const next = { ...current };
      delete next[index];
      return next;
    });
    update(
      outputs.map((port, position) => (position === index ? { ...port, id, name: id } : port)),
      artifacts.map((item) => (item.port === previous ? { ...item, name: id, port: id } : item)),
    );
  };

  const setFilename = (port: string, filename: string) => {
    const others = artifacts.filter((item) => item.port !== port);
    update(outputs, filename ? [...others, artifactForPath(port, filename)] : others);
  };

  const add = () => {
    const id = nextPortId(outputs);
    update(
      [...outputs, { id, name: id, type: 'any', required: false, description: 'Task output.' }],
      artifacts,
    );
  };

  const remove = (index: number) => {
    setDrafts({});
    update(outputs.filter((_port, position) => position !== index), artifacts);
  };

  return (
    <div className="space-y-2">
      {outputs.map((port, index) => {
        const connected = connectedPorts.includes(port.id);
        const others = outputs.filter((_item, position) => position !== index).map((item) => item.id);
        const name = drafts[index] ?? port.id;
        const error = portIdError(name, others);
        const filename = artifactFor(port.id)?.filename || '';
        return (
          <div key={index} className="space-y-1.5 rounded-md border p-2">
            <div className="flex items-center gap-2">
              <Input
                aria-label={`Output ${index + 1} name`}
                value={name}
                disabled={connected}
                title={connected ? 'Disconnect this output before renaming it.' : undefined}
                onChange={(event) => rename(index, event.target.value.trim())}
                className="h-8 text-xs"
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8 shrink-0"
                disabled={connected || outputs.length === 1}
                title={connected ? 'Disconnect this output before removing it.' : 'Remove output'}
                aria-label={`Remove output ${port.id}`}
                onClick={() => remove(index)}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
            <Input
              aria-label={`File published on ${port.id}`}
              value={filename}
              placeholder={several ? 'File for this output, e.g. report.json' : 'Optional: file for this output'}
              onChange={(event) => setFilename(port.id, event.target.value.trim())}
              className="h-8 text-xs"
            />
            {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
            {several && !filename && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                Name the file the Task writes for this output.
              </p>
            )}
          </div>
        );
      })}
      <Button type="button" variant="outline" size="sm" onClick={add}>
        <Plus className="mr-1 h-3.5 w-3.5" />
        Add output
      </Button>
      <p className="text-xs text-muted-foreground">
        Each output sends its own file to the steps connected to it. Regenerate or update the
        Task code after changing outputs.
      </p>
    </div>
  );
};
