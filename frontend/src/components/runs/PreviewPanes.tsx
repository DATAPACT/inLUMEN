import { useMemo, useState } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';

import { Input } from '@/components/ui/input';
import type { RunOutputPreviewColumn } from '@/features/runs/pipelineRuns';

const compareCells = (type: string, left = '', right = '') => {
  if (type === 'integer' || type === 'number') {
    const delta = Number(left) - Number(right);
    if (!Number.isNaN(delta)) return delta;
  }
  return left.localeCompare(right, undefined, { numeric: true });
};

export const TablePreview = ({
  columns,
  rows,
}: {
  columns: RunOutputPreviewColumn[];
  rows: string[][];
}) => {
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState<{ index: number; direction: 1 | -1 } | null>(null);
  const visibleRows = useMemo(() => {
    const query = filter.trim().toLocaleLowerCase();
    const filtered = query
      ? rows.filter((row) => row.some((cell) => cell.toLocaleLowerCase().includes(query)))
      : rows;
    if (!sort) return filtered;
    const type = columns[sort.index]?.type || 'string';
    return [...filtered].sort(
      (left, right) => sort.direction * compareCells(type, left[sort.index], right[sort.index]),
    );
  }, [columns, filter, rows, sort]);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <Input
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
        placeholder="Filter loaded rows"
        aria-label="Filter loaded rows"
        className="h-8 max-w-xs text-xs"
      />
      <div className="min-h-0 flex-1 overflow-auto rounded-md border">
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 bg-muted">
            <tr>
              <th className="w-10 px-2 py-1.5 text-right font-normal text-muted-foreground">#</th>
              {columns.map((column, index) => (
                <th key={`${column.name}-${index}`} className="whitespace-nowrap px-2 py-1.5 text-left">
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 font-medium hover:text-primary"
                    onClick={() => setSort(
                      sort?.index === index
                        ? (sort.direction === 1 ? { index, direction: -1 } : null)
                        : { index, direction: 1 },
                    )}
                  >
                    {column.name}
                    {sort?.index === index && (sort.direction === 1
                      ? <ArrowUp className="h-3 w-3" />
                      : <ArrowDown className="h-3 w-3" />)}
                  </button>
                  <div className="text-[10px] font-normal text-muted-foreground">{column.type}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-t border-border/60 odd:bg-muted/20">
                <td className="px-2 py-1 text-right text-muted-foreground">{rowIndex + 1}</td>
                {columns.map((column, index) => (
                  <td key={`${column.name}-${index}`} className="max-w-[24rem] truncate px-2 py-1" title={row[index]}>
                    {row[index] ?? ''}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="text-xs text-muted-foreground">
        {filter ? `${visibleRows.length} of ${rows.length} loaded rows match` : `${rows.length} rows loaded`}
      </div>
    </div>
  );
};

export const TextPreview = ({ text }: { text: string }) => {
  const lines = text.endsWith('\n') ? text.slice(0, -1).split('\n') : text.split('\n');
  return (
    <div className="min-h-0 flex-1 overflow-auto rounded-md border bg-muted/30">
      <pre className="text-xs leading-5">
        {lines.map((line, index) => (
          <div key={index} className="flex">
            <span className="w-12 shrink-0 select-none pr-3 text-right text-muted-foreground">{index + 1}</span>
            <code className="whitespace-pre-wrap break-all pr-3">{line}</code>
          </div>
        ))}
      </pre>
    </div>
  );
};
