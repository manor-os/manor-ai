import { expect, test } from "@playwright/test";

const fixture = "/e2e/fixtures/workspace-app-calendar.html";

test.use({
  launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH },
  timezoneId: "Asia/Shanghai",
  reducedMotion: "no-preference",
});

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-10-22T02:00:00Z"));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
});

for (const [name, width, suffix] of [["desktop", 1440, ""], ["mobile", 320, ""], ["narrow container", 1440, "?narrow"]] as const) {
  test(`campaign calendar keeps its overview and day details usable on ${name}`, async ({ page }, testInfo) => {
    const missingLikes = name === "desktop";
    await page.setViewportSize({ width, height: 950 });
    await page.goto(`${fixture}${suffix}${missingLikes ? `${suffix ? "&" : "?"}metrics-pending` : ""}`);

    const collection = page.getByTestId("calendar-collection");
    const data = page.getByRole("region", { name: "Key metrics", exact: true, includeHidden: true });
    const outputs = page.getByRole("region", { name: "Recent outputs", exact: true, includeHidden: true });
    await expect(collection).toBeVisible();
    await expect(data.getByText("Campaign review score", { exact: true })).toBeVisible();
    await expect(outputs.getByText("Raise That Flag campaign plan", { exact: true })).toBeVisible();
    await expect(collection.getByRole("button", { name: "Raise That Flag · v2", exact: true })).toBeVisible();
    await expect(collection).toContainText("1/6");
    await expect(collection).toContainText(/1 .*attention/i);
    await expect(collection).toContainText("Lyric teaser");
    await expect(collection).toContainText(/1 .*failed/i);
    await expect(collection).toContainText("1 Processing");
    await expect(page.getByRole("button", { name: /^Launch a Music Marketing Campaign/ })).toBeVisible();
    await collection.getByRole("button", { name: "Raise That Flag · v2", exact: true }).click();

    const calendar = page.getByTestId("campaign-calendar");
    await expect(calendar).toBeVisible();
    await expect(data).toBeHidden();
    await expect(outputs).toBeHidden();
    const back = page.getByRole("button", { name: "All actions", exact: true });
    await expect(back).toBeInViewport();
    await expect(back).toBeFocused();
    await expect(page.getByRole("button", { name: /^Launch a Music Marketing Campaign/ })).toHaveCount(0);
    await expect(calendar.locator("summary").filter({ hasText: /^(Published content|Publishing progress)$/ })).toHaveCount(0);
    const selectedDay = page.getByTestId("calendar-day-2026-10-22");
    await expect(selectedDay).toBeVisible();
    await expect(selectedDay).toHaveAccessibleName(/6 scheduled items/);
    await expect(selectedDay.locator(".workspace-app-calendar__date-dots svg")).toHaveCount(6);
    await expect(selectedDay.locator("..")).toContainText(/2 issues/);
    await page.screenshot({ path: testInfo.outputPath(`campaign-calendar-month-${name}.png`), fullPage: true, animations: "disabled" });
    const more = page.getByTestId(name === "desktop" ? "calendar-more-2026-10-22" : "calendar-view-day");
    const entries = name === "desktop" ? selectedDay.locator("..").locator(".workspace-app-calendar__event")
      : page.getByTestId("calendar-day-preview").locator(".workspace-app-calendar__day-item");
    await expect(entries).toHaveCount(3);
    await more.click();
    await expect(entries).toHaveCount(6);
    await expect(more).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("calendar-day-panel")).toHaveCount(0);
    await expect(selectedDay).toBeVisible();
    await more.click();
    await expect(entries).toHaveCount(3);
    if (name === "desktop") {
      await more.click();
      await selectedDay.click();
    } else {
      await page.getByTestId("calendar-day-preview").getByRole("button", { name: "+3 more", exact: true }).click();
      await expect(entries).toHaveCount(6);
      await expect(page.getByTestId("calendar-day-panel")).toHaveCount(0);
    }

    const dayPanel = page.getByTestId(name === "desktop" ? "calendar-day-panel" : "calendar-day-preview");
    await expect(dayPanel).toBeVisible();
    await expect(dayPanel).toContainText("Morning teaser");
    await expect(dayPanel).toContainText("Night teaser");
    await expect(dayPanel).toContainText("Production needs attention");
    await expect(dayPanel.getByText("One Flag, Raised", { exact: true })).toBeVisible();
    const morning = dayPanel.getByTestId("calendar-entry-morning-teaser");
    // Published results are visible before opening the plan. Zero and missing
    // observations retain their different meanings.
    const metrics = morning.getByTestId("calendar-metrics-morning-teaser");
    await expect(metrics.getByText("Views", { exact: true })).toBeVisible();
    await expect(metrics.getByText("0", { exact: true })).toHaveCount(missingLikes ? 1 : 2);
    await expect(metrics.getByText("—", { exact: true })).toHaveCount(missingLikes ? 1 : 0);
    await expect(metrics.getByText("No data", { exact: true })).toHaveCount(0);
    await expect(morning.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    await morning.getByRole("button").click();
    await expect(calendar.getByRole("combobox", { name: "Change over", exact: true })).toHaveCount(0);
    await expect(morning.getByRole("link", { name: "Open publication", exact: true })).toHaveAttribute("href", "https://example.com/posts/morning-teaser");
    await expect(morning.getByText("No data", { exact: true })).toHaveCount(missingLikes ? 1 : 0);
    await expect(morning.getByText(/Last collected/)).toBeVisible();
    await expect(morning.getByText(/Last collected/)).toHaveCount(1);
    await expect(morning.getByText("Morning teaser", { exact: true })).toHaveCount(1);
    await expect(morning.getByText("0", { exact: true })).toHaveCount(missingLikes ? 1 : 2);
    await expect(morning.getByText("Platform", { exact: true })).toHaveCount(0);
    await expect(morning.getByText("Publish at", { exact: true })).toHaveCount(0);
    await expect(morning.getByText("Publish by", { exact: true })).toHaveCount(1);
    await page.screenshot({ path: testInfo.outputPath(`campaign-result-details-${name}.png`), fullPage: true, animations: "disabled" });
    await morning.getByRole("button").click();
    const failedItem = dayPanel.getByTestId("calendar-entry-lyric-teaser");
    const failedStatus = failedItem.locator(".workspace-app-calendar__status");
    await expect(failedStatus).toHaveText(/failed/i);
    expect(await failedStatus.evaluate((element) => element.getAnimations({ subtree: true }).length)).toBe(0);
    await failedItem.getByRole("button").click();
    await failedItem.getByText("Failure details", { exact: true }).click();
    await expect(failedItem).toContainText("The publishing provider rejected this attempt.");
    await expect(failedItem.getByRole("link", { name: "Open run", exact: true })).toHaveAttribute("href", "/flows/jr-music/runs/publish-lyric");
    await failedItem.getByRole("button").click();
    const processing = dayPanel.getByTestId("calendar-entry-chorus-preview").locator(".workspace-app-calendar__status");
    await expect(processing).toHaveText("Processing");
    await expect.poll(() => processing.evaluate((element) => element.getAnimations({ subtree: true }).filter((animation) => animation.playState === "running").length)).toBe(1);
    // A completed preparation run stays ready to publish, without an active spinner.
    const ready = dayPanel.getByTestId("calendar-entry-one-flag-raised").locator(".workspace-app-calendar__status");
    await expect(ready).toHaveText("Ready to publish");
    expect(await ready.evaluate((element) => element.getAnimations({ subtree: true }).length)).toBe(0);
    if (name === "desktop") {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await expect.poll(() => processing.evaluate((element) => element.getAnimations({ subtree: true }).length)).toBe(0);
      await expect(processing).toHaveText("Processing");
      await page.emulateMedia({ reducedMotion: "no-preference" });
    }
    await page.screenshot({ path: testInfo.outputPath(`campaign-calendar-day-${name}.png`), fullPage: true, animations: "disabled" });

    // A completed preparation run is evidence about the run, never publication status.
    const pendingPublication = dayPanel.getByTestId("calendar-entry-one-flag-raised");
    await expect(pendingPublication).toContainText("Ready to publish");
    await expect(pendingPublication.getByText("Published", { exact: true })).toHaveCount(0);
    await expect(pendingPublication.getByText("Completed", { exact: true })).toHaveCount(0);
    await pendingPublication.getByRole("button").click();
    await pendingPublication.getByText("Execution details", { exact: true }).click();
    await expect(pendingPublication).toContainText("Completed");
    await expect(pendingPublication.getByRole("link", { name: "Open run", exact: true })).toHaveAttribute("href", "/flows/jr-music/runs/prepare-one-flag");

    // Confirmed V1 plans are read-only: no new publishing mutation surface or drag handle.
    for (const action of ["Add post", "Edit campaign", "Delete campaign", "Publish now", "Save changes"]) {
      await expect(dayPanel.getByRole("button", { name: action, exact: true })).toHaveCount(0);
    }
    await expect(dayPanel.locator("[draggable='true']")).toHaveCount(0);

    if (name === "desktop") {
      await page.getByTestId("calendar-day-close").click();
      await expect(dayPanel).toHaveCount(0);
    } else {
      await pendingPublication.locator(".workspace-app-calendar__item-heading").click();
      await expect(pendingPublication.getByRole("link", { name: "Open run", exact: true })).toHaveCount(0);
      await expect(selectedDay).toBeVisible();
    }
    await expect(calendar).toBeVisible();
    // Original plan and reporting references remain available outside the day.
    await expect(calendar.getByRole("link", { name: "Confirmed plan", exact: true })).toHaveAttribute("href", "/viewer/confirmed-plan");
    await expect(calendar.getByTitle("Campaign starts")).toBeVisible();
    await expect(calendar.getByTitle("Collection planned until")).toBeVisible();
    await expect(calendar.getByTitle("Reports scheduled until")).toBeVisible();
    const reportingGroup = calendar.locator("details").filter({ hasText: "Plan & reporting" }).first();
    await reportingGroup.locator("summary").click();
    await expect(calendar.getByRole("link", { name: "Original proposal", exact: true })).toHaveAttribute("href", "/viewer/original-plan");
    await page.getByTestId("calendar-back").click();
    await expect(collection).toBeVisible();
    await expect(data).toBeVisible();
    await expect(outputs).toBeVisible();
    await expect(collection.getByRole("button", { name: "Raise That Flag · v2", exact: true })).toBeFocused();
    await expect(page.getByRole("button", { name: /^Launch a Music Marketing Campaign/ })).toBeVisible();
    // Going back to the collection and reopening preserves the selected campaign's calendar place.
    await collection.getByRole("button", { name: "Raise That Flag · v2", exact: true }).click();
    await expect(page.getByTestId("calendar-day-2026-10-22")).toHaveAttribute("aria-pressed", "true");
    await expect(calendar.getByRole("heading", { name: "October 2026", exact: true })).toBeVisible();
    await page.getByTestId("calendar-back").click();
    await expect(collection).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`campaign-calendar-${name}.png`), fullPage: true, animations: "disabled" });

    await collection.getByRole("button", { name: "Paper Planes · v1", exact: true }).click();
    const publishedDay = page.getByTestId("calendar-day-2026-10-21");
    await publishedDay.click();
    // This receipt is on the second results page. Never substitute another
    // publication's metrics, or require the removed bottom accordion to load it.
    const thanks = page.getByTestId("calendar-entry-paper-thanks");
    await expect(thanks.getByText("20,190", { exact: true })).toBeVisible();
    await expect(thanks.getByText("1,000", { exact: true })).toBeVisible();
    await expect(thanks.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    await expect(calendar.locator("summary").filter({ hasText: /^(Published content|Publishing progress)$/ })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`campaign-inline-metrics-${name}.png`), fullPage: true, animations: "disabled" });
  });
}

