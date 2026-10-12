import { expect, test, type Page, type Route } from "@playwright/test";

const WORKSPACE = "proposal-approval-fixture";
const fixtureUrl = "/e2e/fixtures/workspace-proposal-approval.html";

function proposal(id: string) {
  const tasks = [1, 2].map((n) => ({
    task_id: `${id}-task-${n}`, title: `${id} assessment ${n}`,
    description: "Prepare an assessment for the student.", priority: 4,
  }));
  return {
    id, conversation_id: "fixture-conversation", created_at: `2026-08-31T12:0${id === "proposal-a" ? "1" : "2"}:00Z`,
    body: "Prepare the next assessments.", message_kind: "proposal", author_kind: "agent",
    author_subscription_id: null, refs: [], attachments: [], meta: { proposal: { tasks } },
    pending_action: {
      kind: "approve_proposals", tasks, task_ids: tasks.map((t) => t.task_id),
      task_titles: tasks.map((t) => t.title), options: ["approve", "always_approve", "reject"],
    },
    resolved_at: null as string | null,
    resolution: null as { choice: string; payload?: Record<string, unknown> } | null,
  };
}

async function mockWorkspace(page: Page) {
  const messages = [proposal("proposal-a"), proposal("proposal-b")];
  const requests: { id: string; body: Record<string, any> }[] = [];
  const pending = new Map<string, Route>();
  const unexpected: string[] = [];
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    localStorage.removeItem("manor_token");
  });
  await page.route("**/config", (route) => route.fulfill({ json: {
    deployment_mode: "oss", environment: "e2e", fs_enabled: false,
    flows_available: false, flows_released: false, ai_credits_unlimited: true,
  } }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const resolution = path.match(/\/chat\/messages\/(proposal-[ab])\/resolve$/);
    if (resolution && request.method() === "POST") {
      requests.push({ id: resolution[1], body: request.postDataJSON() });
      pending.set(resolution[1], route);
      return; // Explicitly completed by the test, so two cards can be in flight.
    }
    if (path === `/api/v1/workspaces/${WORKSPACE}/chat/messages/page`) {
      await route.fulfill({ json: {
        items: messages, has_more: false, next_cursor: null, open_actions_complete: true,
        open_action_count: messages.filter((m) => !m.resolved_at).length,
      } });
      return;
    }
    if (path.endsWith("/stats/quick-view")) {
      await route.fulfill({ json: { ordered_stat_ids: [], hidden_stat_ids: [], configured: false } });
      return;
    }
    if (path.endsWith("/connection-status")) {
      await route.fulfill({ json: { workspace_id: WORKSPACE, requirements: [], required_issue_count: 0 } });
      return;
    }
    if (["/api/v1/tasks", "/api/v1/goals", `/api/v1/workspaces/${WORKSPACE}/stats`].includes(path)) {
      await route.fulfill({ json: { items: [], total: 0 } });
      return;
    }
    if ([
      `/api/v1/workspaces/${WORKSPACE}/agents`, `/api/v1/workspaces/${WORKSPACE}/staff`,
      `/api/v1/workspaces/${WORKSPACE}/chat/entrypoints`,
      "/api/v1/chat/conversations/fixture-conversation/feedback",
    ].includes(path)) {
      await route.fulfill({ json: [] });
      return;
    }
    unexpected.push(`${request.method()} ${path}`);
    await route.fulfill({ status: 501, json: { detail: `Unexpected fixture request: ${path}` } });
  });
  const finish = async (id: string, status = 200) => {
    await expect.poll(() => pending.has(id)).toBe(true);
    const route = pending.get(id)!;
    pending.delete(id);
    if (status !== 200) {
      await route.fulfill({ status, json: { detail: `Approval ${status}: try again` } });
      return;
    }
    const message = messages.find((m) => m.id === id)!;
    message.resolved_at = "2026-08-31T12:10:00Z";
    message.resolution = route.request().postDataJSON();
    await route.fulfill({ json: message });
  };
  return { requests, finish, unexpected, pageErrors };
}

const card = (page: Page, id: string) => page.locator(`#workspace-chat-message-${id}`);

