import { useState, useEffect, useCallback, useRef, useMemo, lazy, Suspense, type DragEvent, type KeyboardEvent, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import { useAuthStore } from "../stores/auth";
import { useWorkspaceFilter } from "../stores/workspace";
import type { BookingRecord, DailyAgendaItem, ExternalCalendarEvent as ApiExternalCalendarEvent, Task, Workspace, WorkspaceStaff } from "../lib/types";
import { formatDateShort as formatDate, formatDateFull as formatDateFull, formatDateLong, isDeadlineOverdue } from "../lib/format";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import SmartToolbar from "../components/ui/SmartToolbar";
import GlassCard from "../components/ui/GlassCard";
import StatusBadge from "../components/ui/StatusBadge";
import Modal from "../components/ui/Modal";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Calendar from "../components/ui/Calendar";
import type { CalendarEvent } from "../components/ui/Calendar";
import { IconClose, IconClock, IconFlag, IconUser, IconUsers, IconComment, IconCircleDot, IconCalendar, IconCategory, IconEdit, IconSend, IconUpload, IconDocument, IconDownload, IconExternalLink, IconManorLogo, IconAgent, IconPlay, IconHeadphones, IconCheckCircle, IconTrash, IconPaperclip, IconWorkspace, IconBuilding, IconLayers, IconWarning } from "../components/icons";
import Select from "../components/ui/Select";
import DateTimePicker from "../components/ui/DateTimePicker";
import TabSwitcher from "../components/ui/TabSwitcher";
import CategoryChip from "../components/ui/CategoryChip";
import UserAvatar from "../components/ui/UserAvatar";
import PriorityPill from "../components/ui/PriorityPill";
import StatusPill from "../components/ui/StatusPill";
import { FilterBar, FilterSelect } from "../components/ui/FilterBar";
import { MANOR_AGENT_ID, MANOR_AGENT_TYPE, MANOR_AGENT_NAME, isMasterAgent } from "../lib/constants";
import TaskPropertiesPanel from "../components/task/TaskPropertiesPanel";
import TaskLogItem from "../components/task/TaskLogItem";
import TaskRecoveryPanel from "../components/task/TaskRecoveryPanel";
import ChatMarkdown from "../components/ChatMarkdown";
import InlineFileReferenceCard from "../components/InlineFileReferenceCard";
import { t } from "../lib/i18n";
import { getAgentDescription } from "../lib/localizedContent";
import { inferRuntimeRuleFromText, shouldFallbackToWildcardRule } from "../lib/runtimeRules";
import { formatTaskDescriptionForDisplay, formatUserFacingLabel, formatUserFacingStructuredText, formatUserFacingText, friendlyPersonName } from "../lib/taskDisplay";

const LazyScheduledJobs = lazy(() => import("./ScheduledJobs"));
function ScheduledJobsEmbed({ headerTabs }: { headerTabs?: React.ReactNode }) {
  return (
    <Suspense fallback={<div style={{ padding: 40, textAlign: "center", color: "#a8a29e" }}>{t("status.loading")}</div>}>
      <LazyScheduledJobs headerTabs={headerTabs} />
    </Suspense>
  );
}

/* ── constants ──────────────────────────────────────────── */

const VIEW_TABS: { key: string; label: string; icon: React.ReactNode }[] = [
  {
    key: "board",
    label: t("page.tasks.board"),
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
      </svg>
    ),
  },
  {
    key: "calendar",
    label: t("page.tasks.calendar"),
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="4" width="18" height="18" rx="2" /><path d="M16 2v4M8 2v4M3 10h18" />
      </svg>
    ),
  },
  {
    key: "automations",
    label: t("page.tasks.automations"),
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
      </svg>
    ),
  },
];

/* ── Task constants (mirrors packages/core/constants/task.py) ── */

const TASK_STATUSES: Record<string, { label: string; color: string; order: number }> = {
  created:              { label: t("page.dashboard.created"),              color: "#a8a29e", order: 0 },
  proposed:             { label: t("page.tasks.proposed"),             color: "#a78bfa", order: 1 },
  pending:              { label: t("status.pending"),              color: "#cf9b44", order: 2 },
  scheduled:            { label: t("status.scheduled"),            color: "#9079c2", order: 3 },
  in_progress:          { label: t("status.in_progress"),          color: "#4a7d96", order: 4 },
  waiting_on_customer:  { label: t("page.tasks.waiting_on_customer"),  color: "#d3873f", order: 5 },
  on_hold:              { label: t("page.tasks.on_hold"),              color: "#a07fc0", order: 6 },
  blocked:              { label: t("page.tasks.blocked"),              color: "#d65f59", order: 7 },
  completed:            { label: t("status.completed"),            color: "#4f9c84", order: 8 },
  cancelled:            { label: t("status.cancelled"),            color: "#78716c", order: 9 },
  failed:               { label: t("page.dashboard.failed"),               color: "#c14a44", order: 10 },
};

// Board groups 10 statuses into 5 visual columns (like old repo)
const BOARD_COLUMNS = ["todo", "scheduled", "in_progress", "review", "done"] as const;

type ColumnMeta = {
  label: string;
  dot: string;
  headerBg: string;
  statuses: string[];
  Icon: React.ComponentType<{ size?: number; className?: string; style?: React.CSSProperties }>;
};

const COLUMN_META: Record<string, ColumnMeta> = {
  todo:        { label: t("page.tasks.to_do"),            dot: "#cf9b44", headerBg: "rgba(207,155,68,0.08)",  statuses: ["created", "pending", "proposed"],                Icon: IconClock },
  scheduled:   { label: t("status.scheduled"),        dot: "#9079c2", headerBg: "rgba(144,121,194,0.09)",  statuses: ["scheduled"],                                     Icon: IconCalendar },
  in_progress: { label: t("status.in_progress"),      dot: "#4a7d96", headerBg: "rgba(90,142,166,0.09)",   statuses: ["in_progress"],                                   Icon: IconPlay },
  review:      { label: t("page.tasks.needs_attention"), dot: "#d3873f", headerBg: "rgba(211,135,63,0.08)",  statuses: ["waiting_on_customer", "on_hold", "blocked", "failed"],     Icon: IconHeadphones },
  done:        { label: t("page.team_people.done"),             dot: "#4f9c84", headerBg: "rgba(79,156,132,0.08)",  statuses: ["completed", "cancelled"],              Icon: IconCheckCircle },
};

const STATUS_OPTIONS = [
  { value: "proposed", label: t("page.tasks.proposed") }, { value: "created", label: t("page.dashboard.created") }, { value: "pending", label: t("status.pending") },
  { value: "scheduled", label: t("status.scheduled") }, { value: "in_progress", label: t("status.in_progress") },
  { value: "waiting_on_customer", label: t("page.goal_explorer.waiting") }, { value: "on_hold", label: t("page.tasks.on_hold") },
  { value: "blocked", label: t("page.tasks.blocked") }, { value: "completed", label: t("status.completed") },
  { value: "cancelled", label: t("status.cancelled") }, { value: "failed", label: t("page.dashboard.failed") },
];

const TASK_POLL_INTERVAL_MS = 60_000;
const ENTITY_LEVEL_WORKSPACE_FILTER = "__entity__";
const LIVE_TASK_STATUSES = new Set(["pending", "in_progress"]);
const TERMINAL_TASK_STATUSES = new Set(["completed", "cancelled", "failed"]);
const DONE_RECENT_WINDOW_DAYS = 14;
const MS_PER_DAY = 86_400_000;

function hasLiveTasks(value: unknown): boolean {
  const tasks: unknown[] = Array.isArray((value as any)?.items)
    ? (value as any).items
    : value && typeof value === "object"
      ? Object.entries(value as Record<string, unknown>)
          .filter(([key]) => key !== "_counts")
          .flatMap(([, group]) => Array.isArray(group) ? group : [])
      : [];
  return tasks.some((task: any) => LIVE_TASK_STATUSES.has(String(task?.status || "")));
}

type TaskDependencyInfo = {
  status: "waiting" | "completed" | "blocked";
  dependencyIds: string[];
  outputs: any[];
};

function _taskDependencyInfo(task: Task): TaskDependencyInfo | null {
  const details = (task.details || {}) as Record<string, any>;
  const dependencyIds = Array.isArray(details.depends_on_task_ids)
    ? details.depends_on_task_ids.map((id: unknown) => String(id)).filter(Boolean)
    : [];
  const outputs = Array.isArray(details.dep_outputs) ? details.dep_outputs : [];
  if (dependencyIds.length === 0 && outputs.length === 0) return null;
  const rawStatus = String(details.dependency_status || "");
  const status: TaskDependencyInfo["status"] =
    rawStatus === "blocked"
      ? "blocked"
      : rawStatus === "completed" || outputs.length > 0
        ? "completed"
        : "waiting";
  return { status, dependencyIds, outputs };
}

function DependencyStatusPill({ info, compact }: { info: TaskDependencyInfo; compact?: boolean }) {
  const count = Math.max(info.dependencyIds.length, info.outputs.length, 1);
  const palette = info.status === "blocked"
    ? { fg: "#a23e38", bg: "#f8f0ef", border: "#ecc8c5", label: t("page.tasks.dependency_blocked") }
    : info.status === "completed"
      ? { fg: "#436b65", bg: "#f5f5f4", border: "#ccded9", label: t("page.tasks.dependency_ready") }
      : { fg: "#76502c", bg: "#faf7ef", border: "#ecdca4", label: t("page.tasks.dependency_waiting") };
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      fontSize: compact ? 9 : 10, fontWeight: 700,
      color: palette.fg, background: palette.bg,
      border: `1px solid ${palette.border}`,
      padding: compact ? "1px 6px" : "2px 7px",
      borderRadius: 999,
    }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: palette.fg, opacity: 0.75 }} />
      {palette.label}{count > 1 ? ` · ${count}` : ""}
    </span>
  );
}

function matchesStatusFocus(task: Task, statusFocus: string): boolean {
  if (!statusFocus) return true;
  if (statusFocus === "overdue") {
    return isDeadlineOverdue(task.deadline, task.status);
  }
  return task.status === statusFocus;
}

const PRIORITY_CONFIG: Record<number, { label: string; color: string; badge: "danger" | "warning" | "info" | "teal" | "inactive" }> = {
  5: { label: t("page.tasks.critical"), color: "#d65f59", badge: "danger" },
  4: { label: t("page.tasks.high"),     color: "#d3873f", badge: "warning" },
  3: { label: t("page.tasks.medium"),   color: "#c3a63f", badge: "info" },
  2: { label: t("page.tasks.low"),      color: "#8aa9d1", badge: "teal" },
  1: { label: t("page.tasks.minimal"),  color: "#a8a29e", badge: "inactive" },
};

type TaskStatusFilter = "all" | "overdue" | typeof BOARD_COLUMNS[number];
type TaskOwnerFilter = "all" | "unassigned" | `agent:${string}` | `person:${string}`;
type TaskDueFilter = "all" | "overdue" | "today" | "upcoming" | "no_due";

type TaskFilterOption<T extends string = string> = {
  key: T;
  label: string;
  icon?: ReactNode;
};

const STATUS_FILTER_OPTIONS: TaskFilterOption<TaskStatusFilter>[] = [
  { key: "all", label: t("page.workspaces.filter_all"), icon: <IconCircleDot size={14} style={{ color: "#a8a29e" }} /> },
  { key: "overdue", label: t("page.task_detail.overdue"), icon: <IconWarning size={14} style={{ color: "#d65f59" }} /> },
  ...BOARD_COLUMNS.map((key) => {
    const Icon = COLUMN_META[key].Icon;
    return { key, label: COLUMN_META[key].label, icon: <Icon size={14} style={{ color: COLUMN_META[key].dot }} /> };
  }),
];

const DUE_FILTER_OPTIONS: TaskFilterOption<TaskDueFilter>[] = [
  { key: "all", label: t("page.workspaces.filter_all"), icon: <IconCalendar size={14} style={{ color: "#a8a29e" }} /> },
  { key: "overdue", label: t("page.task_detail.overdue"), icon: <IconWarning size={14} style={{ color: "#d65f59" }} /> },
  { key: "today", label: "Today", icon: <IconCalendar size={14} style={{ color: "#57534e" }} /> },
  { key: "upcoming", label: "Upcoming", icon: <IconClock size={14} style={{ color: "#4869ac" }} /> },
  { key: "no_due", label: "No due", icon: <IconClose size={14} style={{ color: "#a8a29e" }} /> },
];

function sameLocalDate(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}

function matchesStatusFilter(task: Task, filter: TaskStatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "overdue") return isDeadlineOverdue(task.deadline, task.status);
  return COLUMN_META[filter].statuses.includes(task.status);
}

function taskOwnerFilterKey(task: Task): TaskOwnerFilter {
  const isMaster = isMasterAgent(task.agent_id, task.agent_type);
  if (task.agent_id || isMaster) return `agent:${task.agent_id || MANOR_AGENT_ID}`;
  if (task.assignee_id) return `person:${task.assignee_id}`;
  return "unassigned";
}

function matchesOwnerFilter(task: Task, filter: TaskOwnerFilter): boolean {
  if (filter === "all") return true;
  return taskOwnerFilterKey(task) === filter;
}

function matchesWorkspaceFilter(task: Task, workspaceFilter?: string): boolean {
  if (!workspaceFilter) return true;
  if (workspaceFilter === ENTITY_LEVEL_WORKSPACE_FILTER) return !task.workspace_id;
  return task.workspace_id === workspaceFilter;
}

function matchesDueFilter(task: Task, filter: TaskDueFilter): boolean {
  if (filter === "all") return true;
  if (filter === "overdue") return isDeadlineOverdue(task.deadline, task.status);
  if (!task.deadline) return filter === "no_due";
  const deadline = new Date(task.deadline);
  if (Number.isNaN(deadline.getTime())) return filter === "no_due";
  const now = new Date();
  if (filter === "today") return sameLocalDate(deadline, now);
  if (filter === "upcoming") return deadline > now && !TERMINAL_TASK_STATUSES.has(task.status);
  return false;
}

function taskMatchesSearch(task: Task, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [
    task.title,
    task.description,
    task.agent_name,
    task.assignee_name,
    task.category_id,
    task.task_type,
  ].some((value) => String(value || "").toLowerCase().includes(q));
}

