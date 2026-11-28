// Logic of the tour that does not need React: remembering first use,
// finding the element of a step, and placing the pop-up next to it.

import type { TourStep } from './tourSteps';

/** Set in the browser's localStorage once the tour was finished or skipped. */
export const TOUR_SEEN_KEY = 'inlumen-tutorial-seen';

/**
 * True when the tour was finished or skipped in this browser before.
 * Also true when the browser storage cannot be read: without storage the tour
 * could not be remembered and would open on every visit.
 */
export function hasSeenTour(): boolean {
  try {
    return window.localStorage.getItem(TOUR_SEEN_KEY) !== null;
  } catch {
    return true;
  }
}

export function markTourSeen(): void {
  try {
    window.localStorage.setItem(TOUR_SEEN_KEY, new Date().toISOString());
  } catch {
    // Storage may be full or turned off. The tour still closes.
  }
}

/**
 * True when the browser is driven by an automation tool such as Playwright.
 * The tour does not open by itself there, so it cannot cover the controls
 * that browser tests click. It can still be opened from Help.
 */
export function isAutomatedBrowser(): boolean {
  return typeof navigator !== 'undefined' && navigator.webdriver === true;
}

/** The tour opens by itself only on first use in a browser used by a person. */
export function shouldAutoOpenTour(): boolean {
  return !isAutomatedBrowser() && !hasSeenTour();
}

export type Box = { left: number; top: number; width: number; height: number };
export type Size = { width: number; height: number };

const isOnScreen = (box: Box, viewport: Size) =>
  box.width > 0 && box.height > 0
  && box.left < viewport.width && box.top < viewport.height
  && box.left + box.width > 0 && box.top + box.height > 0;

/**
 * The box of the first target of a step that is rendered and on screen,
 * or null when there is none.
 */
export function findTargetBox(step: TourStep, viewport: Size, root: ParentNode = document): Box | null {
  for (const selector of step.targets) {
    const element = root.querySelector(selector);
    if (!element) continue;
    const rect = element.getBoundingClientRect();
    const box = { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
    if (isOnScreen(box, viewport)) return box;
  }
  return null;
}

export type PopoverSide = 'bottom' | 'top' | 'right' | 'left';
export type PopoverPlacement = {
  left: number;
  top: number;
  /** Side of the target the pop-up is on. Null means no pointer is drawn. */
  side: PopoverSide | null;
  /** Distance of the pointer from the left (or top) edge of the pop-up. */
  pointerOffset: number;
};

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, Math.max(min, max)));

/**
 * Places the pop-up below, above, right or left of the target, on the first
 * side with enough room. With no target it is centred in the window. When no
 * side has room (a large target such as the canvas) it is centred on the
 * target. In both cases no pointer is drawn.
 */
export function placePopover(target: Box | null, popover: Size, viewport: Size, gap = 14, margin = 8): PopoverPlacement {
  const maxLeft = viewport.width - popover.width - margin;
  const maxTop = viewport.height - popover.height - margin;
  if (!target) {
    return {
      left: clamp((viewport.width - popover.width) / 2, margin, maxLeft),
      top: clamp((viewport.height - popover.height) / 2, margin, maxTop),
      side: null,
      pointerOffset: 0,
    };
  }
  const centerX = target.left + target.width / 2;
  const centerY = target.top + target.height / 2;
  const right = target.left + target.width;
  const bottom = target.top + target.height;
  const pointerInset = 18;

  const horizontal = (side: 'bottom' | 'top', top: number): PopoverPlacement => {
    const left = clamp(centerX - popover.width / 2, margin, maxLeft);
    return { left, top, side, pointerOffset: clamp(centerX - left, pointerInset, popover.width - pointerInset) };
  };
  const vertical = (side: 'right' | 'left', left: number): PopoverPlacement => {
    const top = clamp(centerY - popover.height / 2, margin, maxTop);
    return { left, top, side, pointerOffset: clamp(centerY - top, pointerInset, popover.height - pointerInset) };
  };

  if (bottom + gap + popover.height <= viewport.height - margin) return horizontal('bottom', bottom + gap);
  if (target.top - gap - popover.height >= margin) return horizontal('top', target.top - gap - popover.height);
  if (right + gap + popover.width <= viewport.width - margin) return vertical('right', right + gap);
  if (target.left - gap - popover.width >= margin) return vertical('left', target.left - gap - popover.width);
  return {
    left: clamp(centerX - popover.width / 2, margin, maxLeft),
    top: clamp(centerY - popover.height / 2, margin, maxTop),
    side: null,
    pointerOffset: 0,
  };
}
