import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

// Current source + isolated PostgreSQL + real HTTP/Celery publication.
// Downstream workers are deliberately disabled: this proves admission/UI, not
// LLM compliance or external publication. See support/task-recovery-server.py.
test.skip(process.env.E2E_TASK_RECOVERY !== "1", "Requires the isolated task recovery server");
const API = process.env.E2E_API ?? "http://127.0.0.1:18920";
let api: APIRequestContext;
const browserErrors = new WeakMap<Page, string[]>();
test.beforeEach(async ({ page }) => {
  const errors: string[] = [];
  browserErrors.set(page, errors);
  page.on("pageerror", error => errors.push(error.message));
});
test.afterEach(async ({ page }) => { expect(browserErrors.get(page)).toEqual([]); });

test.beforeAll(async () => {
  expect(new URL(API).hostname).toMatch(/^(127\.0\.0\.1|localhost)$/);
  const unauthenticated = await request.newContext({ baseURL: API });
  const login = await unauthenticated.post("/api/v1/auth/login", {
    data: { email: "demo@manor.local", password: "manor-demo" },
  });
  expect(login.ok()).toBeTruthy();
  api = await request.newContext({ baseURL: API, extraHTTPHeaders: {
    Authorization: `Bearer ${(await login.json()).access_token}`,
  } });
  await unauthenticated.dispose();
});
test.afterAll(async () => { await api?.dispose(); });

async function fixture(kind: string) {
  const response = await api.post("/__qa/fixtures", { data: { kind } });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}
async function state(id: string) { return (await api.get(`/__qa/state/${id}`)).json(); }
async function login(page: Page) {
  await page.goto("/login");
  await page.getByRole("textbox", { name: "name@company.com" }).fill("demo@manor.local");
  await page.getByRole("textbox", { name: "••••••••" }).fill("manor-demo");
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/);
  const reject = page.getByRole("button", { name: "Reject all", exact: true });
  if (await reject.isVisible()) await reject.click();
  const skip = page.getByRole("button", { name: "Skip", exact: true });
  await expect(skip).toBeVisible();
  await skip.click();
}

