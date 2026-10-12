import {
  WORKSPACE_LUCIDE_ICON_NAMES,
  WORKSPACE_MAIN_ICON_NAMES,
  WORKSPACE_SEMANTIC_ICON_POOLS,
} from "./workspace-icon-catalog.mjs";

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
    terms: ["youtube"],
    iconKey: "youtube",
    label: "YouTube studio",
    bg: "#f3eeee",
    fg: "#7b665f",
  },
  {
    terms: ["restaurant", "cafe", "café", "dining", "catering", "hospitality", "food service"],
    iconKey: "facilities",
    label: "Hospitality",
    bg: "#f3eee7",
    fg: "#796b5d",
  },
  {
    terms: ["finance", "financial", "accounting", "bookkeeping", "budget", "investment", "portfolio", "cash flow"],
    iconKey: "dollar",
    label: "Finance",
    bg: "#eef1eb",
    fg: "#65745f",
  },
  {
    terms: ["recruiting", "recruitment", "hiring", "talent", "human resources", "hr"],
    iconKey: "userPlus",
    label: "People ops",
    bg: "#f1edef",
    fg: "#75676e",
  },
  {
    terms: ["legal", "law", "contract", "attorney", "counsel"],
    iconKey: "scale",
    label: "Legal",
    bg: "#f1efe9",
    fg: "#726c60",
  },
  {
    terms: ["healthcare", "health", "medical", "clinic", "wellness", "fitness"],
    iconKey: "heart",
    label: "Health",
    bg: "#f3eeee",
    fg: "#7c6666",
  },
  {
    terms: ["logistics", "shipping", "delivery", "fulfillment", "supply chain", "warehouse"],
    iconKey: "truck",
    label: "Logistics",
    bg: "#eef0ee",
    fg: "#66736b",
  },
  {
    terms: ["home", "residential", "interior", "renovation", "house"],
    iconKey: "home",
    label: "Home services",
    bg: "#f3efe9",
    fg: "#756b5e",
  },
  {
    terms: ["leasing", "lease", "property", "real estate", "rent", "occupancy", "tenant"],
    iconKey: "building",
    label: "Property ops",
    bg: "#f2eee8",
    fg: "#75695e",
  },
  {
    terms: ["design", "creative", "visual", "illustration", "branding"],
    iconKey: "palette",
    label: "Creative studio",
    bg: "#f2edf0",
    fg: "#766873",
  },
  {
    terms: ["podcast", "audio", "music", "voice", "narration"],
    iconKey: "musicNote",
    label: "Audio studio",
    bg: "#eeeff3",
    fg: "#676d7b",
  },
  {
    terms: ["video", "film", "animation", "motion", "studio", "creator", "content"],
    iconKey: "presentation",
    label: "Content studio",
    bg: "#f1edef",
    fg: "#756871",
  },
  {
    terms: ["newsletter", "writing", "editorial", "blog", "article", "copywriting", "publishing"],
    iconKey: "document",
    label: "Editorial",
    bg: "#f1f0eb",
    fg: "#716e63",
  },
  {
    terms: ["analytics", "analysis", "reporting", "dashboard", "insights", "metrics", "data"],
    iconKey: "report",
    label: "Analytics",
    bg: "#edf0f1",
    fg: "#66737a",
  },
  {
    terms: ["event", "events", "calendar", "scheduling", "booking", "appointments"],
    iconKey: "calendar",
    label: "Scheduling",
    bg: "#f2efeb",
    fg: "#746b62",
  },
  {
    terms: ["qa", "smoke", "test", "runtime", "regression"],
    iconKey: "beaker",
    label: "QA runtime",
    bg: "#edf1ee",
    fg: "#65786e",
  },
  {
    terms: ["store", "shopify", "commerce", "ecommerce", "retail", "checkout", "order"],
    iconKey: "store",
    label: "Store ops",
    bg: "#f3efe7",
    fg: "#766b58",
  },
  {
    terms: ["product", "catalog", "inventory", "sku", "merchandise"],
    iconKey: "box",
    label: "Product ops",
    bg: "#f1eee8",
    fg: "#736a5e",
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
    terms: ["marketing", "campaign", "growth", "seo", "audience", "acquisition"],
    iconKey: "trendingUp",
    label: "Growth",
    bg: "#eef1ec",
    fg: "#667565",
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
    terms: ["website", "web", "localization", "global"],
    iconKey: "globe",
    label: "Web operations",
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
 * Assign a durable visual identity without persisting display-only metadata.
 * Real workspaces use their immutable id, while preview/Blueprint callers
 * fall back to the descriptive fields they already provide.
 *
 * @param {Parameters<typeof workspaceHaystack>[0] & { id?: string | null }} ws
 */
function workspaceMainIconHash(ws) {
  const seed = String(ws.id || workspaceHaystack(ws) || "workspace").toLowerCase();
  let hash = 2166136261;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

/**
 * Select one actual main glyph. Known Workspace categories use a relevant
 * semantic pool; uncategorized Workspaces use the full Lucide catalogue.
 *
 * @param {Parameters<typeof workspaceHaystack>[0] & { id?: string | null }} ws
 * @param {string | null | undefined} semanticIconKey
 * @returns {string}
 */
export function workspaceMainIconName(ws, semanticIconKey) {
  const pool = semanticIconKey
    ? WORKSPACE_SEMANTIC_ICON_POOLS[semanticIconKey]
    : null;
  const candidates = pool?.length ? pool : WORKSPACE_LUCIDE_ICON_NAMES;
  return candidates[workspaceMainIconHash(ws) % candidates.length];
}

/**
 * @param {Parameters<typeof workspaceHaystack>[0] & { id?: string | null }} ws
 * @param {string | null | undefined} semanticIconKey
 */
export function workspaceMainIconIndex(ws, semanticIconKey) {
  return WORKSPACE_MAIN_ICON_NAMES.indexOf(
    workspaceMainIconName(ws, semanticIconKey),
  );
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
