import { expect, test, type Locator, type Page } from "@playwright/test";

const USER_ID = "sidebar-reorder-user";
const ENTITY_ID = "sidebar-reorder-entity";
const sections = [
  { name: "AI chats", list: ".manor-session-list", ids: ["ai-a", "ai-b"], key: `manor-chat-sidebar-ai:${ENTITY_ID}:${USER_ID}` },
  { name: "Workspaces", list: ".workspace-sidebar-list", ids: ["workspace-a", "workspace-b"], key: `manor-workspace-sidebar:${ENTITY_ID}:${USER_ID}` },
  { name: "Group chats", list: "#chat-sidebar-group", ids: ["group-a", "group-b"], key: `manor-chat-sidebar-group:${ENTITY_ID}:${USER_ID}` },
  { name: "Direct chats", list: "#chat-sidebar-dm", ids: ["room:human-a", "agent:agent-a"], key: `manor-chat-sidebar-dm:${ENTITY_ID}:${USER_ID}` },
];

async function mockSidebar(page: Page) {
  const errors: string[] = [];
  const mutations: string[] = [];
  page.on("pageerror", (error) => errors.push(error.stack || error.message));
  await page.addInitScript(({ userId, entityId }) => {
    const encode = (value: Record<string, unknown>) => btoa(JSON.stringify(value))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    localStorage.setItem("manor_token", `${encode({ alg: "none", typ: "JWT" })}.${encode({
      sub: userId, entity_id: entityId, role: "owner", typ: "access", exp: 4102444800,
    })}.fixture`);
    localStorage.setItem("manor_locale", "en");
    localStorage.setItem("manor-interface-mode", "professional");
    localStorage.setItem("manor-sidebar-collapsed", "0");
    localStorage.setItem("manor_tour_completed", "true");
    localStorage.setItem("manor-chat-sidebar-sections", JSON.stringify({ ai: true, workspaces: true, group: true, dm: true }));
    localStorage.setItem("manor_consent_v1", JSON.stringify({
      v: 1, ts: "2026-10-01T00:00:00Z", locale: "en",
      categories: { functional: false, analytics: false, marketing: false },
    }));
  }, { userId: USER_ID, entityId: ENTITY_ID });
  const workspaces = ["a", "b"].map((suffix, index) => ({
    id: `workspace-${suffix}`, entity_id: ENTITY_ID, name: `Sidebar workspace ${suffix}`,
    status: "active", kind: "standard", settings: {},
    created_at: `2026-10-0${2 - index}T00:00:00Z`,
  }));
  const conversations = ["a", "b"].map((suffix, index) => ({
    id: `ai-${suffix}`, entity_id: ENTITY_ID, user_id: USER_ID,
    title: `Sidebar AI ${suffix}`, channel: "web", status: "active",
    updated_at: `2026-10-0${2 - index}T00:00:00Z`,
  }));
  conversations.push({
    ...conversations[0], id: "agent-conversation", title: "Sidebar Agent chat", agent_id: "agent-a",
  } as typeof conversations[number]);
  const agents = [{
    id: "agent-a", entity_id: ENTITY_ID, name: "Sidebar Agent", config: {},
    is_template: false, is_public: false, tags: [], source: "custom", status: "active",
  }];
  const rooms = [
    { id: "group-a", kind: "group", title: "Sidebar group a" },
    { id: "group-b", kind: "group", title: "Sidebar group b" },
    { id: "human-a", kind: "dm", title: "Sidebar human" },
  ].map((room) => ({
    ...room, owner_scope: "personal", member_count: 2,
    members: [{ user_id: USER_ID, name: "Sidebar User", role: "owner" }, { user_id: "other-user", name: "Other User", role: "member" }],
    agents: [], can_manage_agents: true, notifications_muted: false,
    last_message_at: null, created_at: "2026-10-01T00:00:00Z", unread_count: 0,
  }));
  await page.route("**/config", (route) => route.fulfill({ json: {
    deployment_mode: "oss", environment: "e2e", email_enabled: false,
    fs_enabled: false, flows_available: false, flows_released: false, ai_credits_unlimited: true,
  } }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (!["GET", "HEAD"].includes(request.method())) {
      mutations.push(`${request.method()} ${path}`);
      await route.fulfill({ status: 501, json: { detail: "Unexpected sidebar mutation" } });
      return;
    }
    if (path === "/api/v1/auth/me") return route.fulfill({ json: {
      id: USER_ID, entity_id: ENTITY_ID, username: "sidebar-user", display_name: "Sidebar User",
      email: "sidebar@example.test", role: "owner", locale: "en",
    } });
    if (path === "/api/v1/workspaces") return route.fulfill({ json: workspaces });
    if (path === "/api/v1/chat/conversations") return route.fulfill({ json: conversations });
    if (path === "/api/v1/chat/rooms") return route.fulfill({ json: rooms });
    if (path === "/api/v1/chat/rooms/group-usage") return route.fulfill({ json: { used: true } });
    if (path === "/api/v1/agents/catalog") return route.fulfill({ json: { version: "sidebar-agents-v1", items: agents } });
    if (path === "/api/v1/skills/catalog") return route.fulfill({ json: { version: "sidebar-skills-v1", items: [] } });
    if (path.startsWith("/api/v1/approval-settings/")) return route.fulfill({ json: {
      scope: { kind: "personal", entity_id: ENTITY_ID }, revision: 1, version: "sidebar-policy-v1",
      policy: { mode: "risk_based", rules: [] }, effective_policy: { mode: "risk_based", rules: [] },
      inheritance: "root", root_scope: null, policy_versions: [],
      capabilities: { can_view: true, can_edit_root: true, can_edit_local: true, can_save_rule: true },
      action_catalog: [], effective_rows: [], saved_rules: [], inherited_rules: [], rule_sources: [],
    } });
    if (/\/messages\/page$/.test(path)) return route.fulfill({ json: { items: [], has_more: false, next_cursor: null } });
    if (path.startsWith("/api/v1/agent-initiatives/conversation/")) return route.fulfill({ json: { items: [], total: 0 } });
    if (path === "/api/v1/notifications") return route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
    if (path === "/api/v1/platform/flags") return route.fulfill({ json: { flags: {} } });
    if (path === "/api/v1/admin/preferences") return route.fulfill({ json: {
      "feature_discovery:group-chat:v1:seen": "2026-10-01T00:00:00Z",
      "feature_discovery:group-chat:v1:used": "2026-10-01T00:00:00Z",
    } });
    return route.fulfill({ json: [] });
  });
  return { errors, mutations };
}

