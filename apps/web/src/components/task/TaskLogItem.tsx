/**
 * TaskLogItem — shared comment / log row for both the task drawer
 * (Tasks.tsx) and the standalone full view (TaskDetail.tsx).
 *
 * Handles every log_type the API may emit:
 *   - status_change       → muted icon row with italic content
 *   - ai_*                → green agent avatar with badge per kind
 *   - evaluation          → amber star avatar
 *   - default (comment)   → user avatar with markdown content
 *
 * Variants control sizing only; the structure is identical so the
 * sidebar and full view stay visually consistent.
 */
import ChatMarkdown from "../ChatMarkdown";
import UserAvatar from "../ui/UserAvatar";
import { IconDocument } from "../icons";
import { isMasterAgent, MANOR_AGENT_NAME } from "../../lib/constants";
import type { Agent, Task, User } from "../../lib/types";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingStructuredText, friendlyPersonName, isAutomationIdentity } from "../../lib/taskDisplay";


type Variant = "compact" | "full";

const VARIANT_STYLES: Record<Variant, {
  rowGap: number;
  rowPadY: number;
  avatarSize: number;
  iconSize: number;
  fontMain: number;
  fontMeta: number;
  fontTime: number;
  attachFont: number;
  attachIconSize: number;
}> = {
  compact: { rowGap: 8,  rowPadY: 8,  avatarSize: 24, iconSize: 12, fontMain: 12, fontMeta: 11, fontTime: 10, attachFont: 10, attachIconSize: 10 },
  full:    { rowGap: 10, rowPadY: 12, avatarSize: 28, iconSize: 14, fontMain: 13, fontMeta: 12, fontTime: 11, attachFont: 11, attachIconSize: 11 },
};

export const LOG_ICONS: Record<string, { color: string; icon: string }> = {
  ai_execution_started:   { color: "#5f84bd", icon: "M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z" },
  ai_agent_turn:          { color: "#9079c2", icon: "M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" },
  ai_supervisor_verdict:  { color: "#cf9b44", icon: "M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" },
  ai_execution_completed: { color: "#4f9c84", icon: "M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" },
  ai_execution_failed:    { color: "#d65f59", icon: "M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" },
  ai_hitl_requested:      { color: "#d3873f", icon: "M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" },
  ai_hitl_reminder:       { color: "#b27c34", icon: "M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a3 3 0 11-5.714 0" },
  ai_hitl_resumed:        { color: "#4f7d75", icon: "M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" },
  ai_needs_replan:        { color: "#a07fc0", icon: "M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126z" },
  status_change:          { color: "#6d6fb2", icon: "M7.5 21L3 16.5m0 0L7.5 12M3 16.5h13.5m0-13.5L21 7.5m0 0L16.5 12M21 7.5H7.5" },
  comment:                { color: "#4a7d96", icon: "M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" },
  evaluation:             { color: "#c3a63f", icon: "M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" },
};

export interface TaskLogItemProps {
  log: any;
  index?: number;
  variant?: Variant;
  /** Format the timestamp — caller decides relative ("3m ago") vs absolute. */
  formatTime?: (iso: string) => string;
  /** Used to resolve author avatar/name when the backend only stored
   *  ``created_by`` as a free-form string. Optional: falls back to
   *  initials / a generic agent gradient when omitted. */
  users?: User[];
  agents?: Agent[];
  staff?: Array<Record<string, any>>;
  /** The task owning this log — used to fall back to the task's
   *  assigned agent when the comment is generically attributed to
   *  ``AI Agent`` / ``AI Supervisor`` (the plan executor's default). */
  task?: Task;
}

/* ── Author resolution ─────────────────────────────── */

interface ResolvedAuthor {
  name: string;
  avatarUrl?: string | null;
  kind: "user" | "agent" | "manor" | "system" | "none";
}

function isOpaqueInternalId(value: string) {
  const v = value.trim();
  return isAutomationIdentity(v) || /^01[A-Z0-9]{20,}$/i.test(v) || /^[a-z]+_[A-Za-z0-9_-]{16,}$/.test(v);
}

function humanizeServiceKey(value?: string | null): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  return raw
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b[a-z]/g, (char) => char.toUpperCase());
}

function isStepExecutionLog(log: any): boolean {
  const meta = log?.meta || {};
  return Boolean(meta.step_id || meta.step_key || meta.plan_id || String(log?.log_type || "").startsWith("step_"));
}

function taskAgentFallback(task?: Task): ResolvedAuthor | null {
  if (!task) return null;
  if (task.agent_id) {
    return {
      name: task.agent_name || humanizeServiceKey(task.owner_service_key) || t("component.task_log_item.agent"),
      avatarUrl: task.agent_avatar,
      kind: isMasterAgent(task.agent_id, task.agent_type) ? "manor" : "agent",
    };
  }
  const ownerServiceName = humanizeServiceKey(task.owner_service_key);
  if (ownerServiceName) {
    return {
      name: ownerServiceName,
      kind: "agent",
    };
  }
  return null;
}

