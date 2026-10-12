import { expect, test, type Page } from "@playwright/test";
import { PDFDocument } from "pdf-lib";

const accounts = [
  { id: "smtp", email: "first@example.com", provider: "email", is_default: false },
  { id: "oauth:outlook", email: "second@example.com", provider: "outlook", is_default: true },
];
const room = {
  id: "group", kind: "group", title: "Project group", owner_scope: "personal", member_count: 1,
  members: [{ user_id: "inviter", name: "Inviter", role: "owner" }], agents: [], online_user_ids: [],
  can_manage_agents: true, can_manage_ai_execution: true, ai_tool_execution_enabled: false,
  ai_file_actions_direct_enabled: false, ai_tool_execution_revision: 0, notifications_muted: false,
  last_message_at: null, created_at: "2026-10-02T12:00:00Z",
};

async function prepare(page: Page, senders = accounts, emailSent = true, failFirstLoad = false) {
  const writes: Array<Record<string, unknown>> = [];
  const errors: string[] = [];
  let loads = 0;
  let defaultId = senders.find((account) => account.is_default)?.id || senders[0]?.id || null;
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/set-default")) {
      defaultId = path.includes("/connections/") ? `oauth:${path.split("/").at(-2)}` : path.split("/").at(-2)!;
      writes.push({ sender_id: defaultId });
      return route.fulfill({ status: 204 });
    }
    if (path.endsWith("/mcp-servers")) {
      return route.fulfill({ json: ["email", "outlook"].map((provider) => {
        const owned = senders.filter((account) => account.provider === provider).map((account) => ({
          id: account.id.replace(/^oauth:/, ""), name: account.email, display_name: account.email,
          // Each provider has a runtime default; only one mailbox is the common sending default.
          is_default: true, ownership: "mine", owner_display_name: null, can_share: false, can_manage: true,
          runtime_callable: true, availability: "callable", health: null, status: "active", nango_backed: false,
          provider_user_id: account.email, expires_at: null, created_at: null, connected_at: null,
          whatsapp_readiness_code: null, kind: provider === "email" ? "integration" : "oauth_account",
        }));
        return {
          server_key: provider, server_kind: "managed", category: "email",
          name: provider === "email" ? "Email (IMAP + SMTP)" : "Outlook Mail", description: "Read and send email.",
          auth_type: provider === "email" ? "credential" : "oauth", tagline: "Read and send email.",
          supports_multi_account: true, connections: provider === "email" ? [] : owned,
          entity_accounts: provider === "email" ? owned : [], user_connected: provider !== "email",
          entity_connected: provider === "email", agent_can_use: true, required_permission: null,
          user_has_required_permission: true, requires_explicit_account: false, oauth_configured: true,
        };
      }) });
    }
    if (path.endsWith("/email-accounts/default")) {
      defaultId = request.postDataJSON().sender_id;
      writes.push(request.postDataJSON());
      return route.fulfill({ status: 204 });
    }
    if (path.endsWith("/email-accounts")) {
      loads += 1;
      if (failFirstLoad && loads === 1) return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
      return route.fulfill({ json: { accounts: senders.map((account) => ({ ...account, is_default: account.id === defaultId })), default_sender_id: defaultId } });
    }
    if (request.method() === "POST" && (path.endsWith("/rooms") || path.endsWith("/invites"))) {
      const body = request.postDataJSON();
      writes.push(body);
      const email = body.recipient_email || body.email;
      return route.fulfill({ json: path.endsWith("/rooms") ? {
        ...room, invite_token: "first-token", recipient_outcome: "invited", invite_next_step: "accept", invite_email_sent: emailSent,
      } : { outcome: "invited", invite_token: "next-token", email, invite_next_step: "accept", email_sent: emailSent, expires_at: "2026-10-09T12:00:00Z" } });
    }
    if (path.endsWith("/rooms/group")) return route.fulfill({ json: room });
    if (path.endsWith("/presence")) return route.fulfill({ json: { online_user_ids: [] } });
    return route.fulfill({ json: [] });
  });
  return { writes, errors };
}

async function newGroup(page: Page) {
  await page.goto("/e2e/fixtures/group-invite.html");
  await page.getByRole("textbox", { name: "To:" }).fill("recipient@example.com");
  await page.getByRole("textbox", { name: "To:" }).press("Enter");
  await page.getByRole("textbox", { name: "Group name" }).fill("Project group");
}

for (const width of [1440, 390]) {
  test(`new group uses the default mailbox for every recipient at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const { writes, errors } = await prepare(page);
    await newGroup(page);
    await page.getByRole("textbox", { name: "To:" }).fill("second-recipient@example.com");
    await page.getByRole("textbox", { name: "To:" }).press("Enter");
    const submit = page.getByRole("button", { name: "Create group", exact: true });
    await expect(page.getByText("second@example.com · Outlook", { exact: true })).toBeVisible();
    await expect(page.getByRole("combobox")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Manage email accounts" })).toHaveAttribute("href", "/integrations?provider=email");
    await expect(submit).toBeEnabled();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("default-sender.png") });
    await submit.click();
    await expect(page.getByText("Email and in-app invitations sent; awaiting their acceptance.", { exact: false })).toHaveCount(2);
    expect(writes).toEqual([
      { kind: "group", recipient_email: "recipient@example.com", title: "Project group", owner_scope: "personal", sender_integration_id: "oauth:outlook" },
      { email: "second-recipient@example.com", sender_integration_id: "oauth:outlook" },
    ]);
    expect(errors).toEqual([]);
  });

  test(`existing group shows failed email separately from its in-app invitation at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const { writes, errors } = await prepare(page, [accounts[0]], false);
    await page.goto("/e2e/fixtures/group-invite.html?mode=existing");
    await page.getByRole("button", { name: "Invite someone", exact: true }).click();
    await page.getByRole("textbox", { name: "Email", exact: true }).fill("recipient@example.com");
    await expect(page.getByText("first@example.com · SMTP", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Send invitation", exact: true }).click();
    await expect(page.getByText(/In-app invitation sent, but email was not sent/)).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Invitation link" })).toHaveValue(/next-token$/);
    expect(writes).toEqual([{ email: "recipient@example.com", sender_integration_id: "smtp" }]);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("email-failure.png") });
    expect(errors).toEqual([]);
  });
}

