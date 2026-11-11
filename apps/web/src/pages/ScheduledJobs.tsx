import { ConfigAddRow, ConfigBlock, ConfigRow } from "../components/ui/GroupedList";
import { agentListQueryOptions, useCatalogScope } from "../lib/catalogQueries";
import { Fragment, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError, type AutomationHealthEntry } from "../lib/api";
import { useToastStore } from "../stores/toast";
import type { Agent, Workspace } from "../lib/types";
import { getPreferredTimeZone, relativeTime } from "../lib/format";
import { MANOR_AGENT_ID, MANOR_AGENT_NAME, isMasterAgent } from "../lib/constants";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import Select from "../components/ui/Select";
import DateTimePicker from "../components/ui/DateTimePicker";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import Toggle from "../components/ui/Toggle";
import StatPill from "../components/ui/StatPill";
import TabSwitcher from "../components/ui/TabSwitcher";
import PageHeader from "../components/ui/PageHeader";
import EmptyState from "../components/ui/EmptyState";
import SmartToolbar from "../components/ui/SmartToolbar";
import { FilterBar, FilterSelect } from "../components/ui/FilterBar";
import Pagination from "../components/ui/Pagination";
import { getLocale, t } from "../lib/i18n";
import { formatUserFacingText } from "../lib/taskDisplay";
import { stringifyJobRunResult, summarizeJobRunResult } from "../lib/jobRunResult";
import { usePreviewFeatureAccess } from "../lib/previewFeatureAccess";
import { displayReadInterval } from "../lib/readRefreshPolicy";
import { usePageVisible } from "../lib/usePageVisible";
import {
  automationRunView, matchesAutomationStatusFilter, workspaceBlocksConfiguredWork,
} from "../lib/automationRunState";
import InlineFileReferenceCard from "../components/InlineFileReferenceCard";
import Button from "../components/ui/Button";
import {
  formatWorkflowValue, resolveWorkflowFinalResult, workflowFinalArtifactRefs,
  workflowFinalHeadline, workflowArtifactLabel, type WorkflowHistoryNode,
  workflowRunStateKey,
} from "../components/workflows/workflowRunDisplay";
import {
  IconEdit, IconTrash, IconClock, IconChevronRight, IconPlay, IconFlow,
} from "../components/icons";
import "./ScheduledJobs.css";

/* ── types ── */
interface ScheduledJob {
  id: string; job_id: string; entity_id: string; name: string; job_type: string;
  workspace_id?: string;
  workspace_status?: string | null;
  /** Server-projected: the Workspace lifecycle stops this job; `enabled` already reflects it. */
  workspace_blocked?: boolean;
  can_manage?: boolean;
  schedule_kind?: string; cron_expr?: string; interval_seconds?: number; every_seconds?: number;
  run_at?: string; payload?: Record<string, any>; payload_message?: string;
  execution_target?: Record<string, any>;
  execution_type?: string;
  agent_id?: string; enabled: boolean; timezone?: string;
  manor_task_id?: string | null;
  last_run_at?: string; last_status?: string; last_error?: string; consecutive_errors: number; created_at?: string;
}
interface WorkflowBinding {
  id: string; workflow_id: string; workspace_id?: string; name?: string;
  trigger_type: string; trigger_config?: Record<string, any>;
  config?: Record<string, any>; variables?: Record<string, any>; enabled: boolean; status: string;
}
interface WorkflowSummary {
  id: string; name: string; description?: string; status: string;
}
interface JobRun {
  id: string; job_id: string; status: string; trigger_type: string;
  duration_ms?: number; error?: string; result?: Record<string, any>; created_at?: string;
  /** status "waiting": the run's own Task needs a person before it can finish. */
  waiting_task_id?: string | null; waiting_reason?: string | null;
}

/* ── constants ── */
const PAGE_SIZE = 10;

type AutomationStatusFilter = "all" | "enabled" | "paused" | "attention";

const AUTOMATION_STATUS_OPTIONS = [
  { key: "all", label: t("page.scheduled_jobs.all_statuses") },
  { key: "enabled", label: t("page.scheduled_jobs.enabled") },
  { key: "paused", label: t("page.scheduled_jobs.paused") },
  { key: "attention", label: t("page.scheduled_jobs.needs_attention") },
];

const DAYS_OF_WEEK = [
  { key: 1, label: t("page.scheduled_jobs.mon") }, { key: 2, label: t("page.scheduled_jobs.tue") }, { key: 3, label: t("page.scheduled_jobs.wed") },
  { key: 4, label: t("page.scheduled_jobs.thu") }, { key: 5, label: t("page.scheduled_jobs.fri") }, { key: 6, label: t("page.scheduled_jobs.sat") }, { key: 0, label: t("page.scheduled_jobs.sun") },
];

const INTERVAL_OPTIONS = [
  { label: t("page.scheduled_jobs.interval_5_min"), s: 300 }, { label: t("page.scheduled_jobs.interval_15_min"), s: 900 }, { label: t("page.scheduled_jobs.interval_30_min"), s: 1800 },
  { label: t("page.scheduled_jobs.interval_1_hour"), s: 3600 }, { label: t("page.scheduled_jobs.interval_2_hours"), s: 7200 }, { label: t("page.scheduled_jobs.interval_4_hours"), s: 14400 },
  { label: t("page.scheduled_jobs.interval_6_hours"), s: 21600 }, { label: t("page.scheduled_jobs.interval_12_hours"), s: 43200 },
];

const TIMEZONE_OPTIONS = [
  "UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "Europe/London", "Europe/Paris", "Europe/Berlin", "Asia/Tokyo", "Asia/Shanghai",
  "Asia/Singapore", "Asia/Kolkata", "Australia/Sydney", "Pacific/Auckland",
];

type AutomationKind = "agent_schedule" | "workflow_schedule" | "workflow_event";

const AUTOMATION_KIND_TABS = [
  { key: "agent_schedule", label: t("ui.schedule_an_agent") },
  { key: "workflow_schedule", label: t("ui.schedule_a_workflow") },
  { key: "workflow_event", label: t("ui.run_workflow_on_event") },
];

const WORKSPACE_EVENT_SUGGESTIONS = [
  { value: "task.approval_decision", label: t("ui.task_approved_or_changes_requested") },
  { value: "task.comment", label: t("ui.task_receives_a_comment") },
  { value: "workspace.channel_attached", label: t("ui.channel_connected") },
  { value: "workspace.channel_updated", label: t("ui.channel_updated") },
  { value: "workspace_work_batch.completed", label: t("ui.work_batch_completed") },
  { value: "workspace_work_batch.stalled", label: t("ui.work_batch_stalled") },
  { value: "workspace_agent.task_created", label: t("ui.workspace_agent_creates_a_task") },
  { value: "workspace_agent.task_runtime_updated", label: t("ui.task_runtime_changes") },
];

function browserTimezone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return tz || "UTC";
  } catch {
    return "UTC";
  }
}

