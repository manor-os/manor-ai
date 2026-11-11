import { displayReadInterval } from "../lib/readRefreshPolicy";
import { useMemo, useState, type ComponentType } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import { localizeEventText } from "../lib/eventCopy";
import PageHeader from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import Select from "../components/ui/Select";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Modal from "../components/ui/Modal";
import Button from "../components/ui/Button";
import {
  IconAgent,
  IconBell,
  IconBolt,
  IconBug,
  IconCalendar,
  IconChat,
  IconChatBubble,
  IconChecklist,
  IconCheckCircle,
  IconClipboard,
  IconClock,
  IconComment,
  IconConnection,
  IconDocument,
  IconError,
  IconFilter,
  IconFlag,
  IconFlow,
  IconImage,
  IconMail,
  IconMegaphone,
  IconPause,
  IconPlay,
  IconRefresh,
  IconReopen,
  IconReport,
  IconSend,
  IconServer,
  IconShare,
  IconSettings,
  IconSparkles,
  IconTimeline,
  IconUserPlus,
  IconUsers,
  IconWarning,
  type IconProps,
  IconTrash,
} from "../components/icons";

import { t } from "../lib/i18n";
import { parseFileReference } from "../lib/fileReferences";
type StatusFilter = "all" | "unread";

const ALL_NOTIFICATION_TYPES = "all_types";


const TYPE_ICONS: Record<string, ComponentType<IconProps>> = {
  task: IconChecklist,
  task_created: IconClipboard,
  task_assigned: IconUserPlus,
  task_status_changed: IconTimeline,
  task_updated: IconRefresh,
  task_succeeded: IconCheckCircle,
  task_failed: IconBug,
  task_hitl_requested: IconComment,
  task_hitl_reminder: IconBell,
  task_retried: IconReopen,
  task_sla_breach: IconClock,
  task_overdue: IconClock,
  sla_escalation: IconFlag,
  proposal: IconSparkles,
  booking_confirmed: IconCalendar,
  video: IconPlay,
  image: IconImage,
  document: IconDocument,
  agent: IconAgent,
  agent_message: IconChat,
  agent_initiative_update: IconBolt,
  chat: IconChatBubble,
  channel_inbound_message: IconMail,
  workflow: IconFlow,
  automation_paused: IconPause,
  workspace_setup_required: IconConnection,
  workspace_update_available: IconSparkles,
  system_health: IconServer,
  daily_briefing: IconReport,
  workspace_digest: IconReport,
  integration_account_issue: IconWarning,
  system: IconSettings,
  warning: IconWarning,
  team_invite_received: IconUsers,
  team_invite_sent: IconSend,
  integration_shared: IconShare,
  error: IconError,
};

const TYPE_LABELS: Record<string, string> = {
  task: "Task",
  task_created: "Task created",
  task_assigned: "Task assigned",
  task_status_changed: "Task status changed",
  task_updated: "Task updated",
  task_succeeded: "Task completed",
  task_failed: "Task failed",
  task_hitl_requested: "Input needed",
  task_hitl_reminder: "Input reminder",
  task_retried: "Task retried",
  task_sla_breach: "Task SLA breach",
  task_overdue: "Task overdue",
  sla_escalation: "SLA escalation",
  proposal: "Proposal",
  booking_confirmed: "Booking confirmed",
  video: "Video",
  image: "Image",
  document: "Document",
  agent: "Agent",
  agent_message: "Agent message",
  agent_initiative_update: "Agent initiative",
  chat: "Chat",
  channel_inbound_message: "Channel message",
  workflow: "Workflow",
  automation_paused: "Automation paused",
  workspace_setup_required: "Workspace setup",
  workspace_update_available: "Workspace update",
  system_health: "System health",
  daily_briefing: "Daily briefing",
  workspace_digest: "Daily digest",
  integration_account_issue: "Account needs attention",
  system: "System",
  warning: "Warning",
  team_invite_received: "Team invitation",
  team_invite_sent: "Invitation sent",
  integration_shared: "Account shared",
  error: "Error",
};

function notificationTypeCategory(type: string): "task" | "agent" | "chat" | null {
  if (type.startsWith("task_")) return "task";
  if (type.startsWith("agent_")) return "agent";
  if (type.includes("message")) return "chat";
  return null;
}