for (const width of [1440, 390]) {
  test(`proposal approval and selection work during a reply at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await mockWorkspace(page);
    await page.goto(fixtureUrl);
    const first = card(page, "proposal-a");
    const second = card(page, "proposal-b");
    await expect(first.getByRole("button", { name: "Approve all", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Start simulated reply" }).click();
    await expect(page.getByRole("status", { name: "Fixture reply" })).toHaveText("Reply streaming");
    await expect(first.getByRole("button", { name: "Approve all", exact: true })).toBeEnabled();
    const checkbox = first.getByRole("checkbox").last();
    await checkbox.focus();
    await checkbox.press("Space");
    await expect(checkbox).not.toBeChecked();
    await first.screenshot({ path: testInfo.outputPath(`proposal-streaming-${width}.png`) });
    await first.getByRole("button", { name: /Approve 1/ }).click();
    await expect.poll(() => state.requests.length).toBe(1);
    expect(state.requests[0]).toEqual({ id: "proposal-a", body: {
      choice: "approve_selected", payload: { selected_task_ids: ["proposal-a-task-1"], selected_item_ids: [] },
    } });
    await expect(first.getByRole("checkbox").first()).toBeDisabled();
    await expect(second.getByRole("button", { name: "Approve all", exact: true })).toBeEnabled();
    await state.finish("proposal-a");
    await expect(page.getByRole("status", { name: "Fixture reply" })).toHaveText("Reply streaming");
    await page.getByRole("button", { name: "Finish simulated reply" }).click();
    await page.reload();
    await expect(first.getByRole("button", { name: /Approve/ })).toHaveCount(0);
    await expect(second.getByRole("button", { name: "Approve all", exact: true })).toBeEnabled();
    expect(state.unexpected).toEqual([]);
    expect(state.pageErrors).toEqual([]);
  });
}

test("concurrent cards retry independently without unlocking a pending approval", async ({ page }) => {
  const state = await mockWorkspace(page);
  await page.goto(fixtureUrl);
  const first = card(page, "proposal-a");
  const second = card(page, "proposal-b");
  await page.getByRole("button", { name: "Start simulated reply" }).click();
  await first.getByRole("button", { name: "Approve all", exact: true }).dblclick();
  await expect.poll(() => state.requests.length).toBe(1);
  await second.getByRole("button", { name: "Approve all", exact: true }).click();
  await expect.poll(() => state.requests.length).toBe(2);
  await state.finish("proposal-b", 503);
  await expect(page.getByText("Reconnecting...", { exact: true })).toBeVisible();
  await expect(second.getByRole("button", { name: "Approve all", exact: true })).toBeEnabled();
  await expect(first.getByRole("button", { name: /Approve/ })).toHaveCount(0);
  await expect(first.getByRole("checkbox").first()).toBeDisabled();
  await second.getByRole("button", { name: "Approve all", exact: true }).click();
  await expect.poll(() => state.requests.length).toBe(3);
  await state.finish("proposal-b", 403);
  await expect(page.getByText("Approval 403: try again", { exact: true })).toBeVisible();
  await expect(second.getByRole("button", { name: "Approve all", exact: true })).toBeEnabled();
  await expect(first.getByRole("checkbox").first()).toBeDisabled();
  await second.getByRole("button", { name: "Approve all", exact: true }).click();
  await state.finish("proposal-b");
  await expect(first.getByRole("checkbox").first()).toBeDisabled();
  await state.finish("proposal-a");
  expect(state.requests.filter((r) => r.id === "proposal-a")).toHaveLength(1);
  expect(state.requests.filter((r) => r.id === "proposal-b")).toHaveLength(3);
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

test("pause still blocks approval and selection; resume restores them during a reply", async ({ page }) => {
  const state = await mockWorkspace(page);
  await page.goto(fixtureUrl);
  await page.getByRole("button", { name: "Start simulated reply" }).click();
  await page.getByRole("button", { name: "Pause fixture Workspace" }).click();
  const first = card(page, "proposal-a");
  await expect(first.getByRole("button", { name: "Approve all", exact: true })).toBeDisabled();
  await expect(first.getByRole("checkbox").first()).toBeDisabled();
  await page.getByRole("button", { name: "Resume fixture Workspace" }).click();
  await expect(first.getByRole("button", { name: "Approve all", exact: true })).toBeEnabled();
  await expect(first.getByRole("checkbox").first()).toBeEnabled();
  expect(state.requests).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});
