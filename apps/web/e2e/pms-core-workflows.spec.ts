import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type BrowserContext, type Page, type TestInfo } from "@playwright/test";
import { loginPms, verifyPmsInBrowser } from "./support/pms-browser-verifier";
import { pmsReadback, readPms, verifyReadback } from "./support/pms-rest-verifier";

// Historical 44-tool core workflows. The complete expanded catalog has separate
// backend route/argument/approval contract tests; this file does not claim every
// newly exposed endpoint has been exercised in the browser.
// Explicit opt-in: real browser actions and real database writes in disposable
// loopback services. No intercepted API responses or direct MCP test calls.
const enabled = process.env.PMS_ALL_FUNCTIONS_E2E === "1";
test.skip(!enabled, "Requires the isolated PMS core-workflow browser stack");
test.describe.configure({ mode: "serial", timeout: 120_000 });
const root = process.env.PMS_E2E_FIXTURE_DIR || "/private/tmp/manor-pms-all-functions-e2e";
const manorURL = process.env.E2E_BASE || "http://127.0.0.1:18313";
const pmsURL = process.env.PMS_E2E_WEB || "http://127.0.0.1:18315";
const modelURL = "http://127.0.0.1:18314";
const contract = JSON.parse(readFileSync(new URL("../../../packages/core/ai/mcp/manor_pms_tools.json", import.meta.url), "utf8")).tools;
const toolMap = new Map<string, any>(contract.map((tool: any) => [tool.name, tool]));
const readFixture = (name: string) => JSON.parse(readFileSync(`${root}/${name}`, "utf8"));
type State = Record<string, any>;
type Case = { id: string; tool: string; args: (s: State) => State; save?: string; seed?: "property" | "reservation"; check?: (result: any, s: State) => void };
let context: BrowserContext;
let manor: Page;
let pms: Page;
let state: State = {};
let credentials: State;
let accountId: string;
const run = `${Date.now()}`;
const evidence: State[] = [];

function c(tool: string, args: Case["args"], options: Partial<Omit<Case, "tool" | "args">> = {}): Case {
  return { id: tool, tool, args, ...options };
}

