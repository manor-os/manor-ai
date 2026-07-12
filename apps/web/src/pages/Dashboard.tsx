import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import type { Task, Workspace } from "../lib/types";
import { currentZonedHour, formatTodayFull, isDeadlineOverdue, relativeTime } from "../lib/format";
import TrendChart from "../components/ui/TrendChart";
import WorkspaceIconTile from "../components/ui/WorkspaceIcon";
import { useWorkspaceFilter } from "../stores/workspace";
import { useAuthStore } from "../stores/auth";

/* ── helpers ────────────────────────────────────────────── */

function fullDate(): string {
  return formatTodayFull();
}

function greetingWord(): string {
  const h = currentZonedHour();
  if (h < 12) return t("page.dashboard.good_morning");
  if (h < 18) return t("page.dashboard.good_afternoon");
  return t("page.dashboard.good_evening");
}

function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton rounded-xl ${className}`} />;
}

/* ── KPI card color presets ─────────────────────────────── */

const KPI_THEMES = {
  green: { value: "#2f7550", bg: "#f4f9f5" },
  red: { value: "#bd4a43", bg: "#fbf2f1" },
  blue: { value: "#4d6fa8", bg: "#f5f7fb" },
  gray: { value: "#57534e", bg: "#fbfaf8" },
} as const;

type KpiColor = keyof typeof KPI_THEMES;

interface KpiDef {
  value: string | number;
  label: string;
  color: KpiColor;
}

/* ── timeline dot colors ───────────────────────────────── */
const TIMELINE_COLORS = [
  "#2f7550",
  "#4d6fa8",
  "#d3873f",
  "#6f5f9b",
  "#2f7550",
  "#4d6fa8",
  "#d3873f",
  "#6f5f9b",
];

/* ── main component ─────────────────────────────────────── */

export default function Dashboard() {
  const navigate = useNavigate();
  const storeUser = useAuthStore((s) => s.user);
  const stored = !storeUser ? localStorage.getItem("manor_user") : null;
  const user = storeUser || (stored ? JSON.parse(stored) : null);
  const greetingName =
    user?.display_name ||
    [user?.first_name, user?.last_name].filter(Boolean).join(" ") ||
    user?.first_name ||
    "";
  const wsId = useWorkspaceFilter((s) => s.activeWorkspaceId);
  const wsFilter = wsId !== "all" ? wsId : undefined;

  const { data: stats, isLoading: statsLoading } = useQuery({
    queryKey: ["dashboard-stats", wsFilter],
    queryFn: () => api.dashboard.stats(wsFilter),
  });

  const { data: taskTrends, isLoading: _trendsLoading } = useQuery({
    queryKey: ["dashboard-task-trends", wsFilter],
    queryFn: () => api.dashboard.taskTrends(14, wsFilter),
  });

  const { data: recentActivity, isLoading: activityLoading } = useQuery({
    queryKey: ["dashboard-recent-activity", wsFilter],
    queryFn: () => api.dashboard.recentActivity(100, wsFilter),
  });

  const { data: activeGoals } = useQuery({
    queryKey: ["dashboard-active-goals", wsFilter],
    queryFn: () => api.dashboard.activeGoals(5, wsFilter),
  });

  const { data: workspaces } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
  });

  const { data: usageData } = useQuery({
    queryKey: ["dashboard-usage"],
    queryFn: () => api.usage.summary(30),
  });

  /* Derived metrics — API returns nested: stats.tasks.by_status.completed, etc. */
  const tasksByStatus = stats?.tasks?.by_status ?? {};
  const tasksCompleted = (tasksByStatus.completed as number) ?? 0;
  const tasksPending = (tasksByStatus.pending as number) ?? 0;
  const tasksProposed = (tasksByStatus.proposed as number) ?? 0;
  const tasksInProgress = (tasksByStatus.in_progress as number) ?? 0;
  const tasksFailed = (tasksByStatus.failed as number) ?? 0;
  const tasksWaiting = (tasksByStatus.waiting_on_customer as number) ?? 0;
  const totalTasks = (stats?.tasks?.total as number) ?? 0;
  const tasksOverdue = (stats?.tasks?.overdue as number) ?? 0;
  const totalDocs = (stats?.documents?.total as number) ?? 0;
  const agentsSubscribed = (stats?.agents?.subscribed as number) ?? 0;
  const timeSavedMin = tasksCompleted ? Math.round(tasksCompleted * 12) : 0;

  const hasTaskAttention =
    tasksWaiting > 0 || tasksProposed > 0 || tasksOverdue > 0 || tasksFailed > 0;
  const { data: attentionTargets } = useQuery({
    queryKey: [
      "dashboard-attention-targets",
      wsFilter,
      tasksWaiting,
      tasksProposed,
      tasksOverdue,
      tasksFailed,
    ],
    enabled: hasTaskAttention,
    staleTime: 30_000,
    queryFn: async () => {
      const empty = { items: [] as Task[], total: 0 };
      const [waiting, proposed, failed, openTasks] = await Promise.all([
        tasksWaiting > 0
          ? api.tasks.list({ status: "waiting_on_customer", limit: 2, workspace_id: wsFilter })
          : Promise.resolve(empty),
        tasksProposed > 0
          ? api.tasks.list({ status: "proposed", limit: 2, workspace_id: wsFilter })
          : Promise.resolve(empty),
        tasksFailed > 0
          ? api.tasks.list({ status: "failed", limit: 2, workspace_id: wsFilter })
          : Promise.resolve(empty),
        tasksOverdue > 0
          ? api.tasks.list({ limit: 200, workspace_id: wsFilter })
          : Promise.resolve(empty),
      ]);
      const overdue = (openTasks.items ?? [])
        .filter((task) => isDeadlineOverdue(task.deadline, task.status))
        .slice(0, 2);
      return {
        waiting: waiting.items ?? [],
        proposed: proposed.items ?? [],
        failed: failed.items ?? [],
        overdue,
      };
    },
  });

  /* Workspace name for header */
  const selectedWs = wsFilter
    ? (workspaces ?? []).find((w: any) => w.id === wsFilter)
    : null;

  /* While-you-were-away: recent visible task activity */
  const sleepItems = (recentActivity ?? []) as Record<string, any>[];
  const sleepIntakeItems = sleepItems.filter(
    (i) => i.action === "created" || i.action === "proposed",
  );
  const sleepCompletedCount = sleepItems.filter(
    (i) => i.action === "completed",
  ).length;
  const sleepFailedCount = sleepItems.filter(
    (i) => i.action === "failed",
  ).length;
  const sleepNeedsInput = sleepItems.filter(
    (i) => i.action === "waiting_on_customer",
  ).length;

  const latestWorkspaces = (() => {
    const seen = new Set<string>();
    return ((workspaces ?? []) as Workspace[])
      .filter((ws) => {
        if (seen.has(ws.id)) return false;
        seen.add(ws.id);
        return true;
      })
      .slice(0, 4);
  })();

  /* Items that actually need user action — derived from stats, not recentActivity */
  const attentionItems: {
    label: string;
    tag: "warning" | "info";
    urgent?: boolean;
    link: string;
  }[] = [];
  const oneTaskLink = (
    key: "waiting" | "proposed" | "overdue" | "failed",
    fallback: string,
  ) => {
    const tasks = attentionTargets?.[key] ?? [];
    return tasks.length === 1 ? `/tasks/${tasks[0].id}` : fallback;
  };
  if (tasksWaiting > 0)
    attentionItems.push({
      label: `${tasksWaiting} task${tasksWaiting > 1 ? "s" : ""} waiting for your input`,
      tag: "warning",
      urgent: true,
      link: oneTaskLink("waiting", "/tasks?status=waiting_on_customer"),
    });
  if (tasksProposed > 0)
    attentionItems.push({
      label: `${tasksProposed} proposed task${tasksProposed > 1 ? "s" : ""} to review`,
      tag: "warning",
      link: oneTaskLink("proposed", "/tasks?status=proposed"),
    });
  if (tasksOverdue > 0)
    attentionItems.push({
      label: `${tasksOverdue} overdue task${tasksOverdue > 1 ? "s" : ""}`,
      tag: "warning",
      urgent: true,
      link: oneTaskLink("overdue", "/tasks?status=overdue"),
    });
  if (tasksFailed > 0)
    attentionItems.push({
      label: `${tasksFailed} task${tasksFailed > 1 ? "s" : ""} failed — review and retry`,
      tag: "warning",
      link: oneTaskLink("failed", "/tasks?status=failed"),
    });
  const pausedWs = latestWorkspaces.filter(
    (w: any) => w.status === "paused",
  ).length;
  if (pausedWs > 0)
    attentionItems.push({
      label: `${pausedWs} workspace${pausedWs > 1 ? "s" : ""} paused`,
      tag: "info",
      link: "/workspaces",
    });
  const actionCount = attentionItems.length;

  /* KPI cards */
  const kpis: KpiDef[] = [
    { value: tasksCompleted, label: t("status.completed"), color: "green" },
    { value: tasksInProgress, label: t("status.in_progress"), color: "blue" },
    { value: tasksPending, label: t("status.pending"), color: "red" },
    { value: agentsSubscribed, label: t("nav.agents"), color: "gray" },
  ];

  /* Task trend summary for brief prose */
  const trendTotal = (taskTrends ?? []).reduce(
    (sum: number, d: any) =>
      sum + ((d.created ?? d.count ?? d.value ?? 0) as number),
    0,
  );

  /* ── Status badge mapping for workspaces ── */
  const WS_PILL: Record<string, { label: string; bg: string; color: string }> =
    {
      active: {
        label: t("page.dashboard.active"),
        bg: "#e4efe8",
        color: "#3d7351",
      },
      running: {
        label: t("page.dashboard.active"),
        bg: "#e4efe8",
        color: "#3d7351",
      },
      paused: {
        label: t("page.dashboard.attention"),
        bg: "#f3ecd6",
        color: "#76502c",
      },
      archived: {
        label: t("page.dashboard.quiet"),
        bg: "#f5f5f4",
        color: "#78716c",
      },
      completed: {
        label: t("page.dashboard.quiet"),
        bg: "#f5f5f4",
        color: "#78716c",
      },
    };

  const activityTone = (action?: string) => {
    switch (action) {
      case "completed":
        return {
          label: t("page.dashboard.completed"),
          bg: "#e4efe8",
          color: "#3d7351",
          dot: "#2f7550",
        };
      case "in_progress":
        return {
          label: t("page.dashboard.running_now"),
          bg: "#e3e9f1",
          color: "#3f57a0",
          dot: "#4d6fa8",
        };
      case "waiting_on_customer":
        return {
          label: t("page.dashboard.input_needed_short"),
          bg: "#f3e8ff",
          color: "#6443a0",
          dot: "#6f4ba8",
        };
      case "failed":
        return {
          label: t("page.dashboard.failed"),
          bg: "#f1dddb",
          color: "#c14a44",
          dot: "#d65f59",
        };
      case "proposed":
        return {
          label: t("page.dashboard.review"),
          bg: "#e5eeeb",
          color: "#436b65",
          dot: "#4f7169",
        };
      case "created":
      default:
        return {
          label: t("page.dashboard.created"),
          bg: "#e5eeeb",
          color: "#436b65",
          dot: "#4f7169",
        };
    }
  };

  return (
    <div
      className="dashboard-page"
      style={{
        display: "grid",
        gridTemplateRows: "auto auto auto minmax(0, 1fr)",
        minHeight: "100%",
        height: "100%",
        boxSizing: "border-box",
        padding: "12px 24px",
        gap: 10,
        overflow: "hidden",
      }}
    >
      {/* ── Greeting Header ──────────────────────────── */}
      <div>
        <h1
          className="dashboard-title"
          style={{
            fontSize: 26,
            fontWeight: 800,
            color: "#292524",
            lineHeight: 1.12,
            margin: 0,
          }}
        >
          {greetingWord()}
          {greetingName ? `, ${greetingName}` : ""}{" "}
          <span role="img" aria-label={t("page.dashboard.wave")}>
            {t("page.dashboard.and_x1f44b")}</span>
        </h1>
        {selectedWs && (
          <p
            className="dashboard-selected-workspace"
            style={{
              fontSize: 14,
              fontWeight: 600,
              color: "#436b65",
              marginTop: 4,
              marginBottom: 0,
            }}
          >
            {(selectedWs as any).name}
          </p>
        )}
        <p
          className="dashboard-subtitle"
          style={{
            fontSize: 13,
            fontWeight: 400,
            color: "#a8a29e",
            margin: "4px 0 0",
          }}
        >
          {actionCount > 0
            ? `${actionCount} ${actionCount > 1 ? t("page.dashboard.actions") : t("page.dashboard.action")} ${t("page.dashboard.need_attention")}`
            : t("page.dashboard.all_clear")}
        </p>
      </div>

      {/* ── Daily Brief Panel ────────────────────────── */}
      <div
        className="dashboard-brief-card"
        style={{
          background: "rgba(255,255,255,0.72)",
          backdropFilter: "blur(16px) saturate(1.06)",
          WebkitBackdropFilter: "blur(16px) saturate(1.06)",
          borderRadius: 20,
          border: "1px solid rgba(28,25,23,0.065)",
          borderLeft: "3px solid #4f7169",
          boxShadow: "inset 0 1px 0 rgba(255,255,255,0.9), 0 1px 2px rgba(28,25,23,0.014)",
          padding: "12px 18px",
        }}
      >
        {/* Header row */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            marginBottom: 12,
          }}
        >
          {/* Lightning bolt icon */}
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: 8,
              background: "#f3ecd6",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="#b27c34"
              strokeWidth={2.5}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
            </svg>
          </div>
          <span className="dashboard-card-title" style={{ fontSize: 15, fontWeight: 800, color: "#292524" }}>
            {t("page.dashboard.daily_brief")}
          </span>
          <span className="dashboard-muted" style={{ fontSize: 12, color: "#a8a29e", marginLeft: 4 }}>
            {fullDate()}
          </span>
          <div style={{ marginLeft: "auto" }}>
            {actionCount > 0 ? (
              <span
                className="dashboard-attention-pill"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "3px 10px",
                  borderRadius: 99,
                  background: "#f3ecd6",
                  color: "#76502c",
                  fontSize: 11,
                  fontWeight: 700,
                }}
              >
                {actionCount}{" "}
                {actionCount > 1
                  ? t("page.dashboard.actions")
                  : t("page.dashboard.action")}{" "}
                {t("page.dashboard.needed")}
              </span>
            ) : (
              <span
                className="dashboard-clear-pill"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "3px 10px",
                  borderRadius: 99,
                  background: "#e4efe8",
                  color: "#3d7351",
                  fontSize: 11,
                  fontWeight: 700,
                }}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: "#2f7550",
                    animation: "pulse 2s cubic-bezier(0.4,0,0.6,1) infinite",
                  }}
                />
                {t("page.dashboard.all_clear")}
              </span>
            )}
          </div>
        </div>

        {/* Two-column body */}
        <div
          style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 22 }}
        >
          {/* Left — What AI Did Yesterday */}
          <div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                marginBottom: 7,
              }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#78716c"
                strokeWidth={2}
              >
                <circle cx="12" cy="12" r="10" />
                <path d="M12 6v6l4 2" />
              </svg>
              <span
                className="dashboard-eyebrow"
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  color: "#78716c",
                }}
              >
                {t("page.dashboard.what_ai_did_yesterday")}
              </span>
            </div>

            {statsLoading ? (
              <Skeleton className="h-16 mb-4" />
            ) : (
              <p
                className="dashboard-copy"
                style={{
                  fontSize: 13,
                  fontWeight: 500,
                  lineHeight: 1.42,
                  color: "#57534e",
                  margin: "0 0 10px 0",
                }}
              >
                {t("page.dashboard.manor_ai_completed")}{" "}
                <strong>{tasksCompleted}</strong>{" "}
                {tasksCompleted !== 1
                  ? t("page.dashboard.tasks")
                  : t("page.dashboard.task")}
                , {t("page.dashboard.processed")} <strong>{totalDocs}</strong>{" "}
                {totalDocs !== 1
                  ? t("page.dashboard.documents")
                  : t("page.dashboard.document")}
                , {t("page.dashboard.handled")} <strong>{trendTotal}</strong>{" "}
                {t("page.dashboard.tasks_past_14_days")}{" "}
                {t("page.dashboard.estimated")} <strong>{timeSavedMin}{t("page.dashboard.m")}</strong>{" "}
                {t("page.dashboard.time_saved_suffix")}
              </p>
            )}

            {/* KPI cards row */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 82px), 1fr))",
                gap: 8,
              }}
            >
              {kpis.map((kpi, i) => {
                const theme = KPI_THEMES[kpi.color];
                return (
                  <div
                    key={i}
                    className={`dashboard-kpi-card dashboard-kpi-card--${kpi.color}`}
                    style={{
                      background: theme.bg,
                      borderRadius: 10,
                      padding: "8px 10px",
                      textAlign: "center",
                      minWidth: 0,
                    }}
                  >
                    <div
                      className="dashboard-kpi-value"
                      style={{
                        fontSize: 18,
                        fontWeight: 800,
                        color: theme.value,
                        lineHeight: 1.2,
                      }}
                    >
                      {kpi.value}
                    </div>
                    <div
                      className="dashboard-kpi-label"
                      style={{
                        fontSize: 9,
                        fontWeight: 600,
                        textTransform: "uppercase",
                        color: "#a8a29e",
                        marginTop: 2,
                      }}
                    >
                      {kpi.label}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Right — Requires Your Attention */}
          <div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                marginBottom: 7,
              }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#78716c"
                strokeWidth={2}
              >
                <path d="M12 9v4m0 4h.01M10.29 3.86l-8.58 14.85A1 1 0 002.57 20h18.86a1 1 0 00.86-1.29L13.71 3.86a1 1 0 00-1.42 0z" />
              </svg>
              <span
                className="dashboard-eyebrow"
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  color: "#78716c",
                }}
              >
                {t("page.dashboard.requires_attention")}
              </span>
              {attentionItems.length > 0 && (
                <span
                  style={{
                    fontSize: 9,
                    fontWeight: 700,
                    color: "#fff",
                    background: "#4f7169",
                    borderRadius: 99,
                    padding: "1px 7px",
                    marginLeft: 4,
                  }}
                >
                  {attentionItems.length}
                </span>
              )}
            </div>

            {attentionItems.length === 0 ? (
              <p style={{ fontSize: 13, color: "#a8a29e", margin: 0 }}>
                {t("page.dashboard.no_actions_needed")}
              </p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {attentionItems.map((item, i) => {
                  const isUrgent = item.urgent;
                  const tagColors =
                    item.tag === "warning"
                      ? { bg: "#f3ecd6", color: "#76502c" }
                      : { bg: "#e3e9f1", color: "#1e40af" };
                  return (
                    <button
                      type="button"
                      key={item.label}
                      onClick={() => navigate(item.link)}
                      className={`dashboard-attention-row ${isUrgent ? "is-urgent" : ""}`}
                      style={{
                        width: "100%",
                        padding: "7px 10px",
                        borderRadius: 10,
                        border: isUrgent
                          ? "1px solid #ecdca4"
                          : "1px solid #f5f5f4",
                        background: isUrgent ? "#fffdf5" : "#fafbfc",
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                        cursor: "pointer",
                        textAlign: "left",
                      }}
                    >
                      <span
                        className="dashboard-attention-label"
                        style={{
                          flex: 1,
                          fontSize: 13,
                          fontWeight: 500,
                          color: "#44403c",
                        }}
                      >
                        {item.label}
                      </span>
                      <span
                        className={`dashboard-attention-tag dashboard-attention-tag--${item.tag}`}
                        style={{
                          fontSize: 10,
                          fontWeight: 600,
                          padding: "2px 8px",
                          borderRadius: 6,
                          background: tagColors.bg,
                          color: tagColors.color,
                        }}
                      >
                        {item.tag === "warning"
                          ? t("page.dashboard.warning")
                          : t("page.dashboard.info")}
                      </span>
                      <span
                        className="dashboard-attention-link"
                        style={{
                          fontSize: 11,
                          fontWeight: 600,
                          color: "#3f665e",
                          background: "transparent",
                          border: "none",
                          cursor: "pointer",
                          padding: 0,
                        }}
                      >
                        {t("page.dashboard.review_arrow")}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Metrics Row ──────────────────────────────── */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
          gap: 10,
        }}
      >
        {/* Time Saved */}
        <MetricCard
          iconBg="#e4efe8"
          iconColor="#2f7550"
          icon={
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
            >
              <circle cx="12" cy="12" r="10" />
              <path d="M12 6v6l4 2" />
            </svg>
          }
          label={t("page.dashboard.time_saved")}
          value={timeSavedMin ? String(timeSavedMin) : "--"}
          unit="min"
          sub={
            tasksCompleted > 0
              ? `${t("page.dashboard.based_on")} ${tasksCompleted} ${t("page.dashboard.tasks")}`
              : undefined
          }
          trendUp
        />
        {/* Total Tasks */}
        <MetricCard
          iconBg="#f3f0fb"
          iconColor="#6f5f9b"
          icon={
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
            </svg>
          }
          label={t("page.dashboard.total_tasks")}
          value={String(totalTasks)}
          unit={t("page.dashboard.total")}
          sub={
            tasksInProgress > 0
              ? `${tasksInProgress} ${t("page.dashboard.in_progress")}`
              : undefined
          }
        />
        {/* Tasks Running */}
        <MetricCard
          iconBg="#e3ebe8"
          iconColor="#3f665e"
          icon={
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2" />
              <rect x="9" y="3" width="6" height="4" rx="1" />
              <path d="M9 14l2 2 4-4" />
            </svg>
          }
          label={t("page.dashboard.tasks_running")}
          value={String(tasksInProgress)}
          unit={t("page.dashboard.active")}
          sub={
            tasksPending > 0
              ? `${tasksPending} ${t("page.dashboard.pending")}`
              : undefined
          }
          trendWarn={tasksPending > 5}
        />
      </div>

      {/* ── Activity and Context ── */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.18fr) minmax(340px, 0.82fr)",
          gap: 12,
          alignItems: "stretch",
          height: "100%",
          minHeight: 0,
        }}
      >
        {/* Recent activity */}
        <div
          className="dashboard-activity-card"
          style={{
            background: "rgba(255,255,255,0.72)",
            backdropFilter: "blur(16px) saturate(1.06)",
            WebkitBackdropFilter: "blur(16px) saturate(1.06)",
            borderRadius: 16,
            border: "1px solid rgba(28,25,23,0.065)",
            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.9), 0 1px 2px rgba(28,25,23,0.014)",
            padding: "14px 18px",
            minWidth: 0,
            minHeight: 0,
            height: "100%",
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "flex-start",
              justifyContent: "space-between",
              gap: 16,
              marginBottom: 10,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 12,
                minWidth: 0,
              }}
            >
              <div
                style={{
                  width: 34,
                  height: 34,
                  borderRadius: 10,
                  background: "linear-gradient(135deg, #f2f6f5, #ece9f5)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                }}
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#436b65"
                  strokeWidth={2}
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M21 12.79A9 9 0 1111.21 3a7 7 0 009.79 9.79z"
                  />
                </svg>
              </div>
              <div style={{ minWidth: 0 }}>
                <h2
                  className="dashboard-card-title"
                  style={{
                    fontSize: 16,
                    fontWeight: 800,
                    color: "#292524",
                    margin: 0,
                  }}
                >
                  {t("page.dashboard.while_away")}
                </h2>
                {sleepItems.length > 0 && (
                  <p
                    className="dashboard-copy dashboard-copy--muted"
                    style={{
                      fontSize: 13,
                      color: "#78716c",
                      margin: "3px 0 0 0",
                      lineHeight: 1.35,
                    }}
                  >
                    {t("page.chat_history.manor_ai")}{" "}
                    {sleepCompletedCount > 0
                      ? `${t("page.dashboard.completed")} ${sleepCompletedCount} ${sleepCompletedCount > 1 ? t("page.dashboard.tasks") : t("page.dashboard.task")}`
                      : t("page.dashboard.worked_on_tasks")}
                    {sleepFailedCount > 0
                      ? `, ${sleepFailedCount} ${t("page.dashboard.need_attention")}`
                      : ""}
                    {sleepNeedsInput > 0
                      ? `, ${sleepNeedsInput} ${t("page.dashboard.waiting_for_you")}`
                      : ""}
                    {` ${t("page.dashboard.while_away_suffix")}`}
                  </p>
                )}
              </div>
            </div>
            {sleepItems.length > 0 && (
              <span
                className="dashboard-count-pill"
                style={{
                  flexShrink: 0,
                  fontSize: 11,
                  fontWeight: 700,
                  color: "#436b65",
                  background: "#e5eeeb",
                  borderRadius: 999,
                  padding: "4px 10px",
                }}
              >
                {sleepItems.length}{" "}
                {sleepItems.length > 1
                  ? t("page.dashboard.tasks")
                  : t("page.dashboard.task")}
              </span>
            )}
          </div>

          {activityLoading ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: 0 }}>
              {[1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-12" />
              ))}
            </div>
          ) : sleepItems.length > 0 ? (
            <div
              className="dashboard-activity-list"
              style={{
                border: "1px solid rgba(28,25,23,0.06)",
                borderRadius: 14,
                flex: "1 1 auto",
                minHeight: 0,
                overflowY: "auto",
                overflowX: "hidden",
                background: "rgba(250,250,249,0.45)",
              }}
            >
              {sleepItems.map((item, idx) => {
                const tone = activityTone(item.action);
                const actionLabel =
                  item.action === "waiting_on_customer" ||
                  item.action === "proposed"
                    ? t("page.dashboard.review")
                    : item.action === "failed"
                      ? t("page.dashboard.retry")
                      : "";
                return (
                  <div
                    key={idx}
                    onClick={() =>
                      item.task_id && navigate(`/tasks/${item.task_id}`)
                    }
                    className="dashboard-activity-row"
                    style={{
                      display: "grid",
                      gridTemplateColumns: "auto minmax(0, 1fr) auto",
                      alignItems: "center",
                      gap: 10,
                      padding: "9px 12px",
                      borderTop:
                        idx > 0 ? "1px solid rgba(231,229,228,0.9)" : "none",
                      background: "#fff",
                      cursor: item.task_id ? "pointer" : "default",
                    }}
                  >
                    <span
                      className="dashboard-status-pill"
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        borderRadius: 999,
                        padding: "4px 9px",
                        background: tone.bg,
                        color: tone.color,
                        fontSize: 11,
                        fontWeight: 700,
                        whiteSpace: "nowrap",
                      }}
                    >
                      <span
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: "50%",
                          background: tone.dot,
                        }}
                      />
                      {tone.label}
                    </span>
                    <div style={{ minWidth: 0 }}>
                      <div
                        className="dashboard-activity-title"
                        style={{
                          fontSize: 13,
                          fontWeight: 700,
                          color: "#292524",
                          lineHeight: 1.25,
                          overflowWrap: "anywhere",
                        }}
                      >
                        {item.name}
                      </div>
                      {item.description && (
                        <div
                          className="dashboard-activity-description"
                          style={{
                            fontSize: 12,
                            color: "#78716c",
                            marginTop: 2,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {item.description}
                        </div>
                      )}
                    </div>
                    {actionLabel ? (
                      <button
                        type="button"
                        className="dashboard-row-action"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (item.task_id) navigate(`/tasks/${item.task_id}`);
                        }}
                        style={{
                          border: "none",
                          borderRadius: 8,
                          background: tone.bg,
                          color: tone.color,
                          fontSize: 12,
                          fontWeight: 700,
                          padding: "5px 10px",
                          cursor: item.task_id ? "pointer" : "default",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {actionLabel}
                      </button>
                    ) : (
                      <span
                        className="dashboard-activity-time"
                        style={{
                          fontSize: 11,
                          color: "#a8a29e",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {item.timestamp ? relativeTime(item.timestamp) : ""}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div
              style={{
                textAlign: "center",
                padding: "24px 0",
                border: "1px dashed rgba(28,25,23,0.06)",
                borderRadius: 14,
                background: "rgba(250,250,249,0.55)",
              }}
            >
              <div style={{ fontSize: 28, marginBottom: 6 }}>{t("page.dashboard.and_x2728")}</div>
              <p style={{ fontSize: 13, color: "#a8a29e", margin: 0 }}>
                {t("page.dashboard.all_quiet")}
              </p>
            </div>
          )}
        </div>

        {/* Right rail */}
        <div
          style={{
            display: "grid",
            gridTemplateRows: "minmax(0, 1fr) auto",
            gap: 12,
            minWidth: 0,
            minHeight: 0,
            height: "100%",
            overflow: "hidden",
          }}
        >
          {/* Workspaces */}
          <div
            className="dashboard-workspaces-card"
            style={{
              background: "rgba(255,255,255,0.72)",
              backdropFilter: "blur(16px) saturate(1.06)",
              WebkitBackdropFilter: "blur(16px) saturate(1.06)",
              borderRadius: 16,
              border: "1px solid rgba(28,25,23,0.065)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.9), 0 1px 2px rgba(28,25,23,0.014)",
              padding: "14px 18px",
              minHeight: 0,
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            }}
          >
            <h2
              className="dashboard-card-title"
              style={{
                fontSize: 16,
                fontWeight: 800,
                color: "#292524",
                margin: "0 0 10px 0",
              }}
            >
              {t("nav.workspaces")}
            </h2>
            {latestWorkspaces.length > 0 ? (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 7,
                  flex: "1 1 auto",
                  minHeight: 0,
                  overflowY: "auto",
                  overflowX: "hidden",
                  paddingRight: 2,
                }}
              >
                {latestWorkspaces.map((ws: any) => {
                  const pill = WS_PILL[ws.status] ?? {
                    label: ws.status,
                    bg: "#f5f5f4",
                    color: "#78716c",
                  };
                  const st = ws.stats || {};
                  const hasIssue =
                    st.tasks_active > 3 || ws.status === "paused";
                  return (
                    <div
                      key={ws.id}
                      onClick={() => navigate(`/workspaces/${ws.id}`)}
                      className={`dashboard-workspace-row ${hasIssue ? "has-issue" : ""}`}
                      style={{
                        border: hasIssue
                          ? "1px solid rgba(207,155,68,0.3)"
                          : "1px solid #f5f5f4",
                        borderRadius: 12,
                        padding: "9px 12px",
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                        cursor: "pointer",
                        transition: "background 0.15s",
                        background: hasIssue
                          ? "rgba(243,236,214,0.15)"
                          : "transparent",
                      }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.background =
                          "rgba(242,246,245,0.5)";
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.background = hasIssue
                          ? "rgba(243,236,214,0.15)"
                          : "transparent";
                      }}
                    >
                      <WorkspaceIconTile
                        workspace={ws}
                        size={28}
                        iconSize={14}
                        style={{ borderRadius: 8, flexShrink: 0 }}
                      />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div
                          className="dashboard-workspace-title"
                          style={{
                            fontSize: 13,
                            fontWeight: 600,
                            color: "#292524",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {ws.name}
                        </div>
                        <div style={{ display: "flex", gap: 6, marginTop: 2 }}>
                          {st.tasks_active > 0 && (
                            <span style={{ fontSize: 10, color: "#78716c" }}>
                              {st.tasks_active} {t("page.dashboard.active")}
                            </span>
                          )}
                          {st.tasks > 0 && (
                            <span style={{ fontSize: 10, color: "#a8a29e" }}>
                              {st.tasks} {t("page.dashboard.total")}
                            </span>
                          )}
                          {st.agents > 0 && (
                            <span style={{ fontSize: 10, color: "#a8a29e" }}>
                              {st.agents} {t("page.dashboard.agents")}
                            </span>
                          )}
                        </div>
                      </div>
                      <span
                        className="dashboard-workspace-status"
                        style={{
                          fontSize: 10,
                          fontWeight: 600,
                          padding: "2px 8px",
                          borderRadius: 6,
                          background: pill.bg,
                          color: pill.color,
                        }}
                      >
                        {pill.label}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: 14,
                  borderRadius: 14,
                  border: "1px dashed rgba(28,25,23,0.06)",
                  background: "rgba(250,250,249,0.55)",
                }}
              >
                <div
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: 10,
                    background: "#f5f5f4",
                    color: "#78716c",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 12,
                    fontWeight: 800,
                    flexShrink: 0,
                  }}
                >
                  {t("page.dashboard.w")}
                </div>
                <p style={{ fontSize: 13, color: "#a8a29e", margin: 0 }}>
                  {t("page.dashboard.no_workspaces")}
                </p>
              </div>
            )}
          </div>

          {(taskTrends ?? []).length > 0 && (
            <div
              className="dashboard-trend-card"
              style={{
                background: "rgba(255,255,255,0.72)",
                backdropFilter: "blur(16px) saturate(1.06)",
                WebkitBackdropFilter: "blur(16px) saturate(1.06)",
                borderRadius: 16,
                border: "1px solid rgba(28,25,23,0.065)",
                boxShadow: "inset 0 1px 0 rgba(255,255,255,0.9), 0 1px 2px rgba(28,25,23,0.014)",
                padding: "12px 16px 10px",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                  marginBottom: 6,
                }}
              >
                <span
                  className="dashboard-card-title"
                  style={{ fontSize: 15, fontWeight: 800, color: "#292524" }}
                >
                  {t("page.dashboard.trend_14_day")}
                </span>
                <div style={{ display: "flex", gap: 10, flexShrink: 0 }}>
                  <span className="dashboard-legend dashboard-legend--created" style={{ fontSize: 10, color: "#4f7169" }}>
                    ● {t("page.dashboard.created")}
                  </span>
                  <span className="dashboard-legend dashboard-legend--completed" style={{ fontSize: 10, color: "#4d6fa8" }}>
                    ● {t("page.dashboard.completed")}
                  </span>
                </div>
              </div>
              <TrendChart
                data={((taskTrends as Record<string, any>[]) ?? []).map(
                  (d: Record<string, any>) => ({
                    label: d.date
                      ? new Date(d.date as string).toLocaleDateString("en-US", {
                          month: "short",
                          day: "numeric",
                        })
                      : "",
                    value: (d.created ?? d.count ?? 0) as number,
                    value2: (d.completed ?? 0) as number | undefined,
                  }),
                )}
                type="area"
                height={96}
                color="#4f7169"
                color2="#4d6fa8"
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Metric Card sub-component ─────────────────────────── */

function MetricCard({
  iconBg,
  iconColor,
  icon,
  label,
  value,
  unit,
  sub,
  trendUp,
  trendWarn,
}: {
  iconBg: string;
  iconColor: string;
  icon: React.ReactNode;
  label: string;
  value: string;
  unit?: string;
  sub?: string;
  trendUp?: boolean;
  trendWarn?: boolean;
}) {
  return (
    <div
      className="glass-panel card-hover-surface dashboard-metric-card"
      style={{
        padding: "14px 16px",
        cursor: "default",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div
          className="dashboard-metric-icon"
          style={{
            width: 32,
            height: 32,
            borderRadius: 10,
            background: iconBg,
            color: iconColor,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}
        >
          {icon}
        </div>
        <div>
          <div
            className="dashboard-metric-label"
            style={{
              fontSize: 11,
              fontWeight: 500,
              textTransform: "uppercase",
              letterSpacing: "0.03em",
              color: "#78716c",
              marginBottom: 2,
            }}
          >
            {label}
          </div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
            <span
              className="dashboard-metric-value"
              style={{
                fontSize: 24,
                fontWeight: 800,
                color: "#292524",
                lineHeight: 1,
              }}
            >
              {value}
            </span>
            {unit && (
              <span className="dashboard-metric-unit" style={{ fontSize: 14, fontWeight: 400, color: "#a8a29e" }}>
                {unit}
              </span>
            )}
          </div>
          {sub && (
            <div
              className={`dashboard-metric-sub ${trendWarn ? "is-warn" : trendUp ? "is-up" : ""}`}
              style={{
                fontSize: 11,
                color: trendWarn ? "#9a6a2f" : trendUp ? "#2f7550" : "#8f8780",
                marginTop: 1,
              }}
            >
              {sub}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
