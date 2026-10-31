import { t } from "../../lib/i18n";
/**
 * Shared types + form helpers used by TeamPeople, TeamRoles, and
 * TeamLayout. Kept in one file so the three route children can import
 * from a single source.
 */

export type Kind = "employee" | "contractor" | "vendor" | "external";
export type KindFilter = "all" | Kind;

export const KIND_BADGE: Record<
  Kind,
  { type: "teal" | "info" | "active" | "warning" | "inactive"; label: string }
> = {
  employee:   { type: "active",   label: t("page.shared.employee") },
  contractor: { type: "info",     label: t("page.shared.contractor") },
  vendor:     { type: "warning",  label: t("page.shared.vendor") },
  external:   { type: "inactive", label: t("page.shared.external") },
};

export const KIND_FILTERS: { key: KindFilter; label: string }[] = [
  { key: "all",        label: t("page.workspaces.filter_all") },
  { key: "employee",   label: t("page.shared.employees") },
  { key: "contractor", label: t("page.shared.contractors") },
  { key: "vendor",     label: t("page.shared.vendors") },
  { key: "external",   label: t("page.shared.external") },
];

export const STATUS_MAP: Record<
  string,
  { type: "active" | "warning" | "inactive" | "danger"; pulse: boolean }
> = {
  active:   { type: "active",   pulse: true  },
  invited:  { type: "warning",  pulse: false },
  inactive: { type: "inactive", pulse: false },
  rejected: { type: "danger",   pulse: false },
  declined: { type: "danger",   pulse: false },
};

export function staffStatusLabel(status?: string): string {
  const normalized = (status || "active").toLowerCase();
  const labels: Record<string, string> = {
    active: t("page.team_people.status_active"),
    invited: t("page.team_people.status_invited"),
    inactive: t("page.team_people.status_inactive"),
    rejected: t("page.team_people.status_rejected"),
    declined: t("page.team_people.status_rejected"),
  };
  return labels[normalized] || status || t("page.team_people.status_active");
}

export interface StaffForm {
  name: string;
  kind: Kind;
  email: string;
  phone: string;
  title: string;
  role: string;
  skills: string;
  service_categories: string;
  company_name: string;
  tax_id: string;
  billing_rate: string;
  billing_currency: string;
  payment_terms: string;
  preferred_payment_method: string;
  address: string;
  website: string;
  notes: string;
}

export const emptyStaffForm: StaffForm = {
  name: "", kind: "employee", email: "", phone: "", title: "",
  role: "member",
  skills: "", service_categories: "",
  company_name: "", tax_id: "", billing_rate: "", billing_currency: "",
  payment_terms: "", preferred_payment_method: "",
  address: "", website: "", notes: "",
};

export const parseCsv = (s: string) =>
  s.split(",").map((v) => v.trim()).filter(Boolean);

const SYSTEM_ROLE_VALUES = new Set(["viewer", "member", "admin", "owner"]);

function formRoleValue(s: any): string {
  // role_name comes from the assigned StaffRole (the authority); role is the
  // legacy label and can be stale.
  const value = String(s.role_name || s.role || "member").trim().toLowerCase();
  return SYSTEM_ROLE_VALUES.has(value) ? value : "member";
}

export function toForm(s: any): StaffForm {
  return {
    name: s.name || "",
    kind: (s.kind as Kind) || "employee",
    email: s.email || "",
    phone: s.phone || "",
    title: s.title || "",
    role: formRoleValue(s),
    skills: (s.skills || []).join(", "),
    service_categories: (s.service_categories || []).join(", "),
    company_name: s.company_name || "",
    tax_id: s.tax_id || "",
    billing_rate: s.billing_rate != null ? String(s.billing_rate) : "",
    billing_currency: s.billing_currency || "",
    payment_terms: s.payment_terms || "",
    preferred_payment_method: s.preferred_payment_method || "",
    address: s.address || "",
    website: s.website || "",
    notes: s.notes || "",
  };
}

export function formToPayload(f: StaffForm) {
  const base: Record<string, any> = {
    name: f.name,
    kind: f.kind,
    email: f.email || null,
    phone: f.phone || null,
    title: f.title || null,
    role: f.role || null,
    skills: parseCsv(f.skills),
  };
  if (f.kind !== "employee") {
    base.service_categories = parseCsv(f.service_categories);
    base.company_name = f.company_name || null;
    base.tax_id = f.tax_id || null;
    base.billing_rate = f.billing_rate ? Number(f.billing_rate) : null;
    base.billing_currency = f.billing_currency || null;
    base.payment_terms = f.payment_terms || null;
    base.preferred_payment_method = f.preferred_payment_method || null;
  }
  if (f.address) base.address = f.address;
  if (f.website) base.website = f.website;
  if (f.notes) base.notes = f.notes;
  return base;
}

/**
 * Collapse per-permission keys into a plain-English sentence for the
 * role preview shown alongside the role picker in the invite flow.
 */
export function summarizePermissions(perms: string[]): string {
  if (!perms.length) return t("page.team_shared.no_permissions_assigned");
  const moduleLabels: Record<string, string> = {
    tasks: t("nav.tasks"), docs: t("page.team_shared.documents"), agents: t("nav.agents"), workspaces: t("nav.workspaces"),
    users: t("nav.users"), entity: t("page.team_shared.entity"), chat: t("nav.chat"), admin: t("page.team_shared.admin_settings"),
    integrations: t("nav.integrations"), mcp: t("page.team_shared.mcp_servers"),
  };
  const groups: Record<string, string[]> = {};
  perms.forEach((p) => {
    const mod = p.split(".")[0];
    (groups[mod] ||= []).push(p.split(".")[1]);
  });
  const modules = Object.keys(groups).map((m) => moduleLabels[m] || m);
  return `${t("page.team_shared.access_to")} ${modules.join(", ")}.`;
}