const cases: Case[] = [
  c("create_owner", () => ({ first_name: "AllTools", last_name: `Owner${run}`, email: `owner-${run}@example.com`, phone: "+12025550111", company_name: `AllTools Owners ${run}` }), { save: "owner" }),
  c("get_owner", s => ({ owner_id: s.owner.id })),
  c("list_owners", () => ({ limit: 100 })),
  c("create_property", s => ({ name: `AllTools Property ${run}`, address: "100 Browser Test Street", city: "Testville", state: "CA", zip_code: "90001", property_type: "apartment", units_count: 2, owner_id: s.owner.id }), { save: "property", seed: "property" }),
  c("get_property", s => ({ property_id: s.property.id })),
  c("list_properties", () => ({ skip: 0, limit: 100 })),
  c("create_tenant", () => ({ first_name: "AllTools", last_name: `Tenant${run}`, email: `tenant-${run}@example.com`, phone: "+12025550112", emergency_contact_name: "Test Emergency", emergency_contact_phone: "+12025550113" }), { save: "tenant" }),
  c("get_tenant", s => ({ tenant_id: s.tenant.id })),
  c("list_tenants", () => ({ limit: 100 })),
  c("create_vendor", () => ({ company_name: `AllTools Vendor ${run}`, contact_name: "Test Plumber", email: `vendor-${run}@example.com`, phone: "+12025550114", category: "plumbing" }), { save: "vendor" }),
  c("get_vendor", s => ({ vendor_id: s.vendor.id })),
  c("list_vendors", () => ({ limit: 100 })),
  c("create_lease", s => ({ property_id: s.property.id, unit_id: s.ltr_unit_id, tenant_id: s.tenant.id, start_date: "2027-01-01", end_date: "2027-12-31", monthly_rent: 1625, security_deposit: 2000 }), { save: "lease" }),
  c("get_lease", s => ({ lease_id: s.lease.id })),
  c("list_leases", s => ({ property_id: s.property.id, tenant_id: s.tenant.id })),
  c("create_maintenance_request", s => ({ property_id: s.property.id, unit_id: s.ltr_unit_id, tenant_id: s.tenant.id, title: `AllTools Maintenance ${run}`, description: "Browser verified leaking tap", category: "plumbing", priority: "medium" }), { save: "maintenance" }),
  c("list_maintenance_requests", s => ({ property_id: s.property.id, status: "open" })),
  c("create_payment", s => ({ lease_id: s.lease.id, tenant_id: s.tenant.id, amount: 1625, payment_date: "2027-01-01", payment_method: "bank_transfer", payment_type: "rent", status: "completed", reference_number: `ALLTOOLS-${run}` }), { save: "payment" }),
  c("list_payments", s => ({ lease_id: s.lease.id, tenant_id: s.tenant.id, status: "completed" })),
  c("guest_create", () => ({ first_name: "AllTools", last_name: `Guest${run}`, email: `guest-${run}@example.com`, phone: "+12025550115", country: "US", language: "en" }), { save: "guest" }),
  c("guest_get", s => ({ guest_id: s.guest.id })),
  c("guest_search", () => ({ q: `guest-${run}@example.com`, limit: 20 })),
  c("guest_search", () => ({ q: `absent-${run}@example.com`, limit: 20 }), { id: "guest_search_no_match", check: result => expect(result).toEqual([]) }),
  c("reservation_create", s => ({ property_id: s.property.id, unit_id: s.unit_id, primary_guest_id: s.guest.id, check_in_date: "2027-01-10", check_out_date: "2027-01-12", adults: 2, nightly_total_cents: 24000, total_amount_cents: 26000, currency: "EUR" }), { save: "reservation", seed: "reservation" }),
  c("reservation_get", s => ({ reservation_id: s.reservation.id })),
  c("reservation_list", s => ({ property_id: s.property.id, unit_id: s.unit_id, guest_id: s.guest.id, from_date: "2027-01-01", to_date: "2027-01-31" })),
  c("pricing_set_override", s => ({ unit_id: s.unit_id, date: "2027-01-10", nightly_rate_cents: 17500, min_stay_override: 2, source: "manual", note: `AllTools initial ${run}` }), { save: "rate_override" }),
  c("pricing_set_override", s => ({ unit_id: s.unit_id, date: "2027-01-10", nightly_rate_cents: 18500, min_stay_override: 2, source: "event", note: `AllTools updated ${run}` }), { id: "pricing_set_override_update", check: (result, s) => expect(result.id).toBe(s.rate_override.id) }),
  c("pricing_quote", s => ({ unit_id: s.unit_id, check_in: "2027-01-10", check_out: "2027-01-12" })),
  c("promo_validate", s => ({ code: s.promo_code, channel: "direct", nights: 2, subtotal_cents: 30000, total_cents: 32000, guest_id: s.guest.id })),
  c("policy_cancellation_quote", s => ({ reservation_id: s.reservation.id, today: "2026-12-01" })),
  c("policy_modification_quote", s => ({ reservation_id: s.reservation.id, today: "2026-12-01" })),
  c("ancillary_list_services", () => ({})),
  c("ancillary_add_item", s => ({ reservation_id: s.reservation.id, service_id: s.ancillary_service_id, quantity: 2, scheduled_for: "2027-01-11T08:00:00Z", note: `AllTools breakfast ${run}` }), { save: "ancillary" }),
  c("cleaning_list_tasks", s => ({ unit_id: s.unit_id })),
  c("cleaning_list_late", () => ({})),
  c("cleaning_transition_task", s => ({ cleaning_task_id: s.cleaning_task_id, action: "start" }), { id: "cleaning_transition_start" }),
  c("cleaning_transition_task", s => ({ cleaning_task_id: s.cleaning_task_id, action: "complete" }), { id: "cleaning_transition_complete" }),
  c("cleaning_transition_task", s => ({ cleaning_task_id: s.cleaning_cancel_task_id, action: "cancel" }), { id: "cleaning_transition_cancel" }),
  c("damage_get_for_reservation", s => ({ reservation_id: s.reservation.id })),
  c("damage_file_claim", s => ({ deposit_id: s.deposit_id, reservation_id: s.reservation.id, category: "broken_item", description: `AllTools broken lamp ${run}`, amount_cents: 3500 }), { save: "damage_claim" }),
  c("compliance_record_consent", s => ({ guest_id: s.guest.id, policy_version_id: s.policy_version_id, action: "granted", purpose: "marketing", reservation_id: s.reservation.id, ip_address: "127.0.0.1" }), { id: "compliance_consent_granted" }),
  c("compliance_record_consent", s => ({ guest_id: s.guest.id, policy_version_id: s.policy_version_id, action: "withdrawn", purpose: "marketing", reservation_id: s.reservation.id, ip_address: "127.0.0.1" }), { id: "compliance_consent_withdrawn" }),
  c("tax_reservation_breakdown", s => ({ reservation_id: s.reservation.id })),
  c("channel_list_connections", () => ({})),
  c("channel_import_ical", s => ({ subscription_id: s.ical_subscription_id })),
  c("channel_import_ical", s => ({ subscription_id: s.ical_subscription_id }), { id: "channel_import_ical_repeat" }),
  c("review_stats", () => ({})),
  c("review_reply", s => ({ review_id: s.review_id, text: `Thank you for your stay. Browser verification ${run}.` })),
  c("reservation_transition", s => ({ reservation_id: s.reservation.id, action: "confirm" }), { id: "reservation_confirm" }),
  c("reservation_transition", s => ({ reservation_id: s.reservation.id, action: "check-in" }), { id: "reservation_check_in" }),
  c("reservation_transition", s => ({ reservation_id: s.reservation.id, action: "check-out" }), { id: "reservation_check_out" }),
  c("reservation_create", s => ({ property_id: s.property.id, unit_id: s.unit_id, primary_guest_id: s.guest.id, check_in_date: "2027-01-14", check_out_date: "2027-01-16", total_amount_cents: 24000 }), { id: "reservation_create_for_cancel", save: "cancel_reservation" }),
  c("reservation_transition", s => ({ reservation_id: s.cancel_reservation.id, action: "confirm" }), { id: "reservation_confirm_for_cancel" }),
  c("reservation_transition", s => ({ reservation_id: s.cancel_reservation.id, action: "cancel", reason: `Browser cancellation ${run}` }), { id: "reservation_cancel" }),
  c("reservation_create", s => ({ property_id: s.property.id, unit_id: s.unit_id, primary_guest_id: s.guest.id, check_in_date: "2027-01-17", check_out_date: "2027-01-19", total_amount_cents: 24000 }), { id: "reservation_create_for_no_show", save: "no_show_reservation" }),
  c("reservation_transition", s => ({ reservation_id: s.no_show_reservation.id, action: "confirm" }), { id: "reservation_confirm_for_no_show" }),
  c("reservation_transition", s => ({ reservation_id: s.no_show_reservation.id, action: "no-show" }), { id: "reservation_no_show" }),
];

