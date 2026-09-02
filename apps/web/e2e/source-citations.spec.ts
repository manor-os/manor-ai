import { expect, test } from "@playwright/test";

for (const width of [1280, 390]) {
  test(`source citations retain evidence and navigation at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    // Playwright automatically aborts /favicon.ico before user route handlers.
    // Exercise successful icon loading on the same component with a fixture URL;
    // the Markdown cases below exercise its real unavailable-icon fallback.
    const favicon = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#426c87"/></svg>';
    await page.route("https://example.test/source-icon.svg", (route) => route.fulfill({
      // Most source sites serve public icons without any CORS opt-in.
      contentType: "image/svg+xml", body: favicon,
    }));
    await page.goto("/e2e/fixtures/source-citations.html");

    const legacy = page.getByTestId("legacy");
    const source = legacy.getByRole("button", { name: "Pinterest_Adobe_LeetCode_面试真题整理.md", exact: true });
    await expect(source).toBeVisible();
    await expect(legacy).not.toContainText("CREDIT:");
    await expect(legacy).toContainText("接下来我会继续追问");
    await source.focus();
    await page.keyboard.press("Enter");
    const popup = page.getByRole("dialog", { name: "Source details" });
    await expect(popup).toContainText("Pinterest_Adobe_LeetCode_面试真题整理.md");
    await expect(popup).toContainText("Minimum Window Substring");
    await expect(popup.getByRole("link")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(popup).toHaveCount(0);
    await expect(source).toBeFocused();

    await page.getByTestId("files").getByRole("button", { name: /面试真题整理.*\+1/ }).click();
    await expect(popup).toBeVisible();
    await expect(popup.getByRole("link", { name: "Official documentation" })).toHaveAttribute("href", "https://example.test/docs?lang=zh&section=1");
    const box = await popup.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    expect(await popup.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`sources-${width}.png`) });
    await popup.getByRole("button", { name: /面试真题整理/ }).click();
    await expect(page.getByTestId("destination")).toHaveText("/viewer/citation-doc-id");
    await page.keyboard.press("Escape");

    await page.getByTestId("web").getByRole("button", { name: "example.test", exact: true }).click();
    const external = popup.getByRole("link", { name: /^Source/ });
    await expect(external).toHaveAttribute("href", "https://example.test/research");
    await expect(external).toHaveAttribute("rel", "noopener noreferrer");
    await page.getByRole("heading", { name: "Source citations" }).click();
    await expect(popup).toHaveCount(0);

    await page.getByText("非引用原文示例", { exact: true }).click();
    await expect(page.getByTestId("normal").getByRole("link", { name: "website" })).toBeVisible();
    await expect(page.getByTestId("normal").getByRole("button", { name: /report.md/ })).toBeVisible();
    await expect(page.getByTestId("code")).toContainText("CREDIT:");
    await expect(page.getByTestId("user")).toContainText("CREDIT:");
    await expect(page.getByTestId("user").locator(".chat-source-link")).toHaveCount(0);
    await expect(page.getByTestId("stream").locator(".chat-source-link")).toHaveCount(0);
    const loadedIcon = page.getByTestId("loaded-icon").locator("img");
    await loadedIcon.scrollIntoViewIfNeeded();
    await expect(loadedIcon).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(await loadedIcon.getAttribute("crossorigin")).toBeNull();
    await expect.poll(() => loadedIcon.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
    await page.getByText("非引用原文示例", { exact: true }).click();

    const named = page.getByTestId("named-web").getByRole("button", { name: "MDN 官方文档" });
    await expect(named).toBeVisible();
    await expect(named).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(named.locator("img")).toHaveCount(0);
    await expect(named.locator(".chat-source-icon svg")).toBeVisible();
    await expect(page.getByRole("button", { name: "React 官方文档" }).locator("img")).toHaveCount(0);
    await named.hover();
    await expect(popup).toBeVisible();
    await expect(popup.getByRole("link", { name: /^MDN 官方文档/ })).toHaveAttribute("href", "https://developer.mozilla.org/en-US/docs/Web/JavaScript");
    await page.keyboard.press("Escape");

    await page.getByTestId("named-file").getByRole("button", { name: "面试真题整理.md", exact: true }).click();
    await popup.getByRole("button", { name: /面试真题整理.md/ }).click();
    await expect(page.getByTestId("destination")).toHaveText("/viewer/named-citation-doc-id");
    await page.keyboard.press("Escape");
    await page.getByRole("heading", { name: "Source citations" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`inline-sources-light-${width}.png`) });
    await page.getByRole("button", { name: "Dark mode" }).click();
    await expect(named).toHaveCSS("color", "rgb(152, 199, 243)");
    await page.screenshot({ path: testInfo.outputPath(`inline-sources-dark-${width}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}
