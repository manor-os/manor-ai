import { expect, type Locator, type Page } from "@playwright/test";
import { pmsReadback, readPms } from "./pms-rest-verifier";

type RecordData = Record<string, any>;
export type PmsBrowserVerification = {
  route: string;
  assertions: string[];
  uiSupported: boolean;
  notes: string[];
};
export type PmsBrowserInput = {
  tool: string;
  arguments: RecordData;
  result: any;
  caseId: string;
  fixtures?: RecordData;
  baseUrl?: string;
};

const vacation = "/management/vacation/";
const statuses: Record<string, string> = {
  pending: "Pending", confirmed: "Confirmed", checked_in: "Checked in", checked_out: "Checked out",
  cancelled: "Cancelled", no_show: "No-show", assigned: "Assigned", in_progress: "In progress",
  completed: "Completed", verified: "Verified", failed: "Failed",
};

// Normal OAuth UI creates this session. Only presentation preferences are set
// by the test; no auth token, user, role, or entity is injected.
export async function loginPms(page: Page, options: {
  baseUrl: string;
  manorCredentials: { email: string; password: string };
}): Promise<void> {
  expect(new URL(options.baseUrl).hostname).toBe("127.0.0.1");
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    localStorage.setItem("manor_tour_completed", "true");
  });
  await page.goto(`${options.baseUrl}/login?portal=management&lang=en`);
  await page.getByRole("button", { name: /Sign in with Manor/ }).click();
  await page.waitForURL(url => url.origin !== options.baseUrl);
  expect(new URL(page.url()).hostname).toBe("127.0.0.1");
  await expect(page.locator('input[type="email"]').or(page.getByRole("button", { name: "Continue", exact: true }))).toBeVisible();
  if (await page.locator('input[type="email"]').isVisible()) {
    await page.locator('input[type="email"]').fill(options.manorCredentials.email);
    await page.locator('input[type="password"]').fill(options.manorCredentials.password);
    await page.locator('button[type="submit"]').click();
  }
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page).toHaveURL(`${options.baseUrl}/management`, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Management Portal", exact: true })).toBeVisible();
  const verified = await pmsGet(page, "/api/auth/verify");
  expect(verified.valid ?? verified.success ?? !!verified.user).toBeTruthy();
}

async function pmsGet(page: Page, path: string, params?: RecordData) {
  const token = await page.evaluate(() => localStorage.getItem("token"));
  expect(token, "PMS session must come from the completed OAuth UI").toBeTruthy();
  return readPms(page.request, new URL(page.url()).origin, token!, { path, params });
}

function displayDate(value: string): string {
  return `${value.slice(5, 7)}/${value.slice(8, 10)}/${value.slice(0, 4)}`;
}

function records(value: any): RecordData[] {
  if (Array.isArray(value)) return value;
  if (Array.isArray(value?.items)) return value.items;
  if (Array.isArray(value?.data)) return value.data;
  return value && typeof value === "object" ? [value] : [];
}

async function ready(page: Page) {
  await page.waitForLoadState("networkidle");
  await expect(page.locator("h1")).toBeVisible();
  await expect(page.locator(".ant-spin-spinning")).toHaveCount(0);
}

async function rowById(page: Page, id: number | string): Promise<Locator> {
  const row = page.locator(`.ant-table-tbody tr[data-row-key="${id}"]`).filter({ visible: true });
  await ready(page);
  if (!await row.count()) {
    const firstPage = page.locator(".ant-pagination-item-1").filter({ visible: true });
    if (await firstPage.count()) await firstPage.click();
  }
  for (let index = 0; index < 50; index++) {
    if (await row.count()) return row;
    const next = page.locator(".ant-pagination-next:not(.ant-pagination-disabled)").filter({ visible: true });
    if (!await next.count()) break;
    await next.click();
    await ready(page);
  }
  await expect(row, `PMS table must show persisted row ${id}`).toBeVisible();
  return row;
}

async function texts(locator: Locator, values: any[], output: string[]) {
  for (const value of values) {
    if (value === undefined || value === null || value === "") continue;
    await expect(locator).toContainText(String(value));
    output.push(`DOM contains ${String(value)}`);
  }
}

async function descriptionMoney(drawer: Locator, label: string, cents: number, currency: string, output: string[], hideNonPositive = true) {
  const field = drawer.locator(".ant-descriptions-item-label").filter({ hasText: new RegExp(`^${label}$`) });
  if (cents > 0 || !hideNonPositive) {
    const row = field.locator("xpath=ancestor::tr[1]");
    await expect(row.locator(".ant-descriptions-item-content")).toHaveText(`${(cents / 100).toFixed(2)} ${currency}`);
    await row.scrollIntoViewIfNeeded();
    await expect(row).toBeInViewport({ ratio: 1 });
    output.push(`DOM ${label} is ${(cents / 100).toFixed(2)} ${currency} and visible in the viewport`);
  } else {
    await expect(field).toHaveCount(0);
    output.push(`DOM omits ${label}, matching the nonpositive amount ${(cents / 100).toFixed(2)} ${currency}`);
  }
}

