import { describe, expect, it } from 'vitest';

import spec from './portTypes.generated.json';
import {
  artifactPortType,
  checkConnection,
  effectivePortType,
  filePortIds,
  inferOutputTypes,
  sourceOutputTypes,
  type FilesAs,
} from './portTypes';

// Same cases as backend/tests/test_port_types.py, so both sides agree.
describe('port type spec cases', () => {
  it.each(spec.cases.check)('checks $source -> $target', ({ source, target, result }) => {
    const issue = checkConnection(source, target);
    expect(issue ? `${issue.severity}:${issue.code}` : null).toBe(result);
  });

  it.each(spec.cases.artifact)('types artifact as $type', ({ artifact, type }) => {
    expect(artifactPortType(artifact)).toBe(type);
  });

  it('derives file port ids', () => {
    spec.cases.file_port_ids.forEach(({ filenames, ids }) => expect(filePortIds(filenames)).toEqual(ids));
  });

  it.each(spec.cases.source_outputs)('types source outputs ($files_as)', ({ filenames, files_as, ports, types }) => {
    expect(sourceOutputTypes(filenames, files_as as FilesAs, ports)).toEqual(types);
  });

  it.each(spec.cases.effective)('effective type of $declared with $inferred', ({ declared, inferred, type }) => {
    expect(effectivePortType(declared, inferred)).toBe(type);
  });
});

describe('inferOutputTypes', () => {
  it('prefers attached files for a Source over a stale generated contract', () => {
    expect(inferOutputTypes('source', {
      files: [{ filename: 'a.csv' }, { filename: 'b.csv' }, { filename: 'main.py', role: 'code' }],
      generated_artifact: { data_contract: { outputs: [{ filename: 'x.json' }] } },
    }, ['data'])).toEqual({ data: 'table[]' });
  });

  it('reads a Task generated contract', () => {
    expect(inferOutputTypes('task', {
      generated_artifact: { data_contract: { outputs: [{ kind: 'json', filename: 'r.json' }] } },
    }, ['output'])).toEqual({ output: 'json' });
  });
});
