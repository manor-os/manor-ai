/**
 * Global constants — single source of truth.
 * Mirrors packages/core/constants/agents.py for the frontend.
 */

// ── Manor Master Agent ──
/** The canonical agent_id for the Manor master agent */
export const MANOR_AGENT_ID = "manor-master";
/** The agent_type value stored on tasks/channels */
export const MANOR_AGENT_TYPE = "manor_agent";
/** Display name for the master agent */
export const MANOR_AGENT_NAME = "Manor AI";

/** Check if an agent_id or agent_type refers to the Manor master agent */
export function isMasterAgent(agentId?: string | null, agentType?: string | null): boolean {
  if (agentType === MANOR_AGENT_TYPE) return true;
  if (agentId === MANOR_AGENT_ID || agentId === "master") return true;
  return false;
}

// ── Legacy agent-author placeholders ──
/** Strings that landed in ``created_by`` where the writer had an agent and
 *  failed to record it. Not a kind of actor: every one of these rows was
 *  produced by a determinate agent. Nothing writes them any more — see
 *  LEGACY_AGENT_AUTHOR_PLACEHOLDERS in packages/core/constants/agents.py for
 *  what each site records instead. They resolve here so rows already in the
 *  database stop rendering as a person named "workspace-agent". */
export const LEGACY_AGENT_AUTHOR_PLACEHOLDERS: ReadonlySet<string> = new Set([
  "workspace-agent",
  "ai-agent",
  "ai agent",
  "ai supervisor",
  "agent",
]);

/** True for a stored author string that stands in for an agent it failed to
 *  name. */
export function isLegacyAgentAuthorPlaceholder(value?: string | null): boolean {
  return LEGACY_AGENT_AUTHOR_PLACEHOLDERS.has(String(value || "").trim().toLowerCase());
}

// ── Who acted on a task ──
/** The closed set of things that can produce a task log entry. Mirrors
 *  TaskActor in packages/core/constants/task_actors.py, which documents what
 *  writes each one. Stamped at write time into the log's metadata, so the
 *  author is read rather than inferred from a display string. */
export const TASK_ACTORS = {
  /** A person — assignee, workspace owner, teammate. */
  USER: "user",
  /** A specific workspace agent, identified by its subscription. */
  AGENT: "agent",
  /** The Manor master agent acting as the workspace agent. */
  MANOR: "manor",
  /** The plan supervisor — judges step results, not the one that ran them. */
  SUPERVISOR: "supervisor",
  /** The platform itself: state machine, schedulers, reminders, retries. */
  SYSTEM: "system",
  /** An external portal client commenting on their own request. */
  CLIENT: "client",
} as const;

export type TaskActor = (typeof TASK_ACTORS)[keyof typeof TASK_ACTORS];

/** Metadata key carrying the actor kind on a task log. */
export const TASK_ACTOR_META_KEY = "actor_kind";

/** Read the declared actor off a task log, or null for rows written before
 *  the kind was recorded — those still go through the legacy name-matching. */
export function taskActorFromLog(log: any): TaskActor | null {
  const raw = log?.meta?.[TASK_ACTOR_META_KEY] ?? log?.metadata?.[TASK_ACTOR_META_KEY];
  const value = String(raw || "");
  return (Object.values(TASK_ACTORS) as string[]).includes(value)
    ? (value as TaskActor)
    : null;
}
