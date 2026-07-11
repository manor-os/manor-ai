import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import PageHeader from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Modal from "../components/ui/Modal";
import Button from "../components/ui/Button";
import {
  IconChecklist,
  IconCheckCircle,
  IconWarning,
} from "../components/icons";

import { t } from "../lib/i18n";
type FilterMode = "all" | "unread";

const TYPE_STYLES: Record<string, { bg: string; color: string; icon: string }> = {
  task: {
    bg: "#e3ebe8",
    color: "#436b65",
    icon: "M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z",
  },
  system: {
    bg: "#e3e9f1",
    color: "#4869ac",
    icon: "M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z",
  },
  warning: {
    bg: "#f3ecd6",
    color: "#b27c34",
    icon: "M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z",
  },
  document: {
    bg: "#f3e8ff",
    color: "#6f4ba8",
    icon: "M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z",
  },
  agent: {
    bg: "#dceae3",
    color: "#437f6b",
    icon: "M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z",
  },
  chat: {
    bg: "#e8eff4",
    color: "#4a7d96",
    icon: "M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z",
  },
  team_invite_received: {
    bg: "#e5eeeb",
    color: "#436b65",
    icon: "M18 9v3m0 0v3m0-3h3m-3 0h-3m-2.25-4.125A3.375 3.375 0 118.625 4.5a3.375 3.375 0 014.125 3.375zM3 19.5a7.5 7.5 0 0115 0v.75H3v-.75z",
  },
  team_invite_sent: {
    bg: "#f1f6f5",
    color: "#4f7e87",
    icon: "M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0l-7.5-4.615a2.25 2.25 0 01-1.07-1.916V6.75",
  },
  error: {
    bg: "#f1dddb",
    color: "#c14a44",
    icon: "M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z",
  },
};

const DEFAULT_TYPE = {
  bg: "#f5f5f4",
  color: "#78716c",
  icon: "M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9",
};

