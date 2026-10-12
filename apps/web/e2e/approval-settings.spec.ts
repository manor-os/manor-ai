import { expect, test, type Page } from "@playwright/test";
const catalog = [
  { action_key: "workspace.file.modify", category: "files", label: "Edit files", impact: "reversible" },
  { action_key: "communication.send", category: "communication", label: "Send messages", impact: "external" },
  { action_key: "content.publish", category: "publishing", label: "Publish content", impact: "external" },
];
const rootRule = { id: "account-ban", effect: "deny", selector: { kind: "category", category: "publishing" }, source_kind: "user" };
function snapshot(kind = "account", version = "saved", mode: string | null = kind === "account" ? "risk_based" : null, rules: any[] = kind === "account" ? [rootRule] : []) {
  const scope = { kind, id: kind === "account" ? "approval-account" : kind === "personal" ? "approval-user" : "approval-workspace", entity_id: "approval-account" };
  return { scope, revision: 1, version, policy: { mode, rules }, effective_policy: { mode: mode || "risk_based", rules }, root_scope: kind === "account" ? null : { kind: "account", id: "approval-account", entity_id: "approval-account" }, policy_versions: [], inheritance: kind === "account" ? "root" : mode || rules.length ? "custom" : "following", capabilities: { can_view: true, can_edit_root: true, can_edit_local: true, can_save_rule: true }, action_catalog: catalog, effective_rows: [], saved_rules: rules, inherited_rules: kind === "account" ? [] : [{ ...rootRule, scope: { kind: "account", id: "approval-account" }, editable: false }], rule_sources: [] };
}
async function mock(page: Page, options: { conflict?: boolean; readOnly?: boolean; unavailable?: boolean; saveFailure?: number; deferSave?: boolean } = {}) {
  let root = snapshot();
  let local = snapshot("workspace");
  let personal = snapshot("personal");
  const writes: any[] = [];
  const previews: any[] = [];
  let nextConflict = options.conflict;
  let nextSaveFailure = options.saveFailure;
  let finishSave = () => {};
  const pendingSave = options.deferSave ? new Promise<void>(resolve => { finishSave = resolve; }) : undefined;
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.includes("approval-settings")) return route.fulfill({ json: [] });
    if (options.unavailable) return route.fulfill({ status: 403, json: { detail: "Forbidden" } });
    if (path.endsWith("/overrides")) return route.fulfill({ json: { items: [{ scope: local.scope, label: "Publishing Studio", snapshot: local }], total: 1, limit: 50, offset: 0, following_count: 3, custom_count: 1 } });
    const isLocal = path.includes("workspaces");
    const isPersonal = path.endsWith("/personal") || path.endsWith("/personal/preview");
    let current = isLocal ? local : isPersonal ? personal : root;
    if (options.readOnly) current = { ...current, capabilities: { ...current.capabilities, can_edit_root: false, can_edit_local: false, can_save_rule: false } };
    if (path.endsWith("/preview")) {
      const data = route.request().postDataJSON(); previews.push(data);
      return route.fulfill({ json: { before: current, after: current, rules_added: 0, rules_removed: 0, representative_only: true, samples: catalog.map(item => ({ ...item, before: { outcome: "ask" }, after: { outcome: "allow" } })) } });
    }
    if (route.request().method() === "PUT") {
      const data = route.request().postDataJSON(); writes.push(data);
      await pendingSave;
      if (nextSaveFailure) { const status = nextSaveFailure; nextSaveFailure = undefined; return route.fulfill({ status, json: { detail: "Temporary save failure" } }); }
      if (nextConflict) { nextConflict = false; root = snapshot("account", "updated-elsewhere", "confirm_changes"); return route.fulfill({ status: 409, json: { detail: { code: "approval_policy_conflict", current: root } } }); }
      current = snapshot(isLocal ? "workspace" : isPersonal ? "personal" : "account", `saved-${writes.length}`, data.draft.mode, data.draft.user_rules.map((r: any, i: number) => ({ ...r, id: r.id || `rule-${i}`, source_kind: "user" })));
      if (isLocal) local = current; else if (isPersonal) personal = current; else root = current;
    }
    return route.fulfill({ json: current });
  });
  await page.goto("/e2e/fixtures/approval-settings.html");
  return { writes, previews, finishSave };
}
test.beforeEach(async ({ page }) => { await page.addInitScript(() => localStorage.setItem("manor_locale", "en")); });