test("first execution: ownerless team task publishes one Planner job after status change", async ({ page }, info) => {
  const f = await fixture("initial");
  await login(page);
  await page.goto(`/tasks/${f.task_id}`);
  await page.getByRole("button", { name: "Pending", exact: true }).click();
  const response = page.waitForResponse(r => r.url().includes(`/tasks/${f.task_id}`) && r.request().method() === "PUT");
  await page.getByRole("option", { name: "In Progress", exact: true }).click();
  expect((await response).ok()).toBeTruthy();
  await expect.poll(async () => (await state(f.task_id)).status).toBe("in_progress");
  const saved = await state(f.task_id);
  expect(saved.owner).toBeNull();
  expect(saved.published).toHaveLength(1);
  expect(saved.published[0].name).toContain("plan_and_run_task");
  await page.reload();
  await expect(page.getByRole("button", { name: "In Progress", exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("initial.png"), fullPage: true });
});

test("Task detail retry: completed legacy Plan needs no owner and is not replayed", async ({ page }, info) => {
  const f = await fixture("recovery");
  await login(page);
  await page.goto(`/tasks/${f.task_id}`);
  const response = page.waitForResponse(r => r.url().endsWith(`/tasks/${f.task_id}/retry`));
  await page.getByRole("button", { name: "Retry task", exact: true }).click();
  expect((await response).status()).toBe(200);
  const saved = await state(f.task_id);
  expect(saved.status).toBe("in_progress");
  expect(saved.owner).toBeNull();
  expect(saved.published).toHaveLength(1);
  expect(saved.published[0].name).toContain("plan_and_run_task");
  expect(saved.details._replan_context.prior_plan_id).toBe(f.plan_id);
  expect(saved.plans).toEqual([{ id: f.plan_id, status: "completed" }]);
  expect(saved.cards.find((c: { id: string }) => c.id === f.message_id)?.resolved).toBe(true);
  expect((await api.post(`/api/v1/tasks/${f.task_id}/retry`, { data: {} })).status()).toBe(409);
  expect((await state(f.task_id)).published).toHaveLength(1);
  await page.reload();
  await expect(page.getByRole("heading", { name: /Browser QA recovery/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry task", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "In Progress", exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("task-retried.png"), fullPage: true });
});

for (const mobile of [false, true]) {
  test(`Chat recovery ${mobile ? "mobile" : "desktop"}: readable reason, keyboard retry, resolved after reload`, async ({ page }, info) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    const f = await fixture("recovery");
    await login(page);
    await page.goto(`/chat?workspace=${f.workspace_id}`);
    await expect(page.getByText(/仅产出草稿，没有发布回执/)).toBeVisible();
    await expect(page.locator("body")).not.toContainText("publish_post");
    await expect(page.locator("body")).toContainText(/Publish Post/i);
    const retry = page.getByRole("button", { name: "Retry", exact: true });
    await expect(retry).toBeEnabled();
    await retry.focus();
    await expect(retry).toBeFocused();
    await page.screenshot({ path: info.outputPath("recovery-before.png"), fullPage: true });
    const response = page.waitForResponse(r => r.url().endsWith(`/messages/${f.message_id}/resolve`));
    await retry.press("Enter");
    expect((await response).status()).toBe(200);
    const saved = await state(f.task_id);
    expect(saved.status).toBe("in_progress");
    expect(saved.owner).toBeNull();
    expect(saved.published).toHaveLength(1);
    expect(saved.published[0].name).toContain("plan_and_run_task");
    await page.reload();
    await expect(page.locator(".chat-hitl-resolved")).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
    expect((await state(f.task_id)).status).toBe("in_progress");
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({ path: info.outputPath("recovery-after.png"), fullPage: true });
  });
}

test("unavailable team: readable failure remains retryable without dispatch", async ({ page }, info) => {
  const f = await fixture("unavailable");
  await login(page);
  await page.goto(`/chat?workspace=${f.workspace_id}`);
  const response = page.waitForResponse(r => r.url().endsWith(`/messages/${f.message_id}/resolve`));
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  expect((await response).status()).toBe(409);
  await expect(page.getByText(/No reusable execution plan or active task assignee/)).toBeVisible();
  await expect(page.locator("body")).not.toContainText("owner subscription");
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeEnabled();
  const saved = await state(f.task_id);
  expect(saved.status).toBe("waiting_on_customer");
  expect(saved.published).toEqual([]);
  expect(saved.cards.some((c: { resolved: boolean }) => !c.resolved)).toBeTruthy();
  await page.screenshot({ path: info.outputPath("unavailable.png"), fullPage: true });
});

test("cancel recovery: no dispatch and shared card stays resolved", async ({ page }, info) => {
  const f = await fixture("cancel");
  await login(page);
  await page.goto(`/chat?workspace=${f.workspace_id}`);
  const response = page.waitForResponse(r => r.url().endsWith(`/messages/${f.message_id}/resolve`));
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect((await response).status()).toBe(200);
  expect((await state(f.task_id)).status).toBe("cancelled");
  expect((await state(f.task_id)).published).toEqual([]);
  await page.reload();
  await expect(page.locator(".chat-hitl-resolved")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("cancelled.png"), fullPage: true });
});

for (const choice of ["approve", "request_changes"]) {
  test(`approval ${choice}: exact decision persists, shared card closes, replay is a no-op`, async ({ page }, info) => {
    const f = await fixture("approval");
    await login(page);
    await page.goto(`/tasks/${f.task_id}`);
    await page.getByPlaceholder("Optional note for the agent, e.g. approved with changes, or what needs revision...")
      .fill("Browser QA review only; no external publishing permission.");
    const response = page.waitForResponse(r => r.url().includes(`/tasks/${f.task_id}/approval`) && r.request().method() === "POST");
    await page.getByRole("button", {
      name: choice === "approve" ? "Approve and continue" : "Request changes", exact: true,
    }).click();
    expect((await response).status()).toBe(200);
    const saved = await state(f.task_id);
    expect(saved.status).toBe("completed");
    expect(saved.details.approval_decision.choice).toBe(choice);
    expect(saved.details.approval_decision.approved).toBe(choice === "approve");
    expect(saved.published).toEqual([]);
    expect(saved.cards.find((c: { id: string }) => c.id === f.message_id)?.resolved).toBe(true);
    await page.reload();
    await expect(page.getByRole("heading", { name: /Browser QA approval/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Approve and continue", exact: true })).toHaveCount(0);
    await page.goto(`/chat?workspace=${f.workspace_id}`);
    await expect(page.locator(".chat-hitl-resolved")).toContainText(choice === "approve" ? "Approved" : "Revisions requested");
    const replay = await api.post(`/api/v1/workspaces/${f.workspace_id}/chat/messages/${f.message_id}/resolve`, { data: { choice } });
    // The resolved-card endpoint intentionally acknowledges replay without
    // reapplying the decision; Task-level approval rejects a closed Task.
    expect(replay.status()).toBe(200);
    expect((await replay.json()).resolution.choice).toBe(choice);
    expect((await state(f.task_id)).details.approval_decision).toEqual(saved.details.approval_decision);
    expect((await state(f.task_id)).published).toEqual([]);
    expect((await api.post(`/api/v1/tasks/${f.task_id}/approval`, { data: { choice } })).status()).toBe(409);
    await page.screenshot({ path: info.outputPath(`${choice}.png`), fullPage: true });
  });
}
