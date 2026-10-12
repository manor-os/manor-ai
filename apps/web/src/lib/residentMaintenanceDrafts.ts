import type { ResidentMaintenance } from "./useResidentWebchat";

export const maintenanceCategories = ["plumbing", "electrical", "hvac", "appliance", "other"] as const;
export const maintenancePriorities = ["low", "medium", "high", "urgent"] as const;
export type ResidentDraftFields = {
  location_ref: string | null; title: string; description: string;
  category: typeof maintenanceCategories[number]; priority: typeof maintenancePriorities[number];
};
export type ResidentDraft = {
  draft_id: string; revision: number;
  state: "draft" | "confirmed" | "submitting" | "outcome_unknown" | "submitted" | "cancelled" | "rejected";
  fields: ResidentDraftFields; location_label: string | null; confirmation_nonce: string | null;
  request: ResidentMaintenance | null; error: { code: string; message: string } | null; updated_at: string;
};
export type ResidentDraftCollection = {
  create_access: { verified: boolean; expires_at: string | null };
  locations: { location_ref: string; label: string }[];
  items: ResidentDraft[]; skip: number; limit: number; has_more: boolean;
};

export function isResidentDraft(value: unknown): value is ResidentDraft {
  if (!value || typeof value !== "object") return false;
  const d = value as ResidentDraft;
  return typeof d.draft_id === "string" && Number.isInteger(d.revision) && d.revision > 0
    && ["draft", "confirmed", "submitting", "outcome_unknown", "submitted", "cancelled", "rejected"].includes(d.state)
    && !!d.fields && typeof d.fields.title === "string" && typeof d.fields.description === "string"
    && (d.fields.location_ref === null || typeof d.fields.location_ref === "string")
    && maintenanceCategories.includes(d.fields.category) && maintenancePriorities.includes(d.fields.priority)
    && (d.location_label === null || typeof d.location_label === "string")
    && (d.confirmation_nonce === null || typeof d.confirmation_nonce === "string")
    && (d.state !== "submitted" || (!!d.request && Number.isInteger(d.request.id) && d.request.id > 0 && typeof d.request.status === "string"));
}

export function isResidentDraftCollection(value: unknown): value is ResidentDraftCollection {
  if (!value || typeof value !== "object") return false;
  const d = value as ResidentDraftCollection;
  return typeof d.create_access?.verified === "boolean"
    && (d.create_access.expires_at === null || typeof d.create_access.expires_at === "string")
    && Array.isArray(d.locations) && d.locations.every(l => !!l && typeof l.location_ref === "string" && typeof l.label === "string")
    && Array.isArray(d.items) && d.items.every(isResidentDraft)
    && Number.isInteger(d.skip) && d.skip >= 0 && Number.isInteger(d.limit) && d.limit > 0 && typeof d.has_more === "boolean";
}

export function residentCreateIsActive(access: ResidentDraftCollection["create_access"], now = Date.now()) {
  return access.verified === true && typeof access.expires_at === "string" && Date.parse(access.expires_at) > now;
}

export function residentDraftCanConfirm(draft: ResidentDraft, fields: ResidentDraftFields, locations: ResidentDraftCollection["locations"]) {
  return draft.state === "draft" && !!draft.confirmation_nonce
    && residentDraftFieldsEqual(draft.fields, fields)
    && !!fields.location_ref && locations.some(item => item.location_ref === fields.location_ref)
    && fields.title.trim().length > 0 && fields.title.trim().length <= 255
    && fields.description.trim().length > 0 && fields.description.trim().length <= 5000;
}

export function residentDraftFieldsEqual(left: ResidentDraftFields, right: ResidentDraftFields) {
  return (["location_ref", "title", "description", "category", "priority"] as const).every(key => left[key] === right[key]);
}