export default function Notifications() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [filter, setFilter] = useState<FilterMode>("all");
  const [detailOpen, setDetailOpen] = useState<any | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["notifications", filter],
    queryFn: () =>
      api.notifications.list({
        unread_only: filter === "unread" ? true : undefined,
      }),
  });

  const markReadMutation = useMutation({
    mutationFn: (id: string) => api.notifications.markRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const markAllReadMutation = useMutation({
    mutationFn: () => api.notifications.markAllRead(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
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

  const unreadCount = data?.unread_count ?? 0;

  const filterTabs = [
    { key: "all", label: t("page.workspaces.filter_all") },
    { key: "unread", label: t("page.notifications.unread"), count: unreadCount || undefined },
  ];

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: "1.5rem 2rem" }}>
      <PageHeader
        title={t("nav.notifications")}
        subtitle={unreadCount > 0 ? `${unreadCount} unread` : t("page.notifications.you_re_all_caught_up")}
        actions={
          <button
            onClick={() => markAllReadMutation.mutate()}
            disabled={markAllReadMutation.isPending || unreadCount === 0}
            className="btn-manor-teal-light"
            style={{ opacity: markAllReadMutation.isPending || unreadCount === 0 ? 0.5 : 1 }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {markAllReadMutation.isPending ? t("page.notifications.marking") : t("page.notifications.mark_all_read")}
          </button>
        }
      />

      <div style={{ marginBottom: 20 }}>
        <TabSwitcher
          tabs={filterTabs}
          value={filter}
          onChange={(v) => setFilter(v as FilterMode)}
        />
      </div>

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}

      {/* Empty */}
      {!isLoading && (!data?.items || data.items.length === 0) && (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
            </svg>
          }
          title={t("page.notifications.no_notifications_yet")}
          description={t("page.notifications.you_will_see_updates_here_when_something_happens")}
        />
      )}

      {/* List */}
      {!isLoading && data?.items && data.items.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {data.items.map((n: any) => (
            <StandardRow
              key={n.id}
              notification={n}
              onClick={() => handleClick(n)}
              onDelete={() => deleteMutation.mutate(n.id)}
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
  notification: n, onClick, onDelete,
}: { notification: any; onClick: () => void; onDelete: () => void }) {
  const style = TYPE_STYLES[n.type] || DEFAULT_TYPE;
  return (
    <div
      onClick={onClick}
      className="glass-card-sm"
      style={{
        cursor: "pointer",
        borderLeft: !n.read_at ? "3px solid #4f7d75" : undefined,
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <div
          style={{
            width: 40, height: 40, borderRadius: 10,
            background: style.bg,
            display: "flex", alignItems: "center", justifyContent: "center",
            flexShrink: 0,
          }}
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={style.color} strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d={style.icon} />
          </svg>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <p style={{
              fontSize: 14, fontWeight: !n.read_at ? 700 : 600,
              color: !n.read_at ? "#292524" : "#78716c",
              margin: 0, overflow: "hidden",
              textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
              {n.title || t("page.notifications.notification")}
            </p>
            {!n.read_at && (
              <span style={{
                width: 6, height: 6, borderRadius: "50%",
                background: "#4f7d75", flexShrink: 0,
                boxShadow: "0 0 0 3px rgba(79, 125, 117, 0.15)",
                animation: "pulse 2s cubic-bezier(0.4,0,0.6,1) infinite",
              }} />
            )}
          </div>
          {n.content && (
            <p style={{
              fontSize: 13, color: "#a8a29e", margin: "4px 0 0 0",
              display: "-webkit-box", WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical", overflow: "hidden",
            }}>
              {n.content}
            </p>
          )}
        </div>
        <RowMeta n={n} onDelete={onDelete} />
      </div>
    </div>
  );
}

function RowMeta({ n, onDelete }: { n: any; onDelete: () => void }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column",
      alignItems: "flex-end", gap: 4, flexShrink: 0,
    }}>
      <span style={{ fontSize: 12, color: "#a8a29e" }}>
        {n.created_at ? relativeTime(n.created_at) : ""}
      </span>
      <button
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
        style={{
          background: "none", border: "none", cursor: "pointer",
          color: "#d6d3d1", padding: 2, transition: "color 0.2s", lineHeight: 0,
        }}
        onMouseEnter={(e) => { e.currentTarget.style.color = "#d65f59"; }}
        onMouseLeave={(e) => { e.currentTarget.style.color = "#d6d3d1"; }}
        title={t("action.delete")}
      >
        <svg width="14" height="14" fill="none" viewBox="0 0 24 24"
             stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round"
                d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
        </svg>
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
  return { fg: "#57534e", bg: "rgba(245,245,244,0.72)", border: "rgba(28,25,23,0.06)" };
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
            {index > 0 && <span style={{ color: "#d6d3d1", fontWeight: 500 }}>·</span>}
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
    <section style={{ borderTop: "1px solid rgba(28,25,23,0.08)", paddingTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 820, color: "#292524", marginBottom: 9 }}>
        Details
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
              <span style={{ color: "#78716c", fontWeight: 650 }}>{row.label}</span>
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
            <div style={{ fontSize: 14, fontWeight: 820, color: "#292524" }}>
              {hasRisk ? "Needs attention" : "All clear"}
            </div>
            <div style={{ marginTop: 4, fontSize: 13, color: "#57534e", lineHeight: 1.45 }}>
              {statusBody}
            </div>
          </div>
        </div>
      </section>

      {detailRows.length > 0 && <BriefingDetailRows rows={detailRows} />}

      <section
        style={{
          borderTop: "1px solid rgba(28,25,23,0.08)",
          paddingTop: 14,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13, fontWeight: 820, color: "#292524", marginBottom: 10 }}>
          <IconChecklist size={15} style={{ color: "#4f7d75" }} />
          Action items
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
                <span style={{ fontSize: 13, color: "#44403c", lineHeight: 1.5 }}>{item}</span>
              </li>
            ))}
          </ol>
        ) : (
          <div style={{ fontSize: 13, color: "#78716c", lineHeight: 1.45 }}>
            No action needed today.
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
          <div style={{ fontSize: 13, color: "#a8a29e" }}>
            {t("page.notifications.no_body_content")}
          </div>
        )}
        {blocks.map((b, i) => {
          if (b.kind === "heading") {
            return (
              <div key={i} style={{
                marginTop: i === 0 ? 0 : 6,
                fontSize: 11, fontWeight: 800,
                letterSpacing: "0.08em",
                textTransform: "uppercase" as const,
                color: "#78716c",
              }}>
                {b.text}
              </div>
            );
          }
          if (b.kind === "kv") {
            return (
              <div key={i} style={{ fontSize: 13, color: "#44403c", lineHeight: 1.6 }}>
                <span style={{ color: "#78716c", fontWeight: 600 }}>{b.key}: </span>
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
                    fontSize: 13, color: "#44403c", lineHeight: 1.5,
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
              margin: 0, fontSize: 13, color: "#44403c", lineHeight: 1.55,
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
  if (typeof value !== "string" || !value) return null;
  try {
    const parsed = new URL(value, window.location.origin);
    const path = parsed.pathname;
    const match = path.match(/^\/api\/v1\/fs\/[^/]+\/(.+)$/);
    return match ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}
