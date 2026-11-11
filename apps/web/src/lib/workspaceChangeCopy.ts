/**
 * A Workspace change card in the words of the person approving it. The draft
 * records internal field names (`operating_model`, `agent_mappings`); those
 * never reach the card.
 */
type Rec = Record<string, unknown>;

const record = (value: unknown): Rec => (value && typeof value === "object" && !Array.isArray(value) ? value as Rec : {});

const GROUP_BY_KEY: Record<string, string> = {
  services: "work",
  processes: "work",
  agent_mappings: "work",
  immediate_tasks: "work",
  goals: "goals",
  rules: "rules",
  governance_policy: "rules",
  capability_bindings: "connections",
  skill_bindings: "connections",
  integration_scope: "connections",
  channel_config: "connections",
  automations: "automations",
  workflow_attachments: "automations",
  heartbeat_policy: "automations",
  strategist: "automations",
  knowledge_policy: "materials",
  budget_policy: "budget",
};
const GROUP_ORDER = ["work", "goals", "rules", "connections", "automations", "materials", "budget", "other"];
const NAMED_SECTIONS = ["services", "goals", "processes", "automations"];
const MAX_NAMES = 5;

function rowIdentity(row: Rec): string {
  return String(row.service_key || row.goal_key || row.process_key || row.job_id || row.key || row.id || row.name || row.title || "");
}

function namedRows(rows: unknown): Map<string, string> {
  const named = new Map<string, string>();
  for (const value of Array.isArray(rows) ? rows : []) {
    const row = record(value);
    const name = String(row.name || row.title || "").trim();
    if (name) named.set(rowIdentity(row), name);
  }
  return named;
}

export interface WorkspaceChangeCopy {
  pageOnly: boolean;
  /** Which kinds of thing change, as group names to translate. */
  groups: string[];
  added: string[];
  removed: string[];
}

export function workspaceChangeCopy(operation: Rec): WorkspaceChangeCopy {
  const patches = Array.isArray(operation.patches) ? operation.patches : [];
  const pageOnly = patches.length > 0 && patches.every((patch) => record(patch).op === "app_view.update");
  const keys = Array.isArray(operation.changed_keys) ? operation.changed_keys.map(String) : [];
  const groups = new Set(keys.map((key) => GROUP_BY_KEY[key] || "other"));
  const changes = record(record(operation.diff).changes);
  const added: string[] = [];
  const removed: string[] = [];
  for (const section of NAMED_SECTIONS) {
    const change = record(changes[section]);
    const before = namedRows(change.before);
    const after = namedRows(change.after);
    for (const [key, name] of after) if (!before.has(key)) added.push(name);
    for (const [key, name] of before) if (!after.has(key)) removed.push(name);
  }
  return {
    pageOnly,
    groups: GROUP_ORDER.filter((group) => groups.has(group)),
    added: added.slice(0, MAX_NAMES),
    removed: removed.slice(0, MAX_NAMES),
  };
}

/** Says something about the Workspace, not about this change: nothing to weigh when approving. */
export function isUnrelatedReviewWarning(issue: unknown): boolean {
  return /no persistent runtime rules/i.test(String(record(issue).message || issue || ""));
}
