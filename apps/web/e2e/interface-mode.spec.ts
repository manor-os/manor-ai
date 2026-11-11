import { expect, test, type Page } from "@playwright/test";

const WORKSPACE_ID = "interface-mode-workspace";
const CONVERSATION_ID = "interface-mode-conversation";
const INSTALLED_ID = "interface-mode-installed";
const RESPONSE = "The existing Workspace conversation is still here.";
const DRAFT = "Keep this unsent draft while I change the interface.";
const FIRST_RUN_TITLE = "Interface mode";

async function mockInterfaceApp(
  page: Page,
  mode?: "simple" | "professional",
  setupChecks: Array<{ kind: string; reason: string }> = [],
  createdAt = "2026-09-19T00:00:00Z",
) {
  const mutations: Array<{ path: string; body: unknown }> = [];
  const errors: string[] = [];
  const workspaces = [{
    id: WORKSPACE_ID,
    entity_id: "interface-mode-entity",
    name: "Interface mode workspace",
    status: "active",
    heartbeat_enabled: false,
    kind: "standard",
    settings: {},
  }];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript((initialMode) => {
    const encodeClaims = (value: Record<string, unknown>) => btoa(JSON.stringify(value))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    // Catalog reads use the token's identity scope even with a mocked auth API.
    // This unsigned fixture token is consumed only by the intercepted test app.
    localStorage.setItem("manor_token", `${encodeClaims({ alg: "none", typ: "JWT" })}.${encodeClaims({
      sub: "interface-mode-user",
      entity_id: "interface-mode-entity",
      role: "owner",
      token_version: 1,
      typ: "access",
      amr: ["password"],
      exp: 4102444800,
    })}.fixture`);
    localStorage.setItem("manor_locale", "en");
    // Keep automatic onboarding out of unrelated tests. User Guide replay must
    // still start a tour for this already-completed preference.
    localStorage.setItem("manor_tour_completed", "true");
    localStorage.setItem("manor-sidebar-collapsed", "0");
    localStorage.setItem("manor_consent_v1", JSON.stringify({
      v: 1,
      ts: "2026-09-17T00:00:00.000Z",
      locale: "en",
      categories: { functional: false, analytics: false, marketing: false },
    }));
    // Seed once, so a reload exercises the setting saved by the real UI.
    if (initialMode && !localStorage.getItem("manor-interface-mode")) {
      localStorage.setItem("manor-interface-mode", initialMode);
    }
  }, mode);
  await page.route("**/config", (route) => route.fulfill({ json: {
    deployment_mode: "oss",
    environment: "e2e",
    email_enabled: false,
    fs_enabled: false,
    flows_available: false,
    flows_released: false,
    ai_credits_unlimited: true,
    support_tickets_enabled: true,
  } }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (!["GET", "HEAD"].includes(request.method())) {
      mutations.push({ path, body: request.postDataJSON() });
      if (path === "/api/v1/blueprints/install-payload") {
        workspaces.push({ ...workspaces[0], id: INSTALLED_ID, name: "Imported workspace" });
        await route.fulfill({ json: { workspace_id: INSTALLED_ID, todos: [] } });
        return;
      }
      await route.fulfill({ status: 501, json: { detail: `Unexpected mutation: ${path}` } });
      return;
    }
    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: {
        id: "interface-mode-user",
        username: "interface-mode-user",
        display_name: "Interface Mode User",
        email: "interface-mode@example.test",
        entity_id: "interface-mode-entity",
        role: "owner",
        locale: "en",
        created_at: createdAt,
      } });
      return;
    }
    if (path === "/api/v1/agents/catalog" || path === "/api/v1/skills/catalog") {
      const version = path.includes("/agents/") ? "interface-agents-v1" : "interface-skills-v1";
      await route.fulfill({ json: {
        version,
        items: url.searchParams.get("known_version") === version ? null : [],
      } });
      return;
    }
    if (path === "/api/v1/workspaces") {
      await route.fulfill({ json: workspaces });
      return;
    }
    const workspace = workspaces.find((item) => path === `/api/v1/workspaces/${item.id}`);
    if (workspace) {
      await route.fulfill({ json: workspace });
      return;
    }
    if (/\/workspaces\/[^/]+\/chat\/messages\/page$/.test(path)) {
      await route.fulfill({ json: {
        conversation_id: CONVERSATION_ID,
        items: [{
          id: "interface-mode-message",
          conversation_id: CONVERSATION_ID,
          created_at: "2026-09-17T12:00:00.000Z",
          body: RESPONSE,
          message_kind: "chat",
          author_kind: "agent",
          author_subscription_id: null,
          refs: null,
          attachments: null,
          meta: null,
          pending_action: null,
          resolved_at: null,
          resolution: null,
        }],
        has_more: false,
        next_cursor: null,
        open_actions_complete: true,
      } });
      return;
    }
    if (path.endsWith("/stats/quick-view")) {
      await route.fulfill({ json: { ordered_stat_ids: [], hidden_stat_ids: [], configured: false } });
      return;
    }
    if (path.endsWith("/setup-status")) {
      await route.fulfill({ json: {
        ready: setupChecks.length === 0,
        status: setupChecks.length ? "blocked" : "ready",
        summary: "",
        incomplete_checks: setupChecks,
      } });
      return;
    }
    if (path === "/api/v1/workspaces/trash/grace-days") {
      await route.fulfill({ json: { grace_days: 30 } });
      return;
    }
    if (path === "/api/v1/notifications") {
      await route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
      return;
    }
    if (path === "/api/v1/platform/flags") {
      await route.fulfill({ json: { flags: {} } });
      return;
    }
    if (path === "/api/v1/admin/preferences") {
      // Keep the unrelated group-chat discovery hint out of this flow.
      await route.fulfill({ json: { "feature_discovery:group-chat:v1:seen": "2026-09-17T00:00:00Z" } });
      return;
    }
    if (path.startsWith("/api/v1/agent-initiatives/conversation/")) {
      await route.fulfill({ json: { items: [], total: 0 } });
      return;
    }
    if (
      ["/api/v1/tasks", "/api/v1/goals"].includes(path)
      || /\/workspaces\/[^/]+\/stats$/.test(path)
    ) {
      await route.fulfill({ json: { items: [], total: 0 } });
      return;
    }
    await route.fulfill({ json: [] });
  });
  return { mutations, errors };
}

