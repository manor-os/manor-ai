import { expect, test, type Page } from "@playwright/test";

const CONVERSATION_ID = "conversation-recommendation-e2e";
const WORKSPACE_ID = "workspace-recommendation-exact-id";
const REQUEST = "Run a weekly product launch with owners, milestones, and progress tracking.";

type RecommendationAction = "create_new" | "open_existing" | "add_to_existing";

function conversationMessages(action: RecommendationAction) {
  return [
    {
      id: "message-user",
      conversation_id: CONVERSATION_ID,
      role: "user",
      content: REQUEST,
      created_at: "2026-08-22T12:00:00Z",
    },
    {
      id: `message-assistant-${action}`,
      conversation_id: CONVERSATION_ID,
      role: "assistant",
      content: "I can help shape the launch plan and the first set of milestones.",
      meta: {
        stream_status: "completed",
        workspace_recommendation: {
          action,
          reason: "This is recurring work with owners and progress that should stay visible across weeks.",
          request: REQUEST,
          workspace_id: action === "create_new" ? null : WORKSPACE_ID,
          workspace_name: action === "create_new" ? null : "Launch operations",
        },
      },
      created_at: "2026-08-22T12:00:01Z",
    },
  ];
}

async function mockRecommendationApp(page: Page, action: RecommendationAction) {
  const postedWorkspaceMessages: Array<{ url: string; body: unknown }> = [];
  const chatStreamBodies: string[] = [];
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

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
    const url = new URL(request.url());
    const { pathname } = url;

    if (pathname === "/api/v1/auth/me") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "user-recommendation-e2e",
          username: "recommendation-e2e",
          display_name: "Recommendation E2E",
          email: "recommendation@example.test",
          entity_id: "entity-recommendation-e2e",
          role: "owner",
          locale: "en",
        }),
      });
      return;
    }

    if (pathname === "/api/v1/chat/conversations") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{
          id: CONVERSATION_ID,
          entity_id: "entity-recommendation-e2e",
          user_id: "user-recommendation-e2e",
          title: "Weekly launch",
          channel: "internal",
          status: "active",
        }]),
      });
      return;
    }

    if (pathname === `/api/v1/chat/conversations/${CONVERSATION_ID}/messages/page`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: conversationMessages(action),
          has_more: false,
          next_cursor: null,
        }),
      });
      return;
    }

    if (pathname === `/api/v1/workspaces/${WORKSPACE_ID}/chat/messages`) {
      expect(request.method()).toBe("POST");
      postedWorkspaceMessages.push({
        url: request.url(),
        body: request.postDataJSON(),
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: "workspace-message-e2e", body: REQUEST }),
      });
      return;
    }

    if (pathname === "/api/v1/chat/stream") {
      chatStreamBodies.push(request.postData() || "");
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: [
          `event: stream_start\ndata: {"conversation_id":"${CONVERSATION_ID}","message_id":"assistant-new"}\n\n`,
          "event: text_delta\ndata: {\"content\":\"I started and saved the Workspace draft in this Chat.\"}\n\n",
          `event: stream_end\ndata: {"conversation_id":"${CONVERSATION_ID}","message_id":"assistant-new","persisted":true,"rounds":1,"tool_calls":[]}\n\n`,
        ].join(""),
      });
      return;
    }

    if (pathname === "/api/v1/workspaces") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{
          id: WORKSPACE_ID,
          name: "Launch operations",
          status: "active",
          entity_id: "entity-recommendation-e2e",
        }]),
      });
      return;
    }

    if (pathname === `/api/v1/workspaces/${WORKSPACE_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: WORKSPACE_ID,
          name: "Launch operations",
          status: "active",
          entity_id: "entity-recommendation-e2e",
        }),
      });
      return;
    }

    if (
      pathname === "/api/v1/agents" ||
      pathname === "/api/v1/chat/flow-entrypoints" ||
      pathname === "/api/v1/auth/users" ||
      pathname === "/api/v1/auth/users/directory" ||
      pathname === "/api/v1/people/directory" ||
      pathname === "/api/v1/staff" ||
      pathname === "/api/v1/staff/roles"
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

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], total: 0 }),
    });
  });

  await page.addInitScript(() => {
    window.localStorage.setItem("manor_token", "e2e-token");
    window.localStorage.setItem("manor_locale", "en");
    window.localStorage.setItem("manor_tour_completed", "true");
    window.localStorage.setItem("manor_consent_v1", JSON.stringify({
      v: 1,
      ts: "2026-08-22T00:00:00.000Z",
      locale: "en",
      categories: { functional: false, analytics: false, marketing: false },
    }));
  });

  return { postedWorkspaceMessages, chatStreamBodies, pageErrors };
}

test("ordinary Chat can add a recommendation to the exact existing Workspace", async ({ page }) => {
  const state = await mockRecommendationApp(page, "add_to_existing");
  await page.goto("/chat");

  const card = page.getByTestId("workspace-recommendation-card");
  await expect(card).toBeVisible();
  await expect(card.getByText("This may work better in a Workspace")).toBeVisible();
  await expect(page.getByText("Continue setup in the main Chat.")).toHaveCount(0);
  await card.getByRole("button", { name: "Add to Launch operations" }).click();

  await expect.poll(() => state.postedWorkspaceMessages.length).toBe(1);
  expect(state.postedWorkspaceMessages[0].url).toContain(
    `/workspaces/${WORKSPACE_ID}/chat/messages`,
  );
  expect(state.postedWorkspaceMessages[0].body).toMatchObject({
    body: expect.stringContaining(REQUEST),
  });
  await expect(page).toHaveURL(new RegExp(`/workspaces/${WORKSPACE_ID}$`));
});

test("ordinary Chat starts recommended Workspace creation inside the same conversation", async ({ page }) => {
  const state = await mockRecommendationApp(page, "create_new");
  await page.goto("/chat");

  const card = page.getByTestId("workspace-recommendation-card");
  await card.getByRole("button", { name: "Create Workspace" }).click();

  await expect.poll(() => state.chatStreamBodies.length).toBe(1);
  expect(state.chatStreamBodies[0]).toContain("Create a Workspace");
  expect(state.chatStreamBodies[0]).toContain("weekly product launch");
  await expect(page).toHaveURL(/\/chat$/);
  await expect(card).toHaveCount(0);
  expect(state.pageErrors).toEqual([]);
});

test("ordinary Chat can open an exact existing Workspace without posting", async ({ page }) => {
  const state = await mockRecommendationApp(page, "open_existing");
  await page.goto("/chat");

  const card = page.getByTestId("workspace-recommendation-card");
  await card.getByRole("button", { name: "Open Launch operations" }).click();

  await expect(page).toHaveURL(new RegExp(`/workspaces/${WORKSPACE_ID}$`));
  expect(state.postedWorkspaceMessages).toHaveLength(0);
});

test("ordinary Chat can dismiss the recommendation and keep chatting", async ({ page }) => {
  const state = await mockRecommendationApp(page, "add_to_existing");
  await page.goto("/chat");

  const card = page.getByTestId("workspace-recommendation-card");
  await card.getByRole("button", { name: "Continue in Chat" }).click();

  await expect(card).toHaveCount(0);
  await expect(page).toHaveURL(/\/chat$/);
  expect(state.postedWorkspaceMessages).toHaveLength(0);
  expect(state.pageErrors).toEqual([]);
});

test("floating Chat projects the recommendation on viewer pages", async ({ page }) => {
  const state = await mockRecommendationApp(page, "add_to_existing");
  await page.goto(`/viewer/${WORKSPACE_ID}`);

  await page.locator("button.float-chat-btn").click();
  const card = page.getByTestId("workspace-recommendation-card");
  await expect(card).toBeVisible();
  await expect(card.getByRole("button", { name: "Add to Launch operations" })).toBeVisible();
  expect(state.pageErrors).toEqual([]);
});
