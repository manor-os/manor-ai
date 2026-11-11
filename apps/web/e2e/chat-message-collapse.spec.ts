import { expect, test } from "@playwright/test";

const SHOW_MORE_LINES = /^Show \d+ more lines$/;
const SHOW_LESS = { name: "Show less", exact: true } as const;

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/e2e/fixtures/chat-message-collapse.html");
});

test("short and medium replies stay whole; only long messages fold, without a fade when whole", async ({ page }) => {
  for (const id of ["short-ai", "moderate-ai", "ten-lines-ai", "medium-ai", "medium-overlap", "long-source", "responsive", "streaming", "async"]) {
    await expect(page.locator(`#${id}`).getByRole("button")).toHaveCount(0);
    await expect(page.locator(`#${id} .chat-sent-message-collapse__body--collapsed`)).toHaveCount(0);
  }
  // 90 reply lines show 40; 30 user lines show 12. The count is rendered lines, not spacing.
  for (const [id, label] of [["long-ai", "Show 50 more lines"], ["long-user", "Show 18 more lines"]] as const) {
    const row = page.locator(`#${id}`);
    const root = row.locator(".chat-sent-message-collapse");
    const more = row.getByRole("button", { name: SHOW_MORE_LINES });
    await expect(more).toHaveText(label);
    await expect(more).toHaveAttribute("aria-expanded", "false");
    await expect(root.locator(".chat-sent-message-collapse__body--collapsed")).toHaveCount(1);
    const body = root.locator(".chat-sent-message-collapse__body");
    const collapsed = await body.evaluate((el) => el.getBoundingClientRect().height);
    await more.click();
    await expect(row.getByRole("button", SHOW_LESS)).toHaveAttribute("aria-expanded", "true");
    await expect(root.locator(".chat-sent-message-collapse__body--collapsed")).toHaveCount(0);
    expect(await body.evaluate((el) => el.getBoundingClientRect().height)).toBeGreaterThan(collapsed);
    await row.getByRole("button", SHOW_LESS).click();
    await expect(row.getByRole("button", { name: SHOW_MORE_LINES })).toHaveAttribute("aria-expanded", "false");
  }
});

test("expanded, Show less stays pinned on screen while reading the middle of a long reply", async ({ page }) => {
  const row = page.locator("#long-ai");
  const root = row.locator(".chat-sent-message-collapse");
  await row.getByRole("button", { name: SHOW_MORE_LINES }).click();
  await root.evaluate((el) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY + 300));
  await expect(row.getByRole("button", SHOW_LESS)).toBeInViewport();
});

test("stream completion and repeated content changes reset collapse state", async ({ page }) => {
  await page.getByRole("button", { name: "完成输出", exact: true }).click();
  await expect(page.locator("#streaming").getByRole("button", { name: SHOW_MORE_LINES })).toBeVisible();
  await page.getByRole("button", { name: "重新开始输出", exact: true }).click();
  await expect(page.locator("#streaming").getByRole("button")).toHaveCount(0);
  await page.locator("#changing").getByRole("button", { name: SHOW_MORE_LINES }).click();
  await page.getByRole("button", { name: "切换长短内容", exact: true }).click();
  await expect(page.locator("#changing").getByRole("button")).toHaveCount(0);
  await page.getByRole("button", { name: "切换长短内容", exact: true }).click();
  await expect(page.locator("#changing").getByRole("button", { name: SHOW_MORE_LINES })).toBeVisible();
  await page.getByRole("button", { name: "切换异步内容", exact: true }).click();
  await expect(page.locator("#async").getByRole("button", { name: SHOW_MORE_LINES })).toBeVisible();
  await page.getByRole("button", { name: "切换异步内容", exact: true }).click();
  await expect(page.locator("#async").getByRole("button")).toHaveCount(0);
});