async function manorApi(path: string, data?: any) {
  const token = await manor.evaluate(() => localStorage.getItem("manor_token"));
  const options = { headers: { Authorization: `Bearer ${token}` }, data };
  const response = data === undefined
    ? await manor.request.get(`${manorURL}/api/v1${path}`, options)
    : await manor.request.put(`${manorURL}/api/v1${path}`, options);
  expect(response.ok(), `Manor ${path}: ${response.status()}`).toBe(true);
  return response.json();
}

async function screenshot(page: Page, info: TestInfo, label: string) {
  const path = info.outputPath(`${label}.png`);
  await page.screenshot({ path, fullPage: true, animations: "disabled", mask: [page.locator('input[type="password"]')], maskColor: "#d6d3d1" });
  await info.attach(label, { path, contentType: "image/png" });
  return path;
}

test.beforeAll(async ({ browser }) => {
  for (const url of [manorURL, pmsURL, modelURL]) expect(new URL(url).hostname).toBe("127.0.0.1");
  const covered = new Set(cases.map(item => item.tool));
  expect(covered.size).toBe(44);
  for (const name of covered) expect(toolMap.has(name), `Missing PMS contract: ${name}`).toBe(true);
  mkdirSync(root, { recursive: true });
  writeFileSync(`${root}/tool-coverage.json`, JSON.stringify({
    suite: "historical-core-workflows", covered: [...covered].sort(),
    not_covered_by_this_browser_suite: [...toolMap.keys()].filter(name => !covered.has(name)).sort(),
  }, null, 2));
  credentials = readFixture("credentials.json");
  const login = readFixture("manor-credentials.json");
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    localStorage.setItem("manor_tour_completed", "true");
  });
  manor = await context.newPage();
  await manor.goto(`${manorURL}/login`);
  await manor.locator('input[type="email"]').fill(login.email);
  await manor.locator('input[type="password"]').fill(login.password);
  await manor.locator('button[type="submit"]').click();
  await expect(manor).not.toHaveURL(/\/login(?:[/?#]|$)/);
  await manor.goto(`${manorURL}/integrations?provider=manor_pms`);
  const detail = manor.getByRole("dialog", { name: "Manor PMS", exact: true });
  await detail.getByRole("button", { name: /^(Connect|\+ Add account)$/ }).click();
  const add = manor.getByRole("dialog", { name: "Add Manor PMS account", exact: true });
  const name = `PMS all functions ${run}`;
  await add.getByLabel("Account name (optional)").fill(name);
  await add.getByLabel("Access Token").fill(credentials.access_token);
  await add.getByRole("button", { name: "Save", exact: true }).click();
  await expect(add).not.toBeVisible();
  await expect.poll(async () => {
    const catalog = await manorApi("/integrations/mcp-servers");
    return catalog.find((item: any) => item.server_key === "manor_pms").entity_accounts.find((item: any) => item.name === name)?.id;
  }).toBeTruthy();
  const catalog = await manorApi("/integrations/mcp-servers");
  accountId = catalog.find((item: any) => item.server_key === "manor_pms").entity_accounts.find((item: any) => item.name === name).id;
  const actions = Object.fromEntries(contract.filter((tool: any) => !tool.annotations.readOnlyHint).map((tool: any) => [`manor_pms.${tool.name}`, "approval"]));
  await manorApi("/admin/preferences", { runtime_approval_policy: { global: { actions } } });
  const preferences = await manorApi("/admin/preferences");
  expect(preferences.runtime_approval_policy.global.actions).toEqual(actions);
  pms = await context.newPage();
  await loginPms(pms, { baseUrl: pmsURL, manorCredentials: login });
  await manor.goto(`${manorURL}/chat`);
  await manor.getByRole("button", { name: "New Chat", exact: true }).click();
  state = { ...readFixture("scenario-fixtures.json"), run };
});

for (const [index, scenario] of cases.entries()) {
  test(`${String(index + 1).padStart(2, "0")} ${scenario.id}: Manor browser → PMS`, async ({}, info) => {
    const started = Date.now();
    const args = scenario.args(state);
    // Use an opaque numeric marker: Chat intentionally humanizes snake_case
    // identifiers in prose, while the tool/result JSON remains exact.
    const caseId = `${run}-${String(index + 1).padStart(2, "0")}`;
    const readOnly = toolMap.get(scenario.tool).annotations.readOnlyHint;
    const read = pmsReadback(scenario.tool, args);
    const apiRoot = credentials.api_url.replace(/\/api\/mcp\/?$/, "");
    const before = await readPms(pms.request, apiRoot, credentials.access_token, read);
    const reservationBefore = scenario.tool === "ancillary_add_item"
      ? await readPms(pms.request, apiRoot, credentials.access_token, { path: `/api/reservations/${args.reservation_id}` })
      : undefined;
    const responsePromise = manor.waitForResponse(response => response.url().endsWith("/api/v1/chat/stream") && (response.request().postData() || "").includes(caseId));
    const composer = manor.locator('.chat-composer-rich-editor[role="textbox"]');
    await composer.fill(`PMS_E2E_TOOL ${JSON.stringify({ case_id: caseId, tool: scenario.tool, arguments: { ...args, integration_account_id: accountId } })}`);
    await composer.press("Enter");
    const response = await responsePromise;
    expect(response.ok()).toBe(true);
    await response.finished();
    let approvalScreenshot: string | undefined;
    let pending: unknown;
    if (!readOnly) {
      const approve = manor.getByRole("button", { name: "Approve", exact: true });
      await expect(approve).toBeVisible({ timeout: 60_000 });
      pending = await readPms(pms.request, apiRoot, credentials.access_token, read);
      expect(pending, `${scenario.tool}: no mutation before approval`).toEqual(before);
      if (reservationBefore) {
        const pendingReservation = await readPms(pms.request, apiRoot, credentials.access_token, { path: `/api/reservations/${args.reservation_id}` });
        expect(pendingReservation, "Ancillary billing must not change before approval").toEqual(reservationBefore);
      }
      approvalScreenshot = await screenshot(manor, info, "manor-awaiting-approval");
      await approve.click();
    }
    let observed: any;
    await expect.poll(async () => {
      const observation = await manor.request.get(`${modelURL}/cases/${encodeURIComponent(caseId)}`);
      if (!observation.ok()) return false;
      observed = await observation.json();
      if (observed.status === "error") throw new Error(`${scenario.tool}: ${String(observed.raw_tool_content).slice(0, 2000)}`);
      return observed.final_result !== undefined && observed.final_result !== null;
    }, { timeout: 60_000 }).toBe(true);
    expect(observed.tool).toBe(`mcp__manor_pms__${scenario.tool}`);
    const result = observed.final_result;
    const resultMarker = manor.locator("main").getByText(`PMS_E2E_RESULT ${caseId}`, { exact: false }).first();
    await expect(resultMarker).toBeVisible({ timeout: 60_000 });
    const after = await readPms(pms.request, apiRoot, credentials.access_token, read);
    verifyReadback(scenario.tool, args, result, before, after, readOnly);
    const extra: State = {};
    if (reservationBefore) {
      extra.reservation_before = reservationBefore;
      extra.reservation_after = await readPms(pms.request, apiRoot, credentials.access_token, { path: `/api/reservations/${args.reservation_id}` });
      const billableTotal = (items: any[]) => items.filter(item => !["cancelled", "refunded"].includes(item.status))
        .reduce((sum, item) => sum + Number(item.total_cents), 0);
      const delta = billableTotal(after) - billableTotal(before);
      expect(extra.reservation_after.ancillary_total_cents).toBe(reservationBefore.ancillary_total_cents + delta);
      expect(extra.reservation_after.total_amount_cents).toBe(reservationBefore.total_amount_cents + delta);
      expect(extra.reservation_after.paid_amount_cents).toBe(reservationBefore.paid_amount_cents);
    }
    if (["create_property", "get_property", "list_properties"].includes(scenario.tool)) {
      const propertyId = scenario.tool === "create_property" ? result.id : state.property.id;
      extra.property_owners = await readPms(pms.request, apiRoot, credentials.access_token, {
        path: `/api/property-owners/property/${propertyId}`,
      });
      expect(extra.property_owners).toHaveLength(1);
      const relation = extra.property_owners[0];
      expect(relation).toMatchObject({ property_id: propertyId, owner_id: state.owner.id, first_name: state.owner.first_name, last_name: state.owner.last_name });
      expect(Number(relation.ownership_percent)).toBe(100);
      expect(Boolean(relation.is_primary_owner)).toBe(true);
      extra.owner_properties = await readPms(pms.request, apiRoot, credentials.access_token, {
        path: `/api/property-owners/owner/${state.owner.id}`,
      });
      expect(extra.owner_properties.filter((row: any) => row.property_id === propertyId)).toHaveLength(1);
      expect(extra.owner_properties.find((row: any) => row.property_id === propertyId).id).toBe(relation.id);
    }
    if (scenario.tool === "channel_import_ical") {
      expect(result.imported_events).toBe(2);
      extra.blocked_nights = await readPms(pms.request, apiRoot, credentials.access_token, {
        path: `/api/pricing/units/${state.unit_id}/overrides`, params: { from: "2027-01-20", to: "2027-01-22" },
      });
      expect(extra.blocked_nights.map((row: any) => row.date).sort()).toEqual(["2027-01-20", "2027-01-21"]);
      for (const row of extra.blocked_nights) expect(row).toMatchObject({ source: "channel_sync", nightly_rate_cents: 99999999 });
    }
    if (scenario.tool === "compliance_record_consent") {
      extra.active_consent = await readPms(pms.request, apiRoot, credentials.access_token, {
        path: `/api/compliance/guests/${args.guest_id}/consents/${args.purpose}/active`,
      });
      expect(extra.active_consent).toMatchObject({ active: args.action === "granted", purpose: args.purpose, guest_id: args.guest_id });
    }
    scenario.check?.(result, state);
    if (scenario.save) state[scenario.save] = result;
    const manorScreenshot = await screenshot(manor, info, "manor-result");
    const pmsVerification = await verifyPmsInBrowser(pms, { tool: scenario.tool, arguments: args, result, caseId, fixtures: state });
    const pmsScreenshot = await screenshot(pms, info, "pms-verified");
    if (scenario.seed) {
      const seedArgs = scenario.seed === "property"
        ? ["property", "--property-id", String(state.property.id)]
        : ["reservation", "--reservation-id", String(state.reservation.id), "--guest-id", String(state.guest.id)];
      execFileSync("python3", [`${root}/pms/fixture_cli.py`, ...seedArgs], { encoding: "utf8", timeout: 60_000 });
      Object.assign(state, readFixture("scenario-fixtures.json"));
    }
    const row = { case_id: caseId, tool: scenario.tool, read_only: readOnly, arguments: args, result, pms_readback: after, pms_before: before, pms_pending_approval: pending, extra_pms_verification: extra, no_write_before_approval: !readOnly ? true : undefined, pms_ui: pmsVerification, screenshots: { manor: manorScreenshot, pms: pmsScreenshot, approval: approvalScreenshot }, elapsed_ms: Date.now() - started, status: "passed" };
    evidence.push(row);
    writeFileSync(`${root}/case-evidence.json`, JSON.stringify(evidence, null, 2));
    writeFileSync(`${root}/scenario-state.json`, JSON.stringify(state, null, 2));
    await info.attach("function-evidence", { body: JSON.stringify(row, null, 2), contentType: "application/json" });
  });
}

test.afterAll(async () => {
  if (evidence.length === cases.length) {
    expect(new Set(evidence.map(row => row.tool))).toEqual(new Set(cases.map(item => item.tool)));
  }
  await context?.close();
});
