// One timer and one request at a time, including rapid visibility changes.
export function startPolling(poll: () => Promise<number | void>, interval = 3000, hiddenInterval = 15000) {
  let stopped = false, running = false, wakeRequested = false, failures = 0;
  let timer: number | undefined;
  const tick = async () => {
    if (stopped) return;
    if (running) { wakeRequested = true; return; }
    running = true;
    let next: number | void = undefined;
    try { next = await poll(); failures = 0; }
    catch { failures = Math.min(failures + 1, 3); }
    finally {
      running = false;
      if (!stopped) {
        const delay = wakeRequested ? 0 : next ?? (document.hidden ? hiddenInterval : interval) * 2 ** failures;
        wakeRequested = false;
        timer = window.setTimeout(() => { void tick(); }, delay);
      }
    }
  };
  const visible = () => {
    if (document.hidden || stopped) return;
    window.clearTimeout(timer);
    void tick();
  };
  document.addEventListener('visibilitychange', visible);
  void tick();
  return () => { stopped = true; window.clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
}
