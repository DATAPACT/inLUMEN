/** Output artifacts a Task publishes, one per output port. */

/** Node parameter holding the declarations; codegen reads it, steps never receive it. */
export const OUTPUT_ARTIFACTS_PARAMETER = 'output_artifacts';
export type OutputArtifactDeclaration = {
  name: string;
  port: string;
  filename: string;
  kind?: string;
  format?: string;
};

const KIND_BY_EXTENSION: Record<string, { kind: string; format: string }> = {
  json: { kind: 'json', format: 'json' },
  csv: { kind: 'table', format: 'csv' },
  tsv: { kind: 'table', format: 'tsv' },
  parquet: { kind: 'table', format: 'parquet' },
  txt: { kind: 'text', format: 'txt' },
  md: { kind: 'text', format: 'md' },
  png: { kind: 'image', format: 'png' },
  jpg: { kind: 'image', format: 'jpg' },
  pdf: { kind: 'document', format: 'pdf' },
};

const PORT_ID_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;

export const portIdError = (id: string, others: string[]) => {
  if (!PORT_ID_PATTERN.test(id)) return 'Use a letter followed by letters, digits, - or _.';
  if (others.includes(id)) return 'Each output needs its own name.';
  return '';
};

export const artifactForPath = (port: string, filename: string): OutputArtifactDeclaration => {
  const extension = filename.split('.').pop()?.toLowerCase() || '';
  return { name: port, port, filename, ...(KIND_BY_EXTENSION[extension] || {}) };
};
