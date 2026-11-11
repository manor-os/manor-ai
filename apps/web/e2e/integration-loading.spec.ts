import { expect, test, type Page, type Route } from "@playwright/test";
import type { IntegrationMCPServer } from "../src/lib/api";

function provider(server_key: string, name: string): IntegrationMCPServer {
  return {
    server_key, name, server_kind: "managed", category: "Email", description: `Use ${name}`,
    auth_type: "oauth2", scopes: null, tagline: `Connect ${name}`, docs_url: null,
    setup_hint: null, color_hex: null, supports_multi_account: true,
    connections: [], entity_accounts: [], user_connected: false, user_expires_at: null,
    entity_connected: false, required_permission: null, user_has_required_permission: true,
    agent_can_use: false, requires_explicit_account: false, hint: "Connect your account",
    oauth_configured: true, setup_pending: true,
  };
}

async function prepare(page: Page, rows = [provider("gmail", "Gmail")], snapshotFails = false, role = "owner") {
  const state = { fail: false, fullReads: 0, actions: [] as string[], pending: [] as Route[] };
  await page.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/mcp-servers")) {
      if (url.searchParams.get("snapshot") === "true") return route.fulfill(
        snapshotFails ? { status: 503, json: { detail: "Fixture snapshot unavailable" } } : { json: rows },
      );
      state.fullReads++;
      if (state.fail) return route.fulfill({ status: 503, json: { detail: "Fixture unavailable" } });
      state.pending.push(route);
      return;
    }
    if (route.request().method() !== "GET") {
      state.actions.push(url.pathname);
      return route.fulfill({ status: 503, json: { detail: "Fixture stops before external authorization" } });
    }
    if (url.pathname.endsWith("/chrome/connections")) return route.fulfill({ json: { connections: [] } });
    return route.fulfill({ json: [] });
  });
  // This existing fixture mounts the real Integrations page, stores and detail UI.
  await page.goto(`/e2e/fixtures/chrome-integration.html?role=${role}`);
  await expect.poll(() => state.pending.length).toBe(1);
  return {
    state,
    async complete(completeRows = rows.map(row => ({ ...row, setup_pending: false }))) {
      await state.pending.shift()!.fulfill({ json: completeRows });
    },
    async fail() {
      state.fail = true;
      await state.pending.shift()!.fulfill({ status: 503, json: { detail: "Fixture unavailable" } });
    },
  };
}

test("cards and search work while external setup waits; an open detail updates to the correct OAuth path", async ({ page }) => {
  const { state, complete } = await prepare(page);
  const card = page.getByRole("button", { name: /^Gmail Connect Gmail/ });
  await expect(card).toBeVisible();
  const search = page.getByRole("textbox");
  await search.fill("gmail");
  await expect(card).toBeVisible();
  await card.click();
  const dialog = page.getByRole("dialog", { name: "Gmail", exact: true });
  await expect(dialog.getByRole("button", { name: "Checking setup…" })).toBeDisabled();
  expect(state.actions).toEqual([]);
  await complete([{ ...provider("gmail", "Gmail"), setup_pending: false, nango_provider_config_key: "gmail" }]);
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeEnabled();
  await dialog.getByRole("button", { name: "Connect", exact: true }).click();
  await expect.poll(() => state.actions.length).toBe(1);
  expect(state.actions[0]).toContain("/nango/");
});

