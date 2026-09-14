import type { Node } from 'reactflow';
import { getNodeFileName, normalizeType, type NodeFileReference } from '@/features/nodes/nodeSchema';

// Keep folders readable and unique, even when two Tasks have the same label.
export const codeZipFolder = (node: Node) => {
  const label = [...String(node.data?.label || 'Task')]
    .map(char => char.charCodeAt(0) < 32 || /[\\/:*?"<>|]/.test(char) ? '-' : char).join('').trim() || 'Task';
  return `${label}--${encodeURIComponent(node.id)}`;
};

export function codeZipEntries(nodes: Node[]) {
  const entries: Array<{ nodeId: string; file: NodeFileReference; path: string }> = [];
  const paths = new Set<string>();
  for (const node of nodes) {
    if (normalizeType(node.data?.type) !== 'task') continue;
    const files: NodeFileReference[] = node.data?.file_buckets ?? node.data?.files ?? [];
    for (const file of files) {
      const filename = getNodeFileName(file);
      const role = typeof file === 'object' && 'role' in file ? file.role : undefined;
      const legacyCode = /^(requirements\.txt|[\w.-]+\.(py|pyi|json|toml|ya?ml|sql|sh))$/i.test(filename);
      if (role === 'data' || (role !== 'code' && !legacyCode)) continue;
      if (!filename || /[\\/]/.test(filename) || [...filename].some(char => char.charCodeAt(0) < 32) || filename === '.' || filename === '..') {
        throw new Error('A code attachment has an invalid filename. Rename it before downloading.');
      }
      const path = `${codeZipFolder(node)}/${filename}`;
      if (paths.has(path)) throw new Error(`Duplicate code attachment: ${path}`);
      paths.add(path);
      entries.push({ nodeId: node.id, file, path });
    }
  }
  return entries;
}