/* Categories — mirrors packages/core/constants/task.py TASK_CATEGORIES */
const CATEGORY_OPTIONS = [
  // Core operations
  { key: "operations",       label: t("nav.operations"),       icon: "M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085" },
  { key: "maintenance",      label: t("page.tasks.maintenance"),      icon: "M21.75 6.75a4.5 4.5 0 01-4.884 4.484c-1.076-.091-2.264.071-2.95.904l-7.152 8.684a2.548 2.548 0 11-3.586-3.586l8.684-7.152c.833-.686.995-1.874.904-2.95a4.5 4.5 0 014.484-4.884l-3.276 3.276a3 3 0 004.243 4.243l3.276-3.276" },
  { key: "housekeeping",     label: t("page.tasks.housekeeping"),     icon: "M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" },
  { key: "inspection",       label: t("page.tasks.inspection"),       icon: "M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" },
  { key: "security",         label: t("page.account.security"),         icon: "M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" },
  // Customer-facing
  { key: "support",          label: t("page.pricing.support"),          icon: "M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9 5.25h.008v.008H12v-.008z" },
  { key: "customer_request", label: t("page.tasks.customer_request"), icon: "M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" },
  { key: "complaint",        label: t("page.tasks.complaint"),        icon: "M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" },
  { key: "onboarding",       label: t("page.tasks.onboarding"),       icon: "M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.631 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.84m2.699 2.7c-.103.021-.207.041-.311.06a15.09 15.09 0 01-2.448-2.448 14.9 14.9 0 01.06-.312m-2.24 2.39a4.493 4.493 0 00-1.757 4.306 4.493 4.493 0 004.306-1.758M16.5 9a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0z" },
  // Business
  { key: "sales",            label: t("page.tasks.sales"),            icon: "M2.25 18.75a60.07 60.07 0 0115.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 013 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 00-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 01-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 003 15h-.75M15 10.5a3 3 0 11-6 0 3 3 0 016 0zm3 0h.008v.008H18V10.5zm-12 0h.008v.008H6V10.5z" },
  { key: "finance",          label: t("page.tasks.finance"),          icon: "M12 6v12m-3-2.818l.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 11-18 0 9 9 0 0118 0z" },
  { key: "procurement",      label: t("page.tasks.procurement"),      icon: "M2.25 3h1.386c.51 0 .955.343 1.087.835l.383 1.437M7.5 14.25a3 3 0 00-3 3h15.75m-12.75-3h11.218c1.121-2.3 2.1-4.684 2.924-7.138a60.114 60.114 0 00-16.536-1.84M7.5 14.25L5.106 5.272M6 20.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm12.75 0a.75.75 0 11-1.5 0 .75.75 0 011.5 0z" },
  { key: "billing",          label: t("page.tasks.billing"),          icon: "M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 002.25-2.25V6.75A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25v10.5A2.25 2.25 0 004.5 19.5z" },
  // People
  { key: "hr",               label: t("page.tasks.hr"),               icon: "M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" },
  { key: "training",         label: t("page.tasks.training"),         icon: "M4.26 10.147a60.436 60.436 0 00-.491 6.347A48.627 48.627 0 0112 20.904a48.627 48.627 0 018.232-4.41 60.46 60.46 0 00-.491-6.347m-15.482 0a50.57 50.57 0 00-2.658-.813A59.905 59.905 0 0112 3.493a59.902 59.902 0 0110.399 5.84c-.896.248-1.783.52-2.658.814m-15.482 0A50.697 50.697 0 0112 13.489a50.702 50.702 0 017.74-3.342M6.75 15a.75.75 0 100-1.5.75.75 0 000 1.5zm0 0v-3.675A55.378 55.378 0 0112 8.443m-7.007 11.55A5.981 5.981 0 006.75 15.75v-1.5" },
  { key: "recruitment",      label: t("page.tasks.recruitment"),      icon: "M19 7.5v3m0 0v3m0-3h3m-3 0h-3m-2.25-4.125a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zM4 19.235v-.11a6.375 6.375 0 0112.75 0v.109A12.318 12.318 0 0110.374 21c-2.331 0-4.512-.645-6.374-1.766z" },
  // Tech
  { key: "development",      label: t("page.tasks.development"),      icon: "M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" },
  { key: "it",               label: t("page.tasks.it"),               icon: "M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z" },
  { key: "bug",              label: t("page.tasks.bug_fix"),          icon: "M12 12.75c1.148 0 2.278.08 3.383.237 1.037.146 1.866.966 1.866 2.013 0 3.728-2.35 6.75-5.25 6.75S6.75 18.728 6.75 15c0-1.046.83-1.867 1.866-2.013A24.204 24.204 0 0112 12.75zm0 0c2.883 0 5.647.508 8.207 1.44a23.91 23.91 0 01-1.152 6.06M12 12.75c-2.883 0-5.647.508-8.208 1.44.125 2.104.52 4.136 1.153 6.06M12 12.75a2.25 2.25 0 002.248-2.354M12 12.75a2.25 2.25 0 01-2.248-2.354M12 8.25c.995 0 1.971-.08 2.922-.236.403-.066.74-.358.795-.762a3.778 3.778 0 00-.399-2.25M12 8.25c-.995 0-1.97-.08-2.922-.236-.402-.066-.74-.358-.795-.762a3.734 3.734 0 01.4-2.253M12 8.25a2.25 2.25 0 00-2.248 2.146M12 8.25a2.25 2.25 0 012.248 2.146M8.683 5a6.032 6.032 0 01-1.155-1.002c.07-.63.27-1.222.574-1.747m.581 2.749A3.75 3.75 0 0115.318 5m0 0c.427-.283.815-.62 1.155-.999a4.471 4.471 0 00-.575-1.752M4.921 6a24.048 24.048 0 00-.392 3.314c1.668.546 3.416.914 5.223 1.082M19.08 6c.205 1.08.337 2.187.392 3.314a23.882 23.882 0 01-5.223 1.082" },
  { key: "devops",           label: t("page.tasks.devops"),           icon: "M6.75 7.5l3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0021 18V6a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 6v12a2.25 2.25 0 002.25 2.25z" },
  // Marketing & comms
  { key: "marketing",        label: t("page.tasks.marketing"),        icon: "M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5" },
  { key: "content",          label: t("page.memories.content"),          icon: "M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" },
  { key: "design",           label: t("page.tasks.design"),           icon: "M9.53 16.122a3 3 0 00-5.78 1.128 2.25 2.25 0 01-2.4 2.245 4.5 4.5 0 008.4-2.245c0-.399-.078-.78-.22-1.128zm0 0a15.998 15.998 0 003.388-1.62m-5.043-.025a15.994 15.994 0 011.622-3.395m3.42 3.42a15.995 15.995 0 004.764-4.648l3.876-5.814a1.151 1.151 0 00-1.597-1.597L14.146 6.32a15.996 15.996 0 00-4.649 4.763m3.42 3.42a6.776 6.776 0 00-3.42-3.42" },
  { key: "social_media",     label: t("page.tasks.social_media"),     icon: "M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418" },
  // Logistics & facilities
  { key: "logistics",        label: t("page.tasks.logistics"),        icon: "M8.25 18.75a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m3 0h6m-9 0H3.375a1.125 1.125 0 01-1.125-1.125V14.25m17.25 4.5a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m3 0h1.125c.621 0 1.129-.504 1.09-1.124a17.902 17.902 0 00-3.213-9.193 2.056 2.056 0 00-1.58-.86H14.25M16.5 18.75h-2.25m0-11.177v-.958c0-.568-.422-1.048-.987-1.106a48.554 48.554 0 00-10.026 0 1.106 1.106 0 00-.987 1.106v7.635m12-6.677v6.677m0 4.5v-4.5m0 0h-12" },
  { key: "inventory",        label: t("page.tasks.inventory"),        icon: "M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" },
  { key: "facilities",       label: t("page.tasks.facilities"),       icon: "M2.25 21h19.5m-18-18v18m10.5-18v18m6-13.5V21M6.75 6.75h.75m-.75 3h.75m-.75 3h.75m3-6h.75m-.75 3h.75m-.75 3h.75M6.75 21v-3.375c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21M3 3h12m-.75 4.5H21m-3.75 3H21m-3.75 3H21" },
  // Governance
  { key: "compliance",       label: t("page.tasks.compliance"),       icon: "M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" },
  { key: "legal",            label: t("page.tasks.legal"),            icon: "M12 3v17.25m0 0c-1.472 0-2.882.265-4.185.75M12 20.25c1.472 0 2.882.265 4.185.75M18.75 4.97A48.416 48.416 0 0012 4.5c-2.291 0-4.545.16-6.75.47m13.5 0c1.01.143 2.01.317 3 .52m-3-.52l2.62 10.726c.122.499-.106 1.028-.589 1.202a5.988 5.988 0 01-2.031.352 5.988 5.988 0 01-2.031-.352c-.483-.174-.711-.703-.59-1.202L18.75 4.971zm-16.5.52c.99-.203 1.99-.377 3-.52m0 0l2.62 10.726c.122.499-.106 1.028-.589 1.202a5.989 5.989 0 01-2.031.352 5.989 5.989 0 01-2.031-.352c-.483-.174-.711-.703-.59-1.202L5.25 4.971z" },
  { key: "audit",            label: t("page.tasks.audit"),            icon: "M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9zm3.75 11.625a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" },
  // Misc
  { key: "project",          label: t("page.tasks.project"),          icon: "M6 6.878V6a2.25 2.25 0 012.25-2.25h7.5A2.25 2.25 0 0118 6v.878m-12 0c.235-.083.487-.128.75-.128h10.5c.263 0 .515.045.75.128m-12 0A2.25 2.25 0 004.5 9v.878m13.5-3A2.25 2.25 0 0119.5 9v.878m0 0a2.246 2.246 0 00-.75-.128H5.25c-.263 0-.515.045-.75.128m15 0A2.25 2.25 0 0121 12v6a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 18v-6c0-.98.626-1.813 1.5-2.122" },
  { key: "meeting",          label: t("page.tasks.meeting"),          icon: "M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5" },
  { key: "research",         label: t("page.tasks.research"),         icon: "M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23-.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" },
  { key: "other",            label: t("page.tasks.other"),            icon: "M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z" },
];

/* ── helpers ────────────────────────────────────────────── */



/* ── sub-components ─────────────────────────────────────── */

const TASK_CARD_AI_PROCESSING_STYLES = `
  .task-card-ai-processing::before {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: inherit;
    border: 1px solid rgba(125,211,252,0.34);
    box-shadow:
      inset 0 0 0 1px rgba(244,247,250,0.58),
      0 0 14px rgba(90,142,166,0.06);
    animation: task-card-processing-pulse 2.6s ease-in-out infinite;
    pointer-events: none;
    z-index: 2;
  }
  .task-card-comet-runner {
    position: absolute;
    left: 0;
    top: 0;
    width: 1px;
    height: 1px;
    offset-path: inset(4px round 10px);
    offset-distance: 0%;
    offset-rotate: auto;
    animation: task-card-comet-run 5.8s linear infinite;
    pointer-events: none;
    z-index: 4;
    filter: drop-shadow(0 0 4px rgba(90,142,166,0.30));
  }
  .task-card-comet-tail {
    position: absolute;
    right: -1px;
    top: -0.75px;
    width: 64px;
    height: 1.5px;
    border-radius: 999px;
    background: linear-gradient(
      90deg,
      transparent 0%,
      rgba(56,189,248,0.05) 18%,
      rgba(56,189,248,0.22) 52%,
      rgba(34,211,238,0.68) 82%,
      rgba(232,239,244,0.96) 94%,
      rgba(255,255,255,1) 100%
    );
    box-shadow:
      0 0 3px rgba(56,189,248,0.36),
      0 0 7px rgba(95,146,138,0.16);
  }
  .task-card-comet-tail::before {
    content: "";
    position: absolute;
    inset: -2px -3px;
    border-radius: inherit;
    background: linear-gradient(
      90deg,
      transparent 0%,
      rgba(90,142,166,0.04) 30%,
      rgba(90,142,166,0.11) 62%,
      rgba(34,211,238,0.24) 88%,
      rgba(255,255,255,0.42) 100%
    );
    filter: blur(2px);
    opacity: 0.72;
  }
  .task-card-comet-tail::after {
    content: "";
    position: absolute;
    right: 0;
    top: 0;
    width: 22px;
    height: 1px;
    border-radius: inherit;
    background: linear-gradient(
      90deg,
      transparent 0%,
      rgba(186,230,253,0.70) 72%,
      rgba(255,255,255,1) 100%
    );
  }
  .task-card-ai-processing > :not(.task-card-comet-runner) { position: relative; z-index: 3; }
  html[data-theme="dark"] .task-card-ai-processing::before {
    animation: none;
    border-color: rgba(148, 163, 184, 0.18);
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.035);
  }
  html[data-theme="dark"] .task-card-comet-runner {
    display: none;
  }
  @keyframes task-card-processing-pulse {
    0%, 100% {
      box-shadow:
        inset 0 0 0 1px rgba(244,247,250,0.58),
        0 0 10px rgba(90,142,166,0.05),
        0 0 0 rgba(34,211,238,0);
    }
    50% {
      box-shadow:
        inset 0 0 0 1px rgba(244,247,250,0.72),
        0 0 18px rgba(90,142,166,0.14),
        0 0 26px rgba(34,211,238,0.13);
    }
  }
  @keyframes task-card-comet-run {
    to { offset-distance: 100%; }
  }
  @media (prefers-reduced-motion: reduce) {
    .task-card-comet-runner {
      display: none;
    }
    .task-card-ai-processing::before {
      animation: none;
    }
  }
`;