async function selectUnit(page: Page, id: number | string, within?: Locator) {
  const input = (within || page.locator(".ant-card").first()).getByRole("combobox").first();
  await input.fill(`#${id}`);
  await page.locator(".ant-select-item-option").filter({ hasText: `(#${id})` }).click();
}

async function openReservation(page: Page, baseUrl: string, id: number | string) {
  await page.goto(`${baseUrl}${vacation}reservations?lang=en`);
  await ready(page);
  await (await rowById(page, id)).locator("td").first().click();
  const drawer = page.locator(".ant-drawer-content");
  const wrapper = page.locator(".ant-drawer-content-wrapper");
  await expect(drawer).toBeVisible();
  await expect(wrapper).not.toHaveClass(/ant-drawer-panel-motion-.*-(?:appear|enter)/);
  await expect(wrapper).toHaveCSS("transform", "none");
  await expect(wrapper).toBeInViewport({ ratio: 1 });
  await expect(drawer.locator(".ant-spin-spinning")).toHaveCount(0);
  const reservation = await pmsGet(page, `/api/reservations/${id}`);
  await expect(drawer.locator(".ant-drawer-title")).toHaveText(`Reservation #${id}`);
  const party = drawer.locator(".ant-descriptions-item-label").filter({ hasText: /^Party$/ }).locator("xpath=ancestor::tr[1]");
  await expect(party).toContainText(`${reservation.adults} adults`);
  return drawer;
}

