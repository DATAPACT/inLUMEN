import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Button } from '@/components/ui/button';
import { TOUR_STEPS, type TourStep } from './tourSteps';
import { findTargetBox, placePopover, type Box, type PopoverSide, type Size } from './tourState';

type TutorialProps = {
  open: boolean;
  /** Called when the tour is finished or skipped. */
  onClose: (reason: 'finished' | 'skipped') => void;
  steps?: TourStep[];
};

const HIGHLIGHT_PADDING = 4;
// Other pop-ups that manage focus themselves. Focus inside them is left alone.
const OTHER_LAYERS = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]';

const viewportSize = (): Size => ({ width: window.innerWidth, height: window.innerHeight });
const sameBox = (a: Box | null, b: Box | null) =>
  a === b || (!!a && !!b && a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height);

const pointerStyle = (side: PopoverSide, offset: number): React.CSSProperties => {
  const half = 6;
  const base: React.CSSProperties = { position: 'absolute', width: half * 2, height: half * 2, transform: 'rotate(45deg)' };
  // The pointer sits on the edge of the pop-up that faces the target.
  if (side === 'bottom') return { ...base, top: -half - 1, left: offset - half, borderRightWidth: 0, borderBottomWidth: 0 };
  if (side === 'top') return { ...base, bottom: -half - 1, left: offset - half, borderLeftWidth: 0, borderTopWidth: 0 };
  if (side === 'right') return { ...base, left: -half - 1, top: offset - half, borderRightWidth: 0, borderTopWidth: 0 };
  return { ...base, right: -half - 1, top: offset - half, borderLeftWidth: 0, borderBottomWidth: 0 };
};

/**
 * The tour: a dimmed page, a frame around the element of the current step,
 * and a pop-up with the text and Back, Next and Skip.
 */
export function Tutorial({ open, onClose, steps = TOUR_STEPS }: TutorialProps) {
  const [index, setIndex] = useState(0);
  const [target, setTarget] = useState<Box | null>(null);
  const [viewport, setViewport] = useState<Size>(viewportSize);
  const [popoverSize, setPopoverSize] = useState<Size>({ width: 0, height: 0 });
  const popoverRef = useRef<HTMLDivElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const step = steps[Math.min(index, steps.length - 1)];
  const isLast = index >= steps.length - 1;

  // Start at the first step each time the tour opens, and give the focus
  // back to where it was when the tour closes.
  useLayoutEffect(() => {
    if (!open) return;
    setIndex(0);
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      const previous = previousFocusRef.current;
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);

  const measure = useCallback(() => {
    const size = viewportSize();
    setViewport(current => (current.width === size.width && current.height === size.height ? current : size));
    const box = step ? findTargetBox(step, size) : null;
    setTarget(current => (sameBox(current, box) ? current : box));
  }, [step]);

  // Follow the target: panels load late, and can be resized or closed.
  useLayoutEffect(() => {
    if (!open) return;
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    const timer = window.setInterval(measure, 300);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
      window.clearInterval(timer);
    };
  }, [open, measure]);

  useLayoutEffect(() => {
    const element = popoverRef.current;
    if (!open || !element) return;
    const size = { width: element.offsetWidth, height: element.offsetHeight };
    setPopoverSize(current => (current.width === size.width && current.height === size.height ? current : size));
  }, [open, index, viewport]);

  const skip = useCallback(() => onClose('skipped'), [onClose]);
  const next = useCallback(() => {
    if (isLast) onClose('finished');
    else setIndex(current => current + 1);
  }, [isLast, onClose]);
  const back = useCallback(() => setIndex(current => Math.max(0, current - 1)), []);

  useEffect(() => {
    if (open) nextRef.current?.focus();
  }, [open, index]);

  // Keyboard: Escape skips, Tab stays inside the pop-up.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const popover = popoverRef.current;
      if (!popover) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        skip();
        return;
      }
      if (event.key !== 'Tab') return;
      const buttons = Array.from(popover.querySelectorAll<HTMLButtonElement>('button:not([disabled])'));
      if (buttons.length === 0) return;
      const position = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const nextPosition = position === -1
        ? 0
        : (position + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length;
      event.preventDefault();
      buttons[nextPosition].focus();
    };
    const onFocusIn = (event: FocusEvent) => {
      const popover = popoverRef.current;
      const focused = event.target;
      if (!popover || !(focused instanceof Element) || popover.contains(focused)) return;
      if (focused.closest(OTHER_LAYERS)) return;
      nextRef.current?.focus();
    };
    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [open, skip]);

  if (!open || !step) return null;

  const placement = placePopover(target, popoverSize, viewport);
  const dim = 'rgb(0 0 0 / 0.55)';

  return createPortal(
    <div data-tour-overlay="" style={{ position: 'fixed', inset: 0, zIndex: 100, pointerEvents: 'auto' }}>
      {target ? (
        <div
          data-tour-highlight=""
          aria-hidden="true"
          className="rounded-lg ring-2 ring-ring transition-all duration-200 motion-reduce:transition-none"
          style={{
            position: 'fixed',
            left: target.left - HIGHLIGHT_PADDING,
            top: target.top - HIGHLIGHT_PADDING,
            width: target.width + HIGHLIGHT_PADDING * 2,
            height: target.height + HIGHLIGHT_PADDING * 2,
            // A very large shadow dims everything outside the frame.
            boxShadow: `0 0 0 9999px ${dim}`,
          }}
        />
      ) : (
        <div aria-hidden="true" style={{ position: 'fixed', inset: 0, background: dim }} />
      )}
      <div
        ref={popoverRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tour-title"
        aria-describedby="tour-text"
        className="w-80 max-w-[calc(100vw-1rem)] rounded-xl border border-border bg-popover p-4 text-popover-foreground shadow-2xl"
        style={{ position: 'fixed', left: placement.left, top: placement.top }}
      >
        {placement.side ? (
          <div
            data-tour-pointer={placement.side}
            aria-hidden="true"
            className="border border-border bg-popover"
            style={pointerStyle(placement.side, placement.pointerOffset)}
          />
        ) : null}
        <h2 id="tour-title" className="text-sm font-semibold">{step.title}</h2>
        <p id="tour-text" className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{step.text}</p>
        <div className="mt-4 flex items-center gap-2">
          <span className="mr-auto text-xs text-muted-foreground" aria-live="polite">
            Step {index + 1} of {steps.length}
          </span>
          <Button type="button" variant="ghost" size="sm" className="h-8 px-2 text-xs" onClick={skip}>
            Skip
          </Button>
          <Button type="button" variant="outline" size="sm" className="h-8 px-3 text-xs" onClick={back} disabled={index === 0}>
            Back
          </Button>
          <Button ref={nextRef} type="button" size="sm" className="h-8 px-3 text-xs" onClick={next}>
            {isLast ? 'Done' : 'Next'}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** The entry in the Help window that starts the tour again. */
export function TutorialHelpEntry({ onStart }: { onStart: () => void }) {
  return (
    <div className="rounded-xl border border-border bg-muted/35 p-3">
      <div className="mb-1 font-medium">Guided tour</div>
      <p>A short walk through the main parts of the workspace. It opens once on first use; start it again here.</p>
      <Button type="button" variant="outline" size="sm" className="mt-2" onClick={onStart}>
        Start the tour
      </Button>
    </div>
  );
}