function notificationTypeLabel(type: string): string {
  return TYPE_LABELS[type] || type
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function NotificationTypeIcon({ type, size = 20 }: { type: string; size?: number }) {
  const category = notificationTypeCategory(type);
  const TypeIcon = TYPE_ICONS[type] || (category ? TYPE_ICONS[category] : undefined) || IconBell;
  return <TypeIcon size={size} style={{ color: "var(--text-muted)" }} />;
}

export default function Notifications() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialFilter = searchParams.get("filter");
  let statusFilter: StatusFilter = initialFilter === "unread" ? "unread" : "all";
  let typeFilter = searchParams.get("type") || ALL_NOTIFICATION_TYPES;
  const [detailOpen, setDetailOpen] = useState<any | null>(null);

  const showStandardNotifications = statusFilter === "all" || statusFilter === "unread";
  const notificationQuery = useQuery({
    queryKey: ["notifications", statusFilter === "unread" ? "unread" : "all"],
    queryFn: () =>
      api.notifications.list({
        unread_only: statusFilter === "unread" ? true : undefined,
      }),
  });


  const markReadMutation = useMutation({
    mutationFn: (id: string) => api.notifications.markRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const markAllReadMutation = useMutation({
    mutationFn: async () => {
      const requests: Promise<unknown>[] = [api.notifications.markAllRead()];
      await Promise.all(requests);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.notifications.delete(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const handleClick = async (notification: any) => {
    if (!notification.read_at) markReadMutation.mutate(notification.id);
    const inviteToken =
      typeof notification.metadata?.invite_token === "string"
        ? notification.metadata.invite_token
        : "";
    if (notification.type === "team_invite_received" && inviteToken) {
      queryClient.invalidateQueries({ queryKey: ["people-me"] });
      navigate(`/account?team_invite=${encodeURIComponent(inviteToken)}`);
      return;
    }
    if (isVideoNotification(notification)) {
      const viewerLink = await resolveVideoNotificationViewerLink(notification);
      if (viewerLink) navigate(viewerLink);
      return;
    }
    // Long bodies (daily briefings, alerts) open a reader modal so the
    // content renders with section headings instead of a clipped blob.
    // Short notifications with just a title + link still navigate.
    const body = (notification.content || "").trim();
    if (body.length > 120) {
      setDetailOpen(notification);
      return;
    }
    const link = notification.link || notification.metadata?.link;
    if (link) navigate(link);
  };


  let unreadCount = notificationQuery.data?.unread_count ?? 0;

  const rows = useMemo(() => {
    const standardRows = showStandardNotifications
      ? (notificationQuery.data?.items || [])
          .filter((notification) => (
            typeFilter === ALL_NOTIFICATION_TYPES || notification.type === typeFilter
          ))
          .map((notification) => ({
            key: `notification:${notification.id}`,
            source: "notification" as const,
            notification,
            actionLabel: undefined,
          }))
      : [];
    return standardRows;
  }, [
    statusFilter,
    notificationQuery.data?.items,
    showStandardNotifications,
    typeFilter,
  ]);

  const isLoading = notificationQuery.isLoading
    ;
  let showEmpty = !isLoading && rows.length === 0;

  const statusTabs = [
    { key: "all", label: t("page.workspaces.filter_all") },
    { key: "unread", label: t("page.notifications.unread"), count: unreadCount || undefined },
  ];
  const typeOptions = useMemo(() => {
    const availableTypes = new Set<string>();
    for (const notification of notificationQuery.data?.items || []) {
      if (notification.type) availableTypes.add(notification.type);
    }
    if (typeFilter !== ALL_NOTIFICATION_TYPES) availableTypes.add(typeFilter);
    return [
      {
        value: ALL_NOTIFICATION_TYPES,
        label: t("page.knowledge.all_types"),
        icon: <IconFilter size={16} style={{ color: "var(--text-muted)" }} />,
      },
      ...Array.from(availableTypes)
        .sort((a, b) => notificationTypeLabel(a).localeCompare(notificationTypeLabel(b)))
        .map((type) => ({
          value: type,
          label: notificationTypeLabel(type),
          icon: <NotificationTypeIcon type={type} size={16} />,
        })),
    ];
  }, [
    notificationQuery.data?.items,
    typeFilter,
  ]);

  const updateFilters = (nextStatus: StatusFilter, nextType: string) => {
    const nextParams = new URLSearchParams();
    if (nextStatus !== "all") nextParams.set("filter", nextStatus);
    if (nextType !== ALL_NOTIFICATION_TYPES) nextParams.set("type", nextType);
    setSearchParams(nextParams, { replace: true });
  };

  // An empty inbox is only its empty state: no count line, filters or
  // a disabled "mark all read" repeating that there is nothing here.
  const filtersNarrowed = statusFilter !== "all" || typeFilter !== ALL_NOTIFICATION_TYPES;
  const showControls = rows.length > 0 || filtersNarrowed;

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: 0 }}>
      <PageHeader
        title={t("nav.notifications")}
        actions={unreadCount > 0 &&
          <button
            onClick={() => markAllReadMutation.mutate()}
            disabled={markAllReadMutation.isPending || unreadCount === 0}
            className="btn-manor-ghost"
            style={{ opacity: markAllReadMutation.isPending || unreadCount === 0 ? 0.5 : 1 }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {markAllReadMutation.isPending ? t("page.notifications.marking") : t("page.notifications.mark_all_read")}
          </button>
        }
      />

      {showControls && <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        flexWrap: "wrap",
        gap: 12,
        marginBottom: 20,
      }}>
        <TabSwitcher
          tabs={statusTabs}
          value={statusFilter}
          onChange={(value) => updateFilters(value as StatusFilter, typeFilter)}
        />
        <Select
          value={typeFilter}
          onChange={(value) => updateFilters(statusFilter, value)}
          options={typeOptions}
          appearance="ghost"
          filterable={typeOptions.length > 7}
          showSelectedIcon
          ariaLabel={t("page.knowledge.all_types")}
          style={{ width: 220, maxWidth: "100%" }}
          dropdownMinWidth={220}
        />
      </div>}

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}


      {/* Empty */}
      {showEmpty && (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d1d1d6" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
            </svg>
          }
          title={
            t("page.notifications.no_notifications_yet")
          }
          description={
            t("page.notifications.you_will_see_updates_here_when_something_happens")
          }
        />
      )}

      {/* List */}
      {!isLoading && rows.length > 0 && (
        <div className="inset-list">
          {rows.map((row) => (
            <StandardRow
              key={row.key}
              notification={row.notification}
              onClick={() => {
                if (row.source === "notification") {
                  void handleClick(row.notification);
                  return;
                }
              }}
              onDelete={() => {
                if (row.source === "notification") {
                  deleteMutation.mutate(row.notification.id);
                  return;
                }
              }}
              actionLabel={row.actionLabel}
            />
          ))}
        </div>
      )}

      <NotificationDetailModal
        open={!!detailOpen}
        notification={detailOpen}
        onClose={() => setDetailOpen(null)}
        onOpenLink={(link) => { setDetailOpen(null); navigate(link); }}
      />
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   StandardRow — the original notification row, preserved for every
   non-briefing notification type.
   ══════════════════════════════════════════════════════════════════════════ */

function StandardRow({
  notification: n, onClick, onDelete, actionLabel,
}: {
  notification: any;
  onClick: () => void;
  onDelete: () => void;
  actionLabel?: string;
}) {
  return (
    <div
      onClick={onClick}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      }}
      role="button"
      tabIndex={0}
      className="inset-row"
    >
      <NotificationTypeIcon type={n.type} size={18} />
      <div className="inset-row-copy">
        <div className="inset-row-title">
          <span className={n.read_at ? "" : "is-unread"}>{localizeEventText(n.title || "") || t("page.notifications.notification")}</span>
          {!n.read_at && <span className="inset-row-dot" aria-hidden="true" />}
        </div>
        {n.content && <div className="inset-row-desc">{localizeEventText(n.content)}</div>}
      </div>
      <RowMeta n={n} onDelete={onDelete} actionLabel={actionLabel} />
    </div>
  );
}