function summarizeAutomationMessage(message?: string | null, maxLength = 138): string {
  const raw = String(message || "").trim();
  if (!raw) return "";

  const descriptionMatch = raw.match(/Automation description:\s*([\s\S]*?)(?:\nComplete the work|\.\s*Complete the work|$)/i);
  const candidate = (descriptionMatch?.[1] || raw)
    .replace(/^Run the workspace automation:\s*/i, "")
    .replace(/\bWorkspace ID:\s*\S+\.?/gi, "")
    .replace(/\bResponsible service:\s*[^.\n]+\.?/gi, "")
    .replace(/\bOriginal trigger\/cadence:\s*[^.\n]+\.?/gi, "")
    .replace(/\bComplete the work[\s\S]*$/i, "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\.\.$/, ".");

  const friendly = formatUserFacingText(candidate);
  if (friendly.length <= maxLength) return friendly;
  return `${friendly.slice(0, Math.max(0, maxLength - 3)).trim()}...`;
}

// Build cron from schedule builder state
function buildCron(freq: string, hour: number, minute: number, days: number[], dayOfMonth: number): string {
  const m = minute; const h = hour;
  if (freq === "hourly") return `${m} * * * *`;
  if (freq === "daily") return `${m} ${h} * * *`;
  if (freq === "weekly") {
    const d = days.length > 0 ? days.join(",") : "1";
    return `${m} ${h} * * ${d}`;
  }
  if (freq === "monthly") return `${m} ${h} ${dayOfMonth} * *`;
  return `${m} ${h} * * *`;
}

// Human-readable from cron
function describeCron(cron: string): string {
  const p = cron.split(" ");
  if (p.length !== 5) return cron;
  const [min, hr, dom, , dow] = p;
  const minute = min.padStart(2, "0");
  if (hr === "*") return t("ui.schedule_hourly", { minute });
  const time = getLocale() === "en"
    ? `${Number(hr) % 12 || 12}:${minute} ${Number(hr) >= 12 ? "PM" : "AM"}`
    : `${hr.padStart(2, "0")}:${minute}`;
  if (dow !== "*") {
    const dayMap: Record<string, string> = {
      "0": t("page.scheduled_jobs.sun"), "1": t("page.scheduled_jobs.mon"), "2": t("page.scheduled_jobs.tue"),
      "3": t("page.scheduled_jobs.wed"), "4": t("page.scheduled_jobs.thu"), "5": t("page.scheduled_jobs.fri"),
      "6": t("page.scheduled_jobs.sat"), "1-5": t("ui.schedule_weekdays"),
    };
    return t("ui.schedule_weekly", { days: dow.split(",").map((d) => dayMap[d] || d).join(", "), time });
  }
  if (dom !== "*") return t("ui.schedule_monthly", { day: dom, time });
  return t("ui.schedule_daily", { time });
}

function fmtSchedule(job: ScheduledJob): string {
  const tz = job.timezone && job.timezone !== "UTC" ? ` (${job.timezone})` : "";
  if (job.job_type === "cron" && job.cron_expr) return `${describeCron(job.cron_expr)}${tz}`;
  const intervalSeconds = job.interval_seconds ?? job.every_seconds;
  if ((job.job_type === "interval" || job.schedule_kind === "every") && intervalSeconds) {
    const s = intervalSeconds;
    const opt = INTERVAL_OPTIONS.find((o) => o.s === s);
    if (opt) return t("ui.schedule_every", { value: opt.label });
    if (s < 60) return t("ui.schedule_every_seconds", { count: s });
    return s < 3600
      ? t("ui.schedule_every_minutes", { count: Math.floor(s / 60) })
      : t("ui.schedule_every_hours", { count: Math.floor(s / 3600) });
  }
  if (job.job_type === "once" && job.run_at) return `${new Date(job.run_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: getPreferredTimeZone() })}${tz}`;
  return "--";
}

/* ── Run detail panel (lazy-loaded, inline) ── */
const ACTIVE_JOB_RUN_STATUSES = new Set(["pending", "queued", "running"]);
const PAUSED_JOB_RUN_STATUSES = new Set(["paused", "waiting"]);

function jobRunReadInterval(
  state: { status?: string; error?: unknown },
  status?: string,
  discovering = false,
): number | false {
  const normal = discovering || ACTIVE_JOB_RUN_STATUSES.has(status || "")
    ? 5_000
    : PAUSED_JOB_RUN_STATUSES.has(status || "") ? 30_000 : 60_000;
  return displayReadInterval(state, normal);
}

function AutomationWorkflowResult({ runId }: { runId: string }) {
  const visible = usePageVisible();
  const query = useQuery({
    queryKey: ["workflow-run-history-detail", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId, true, signal),
    enabled: visible,
    retry: false,
    refetchInterval: (query) => jobRunReadInterval(query.state, query.state.data?.status),
    // `enabled` already follows document visibility; continue when a visible
    // Automation tab lacks browser focus so its open run can settle.
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  if (query.isLoading) return <LoadingSpinner size={14} />;
  if (query.isError || !query.data) return <div role="alert">
    <p>{t("page.scheduled_jobs.failed_to_load_run_detail")}</p>
    <Button variant="ghost" size="sm" onClick={() => query.refetch()}>{t("component.workflow_run_history.try_again")}</Button>
  </div>;
  const run = query.data;
  // Persisted display snapshots use `targets` for every outgoing graph edge.
  const steps = run.definition_snapshot?.steps || (run.definition_snapshot?.nodes || [])
    .map((node: WorkflowHistoryNode) => ({ ...node, next: node.targets }));
  const result = resolveWorkflowFinalResult(steps, run);
  const headline = workflowFinalHeadline(result?.output);
  const artifacts = workflowFinalArtifactRefs(result?.output ?? run.step_results, 50);
  const output = result ? formatWorkflowValue(result.output, t("component.workflow_run.error_truncated")) : "";
  return <section aria-label={t("page.scheduled_jobs.execution_result")} style={{ marginTop: 12, color: "var(--text-default)", overflowWrap: "anywhere" }}>
    <span className="manor-label">{t("component.workflow_run_history.run_detail")} · {t(workflowRunStateKey(run.status))}</span>
    {headline?.title && <h3 style={{ fontSize: 14 }}>{headline.title}</h3>}
    {headline?.summary && <p style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>{headline.summary}</p>}
    {run.error && <p role="alert">{formatWorkflowValue(run.error, t("component.workflow_run.error_truncated"))}</p>}
    {artifacts.map((ref, index) => <InlineFileReferenceCard key={`${ref.document_id || ref.fs_path}:${index}`}
      reference={ref.document_id ? `/viewer/${encodeURIComponent(ref.document_id)}` : ref.fs_path!}
      label={workflowArtifactLabel(ref, t("component.workflow_run_history.artifact_number", { count: index + 1 }))} compact />)}
    {output ? <details open={!headline && artifacts.length === 0} style={{ marginTop: 8 }}>
      <summary>{t("page.scheduled_jobs.execution_result")}</summary>
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12, maxHeight: 320, overflow: "auto" }}>{output}</pre>
    </details> : artifacts.length === 0 && <p>{t(ACTIVE_JOB_RUN_STATUSES.has(run.status) ? "page.scheduled_jobs.loading_run" : "page.scheduled_jobs.no_detailed_result_recorded")}</p>}
  </section>;
}

// Internal failure codes people can act on; anything else stays in the technical details.
const RUN_ERROR_MESSAGES: Record<string, string> = {
  proposal_invalid_output: "page.scheduled_jobs.run_error_proposal_invalid_output",
};

function RunDetail({ jobId, runId }: { jobId: string; runId: string }) {
  const visible = usePageVisible();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["job-run-detail", jobId, runId],
    queryFn: () => api.jobs.runDetail(jobId, runId),
    enabled: visible,
    retry: false,
    refetchInterval: (query) => jobRunReadInterval(query.state, query.state.data?.run.status),
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });

  if (isLoading) {
    return (
      <div style={{ padding: "10px 0", display: "flex", alignItems: "center", gap: 8 }}>
        <LoadingSpinner size={14} /> <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("page.scheduled_jobs.loading_run")}</span>
      </div>
    );
  }
  if (error || !data) {
    return <div role="alert" style={{ fontSize: 12, color: "var(--editor-danger-text, #a23e38)", padding: "6px 0" }}>
      <p>{t("page.scheduled_jobs.failed_to_load_run_detail")}</p>
      <Button variant="ghost" size="sm" onClick={() => refetch()}>{t("component.workflow_run_history.try_again")}</Button>
    </div>;
  }

  const { run, task, agent_execution: ae } = data;
  const failed = Boolean(run.error) || ["error", "failed"].includes(String(run.status));
  const failureReason = t(RUN_ERROR_MESSAGES[String(run.error || "").split(":")[0]] || "page.scheduled_jobs.run_failed_plain");
  const workflowRunId = typeof run.result?.workflow_run_id === "string" ? run.result.workflow_run_id : "";
  const outcome = String(ae?.output_message || task?.response || run.result?.message || run.result?.summary || run.result?.result_summary || "").trim();
  const resultText = stringifyJobRunResult(run.result);
  const resultSummary = summarizeJobRunResult(run.result);
  const tokens = ae?.token_usage || {};
  const tokenTotal = (tokens.input_tokens || tokens.prompt_tokens || 0)
                  + (tokens.output_tokens || tokens.completion_tokens || 0);

  const Section = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 9, fontWeight: 700, color: "var(--text-muted)", marginBottom: 4 }}>{label}</div>
      {children}
    </div>
  );
  const Pre = ({ text }: { text: string }) => (
    <pre style={{
      margin: 0, padding: "8px 10px", borderRadius: 8,
      background: "rgba(250,250,249,0.7)", border: "0.5px solid rgba(29, 29, 31,0.06)",
      fontSize: 11, color: "var(--text-muted)", whiteSpace: "pre-wrap", wordBreak: "break-word",
      maxHeight: 220, overflow: "auto", fontFamily: "var(--font-mono)",
    }}>{text}</pre>
  );

  return (
    <div className="scheduled-run-detail">
      {workflowRunId ? (
        <AutomationWorkflowResult runId={workflowRunId} />
      ) : failed ? (
        <p className="scheduled-run-outcome">{failureReason}</p>
      ) : outcome ? (
        <p className="scheduled-run-outcome">{outcome}</p>
      ) : (
        <p className="scheduled-run-outcome is-empty">{t("page.scheduled_jobs.no_detailed_result_recorded")}</p>
      )}
      <details className="scheduled-run-technical">
        <summary>{t("component.workflow_run_history.technical_details")}</summary>
      <div style={{ display: "flex", gap: 10, alignItems: "center", fontSize: 10, color: "var(--text-muted)", flexWrap: "wrap" }}>
        <span style={{ padding: "1px 6px", borderRadius: 6, background: "#e6e9f3", color: "#494596", fontWeight: 600 }}>{run.trigger_type || "—"}</span>
        {run.duration_ms != null && <span>{(run.duration_ms / 1000).toFixed(2)}{t("page.scheduled_jobs.s")}</span>}
        {tokenTotal > 0 && <span>{tokenTotal.toLocaleString()} {t("page.scheduled_jobs.tokens")}</span>}
        {ae?.tools_used?.length > 0 && <span>{ae.tools_used.length} {t("page.scheduled_jobs.tool_calls")}</span>}
      </div>

      {run.error && (
        <Section label={t("page.job_logs.error")}>
          <Pre text={run.error} />
        </Section>
      )}
      {resultText && (
        <details style={{ marginTop: 10 }}>
          <summary>{t(run.result?.workflow_run_id || ae?.output_message || task?.response
            ? "component.workflow_run_history.technical_details" : "page.scheduled_jobs.execution_result")}</summary>
          {resultSummary.length > 0 && (
            <div style={{
              display: "flex", flexDirection: "column", gap: 3,
              marginBottom: 6, fontSize: 11, color: "var(--text-muted)",
            }}>
              {resultSummary.map((line, i) => (
                <div key={i} style={{
                  padding: "4px 7px", borderRadius: 7,
                  background: "var(--surface-panel)", border: "1px solid rgba(226,232,240,0.6)",
                }}>
                  {line}
                </div>
              ))}
            </div>
          )}
          <Pre text={resultText} />
        </details>
      )}
      {task?.description && (
        <Section label={t("page.scheduled_jobs.prompt_sent")}>
          <Pre text={task.description} />
        </Section>
      )}
      {ae?.input_message && !task?.description && (
        <Section label={t("page.agent_dashboard.input")}>
          <Pre text={ae.input_message} />
        </Section>
      )}
      {!ae?.output_message && task?.response && (
        <Section label={
          String(task.response).toLowerCase().startsWith("sorry, the request failed")
            ? t("page.scheduled_jobs.agent_failed_error_detail")
            : t("page.scheduled_jobs.agent_response")
        }>
          <Pre text={task.response} />
          {task.supervisor_verdict?.reason && (
            <p style={{ fontSize: 10, color: "var(--text-muted)", margin: "4px 0 0", fontStyle: "italic" }}>
              {t("page.scheduled_jobs.supervisor")} {task.supervisor_verdict.reason}
            </p>
          )}
        </Section>
      )}
      {ae?.tools_used?.length > 0 && (
        <Section label={t("page.scheduled_jobs.tools_used")}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {ae.tools_used.map((t: any, i: number) => (
              <span key={i} style={{ fontSize: 10, padding: "2px 8px", borderRadius: 12, background: "var(--surface-panel)", border: "0.5px solid rgba(29, 29, 31,0.06)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                {typeof t === "string" ? t : t.name || JSON.stringify(t)}
              </span>
            ))}
          </div>
        </Section>
      )}
      {task?.timeline?.length > 0 && (
        <Section label={`Execution timeline (${task.timeline.length} step${task.timeline.length === 1 ? "" : "s"})`}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {task.timeline.map((step: any, i: number) => {
              const dot =
                step.type === "ai_execution_completed" ? "#4f9c84"
                : step.type === "ai_execution_failed" ? "#d65f59"
                : step.type === "ai_supervisor_verdict" ? "#6e6e73"
                : step.type === "ai_agent_turn" ? "#4f7d75"
                : "#aeaeb2";
              const calledTools = step.type === "ai_agent_turn"
                && step.content.includes("tools:");
              return (
                <div key={i} style={{
                  display: "flex", gap: 8, alignItems: "flex-start",
                  fontSize: 11, padding: "4px 6px",
                  borderLeft: `2px solid ${dot}`,
                  background: calledTools ? "rgba(79,125,117,0.04)" : "transparent",
                }}>
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: dot, flexShrink: 0, marginTop: 5 }} />
                  <span style={{ fontWeight: 600, color: "var(--text-muted)", minWidth: 130, fontFamily: "var(--font-mono)", fontSize: 10 }}>
                    {step.type}
                  </span>
                  <span style={{ color: "var(--text-default)", flex: 1, wordBreak: "break-word" }}>
                    {step.content}
                  </span>
                </div>
              );
            })}
          </div>
          {!task.timeline.some((s: any) =>
            s.type === "ai_agent_turn" && s.content.includes("tools:")
          ) && (
            <p style={{ fontSize: 10, color: "#c14a44", margin: "6px 0 0", fontStyle: "italic" }}>
              {t("page.scheduled_jobs.the_agent_never_invoked_a_tool_it_produced_text")}
            </p>
          )}
        </Section>
      )}
      {task && (
        <Section label={t("page.scheduled_jobs.linked_task")}>
          <a href={`/tasks/${task.id}`} style={{ fontSize: 11, color: "var(--text-muted)", textDecoration: "none", fontWeight: 500 }}>
            #{task.id.slice(-6)} · {task.title} · {task.status}
          </a>
        </Section>
      )}
      </details>
    </div>
  );
}


