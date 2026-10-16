import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';

import { apiFetch } from '@/utils/apiFetch';

vi.mock('@/utils/apiFetch', () => ({ apiFetch: vi.fn() }));

import { NodeRunOutputs } from '@/components/runs/NodeRunOutputs';
import { PIPELINE_RUN_STARTED_EVENT, startPipelineRun } from '@/features/runs/pipelineRuns';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const json = (payload: unknown) => new Response(JSON.stringify(payload), { status: 200 });

describe('run options', () => {
  it('sends cache options and announces started runs', async () => {
    const started = vi.fn();
    window.addEventListener(PIPELINE_RUN_STARTED_EVENT, started);
    vi.mocked(apiFetch).mockResolvedValueOnce(json({ run_id: 'run-9', status: 'queued' }));

    await startPipelineRun('key-1', { cache: 'refresh' });

    expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body))).toEqual({ idempotency_key: 'key-1', cache: 'refresh' });
    expect((started.mock.calls[0][0] as CustomEvent).detail).toMatchObject({ run_id: 'run-9' });
    window.removeEventListener(PIPELINE_RUN_STARTED_EVENT, started);
  });
});

describe('NodeRunOutputs', () => {
  it('re-runs the pipeline from the selected step', async () => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(apiFetch).mockResolvedValueOnce(json({ run_id: 'run-10', status: 'queued' }));
    const container = document.createElement('div');
    const root = createRoot(container);
    await act(async () => root.render(<NodeRunOutputs nodeId="split" />));

    const button = [...container.querySelectorAll('button')].find((item) => item.textContent?.includes('Re-run from this step'))!;
    await act(async () => button.dispatchEvent(new MouseEvent('click', { bubbles: true })));

    const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body));
    expect(body).toMatchObject({ cache: 'reuse', rerun_from: 'split' });
    expect(container.textContent).toContain('Run started');
    await act(async () => root.unmount());
  });
});