test("a post waiting for review is reviewed and answered from its own row", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 950 });
  await page.goto(fixture);
  const collection = page.getByTestId("calendar-collection");
  await expect(collection.getByRole("button", { name: "Midnight Drive · v1", exact: true })).toContainText(/1 .*attention/i);
  await collection.getByRole("button", { name: "Midnight Drive · v1", exact: true }).click();
  // The header's problem count opens the post it counts, on its day.
  await page.getByTestId("campaign-calendar").getByRole("button", { name: "1 needs attention", exact: true }).click();
  await expect(page.getByTestId("calendar-day-2026-10-25")).toHaveAttribute("aria-pressed", "true");
  const post = page.getByTestId("calendar-entry-studio-teaser");
  await expect(post).toContainText("Waiting for your review");
  await expect(post.locator(".workspace-app-calendar__item-heading")).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByTestId("calendar-day-panel")).toContainText("1 scheduled item");
  // The answer sits on the row, and the expanded post does not offer it a second time.
  await expect(post.getByRole("button", { name: "Review request", exact: true })).toHaveCount(0);
  await post.getByRole("button", { name: "Review", exact: true }).click();
  const dialog = page.locator(".workflow-response-dialog");
  await expect(dialog.getByText("Studio teaser", { exact: true }).first()).toBeVisible();
  await dialog.getByRole("button", { name: "Request changes", exact: true }).click();
  await dialog.locator("textarea").fill("Shorten the opening and keep the chorus.");
  await dialog.getByRole("button", { name: "Send and redo", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  // The decision and its notes resume the run's own wait, then the row stops asking.
  expect(await page.evaluate(() => (window as any).__reviewAnswers)).toEqual([{
    runId: "review-studio-teaser", expectedStepId: "review_prepared_content",
    variables: { owner_review_decision: { choice: "request_changes", note: "Shorten the opening and keep the chorus." } },
  }]);
  await expect(post.getByRole("button", { name: "Review", exact: true })).toHaveCount(0);
});

test("campaign calendar exposes a failed calendar load without presenting it as an empty plan", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto(`${fixture}?calendar-error`);
  await page.getByRole("button", { name: "Raise That Flag · v2", exact: true }).click();
  const calendar = page.getByTestId("campaign-calendar");
  await expect(calendar).toBeVisible();
  await expect(calendar).toContainText("Publishing plan could not load.");
  await expect(calendar.getByText("No publishing is planned.", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Key metrics", exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Recent outputs", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "All actions", exact: true }).click();
  await expect(page.getByTestId("calendar-collection")).toBeVisible();
  await expect(page.getByRole("region", { name: "Key metrics", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Recent outputs", exact: true })).toBeVisible();
});