interface ScheduledJobsProps {
  workspaceId?: string;
  workspaceStatus?: string;
  workflowsEnabled?: boolean;
  canWriteWorkspace?: boolean;
}

/** What Manor runs by itself to keep a Workspace healthy; nobody set these up. */
const SYSTEM_AUTOMATION_NAMES: Record<string, string> = {
  strategist_review: "page.scheduled_jobs.system_review",
  outcome_evaluation: "page.scheduled_jobs.system_outcome",
  chat_insight_extraction: "page.scheduled_jobs.system_insights",
};

function automationDisplayName(job: ScheduledJob): string {
  const systemName = SYSTEM_AUTOMATION_NAMES[String(job.execution_type || "")];
  return systemName ? t(systemName) : formatUserFacingText(job.name);
}

interface AutomationGroup {
  key: string;
  workspace?: { name: string };
  own: ScheduledJob[];
  upkeep: ScheduledJob[];
}

function automationGroups(jobs: ScheduledJob[], workspaces: Map<string, Workspace>): AutomationGroup[] {
  const groups = new Map<string, AutomationGroup>();
  for (const job of jobs) {
    const key = job.workspace_id || "";
    const group = groups.get(key) || { key, workspace: workspaces.get(key), own: [], upkeep: [] };
    (SYSTEM_AUTOMATION_NAMES[String(job.execution_type || "")] ? group.upkeep : group.own).push(job);
    groups.set(key, group);
  }
  // Personal automations come last, after the Workspaces.
  return [...groups.values()].sort((a, b) => Number(!a.key) - Number(!b.key));
}

/* ── main ── */
/**
 * What the ledger knows that a row can't: how often it really ran. Misses stay out,
 * since the next tick dispatches whatever is overdue and Workspace health watches the rest.
 */
function automationRecord(health: AutomationHealthEntry | undefined): string {
  if (!health) return "";
  const { dispatched, failed } = health.runs_30d;
  return [
    dispatched > 0 ? t("ui.automation_runs_30d", { count: dispatched }) : "",
    failed > 0 ? t("ui.automation_failed_30d", { count: failed }) : "",
  ].filter(Boolean).map((part) => ` · ${part}`).join("");
}

