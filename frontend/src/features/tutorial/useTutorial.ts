import { useCallback, useState } from 'react';
import { markTourSeen, shouldAutoOpenTour } from './tourState';

/**
 * Open state of the tour. It starts open on first use. Closing it (finished
 * or skipped) is remembered, so it does not open by itself again. `start`
 * opens it again, for example from Help.
 */
export function useTutorial() {
  const [open, setOpen] = useState(shouldAutoOpenTour);
  const start = useCallback(() => setOpen(true), []);
  const close = useCallback(() => {
    markTourSeen();
    setOpen(false);
  }, []);
  return { open, start, close };
}