test("mode selection stays a draft and saves one versioned account policy", async ({ page }, info) => {
  const { writes } = await mock(page);
  await page.getByRole("radio", { name: "Run automatically", exact: true }).click();
  expect(writes).toHaveLength(0);
  await expect(page.getByText("Unsaved changes", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByText("Changes saved", { exact: true })).toBeVisible();
  await expect(page.getByRole("radio", { name: "Run automatically", exact: true })).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
  expect(writes).toHaveLength(1); expect(writes[0]).toMatchObject({ expected_version: "saved", draft: { mode: "automatic" } });
  expect(writes[0].draft.user_rules[0].id).toBe("account-ban");
  await page.screenshot({ path: info.outputPath("approval-desktop.png"), fullPage: true });
});

for (const triggerName of ["Personal chat settings", "Workspace settings"]) {
  test(`${triggerName} closes only after saving and reopens the saved policy`, async ({ page }) => {
    const { writes, finishSave } = await mock(page, { deferSave: true });
    const trigger = page.getByRole("button", { name: triggerName, exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "Execution & approvals", exact: true });
    await dialog.getByRole("radio", { name: "Run automatically", exact: true }).click();
    const save = dialog.getByRole("button", { name: "Save changes", exact: true });
    await save.click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(dialog).toBeVisible();
    await expect(save).toBeDisabled();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("dialog", { name: "Discard unsaved changes?", exact: true })).toHaveCount(0);
    finishSave();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("dialog", { name: "Discard unsaved changes?", exact: true })).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(writes[0]).toMatchObject({ expected_version: "saved", draft: { mode: "automatic" } });
    await trigger.click();
    await expect(dialog.getByRole("radio", { name: "Run automatically", exact: true })).toHaveAttribute("aria-checked", "true");
    await expect(dialog.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
    await expect(dialog.getByText("Unsaved changes", { exact: true })).toHaveCount(0);
    expect(writes).toHaveLength(1);
  });
}

test("a failed dialog save preserves the draft and closes after a successful retry", async ({ page }) => {
  const { writes } = await mock(page, { saveFailure: 503 });
  await page.getByRole("button", { name: "Personal chat settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Execution & approvals", exact: true });
  await dialog.getByRole("radio", { name: "Run automatically", exact: true }).click();
  const save = dialog.getByRole("button", { name: "Save changes", exact: true });
  await save.click();
  await expect(dialog.getByRole("alert")).toContainText("Changes were not saved. Your draft is preserved.");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radio", { name: "Run automatically", exact: true })).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByText("Unsaved changes", { exact: true })).toBeVisible();
  await expect(save).toBeEnabled();
  expect(writes).toHaveLength(1);
  await save.click();
  await expect(dialog).toHaveCount(0);
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
});

test("409 keeps the draft and requires an explicit fresh preview before retry", async ({ page }) => {
  const { writes, previews } = await mock(page, { conflict: true });
  await page.getByRole("radio", { name: "Run automatically", exact: true }).click();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("radio", { name: "Run automatically", exact: true })).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Preview against latest settings", exact: true }).click();
  await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByText("Changes saved", { exact: true })).toBeVisible();
  expect(writes[1].expected_version).toBe("updated-elsewhere");
  expect(previews[0].draft.mode).toBe("automatic");
});

