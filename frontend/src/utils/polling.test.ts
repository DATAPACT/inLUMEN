import { afterEach, describe, expect, it, vi } from 'vitest';
import { startPolling } from './polling';

afterEach(() => vi.useRealTimers());

describe('visibility-aware polling', () => {
  it('never overlaps requests and wakes once when visibility changes during a request', async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    let release!: () => void;
    const poll = vi.fn(() => new Promise<void>(resolve => { release = resolve; }));
    const stop = startPolling(poll);
    document.dispatchEvent(new Event('visibilitychange'));
    document.dispatchEvent(new Event('visibilitychange'));
    expect(poll).toHaveBeenCalledTimes(1);
    release(); await vi.advanceTimersByTimeAsync(0);
    expect(poll).toHaveBeenCalledTimes(2);
    stop(); release(); await vi.advanceTimersByTimeAsync(60000);
    expect(poll).toHaveBeenCalledTimes(2);
  });
  it('slows hidden tabs and backs off failures while preserving a retry', async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    const poll = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined);
    const stop = startPolling(poll);
    await vi.advanceTimersByTimeAsync(29999);
    expect(poll).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(poll).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(15000);
    expect(poll).toHaveBeenCalledTimes(3);
    stop();
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  });
});
