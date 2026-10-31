import { expect, test, type Page } from "@playwright/test";

const run = (overrides: Record<string, unknown> = {}) => ({
  run_id: "run-1", workflow_id: "workflow", binding_id: "binding", title: "Weekly report", status: "completed",
  business_outcome: null, trigger_source: "manual", created_at: "2026-10-10T09:00:00Z",
  started_at: "2026-10-10T09:00:00Z", completed_at: "2026-10-10T09:03:00Z",
  files: [{ document_id: "doc1", name: "Weekly W41.docx", open_url: "/viewer/doc1" }], file_count: 1,
  ...overrides,
});

const section = (id: string, block: string, source: string | null, extra: Record<string, unknown> = {}) => ({
  id, block, source, view: block === "outputs" ? "list" : block === "queue" ? "list" : "default",
  limit: block === "outputs" ? 8 : null, implicit: true, available: true, source_title: null, ...extra,
});

// A 1×1 PNG served as every document thumbnail.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const metric = (overrides: Record<string, unknown> = {}) => ({
  kind: "stat", id: "orders", title: "Orders today", value_type: "number", unit: null, value: 128,
  updated_at: "2026-10-10T09:00:00Z", freshness_status: "fresh", goal: null,
  series: [{ at: "2026-10-08T09:00:00Z", value: 96 }, { at: "2026-10-09T09:00:00Z", value: 110 }, { at: "2026-10-10T09:00:00Z", value: 128 }],
  ...overrides,
});

async function mockApp(page: Page, { sections, outputs, queue, metrics = [], suggestions = [], requirements = [] }: {
  sections: unknown[]; outputs: unknown[]; queue: unknown[]; metrics?: unknown[]; suggestions?: unknown[]; requirements?: unknown[];
}) {
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/thumbnail")) return route.fulfill({ body: PNG, contentType: "image/png" });
    if (path.endsWith("/stats/views/observations")) return route.fulfill({ json: { items: [
      { id: "o2", stat_id: "views", value: 12480, observed_at: "2026-10-10T09:00:00Z", window_start: null, window_end: null,
        source: "agent_review", collector_revision: 1,
        evidence: { status: "measured", reasoning: "Read the latest video's view count from the creator dashboard.",
          references: [{ document_id: "shot1", updated_at: "2026-10-10T09:00:00Z", locator: "Dashboard", reason: "Dashboard screenshot" }], gaps: [] } },
      { id: "o1", stat_id: "views", value: 9800, observed_at: "2026-10-09T09:00:00Z", window_start: null, window_end: null,
        source: "agent_review", collector_revision: 1, evidence: null },
    ] } });
    if (path.endsWith("/run_now")) return route.fulfill({ status: 202, json: { job_id: "audience-job", queued_at: "2026-10-10T09:10:00Z", idempotency_key: "k" } });
    if (path.endsWith("/app-view/sections/outputs") || path.endsWith("/app-view/sections/reports")) {
      return route.fulfill({ json: { id: path.split("/").pop(), block: "outputs", items: outputs } });
    }
    if (path.endsWith("/app-view/sections/queue")) return route.fulfill({ json: { id: "queue", block: "queue", items: queue } });
    if (path.endsWith("/app-view/sections/metrics")) return route.fulfill({ json: { id: "metrics", block: "metrics", items: metrics, suggestions } });
    // The server's connections block returns what is still missing (its own test covers that).
    if (path.endsWith("/app-view/sections/connections")) {
      return route.fulfill({ json: { id: "connections", block: "connections", items: requirements } });
    }
    if (path.endsWith("/stats/assessed-metrics")) return route.fulfill({ status: 201, json: { items: [], job_id: "stat-review:app-fixture", schedule_enabled: false, service_key: "analyst" } });
    if (path.endsWith("/app-view")) return route.fulfill({ json: {
      workspace_id: "app-fixture", title: "Studio", description: "Prepare and review articles",
      status: "active", can_start: true, hero: "workflow:binding",
      config: { visual: "plain", layout: "feature" },
      actions: [{ key: "workflow:binding", kind: "workflow", binding_id: "binding", workflow_id: "workflow",
        title: "Article Flow", description: "Start with a topic", inputs: [] }],
      sections, agent_options: [], workflow_options: [], automation_options: [],
    } });
    return route.fulfill({ json: [] });
  });
}

