import { describe, expect, it } from 'vitest';

import {
  collapsedSummary,
  findRecordArrays,
  fitsInline,
  jsonPath,
  recordsToTable,
  valueAt,
} from '@/features/runs/jsonView';

describe('jsonPath', () => {
  it('builds dotted paths and quotes keys that are not identifiers', () => {
    expect(jsonPath([])).toBe('$');
    expect(jsonPath(['annotations', 3, 'label'])).toBe('$.annotations[3].label');
    expect(jsonPath(['first name', '0'])).toBe('$["first name"]["0"]');
  });

  it('finds the value at a path', () => {
    expect(valueAt({ a: [{ b: 7 }] }, ['a', 0, 'b'])).toBe(7);
    expect(valueAt({ a: 1 }, ['a', 'missing'])).toBeUndefined();
  });
});

describe('fitsInline', () => {
  it('keeps arrays of scalars and short flat objects on one line', () => {
    expect(fitsInline(['person', 'location', 'org'])).toBe(true);
    expect(fitsInline(Array.from({ length: 150 }, (_, index) => index))).toBe(true);
    expect(fitsInline({ start: 12, end: 18, label: 'Oslo' })).toBe(true);
    expect(fitsInline([])).toBe(true);
  });

  it('leaves nested, long or very large containers in tree layout', () => {
    expect(fitsInline({ span: { start: 1 } })).toBe(false);
    expect(fitsInline([[1, 2]])).toBe(false);
    expect(fitsInline({ text: 'x'.repeat(120) })).toBe(false);
    expect(fitsInline(['x'.repeat(250)])).toBe(false);
    expect(fitsInline(Array.from({ length: 201 }, (_, index) => index))).toBe(false);
  });
});

describe('collapsedSummary', () => {
  it('previews keys and array contents', () => {
    expect(collapsedSummary({ id: 1, label: 'a', score: 2, extra: 3 })).toBe('{ id, label, score, … }');
    expect(collapsedSummary({ id: 1 })).toBe('{ id }');
    expect(collapsedSummary([{ id: 1, label: 'a' }, { id: 2 }])).toBe('[2 × { id, label }]');
    expect(collapsedSummary([1, 2, 3])).toBe('[3 × number]');
    expect(collapsedSummary([1, 'a'])).toBe('[2 items]');
    expect(collapsedSummary([])).toBe('[]');
  });
});

describe('table detection', () => {
  it('finds arrays of objects, root first, without descending into them', () => {
    const value = {
      text: 'hello',
      annotations: [{ label: 'a', spans: [{ start: 1 }] }],
      meta: { pages: [{ n: 1 }], tags: ['x'] },
    };
    expect(findRecordArrays(value)).toEqual([['annotations'], ['meta', 'pages']]);
    expect(findRecordArrays([{ a: 1 }])).toEqual([[]]);
    expect(findRecordArrays([1, 2])).toEqual([]);
  });

  it('turns records into columns with inferred types', () => {
    const table = recordsToTable([
      { id: 1, label: 'Oslo', span: { start: 0 } },
      { id: 2, score: 0.5 },
    ]);
    expect(table.columns).toEqual([
      { name: 'id', type: 'integer' },
      { name: 'label', type: 'string' },
      { name: 'span', type: 'string' },
      { name: 'score', type: 'number' },
    ]);
    expect(table.rows).toEqual([
      ['1', 'Oslo', '{"start":0}', ''],
      ['2', '', '', '0.5'],
    ]);
  });
});
