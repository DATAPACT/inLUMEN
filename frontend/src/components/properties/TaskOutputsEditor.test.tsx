import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';

import { TaskOutputsEditor } from '@/components/properties/TaskOutputsEditor';
import { artifactForPath, portIdError } from '@/features/nodes/outputArtifacts';
import type { NodePort } from '@/features/nodes/nodeSchema';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const port = (id: string): NodePort => ({ id, name: id, type: 'any', required: true, description: '' });

const type = async (input: HTMLInputElement, value: string) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};

const mount = async (props: Parameters<typeof TaskOutputsEditor>[0]) => {
  const container = document.createElement('div');
  const root = createRoot(container);
  await act(async () => root.render(<TaskOutputsEditor {...props} />));
  return { container, root };
};

describe('output artifact helpers', () => {
  it('validates port names and infers artifact kinds', () => {
    expect(portIdError('report', ['data'])).toBe('');
    expect(portIdError('9bad', [])).not.toBe('');
    expect(portIdError('data', ['data'])).toContain('own name');
    expect(artifactForPath('data', 'clean.csv')).toEqual({ name: 'data', port: 'data', filename: 'clean.csv', kind: 'table', format: 'csv' });
    expect(artifactForPath('blob', 'model.bin')).toEqual({ name: 'blob', port: 'blob', filename: 'model.bin' });
  });
});

describe('TaskOutputsEditor', () => {
  it('adds an output and records the file each output publishes', async () => {
    const onChange = vi.fn();
    const { container, root } = await mount({ outputs: [port('output')], artifacts: [], connectedPorts: [], onChange });

    const add = [...container.querySelectorAll('button')].find((button) => button.textContent?.includes('Add output'))!;
    await act(async () => add.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(onChange.mock.lastCall![0].map((item: NodePort) => item.id)).toEqual(['output', 'output_2']);

    await act(async () => root.render(
      <TaskOutputsEditor outputs={[port('output'), port('output_2')]} artifacts={[]} connectedPorts={[]} onChange={onChange} />,
    ));
    expect(container.textContent).toContain('Name the file the Task writes for this output.');
    const file = container.querySelector('input[aria-label="File published on output_2"]') as HTMLInputElement;
    await type(file, 'report.json');
    expect(onChange.mock.lastCall![1]).toEqual([
      { name: 'output_2', port: 'output_2', filename: 'report.json', kind: 'json', format: 'json' },
    ]);
    await act(async () => root.unmount());
  });

  it('keeps invalid names local and renames artifacts with valid ones', async () => {
    const onChange = vi.fn();
    const artifacts = [artifactForPath('first', 'a.json'), artifactForPath('second', 'b.csv')];
    const { container, root } = await mount({ outputs: [port('first'), port('second')], artifacts, connectedPorts: [], onChange });
    const name = container.querySelector('input[aria-label="Output 1 name"]') as HTMLInputElement;

    await type(name, 'second');
    expect(onChange).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('own name');

    await type(name, 'summary');
    const [outputs, nextArtifacts] = onChange.mock.lastCall!;
    expect(outputs.map((item: NodePort) => item.id)).toEqual(['summary', 'second']);
    expect(nextArtifacts[0]).toMatchObject({ name: 'summary', port: 'summary', filename: 'a.json' });
    await act(async () => root.unmount());
  });

  it('locks connected outputs and the last remaining output', async () => {
    const { container, root } = await mount({
      outputs: [port('report'), port('data')], artifacts: [], connectedPorts: ['report'], onChange: vi.fn(),
    });
    const names = container.querySelectorAll('input[aria-label$="name"]');
    expect((names[0] as HTMLInputElement).disabled).toBe(true);
    expect((names[1] as HTMLInputElement).disabled).toBe(false);
    expect((container.querySelector('button[aria-label="Remove output report"]') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => root.render(
      <TaskOutputsEditor outputs={[port('only')]} artifacts={[]} connectedPorts={[]} onChange={vi.fn()} />,
    ));
    expect((container.querySelector('button[aria-label="Remove output only"]') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => root.unmount());
  });
});