test("a failed refresh retains cards and retry resolves their open details", async ({ page }) => {
  const { state, complete, fail } = await prepare(page);
  await page.getByRole("button", { name: /^Gmail Connect Gmail/ }).click();
  const dialog = page.getByRole("dialog", { name: "Gmail", exact: true });
  await fail();
  await expect(dialog.getByRole("button", { name: "Status unavailable", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("button", { name: /^Gmail Connect Gmail/ })).toBeVisible();
  // It recovers by itself and says nothing about the failed read.
  await expect(page.getByText("Could not refresh", { exact: false })).toHaveCount(0);
  state.fail = false;
  await expect.poll(() => state.pending.length, { timeout: 25_000 }).toBe(1);
  await complete();
  await page.getByRole("button", { name: /^Gmail Connect Gmail/ }).click();
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeEnabled();
});

test("Google Workspace detail updates in place when the catalog finishes", async ({ page }) => {
  const { complete } = await prepare(page, [provider("google_calendar", "Google Calendar"), provider("google_drive", "Google Drive")]);
  await page.getByRole("button", { name: /Google Workspace/ }).click();
  const dialog = page.getByRole("dialog", { name: "Google Workspace", exact: true });
  await expect(dialog.getByRole("button", { name: "Checking setup…" })).toHaveCount(2);
  await complete();
  await expect(dialog.getByRole("button", { name: "Checking setup…" })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toHaveCount(2);
  await expect(dialog.getByRole("button", { name: "Connect", exact: true }).first()).toBeEnabled();
});

test("Google Workspace never enables an unverified OAuth path after a failed initial refresh", async ({ page }) => {
  const { state, fail } = await prepare(page, [provider("google_calendar", "Google Calendar"), provider("google_drive", "Google Drive")]);
  await page.getByRole("button", { name: /Google Workspace/ }).click();
  const dialog = page.getByRole("dialog", { name: "Google Workspace", exact: true });
  await fail();
  await expect(dialog.getByRole("button", { name: "Status unavailable", exact: true })).toHaveCount(2);
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Status unavailable", exact: true })).toHaveCount(2);
  expect(state.actions).toEqual([]);
});

test("a failed snapshot still loads the full catalog", async ({ page }) => {
  const { complete } = await prepare(page, [provider("gmail", "Gmail")], true);
  await expect(page.getByRole("status", { name: "Loading integrations…" })).toBeVisible();
  await complete();
  await expect(page.getByRole("button", { name: /^Gmail Connect Gmail/ })).toBeVisible();
  await expect(page.getByText("Failed to load integrations", { exact: true })).toHaveCount(0);
});

test("failure of both initial reads can recover through retry", async ({ page }) => {
  const { state, complete, fail } = await prepare(page, [provider("gmail", "Gmail")], true);
  await fail();
  await expect(page.getByText("Failed to load integrations", { exact: true })).toBeVisible();
  state.fail = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect.poll(() => state.pending.length).toBe(1);
  await complete();
  await expect(page.getByRole("button", { name: /^Gmail Connect Gmail/ })).toBeVisible();
  await expect(page.getByText("Failed to load integrations", { exact: true })).toHaveCount(0);
});

for (const key of ["gmail", "google_calendar"]) {
  test(`${key}: account reconnect follows setup state while management stays available`, async ({ page }) => {
    const row = {
      ...provider(key, key === "gmail" ? "Gmail" : "Google Calendar"),
      nango_provider_config_key: key,
      entity_accounts: [{
        id: "saved-account", name: "Saved account", display_name: "Saved account", is_default: true,
        created_at: null, status: "active", nango_backed: true, whatsapp_readiness_code: null,
        kind: "integration" as const, ownership: "mine" as const, owner_user_id: "user-1",
        owner_display_name: null, can_manage: true, can_share: false, runtime_callable: false,
        availability: "reconnect_required" as const, health: null,
      }],
    };
    const { state, complete } = await prepare(page, [row]);
    await page.getByRole("button", { name: key === "gmail" ? /^Gmail Connect Gmail/ : /Google Workspace/ }).click();
    const dialog = page.getByRole("dialog", { name: key === "gmail" ? "Gmail" : "Google Workspace", exact: true });
    await dialog.getByRole("button", { name: "More actions", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: "Reconnect", exact: true })).toBeDisabled();
    await expect(page.getByRole("menuitem", { name: "Remove", exact: true })).toBeEnabled();
    await dialog.getByText("Saved account", { exact: true }).last().click();
    await complete([{ ...row, setup_pending: false, setup_unavailable: true }]);
    await expect(dialog.getByRole("button", { name: "Status unavailable", exact: true })).toBeDisabled();
    await dialog.getByRole("button", { name: "More actions", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: "Reconnect", exact: true })).toBeDisabled();
    await dialog.getByText("Saved account", { exact: true }).last().click();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect.poll(() => state.pending.length, { timeout: 40_000 }).toBe(1);
    await complete([{ ...row, setup_pending: false, setup_unavailable: false }]);
    await page.getByRole("button", { name: key === "gmail" ? /^Gmail Connect Gmail/ : /Google Workspace/ }).click();
    await dialog.getByRole("button", { name: "More actions", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: "Reconnect", exact: true })).toBeEnabled();
    expect(state.actions).toEqual([]);
  });
}

for (const nativeConfigured of [false, true]) {
  test(`Google Workspace uses the confirmed Nango route with native configured=${nativeConfigured}`, async ({ page }) => {
    const rows = [provider("google_calendar", "Google Calendar"), provider("google_drive", "Google Drive")]
      .map(row => ({ ...row, oauth_configured: nativeConfigured, nango_provider_config_key: row.server_key }));
    const { state, complete } = await prepare(page, rows);
    await page.getByRole("button", { name: /Google Workspace/ }).click();
    const dialog = page.getByRole("dialog", { name: "Google Workspace", exact: true });
    await complete();
    await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toHaveCount(2);
    await dialog.getByRole("button", { name: "Connect", exact: true }).first().click();
    await expect.poll(() => state.actions.length).toBe(1);
    expect(state.actions[0]).toContain("/nango/");
  });
}

test("members can see a Google group when one service has a configured Nango route", async ({ page }) => {
  const rows = [
    { ...provider("google_calendar", "Google Calendar"), oauth_configured: false, nango_provider_config_key: "google_calendar" },
    { ...provider("google_drive", "Google Drive"), oauth_configured: false },
  ];
  const { complete } = await prepare(page, rows, false, "member");
  await complete();
  await expect(page.getByRole("button", { name: /Google Workspace/ })).toBeVisible();
});
