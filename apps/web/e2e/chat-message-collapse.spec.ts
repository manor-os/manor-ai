import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/e2e/fixtures/chat-message-collapse.html");
});

test("short rendered replies stay open and both authors use reversible long-message controls", async ({ page }) => {
  for (const id of ["short-ai", "moderate-ai", "long-source", "responsive", "streaming", "async"]) {
    await expect(page.locator(`#${id}`).getByRole("button")).toHaveCount(0);
  }
  for (const id of ["long-ai", "long-user"]) {
    const row = page.locator(`#${id}`);
    const root = row.locator(".chat-sent-message-collapse");
    await expect(row.getByRole("button", { name: "Show all", exact: true })).toBeVisible();
    const collapsedHeight = await root.evaluate((el) => el.getBoundingClientRect().height);
    await row.getByRole("button", { name: "Show all", exact: true }).click();
    await expect(row.getByRole("button", { name: "Show less", exact: true })).toHaveAttribute("aria-expanded", "true");
    expect(await root.evaluate((el) => el.getBoundingClientRect().height)).toBeGreaterThan(collapsedHeight);
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

for (const width of [1280, 390]) {
  test(`historical final snapshots render once after refresh and expansion at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const row = page.locator("#historical-final");
    for (let load = 0; load < 2; load += 1) {
      if (load) await page.reload();
      await expect(row.locator(".chat-sent-message-collapse")).toHaveCount(1);
      await expect(row.getByText("Canonical final answer.", { exact: true })).toHaveCount(1);
      await expect(row.getByText("Obsolete working summary.", { exact: true })).toHaveCount(0);
      await expect(row.getByText("Inspecting the brief.", { exact: true })).toBeVisible();
      await expect(row.getByText("The brief has been checked.", { exact: true })).toBeVisible();
      await row.getByRole("button", { name: "Show all", exact: true }).click();
      await expect(row.getByRole("button", { name: "Show less", exact: true })).toHaveCount(1);
      await expect(row.getByText("Canonical final answer.", { exact: true })).toHaveCount(1);
      await row.getByRole("button", { name: "Show less", exact: true }).click();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await row.screenshot({ path: testInfo.outputPath(`historical-final-${width}.png`) });
  });
}

test("medium single-paragraph messages collapse without duplicating the middle", async ({ page }) => {
  const row = page.locator("#medium-overlap");
  const root = row.locator(".chat-sent-message-collapse");
  await expect(row.getByRole("button", { name: "Show all", exact: true })).toBeVisible();
  await expect(row.locator(".chat-sent-message-collapse__tail")).toBeHidden();
  const collapsedText = await root.evaluate((el) => (el as HTMLElement).innerText);
  expect((collapsedText.match(/ISRC\/UPC：未知/g) || []).length).toBe(1);
  expect((collapsedText.match(/目标日：2026-10-25/g) || []).length).toBe(1);
  const collapsedHeight = await root.evaluate((el) => el.getBoundingClientRect().height);
  await row.getByRole("button", { name: "Show all", exact: true }).click();
  await expect(row.getByRole("button", { name: "Show less", exact: true })).toHaveAttribute("aria-expanded", "true");
  const expandedText = await root.evaluate((el) => (el as HTMLElement).innerText);
  expect((expandedText.match(/ISRC\/UPC：未知/g) || []).length).toBe(1);
  expect((expandedText.match(/目标日：2026-10-25/g) || []).length).toBe(1);
  expect(await root.evaluate((el) => el.getBoundingClientRect().height)).toBeGreaterThan(collapsedHeight);
});

test("collapsed preview keeps the trailing action block visible", async ({ page }) => {
  const row = page.locator("#trailing-cta");
  await expect(row.getByRole("button", { name: "Show all", exact: true })).toBeVisible();
  const tail = row.locator(".chat-sent-message-collapse__tail");
  await expect(tail).toBeVisible();
  await expect(tail.getByText("Reply with the next file, or tell me to skip this step.", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "Show all", exact: true }).click();
  await expect(row.locator(".chat-sent-message-collapse__tail")).toHaveCount(0);
  await expect(row.getByText("Reply with the next file, or tell me to skip this step.", { exact: true })).toBeVisible();
});