/** Workflows named by a generator read like identifiers; a person shouldn't have to. */
function readableWorkflowName(name: string): string {
  if (!/^[a-z0-9]+(?:[_-][a-z0-9]+)+$/.test(name)) return name;
  const words = name.replace(/[_-]+/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export default function ScheduledJobs({ workspaceId, workspaceStatus, workflowsEnabled, canWriteWorkspace = true }: ScheduledJobsProps) {
  const [searchParams] = useSearchParams();
  const requestedJobId = workspaceId ? null : searchParams.get("job");
  const requestedRunId = requestedJobId ? searchParams.get("run") : null;
  const autoOpenedRequest = useRef("");
  useEffect(() => { autoOpenedRequest.current = ""; }, [requestedJobId, requestedRunId]);
  const catalogScope = useCatalogScope();
  const qc = useQueryClient();
  const visible = usePageVisible();
  const toast = useToastStore();
  const flowsAccess = usePreviewFeatureAccess("flows");
  const showWorkflowAutomations = workflowsEnabled ?? flowsAccess.enabled;
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const [statusFilter, setStatusFilter] = useState<AutomationStatusFilter>("all");
  const [agentFilter, setAgentFilter] = useState("all");
  const navigate = useNavigate();
  const [workspaceFilter, setWorkspaceFilter] = useState(workspaceId || "all");
  const [openUpkeep, setOpenUpkeep] = useState<Set<string>>(() => new Set());
  const [page, setPage] = useState(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [runDiscoveryUntil, setRunDiscoveryUntil] = useState<Record<string, number>>({});
  const [modalOpen, setModalOpen] = useState(false);
  const [editJob, setEditJob] = useState<ScheduledJob | null>(null);
  const [editBinding, setEditBinding] = useState<WorkflowBinding | null>(null);
  const [fKind, setFKind] = useState<AutomationKind>("agent_schedule");
  const [fWorkflow, setFWorkflow] = useState("");
  const [fWorkspaceBinding, setFWorkspaceBinding] = useState("");
  const [fEvent, setFEvent] = useState("task.approval_decision");
  const [fName, setFName] = useState("");
  const [fFreq, setFFreq] = useState<"hourly" | "daily" | "weekly" | "monthly" | "interval" | "once">("daily");
  const [fHour, setFHour] = useState(9); const [fMinute, setFMinute] = useState(0);
  const [fDays, setFDays] = useState<number[]>([1, 2, 3, 4, 5]); // Mon-Fri
  const [fDom, setFDom] = useState(1); // day of month
  const [fIntervalSec, setFIntervalSec] = useState(3600);
  const [fRunAt, setFRunAt] = useState(""); const [fMessage, setFMessage] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [deleteBindingTarget, setDeleteBindingTarget] = useState<string | null>(null);
  const [fAgent, setFAgent] = useState(MANOR_AGENT_ID);
  const [fTimezone, setFTimezone] = useState(browserTimezone);
  const selectedWorkspaceId = workspaceId || (workspaceFilter === "all" ? undefined : workspaceFilter);
  const { data: automationHealth } = useQuery({
    queryKey: ["workspace-automation-health", workspaceId],
    queryFn: () => api.workspaces.observability.automationHealth(workspaceId!),
    enabled: !!workspaceId,
    retry: false,
  });
  const healthById = useMemo(
    () => new Map((automationHealth?.automations ?? []).map((entry) => [entry.id, entry])),
    [automationHealth],
  );
  const jobsQueryKey = useMemo(
    () => [
      "scheduled-jobs",
      selectedWorkspaceId || "all",
      deferredSearch,
      statusFilter,
      agentFilter,
      page,
      showWorkflowAutomations,
    ],
    [
      agentFilter,
      deferredSearch,
      page,
      selectedWorkspaceId,
      showWorkflowAutomations,
      statusFilter,
    ],
  );

  useEffect(() => {
    setPage(1);
    setExpandedId(null);
  }, [agentFilter, deferredSearch, selectedWorkspaceId, statusFilter]);

  // WebSocket job_update events and local mutations invalidate this query live.
  const { data: jobsData, isLoading, isFetching } = useQuery({
    queryKey: jobsQueryKey,
    queryFn: () => api.jobs.list({
      workspace_id: selectedWorkspaceId,
      search: deferredSearch || undefined,
      status: statusFilter,
      agent_id: agentFilter === "all" ? undefined : agentFilter,
      include_workflows: showWorkflowAutomations,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    placeholderData: (previousData) => previousData,
  });
  useEffect(() => {
    const deadlines = Object.values(runDiscoveryUntil);
    if (!deadlines.length) return;
    const delay = Math.max(0, Math.min(...deadlines) - Date.now());
    const timer = window.setTimeout(() => {
      const now = Date.now();
      setRunDiscoveryUntil((current) => Object.fromEntries(
        Object.entries(current).filter(([, deadline]) => deadline > now),
      ));
    }, delay);
    return () => window.clearTimeout(timer);
  }, [runDiscoveryUntil]);
  const allJobs: ScheduledJob[] = (jobsData?.items as ScheduledJob[] | undefined) ?? [];
  const focusedJobQuery = useQuery({
    queryKey: ["scheduled-jobs", "detail", requestedJobId],
    queryFn: () => api.jobs.get(requestedJobId!),
    enabled: Boolean(requestedJobId),
    retry: false,
  });
  const fetchedJob = (focusedJobQuery.isError ? undefined : focusedJobQuery.data) as ScheduledJob | undefined;
  const focusedJob = fetchedJob && (showWorkflowAutomations || fetchedJob.execution_type !== "workflow") ? fetchedJob : undefined;
  const visibleJobs = showWorkflowAutomations ? allJobs : allJobs.filter((job) => job.execution_type !== "workflow");
  const jobs = focusedJob
    ? [focusedJob, ...visibleJobs.filter((job) => job.id !== focusedJob.id)]
    : visibleJobs;
  useEffect(() => {
    if (focusedJob) setExpandedId(focusedJob.id);
  }, [focusedJob?.id, requestedJobId, requestedRunId]);
  const totalPages = Math.max(1, Math.ceil((jobsData?.total ?? 0) / PAGE_SIZE));

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);
  const { data: workflows = [] } = useQuery({
    queryKey: ["workflows", "summary"],
    queryFn: () => api.workflows.listSummaries(),
    // The company-wide list names the workflow behind each automation too.
    enabled: !workspaceId || showWorkflowAutomations,
  });
  const bindingsQueryKey = useMemo(() => ["workflow-bindings", workspaceId || "none"], [workspaceId]);
  const { data: bindings = [], isLoading: bindingsLoading } = useQuery({
    queryKey: bindingsQueryKey,
    queryFn: () => workspaceId ? api.workflows.listBindings({ workspace_id: workspaceId }) : Promise.resolve([]),
    enabled: !!workspaceId && showWorkflowAutomations,
  });
  const { data: agents = [] } = useQuery(agentListQueryOptions(catalogScope));
  const { data: availableWorkspaces = [] } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
    enabled: !workspaceId,
  });
  const workspaceById = useMemo(
    () => new Map(
      (availableWorkspaces as Workspace[]).map((workspace) => [workspace.id, workspace]),
    ),
    [availableWorkspaces],
  );
  const jobRunView = (job: ScheduledJob) => automationRunView({
    enabled: job.enabled,
    workspaceBlocked: job.workspace_blocked,
    workspaceStatus: job.workspace_status
      ?? (workspaceId ? workspaceStatus : workspaceById.get(job.workspace_id || "")?.status),
  });
  const bindingRunView = (binding: WorkflowBinding) => automationRunView({
    enabled: binding.enabled && binding.status === "active",
    workspaceBlocked: workspaceBlocksConfiguredWork(workspaceStatus),
    workspaceStatus,
  });
  const workspaceFilterOptions = useMemo(
    () => [
      { key: "all", label: t("page.scheduled_jobs.all_workspaces") },
      ...(availableWorkspaces as Workspace[]).map((workspace) => ({
        key: workspace.id,
        label: workspace.name,
      })),
    ],
    [availableWorkspaces],
  );
  const agentFilterOptions = useMemo(
    () => [
      { key: "all", label: t("page.scheduled_jobs.all_agents") },
      { key: MANOR_AGENT_ID, label: MANOR_AGENT_NAME },
      ...(agents as Agent[])
        .filter((agent) => !isMasterAgent(agent.id))
        .map((agent) => ({ key: agent.id, label: agent.name })),
    ],
    [agents],
  );
  const activeFilterCount = (statusFilter === "all" ? 0 : 1)
    + (agentFilter === "all" ? 0 : 1)
    + (!workspaceId && workspaceFilter !== "all" ? 1 : 0);
  const { data: runs = [], isLoading: runsLoading, isError: runsError, refetch: refetchRuns } = useQuery({
    queryKey: ["job-runs", expandedId],
    queryFn: () => expandedId ? api.jobs.runs(expandedId) : Promise.resolve([]),
    enabled: visible && !!expandedId,
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.find((run) => ACTIVE_JOB_RUN_STATUSES.has(run.status))?.status
        || query.state.data?.find((run) => PAUSED_JOB_RUN_STATUSES.has(run.status))?.status;
      return jobRunReadInterval(query.state, status, Boolean(expandedId && runDiscoveryUntil[expandedId] > Date.now()));
    },
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  const requestedRunQuery = useQuery({
    queryKey: ["job-run-detail", focusedJob?.job_id, requestedRunId],
    queryFn: () => api.jobs.runDetail(focusedJob!.job_id, requestedRunId!),
    enabled: Boolean(focusedJob && requestedRunId),
    retry: false,
  });
  const recentRuns = (runs as JobRun[]).slice(0, 10);
  const historyRuns = expandedId === focusedJob?.id && requestedRunQuery.data
    ? [requestedRunQuery.data.run as JobRun, ...recentRuns.filter((run) => run.id !== requestedRunId)]
    : recentRuns;
  useEffect(() => {
    const request = `${requestedJobId}:${requestedRunId || "latest"}`;
    if (!focusedJob || expandedId !== focusedJob.id || runsLoading || runsError || autoOpenedRequest.current === request) return;
    const selectedRun = requestedRunId ? requestedRunQuery.data?.run : runs[0];
    if (!selectedRun) return;
    autoOpenedRequest.current = request;
    setOpenRunId(selectedRun.id);
  }, [requestedJobId, requestedRunId, focusedJob?.id, expandedId, runs, runsLoading, runsError, requestedRunQuery.data]);
  const invalidateJobs = () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] });

  const createMut = useMutation({
    mutationFn: (d: any) => api.jobs.create(d),
    onSuccess: () => { invalidateJobs(); toast.success("Automation created"); closeModal(); },
    onError: (e) => toast.error(translateApiError(e, "Failed to create automation")),
  });
  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => api.jobs.update(id, data),
    onSuccess: () => { invalidateJobs(); toast.success("Automation saved"); closeModal(); },
    onError: (e) => toast.error(translateApiError(e, "Failed to save automation")),
  });
  const toggleMut = useMutation({ mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.jobs.toggle(id, enabled), onSuccess: invalidateJobs });
  const deleteMut = useMutation({ mutationFn: (id: string) => api.jobs.delete(id), onSuccess: invalidateJobs });
  const runNowMut = useMutation({
    mutationFn: (id: string) => api.jobs.runNow(id),
    onSuccess: (_, id) => {
      // Worker creates the JobRun row; refresh both list (updates last_run_at)
      // and the runs panel for this job.
      invalidateJobs();
      qc.invalidateQueries({ queryKey: ["job-runs", id] });
      // Worker dispatch and JobRun persistence are separate. The expanded
      // query observes the short window and returns to its state-based cadence
      // as soon as it sees the new row (or after this bounded fallback).
      setRunDiscoveryUntil((current) => ({ ...current, [id]: Date.now() + 20_000 }));
    },
  });

  const createBindingMut = useMutation({
    mutationFn: (data: any) => api.workflows.createBinding(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: bindingsQueryKey });
      toast.success("Workflow automation created");
      closeModal();
    },
    onError: (error) => toast.error(translateApiError(error, "Failed to create workflow automation")),
  });
  const updateBindingMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => api.workflows.updateBinding(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: bindingsQueryKey });
      toast.success("Workflow automation saved");
      closeModal();
    },
    onError: (error) => toast.error(translateApiError(error, "Failed to save workflow automation")),
  });
  const toggleBindingMut = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.workflows.updateBinding(id, { enabled, status: enabled ? "active" : "paused" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: bindingsQueryKey }),
  });
  const deleteBindingMut = useMutation({
    mutationFn: (id: string) => api.workflows.deleteBinding(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: bindingsQueryKey });
      setDeleteBindingTarget(null);
    },
  });
  const runBindingMut = useMutation({
    mutationFn: (id: string) => api.workflows.runBinding(id, { execute: false, trigger_data: { manual_test: true } }),
    onSuccess: () => toast.success("Workflow run queued"),
    onError: (error) => toast.error(
      "Couldn't run workflow",
      translateApiError(error, "The workflow service is unavailable. Refresh and try again."),
    ),
  });

  const workflowBindings = useMemo(
    () => showWorkflowAutomations
      ? (bindings as WorkflowBinding[]).filter((binding) => binding.trigger_type === "workspace_event")
      : [],
    [bindings, showWorkflowAutomations],
  );
  const workspaceWorkflowBindings = useMemo(
    () => showWorkflowAutomations
      ? (bindings as WorkflowBinding[]).filter((binding) => binding.trigger_type === "manual")
      : [],
    [bindings, showWorkflowAutomations],
  );
  const workflowBindingOptions = useMemo(() => {
    const options = workspaceWorkflowBindings.map((binding) => {
      const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === binding.workflow_id);
      return { value: binding.id, label: binding.name || workflow?.name || "Workspace workflow" };
    });
    if (fWorkflow && !fWorkspaceBinding && !workspaceWorkflowBindings.some((binding) => binding.workflow_id === fWorkflow)) {
      const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === fWorkflow);
      options.unshift({ value: `legacy:${fWorkflow}`, label: `${workflow?.name || "Legacy workflow"} · not attached` });
    }
    return options;
  }, [fWorkflow, fWorkspaceBinding, workspaceWorkflowBindings, workflows]);
  const selectedWorkflowBinding = fWorkspaceBinding || (fWorkflow ? `legacy:${fWorkflow}` : "");
  const workflowSelectionMissing = fKind !== "agent_schedule"
    && (!fWorkflow || ((!editJob && !editBinding) && !fWorkspaceBinding));
  const workflowAutomationTabs = showWorkflowAutomations
    ? AUTOMATION_KIND_TABS
    : AUTOMATION_KIND_TABS.filter((tab) => tab.key === "agent_schedule");
  const filteredBindings = useMemo(() => {
    if (page !== 1 || agentFilter !== "all") return [];
    const query = deferredSearch.toLowerCase();
    return workflowBindings.filter((binding) => {
      if (statusFilter === "attention") return false;
      if (!matchesAutomationStatusFilter(bindingRunView(binding), statusFilter)) return false;
      const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === binding.workflow_id);
      return !query
        || `${binding.name || ""} ${workflow?.name || ""} ${binding.trigger_config?.event || ""}`
          .toLowerCase()
          .includes(query);
    });
  }, [agentFilter, deferredSearch, page, statusFilter, workflowBindings, workflows, workspaceStatus]);
  const bindingSummary = useMemo(() => {
    if (agentFilter !== "all") return { total: 0, enabled: 0 };
    const query = deferredSearch.toLowerCase();
    const matching = workflowBindings.filter((binding) => {
      const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === binding.workflow_id);
      return !query
        || `${binding.name || ""} ${workflow?.name || ""} ${binding.trigger_config?.event || ""}`
          .toLowerCase()
          .includes(query);
    });
    return {
      total: matching.length,
      enabled: matching.filter((binding) => bindingRunView(binding).state === "enabled").length,
    };
  }, [agentFilter, deferredSearch, workflowBindings, workflows, workspaceStatus]);
  const attentionN = jobsData?.attention_total ?? 0;
  const stats = attentionN > 0 ? (
    <div className="flex flex-wrap items-center gap-2">
      {attentionN > 0 && (
        <StatPill
          label={t("page.scheduled_jobs.attention_count", { count: attentionN })}
          color="var(--editor-danger-text)"
          bg="var(--editor-danger-bg)"
        />
      )}
    </div>
  ) : null;

  function openModal(job?: ScheduledJob) {
    const workflowId = String(job?.execution_target?.workflow_id || "");
    const matchingBinding = workspaceWorkflowBindings.find((binding) => binding.workflow_id === workflowId);
    setEditJob(job || null);
    setEditBinding(null);
    setFKind(job?.execution_type === "workflow" ? "workflow_schedule" : "agent_schedule");
    setFWorkflow(workflowId || matchingBinding?.workflow_id || "");
    setFWorkspaceBinding(String(job?.execution_target?.binding_id || matchingBinding?.id || ""));
    setFEvent("task.approval_decision");
    setFName(job?.name || "");
    // Parse existing schedule into builder state
    if (job?.job_type === "once" || job?.schedule_kind === "at") {
      setFFreq("once"); setFRunAt(job?.run_at || "");
    } else if (job?.schedule_kind === "every" && (job?.interval_seconds ?? job?.every_seconds)) {
      setFFreq("interval"); setFIntervalSec(job.interval_seconds ?? job.every_seconds ?? 3600);
    } else if (job?.cron_expr) {
      const p = job.cron_expr.split(" ");
      if (p.length === 5) {
        setFMinute(p[0] === "*" ? 0 : Number(p[0]) || 0);
        setFHour(p[1] === "*" ? 0 : Number(p[1]) || 9);
        if (p[4] !== "*") { setFFreq("weekly"); setFDays(p[4].split(",").map(Number)); }
        else if (p[2] !== "*") { setFFreq("monthly"); setFDom(Number(p[2]) || 1); }
        else if (p[1] === "*") { setFFreq("hourly"); }
        else { setFFreq("daily"); }
      }
    } else {
      setFFreq("daily"); setFHour(9); setFMinute(0); setFDays([1, 2, 3, 4, 5]);
    }
    setFMessage(job?.payload_message || (job?.payload as any)?.message || "");
    setFAgent(job?.agent_id || MANOR_AGENT_ID); setFTimezone(job?.timezone || browserTimezone());
    setModalOpen(true);
  }
  function openBindingModal(binding: WorkflowBinding) {
    const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === binding.workflow_id);
    const referencedBindingId = String(binding.config?.workspace_workflow_binding_id || "");
    const matchingBinding = workspaceWorkflowBindings.find((item) => item.id === referencedBindingId)
      || workspaceWorkflowBindings.find((item) => item.workflow_id === binding.workflow_id);
    setEditJob(null);
    setEditBinding(binding);
    setFKind("workflow_event");
    setFWorkflow(binding.workflow_id);
    setFWorkspaceBinding(matchingBinding?.id || "");
    setFEvent(String(binding.trigger_config?.event || "task.approval_decision"));
    setFName(binding.name || workflow?.name || "Workflow automation");
    setFMessage("");
    setModalOpen(true);
  }
  function openNewModal() {
    openModal();
    if (workspaceId && showWorkflowAutomations) {
      const binding = workspaceWorkflowBindings[0];
      setFKind("workflow_schedule");
      setFWorkflow(binding?.workflow_id || "");
      setFWorkspaceBinding(binding?.id || "");
    }
  }
  function closeModal() { setModalOpen(false); setEditJob(null); setEditBinding(null); }
  function submit() {
    if (fKind === "workflow_event") {
      if (!workspaceId || workflowSelectionMissing || !fEvent.trim()) return;
      const config = { ...(editBinding?.config || {}) };
      if (fWorkspaceBinding) config.workspace_workflow_binding_id = fWorkspaceBinding;
      else delete config.workspace_workflow_binding_id;
      const data = {
        workflow_id: fWorkflow,
        workspace_id: workspaceId,
        name: fName.trim(),
        trigger_type: "workspace_event",
        trigger_config: { event: fEvent.trim() },
        config,
      };
      if (editBinding) {
        updateBindingMut.mutate({
          id: editBinding.id,
          data: { ...data, enabled: editBinding.enabled, status: editBinding.status },
        });
      } else {
        createBindingMut.mutate(data);
      }
      return;
    }

    if (fKind === "workflow_schedule" && workflowSelectionMissing) return;
    const d: any = {
      name: fName,
      timezone: fTimezone,
    };
    if (fKind === "workflow_schedule") {
      d.execution_type = "workflow";
      d.execution_target = {
        workflow_id: fWorkflow,
        ...(fWorkspaceBinding ? { binding_id: fWorkspaceBinding } : {}),
        ...(workspaceId ? { workspace_id: workspaceId } : {}),
      };
    } else {
      d.execution_type = "agent";
      d.payload_message = fMessage || undefined;
      d.agent_id = fAgent || undefined;
      if (editJob?.execution_target && Object.keys(editJob.execution_target).length > 0) {
        d.execution_target = editJob.execution_target;
      }
    }
    if (workspaceId) d.workspace_id = workspaceId;
    if (fFreq === "once") {
      d.job_type = "once"; d.schedule_kind = "at"; d.run_at = fRunAt;
    } else if (fFreq === "interval") {
      d.job_type = "interval"; d.schedule_kind = "every";
      d.every_seconds = fIntervalSec; d.interval_seconds = fIntervalSec;
    } else {
      d.job_type = "cron"; d.schedule_kind = "cron";
      d.cron_expr = buildCron(fFreq, fHour, fMinute, fDays, fDom);
    }
    if (!editJob) d.job_id = `auto-${Date.now().toString(36)}`;
    editJob ? updateMut.mutate({ id: editJob.id, data: d }) : createMut.mutate(d);
  }

  const renderJobRow = (job: ScheduledJob) => {
        const exp = expandedId === job.id;
        const ag = job.agent_id && !isMasterAgent(job.agent_id) ? (agents as Agent[]).find((a) => a.id === job.agent_id) : null;
        const hasErr = job.consecutive_errors > 0;
        const health = healthById.get(job.id);
        const msg = (job as any).payload_message || job.payload?.message;
        const msgSummary = summarizeAutomationMessage(msg);
        const jobWorkflow = job.execution_type === "workflow"
          ? (workflows as WorkflowSummary[]).find((item) => item.id === job.execution_target?.workflow_id)
          : null;
        const jobWorkflowBinding = jobWorkflow
          ? (bindings as WorkflowBinding[]).find((binding) => (
              binding.id === job.execution_target?.binding_id
              || binding.workflow_id === jobWorkflow.id
            ))
          : null;
        const runView = jobRunView(job);
        const effectiveEnabled = runView.state === "enabled";
        const canManageJob = job.can_manage !== false && (!job.workspace_id || canWriteWorkspace);

        return (
          <div key={job.id} className={`scheduled-job-card${effectiveEnabled ? "" : " scheduled-job-card--disabled"}`} style={{
            padding: 0, overflow: "visible", borderRadius: 8, transform: "none",
          }}>
            {/* Main row */}
            <div onClick={() => { setExpandedId(exp ? null : job.id); setOpenRunId(null); }}
              role="button" tabIndex={0} aria-expanded={exp} aria-label={job.name}
              onKeyDown={(event) => {
                if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) {
                  event.preventDefault(); setExpandedId(exp ? null : job.id); setOpenRunId(null);
                }
              }}
              className="scheduled-job-row scheduled-automation-grid"
              style={{ cursor: "pointer", transition: "background 0.15s", borderRadius: exp ? "8px 8px 0 0" : 8 }}>

              {/* Name, then one line: when it runs and how it last went. */}
              <div className="scheduled-job-info" style={{ flex: 1, minWidth: 0 }} title={msgSummary || undefined}>
                <div className="scheduled-job-title-row">
                  <span className="scheduled-job-title">{automationDisplayName(job)}</span>
                  {health?.active_experiment && <span className="workspace-workflow-automation-state">{t("ui.automation_in_experiment")}</span>}
                  {runView.labelKey && <span className="workspace-workflow-automation-state">{t(runView.labelKey)}</span>}
                </div>
                <div className="scheduled-job-meta">
                  <span>{fmtSchedule(job)}</span>
                  {jobWorkflow && <span>{jobWorkflowBinding?.name || readableWorkflowName(jobWorkflow.name)}</span>}
                  {!jobWorkflow && ag && <span>{ag.name}</span>}
                  {hasErr ? (
                    <span className="scheduled-job-error-summary" title={job.last_error || t("page.scheduled_jobs.last_run_failed")}>
                      {job.consecutive_errors > 1
                        ? t("ui.automation_failure_streak", { count: job.consecutive_errors })
                        : t("page.scheduled_jobs.last_run_failed")}
                    </span>
                  ) : (
                    <span className="scheduled-job-last-run">
                      {job.last_run_at ? `${t("page.scheduled_jobs.last_run")} ${relativeTime(job.last_run_at)}` : t("page.scheduled_jobs.not_run_yet")}
                    </span>
                  )}
                </div>
              </div>

              {/* Actions */}
              <div className="scheduled-job-actions scheduled-automation-controls" onClick={(e) => e.stopPropagation()}>
                <span className="scheduled-automation-linked-slot">
                  {job.manor_task_id && (
                  <Link
                    to={`/tasks/${job.manor_task_id}`}
                    title={t("page.scheduled_jobs.linked_task")}
                    style={{
                      height: 28,
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      padding: "0 10px",
                      borderRadius: 8,
                      background: "var(--surface-muted)",
                      color: "var(--text-muted)",
                      fontSize: 11,
                      fontWeight: 500,
                      textDecoration: "none",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {t("page.scheduled_jobs.linked_task")}
                  </Link>
                  )}
                </span>
                <Toggle
                  checked={runView.checked}
                  onChange={() => toggleMut.mutate({ id: job.id, enabled: runView.nextEnabled })}
                  disabled={!canManageJob || !runView.canToggle}
                  aria-label={`${runView.checked ? "Pause" : "Enable"} ${job.name}`}
                />
                <button
                  onClick={() => runNowMut.mutate(job.id)}
                  disabled={!canManageJob || (runNowMut.isPending && runNowMut.variables === job.id)}
                  className="btn-manor-ghost"
                  style={{ width: 28, height: 28, padding: 0, borderRadius: 8 }}
                  title={t("page.scheduled_jobs.run_now")}
                ><IconPlay size={13} /></button>
                <button onClick={() => openModal(job)} disabled={!canManageJob} className="btn-manor-ghost" style={{ width: 28, height: 28, padding: 0, borderRadius: 8 }} title={t("action.edit")}><IconEdit size={13} /></button>
                <button onClick={() => setDeleteTarget(job.job_id)} disabled={!canManageJob} className="btn-manor-ghost" style={{ width: 28, height: 28, padding: 0, borderRadius: 8, color: "var(--text-muted)" }} title={t("action.delete")}><IconTrash size={13} /></button>
              </div>

              <IconChevronRight className="scheduled-automation-expand" size={12} style={{ color: "var(--text-faint)", transition: "transform 0.2s", transform: exp ? "rotate(90deg)" : "none", flexShrink: 0 }} />
            </div>

            {/* Runs */}
            {exp && (
              <div style={{ padding: "0 18px 16px", borderTop: "0.5px solid rgba(29, 29, 31,0.06)" }}>
                {hasErr && <p className="scheduled-job-error-detail">{t("page.scheduled_jobs.failure_note")}</p>}
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", margin: "12px 0 8px" }}>
                  <span className="manor-label" style={{ margin: 0 }}>{t("page.scheduled_jobs.recent_runs")}</span>
                </div>
                {runsLoading ? <LoadingSpinner size={14} /> : runsError ? <div role="alert">
                  <p>{t("page.scheduled_jobs.failed_to_load_run_detail")}</p>
                  <Button variant="ghost" size="sm" onClick={() => refetchRuns()}>{t("component.workflow_run_history.try_again")}</Button>
                </div> : historyRuns.length === 0 && !requestedRunId ? (
                  <p style={{ fontSize: 12, color: "var(--text-muted)", margin: 0 }}>{t("page.flows.no_runs")}</p>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
                    {requestedRunId && job.id === focusedJob?.id && requestedRunQuery.isLoading && <LoadingSpinner size={14} />}
                    {requestedRunId && job.id === focusedJob?.id && requestedRunQuery.isError && (
                      <div role="alert">
                        <p>{t("page.scheduled_jobs.failed_to_load_run_detail")}</p>
                        <Button variant="ghost" size="sm" onClick={() => requestedRunQuery.refetch()}>{t("component.workflow_run_history.try_again")}</Button>
                      </div>
                    )}
                    {historyRuns.map((r) => {
                      const isOpen = openRunId === r.id;
                      const failedRun = r.status === "error" || r.status === "failed";
                      const active = ACTIVE_JOB_RUN_STATUSES.has(r.status) || r.status === "waiting";
                      const dot = failedRun ? "var(--editor-danger-text, #a23e38)" : active ? "#cf9b44" : "var(--text-muted)";
                      return (
                        <div key={r.id}>
                          <button type="button" aria-expanded={isOpen}
                            onClick={() => setOpenRunId(isOpen ? null : r.id)}
                            style={{
                              display: "flex", alignItems: "center", gap: 8, fontSize: 13,
                              padding: "6px 0", borderBottom: isOpen ? "none" : "1px solid rgba(245,245,244,0.5)",
                              cursor: "pointer", background: "transparent", border: 0, width: "100%", textAlign: "left", flexWrap: "wrap",
                            }}
                          >
                            <span style={{ width: 6, height: 6, borderRadius: "50%", flexShrink: 0, background: dot }} />
                            <span style={{ color: "var(--text-strong)" }}>{r.status === "waiting" ? t("component.status.waiting") : t(workflowRunStateKey(failedRun ? "failed" : r.status === "success" ? "completed" : r.status))}</span>
                            {r.status === "waiting" && r.waiting_reason && !isOpen && <span style={{ color: "#b07d2b", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.waiting_reason}</span>}
                            <span style={{ color: "var(--text-muted)", marginLeft: "auto" }}>{r.created_at ? relativeTime(r.created_at) : ""}</span>
                          </button>
                          {isOpen && <RunDetail jobId={job.job_id} runId={r.id} />}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        );
  };

  const finding = activeFilterCount > 0 || Boolean(search) || Boolean(requestedJobId);
  const jobGroups = automationGroups(jobs, workspaceById);
  const renderJobGroupRows = (group: AutomationGroup) => {
    const open = finding || openUpkeep.has(group.key);
    const failing = group.upkeep.filter((job) => job.consecutive_errors > 0).length;
    return (
      <Fragment key={group.key}>
        {group.own.map(renderJobRow)}
        {group.upkeep.length > 0 && (
          <ConfigRow
            label={t("page.scheduled_jobs.system_upkeep", { count: group.upkeep.length })}
            meta={failing > 0 ? <span className="scheduled-job-error-summary">{t("page.scheduled_jobs.system_upkeep_failing", { count: failing })}</span> : undefined}
            expanded={open}
            onClick={() => setOpenUpkeep((current) => {
              const next = new Set(current);
              if (next.has(group.key)) next.delete(group.key); else next.add(group.key);
              return next;
            })}
          />
        )}
        {open && group.upkeep.map(renderJobRow)}
      </Fragment>
    );
  };

  // Search and filters appear once there are enough automations to need them.
  const showFinder = activeFilterCount > 0 || Boolean(search) || (jobsData?.total ?? 0) + filteredBindings.length > 8;

  return (
    <div className={workspaceId ? "config-group" : undefined} style={workspaceId ? undefined : { display: "flex", flexDirection: "column", gap: 16, minHeight: "100%", padding: 0 }}>

      {workspaceId ? (
        <>
          <div className="config-group-head"><h3 className="config-group-title">{t("page.scheduled_jobs.automations")}</h3></div>
          {showFinder && <SmartToolbar searchValue={search} onSearchChange={setSearch} searchPlaceholder={t("action.search")} className="w-full sm:w-56" />}
        </>
      ) : (
        <PageHeader
          title={t("page.scheduled_jobs.automations")}
          subtitle={t("page.scheduled_jobs.automations_subtitle")}
          meta={stats}
          toolbar={(
            <SmartToolbar
              searchValue={search}
              onSearchChange={setSearch}
              searchPlaceholder={t("action.search")}
              className="w-full sm:w-56"
            />
          )}
        />
      )}

      {requestedJobId && focusedJobQuery.isLoading && <LoadingSpinner size={24} />}
      {requestedJobId && focusedJobQuery.isError && <div role="alert">
        <p>{translateApiError(focusedJobQuery.error, t("page.scheduled_jobs.failed_to_load_run_detail"))}</p>
        <Button variant="ghost" size="sm" onClick={() => focusedJobQuery.refetch()}>{t("component.workflow_run_history.try_again")}</Button>
      </div>}
      {requestedJobId && fetchedJob?.execution_type === "workflow" && !showWorkflowAutomations && (
        <p role="status">{t("component.chat_mode.flows_coming_soon")}</p>
      )}

      {showFinder && <FilterBar
        activeCount={activeFilterCount}
        trailing={isFetching ? <LoadingSpinner size={14} /> : undefined}
      >
        <FilterSelect
          label={t("page.scheduled_jobs.status")}
          value={statusFilter}
          onChange={(value) => setStatusFilter(value as AutomationStatusFilter)}
          options={AUTOMATION_STATUS_OPTIONS}
          width={154}
          dropdownMinWidth={176}
        />
        {!workspaceId && (
          <FilterSelect
            label={t("page.scheduled_jobs.workspace")}
            value={workspaceFilter}
            onChange={setWorkspaceFilter}
            options={workspaceFilterOptions}
            width={196}
            dropdownMinWidth={240}
            filterable
          />
        )}
        <FilterSelect
          label={t("page.scheduled_jobs.runs_as")}
          value={agentFilter}
          onChange={setAgentFilter}
          options={agentFilterOptions}
          width={182}
          dropdownMinWidth={220}
          filterable
        />
      </FilterBar>}

      {/* ── Loading ── */}
      {(isLoading || bindingsLoading) && <div style={{ display: "flex", justifyContent: "center", padding: 48 }}><LoadingSpinner size={24} /></div>}

      {/* ── Empty ── */}
      {!workspaceId && !isLoading && !bindingsLoading && jobs.length === 0 && filteredBindings.length === 0 && (
        <EmptyState
          title={t("page.scheduled_jobs.no_automations_yet")}
          description={t("page.scheduled_jobs.schedule_recurring_tasks_daily_reports_weekly_sy")}
        />
      )}

      {workspaceId && <div className="config-group-body scheduled-automation-list scheduled-automation-list--grouped">
      {workspaceId && !isLoading && !bindingsLoading && jobs.length === 0 && filteredBindings.length === 0 && (
        <ConfigBlock><p className="config-empty">{t("page.scheduled_jobs.no_automations_yet")}</p></ConfigBlock>
      )}
      {/* Workspace-event workflow bindings share this tab with scheduled jobs. */}
      {!isLoading && !bindingsLoading && filteredBindings.map((binding) => {
        const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === binding.workflow_id);
        const runView = bindingRunView(binding);
        const enabled = runView.state === "enabled";
        const eventName = String(binding.trigger_config?.event || t("ui.workspace_event"));
        const health = healthById.get(binding.id);
        return (
          <div
            key={`binding-${binding.id}`}
            className={`scheduled-job-card${enabled ? "" : " scheduled-job-card--disabled"}`}
            style={{ padding: 0, overflow: "visible", borderRadius: 18, transform: "none" }}
          >
            <div className="scheduled-job-row scheduled-automation-grid">
              <div className="workspace-workflow-automation-icon scheduled-automation-icon-cell" aria-hidden>
                <IconFlow size={17} />
              </div>
              <div className="scheduled-job-info">
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                  <span className="scheduled-job-title">{formatUserFacingText(binding.name || workflow?.name || "Workflow automation")}</span>
                  {health?.active_experiment && <span className="workspace-workflow-automation-state">{t("ui.automation_in_experiment")}</span>}
                  {runView.labelKey && <span className="workspace-workflow-automation-state">{t(runView.labelKey)}</span>}
                </div>
                <div className="scheduled-job-meta">
                  <span className="workspace-workflow-automation-kind">{t("ui.workspace_event")}</span>
                  <span className="mono">{eventName}</span>
                  <span className="scheduled-job-separator">|</span>
                  <span>{workflow ? readableWorkflowName(workflow.name) : t("ui.missing_workflow")}</span>
                </div>
              </div>
              <div className="scheduled-automation-foot">
                <span className="scheduled-job-last-run">
                  {t("page.scheduled_jobs.last_run")} {health?.last_run_at ? relativeTime(health.last_run_at) : t("page.scheduled_jobs.never")}{automationRecord(health)}
                </span>
              </div>
              <div className="scheduled-job-actions scheduled-automation-controls" onClick={(event) => event.stopPropagation()}>
                <span className="scheduled-automation-linked-slot" />
                <Toggle
                  checked={runView.checked}
                  onChange={() => toggleBindingMut.mutate({ id: binding.id, enabled: runView.nextEnabled })}
                  disabled={!canWriteWorkspace || !runView.canToggle}
                  aria-label={`${runView.checked ? "Pause" : "Enable"} ${binding.name || workflow?.name || "workflow automation"}`}
                />
                <button
                  type="button"
                  onClick={() => runBindingMut.mutate(binding.id)}
                  disabled={!canWriteWorkspace || (runBindingMut.isPending && runBindingMut.variables === binding.id)}
                  className="btn-manor-ghost workspace-workflow-automation-action"
                  aria-label={`Run ${binding.name || workflow?.name || "workflow automation"}`}
                  title={t("page.scheduled_jobs.run_now")}
                ><IconPlay size={13} /></button>
                <button
                  type="button"
                  onClick={() => openBindingModal(binding)}
                  disabled={!canWriteWorkspace}
                  className="btn-manor-ghost workspace-workflow-automation-action"
                  aria-label={`Edit ${binding.name || workflow?.name || "workflow automation"}`}
                  title={t("action.edit")}
                ><IconEdit size={13} /></button>
                <button
                  type="button"
                  onClick={() => setDeleteBindingTarget(binding.id)}
                  disabled={!canWriteWorkspace}
                  className="btn-manor-ghost workspace-workflow-automation-action is-delete"
                  aria-label={`Delete ${binding.name || workflow?.name || "workflow automation"}`}
                  title={t("action.delete")}
                ><IconTrash size={13} /></button>
              </div>
              <span className="scheduled-automation-expand" />
            </div>
          </div>
        );
      })}

      {/* ── Job Cards ── */}
      {!isLoading && workspaceId && jobGroups.map((group) => renderJobGroupRows(group))}
      {workspaceId && <ConfigAddRow label={t("page.scheduled_jobs.add_automation")} onClick={openNewModal} disabled={!canWriteWorkspace} />}
      </div>}

      {!isLoading && !workspaceId && jobGroups.map((group) => (
        <section key={group.key} className="config-group">
          {group.workspace ? (
            <div className="config-group-head">
              <h3 className="config-group-title">
                <Link className="scheduled-group-link" to={`/workspaces/${group.key}`}>{group.workspace.name}</Link>
              </h3>
            </div>
          ) : !group.key && (
            <div className="config-group-head"><h3 className="config-group-title">{t("page.scheduled_jobs.personal")}</h3></div>
          )}
          <div className="config-group-body scheduled-automation-list scheduled-automation-list--grouped">
            {renderJobGroupRows(group)}
            {group.workspace ? (
              <ConfigAddRow label={t("page.scheduled_jobs.add_automation")} onClick={() => navigate(`/workspaces/${group.key}?tab=workflows`)} />
            ) : !group.key && (
              <ConfigAddRow label={t("page.scheduled_jobs.add_automation")} onClick={openNewModal} />
            )}
          </div>
        </section>
      ))}
      {!isLoading && !workspaceId && !jobGroups.some((group) => !group.key) && (
        <section className="config-group">
          <div className="config-group-head"><h3 className="config-group-title">{t("page.scheduled_jobs.personal")}</h3></div>
          <div className="config-group-body scheduled-automation-list scheduled-automation-list--grouped">
            <ConfigAddRow label={t("page.scheduled_jobs.add_automation")} onClick={openNewModal} />
          </div>
        </section>
      )}

      {!isLoading && !bindingsLoading && totalPages > 1 && (
        <div className="scheduled-automation-pagination">
          <span>{t("page.scheduled_jobs.showing_range", {
            start: Math.min((page - 1) * PAGE_SIZE + 1, jobsData?.total ?? 0),
            end: Math.min(page * PAGE_SIZE, jobsData?.total ?? 0),
            total: jobsData?.total ?? 0,
          })}</span>
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </div>
      )}

      {/* ── Modal ── */}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) deleteMut.mutate(deleteTarget); setDeleteTarget(null); }}
        title={t("page.scheduled_jobs.delete_automation")}
        message={t("page.scheduled_jobs.this_will_permanently_delete_this_automation_and")}
        confirmLabel={t("action.delete")}
        danger
      />
      <ConfirmDialog
        open={!!deleteBindingTarget}
        onClose={() => setDeleteBindingTarget(null)}
        onConfirm={() => { if (deleteBindingTarget) deleteBindingMut.mutate(deleteBindingTarget); }}
        title={t("ui.delete_workflow_automation")}
        message="This removes the workflow from this workspace event. The workflow itself is kept."
        confirmLabel={t("action.delete")}
        danger
      />

      <Modal open={modalOpen} onClose={closeModal} title={editJob || editBinding ? t("page.scheduled_jobs.edit_automation") : t("page.scheduled_jobs.new_automation")} maxWidth="560px"
        footer={
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, width: "100%" }}>
            <button className="btn-manor-ghost" onClick={closeModal} style={{ fontSize: 12, height: 34, padding: "0 16px" }}>{t("action.cancel")}</button>
            <button
              className="btn-manor"
              onClick={submit}
              disabled={
                !canWriteWorkspace
                || !fName.trim()
                || workflowSelectionMissing
                || (fKind === "workflow_event" && !fEvent.trim())
              }
              style={{ fontSize: 12, height: 34, padding: "0 20px" }}
            >
              {createMut.isPending || updateMut.isPending || createBindingMut.isPending || updateBindingMut.isPending
                ? t("page.task_collections.saving")
                : editJob || editBinding ? t("action.save") : t("action.create")}
            </button>
          </div>
        }>
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          {/* Name */}
          <div>
            <label className="manor-label">{t("page.task_collections.name")}</label>
            <input className="manor-input" value={fName} onChange={(e) => setFName(e.target.value)} placeholder={t("page.scheduled_jobs.e_g_daily_report")} style={{ width: "100%", fontSize: 14, fontWeight: 500, height: 40 }} autoFocus />
          </div>

          {/* Outside a Workspace there is one kind, so there is nothing to choose. */}
          {(workspaceId || editJob || editBinding) && <div>
            <label className="manor-label">{t("ui.automation_action")}</label>
            <TabSwitcher
              value={fKind}
              onChange={(value) => {
                if (editJob || editBinding) return;
                setFKind(value as AutomationKind);
              }}
              size="sm"
              wrap
              tabs={(editJob || editBinding)
                ? workflowAutomationTabs.filter((tab) => tab.key === fKind)
                : workflowAutomationTabs.filter((tab) => workspaceId || tab.key === "agent_schedule")}
            />
            {(editJob || editBinding) && (
              <p className="workspace-workflow-automation-help">{t("ui.automation_type_fixed")}</p>
            )}
          </div>}

          {fKind !== "agent_schedule" && (
            <div>
              <label className="manor-label">{t("ui.workspace_workflow")}</label>
              {workflowBindingOptions.length > 0 ? (
                <Select
                  value={selectedWorkflowBinding}
                  onChange={(value) => {
                    if (value.startsWith("legacy:")) {
                      setFWorkspaceBinding("");
                      setFWorkflow(value.slice("legacy:".length));
                      return;
                    }
                    const binding = workspaceWorkflowBindings.find((item) => item.id === value);
                    setFWorkspaceBinding(binding?.id || "");
                    setFWorkflow(binding?.workflow_id || "");
                  }}
                  filterable
                  placeholder={t("ui.select_an_attached_workflow")}
                  options={workflowBindingOptions}
                />
              ) : (
                <div className="workspace-workflow-automation-empty">
                  <span>Attach a workflow to this workspace before creating a workflow automation.</span>
                  {workspaceId && <Link to={`/workspaces/${workspaceId}?tab=workflows`}>{t("ui.manage_workspace_workflows")}</Link>}
                </div>
              )}
              <p className="workspace-workflow-automation-help">Automations trigger the selected workspace binding; the workflow can also run manually or from an API.</p>
            </div>
          )}

          {fKind === "workflow_event" && (
            <div>
              <label className="manor-label">{t("ui.workspace_event")}</label>
              <input
                className="manor-input mono"
                value={fEvent}
                onChange={(event) => setFEvent(event.target.value)}
                placeholder="task.approval_decision"
                aria-label={t("ui.workspace_event_name")}
                style={{ width: "100%" }}
              />
              <div className="workspace-workflow-event-suggestions" aria-label={t("ui.common_workspace_events")}>
                {WORKSPACE_EVENT_SUGGESTIONS.map((event) => (
                  <button key={event.value} type="button" onClick={() => setFEvent(event.value)} title={event.value}>
                    {event.label}
                  </button>
                ))}
              </div>
              <p className="workspace-workflow-automation-help">{t("ui.the_event_payload_is_available_to_workflow_nodes")}<span className="mono">trigger</span>.</p>
            </div>
          )}

          {/* When should this run? */}
          {fKind !== "workflow_event" && <div>
            <label className="manor-label">{t("page.scheduled_jobs.when_should_this_run")}</label>
            {/* Frequency selector */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginBottom: 12 }}>
              {([
                { key: "hourly", label: t("page.workspace_detail.hourly") }, { key: "daily", label: t("page.workspace_detail.daily") },
                { key: "weekly", label: t("page.workspace_detail.weekly") }, { key: "monthly", label: t("page.workspace_detail.monthly") },
                { key: "interval", label: t("page.scheduled_jobs.every_x_min") }, { key: "once", label: t("page.scheduled_jobs.one_time") },
              ] as const).map((f) => {
                const active = fFreq === f.key;
                return (
                  <button key={f.key} type="button" onClick={() => setFFreq(f.key)}
                    style={{
                      padding: "6px 14px", borderRadius: 10, fontSize: 12, fontWeight: 600, cursor: "pointer",
                      border: active ? "1.5px solid var(--text-strong)" : "0.5px solid var(--border-subtle)",
                      background: active ? "#f1f3f9" : "#fff", color: active ? "#5a55a6" : "#8e8e93",
                      transition: "all 0.12s",
                    }}>
                    {f.label}
                  </button>
                );
              })}
            </div>

            {/* Time picker (for hourly/daily/weekly/monthly) */}
            {fFreq !== "interval" && fFreq !== "once" && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 500 }}>
                  {fFreq === "hourly" ? t("page.scheduled_jobs.at_minute") : t("page.scheduled_jobs.at")}
                </span>
                {fFreq !== "hourly" && (
                  <Select
                    value={String(fHour)}
                    onChange={(v) => setFHour(Number(v))}
                    options={Array.from({ length: 24 }, (_, i) => ({ value: String(i), label: i === 0 ? "12 AM" : i < 12 ? `${i} AM` : i === 12 ? "12 PM" : `${i - 12} PM` }))}
                    style={{ width: 90 }}
                  />
                )}
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>:</span>
                <Select
                  value={String(fMinute)}
                  onChange={(v) => setFMinute(Number(v))}
                  options={[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map((m) => ({ value: String(m), label: String(m).padStart(2, "0") }))}
                  style={{ width: 70 }}
                />
              </div>
            )}

            {/* Day of week picker (weekly) */}
            {fFreq === "weekly" && (
              <div style={{ marginBottom: 10 }}>
                <span style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 500, display: "block", marginBottom: 6 }}>{t("page.scheduled_jobs.on_these_days")}</span>
                <div style={{ display: "flex", gap: 4 }}>
                  {DAYS_OF_WEEK.map((d) => {
                    const active = fDays.includes(d.key);
                    return (
                      <button key={d.key} type="button"
                        onClick={() => setFDays(active ? fDays.filter((x) => x !== d.key) : [...fDays, d.key].sort())}
                        style={{
                          width: 38, height: 34, borderRadius: 8, fontSize: 11, fontWeight: 600, cursor: "pointer",
                          border: active ? "1.5px solid var(--text-strong)" : "0.5px solid var(--border-subtle)",
                          background: active ? "#f1f3f9" : "#fff", color: active ? "#5a55a6" : "#8e8e93",
                          transition: "all 0.12s",
                        }}>
                        {d.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Day of month (monthly) */}
            {fFreq === "monthly" && (
              <div style={{ marginBottom: 10, display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 500 }}>{t("page.scheduled_jobs.on_day")}</span>
                <Select
                  value={String(fDom)}
                  onChange={(v) => setFDom(Number(v))}
                  options={Array.from({ length: 28 }, (_, i) => ({ value: String(i + 1), label: String(i + 1) }))}
                  style={{ width: 70 }}
                />
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("page.scheduled_jobs.of_each_month")}</span>
              </div>
            )}

            {/* Interval picker */}
            {fFreq === "interval" && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                {INTERVAL_OPTIONS.map((o) => {
                  const active = fIntervalSec === o.s;
                  return (
                    <button key={o.s} type="button" onClick={() => setFIntervalSec(o.s)}
                      style={{
                        padding: "7px 14px", borderRadius: 10, fontSize: 12, fontWeight: 600, cursor: "pointer",
                        border: active ? "2px solid #4f7d75" : "1px solid rgba(231,229,228,0.6)",
                        background: active ? "#f2f6f5" : "#fff", color: active ? "#436b65" : "#8e8e93",
                        transition: "all 0.12s",
                      }}>
                      {o.label}
                    </button>
                  );
                })}
              </div>
            )}

            {/* One-time datetime */}
            {fFreq === "once" && (
              <DateTimePicker mode="datetime" value={fRunAt} onChange={setFRunAt} placeholder={t("page.scheduled_jobs.pick_date_and_time")} dropDirection="up" />
            )}

            {/* Preview */}
            {fFreq !== "once" && fFreq !== "interval" && (
              <div style={{ marginTop: 8, padding: "6px 10px", borderRadius: 8, background: "var(--surface-muted)", border: "0.5px solid rgba(29, 29, 31,0.06)", fontSize: 12, color: "var(--text-muted)" }}>
                <span style={{ fontWeight: 500, color: "var(--text-strong)" }}>{describeCron(buildCron(fFreq, fHour, fMinute, fDays, fDom))}</span>
                <span style={{ color: "var(--text-muted)" }}> · {fTimezone}</span>
              </div>
            )}
          </div>}

          {/* What to do */}
          {fKind === "agent_schedule" && <div>
            <label className="manor-label">{t("page.scheduled_jobs.what_should_the_agent_do")}</label>
            <textarea className="manor-textarea" value={fMessage} onChange={(e) => setFMessage(e.target.value)} rows={3}
              placeholder={t("page.scheduled_jobs.e_g_generate_a_summary_of_all_tasks_completed_th")}
              style={{ width: "100%", fontSize: 13, lineHeight: 1.6 }} />
            <p style={{ fontSize: 10, color: "var(--text-muted)", margin: "6px 0 0", display: "flex", alignItems: "center", gap: 4 }}>
              <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" /></svg>
              {t("page.scheduled_jobs.a_step_by_step_execution_procedure_will_be_auto")}
            </p>
          </div>}

          {/* Agent */}
          {fKind === "agent_schedule" && <div>
            <label className="manor-label">{t("page.scheduled_jobs.execute_with")}</label>
            <Select value={fAgent} onChange={setFAgent} filterable
              options={[{ value: "", label: t("page.scheduled_jobs.no_agent_create_task_only") }, { value: MANOR_AGENT_ID, label: MANOR_AGENT_NAME }, ...(agents as Agent[]).map((a) => ({ value: a.id, label: a.name }))]} />
          </div>}

          {/* Timezone */}
          {fKind !== "workflow_event" && <div>
            <label className="manor-label">{t("page.scheduled_jobs.timezone")}</label>
            <Select value={fTimezone} onChange={setFTimezone} filterable
              options={TIMEZONE_OPTIONS.includes(fTimezone) ? TIMEZONE_OPTIONS : [fTimezone, ...TIMEZONE_OPTIONS]} />
          </div>}
        </div>
      </Modal>
    </div>
  );
}