const app = (page: Page) => page.locator('.workspace-chat-output-panes [data-pane-id="app"]');

test("default App page shows pending decisions and recent outputs after its actions", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockApp(page, {
    sections: [section("actions", "actions", null), section("queue", "queue", "approvals:pending"),
      section("metrics", "metrics", "workspace"), section("outputs", "outputs", "workspace")],
    outputs: [
      run({ publications: [
        { platform: "linkedin", title: "Launch post", published_url: "https://www.linkedin.com/feed/update/1", verification_status: "verified" },
        { platform: "x", title: "Launch thread", verification_status: "unverified" },
      ] }),
      run({ run_id: "run-2", status: "running", files: [], file_count: 0, title: "Weekly report", progress: {
        stages: [{ name: "Draft", status: "completed" }, { name: "Check source sufficiency", status: "running" },
          { name: "Name", status: "pending" }, { name: "Save", status: "pending" }],
        stage_count: 4, done_count: 1,
        current: { id: "check", name: "Check source sufficiency", index: 1, status: "running", waiting: false },
      } }),
    ],
    queue: [{ key: "decision-1", message_id: "message-1", source_id: "message-1", kind: "governance_approval",
      summary: "Send Weekly W41 to 3 recipients", created_at: "2026-10-10T09:05:00Z" }],
    metrics: [metric(), metric({ id: "refunds", title: "Refund rate", value_type: "percent", value: 2.5,
      goal: { id: "goal", title: "Refunds under 2%", target: 2, baseline: 4, deadline: null, pace_status: "behind", status: "active" } }),
    metric({ id: "views", title: "Latest video views", value: 12480, collector_type: "agent_review", freshness_status: "assessing",
      gaps: ["TikTok is not connected"], next_assessment_at: "2026-10-11T03:00:00Z" }),
    // An older server omits the newer list fields; the tile still renders.
    { kind: "stat", id: "legacy", title: "Comments", value_type: "number", unit: null, value: 152, updated_at: null,
      freshness_status: "fresh", goal: null }],
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");

  const queue = app(page).getByRole("region", { name: "Needs you" });
  const tracking = app(page).getByRole("region", { name: "Key metrics" });
  const outputs = app(page).getByRole("region", { name: "Recent outputs" });
  await expect(queue.getByText("Send Weekly W41 to 3 recipients")).toBeVisible();
  await expect(tracking.getByRole("button", { name: /Orders today\s*128/ })).toBeVisible();
  await expect(tracking.locator(".workspace-app-metric__spark")).toHaveCount(3);
  const refunds = tracking.getByRole("button", { name: /Refund rate/ });
  await expect(refunds.getByText("2.5%", { exact: true })).toBeVisible();
  await expect(refunds.getByText(/Target 2% · 75%/)).toBeVisible();
  await expect(refunds.getByText("Behind", { exact: true })).toBeVisible();
  await expect(tracking.getByRole("button", { name: /Orders today/ }).getByText("↑ 16.4%")).toBeVisible();
  const views = tracking.getByRole("button", { name: /Latest video views/ });
  // A tile's one footer line carries its state; gaps stay in the detail drawer.
  await expect(views.locator(".workspace-app-metric__foot")).toHaveText("Assessing evidence");
  await expect(views.locator(".workspace-app-metric__dot--active")).toHaveCount(1);
  await expect(views.getByText("TikTok is not connected")).toHaveCount(0);
  await expect(tracking.getByRole("button", { name: /Comments\s*152/ })).toBeVisible();
  await expect(outputs.getByRole("link", { name: /Weekly W41\.docx/ })).toHaveAttribute("href", "/viewer/doc1");
  await expect(outputs.locator(".workspace-app-status--active")).toHaveText(/^Running/);
  // Stage progress and publications come from the run summary projection.
  await expect(outputs.getByText("Step 2 of 4 · Check source sufficiency")).toBeVisible();
  await expect(outputs.locator(".workspace-app-outputs__stages i.is-done")).toHaveCount(1);
  await expect(outputs.getByRole("link", { name: "linkedin · Launch post ↗" })).toHaveAttribute("href", "https://www.linkedin.com/feed/update/1");
  await expect(outputs.getByText("Not verified yet")).toBeVisible();
  // The four zones, always in this order: notices, functions, data, outputs.
  const order = await app(page).locator("section[aria-label]").evaluateAll(
    (nodes) => nodes.map((node) => node.getAttribute("aria-label")),
  );
  expect(order.indexOf("Needs you")).toBeLessThan(order.indexOf("Functions"));
  expect(order.indexOf("Functions")).toBeLessThan(order.indexOf("Key metrics"));
  expect(order.indexOf("Key metrics")).toBeLessThan(order.indexOf("Recent outputs"));
  await expect(app(page).getByRole("region", { name: "Functions" }).locator(".workspace-app-view__hero")).toHaveCount(1);
  await app(page).screenshot({ path: testInfo.outputPath("workspace-app-sections.png"), animations: "disabled" });

  // Deciding happens on the chat card, so a queue item opens the chat there.
  await queue.getByRole("button", { name: /Send Weekly W41/ }).click();
  await expect(app(page)).toBeHidden();
  expect(errors).toEqual([]);
});

test("a tracking tile opens its readings and evidence, and can start an assessment", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockApp(page, {
    sections: [section("metrics", "metrics", "workspace", { implicit: false }), section("actions", "actions", null, { implicit: false })],
    outputs: [], queue: [],
    metrics: [metric({ id: "views", title: "Latest video views", value: 12480, collector_type: "agent_review", gaps: [],
      assessment: { next_run_at: "2026-10-11T03:00:00Z", job_id: "audience-job", workflow_id: "review", run_id: "review-run", run_status: "completed" } })],
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  // Opening a number shows its detail instead of navigating to settings.
  await app(page).getByRole("button", { name: /Latest video views/ }).click();
  const drawer = page.getByRole("dialog", { name: "Latest video views" });
  await expect(drawer.getByText("Read the latest video's view count from the creator dashboard.")).toBeVisible();
  await expect(drawer.getByRole("link", { name: "Dashboard screenshot" })).toHaveAttribute("href", "/viewer/shot1");
  await expect(drawer.getByText(/^Next assessment /)).toBeVisible();
  await expect(drawer.getByRole("link", { name: "See the latest assessment" })).toBeVisible();
  const started = page.waitForRequest((request) => request.url().endsWith("/jobs/audience-job/run_now") && request.method() === "POST");
  await drawer.getByRole("button", { name: "Assess now" }).click();
  await started;
  await expect(drawer.getByRole("button", { name: "Manage stats" })).toBeVisible();
});

test("tracking shows a few headline tiles and lists the other numbers as rows", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const waiting = (id: string, title: string) => metric({ id, title, value: null, series: [], updated_at: null,
    collector_type: "agent_review", freshness_status: "not_assessed", next_assessment_at: "2026-10-11T03:00:00Z" });
  await mockApp(page, {
    sections: [section("actions", "actions", null), section("metrics", "metrics", "workspace")],
    outputs: [], queue: [],
    metrics: [
      waiting("followers", "Followers"),
      ...["Orders", "Refunds", "Visits", "Signups", "Revenue"].map((title, index) => metric({ id: `m${index}`, title })),
      waiting("likes", "Latest video likes"),
    ],
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  const tracking = app(page).getByRole("region", { name: "Key metrics" });
  await expect(tracking.locator(".workspace-app-metric")).toHaveCount(4);
  await expect(tracking.getByText(/^Next reading /)).toBeVisible();
  // Readings past the headline come before numbers still waiting, which keep a dash.
  const rows = tracking.locator(".workspace-app-metric-row");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("Revenue");
  await expect(rows.nth(0).locator(".workspace-app-metric-row__value")).toHaveText("128");
  await expect(rows.nth(1)).toContainText("Followers");
  await expect(rows.nth(1).locator(".workspace-app-metric-row__value")).toHaveText("—");
  await expect(rows.nth(1)).toContainText("Waiting for a first reading");
  // A waiting number still opens its own detail.
  await rows.nth(1).click();
  await expect(page.getByRole("dialog", { name: "Followers" })).toBeVisible();
});

test("a media Workspace with nothing tracked offers audience metrics", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockApp(page, {
    sections: [section("actions", "actions", null), section("metrics", "metrics", "workspace")],
    outputs: [], queue: [],
    suggestions: [{ key: "latest_video_views", name: "Latest video views", unit: "views" },
      { key: "followers", name: "Followers", unit: "followers" }, { key: "active_users", name: "Active users (7 days)", unit: "users" }],
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  const tracking = app(page).getByRole("region", { name: "Key metrics" });
  await expect(tracking.getByRole("button", { name: "Latest video views" })).toHaveAttribute("aria-pressed", "true");
  // Active users is opt-in.
  await expect(tracking.getByRole("button", { name: "Active users (7 days)" })).toHaveAttribute("aria-pressed", "false");
  await tracking.getByRole("button", { name: "Followers" }).click();
  const request = page.waitForRequest((r) => r.url().endsWith("/stats/assessed-metrics") && r.method() === "POST");
  await tracking.getByRole("button", { name: "Start tracking" }).click();
  expect((await request).postDataJSON()).toEqual({ templates: ["latest_video_views"], cadence: "daily" });
});

test("connections the Blueprint needs are set up from the App", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const requirement = (key: string, kind: string, provider: string, label: string, required: boolean, ready = false) => ({
    key, kind, provider, label, required, ready, reason: "", service_keys: [], setup_kind: null,
  });
  await mockApp(page, {
    sections: [section("connections", "connections", null), section("actions", "actions", null)], outputs: [], queue: [],
    requirements: [
      requirement("integration:youtube:0", "integration", "youtube", "YouTube", true),
      requirement("channel:0:telegram", "channel", "telegram", "Telegram", false),
    ],
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  const connections = app(page).getByRole("region", { name: "Needs you" });
  // The required one up front, optional ones folded away.
  await expect(connections.getByRole("heading", { name: "Needs you · 1" })).toBeVisible();
  await expect(connections.getByRole("listitem").filter({ hasText: "YouTube" }).getByRole("button", { name: "Connect" })).toBeVisible();
  await expect(connections.getByText("Telegram")).toBeHidden();
  await connections.getByText("Optional connections · 1").click();
  await expect(connections.getByRole("listitem").filter({ hasText: "Telegram" }).getByRole("button", { name: "Connect" })).toBeVisible();
});

test("empty default blocks stay hidden and configured blocks explain themselves", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await mockApp(page, {
    sections: [section("actions", "actions", null), section("queue", "queue", "approvals:pending"),
      section("metrics", "metrics", "workspace"), section("outputs", "outputs", "workspace")],
    outputs: [], queue: [],
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  await expect(page.getByText("Article Flow", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Recent outputs" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Needs you" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Key metrics" })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test("configured gallery and a removed flow source", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockApp(page, {
    sections: [
      section("reports", "outputs", "workflow:binding", { view: "gallery", implicit: false, source_title: "Article Flow" }),
      section("gone", "outputs", "workflow:removed", { implicit: false, available: false }),
      section("actions", "actions", null, { implicit: false }),
    ],
    outputs: [run({ files: [{ document_id: "doc1", name: "Weekly W41.docx" }, { document_id: "img1", name: "Cover.png" },
      { document_id: "clip1", name: "Teaser.mp4" }, { document_id: "song1", name: "Theme.mp3" }], file_count: 4 }),
    run({ run_id: "run-2", status: "running", files: [], file_count: 0 })],
    queue: [],
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  // A flow-scoped section is named after its flow.
  const gallery = app(page).getByRole("region", { name: "Article Flow" }).locator(".workspace-app-gallery");
  await expect(gallery.getByRole("link", { name: /Weekly W41\.docx/ })).toBeVisible();
  await expect(gallery.locator(".workspace-app-gallery__pulse")).toHaveCount(1);
  // Previewable files show their thumbnail; audio keeps its icon; video is marked as playable.
  await expect(gallery.locator("img.workspace-app-gallery__thumb")).toHaveCount(3);
  await expect(gallery.getByRole("link", { name: /Theme\.mp3/ }).locator("img")).toHaveCount(0);
  await expect(gallery.getByRole("link", { name: /Teaser\.mp4/ }).locator(".workspace-app-gallery__badge")).toBeVisible();
  await expect(app(page).getByText("The flow this section shows is no longer in this Workspace.")).toBeVisible();
  await app(page).screenshot({ path: testInfo.outputPath("workspace-app-gallery.png"), animations: "disabled" });
});
