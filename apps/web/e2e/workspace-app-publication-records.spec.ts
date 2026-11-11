import { expect, test } from "@playwright/test";

const fixture = "/e2e/fixtures/workspace-app-publication-records.html";

for (const width of [1440, 390]) {
  test(`publication records keep metrics with their publication at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    await page.goto(fixture);

    await expect(page.getByRole("button", { name: "Publish content Start a new publication", exact: true })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Change over" })).toBeVisible();
    await page.getByRole("combobox", { name: "Change over" }).selectOption("30d");
    await expect(page.getByRole("combobox", { name: "Change over" })).toHaveValue("30d");
    await expect(page.getByText("Change over", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Published content", exact: true })).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Published content", exact: true })).toBeVisible();
    const first = page.locator(".workspace-app-data__record").filter({ hasText: "Lesson one" });
    const second = page.locator(".workspace-app-data__record").filter({ hasText: "Lesson two" });
    await expect(first.locator(".workspace-app-data__platform")).toHaveText("YouTube");
    await expect(first.locator('[data-integration-brand="youtube"]')).toBeVisible();
    // A configured provider field replaces the receipt's platform chip and names the account.
    await expect(second.locator(".workspace-app-data__platform")).toHaveCount(1);
    await expect(second.locator(".workspace-app-data__platform")).toHaveText("Instagram@course.studio");
    await expect(second.locator('[data-integration-brand="instagram"]')).toBeVisible();
    const preview = page.getByRole("link").filter({ hasText: "Course preview" });
    await expect(preview.locator(".workspace-app-data__platform")).toHaveText("TikTok");
    await expect(preview.locator('[data-integration-brand="tiktok"]')).toBeVisible();
    await expect(preview).toHaveAttribute("href", "https://www.tiktok.com/@courses/video/1234567890123456789");
    await expect(first).not.toContainText("Instagram");
    await expect(second).not.toContainText("YouTube");
    await expect(page.getByRole("heading", { name: "Course materials", exact: true })).toBeVisible();
    await expect(page.getByText("Learning portal", { exact: true })).toBeVisible();
    await expect(first.locator('.workspace-app-data__record-meta time[datetime="2026-10-10T10:00:00Z"]')).toBeVisible();
    await expect(first.getByText("Tracking configured", { exact: true })).not.toBeVisible();
    await expect(first.locator(".workspace-app-data__metric-copy > strong")).toHaveText(["0", "12", "4"]);
    await expect(first.locator(".workspace-app-data__metric-meta b")).toHaveText("+3");
    await expect(first.getByText("Stale", { exact: true })).toHaveCount(1);
    await expect(first).toContainText("Work Course introduction");
    await expect(first).toContainText("Edition Revised Course");
    await expect(first.locator(".workspace-app-data__record-media")).toHaveCount(0);
    const recordFiles = first.locator(".workspace-app-data__record-files");
    await expect(recordFiles).not.toHaveAttribute("open", "");
    await recordFiles.locator("summary").click();
    await expect(recordFiles).toHaveAttribute("open", "");
    await expect(recordFiles.getByRole("link", { name: "Video original", exact: true })).toHaveAttribute("href", "/viewer/lesson-video");
    const recordNotes = first.locator(".workspace-app-data__field--long");
    const recordNotesDisclosure = recordNotes.locator("details");
    await expect(recordNotesDisclosure).not.toHaveAttribute("open", "");
    await recordNotesDisclosure.locator("summary").click();
    await expect(recordNotesDisclosure).toHaveAttribute("open", "");
    await expect(recordNotes).toContainText("review early audience feedback");
    const relatedRun = first.locator(".workspace-app-data__execution-disclosure");
    await expect(relatedRun).not.toHaveAttribute("open", "");
    await expect(relatedRun).toContainText("Latest related run");
    await expect(first).not.toContainText("Recent execution");
    await expect(first).not.toContainText("Persist Receipt");
    await relatedRun.locator("summary").click();
    await expect(relatedRun).toHaveAttribute("open", "");
    await expect(relatedRun).toContainText("Completed");
    await expect(relatedRun.getByRole("link", { name: "Open run", exact: true })).toHaveAttribute("href", "/flows/lesson-flow/runs/run-publication-one");
    await expect(first.locator(".workspace-app-data__record-heading")).toHaveAttribute("href", "https://www.youtube.com/watch?v=courseintro");
    await expect(first.getByText("Not enough history to compare yet.", { exact: true })).toHaveCount(0);
    await expect(first.locator("time").filter({ hasText: /^Last collected \d+d ago$/ })).toHaveCount(3);
    await expect(first.getByRole("table")).toHaveCount(0);
    for (const text of ["—", "Tracking partial", "Publication identity conflict: The receipt references two external IDs."]) await expect(second).toContainText(text);
    await expect(second.getByText("Likes are unavailable.", { exact: true })).toBeVisible();
    await expect(second.locator(".workspace-app-data__metric-copy > strong")).toHaveAttribute("title", /Likes are unavailable\. No data/);
    await expect(second.locator(".workspace-app-data__metric-copy > strong")).toHaveAccessibleDescription("Likes: —. Likes are unavailable. No data");
    await expect(first).not.toContainText("Likes are unavailable.");
    await expect(second).not.toContainText("Course introduction");
    const report = page.getByRole("region", { name: "Weekly reports", exact: true }).getByRole("link");
    await expect(report).toContainText("Weekly reports");
    await expect(report).toHaveAttribute("href", "/viewer/report-document");
    await expect(report).not.toContainText("01M4TTSDVDZ670M0KKVXJRHJ4J");
    await expect(page.getByRole("heading", { name: "Archived drafts", exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`publication-records-${width}.png`), fullPage: true, animations: "disabled" });
  });
}

test("project choices distinguish editions without showing an empty comparison control", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.clock.setFixedTime(new Date("2026-10-14T12:00:00Z"));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?projects`);
  const picker = page.getByRole("combobox", { name: "Course", exact: true });
  await expect(picker).toBeVisible();
  const labels = await picker.locator("option").allTextContents();
  expect(labels).toHaveLength(2);
  expect(new Set(labels).size).toBe(2);
  for (const label of labels) {
    expect(label).toContain("Watercolor basics · Second edition");
    expect(label).not.toMatch(/2026|T\d{2}:|course-new|course-old/);
  }
  await expect(picker.locator("option").first()).toHaveAttribute("title", /2026/);
  await picker.selectOption("course-old");
  await expect(picker).toHaveValue("course-old");
  await expect(page.getByText("No results are available yet.", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Awaiting data", { exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Change over" })).toHaveCount(0);
  await expect(page.getByRole("tablist")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Publish content Start a new publication", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test("tracking sections hide empty or revoked data, recover when observed and expose fetch errors", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?tracking=empty`);
  await expect(page.getByTestId("fixture-query-state")).toHaveText("Ready");
  await expect(page.getByRole("region", { name: "Publication tracking", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Publish content Start a new publication", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Collect publication data", exact: true }).click();
  await expect(page.getByRole("region", { name: "Publication tracking", exact: true })).toBeVisible();
  await expect(page.locator(".workspace-app-data__record").filter({ hasText: "Lesson one" })).toContainText("0");
  await page.goto(`${fixture}?tracking=revoked`);
  await expect(page.getByTestId("fixture-query-state")).toHaveText("Ready");
  await expect(page.getByRole("region", { name: "Publication tracking", exact: true })).toHaveCount(0);
  await page.goto(`${fixture}?tracking=error`);
  await expect(page.getByRole("region", { name: "Publication tracking", exact: true })).toBeVisible();
  await expect(page.locator(".workspace-app-data__error")).toBeVisible();
});