async function openSidebar(page: Page) {
  if ((page.viewportSize()?.width || 1280) < 1024) {
    await page.locator(".mobile-hamburger").click();
    await expect(page.locator(".sidebar-mobile.open")).toHaveCSS("margin-left", "0px");
  }
  const toggle = page.locator('button[aria-controls="manor-chat-sessions"]');
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  for (const section of sections) {
    await expect(page.locator(section.list).locator("[data-sidebar-id]")).toHaveCount(2);
  }
}

function row(page: Page, id: string) {
  return page.locator(`[data-sidebar-id="${id}"]`);
}

function mainButton(element: Locator) {
  return element.locator(":scope > button").first();
}

async function order(page: Page, selector: string) {
  return page.locator(selector).locator("[data-sidebar-id]")
    .evaluateAll((rows) => rows.map((element) => element.getAttribute("data-sidebar-id")));
}

async function dragBefore(page: Page, sourceId: string, targetId: string) {
  const source = mainButton(row(page, sourceId));
  const target = row(page, targetId);
  await source.scrollIntoViewIfNeeded();
  await target.scrollIntoViewIfNeeded();
  const sourceBox = await source.boundingBox();
  const targetBox = await target.boundingBox();
  if (!sourceBox || !targetBox) throw new Error("Sidebar drag rows are not visible");
  await page.mouse.move(sourceBox.x + 35, sourceBox.y + sourceBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(targetBox.x + 35, targetBox.y + targetBox.height / 4, { steps: 8 });
  await page.mouse.up();
}

for (const width of [1280, 390]) {
  test(`sidebar uses arrow cursors and keeps keyboard order after reload at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await mockSidebar(page);
    await page.goto("/chat?new=chat");
    await openSidebar(page);
    for (const section of sections) {
      const button = mainButton(row(page, section.ids[1]));
      await button.scrollIntoViewIfNeeded();
      await button.hover();
      await expect(button).toHaveCSS("cursor", "default");
      await button.focus();
      await expect(button).toBeFocused();
      await button.press("ArrowUp");
      await expect.poll(() => order(page, section.list)).toEqual([...section.ids].reverse());
      await expect(button).toBeFocused();
      await expect(button).not.toHaveCSS("box-shadow", "none");
    }
    if (width === 1280) {
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      const collapsedButtons = page.locator(".workspace-collapsed-chat-item > button");
      await expect(collapsedButtons).toHaveCount(7);
      expect(await collapsedButtons.evaluateAll((buttons) => buttons.map((button) => button.getAttribute("aria-label"))))
        .toEqual([
          "Open Manor AI", "Open Sidebar workspace b", "Open Sidebar workspace a",
          "Open Sidebar group b", "Open Sidebar group a", "Open Sidebar Agent", "Open Sidebar human",
        ]);
      for (const button of await collapsedButtons.all()) await expect(button).toHaveCSS("cursor", "default");
      await expect(page.locator(".sidebar-mobile")).toHaveCSS("width", "92px");
      await page.screenshot({ path: testInfo.outputPath("sidebar-collapsed.png") });
      await page.getByRole("button", { name: "Expand sidebar", exact: true }).click();
    }
    await page.reload();
    await openSidebar(page);
    for (const section of sections) {
      expect(await order(page, section.list)).toEqual([...section.ids].reverse());
      expect(await page.evaluate((key) => JSON.parse(localStorage.getItem(key) || "null"), section.key))
        .toMatchObject({ mode: "manual", order: [...section.ids].reverse() });
    }
    await page.screenshot({ path: testInfo.outputPath(`sidebar-${width}.png`) });
    expect(state.errors).toEqual([]);
    expect(state.mutations).toEqual([]);
  });
}

test("drag reorders every chat section without opening the dragged chat", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  const state = await mockSidebar(page);
  await page.goto("/chat?new=chat");
  await openSidebar(page);
  const originalUrl = page.url();
  for (const section of sections) {
    await dragBefore(page, section.ids[1], section.ids[0]);
    await expect.poll(() => order(page, section.list)).toEqual([...section.ids].reverse());
    await expect(page).toHaveURL(originalUrl);
    await expect(page.locator("[data-sidebar-drop]")).toHaveCount(0);
  }
  // A normal click after the drag still opens the selected chat.
  await mainButton(row(page, "ai-b")).click();
  await expect(page).toHaveURL(/conversation=ai-b/);
  expect(state.errors).toEqual([]);
  expect(state.mutations).toEqual([]);
});

test("search and drops across sections preserve each list order", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  const state = await mockSidebar(page);
  await page.goto("/chat?new=chat");
  await openSidebar(page);
  await dragBefore(page, "workspace-b", "group-a");
  for (const section of sections) expect(await order(page, section.list)).toEqual(section.ids);
  await page.locator(".chat-sidebar-global-search").fill("Sidebar");
  for (const section of sections) {
    await mainButton(row(page, section.ids[1])).focus();
    await mainButton(row(page, section.ids[1])).press("ArrowUp");
    await dragBefore(page, section.ids[1], section.ids[0]);
    expect(await order(page, section.list)).toEqual(section.ids);
    expect(await page.evaluate((key) => localStorage.getItem(key), section.key)).toBeNull();
  }
  expect(state.errors).toEqual([]);
  expect(state.mutations).toEqual([]);
});
