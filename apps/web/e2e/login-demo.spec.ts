import { expect, test } from "@playwright/test";

test("the seeded self-hosted demo account can sign in through the form", async ({ page }) => {
  await page.goto("/login");

  await page.locator('input[type="email"]').fill("demo@manor.local");
  await page.locator('input[type="password"]').fill("manor-demo");
  await page.locator('button[type="submit"]').click();

  await expect(page).not.toHaveURL(/\/login(?:[/?#]|$)/);
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
});