test("receipt visibility keeps pending metrics and collection state without redundant headings", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?tracking=records&block=stale`);
  const tracking = page.getByRole("region", { name: "Publication tracking", exact: true });
  await expect(tracking).toBeVisible();
  await expect(tracking.getByText("Lesson two", { exact: true })).toBeVisible();
  await expect(tracking.getByText("Likes are unavailable.", { exact: true })).toBeVisible();
  await expect(tracking.getByRole("region", { name: "Published content", exact: true }).getByText("May be out of date", { exact: true })).toBeVisible();
  await expect(tracking.getByRole("heading", { name: "Published content", exact: true })).toHaveCount(0);
  await page.goto(`${fixture}?tracking=records&block=error`);
  await expect(tracking.getByText("Publication data unavailable.", { exact: true })).toBeVisible();
});

test("stacked data sections use configured order without tabs or repeated block headings", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?stack-groups`);
  const tracking = page.getByRole("region", { name: "Publication tracking", exact: true });
  await expect(tracking.getByRole("tablist")).toHaveCount(0);
  await expect(tracking.locator(".workspace-app-data__group-title")).toHaveText([
    "Publication data", "Course materials", "Weekly reports",
  ]);
  const materials = tracking.locator(".workspace-app-data__group").filter({ hasText: "Painting guide" });
  await expect(materials.getByRole("heading", { name: "Course materials", exact: true })).toHaveCount(1);
  await expect(materials.locator(".workspace-app-data__block > header > h4")).toHaveCount(0);
});

