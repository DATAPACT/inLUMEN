import { describe, expect, it } from 'vitest';
import { codeZipEntries, codeZipFolder } from './codeZip';
import type { Node } from 'reactflow';
const node = (id: string, label: string, files: unknown[], type = 'task'): Node => ({
  id, position: { x: 0, y: 0 }, data: { type, label, files },
});
describe('code ZIP', () => {
  it('exports Task code and legacy scripts but excludes input data', () => {
    const entries = codeZipEntries([
      node('1', 'Task', [{ filename: 'main.py', role: 'code' }, { filename: 'input.json', role: 'data' }, 'requirements.txt']),
      node('2', 'Source', [{ filename: 'main.py', role: 'code' }], 'source'),
    ]);
    expect(entries.map(entry => entry.path)).toEqual(['Task--1/main.py', 'Task--1/requirements.txt']);
  });
  it('keeps duplicate Task labels separate and escapes path separators', () => {
    expect(codeZipFolder(node('1', 'Same/label', []))).toBe('Same-label--1');
    expect(codeZipFolder(node('2', 'Same/label', []))).toBe('Same-label--2');
    expect(codeZipFolder(node('../3', 'Task', []))).not.toContain('/');
  });
  it('rejects unsafe attachment paths instead of writing outside the Task folder', () => {
    expect(() => codeZipEntries([node('1', 'Task', [{ filename: '../main.py', role: 'code' }])])).toThrow('invalid filename');
  });
});
