import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installTestStorage } from '@/test-utils/storage';
import { Tutorial, TutorialHelpEntry } from './Tutorial';
import { TOUR_STEPS, type TourStep } from './tourSteps';
import {
  TOUR_SEEN_KEY,
  findTargetBox,
  hasSeenTour,
  markTourSeen,
  placePopover,
  shouldAutoOpenTour,
} from './tourState';
import { useTutorial } from './useTutorial';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const viewport = { width: 1024, height: 768 };

const setWebdriver = (value: boolean | undefined) =>
  Object.defineProperty(navigator, 'webdriver', { configurable: true, value });

/** Adds an element with a `data-tour` mark and a fixed place on screen. */
const addTarget = (name: string, box: { left: number; top: number; width: number; height: number }) => {
  const element = document.createElement('button');
  element.setAttribute('data-tour', name);
  element.getBoundingClientRect = () => ({
    ...box, x: box.left, y: box.top, right: box.left + box.width, bottom: box.top + box.height, toJSON: () => box,
  });
  document.body.append(element);
  return element;
};

const steps: TourStep[] = [
  { id: 'one', title: 'First', text: 'No target here.', targets: [] },
  { id: 'two', title: 'Second', text: 'Points at a panel.', targets: ['[data-tour="panel"]', '[data-tour="toggle"]'] },
  { id: 'three', title: 'Third', text: 'Its target is missing.', targets: ['[data-tour="nowhere"]'] },
];

function Harness() {
  const tutorial = useTutorial();
  return (
    <>
      <button id="reopen" onClick={tutorial.start}>Reopen</button>
      <Tutorial open={tutorial.open} onClose={tutorial.close} steps={steps} />
    </>
  );
}

const dialog = () => document.querySelector('[role="dialog"]');
const button = (label: string) =>
  Array.from(document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')).find(item => item.textContent === label)!;
const click = (label: string) => act(async () => button(label).click());
const press = (key: string, shiftKey = false) => act(async () => {
  (document.activeElement ?? document.body).dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey, bubbles: true }));
});

describe('tour state', () => {
  beforeEach(() => { installTestStorage(); setWebdriver(false); });
  afterEach(() => { setWebdriver(undefined); });

  it('opens by itself on first use only', () => {
    expect(hasSeenTour()).toBe(false);
    expect(shouldAutoOpenTour()).toBe(true);
    markTourSeen();
    expect(window.localStorage.getItem(TOUR_SEEN_KEY)).not.toBeNull();
    expect(shouldAutoOpenTour()).toBe(false);
  });

  it('does not open by itself in an automated browser', () => {
    setWebdriver(true);
    expect(hasSeenTour()).toBe(false);
    expect(shouldAutoOpenTour()).toBe(false);
  });

  it('does not open by itself, and does not fail, when storage is not available', () => {
    Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw new Error('blocked'); } });
    expect(shouldAutoOpenTour()).toBe(false);
    expect(() => markTourSeen()).not.toThrow();
  });
});

describe('tour steps', () => {
  it('has six to eight steps with their own ids and data-tour targets', () => {
    expect(TOUR_STEPS.length).toBeGreaterThanOrEqual(6);
    expect(TOUR_STEPS.length).toBeLessThanOrEqual(8);
    expect(new Set(TOUR_STEPS.map(step => step.id)).size).toBe(TOUR_STEPS.length);
    for (const step of TOUR_STEPS) {
      expect(step.title).not.toBe('');
      expect(step.text).not.toBe('');
      for (const target of step.targets) expect(target).toMatch(/^\[data-tour="[a-z-]+"\]$/);
    }
  });
});

describe('tour targets and placement', () => {
  afterEach(() => { document.body.innerHTML = ''; });

  it('uses the first target that is on screen', () => {
    expect(findTargetBox(steps[1], viewport)).toBeNull();
    addTarget('toggle', { left: 10, top: 10, width: 80, height: 30 });
    expect(findTargetBox(steps[1], viewport)).toEqual({ left: 10, top: 10, width: 80, height: 30 });
    addTarget('panel', { left: 0, top: 60, width: 250, height: 600 });
    expect(findTargetBox(steps[1], viewport)?.width).toBe(250);
  });

  it('treats hidden and off-screen targets as missing', () => {
    addTarget('panel', { left: 0, top: 0, width: 0, height: 0 });
    expect(findTargetBox(steps[1], viewport)).toBeNull();
    document.body.innerHTML = '';
    addTarget('panel', { left: 2000, top: 10, width: 100, height: 30 });
    expect(findTargetBox(steps[1], viewport)).toBeNull();
  });

  it('places the pop-up next to the target, inside the window', () => {
    const popover = { width: 320, height: 160 };
    const below = placePopover({ left: 900, top: 10, width: 80, height: 30 }, popover, viewport);
    expect(below.side).toBe('bottom');
    expect(below.top).toBe(54);
    expect(below.left + popover.width).toBeLessThanOrEqual(viewport.width - 8);
    // The pointer still points at the middle of the target.
    expect(below.left + below.pointerOffset).toBeGreaterThan(900);

    expect(placePopover({ left: 400, top: 700, width: 200, height: 40 }, popover, viewport).side).toBe('top');
    expect(placePopover({ left: 0, top: 60, width: 250, height: 700 }, popover, viewport).side).toBe('right');
    expect(placePopover({ left: 760, top: 60, width: 260, height: 700 }, popover, viewport).side).toBe('left');
  });

  it('centres the pop-up without a pointer when there is no target or no room', () => {
    const popover = { width: 320, height: 160 };
    expect(placePopover(null, popover, viewport)).toEqual({ left: 352, top: 304, side: null, pointerOffset: 0 });
    const large = placePopover({ left: 100, top: 60, width: 900, height: 700 }, popover, viewport);
    expect(large.side).toBeNull();
    expect(large.left).toBe(390);
  });
});

