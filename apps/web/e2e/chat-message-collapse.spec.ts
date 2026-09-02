import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/e2e/fixtures/chat-message-collapse.html");
});

test("short rendered replies stay open and both authors use reversible long-message controls", async ({ page }) => {
  for (const id of ["short-ai", "long-source", "responsive", "streaming", "async"]) {
    await expect(page.locator(`#${id}`).getByRole("button")).toHaveCount(0);
  }
  for (const id of ["long-ai", "long-user"]) {
    const row = page.locator(`#${id}`);
    const body = row.locator(".chat-sent-message-collapse__body");
    await expect(row.getByRole("button", { name: "Show all", exact: true })).toBeVisible();
    const collapsedHeight = await body.evaluate((el) => el.clientHeight);
    await row.getByRole("button", { name: "Show all", exact: true }).click();
    await expect(row.getByRole("button", { name: "Show less", exact: true })).toHaveAttribute("aria-expanded", "true");
    expect(await body.evaluate((el) => el.clientHeight)).toBeGreaterThan(collapsedHeight);
    await row.getByRole("button", { name: "Show less", exact: true }).click();
    await expect(row.getByRole("button", { name: "Show all", exact: true })).toHaveAttribute("aria-expanded", "false");
  }
});

test("stream completion and repeated content changes reset collapse state", async ({ page }) => {
  await page.getByRole("button", { name: "完成输出", exact: true }).click();
  await expect(page.locator("#streaming").getByRole("button", { name: "Show all", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "重新开始输出", exact: true }).click();
  await expect(page.locator("#streaming").getByRole("button")).toHaveCount(0);
  await page.locator("#changing").getByRole("button", { name: "Show all", exact: true }).click();
  await page.getByRole("button", { name: "切换长短内容", exact: true }).click();
  await expect(page.locator("#changing").getByRole("button")).toHaveCount(0);
  await page.getByRole("button", { name: "切换长短内容", exact: true }).click();
  await expect(page.locator("#changing").getByRole("button", { name: "Show all", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "切换异步内容", exact: true }).click();
  await expect(page.locator("#async").getByRole("button", { name: "Show all", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "切换异步内容", exact: true }).click();
  await expect(page.locator("#async").getByRole("button")).toHaveCount(0);
});

test("wrapping at mobile widths changes eligibility without horizontal overflow", async ({ page }) => {
  const row = page.locator("#responsive");
  await expect(row.getByRole("button")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(row.getByRole("button", { name: "Show all", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(row.getByRole("button")).toHaveCount(0);
});

test("long-message controls support keyboard activation", async ({ page }) => {
  const row = page.locator("#long-ai");
  await row.getByRole("button", { name: "Show all", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(row.getByRole("button", { name: "Show less", exact: true })).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Space");
  await expect(row.getByRole("button", { name: "Show all", exact: true })).toHaveAttribute("aria-expanded", "false");
});
