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
    await expect(page.getByText("The fixture run completed in the App.")).toBeVisible();
    await page.getByRole("button", { name: "Show submitted request" }).click();
    await expect(page.locator("#submitted")).toHaveText(JSON.stringify({ bindingId: "analyze-binding", variables: { source: "Synthetic caption for a structure review.", mode: "text_only" }, trigger_data: { workspace_manual: true }, execute: false }));
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
