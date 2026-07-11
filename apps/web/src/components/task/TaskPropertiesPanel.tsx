/**
 * TaskPropertiesPanel — shared property rows (Status / Assignee / Deadline /
 * Priority / Category / SLA) used by both the kanban-drawer (Tasks.tsx)
 * and the standalone full view (TaskDetail.tsx).
 *
 * Two visual variants:
 *   - "compact"  — 90px label column, 12-13px text, fits in the 380px drawer
 *   - "full"     — 120px label column, 13-14px text, full-width pages
 *
 * Each row is dispatched via a single ``onUpdate(patch)`` callback that
 * the parent wires to its own update mutation. The panel never imports
 * react-query — it stays a pure rendering component, easy to drop into
 * any task-detail surface that comes later (modals, side panels, etc).
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  IconCircleDot, IconFlag, IconUser, IconAgent, IconCalendar, IconCategory, IconClock,
} from "../icons";
import Select from "../ui/Select";
import DateTimePicker from "../ui/DateTimePicker";
import UserAvatar from "../ui/UserAvatar";
import { STATUS_CONFIG } from "../ui/StatusPill";
import { PRIORITY_CONFIG } from "../ui/PriorityPill";
import { CATEGORIES } from "../../lib/taskCategories";
import { MANOR_AGENT_ID, MANOR_AGENT_NAME, MANOR_AGENT_TYPE, isMasterAgent } from "../../lib/constants";
import type { Task, Agent, User } from "../../lib/types";
import { t } from "../../lib/i18n";
import { friendlyPersonName, isAutomationIdentity } from "../../lib/taskDisplay";


type Variant = "compact" | "full";

const VARIANT_STYLES: Record<Variant, { labelW: number; padY: number; fontSize: number; iconSize: number; pickerMaxWidth: number }> = {
  compact: { labelW: 90,  padY: 8,  fontSize: 12, iconSize: 12, pickerMaxWidth: 220 },
  full:    { labelW: 120, padY: 12, fontSize: 13, iconSize: 14, pickerMaxWidth: 260 },
};

function humanizeServiceKey(value?: string | null): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  return raw
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b[a-z]/g, (char) => char.toUpperCase());
}

function workspaceAgentTeamName(task: Task): string {
  const workspaceName = String((task as any).workspace_name || "").trim();
  if (workspaceName) {
    return t("component.task_properties_panel.workspace_agent_team_named", { workspace: workspaceName });
  }
  const serviceName = humanizeServiceKey(task.owner_service_key);
  if (serviceName) {
    return t("component.task_properties_panel.workspace_service_agent_team_named", { service: serviceName });
  }
  return t("component.task_properties_panel.workspace_agent_team");
}

/* ── Generic labeled row primitive ─────────────────────── */

export function PropertyRow({
  label, icon, help, children, variant = "full", divider = true,
}: {
  label: string;
  icon?: React.ReactNode;
  help?: string;
  children: React.ReactNode;
  variant?: Variant;
  divider?: boolean;
}) {
  const v = VARIANT_STYLES[variant];
  return (
    <div className={`task-property-row task-property-row--${variant}`} style={{
      display: "flex", alignItems: "center",
      padding: `${v.padY}px 0`,
      borderBottom: divider ? "1px solid rgba(231,229,228,0.3)" : "none",
    }}>
      <div className="task-property-label" style={{
        width: v.labelW, display: "flex", alignItems: "center", gap: variant === "compact" ? 6 : 8,
        color: "#78716c", fontSize: v.fontSize, fontWeight: 500, flexShrink: 0,
      }}>
        {icon}
        <span>{label}</span>
        {help && (
          <span
            className="task-property-help"
            title={help}
            aria-label={help}
            style={{
              width: variant === "compact" ? 15 : 16,
              height: variant === "compact" ? 15 : 16,
              borderRadius: 999,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
              color: "#8a8178",
              background: "rgba(255,255,255,0.72)",
              border: "1px solid rgba(28,25,23,0.1)",
              cursor: "help",
            }}
          >
            <span style={{ fontSize: variant === "compact" ? 10 : 11, fontWeight: 700, lineHeight: 1 }}>?</span>
          </span>
        )}
      </div>
      <div className="task-property-control" style={{ flex: 1 }}>
        {children}
      </div>
    </div>
  );
}

