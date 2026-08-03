type ApprovalCopyInput = {
  prompt?: string;
  action?: string;
  tool?: string;
  hasWorkspace?: boolean;
  paths?: string[];
  content?: unknown;
  argsPreview?: unknown;
  operation?: unknown;
  /** `pending_action.hitl_type` — "input" | "review" | "authorize" |
   *  "choice" | "error". Absent on cards posted before the type shipped. */
  hitlType?: string;
  /** `pending_action.payload` — the typed copy that answers what / why /
   *  what-to-do. Absent on those same older cards. */
  payload?: unknown;
};

/** The three things every typed card must be able to say. Built from the
 *  request's own payload; `null` when the card carries no payload at all,
 *  which is the signal to fall back to prompt-based copy.
 *
 *  This exists because the prompt-based path could not say them. The
 *  dispatcher's synthesized reason ("Step requires operator approval before
 *  dispatching 'x'.") answers only "what", and answers it in internal terms —
 *  so the UI rewrote it to one fixed sentence, and the real failure underneath
 *  ("your local worker is offline") was destroyed on the way to the screen. */
export type StructuredApprovalCopy = {
  /** The one-line answer to "what is this". */
  headline: string;
  /** Supporting "why", when the payload carries one distinct from headline. */
  detail: string | null;
  /** The call to action — what the user should go do. */
  actionToTake: string | null;
  /** In-app route for the call to action (e.g. "/integrations"). */
  actionLink: string | null;
};

function payloadText(payload: Record<string, any>, key: string): string {
  const value = payload[key];
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
}

/**
 * Render copy from a typed HITL payload. Returns null when there is nothing
 * structured to render — callers then keep their existing prompt-based
 * behavior, so cards posted before the type system still look exactly as
 * they did.
 *
 * The headline is the type's own "what": `what_happened` for `error`,
 * `question` for `input`/`choice`, `action_description` for `authorize`.
 * `review` has no headline field of its own, so its `why` leads.
 */
export function structuredApprovalCopy(
  payload?: unknown,
): StructuredApprovalCopy | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, any>;
  const why = payloadText(record, "why");
  const headline =
    payloadText(record, "what_happened")
    || payloadText(record, "question")
    || payloadText(record, "action_description")
    || why;
  if (!headline) return null;
  const actionToTake = payloadText(record, "action_to_take");
  const rawLink = record.action_link;
  const actionLink =
    typeof rawLink === "string" && rawLink.trim() ? rawLink.trim() : null;
  return {
    headline,
    detail: why && why !== headline ? why : null,
    actionToTake: actionToTake || null,
    actionLink,
  };
}

/** True when this card is a failure report rather than a request for
 *  permission. An error card must never offer "Approve". */
export function isErrorHitlCard(hitlType?: string | null): boolean {
  return String(hitlType || "").toLowerCase() === "error";
}

const ACTION_LABELS: Record<string, string> = {
  create_document: "Create file",
  upload_document: "Save file",
  save_file: "Save file",
  write: "Create file",
  edit: "Update file",
  update: "Update file",
  delete: "Delete item",
  "workspace.file.create": "Create files in this workspace",
  "workspace.file.write": "Modify files in this workspace",
  "workspace.file.modify": "Modify files in this workspace",
  "workspace.file.delete": "Delete files from this workspace",
  "workspace.automation.create": "Create automation",
  "workspace.automation.delete": "Cancel automation",
  "workspace.automation.update": "Update automation",
  "workspace.automation.run": "Run automation",
  "workspace.operation.apply": "Apply workspace changes",
  "workspace.operation.draft": "Draft workspace changes",
  "workspace.operation.discard": "Discard workspace changes",
  "workspace.operation.update": "Update workspace changes",
  "email.delete": "Delete email",
  "email.send": "Send email",
  "external_message.send": "Send message",
  "social_post.publish": "Publish post",
  "social_post.delete": "Delete post",
  "social_post.mutate": "Update post",
  shell_modify: "Run a command",
  "cli.exec": "Run a command",
};

const PROMPT_REWRITES: Array<[RegExp, string]> = [
  [/^(mutate|modify|update)\s+workspace\s+file\s+via\s+cli$/i, "Modify files in this workspace"],
  [/^delete\s+workspace\s+file\s+via\s+cli$/i, "Delete files from this workspace"],
  [/^create\s+workspace\s+file\s+via\s+cli$/i, "Create files in this workspace"],
  [/^run\s+cli\s+command$/i, "Run a command"],
  // Backend < 2026-06-13 dumped the raw shell command into the prompt
  // ("Run shell command that may modify workspace files: python3 …"). New
  // backends return paths + a verb-aware short label; for older prompts
  // still in flight, swap the noisy raw command for the same
  // intent-based wording.
  [/^Run shell command that modifies:\s*/i, "Modify: "],
  [/^Run shell command that may modify workspace files:.*$/i, "Modify files in this workspace"],
];