async function selectMode(page: Page, label: "Simple" | "Professional") {
  await page.getByRole("button", { name: "Interface mode", exact: true }).click();
  await page.getByRole("option", { name: label, exact: true }).click();
}

async function openDisplaySettings(page: Page) {
  await page.locator(".app-shell-sidebar").getByRole("button", { name: "Interface Mode User", exact: true }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByRole("complementary", { name: "Settings navigation" })
    .getByRole("button", { name: "Appearance", exact: true }).click();
}

async function openMoreMenu(page: Page) {
  if ((page.viewportSize()?.width || 1280) < 1280 && !await page.locator(".sidebar-mobile.open").count()) {
    await page.locator(".mobile-hamburger").click();
  }
  await page.locator(".app-shell-sidebar").getByRole("button", { name: "Interface Mode User", exact: true }).click();
}

async function startUserGuide(page: Page) {
  await openMoreMenu(page);
  const sidebar = page.locator(".app-shell-sidebar");
  await expect(sidebar.getByRole("button", { name: FIRST_RUN_TITLE, exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Support", exact: true })).toHaveCount(1);
  const guide = page.getByRole("button", { name: "User Guide", exact: true });
  await expect(guide).toHaveCount(1);
  await guide.focus();
  await expect(guide).toBeFocused();
  await guide.press("Enter");
}

for (const width of [1280, 390]) {
  for (const mode of ["simple", "professional"] as const) {
    test(`User Guide follows the current ${mode} mode at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 });
      const state = await mockInterfaceApp(page, mode);
      await page.goto("/notifications");
      await startUserGuide(page);
      await expect(page.getByRole("dialog", { name: FIRST_RUN_TITLE, exact: true })).not.toBeVisible();
      if (mode === "simple") {
        await expect(page.getByText("Tell Manor what you need.", { exact: true })).toBeVisible();
        await expect(page.getByRole("heading", { name: "Where things live", exact: true })).not.toBeVisible();
      } else {
        await expect(page.getByRole("heading", { name: "Where things live", exact: true })).toBeVisible();
      }
      expect(await page.evaluate(() => localStorage.getItem("manor-interface-mode"))).toBe(mode);
      await page.getByRole("button", { name: "Skip", exact: true }).click();
      expect(state.mutations).toEqual([]);
      expect(state.errors).toEqual([]);
    });
  }
}

for (const width of [1280, 390]) {
  test(`Simple User Guide covers the complete chat sidebar at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    const state = await mockInterfaceApp(page, "simple");
    await page.goto("/chat");
    await startUserGuide(page);
    const steps = [
      { title: "Chat", target: "[data-tour='chat-input'], .chat-composer" },
      { title: "Conversations", target: "[data-tour='chat-sidebar-ai-chats']" },
      { title: "Workspaces", target: "[data-tour='chat-sidebar-workspaces']" },
      { title: "Create Workspace", target: "[data-tour='create-workspace']" },
      { title: "More", target: "[data-tour='sidebar-more']" },
    ];
    for (const [index, step] of steps.entries()) {
      const heading = page.getByRole("heading", { name: step.title, exact: true });
      await expect(heading).toBeVisible();
      await expect(page.getByText(`${index + 1} / 7`, { exact: true })).toBeVisible();
      const target = page.locator(step.target).filter({ visible: true }).first();
      await expect(target).toBeVisible();
      await expect.poll(async () => target.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight;
      })).toBe(true);
      const card = heading.locator("..");
      await expect.poll(async () => card.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight;
      })).toBe(true);
      await expect.poll(async () => {
        const anchor = await target.boundingBox();
        const tooltip = await card.boundingBox();
        if (!anchor || !tooltip) return false;
        return tooltip.x >= anchor.x + anchor.width || tooltip.x + tooltip.width <= anchor.x
          || tooltip.y >= anchor.y + anchor.height || tooltip.y + tooltip.height <= anchor.y;
      }).toBe(true);
      if (index === 1) {
        await page.getByRole("button", { name: "Back", exact: true }).click();
        await expect(page.getByRole("heading", { name: "Chat", exact: true })).toBeVisible();
        if (width < 1280) await expect(page.locator(".sidebar-mobile")).not.toHaveClass(/open/);
        await page.getByRole("button", { name: "Next", exact: true }).click();
        await expect(heading).toBeVisible();
      }
      await page.getByRole("button", { name: index === steps.length - 1 ? "Done" : "Next", exact: true }).click();
    }
    await expect(page.getByRole("button", { name: "Skip", exact: true })).not.toBeVisible();
    await expect(page.locator(".sidebar-mobile")).not.toHaveClass(/open/);
    expect(await page.evaluate(() => localStorage.getItem("manor-interface-mode"))).toBe("simple");
    expect(await page.evaluate(() => localStorage.getItem("manor-sidebar-collapsed"))).toBe("0");
    expect(await page.evaluate(() => localStorage.getItem("manor_tour_completed"))).toBe("true");
    await startUserGuide(page);
    await expect(page.getByRole("heading", { name: "Chat", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Skip", exact: true })).not.toBeVisible();
    expect(state.mutations).toEqual([]);
    expect(state.errors).toEqual([]);
  });
}

test("a new account starts in Professional without asking for an interface choice", async ({ page }) => {
  const state = await mockInterfaceApp(page);
  await page.goto("/chat");
  await expect(page.locator('[role="textbox"][contenteditable="true"]')).toBeVisible();
  await expect(page.getByRole("dialog", { name: FIRST_RUN_TITLE, exact: true })).not.toBeVisible();
  await page.goto("/settings?tab=appearance");
  const modes = page.getByRole("radiogroup", { name: "Interface mode", exact: true });
  await expect(modes.getByRole("radio", { name: /^Professional/ })).toBeChecked();
  // The default is not a saved browser choice; Settings is where people change it.
  expect(await page.evaluate(() => localStorage.getItem("manor-interface-mode"))).toBeNull();
  expect(state.mutations).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("a first-time account is greeted with one question that fills the composer", async ({ page }) => {
  const state = await mockInterfaceApp(page);
  // No Workspaces yet: the account is brand new.
  await page.route("**/api/v1/workspaces?*", (route) => route.fulfill({ json: [] }));
  await page.goto("/chat");
  // A new account cycles through example Workspaces in "Hand it to ___."
  await expect(page.getByRole("heading", { name: /^Hand it to .+\.$/ })).toBeVisible();
  const composer = page.locator('[role="textbox"][contenteditable="true"]');
  await expect(composer).toHaveAttribute("data-placeholder", "Message Manor");
  await page.getByRole("button", { name: "Café or restaurant", exact: true }).click();
  await expect(composer).toHaveText("I run a café and want regulars to come back more often,");
  expect(state.mutations).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("an existing account defaults to Professional in a new browser without opening the interface choice", async ({ page }) => {
  const state = await mockInterfaceApp(page, undefined, [], "2026-09-17T00:00:00Z");
  await page.goto("/settings?tab=appearance");
  const selector = page.getByRole("button", { name: "Interface mode", exact: true });
  await expect(selector).toHaveText("Professional");
  await expect(page.getByRole("dialog", { name: FIRST_RUN_TITLE, exact: true })).not.toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("manor-interface-mode"))).toBeNull();
  await page.reload();
  await expect(selector).toHaveText("Professional");
  await expect(page.getByRole("dialog", { name: FIRST_RUN_TITLE, exact: true })).not.toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("manor-interface-mode"))).toBeNull();
  expect(state.mutations).toEqual([]);
  expect(state.errors).toEqual([]);
});

for (const mode of ["simple", "professional"] as const) {
  test(`a saved ${mode} preference skips the first interface choice`, async ({ page }) => {
    const state = await mockInterfaceApp(page, mode);
    await page.goto("/settings?tab=appearance");
    await expect(page.getByRole("button", { name: "Interface mode", exact: true }))
      .toHaveText(mode === "simple" ? "Simple" : "Professional");
    await expect(page.getByRole("dialog", { name: FIRST_RUN_TITLE, exact: true })).not.toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem("manor-interface-mode"))).toBe(mode);
    expect(state.mutations).toEqual([]);
    expect(state.errors).toEqual([]);
  });
}

test("the real display setting changes an existing preference and persists after reload", async ({ page }) => {
  const state = await mockInterfaceApp(page, "professional");
  await page.goto("/settings?tab=appearance");
  const selector = page.getByRole("button", { name: "Interface mode", exact: true });
  await expect(selector).toHaveText("Professional");
  await selectMode(page, "Simple");
  await expect(selector).toHaveText("Simple");
  await expect(page).toHaveURL(/\/settings\?tab=appearance$/);
  await page.reload();
  await expect(selector).toHaveText("Simple");
  await selectMode(page, "Professional");
  await page.reload();
  await expect(selector).toHaveText("Professional");
  expect(state.mutations).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("switching views preserves the Workspace conversation and unsent draft", async ({ page }) => {
  const state = await mockInterfaceApp(page, "professional");
  await page.goto(`/chat?workspace=${WORKSPACE_ID}`);
  await expect(page.getByText(RESPONSE, { exact: true })).toBeVisible();
  const composer = page.locator('[role="textbox"][contenteditable="true"]');
  await composer.fill(DRAFT);
  await openDisplaySettings(page);
  await selectMode(page, "Simple");
  await page.getByRole("button", { name: "Back to app", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/chat\\?workspace=${WORKSPACE_ID}$`));
  await expect(page.getByText(RESPONSE, { exact: true })).toBeVisible();
  await expect(composer).toHaveText(DRAFT);
  await expect(composer).toHaveAttribute("data-placeholder", "What would you like to do?");
  await openDisplaySettings(page);
  await selectMode(page, "Professional");
  await page.getByRole("button", { name: "Back to app", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/chat\\?workspace=${WORKSPACE_ID}$`));
  await expect(page.getByText(RESPONSE, { exact: true })).toBeVisible();
  await expect(composer).toHaveText(DRAFT);
  expect(state.mutations).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("simple mode excludes professional pages while display settings remain reachable", async ({ page }) => {
  const state = await mockInterfaceApp(page, "simple");
  await page.goto("/");
  await expect(page).toHaveURL(/\/chat$/);
  await page.goto("/chat/legacy-conversation-link");
  await expect(page).toHaveURL(/\/chat\/history$/);
  await page.goto(`/workspaces/${WORKSPACE_ID}`);
  await expect(page.getByText("Professional mode page", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open display settings", exact: true }).click();
  await expect(page.getByRole("button", { name: "Interface mode", exact: true })).toHaveText("Simple");
  expect(await page.evaluate(() => localStorage.getItem("manor-interface-mode"))).toBe("simple");
  expect(state.mutations).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("simple mode imports an OSS Workspace through the existing installer and opens its chat", async ({ page }) => {
  const state = await mockInterfaceApp(page, "simple");
  await page.goto("/workspaces");
  await page.getByRole("button", { name: "Import Workspace", exact: true }).first().click();
  await page.locator('input[type="file"]').setInputFiles({
    name: "interface-mode-blueprint.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify({ manifest: { title: "Imported workspace" }, recipe: {} })),
  });
  await page.getByRole("button", { name: "Import Workspace", exact: true }).last().click();
  await expect(page).toHaveURL(new RegExp(`/chat\\?workspace=${INSTALLED_ID}(?:&.*)?$`));
  await expect(page.getByText(RESPONSE, { exact: true })).toBeVisible();
  expect(state.mutations).toEqual([{
    path: "/api/v1/blueprints/install-payload",
    body: expect.objectContaining({
      payload: { manifest: { title: "Imported workspace" }, recipe: {} },
      mode: "live",
      governance_preset: "standard",
    }),
  }]);
  expect(state.errors).toEqual([]);
});

test("simple chat retains blocking setup information and the exact repair destination", async ({ page }) => {
  const reason = "Connect a channel before this Workspace can start.";
  const state = await mockInterfaceApp(page, "simple", [{ kind: "channel", reason }]);
  await page.goto(`/chat?workspace=${WORKSPACE_ID}&setup=1`);
  await expect(page.getByText(reason, { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Review setup", exact: true })).toHaveAttribute(
    "href", `/workspaces/${WORKSPACE_ID}?tab=channels&setup=1`,
  );
  expect(state.mutations).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("settings chat history preserves a channel link and customer chat actions", async ({ page }) => {
  await mockInterfaceApp(page);
  const chatPath = `/api/v1/chat/conversations/${CONVERSATION_ID}`;
  const conversation = { id: CONVERSATION_ID, channel: "webchat", title: "webchat: Taylor" };
  let deleted = false;
  await page.route("**/api/v1/chat/conversations?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const scoped = params.get("workspace_id") === WORKSPACE_ID && params.get("channel") === "webchat";
    return route.fulfill({ json: scoped && !deleted ? [conversation] : [] });
  });
  await page.route(`**${chatPath}/messages?**`, (route) => route.fulfill({ json: [{
    id: "visitor-message", role: "user", content: "Order arrived today.",
  }] }));
  await page.route(`**${chatPath}`, (route) => {
    if (route.request().method() === "DELETE") {
      deleted = true;
      return route.fulfill({ status: 204 });
    }
    conversation.title = route.request().postDataJSON().title;
    return route.fulfill({ json: conversation });
  });
  await page.goto(`/chat/history?workspace=${WORKSPACE_ID}&channel=webchat`, { waitUntil: "domcontentloaded" });
  await expect(page).toHaveURL(/\/settings\?.*tab=history/);
  await page.getByRole("button", { name: /Taylor/ }).click();
  await expect(page.getByText("Order arrived today.")).toBeVisible();
  await page.getByRole("button", { name: "Rename chat", exact: true }).click();
  const titleInput = page.getByRole("textbox", { name: "Rename chat", exact: true });
  await titleInput.fill("Order: delivery confirmed");
  await titleInput.press("Enter");
  await expect(page.getByRole("heading", { name: "Order: delivery confirmed" })).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("Order arrived today.")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Chat History", exact: true })).toBeVisible();
  await expect(page.getByText("Order: delivery confirmed")).toHaveCount(0);
});
