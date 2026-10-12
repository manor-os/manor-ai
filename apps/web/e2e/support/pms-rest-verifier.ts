import { expect, type APIRequestContext } from "@playwright/test";

type Args = Record<string, any>;
type Read = { path: string; params?: Args; data?: Args };

// Independent REST readback. This never calls the MCP endpoint or repeats a
// write. promo/validate is a read-only calculation despite its POST method.
export function pmsReadback(tool: string, a: Args): Read {
  const ltr = tool.match(/^(get|list|create)_(property|properties|tenant|tenants|owner|owners|vendor|vendors|lease|leases|maintenance_request|maintenance_requests|payment|payments)$/);
  if (ltr) {
    const resource = ltr[2].replace(/ies$/, "y").replace(/s$/, "");
    const collection = resource === "property" ? "properties" : resource === "maintenance_request" ? "maintenance" : `${resource}s`;
    if (ltr[1] === "get") return { path: `/api/${collection}/${a[`${resource}_id`]}` };
    return { path: `/api/${collection}`, params: ltr[1] === "list" ? a : undefined };
  }
  switch (tool) {
    case "reservation_list": return { path: "/api/reservations", params: a };
    case "reservation_create": return { path: "/api/reservations", params: { property_id: a.property_id, unit_id: a.unit_id } };
    case "reservation_get": case "reservation_transition": return { path: `/api/reservations/${a.reservation_id}` };
    case "guest_search": return { path: "/api/guests", params: { search: a.q, limit: a.limit } };
    case "guest_create": return { path: "/api/guests", params: { search: a.email } };
    case "guest_get": return { path: `/api/guests/${a.guest_id}` };
    case "pricing_quote": return { path: `/api/pricing/units/${a.unit_id}/quote`, params: { check_in: a.check_in, check_out: a.check_out } };
    case "pricing_set_override": return { path: `/api/pricing/units/${a.unit_id}/overrides`, params: { from: a.date, to: `${Number(a.date.slice(0, 4)) + 1}${a.date.slice(4)}` } };
    case "promo_validate": return { path: "/api/promo-codes/validate", data: a };
    case "policy_cancellation_quote": return { path: `/api/policies/quote/cancellation/${a.reservation_id}`, params: a.today ? { today: a.today } : undefined };
    case "policy_modification_quote": return { path: `/api/policies/quote/modification/${a.reservation_id}`, params: a.today ? { today: a.today } : undefined };
    case "ancillary_list_services": return { path: "/api/ancillary/services" };
    case "ancillary_add_item": return { path: `/api/ancillary/reservations/${a.reservation_id}/items` };
    case "cleaning_list_tasks": return { path: "/api/cleaning/tasks", params: a };
    case "cleaning_list_late": return { path: "/api/cleaning/late" };
    case "cleaning_transition_task": return { path: `/api/cleaning/tasks/${a.cleaning_task_id}` };
    case "damage_get_for_reservation": return { path: `/api/damage/reservations/${a.reservation_id}/deposit` };
    case "damage_file_claim": return { path: `/api/damage/deposits/${a.deposit_id}/claims` };
    case "compliance_record_consent": return { path: `/api/compliance/guests/${a.guest_id}/consents` };
    case "tax_reservation_breakdown": return { path: `/api/tax/reservations/${a.reservation_id}/breakdown` };
    case "channel_list_connections": return { path: "/api/channels/connections" };
    case "channel_import_ical": return { path: "/api/channels/ical-subscriptions" };
    case "review_stats": return { path: "/api/reviews/stats" };
    case "review_reply": return { path: "/api/reviews" };
    default: throw new Error(`No independent PMS readback for ${tool}`);
  }
}

export async function readPms(request: APIRequestContext, baseURL: string, accessToken: string, read: Read) {
  expect(new URL(baseURL).hostname).toBe("127.0.0.1");
  const options = { headers: { Authorization: `Bearer ${accessToken}` }, params: read.params, data: read.data };
  const response = read.data
    ? await request.post(`${baseURL}${read.path}`, options)
    : await request.get(`${baseURL}${read.path}`, options);
  expect(response.ok(), `Independent PMS ${read.path}: HTTP ${response.status()}`).toBe(true);
  return response.json();
}