test("wrapping at mobile widths changes eligibility without horizontal overflow", async ({ page }) => {
  const row = page.locator("#responsive");
  await expect(row.getByRole("button")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(row.getByRole("button", { name: SHOW_MORE_LINES })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(row.getByRole("button")).toHaveCount(0);
});

test("lines are written lines, so a phone counts the same as a desktop", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  // Each of these lines wraps on a phone; wrapping does not make the content longer.
  await expect(page.locator("#long-ai").getByRole("button", { name: SHOW_MORE_LINES })).toHaveText("Show 50 more lines");
  await expect(page.locator("#long-user").getByRole("button", { name: SHOW_MORE_LINES })).toHaveText("Show 18 more lines");
  await expect(page.locator("#medium-ai").getByRole("button")).toHaveCount(0);
});

test("Markdown lists keep their numbers and bullets, and a folded list continues its numbering", async ({ page }) => {
  const row = page.locator("#numbered-ai");
  const style = (selector: string) => row.locator(selector).first().evaluate((el) => getComputedStyle(el).listStyleType);
  // Tailwind's reset removed every marker: a 70-step checklist read as 70 unnumbered lines.
  expect(await style(".chat-md > ol")).toBe("decimal");
  expect(await style(".chat-md ol ul")).toBe("circle");
  expect(await style(".chat-md > ul")).toBe("disc");
  // One list, folded by height: hidden items are the same list, so numbering runs on (41, 42, ...).
  await expect(row.locator(".chat-md > ol")).toHaveCount(1);
  await expect(row.locator(".chat-md > ol > li")).toHaveCount(70);
  await expect(row.getByRole("button", { name: SHOW_MORE_LINES })).toBeVisible();
  const markerInside = await row.locator(".chat-md > ol > li").nth(9).evaluate((li) => {
    const list = li.closest("ol")!.getBoundingClientRect();
    return li.getBoundingClientRect().left - list.left;
  });
  expect(markerInside).toBeGreaterThanOrEqual(20);
  await row.getByRole("button", { name: SHOW_MORE_LINES }).click();
  await expect(row.locator(".chat-md > ol > li").nth(40)).toBeVisible();
  expect(await row.locator(".chat-md > ol > li").nth(40).evaluate((li) => [...li.parentElement!.children].indexOf(li) + 1)).toBe(41);
});

test("long-message controls support keyboard activation", async ({ page }) => {
  const row = page.locator("#long-ai");
  await row.getByRole("button", { name: SHOW_MORE_LINES }).focus();
  await page.keyboard.press("Enter");
  await expect(row.getByRole("button", SHOW_LESS)).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Space");
  await expect(row.getByRole("button", { name: SHOW_MORE_LINES })).toHaveAttribute("aria-expanded", "false");
});

for (const width of [1280, 390]) {
  test(`chat tables keep one line per cell and scroll sideways at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const short = page.locator("#short-table");
    await expect(short.locator("tbody tr")).toHaveCount(16);
    await expect(short.getByRole("button")).toHaveCount(0);
    const long = page.locator("#long-table");
    await expect(long.locator("tbody tr")).toHaveCount(10);
    const cell = long.locator("tbody td").first();
    const rowHeight = await cell.evaluate((el) => el.getBoundingClientRect().height);
    expect(rowHeight).toBeLessThan(40);
    const scroll = long.locator(".chat-table-preview__scroll");
    if (width < 800) {
      expect(await scroll.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    }
    await long.getByRole("button", { name: "Show 30 more rows", exact: true }).click();
    await expect(long.locator("tbody tr")).toHaveCount(40);
    await expect(long.getByRole("button", SHOW_LESS)).toHaveAttribute("aria-expanded", "true");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await long.getByRole("button", SHOW_LESS).click();
    await expect(long.locator("tbody tr")).toHaveCount(10);
    await long.screenshot({ path: testInfo.outputPath(`long-table-${width}.png`) });
  });

  test(`historical final snapshots render once after refresh and expansion at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const row = page.locator("#historical-final");
    for (let load = 0; load < 2; load += 1) {
      if (load) await page.reload();
      await expect(row.locator(".chat-sent-message-collapse")).toHaveCount(1);
      await expect(row.getByText("Canonical final answer.", { exact: true })).toHaveCount(1);
      await expect(row.getByText("Obsolete working summary.", { exact: true })).toHaveCount(0);
      // A finished turn keeps its process, folded behind its summary.
      await row.getByRole("button", { name: /^Processed/ }).click();
      await expect(row.getByText("Inspecting the brief.", { exact: true })).toBeVisible();
      await expect(row.getByText("The brief has been checked.", { exact: true })).toBeVisible();
      await row.getByRole("button", { name: SHOW_MORE_LINES }).click();
      await expect(row.getByRole("button", SHOW_LESS)).toHaveCount(1);
      await expect(row.getByText("Canonical final answer.", { exact: true })).toHaveCount(1);
      await row.getByRole("button", SHOW_LESS).click();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await row.screenshot({ path: testInfo.outputPath(`historical-final-${width}.png`) });
  });
}
