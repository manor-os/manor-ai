import { readFileSync } from "node:fs";
import { expect, test, type Page, type Request, type TestInfo } from "@playwright/test";

// Real browser + Manor HTTP/credentials/approvals + PMS HTTP/MySQL.
// Run only against the isolated loopback stack described in the E2E README.
const enabled = process.env.PMS_LIVE_E2E === "1";
test.skip(!enabled, "Requires the isolated Manor/PMS browser stack");
test.setTimeout(180_000);
const fixtureDir = process.env.PMS_E2E_FIXTURE_DIR || "/private/tmp/manor-pms-browser-e2e";
const readFixture = (name: string) => JSON.parse(readFileSync(`${fixtureDir}/${name}`, "utf8"));

async function signIn(page: Page) {
  const credentials = readFixture("manor-credentials.json");
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    localStorage.setItem("manor_tour_completed", "true");
  });
  await page.goto("/login");
  expect(new URL(page.url()).hostname).toMatch(/^(127\.0\.0\.1|localhost)$/);
  await page.locator('input[type="email"]').fill(credentials.email);
  await page.locator('input[type="password"]').fill(credentials.password);
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login(?:[/?#]|$)/);
}

async function catalog(page: Page) {
  const token = await page.evaluate(() => localStorage.getItem("manor_token"));
  const response = await page.request.get("/api/v1/integrations/mcp-servers", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok()).toBeTruthy();
  const servers = await response.json();
  return servers.find((server: any) => server.server_key === "manor_pms");
}

async function authorizedRequest(page: Page, path: string, data?: unknown) {
  const token = await page.evaluate(() => localStorage.getItem("manor_token"));
  const headers = { Authorization: `Bearer ${token}` };
  const response = data === undefined
    ? await page.request.get(path, { headers })
    : await page.request.put(path, { headers, data });
  expect(response.ok(), `${path}: ${response.status()}`).toBeTruthy();
  return response.json();
}

async function openPms(page: Page) {
  await page.goto("/integrations?provider=manor_pms");
  const detail = page.getByRole("dialog", { name: "Manor PMS", exact: true });
  await expect(detail).toBeVisible();
  return detail;
}

async function screenshot(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true, animations: "disabled", mask: [page.locator('input[type="password"]')], maskColor: "#d6d3d1" });
  await info.attach(name, { path, contentType: "image/png" });
}

async function replayApproval(page: Page, original: Request, terminalState: "consumed" | "rejected") {
  const fields = Object.fromEntries(
    [...(original.postData() || "").matchAll(/name="([^"]+)"\r\n\r\n([^\r]*)/g)]
      .map(match => [match[1], match[2]]),
  );
  const decision = JSON.parse(fields.message);
  expect(decision.hitl_id).toBeTruthy();
  expect(fields.conversation_id).toBeTruthy();
  const token = await page.evaluate(() => localStorage.getItem("manor_token"));
  const response = await page.request.post("/api/v1/chat/stream", {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      conversation_id: fields.conversation_id,
      message: JSON.stringify({ hitl_id: decision.hitl_id, action: "approve" }),
    },
  });
  expect(response.ok()).toBe(true);
  expect(await response.text()).toContain(`already ${terminalState}`);
}

function waitForDecision(page: Page) {
  return page.waitForRequest(request => request.url().endsWith("/api/v1/chat/stream")
    && request.method() === "POST" && (request.postData() || "").includes('"hitl_id"'));
}