function RowMeta({
  n,
  onDelete,
  actionLabel,
}: {
  n: any;
  onDelete: () => void;
  actionLabel?: string;
}) {
  return (
    <div className="inset-row-meta">
      <span>{n.created_at ? relativeTime(n.created_at) : ""}</span>
      <button
        type="button"
        className="inset-row-action"
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
        title={actionLabel || t("action.delete")}
        aria-label={actionLabel || t("action.delete")}
      >
        <IconTrash size={14} />
      </button>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   NotificationDetailModal — reads ``notification.content`` as the
   plaintext body the backend produces (daily briefings, alerts, system
   reports) and renders it with proper typography. Parses the modest
   markdown-ish conventions we actually use in these bodies:

     ``--- Section Name ---``       → section heading
     ``# …`` / ``## …``             → section heading
     lines like ``  1. item``       → ordered list item
     lines like ``- item`` / ``* …``→ bullet list item
     ``key: value``                 → definition row
     blank lines                    → paragraph break

   Anything that doesn't match falls through as a plain paragraph.
   ══════════════════════════════════════════════════════════════════════════ */

type Block =
  | { kind: "heading"; text: string }
  | { kind: "para"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "kv"; key: string; value: string };

type BriefingTone = "neutral" | "warn" | "danger";

function isDailyBriefingNotification(notification: any) {
  const kind = notification?.metadata?.kind;
  const title = String(notification?.title || "");
  const content = String(notification?.content || "");
  return (
    kind === "daily_briefing" ||
    title.toLowerCase().includes("daily briefing") ||
    content.startsWith("Manor Daily Briefing")
  );
}

function metaNumber(meta: Record<string, any>, group: string, key: string) {
  const value = meta?.[group]?.[key];
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

function cleanBriefingCopy(text: string) {
  return text
    .replace(/\b1 ([A-Za-z][A-Za-z -]*?)\(s\)/g, "1 $1")
    .replace(/\b(\d+) ([A-Za-z][A-Za-z -]*?)\(s\)/g, (_match, count, noun) => `${count} ${noun}s`)
    .replace(/\s+/g, " ")
    .trim();
}

type BriefingRow = { label: string; value: string; tone?: BriefingTone };

function toneColors(tone: BriefingTone) {
  if (tone === "danger") {
    return { fg: "#b8423c", bg: "rgba(214,95,89,0.08)", border: "rgba(214,95,89,0.16)" };
  }
  if (tone === "warn") {
    return { fg: "#8c5e25", bg: "rgba(207,155,68,0.08)", border: "rgba(207,155,68,0.18)" };
  }
  return { fg: "#6e6e73", bg: "rgba(245,245,244,0.72)", border: "rgba(29, 29, 31,0.06)" };
}

function isClearBriefingText(text: string) {
  const normalized = cleanBriefingCopy(text).toLowerCase();
  return (
    normalized === "all clear" ||
    normalized === "none" ||
    normalized === "no alerts" ||
    normalized.includes("no urgent actions required")
  );
}

function hasPositiveCount(text: string) {
  return /\b[1-9]\d*\b/.test(text);
}

function hasPositiveRiskTerm(text: string, terms: string[]) {
  return text
    .toLowerCase()
    .split(/[|;,]/)
    .some((part) => hasPositiveCount(part) && terms.some((term) => part.includes(term)));
}

function briefingRowTone(row: BriefingRow): BriefingTone {
  const text = `${row.label} ${row.value}`.toLowerCase();
  if (hasPositiveRiskTerm(text, ["overdue", "blocked", "broken", "failure", "failed"])) return "danger";
  if (hasPositiveRiskTerm(text, ["stalled", "errored", "awaiting", "waiting", "intervention"])) return "warn";
  return "neutral";
}

function briefingTextTone(text: string): BriefingTone {
  if (hasPositiveRiskTerm(text, ["overdue", "blocked", "broken", "failure", "failed"])) return "danger";
  if (hasPositiveRiskTerm(text, ["stalled", "errored", "awaiting", "waiting", "intervention"])) return "warn";
  return "neutral";
}

function BriefingValue({ value, fallbackTone = "neutral" }: { value: string; fallbackTone?: BriefingTone }) {
  const parts = value.split("|").map((part) => part.trim()).filter(Boolean);
  if (parts.length <= 1) {
    const colors = toneColors(fallbackTone);
    return (
      <span style={{ color: colors.fg, fontWeight: fallbackTone !== "neutral" ? 720 : 500 }}>
        {value}
      </span>
    );
  }

  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: "4px 8px", alignItems: "center" }}>
      {parts.map((part, index) => {
        const tone = briefingTextTone(part);
        const colors = toneColors(tone);
        return (
          <span key={`${part}-${index}`} style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            {index > 0 && <span style={{ color: "var(--text-faint)", fontWeight: 500 }}>·</span>}
            <span style={{ color: colors.fg, fontWeight: tone !== "neutral" ? 720 : 500 }}>
              {part}
            </span>
          </span>
        );
      })}
    </span>
  );
}

