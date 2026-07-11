const KNOWN_KEY_LABELS: Record<string, string> = {
  approval_queue_avg_hours: "approval queue average hours",
  approval_decision: "approval decision",
  approve_proposals: "review proposals",
  brand_voice_doc: "brand voice document",
  compile_draft_document: "compile draft document",
  document_create: "create document",
  draft_brand_voice_doc: "draft brand voice document",
  draft_tour_invitations: "draft tour invitations",
  email_send: "send email",
  external_message_send: "send external message",
  extract_high_intent_leads: "extract high-intent leads",
  followup_drafting: "Leasing Follow-Up Drafter",
  lead_intake_qualification: "Leasing Lead Qualifier",
  occupancy_rate: "occupancy rate",
  pipeline_reporting: "Leasing Pipeline Analyst",
  publish_tweet: "publish tweet",
  qualification_rate: "qualification rate",
  social_post_publish: "publish social post",
  stale_lead_count: "stale lead count",
  subagent: "agent",
  send_external_message: "send external message",
  output_validation: "output check",
  plan_approval: "plan approval",
  tour_conversion_rate: "tour conversion rate",
  tour_scheduling: "Tour Scheduling Coordinator",
  unit_matching: "Unit Match Advisor",
  workspace_file_create: "create workspace file",
  workspace_file_delete: "delete workspace file",
  workspace_file_modify: "edit workspace file",
  workspace_request_strategist_review: "Request Manor AI review",
  workspace_update_task_runtime: "task guidance update",
  waiting_on_customer: "waiting for input",
  write_report_file: "save report file",
  generate_pipeline_report: "generate pipeline report",
  search_workspace_context: "review workspace context",
};

const FILE_LABELS: Record<string, string> = {
  "LEARNINGS.md": "workspace learning notes",
  "RULES.md": "workspace rules",
  "TOOLS.md": "workspace tool guide",
};

const ACRONYMS = new Set(["ai", "api", "cli", "csv", "doc", "faq", "html", "id", "json", "mcp", "pdf", "ppt", "pptx", "qa", "sla", "sms", "url", "xls", "xlsx"]);
const LOWERCASE_TITLE_WORDS = new Set(["and", "as", "for", "in", "of", "or", "to", "with"]);
const SUMMARY_KEYS = [
  "summary",
  "result_summary",
  "final_summary",
  "message",
  "user_message",
  "text",
  "content",
  "answer",
  "final_message",
  "final",
  "response",
  "reply",
  "question",
  "prompt",
  "reason",
  "error_message",
  "error",
  "stderr",
  "stdout",
  "output",
  "result",
];
const COLLECTION_KEYS = ["tasks", "items", "results", "outputs", "files", "artifacts", "attachments", "documents", "steps"];
const INTERNAL_SUMMARY_KEYS = new Set([
  "id",
  "task_id",
  "plan_id",
  "step_id",
  "worker_id",
  "agent_id",
  "subscription_id",
  "workspace_id",
  "entity_id",
  "user_id",
  "conversation_id",
  "thread_id",
  "request_id",
  "run_id",
  "session_id",
  "trace_id",
  "raw",
  "raw_type",
  "meta",
  "metadata",
  "params",
  "arguments",
  "args",
  "payload",
  "data",
]);

export function isAutomationIdentity(value?: string | null): boolean {
  const text = String(value || "").trim();
  if (!text) return false;
  return /\bcodex_auto_\d+\b/i.test(text) || /^automation[_-]user/i.test(text);
}

function titleWord(word: string, index = 0): string {
  const lower = word.toLowerCase();
  if (ACRONYMS.has(lower)) return lower.toUpperCase();
  if (index > 0 && LOWERCASE_TITLE_WORDS.has(lower)) return lower;
  if (lower === "followup") return "Follow-up";
  if (lower === "high-intent") return "High-intent";
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

function humanizeKey(value: string, titleCase = false): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const normalized = raw.replace(/[.-]+/g, "_");
  const known = KNOWN_KEY_LABELS[normalized] || KNOWN_KEY_LABELS[raw];
  const base = known || raw.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  if (!titleCase) {
    return base.replace(/\b(ai|api|cli|csv|doc|faq|html|id|json|mcp|pdf|ppt|pptx|qa|sla|sms|url|xls|xlsx)\b/gi, (part) => part.toUpperCase());
  }
  return base.split(/\s+/).map((word, index) => titleWord(word, index)).join(" ");
}

