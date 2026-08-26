import { beforeEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from '@/utils/apiFetch';
import {
  cancelPipelineRun,
  isActivePipelineRun,
  listPipelineRuns,
  startPipelineRun,
} from '@/features/runs/pipelineRuns';


vi.mock('@/utils/apiFetch', () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

describe('pipeline run API', () => {
  beforeEach(() => mockedFetch.mockReset());

  it('submits an idempotency key and accepts a background run', async () => {
    mockedFetch.mockResolvedValue(new Response(JSON.stringify({
      schema_version: 'inlumen.pipeline-run@1',
      run_id: 'run-1',
      status: 'queued',
    }), { status: 202, headers: { 'Content-Type': 'application/json' } }));

    const run = await startPipelineRun('request-1');

    expect(run.run_id).toBe('run-1');
    expect(mockedFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/pipeline-runs'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ idempotency_key: 'request-1' }),
      }),
    );
  });

  it('restores recent runs and cancels by stable run id', async () => {
    mockedFetch
      .mockResolvedValueOnce(new Response(JSON.stringify({ runs: [{ run_id: 'run-1' }] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ run_id: 'run-1', status: 'cancelling' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }));

    expect(await listPipelineRuns()).toHaveLength(1);
    expect((await cancelPipelineRun('run-1')).status).toBe('cancelling');
    expect(mockedFetch).toHaveBeenLastCalledWith(
      expect.stringContaining('/api/pipeline-runs/run-1'),
      { method: 'DELETE' },
    );
  });

  it('distinguishes active and terminal lifecycle states', () => {
    expect(isActivePipelineRun('running')).toBe(true);
    expect(isActivePipelineRun('cancelling')).toBe(true);
    expect(isActivePipelineRun('succeeded')).toBe(false);
    expect(isActivePipelineRun('failed')).toBe(false);
  });
});

