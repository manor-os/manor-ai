import { expect, test } from "@playwright/test";

test.use({ launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } });

const fixture = "/e2e/fixtures/workspace-app-inputs.html";
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
});
for (const width of [1440, 390]) {
  test(`Workflow inputs stay in App when chat is available at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 950 });
    await page.goto(fixture);
    const source = page.getByRole("textbox", { name: "Source caption" });
    await expect(source).toBeVisible();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(source).toBeVisible();
    await source.fill("Synthetic caption for a structure review.");
    await source.press("Tab");
    await expect(page.getByRole("button", { name: "Next", exact: true })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`app-inputs-${width}.png`), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Analysis mode" })).toBeVisible();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByText("Synthetic caption for a structure review.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Run", exact: true }).click();
    // The summary line; the same words also sit in the folded original data.
    await expect(page.getByText("The fixture run completed in the App.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Show submitted request" }).click();
    await expect(page.locator("#submitted")).toHaveText(JSON.stringify({ bindingId: "analyze-binding", variables: { source: "Synthetic caption for a structure review.", mode: "text_only" }, trigger_data: { workspace_manual: true, _workspace_app_action: "analyze" }, execute: false }));
    await expect(page.getByText("Chat requests: 0", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Discover content", exact: true }).click();
    await expect(page.getByRole("spinbutton", { name: "Lookback days" })).toHaveValue("14");
    await expect(source).toBeHidden();
  });
}
test("chat is optional and setup/read-only gates remain effective", async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole("button", { name: "Write in chat", exact: true }).click();
  await expect(page.getByText("Chat requests: 1", { exact: true })).toBeVisible();
  await page.goto(`${fixture}?nochat`);
  await expect(page.getByRole("textbox", { name: "Source caption" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Write in chat", exact: true })).toBeHidden();
  await page.goto(`${fixture}?readonly`);
  await expect(page.getByRole("textbox", { name: "Source caption" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Write in chat", exact: true })).toBeDisabled();
  await page.goto(`${fixture}?setup`);
  await expect(page.getByText("Connect the required Skill.")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Source caption" })).toBeHidden();
  await page.goto(fixture);
  await page.getByRole("button", { name: "Review saved content", exact: true }).click();
  await expect(page.getByRole("button", { name: "Run", exact: true })).toBeVisible();
});

test("Workspace App shows independent upload progress, queues the fourth file, and waits for materials linking", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 950 });
  await page.goto(fixture);
  await page.getByRole("button", { name: "Upload materials", exact: true }).click();
  await page.locator('input[type="file"]').setInputFiles([
    { name: "brief.txt", mimeType: "text/plain", buffer: Buffer.from("brief") },
    { name: "outline.txt", mimeType: "text/plain", buffer: Buffer.from("outline") },
    { name: "references.txt", mimeType: "text/plain", buffer: Buffer.from("references") },
    { name: "appendix.txt", mimeType: "text/plain", buffer: Buffer.from("appendix") },
  ]);

  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.started().length)).toBe(3);
  expect(await page.evaluate(() => window.workspaceAppUploadFixture.started().map((item) => item.folderId))).toEqual([
    "fixture-workspace-root", "fixture-workspace-root", "fixture-workspace-root",
  ]);
  await expect(page.getByRole("group", { name: "appendix.txt" })).toContainText("Waiting to upload");
  await page.evaluate(() => {
    window.workspaceAppUploadFixture.progress("brief.txt", 42);
    window.workspaceAppUploadFixture.progress("references.txt", 100, "processing");
  });
  await expect(page.getByRole("progressbar", { name: "brief.txt" })).toHaveAttribute("aria-valuenow", "42");
  await expect(page.getByRole("group", { name: "brief.txt" })).toContainText("Uploading…42%");
  await expect(page.getByRole("progressbar", { name: "references.txt" })).not.toHaveAttribute("aria-valuenow");
  await expect(page.getByRole("group", { name: "references.txt" })).toContainText("Processing…");
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("workspace-app-upload-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);

  await page.evaluate(() => {
    window.workspaceAppUploadFixture.succeed("outline.txt");
    window.workspaceAppUploadFixture.succeed("references.txt");
    window.workspaceAppUploadFixture.succeed("brief.txt");
  });
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.started().length)).toBe(4);
  await page.evaluate(() => window.workspaceAppUploadFixture.succeed("appendix.txt"));
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.linkCalls())).toBe(1);
  await expect(page.getByText("Adding files to this Workspace…", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();

  await page.evaluate(() => window.workspaceAppUploadFixture.finishLinking());
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
  expect(await page.locator(".workflow-wait-document-row").evaluateAll((rows) => rows.map((row) => row.textContent))).toEqual([
    expect.stringContaining("brief.txt"),
    expect.stringContaining("outline.txt"),
    expect.stringContaining("references.txt"),
    expect.stringContaining("appendix.txt"),
  ]);
});

test("Workspace App reuses successful uploads and starts queued files only on retry", async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole("button", { name: "Upload materials", exact: true }).click();
  await page.locator('input[type="file"]').setInputFiles([
    { name: "first.txt", mimeType: "text/plain", buffer: Buffer.from("first") },
    { name: "retry.txt", mimeType: "text/plain", buffer: Buffer.from("retry") },
    { name: "third.txt", mimeType: "text/plain", buffer: Buffer.from("third") },
    { name: "fourth.txt", mimeType: "text/plain", buffer: Buffer.from("fourth") },
  ]);
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.started().length)).toBe(3);
  const retryKey = await page.evaluate(() => window.workspaceAppUploadFixture.uploadKey("retry.txt"));
  await page.evaluate(() => {
    window.workspaceAppUploadFixture.fail("retry.txt");
    window.workspaceAppUploadFixture.succeed("first.txt");
    window.workspaceAppUploadFixture.succeed("third.txt");
  });
  await expect(page.getByRole("alert")).toContainText("Synthetic upload failure");
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.started().length)).toBe(3);
  await expect(page.getByRole("group", { name: "fourth.txt" })).toContainText("Waiting to upload");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.started().length)).toBe(5);
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.uploadKey("retry.txt"))).toBe(retryKey);
  await page.evaluate(() => {
    window.workspaceAppUploadFixture.succeed("retry.txt");
    window.workspaceAppUploadFixture.succeed("fourth.txt");
  });
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.linkCalls())).toBe(1);
  await page.evaluate(() => window.workspaceAppUploadFixture.finishLinking());
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
});

test("Workspace App keeps Next disabled while either nested document field is linking", async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole("button", { name: "Upload two material sets", exact: true }).click();
  const inputs = page.locator('input[type="file"]');
  await expect(inputs).toHaveCount(2);
  await inputs.nth(0).setInputFiles({ name: "source.txt", mimeType: "text/plain", buffer: Buffer.from("source") });
  await inputs.nth(1).setInputFiles({ name: "reference.txt", mimeType: "text/plain", buffer: Buffer.from("reference") });
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.started().length)).toBe(2);
  await page.evaluate(() => {
    window.workspaceAppUploadFixture.succeed("source.txt");
    window.workspaceAppUploadFixture.succeed("reference.txt");
  });
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.linkCalls())).toBe(2);
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();

  await page.evaluate(() => window.workspaceAppUploadFixture.finishNextLink());
  await expect(page.getByRole("button", { name: "Remove source.txt", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await page.evaluate(() => window.workspaceAppUploadFixture.finishNextLink());
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
});

test("Chat workflow starter waits for sibling schema roots before enabling Run", async ({ page }) => {
  await page.goto(`${fixture}?mixed-card`);
  await page.getByRole("textbox", { name: "Note" }).fill("Use these files.");
  const uploads = page.locator('input[type="file"]');
  await expect(uploads).toHaveCount(2);
  await uploads.nth(0).setInputFiles({ name: "cover.png", mimeType: "image/png", buffer: Buffer.from("cover") });
  await uploads.nth(1).setInputFiles({ name: "sources.pdf", mimeType: "application/pdf", buffer: Buffer.from("sources") });
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.started().length)).toBe(2);
  const run = page.getByRole("button", { name: "Run workflow", exact: true });
  await expect(run).toBeDisabled();

  await page.evaluate(() => window.workspaceAppUploadFixture.succeed("cover.png"));
  await expect(page.getByRole("button", { name: "Remove selected file", exact: true })).toBeVisible();
  await expect(run).toBeDisabled();
  await page.evaluate(() => window.workspaceAppUploadFixture.succeed("sources.pdf"));
  await expect.poll(() => page.evaluate(() => window.workspaceAppUploadFixture.linkCalls())).toBe(1);
  await expect(run).toBeDisabled();
  await page.evaluate(() => window.workspaceAppUploadFixture.finishLinking());
  await expect(run).toBeEnabled();
});
