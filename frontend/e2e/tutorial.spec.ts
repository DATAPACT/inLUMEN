import { test, expect } from '@playwright/test';
import { installConversationFixture } from './helpers/conversation';

const titles = [
  'Welcome to inLUMEN',
  'Component library',
  'Canvas',
  'Chat',
  'Inspector',
  'Save, import and export',
  'Code and Run',
  'Help',
];

test('the tour stays closed in browser tests and can be walked through from Help', async ({ page }) => {
  await page.route('**/api/**', route => route.fulfill({
    json: { nodes: [], edges: [], configs: [], runs: [], versions: [], definitions: [] },
    headers: { ETag: '"1"' },
  }));
  await installConversationFixture(page);
  await page.goto('/');

  // A fresh profile under automation: the tour must not open by itself.
  const help = page.getByRole('button', { name: 'Help' });
  await expect(help).toBeVisible();
  await expect(page.locator('[data-tour-overlay]')).toHaveCount(0);

  await help.click();
  await page.getByRole('button', { name: 'Start the tour' }).click();

  for (const [index, title] of titles.entries()) {
    const popup = page.getByRole('dialog', { name: title });
    await expect(popup).toBeVisible();
    await expect(popup).toContainText(`Step ${index + 1} of ${titles.length}`);
    await expect(popup).toBeInViewport({ ratio: 1 });
    if (title === 'Help') {
      // The Help button is always on screen, so this step has a frame and a pointer.
      await expect(page.locator('[data-tour-highlight]')).toBeVisible();
      await expect(popup.locator('[data-tour-pointer]')).toBeVisible();
    }
    await popup.getByRole('button', { name: index === titles.length - 1 ? 'Done' : 'Next' }).click();
  }
  await expect(page.locator('[data-tour-overlay]')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('inlumen-tutorial-seen'))).not.toBeNull();

  // Escape skips the tour.
  await help.click();
  await page.getByRole('button', { name: 'Start the tour' }).click();
  await expect(page.getByRole('dialog', { name: titles[0] })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('[data-tour-overlay]')).toHaveCount(0);
});
