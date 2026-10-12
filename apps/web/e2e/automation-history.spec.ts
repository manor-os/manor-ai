import { expect, test, type Page } from "@playwright/test";

async function openFixture(page: Page, width: number, mode = "workflow") {
  await page.setViewportSize({ width, height: 1000 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`/e2e/fixtures/automation-history.html?mode=${mode}`);
}

function reportFile(page: Page) {
  return page.getByRole("button", { name: /automation-report\.md/ })
    .or(page.getByRole("link", { name: /automation-report\.md/ }));
}

for (const width of [1440, 390]) {
  test(`automation history deep link opens the canonical result outside the first page at ${width}px`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await openFixture(page, width);
    await expect(page.getByRole("heading", { name: "Automations", exact: true })).toBeVisible();
    await expect(page.getByText("Daily automation report", { exact: true })).toBeVisible();
    await expect(page.getByRole("status", { name: "Automation lookups" })).toContainText("business-job-id");
    await expect(page.getByText("The automation report is ready.", { exact: true })).toBeVisible();
    await expect(reportFile(page).first()).toBeVisible();
    await expect(page.getByRole("status", { name: "Run detail reads" })).not.toHaveText("0");
    await expect(page.getByRole("status", { name: "Workflow reads" })).not.toHaveText("0");
    await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`automation-history-${width}.png`), fullPage: true });
    await reportFile(page).first().click();
    await expect(page.getByRole("status", { name: "Current route" })).toHaveText("/viewer/automation-report");
    expect(errors).toEqual([]);
  });

  test(`automation run detail displays Agent output without a raw run result at ${width}px`, async ({ page }) => {
    await openFixture(page, width, "agent");
    await expect(page.getByText("Daily automation report", { exact: true })).toBeVisible();
    await expect(page.getByText("The Agent report was saved and is ready to review.", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("status", { name: "Run detail reads" })).not.toHaveText("0");
    await expect(page.getByRole("status", { name: "Workflow reads" })).toHaveText("0");
    await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  });
}

test("an open running Automation refreshes to its completed result without dispatching work", async ({ page }) => {
  await openFixture(page, 1440, "running");
  await expect(page.getByText("Daily automation report", { exact: true })).toBeVisible();
  await expect(page.getByRole("status", { name: "Run detail reads" })).not.toHaveText("0");
  await expect(page.getByText("The automation report is ready.", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Complete simulated run", exact: true }).click();
  await expect(page.getByText("The automation report is ready.", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(reportFile(page).first()).toBeVisible();
  await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
  await expect(page.getByRole("status", { name: "Run detail reads" })).not.toHaveText("1");
});

test("legacy job logs preserve the requested run on the canonical Automation page", async ({ page }) => {
  await openFixture(page, 1440, "legacy");
  await expect(page.getByRole("status", { name: "Current route" })).toHaveText("/jobs?job=business-job-id&run=workflow-result-run");
  await expect(page.getByRole("heading", { name: "Automations", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Job Logs", exact: true })).toHaveCount(0);
  await expect(page.getByText("Daily automation report", { exact: true })).toBeVisible();
  await expect(page.getByText("The automation report is ready.", { exact: true })).toBeVisible();
  await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
});

test("an unavailable canonical result shows an explicit error and retries only the read", async ({ page }) => {
  await openFixture(page, 390, "failure");
  const alert = page.getByRole("alert").filter({ hasText: "Failed to load run detail" });
  await expect(alert).toBeVisible();
  await expect(page.getByRole("status", { name: "Workflow reads" })).toHaveText("1");
  await expect(page.getByText("The automation report is ready.", { exact: true })).toHaveCount(0);
  await alert.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("The automation report is ready.", { exact: true })).toBeVisible();
  await expect(page.getByRole("status", { name: "Workflow reads" })).toHaveText("2");
  await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
});

test("an exact older run remains readable when the recent 50 runs omit it", async ({ page }) => {
  await openFixture(page, 1440, "old-run");
  await expect(page.getByRole("status", { name: "Recent run count" })).toHaveText("50");
  await expect(page.getByRole("status", { name: "Requested run in recent history" })).toHaveText("false");
  await expect(page.getByRole("status", { name: "Run detail reads" })).not.toHaveText("0");
  await expect(page.getByText("Daily automation report", { exact: true })).toBeVisible();
  await expect(page.getByText("The automation report is ready.", { exact: true })).toBeVisible();
  await expect(reportFile(page).first()).toBeVisible();
  await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
});

test("a direct Workflow Automation link respects the disabled Workflow surface", async ({ page }) => {
  await openFixture(page, 1440, "unavailable");
  await expect(page.getByRole("heading", { name: "Automations", exact: true })).toBeVisible();
  await expect(page.getByRole("status", { name: "Automation lookups" })).toContainText("business-job-id");
  await expect(page.getByText("Daily automation report", { exact: true })).toHaveCount(0);
  await expect(page.getByText("The automation report is ready.", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("status", { name: "Run detail reads" })).toHaveText("0");
  await expect(page.getByRole("status", { name: "Workflow reads" })).toHaveText("0");
  await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
});

test("a generic Automation result is immediately visible without linked Agent or Workflow output", async ({ page }) => {
  await openFixture(page, 390, "generic");
  await expect(page.getByText("Daily automation report", { exact: true })).toBeVisible();
  await expect(page.getByText("Message: The generic automation result is ready.", { exact: true })).toBeVisible();
  await expect(page.locator("details").filter({ hasText: "The generic automation result is ready." })).toHaveAttribute("open", "");
  await expect(page.getByRole("status", { name: "Run detail reads" })).not.toHaveText("0");
  await expect(page.getByRole("status", { name: "Workflow reads" })).toHaveText("0");
  await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
});


test("persisted snapshot graph edges select the final step without a durable result envelope", async ({ page }) => {
  await openFixture(page, 1440, "snapshot-nodes");
  await expect(page.getByText("The automation report is ready.", { exact: true })).toBeVisible();
  await expect(reportFile(page).first()).toBeVisible();
  await expect(page.getByText("Intermediate research notes.", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /intermediate-report\.md/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /intermediate-report\.md/ })).toHaveCount(0);
  await expect(page.getByRole("status", { name: "Dispatch requests" })).toHaveText("0");
});