function getBriefingSectionRows(blocks: Block[], sectionName: string): BriefingRow[] {
  const rows: BriefingRow[] = [];
  let inSection = false;

  for (const block of blocks) {
    if (block.kind === "heading") {
      inSection = block.text.toLowerCase() === sectionName.toLowerCase();
      continue;
    }
    if (!inSection) continue;
    if (block.kind === "kv") {
      const row = {
        label: block.key,
        value: cleanBriefingCopy(block.value),
      };
      rows.push({ ...row, tone: briefingRowTone(row) });
    }
  }
  return rows;
}

function getBriefingActions(blocks: Block[]) {
  const actions: string[] = [];
  let inActionSection = false;

  for (const block of blocks) {
    if (block.kind === "heading") {
      inActionSection = block.text.toLowerCase() === "action items";
      continue;
    }
    if (inActionSection && block.kind === "list") {
      actions.push(...block.items.map(cleanBriefingCopy));
    }
  }
  return actions;
}

function usefulBriefingRow(row: BriefingRow) {
  if (isClearBriefingText(row.value)) return false;
  return hasPositiveCount(row.value);
}

function BriefingDetailRows({ rows }: { rows: BriefingRow[] }) {
  return (
    <section style={{ borderTop: "0.5px solid rgba(29, 29, 31,0.08)", paddingTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 820, color: "var(--text-strong)", marginBottom: 9 }}>
        {t("ui.details")}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        {rows.map((row) => {
          return (
            <div
              key={`${row.label}-${row.value}`}
              style={{
                display: "grid",
                gridTemplateColumns: "132px minmax(0, 1fr)",
                gap: 12,
                alignItems: "baseline",
                fontSize: 13,
                lineHeight: 1.45,
              }}
            >
              <span style={{ color: "var(--text-muted)", fontWeight: 650 }}>{row.label}</span>
              <BriefingValue value={row.value} fallbackTone={row.tone || "neutral"} />
            </div>
          );
        })}
      </div>
    </section>
  );
}

