import { t } from "./i18n";
import type { Task, TaskBlocker, TaskLog } from "./types";

const BLOCKER_KINDS = new Set([
  "capability_unavailable", "tool_missing", "connection_required", "approval_required", "policy_denied",
]);

/** Only typed runtime facts establish a blocker; free-form error prose never does. */
export function parseTaskBlocker(value: unknown): TaskBlocker | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  if (!BLOCKER_KINDS.has(String(data.kind)) || !["planning", "execution"].includes(String(data.stage))) return null;
  return {
    ...data,
    kind: data.kind as TaskBlocker["kind"],
    stage: data.stage as TaskBlocker["stage"],
    affected_agents: Array.isArray(data.affected_agents)
      ? data.affected_agents.filter((agent) => agent && typeof agent === "object")
      : [],
    missing_capabilities: Array.isArray(data.missing_capabilities)
      ? data.missing_capabilities.filter((capability): capability is string => typeof capability === "string")
      : [],
  };
}

/** A historical event can remain a setup blocker after the Task has recovered. */
export function taskBlockerFromLog(log: Pick<TaskLog, "id" | "meta">, task?: Task): TaskBlocker | null {
  const recorded = parseTaskBlocker(log.meta?.blocker);
  if (recorded) return recorded;
  if (log.meta?.capability_gap !== true) return null;
  const current = parseTaskBlocker(task?.current_blocker);
  if (current?.log_id === log.id) return current;
  return {
    kind: "capability_unavailable",
    stage: "planning",
    affected_agents: [],
    missing_capabilities: Array.isArray(log.meta.missing_capabilities)
      ? log.meta.missing_capabilities.filter((item: unknown): item is string => typeof item === "string")
      : [],
    log_id: log.id,
  };
}

/** Current state is API-owned and disappears as soon as this Task leaves blocked. */
export function currentTaskBlocker(task?: Task): TaskBlocker | null {
  return task?.status === "blocked" ? parseTaskBlocker(task.current_blocker) : null;
}

const CAPABILITY_LABEL_KEYS: Record<string, string> = {
  "web.safe_search": "component.task_blocker.web_search",
  "file.write": "component.task_blocker.file_write",
};

export function taskBlockerView(blocker: TaskBlocker) {
  const agents = [...new Set(blocker.affected_agents
    .map((agent) => typeof agent.agent_name === "string" ? agent.agent_name.trim() : "")
    .filter(Boolean))];
  const knownCapabilities = [...new Set(blocker.missing_capabilities
    .map((id) => CAPABILITY_LABEL_KEYS[id])
    .filter(Boolean)
    .map((key) => t(key)))];
  const capabilityLabel = knownCapabilities.length === blocker.missing_capabilities.length && knownCapabilities.length
    ? knownCapabilities.join(", ")
    : t("component.task_blocker.required_capability");
  const label = t(`component.task_blocker.${blocker.kind}`);
  return {
    label,
    subject: agents.join(" · "),
    description: t(`component.task_blocker.reason_${blocker.kind}`).replace("{capability}", capabilityLabel),
    planningNote: blocker.stage === "planning" ? t("component.task_blocker.before_execution") : "",
    status: blocker.kind === "policy_denied" ? "blocked" : blocker.kind === "approval_required" ? "waiting_on_customer" : "needs_setup",
  };
}

/** Navigation only. Approval remains on the original server-bound request. */
export function taskBlockerAction(blocker: TaskBlocker, workspaceId?: string) {
  if (blocker.kind === "policy_denied") return workspaceId ? {
    label: t("component.task_blocker.view_rules"),
    href: `/workspaces/${encodeURIComponent(workspaceId)}?tab=rules`,
  } : null;
  if (workspaceId && blocker.operation_draft_id) return {
    label: t("component.task_blocker.review_configuration"),
    href: `/chat?workspace=${encodeURIComponent(workspaceId)}`,
  };
  if (blocker.kind === "approval_required") return workspaceId && blocker.request_id ? {
    label: t("component.task_blocker.review_approval"),
    href: `/chat?workspace=${encodeURIComponent(workspaceId)}`,
  } : null;
  if (blocker.kind === "connection_required") return {
    label: t("component.task_blocker.connect_app"), href: "/integrations",
  };
  if (!workspaceId) return null;
  return {
    label: t("component.task_blocker.view_capabilities"),
    href: `/workspaces/${encodeURIComponent(workspaceId)}?tab=capabilities`,
  };
}

export function canRetryTaskBlocker(blocker: TaskBlocker): boolean {
  return !blocker.operation_draft_id && ["capability_unavailable", "tool_missing", "connection_required"].includes(blocker.kind);
}
