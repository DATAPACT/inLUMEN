import { test, expect, type Locator } from '@playwright/test';
import { installConversationFixture } from './helpers/conversation';

async function colors(element: Locator) {
  return element.evaluate(node => {
    const style = getComputedStyle(node);
    const rgb = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number);
    const luminance = (color: number[]) => color
      .map(value => value / 255)
      .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
      .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    const background = rgb(style.backgroundColor);
    const foreground = rgb(style.color);
    const light = luminance(background);
    const dark = luminance(foreground);
    return {
      brightness: background.reduce((sum, value) => sum + value, 0) / 3,
      contrast: (Math.max(light, dark) + 0.05) / (Math.min(light, dark) + 0.05),
    };
  });
}

for (const theme of ['dark', 'light'] as const) {
  test(`canvas controls and import notifications remain readable in ${theme} mode`, async ({ page }) => {
    await page.route('**/api/**', route => route.fulfill({
      json: { nodes: [], edges: [], configs: [], runs: [], versions: [], definitions: [] },
      headers: { ETag: '"1"' },
    }));
    await installConversationFixture(page);
    await page.goto('/');
    if (theme === 'light') await page.getByTitle('Switch to light mode', { exact: true }).click();
    await expect(page.getByTitle(theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode', { exact: true })).toBeVisible();

    const control = page.locator('#canvas-panel .react-flow__controls-button').first();
    await expect(control).toBeVisible();
    await page.locator('input[accept*="json"]').setInputFiles({
      name: 'invalid.json', mimeType: 'application/json', buffer: Buffer.from('invalid JSON'),
    });
    const notification = page.locator('[data-sonner-toast]').filter({ hasText: 'Failed to import flow' });
    await expect(notification).toBeVisible();

    for (const element of [control, notification]) {
      const rendered = await colors(element);
      expect(rendered.contrast).toBeGreaterThan(4.5);
      if (theme === 'dark') expect(rendered.brightness).toBeLessThan(80);
      else expect(rendered.brightness).toBeGreaterThan(200);
    }
    await expect(control.locator('svg')).toHaveCSS('fill', await control.evaluate(node => getComputedStyle(node).color));
    await page.screenshot({ path: `test-results/theme-${theme}.png`, fullPage: true });
  });
}