describe('tour', () => {
  let container: HTMLDivElement;
  let root: Root;
  const mount = () => act(async () => root.render(<Harness />));

  beforeEach(() => {
    installTestStorage();
    setWebdriver(false);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    document.body.innerHTML = '';
    setWebdriver(undefined);
  });

  it('opens on first use and shows the first step', async () => {
    await mount();
    expect(dialog()?.textContent).toContain('First');
    expect(dialog()?.textContent).toContain('Step 1 of 3');
    expect(button('Back').disabled).toBe(true);
    expect(document.activeElement).toBe(button('Next'));
  });

  it('is remembered when skipped, and can be opened again', async () => {
    await mount();
    await click('Skip');
    expect(dialog()).toBeNull();
    expect(hasSeenTour()).toBe(true);

    await act(async () => root.unmount());
    root = createRoot(container);
    await mount();
    expect(dialog()).toBeNull();

    await act(async () => document.querySelector<HTMLButtonElement>('#reopen')!.click());
    expect(dialog()?.textContent).toContain('Step 1 of 3');
  });

  it('is remembered when finished', async () => {
    await mount();
    await click('Next');
    expect(dialog()?.textContent).toContain('Step 2 of 3');
    await click('Back');
    expect(dialog()?.textContent).toContain('Step 1 of 3');
    await click('Next');
    await click('Next');
    expect(hasSeenTour()).toBe(false);
    await click('Done');
    expect(dialog()).toBeNull();
    expect(hasSeenTour()).toBe(true);
  });

  it('does not open by itself in an automated browser, but opens from Help', async () => {
    setWebdriver(true);
    await mount();
    expect(dialog()).toBeNull();
    await act(async () => document.querySelector<HTMLButtonElement>('#reopen')!.click());
    expect(dialog()).not.toBeNull();
  });

  it('skips with Escape and gives the focus back', async () => {
    window.localStorage.setItem(TOUR_SEEN_KEY, 'yes');
    await mount();
    const reopen = document.querySelector<HTMLButtonElement>('#reopen')!;
    reopen.focus();
    await act(async () => reopen.click());
    expect(document.activeElement).toBe(button('Next'));
    await press('Escape');
    expect(dialog()).toBeNull();
    expect(document.activeElement).toBe(reopen);
  });

  it('keeps the focus inside the pop-up', async () => {
    await mount();
    await press('Tab');
    expect(document.activeElement).toBe(button('Skip'));
    await press('Tab', true);
    expect(document.activeElement).toBe(button('Next'));
    // Focus that lands on the page behind is pulled back.
    await act(async () => document.querySelector<HTMLButtonElement>('#reopen')!.focus());
    expect(document.activeElement).toBe(button('Next'));
  });

  it('frames the target of a step and points at it', async () => {
    addTarget('toggle', { left: 100, top: 10, width: 80, height: 30 });
    await mount();
    expect(document.querySelector('[data-tour-highlight]')).toBeNull();
    await click('Next');
    const highlight = document.querySelector<HTMLElement>('[data-tour-highlight]')!;
    expect(highlight.style.left).toBe('96px');
    expect(highlight.style.width).toBe('88px');
    expect(document.querySelector('[data-tour-pointer]')?.getAttribute('data-tour-pointer')).toBe('bottom');
  });

  it('shows the text without a pointer when the target is missing', async () => {
    addTarget('toggle', { left: 100, top: 10, width: 80, height: 30 });
    await mount();
    await click('Next');
    await click('Next');
    expect(dialog()?.textContent).toContain('Its target is missing.');
    expect(document.querySelector('[data-tour-highlight]')).toBeNull();
    expect(document.querySelector('[data-tour-pointer]')).toBeNull();
  });

  it('follows a target that is removed while the step is shown', async () => {
    vi.useFakeTimers();
    try {
      const target = addTarget('toggle', { left: 100, top: 10, width: 80, height: 30 });
      await mount();
      await click('Next');
      expect(document.querySelector('[data-tour-highlight]')).not.toBeNull();
      target.remove();
      await act(async () => { vi.advanceTimersByTime(400); });
      expect(document.querySelector('[data-tour-highlight]')).toBeNull();
      expect(dialog()?.textContent).toContain('Second');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('help entry', () => {
  it('starts the tour', async () => {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const onStart = vi.fn();
    try {
      await act(async () => root.render(<TutorialHelpEntry onStart={onStart} />));
      await act(async () => container.querySelector('button')!.click());
      expect(onStart).toHaveBeenCalledOnce();
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });
});
