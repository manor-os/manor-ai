import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";

const TABLE = "01M5KEWXCEFY6PT79TGQSCHVM1";
const REPORT = "01M5KEWXCEFY6PT79TGQSCHVM2";
const COVER = "01M5KEWXCEFY6PT79TGQSCHVM3";

const csv = ["handle,platform,followers,reels_14d,avg_views,engagement,status,note",
  ...Array.from({ length: 24 }, (_, i) => `@creator.${i + 1},Instagram,${48 - i}.2k,${6 + (i % 3)},21.4k,4.8%,active,a longer note that must stay on one line`)].join("\n");
const report = ["# Weekly overview", "", ...Array.from({ length: 12 }, (_, i) => `- Item ${i + 1}: followers and reach recorded.`)].join("\n");

test.beforeEach(async ({ page }) => {
  const png = await readFile(new URL("../public/email-logo.png", import.meta.url));
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const id = url.pathname.match(/\/documents\/([^/]+)\/preview\/content$/)?.[1];
    if (id === TABLE) return route.fulfill({ contentType: "text/csv", body: csv });
    if (id === REPORT) return route.fulfill({ contentType: "text/markdown", body: report });
    if (id === COVER) return route.fulfill({ contentType: "image/png", body: png });
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
});

test("a Workspace result starts as rows and expands in the message without leaving it", async ({ page }) => {
  await page.goto("/e2e/fixtures/chat-result-files.html");
  const result = page.locator("#workspace-result");
  await result.scrollIntoViewIfNeeded();

  // Attached files and a file linked in the sentence all start folded, with nothing loaded.
  for (const name of ["creator-snapshot.csv", "cover.png", "weekly-report.md"]) {
    await expect(result.getByRole("button", { name: new RegExp(`^${name.replace(".", "\\.")}`) })).toHaveAttribute("aria-expanded", "false");
    await expect(result.getByRole("region", { name, exact: true })).toHaveCount(0);
  }
  await expect(result.getByRole("button", { name: /Weekly report/ })).toHaveText(/View run/);

  const toggle = result.locator("button[aria-expanded]", { hasText: "creator-snapshot.csv" });
  await toggle.click();
  await expect(result.getByRole("region", { name: "creator-snapshot.csv", exact: true }).locator("tbody tr")).toHaveCount(10);
  await expect(page).toHaveURL(/chat-result-files\.html/);
  // Each row opens on its own and folds back.
  await expect(result.getByRole("region", { name: "cover.png", exact: true })).toHaveCount(0);
  // The row names the file, so the link inside only says what it does.
  await expect(result.getByRole("region", { name: "creator-snapshot.csv", exact: true })
    .locator(".generated-file__head").getByRole("button", { name: /^Open/ })).toBeVisible();
  await toggle.click();
  await expect(result.getByRole("region", { name: "creator-snapshot.csv", exact: true })).toHaveCount(0);
});

for (const width of [1280, 390]) {
  test(`a result shows each file once, as itself, under a name and Download line at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/e2e/fixtures/chat-result-files.html");
    const result = page.locator("#result");

    // No "Generated files" heading and no second card repeating the summary.
    await expect(result.getByRole("heading", { name: "Generated files" })).toHaveCount(0);
    await expect(result.getByText("Both creators are unchanged this week; the list needs no edits.", { exact: true })).toHaveCount(1);

    for (const name of ["creator-snapshot.csv", "weekly-report.md"]) {
      const file = result.getByRole("region", { name, exact: true });
      const head = file.locator(".generated-file__head");
      await expect(head.getByRole("button", { name: new RegExp(`^${name.replace(".", "\\.")}`) })).toBeVisible();
      await expect(head.getByRole("button", { name: `Download ${name}`, exact: true })).toBeVisible();
      // The name line sits above the content.
      const headBox = await head.boundingBox();
      const contentBox = await file.locator(".inline-document-text").boundingBox();
      expect(headBox && contentBox && headBox.y + headBox.height <= contentBox.y + 1).toBe(true);
    }

    // The table starts with 10 rows, one line per cell.
    const table = result.getByRole("region", { name: "creator-snapshot.csv", exact: true });
    await expect(table.locator("tbody tr")).toHaveCount(10);
    await expect(table.getByRole("button", { name: "Show 14 more rows", exact: true })).toBeVisible();
    expect(await table.locator("tbody td").first().evaluate((el) => el.getBoundingClientRect().height)).toBeLessThan(40);

    // A 14-line document is shown whole, with no fold and no fade.
    const doc = result.getByRole("region", { name: "weekly-report.md", exact: true });
    await expect(doc.getByText("Item 12: followers and reach recorded.", { exact: true })).toBeVisible();
    await expect(doc.getByRole("button", { name: /more lines|Show all/ })).toHaveCount(0);
    await expect(doc.locator(".chat-sent-message-collapse__body--collapsed")).toHaveCount(0);

    // The run is one quiet line, not a card repeating the result.
    await expect(result.getByRole("button", { name: /Weekly report/ })).toHaveText(/View run/);

    // A reply that links its files in the text (personal and floating chat) shows them too.
    const linked = page.locator("#linked-result");
    const linkedTable = linked.getByRole("region", { name: "creator-snapshot.csv", exact: true });
    await expect(linkedTable.locator("tbody tr")).toHaveCount(10);
    await expect(linked.getByRole("region", { name: "weekly-report.md", exact: true })
      .getByText("Item 12: followers and reach recorded.", { exact: true })).toBeVisible();
    // In the sentence the file reads as its name; the chip is only for what is not shown.
    await expect(linked.locator("p", { hasText: "I saved this week's list as" }).getByRole("button")).toHaveCount(0);
    await expect(linked.locator("p", { hasText: "The archive stays a link" }).getByRole("button", { name: /assets\.zip/ })).toBeVisible();

    // A picture in the reply text shows once, as the file; never as a broken /viewer image.
    const picture = page.locator("#picture-in-text");
    await expect(picture.getByRole("region", { name: "coffee-pop-up-promotion.png", exact: true })).toHaveCount(1);
    await expect(picture.locator(".generated-file img")).toHaveCount(1);
    await expect(picture.locator(".generated-file img")).toHaveAttribute("src", /^blob:/);
    await expect(picture.locator("img[src^='/viewer/']")).toHaveCount(0);
    expect(await picture.locator("img").evaluateAll((images) => images.filter((img) => !(img as HTMLImageElement).naturalWidth).length)).toBe(0);
    await expect(picture.getByText("Same picture again, named in a sentence:")).toBeVisible();
    // The same picture linked by document id and attached by storage path shows once.
    const byPath = page.locator("#picture-by-path");
    await expect(byPath.locator(".generated-file")).toHaveCount(1);
    await expect(byPath.locator(".video-result-files")).toHaveCount(0);
    await expect(byPath.locator(".generated-file img")).toHaveAttribute("src", /^blob:/);
    // Outside a chat reply, a /viewer picture loads its bytes; a missing file reads as its name.
    const outside = page.locator("#picture-outside-reply");
    await expect(outside.locator("img")).toHaveCount(1);
    await expect(outside.locator("img")).toHaveAttribute("src", /^blob:/);
    await expect(outside.locator(".chat-md-image-missing", { hasText: "missing-poster.png" })).toBeVisible();

    // An image is shown directly (previews load as they scroll into view).
    await page.locator("#image-result").scrollIntoViewIfNeeded();
    const cover = page.locator("#image-result").getByRole("img", { name: "cover.png", exact: true });
    await expect(cover).toBeVisible();

    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`chat-result-files-${width}.png`), fullPage: true });
  });
}
