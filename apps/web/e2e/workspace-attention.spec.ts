import { expect, test } from "@playwright/test";

test.use({ launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } });

// Mount the real AppLayout and WorkspaceChat together using the existing fixture.
// All API effects are intercepted; this test cannot execute work or send email.
for (const width of [1440, 390]) {
  test(`sidebar is the pending-action entry and retains already-read actions at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const messages = [0, 1].map((n) => ({
      id: `pending-${n}`, conversation_id: "fixture-conversation",
      created_at: `2026-10-19T12:0${n}:00Z`, body: `Review request ${n}`,
      author_kind: "system", message_kind: "hitl_request", refs: [], attachments: [], meta: {},
      pending_action: { kind: "human_input", title: `Question ${n}`, step_id: `step-${n}` },
      resolved_at: null as string | null, resolution: null,
    }));
    const unexpectedWrites: string[] = [];
    const attentionScopes: (string | null)[] = [];
    page.on("pageerror", (error) => { throw error; });
    await page.route("**/config", (route) => route.fulfill({ json: {} }));
    await page.route("**/api/v1/**", (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const open = messages.filter((message) => !message.resolved_at);
      const workspace = {
        id: "app-fixture", name: "Pending Studio", status: "active", heartbeat_enabled: true,
        settings: {}, operating_model: {},
        stats: { chat_pending_actions: open.length, proposal_actions: 0, failed_actions: 0, unseen_actions: 0 },
      };
      if (request.method() !== "GET") {
        unexpectedWrites.push(`${request.method()} ${path}`);
        return route.fulfill({ status: 501, json: { detail: "Unexpected mutation" } });
      }
      if (path === "/api/v1/workspaces") return route.fulfill({ json: [workspace] });
      if (path === "/api/v1/workspaces/app-fixture") return route.fulfill({ json: workspace });
      if (path.endsWith("/app-view")) return route.fulfill({ json: {
        workspace_id: workspace.id, title: workspace.name, status: "active", can_start: true,
        config: { visual: "plain", layout: "feature" }, actions: [],
        agent_options: [], workflow_options: [], automation_options: [],
      } });
      if (path.endsWith("/chat/messages/page")) return route.fulfill({ json: {
        items: messages, has_more: false, next_cursor: null, open_actions_complete: true,
        open_action_count: open.length, unseen_action_keys: {},
      } });
      if (path.endsWith("/chat/attention")) {
        attentionScopes.push(url.searchParams.get("scope"));
        return route.fulfill({ json: { items: url.searchParams.get("scope") === "open"
          ? open.toReversed().map((message) => ({
            key: message.id, message_id: message.id, source_id: message.id,
            kind: "human_input", summary: message.body, created_at: message.created_at,
          })) : [] } });
      }
      if (path.endsWith("/connection-status")) return route.fulfill({ json: { requirements: [] } });
      if (path.includes("/agent-initiatives")) return route.fulfill({ json: { items: [], total: 0 } });
      if (path.startsWith("/api/v1/approval-settings/")) return route.fulfill({ json: {
        scope: { kind: "workspace", id: workspace.id, entity_id: "loading-entity" },
        revision: 1, version: "fixture-policy", policy: { mode: "risk_based", rules: [] },
        effective_policy: { mode: "risk_based", rules: [] }, inheritance: "root", root_scope: null,
        policy_versions: [], capabilities: { can_view: true, can_edit_root: true, can_edit_local: true, can_save_rule: true },
        action_catalog: [], effective_rows: [], saved_rules: [], inherited_rules: [], rule_sources: [],
      } });
      return route.fulfill({ json: [] });
    });
    await page.goto("/e2e/fixtures/workspace-chat-loading.html");
    const badge = page.getByRole("button", { name: "Open pending actions in Pending Studio (2 pending)", exact: true });
    const chatHistory = page.locator(".workspace-chat-history-stage");
    if (width < 1024) await page.getByRole("button", { name: "Open chat", exact: true }).click();
    await expect(chatHistory).toBeVisible();
    await expect(page.locator("#workspace-chat-message-pending-0")).toBeVisible();
    await expect(page.locator(".workspace-pending-actions-banner")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Go to oldest", exact: true })).toHaveCount(0);
    await page.screenshot({ animations: "disabled", path: testInfo.outputPath(`chat-without-pending-banner-${width}.png`) });
    if (width < 1024) await page.locator(".mobile-hamburger").click();
    const workspaceGroup = page.getByRole("region", { name: "Workspaces", exact: true });
    await workspaceGroup.getByRole("button", { name: /^Workspaces/ }).click();
    await expect(badge).toHaveText("2");
    await badge.focus();
    await badge.press("Enter");
    await expect.poll(() => attentionScopes).toEqual(["open"]);
    await expect(page.locator("#workspace-chat-message-pending-0")).toHaveClass(/workspace-chat-message--highlighted/);
    await expect(badge).toHaveText("2");
    await page.screenshot({ animations: "disabled", path: testInfo.outputPath(`pending-already-read-${width}.png`) });
    // Simulate an actual resolution from another client; a read receipt above cannot do this.
    messages[0].resolved_at = "2026-10-19T13:00:00Z";
    await page.reload();
    await expect(page.locator("#workspace-chat-message-pending-1")).toBeAttached();
    if (width < 1024 && !(await chatHistory.isVisible())) {
      await page.getByRole("button", { name: "Open chat", exact: true }).click();
    }
    await expect(chatHistory).toBeVisible();
    await expect(page.locator(".workspace-pending-actions-banner")).toHaveCount(0);
    if (width < 1024) await page.locator(".mobile-hamburger").click();
    await expect(page.getByRole("button", { name: "Open pending actions in Pending Studio (1 pending)", exact: true })).toHaveText("1");
    expect(unexpectedWrites).toEqual([]);
  });
}