function resolveAuthor(log: any, users: User[], agents: Agent[], staff: Array<Record<string, any>>, task?: Task): ResolvedAuthor {
  const cb: string = log.created_by || "";
  const meta = log.meta || {};

  // 1. Explicit agent hints from the backend (preferred — covers the
  // case where multiple agents post on the same task).
  const hintAgentId: string | undefined = log.author_agent_id || meta.agent_id;
  const hintAgentName: string | undefined =
    log.author_agent_name
    || meta.agent_name
    || meta.agent_subscription_name
    || humanizeServiceKey(meta.service_key);
  if (hintAgentId || hintAgentName) {
    if (hintAgentId && isMasterAgent(hintAgentId, meta.agent_type)) {
      return { name: MANOR_AGENT_NAME, kind: "manor" };
    }
    const matchedAgent = hintAgentId
      ? agents.find((a) => a.id === hintAgentId)
      : agents.find((a) => a.name === hintAgentName);
    if (matchedAgent) {
      return {
        name: matchedAgent.name,
        avatarUrl: matchedAgent.avatar_url,
        kind: "agent",
      };
    }
    if (hintAgentName) {
      return { name: hintAgentName, kind: "agent" };
    }
    const fallback = isStepExecutionLog(log) ? taskAgentFallback(task) : null;
    if (fallback) return fallback;
  }

  // 2. System events
  if (cb === "system" || cb === "System") {
    const fallback = isStepExecutionLog(log) ? taskAgentFallback(task) : null;
    if (fallback) return fallback;
    return { name: MANOR_AGENT_NAME, kind: "manor" };
  }

  // 3. Try matching a real user — display_name or email
  const matchedUser = users.find(
    (u) => u.id === cb || u.display_name === cb || u.email === cb,
  );
  if (matchedUser) {
    return {
      name: friendlyPersonName(matchedUser.display_name || matchedUser.email, t("page.users.user")),
      avatarUrl: matchedUser.avatar_url,
      kind: "user",
    };
  }

  const matchedStaff = staff.find(
    (s) => s.id === cb || s.user_id === cb || s.name === cb || s.display_name === cb || s.email === cb,
  );
  if (matchedStaff) {
    return {
      name: friendlyPersonName(matchedStaff.name || matchedStaff.display_name || matchedStaff.email, t("page.users.user")),
      avatarUrl: matchedStaff.avatar_url,
      kind: "user",
    };
  }

  // 4. Generic agent attributions ("AI Agent", "AI Supervisor", "Agent")
  // — fall back to the task's assigned agent (older logs from before
  // the meta hint was wired don't have author_agent_id).
  if (cb.startsWith("AI ") || cb === "Agent") {
    if (task?.agent_id) {
      if (isMasterAgent(task.agent_id, task.agent_type)) {
        return { name: MANOR_AGENT_NAME, kind: "manor" };
      }
      const matchedAgent = agents.find((a) => a.id === task.agent_id);
      if (matchedAgent) {
        return {
          name: matchedAgent.name,
          avatarUrl: matchedAgent.avatar_url,
          kind: "agent",
        };
      }
    }
    return { name: cb || t("component.task_log_item.agent"), kind: "agent" };
  }

  // 5. ``created_by`` may be an internal id. Prefer resolved task fields
  // over showing opaque database ids in user-facing activity feeds.
  if (cb && cb === task?.creator_id) {
    if (task.creator_id === "system") {
      return {
        name: MANOR_AGENT_NAME,
        kind: "manor",
      };
    }
    return {
      name: friendlyPersonName(task.creator_name, t("page.users.user")),
      avatarUrl: task.creator_avatar,
      kind: "user",
    };
  }
  if (cb && cb === task?.assignee_id) {
    return {
      name: friendlyPersonName(task.assignee_name, t("page.users.user")),
      avatarUrl: task.assignee_avatar,
      kind: "user",
    };
  }
  if (cb && cb === task?.agent_id) {
    if (isMasterAgent(task.agent_id, task.agent_type)) {
      return { name: MANOR_AGENT_NAME, kind: "manor" };
    }
    return {
      name: task.agent_name || t("component.task_log_item.agent"),
      avatarUrl: task.agent_avatar,
      kind: "agent",
    };
  }

  // 6. ``created_by`` may already be an agent's actual name (we now
  // write it that way going forward — see TaskRunner._log). Match it
  // against the agents list.
  const matchedByName = agents.find((a) => a.id === cb || a.name === cb);
  if (matchedByName) {
    return {
      name: matchedByName.name,
      avatarUrl: matchedByName.avatar_url,
      kind: "agent",
    };
  }

  // 7. Never leak opaque ids into the UI. If the backend only provided an
  // id and we could not resolve it, fall back to a friendly role label.
  if (isOpaqueInternalId(cb)) {
    return {
      name: log.log_type?.startsWith("ai_")
        ? t("component.task_log_item.agent")
        : t("page.users.user"),
      kind: log.log_type?.startsWith("ai_") ? "agent" : "user",
    };
  }

  // 8. Anything else → treat as a free-form name (no avatar).
  return { name: friendlyPersonName(cb, t("component.task_log_item.system")), kind: cb ? "user" : "none" };
}