function parseNotificationBody(raw: string): Block[] {
  const lines = raw.replace(/\r/g, "").split("\n");
  const out: Block[] = [];
  let listBuf: { ordered: boolean; items: string[] } | null = null;

  const flushList = () => {
    if (listBuf) {
      out.push({ kind: "list", ...listBuf });
      listBuf = null;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();

    // Blank — paragraph / list break
    if (!trimmed) { flushList(); continue; }

    // Divider heading  --- Section ---
    const divider = trimmed.match(/^-{2,}\s*(.+?)\s*-{2,}$/);
    if (divider) {
      flushList();
      out.push({ kind: "heading", text: divider[1] });
      continue;
    }

    // Hash heading  # / ## Section
    const hash = trimmed.match(/^#{1,3}\s+(.*)$/);
    if (hash) {
      flushList();
      out.push({ kind: "heading", text: hash[1] });
      continue;
    }

    // Ordered list  "  1. item"
    const ol = trimmed.match(/^\d+[.)]\s+(.*)$/);
    if (ol) {
      if (!listBuf || !listBuf.ordered) { flushList(); listBuf = { ordered: true, items: [] }; }
      listBuf.items.push(ol[1]);
      continue;
    }

    // Bullet list  "- item" or "* item" or "• item"
    const ul = trimmed.match(/^[-*•]\s+(.*)$/);
    if (ul) {
      if (!listBuf || listBuf.ordered) { flushList(); listBuf = { ordered: false, items: [] }; }
      listBuf.items.push(ul[1]);
      continue;
    }

    // Key: Value  (short key, no pipe/colon-in-value edge cases covered)
    const kv = trimmed.match(/^([A-Za-z][\w\s()]{0,40}):\s+(.+)$/);
    if (kv && kv[1].length < 30) {
      flushList();
      out.push({ kind: "kv", key: kv[1].trim(), value: kv[2].trim() });
      continue;
    }

    flushList();
    out.push({ kind: "para", text: trimmed });
  }
  flushList();
  return out;
}

function DailyBriefingBody({ notification }: { notification: any }) {
  const meta = (notification?.metadata || {}) as Record<string, any>;
  const parsedBlocks = parseNotificationBody(String(notification?.content || ""));
  const healthRows = getBriefingSectionRows(parsedBlocks, "Platform Health");
  const overviewRows = getBriefingSectionRows(parsedBlocks, "Business Overview");
  const alertRow = healthRows.find((row) => row.label.toLowerCase() === "alerts");
  const overdueTasks = metaNumber(meta, "tasks", "overdue");
  const stalledTasks = metaNumber(meta, "tasks", "stalled");
  const blockedTasks = metaNumber(meta, "tasks", "blocked");
  const erroredJobs = metaNumber(meta, "jobs", "errored");
  const brokenJobs = metaNumber(meta, "jobs", "broken");
  const stuckGoals = metaNumber(meta, "goals", "stuck_hitl");
  const metadataAlerts = Array.isArray(meta.alerts) ? meta.alerts.filter(Boolean).map((item: unknown) => cleanBriefingCopy(String(item))) : [];
  const bodyAlerts = alertRow?.value
    ? alertRow.value.split(";").map(cleanBriefingCopy).filter(Boolean)
    : [];
  const alertSource = alertRow?.value ? bodyAlerts : metadataAlerts;
  const alerts = alertSource.filter((item) => !isClearBriefingText(item));
  const fallbackActionItems = getBriefingActions(parsedBlocks);
  const actionItems = Array.isArray(meta.action_items) && meta.action_items.length > 0
    ? meta.action_items.filter(Boolean).map((item: unknown) => cleanBriefingCopy(String(item)))
    : fallbackActionItems;
  const usefulActions = actionItems.filter((item) => !/no urgent actions required/i.test(item));

  const overviewDetails = overviewRows.filter(usefulBriefingRow);
  const hasOpenWorkload = overviewDetails.some((row) => row.label.toLowerCase() === "open workload");
  const healthDetails = healthRows
    .filter((row) => row.label.toLowerCase() !== "alerts")
    .filter((row) => !(hasOpenWorkload && row.label.toLowerCase() === "tasks"))
    .filter(usefulBriefingRow);
  const detailRows = [...overviewDetails, ...healthDetails];
  const metadataRisk = overdueTasks + stalledTasks + blockedTasks + erroredJobs + brokenJobs + stuckGoals > 0;
  const rowRisk = detailRows.some((row) => row.tone === "warn" || row.tone === "danger");
  const hasRisk = alerts.length > 0 || rowRisk || (!alertRow && metadataRisk);

  const statusColor = hasRisk ? "#8c5e25" : "#3f6f68";
  const statusBody = alerts.length > 0
    ? alerts.join(" ")
    : hasRisk
      ? "Review the highlighted operational items."
      : "No urgent operational issues in this briefing.";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <section
        style={{
          border: `1px solid ${hasRisk ? "rgba(207,155,68,0.18)" : "rgba(79,125,117,0.16)"}`,
          borderRadius: 16,
          background: hasRisk ? "rgba(255,250,240,0.76)" : "rgba(248,250,249,0.9)",
          padding: "14px 16px",
        }}
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 11 }}>
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: 10,
              background: hasRisk ? "rgba(207,155,68,0.12)" : "rgba(79,125,117,0.10)",
              color: statusColor,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            {hasRisk ? <IconWarning size={17} /> : <IconCheckCircle size={17} />}
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 820, color: "var(--text-strong)" }}>
              {hasRisk ? "Needs attention" : "All clear"}
            </div>
            <div style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)", lineHeight: 1.45 }}>
              {statusBody}
            </div>
          </div>
        </div>
      </section>

      {detailRows.length > 0 && <BriefingDetailRows rows={detailRows} />}

      <section
        style={{
          borderTop: "0.5px solid rgba(29, 29, 31,0.08)",
          paddingTop: 14,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13, fontWeight: 820, color: "var(--text-strong)", marginBottom: 10 }}>
          <IconChecklist size={15} style={{ color: "#4f7d75" }} />
          {t("ui.action_items")}
        </div>
        {usefulActions.length > 0 ? (
          <ol style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 9 }}>
            {usefulActions.map((item, index) => (
              <li key={`${item}-${index}`} style={{ display: "flex", alignItems: "flex-start", gap: 9 }}>
                <span
                  style={{
                    width: 20,
                    height: 20,
                    borderRadius: 999,
                    background: "rgba(79,125,117,0.09)",
                    color: "#3f6f68",
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 10,
                    fontWeight: 850,
                    flexShrink: 0,
                  }}
                >
                  {index + 1}
                </span>
                <span style={{ fontSize: 13, color: "var(--text-default)", lineHeight: 1.5 }}>{item}</span>
              </li>
            ))}
          </ol>
        ) : (
          <div style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.45 }}>
            {t("ui.no_action_needed_today")}
          </div>
        )}
      </section>
    </div>
  );
}