/* ── Assignee picker (rich, with avatars + search) ─────── */

export function AssigneePicker({
  task, agents, users, staff = [], currentUser, onSelect, style,
}: {
  task: Task;
  agents: Agent[];
  users: User[];
  staff?: Array<{
    id?: string;
    user_id?: string | null;
    name?: string | null;
    display_name?: string | null;
    email?: string | null;
    avatar_url?: string | null;
    status?: string | null;
  }>;
  currentUser: User | null;
  onSelect: (patch: Partial<Task>) => void;
  style?: React.CSSProperties;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [coords, setCoords] = useState<{ top: number; left: number; width: number } | null>(null);

  useEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (!r) return;
      const viewportPadding = 12;
      const menuWidth = Math.max(r.width, 260);
      const maxLeft = Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding);
      setCoords({
        top: r.bottom + 4,
        left: Math.min(Math.max(viewportPadding, r.left), maxLeft),
        width: menuWidth,
      });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (ref.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);
  useEffect(() => { if (open && inputRef.current) inputRef.current.focus(); }, [open]);

  const matchedAssigneeAgent = task.assignee_id ? agents.find((a) => a.id === task.assignee_id) : null;
  const matchedAgent = task.agent_id ? agents.find((a) => a.id === task.agent_id) : matchedAssigneeAgent;
  const matchedUser = task.assignee_id ? users.find((u) => u.id === task.assignee_id) : null;
  const matchedStaff = task.assignee_id
    ? staff.find((s) => s.id === task.assignee_id || s.user_id === task.assignee_id)
    : null;
  const matchedOwnerUser = task.assignee_id
    ? users.find((u) => u.entity_id === task.assignee_id && (u.role === "owner" || u.role === "admin"))
    : null;
  const isManor = isMasterAgent(task.agent_id, task.agent_type) || isMasterAgent(task.assignee_id);
  const isWorkspaceAgentTeam = Boolean(task.workspace_id && !task.agent_id && !task.assignee_id && !isManor);
  const ownerServiceLabel = humanizeServiceKey(task.owner_service_key);
  const workspaceTeamName = workspaceAgentTeamName(task);

  const rawDisplayName = isManor
    ? MANOR_AGENT_NAME
    : isWorkspaceAgentTeam
      ? workspaceTeamName
      : matchedAgent?.name
        || task.assignee_name
        || (matchedUser?.display_name || matchedUser?.email)
        || (matchedStaff?.display_name || matchedStaff?.name || matchedStaff?.email)
        || (matchedOwnerUser?.display_name || matchedOwnerUser?.email)
        || (task.assignee_id === currentUser?.id || task.assignee_id === currentUser?.entity_id
          ? (currentUser?.display_name || currentUser?.email)
          : null)
        || (task.assignee_id ? t("component.comment_thread.user") : null);
  const displayName = rawDisplayName ? friendlyPersonName(rawDisplayName, t("component.comment_thread.user")) : null;

  const displayType: "agent" | "manor" | "workspace" | "user" | "none" =
    isManor ? "manor" : isWorkspaceAgentTeam ? "workspace" : (task.agent_id || matchedAssigneeAgent) ? "agent" : displayName ? "user" : "none";

  const displayAvatar = isManor
    ? null
    : matchedAgent?.avatar_url
      || task.assignee_avatar
      || matchedUser?.avatar_url
      || matchedStaff?.avatar_url
      || matchedOwnerUser?.avatar_url
      || (task.assignee_id === currentUser?.id || task.assignee_id === currentUser?.entity_id ? currentUser?.avatar_url : null);

  type Option = {
    id: string; name: string; type: "agent" | "manor" | "workspace" | "user" | "none"; badge: string;
    avatarUrl?: string | null; patch: Partial<Task>;
  };
  const assignableUsers = users.filter((u) => {
    const identity = [u.id, u.display_name, u.email, (u as any).name].filter(Boolean).join(" ");
    return !isAutomationIdentity(identity);
  });

  const options: Option[] = [
    {
      id: isWorkspaceAgentTeam ? "__workspace_agent_team__" : "__unassign__",
      name: isWorkspaceAgentTeam
        ? workspaceTeamName
        : t("component.task_properties_panel.unassigned"),
      type: isWorkspaceAgentTeam ? "workspace" : "none",
      badge: isWorkspaceAgentTeam
        ? (ownerServiceLabel || t("component.task_properties_panel.workspace_agent_team_badge"))
        : "",
      patch: { assignee_id: "" as any, agent_id: "" as any, agent_type: "" as any },
    },
    ...(currentUser ? [{
      id: `user:${currentUser.id}`,
      name: friendlyPersonName(currentUser.display_name || currentUser.email, t("component.task_properties_panel.me")),
      type: "user" as const,
      badge: t("component.workspace_chat.you"),
      avatarUrl: currentUser.avatar_url,
      patch: { assignee_id: currentUser.id, agent_id: "" as any, agent_type: "" as any },
    }] : []),
    {
      id: "__manor__", name: MANOR_AGENT_NAME, type: "manor", badge: t("component.task_properties_panel.master"),
      patch: { agent_id: MANOR_AGENT_ID, agent_type: MANOR_AGENT_TYPE, assignee_id: "" as any },
    },
    ...agents.map((a) => ({
      id: `agent:${a.id}`, name: a.name, type: "agent" as const, badge: t("component.task_log_item.agent"),
      avatarUrl: a.avatar_url,
      patch: { agent_id: a.id, agent_type: "agent", assignee_id: "" as any },
    })),
    ...assignableUsers
      .filter((u) => !currentUser || u.id !== currentUser.id)
      .map((u) => ({
        id: `user:${u.id}`,
        name: friendlyPersonName(u.display_name || u.email, t("component.comment_thread.user")),
        type: "user" as const,
        badge: t("component.comment_thread.user"),
        avatarUrl: u.avatar_url,
        patch: { assignee_id: u.id, agent_id: "" as any, agent_type: "" as any },
      })),
    ...staff
      .filter((s) => s?.id && s.status !== "inactive")
      .filter((s) => !isAutomationIdentity([s.id, s.user_id, s.display_name, s.name, s.email].filter(Boolean).join(" ")))
      .filter((s) => {
        const alreadyListedAsUser = !!s.user_id && assignableUsers.some((u) => u.id === s.user_id);
        return !alreadyListedAsUser || s.id === task.assignee_id;
      })
      .map((s) => ({
        id: `staff:${s.id}`,
        name: friendlyPersonName(s.display_name || s.name || s.email, t("component.comment_thread.user")),
        type: "user" as const,
        badge: t("page.workspace_detail.staff"),
        avatarUrl: s.avatar_url,
        patch: { assignee_id: (s.user_id || s.id) as any, agent_id: "" as any, agent_type: "" as any },
      })),
  ];

  const filtered = query
    ? options.filter((o) => o.name.toLowerCase().includes(query.toLowerCase()))
    : options;

  const isCurrent = (o: Option): boolean => {
    if (o.id === "__unassign__") return !task.assignee_id && !task.agent_id && !isManor;
    if (o.id === "__workspace_agent_team__") return isWorkspaceAgentTeam;
    if (o.id === "__manor__") return isManor;
    if (o.id.startsWith("agent:")) {
      const agentId = o.id.slice(6);
      return agentId === task.agent_id || (!task.agent_id && agentId === task.assignee_id);
    }
    if (o.id.startsWith("user:")) return o.id.slice(5) === task.assignee_id && !task.agent_id && !matchedAssigneeAgent;
    if (o.id.startsWith("staff:")) {
      const staffId = o.id.slice(6);
      const staffRow = staff.find((s) => s.id === staffId);
      return !task.agent_id && (staffId === task.assignee_id || staffRow?.user_id === task.assignee_id);
    }
    return false;
  };

  return (
    <div ref={ref} style={{ position: "relative", ...style }}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => { setOpen(!open); setQuery(""); }}
        className="manor-input task-assignee-picker-trigger"
        style={{
          width: "100%", textAlign: "left", cursor: "pointer",
          display: "flex", alignItems: "center", gap: 8, paddingLeft: 6,
          color: displayName ? "var(--text-strong)" : "var(--text-faint)",
          ...(open ? { borderColor: "var(--accent)", boxShadow: "0 0 0 3px var(--accent-ring)", background: "var(--surface-panel)" } : {}),
        }}
      >
        <UserAvatar name={displayName || t("component.task_properties_panel.unassigned")} type={displayType} avatarUrl={displayAvatar} size={24} />
        <span
          className="task-assignee-picker-name"
          title={displayName || t("component.task_properties_panel.unassigned")}
          style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 13, fontWeight: 500 }}
        >
          {displayName || t("component.task_properties_panel.unassigned")}
        </span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth={2.5}
          style={{ flexShrink: 0, transition: "transform 0.2s", transform: open ? "rotate(180deg)" : "none" }}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
        </svg>
      </button>

      {open && coords && createPortal(
        <div ref={menuRef} className="task-assignee-picker-menu" style={{
          position: "fixed", top: coords.top, left: coords.left, width: coords.width, zIndex: 100000,
          background: "color-mix(in srgb, var(--surface-panel) 98%, transparent)", backdropFilter: "blur(24px)",
          border: "1px solid var(--border-default)", borderRadius: 12,
          boxShadow: "var(--shadow-lg)",
          maxHeight: 320, overflowY: "auto", padding: 4,
          animation: "dialog-in 0.15s ease-out",
        }}>
          <div style={{ padding: "4px 4px 6px" }}>
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("component.task_properties_panel.search_assignee")}
              className="task-assignee-picker-search"
              style={{ width: "100%", height: 32, padding: "0 10px", fontSize: 13, fontWeight: 500, border: "1px solid var(--border-default)", borderRadius: 8, outline: "none", color: "var(--text-strong)", background: "var(--surface-muted)", boxSizing: "border-box" }}
            />
          </div>
          {filtered.length === 0 && (
            <div style={{ padding: "12px 14px", fontSize: 13, color: "var(--text-faint)", textAlign: "center" }}>{t("component.task_properties_panel.no_results")}</div>
          )}
          {filtered.map((o) => {
            const sel = isCurrent(o);
            return (
              <button
                key={o.id}
                type="button"
                onClick={() => { onSelect(o.patch); setOpen(false); setQuery(""); }}
                className={`task-assignee-option${sel ? " is-selected" : ""}`}
                style={{
                  display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "6px 10px",
                  border: "none", borderRadius: 8, cursor: "pointer", textAlign: "left",
                  background: sel ? "var(--accent-soft)" : "transparent", transition: "background 0.1s",
                }}
                onMouseEnter={(e) => { if (!sel) e.currentTarget.style.background = "var(--surface-muted)"; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = sel ? "var(--accent-soft)" : "transparent"; }}
              >
                <UserAvatar name={o.name} type={o.type} avatarUrl={o.avatarUrl} size={28} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: sel ? 600 : 400, color: sel ? "var(--accent)" : "var(--text-strong)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {o.name}
                  </div>
                  {o.badge && <div style={{ fontSize: 10, color: "var(--text-faint)", fontWeight: 500 }}>{o.badge}</div>}
                </div>
                {sel && (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="var(--accent)" style={{ flexShrink: 0 }}>
                    <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>,
        document.body,
      )}
    </div>
  );
}

/* ── Composed panel ──────────────────────────────────── */

export interface TaskPropertiesPanelProps {
  task: Task;
  agents: Agent[];
  users?: User[];
  staff?: Array<Record<string, any>>;
  currentUser?: User | null;
  variant?: Variant;
  /** Show priority row (off in compact since the kanban card already shows it). */
  showPriority?: boolean;
  /** Show category row. */
  showCategory?: boolean;
  /** Show SLA-policy row (only useful when SLA policies exist). */
  showSla?: boolean;
  /** Show requester (creator) row — read-only avatar + name. */
  showRequester?: boolean;
  /** Render with a soft background panel + border (the drawer's style). */
  framed?: boolean;
  statusTransitions?: Record<string, string[]>;
  /** Single dispatch — parent wires this to its update mutation. */
  onUpdate: (patch: Partial<Task>) => void;
}

export default function TaskPropertiesPanel({
  task, agents, users = [], staff = [], currentUser = null,
  variant = "full",
  showPriority = true, showCategory = true, showSla = false,
  showRequester = true,
  framed = false,
  statusTransitions,
  onUpdate,
}: TaskPropertiesPanelProps) {
  const v = VARIANT_STYLES[variant];
  const isAI = !!task.agent_id || isMasterAgent(task.agent_id, task.agent_type);
  const isWorkspaceAgentTeamAssignee = Boolean(
    task.workspace_id
    && !task.agent_id
    && !task.assignee_id
    && !isMasterAgent(task.agent_id, task.agent_type)
    && !isMasterAgent(task.assignee_id)
  );
  const assigneeHelp = t("component.task_properties_panel.assignee_help");
  const statusKeys = statusTransitions?.[task.status] || Object.keys(STATUS_CONFIG);
  const statusOptions = statusKeys
    .filter((key) => STATUS_CONFIG[key])
    .map((key) => ({ value: key, label: t(STATUS_CONFIG[key].labelKey) }));

  // Last visible row should not draw a divider — collect rows and drop
  // the trailing one's bottom border.
  const rows: { key: string; node: React.ReactNode }[] = [];
  rows.push({
    key: "status",
    node: (
      <PropertyRow label={t("page.agent_dashboard.status")} variant={variant} icon={<IconCircleDot size={v.iconSize} />}>
        <Select
          value={task.status}
          onChange={(s) => onUpdate({ status: s })}
          options={statusOptions}
          style={{ maxWidth: v.pickerMaxWidth }}
        />
      </PropertyRow>
    ),
  });
  if (showRequester) {
    // Prefer the resolved fields the API returns; fall back to a lookup
    // in the users list (covers older payloads or pages that fetched
    // from a cached endpoint that pre-dated the resolver). System-owned
    // tasks are Manor-originated automation and should not leak the raw
    // internal "system" actor name.
    const creatorId = (task as any).creator_id;
    const isSystemCreator = creatorId === "system" || isMasterAgent(creatorId);
    const matchedCreator = creatorId
      ? users.find((u) => u.id === (task as any).creator_id)
      : null;
    const matchedCreatorStaff = creatorId
      ? staff.find((s) => s.id === creatorId || s.user_id === creatorId || s.email === creatorId)
      : null;
    const matchedCreatorAgent = creatorId
      ? agents.find((a) => a.id === creatorId)
      : null;
    const requesterName = isSystemCreator
      ? MANOR_AGENT_NAME
      : (
          (task as any).creator_name
          || matchedCreator?.display_name
          || matchedCreator?.email
          || matchedCreatorStaff?.name
          || matchedCreatorStaff?.display_name
          || matchedCreatorStaff?.email
          || matchedCreatorAgent?.name
          || null
        );
    const requesterAvatar =
      (task as any).creator_avatar
      || matchedCreator?.avatar_url
      || matchedCreatorStaff?.avatar_url
      || matchedCreatorAgent?.avatar_url
      || null;
    if (requesterName) {
      const requesterDisplayName = friendlyPersonName(requesterName, t("page.users.user"));
      const requesterType = isSystemCreator ? "manor" : matchedCreatorAgent ? "agent" : "user";
      rows.push({
        key: "requester",
        node: (
          <PropertyRow label={t("component.task_properties_panel.requester")} variant={variant} icon={<IconUser size={v.iconSize} />}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "2px 0" }}>
              <UserAvatar
                name={requesterDisplayName}
                type={requesterType}
                avatarUrl={requesterAvatar}
                size={variant === "compact" ? 22 : 26}
              />
              <span style={{ fontSize: v.fontSize, color: "#292524", fontWeight: 500 }}>
                {requesterDisplayName}
              </span>
            </span>
          </PropertyRow>
        ),
      });
    }
  }
  if (showPriority) {
    rows.push({
      key: "priority",
      node: (
        <PropertyRow label={t("page.task_detail.priority")} variant={variant} icon={<IconFlag size={v.iconSize} />}>
          <Select
            value={String(task.priority)}
            onChange={(p) => onUpdate({ priority: Number(p) })}
            options={Object.entries(PRIORITY_CONFIG).reverse().map(([k, c]: [string, any]) => ({ value: k, label: t(c.labelKey) }))}
            style={{ maxWidth: v.pickerMaxWidth }}
          />
        </PropertyRow>
      ),
    });
  }
  rows.push({
    key: "assignee",
    node: (
      <PropertyRow
        label={t("component.embedded_chat.assignee")}
        variant={variant}
        icon={(isAI || isWorkspaceAgentTeamAssignee) ? <IconAgent size={v.iconSize} /> : <IconUser size={v.iconSize} />}
        help={assigneeHelp}
      >
        <AssigneePicker
          task={task}
          agents={agents}
          users={users}
          staff={staff}
          currentUser={currentUser}
          onSelect={onUpdate}
          style={{ maxWidth: v.pickerMaxWidth }}
        />
      </PropertyRow>
    ),
  });
  rows.push({
    key: "deadline",
    node: (
      <PropertyRow label={t("page.task_detail.deadline")} variant={variant} icon={<IconCalendar size={v.iconSize} />}>
        <DateTimePicker
          value={task.deadline ? task.deadline.slice(0, 10) : ""}
          onChange={(d) => onUpdate({ deadline: (d || undefined) as any })}
          placeholder={t("page.tasks.no_deadline")}
          style={{ maxWidth: v.pickerMaxWidth - 30 }}
        />
      </PropertyRow>
    ),
  });
  if (showCategory) {
    rows.push({
      key: "category",
      node: (
        <PropertyRow label={t("page.workspaces.category")} variant={variant} icon={<IconCategory size={v.iconSize} />}>
          <Select
            value={(task as any).category_id || ""}
            onChange={(c) => onUpdate({ category_id: (c || "") as any })}
            options={CATEGORIES.map((c) => ({ value: c.key, label: t(c.labelKey) }))}
            placeholder={t("page.workspace_detail.none")}
            filterable
            style={{ maxWidth: v.pickerMaxWidth }}
          />
        </PropertyRow>
      ),
    });
  }
  if (showSla) {
    rows.push({
      key: "sla",
      node: (
        <PropertyRow label={t("page.task_detail.sla")} variant={variant} icon={<IconClock size={v.iconSize} />}>
          <span style={{ fontSize: v.fontSize, color: "#a8a29e" }}>
            {(task as any).sla_policy_id || t("page.workspace_detail.none")}
          </span>
        </PropertyRow>
      ),
    });
  }

  const wrapperStyle: React.CSSProperties = framed
    ? {
        display: "flex", flexDirection: "column", gap: 0,
        background: "#fafaf9", borderRadius: 12, padding: "4px 12px",
        border: "1px solid rgba(28,25,23,0.06)",
      }
    : { display: "flex", flexDirection: "column", gap: 0 };

  return (
    <div style={wrapperStyle}>
      {rows.map((r, i) => (
        <div key={r.key} style={i === rows.length - 1 ? { borderBottom: "none" } : undefined}>
          {/* PropertyRow's divider applies its own border-bottom; we don't
              need to override unless this is the last row. We let the row
              draw its border, then nullify on the last via wrapper override
              wouldn't work — so the rows themselves accept ``divider`` from
              the panel. */}
          {i === rows.length - 1
            ? injectNoDivider(r.node)
            : r.node}
        </div>
      ))}
    </div>
  );
}

// Last-row trick: clone the row element with ``divider={false}`` so the
// trailing border is suppressed without each call site doing it manually.
function injectNoDivider(node: React.ReactNode): React.ReactNode {
  if (!node || typeof node !== "object" || !("props" in (node as any))) return node;
  const el = node as React.ReactElement<any>;
  return { ...el, props: { ...el.props, divider: false } } as React.ReactElement;
}