test("local drafts intercept Escape and retain independent follow/reset semantics", async ({ page }) => {
  await mock(page);
  await page.getByRole("button", { name: "Workspace settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Execution & approvals", exact: true });
  await dialog.getByRole("radio", { name: "Run automatically", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Discard unsaved changes?", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Keep editing", exact: true }).click();
  await dialog.getByText("Advanced", { exact: false }).click();
  await expect(dialog.getByText("Account / team restriction", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Add a rule", exact: true }).click();
  await dialog.getByRole("radio", { name: "Follow account default", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Remove rule", exact: true })).toHaveCount(1);
  await dialog.getByRole("button", { name: "Restore all defaults", exact: true }).click();
  await page.getByRole("dialog", { name: "Restore all defaults", exact: true }).getByRole("button", { name: "Restore all defaults", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Remove rule", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("permission loss and mobile layout remain usable", async ({ page }, info) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await mock(page, { readOnly: true });
  await expect(page.getByRole("radio", { name: "Run automatically", exact: true })).toBeDisabled();
  await expect(page.getByText("You can view these settings.", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("approval-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "Workspace settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Execution & approvals", exact: true });
  await expect(dialog).toBeVisible();
  expect((await dialog.boundingBox())!.width).toBeLessThanOrEqual(328);
  await page.screenshot({ path: info.outputPath("approval-mobile-dialog.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

async function commandMock(page: Page, firstStatus: number) {
  await mock(page);
  const writes: any[] = [];
  let version = "policy-before";
  await page.route(/\/api\/v1\/approval-commands(?:\/|$)/, async route => {
    if (route.request().method() === "POST") {
      writes.push(route.request().postDataJSON());
      if (writes.length === 1) { version = "policy-latest"; return route.fulfill({ status: firstStatus, json: { detail: { message: "Changed elsewhere" } } }); }
      return route.fulfill({ json: { request_ref: { kind: "hitl", id: "approval-request" }, status: "granted", digest: "request-digest", changed_scope: { kind: "workspace", id: "approval-workspace", entity_id: "approval-account" }, policy: snapshot("workspace"), idempotent: true } });
    }
    return route.fulfill({ json: { request_ref: { kind: "hitl", id: "approval-request" }, status: "pending", digest: "request-digest", available_decisions: ["approve_once", "reject", "approve_and_save_rule"], scope: { kind: "workspace", id: "approval-workspace", entity_id: "approval-account" }, policy_version: version, rule_options: [{ id: "server-offered-option", action_key: "communication.send", coverage: "all_targets_for_action", rule: { effect: "allow", selector: { kind: "action", action_key: "communication.send" } } }] } });
  });
  await page.reload();
  return writes;
}

test("combined approval is one command and a conflict requires a new preview", async ({ page }, info) => {
  const writes = await commandMock(page, 409);
  await page.getByRole("button", { name: "Approve once & save rule", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Approve once & save rule", exact: true });
  await expect(dialog.getByText("All targets for this action in this scope", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Applies to: This Workspace · Publishing Studio", { exact: true })).toBeVisible();
  expect(writes).toHaveLength(0);
  await dialog.getByRole("button", { name: "Approve once & save rule", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Approve once & save rule", exact: true })).toBeDisabled();
  expect(writes).toHaveLength(1);
  await dialog.getByRole("button", { name: "Review latest details", exact: true }).click();
  await page.setViewportSize({ width: 360, height: 800 });
  await page.screenshot({ path: info.outputPath("approval-save-rule-mobile.png"), animations: "disabled" });
  expect((await dialog.boundingBox())!.width).toBeLessThanOrEqual(328);
  await dialog.getByRole("button", { name: "Approve once & save rule", exact: true }).click();
  await expect(page.getByText("Approval and rule saved", { exact: true })).toBeVisible();
  expect(writes[1]).toMatchObject({ choice: "approve_and_save_rule", expectedPolicyVersion: "policy-latest", expectedDigest: "request-digest", optionId: "server-offered-option" });
  expect(writes[1].idempotencyKey).not.toBe(writes[0].idempotencyKey);
});

test("an uncertain command retry keeps the same idempotency key", async ({ page }) => {
  const writes = await commandMock(page, 503);
  await page.getByRole("button", { name: "Approve once & save rule", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Approve once & save rule", exact: true });
  const submit = dialog.getByRole("button", { name: "Approve once & save rule", exact: true });
  await submit.click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await submit.click();
  await expect(page.getByText("Approval and rule saved", { exact: true })).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
});