test("project summaries keep launch context compact and render safe links", async ({ page }) => {
  await page.setViewportSize({ width: 577, height: 900 });
  await page.clock.setFixedTime(new Date("2026-10-14T12:00:00Z"));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?summary`);
  await expect(page.getByRole("combobox", { name: "Course", exact: true })).toBeVisible();
  await expect(page.getByText("Course", { exact: true })).toHaveCount(1);
  const app = page.getByRole("region", { name: "Course", exact: true });
  await expect(app.getByText(/^Times shown in [A-Za-z_]+(?:\/[A-Za-z_]+)*$/, { exact: true })).toBeVisible();
  const context = page.getByRole("region", { name: "Campaign context", exact: true });
  await expect(context).toBeVisible();
  await expect(context.getByText("Release status", { exact: true })).toBeVisible();
  await expect(context.getByText("Unreleased", { exact: true })).toBeVisible();
  await expect(context.getByText("Release date", { exact: true })).toBeVisible();
  const blocker = context.locator(".workspace-app-data__summary-item--long");
  await expect(blocker).toContainText("The selected account still needs a verified connection");
  await expect(blocker).toHaveCSS("grid-column", "1 / -1");
  const blockerDisclosure = blocker.locator("details");
  await expect(blockerDisclosure).not.toHaveAttribute("open", "");
  await expect(blocker.getByText("Show more", { exact: true })).toBeVisible();
  await blockerDisclosure.locator("summary").click();
  await expect(blockerDisclosure).toHaveAttribute("open", "");
  await expect(blocker.getByText("Show less", { exact: true })).toBeVisible();
  await expect(blocker.locator(".workspace-app-data__summary-full")).toContainText("resume the approved schedule");
  await blockerDisclosure.locator("summary").click();
  await expect(blockerDisclosure).not.toHaveAttribute("open", "");
  const releaseDate = context.locator('time[datetime="2026-11-03"]');
  await expect(releaseDate).toHaveText(/Nov 3/);
  await expect(releaseDate).not.toHaveText(/2026/);
  await expect(releaseDate).toHaveAttribute("title", /2026/);
  await expect(context.getByRole("link", { name: "Artist site", exact: true })).toHaveAttribute("href", "https://example.com/listen");
  await expect(context.getByRole("link", { name: "Malformed URL", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Publish content Start a new publication", exact: true })).toBeVisible();
  await page.goto(`${fixture}?summary&block=stale`);
  await expect(context).toBeVisible();
  await expect(context.getByText("May be out of date", { exact: true })).toBeVisible();
  await page.goto(`${fixture}?summary&block=error`);
  await expect(context).toBeVisible();
  await expect(context.getByText("This view could not load its data.", { exact: true })).toBeVisible();
  await page.goto(`${fixture}?summary&cross-year`);
  const crossYearContext = page.getByRole("region", { name: "Campaign context", exact: true });
  await expect(crossYearContext.locator('time[datetime="2026-11-03"]')).toHaveText(/2026/);
  await expect(crossYearContext.locator('time[datetime="2027-01-17"]')).toHaveText(/2027/);
  await page.clock.setFixedTime(new Date("2027-10-14T12:00:00Z"));
  await page.goto(`${fixture}?summary`);
  await expect(page.getByRole("region", { name: "Campaign context", exact: true }).locator('time[datetime="2026-11-03"]')).toHaveText(/2026/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test("course reminder scheduling keeps stages, execution, and job lifecycle separate", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?schedule`);
  const plan = page.getByRole("region", { name: "Lesson plan", exact: true });
  await expect(plan).toContainText("4");
  await expect(plan).toContainText("Scheduled");
  await expect(plan).toContainText("Verification Required");
  await expect(plan.locator(".workspace-app-data__rollup-counts dd")).toHaveText(["2", "1", "1"]);
  await expect(plan).toContainText("Earliest pending");
  await expect(plan).toContainText("Lesson three review");
  const nextRun = plan.locator(".workspace-app-data__execution-disclosure");
  await expect(nextRun).not.toHaveAttribute("open", "");
  await expect(nextRun).toContainText("Execution details");
  await expect(plan).not.toContainText("Initialize");
  await nextRun.locator("summary").click();
  await expect(nextRun).toHaveAttribute("open", "");
  await expect(nextRun).toContainText("Completed");
  await expect(nextRun.getByRole("link", { name: "Open run", exact: true })).toHaveAttribute("href", "/flows/lesson-flow/runs/run-initialize");
  await expect(plan.locator(".workspace-app-data__field--long details")).toHaveCount(1);
  const execution = page.getByRole("region", { name: "Lesson execution", exact: true });
  await expect(execution).toContainText("Verification Required");
  const scheduledLesson = execution.locator(".workspace-app-data__progress li").filter({ hasText: "Lesson four" });
  await expect(scheduledLesson.locator(".workspace-app-data__platform")).toHaveText("TikTok@courses");
  await expect(scheduledLesson.locator('[data-integration-brand="tiktok"]')).toBeVisible();
  await expect(execution).toContainText("Execution Paused");
  await expect(execution).toContainText("Review Media");
  await expect(execution).toContainText("Needs a reviewer.");
  await expect(execution.locator(".workspace-app-data__field--long details")).toHaveCount(1);
  const scheduled = execution.locator("li", { hasText: "Lesson four" });
  await expect(scheduled).toContainText("Scheduled");
  const scheduledRun = scheduled.locator(".workspace-app-data__execution-disclosure");
  await expect(scheduledRun).not.toHaveAttribute("open", "");
  await expect(scheduledRun).toContainText("Execution details");
  await expect(scheduledRun.locator(".workspace-app-data__execution")).not.toBeVisible();
  await scheduledRun.locator("summary").click();
  await expect(scheduledRun).toHaveAttribute("open", "");
  await expect(scheduledRun).toContainText("Completed");
  await expect(scheduledRun.locator(".workspace-app-data__execution")).toBeVisible();
  await expect(scheduledRun.getByRole("link", { name: "Open run", exact: true })).toHaveAttribute("href", "/flows/lesson-flow/runs/run-finished");
  await page.goto(`${fixture}?schedule&long-run-error`);
  const longErrorExecution = page.getByRole("region", { name: "Lesson execution", exact: true }).locator("li", { hasText: "Lesson three review" });
  const longErrorDisclosure = longErrorExecution.locator(".workspace-app-data__execution-error-disclosure");
  await expect(longErrorDisclosure).not.toHaveAttribute("open", "");
  await expect(longErrorDisclosure.getByText("Show more", { exact: true })).toBeVisible();
  await expect(longErrorExecution).toContainText("Execution Paused");
  await expect(longErrorExecution.getByRole("link", { name: "Open run", exact: true })).toHaveAttribute("href", "/flows/lesson-flow/runs/run-review");
  await longErrorDisclosure.locator("summary").click();
  await expect(longErrorDisclosure).toContainText("resume the existing run");
  await expect(longErrorDisclosure.locator(".workspace-app-data__summary-preview")).not.toBeVisible();
  await expect(longErrorDisclosure.locator(".workspace-app-data__summary-full")).toBeVisible();
  const reminders = page.getByRole("region", { name: "Reminder checks", exact: true });
  const disabled = reminders.locator("li", { hasText: "Disabled reminder" });
  await expect(disabled).toContainText("Disabled");
  await expect(disabled).toContainText("Last run");
  await expect(disabled).toContainText("Completed");
  await expect(disabled).toContainText("Schedule ends");
  await expect(disabled).not.toContainText("Ended");
  const ended = reminders.locator("li", { hasText: "Course reminder" });
  await expect(ended).toContainText("Ended");
  await expect(ended).toContainText("Schedule ends");
  await expect(ended).not.toContainText("Next check");
  await page.goto(`${fixture}?schedule&no-rollup-first`);
  const rollupWithoutFirst = page.getByRole("region", { name: "Lesson plan", exact: true });
  await expect(rollupWithoutFirst).toContainText("4");
  await expect(rollupWithoutFirst).toContainText("Verification Required");
  await expect(rollupWithoutFirst.getByText("Earliest pending", { exact: true })).toHaveCount(0);
  await expect(rollupWithoutFirst.getByText("No pending work.", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.goto(`${fixture}?schedule&single-rollup`);
  const singleStageRollup = page.getByRole("region", { name: "Lesson plan", exact: true });
  await expect(singleStageRollup.getByText("Total items", { exact: true })).toHaveCount(0);
  await expect(singleStageRollup.getByText("Published", { exact: true })).toHaveCount(1);
  await expect(singleStageRollup.locator(".workspace-app-data__rollup-counts dd")).toHaveText(["2"]);
  await page.goto(`${fixture}?schedule&single-rollup&single-rollup-next`);
  const compactNextRollup = page.getByRole("region", { name: "Lesson plan", exact: true });
  await expect(compactNextRollup.getByText("Total items", { exact: true })).toHaveCount(0);
  await expect(compactNextRollup.getByText("Earliest pending", { exact: true })).toBeVisible();
  await expect(compactNextRollup.locator(".workspace-app-data__rollup.is-next-stage-only .workspace-app-data__rollup-counts dd")).toHaveText("2");
  await page.goto(`${fixture}?schedule&single-rollup=zero`);
  const zeroStageRollup = page.getByRole("region", { name: "Lesson plan", exact: true });
  await expect(zeroStageRollup.getByText("Total items", { exact: true })).toHaveCount(0);
  await expect(zeroStageRollup.getByText("Published", { exact: true })).toHaveCount(1);
  await expect(zeroStageRollup.locator(".workspace-app-data__rollup-counts dd")).toHaveText(["0"]);
  await page.goto(`${fixture}?schedule&single-rollup=zero&rollup-empty`);
  const configuredEmptyRollup = page.getByRole("region", { name: "Lesson plan", exact: true });
  await expect(configuredEmptyRollup.getByText("No lesson schedule yet.", { exact: true })).toBeVisible();
  await expect(configuredEmptyRollup.getByText("Published", { exact: true })).toHaveCount(0);
  await expect(configuredEmptyRollup.getByText("Total items", { exact: true })).toHaveCount(0);
  await page.goto(`${fixture}?schedule&single-rollup=partial`);
  const partialStageRollup = page.getByRole("region", { name: "Lesson plan", exact: true });
  await expect(partialStageRollup.getByText("Total items", { exact: true })).toHaveCount(1);
  await expect(partialStageRollup.getByText("Published", { exact: true })).toHaveCount(1);
  await expect(partialStageRollup.locator(".workspace-app-data__rollup-total strong")).toHaveText("2");
  await expect(partialStageRollup.locator(".workspace-app-data__rollup-counts dd")).toHaveText(["1"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test("training delivery uses configured completion and stage labels", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?training`);
  const progress = page.getByRole("region", { name: "Training delivery progress", exact: true });
  await expect(progress).toContainText("2 / 3");
  await expect(progress).toContainText("Delivered");
  await expect(progress).toContainText("Awaiting learner feedback");
  await expect(progress.getByText("Total items", { exact: true })).toHaveCount(0);
  await expect(progress.getByText("Earliest pending", { exact: true })).toHaveCount(0);
  const upcoming = page.getByRole("region", { name: "Upcoming training delivery", exact: true });
  await expect(upcoming.getByText("Module three feedback", { exact: true })).toBeVisible();
  await expect(upcoming.getByText("Awaiting learner feedback", { exact: true })).toBeVisible();
  await page.goto(`${fixture}?training&empty-schedule`);
  const emptyProgress = page.getByRole("region", { name: "Training delivery progress", exact: true });
  await expect(emptyProgress.getByText("No training delivery schedule yet.", { exact: true })).toBeVisible();
  await expect(emptyProgress.getByText("0 / 0", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test("inspection delivery shows its image and status without publication metadata or metrics", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?inspection`);
  const delivery = page.locator(".workspace-app-data__record", { hasText: "Kitchen cabinet inspection" });
  await expect(delivery).toBeVisible();
  await expect(delivery.getByText("Ready For Review", { exact: true })).toBeVisible();
  await expect(delivery.locator(".workspace-app-data__platform")).toHaveCount(0);
  await expect(delivery.locator(".workspace-app-data__metric")).toHaveCount(0);
  const image = delivery.locator(".workspace-app-data__record-media-item img");
  await expect(image).toHaveAttribute("alt", "Preview");
  await expect(delivery.getByText("Preview", { exact: true })).toHaveCount(0);
  await expect(delivery.locator(".workspace-app-data__record-media-item")).toHaveAttribute("href", "/viewer/inspection-image");
  const files = delivery.locator(".workspace-app-data__record-files");
  await expect(files).not.toHaveAttribute("open", "");
  await files.locator("summary").click();
  await expect(files.getByRole("link", { name: "Video original", exact: true })).toHaveAttribute("href", "/viewer/inspection-video");
  await expect(delivery.locator(".workspace-app-data__record-heading")).toHaveAttribute("href", "/records/inspection-delivery");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test("a workspace App retries a safe failed course delivery through the native run contract", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?schedule&recovery=safe`);
  const delivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  // Recovery authority is loaded when its row enters the viewport.
  await delivery.scrollIntoViewIfNeeded();
  const headingStatus = delivery.locator("li", { hasText: "Lesson three review" }).locator(".workspace-app-data__progress-heading em");
  await expect(headingStatus).toHaveText("Run failed");
  await expect(delivery.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await delivery.getByRole("button", { name: "Retry", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByTestId("fixture-retry-requests")).toHaveText("Retry requests: 1");
  await expect(delivery).toContainText("Execution Running");
  await expect(headingStatus).toHaveText("Verification Required");
  await expect(delivery.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
});

test("a workspace App retries a cancelled delivery when the server still grants retry", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?schedule&recovery=cancelled`);
  const delivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await delivery.scrollIntoViewIfNeeded();
  await expect(delivery.locator("li", { hasText: "Lesson three review" }).locator(".workspace-app-data__progress-heading em")).toHaveText("Cancelled");
  await expect(delivery).toContainText("Execution Cancelled");
  await expect(delivery.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await delivery.getByRole("button", { name: "Retry", exact: true }).click();
  await page.getByRole("dialog", { name: "Retry", exact: true }).getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByTestId("fixture-retry-requests")).toHaveText("Retry requests: 1");
  await expect(delivery).toContainText("Execution Running");
});

test("a workspace App can reconcile an unconfirmed lesson delivery through native retry", async ({ page }) => {
  await page.goto(`${fixture}?schedule&recovery=reconcile`);
  const delivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await delivery.scrollIntoViewIfNeeded();
  await delivery.getByRole("button", { name: "Review & retry", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Review & retry" });
  const submit = dialog.getByRole("button", { name: "Confirm & retry" });
  await expect(submit).toBeDisabled();
  await expect(dialog).toContainText("Retrying does not extend it");
  await dialog.getByRole("textbox", { name: "Verification evidence (20–2,000 characters)" }).fill("Verified provider delivery log and queue: no lesson was sent, and no operation is pending.");
  await expect(submit).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await submit.click();
  await expect(page.getByTestId("fixture-retry-requests")).toHaveText("Retry requests: 1");
  await expect(dialog).not.toBeVisible();
  await expect(delivery.getByRole("button", { name: "Review & retry", exact: true })).toHaveCount(0);
});

test("a workspace App discloses failure details and never retries unknown outcomes or read-only runs", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?schedule&recovery=review`);
  const reviewDelivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await reviewDelivery.scrollIntoViewIfNeeded();
  await reviewDelivery.getByRole("button", { name: "Retry", exact: true }).click();
  const unavailable = page.getByRole("dialog", { name: "Retry", exact: true });
  await expect(unavailable.getByRole("alert")).toContainText("Retry is no longer available here.");
  await expect(unavailable.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
  await unavailable.getByRole("button", { name: "Close", exact: true }).click();
  await expect(reviewDelivery.getByRole("link", { name: "View run details", exact: true })).toHaveAttribute("href", "/flows/lesson-flow/runs/course-recovery-review");
  const failure = reviewDelivery.getByText("The external delivery result must be reviewed before another attempt.", { exact: true });
  await expect(failure).not.toBeVisible();
  await expect(reviewDelivery.getByText("Prepare Lesson", { exact: true })).not.toBeVisible();
  await reviewDelivery.getByText("Failure details", { exact: true }).click();
  await expect(failure).toBeVisible();
  await expect(failure).toHaveCount(1);
  await expect(reviewDelivery.getByText("Prepare Lesson", { exact: true })).toBeVisible();
  for (const width of [390, 900]) {
    await page.setViewportSize({ width, height: 900 });
    const item = reviewDelivery.locator("li").filter({ hasText: "Lesson three review" });
    const itemBox = await item.boundingBox();
    const detailsBox = await item.locator(".workspace-app-data__failure-body").boundingBox();
    expect(detailsBox!.width).toBeGreaterThan(itemBox!.width * 0.9);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  }

  await page.goto(`${fixture}?schedule&recovery=blocked`);
  const blockedDelivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await blockedDelivery.scrollIntoViewIfNeeded();
  await blockedDelivery.getByRole("button", { name: "Retry", exact: true }).click();
  const blocked = page.getByRole("dialog", { name: "Retry", exact: true });
  await expect(blocked.getByRole("alert")).toContainText("Retry is no longer available here.");
  await expect(blocked.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
  await blocked.getByRole("button", { name: "Close", exact: true }).click();
  await blockedDelivery.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(blocked.getByRole("alert")).toContainText("Retry is no longer available here.");
  await expect(blocked.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
  await expect(page.getByTestId("fixture-retry-requests")).toHaveText("Retry requests: 0");

  await page.goto(`${fixture}?schedule&recovery=readonly`);
  const readonlyDelivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await readonlyDelivery.scrollIntoViewIfNeeded();
  await expect(readonlyDelivery.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
  await expect(readonlyDelivery.getByRole("link", { name: "Open run", exact: true })).toHaveAttribute("href", "/flows/lesson-flow/runs/course-recovery-readonly");
  await expect(page.getByTestId("fixture-retry-requests")).toHaveText("Retry requests: 0");
});

test("a workspace App revalidates retry authorization before retrying", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?schedule&recovery=stale`);
  const delivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await delivery.scrollIntoViewIfNeeded();
  await expect(delivery.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Expire retry authorization", exact: true }).click();
  await delivery.getByRole("button", { name: "Retry", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("alert")).toContainText("Retry is no longer available here.");
  await expect(page.getByTestId("fixture-retry-requests")).toHaveText("Retry requests: 0");
});

test("a workspace App reviews a native TikTok approval, retains its draft after failure, and retries the same request", async ({ page }) => {
  await page.route("https://tiktok.fixture/**", (route) => route.fulfill({
    contentType: "image/gif",
    body: Buffer.from("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==", "base64"),
  }));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?schedule&recovery=tiktok`);
  const delivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await delivery.scrollIntoViewIfNeeded();
  await delivery.getByRole("button", { name: "Review request", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Review before continuing", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Posting to", { exact: false })).toBeVisible();
  await expect(page.getByTestId("fixture-tiktok-review-refreshes")).toHaveText("TikTok review refreshes: 1");
  await dialog.getByRole("button", { name: "Use workspace-app-cover.gif as cover photo", exact: true }).click();
  await dialog.getByRole("textbox", { name: "TikTok caption" }).fill("Draft retained after a network error");
  await dialog.getByRole("button", { name: "TikTok privacy", exact: true }).click();
  await page.getByRole("option", { name: "Everyone", exact: true }).click();
  await dialog.getByRole("checkbox", { name: "I agree to TikTok’s Music Usage Confirmation", exact: true }).check();
  await dialog.getByRole("checkbox", { name: "I consent to upload and post these photos to the TikTok account shown above with these settings.", exact: true }).check();
  const post = dialog.getByRole("button", { name: "Post to TikTok", exact: true });
  await expect(post).toBeEnabled();
  await post.click();
  await expect(page.getByTestId("fixture-runtime-approval-requests")).toHaveText("Runtime approval requests: 1");
  await expect(page.getByTestId("fixture-runtime-approval-payload")).toContainText('"run_id":"course-recovery-tiktok"');
  await expect(page.getByTestId("fixture-runtime-approval-payload")).toContainText('"approval_id":"workspace-app-native-tiktok"');
  await expect(page.getByTestId("fixture-runtime-approval-payload")).toContainText('"choice":"approve"');
  await expect(page.getByTestId("fixture-runtime-approval-payload")).toContainText('"caption":"Draft retained after a network error"');
  await expect(dialog.getByRole("alert")).toContainText("first Workspace App approval attempt was rejected");
  await expect(dialog.getByRole("textbox", { name: "TikTok caption" })).toHaveValue("Draft retained after a network error");
  await expect(post).toBeEnabled();
  await post.click();
  await expect(page.getByTestId("fixture-runtime-approval-requests")).toHaveText("Runtime approval requests: 2");
  await expect(dialog).not.toBeVisible();
  await expect(delivery).toContainText("Execution Running");
});

test("a workspace App keeps ordinary approvals generic and leaves read-only or background waits inert", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(`${fixture}?schedule&recovery=ordinary-paused`);
  const delivery = page.getByRole("region", { name: "Lesson execution", exact: true });
  await delivery.scrollIntoViewIfNeeded();
  await delivery.getByRole("button", { name: "Review request", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Review before continuing", exact: true });
  await expect(dialog.getByRole("button", { name: "Approve", exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Post to TikTok", exact: true })).toHaveCount(0);
  await dialog.getByRole("button", { name: "Close", exact: true }).click();

  for (const recovery of ["paused-readonly", "background-wait"]) {
    await page.goto(`${fixture}?schedule&recovery=${recovery}`);
    await page.getByRole("region", { name: "Lesson execution", exact: true }).scrollIntoViewIfNeeded();
    await expect(page.getByRole("region", { name: "Lesson execution", exact: true }).getByRole("button", { name: "Review request", exact: true })).toHaveCount(0);
    await expect(page.getByTestId("fixture-runtime-approval-requests")).toHaveText("Runtime approval requests: 0");
  }
});