export function friendlyApprovalActionLabel(action?: string): string {
  const normalized = normalizeAction(action);
  if (ACTION_LABELS[normalized]) return ACTION_LABELS[normalized];
  const words = normalized.replace(/[_.]+/g, " ").trim() || "change";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function friendlyApprovalToolLabel(tool?: string): string {
  const normalized = String(tool || "").trim();
  if (!normalized) return "Tool";
  const dispatchVerb = friendlyDispatchVerb(normalizeAction(normalized));
  if (dispatchVerb) return dispatchVerb.charAt(0).toUpperCase() + dispatchVerb.slice(1);
  const mcpMatch = normalized.match(/^mcp__([^_]+)__(.+)$/);
  const labelSource = mcpMatch
    ? `${mcpMatch[1]} ${mcpMatch[2]}`
    : normalized;
  const words = labelSource
    .replace(/[_.:-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return words
    ? words.replace(/\b\w/g, (letter) => letter.toUpperCase())
    : "Tool";
}

export function friendlyApprovalDescription(input: ApprovalCopyInput): string {
  // A typed payload knows what this card is about; the prompt only guesses.
  // When one is present it wins outright — no rewrite pass runs over it, so
  // nothing it says can be swallowed.
  const structured = structuredApprovalCopy(input.payload);
  if (structured) return structured.headline;
  const prompt = cleanPrompt(input.prompt);
  const rewritten = rewriteInternalPrompt(prompt);
  const paths = normalizePaths(input.paths);
  const action = normalizeAction(input.action);
  const actionLabel = friendlyApprovalActionLabel(input.action);
  const internalWorkspaceFilePrompt = isInternalWorkspaceFilePrompt(prompt);
  const hasWorkspace = Boolean(input.hasWorkspace);

  if (action.startsWith("workspace.file.") && (paths.length > 0 || !prompt || internalWorkspaceFilePrompt)) {
    return workspaceFileDescription(action, actionLabel, paths, hasWorkspace);
  }

  if (rewritten) return rewritten;
  if (prompt) return prompt;
  if (input.action) return actionLabel;
  return "Approval needed";
}

function normalizeAction(action?: string): string {
  return String(action || "change").toLowerCase().replace(/[-\s]+/g, "_");
}

function cleanPrompt(prompt?: string): string {
  return String(prompt || "").replace(/\s+/g, " ").trim();
}

// Internal dispatcher copy: "Step '<step_key>' requires operator approval
// before dispatching '<subject>'." Never surface the step key or the raw
// tool/action id — rewrite to a plain, user-facing sentence.
function friendlyDispatchVerb(tool: string): string | null {
  const key = String(tool || "").toLowerCase();
  if (/delete/.test(key)) return "delete a file";
  if (/modify|edit|update|mutate/.test(key)) return "edit a file";
  if (/file\.?write|write|create|save|upload|document/.test(key)) return "save a file";
  if (/email|external_message|message|reply|send/.test(key)) return "send a message";
  if (/social_post|publish|post/.test(key)) return "publish a post";
  if (/cli|shell|exec|command/.test(key)) return "run a command";
  if (/subagent|agent|step/.test(key)) return "run this step";
  return null;
}

// Prefer the human-readable object from the step key ("save_schedule_file" →
// "save the schedule file") so the prompt says WHICH file; fall back to the
// dispatched tool's generic verb. Never returns the raw key.
const STEP_VERB_RE =
  /^(save|write|create|update|delete|draft|generate|edit|modify|upload|send|publish|post|run|review|search|fetch|build|prepare|render|export)\b/;

function describeStepAction(
  stepKey?: string,
  tool?: string,
): string | null {
  const human = String(stepKey || "").replace(/[_.\-]+/g, " ").trim().toLowerCase();
  if (human && STEP_VERB_RE.test(human) && /\s/.test(human)) {
    // "save schedule file" → "save the schedule file"
    return human.replace(/^(\S+)\s+(.+)$/, "$1 the $2");
  }
  return friendlyDispatchVerb(String(tool || ""));
}

// The approval gate's fully-synthesized reasons. These sentences contain
// nothing but internal identifiers, so replacing them wholesale loses nothing.
// Anything that merely CONTAINS the dispatch phrasing is not one of these:
// the extra words are real content — the worker's actual failure, a policy
// rule's description — and get local redaction instead. Replacing those
// wholesale is exactly how "Manor could not reach the browser on your
// computer" became "This step needs your approval before it runs", fifteen
// times, for one operator whose local worker daemon was simply not running.
const DISPATCH_PROMPT_TEMPLATES: RegExp[] = [
  /^step(\s+['"`][\w.\-]+['"`])?\s+requires operator approval before dispatching\s+['"`]?[\w.\-]+['"`]?\.?$/i,
  /^high[\s-]?risk step needs one-time operator approval before dispatching\s+['"`]?[\w.\-]+['"`]?\.?$/i,
  /^step\s+['"`][\w.\-]+['"`]\s+is high[\s-]?risk and needs one-time operator approval before dispatching\s+['"`]?[\w.\-]+['"`]?\.?$/i,
];

/** Swap internal identifiers for human words IN PLACE, leaving every other
 *  word of the sentence intact. */
function redactInternalIdentifiers(prompt: string): string {
  const toolMatch = prompt.match(/dispatching\s+['"`]?([\w.\-]+)['"`]?/i);
  const stepMatch = prompt.match(/\bstep\s+['"`]([\w.\-]+)['"`]/i);
  let out = prompt;
  if (stepMatch) {
    const described = describeStepAction(stepMatch[1], toolMatch?.[1]);
    out = out.replace(
      stepMatch[0],
      described ? `The step to ${described}` : "This step",
    );
  }
  if (toolMatch) {
    const verb = friendlyDispatchVerb(toolMatch[1]);
    out = out.replace(
      toolMatch[0],
      `dispatching ${verb || friendlyApprovalActionLabel(toolMatch[1]).toLowerCase()}`,
    );
  }
  return out.replace(/\s+/g, " ").trim();
}

function rewriteInternalPrompt(prompt: string): string | null {
  if (!prompt) return null;
  for (const [pattern, replacement] of PROMPT_REWRITES) {
    if (pattern.test(prompt)) return replacement;
  }
  if (/\bapproval\s+before\s+dispatching\b/i.test(prompt)) {
    const isPureTemplate = DISPATCH_PROMPT_TEMPLATES.some((re) => re.test(prompt));
    if (!isPureTemplate) return redactInternalIdentifiers(prompt) || null;
    const stepMatch = prompt.match(/step\s+['"`]?([\w.\-]+)['"`]?/i);
    const toolMatch = prompt.match(/dispatching\s+['"`]?([\w.\-]+)['"`]?/i);
    const action = describeStepAction(stepMatch?.[1], toolMatch?.[1]);
    const highRisk = /\bhigh[\s-]?risk\b/i.test(prompt);
    const lead = action
      ? `Needs your approval to ${action}`
      : "This step needs your approval before it runs";
    return highRisk ? `${lead} — this is a high-impact action.` : `${lead}.`;
  }
  return prompt
    .replace(/\bvia\s+cli\b/gi, "")
    .replace(/\bmutate\b/gi, "modify")
    .replace(/\s+/g, " ")
    .trim() || null;
}

function isInternalWorkspaceFilePrompt(prompt: string): boolean {
  return /\bworkspace\s+file\s+via\s+cli\b/i.test(prompt);
}

function normalizePaths(paths?: string[]): string[] {
  return (paths || [])
    .map((path) => String(path || "").trim())
    .filter(Boolean);
}

function workspaceFileDescription(
  action: string,
  fallbackLabel: string,
  paths: string[],
  hasWorkspace: boolean,
): string {
  const verb = action.includes("delete")
    ? "Delete"
    : action.includes("create")
      ? "Create"
      : "Modify";
  const scope = hasWorkspace ? " in this workspace" : "";

  if (paths.length === 1) {
    return `${verb} ${fileName(paths[0])}${scope}`;
  }
  if (paths.length > 1) {
    return `${verb} ${paths.length} files${scope}`;
  }
  if (!hasWorkspace && fallbackLabel.endsWith(" in this workspace")) {
    return fallbackLabel.replace(/\s+in this workspace$/i, "");
  }
  return fallbackLabel;
}

function fileName(path: string): string {
  const trimmed = String(path || "").trim();
  if (!trimmed || [".", "./", "/"].includes(trimmed)) return "files";
  return trimmed.split(/[\\/]/).filter(Boolean).pop() || trimmed;
}