export async function verifyPmsInBrowser(page: Page, input: PmsBrowserInput): Promise<PmsBrowserVerification> {
  const tool = input.tool.replace(/^mcp__manor_pms__/, "");
  const a = input.arguments;
  const result = input.result;
  const baseUrl = input.baseUrl || input.fixtures?.pms_frontend_origin || "http://127.0.0.1:18315";
  expect(new URL(baseUrl).hostname).toBe("127.0.0.1");
  const assertions: string[] = [];
  const notes: string[] = [];
  let uiSupported = true;
  let route: string;
  const ltr = tool.match(/^(get|list|create)_(property|properties|tenant|tenants|owner|owners|vendor|vendors|lease|leases|maintenance_request|maintenance_requests|payment|payments)$/);
  if (ltr) {
    const noun = ltr[2];
    const resource = noun.startsWith("propert") ? "properties" : noun.startsWith("maintenance") ? "maintenance" : noun.replace(/s$/, "") + "s";
    route = `/management/${resource}`;
  } else if (tool.startsWith("reservation_") || tool.startsWith("guest_") || tool === "pricing_quote" || tool === "ancillary_add_item" || tool === "tax_reservation_breakdown") route = vacation + "reservations";
  else if (tool.startsWith("pricing_")) route = vacation + "pricing";
  else if (tool.startsWith("promo_")) route = vacation + "promo-codes";
  else if (tool.startsWith("policy_")) route = vacation + "policies";
  else if (tool.startsWith("ancillary_")) route = vacation + "ancillary";
  else if (tool.startsWith("cleaning_")) route = vacation + "cleaning";
  else if (tool.startsWith("damage_")) route = vacation + "damage";
  else if (tool.startsWith("compliance_")) route = vacation + "compliance";
  else if (tool.startsWith("channel_")) route = vacation + "channels";
  else if (tool.startsWith("review_")) route = vacation + "reviews";
  else throw new Error(`Missing PMS browser mapping: ${tool}`);
  await page.goto(`${baseUrl}${route}?lang=en`);
  await ready(page);
  assertions.push(`Real PMS page opened: ${route}`);

  if (ltr) {
    for (const record of records(result)) {
      const row = await rowById(page, record.id);
      const resource = route.split("/").pop();
      const fields = resource === "properties" ? [record.name, record.property_type, record.status]
        : resource === "tenants" || resource === "owners" ? [record.first_name, record.last_name, record.email, record.status]
          : resource === "vendors" ? [record.company_name, record.contact_name, record.email, record.status]
            : resource === "maintenance" ? [record.title, record.priority, record.category, record.status]
              : resource === "payments" ? [`Lease #${record.lease_id}`, Number(record.amount).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }), record.payment_method, record.payment_type, record.status]
                : [record.unit_id == null ? undefined : `#${record.unit_id}`, record.start_date ? displayDate(record.start_date) : undefined, record.end_date ? displayDate(record.end_date) : undefined, record.monthly_rent ? `$${Number(record.monthly_rent).toLocaleString("en-US")}` : undefined, record.lease_type, record.status];
      await texts(row, fields, assertions);
      const currentPropertyId = tool === "create_property" ? result.id : input.fixtures?.property?.id;
      if (resource === "properties" && record.id === currentPropertyId) {
        const owner = input.fixtures?.owner;
        if (!owner) throw new Error("Current-property owner fixture is required for the Owners column assertion");
        const ownerColumn = await page.getByRole("columnheader", { name: "Owners", exact: true })
          .evaluate(header => (header as HTMLTableCellElement).cellIndex);
        const ownerTag = row.getByRole("cell").nth(ownerColumn).locator(".ant-tag");
        const label = `${owner.first_name} ${owner.last_name} (Primary) - 100%`;
        await expect(ownerTag).toHaveText([label]);
        assertions.push(`DOM Owners column shows ${label} for current property ${record.id}`);
      }
    }
  } else if (tool.startsWith("reservation_")) {
    for (const record of records(result)) {
      const row = await rowById(page, record.id);
      await texts(row, [`#${record.primary_guest_id}`, `#${record.unit_id}`, record.channel, (record.total_amount_cents / 100).toFixed(2), statuses[record.status]], assertions);
    }
  } else if (tool.startsWith("guest_")) {
    if (records(result).length === 0) {
      uiSupported = false;
      notes.push("PMS has no standalone guest search UI; the empty search is independently verified through authenticated PMS REST.");
    }
    const reservations = await pmsGet(page, "/api/reservations", { limit: 100 });
    for (const guest of records(result)) {
      const reservation = records(reservations).find(record => record.primary_guest_id === guest.id);
      if (!reservation) {
        uiSupported = false;
        notes.push(`Guest ${guest.id} has no linked reservation; PMS has no standalone guest page. Guest fields verified through authenticated PMS REST.`);
        expect(await pmsGet(page, `/api/guests/${guest.id}`)).toMatchObject(guest);
        assertions.push(`PMS REST guest ${guest.id} equals Manor result`);
      } else {
        const drawer = await openReservation(page, baseUrl, reservation.id);
        await texts(drawer, [guest.first_name, guest.last_name, guest.email, guest.phone], assertions);
        await drawer.getByRole("button", { name: "Close", exact: true }).click();
      }
    }
  } else if (tool === "pricing_quote") {
    await page.getByRole("button", { name: /New Reservation/ }).click();
    const modal = page.getByRole("dialog");
    await selectUnit(page, a.unit_id, modal);
    const inputs = modal.locator(".ant-picker-input input");
    await inputs.nth(0).fill(displayDate(a.check_in));
    await inputs.nth(0).press("Tab");
    await inputs.nth(1).fill(displayDate(a.check_out));
    await inputs.nth(1).press("Enter");
    await descriptionMoney(modal, "Nightly subtotal", result.subtotal_cents, result.currency, assertions, false);
    await descriptionMoney(modal, "Total", result.total_cents, result.currency, assertions, false);
    await expect(modal.locator(".ant-descriptions-title")).toContainText(`${result.nights} `);
    if (result.grand_total_cents !== undefined && result.grand_total_cents !== result.total_cents) {
      uiSupported = false;
      notes.push("The reservation quote preview shows nightly subtotal and discounted total; separate fees and grand total are not rendered in this form. PMS REST verifies the complete quote.");
      expect(await pmsGet(page, `/api/pricing/units/${a.unit_id}/quote`, { check_in: a.check_in, check_out: a.check_out })).toEqual(result);
      assertions.push("Independent PMS REST verifies the complete quote including fees and grand total");
    }
    notes.push("Only the live quote preview is used; the reservation form is never submitted.");
  } else if (tool === "pricing_set_override") {
    await selectUnit(page, a.unit_id);
    const row = await rowById(page, result.id);
    await texts(row, [displayDate(result.date), (result.nightly_rate_cents / 100).toFixed(2), result.source, result.note], assertions);
  } else if (tool === "ancillary_list_services") {
    for (const record of records(result)) await texts(await rowById(page, record.id), [record.code, record.name, (record.unit_price_cents / 100).toFixed(2)], assertions);
  } else if (tool.startsWith("cleaning_")) {
    if (tool === "cleaning_list_late") {
      await page.getByRole("switch").check();
      await ready(page);
    }
    for (const record of records(result)) await texts(await rowById(page, record.id), [`#${record.unit_id}`, statuses[record.status]], assertions);
  } else if (tool.startsWith("damage_")) {
    const reservationId = a.reservation_id || result?.reservation_id;
    expect(reservationId, "Damage verification requires the real reservation id").toBeTruthy();
    await page.locator('input[type="number"]').fill(String(reservationId));
    await page.locator('input[type="number"]').press("Enter");
    if (tool === "damage_file_claim") await texts(await rowById(page, result.id), [result.description, (result.amount_cents / 100).toFixed(2), result.status], assertions);
    else if (result) await texts(page.locator(".ant-descriptions"), [`Deposit #${result.id}`, (result.amount_cents / 100).toFixed(2), result.status], assertions);
    else await texts(page.locator(".ant-empty"), ["No deposit"], assertions);
  } else if (tool === "compliance_record_consent") {
    await page.getByRole("tab", { name: "GDPR consents", exact: true }).click();
    await texts(await rowById(page, result.id), [result.purpose, result.action, result.ip_address], assertions);
  } else if (tool === "channel_list_connections") {
    for (const record of records(result)) await texts(await rowById(page, record.id), [record.channel, record.status, record.external_property_id], assertions);
  } else if (tool === "channel_import_ical") {
    await page.getByRole("tab", { name: "iCal subscriptions", exact: true }).click();
    const subscriptions = await pmsGet(page, "/api/channels/ical-subscriptions");
    const record = records(subscriptions).find(row => row.id === a.subscription_id);
    if (!record) throw new Error(`PMS subscription ${a.subscription_id} was not found after iCal import`);
    expect(record.last_imported_at).toBeTruthy();
    await texts(await rowById(page, a.subscription_id), [record.source_label, String(record.last_event_count)], assertions);
  } else if (tool === "review_stats") {
    const metrics = page.locator(".ant-statistic-content-value");
    await expect(metrics).toHaveText([String(result.count), Number(result.average).toFixed(2), String(result.last_30d_count), String(result.pending_reply_count)]);
    assertions.push("DOM review count, average, recent count and pending replies match Manor result");
  } else if (tool === "review_reply") {
    await texts(await rowById(page, a.review_id), [a.text, "Replied"], assertions);
  } else {
    // These calculations/items do not have an equivalent operator result view.
    // Open the relevant real UI, compare its related fields when available,
    // and explicitly record the gap alongside independent authenticated REST.
    uiSupported = false;
    const token = await page.evaluate(() => localStorage.getItem("token"));
    const readback = await readPms(page.request, baseUrl, token!, pmsReadback(tool, a));
    if (tool === "ancillary_add_item") {
      expect(records(readback).find(row => row.id === result.id)).toMatchObject(result);
      const reservation = await pmsGet(page, `/api/reservations/${a.reservation_id}`);
      const drawer = await openReservation(page, baseUrl, a.reservation_id);
      await descriptionMoney(drawer, "Ancillary", reservation.ancillary_total_cents, reservation.currency, assertions);
      await descriptionMoney(drawer, "Total", reservation.total_amount_cents, reservation.currency, assertions, false);
      await descriptionMoney(drawer, "Balance", reservation.total_amount_cents - reservation.paid_amount_cents, reservation.currency, assertions);
      notes.push("Reservation drawer shows ancillary total; individual ancillary items have no PMS operator list view.");
    } else {
      expect(readback).toEqual(result);
      if (tool === "promo_validate") {
        await texts(page.locator(".ant-table-tbody"), [a.code], assertions);
        notes.push("PMS Promo Codes page shows configuration but has no validation result panel.");
      } else if (tool.startsWith("policy_")) {
        await page.getByRole("tab", { name: tool.includes("cancellation") ? "Cancellation" : "Modification", exact: true }).click();
        if (result.policy_id) {
          await texts(await rowById(page, result.policy_id), [result.policy_slug], assertions);
        }
        notes.push("PMS Policies page shows terms but has no per-reservation quote panel.");
      } else if (tool === "tax_reservation_breakdown") {
        const reservation = await pmsGet(page, `/api/reservations/${a.reservation_id}`);
        const drawer = await openReservation(page, baseUrl, a.reservation_id);
        await descriptionMoney(drawer, "Tax", reservation.tax_total_cents, reservation.currency, assertions);
        await descriptionMoney(drawer, "Tourism tax", reservation.tourism_tax_cents, reservation.currency, assertions);
        notes.push("Reservation drawer shows tax totals; full tax breakdown is available only through PMS REST.");
      }
    }
    assertions.push("Independent authenticated PMS REST matches actual Manor tool result");
  }
  // Empty lists must be backed by the corresponding real read; an arbitrary
  // empty page is not evidence that the correct filter returned no records.
  if (Array.isArray(result) && result.length === 0) {
    const token = await page.evaluate(() => localStorage.getItem("token"));
    expect(await readPms(page.request, baseUrl, token!, pmsReadback(tool, a))).toEqual([]);
    assertions.push("Independent PMS REST confirms the empty filtered result");
  }
  return { route, assertions, uiSupported, notes };
}
