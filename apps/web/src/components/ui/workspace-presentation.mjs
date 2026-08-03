/**
 * Which category a workspace card presents as — icon, label, colours.
 *
 * A workspace with no `category` is classified by keyword match over its
 * name/description/kind/tags. That matching used to be plain substring
 * (`haystack.includes(term)`), and short rule terms are common word
 * fragments: "rent" is also the tail of "cur-RENT", "diffe-RENT",
 * "appa-RENT". A stickman-video workspace whose description said "the
 * current theme" was classified as Property ops — the FIRST rule in the
 * list, so nothing later got a chance to match instead.
 *
 * Matching is now whole-word: a term matches only at a word boundary, not
 * anywhere it happens to appear as a substring.
 */

/** @typedef {{ terms: string[], iconKey: string, label: string, bg: string, fg: string }} PresentationRule */

/** @type {PresentationRule[]} */
export const WORKSPACE_PRESENTATION_RULES = [
  {
    terms: ["leasing", "lease", "property", "real estate", "rent", "occupancy", "tenant"],
    iconKey: "building",
    label: "Property ops",
    bg: "#f2eee8",
    fg: "#75695e",
  },
  {
    terms: ["qa", "smoke", "test", "runtime", "regression"],
    iconKey: "beaker",
    label: "QA runtime",
    bg: "#edf1ee",
    fg: "#65786e",
  },
  {
    terms: ["x account", "twitter", "threads", "social channel", "social account"],
    iconKey: "twitter",
    label: "Social channel",
    bg: "#eef1f4",
    fg: "#647382",
  },
  {
    terms: ["tiktok"],
    iconKey: "tiktok",
    label: "Short video",
    bg: "#f1ecef",
    fg: "#7a6570",
  },
  {
    terms: ["video", "youtube", "creator", "content"],
    iconKey: "youtube",
    label: "Content studio",
    bg: "#f3eeee",
    fg: "#7b665f",
  },
  {
    terms: ["store", "shopify", "commerce", "ecommerce", "product", "order"],
    iconKey: "store",
    label: "Store ops",
    bg: "#f3efe7",
    fg: "#766b58",
  },
  {
    terms: ["support", "customer", "inbox", "ticket", "community"],
    iconKey: "chat",
    label: "Support desk",
    bg: "#eef1ec",
    fg: "#667569",
  },
  {
    terms: ["ai", "tech", "founder", "launch", "startup"],
    iconKey: "rocket",
    label: "Founder OS",
    bg: "#f2efe9",
    fg: "#706a60",
  },
  {
    terms: ["sales", "outreach", "pipeline", "revenue", "crm"],
    iconKey: "megaphone",
    label: "Revenue room",
    bg: "#f3efe9",
    fg: "#76685c",
  },
  {
    terms: ["engineering", "code", "developer", "software"],
    iconKey: "code",
    label: "Engineering",
    bg: "#eef0f1",
    fg: "#68727a",
  },
  {
    terms: ["research", "learning", "course", "training"],
    iconKey: "academicCap",
    label: "Research",
    bg: "#f0eee7",
    fg: "#716b5e",
  },
  {
    terms: ["brand", "website", "marketing", "campaign"],
    iconKey: "globe",
    label: "Growth",
    bg: "#f0f0e9",
    fg: "#6d705f",
  },
  {
    terms: ["compliance", "security", "policy", "approval"],
    iconKey: "shield",
    label: "Governance",
    bg: "#efefec",
    fg: "#6f6d68",
  },
  {
    terms: ["project", "launch", "operation", "ops"],
    iconKey: "checklist",
    label: "Operations",
    bg: "#f2efe9",
    fg: "#73695f",
  },
];

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Whether `term` occurs in `haystack` as a whole word (or word phrase), not
 * merely as a substring somewhere inside a longer word.
 *
 * @param {string} haystack already-lowercased
 * @param {string} term already-lowercased
 */
export function matchesWorkspaceTerm(haystack, term) {
  return new RegExp(`\\b${escapeRegExp(term)}\\b`).test(haystack);
}

/**
 * @param {{
 *   name?: string | null, description?: string | null, category?: string | null,
 *   kind?: string | null, identity_label?: string | null, property_type?: string | null,
 *   primary_work?: string | null, operating_context?: string | null,
 *   attribute_tags?: string[] | null,
 * }} ws
 */
export function workspaceHaystack(ws) {
  return [
    ws.name, ws.description, ws.category, ws.kind, ws.identity_label,
    ws.property_type, ws.primary_work, ws.operating_context,
    ...(ws.attribute_tags || []),
  ].filter(Boolean).join(" ").toLowerCase();
}

/**
 * @param {Parameters<typeof workspaceHaystack>[0]} ws
 * @returns {PresentationRule | null}
 */
export function matchWorkspacePresentationRule(ws) {
  const haystack = workspaceHaystack(ws);
  return (
    WORKSPACE_PRESENTATION_RULES.find((rule) =>
      rule.terms.some((term) => matchesWorkspaceTerm(haystack, term))
    ) || null
  );
}