function shortId(value?: string | null) {
  return value ? value.slice(-6) : "";
}

function diagnosticItems(meta: Record<string, any> | null | undefined) {
  if (!meta) return [];
  const items: { label: string; value: string; tone?: "danger" | "muted" }[] = [];
  if (meta.plan_id) items.push({ label: t("component.task_log_item.plan"), value: shortId(meta.plan_id) });
  if (meta.step_id) items.push({ label: t("component.task_log_item.step"), value: shortId(meta.step_id) });
  if (meta.lease_id) items.push({ label: t("component.task_log_item.lease"), value: shortId(meta.lease_id) });
  if (meta.worker_id) items.push({ label: t("component.task_log_item.worker"), value: shortId(meta.worker_id) });
  if (meta.error_type) items.push({ label: t("component.task_log_item.error"), value: formatUserFacingLabel(String(meta.error_type)), tone: "danger" });
  if (meta.retry_count !== undefined || meta.attempt_count !== undefined) {
    const attempt = meta.attempt_count ?? meta.retry_count;
    const max = meta.max_attempts ? `/${meta.max_attempts}` : "";
    items.push({ label: t("component.task_log_item.attempt"), value: `${attempt}${max}`, tone: "muted" });
  }
  return items;
}

export default function TaskLogItem({
  log, index = 0, variant = "full", formatTime,
  users = [], agents = [], staff = [], task,
}: TaskLogItemProps) {
  const v = VARIANT_STYLES[variant];
  const author = resolveAuthor(log, users, agents, staff, task);

  const isStatus     = log.log_type === "status_change";
  const isEval       = log.log_type === "evaluation";
  const isHitlReq    = log.log_type === "ai_hitl_requested";
  const isHitlReminder = log.log_type === "ai_hitl_reminder";
  const isHitlResume = log.log_type === "ai_hitl_resumed";
  const isReplan     = log.log_type === "ai_needs_replan";
  const isAIDone     = log.log_type === "ai_execution_completed";
  const isAIFail     = log.log_type === "ai_execution_failed";
  const isAIEvent    = isHitlReq || isHitlReminder || isHitlResume || isReplan || isAIDone || isAIFail;
  const lcfg = LOG_ICONS[log.log_type] || LOG_ICONS.comment;

  const eventBadge = isHitlReq    ? { text: t("component.task_log_item.needs_input"), color: "#b27c34", bg: "#f3ecd6" }
                  : isHitlReminder ? { text: t("component.task_log_item.reminder"), color: "#b27c34", bg: "#f3ecd6" }
                  : isHitlResume ? { text: t("component.task_log_item.resumed"), color: "#4f7d75", bg: "#e5eeeb" }
                  : isReplan     ? { text: t("component.task_log_item.needs_replan"), color: "#b66a3c", bg: "#ffedd5" }
                  : isAIDone     ? { text: t("status.completed"), color: "#437f6b", bg: "#dceae3" }
                  : isAIFail     ? { text: t("component.task_log_item.failed"), color: "#c14a44", bg: "#f1dddb" }
                  : null;

  const skipContent = log.content && log.content.startsWith("Attached ") && log.attachments?.length > 0;
  const diagnostics = diagnosticItems(log.meta);

  return (
    <div style={{
      display: "flex", gap: v.rowGap, alignItems: "flex-start",
      padding: `${v.rowPadY}px 0`,
      borderTop: index > 0 ? "1px solid rgba(231,229,228,0.25)" : "none",
    }}>
      {/* Avatar — status events get a typed icon badge; everyone else
          gets a real avatar (image when available, initials fallback). */}
      {isStatus ? (
        <div style={{ width: v.avatarSize, height: v.avatarSize, borderRadius: "50%", flexShrink: 0, background: `${lcfg.color}10`, border: `1.5px solid ${lcfg.color}25`, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <svg width={v.iconSize} height={v.iconSize} fill="none" viewBox="0 0 24 24" stroke={lcfg.color} strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d={lcfg.icon} />
          </svg>
        </div>
      ) : (
        <UserAvatar
          name={author.name}
          type={author.kind === "system" ? "none" : (author.kind as any)}
          avatarUrl={author.avatarUrl}
          size={v.avatarSize}
        />
      )}

      {/* Content column */}
      <div style={{ flex: 1, minWidth: 0 }}>
        {/* Header row: name · badges · timestamp */}
        <div style={{ display: "flex", alignItems: "center", gap: v.rowGap - 2, marginBottom: 3, flexWrap: "wrap" }}>
          <span style={{ fontSize: v.fontMeta, fontWeight: 600, color: "#292524" }}>
            {isAIEvent && author.kind !== "agent" && author.kind !== "manor" ? t("page.workspace_detail.agent") : author.name}
          </span>
          {isStatus && (
            <span style={{ fontSize: 10, fontWeight: 600, color: lcfg.color, padding: "1px 6px", borderRadius: 4, background: `${lcfg.color}0d`, textTransform: "uppercase", letterSpacing: "0.03em" }}>
              {t("component.task_log_item.status_change")}</span>
          )}
          {isEval && (
            <span style={{ fontSize: 10, fontWeight: 600, color: "#c3a63f", padding: "1px 6px", borderRadius: 4, background: "rgba(195,166,63,0.08)", textTransform: "uppercase", letterSpacing: "0.03em" }}>
              {t("component.task_log_item.evaluation")}
            </span>
          )}
          {eventBadge && (
            <span style={{ fontSize: 10, fontWeight: 700, color: eventBadge.color, padding: "1px 6px", borderRadius: 4, background: eventBadge.bg, textTransform: "uppercase", letterSpacing: "0.03em" }}>
              {eventBadge.text}
            </span>
          )}
          <span style={{ fontSize: v.fontTime, color: "#a8a29e" }}>
            {log.created_at && formatTime ? formatTime(log.created_at) : ""}
          </span>
        </div>

        {/* HITL question (when present) */}
        {isHitlReq && (log.meta as any)?.question && (
          <p style={{ fontSize: v.fontMain, color: "#0f172a", margin: "0 0 4px", lineHeight: 1.6, fontWeight: 500 }}>
            {formatUserFacingStructuredText((log.meta as any).question)}
          </p>
        )}

        {/* @mention chips */}
        {Array.isArray(log.meta?.mentions) && log.meta.mentions.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 4 }}>
            {log.meta.mentions.map((m: any) => (
              <span
                key={`${m.type}-${m.id}`}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 3,
                  padding: "1px 7px", borderRadius: 999, fontSize: 11, fontWeight: 600,
                  background: m.type === "agent" ? "rgba(67,107,101,0.10)" : "rgba(74,125,150,0.10)",
                  color: m.type === "agent" ? "#436b65" : "#4a7d96",
                }}
              >
                @{m.name || m.id}
              </span>
            ))}
          </div>
        )}

        {/* Content — markdown for non-status, italic for status events */}
        {log.content && !skipContent && (
          isStatus ? (
            <p style={{ fontSize: v.fontMain, color: "#78716c", margin: 0, lineHeight: 1.5, fontStyle: "italic" }}>
              {formatUserFacingStructuredText(log.content)}
            </p>
          ) : (
            <div style={{ fontSize: v.fontMain, color: "#57534e", lineHeight: 1.6 }}>
              <ChatMarkdown content={formatUserFacingStructuredText(log.content)} />
            </div>
          )
        )}

        {/* Execution diagnostics — only shown when backend emits correlation metadata. */}
        {diagnostics.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
            {diagnostics.map((item) => (
              <span
                key={`${item.label}:${item.value}`}
                title={`${item.label}: ${item.value}`}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  padding: "2px 6px", borderRadius: 999,
                  background: item.tone === "danger" ? "#f8f0ef" : "#fafaf9",
                  border: `1px solid ${item.tone === "danger" ? "#ecc8c5" : "#e7e5e4"}`,
                  color: item.tone === "danger" ? "#a23e38" : "#78716c",
                  fontSize: v.fontTime,
                  fontWeight: 650,
                  fontFamily: item.label === "error" ? undefined : "ui-monospace, SFMono-Regular, Menlo, monospace",
                }}
              >
                <span style={{ color: item.tone === "danger" ? "#c14a44" : "#a8a29e", fontFamily: "inherit" }}>{item.label}</span>
                {item.value}
              </span>
            ))}
          </div>
        )}

        {/* Attachments */}
        {log.attachments && log.attachments.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
            {log.attachments.map((att: any, ai: number) => (
              <a
                key={ai}
                href={att.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  padding: "3px 8px", borderRadius: 6,
                  background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                  fontSize: v.attachFont, color: "#436b65", fontWeight: 500,
                  textDecoration: "none", transition: "border-color 0.15s",
                }}
              >
                <IconDocument size={v.attachIconSize} />{att.original_name || att.filename}
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