for (const width of [1440, 390]) {
  test(`eSign uses the common default without a mailbox selector at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const pdf = await PDFDocument.create();
    pdf.addPage([612, 792]);
    const body = Buffer.from(await pdf.save());
    const writes: Array<Record<string, unknown>> = [];
    await page.route("**/api/v1/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/signature-senders")) return route.fulfill({ json: accounts });
      if (path.endsWith("/signature-requests")) {
        if (route.request().method() === "POST") {
          writes.push(route.request().postDataJSON());
          return route.fulfill({ status: 201, json: { id: "signature", status: "pending", delivery_status: "sent", url: "https://example.test/sign/synthetic" } });
        }
        return route.fulfill({ json: [] });
      }
      if (path.endsWith("/download")) return route.fulfill({ contentType: "application/pdf", body });
      return route.fulfill({ json: {} });
    });
    await page.goto("/e2e/fixtures/pdf-signature-dialog.html");
    const dialog = page.getByRole("dialog", { name: "Send for signature", exact: true });
    await dialog.getByRole("button", { name: "Add Signature field", exact: true }).click();
    await dialog.getByRole("button", { name: "Place in page center", exact: true }).click();
    await dialog.getByRole("button", { name: "Preview and continue", exact: true }).click();
    await expect(dialog.getByText("second@example.com · Outlook", { exact: true })).toBeVisible();
    await expect(dialog.getByRole("combobox")).toHaveCount(0);
    await expect(dialog.getByRole("link", { name: "Manage email accounts" })).toHaveAttribute("href", "/integrations?provider=email");
    await dialog.getByLabel("Recipient name", { exact: true }).fill("Recipient");
    await dialog.getByLabel("Recipient emails", { exact: true }).fill("recipient@example.test");
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("signature-default-sender.png") });
    await dialog.getByRole("button", { name: "Send for signature", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].sender_integration_id).toBe("oauth:outlook");
  });
}

test("missing mailbox still creates an invitation link", async ({ page }) => {
  const { writes } = await prepare(page, [], false);
  await newGroup(page);
  await expect(page.getByText(/No sending account is connected/)).toBeVisible();
  await page.getByRole("button", { name: "Create group", exact: true }).click();
  await expect(page.getByText(/In-app invitation sent, but email was not sent/)).toBeVisible();
  expect(writes[0]).not.toHaveProperty("sender_integration_id");
});

test("account loading failure disables sending and can be retried", async ({ page }) => {
  await prepare(page, [accounts[0]], true, true);
  await newGroup(page);
  await expect(page.getByRole("alert")).toContainText("Sending accounts could not be loaded");
  await expect(page.getByRole("button", { name: "Create group", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByText("first@example.com · SMTP", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Create group", exact: true })).toBeEnabled();
});

for (const width of [1440, 390]) {
  test(`original integration account menus change one default across providers at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const { writes, errors } = await prepare(page);
    await page.goto("/e2e/fixtures/group-invite.html?mode=email");
    await expect(page.getByRole("region", { name: "Default sending account", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: /Email \(IMAP \+ SMTP\)/ }).click();
    const email = page.getByRole("dialog", { name: "Email (IMAP + SMTP)", exact: true });
    const smtpRow = email.getByText("first@example.com", { exact: true }).locator("..");
    await expect(smtpRow.getByText("Default", { exact: true })).toHaveCount(0);
    await smtpRow.getByRole("button", { name: "More actions", exact: true }).click();
    await page.getByRole("menuitem", { name: "Set as default", exact: true }).click();
    await expect(smtpRow.getByText("Default", { exact: true })).toBeVisible();
    expect(writes).toEqual([{ sender_id: "smtp" }]);
    await email.getByRole("button", { name: "Close", exact: true }).click();
    await page.getByRole("button", { name: /Outlook Mail/ }).click();
    const outlook = page.getByRole("dialog", { name: "Outlook Mail", exact: true });
    const outlookRow = outlook.getByText("second@example.com", { exact: true }).locator("..");
    await expect(outlookRow.getByText("Default", { exact: true })).toHaveCount(0);
    await outlookRow.getByRole("button", { name: "More actions", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: "Set as default", exact: true })).toBeEnabled();
    await page.getByRole("menuitem", { name: "Set as default", exact: true }).click();
    await expect(outlookRow.getByText("Default", { exact: true })).toBeVisible();
    expect(writes).toEqual([{ sender_id: "smtp" }, { sender_id: "oauth:outlook" }]);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("integration-email-default.png") });
    expect(errors).toEqual([]);
  });
}