function NotificationDetailModal({
  open, notification, onClose, onOpenLink,
}: {
  open: boolean;
  notification: any;
  onClose: () => void;
  onOpenLink: (path: string) => void;
}) {
  const body = (notification?.content || "").trim();
  const blocks = body ? parseNotificationBody(body) : [];
  const link = notification?.link || notification?.metadata?.link;
  const isDailyBriefing = isDailyBriefingNotification(notification);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={notification?.title || t("page.notifications.notification")}
      maxWidth={isDailyBriefing ? "620px" : "600px"}
      footer={
        <>
          <Button variant="outline" onClick={onClose}>{t("page.flows.close")}</Button>
          {link && (
            <Button variant="primary" onClick={() => onOpenLink(link)}>
              {t("page.notifications.open")}
            </Button>
          )}
        </>
      }
    >
      {isDailyBriefing ? (
        <DailyBriefingBody notification={notification} />
      ) : (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {blocks.length === 0 && (
          <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
            {t("page.notifications.no_body_content")}
          </div>
        )}
        {blocks.map((b, i) => {
          if (b.kind === "heading") {
            return (
              <div key={i} style={{
                marginTop: i === 0 ? 0 : 6,
                fontSize: 11, fontWeight: 800,
                color: "var(--text-muted)",
              }}>
                {b.text}
              </div>
            );
          }
          if (b.kind === "kv") {
            return (
              <div key={i} style={{ fontSize: 13, color: "var(--text-default)", lineHeight: 1.6 }}>
                <span style={{ color: "var(--text-muted)", fontWeight: 600 }}>{b.key}: </span>
                <span>{b.value}</span>
              </div>
            );
          }
          if (b.kind === "list") {
            return (
              <ul key={i} style={{
                margin: 0, paddingLeft: 0, listStyle: "none",
                display: "flex", flexDirection: "column", gap: 4,
              }}>
                {b.items.map((item, j) => (
                  <li key={j} style={{
                    display: "flex", alignItems: "flex-start", gap: 8,
                    fontSize: 13, color: "var(--text-muted)", lineHeight: 1.5,
                  }}>
                    <span style={{
                      flexShrink: 0, color: "#436b65", fontWeight: 700,
                      minWidth: b.ordered ? 18 : 10,
                    }}>
                      {b.ordered ? `${j + 1}.` : "•"}
                    </span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            );
          }
          // paragraph
          return (
            <p key={i} style={{
              margin: 0, fontSize: 13, color: "var(--text-muted)", lineHeight: 1.55,
            }}>
              {b.text}
            </p>
          );
        })}
      </div>
      )}
    </Modal>
  );
}

function isVideoNotification(notification: any): boolean {
  return notification?.type === "video" || Boolean(notification?.metadata?.job_id);
}

async function resolveVideoNotificationViewerLink(notification: any): Promise<string | null> {
  const meta = notification?.metadata || {};
  const link = notification?.link || meta.link;
  if (typeof link === "string" && link.startsWith("/viewer/")) return link;
  if (typeof meta.document_id === "string" && meta.document_id) return `/viewer/${meta.document_id}`;

  const fsPath = fsPathFromResultUrl(meta.result_url || link);
  if (!fsPath) return null;

  try {
    const docs = await api.documents.list({
      include_generated_assets: true,
      search: fsPath.split("/").pop(),
      limit: 100,
    });
    const match = docs.items.find((doc) => doc.fs_path === fsPath);
    return match ? `/viewer/${match.id}` : null;
  } catch {
    return null;
  }
}

function fsPathFromResultUrl(value: unknown): string | null {
  const parsed = parseFileReference(value);
  return parsed?.kind === "fs" && parsed.entity ? parsed.fs_path : null;
}
