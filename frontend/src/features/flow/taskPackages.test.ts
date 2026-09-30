import { describe, expect, it } from 'vitest';
import { taskPackageFolder, taskDisplayName, taskConnections } from './taskPackages';

describe('readable package identities', () => {
  it.each([
    ['Transcribe audio', '2', 'nodes/Transcribe audio--2'],
    [' Analyze / audio: [raw] ', 'branch--a/b', 'nodes/Analyze - audio- -raw--branch%2D%2Da%2Fb'],
    ['', '2', 'nodes/Task--2'],
    ['A'.repeat(100), '2', `nodes/${'A'.repeat(64)}--2`],
  ])('formats %s consistently with the ZIP exporter', (label, id, expected) => {
    expect(taskPackageFolder(id, label)).toBe(expected);
  });

  it('distinguishes parallel tasks with repeated labels and shows both branches', () => {
    const nodes = [
      { id: '1', data: { label: 'Audio' } },
      { id: '2', data: { label: 'Analyze' } },
      { id: '3', data: { label: 'Analyze' } },
      { id: '4', data: { label: 'Combine' } },
    ];
    const edges = [{ source: '1', target: '3' }, { source: '1', target: '2' }, { source: '3', target: '4' }, { source: '2', target: '4' }];
    expect(taskDisplayName(nodes[1], nodes)).toBe('Analyze (ID: 2)');
    expect(taskDisplayName(nodes[2], nodes)).toBe('Analyze (ID: 3)');
    expect(taskConnections('1', nodes, edges).outgoing).toEqual(['Analyze (ID: 3)', 'Analyze (ID: 2)']);
    expect(taskConnections('2', nodes, edges)).toEqual({ incoming: ['Audio'], outgoing: ['Combine'] });
    expect(taskConnections('4', nodes, edges).incoming).toEqual(['Analyze (ID: 3)', 'Analyze (ID: 2)']);
  });
});
