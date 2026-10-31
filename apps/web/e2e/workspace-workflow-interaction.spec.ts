import { expect, test, type Page, type Route } from "@playwright/test";
import type { WorkflowRunAction } from "../src/components/workflows/workflowRunDisplay";
import { FEATURE_DISCOVERIES, featureDiscoveryPreferenceKey } from "../src/lib/featureDiscovery";

const WORKSPACE = "workflow-interaction-workspace";
const RUN = "workflow-interaction-run";
const MESSAGE = "workflow-interaction-action";

async function readHistoryAt(page: Page, top: number) {
  const body = page.locator(".workspace-chat-body-wrap > .embedded-chat-body");
  const position = await body.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + 20 };
  });
  await page.mouse.move(position.x, position.y);
  // Establish a known reading position before the second wheel input; card
  // clearance can still shrink between measuring the bottom and scrolling.
  await page.mouse.wheel(0, -await body.evaluate((element) => element.scrollHeight));
  await expect.poll(() => body.evaluate((element) => element.scrollTop)).toBeLessThanOrEqual(1);
  await page.mouse.wheel(0, top);
  await expect.poll(() => body.evaluate((element) => element.scrollTop)).toBeCloseTo(top, 0);
}

async function installWorkflowFixture(page: Page, options: {
  canControl?: boolean;
  action?: WorkflowRunAction | null;
  status?: string;
  secondRun?: boolean;
  deferFamily?: boolean;
  attentionActivity?: boolean;
} = {}) {
  const action: WorkflowRunAction | null = options.action !== undefined ? options.action : {
    kind: "workflow_starter_input",
    workflow_run_id: RUN,
    message_id: MESSAGE,
    title: "Review workflow",
    inputs: [{ key: "brief", label: "Brief", type: "string", required: true }],
    values: { brief: "Keep this input" },
    options: ["run", "cancel"],
  };
  const nodes = Array.from({ length: 6 }, (_, index) => ({
    id: `step-${index}`, name: `Workflow step ${index + 1}`, status: "pending",
  }));
  const run = {
    id: RUN, workflow_id: "workflow", workspace_id: WORKSPACE,
    workflow_name: "Review workflow", status: options.status || "paused", current_step_id: "step-0",
    updated_at: "2026-09-27T10:00:00Z", attempt_number: 1,
    business_outcome: action?.business_outcome,
    retry_from_step_id: options.status === "failed" ? "step-0" : undefined,
    workflow_steps: nodes, intervention: action,
    capabilities: { can_control: options.canControl ?? true },
  };
  const activityMessage = () => ({
    id: "workflow-activity", conversation_id: "workflow-conversation",
    created_at: "2026-09-27T10:00:00Z", updated_at: run.updated_at,
    author_kind: "agent", message_kind: "workflow_activity", body: "Review workflow",
    refs: [{ type: "workflow", id: "workflow" }],
    meta: { workflow_run_id: RUN, workflow_title: "Review workflow",
      workflow_status: run.status, workflow_business_outcome: run.business_outcome, workflow_steps: nodes },
  });
  const otherRun = { ...run, id: "other-run", workflow_name: "Other workflow", status: "running", intervention: null };
  let retryRun: (typeof run & { retry_of_run_id: string; lineage_root_run_id: string }) | undefined;
  const messages = Array.from({ length: 24 }, (_, index) => ({
    id: `history-${index}`, conversation_id: "workflow-conversation",
    created_at: `2026-09-27T09:${String(index).padStart(2, "0")}:00Z`,
    message_kind: "chat", author_kind: "user", author_user_id: "fixture-user",
    body: `History message ${index + 1}: preserve my reading position.`,
  }));
  let resolved = false;
  let pending: Route | undefined;
  let familyReady = !options.deferFamily;
  let pendingFamily: Route | undefined;
  const submissions: unknown[] = [];
  const submissionPaths: string[] = [];
  const pageErrors: string[] = [];
  const unexpectedWrites: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("manor_token", "workflow-fixture-token");
    localStorage.setItem("manor_locale", "en");
    localStorage.setItem("manor_tour_completed", "true");
    localStorage.setItem("workspace-chat-app-display", "chat");
    localStorage.setItem("manor_consent_v1", JSON.stringify({
      v: 1, ts: "2026-09-27T00:00:00Z", locale: "en",
      categories: { functional: false, analytics: false, marketing: false },
    }));
  });
  await page.route("**/config", (route) => route.fulfill({ json: {
    deployment_mode: "oss", environment: "e2e", email_enabled: false,
    fs_enabled: false, flows_available: true, flows_released: true,
    ai_credits_unlimited: true, support_tickets_enabled: false,
  } }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (path === "/admin/preferences" && request.method() === "PUT") {
      return route.fulfill({ json: request.postDataJSON() });
    }
    const workspace = {
      id: WORKSPACE, entity_id: "fixture-entity", name: "Workflow interactions",
      status: "active", heartbeat_enabled: false, kind: "standard", settings: {},
    };
    if (path === `/workspaces/${WORKSPACE}/chat/messages/${MESSAGE}/resolve`
      || path === `/workflows/runs/${RUN}/approvals/native-request/resolve`
      || new RegExp(`^/workflows/runs/${RUN}/(pause|resume|retry|cancel)$`).test(path)) {
      submissions.push(request.postData() ? request.postDataJSON() : null);
      submissionPaths.push(path);
      pending = route;
      return;
    }
    if (request.method() !== "GET") {
      unexpectedWrites.push(`${request.method()} ${path}`);
      await route.fulfill({ status: 500, json: { detail: "Unexpected fixture mutation" } });
      return;
    }
    let data: unknown = [];
    if (path === "/auth/me") data = {
      id: "fixture-user", entity_id: "fixture-entity", username: "fixture",
      display_name: "Fixture", email: "fixture@example.test", role: "owner", locale: "en",
    };
    else if (path === "/workspaces") data = [workspace];
    else if (path === `/workspaces/${WORKSPACE}`) data = workspace;
    else if (path.startsWith("/approval-settings/")) data = {
      scope: { kind: "workspace", id: WORKSPACE, entity_id: "fixture-entity" },
      revision: 1, version: "fixture-policy", policy: { mode: null, rules: [] },
      effective_policy: { mode: "risk_based", rules: [] }, inheritance: "following",
      root_scope: { kind: "account", id: "fixture-entity", entity_id: "fixture-entity" },
      capabilities: { can_view: true, can_edit_root: true, can_edit_local: true, can_save_rule: true },
      policy_versions: [], action_catalog: [], effective_rows: [], saved_rules: [], inherited_rules: [], rule_sources: [],
    };
    else if (path.endsWith("/connection-status")) data = { requirements: [] };
    else if (path.startsWith("/agent-initiatives/conversation/") || path.endsWith("/initiatives")) data = { items: [] };
    else if (path === `/workspaces/${WORKSPACE}/chat/messages/page`) data = {
      conversation_id: "workflow-conversation", has_more: false, next_cursor: null,
      open_actions_complete: true, open_action_count: resolved ? 0 : 1,
      items: [...messages, ...(options.secondRun ? [{
        id: "other-workflow-activity", conversation_id: "workflow-conversation",
        created_at: "2026-09-27T09:59:00Z", updated_at: otherRun.updated_at,
        author_kind: "agent", message_kind: "workflow_activity", body: "Other workflow",
        meta: { workflow_run_id: otherRun.id, workflow_title: otherRun.workflow_name,
          workflow_status: otherRun.status, workflow_steps: nodes },
      }] : []), activityMessage(), {
        id: MESSAGE, conversation_id: "workflow-conversation",
        created_at: "2026-09-27T10:00:01Z", updated_at: run.updated_at,
        author_kind: "agent", message_kind: "hitl_request", body: "Workflow inputs",
        pending_action: action, resolved_at: resolved ? run.updated_at : null,
      }],
    };
    else if (path === `/workspaces/${WORKSPACE}/chat/messages/workflow-activity`) data = activityMessage();
    else if (path === "/workflows/runs/other-run/family") data = [otherRun];
    else if (path === "/workflows/runs/other-run") data = otherRun;
    else if (path === `/workflows/runs/${RUN}/family`) {
      if (!familyReady) { pendingFamily = route; return; }
      data = retryRun ? [run, retryRun] : [run];
    }
    else if (retryRun && path === `/workflows/runs/${retryRun.id}`) data = retryRun;
    else if (path === `/workflows/runs/${RUN}`) data = run;
    else if (path.endsWith("/stats/quick-view")) data = { ordered_stat_ids: [], hidden_stat_ids: [], configured: false };
    else if (path.endsWith("/stats") || path === "/tasks" || path === "/goals") data = { items: [], total: 0 };
    else if (path === "/notifications") data = { items: [], total: 0, unread_count: 0 };
    else if (path === "/platform/flags") data = { flags: {} };
    else if (path === "/admin/preferences") data = Object.fromEntries(
      Object.values(FEATURE_DISCOVERIES).flatMap((feature) => ["seen", "used"].map((event) => [
        featureDiscoveryPreferenceKey(feature, event as "seen" | "used"), "2026-09-27T00:00:00Z",
      ])),
    );
    await route.fulfill({ json: data });
  });
  await page.goto(`/chat?workspace=${WORKSPACE}${options.attentionActivity ? "&attention=fixture#workspace-chat-message-workflow-activity" : ""}`);
  // A fresh CI Vite process compiles the full Chat module on its first visit.
  // Bound only initial fixture readiness; interaction latency assertions stay strict.
  if (!options.deferFamily) await expect(page.locator(".workspace-workflow-run-host")).toBeVisible({ timeout: 45_000 });
  return {
    submissions, submissionPaths, pageErrors, unexpectedWrites,
    async revealWorkflow() {
      await expect.poll(() => Boolean(pendingFamily)).toBe(true);
      familyReady = true;
      await pendingFamily!.fulfill({ json: [run] });
    },
    async finish(success: boolean) {
      expect(pending).toBeDefined();
      const operation = new URL(pending!.request().url()).pathname.split("/").at(-1);
      if (success) {
        resolved = true;
        run.status = operation === "retry" ? "failed" : operation === "cancel" ? "cancelled" : operation === "pause" ? "paused" : "running";
        run.updated_at = "2026-09-27T10:01:00Z";
        run.intervention = operation === "pause" ? {
          kind: "workflow_input", workflow_run_id: RUN, options: ["resume"],
        } : null;
        if (operation === "retry") retryRun = {
          ...run, id: `${RUN}-retry`, status: "running", attempt_number: 2,
          retry_of_run_id: RUN, lineage_root_run_id: RUN,
        };
      }
      await pending!.fulfill(success
        ? { json: operation === "resolve" && !pending!.request().url().includes("/approvals/")
          ? { id: MESSAGE, resolved_at: run.updated_at, resolution: submissions.at(-1) }
          : retryRun || run }
        : { status: 409, json: { detail: "The workflow could not accept this input" } });
      pending = undefined;
    },
  };
}

