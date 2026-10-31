import { expect, test, type Page } from "@playwright/test";

const initialToken = "synthetic-public-invitation-token-123456789";
const room = {
  id: "group", kind: "group", title: "Open study group", owner_scope: "personal", member_count: 1,
  members: [{ user_id: "inviter", name: "Inviter", role: "owner" }], agents: [], online_user_ids: [],
  can_manage_agents: true, can_manage_ai_execution: true, ai_tool_execution_enabled: false,
  ai_file_actions_direct_enabled: false, ai_tool_execution_revision: 0, notifications_muted: false,
  last_message_at: null, created_at: "2026-10-19T12:00:00Z",
};

async function prepare(page: Page, opts: { joinStatus?: number; role?: string; loadFailure?: boolean; verification?: boolean } = {}) {
  let token: string | null = null;
  let loads = 0;
  const joins: string[] = [];
  const registrations: Record<string, unknown>[] = [];
  const errors: string[] = [];
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  page.on("pageerror", (error) => { errors.push(error.message); console.error(error.message); });
  await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
  await page.route("https://fonts.gstatic.com/**", (route) => route.abort());
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.includes("/approval-settings/")) return route.fulfill({ json: {
      scope: { kind: "room", id: "group" }, policy: { mode: null, rules: [] },
      effective_policy: { mode: "risk_based", rules: [] }, inheritance: "following",
      root_scope: { kind: "account", id: "entity" },
    } });
    if (path.endsWith("/public-invite")) {
      if (request.method() === "GET" && opts.loadFailure && loads++ === 0) return route.fulfill({ status: 500, json: { detail: "Load failed" } });
      if (request.method() === "DELETE") { token = null; return route.fulfill({ status: 204 }); }
      if (request.method() === "PUT") token = request.postDataJSON().reset ? "synthetic-reset-public-invitation-token-123" : initialToken;
      return route.fulfill({ json: { token } });
    }
    if (path.endsWith("/public-invites/join")) {
      joins.push(request.postDataJSON().token);
      return opts.joinStatus ? route.fulfill({ status: opts.joinStatus, json: { detail: "Unavailable" } }) : route.fulfill({ json: room });
    }
    if (path.endsWith("/signup-config")) return route.fulfill({ json: { invitation_code_required: true } });
    if (path.endsWith("/register")) {
      registrations.push(request.postDataJSON());
      if (opts.verification) return route.fulfill({ json: { requires_verification: true, email: "guest@example.com" } });
    }
    if (["/login", "/register", "/verify-email"].some((suffix) => path.endsWith(suffix))) {
      return route.fulfill({ json: { access_token: `test.${Buffer.from(JSON.stringify({ sub: "guest", exp: 4102444800 })).toString("base64url")}.synthetic` } });
    }
    if (path.endsWith("/me")) return route.fulfill({ json: { id: "guest", entity_id: "guest-entity", role: "owner", email: "guest@example.com", locale: "en" } });
    if (path.endsWith("/refresh")) return route.fulfill({ status: 401, json: { detail: "Not signed in" } });
    if (path.endsWith("/rooms/group")) return route.fulfill({ json: { ...room, members: [{ ...room.members[0], role: opts.role || "owner" }] } });
    if (path.endsWith("/presence")) return route.fulfill({ json: { online_user_ids: [] } });
    if (path.endsWith("/email-accounts")) return route.fulfill({ json: { accounts: [], default_sender_id: null } });
    return route.fulfill({ json: [] });
  });
  return { joins, registrations, errors };
}

for (const width of [1440, 390]) {
  test(`manage public links at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const { errors } = await prepare(page);
    await page.goto("/e2e/fixtures/group-public-invite.html");
    await page.getByRole("button", { name: "Invite someone", exact: true }).click();
    await page.getByRole("tab", { name: "Public link", exact: true }).click();
    await expect(page.getByText("Public invitations are disabled.")).toBeVisible();
    const create = page.getByRole("button", { name: "Create public link" });
    await create.focus();
    await expect(create).toBeFocused();
    await page.keyboard.press("Enter");
    const link = page.getByRole("textbox", { name: "Public invitation link" });
    await expect(link).toHaveValue(new RegExp(`join=${initialToken}$`));
    await page.getByRole("button", { name: "Copy link", exact: true }).click();
    await expect(page.getByRole("button", { name: "Copied", exact: true })).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(`?join=${initialToken}`);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("public-link.png") });
    await page.getByRole("button", { name: "Reset link", exact: true }).click();
    await expect(link).toHaveValue(/join=synthetic-reset/);
    await page.getByRole("button", { name: "Disable link", exact: true }).click();
    await expect(page.getByText("Public invitations are disabled.")).toBeVisible();
    expect(errors).toEqual([]);
  });
}

test("regular members have no public-link management control", async ({ page }) => {
  await prepare(page, { role: "member" });
  await page.goto("/e2e/fixtures/group-public-invite.html");
  await page.getByRole("button", { name: "Invite someone", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Email", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Public link", exact: true })).toHaveCount(0);
});

test("public link loading error can be retried", async ({ page }) => {
  await prepare(page, { loadFailure: true });
  await page.goto("/e2e/fixtures/group-public-invite.html");
  await page.getByRole("button", { name: "Invite someone", exact: true }).click();
  await page.getByRole("tab", { name: "Public link", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("Could not load the invitation link.");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("button", { name: "Create public link" })).toBeVisible();
});

test("signed-in visitors join automatically", async ({ page }) => {
  const { joins } = await prepare(page);
  await page.goto("/e2e/fixtures/group-public-invite.html?mode=join");
  await expect(page.getByRole("status")).toHaveText("Joined Open study group");
  expect(joins).toEqual([initialToken]);
});

test("invalid links fail clearly", async ({ page }) => {
  const { joins } = await prepare(page, { joinStatus: 404 });
  await page.goto("/e2e/fixtures/group-public-invite.html?mode=join");
  await expect(page.getByRole("alert")).toContainText("Ask the group owner for a new link");
  expect(joins).toEqual([initialToken]);
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
});

for (const flow of ["login", "register", "verify-register"]) {
  test(`public link returns through ${flow} and joins without approval`, async ({ page }) => {
    const { joins, registrations, errors } = await prepare(page, { verification: flow === "verify-register" });
    await page.goto("/e2e/fixtures/group-public-invite.html?mode=auth");
    await expect(page.getByText(/Sign in or register to join the group automatically/)).toBeVisible();
    if (flow !== "login") await page.getByRole("button", { name: "Create Account", exact: true }).click();
    await page.locator('input[type="email"]').fill("guest@example.com");
    await page.locator('input[type="password"]').fill("test-password-123");
    await page.locator('button[type="submit"]').click();
    if (flow === "verify-register") {
      await expect(page.getByRole("heading", { name: "Verify Email" })).toBeVisible();
      expect(joins).toEqual([]);
      await page.locator('input[autocomplete="one-time-code"]').fill("AB12CD34");
      await page.locator('button[type="submit"]').click();
    }
    await expect(page.getByRole("status")).toHaveText("Joined Open study group");
    expect(joins).toEqual([initialToken]);
    if (flow !== "login") expect(registrations[0].chat_invite_token).toBe(initialToken);
    expect(errors).toEqual([]);
  });
}
