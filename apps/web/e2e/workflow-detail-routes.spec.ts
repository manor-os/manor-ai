import { expect, test, type Page } from "@playwright/test";

const workspaceId = "routes-workspace";
const workflowId = "routes-flow";
const actionTitle = "Prepare release materials";
const runId = "01M480XMPKGANGMCGVMC5K9EC4";
const olderId = "01M480XMPKGANGMCGVMC5OLDER";

async function installFixture(page: Page, status = "failed", locale = "en", includeUnbound = false) {
  const writes: string[] = [];
  const errors: string[] = [];
  const nodes = [
    { id: "start", name: "Start", type: "trigger", order: 0, next: ["campaign"], targets: ["campaign"] },
    { id: "campaign", name: "Create Campaign Content", type: "agent", order: 1, next: ["finish"], targets: ["finish"] },
    { id: "finish", name: "Finish", type: "end", order: 2, targets: [] },
  ];
  const workflow = { id: workflowId, name: actionTitle, status: "active", is_active: true,
    trigger_type: "manual", version: 1, workspace_id: workspaceId, steps: nodes };
  const runs = [
    { id: runId, started_at: "2026-10-06T07:16:42Z", completed_at: "2026-10-06T07:18:42Z", attempt_number: 2 },
    { id: olderId, started_at: "2026-10-06T07:16:12Z", completed_at: "2026-10-06T07:17:12Z", attempt_number: 1 },
  ].map((run) => ({ ...run, workflow_id: workflowId, workspace_id: workspaceId, binding_id: "routes-binding",
    workflow_name: actionTitle, status, trigger_source: "workspace_manual",
    current_step_id: "campaign", current_step_name: "Create Campaign Content",
    processed_count: status === "completed" ? 19 : 1, total_count: status === "completed" ? 21 : 3,
    error: status === "failed" ? "Campaign generation failed" : null, capabilities: { can_control: false }, workflow_steps: [
      { id: "start", name: "Start", status: "completed" }, { id: "campaign", name: "Create Campaign Content", status: "failed" },
    ], variables: { __result: { title: "Release report", summary: run.id === runId ? "Latest release materials" : "Previous release materials" } },
    step_results: { start: { status: "completed" }, campaign: {
      status: status === "completed" ? "completed" : "failed",
      error: status === "completed" ? null : "Campaign generation failed",
      output: { title: "Release report", summary: run.id === runId ? "Latest release materials" : "Previous release materials" },
    } },
    definition_snapshot: { name: actionTitle, nodes },
  }));
  if (includeUnbound) runs.unshift({
    ...runs[0], id: "01M480XMPKGANGMCGVDIRECT", binding_id: null, trigger_source: "manual",
    started_at: "2026-10-07T07:16:42Z", completed_at: "2026-10-07T07:18:42Z",
    variables: { __result: { title: "Release report", summary: "Direct release materials" } },
    step_results: { start: { status: "completed" }, campaign: {
      status: status === "completed" ? "completed" : "failed",
      error: status === "completed" ? null : "Campaign generation failed",
      output: { title: "Release report", summary: "Direct release materials" },
    } },
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript((locale) => {
    localStorage.setItem("manor_token", "routes-fixture-token");
    localStorage.setItem("manor_locale", locale);
    localStorage.setItem("manor_tour_completed", "true");
    localStorage.setItem("workspace-chat-app-display", "app");
  }, locale);
  await page.route("**/config", (route) => route.fulfill({ json: {
    deployment_mode: "oss", environment: "e2e", fs_enabled: false,
    flows_available: true, flows_released: true, ai_credits_unlimited: true,
  } }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (path === "/admin/preferences" && request.method() === "PUT") {
      return route.fulfill({ json: request.postDataJSON() });
    }
    if (request.method() !== "GET") {
      writes.push(`${request.method()} ${path}`);
      return route.fulfill({ status: 500, json: { detail: "Navigation must not mutate runtime state" } });
    }
    const workspace = { id: workspaceId, entity_id: "fixture-entity", name: "Release Studio",
      kind: "standard", status: "active", heartbeat_enabled: false, settings: {} };
    let data: unknown = [];
    if (path === "/auth/me") data = { id: "fixture-user", entity_id: "fixture-entity", role: "owner", locale };
    else if (path === "/workspaces") data = [workspace];
    else if (path === `/workspaces/${workspaceId}`) data = workspace;
    else if (path.endsWith("/app-view")) data = {
      workspace_id: workspaceId, title: "Release Studio", status: "active", can_start: true,
      config: { version: 1, visual: "plain", layout: "list", order: [], hidden: [], labels: {}, descriptions: {}, forms: {}, targets: {} },
      actions: [{ key: "release", kind: "workflow", title: actionTitle, workflow_id: workflowId, binding_id: "routes-binding", inputs: [] }],
    };
    else if (path.endsWith("/setup-status")) data = { ready: true, incomplete_checks: [] };
    else if (path.endsWith("/chat/messages/page")) data = { items: [], has_more: false, open_actions_complete: true, open_action_count: 0 };
    else if (path.endsWith("/connection-status")) data = { requirements: [] };
    else if (path.endsWith("/stats/quick-view")) data = { ordered_stat_ids: [], hidden_stat_ids: [], configured: false };
    else if (path.endsWith("/initiatives") || path.startsWith("/agent-initiatives/conversation/")) data = { items: [] };
    else if (path.endsWith("/stats") || path === "/tasks" || path === "/goals") data = { items: [], total: 0 };
    else if (path === "/notifications") data = { items: [], total: 0, unread_count: 0 };
    else if (path === "/platform/flags") data = { flags: {} };
    else if (path === "/admin/preferences") data = {};
    else if (path.startsWith("/approval-settings/")) data = {
      scope: { kind: "workspace", id: workspaceId }, revision: 1, version: "fixture-v1",
      effective_policy: { mode: "confirm_changes", rules: [] }, policy: { mode: null, rules: [] },
      inherited_mode: "confirm_changes", root_scope: { kind: "account", id: "fixture-entity" },
      policy_versions: [], inheritance: "following",
      capabilities: { can_view: true, can_edit_root: false, can_edit_local: false, can_save_rule: false },
      action_catalog: [], effective_rows: [], saved_rules: [], inherited_rules: [], rule_sources: [],
    };
    else if (path === "/workflows") data = [workflow];
    else if (path === `/workflows/${workflowId}`) data = workflow;
    else if (path === "/workflows/runs" || path === `/workflows/${workflowId}/runs`) data = runs;
    else {
      const run = runs.find((item) => path === `/workflows/runs/${item.id}` || path === `/workflows/runs/${item.id}/family`);
      if (run) data = path.endsWith("/family") ? [run] : run;
    }
    await route.fulfill({ json: data });
  });
  return { writes, errors, runs };
}

test("App actions open a matching unbound direct run from their history", async ({ page }) => {
  const fixture = await installFixture(page, "completed", "en", true);
  await page.goto(`/chat?workspace=${workspaceId}`);
  await page.getByRole("button", { name: new RegExp(actionTitle) }).click();
  const history = page.getByRole("region", { name: "Recent runs", exact: true });
  await expect(history.getByRole("button")).toHaveCount(3);
  await expect(history.getByRole("button").first()).toContainText("Direct workflow run");
  await history.getByRole("button").first().click();
  await expect(page.locator(".workspace-app-flow__panel")).toContainText("Direct release materials");
  await expect(page.locator(".workspace-app-flow__panel")).toContainText("Direct workflow run");
  await expect(page.getByRole("link", { name: "Full run details", exact: true }))
    .toHaveAttribute("href", `/flows/${workflowId}/runs/01M480XMPKGANGMCGVDIRECT`);
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

for (const width of [1440, 390]) {
  test(`App recent runs select exact results inline and full details reuse the canvas at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const fixture = await installFixture(page);
    await page.goto(`/chat?workspace=${workspaceId}`);
    await page.getByRole("button", { name: new RegExp(actionTitle) }).click();
    const history = page.getByRole("region", { name: "Recent runs", exact: true });
    const latest = history.getByRole("button").first();
    await expect(latest).not.toContainText("#MC5K9EC4");
    await expect(latest).toContainText("Manual run");
    await expect(latest).toContainText("Attempts: 2");
    await expect(latest).toContainText("1/3 steps");
    await expect(latest).toContainText("Failed step: Create Campaign Content");
    await expect(latest).toContainText("2m 0s");
    await expect(latest).toContainText(":42");
    await page.screenshot({ path: testInfo.outputPath(`recent-runs-${width}.png`), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await latest.click();
    await expect(page).toHaveURL(new RegExp(`/chat\\?workspace=${workspaceId}$`));
    await expect(latest).toHaveAttribute("aria-current", "true");
    const fullDetails = page.getByRole("link", { name: "Full run details", exact: true });
    await expect(fullDetails).toHaveAttribute("href", `/flows/${workflowId}/runs/${runId}`);
    await history.getByRole("button").nth(1).click();
    await expect(fullDetails).toHaveAttribute("href", `/flows/${workflowId}/runs/${olderId}`);
    await latest.click();
    await fullDetails.click();
    await expect(page).toHaveURL(`/flows/${workflowId}/runs/${runId}`);
    await expect(page.locator(".workflow-editor-header")).toContainText(actionTitle);
    await expect(page.locator(".react-flow__node")).toHaveCount(3);
    await expect(page.getByRole("region", { name: "Workflow final result" })).toContainText("Latest release materials");
    await page.reload();
    await expect(page.locator(".react-flow__node")).toHaveCount(3);
    await expect.poll(() => page.locator(".react-flow").evaluate((canvas) => {
      const bounds = canvas.getBoundingClientRect();
      return Array.from(canvas.querySelectorAll(".react-flow__node")).every((node) => {
        const rect = node.getBoundingClientRect();
        return rect.left >= bounds.left && rect.right <= bounds.right
          && rect.top >= bounds.top && rect.bottom <= bounds.bottom;
      });
    })).toBe(true);
    const nodeAction = await page.getByRole("button", { name: "Open failed node", exact: true }).boundingBox();
    const chatAction = await page.getByRole("button", { name: "Chat with Manor AI", exact: true }).boundingBox();
    expect(nodeAction && chatAction && (nodeAction.x + nodeAction.width <= chatAction.x
      || nodeAction.y + nodeAction.height <= chatAction.y
      || nodeAction.x >= chatAction.x + chatAction.width
      || nodeAction.y >= chatAction.y + chatAction.height)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`run-details-${width}.png`), fullPage: true });
    await page.getByRole("button", { name: "Back to History", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/chat\\?workspace=${workspaceId}$`));
    await expect(history).toBeVisible();
    await fullDetails.click();
    await page.goBack();
    await expect(history).toBeVisible();
    expect(fixture.writes).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}

for (const width of [1440, 390]) {
  test(`completed recent runs omit stale current steps and progress at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const fixture = await installFixture(page, "completed");
    await page.goto(`/chat?workspace=${workspaceId}`);
    await page.getByRole("button", { name: new RegExp(actionTitle) }).click();
    const history = page.getByRole("region", { name: "Recent runs", exact: true });
    await expect(history.getByRole("button")).toHaveCount(2);
    await expect(history.locator(".workspace-app-flow__history-step")).toHaveCount(0);
    const latest = history.getByRole("button").first();
    await expect(latest).not.toContainText("#MC5K9EC4");
    await expect(latest).toContainText("Completed");
    await expect(history).not.toContainText(/\d+\/\d+ steps/);
    await expect(latest).toContainText("Manual run");
    await expect(latest).toContainText("2m 0s");
    await expect(latest).toContainText(":42");
    await latest.focus();
    await expect(latest).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await history.screenshot({ path: testInfo.outputPath(`recent-completed-${width}.png`) });
    await latest.click();
    await expect(page).toHaveURL(`/chat?workspace=${workspaceId}`);
    await expect(page.locator(".workspace-app-flow__panel")).toContainText("Latest release materials");
    await history.getByRole("button").nth(1).click();
    await expect(page.locator(".workspace-app-flow__panel")).toContainText("Previous release materials");
    expect(fixture.writes).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}

for (const status of ["pending", "running", "paused", "cancelled", "skipped"]) {
  test(`recent ${status} runs show current steps only while active`, async ({ page }) => {
    const fixture = await installFixture(page, status);
    await page.goto(`/chat?workspace=${workspaceId}`);
    await page.getByRole("button", { name: new RegExp(actionTitle) }).click();
    const history = page.getByRole("region", { name: "Recent runs", exact: true });
    await expect(history.getByRole("button")).toHaveCount(2);
    await expect(history.getByRole("button").first()).toContainText("1/3 steps");
    if (["pending", "running", "paused"].includes(status)) {
      await expect(history.locator(".workspace-app-flow__history-step")).toHaveCount(2);
      await expect(history.getByRole("button").first()).toContainText("Current step: Create Campaign Content");
    } else {
      await expect(history.locator(".workspace-app-flow__history-step")).toHaveCount(0);
    }
    expect(fixture.writes).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}

for (const [locale, historyLabel, manualLabel] of [
  ["zh", "最近执行", "手动运行"],
  ["es", "Ejecuciones recientes", "Ejecución manual"],
]) {
  test(`recent runs identify manual triggers clearly in ${locale}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 1000 });
    const fixture = await installFixture(page, "completed", locale);
    await page.goto(`/chat?workspace=${workspaceId}`);
    await page.getByRole("button", { name: new RegExp(actionTitle) }).click();
    const history = page.getByRole("region", { name: historyLabel, exact: true });
    await expect(history.getByRole("button")).toHaveCount(2);
    await expect(history.getByRole("button").first()).toContainText(manualLabel);
    await expect(history).not.toContainText("19/21");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await history.screenshot({ path: testInfo.outputPath(`recent-completed-${locale}.png`) });
    expect(fixture.writes).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}

test("selected completed App runs omit misleading step progress", async ({ page }) => {
  const fixture = await installFixture(page, "completed");
  await page.addInitScript(({ workspaceId, runId }) => {
    if (location.pathname === "/chat") {
      history.replaceState({ ...history.state, usr: {
        workspaceApp: { workspaceId, actionKey: "release", runId },
      } }, "");
    }
  }, { workspaceId, runId });
  await page.goto(`/chat?workspace=${workspaceId}`);
  const result = page.locator(".workspace-app-flow__panel[role='status']");
  await expect(result.getByRole("heading", { name: "Completed", exact: true })).toBeVisible();
  await expect(result).not.toContainText(/19\s*\/\s*21/);
  await expect(result).toContainText("Manual run");
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("legacy workflow and run links redirect while a mismatched workspace cannot render controls", async ({ page }) => {
  const fixture = await installFixture(page);
  await page.goto(`/flows?workflow=${workflowId}`);
  await expect(page).toHaveURL(`/flows/${workflowId}`);
  await expect(page.locator(".workflow-editor-header")).toBeVisible();
  await page.goto(`/flows?workflow=${workflowId}&run=${runId}&inspect=1#receipt`);
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${runId}?inspect=1#receipt`);
  await expect(page.locator(".workflow-editor-header")).toBeVisible();
  await page.goto(`/workspaces/${workspaceId}?tab=workflows&workflow_view=history`);
  await page.locator(`[data-run-id="${olderId}"] .workflow-run-history-row-main`).click();
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${olderId}`);
  await page.goto(`/workspaces/${workspaceId}?tab=workflows&workflow_view=history&workflow_run=${olderId}&inspect=1#receipt`);
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${olderId}?inspect=1#receipt`);
  await expect(page.locator(".workflow-editor-header")).toBeVisible();
  await page.goto(`/workspaces/wrong/workflow-runs/${runId}`);
  await expect(page.getByRole("alert")).toContainText("This run no longer belongs");
  await expect(page.locator(".workflow-run-history-detail")).toHaveCount(0);
  await page.goto(`/flows/wrong/runs/${runId}`);
  await expect(page.getByRole("alert")).toContainText("This run no longer belongs");
  await expect(page.locator(".workflow-run-history-detail")).toHaveCount(0);
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("clicking an already selected App run stays inline and full details preserve the selection", async ({ page }) => {
  const fixture = await installFixture(page);
  await page.addInitScript(({ workspaceId, runId }) => {
    if (location.pathname === "/chat") {
      history.replaceState({ ...history.state, usr: {
        workspaceApp: { workspaceId, actionKey: "release", runId },
      } }, "");
    }
  }, { workspaceId, runId });
  await page.goto(`/chat?workspace=${workspaceId}`);
  await expect(page.getByRole("link", { name: "Full run details", exact: true })).toHaveAttribute("href", `/flows/${workflowId}/runs/${runId}`);
  await page.getByRole("region", { name: "Recent runs", exact: true }).getByRole("button").first().click();
  await expect(page).toHaveURL(`/chat?workspace=${workspaceId}`);
  await page.getByRole("link", { name: "Full run details", exact: true }).click();
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${runId}`);
  await expect(page.locator(".workflow-editor-header")).toContainText(actionTitle);
  await page.getByRole("button", { name: "Back to History", exact: true }).click();
  await expect(page.getByRole("link", { name: "Full run details", exact: true })).toHaveAttribute("href", `/flows/${workflowId}/runs/${runId}`);
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("old runs hydrate by ID with their snapshot even when the definition is unavailable", async ({ page }) => {
  const fixture = await installFixture(page);
  await page.route(`**/api/v1/workflows/${workflowId}`, (route) => route.fulfill({ status: 404, json: { detail: "Workflow not found" } }));
  await page.route("**/api/v1/workflows/runs?**", (route) => route.fulfill({ json: [] }));
  await page.goto(`/flows/${workflowId}/runs/${olderId}`);
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${olderId}`);
  await expect(page.locator(".workflow-editor-header")).toContainText(actionTitle);
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await expect(page.getByRole("region", { name: "Workflow final result" })).toContainText("Previous release materials");
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("changing definition routes never leaves the previous editor active for a missing target", async ({ page }) => {
  const fixture = await installFixture(page);
  await page.route("**/api/v1/workflows/missing", (route) => route.fulfill({ status: 404, json: { detail: "Workflow not found" } }));
  await page.goto(`/flows/${workflowId}`);
  await expect(page.locator(".workflow-editor-header")).toBeVisible();
  await page.evaluate(() => {
    history.pushState(null, "", "/flows/missing");
    dispatchEvent(new PopStateEvent("popstate", { state: history.state }));
  });
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.locator(".workflow-editor-header")).toHaveCount(0);
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

for (const sourceStatus of ["failed", "cancelled"]) test(`retry from ${sourceStatus} run details navigates to the server's new attempt`, async ({ page }) => {
  const fixture = await installFixture(page, sourceStatus);
  const nextId = "01M480XMPKGANGMCGVMC5RETRY";
  const source = { ...fixture.runs[0], status: sourceStatus, lineage_root_run_id: runId, retry_from_step_id: "campaign",
    capabilities: { can_control: true }, intervention: {
      kind: "workflow_retry", workflow_run_id: runId, workflow_binding_id: "routes-binding",
      step_id: "campaign", retry_from_step_id: "campaign", options: ["retry"],
      editable_input_schema: { type: "object", properties: {} },
    } };
  const next = { ...source, id: nextId, status: "running", attempt_number: 3, retry_of_run_id: runId,
    error: null, completed_at: null };
  const retryRequests: unknown[] = [];
  await page.route("**/api/v1/workflows/runs/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/${runId}/retry`) && route.request().method() === "POST") {
      retryRequests.push(route.request().postDataJSON());
      return route.fulfill({ json: next });
    }
    if (path.endsWith(`/${runId}/family`) || path.endsWith(`/${nextId}/family`)) {
      return route.fulfill({ json: retryRequests.length ? [source, next] : [source] });
    }
    if (path.endsWith(`/${runId}`)) return route.fulfill({ json: new URL(route.request().url()).searchParams.get("detail") === "false" ? source : { ...source, intervention: undefined } });
    if (path.endsWith(`/${nextId}`)) return route.fulfill({ json: next });
    return route.fallback();
  });
  await page.goto(`/workspaces/${workspaceId}/workflow-runs/${runId}`);
  await page.getByRole("button", { name: "Retry", exact: true }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${nextId}`);
  await expect(page.locator(".workflow-editor-header")).toContainText(actionTitle);
  expect(retryRequests).toEqual([{ from_step_id: "campaign", variables: {}, execute: true }]);
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

for (const decision of ["not_applied", "applied"] as const) test(`run details offer operator review for ${decision} result`, async ({ page }) => {
  const fixture = await installFixture(page);
  const source = { ...fixture.runs[0], lineage_root_run_id: runId, capabilities: { can_control: true },
    intervention: { kind: "workflow_effect_review", workflow_run_id: runId, step_id: "campaign",
      options: [decision === "applied" ? "review_applied" : "review"], effect_fingerprint: "a".repeat(64),
      ...(decision === "applied" ? { project_revisions: { "report-project": 4 }, result_schema: { type: "object", required: ["receipt_id"], properties: {
        receipt_id: { type: "string", title: "Delivery receipt", minLength: 1 },
        failure_reason: { type: "string", minLength: 1 },
      } } } : {}),
    } };
  const nextId = "01M480XMPKGANGMCGVMC5RETRY";
  const next = { ...source, id: nextId, status: "running", attempt_number: 3, retry_of_run_id: runId, intervention: null, error: null };
  const requests: any[] = [];
  await page.route("**/api/v1/workflows/runs/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/${runId}/retry`) && route.request().method() === "POST") {
      requests.push(route.request().postDataJSON());
      return route.fulfill({ json: next });
    }
    if (path.endsWith("/family")) return route.fulfill({ json: requests.length ? [source, next] : [source] });
    if (path.endsWith(`/${runId}`)) return route.fulfill({ json: new URL(route.request().url()).searchParams.get("detail") === "false" ? source : { ...source, intervention: undefined } });
    if (path.endsWith(`/${nextId}`)) return route.fulfill({ json: next });
    return route.fallback();
  });
  await page.goto(`/flows/${workflowId}/runs/${runId}`);
  await page.getByRole("button", { name: "Review & retry", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Review & retry" });
  const submit = dialog.getByRole("button", { name: decision === "applied" ? "Use verified result & continue" : "Confirm & retry" });
  await expect(submit).toBeDisabled();
  await dialog.getByRole("textbox", { name: "Verification evidence (20–2,000 characters)" }).fill(decision === "applied"
    ? "Verified this exact report in the recipient delivery receipt; no delivery remains pending."
    : "Verified the account log and pending queue; nothing was published or left in progress.");
  await dialog.getByRole("checkbox").check();
  if (decision === "applied") {
    await expect(dialog.getByText("A later check released this item's execution lock.", { exact: false })).toBeVisible();
    await submit.click();
    expect(requests).toHaveLength(0);
    await dialog.getByRole("textbox", { name: "Delivery receipt" }).fill("report-receipt-42");
  }
  await submit.click();
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${nextId}`);
  expect(requests).toHaveLength(1);
  expect(requests[0].effect_review).toMatchObject({ step_id: "campaign", decision, confirmed: true, effect_fingerprint: "a".repeat(64) });
  if (decision === "applied") {
    expect(requests[0].effect_review.result).toEqual({ receipt_id: "report-receipt-42" });
    expect(requests[0].effect_review.project_revisions).toEqual({ "report-project": 4 });
  }
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("failed run keeps a retry entry but requires fresh server permission before submission", async ({ page }) => {
  const fixture = await installFixture(page);
  let source = {
    ...fixture.runs[0],
    lineage_root_run_id: runId,
    capabilities: { can_control: true },
    intervention: null as Record<string, unknown> | null,
  };
  await page.route("**/api/v1/workflows/runs/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/${runId}/family`)) return route.fulfill({ json: [source] });
    if (path.endsWith(`/${runId}`)) return route.fulfill({ json: source });
    return route.fallback();
  });
  await page.goto(`/workspaces/${workspaceId}/workflow-runs/${runId}`);
  await expect(page.locator(".workflow-run-banner")).toBeVisible();
  await page.locator(".workflow-run-banner").getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Retry", exact: true })).toBeDisabled();
  await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).last().click();
  source = { ...source, intervention: {
    kind: "workflow_effect_review", options: [],
    blocked_reason: "external_effect_requires_reconciliation", deadline_at: "2026-10-15T08:00:00Z",
  } };
  await page.reload();
  await expect(page.locator(".workflow-run-banner")).toContainText("Retry blocked · verify previous action");
  await page.locator(".workflow-run-banner").getByRole("button", { name: "Retry", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Retry", exact: true });
  await expect(dialog).toContainText("A browser action was executed or is still pending");
  await expect(dialog).toContainText("Retrying does not extend it");
  await expect(dialog.getByRole("button", { name: "Retry", exact: true })).toBeDisabled();
  source = { ...source, intervention: {
    kind: "workflow_retry", step_id: "campaign", retry_from_step_id: "campaign", options: ["retry"],
    editable_input_schema: { type: "object", properties: {} },
  } };
  await dialog.getByRole("button", { name: "Check again", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Retry", exact: true })).toBeEnabled();
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("run details keep the frozen graph read-only when the current definition changes", async ({ page }) => {
  const fixture = await installFixture(page, "completed");
  await page.route(`**/api/v1/workflows/${workflowId}`, (route) => route.fulfill({ json: {
    id: workflowId, name: "Renamed workflow", steps: [{ id: "replacement", name: "New node", type: "trigger" }],
  } }));
  await page.goto(`/flows/${workflowId}/runs/${runId}`);
  await expect(page.locator(".workflow-editor-header")).toContainText(actionTitle);
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await expect(page.getByRole("button", { name: "Add node", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Deploy workflow", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Edit workflow name, description, and icon" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Workflow final result" })).toContainText("Latest release materials");
  await expect(page.locator(".workflow-run-banner")).not.toContainText(/\d+\/\d+/);
  await page.getByRole("button", { name: "Close final result" }).click();
  await page.locator(".react-flow__node").getByText("Create Campaign Content", { exact: true }).dblclick();
  await expect(page.getByRole("dialog", { name: "Create Campaign Content", exact: true })).toContainText("Latest release materials");
  await expect(page.getByRole("button", { name: "Save", exact: true })).toHaveCount(0);
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("opening a newer active run changes the exact run route and result", async ({ page }) => {
  const fixture = await installFixture(page, "completed");
  const active = { ...fixture.runs[0], status: "running", completed_at: null };
  await page.route(`**/api/v1/workflows/${workflowId}/runs**`, (route) => route.fulfill({ json: [active, fixture.runs[1]] }));
  await page.route(`**/api/v1/workflows/runs/${runId}`, (route) => route.fulfill({ json: active }));
  await page.goto(`/flows/${workflowId}/runs/${olderId}`);
  await expect(page.getByRole("region", { name: "Workflow final result" })).toContainText("Previous release materials");
  await page.getByRole("button", { name: "Open running run", exact: true }).click();
  await expect(page).toHaveURL(`/flows/${workflowId}/runs/${runId}`);
  await expect(page.locator(".workflow-editor-header")).toContainText("Running");
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("legacy runs label the current-definition fallback and preserve its routes", async ({ page }) => {
  const fixture = await installFixture(page, "completed");
  const legacy = { ...fixture.runs[0], definition_snapshot: undefined };
  await page.route(`**/api/v1/workflows/runs/${runId}`, (route) => route.fulfill({ json: legacy }));
  await page.goto(`/flows/${workflowId}/runs/${runId}`);
  await expect(page.locator(".workflow-editor-header")).toContainText("Legacy names");
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await expect(page.locator(".react-flow__edge")).toHaveCount(2);
  await expect(page.getByRole("region", { name: "Workflow final result" })).toContainText("Latest release materials");
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("legacy runs without a definition retain their recorded node results", async ({ page }) => {
  const fixture = await installFixture(page, "completed");
  const legacy = { ...fixture.runs[0], definition_snapshot: undefined };
  await page.route(`**/api/v1/workflows/runs/${runId}`, (route) => route.fulfill({ json: legacy }));
  await page.route(`**/api/v1/workflows/${workflowId}`, (route) => route.fulfill({ status: 404, json: { detail: "Workflow not found" } }));
  await page.goto(`/flows/${workflowId}/runs/${runId}`);
  await expect(page.locator(".workflow-editor-header")).toContainText("Legacy names");
  await expect(page.locator(".react-flow__node")).toHaveCount(2);
  await page.locator(".react-flow__node[data-id='campaign']").dblclick();
  await expect(page.getByRole("dialog")).toContainText("Latest release materials");
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

for (const status of ["running", "paused", "failed"]) {
  test(`run details with no control capability hide ${status} actions`, async ({ page }) => {
    const fixture = await installFixture(page, status);
    await page.goto(`/flows/${workflowId}/runs/${runId}`);
    await expect(page.locator(".react-flow__node")).toHaveCount(3);
    await expect(page.getByRole("button", { name: /Pause workflow|Stop workflow|Retry workflow|Retry from this step|Resume/i })).toHaveCount(0);
    expect(fixture.writes).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}
