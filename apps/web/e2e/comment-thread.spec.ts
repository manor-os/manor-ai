import { expect, test } from "@playwright/test";

for (const width of [1280, 390]) {
  test(`compact comments preserve actions and anchors at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/e2e/fixtures/comment-thread.html");
    const thread = page.getByTestId("comments");
    const first = thread.locator(".comment-thread-item").first();
    await expect(first.locator("blockquote")).toHaveText("groups");
    await expect(thread).not.toContainText("选中文字");
    await expect(thread).not.toContainText("Selected text");
    await expect(thread).not.toContainText("Texto seleccionado");
    await expect(thread).toContainText("Slide 3");
    await expect(thread).toContainText("Lines 12-14");
    await expect(first.getByRole("button", { name: "Delete", exact: true })).toHaveCount(0);
    await expect(first.getByRole("button", { name: "Like comment", exact: true })).toHaveCSS("height", "28px");
    expect(await first.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    const actions = await first.locator(".comment-thread-actions button").evaluateAll((buttons) => buttons.map((el) => el.getBoundingClientRect().y));
    expect(new Set(actions).size).toBe(1);
    await page.screenshot({ path: testInfo.outputPath(`comments-light-${width}.png`) });

    const more = first.locator(":scope > .comment-thread-header").getByRole("button", { name: "More actions" });
    await more.press("ArrowDown");
    await expect(page.getByRole("menuitem", { name: "Edit", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(more).toBeFocused();
    await more.click();
    await page.getByRole("menuitem", { name: "Edit", exact: true }).click();
    await first.getByRole("textbox", { name: "Edit", exact: true }).fill("Updated note");
    await first.getByRole("button", { name: "Save", exact: true }).click();
    await expect(first).toContainText("Updated note");
    await expect(page.getByLabel("Selected comment")).toHaveText("none");
    await first.locator("blockquote").click();
    await expect(page.getByLabel("Selected comment")).toHaveText("groups");
    await first.getByRole("button", { name: "Like comment", exact: true }).click();
    await expect(first.getByRole("button", { name: "Unlike comment (1)" })).toHaveAttribute("aria-pressed", "true");
    await first.getByRole("button", { name: "Reply", exact: true }).click();
    await first.getByPlaceholder("Write a reply...").fill("Reply note");
    await first.locator(".comment-thread-reply-composer").getByRole("button", { name: "Reply", exact: true }).click();
    await expect(first).toContainText("Reply note");
    await more.click();
    await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(first).toContainText("Updated note");

    await page.getByRole("button", { name: "Width", exact: true }).click();
    await page.getByRole("button", { name: "Theme", exact: true }).click();
    await page.getByRole("button", { name: "Language", exact: true }).click();
    await expect(thread).not.toContainText("选中文字");
    await expect(thread).not.toContainText("Selected text");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`comments-dark-${width}.png`) });
    expect(errors).toEqual([]);
  });
}