for (const choice of ["approve", "reject"] as const) {
  test(`native workflow ${choice} uses the existing read-only review and exact approval request`, async ({ page }) => {
    if (choice === "reject") await page.setViewportSize({ width: 390, height: 844 });
    const fixture = await installWorkflowFixture(page, { action: {
      kind: "workflow_approval", workflow_run_id: RUN, step_id: "step-0",
      runtime_approval_id: "native-request", message_id: MESSAGE,
      title: "Review file update", prompt: "Review this workflow operation before continuing.",
      options: ["approve", "reject"], review_read_only: true,
      review: { tool: "file_engine", action: "write_file", paths: ["notes/review.md"], args_preview: { content: "Reviewed output" } },
    } });
    const host = page.locator(".workspace-workflow-run-host");
    await host.getByRole("button", { name: "Review", exact: true }).click();
    await expect(host.getByRole("button", { name: /^review\.md\b/ })).toBeVisible();
    await expect(host.getByRole("textbox")).toHaveCount(0);
    const button = host.getByRole("button", { name: choice === "approve" ? "Approve" : "Reject", exact: true });
    await button.focus();
    await page.keyboard.press("Enter");
    await expect.poll(() => fixture.submissions.length).toBe(1);
    await expect(button).toBeDisabled();
    await button.evaluate((element) => element.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(fixture.submissions).toEqual([{ choice }]);
    expect(fixture.submissionPaths).toEqual([`/workflows/runs/${RUN}/approvals/native-request/resolve`]);
    await fixture.finish(false);
    await expect(host.getByRole("alert")).toContainText("could not accept");
    await expect(button).toBeEnabled();
    await button.click();
    await expect.poll(() => fixture.submissions.length).toBe(2);
    await fixture.finish(true);
    await expect(host.locator(".workflow-run-status")).toHaveText("Running");
    expect(fixture.unexpectedWrites).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
  });
}

test("workflow submission responds immediately and survives slow success and failure", async ({ page }) => {
  const fixture = await installWorkflowFixture(page);
  const host = page.locator(".workspace-workflow-run-host");
  const submit = host.getByRole("button", { name: "Run workflow", exact: true });
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(host.locator(".workflow-run-status")).toHaveText("Submitting", { timeout: 500 });
  await expect(submit).toBeDisabled();
  await expect.poll(() => fixture.submissions.length).toBe(1);
  // Dispatch a second click even while disabled to exercise the submission guard.
  await submit.evaluate((button) => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await page.waitForTimeout(700);
  expect(fixture.submissions).toHaveLength(1);
  await expect(host.locator(".workflow-run-status")).toHaveText("Submitting");
  expect(await host.locator(".workflow-run-status").evaluate((element) => getComputedStyle(element).color))
    .not.toBe(await host.locator(".workflow-run-meta").evaluate((element) => getComputedStyle(element).color));

  await fixture.finish(false);
  await expect(submit).toBeEnabled();
  await expect(host.getByRole("alert")).toContainText("could not accept");
  await expect(host.getByRole("textbox", { name: "Brief" })).toHaveValue("Keep this input");
  await expect(host.locator(".workflow-run-status")).toHaveText("Paused");
  await submit.click();
  await expect.poll(() => fixture.submissions.length).toBe(2);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  await expect(submit).toHaveCount(0);
  expect(fixture.submissions[1]).toEqual({ choice: "run", payload: { inputs: { brief: "Keep this input" } } });
  expect(fixture.pageErrors).toEqual([]);
  expect(fixture.unexpectedWrites).toEqual([]);
});

test("card resizing keeps the bottom anchored without animated intermediate scrolls", async ({ page }) => {
  await installWorkflowFixture(page);
  const body = page.locator(".workspace-chat-body-wrap > .embedded-chat-body");
  await body.evaluate((element) => {
    element.style.scrollBehavior = "auto";
    element.scrollTop = element.scrollHeight;
    element.style.scrollBehavior = "";
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  const toggle = page.getByRole("button", { name: "Show all workflow steps", exact: true });
  const gaps = await toggle.evaluate(async (button) => {
    const body = document.querySelector(".workspace-chat-body-wrap > .embedded-chat-body")!;
    const samples: number[] = [];
    (button as HTMLButtonElement).click();
    for (let frame = 0; frame < 15; frame += 1) {
      await new Promise(requestAnimationFrame);
      samples.push(body.scrollHeight - body.clientHeight - body.scrollTop);
    }
    return samples;
  });
  // ResizeObserver runs before the following paint. No smooth scroll may run
  // through intermediate positions or accidentally switch off auto-follow.
  expect(Math.max(...gaps.slice(2))).toBeLessThanOrEqual(2);
  await page.getByRole("button", { name: "Hide workflow step list", exact: true }).click();
  await expect.poll(() => body.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThanOrEqual(2);

  await readHistoryAt(page, 250);
  await toggle.click();
  await expect.poll(() => body.evaluate((element) => element.scrollTop)).toBeCloseTo(250, 0);
});

test("read-only workflow cards keep action controls disabled", async ({ page }) => {
  const fixture = await installWorkflowFixture(page, { canControl: false });
  await expect(page.getByRole("button", { name: "Run workflow", exact: true })).toBeDisabled();
  expect(fixture.submissions).toEqual([]);
});

test("invalid input stays local and retains the server state", async ({ page }) => {
  const fixture = await installWorkflowFixture(page);
  const host = page.locator(".workspace-workflow-run-host");
  await host.getByRole("textbox", { name: "Brief" }).fill("");
  await host.getByRole("button", { name: "Run workflow", exact: true }).click();
  await expect(host.locator('[aria-invalid="true"]')).toBeFocused();
  await expect(host.locator(".workflow-run-status")).toHaveText("Paused");
  expect(fixture.submissions).toEqual([]);
});

test("collection workflow input submits answers through the message contract", async ({ page }) => {
  const fixture = await installWorkflowFixture(page, { action: {
    kind: "workflow_input", workflow_run_id: RUN, message_id: MESSAGE,
    step_id: "step-0", response_variable: "answers",
    inputs: [{ key: "campaign", label: "Campaign", type: "string", required: true }],
    options: ["respond", "cancel"],
  } });
  const host = page.locator(".workspace-workflow-run-host");
  await host.getByRole("textbox", { name: "Campaign *", exact: true }).fill("Release");
  await host.getByRole("button", { name: "Respond", exact: true }).click();
  await expect.poll(() => fixture.submissions.length).toBe(1);
  expect(fixture.submissionPaths).toEqual([`/workspaces/${WORKSPACE}/chat/messages/${MESSAGE}/resolve`]);
  expect(fixture.submissions).toEqual([{ choice: "respond", payload: { inputs: { campaign: "Release" } } }]);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  expect(fixture.pageErrors).toEqual([]);
  expect(fixture.unexpectedWrites).toEqual([]);
});

test("workflow input renders and submits a prefilled document intake schema", async ({ page }) => {
  const sourceDocument = {
    document_id: "customer-brief", id: "customer-brief", name: "Customer brief.pdf",
    type: "knowledge", viewer_url: "/viewer/customer-brief",
  };
  const fixture = await installWorkflowFixture(page, { action: {
    kind: "workflow_input", workflow_run_id: RUN, message_id: MESSAGE,
    step_id: "step-0", response_variable: "intake",
    input_schema: {
      type: "object", required: ["source_documents", "editor_note"], properties: {
        source_documents: {
          type: "array", items: { type: "object", required: ["document_id"] },
          "x-ui": { control: "documents" }, title: "Source documents",
        },
        editor_note: { type: "string", minLength: 1, title: "Editorial note" },
        existing_source_project_id: {
          type: "string", minLength: 1, title: "Existing source project",
          "x-ui": {
            control: "workflow_project_select", project_type: "release",
            allowed_stages: ["verified"],
          },
        },
      },
    },
    values: { source_documents: [sourceDocument] },
    options: ["respond", "cancel"],
  } });
  const host = page.locator(".workspace-workflow-run-host");
  await expect(host.getByText("Source documents *", { exact: true })).toBeVisible();
  await expect(host.getByText("Customer brief.pdf", { exact: true })).toBeVisible();
  await expect(host.getByRole("combobox", { name: "Existing source project", exact: true })).toBeVisible();
  await host.getByRole("textbox", { name: "Editorial note *", exact: true }).fill("Approved for publishing.");
  await host.getByRole("button", { name: "Respond", exact: true }).click();
  await expect.poll(() => fixture.submissions.length).toBe(1);
  expect(fixture.submissionPaths).toEqual([`/workspaces/${WORKSPACE}/chat/messages/${MESSAGE}/resolve`]);
  expect(fixture.submissions).toEqual([{
    choice: "respond", payload: { intake: {
      source_documents: [sourceDocument], editor_note: "Approved for publishing.",
    } },
  }]);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  expect(fixture.pageErrors).toEqual([]);
  expect(fixture.unexpectedWrites).toEqual([]);
});

for (const scenario of [
  { kind: "workflow_approval", choice: "accept", label: "Accept" },
  { kind: "workflow_input", choice: "resume", label: "Resume" },
] as const) {
  test(`${scenario.kind} preserves the message resolution contract`, async ({ page }) => {
    const fixture = await installWorkflowFixture(page, { action: {
      kind: scenario.kind, workflow_run_id: RUN, message_id: MESSAGE,
      step_id: "step-0", options: [scenario.choice, "cancel"],
    } });
    const host = page.locator(".workspace-workflow-run-host");
    await host.getByRole("button", { name: scenario.label, exact: true }).click();
    await expect(host.locator(".workflow-run-status")).toHaveText("Submitting", { timeout: 500 });
    await expect.poll(() => fixture.submissions.length).toBe(1);
    await fixture.finish(true);
    await expect(host.locator(".workflow-run-status")).toHaveText("Running");
    expect(fixture.submissionPaths).toEqual([`/workspaces/${WORKSPACE}/chat/messages/${MESSAGE}/resolve`]);
    expect(fixture.submissions).toEqual([{ choice: scenario.choice }]);
    expect(fixture.pageErrors).toEqual([]);
    expect(fixture.unexpectedWrites).toEqual([]);
  });
}

test("pause, resume and confirmed cancellation retain their original endpoints", async ({ page }) => {
  const fixture = await installWorkflowFixture(page, { action: null, status: "running" });
  const host = page.locator(".workspace-workflow-run-host");
  await host.getByRole("button", { name: "Pause after current step", exact: true }).click();
  await expect(host.locator(".workflow-run-status")).toHaveText("Submitting", { timeout: 500 });
  await expect.poll(() => fixture.submissions.length).toBe(1);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Paused");
  await host.getByRole("button", { name: "Resume", exact: true }).click();
  await expect.poll(() => fixture.submissions.length).toBe(2);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  await host.getByRole("button", { name: "Cancel", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  expect(fixture.submissions).toHaveLength(2);
  await dialog.getByRole("button", { name: "Keep running", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await host.getByRole("button", { name: "Cancel", exact: true }).click();
  await dialog.getByRole("button", { name: "Cancel run", exact: true }).click();
  await expect.poll(() => fixture.submissions.length).toBe(3);
  await fixture.finish(false);
  await expect(dialog.getByRole("alert")).toContainText("could not accept");
  await dialog.getByRole("button", { name: "Cancel run", exact: true }).click();
  await expect.poll(() => fixture.submissions.length).toBe(4);
  await fixture.finish(true);
  await expect(host).toHaveCount(0);
  await expect(dialog).toHaveCount(0);
  expect(fixture.submissionPaths).toEqual(["pause", "resume", "cancel", "cancel"].map(
    (operation) => `/workflows/runs/${RUN}/${operation}`,
  ));
  expect(fixture.pageErrors).toEqual([]);
  expect(fixture.unexpectedWrites).toEqual([]);
});

test("switching runs keeps a pending submission scoped and prevents duplicate clicks", async ({ page }) => {
  const fixture = await installWorkflowFixture(page, { secondRun: true });
  const host = page.locator(".workspace-workflow-run-host");
  await host.getByRole("button", { name: "Run workflow", exact: true }).click();
  await expect.poll(() => fixture.submissions.length).toBe(1);
  const switcher = host.getByRole("button", { name: "Select workflow run", exact: true });
  await switcher.click();
  await page.getByRole("option", { name: /Other workflow/ }).click();
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  await expect(host.getByRole("button", { name: "Pause after current step", exact: true })).toBeEnabled();
  await switcher.click();
  await page.getByRole("option", { name: /Review workflow/ }).click();
  await expect(host.locator(".workflow-run-status")).toHaveText("Submitting");
  await expect(host.getByRole("button", { name: "Run workflow", exact: true })).toBeDisabled();
  expect(fixture.submissions).toHaveLength(1);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  expect(fixture.pageErrors).toEqual([]);
});

test("retry keeps the selected step and adopts the server's new attempt", async ({ page }) => {
  const fixture = await installWorkflowFixture(page, { action: null, status: "failed" });
  const host = page.locator(".workspace-workflow-run-host");
  await host.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(host.locator(".workflow-run-status")).toHaveText("Submitting", { timeout: 500 });
  await expect.poll(() => fixture.submissions.length).toBe(1);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  await expect(host.locator(`a[href="/flows/workflow/runs/${RUN}-retry"]`)).toHaveCount(1);
  expect(fixture.submissions).toEqual([{ from_step_id: "step-0", variables: {}, execute: true }]);
  expect(fixture.submissionPaths).toEqual([`/workflows/runs/${RUN}/retry`]);
  expect(fixture.pageErrors).toEqual([]);
  expect(fixture.unexpectedWrites).toEqual([]);
});

for (const following of [true, false]) {
  test(`delayed card appearance preserves ${following ? "bottom following" : "history reading"}`, async ({ page }) => {
    const fixture = await installWorkflowFixture(page, { deferFamily: true });
    const body = page.locator(".workspace-chat-body-wrap > .embedded-chat-body");
    await expect(body).toContainText("History message 24");
    await expect.poll(() => body.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThanOrEqual(2);
    if (!following) await readHistoryAt(page, 250);
    await fixture.revealWorkflow();
    await expect(page.locator(".workspace-workflow-run-host")).toBeVisible();
    await expect.poll(() => body.evaluate((element, follow) => follow
      ? element.scrollHeight - element.clientHeight - element.scrollTop
      : Math.abs(element.scrollTop - 250), following)).toBeLessThanOrEqual(2);
    expect(fixture.pageErrors).toEqual([]);
  });
}

test("newly verified workflow card enters softly without delaying its actions", async ({ page }) => {
  const fixture = await installWorkflowFixture(page, { deferFamily: true });
  const host = page.locator(".workspace-workflow-run-host");
  await expect(host).toHaveCount(0);
  await fixture.revealWorkflow();
  await expect(host).toBeVisible();
  const entrance = await host.evaluate((element) => {
    const style = getComputedStyle(element);
    const animation = element.getAnimations().find((item) => item instanceof CSSAnimation);
    const keyframes = animation?.effect?.getKeyframes().map((frame) => ({
      opacity: frame.opacity,
      transform: frame.transform,
    }));
    if (animation) animation.currentTime = parseFloat(style.animationDuration) * 500;
    return {
      name: style.animationName,
      duration: parseFloat(style.animationDuration),
      easing: style.animationTimingFunction,
      keyframes,
      midpointOpacity: Number(getComputedStyle(element).opacity),
    };
  });
  expect(entrance.name).toBe("workspace-workflow-card-in");
  expect(entrance.duration).toBeGreaterThanOrEqual(0.3);
  expect(entrance.duration).toBeLessThanOrEqual(0.35);
  expect(entrance.easing).toBe("ease-in-out");
  expect(entrance.keyframes?.[0]).toMatchObject({ opacity: "0.2", transform: "translateY(18px)" });
  expect(entrance.midpointOpacity).toBeGreaterThan(0.5);
  expect(entrance.midpointOpacity).toBeLessThan(0.75);
  await expect(host.getByRole("button", { name: "Run workflow", exact: true })).toBeEnabled();
  expect(fixture.pageErrors).toEqual([]);
});

test("workflow card appears immediately when reduced motion is requested", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const fixture = await installWorkflowFixture(page, { deferFamily: true });
  await fixture.revealWorkflow();
  const host = page.locator(".workspace-workflow-run-host");
  await expect(host).toBeVisible();
  expect(await host.evaluate((element) => getComputedStyle(element).animationName)).toBe("none");
  await expect(host.getByRole("button", { name: "Run workflow", exact: true })).toBeEnabled();
  expect(fixture.pageErrors).toEqual([]);
});

test("mobile dark mode gives immediate green-dot feedback with reduced motion", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(() => localStorage.setItem("manor-theme-preference-v2", "dark"));
  const fixture = await installWorkflowFixture(page);
  const host = page.locator(".workspace-workflow-run-host");
  await host.getByRole("button", { name: "Run workflow", exact: true }).click();
  await expect(host.locator(".workflow-run-status")).toHaveText("Submitting", { timeout: 500 });
  const indicator = host.locator(".workflow-run-status-indicator");
  const appearance = await indicator.evaluate((element) => {
    const dot = getComputedStyle(element, "::before");
    return { color: dot.backgroundColor, width: dot.width, animation: dot.animationName };
  });
  const rgb = appearance.color.match(/\d+/g)!.map(Number);
  expect(rgb[1]).toBeGreaterThan(rgb[0]);
  expect(rgb[1]).toBeGreaterThan(rgb[2]);
  expect(appearance.width).toBe("5px");
  expect(appearance.animation).toBe("none");
  await expect.poll(() => fixture.submissions.length).toBe(1);
  await fixture.finish(true);
  await expect(host.locator(".workflow-run-status")).toHaveText("Running");
  expect(fixture.pageErrors).toEqual([]);
});

for (const width of [1440, 390]) {
  test(`Chat collects missing input in a dialog and preserves it on failure at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 950 });
    await page.addInitScript(() => localStorage.setItem("manor-interface-mode", "professional"));
    const fixture = await installWorkflowFixture(page, { status: "completed", attentionActivity: true, action: {
      kind: "workflow_retry", workflow_run_id: RUN, step_id: "step-0", retry_from_step_id: "step-0",
      options: ["retry"], business_outcome: "needs_input", values: { brief: "" },
      required_change: "Provide a source caption to continue.", observed_problem: ["The source caption is missing."],
      editable_input_schema: { type: "object", required: ["brief"], properties: {
        brief: { type: "string", minLength: 1, title: "Source caption", "x-ui": { control: "textarea" } },
      } },
    } });
    const host = page.locator(".workspace-workflow-run-host");
    await expect(host.locator(".workflow-run-status")).toHaveText("Input needed");
    await expect(page.getByText("Review workflow · Input needed", { exact: true })).toBeVisible();
    await expect(host.getByRole("textbox", { name: "Source caption" })).toBeHidden();
    await host.getByRole("button", { name: "Provide input", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Provide input", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Continue", exact: true })).toBeDisabled();
    expect(fixture.submissions).toHaveLength(0);
    await dialog.getByRole("textbox", { name: "Source caption" }).fill("A complete synthetic caption.");
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await expect.poll(() => fixture.submissions.length).toBe(1);
    await expect(dialog.getByRole("button", { name: "Continue", exact: true })).toBeDisabled();
    await fixture.finish(false);
    await expect(dialog.getByRole("alert")).toContainText("could not accept");
    await expect(dialog.getByRole("textbox", { name: "Source caption" })).toHaveValue("A complete synthetic caption.");
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await expect.poll(() => fixture.submissions.length).toBe(2);
    await fixture.finish(true);
    await expect(dialog).toBeHidden();
    await expect(host.locator(".workflow-run-status")).toHaveText("Running");
    expect(fixture.submissions).toEqual(Array(2).fill({ from_step_id: "step-0", variables: { brief: "A complete synthetic caption." }, execute: true }));
    expect(fixture.pageErrors).toEqual([]);
    expect(fixture.unexpectedWrites).toEqual([]);
  });
}
