import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from '@/utils/apiFetch';

vi.mock('@/utils/apiFetch', () => ({ apiFetch: vi.fn() }));

import { JsonTree, RunOutputViewer, TablePreview } from '@/components/runs/RunOutputViewer';
import { presentRunOutputs } from '@/features/runs/runPresentation';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mount = async (element: React.ReactElement) => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  return {
    container,
    unmount: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
};

const jsonResponse = (payload: unknown) => new Response(JSON.stringify(payload), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
});

afterEach(() => {
  vi.mocked(apiFetch).mockReset();
  document.body.innerHTML = '';
});

describe('TablePreview', () => {
  it('sorts numerically and filters loaded rows', async () => {
    const view = await mount(
      <TablePreview
        columns={[{ name: 'city', type: 'string' }, { name: 'temp', type: 'integer' }]}
        rows={[['Oslo', '12'], ['Rome', '9'], ['Bergen', '100']]}
      />,
    );
    const cells = () => [...view.container.querySelectorAll('tbody tr')].map((row) => row.children[1].textContent);
    const tempHeader = [...view.container.querySelectorAll('th button')].find((button) => button.textContent?.includes('temp'))!;

    await act(async () => tempHeader.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(cells()).toEqual(['Rome', 'Oslo', 'Bergen']);

    const filter = view.container.querySelector('input')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
      setter.call(filter, 'berg');
      filter.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(cells()).toEqual(['Bergen']);
    expect(view.container.textContent).toContain('1 of 3 loaded rows match');
    await view.unmount();
  });
});

describe('JsonTree', () => {
  it('expands to matches and highlights them', async () => {
    const value = { a: { b: { c: { needle: 'found it' } } }, other: 1 };
    const view = await mount(<JsonTree value={value} query="needle" />);
    expect(view.container.querySelector('mark')?.textContent).toBe('needle');
    await view.unmount();

    const collapsed = await mount(<JsonTree value={value} />);
    expect(collapsed.container.textContent).not.toContain('found it');
    await collapsed.unmount();
  });
});

describe('RunOutputViewer', () => {
  it('previews the chosen output, loads more rows, and switches outputs', async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(jsonResponse({
        kind: 'table', path: 'outputs/n2/result.csv', filename: 'result.csv', content_type: 'text/csv',
        size_bytes: 40, columns: [{ name: 'x', type: 'integer' }], rows: [['1']], has_more: true, next_offset: 4,
      }))
      .mockResolvedValueOnce(jsonResponse({
        kind: 'table', path: 'outputs/n2/result.csv', filename: 'result.csv', content_type: 'text/csv',
        size_bytes: 40, columns: [{ name: 'x', type: 'integer' }], rows: [['2']], has_more: false, next_offset: null,
      }))
      .mockResolvedValueOnce(jsonResponse({
        kind: 'json', path: 'outputs/n1/meta.json', filename: 'meta.json', content_type: 'application/json',
        size_bytes: 11, value: { ok: true },
      }));
    const outputs = presentRunOutputs([
      { path: 'outputs/n1/meta.json', filename: 'meta.json', source_node: 'n1', node_label: 'Load', output_role: 'intermediate' },
      { path: 'outputs/n2/result.csv', filename: 'result.csv', source_node: 'n2', node_label: 'Report', output_role: 'final' },
    ]).all;

    const view = await mount(
      <RunOutputViewer open onOpenChange={vi.fn()} runId="run/1" outputs={outputs} initialPath="outputs/n2/result.csv" />,
    );
    await act(async () => {});

    expect(String(vi.mocked(apiFetch).mock.calls[0][0])).toContain('/api/pipeline-runs/run%2F1/preview/outputs/n2/result.csv');
    const rows = () => [...document.body.querySelectorAll('tbody tr')].map((row) => row.children[1].textContent);
    expect(rows()).toEqual(['1']);

    const loadMore = [...document.body.querySelectorAll('button')].find((button) => button.textContent?.includes('Load more'))!;
    await act(async () => loadMore.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain('offset=4');
    expect(rows()).toEqual(['1', '2']);

    const sidebar = document.body.querySelector('aside')!;
    expect(sidebar.textContent?.indexOf('Report')).toBeLessThan(sidebar.textContent?.indexOf('Load') ?? -1);
    const metaButton = [...sidebar.querySelectorAll('button')].find((button) => button.textContent?.includes('meta.json'))!;
    await act(async () => metaButton.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    await act(async () => {});
    expect(document.body.textContent).toContain('ok');
    expect(document.body.textContent).toContain('true');
    await view.unmount();
  });
});