test("live PMS connection persists, verifies, rejects expired credentials, and reconnects", async ({ page }, info) => {
  const pms = readFixture("credentials.json");
  const accountName = `PMS browser verification ${Date.now()}`;
  const browserErrors: string[] = [];
  page.on("pageerror", error => browserErrors.push(error.message));
  await signIn(page);
  // Retrying this disposable test should not leave stale connection rows in
  // screenshots or change account selection. Only our named fixtures qualify.
  const token = await page.evaluate(() => localStorage.getItem("manor_token"));
  for (const previous of (await catalog(page)).entity_accounts) {
    if (!previous.name?.startsWith("PMS browser verification ") || previous.ownership !== "mine" || !previous.can_manage) continue;
    const removed = await page.request.delete(`/api/v1/integrations/${previous.id}`, { headers: { Authorization: `Bearer ${token}` } });
    expect(removed.ok()).toBe(true);
  }
  let detail = await openPms(page);
  await detail.getByRole("button", { name: /^(Connect|\+ Add account)$/ }).click();
  const create = page.getByRole("dialog", { name: "Add Manor PMS account", exact: true });
  await expect(create.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
  await create.getByLabel("Account name (optional)").fill(accountName);
  await create.getByLabel("Access Token").fill(pms.access_token);
  await expect(create.getByLabel("PMS endpoint (optional)")).toHaveValue("");
  await create.getByRole("button", { name: "Save", exact: true }).click();
  await expect(create).not.toBeVisible();
  await expect.poll(async () => (await catalog(page)).entity_accounts.some((account: any) => account.name === accountName)).toBe(true);
  let server = await catalog(page);
  const account = server.entity_accounts.find((entry: any) => entry.name === accountName);
  expect(account.id).toBeTruthy();

  detail = await openPms(page);
  // Each owned account row has its own menu; scope it by the account label.
  const menu = () => detail.getByText(accountName, { exact: true }).locator("..", {}).getByRole("button", { name: "More actions", exact: true });
  await menu().click();
  await page.getByText("Test connection", { exact: true }).click();
  await expect.poll(async () => (await catalog(page)).entity_accounts.find((entry: any) => entry.id === account.id).health?.ok).toBe(true);
  await screenshot(page, info, "pms-management-verified");

  await menu().click();
  await page.getByText("Edit", { exact: true }).click();
  let edit = page.getByRole("dialog", { name: "Edit Manor PMS account", exact: true });
  await expect(edit.getByLabel("Access Token")).toHaveValue("");
  await edit.getByLabel("Access Token").fill(pms.expired_access_token);
  await edit.getByRole("button", { name: "Save", exact: true }).click();
  await expect(edit).not.toBeVisible();
  detail = await openPms(page);
  await menu().click();
  await page.getByText("Test connection", { exact: true }).click();
  await expect.poll(async () => (await catalog(page)).entity_accounts.find((entry: any) => entry.id === account.id).health?.ok).toBe(false);
  await expect(detail.getByText("PMS rejected these management credentials.", { exact: true })).toBeVisible();
  await screenshot(page, info, "pms-expired-token-rejected");

  await menu().click();
  await page.getByText("Edit", { exact: true }).click();
  edit = page.getByRole("dialog", { name: "Edit Manor PMS account", exact: true });
  await edit.getByLabel("Access Token").fill(pms.access_token);
  await edit.getByRole("button", { name: "Save", exact: true }).click();
  await expect(edit).not.toBeVisible();
  detail = await openPms(page);
  await menu().click();
  await page.getByText("Test connection", { exact: true }).click();
  await expect.poll(async () => (await catalog(page)).entity_accounts.find((entry: any) => entry.id === account.id).health?.ok).toBe(true);
  server = await catalog(page);
  expect(server.agent_can_use).toBe(true);
  await page.reload();
  await expect(page.getByRole("dialog", { name: "Manor PMS", exact: true }).getByText(accountName, { exact: true })).toBeVisible();
  await screenshot(page, info, "pms-reconnected-after-reload");
  expect(browserErrors).toEqual([]);
  await info.attach("connection-evidence", {
    body: JSON.stringify({ account_id: account.id, provider: "manor_pms", verified: true, expired_rejected: true, persisted_after_reload: true }),
    contentType: "application/json",
  });
});

test("ordinary Chat reads real PMS data and writes only after browser approval", async ({ page }, info) => {
  const pms = readFixture("credentials.json");
  const title = `PMS browser maintenance ${Date.now()}`;
  await signIn(page);
  const server = await catalog(page);
  const account = server.entity_accounts.find((entry: any) => entry.health?.ok === true && entry.name?.startsWith("PMS browser verification"));
  expect(account, "Run the live connection journey first").toBeTruthy();
  // Set a real user approval preference, then exercise the normal Chat action.
  await authorizedRequest(page, "/api/v1/admin/preferences", {
    runtime_approval_policy: { global: { actions: { "manor_pms.create_maintenance_request": "approval" } } },
  });
  const preferences = await authorizedRequest(page, "/api/v1/admin/preferences");
  expect(preferences.runtime_approval_policy.global.actions["manor_pms.create_maintenance_request"]).toBe("approval");
  await page.goto("/chat");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  const readResult = page.locator("main p").filter({ hasText: "PMS_E2E_READ_RESULT" });
  const writeResult = page.locator("main p").filter({ hasText: "PMS_E2E_WRITE_RESULT" }).filter({ hasText: title });
  await expect(readResult).toHaveCount(0);
  const composer = page.locator('.chat-composer-rich-editor[role="textbox"]');
  await expect(composer).toBeVisible();
  const readResponsePromise = page.waitForResponse(response => response.url().endsWith("/api/v1/chat/stream")
    && (response.request().postData() || "").includes("PMS_E2E_READ"));
  await composer.fill(`PMS_E2E_READ ${JSON.stringify({ limit: 10, integration_account_id: account.id })}`);
  await composer.press("Enter");
  const readResponse = await readResponsePromise;
  expect(readResponse.ok()).toBe(true);
  expect(await readResponse.text()).toContain(pms.property_name);
  await expect(readResult.last()).toBeVisible({ timeout: 60_000 });
  await expect(readResult.last()).toContainText(pms.property_name);
  await expect(page.locator("main")).not.toContainText(pms.other_entity.property_name);
  await screenshot(page, info, "chat-pms-real-read");

  const pmsRoot = pms.api_url.replace(/\/api\/mcp\/?$/, "");
  expect(new URL(pmsRoot).hostname).toBe("127.0.0.1");
  const maintenance = async () => {
    const response = await page.request.get(`${pmsRoot}/api/maintenance`, { headers: { Authorization: `Bearer ${pms.access_token}` } });
    expect(response.ok()).toBeTruthy();
    return response.json();
  };
  expect(JSON.stringify(await maintenance())).not.toContain(title);
  await composer.fill(`PMS_E2E_WRITE ${JSON.stringify({
    property_id: pms.property_id, title, description: "Browser end-to-end verification", category: "plumbing", priority: "medium", integration_account_id: account.id,
  })}`);
  await composer.press("Enter");
  const approve = page.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("main").getByText("Manor PMS · Create maintenance request", { exact: true }).first()).toBeVisible();
  expect(JSON.stringify(await maintenance()), "No PMS write before approval").not.toContain(title);
  await screenshot(page, info, "chat-pms-write-awaiting-approval");
  const approvalRequestPromise = waitForDecision(page);
  await approve.click();
  const approvalRequest = await approvalRequestPromise;
  await expect.poll(async () => JSON.stringify(await maintenance()).includes(title), { timeout: 60_000 }).toBe(true);
  expect((await maintenance()).filter((record: any) => record.title === title)).toHaveLength(1);
  await expect(writeResult).toBeVisible({ timeout: 60_000 });
  await expect(writeResult).toContainText(title);
  await expect(approve).not.toBeVisible();
  await screenshot(page, info, "chat-pms-approved-write");
  const response = await page.request.get(`${pmsRoot}/api/maintenance`, { headers: { Authorization: `Bearer ${pms.other_entity.access_token}` } });
  expect(response.ok()).toBeTruthy();
  expect(JSON.stringify(await response.json()), "Other PMS entity cannot see the created request").not.toContain(title);
  await page.reload();
  await expect(writeResult).toBeVisible();
  expect((await maintenance()).filter((record: any) => record.title === title)).toHaveLength(1);
  await screenshot(page, info, "chat-pms-history-after-reload");
  await replayApproval(page, approvalRequest, "consumed");
  expect((await maintenance()).filter((record: any) => record.title === title)).toHaveLength(1);

  const rejectedTitle = `${title} rejected`;
  await composer.fill(`PMS_E2E_WRITE ${JSON.stringify({
    property_id: pms.property_id, title: rejectedTitle, description: "Browser rejection verification", category: "plumbing", priority: "medium", integration_account_id: account.id,
  })}`);
  await composer.press("Enter");
  const reject = page.getByRole("button", { name: "Reject", exact: true });
  await expect(reject).toBeVisible({ timeout: 60_000 });
  expect((await maintenance()).filter((record: any) => record.title === rejectedTitle)).toHaveLength(0);
  const rejectionRequestPromise = waitForDecision(page);
  await reject.click();
  const rejectionRequest = await rejectionRequestPromise;
  const rejectionResponse = await rejectionRequest.response();
  expect(rejectionResponse?.ok()).toBe(true);
  await rejectionResponse!.finished();
  await expect(reject).not.toBeVisible();
  await page.reload();
  await expect(page.locator("main")).toContainText("Rejected");
  expect((await maintenance()).filter((record: any) => record.title === rejectedTitle)).toHaveLength(0);
  await replayApproval(page, rejectionRequest, "rejected");
  expect((await maintenance()).filter((record: any) => record.title === rejectedTitle)).toHaveLength(0);
  await screenshot(page, info, "chat-pms-rejected-write");
  await info.attach("chat-evidence", {
    body: JSON.stringify({ integration_account_id: account.id, property_id: pms.property_id, title, no_write_before_approval: true, created_after_approval: true, created_count: 1, other_entity_isolated: true, approved_replay_blocked: true, rejected_title: rejectedTitle, rejected_created_count: 0, rejected_replay_blocked: true }),
    contentType: "application/json",
  });
});

test("mobile browser can sign in and review the persisted PMS connection", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const account = (await catalog(page)).entity_accounts.find((entry: any) => entry.health?.ok === true && entry.name?.startsWith("PMS browser verification"));
  expect(account).toBeTruthy();
  const detail = await openPms(page);
  await expect(detail.getByText(account.name, { exact: true })).toBeVisible();
  await detail.getByText(account.name, { exact: true }).locator("..").getByRole("button", { name: "More actions", exact: true }).click();
  await page.getByText("Edit", { exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Edit Manor PMS account", exact: true });
  await expect(modal.getByLabel("Access Token")).toHaveValue("");
  await expect(modal.getByLabel("PMS endpoint (optional)")).toBeEnabled();
  await modal.getByLabel("PMS endpoint (optional)").focus();
  await page.keyboard.press("Tab");
  await expect(modal.getByLabel("Access Token")).toBeFocused();
  const bounds = await modal.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(391);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391);
  await screenshot(page, info, "mobile-pms-credential-form");
  await modal.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(modal).not.toBeVisible();
});