function BoardTaskCard({ task, onClick, onOpenFull, agents, dimmed, compact, wsName, workspaceScoped }: { task: Task; onClick: () => void; onOpenFull: () => void; agents?: any[]; dimmed?: boolean; compact?: boolean; wsName?: string; workspaceScoped?: boolean }) {
  const handleDragStart = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.dataTransfer.setData("taskId", task.id);
    e.dataTransfer.effectAllowed = "move";
    e.currentTarget.style.opacity = "0.4";
  }, [task.id]);
  const handleDragEnd = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.currentTarget.style.opacity = dimmed ? "0.5" : "1";
  }, [dimmed]);
  const handleKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onClick();
    }
  }, [onClick]);

  const pcfg = PRIORITY_CONFIG[task.priority] ?? PRIORITY_CONFIG[3];
  const overdue = isDeadlineOverdue(task.deadline, task.status);
  const isMaster = isMasterAgent(task.agent_id, task.agent_type);
  const isAI = !!task.agent_id || isMaster;
  const assigneeName = friendlyPersonName(
    task.agent_name || task.assignee_name || (isMaster ? MANOR_AGENT_NAME : task.agent_id ? t("page.tasks.ai_agent") : ""),
    "",
  ) || null;
  const assigneeAvatar = task.agent_avatar || task.assignee_avatar || null;
  const aiResult = (task.details as any)?.ai_result;
  const hasChecklist = !compact && (task.details as any)?.checklist_total > 0;
  const checkDone = (task.details as any)?.checklist_done || 0;
  const checkTotal = (task.details as any)?.checklist_total || 0;
  const dependencyInfo = _taskDependencyInfo(task);
  const isWorkspaceTask = Boolean(task.workspace_id || task.workspace_name || wsName || workspaceScoped);
  const isProcessingGlow = (isAI || isWorkspaceTask) && task.status === "in_progress" && !dimmed;
  const scopeName = wsName || (!task.workspace_id ? "Entity-level" : "");
  const scopeIsEntityLevel = !task.workspace_id;

  return (
    <div
        draggable
        role="button"
        tabIndex={0}
        aria-label={`${t("component.workspace_chat.view_task")}: ${task.title}`}
        title={t("component.workspace_chat.view_task")}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onClick={onClick}
        onKeyDown={handleKeyDown}
        className={`task-board-card${isProcessingGlow ? " task-card-ai-processing" : ""}`}
        style={{
          position: "relative",
          isolation: "isolate",
          background: dimmed
            ? "linear-gradient(180deg, rgba(255,255,255,0.56), rgba(255,254,252,0.42))"
            : "linear-gradient(180deg, rgba(255,255,255,0.86), rgba(255,254,252,0.72))",
          backdropFilter: "blur(14px) saturate(1.08)",
          WebkitBackdropFilter: "blur(14px) saturate(1.08)",
          borderRadius: 14,
          border: isProcessingGlow ? "1px solid rgba(186,230,253,0.46)" : "1px solid rgba(117,96,66,0.07)",
          padding: 0, cursor: "grab", userSelect: "none",
          transition: "all 0.2s cubic-bezier(0.4,0,0.2,1)",
          opacity: dimmed ? 0.5 : 1, overflow: "hidden", flexShrink: 0,
        }}
        onMouseEnter={(e) => {
          if (!dimmed) {
            e.currentTarget.style.borderColor = "var(--card-hover-border)";
            e.currentTarget.style.background = "var(--card-hover-bg)";
            e.currentTarget.style.boxShadow = "var(--card-hover-shadow)";
            e.currentTarget.style.transform = "var(--card-hover-transform)";
          }
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.borderColor = isProcessingGlow ? "rgba(186,230,253,0.46)" : "rgba(117,96,66,0.07)";
          e.currentTarget.style.background = dimmed
            ? "linear-gradient(180deg, rgba(255,255,255,0.56), rgba(255,254,252,0.42))"
            : "linear-gradient(180deg, rgba(255,255,255,0.86), rgba(255,254,252,0.72))";
          e.currentTarget.style.boxShadow = "none";
          e.currentTarget.style.transform = "none";
        }}
      >
      {isProcessingGlow && (
        <div className="task-card-comet-runner" aria-hidden="true">
          <span className="task-card-comet-tail" />
        </div>
      )}
      {/* Priority accent */}
      <div style={{ height: compact ? 2 : 3, background: isProcessingGlow ? "transparent" : `linear-gradient(90deg, ${pcfg.color}, ${pcfg.color}50)` }} />

      <div style={{ padding: compact ? "6px 10px" : "10px 12px" }}>
        {/* Header: avatar + title + id */}
        <div style={{ display: "flex", alignItems: "flex-start", gap: compact ? 6 : 8, marginBottom: compact ? 2 : 4 }}>
          {(isAI || task.assignee_id) && (
            <UserAvatar
              type={isMaster ? "manor" : isAI ? "agent" : "user"}
              name={assigneeName}
              avatarUrl={assigneeAvatar}
              seed={isAI ? task.agent_id || task.assignee_id || undefined : undefined}
              size={compact ? 18 : 22}
              style={!isAI ? {
                background: "var(--task-board-avatar-bg, linear-gradient(135deg, #e8eff4, #ddd6fe))",
                borderColor: "var(--task-board-avatar-border, rgba(255,255,255,0.8))",
                color: "var(--task-board-avatar-fg, #78716c)",
              } : undefined}
            />
          )}
          <div style={{ flex: 1, minWidth: 0 }}>
            <p className="task-board-card-title" style={{
              fontSize: compact ? 12 : 13, fontWeight: 600, color: dimmed ? "#a8a29e" : "#1c1917",
              lineHeight: 1.35, margin: 0, textDecoration: dimmed ? "line-through" : "none",
              display: "-webkit-box", WebkitLineClamp: compact ? 1 : 2,
              WebkitBoxOrient: "vertical" as const, overflow: "hidden",
            }}>
              {formatUserFacingText(task.title)}
            </p>
            {assigneeName && (
              <p className="task-board-card-assignee" style={{ fontSize: 10, color: isAI ? "#436b65" : "#a8a29e", margin: "1px 0 0", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {assigneeName}
              </p>
            )}
          </div>
          <span className="task-board-card-id" style={{ fontSize: 9, color: "#d6d3d1", fontFamily: "monospace", flexShrink: 0, marginTop: compact ? 1 : 3 }}>#{task.id.slice(-4)}</span>
        </div>

        {/* Scope badge */}
        {scopeName && (
          <span className="task-board-card-scope" style={{
            display: "inline-flex", alignItems: "center", gap: 3,
            fontSize: 9, fontWeight: 600, color: scopeIsEntityLevel ? "#57534e" : "#436b65",
            background: scopeIsEntityLevel ? "#fafaf9" : "#f5f5f4", padding: "1px 6px", borderRadius: 4,
            marginBottom: compact ? 2 : 4, maxWidth: "100%",
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>
            <span className="task-board-card-scope-dot" style={{ width: 4, height: 4, borderRadius: "50%", background: scopeIsEntityLevel ? "#a8a29e" : "#5f928a", flexShrink: 0 }} />
            {scopeName}
          </span>
        )}

        {/* Description — hidden in compact mode */}
        {!compact && task.description && !dimmed && (
          <p className="task-board-card-description" style={{ fontSize: 11, color: "#78716c", margin: "0 0 6px", lineHeight: 1.4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {formatUserFacingText(task.description)}
          </p>
        )}

        {/* Tags — compact mode: only priority + overdue */}
        <div style={{ display: "flex", alignItems: "center", gap: 3, flexWrap: "wrap", marginBottom: compact ? 0 : 6 }}>
          <PriorityPill priority={task.priority} size="sm" />
          {!compact && <CategoryChip categoryKey={task.category_id} size="sm" />}
          {overdue && (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 9, fontWeight: 700, color: "#c14a44", padding: "2px 7px", borderRadius: 20, background: "#f8f0ef" }}>
              <IconClock size={8} /> {t("page.task_detail.overdue")}
            </span>
          )}
          {dependencyInfo && <DependencyStatusPill info={dependencyInfo} compact={compact} />}
          {!compact && aiResult?.supervisor_verdict?.verdict && (
            <StatusPill
              label={formatUserFacingLabel(aiResult.supervisor_verdict.verdict)}
              color={aiResult.supervisor_verdict.verdict === "done" ? "#059669" : aiResult.supervisor_verdict.verdict === "failed" ? "#dc2626" : "#d97706"}
              bg={aiResult.supervisor_verdict.verdict === "done" ? "#d1fae5" : aiResult.supervisor_verdict.verdict === "failed" ? "#fee2e2" : "#fef3c7"}
              size="sm"
            />
          )}
          {compact && (
            <StatusPill status={task.status} size="sm" />
          )}
          {compact && task.deadline && (
            <span style={{ fontSize: 9, color: overdue ? "#d65f59" : "#a8a29e", fontWeight: 500 }}>
              {formatDate(task.deadline)}
            </span>
          )}
        </div>

        {/* Checklist — hidden in compact */}
        {!compact && hasChecklist && (
          <div style={{ marginBottom: 8 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 3 }}>
              <span style={{ fontSize: 10, color: "#78716c", fontWeight: 500 }}>{t("page.tasks.checklist")}</span>
              <span style={{ fontSize: 10, fontWeight: 700, color: checkDone === checkTotal ? "#437f6b" : "#57534e" }}>{checkDone}/{checkTotal}</span>
            </div>
            <div style={{ height: 4, borderRadius: 2, background: "#f5f5f4" }}>
              <div style={{ height: "100%", borderRadius: 2, background: checkDone === checkTotal ? "#4f9c84" : "linear-gradient(90deg, #436b65, #5f928a)", width: `${(checkDone / checkTotal) * 100}%`, transition: "width 0.4s ease" }} />
            </div>
          </div>
        )}

        {/* Footer — hidden in compact (info merged into tags row) */}
        {!compact && (
          <div className="task-board-card-footer" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", paddingTop: 6, borderTop: "1px solid rgba(245,245,244,0.8)" }}>
            <StatusPill status={task.status} size="sm" />
            {task.deadline ? (
              <span className="task-board-card-deadline" style={{ fontSize: 10, color: overdue ? "#d65f59" : "#a8a29e", fontWeight: 500, display: "flex", alignItems: "center", gap: 3 }}>
                <IconClock size={9} />
                {formatDate(task.deadline)}
              </span>
            ) : (
              <span className="task-board-card-no-deadline" style={{ fontSize: 10, color: "#e7e5e4" }}>{t("page.tasks.no_deadline")}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

const _COLLAPSIBLE = new Set(["done"]);

function taskDoneDate(task: Task): Date | null {
  const raw = task.completed_at || (task as any).updated_at || task.created_at || "";
  if (!raw) return null;
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? null : date;
}

function compareDoneTasks(a: Task, b: Task): number {
  const bTime = taskDoneDate(b)?.getTime() ?? 0;
  const aTime = taskDoneDate(a)?.getTime() ?? 0;
  if (bTime !== aTime) return bTime - aTime;
  if ((b.priority || 0) !== (a.priority || 0)) return (b.priority || 0) - (a.priority || 0);
  return (b.title || "").localeCompare(a.title || "");
}

function isRecentDoneTask(task: Task): boolean {
  const date = taskDoneDate(task);
  if (!date) return false;
  return Date.now() - date.getTime() <= DONE_RECENT_WINDOW_DAYS * MS_PER_DAY;
}

function _taskOutputFiles(task: Task): any[] {
  const output = (task.actual_output || {}) as Record<string, any>;
  const files = Array.isArray(output.files) ? output.files : [];
  const stepFiles = Array.isArray(output.steps)
    ? output.steps.flatMap((step: any) => Array.isArray(step.files) ? step.files : [])
    : [];
  return [...files, ...stepFiles];
}

const GENERIC_OUTPUT_STEP_LABELS = new Set(["subagent", "agent", "human", "system", "tool", "worker"]);

function taskOutputStepLabel(step: any, index: number): string {
  const raw = typeof step === "string"
    ? step
    : step?.title
      || step?.name
      || step?.label
      || step?.display_name
      || step?.task_title
      || step?.step_title
      || step?.step_key
      || step?.key
      || step?.kind
      || step?.type
      || step?.role
      || "";
  const value = String(raw || "").trim();
  if (!value) return `Step ${index + 1}`;

  const normalized = value.toLowerCase().replace(/[\s_-]+/g, "_");
  if (GENERIC_OUTPUT_STEP_LABELS.has(normalized)) {
    if (normalized === "human") return "Human review";
    if (normalized === "subagent" || normalized === "agent" || normalized === "worker") return "Agent step";
    if (normalized === "tool") return "Tool run";
    return "System step";
  }

  return formatUserFacingLabel(value);
}

function TaskDependencySummary({ task }: { task: Task }) {
  const info = _taskDependencyInfo(task);
  if (!info) return null;
  return (
    <div style={{ borderRadius: 12, border: "1px solid rgba(207,155,68,0.18)", background: "rgba(250,247,239,0.58)", padding: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <IconCircleDot size={13} style={{ color: info.status === "completed" ? "#436b65" : info.status === "blocked" ? "#a23e38" : "#76502c" }} />
        <span style={{ fontSize: 11, fontWeight: 800, color: "#76502c", textTransform: "uppercase", letterSpacing: "0.06em" }}>
          {t("page.tasks.dependency_inputs")}
        </span>
        <span style={{ marginLeft: "auto" }}>
          <DependencyStatusPill info={info} compact />
        </span>
      </div>
      {info.outputs.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          {info.outputs.slice(0, 4).map((dep: any, i: number) => {
            const files = Array.isArray(dep.files) ? dep.files : [];
            return (
              <div key={`${dep.task_id || i}`} style={{ borderRadius: 10, background: "rgba(255,255,255,0.72)", border: "1px solid rgba(28,25,23,0.06)", padding: "7px 9px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 750, color: "#44403c" }}>
                  <IconCheckCircle size={12} style={{ color: "#57534e" }} />
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {formatUserFacingText(dep.task_title || `${t("page.tasks.predecessor_outputs")} ${i + 1}`)}
                  </span>
                </div>
                {dep.result_summary && (
                  <p style={{ fontSize: 11, color: "#78716c", lineHeight: 1.45, margin: "4px 0 0" }}>
                    {formatUserFacingStructuredText(dep.result_summary).slice(0, 180)}
                  </p>
                )}
                {files.length > 0 && (
                  <p style={{ fontSize: 10, color: "#57534e", fontWeight: 700, margin: "4px 0 0" }}>
                    {files.length} {t("page.tasks.files")}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <p style={{ fontSize: 12, color: "#78716c", lineHeight: 1.55, margin: 0 }}>
          {t("page.tasks.waiting_to_start_when_ready")}
        </p>
      )}
    </div>
  );
}

function TaskOutputSummary({ task }: { task: Task }) {
  const output = (task.actual_output || null) as Record<string, any> | null;
  const details = (task.details || {}) as Record<string, any>;
  const batchId = typeof details.workspace_work_batch_id === "string" ? details.workspace_work_batch_id : "";
  const files = _taskOutputFiles(task);
  const steps = Array.isArray(output?.steps) ? output?.steps : [];
  const summaryValue = output?.summary || output?.result_summary || output?.message || output?.text || "";
  const summary = typeof summaryValue === "string"
    ? summaryValue.trim()
    : formatUserFacingStructuredText(summaryValue).trim();
  const outputStatus = String(output?.plan_status || task.status || "").trim();

  if (!output && !batchId) return null;

  return (
    <div className="task-output-summary-card" style={{ borderRadius: 12, border: "1px solid rgba(95,146,138,0.16)", background: "rgba(242,246,245,0.52)", padding: 12 }}>
      <div className="task-output-summary-header" style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <IconDocument className="task-output-summary-icon" size={13} style={{ color: "#436b65" }} />
        <span className="task-output-summary-label" style={{ fontSize: 11, fontWeight: 800, color: "#436b65", textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Result
        </span>
        {outputStatus && (
          <span
            className={`task-output-summary-status task-output-summary-status--${outputStatus.replace(/[^a-z0-9_-]/gi, "_").toLowerCase()}`}
            style={{ marginLeft: "auto", fontSize: 10, fontWeight: 700, color: "#78716c", background: "rgba(255,255,255,0.8)", border: "1px solid rgba(226,232,240,0.8)", borderRadius: 999, padding: "2px 7px" }}
          >
            {formatUserFacingLabel(outputStatus)}
          </span>
        )}
      </div>
      {summary ? (
        <div className="task-output-summary-markdown" style={{ fontSize: 12, color: "#44403c", lineHeight: 1.55, marginBottom: files.length || steps.length ? 8 : 0 }}>
          <ChatMarkdown content={formatUserFacingStructuredText(summary)} />
        </div>
      ) : files.length === 0 && steps.length === 0 ? (
        <p className="task-output-summary-empty" style={{ fontSize: 12, color: "#78716c", lineHeight: 1.55, margin: files.length || steps.length ? "0 0 8px" : 0 }}>
          {batchId
            ? "This task is part of a grouped run. Results will appear here after the agent finishes."
            : "The run recorded activity, but no final summary was saved."}
        </p>
      ) : !summary && steps.length > 0 ? (
        <p className="task-output-summary-empty" style={{ fontSize: 12, color: "#78716c", lineHeight: 1.55, margin: files.length ? "0 0 8px" : 0 }}>
          The run recorded {steps.length} {steps.length === 1 ? "step" : "steps"}.
        </p>
      ) : null}
      {files.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
          {files.slice(0, 4).map((file: any, i: number) => {
            const reference = file.document_id || file.doc_id || file.id || file.url || file.path || file.fs_path || file.public_url || "";
            const label = file.name || file.filename || file.original_name || (reference ? String(reference).split(/[\\/]/).pop() : file.type) || `File ${i + 1}`;
            return reference ? (
              <InlineFileReferenceCard key={i} reference={String(reference)} label={String(label)} compact />
            ) : (
              <span key={i} className="task-output-summary-file" style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 700, color: "#57534e" }}>
                <IconDocument size={11} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
              </span>
            );
          })}
          {files.length > 4 && <span className="task-output-summary-more" style={{ fontSize: 11, color: "#a8a29e" }}>+ {files.length - 4} more files</span>}
        </div>
      )}
      {steps.length > 0 && (
        <div className="task-output-summary-steps" style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
          <span className="task-output-summary-section-title" style={{ flexBasis: "100%", fontSize: 11, fontWeight: 800, color: "#57534e", marginBottom: 2 }}>
            Steps
          </span>
          {steps.slice(0, 6).map((step: any, i: number) => (
            <span key={i} className="task-output-summary-step-chip" style={{ fontSize: 10, color: "#78716c", background: "rgba(255,255,255,0.75)", border: "1px solid rgba(226,232,240,0.8)", borderRadius: 999, padding: "2px 7px" }}>
              {taskOutputStepLabel(step, i)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function localDateKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function formatAgendaTime(value: string, timezone?: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: timezone || undefined,
  });
}

function humanizeBookingSlug(slug?: string | null): string {
  if (!slug) return "Booking";
  return slug
    .split(/[-_]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function bookingDurationMinutes(booking: BookingRecord): number | undefined {
  const start = new Date(booking.starts_at);
  const end = new Date(booking.ends_at);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return undefined;
  return Math.max(1, Math.round((end.getTime() - start.getTime()) / 60000));
}

function eventDurationMinutes(event: ApiExternalCalendarEvent): number | undefined {
  if (!event.ends_at) return undefined;
  const start = new Date(event.starts_at);
  const end = new Date(event.ends_at);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return undefined;
  return Math.max(1, Math.round((end.getTime() - start.getTime()) / 60000));
}

function calendarVisibleRangeForMonth(month: Date): { start: string; end: string } {
  const year = month.getFullYear();
  const mo = month.getMonth();
  const first = new Date(year, mo, 1);
  const startDow = first.getDay() === 0 ? 6 : first.getDay() - 1;
  const start = new Date(year, mo, 1 - startDow);
  const last = new Date(year, mo + 1, 0);
  const endDow = last.getDay() === 0 ? 6 : last.getDay() - 1;
  const end = new Date(year, mo + 1, 6 - endDow);
  return { start: localDateKey(start), end: localDateKey(end) };
}

function bookingLocationLabel(booking: BookingRecord): string | null {
  if (booking.meeting_url) return "Video";
  if (booking.calendar_event_url) return "Calendar event";
  return null;
}

function CalendarOpsRail({
  items,
  timezone,
  onOpenTask,
  onManageBookingLinks,
}: {
  items: DailyAgendaItem[];
  timezone?: string;
  onOpenTask: (taskId: string) => void;
  onManageBookingLinks: () => void;
}) {
  return (
    <div style={{
      border: "1px solid rgba(28,25,23,0.06)",
      background: "rgba(255,255,255,0.78)",
      borderRadius: 18,
      padding: "12px 14px",
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      gap: 14,
      marginBottom: 12,
      boxShadow: "0 8px 20px rgba(0,0,0,0.03)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, flex: 1 }}>
        <span style={{ width: 28, height: 28, borderRadius: 10, background: "rgba(79,125,117,0.1)", display: "inline-flex", alignItems: "center", justifyContent: "center", color: "#436b65", flexShrink: 0 }}>
          <IconCalendar size={14} />
        </span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 800, color: "#1c1917", marginBottom: 5 }}>Today</div>
          {items.length === 0 ? (
            <div style={{ fontSize: 11, fontWeight: 650, color: "#a8a29e" }}>No scheduled items</div>
          ) : (
            <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0, overflow: "hidden" }}>
              {items.slice(0, 3).map((item) => {
                const isBooking = item.source === "booking";
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => item.task_id && onOpenTask(item.task_id)}
                    disabled={!item.task_id}
                    style={{
                      minWidth: 0,
                      maxWidth: 220,
                      height: 30,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      borderRadius: 9,
                      border: `1px solid ${isBooking ? "rgba(79,125,117,0.18)" : "rgba(28,25,23,0.06)"}`,
                      background: isBooking ? "rgba(247,250,249,0.96)" : "#fff",
                      padding: "0 9px",
                      cursor: item.task_id ? "pointer" : "default",
                      color: "#44403c",
                      fontSize: 11,
                      fontWeight: 750,
                    }}
                  >
                    <IconClock size={11} style={{ color: isBooking ? "#4f7d75" : "#a8a29e", flexShrink: 0 }} />
                    <span style={{ color: "#78716c", flexShrink: 0 }}>{formatAgendaTime(item.starts_at, timezone)}</span>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.title}</span>
                  </button>
                );
              })}
              {items.length > 3 && (
                <span style={{ fontSize: 11, fontWeight: 800, color: "#78716c", flexShrink: 0 }}>+{items.length - 3}</span>
              )}
            </div>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={onManageBookingLinks}
        style={{
          height: 32,
          borderRadius: 10,
          border: "1px solid rgba(79,125,117,0.16)",
          background: "#fff",
          color: "#436b65",
          fontSize: 11,
          fontWeight: 800,
          padding: "0 11px",
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          flexShrink: 0,
        }}
      >
        <IconExternalLink size={12} />
        Booking links
      </button>
    </div>
  );
}


/* ── Inline Task Detail Panel (matches standalone TaskDetail page) ── */
function InlineTaskDetail({ task: initialTask, agents, statusTransitions, onClose, onOpenFull, onUpdate, onDelete }: {
  task: Task; agents: any[];
  statusTransitions?: Record<string, string[]>;
  onClose: () => void; onOpenFull: () => void;
  onUpdate: (data: Partial<Task>) => void;
  onDelete: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const currentUser = useAuthStore((s) => s.user);
  const [comment, setComment] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [showMore, setShowMore] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // Entity users — shared with the assignee picker in TaskPropertiesPanel.
  const { data: usersList } = useQuery({
    queryKey: ["entity-users-for-assignee", "directory"],
    queryFn: () => api.users.directory(),
  });
  const { data: staffList = [] } = useQuery({
    queryKey: ["entity-staff-for-assignee"],
    queryFn: () => api.staff.list(),
  });

  // Subscribe to the same task cache key the rest of the app uses so
  // mutations + WebSocket task_update events flow into this drawer
  // automatically. Falls back to the prop the parent opened us with
  // until the first fetch lands.
  const { data: liveTask } = useQuery({
    queryKey: ["task", initialTask.id],
    queryFn: () => api.tasks.get(initialTask.id),
    initialData: initialTask,
  });
  const task: Task = (liveTask as Task) || initialTask;

  const { data: logs = [] } = useQuery({
    queryKey: ["task-logs", task.id],
    queryFn: () => api.tasks.logs(task.id),
  });

  const updateMut = useMutation({
    mutationFn: (data: Partial<Task>) => api.tasks.update(task.id, data),
    onSuccess: (updated) => {
      // Write the API response straight into the cache so the drawer
      // re-renders synchronously — no waiting for a refetch round-trip.
      if (updated) queryClient.setQueryData(["task", task.id], updated);
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    },
  });

  const retryMut = useMutation({
    mutationFn: (note?: string) => api.tasks.retry(task.id, note),
    onSuccess: (result) => {
      queryClient.setQueryData(["task", task.id], result.task);
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task", task.id] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", task.id] });
      toast.success(
        result.dispatched ? t("page.tasks.retry_started") : t("page.tasks.retry_queued"),
        `${t("page.tasks.mode")}: ${result.mode}`,
      );
    },
    onError: (err: any) => toast.error(t("page.tasks.retry_failed"), err?.message || t("page.tasks.could_not_retry_this_task")),
  });

  const hitlMut = useMutation({
    mutationFn: ({ response, fields }: { response: string; fields?: Record<string, string> }) => api.tasks.respondHITL(task.id, { response, fields }),
    onSuccess: (result) => {
      queryClient.setQueryData(["task", task.id], result.task);
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task", task.id] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", task.id] });
      window.dispatchEvent(new CustomEvent("manor:workspace-actions-refresh", { detail: { workspaceId: result.task.workspace_id } }));
      setComment("");
      toast.success(
        result.dispatched ? t("page.tasks.input_sent") : t("page.tasks.input_saved"),
        result.mode ? `${t("page.tasks.mode")}: ${result.mode}` : undefined,
      );
    },
    onError: (err: any) => toast.error(t("page.tasks.resume_failed"), err?.message || t("page.tasks.could_not_resume_this_task")),
  });

  const sendComment = async () => {
    if (!comment.trim() && pendingFiles.length === 0) return;
    const attachments: any[] = [];
    for (const f of pendingFiles) {
      try { attachments.push(await api.tasks.uploadAttachment(task.id, f)); } catch { }
    }
    const content = comment.trim() || (attachments.length > 0 ? `${t("page.tasks.attached")} ${attachments.length} ${attachments.length > 1 ? t("page.tasks.files") : t("page.tasks.file")}` : "");
    if (!content) return;
    await api.tasks.addLog(task.id, content, "comment", attachments.length > 0 ? attachments : undefined);
    setComment("");
    setPendingFiles([]);
    queryClient.invalidateQueries({ queryKey: ["task-logs", task.id] });
  };
  const commentMut = useMutation({ mutationFn: sendComment });

  const pcfg = PRIORITY_CONFIG[task.priority] ?? PRIORITY_CONFIG[3];
  const isMaster = isMasterAgent(task.agent_id, task.agent_type);
  const assigneeLabel = friendlyPersonName(
    task.agent_name || task.assignee_name || (isMaster ? MANOR_AGENT_NAME : task.agent_id ? t("page.tasks.ai_agent") : task.assignee_id),
    "",
  );
  const assigneeAvatar: string | null = task.agent_avatar || task.assignee_avatar || null;
  const isAI = !!task.agent_id || isMaster;
  const comments = (logs as any[]).filter((l: any) => !l.log_type.startsWith("ai_"));

  return (
    <div className="task-detail-drawer-panel" style={{ width: "min(100%, 380px)", maxWidth: "100%", borderLeft: "1px solid rgba(28,25,23,0.06)", background: "rgba(255,255,255,0.85)", backdropFilter: "blur(16px)", display: "flex", flexDirection: "column", overflowY: "auto", height: "100%" }}>
      {/* Header — Open full view + Delete + close. Surfaced directly
          so the actions are reachable without hunting in a "..." menu. */}
      <div style={{ padding: "12px 16px", borderBottom: "1px solid rgba(28,25,23,0.06)", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <button
            onClick={onOpenFull}
            title={t("page.tasks.open_full_view")}
            style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "5px 10px", borderRadius: 8, border: "1px solid rgba(28,25,23,0.9)",
              background: "#1c1917", cursor: "pointer", fontSize: 12, fontWeight: 650, color: "#fff",
              boxShadow: "0 8px 18px rgba(28,25,23,0.12)",
              transition: "background 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "#0c0a09";
              e.currentTarget.style.boxShadow = "0 10px 22px rgba(28,25,23,0.18)";
              e.currentTarget.style.transform = "translateY(-1px)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "#1c1917";
              e.currentTarget.style.boxShadow = "0 8px 18px rgba(28,25,23,0.12)";
              e.currentTarget.style.transform = "none";
            }}
          >
            <IconExternalLink size={12} />
            {t("page.tasks.full_view")}
          </button>
          <button
            onClick={onDelete}
            title={t("page.tasks.delete_task")}
            style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "5px 10px", borderRadius: 8, border: "1px solid rgba(236,200,197,0.8)",
              background: "#fff", cursor: "pointer", fontSize: 12, fontWeight: 500, color: "#c14a44",
              transition: "background 0.15s",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "#f8f0ef"; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "#fff"; }}
          >
            <IconTrash size={12} />
            {t("action.delete")}
          </button>
        </div>
        <button onClick={onClose} style={{ width: 28, height: 28, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", color: "#a8a29e" }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "#f5f5f4"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
          <IconClose size={14} />
        </button>
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
        {/* Title */}
        <h3 style={{ fontSize: 17, fontWeight: 700, color: "#1c1917", margin: 0, lineHeight: 1.3 }}>
          {formatUserFacingText(task.title)}
          <span style={{ fontSize: 10, color: "#a8a29e", fontWeight: 500, marginLeft: 6, fontFamily: "monospace" }}>#{task.id.slice(-6)}</span>
        </h3>

        {/* Properties — shared component (compact variant). Priority +
            Category sit behind the "More" toggle since they're already
            on the kanban card. */}
        <TaskPropertiesPanel
          task={task}
          agents={(agents || []) as any[]}
          users={(usersList as any[]) || []}
          staff={(staffList as any[]) || []}
          currentUser={currentUser as any}
          variant="compact"
          framed
          showPriority={showMore}
          showCategory={showMore}
          statusTransitions={statusTransitions}
          onUpdate={(patch) => updateMut.mutate(patch as any)}
        />

        {/* More / Less toggle */}
        <button
          onClick={() => setShowMore((v) => !v)}
          style={{
            alignSelf: "flex-start",
            fontSize: 11, fontWeight: 600, color: "#78716c",
            background: "transparent", border: "none", cursor: "pointer",
            padding: "0 0 0 4px",
          }}
        >
          {showMore ? t("page.tasks.hide_details") : t("page.tasks.more_details")}
        </button>

        <TaskRecoveryPanel
          key={`${task.id}:${task.status}`}
          status={task.status}
          logs={logs}
          comment={comment}
          isPending={retryMut.isPending || hitlMut.isPending}
          variant="compact"
          onRetry={(note) => retryMut.mutate(note)}
          onRespond={(response, fields) => hitlMut.mutate({ response, fields })}
        />

        <TaskDependencySummary task={task} />

        <TaskOutputSummary task={task} />

        {/* Description */}
        {task.description && (
          <div>
            <span style={{ fontSize: 11, fontWeight: 700, color: "#57534e", textTransform: "uppercase", letterSpacing: "0.04em" }}>{t("page.task_collections.description")}</span>
            <div style={{ fontSize: 13, color: "#44403c", margin: "4px 0 0", lineHeight: 1.6 }}>
              <ChatMarkdown content={formatTaskDescriptionForDisplay(task.description)} />
            </div>
          </div>
        )}

        {/* Dates — only when "More details" is open */}
        {showMore && (
          <div style={{ display: "flex", gap: 16, fontSize: 11, color: "#a8a29e" }}>
            {task.created_at && <span>{t("page.dashboard.created")} {formatDate(task.created_at)}</span>}
            {task.started_at && <span>{t("page.tasks.started")} {formatDate(task.started_at)}</span>}
            {task.completed_at && <span>{t("page.team_people.done")} {formatDate(task.completed_at)}</span>}
          </div>
        )}

        {/* Delete moved to the header overflow menu (⋯) — keeps the
            primary scroll area free of destructive actions. */}

        {/* Comments */}
        <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 10 }}>
            <IconComment size={12} style={{ color: "#78716c" }} />
            <span style={{ fontSize: 11, fontWeight: 700, color: "#57534e", textTransform: "uppercase", letterSpacing: "0.04em" }}>{t("page.tasks.comments")} ({comments.length})</span>
          </div>

          {/* Comment input */}
          {/* Comment composer — single shell (matches full view) */}
          <div style={{
            marginBottom: 10,
            display: "flex", flexDirection: "column",
            background: "#fff",
            border: "1px solid rgba(28,25,23,0.06)", borderRadius: 10,
            transition: "border-color 0.15s, box-shadow 0.15s",
          }}>
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={t("page.tasks.write_comment")}
              rows={3}
              style={{
                width: "100%", resize: "vertical", fontSize: 12, lineHeight: 1.5,
                padding: "8px 10px", border: "none", outline: "none",
                background: "transparent", color: "#292524",
                fontFamily: "inherit", boxSizing: "border-box", minHeight: 56,
              }}
              onFocus={(e) => {
                const shell = e.currentTarget.parentElement;
                if (shell) { shell.style.borderColor = "#436b65"; shell.style.boxShadow = "0 0 0 3px rgba(28,25,23,0.10)"; }
              }}
              onBlur={(e) => {
                const shell = e.currentTarget.parentElement;
                if (shell) { shell.style.borderColor = "#e7e5e4"; shell.style.boxShadow = "none"; }
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && (comment.trim() || pendingFiles.length > 0)) {
                  e.preventDefault();
                  commentMut.mutate();
                }
              }}
            />
            {pendingFiles.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4, padding: "0 10px 6px" }}>
                {pendingFiles.map((f, i) => (
                  <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 6px", borderRadius: 6, background: "#f5f5f4", border: "1px solid rgba(28,25,23,0.06)", fontSize: 10, color: "#57534e" }}>
                    <IconDocument size={9} style={{ color: "#a8a29e" }} />{f.name}
                    <button type="button" onClick={() => setPendingFiles((p) => p.filter((_, j) => j !== i))}
                      style={{ width: 12, height: 12, padding: 0, border: "none", background: "none", cursor: "pointer", color: "#a8a29e", display: "flex", alignItems: "center", justifyContent: "center" }}>
                      <IconClose size={8} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div style={{
              display: "flex", alignItems: "center", gap: 4,
              padding: "4px 6px",
              borderTop: "1px solid rgba(28,25,23,0.06)",
            }}>
              <input ref={fileRef} type="file" multiple hidden onChange={(e) => { if (e.target.files) { setPendingFiles((p) => [...p, ...Array.from(e.target.files!)]); e.target.value = ""; } }} />
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                title={t("page.tasks.attach_file")}
                style={{
                  width: 26, height: 26, borderRadius: 6, border: "none",
                  background: "transparent", cursor: "pointer",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  color: "#78716c", transition: "background 0.15s, color 0.15s",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = "#f5f5f4"; e.currentTarget.style.color = "#436b65"; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "#78716c"; }}
              >
                <IconPaperclip size={13} />
              </button>
              <span style={{ flex: 1 }} />
              <button
                type="button"
                onClick={() => commentMut.mutate()}
                disabled={(!comment.trim() && pendingFiles.length === 0) || commentMut.isPending}
                title={t("page.tasks.send")}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  padding: "5px 10px", borderRadius: 6, border: "none",
                  background: (comment.trim() || pendingFiles.length > 0) ? "linear-gradient(135deg, #436b65, #5f928a)" : "#e7e5e4",
                  color: (comment.trim() || pendingFiles.length > 0) ? "#fff" : "#a8a29e",
                  cursor: (comment.trim() || pendingFiles.length > 0) ? "pointer" : "not-allowed",
                  fontSize: 11, fontWeight: 600,
                  boxShadow: (comment.trim() || pendingFiles.length > 0) ? "0 1px 2px rgba(28,25,23,0.25)" : "none",
                  transition: "all 0.15s",
                }}
              >
                <IconSend size={11} />
                {t("page.tasks.send")}
              </button>
            </div>
          </div>

          {/* Comment list — shared TaskLogItem (same as TaskDetail) */}
          {comments.map((log: any, i: number) => (
            <TaskLogItem
              key={log.id || i}
              log={log}
              index={i}
              variant="compact"
              formatTime={formatDateLong}
              task={task}
              users={(usersList as any[]) || []}
              agents={(agents as any[]) || []}
              staff={(staffList as any[]) || []}
            />
          ))}
          {comments.length === 0 && <p style={{ fontSize: 12, color: "#a8a29e", margin: 0, textAlign: "center" }}>{t("page.tasks.no_comments_yet")}</p>}
        </div>
      </div>
    </div>
  );
}

function KanbanColumn({
  status,
  agents,
  tasks,
  onDrop,
  onCardClick,
  onOpenTask,
  dragOver,
  onDragEnter,
  onDragLeave,
  wsMap,
  totalCount,
  filtersActive,
  workspaceScoped,
}: {
  status: string;
  tasks: Task[];
  agents?: any[];
  onDrop: (taskId: string) => void;
  onCardClick: (task: Task) => void;
  onOpenTask: (task: Task) => void;
  dragOver: boolean;
  onDragEnter: () => void;
  onDragLeave: () => void;
  wsMap?: Record<string, string>;
  totalCount?: number;
  filtersActive?: boolean;
  workspaceScoped?: boolean;
}) {
  const meta = COLUMN_META[status] ?? { label: status, dot: "#a8a29e", headerBg: "rgba(168,162,158,0.08)", statuses: [], Icon: IconCircleDot };
  const canCollapse = _COLLAPSIBLE.has(status);
  const isDone = status === "done";
  const [collapsed, setCollapsed] = useState(() => canCollapse);
  const [showAllDone, setShowAllDone] = useState(false);
  const PAGE_SIZE = 20;
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const doneBoardManaged = isDone && !filtersActive;
  const orderedTasks = useMemo(
    () => isDone ? [...tasks].sort(compareDoneTasks) : tasks,
    [isDone, tasks],
  );
  const recentDoneTasks = useMemo(
    () => orderedTasks.filter(isRecentDoneTask),
    [orderedTasks],
  );
  const displayTasks = doneBoardManaged && !showAllDone ? recentDoneTasks : orderedTasks;
  const visibleTasks = displayTasks.slice(0, visibleCount);
  const hasMore = displayTasks.length > visibleCount;
  const hiddenDoneCount = doneBoardManaged && !showAllDone
    ? Math.max(0, (totalCount ?? orderedTasks.length) - displayTasks.length)
    : 0;

  useEffect(() => {
    if (canCollapse && filtersActive && tasks.length > 0) {
      setCollapsed(false);
    }
  }, [canCollapse, filtersActive, tasks.length]);

  useEffect(() => {
    if (canCollapse && !filtersActive) {
      setCollapsed(true);
    }
  }, [canCollapse, filtersActive]);

  useEffect(() => {
    setVisibleCount(PAGE_SIZE);
  }, [status, showAllDone, filtersActive, tasks.length]);

  useEffect(() => {
    if (doneBoardManaged) {
      setShowAllDone(false);
    }
  }, [doneBoardManaged]);

  const handleDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      const taskId = e.dataTransfer.getData("taskId");
      if (taskId) onDrop(taskId);
      onDragLeave();
    },
    [onDrop, status, onDragLeave],
  );

  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  return (
    <div
      className="task-board-column"
      title={canCollapse ? `${meta.label} · ${totalCount ?? tasks.length}` : undefined}
      style={{
        flex: collapsed ? "0 0 56px" : "1 0 280px",
        width: collapsed ? 56 : "auto",
        minWidth: collapsed ? 56 : 280,
        maxWidth: collapsed ? 56 : 420,
        background: dragOver
          ? "rgba(255, 255, 254, 0.86)"
          : isDone
            ? "linear-gradient(180deg, rgba(255,255,255,0.74), rgba(255,254,252,0.58))"
            : "linear-gradient(180deg, rgba(255,255,255,0.82), rgba(255,254,252,0.66))",
        backdropFilter: "blur(18px) saturate(1.08)",
        WebkitBackdropFilter: "blur(18px) saturate(1.08)",
        borderRadius: 18,
        border: dragOver ? "2px dashed rgba(79,125,117,0.24)" : "1px solid rgba(117,96,66,0.07)",
        transition: "all 0.25s cubic-bezier(0.4,0,0.2,1)",
        display: "flex",
        flexDirection: "column",
        height: "100%",
        minHeight: 0,
        overflow: "hidden",
        boxShadow: dragOver
          ? "0 0 0 4px rgba(79,125,117,0.04)"
          : "inset 0 1px 0 rgba(255,255,255,0.92), inset 0 -1px 0 rgba(255,255,254,0.48)",
      }}
      onDragOver={handleDragOver}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDrop={handleDrop}
    >
      {/* Column header */}
      <div
        className="task-board-column-header"
        onClick={canCollapse ? () => setCollapsed(!collapsed) : undefined}
        onKeyDown={canCollapse ? (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setCollapsed(!collapsed);
          }
        } : undefined}
        role={canCollapse ? "button" : undefined}
        tabIndex={canCollapse ? 0 : undefined}
        aria-expanded={canCollapse ? !collapsed : undefined}
        style={{
          display: "flex",
          flexDirection: collapsed ? "column" : "row",
          alignItems: "center",
          justifyContent: collapsed ? "center" : "space-between",
          gap: collapsed ? 6 : 0,
          width: "100%",
          boxSizing: "border-box",
          padding: collapsed ? "10px 6px" : "10px 14px",
          borderRadius: collapsed ? 18 : "18px 18px 0 0",
          background: meta.headerBg,
          cursor: canCollapse ? "pointer" : "default",
          userSelect: "none",
          borderBottom: collapsed ? "none" : "1px solid rgba(231,229,228,0.3)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
          {/* Per-column glyph — picked from STATUS_CONFIG family so the
              header reads at a glance even when collapsed. Keeps the
              colored dot below it for the brand colour stripe. */}
          <span style={{
            width: 22, height: 22, borderRadius: 6, background: `${meta.dot}1a`,
            color: meta.dot, flexShrink: 0,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <meta.Icon size={13} />
          </span>
          {!collapsed && <h3 className="task-board-column-title" style={{ fontWeight: 700, fontSize: 12, color: "#292524", margin: 0, letterSpacing: 0 }}>{meta.label}</h3>}
        </div>
        <span className="task-board-column-count" style={{
          fontSize: 10, fontWeight: 700, color: "#57534e",
          background: "rgba(255,255,255,0.7)", padding: collapsed ? "2px 5px" : "2px 8px", borderRadius: 10,
          minWidth: 20, textAlign: "center" as const,
          border: "1px solid rgba(28,25,23,0.06)",
        }}>
          {totalCount ?? tasks.length}
        </span>
      </div>

      {/* Collapsed state */}
      {collapsed && (
        <button
          type="button"
          className="task-board-column-collapsed-label"
          aria-label={`Expand ${meta.label}`}
          onClick={() => setCollapsed(false)}
          style={{
            writingMode: "vertical-rl",
            textOrientation: "mixed",
            padding: "16px 0",
            textAlign: "center",
            fontSize: 11,
            fontWeight: 600,
            color: "#a8a29e",
            flex: 1,
            width: "100%",
            border: 0,
            background: "transparent",
            cursor: "pointer",
            letterSpacing: 0,
          }}
        >
          {meta.label}
        </button>
      )}

      {/* Cards */}
      {!collapsed && (
        <div style={{ display: "flex", flexDirection: "column", gap: visibleTasks.length > 4 ? 6 : 8, padding: "8px 8px 12px", flex: 1, minHeight: 0, overflowY: "auto", overscrollBehavior: "contain" }}>
          {doneBoardManaged && (
            <div
              className="task-board-done-summary"
              style={{
                display: "flex",
                alignItems: "flex-start",
                justifyContent: "space-between",
                gap: 8,
                padding: "9px 10px",
                borderRadius: 12,
                border: "1px solid rgba(79, 156, 132, 0.13)",
                background: "rgba(250, 253, 251, 0.76)",
                color: "#57534e",
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div className="task-board-done-summary-title" style={{ fontSize: 11, fontWeight: 780, color: "#436b65", lineHeight: 1.35 }}>
                  {showAllDone
                    ? t("page.tasks.done_showing_all")
                    : t("page.tasks.done_recent_window").replace("{days}", String(DONE_RECENT_WINDOW_DAYS))}
                </div>
                {!showAllDone && hiddenDoneCount > 0 && (
                  <div className="task-board-done-summary-meta" style={{ marginTop: 3, fontSize: 10, color: "#8a8179", lineHeight: 1.35 }}>
                    {t("page.tasks.done_older_hidden").replace("{count}", String(hiddenDoneCount))}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setShowAllDone((v) => !v); }}
                style={{
                  flexShrink: 0,
                  height: 26,
                  padding: "0 8px",
                  borderRadius: 9,
                  border: "1px solid rgba(79, 113, 105, 0.16)",
                  background: "rgba(255,255,255,0.84)",
                  color: "#4f7169",
                  fontSize: 10,
                  fontWeight: 760,
                  cursor: "pointer",
                }}
              >
                {showAllDone ? t("page.tasks.show_recent_done") : t("page.tasks.show_all_done")}
              </button>
            </div>
          )}
          {displayTasks.length === 0 && (
            <div className="task-board-empty" style={{
              padding: "28px 14px", textAlign: "center", color: "#d6d3d1", fontSize: 12,
              border: "2px dashed rgba(128,102,64,0.08)", borderRadius: 12,
              background: "rgba(255,254,252,0.58)",
            }}>
              {doneBoardManaged && tasks.length > 0
                ? t("page.tasks.no_recent_done")
                : t("page.tasks.drop_tasks_here")}
            </div>
          )}
          {visibleTasks.map((task) => {
            const isCrowded = visibleTasks.length > 4;
            return (
              <BoardTaskCard
                key={task.id} task={task} agents={agents}
                onClick={() => onCardClick(task)}
                onOpenFull={() => onOpenTask(task)}
                dimmed={isDone}
                compact={isCrowded}
                wsName={task.workspace_id && wsMap ? wsMap[task.workspace_id] : undefined}
                workspaceScoped={workspaceScoped}
              />
            );
          })}
          {hasMore && (
            <button
              className="task-board-load-more"
              onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
              style={{
                padding: "8px 0", borderRadius: 10, border: "1px dashed rgba(128,102,64,0.09)",
                background: "rgba(255,254,252,0.7)", cursor: "pointer",
                fontSize: 11, fontWeight: 600, color: "#78716c",
                transition: "all 0.15s",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = "rgba(255,255,255,0.82)"; e.currentTarget.style.color = "#436b65"; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = "rgba(255,254,252,0.7)"; e.currentTarget.style.color = "#78716c"; }}
            >
              {t("page.team_people.show")} {Math.min(PAGE_SIZE, displayTasks.length - visibleCount)} {t("page.tasks.more")}{displayTasks.length - visibleCount} {t("page.tasks.remaining")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ── main component ─────────────────────────────────────── */

export default function Tasks() {
  const { id: taskId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const currentUser = useAuthStore((s) => s.user);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [view, setView] = useState("board");
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [dragOverCol, setDragOverCol] = useState<string | null>(null);
  // workspaceFilter now comes from global useWorkspaceFilter store (wsFilter)
  const [searchQuery, setSearchQuery] = useState("");
  const [priorityFilter, setPriorityFilter] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<TaskStatusFilter>("all");
  const [ownerFilter, setOwnerFilter] = useState<TaskOwnerFilter>("all");
  const [dueFilter, setDueFilter] = useState<TaskDueFilter>("all");
  const [calendarMonth, setCalendarMonth] = useState(() => new Date());
  const wsId = useWorkspaceFilter((s) => s.activeWorkspaceId);
  const workspaceIdParam = searchParams.get("workspaceId") || searchParams.get("workspace_id") || "";
  const isEntityScopeFilter = workspaceIdParam === ENTITY_LEVEL_WORKSPACE_FILTER;
  const wsFilter = isEntityScopeFilter ? ENTITY_LEVEL_WORKSPACE_FILTER : wsId !== "all" ? wsId : undefined;
  const apiWorkspaceFilter = wsFilter === ENTITY_LEVEL_WORKSPACE_FILTER ? undefined : wsFilter;
  const statusFocus = searchParams.get("status") || "";
  const statusFocusLabel =
    statusFocus === "overdue"
      ? "Overdue"
      : TASK_STATUSES[statusFocus]?.label || statusFocus;
  const clearStatusFocus = useCallback(() => {
    const next = new URLSearchParams(searchParams);
    next.delete("status");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  const clearTaskFilters = useCallback(() => {
    setSearchQuery("");
    setPriorityFilter(null);
    setStatusFilter("all");
    setOwnerFilter("all");
    setDueFilter("all");
    useWorkspaceFilter.getState().setActiveWorkspaceId("all");
    const next = new URLSearchParams(searchParams);
    next.delete("status");
    next.delete("workspaceId");
    next.delete("workspace_id");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  // Create form state
  const [formTitle, setFormTitle] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [formPriority, setFormPriority] = useState(3);
  const [formDeadline, setFormDeadline] = useState("");
  const [formStep, setFormStep] = useState(1);
  const [formCategory, setFormCategory] = useState("");
  const [formWorkspace, setFormWorkspace] = useState<string | null>(null);
  const [formAssigneeTab, setFormAssigneeTab] = useState<"self" | "ai" | "agent" | "staff">("self");
  const [formSelectedAgentId, setFormSelectedAgentId] = useState("");
  const [formSelectedStaffUserId, setFormSelectedStaffUserId] = useState("");
  const [formRuntimeRequirement, setFormRuntimeRequirement] = useState("");
  const [formRequiredRefs, setFormRequiredRefs] = useState("");

  /* ── queries ──────────────────────────────────────── */

  // WebSocket task_update events invalidate these queries live. Poll only while
  // a task is actively changing so idle task boards do not keep hitting the API.
  useEffect(() => {
    const nextWorkspaceId = workspaceIdParam && workspaceIdParam !== ENTITY_LEVEL_WORKSPACE_FILTER ? workspaceIdParam : "all";
    if (nextWorkspaceId !== wsId) {
      useWorkspaceFilter.getState().setActiveWorkspaceId(nextWorkspaceId);
    }
  }, [workspaceIdParam, wsId]);

  const setWorkspaceFilter = useCallback((id: string) => {
    useWorkspaceFilter.getState().setActiveWorkspaceId(id === ENTITY_LEVEL_WORKSPACE_FILTER ? "all" : id);
    const next = new URLSearchParams(searchParams);
    if (id === "all") {
      next.delete("workspaceId");
      next.delete("workspace_id");
    } else {
      next.set("workspaceId", id);
      next.delete("workspace_id");
    }
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const tasksPath = wsFilter ? `/tasks?workspaceId=${encodeURIComponent(wsFilter)}` : "/tasks";

  const { data: board, isLoading: boardLoading } = useQuery({
    queryKey: ["taskBoard", wsFilter],
    queryFn: () => api.tasks.board(apiWorkspaceFilter),
    refetchInterval: (query) => hasLiveTasks(query.state.data) ? TASK_POLL_INTERVAL_MS : false,
  });

  const { data: taskConstants } = useQuery({
    queryKey: ["task-constants"],
    queryFn: () => api.tasks.constants(),
  });

  const { data: taskList } = useQuery({
    queryKey: ["tasks", "all", wsFilter],
    queryFn: () => api.tasks.list({ limit: 100, workspace_id: apiWorkspaceFilter }),
    refetchInterval: (query) => hasLiveTasks(query.state.data) ? TASK_POLL_INTERVAL_MS : false,
  });

  const { data: workspaces } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
  });
  const wsMap = useMemo(() => {
    const m: Record<string, string> = {};
    for (const ws of (workspaces ?? []) as any[]) m[ws.id] = ws.name;
    return m;
  }, [workspaces]);

  const { data: taskDetail } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.tasks.get(taskId!),
    enabled: !!taskId,
  });

  const { data: agentsList } = useQuery({
    queryKey: ["agents-list"],
    queryFn: () => api.agents.list(),
  });
  const { data: entityUsersForCreate = [], isLoading: entityUsersLoading } = useQuery({
    queryKey: ["entity-users-for-create-task-assignee", "directory"],
    queryFn: () => api.users.directory(),
    enabled: showCreateModal && formStep === 3 && formAssigneeTab === "staff",
  });
  const { data: workspaceStaff = [], isLoading: workspaceStaffLoading } = useQuery({
    queryKey: ["workspace-staff-for-task-assignee", formWorkspace],
    queryFn: () => api.workspaces.staff.list(formWorkspace!),
    enabled: showCreateModal && formStep === 3 && formAssigneeTab === "staff" && !!formWorkspace,
  });

  const { data: entityStaff, isLoading: entityStaffLoading } = useQuery({
    queryKey: ["entity-staff-for-task-assignee"],
    queryFn: () => api.staff.list(),
    enabled: showCreateModal && formStep === 3 && formAssigneeTab === "staff",
  });

  type StaffAssigneeOption = {
    id: string;
    name: string;
    meta: string;
    avatarUrl?: string | null;
    disabled?: boolean;
    disabledReason?: string | null;
  };

  const assigneeLookupMaps = useMemo(() => {
    const usersById = new Map<string, any>();
    const staffById = new Map<string, any>();
    const staffByUserId = new Map<string, any>();
    for (const user of (entityUsersForCreate || []) as any[]) {
      if (user?.id) usersById.set(user.id, user);
    }
    for (const staff of (entityStaff || []) as any[]) {
      if (staff?.id) staffById.set(staff.id, staff);
      if (staff?.user_id) staffByUserId.set(staff.user_id, staff);
    }
    return { usersById, staffById, staffByUserId };
  }, [entityUsersForCreate, entityStaff]);

  const entityPeopleOptions = useMemo(() => {
    const options: StaffAssigneeOption[] = [];
    const seen = new Set<string>();

    for (const user of (entityUsersForCreate || []) as any[]) {
      if (!user?.id || seen.has(user.id)) continue;
      seen.add(user.id);
      options.push({
        id: user.id,
        name: user.display_name || user.email || t("component.comment_thread.user"),
        meta: user.role || t("component.comment_thread.user"),
        avatarUrl: user.avatar_url,
        disabled: user.status === "inactive",
        disabledReason: user.status === "inactive" ? t("page.tasks.staff_unavailable") : null,
      });
    }

    for (const staff of (entityStaff || []) as any[]) {
      const assigneeId = staff?.user_id || staff?.id;
      if (!assigneeId || seen.has(assigneeId)) continue;
      seen.add(assigneeId);
      const meta = [staff.title, staff.department, staff.kind, staff.email].filter(Boolean).join(" · ");
      options.push({
        id: assigneeId,
        name: staff.display_name || staff.name || staff.email || t("page.workspace_detail.staff"),
        meta,
        avatarUrl: staff.avatar_url,
        disabled: staff.status === "inactive",
        disabledReason: staff.status === "inactive" ? t("page.tasks.staff_unavailable") : null,
      });
    }

    return options;
  }, [entityUsersForCreate, entityStaff]);

  const workspacePeopleOptions = useMemo(() => {
    const options: StaffAssigneeOption[] = [];

    for (const assignment of (workspaceStaff || []) as WorkspaceStaff[]) {
      const staff = assigneeLookupMaps.staffById.get(assignment.staff_id || "")
        || assigneeLookupMaps.staffByUserId.get(assignment.user_id || "");
      const user = assigneeLookupMaps.usersById.get(assignment.user_id || staff?.user_id || "");
      const assigneeId = assignment.user_id || staff?.user_id || staff?.id || assignment.staff_id;
      if (!assigneeId) continue;

      const expired = !!assignment.expires_at && new Date(assignment.expires_at) < new Date();
      const inactive = assignment.status === "inactive" || staff?.status === "inactive" || user?.status === "inactive";
      const name = staff?.display_name || staff?.name || user?.display_name || staff?.email || user?.email || t("page.workspace_detail.staff");
      const meta = [staff?.title, staff?.department, assignment.role, staff?.email || user?.email].filter(Boolean).join(" · ");
      const disabledReason = expired
        ? t("page.tasks.staff_assignment_expired")
        : inactive
          ? t("page.tasks.staff_unavailable")
          : null;
      options.push({
        id: assigneeId,
        name,
        meta,
        avatarUrl: staff?.avatar_url || user?.avatar_url,
        disabled: expired || inactive,
        disabledReason,
      });
    }

    return options;
  }, [workspaceStaff, assigneeLookupMaps]);

  const staffAssigneeOptions: StaffAssigneeOption[] = formWorkspace ? workspacePeopleOptions : entityPeopleOptions;
  const staffAssigneeLoading = formWorkspace
    ? (workspaceStaffLoading || entityStaffLoading || entityUsersLoading)
    : (entityStaffLoading || entityUsersLoading);

  /* ── mutations ────────────────────────────────────── */

  const moveMutation = useMutation({
    mutationFn: ({ taskId: tid, status }: { taskId: string; status: string }) =>
      api.tasks.move(tid, status),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  const createMutation = useMutation({
    mutationFn: (d: Partial<Task>) => api.tasks.create(d),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      setShowCreateModal(false);
      resetForm();
      toast.success(t("page.tasks.task_created"));
    },
  });

  const statusMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api.tasks.updateStatus(id, status),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.tasks.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      setSelectedTask(null);
      navigate(tasksPath);
      toast.success(t("page.tasks.task_deleted"));
    },
  });

  const importMutation = useMutation({
    mutationFn: (file: File) => api.bulk.importTasksCsv(file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
    },
  });

  /* ── handlers ─────────────────────────────────────── */

  const resetForm = () => {
    setFormTitle("");
    setFormDesc("");
    setFormPriority(3);
    setFormDeadline("");
    setFormStep(1);
    setFormCategory("");
    setFormWorkspace(null);
    setFormAssigneeTab("self");
    setFormSelectedAgentId("");
    setFormSelectedStaffUserId("");
    setFormRuntimeRequirement("");
    setFormRequiredRefs("");
  };

  const openCreateModal = (deadline?: string) => {
    resetForm();
    if (apiWorkspaceFilter) setFormWorkspace(apiWorkspaceFilter);
    if (deadline) setFormDeadline(deadline);
    setShowCreateModal(true);
  };

  useEffect(() => {
    setFormSelectedStaffUserId("");
  }, [formWorkspace, formAssigneeTab]);

  const buildRuntimeContextFromForm = () => {
    const requirement = formRuntimeRequirement.trim();
    const refs = formRequiredRefs.split(",").map((v) => v.trim()).filter(Boolean);
    const runtimeContext: Record<string, any> = {};

    if (requirement) {
      runtimeContext.instructions = requirement;
      const inferred = inferRuntimeRuleFromText(requirement, shouldFallbackToWildcardRule(requirement));
      if (inferred.patterns.length > 0) {
        runtimeContext.rules = [{
          rule_key: `task_create_${Date.now()}`,
          rule_type: inferred.rule_type,
          description: requirement,
          severity: inferred.field === "never_allow_actions" ? "high" : "medium",
          action_patterns: inferred.patterns,
          capability_patterns: inferred.capabilityPatterns,
          source: "task_create",
          enabled: true,
        }];
      }
    }

    if (refs.length > 0) {
      runtimeContext.required_refs = refs;
    }

    return Object.keys(runtimeContext).length > 0 ? runtimeContext : null;
  };

  const handleCreate = () => {
    if (!formTitle.trim()) return;
    const payload: Partial<Task> & Record<string, any> = {
      title: formTitle,
      description: formDesc,
      priority: formPriority,
      deadline: formDeadline || undefined,
      workspace_id: formWorkspace || undefined,
      category_id: formCategory || undefined,
    };
    // Assignee from step 3
    if (formAssigneeTab === "ai") {
      payload.agent_id = MANOR_AGENT_ID;
      payload.agent_type = MANOR_AGENT_TYPE;
    } else if (formAssigneeTab === "agent" && formSelectedAgentId) {
      payload.agent_id = formSelectedAgentId;
      payload.agent_type = "agent";
    } else if (formAssigneeTab === "staff" && formSelectedStaffUserId) {
      payload.assignee_id = formSelectedStaffUserId;
    } else if (formAssigneeTab === "self" && currentUser?.id) {
      payload.assignee_id = currentUser.id;
    }
    const runtimeContext = buildRuntimeContextFromForm();
    if (runtimeContext) {
      payload.details = { runtime_context: runtimeContext };
    }
    // "staff" currently leaves assignment empty until the staff picker is wired.
    createMutation.mutate(payload);
  };

  const taskCreateDisabled = !formTitle.trim()
    || createMutation.isPending
    || (formAssigneeTab === "staff" && !formSelectedStaffUserId);

  const handleDrop = useCallback(
    (tid: string, newStatus: string) => {
      let currentStatus: string | null = null;
      if (board) {
        // ``board`` carries a ``_counts`` meta key (status → total)
        // alongside the per-status task arrays. Skip non-array entries
        // so we don't call .some() on the counts dict.
        for (const [status, tasks] of Object.entries(board)) {
          if (!Array.isArray(tasks)) continue;
          if (tasks.some((t: Task) => t.id === tid)) {
            currentStatus = status;
            break;
          }
        }
      }
      if (currentStatus === newStatus) return;
      const allowed = currentStatus ? taskConstants?.status_transitions?.[currentStatus] : null;
      if (currentStatus && allowed && !allowed.includes(newStatus)) {
        const from = TASK_STATUSES[currentStatus]?.label || currentStatus;
        const to = TASK_STATUSES[newStatus]?.label || newStatus;
        toast.warning(
          t("page.tasks.move_not_allowed"),
          t("page.tasks.move_not_allowed_detail")
            .replace("{from}", from)
            .replace("{to}", to),
        );
        return;
      }
      moveMutation.mutate({ taskId: tid, status: newStatus });
    },
    [board, moveMutation, taskConstants?.status_transitions, toast],
  );

  const handleCardClick = useCallback((task: Task) => {
    setSelectedTask(task);
  }, []);

  const handleExportCsv = async () => {
    try {
      const csv = await api.bulk.exportTasksCsv();
      const blob = new Blob([csv], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "tasks.csv";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // silently fail
    }
  };

  const handleImportCsv = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) importMutation.mutate(file);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  /* ── derived data ─────────────────────────────────── */

  const allTasks: Task[] = taskList?.items ?? [];
  const activeDetail = taskDetail ?? selectedTask;
  const agentById = useMemo(() => {
    const map = new Map<string, any>();
    for (const agent of (agentsList || []) as any[]) {
      if (agent?.id) map.set(agent.id, agent);
    }
    return map;
  }, [agentsList]);
  const ownerFilterOptions = useMemo<TaskFilterOption<TaskOwnerFilter>[]>(() => {
    const ownerMap = new Map<TaskOwnerFilter, { label: string; icon: ReactNode; count: number }>();
    const addOwner = (key: TaskOwnerFilter, label: string, icon: ReactNode) => {
      const existing = ownerMap.get(key);
      ownerMap.set(key, { label, icon, count: (existing?.count || 0) + 1 });
    };

    for (const task of allTasks) {
      if (!matchesWorkspaceFilter(task, wsFilter)) continue;
      const key = taskOwnerFilterKey(task);
      const isMaster = isMasterAgent(task.agent_id, task.agent_type);
      if (key.startsWith("agent:")) {
        const agentId = task.agent_id || MANOR_AGENT_ID;
        const agent = agentById.get(agentId);
        const label = task.agent_name || agent?.name || (isMaster ? MANOR_AGENT_NAME : t("page.tasks.ai_agent"));
        addOwner(
          key,
          label,
          <UserAvatar
            type={isMaster ? "manor" : "agent"}
            name={label}
            avatarUrl={task.agent_avatar || agent?.avatar_url || null}
            seed={agentId}
            size={16}
          />,
        );
      } else if (key.startsWith("person:")) {
        const label = task.assignee_name
          || (task.assignee_id === currentUser?.id ? currentUser?.display_name || currentUser?.email : "")
          || task.assignee_id
          || t("component.comment_thread.user");
        addOwner(
          key,
          label,
          <UserAvatar
            type="user"
            name={label}
            avatarUrl={task.assignee_avatar || (task.assignee_id === currentUser?.id ? currentUser?.avatar_url : null) || null}
            size={16}
          />,
        );
      } else {
        addOwner("unassigned", "Unassigned", <IconUser size={14} style={{ color: "#a8a29e" }} />);
      }
    }

    return [
      { key: "all", label: t("page.workspaces.filter_all"), icon: <IconUsers size={14} style={{ color: "#a8a29e" }} /> },
      ...Array.from(ownerMap.entries())
        .sort(([, a], [, b]) => b.count - a.count || a.label.localeCompare(b.label))
        .map(([key, value]) => ({
          key,
          label: value.count > 0 ? `${value.label} (${value.count})` : value.label,
          icon: value.icon,
        })),
    ];
  }, [agentById, allTasks, currentUser?.avatar_url, currentUser?.display_name, currentUser?.email, currentUser?.id, wsFilter]);
  useEffect(() => {
    if (ownerFilter !== "all" && !ownerFilterOptions.some((option) => option.key === ownerFilter)) {
      setOwnerFilter("all");
    }
  }, [ownerFilter, ownerFilterOptions]);
  const hasTaskFilters = !!searchQuery.trim()
    || !!wsFilter
    || priorityFilter !== null
    || statusFilter !== "all"
    || ownerFilter !== "all"
    || dueFilter !== "all"
    || !!statusFocus;
  const activeFilterCount = [
    !!searchQuery.trim(),
    !!wsFilter,
    priorityFilter !== null,
    statusFilter !== "all",
    ownerFilter !== "all",
    dueFilter !== "all",
    !!statusFocus,
  ].filter(Boolean).length;
  const boardContentFiltersActive = !!searchQuery.trim()
    || priorityFilter !== null
    || statusFilter !== "all"
    || ownerFilter !== "all"
    || dueFilter !== "all"
    || !!statusFocus;
  const taskMatchesFilters = useCallback((task: Task) => {
    if (!matchesWorkspaceFilter(task, wsFilter)) return false;
    if (statusFocus && !matchesStatusFocus(task, statusFocus)) return false;
    if (!taskMatchesSearch(task, searchQuery)) return false;
    if (priorityFilter !== null && task.priority !== priorityFilter) return false;
    if (!matchesStatusFilter(task, statusFilter)) return false;
    if (!matchesOwnerFilter(task, ownerFilter)) return false;
    if (!matchesDueFilter(task, dueFilter)) return false;
    return true;
  }, [dueFilter, ownerFilter, priorityFilter, searchQuery, statusFilter, statusFocus, wsFilter]);
  const filteredCalendarTasks = useMemo(
    () => allTasks.filter(taskMatchesFilters),
    [allTasks, taskMatchesFilters],
  );
  const todayKey = useMemo(() => localDateKey(new Date()), []);
  const { data: dailyAgenda } = useQuery({
    queryKey: ["calendar-agenda-day", todayKey],
    queryFn: () => api.calendarSettings.day(todayKey),
    enabled: view === "calendar",
    staleTime: 60_000,
  });
  const { data: calendarSettingsData } = useQuery({
    queryKey: ["calendar-settings"],
    queryFn: () => api.calendarSettings.get(),
    enabled: view === "calendar",
    staleTime: 60_000,
  });
  const calendarVisibleRange = useMemo(() => calendarVisibleRangeForMonth(calendarMonth), [calendarMonth]);
  const { data: externalCalendarData } = useQuery({
    queryKey: ["calendar-settings-events", calendarVisibleRange.start, calendarVisibleRange.end],
    queryFn: () => api.calendarSettings.events(calendarVisibleRange.start, calendarVisibleRange.end),
    enabled: view === "calendar",
    staleTime: 120_000,
  });
  const taskCalendarEvents = useMemo(
    () =>
      filteredCalendarTasks
        .filter((task) => task.deadline || task.scheduled_at)
        .map((task): CalendarEvent => ({
          date: task.scheduled_at || task.deadline!,
          title: task.title,
          id: task.id,
          source: "task",
          priority: task.priority,
          status: task.status,
          agent_name: task.agent_name,
          assignee_name: task.assignee_name,
          agent_avatar: task.agent_avatar,
          assignee_avatar: task.assignee_avatar,
          agent_type: task.agent_type,
          agent_id: task.agent_id,
          scheduled_at: task.scheduled_at,
          duration_minutes: task.duration_minutes,
        })),
    [filteredCalendarTasks],
  );
  const bookingCalendarEvents = useMemo(
    () =>
      (calendarSettingsData?.settings.bookings || [])
        .filter((booking) => booking.status !== "cancelled")
        .map((booking): CalendarEvent => ({
          date: booking.starts_at,
          title: `${booking.guest_name}: ${humanizeBookingSlug(booking.booking_link_slug)}`,
          id: `booking:${booking.id}`,
          source: "booking",
          status: booking.status,
          scheduled_at: booking.starts_at,
          duration_minutes: bookingDurationMinutes(booking),
          timezone: booking.timezone,
          guest_name: booking.guest_name,
          guest_email: booking.guest_email,
          note: booking.note,
          location_label: bookingLocationLabel(booking),
          booking_link_id: booking.booking_link_id,
          booking_link_slug: booking.booking_link_slug,
          calendar_event_url: booking.calendar_event_url,
          meeting_url: booking.meeting_url,
        })),
    [calendarSettingsData?.settings.bookings],
  );
  const externalCalendarEvents = useMemo(
    () =>
      (externalCalendarData?.events || []).map((event): CalendarEvent => ({
        date: event.starts_at,
        title: event.title || "Untitled event",
        id: event.id,
        source: "external",
        status: event.status ?? undefined,
        scheduled_at: event.starts_at,
        duration_minutes: eventDurationMinutes(event),
        timezone: event.timezone || externalCalendarData?.timezone,
        location_label: event.location,
        calendar_event_url: event.calendar_event_url,
        meeting_url: event.meeting_url,
        external_provider: event.provider,
        external_event_id: event.external_event_id,
        calendar_id: event.calendar_id,
        calendar_name: event.calendar_name,
        all_day: event.all_day,
        description: event.description,
        organizer_email: event.organizer_email,
        attendee_count: event.attendee_count,
      })),
    [externalCalendarData],
  );
  const calendarEvents = useMemo(
    () => [...taskCalendarEvents, ...bookingCalendarEvents, ...externalCalendarEvents],
    [bookingCalendarEvents, externalCalendarEvents, taskCalendarEvents],
  );

  // Workspace health strip data
  const wsTaskCounts: Record<string, number> = {};
  let entityLevelTaskCount = 0;
  if (board) {
    const boardTasks = Object.entries(board)
      .filter(([key]) => key !== "_counts")
      .flatMap(([, tasks]) => Array.isArray(tasks) ? tasks : []) as Task[];
    for (const t of boardTasks) {
      if (t.workspace_id) {
        wsTaskCounts[t.workspace_id] = (wsTaskCounts[t.workspace_id] ?? 0) + 1;
      } else {
        entityLevelTaskCount += 1;
      }
    }
  }

  const boardCounts: Record<string, number> = (board as any)?._counts ?? {};
  const totalBoardTasks = Object.keys(boardCounts).length > 0
    ? Object.values(boardCounts).reduce((sum, n) => sum + (n as number), 0)
    : board
      ? Object.entries(board).filter(([k]) => k !== "_counts").reduce((sum, [, tasks]) => sum + (tasks as Task[]).length, 0)
      : 0;
  const workspaceFilterOptions: TaskFilterOption[] = [
    { key: "all", label: `${t("page.workspaces.filter_all")} (${totalBoardTasks})`, icon: <IconLayers size={14} style={{ color: "#a8a29e" }} /> },
    {
      key: ENTITY_LEVEL_WORKSPACE_FILTER,
      label: entityLevelTaskCount > 0 ? `Entity-level (${entityLevelTaskCount})` : "Entity-level",
      icon: <IconBuilding size={14} style={{ color: "#78716c" }} />,
    },
    ...(workspaces ?? []).map((ws: Workspace) => {
      const count = wsTaskCounts[ws.id] ?? 0;
      return { key: ws.id, label: count > 0 ? `${ws.name} (${count})` : ws.name, icon: <IconWorkspace size={14} style={{ color: "#57534e" }} /> };
    }),
  ];
  const priorityFilterOptions: TaskFilterOption[] = [
    { key: "all", label: t("page.workspaces.filter_all"), icon: <IconFlag size={14} style={{ color: "#a8a29e" }} /> },
    ...[5, 4, 3, 2, 1].map((p) => ({
      key: String(p),
      label: PRIORITY_CONFIG[p].label,
      icon: <IconFlag size={14} style={{ color: PRIORITY_CONFIG[p].color }} />,
    })),
  ];

  // Operation status counts
  const statusCounts = {
    pending: board ? ((board as Record<string, Task[]>)["pending"] ?? []).length : 0,
    in_progress: board ? ((board as Record<string, Task[]>)["in_progress"] ?? []).length : 0,
    completed: board ? ((board as Record<string, Task[]>)["completed"] ?? []).length : 0,
  };
  const taskViewTabs = <TabSwitcher tabs={VIEW_TABS} value={view} onChange={setView} wrap />;

  /* ── render ───────────────────────────────────────── */

  return (
    <div style={{ display: "flex", height: "100%" }}>
      <style>{TASK_CARD_AI_PROCESSING_STYLES}</style>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {view !== "automations" && (
          <div style={{ padding: "24px 24px 0" }}>
            <PageHeader
              title={t("page.tasks.my_tasks")}
              subtitle={`${totalBoardTasks || allTasks.length} ${t("page.tasks.tasks")}`}
              compactControls
              tabs={taskViewTabs}
              toolbar={(
                <SmartToolbar
                  searchValue={searchQuery}
                  onSearchChange={setSearchQuery}
                  searchPlaceholder={t("page.tasks.search_tasks")}
                  className="w-full sm:w-52"
                />
              )}
              actions={<PageHeaderAddButton label={t("page.tasks.add_task")} onClick={() => openCreateModal()} />}
            />
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv"
              className="hidden"
              onChange={handleImportCsv}
            />

            <FilterBar
              activeCount={activeFilterCount}
              trailing={(
                <>
                  {hasTaskFilters && (
                    <button
                      type="button"
                      onClick={clearTaskFilters}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        height: 34,
                        padding: "0 10px",
                        borderRadius: 10,
                        border: "1px solid transparent",
                        background: "transparent",
                        color: "#78716c",
                        fontSize: 12,
                        fontWeight: 700,
                        cursor: "pointer",
                        whiteSpace: "nowrap",
                      }}
                    >
                      <IconClose size={13} />
                      {t("common.clear")}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn-manor-outline"
                    onClick={() => fileInputRef.current?.click()}
                    title={t("action.import")}
                    style={{ width: 34, height: 34, padding: 0, borderRadius: 10, background: "rgba(255,255,255,0.78)" }}
                  >
                    <IconUpload size={13} />
                  </button>
                  <button
                    type="button"
                    className="btn-manor-outline"
                    onClick={handleExportCsv}
                    title={t("action.export")}
                    style={{ width: 34, height: 34, padding: 0, borderRadius: 10, background: "rgba(255,255,255,0.78)" }}
                  >
                    <IconDownload size={13} />
                  </button>
                </>
              )}
            >
              <FilterSelect
                label={t("nav.workspaces")}
                Icon={IconWorkspace}
                value={wsFilter ?? "all"}
                onChange={setWorkspaceFilter}
                options={workspaceFilterOptions}
                width={204}
                dropdownMinWidth={240}
                filterable
              />
              <FilterSelect
                label={t("page.agent_dashboard.status")}
                Icon={IconCircleDot}
                value={statusFilter}
                onChange={(value) => setStatusFilter(value as TaskStatusFilter)}
                options={STATUS_FILTER_OPTIONS}
                width={136}
                dropdownMinWidth={178}
              />
              <FilterSelect
                label={t("page.tasks.priority")}
                Icon={IconFlag}
                value={priorityFilter === null ? "all" : String(priorityFilter)}
                onChange={(key) => setPriorityFilter(key === "all" ? null : Number(key))}
                options={priorityFilterOptions}
                width={140}
                dropdownMinWidth={128}
              />
              <FilterSelect
                label={t("component.embedded_chat.assignee")}
                Icon={IconUser}
                value={ownerFilter}
                onChange={(value) => setOwnerFilter(value as TaskOwnerFilter)}
                options={ownerFilterOptions}
                width={168}
                valueMinWidth={76}
                dropdownMinWidth={220}
                showSelectedIcon
              />
              <FilterSelect
                label={t("page.task_process.due")}
                Icon={IconCalendar}
                value={dueFilter}
                onChange={(value) => setDueFilter(value as TaskDueFilter)}
                options={DUE_FILTER_OPTIONS}
                width={136}
                dropdownMinWidth={144}
              />
              {statusFocus && (
                <button
                  type="button"
                  onClick={clearStatusFocus}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 6,
                    height: 34,
                    padding: "0 10px",
                    borderRadius: 11,
                    border: "1px solid #ccded9",
                    background: "#f5f5f4",
                    color: "#57534e",
                    fontSize: 12,
                    fontWeight: 800,
                    cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  Focus: {statusFocusLabel}
                  <IconClose size={13} />
                </button>
              )}
            </FilterBar>
          </div>
        )}

        {/* View content */}
        <div
          key={view}
          style={{
            flex: 1,
            minHeight: 0,
            overflowX: view === "automations" ? "hidden" : "auto",
            overflowY: view === "automations" ? "auto" : "hidden",
            padding: view === "automations" ? "24px 24px 24px" : "8px 24px 24px",
          }}
        >
          {/* ── Board View ──────────────────────────── */}
          {view === "board" && (
            boardLoading ? (
              <div style={{ display: "flex", gap: "16px" }}>
                {BOARD_COLUMNS.map((col) => (
                  <div key={col} style={{ flex: "1 1 0", minWidth: "260px", maxWidth: "340px", background: "linear-gradient(180deg, rgba(255,255,255,0.76), rgba(255,254,252,0.62))", border: "1px solid rgba(128,102,64,0.075)", borderRadius: "16px", padding: "14px" }}>
                    <div style={{ height: "16px", width: "80px", background: "rgba(235,231,224,0.55)", borderRadius: "8px", marginBottom: "12px" }} />
                    <div style={{ height: "80px", background: "rgba(255,255,255,0.64)", borderRadius: "14px", marginBottom: "8px" }} />
                    <div style={{ height: "80px", background: "rgba(255,255,255,0.64)", borderRadius: "14px" }} />
                  </div>
                ))}
              </div>
            ) : board ? (
              <div style={{ display: "flex", gap: "16px", width: "100%", minWidth: 0, height: "100%", paddingBottom: "16px", alignItems: "stretch" }}>
                {BOARD_COLUMNS.map((colKey) => {
                  const colMeta = COLUMN_META[colKey];
                  // Merge tasks from all statuses in this column group
                  let tasks = (colMeta.statuses.flatMap((s) => (board[s] ?? []) as Task[]));
                  // Real total from _counts (before client-side filters)
                  const colTotal = colMeta.statuses.reduce((sum, s) => sum + (boardCounts[s] || 0), 0);
                  tasks = tasks.filter(taskMatchesFilters);
                  const dropStatus = colMeta.statuses[0];
                  return (
                  <KanbanColumn
                    key={colKey}
                    status={colKey}
                    tasks={tasks}
                    totalCount={hasTaskFilters ? tasks.length : colTotal || undefined}
                    filtersActive={boardContentFiltersActive}
                    workspaceScoped={Boolean(apiWorkspaceFilter)}
                    agents={agentsList as any[]}
                    wsMap={wsMap}
                    onDrop={(taskId) => handleDrop(taskId, dropStatus)}
                    onCardClick={handleCardClick}
                    onOpenTask={(task) => navigate(`/tasks/${task.id}`)}
                    dragOver={dragOverCol === colKey}
                    onDragEnter={() => setDragOverCol(colKey)}
                    onDragLeave={() => setDragOverCol((prev) => (prev === colKey ? null : prev))}
                  />
                  );
                })}
              </div>
            ) : (
              <EmptyState title={t("page.tasks.no_tasks_found")} description={t("page.tasks.create_to_get_started")} />
            )
          )}

          {/* ── Calendar View ───────────────────────── */}
          {view === "calendar" && (
            <div>
              <CalendarOpsRail
                items={dailyAgenda?.items || []}
                timezone={dailyAgenda?.timezone}
                onOpenTask={(id) => navigate(`/tasks/${id}`)}
                onManageBookingLinks={() => navigate("/settings?tab=calendar")}
              />
              <Calendar
                month={calendarMonth}
                events={calendarEvents}
                onMonthChange={setCalendarMonth}
                onDateClick={(date) => {
                  openCreateModal(`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`);
                }}
                onEventClick={(event) => {
                  const task = allTasks.find((t) => t.id === event.id);
                  if (task) {
                    setSelectedTask(task);
                    navigate(`/tasks/${task.id}`);
                  }
                }}
              />
            </div>
          )}

          {/* ── Automations View (Scheduled Jobs) ── */}
          {view === "automations" && (
            <ScheduledJobsEmbed headerTabs={taskViewTabs} />
          )}

        </div>
      </div>

      {/* ── Detail Panel ────────────────────────────── */}
      {activeDetail && (
        <InlineTaskDetail
          task={activeDetail}
          agents={agentsList || []}
          statusTransitions={taskConstants?.status_transitions}
          onClose={() => { setSelectedTask(null); navigate(tasksPath); }}
          onOpenFull={() => navigate(`/tasks/${activeDetail.id}`)}
          onUpdate={(data) => statusMutation.mutate({ id: activeDetail.id, status: data.status || activeDetail.status })}
          onDelete={() => deleteMutation.mutate(activeDetail.id)}
        />
      )}

      {/* ── Create Task Modal (3-Step) ──────────────── */}
      <Modal
        open={showCreateModal}
        onClose={() => { setShowCreateModal(false); resetForm(); }}
        title={t("page.tasks.new_task")}
        maxWidth="640px"
        footer={
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
            {/* Step dots */}
            <div style={{ display: "flex", gap: 6 }}>
              {[1, 2, 3].map((s) => (
                <span
                  key={s}
                  style={{
                    width: 8, height: 8, borderRadius: "50%",
                    background: formStep >= s ? "#4f7d75" : "transparent",
                    border: formStep >= s ? "2px solid #4f7d75" : "2px solid #d6d3d1",
                    transition: "all 0.2s",
                  }}
                />
              ))}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              {formStep > 1 && (
                <Button variant="outline" onClick={() => setFormStep(formStep - 1)}>
                  {t("page.onboarding.back")}
                </Button>
              )}
              {formStep < 3 ? (
                <button
                  onClick={() => setFormStep(formStep + 1)}
                  disabled={formStep === 2 && !formTitle.trim()}
                  style={{
                    padding: "8px 20px", borderRadius: 10, fontSize: 13, fontWeight: 600,
                    border: "none", cursor: (formStep === 2 && !formTitle.trim()) ? "not-allowed" : "pointer",
                    background: "#1c1917", color: "#ffffff",
                    opacity: (formStep === 2 && !formTitle.trim()) ? 0.5 : 1,
                    transition: "opacity 0.2s",
                  }}
                >
                  {t("page.onboarding.next")}
                </button>
              ) : (
                <button
                  onClick={handleCreate}
                  disabled={taskCreateDisabled}
                  style={{
                    padding: "8px 20px", borderRadius: 10, fontSize: 13, fontWeight: 600,
                    border: "none",
                    cursor: taskCreateDisabled ? "not-allowed" : "pointer",
                    background: "#1c1917", color: "#ffffff",
                    opacity: taskCreateDisabled ? 0.5 : 1,
                    transition: "opacity 0.2s",
                  }}
                >
                  {createMutation.isPending ? t("page.flows.creating") : t("page.tasks.create_task")}
                </button>
              )}
            </div>
          </div>
        }
      >
        {/* ── Step 1: Scope ── */}
        {formStep === 1 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
              <span style={{ width: 24, height: 24, borderRadius: "50%", background: "#1c1917", color: "#fff", fontSize: 12, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center" }}>1</span>
              <span style={{ fontSize: 14, fontWeight: 600, color: "#292524" }}>{t("page.tasks.where_should_this_task_live")}</span>
            </div>

            {/* Entity-level option */}
            <button
              onClick={() => setFormWorkspace(null)}
              style={{
                display: "flex", alignItems: "center", gap: 12, padding: 16, borderRadius: 14,
                border: formWorkspace === null ? "2px solid #4f7d75" : "1px solid #e7e5e4",
                background: formWorkspace === null ? "rgba(79,125,117,0.04)" : "rgba(255,255,255,0.7)",
                backdropFilter: "blur(8px)", cursor: "pointer", textAlign: "left" as const, transition: "all 0.2s",
              }}
            >
              <div style={{ width: 40, height: 40, borderRadius: 12, background: formWorkspace === null ? "rgba(79,125,117,0.1)" : "#f5f5f4", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={formWorkspace === null ? "#4f7d75" : "#78716c"} strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75c.621 0 1.125.504 1.125 1.125V21" />
                </svg>
              </div>
              <div>
                <p style={{ fontSize: 14, fontWeight: 600, color: formWorkspace === null ? "#4f7d75" : "#44403c", margin: 0 }}>{t("page.tasks.entity_level")}</p>
                <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0" }}>{t("page.tasks.applies_across_all_workspaces")}</p>
              </div>
            </button>

            {/* Workspace options */}
            {(workspaces ?? []).map((ws: Workspace) => {
              const isActive = formWorkspace === ws.id;
              return (
                <button
                  key={ws.id}
                  onClick={() => setFormWorkspace(ws.id)}
                  style={{
                    display: "flex", alignItems: "center", gap: 12, padding: 16, borderRadius: 14,
                    border: isActive ? "2px solid #4f7d75" : "1px solid #e7e5e4",
                    background: isActive ? "rgba(79,125,117,0.04)" : "rgba(255,255,255,0.7)",
                    backdropFilter: "blur(8px)", cursor: "pointer", textAlign: "left" as const, transition: "all 0.2s",
                  }}
                >
                  <div style={{ width: 40, height: 40, borderRadius: 12, background: isActive ? "rgba(79,125,117,0.1)" : "#f5f5f4", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={isActive ? "#4f7d75" : "#78716c"} strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
                      <path d="M2.25 21h19.5m-18-18v18m10.5-18v18m6-13.5V21M6.75 6.75h.75m-.75 3h.75m-.75 3h.75m3-6h.75m-.75 3h.75m-.75 3h.75M6.75 21v-3.375c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21M3 3h12m-.75 4.5H21m-3.75 3H21m-3.75 3H21" />
                    </svg>
                  </div>
                  <div>
                    <p style={{ fontSize: 14, fontWeight: 600, color: isActive ? "#4f7d75" : "#44403c", margin: 0 }}>{ws.name}</p>
                    <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0" }}>{ws.category || ws.kind || t("page.tasks.operation")}</p>
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {/* ── Step 2: Details ── */}
        {formStep === 2 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
              <span style={{ width: 24, height: 24, borderRadius: "50%", background: "#1c1917", color: "#fff", fontSize: 12, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center" }}>2</span>
              <span style={{ fontSize: 14, fontWeight: 600, color: "#292524" }}>{t("page.tasks.task_details")}</span>
            </div>

            <Input label={t("page.tasks.title")} value={formTitle} onChange={(e) => setFormTitle(e.target.value)} placeholder={t("page.tasks.task_title")} />
            <Textarea label={t("page.task_collections.description")} value={formDesc} onChange={(e) => setFormDesc(e.target.value)} rows={3} placeholder={t("page.task_collections.optional_description")} />
            <div>
              <label className="manor-label">{t("page.tasks.deadline")}</label>
              <DateTimePicker value={formDeadline} onChange={setFormDeadline} placeholder={t("page.tasks.no_deadline")} />
            </div>

            {/* Priority chips */}
            <div>
              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>{t("page.tasks.priority")}</label>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {[5, 4, 3, 2, 1].map((p) => {
                  const cfg = PRIORITY_CONFIG[p];
                  const isActive = formPriority === p;
                  return (
                    <button
                      key={p}
                      onClick={() => setFormPriority(p)}
                      style={{
                        display: "inline-flex", alignItems: "center", gap: 6,
                        padding: "6px 14px", borderRadius: 20,
                        border: isActive ? `2px solid ${cfg.color}` : "1px solid #e7e5e4",
                        background: isActive ? `${cfg.color}10` : "#ffffff",
                        cursor: "pointer", fontSize: 13, fontWeight: 500,
                        color: isActive ? "#292524" : "#78716c", transition: "all 0.2s",
                      }}
                    >
                      <span style={{ width: 8, height: 8, borderRadius: "50%", background: cfg.color }} />
                      {cfg.label}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Category */}
            <div>
              <label className="manor-label">{t("page.workspaces.category")}</label>
              <Select
                value={formCategory}
                onChange={setFormCategory}
                options={[{ value: "", label: t("page.workspace_detail.none") }, ...CATEGORY_OPTIONS.map((c) => ({ value: c.key, label: c.label }))]}
                placeholder={t("page.tasks.select_category")}
                filterable
              />
            </div>
          </div>
        )}

        {/* ── Step 3: Assignee ── */}
        {formStep === 3 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
              <span style={{ width: 24, height: 24, borderRadius: "50%", background: "#1c1917", color: "#fff", fontSize: 12, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center" }}>3</span>
              <span style={{ fontSize: 14, fontWeight: 600, color: "#292524" }}>{t("page.tasks.who_should_handle_this")}</span>
            </div>

            {/* Assignee tab switcher */}
            <div style={{ display: "flex", gap: 4, padding: 3, background: "rgba(245,245,244,0.8)", borderRadius: 12 }}>
              {([
                { key: "self" as const, label: t("page.tasks.myself") },
                { key: "ai" as const, label: t("page.chat_history.manor_ai") },
                { key: "agent" as const, label: t("page.workspace_detail.agent") },
                { key: "staff" as const, label: t("page.workspace_detail.staff") },
              ]).map((tab) => {
                const isActive = formAssigneeTab === tab.key;
                return (
                  <button
                    key={tab.key}
                    onClick={() => setFormAssigneeTab(tab.key)}
                    style={{
                      flex: 1, padding: "8px 12px", borderRadius: 9, fontSize: 13, fontWeight: 600,
                      border: "none", cursor: "pointer", transition: "all 0.2s",
                      background: isActive ? "#ffffff" : "transparent",
                      color: isActive ? "#1c1917" : "#78716c",
                      boxShadow: isActive ? "0 1px 4px rgba(0,0,0,0.06)" : "none",
                    }}
                  >
                    {tab.label}
                  </button>
                );
              })}
            </div>

            {/* Tab content */}
            {formAssigneeTab === "self" && (
              <div style={{ display: "flex", alignItems: "center", gap: 12, padding: 20, borderRadius: 14, border: "2px solid #4f7d75", background: "rgba(79,125,117,0.04)" }}>
                <div style={{ width: 44, height: 44, borderRadius: "50%", background: "linear-gradient(135deg, #e8eff4, #ddd6fe)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16, fontWeight: 700, color: "#57534e" }}>
                  {t("page.tasks.me")}
                </div>
                <div>
                  <p style={{ fontSize: 14, fontWeight: 600, color: "#57534e", margin: 0 }}>{t("page.tasks.assign_to_myself")}</p>
                  <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0" }}>{t("page.tasks.you_will_be_responsible_for_this_task")}</p>
                </div>
              </div>
            )}

            {formAssigneeTab === "ai" && (
              <div style={{ display: "flex", alignItems: "center", gap: 12, padding: 20, borderRadius: 14, border: "2px solid #4f7d75", background: "rgba(79,125,117,0.04)" }}>
                <div style={{ width: 44, height: 44, borderRadius: "50%", background: "linear-gradient(135deg, #efedea, #ccded9)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#4f7d75" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
                    <path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
                  </svg>
                </div>
                <div>
                  <p style={{ fontSize: 14, fontWeight: 600, color: "#57534e", margin: 0 }}>{t("page.chat_history.manor_ai")}</p>
                  <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0" }}>{t("page.tasks.let_ai_handle_and_complete_this_task")}</p>
                </div>
              </div>
            )}

            {formAssigneeTab === "agent" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>{t("page.tasks.select_an_agent_to_assign_this_task_to")}</p>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 8 }}>
                  {(agentsList || []).map((agent: any) => {
                    const isActive = formSelectedAgentId === agent.id;
                    const description = getAgentDescription(agent);
                    return (
                      <button key={agent.id} onClick={() => setFormSelectedAgentId(agent.id)}
                        style={{
                          display: "flex", alignItems: "center", gap: 10, padding: 12, borderRadius: 12,
                          border: isActive ? "2px solid #4f7d75" : "1px solid #e7e5e4",
                          background: isActive ? "rgba(79,125,117,0.04)" : "#ffffff",
                          cursor: "pointer", textAlign: "left" as const, transition: "all 0.2s",
                        }}>
                        <div style={{ width: 32, height: 32, borderRadius: "50%", background: "linear-gradient(135deg, #dceae3, #c4dfd2)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                          <IconAgent size={16} style={{ color: "#437f6b" }} />
                        </div>
                        <div style={{ minWidth: 0 }}>
                          <p style={{ fontSize: 13, fontWeight: 600, color: "#292524", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent.name}</p>
                          {description && <p style={{ fontSize: 11, color: "#a8a29e", margin: "2px 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{description}</p>}
                        </div>
                      </button>
                    );
                  })}
                  {(agentsList || []).length === 0 && <p style={{ fontSize: 12, color: "#a8a29e", margin: 0, gridColumn: "1/-1", textAlign: "center", padding: 12 }}>{t("page.tasks.no_agents_available")}</p>}
                </div>
              </div>
            )}

            {formAssigneeTab === "staff" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>{t("page.tasks.select_a_staff_member")}</p>
                <p style={{ fontSize: 11, color: "#a8a29e", margin: "-2px 0 4px" }}>
                  {formWorkspace ? t("page.tasks.workspace_staff_hint") : t("page.tasks.entity_staff_hint")}
                </p>
                {staffAssigneeLoading ? (
                  <div style={{ padding: 16, borderRadius: 12, border: "1px solid rgba(28,25,23,0.06)", background: "#fafaf9", textAlign: "center", fontSize: 13, color: "#a8a29e" }}>
                    {t("page.tasks.workspace_staff_loading")}
                  </div>
                ) : staffAssigneeOptions.length === 0 ? (
                  <div style={{ padding: 16, borderRadius: 12, border: "1px solid rgba(28,25,23,0.06)", background: "#fafaf9", textAlign: "center" }}>
                    <p style={{ fontSize: 13, color: "#a8a29e", margin: 0 }}>{t("page.tasks.no_staff_members_available")}</p>
                    <p style={{ fontSize: 11, color: "#d6d3d1", margin: "4px 0 0" }}>
                      {formWorkspace ? t("page.tasks.add_staff_to_your_workspace_to_assign_tasks") : t("page.tasks.add_people_to_your_team_to_assign_tasks")}
                    </p>
                  </div>
                ) : (
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 8 }}>
                    {staffAssigneeOptions.map((option) => {
                      const isActive = formSelectedStaffUserId === option.id;
                      return (
                        <button
                          key={option.id}
                          type="button"
                          disabled={option.disabled}
                          onClick={() => setFormSelectedStaffUserId(option.id)}
                          style={{
                            display: "flex", alignItems: "center", gap: 10, padding: 12, borderRadius: 12,
                            border: isActive ? "2px solid #4f7d75" : "1px solid #e7e5e4",
                            background: isActive ? "rgba(79,125,117,0.04)" : "#ffffff",
                            cursor: option.disabled ? "not-allowed" : "pointer",
                            textAlign: "left" as const,
                            opacity: option.disabled ? 0.58 : 1,
                            transition: "all 0.2s",
                          }}
                        >
                          <UserAvatar name={option.name} avatarUrl={option.avatarUrl} size={32} />
                          <div style={{ minWidth: 0, flex: 1 }}>
                            <p style={{ fontSize: 13, fontWeight: 700, color: "#292524", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{option.name}</p>
                            {option.meta && <p style={{ fontSize: 11, color: "#a8a29e", margin: "2px 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{option.meta}</p>}
                            {option.disabled && (
                              <p style={{ fontSize: 11, color: "#936027", margin: "4px 0 0" }}>
                                {option.disabledReason || t("page.tasks.staff_unavailable")}
                              </p>
                            )}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            <div style={{ padding: 14, borderRadius: 14, border: "1px solid rgba(251,146,60,0.28)", background: "rgba(249,244,236,0.55)", display: "flex", flexDirection: "column", gap: 10 }}>
              <div>
                <div style={{ fontSize: 13, fontWeight: 800, color: "#7c4a2e" }}>{t("page.tasks.runtime_requirements")}</div>
                <p style={{ fontSize: 12, color: "#7c4a2e", margin: "3px 0 0", lineHeight: 1.5 }}>
                  {t("page.tasks.task_specific_instructions_and_guardrails_these")}
                </p>
              </div>
              <Textarea
                rows={3}
                label={t("page.tasks.task_specific_requirement")}
                value={formRuntimeRequirement}
                onChange={(e) => setFormRuntimeRequirement(e.target.value)}
                placeholder={t("page.tasks.example_post")}
              />
              <Input
                label={t("page.tasks.required_knowledge_refs")}
                value={formRequiredRefs}
                onChange={(e) => setFormRequiredRefs(e.target.value)}
                placeholder={t("page.tasks.optional_doc_group_ids_comma_separated")}
              />
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                <Button variant="outline" size="sm" onClick={() => setFormRuntimeRequirement(t("page.tasks.quick_runtime_social_approval_text"))}>
                  {t("page.tasks.social_post_approval")}
                </Button>
                <Button variant="outline" size="sm" onClick={() => setFormRuntimeRequirement(t("page.tasks.quick_runtime_add_files_only_text"))}>
                  {t("page.task_detail.runtime.quick_add_files_only")}
                </Button>
                <Button variant="outline" size="sm" onClick={() => setFormRuntimeRequirement(t("page.tasks.quick_runtime_draft_only_text"))}>
                  {t("page.task_detail.runtime.quick_draft_only")}
                </Button>
              </div>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
