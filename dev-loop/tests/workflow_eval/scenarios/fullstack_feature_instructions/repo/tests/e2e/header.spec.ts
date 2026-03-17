import { test, expect } from '@playwright/test';
test('header visible', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toBeVisible();
});