export function verifyReadback(tool: string, args: Args, result: any, before: any, after: any, readOnly: boolean) {
  expect(result, `${tool} must return an actual result`).not.toBeNull();
  expect(result?.isError).not.toBe(true);
  expect(result?.error).toBeUndefined();
  if (readOnly) {
    expect(result, `${tool}: Manor tool result must match PMS REST`).toEqual(after);
    expect(after, `${tool}: read preserves corresponding PMS state`).toEqual(before);
    return;
  }
  if (tool === "channel_import_ical") {
    expect(after).not.toEqual(before);
    const subscription = after.find((row: any) => row.id === args.subscription_id);
    expect(subscription).toBeTruthy();
    expect(subscription.last_synced_at || subscription.last_sync_at || subscription.last_imported_at).toBeTruthy();
    return;
  }
  if (tool === "review_reply") {
    const rows = Array.isArray(after) ? after : after.items || after.data;
    const review = rows.find((row: any) => row.id === args.review_id);
    expect(review).toBeTruthy();
    expect(review.reply_text || review.reply).toBe(args.text);
    return;
  }
  expect(result.id, `${tool}: persisted row identity`).toBeTruthy();
  const record = Array.isArray(after) ? after.find((row: any) => row.id === result.id) : after;
  expect(record, `${tool}: record exists in PMS`).toBeTruthy();
  expect(record).toMatchObject(result);
  if (Array.isArray(after)) expect(after.filter((row: any) => row.id === result.id)).toHaveLength(1);
  if (tool.startsWith("create_") || ["guest_create", "reservation_create", "ancillary_add_item", "damage_file_claim", "compliance_record_consent"].includes(tool)) {
    const previousIds = new Set(before.map((row: any) => row.id));
    expect(after.filter((row: any) => !previousIds.has(row.id)).map((row: any) => row.id), `${tool}: exactly one new row`).toEqual([result.id]);
  }
  for (const [key, expected] of Object.entries(args)) {
    if (key === "action" && ["cleaning_transition_task", "reservation_transition"].includes(tool)) continue;
    const field = key === "cleaning_task_id" || (key === "reservation_id" && tool === "reservation_transition") ? "id"
      : (key === "reason" && tool === "reservation_transition") ? "cancellation_reason" : key;
    expect(record, `${tool}: persisted ${field}`).toHaveProperty(field);
    if (key === "scheduled_for") {
      // PMS stores MySQL UTC datetimes without a timezone suffix.
      const stored = /Z|[+-]\d\d:\d\d$/.test(record[field]) ? record[field] : `${record[field]}Z`;
      expect(new Date(stored).toISOString()).toBe(new Date(String(expected)).toISOString());
    } else if (typeof expected === "number") {
      // Pydantic serializes Decimal columns as JSON strings.
      expect(Number(record[field]), `${tool}: persisted ${field}`).toBe(expected);
    } else expect(record[field], `${tool}: persisted ${field}`).toEqual(expected);
  }
  if (tool === "reservation_transition") {
    const statuses: Args = { confirm: "confirmed", "check-in": "checked_in", "check-out": "checked_out", cancel: "cancelled", "no-show": "no_show" };
    expect(record.status).toBe(statuses[args.action]);
  }
  if (tool === "cleaning_transition_task") {
    const statuses: Args = { start: "in_progress", complete: "completed", cancel: "cancelled" };
    expect(record.status).toBe(statuses[args.action]);
  }
  if (tool === "pricing_set_override") {
    expect(record.nightly_rate_cents).toBe(args.nightly_rate_cents);
    expect(after.filter((row: any) => row.date === args.date)).toHaveLength(1);
  }
  if (tool === "compliance_record_consent") expect(record.action).toBe(args.action);
}
