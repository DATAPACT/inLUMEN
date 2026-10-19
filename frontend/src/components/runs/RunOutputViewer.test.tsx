import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from '@/utils/apiFetch';

vi.mock('@/utils/apiFetch', () => ({ apiFetch: vi.fn() }));

import { JsonOutputView, JsonTree } from '@/components/runs/JsonOutputView';
import { TablePreview } from '@/components/runs/PreviewPanes';
import { RunOutputViewer } from '@/components/runs/RunOutputViewer';
import { presentRunOutputs } from '@/features/runs/runPresentation';
import { installTestStorage } from '@/test-utils/storage';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mount = async (element: React.ReactElement) => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  return {
    container,
    rerender: async (next: React.ReactElement) => {
      await act(async () => root.render(next));
    },
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


describe('RunOutputViewer selection', () => {
  it('keeps the chosen output when the run list refreshes', async () => {
    vi.mocked(apiFetch).mockImplementation(async (url) => jsonResponse(
      String(url).includes('b.txt')
        ? { kind: 'text', path: 'outputs/n/b.txt', filename: 'b.txt', content_type: 'text/plain', size_bytes: 1, text: 'B' }
        : { kind: 'text', path: 'outputs/n/a.txt', filename: 'a.txt', content_type: 'text/plain', size_bytes: 1, text: 'A' },
    ));
    const make = () => presentRunOutputs([
      { path: 'outputs/n/a.txt', filename: 'a.txt' },
      { path: 'outputs/n/b.txt', filename: 'b.txt' },
    ]).all;
    const view = await mount(<RunOutputViewer open onOpenChange={vi.fn()} runId="r" outputs={make()} />);
    await act(async () => {});
    const second = [...document.body.querySelectorAll('aside button')].find((button) => button.textContent?.includes('b.txt'))!;
    await act(async () => second.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    await act(async () => {});

    // A background refresh passes an equal but new outputs array.
    await view.rerender(<RunOutputViewer open onOpenChange={vi.fn()} runId="r" outputs={make()} />);
    await act(async () => {});
    expect(document.body.querySelector('[role="dialog"] h2')?.textContent).toBe('b.txt');
    await view.unmount();
  });
});

const click = async (element: Element) => {
  await act(async () => element.dispatchEvent(new MouseEvent('click', { bubbles: true })));
};

const typeInto = async (input: HTMLInputElement, value: string) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};

const buttonNamed = (root: ParentNode, name: string) =>
  [...root.querySelectorAll('button')].find((button) =>
    button.getAttribute('aria-label') === name || button.textContent?.trim() === name)!;

describe('JsonOutputView', () => {
  const writeText = vi.fn(async () => {});
  const sample = {
    tags: ['person', 'place'],
    span: { start: 1, end: 5 },
    note: 'n'.repeat(300),
    annotations: [{ id: 1, label: 'Oslo' }, { id: 2, label: 'Bergen' }],
  };

  beforeEach(() => {
    installTestStorage();
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  });

  it('shows flat values on one line and previews collapsed nodes', async () => {
    const view = await mount(<JsonOutputView value={sample} />);
    const text = () => view.container.textContent ?? '';
    expect(text()).toContain('tags: ["person", "place"]');
    expect(text()).toContain('span: { start: 1, end: 5 }');
    expect(text()).toContain(`note: "${'n'.repeat(199)}…show more`);
    await click(buttonNamed(view.container, 'show more'));
    expect(text()).toContain('n'.repeat(300));

    await click(buttonNamed(view.container, 'Collapse all'));
    expect(text()).toContain('annotations: [2 × { id, label }]');
    await click(buttonNamed(view.container, 'Expand all'));
    expect(text()).toContain('{ id: 2, label: "Bergen" }');
    await view.unmount();
  });

  it('switches views and remembers the choice', async () => {
    const view = await mount(<JsonOutputView value={sample} />);
    await click(buttonNamed(view.container, 'Tree'));
    expect(view.container.textContent).toContain('tags: [2]');
    await view.unmount();

    const again = await mount(<JsonOutputView value={sample} />);
    expect(buttonNamed(again.container, 'Tree').getAttribute('aria-pressed')).toBe('true');
    await click(buttonNamed(again.container, 'Table'));
    const cells = [...again.container.querySelectorAll('tbody tr')].map((row) => row.children[2].textContent);
    expect(cells).toEqual(['Oslo', 'Bergen']);

    await click(buttonNamed(again.container, 'Raw'));
    expect(again.container.textContent).toContain('"tags": [');
    const minified = again.container.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    await click(minified);
    expect(again.container.textContent).toContain('{"tags":["person","place"]');
    await again.unmount();
  });

  it('hides the table view when there is no array of objects', async () => {
    window.localStorage.setItem('inlumen-run-output-json-view', 'table');
    const view = await mount(<JsonOutputView value={{ a: 1 }} />);
    expect(buttonNamed(view.container, 'Table')).toBeUndefined();
    expect(buttonNamed(view.container, 'Compact').getAttribute('aria-pressed')).toBe('true');
    await view.unmount();
  });

  it('counts matches and moves between them', async () => {
    const view = await mount(<JsonOutputView value={sample} />);
    const search = view.container.querySelector<HTMLInputElement>('input[aria-label="Search keys and values"]')!;
    await typeInto(search, 'o');
    const current = () => view.container.querySelector('mark[data-current="true"]')?.parentElement?.textContent;
    expect(view.container.textContent).toContain('1 of 4');
    expect(current()).toBe('"person"');

    await click(buttonNamed(view.container, 'Next match'));
    expect(view.container.textContent).toContain('2 of 4');
    await act(async () => search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true })));
    await act(async () => search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true })));
    expect(view.container.textContent).toContain('4 of 4');

    await typeInto(search, 'zzz');
    expect(view.container.textContent).toContain('No matches');
    await view.unmount();
  });

  it('copies values and paths, and opens a nested array as a table', async () => {
    const value = { result: { pages: [{ n: 1, words: [{ w: 'hi' }, { w: 'there' }] }] } };
    const view = await mount(<JsonOutputView value={value} />);
    await click(buttonNamed(view.container, 'Expand all'));
    await click(buttonNamed(view.container, 'Copy path $.result.pages[0].words[1]'));
    expect(writeText).toHaveBeenLastCalledWith('$.result.pages[0].words[1]');
    await click(buttonNamed(view.container, 'Copy value of $.result.pages[0].words[1]'));
    expect(writeText).toHaveBeenLastCalledWith('{\n  "w": "there"\n}');

    await click(buttonNamed(view.container, 'Show $.result.pages[0].words as a table'));
    const select = view.container.querySelector<HTMLSelectElement>('select[aria-label="Array shown as a table"]')!;
    expect(select.value).toBe('$.result.pages[0].words');
    expect([...view.container.querySelectorAll('tbody tr')].map((row) => row.children[1].textContent)).toEqual(['hi', 'there']);
    await view.unmount();
  });
});

describe('RunOutputViewer copy', () => {
  it('copies a whole text output', async () => {
    // jsdom's Blob has no text(); browsers do.
    const fileResponse = new Response('', { status: 200 });
    fileResponse.blob = async () => ({ text: async () => '{"a": 1}\n' }) as Blob;
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(jsonResponse({
        kind: 'json', path: 'outputs/n/a.json', filename: 'a.json', content_type: 'application/json',
        size_bytes: 9, value: { a: 1 },
      }))
      .mockResolvedValueOnce(fileResponse);
    const outputs = presentRunOutputs([{ path: 'outputs/n/a.json', filename: 'a.json' }]).all;
    const view = await mount(<RunOutputViewer open onOpenChange={vi.fn()} runId="r" outputs={outputs} />);
    await act(async () => {});
    await click(buttonNamed(document.body, 'Copy'));
    for (let tick = 0; tick < 5; tick += 1) await act(async () => {});
    expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain('disposition=inline');
    expect(writeText).toHaveBeenCalledWith('{"a": 1}\n');
    await view.unmount();
  });
});
