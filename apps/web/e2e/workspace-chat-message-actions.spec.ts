import { expect, test, type Page } from "@playwright/test";

const WORKSPACE_ID = "workspace-message-actions-e2e";
const CONVERSATION_ID = "conversation-message-actions-e2e";
const MESSAGE_ID = "message-assistant-actions-e2e";
const RESPONSE = "This response can be copied and rated from Workspace Chat.";

async function installWorkspaceChatMock(
  page: Page,
  options: { retiredCompletion?: boolean } = {},
) {
  const feedbackBodies: Array<{ rating: "up" | "down" }> = [];
  const pageErrors: Error[] = [];
  const unexpectedRequests: string[] = [];
  const firstPageError = new Promise<never>((_resolve, reject) => {
    page.once("pageerror", reject);
  });
  let persistedRating: "up" | "down" | null = null;
  let mutationSequence = 0;

  page.on("pageerror", (error) => pageErrors.push(error));
  await page.addInitScript(() => {
    window.localStorage.setItem("manor_token", "workspace-message-actions-token");
    window.localStorage.setItem("manor_locale", "en");
    window.localStorage.setItem("manor_tour_completed", "true");
    window.localStorage.setItem("manor_consent_v1", JSON.stringify({
      v: 1,
      ts: "2026-08-24T00:00:00.000Z",
      locale: "en",
      categories: { functional: false, analytics: false, marketing: false },
    }));
  });

  await page.route("**/config", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        deployment_mode: "oss",
        environment: "e2e",
        email_enabled: false,
        fs_enabled: false,
        flows_available: false,
        flows_released: false,
        ai_credits_unlimited: true,
        support_tickets_enabled: false,
      }),
    });
  });

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;

    if (pathname === "/api/v1/auth/me") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "user-message-actions-e2e",
          username: "message-actions-e2e",
          display_name: "Message Actions E2E",
          email: "message-actions@example.test",
          entity_id: "entity-message-actions-e2e",
          role: "owner",
          locale: "en",
        }),
      });
      return;
    }

    const workspace = {
      id: WORKSPACE_ID,
      entity_id: "entity-message-actions-e2e",
      name: "Message actions workspace",
      status: "active",
      heartbeat_enabled: false,
      kind: "standard",
      settings: {},
    };
    if (pathname === "/api/v1/workspaces") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([workspace]),
      });
      return;
    }
    if (pathname === `/api/v1/workspaces/${WORKSPACE_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(workspace),
      });
      return;
    }
    if (pathname === `/api/v1/workspaces/${WORKSPACE_ID}/chat/messages/page`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{
            id: MESSAGE_ID,
            conversation_id: CONVERSATION_ID,
            created_at: "2026-08-24T12:00:00.000Z",
            updated_at: null,
            body: RESPONSE,
            message_kind: options.retiredCompletion ? "agent_update" : "chat",
            author_kind: "agent",
            author_subscription_id: null,
            refs: options.retiredCompletion
              ? [{ type: "task", id: "deleted-task-message-actions-e2e" }]
              : null,
            attachments: null,
            meta: options.retiredCompletion
              ? { feedback_target_kind: "none" }
              : null,
            pending_action: null,
            resolved_at: null,
            resolution: null,
          }],
          has_more: false,
          next_cursor: null,
          open_actions_complete: true,
        }),
      });
      return;
    }
    if (pathname === `/api/v1/workspaces/${WORKSPACE_ID}/stats/quick-view`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ordered_stat_ids: [],
          hidden_stat_ids: [],
          configured: false,
        }),
      });
      return;
    }
    if (pathname === `/api/v1/workspaces/${WORKSPACE_ID}/stats`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
      return;
    }

    if (pathname === `/api/v1/chat/conversations/${CONVERSATION_ID}/feedback`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(persistedRating ? [{
          message_id: MESSAGE_ID,
          rating: persistedRating,
          mutation_sequence: mutationSequence,
          target_kind: "response",
          target_id: MESSAGE_ID,
          task_id: null,
          plan_id: null,
        }] : []),
      });
      return;
    }
    if (
      pathname === `/api/v1/chat/conversations/${CONVERSATION_ID}/messages/${MESSAGE_ID}/feedback`
      && request.method() === "POST"
    ) {
      const body = request.postDataJSON() as { rating: "up" | "down" };
      feedbackBodies.push({ rating: body.rating });
      persistedRating = body.rating;
      mutationSequence += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          message_id: MESSAGE_ID,
          rating: persistedRating,
          mutation_sequence: mutationSequence,
          mutation_status: "accepted",
          updated_at: "2026-08-24T12:01:00.000Z",
          target_kind: "response",
          target_id: MESSAGE_ID,
          task_id: null,
          plan_id: null,
        }),
      });
      return;
    }

    if (
      pathname === `/api/v1/workspaces/${WORKSPACE_ID}/agents`
      || pathname === `/api/v1/workspaces/${WORKSPACE_ID}/chat/entrypoints`
      || pathname === `/api/v1/workspaces/${WORKSPACE_ID}/staff`
      || pathname === `/api/v1/workspaces/${WORKSPACE_ID}/chat/threads`
      || pathname === "/api/v1/agents"
      || pathname === "/api/v1/chat/conversations"
      || pathname === "/api/v1/chat/flow-entrypoints"
      || pathname === "/api/v1/auth/users"
      || pathname === "/api/v1/auth/users/directory"
      || pathname === "/api/v1/people/directory"
      || pathname === "/api/v1/staff"
      || pathname === "/api/v1/staff/roles"
    ) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    if (pathname === "/api/v1/notifications") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0, unread_count: 0 }),
      });
      return;
    }
    if (pathname === "/api/v1/platform/flags") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ flags: {} }),
      });
      return;
    }
    if (pathname === "/api/v1/admin/preferences") {
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
      return;
    }
    if (pathname === "/api/v1/tasks" || pathname === "/api/v1/goals") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0 }),
      });
      return;
    }

    unexpectedRequests.push(`${request.method()} ${pathname}`);
    await route.fulfill({
      status: 501,
      contentType: "application/json",
      body: JSON.stringify({ detail: `Unexpected E2E request: ${request.method()} ${pathname}` }),
    });
  });

  return { feedbackBodies, firstPageError, pageErrors, unexpectedRequests };
}

test("Workspace Chat copies responses and persists serialized feedback", async ({ context, page }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const state = await installWorkspaceChatMock(page);
  await page.goto(`/chat?workspace=${WORKSPACE_ID}`);

  await Promise.race([
    expect(page.getByText(RESPONSE, { exact: true })).toBeVisible(),
    state.firstPageError,
  ]);
  const message = page.locator(".chat-message-shell").filter({ hasText: RESPONSE });
  await message.hover();
  await message.getByRole("button", { name: "Copy response" }).click();
  await expect(page.getByRole("button", { name: "Copied" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(RESPONSE);

  const good = message.getByRole("button", { name: "Good response" });
  const needsImprovement = message.getByRole("button", { name: "Needs improvement" });
  await good.click();
  await needsImprovement.click();
  await expect.poll(() => state.feedbackBodies).toEqual([
    { rating: "up" },
    { rating: "down" },
  ]);
  await expect(good).toHaveAttribute("aria-pressed", "false");
  await expect(needsImprovement).toHaveAttribute("aria-pressed", "true");

  await page.reload();
  await expect(page.getByText(RESPONSE, { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Needs improvement" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(state.pageErrors).toEqual([]);
  expect(state.unexpectedRequests).toEqual([]);
});

test("retired Task completion remains copyable without rating actions", async ({
  context,
  page,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const state = await installWorkspaceChatMock(page, { retiredCompletion: true });
  await page.goto(`/chat?workspace=${WORKSPACE_ID}`);

  await Promise.race([
    expect(page.getByText(RESPONSE, { exact: true })).toBeVisible(),
    state.firstPageError,
  ]);
  const message = page.locator(".chat-message-shell").filter({ hasText: RESPONSE });
  await message.hover();
  await expect(message.getByRole("button", { name: "Copy response" })).toBeVisible();
  await expect(message.getByRole("button", { name: "Good response" })).toHaveCount(0);
  await expect(
    message.getByRole("button", { name: "Needs improvement" }),
  ).toHaveCount(0);

  await message.getByRole("button", { name: "Copy response" }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(RESPONSE);
  expect(state.feedbackBodies).toEqual([]);
  expect(state.pageErrors).toEqual([]);
  expect(state.unexpectedRequests).toEqual([]);
});