function looksJsonish(value: string): boolean {
  const text = value.trim();
  return (
    (text.startsWith("{") && text.endsWith("}")) ||
    (text.startsWith("[") && text.endsWith("]")) ||
    /^```(?:json)?\s*[\[{]/i.test(text)
  );
}

function stripJsonFence(value: string): string {
  return value
    .trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .trim();
}

function tryParseJsonish(value: string): unknown | null {
  const text = stripJsonFence(value);
  if (!looksJsonish(text)) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function isOpaqueIdLike(value: unknown): boolean {
  const text = String(value || "").trim();
  return /^01[A-Z0-9]{20,}$/i.test(text) || /^[a-z]+_[A-Za-z0-9_-]{16,}$/.test(text);
}

function basename(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) return "";
  if (/^https?:\/\//i.test(text)) {
    try {
      const url = new URL(text);
      const last = url.pathname.split("/").filter(Boolean).pop();
      return last || url.hostname;
    } catch {
      return text;
    }
  }
  return text.replace(/\\/g, "/").split("/").filter(Boolean).pop() || text;
}

function scalarPreview(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  if (typeof value === "string") {
    const parsed = tryParseJsonish(value);
    if (parsed !== null) return structuredText(parsed);
    const text = value.trim();
    if (!text || isOpaqueIdLike(text)) return "";
    return formatUserFacingText(text);
  }
  return structuredText(value);
}

function fileLabel(value: any, index = 0): string {
  if (!value || typeof value !== "object") return scalarPreview(value) || `File ${index + 1}`;
  const label =
    value.name ||
    value.filename ||
    value.original_name ||
    value.title ||
    value.fs_path ||
    value.saved_to ||
    value.path ||
    value.file_url ||
    value.document_url ||
    value.public_url ||
    value.url ||
    value.document_id;
  return formatUserFacingText(basename(label) || value.type || `File ${index + 1}`);
}

function itemLabel(value: any, index: number, fallback: string): string {
  if (value === null || value === undefined) return "";
  if (typeof value !== "object") return scalarPreview(value);
  const label =
    value.title ||
    value.name ||
    value.task_title ||
    value.step_title ||
    value.summary ||
    value.result_summary ||
    value.message ||
    value.text ||
    value.kind ||
    value.type ||
    value.status;
  const rendered = scalarPreview(label);
  return rendered || `${fallback} ${index + 1}`;
}

function structuredList(label: string, values: unknown[], fallbackItem: string): string {
  const rows = values
    .slice(0, 6)
    .map((item, index) => `- ${itemLabel(item, index, fallbackItem)}`)
    .filter((row) => row.trim() !== "-");
  if (values.length > 6) rows.push(`- ${values.length - 6} more`);
  return rows.length ? `**${label}**\n${rows.join("\n")}` : "";
}

function structuredText(value: unknown, depth = 0): string {
  if (value === null || value === undefined || depth > 3) return "";
  if (typeof value === "string") {
    const parsed = tryParseJsonish(value);
    if (parsed !== null) return structuredText(parsed, depth + 1);
    return formatUserFacingText(value);
  }
  if (typeof value === "number" || typeof value === "boolean") return scalarPreview(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "";
    const lines = value
      .slice(0, 8)
      .map((item, index) => `- ${itemLabel(item, index, "Item")}`)
      .filter((line) => line.trim() !== "-");
    if (value.length > 8) lines.push(`- ${value.length - 8} more`);
    return lines.join("\n");
  }
  if (typeof value !== "object") return "";

  const obj = value as Record<string, any>;
  for (const key of SUMMARY_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(obj, key)) continue;
    const candidate = structuredText(obj[key], depth + 1).trim();
    if (candidate) return candidate;
  }

  if (Array.isArray(obj.files) || Array.isArray(obj.artifacts) || Array.isArray(obj.attachments) || Array.isArray(obj.documents)) {
    const files = (obj.files || obj.artifacts || obj.attachments || obj.documents) as unknown[];
    const rows = files
      .slice(0, 6)
      .map((file, index) => `- ${fileLabel(file, index)}`)
      .filter((row) => row.trim() !== "-");
    if (files.length > 6) rows.push(`- ${files.length - 6} more`);
    if (rows.length) return `**Files**\n${rows.join("\n")}`;
  }

  for (const key of COLLECTION_KEYS) {
    if (!Array.isArray(obj[key]) || obj[key].length === 0) continue;
    const label = formatUserFacingLabel(key);
    const item = key === "steps" ? "Step" : key === "tasks" ? "Task" : key === "files" ? "File" : "Item";
    const rendered = structuredList(label, obj[key], item);
    if (rendered) return rendered;
  }

  const rows: string[] = [];
  for (const [key, rawValue] of Object.entries(obj)) {
    if (rows.length >= 6) break;
    if (INTERNAL_SUMMARY_KEYS.has(key) || rawValue === null || rawValue === undefined || rawValue === "") continue;
    if (Array.isArray(rawValue) || typeof rawValue === "object") continue;
    const rendered = scalarPreview(rawValue);
    if (!rendered) continue;
    rows.push(`- **${formatUserFacingLabel(key)}:** ${rendered}`);
  }
  return rows.join("\n");
}

export function formatUserFacingLabel(value?: string | null): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (isAutomationIdentity(raw)) return "Workspace automation";
  return humanizeKey(raw, true);
}

export function friendlyPersonName(value?: string | null, fallback = "Team member"): string {
  const raw = String(value || "").trim();
  if (!raw) return fallback;
  if (isAutomationIdentity(raw)) return "Workspace automation";
  if (/^01[A-Z0-9]{20,}$/i.test(raw)) return fallback;
  return raw.replace(/\bcodex_auto_\d+\b/gi, "Workspace automation").replace(/\s+/g, " ").trim();
}

export function formatUserFacingText(value?: string | null): string {
  let text = String(value || "").trim();
  if (!text) return "";

  // Never surface an unresolved plan-ref / template expression
  // (e.g. "${{ steps.generate_video.result.file_url }}").
  text = text.replace(/\$?\{\{[^}]*\}\}/g, "…");
  // Strip the internal HITL header ("⚠ Need your input on step `step_key`:").
  text = text.replace(
    /^\W*needs?\s+your\s+input\s+on\s+(?:step\s+)?`?[^`\n:：]+`?\s*[:：]\s*\n*/i,
    "",
  );

  Object.entries(FILE_LABELS).forEach(([file, label]) => {
    text = text.replace(new RegExp(file.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g"), label);
  });

  text = text
    .replace(/\bcodex_auto_\d+\b/gi, "Workspace automation")
    .replace(/\bWorkspace Request Strategist Review\b/gi, "Request Manor AI review")
    .replace(/\bworkspace request strategist review\b/gi, "Request Manor AI review")
    .replace(/\bRequest Strategist Review\b/gi, "Request Manor AI review")
    .replace(/\bStrategist proposal\b/gi, "Workspace proposal")
    .replace(/\bworkspace_agent[.:_-]/gi, "Workspace AI ")
    .replace(/\bworkspace agent\b/gi, "Workspace AI")
    .replace(/\bstrategist\b/gi, "Manor AI")
    .replace(/\bPPT Master\b/g, "presentation builder")
    .replace(/\bknowledge nets\b/gi, "knowledge collections")
    .replace(/\bknowledge net\b/gi, "knowledge collection")
    .replace(/\blearning candidates?\b/gi, "learning suggestions")
    .replace(/\bNo external integrations are configured\b/gi, "No connected apps are set up")
    .replace(/\bexternal integrations\b/gi, "connected apps")
    .replace(/\bintegrations are configured\b/gi, "connected apps are set up")
    .replace(/\bHITL[-\s]?required\b/gi, "approval required")
    .replace(/\bNever[-\s]?allow\b/gi, "blocked")
    .replace(/\bgovernance policy\b/gi, "workspace rules")
    .replace(/\bsolo operator\b/gi, "workspace owner")
    .replace(/\boperator review\b/gi, "your review")
    .replace(/\boperator confirms\b/gi, "you confirm")
    .replace(/\boperator flagged\b/gi, "you flagged")
    .replace(/\boperator\b/gi, "you")
    .replace(/\bre[-\s]?triggering\b/gi, "restarting")
    .replace(/\bre[-\s]?seeding\b/gi, "refreshing")
    .replace(/\bautonomously\b/gi, "without help")
    .replace(/\bworkspace_update_task_runtime\b/gi, "task guidance update")
    .replace(/\bWorkspace Update Task Runtime\b/gi, "Task guidance update")
    .replace(/\bworkspace update task runtime\b/gi, "task guidance update")
    .replace(/\bworkspace\s+runtime\b/gi, "workspace work history")
    .replace(/\bworker\s+runtime\b/gi, "agent run")
    .replace(/\bruntime\s+requirements?\b/gi, "task constraints")
    .replace(/\bruntime\s+requirement\b/gi, "task constraint")
    .replace(/\bruntime\b/gi, "run")
    .replace(/\bworker\b/gi, "agent")
    .replace(/\bexception reviews?\b/gi, "items needing review")
    .replace(/\bexceptions\b/gi, "special cases")
    .replace(/\bexception\b/gi, "special case")
    .replace(/\bkeywords?\b/gi, "search terms")
    .replace(/\bMCP\b/g, "connector")
    .replace(/\bplan executor\b/gi, "planner")
    .replace(/\bOutputSchemaError\b/g, "output format error");

  text = text
    .replace(/\bchoice=approve_all\b/gi, "approved all")
    .replace(/\bchoice=reject_all\b/gi, "rejected all")
    .replace(/\bchoice=request_changes\b/gi, "requested changes")
    .replace(/\bchoice=approve\b/gi, "approved")
    .replace(/\bchoice=reject\b/gi, "rejected")
    .replace(/\bstatus=([a-z0-9_-]+)\b/gi, (_match, status) => `status: ${humanizeKey(String(status))}`)
    .replace(/\bstop_reason=([^.\n]+)\.\s*/gi, (_match, reason) => `${formatUserFacingText(String(reason))}. `)
    .replace(/\berror=([^.\n]+)/gi, (_match, reason) => `Error: ${formatUserFacingText(String(reason))}`)
    .replace(/\bstep\(s\)/gi, "steps")
    .replace(/\btask\(s\)/gi, "tasks")
    .replace(/\bproposal\(s\)/gi, "proposals")
    .replace(/\bsignal\(s\)/gi, "signals")
    .replace(/\bpolicy violation\(s\)/gi, "policy violations")
    .replace(/\bapproval-gated\b/gi, "approval-required")
    .replace(/\b1 ([a-z][a-z -]*?) steps\b/gi, "1 $1 step")
    .replace(/\b1 ([a-z][a-z -]*?) tasks\b/gi, "1 $1 task")
    .replace(/\b1 steps\b/gi, "1 step")
    .replace(/\b1 tasks\b/gi, "1 task")
    .replace(/\b1 proposals\b/gi, "1 proposal")
    .replace(/\b1 signals\b/gi, "1 signal")
    .replace(/\b1 policy violations\b/gi, "1 policy violation")
    .replace(/\b1 ([a-z][a-z -]*?) task were\b/gi, "1 $1 task was")
    .replace(/\bUrl:\s*/g, "File: ")
    .replace(/\bSearch Workspace Context\b/g, "Review workspace context")
    .replace(/\bGenerate Pipeline Report\b/g, "Generate pipeline report")
    .replace(/\bWrite Report File\b/g, "Save report file")
    .replace(/\bsearch workspace context\b/g, "review workspace context")
    .replace(/\bgenerate pipeline report\b/g, "generate pipeline report")
    .replace(/\bwrite report file\b/g, "save report file")
    .replace(/Workspaces\/[^\n\r]+\/documents\/([^\n\r]+)/g, "$1");

  text = text
    .replace(/\bthe you\b/gi, "you")
    .replace(/\byou takes\b/gi, "you take")
    .replace(/\byou confirms\b/gi, "you confirm")
    .replace(/\byou flagged\b/gi, "you flagged")
    .replace(/\bworkspace rules is\b/gi, "workspace rules are")
    .replace(/\buntil workspace rules are reconciled by you\b/gi, "until you update the workspace rules")
    .replace(/\bseeded\b/gi, "added");

  text = text.replace(/\b[a-z][a-z0-9]+(?:[._-][a-z0-9]+)+\b/g, (match) => {
    if (/^https?:\/\//i.test(match)) return match;
    const normalized = match.replace(/[.-]+/g, "_");
    if (KNOWN_KEY_LABELS[normalized] || KNOWN_KEY_LABELS[match]) return humanizeKey(match);
    if (match.includes(".") && /\.[a-z0-9]{2,6}$/i.test(match)) return match;
    return humanizeKey(match);
  });

  return text;
}

export function formatUserFacingStructuredText(value?: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") {
    const parsed = tryParseJsonish(value);
    if (parsed !== null) {
      return structuredText(parsed) || formatUserFacingText(value);
    }
    return formatUserFacingText(value);
  }
  return structuredText(value) || formatUserFacingText(String(value || ""));
}

export function formatTaskDescriptionForDisplay(description?: string | null): string {
  const text = formatUserFacingText(description);
  if (!text) return "";
  const alreadyStructured = /(^|\n)\s*(-|\*|\d+\.)\s+/.test(text) || text.includes("\n\n");
  const firstNumberedItem = text.search(/\(\d+\)\s+/);
  if (alreadyStructured || firstNumberedItem < 0) {
    return text;
  }

  const intro = text.slice(0, firstNumberedItem).trim().replace(/[:：]\s*$/, "");
  const numbered = text.slice(firstNumberedItem)
    .split(/(?=\(\d+\)\s+)/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (numbered.length < 2) return text;

  const items = numbered.map((item, index) => {
    const cleaned = item
      .replace(/^\(\d+\)\s*/, "")
      .replace(/\s+Deliverable:\s*/i, "\n\n**Deliverable:** ")
      .replace(/[;；]\s*$/, "")
      .trim();
    return `**${index + 1}.** ${cleaned}`;
  });
  return [intro, "", ...items].filter(Boolean).join("\n");
}
