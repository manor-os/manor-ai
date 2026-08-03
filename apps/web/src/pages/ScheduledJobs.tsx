import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../lib/api";
import { useToastStore } from "../stores/toast";
import type { Agent, Workspace } from "../lib/types";
import { relativeTime } from "../lib/format";
import { MANOR_AGENT_ID, MANOR_AGENT_NAME, isMasterAgent } from "../lib/constants";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import Select from "../components/ui/Select";
import DateTimePicker from "../components/ui/DateTimePicker";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import UserAvatar from "../components/ui/UserAvatar";
import Toggle from "../components/ui/Toggle";
import StatPill from "../components/ui/StatPill";
import TabSwitcher from "../components/ui/TabSwitcher";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import SmartToolbar from "../components/ui/SmartToolbar";
import { FilterBar, FilterSelect } from "../components/ui/FilterBar";
import Pagination from "../components/ui/Pagination";
import { t } from "../lib/i18n";
import { formatUserFacingText } from "../lib/taskDisplay";
import { stringifyJobRunResult, summarizeJobRunResult } from "../lib/jobRunResult";
import {
  IconEdit, IconTrash, IconClock, IconChevronRight, IconPlay, IconFlow, IconWorkspace,
} from "../components/icons";

/* ── types ── */
interface ScheduledJob {
  id: string; job_id: string; entity_id: string; name: string; job_type: string;
  workspace_id?: string;
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

const TYPE_META: Record<string, { label: string; color: string; icon: string }> = {
  cron:     { label: t("page.scheduled_jobs.recurring"), color: "#6d6fb2", icon: "M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" },
  interval: { label: t("page.scheduled_jobs.interval"), color: "#4f7d75", icon: "M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" },
  once:     { label: t("page.scheduled_jobs.one_time"), color: "#cf9b44", icon: "M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z" },
};

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
  { key: "agent_schedule", label: "Schedule an agent" },
  { key: "workflow_schedule", label: "Schedule a workflow" },
  { key: "workflow_event", label: "Run workflow on event" },
];

const WORKSPACE_EVENT_SUGGESTIONS = [
  { value: "task.approval_decision", label: "Task approved or changes requested" },
  { value: "task.comment", label: "Task receives a comment" },
  { value: "workspace.channel_attached", label: "Channel connected" },
  { value: "workspace.channel_updated", label: "Channel updated" },
  { value: "workspace_work_batch.completed", label: "Work batch completed" },
  { value: "workspace_work_batch.stalled", label: "Work batch stalled" },
  { value: "workspace_agent.task_created", label: "Workspace agent creates a task" },
  { value: "workspace_agent.task_runtime_updated", label: "Task runtime changes" },
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

function summarizeAutomationError(error?: string | null, maxLength = 120): string {
  const friendly = formatUserFacingText(
    String(error || "")
      .replace(/^Agent execution failed:\s*/i, "")
      .replace(/\s+/g, " ")
      .trim(),
  );
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
  const t = `${Number(hr) % 12 || 12}:${min.padStart(2, "0")} ${Number(hr) >= 12 ? "PM" : "AM"}`;
  if (hr === "*") return `Every hour at :${min.padStart(2, "0")}`;
  if (dow !== "*") {
    const dayMap: Record<string, string> = { "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "1-5": "Weekdays" };
    const dayStr = dow.split(",").map((d) => dayMap[d] || d).join(", ");
    return `${dayStr} at ${t}`;
  }
  if (dom !== "*") return `${dom}${dom === "1" ? "st" : dom === "2" ? "nd" : dom === "3" ? "rd" : "th"} of each month at ${t}`;
  return `Daily at ${t}`;
}

function fmtSchedule(job: ScheduledJob): string {
  const tz = job.timezone && job.timezone !== "UTC" ? ` (${job.timezone})` : "";
  if (job.job_type === "cron" && job.cron_expr) return `${describeCron(job.cron_expr)}${tz}`;
  const intervalSeconds = job.interval_seconds ?? job.every_seconds;
  if ((job.job_type === "interval" || job.schedule_kind === "every") && intervalSeconds) {
    const s = intervalSeconds;
    const opt = INTERVAL_OPTIONS.find((o) => o.s === s);
    if (opt) return `Every ${opt.label}`;
    return s < 60 ? `Every ${s}s` : s < 3600 ? `Every ${Math.floor(s / 60)} min` : `Every ${Math.floor(s / 3600)}h`;
  }
  if (job.job_type === "once" && job.run_at) return `${new Date(job.run_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}${tz}`;
  return "--";
}

/* ── Run detail panel (lazy-loaded, inline) ── */
function RunDetail({ jobId, runId }: { jobId: string; runId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["job-run-detail", jobId, runId],
    queryFn: () => api.jobs.runDetail(jobId, runId),
  });

  if (isLoading) {
    return (
      <div style={{ padding: "10px 0", display: "flex", alignItems: "center", gap: 8 }}>
        <LoadingSpinner size={14} /> <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.scheduled_jobs.loading_run")}</span>
      </div>
    );
  }
  if (error || !data) {
    return <div style={{ fontSize: 11, color: "#d65f59", padding: "6px 0" }}>{t("page.scheduled_jobs.failed_to_load_run_detail")}</div>;
  }

  const { run, task, agent_execution: ae } = data;
  const resultText = stringifyJobRunResult(run.result);
  const resultSummary = summarizeJobRunResult(run.result);
  const tokens = ae?.token_usage || {};
  const tokenTotal = (tokens.input_tokens || tokens.prompt_tokens || 0)
                  + (tokens.output_tokens || tokens.completion_tokens || 0);

  const Section = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 9, fontWeight: 700, color: "#a8a29e", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4 }}>{label}</div>
      {children}
    </div>
  );
  const Pre = ({ text }: { text: string }) => (
    <pre style={{
      margin: 0, padding: "8px 10px", borderRadius: 8,
      background: "rgba(250,250,249,0.7)", border: "1px solid rgba(28,25,23,0.06)",
      fontSize: 11, color: "#44403c", whiteSpace: "pre-wrap", wordBreak: "break-word",
      maxHeight: 220, overflow: "auto", fontFamily: "ui-monospace, monospace",
    }}>{text}</pre>
  );

  return (
    <div style={{ padding: "10px 14px 12px", background: "rgba(250,250,249,0.4)", borderRadius: 10, marginTop: 6 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", fontSize: 10, color: "#78716c", flexWrap: "wrap" }}>
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
        <Section label={t("page.scheduled_jobs.execution_result")}>
          {resultSummary.length > 0 && (
            <div style={{
              display: "flex", flexDirection: "column", gap: 3,
              marginBottom: 6, fontSize: 11, color: "#44403c",
            }}>
              {resultSummary.map((line, i) => (
                <div key={i} style={{
                  padding: "4px 7px", borderRadius: 7,
                  background: "#fff", border: "1px solid rgba(226,232,240,0.6)",
                }}>
                  {line}
                </div>
              ))}
            </div>
          )}
          <Pre text={resultText} />
        </Section>
      )}
      {!resultText && !task && !ae && (
        <p style={{ fontSize: 11, color: "#a8a29e", margin: "10px 0 0" }}>
          {t("page.scheduled_jobs.no_detailed_result_recorded")}
        </p>
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
      {ae?.output_message && (
        <Section label={t("page.scheduled_jobs.agent_output")}>
          <Pre text={ae.output_message} />
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
            <p style={{ fontSize: 10, color: "#a8a29e", margin: "4px 0 0", fontStyle: "italic" }}>
              {t("page.scheduled_jobs.supervisor")} {task.supervisor_verdict.reason}
            </p>
          )}
        </Section>
      )}
      {ae?.tools_used?.length > 0 && (
        <Section label={t("page.scheduled_jobs.tools_used")}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {ae.tools_used.map((t: any, i: number) => (
              <span key={i} style={{ fontSize: 10, padding: "2px 8px", borderRadius: 12, background: "#fff", border: "1px solid rgba(28,25,23,0.06)", color: "#57534e", fontFamily: "ui-monospace, monospace" }}>
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
                : step.type === "ai_supervisor_verdict" ? "#6d6fb2"
                : step.type === "ai_agent_turn" ? "#4f7d75"
                : "#a8a29e";
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
                  <span style={{ fontWeight: 600, color: "#57534e", minWidth: 130, fontFamily: "ui-monospace, monospace", fontSize: 10 }}>
                    {step.type}
                  </span>
                  <span style={{ color: "#44403c", flex: 1, wordBreak: "break-word" }}>
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
          <a href={`/tasks/${task.id}`} style={{ fontSize: 11, color: "#6d6fb2", textDecoration: "none", fontWeight: 500 }}>
            #{task.id.slice(-6)} · {task.title} · {task.status}
          </a>
        </Section>
      )}
    </div>
  );
}


interface ScheduledJobsProps {
  workspaceId?: string;
  workflowsEnabled?: boolean;
}

/* ── main ── */
export default function ScheduledJobs({ workspaceId, workflowsEnabled = true }: ScheduledJobsProps) {
  const qc = useQueryClient();
  const toast = useToastStore();
  const showWorkflowAutomations = workspaceId ? workflowsEnabled : true;
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const [statusFilter, setStatusFilter] = useState<AutomationStatusFilter>("all");
  const [agentFilter, setAgentFilter] = useState("all");
  const [workspaceFilter, setWorkspaceFilter] = useState(workspaceId || "all");
  const [page, setPage] = useState(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
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
  const allJobs: ScheduledJob[] = (jobsData?.items as ScheduledJob[] | undefined) ?? [];
  const jobs = showWorkflowAutomations ? allJobs : allJobs.filter((job) => job.execution_type !== "workflow");
  const totalPages = Math.max(1, Math.ceil((jobsData?.total ?? 0) / PAGE_SIZE));

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);
  const { data: workflows = [] } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
    enabled: !!workspaceId && showWorkflowAutomations,
  });
  const bindingsQueryKey = useMemo(() => ["workflow-bindings", workspaceId || "none"], [workspaceId]);
  const { data: bindings = [], isLoading: bindingsLoading } = useQuery({
    queryKey: bindingsQueryKey,
    queryFn: () => workspaceId ? api.workflows.listBindings({ workspace_id: workspaceId }) : Promise.resolve([]),
    enabled: !!workspaceId && showWorkflowAutomations,
  });
  const { data: agents = [] } = useQuery({ queryKey: ["agents"], queryFn: () => api.agents.list() });
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
  const { data: runs = [] } = useQuery({ queryKey: ["job-runs", expandedId], queryFn: () => expandedId ? api.jobs.runs(expandedId) : Promise.resolve([]), enabled: !!expandedId });
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
      // Auto-refresh runs panel for ~10s so the new row appears as the
      // worker picks it up — JobRun rows often land 1-3s after dispatch.
      let n = 0;
      const t = setInterval(() => {
        qc.invalidateQueries({ queryKey: ["job-runs", id] });
        if (++n > 5) clearInterval(t);
      }, 2000);
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
      const enabled = binding.enabled && binding.status === "active";
      if (statusFilter === "enabled" && !enabled) return false;
      if (statusFilter === "paused" && enabled) return false;
      if (statusFilter === "attention") return false;
      const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === binding.workflow_id);
      return !query
        || `${binding.name || ""} ${workflow?.name || ""} ${binding.trigger_config?.event || ""}`
          .toLowerCase()
          .includes(query);
    });
  }, [agentFilter, deferredSearch, page, statusFilter, workflowBindings, workflows]);
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
      enabled: matching.filter((binding) => binding.enabled && binding.status === "active").length,
    };
  }, [agentFilter, deferredSearch, workflowBindings, workflows]);
  const automationCount = (jobsData?.summary_total ?? 0) + bindingSummary.total;
  const enabledN = (jobsData?.enabled_total ?? 0) + bindingSummary.enabled;
  const attentionN = jobsData?.attention_total ?? 0;
  const stats = automationCount > 0 ? (
    <div className="flex flex-wrap items-center gap-2">
      <StatPill label={t("page.scheduled_jobs.total_count", { count: automationCount })} />
      {enabledN > 0 && (
        <StatPill
          label={t("page.scheduled_jobs.enabled_count", { count: enabledN })}
          color="var(--accent)"
          bg="var(--accent-soft)"
        />
      )}
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

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, minHeight: "100%", padding: 0 }}>

      <PageHeader
        title={t("page.scheduled_jobs.automations")}
        subtitle={workspaceId
          ? showWorkflowAutomations
            ? "Schedule agents, or use an attached workflow on a schedule or workspace event."
            : "Schedule agents to run recurring or one-time workspace work."
          : t("page.scheduled_jobs.automations_subtitle")}
        meta={stats}
        toolbar={(
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("action.search")}
            className="w-full sm:w-56"
          />
        )}
        actions={<PageHeaderAddButton label={t("page.scheduled_jobs.add_automation")} onClick={openNewModal} />}
      />

      <FilterBar
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
      </FilterBar>

      {/* ── Loading ── */}
      {(isLoading || bindingsLoading) && <div style={{ display: "flex", justifyContent: "center", padding: 48 }}><LoadingSpinner size={24} /></div>}

      {/* ── Empty ── */}
      {!isLoading && !bindingsLoading && jobs.length === 0 && filteredBindings.length === 0 && (
        <div className="glass-panel" style={{ textAlign: "center", padding: "56px 24px", borderRadius: 24 }}>
          <div style={{ width: 56, height: 56, borderRadius: 16, background: "linear-gradient(135deg, rgba(109,111,178,0.08), rgba(79,125,117,0.08))", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
            <IconClock size={28} style={{ color: "#6d6fb2" }} />
          </div>
          <p style={{ fontSize: 16, fontWeight: 700, color: "#292524", margin: "0 0 4px" }}>{t("page.scheduled_jobs.no_automations_yet")}</p>
          <p style={{ fontSize: 13, color: "#a8a29e", margin: "0 0 16px", maxWidth: 320, marginLeft: "auto", marginRight: "auto" }}>
            {t("page.scheduled_jobs.schedule_recurring_tasks_daily_reports_weekly_sy")}
          </p>
          <PageHeaderAddButton label={t("page.scheduled_jobs.add_automation")} onClick={openNewModal} />
        </div>
      )}

      {!isLoading && !bindingsLoading && (jobs.length > 0 || filteredBindings.length > 0) && (
        <div className="scheduled-automation-list-header" aria-hidden="true">
          <span />
          <span>{t("page.scheduled_jobs.automation")}</span>
          <span>{t("page.scheduled_jobs.runs_as")}</span>
          <span>{t("page.scheduled_jobs.last_run")}</span>
          <span>{t("page.scheduled_jobs.controls")}</span>
          <span />
        </div>
      )}

      {/* Workspace-event workflow bindings share this tab with scheduled jobs. */}
      {!isLoading && !bindingsLoading && filteredBindings.map((binding) => {
        const workflow = (workflows as WorkflowSummary[]).find((item) => item.id === binding.workflow_id);
        const enabled = binding.enabled && binding.status === "active";
        const eventName = String(binding.trigger_config?.event || "workspace event");
        return (
          <div
            key={`binding-${binding.id}`}
            className={`glass-card scheduled-job-card${enabled ? "" : " scheduled-job-card--disabled"}`}
            style={{ padding: 0, overflow: "visible", borderRadius: 18, transform: "none" }}
          >
            <div className="scheduled-job-row scheduled-automation-grid">
              <div className="workspace-workflow-automation-icon scheduled-automation-icon-cell" aria-hidden>
                <IconFlow size={17} />
              </div>
              <div className="scheduled-job-info">
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                  <span className="scheduled-job-title">{formatUserFacingText(binding.name || workflow?.name || "Workflow automation")}</span>
                  {!enabled && <span className="workspace-workflow-automation-state">Paused</span>}
                </div>
                <div className="scheduled-job-meta">
                  <span className="workspace-workflow-automation-kind">Workspace event</span>
                  <span className="mono">{eventName}</span>
                  <span className="scheduled-job-separator">|</span>
                  <span>{workflow?.name || "Missing workflow"}</span>
                </div>
              </div>
              <div className="scheduled-automation-executor">
                <div className="workspace-workflow-automation-chip">
                  <IconFlow size={13} />
                  <span>{workflow?.name || "Workflow"}</span>
                </div>
              </div>
              <span className="scheduled-job-last-run">{t("page.scheduled_jobs.never")}</span>
              <div className="scheduled-job-actions scheduled-automation-controls" onClick={(event) => event.stopPropagation()}>
                <span className="scheduled-automation-linked-slot" />
                <Toggle
                  checked={enabled}
                  onChange={() => toggleBindingMut.mutate({ id: binding.id, enabled: !enabled })}
                  aria-label={`${enabled ? "Pause" : "Enable"} ${binding.name || workflow?.name || "workflow automation"}`}
                />
                <button
                  type="button"
                  onClick={() => runBindingMut.mutate(binding.id)}
                  disabled={!enabled || (runBindingMut.isPending && runBindingMut.variables === binding.id)}
                  className="btn-manor-ghost workspace-workflow-automation-action"
                  aria-label={`Run ${binding.name || workflow?.name || "workflow automation"}`}
                  title={enabled ? t("page.scheduled_jobs.run_now") : t("page.scheduled_jobs.enable_first")}
                ><IconPlay size={13} /></button>
                <button
                  type="button"
                  onClick={() => openBindingModal(binding)}
                  className="btn-manor-ghost workspace-workflow-automation-action"
                  aria-label={`Edit ${binding.name || workflow?.name || "workflow automation"}`}
                  title={t("action.edit")}
                ><IconEdit size={13} /></button>
                <button
                  type="button"
                  onClick={() => setDeleteBindingTarget(binding.id)}
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
      {!isLoading && jobs.length > 0 && jobs.map((job) => {
        const exp = expandedId === job.id;
        const m = TYPE_META[job.job_type] || TYPE_META.cron;
        const ag = job.agent_id && !isMasterAgent(job.agent_id) ? (agents as Agent[]).find((a) => a.id === job.agent_id) : null;
        const isM = isMasterAgent(job.agent_id);
        const hasErr = job.consecutive_errors > 0;
        const lastError = summarizeAutomationError(job.last_error);
        const sourceWorkspace = job.workspace_id
          ? workspaceById.get(job.workspace_id)
          : undefined;
        const msg = (job as any).payload_message || job.payload?.message;
        const msgSummary = summarizeAutomationMessage(msg);
        const jobWorkflow = job.execution_type === "workflow"
          ? (workflows as WorkflowSummary[]).find((item) => item.id === job.execution_target?.workflow_id)
          : null;

        return (
          <div key={job.id} className={`glass-card scheduled-job-card${job.enabled ? "" : " scheduled-job-card--disabled"}`} style={{
            padding: 0, overflow: "visible", borderRadius: 8, transform: "none",
          }}>
            {/* Main row */}
            <div onClick={() => setExpandedId(exp ? null : job.id)}
              className="scheduled-job-row scheduled-automation-grid"
              style={{ cursor: "pointer", transition: "background 0.15s", borderRadius: exp ? "8px 8px 0 0" : 8 }}>

              {/* Type icon box */}
              <div className="scheduled-automation-icon-cell" style={{ width: 38, height: 38, borderRadius: 12, background: `${m.color}08`, border: `1.5px solid ${m.color}15`, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke={m.color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d={m.icon} /></svg>
              </div>

              {/* Info */}
              <div className="scheduled-job-info" style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                  <span className="scheduled-job-title" style={{ fontSize: 14, fontWeight: 600, color: "#0f172a" }}>{formatUserFacingText(job.name)}</span>
                  {!job.enabled && (
                    <span className="workspace-workflow-automation-state">
                      {t("page.workspaces.filter_paused")}
                    </span>
                  )}
                </div>
                <div className="scheduled-job-meta" style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "#78716c" }}>
                  <span className="scheduled-job-kind" style={{ fontWeight: 600, color: m.color, padding: "1px 7px", borderRadius: 6, background: `${m.color}08` }}>{m.label}</span>
                  {!workspaceId && job.workspace_id && (
                    <>
                      <span className="scheduled-job-separator" style={{ color: "#e7e5e4" }}>|</span>
                      <Link
                        to={`/workspaces/${job.workspace_id}`}
                        onClick={(event) => event.stopPropagation()}
                        title={sourceWorkspace?.name || "Workspace"}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          maxWidth: 220,
                          color: "inherit",
                          textDecoration: "none",
                          overflow: "hidden",
                        }}
                      >
                        <IconWorkspace size={12} style={{ flexShrink: 0 }} />
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {sourceWorkspace?.name || t("page.workspaces.workspace")}
                        </span>
                      </Link>
                    </>
                  )}
                  <span style={{ fontFamily: "monospace" }}>{fmtSchedule(job)}</span>
                  {jobWorkflow && <><span className="scheduled-job-separator" style={{ color: "#e7e5e4" }}>|</span><span>{jobWorkflow.name}</span></>}
                  {msgSummary && <><span className="scheduled-job-separator" style={{ color: "#e7e5e4" }}>|</span><span title={msgSummary} style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontStyle: "italic" }}>{msgSummary}</span></>}
                </div>
                {hasErr && (
                  <div
                    className="scheduled-job-error-summary"
                    title={job.last_error || t("page.scheduled_jobs.last_run_failed")}
                  >
                    <span>{t("page.scheduled_jobs.last_run_failed")}</span>
                    {lastError && <span className="scheduled-job-error-message">{lastError}</span>}
                  </div>
                )}
              </div>

              <div className="scheduled-automation-executor">
                {jobWorkflow ? (
                  <div className="workspace-workflow-automation-chip">
                    <IconFlow size={13} />
                    <span>{jobWorkflow.name}</span>
                  </div>
                ) : (isM || ag) ? (
                  <div className="scheduled-job-agent-chip" style={{ display: "flex", alignItems: "center", gap: 5, padding: "4px 10px 4px 5px", borderRadius: 20, background: "rgba(250,250,249,0.8)", border: "1px solid rgba(28,25,23,0.06)", flexShrink: 0 }}>
                    <UserAvatar type={isM ? "manor" : "agent"} name={ag?.name} avatarUrl={ag?.avatar_url} seed={ag?.id} size={20} />
                    <span className="scheduled-job-agent-name" style={{ fontSize: 11, color: "#57534e", fontWeight: 500 }}>{isM ? MANOR_AGENT_NAME : ag?.name || t("page.workspace_detail.agent")}</span>
                  </div>
                ) : (
                  <span className="scheduled-automation-empty-value">--</span>
                )}
              </div>

              {/* Last run */}
              <span className="scheduled-job-last-run">
                {job.last_run_at ? relativeTime(job.last_run_at) : t("page.scheduled_jobs.never")}
              </span>

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
                      border: "1px solid rgba(28,25,23,0.06)",
                      background: "#fafaf9",
                      color: "#57534e",
                      fontSize: 11,
                      fontWeight: 750,
                      textDecoration: "none",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {t("page.scheduled_jobs.linked_task")}
                  </Link>
                  )}
                </span>
                <Toggle
                  checked={job.enabled}
                  onChange={() => toggleMut.mutate({ id: job.id, enabled: !job.enabled })}
                  aria-label={`${job.enabled ? "Pause" : "Enable"} ${job.name}`}
                />
                <button
                  onClick={() => runNowMut.mutate(job.id)}
                  disabled={!job.enabled || (runNowMut.isPending && runNowMut.variables === job.id)}
                  className="btn-manor-ghost"
                  style={{ width: 28, height: 28, padding: 0, borderRadius: 8, color: "#4f7d75" }}
                  title={job.enabled ? t("page.scheduled_jobs.run_now") : t("page.scheduled_jobs.enable_first")}
                ><IconPlay size={13} /></button>
                <button onClick={() => openModal(job)} className="btn-manor-ghost" style={{ width: 28, height: 28, padding: 0, borderRadius: 8 }} title={t("action.edit")}><IconEdit size={13} /></button>
                <button onClick={() => setDeleteTarget(job.job_id)} className="btn-manor-ghost" style={{ width: 28, height: 28, padding: 0, borderRadius: 8, color: "#a8a29e" }} title={t("action.delete")}><IconTrash size={13} /></button>
              </div>

              <IconChevronRight className="scheduled-automation-expand" size={12} style={{ color: "#d6d3d1", transition: "transform 0.2s", transform: exp ? "rotate(90deg)" : "none", flexShrink: 0 }} />
            </div>

            {/* Runs */}
            {exp && (
              <div style={{ padding: "0 18px 16px 70px", borderTop: "1px solid rgba(28,25,23,0.06)" }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", margin: "12px 0 8px" }}>
                  <span className="manor-label" style={{ margin: 0 }}>{t("page.scheduled_jobs.recent_runs")}</span>
                  {job.created_at && <span style={{ fontSize: 10, color: "#d6d3d1" }}>{t("page.dashboard.created")} {relativeTime(job.created_at)}</span>}
                </div>
                {(runs as JobRun[]).length === 0 ? (
                  <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>{t("page.flows.no_runs")}</p>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
                    {(runs as JobRun[]).slice(0, 10).map((r) => {
                      const isOpen = openRunId === r.id;
                      const dot = r.status === "success" ? "#4f9c84"
                                : r.status === "running" || r.status === "queued" ? "#cf9b44"
                                : "#d65f59";
                      return (
                        <div key={r.id}>
                          <div
                            onClick={() => setOpenRunId(isOpen ? null : r.id)}
                            style={{
                              display: "flex", alignItems: "center", gap: 10, fontSize: 11,
                              padding: "6px 0", borderBottom: isOpen ? "none" : "1px solid rgba(245,245,244,0.5)",
                              cursor: "pointer",
                            }}
                          >
                            <IconChevronRight
                              size={10}
                              style={{ color: "#d6d3d1", transition: "transform 0.15s", transform: isOpen ? "rotate(90deg)" : "none", flexShrink: 0 }}
                            />
                            <span style={{ width: 7, height: 7, borderRadius: "50%", flexShrink: 0, background: dot }} />
                            <span style={{ fontWeight: 600, color: "#57534e", textTransform: "capitalize", minWidth: 52 }}>{r.status}</span>
                            <span style={{ color: "#a8a29e", minWidth: 44 }}>{r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : "--"}</span>
                            {r.error && !isOpen && <span style={{ color: "#d65f59", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.error}</span>}
                            <span style={{ color: "#d6d3d1", marginLeft: "auto", fontSize: 10 }}>{r.created_at ? relativeTime(r.created_at) : ""}</span>
                          </div>
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
      })}

      {!isLoading && !bindingsLoading && (jobsData?.total ?? 0) > 0 && (
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
        title="Delete workflow automation"
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
                !fName.trim()
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

          <div>
            <label className="manor-label">Automation action</label>
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
              <p className="workspace-workflow-automation-help">Automation type is fixed after creation; its workflow and condition remain editable.</p>
            )}
          </div>

          {fKind !== "agent_schedule" && (
            <div>
              <label className="manor-label">Workspace workflow</label>
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
                  placeholder="Select an attached workflow"
                  options={workflowBindingOptions}
                />
              ) : (
                <div className="workspace-workflow-automation-empty">
                  <span>Attach a workflow to this workspace before creating a workflow automation.</span>
                  {workspaceId && <Link to={`/workspaces/${workspaceId}?tab=workflows`}>Manage workspace workflows</Link>}
                </div>
              )}
              <p className="workspace-workflow-automation-help">Automations trigger the selected workspace binding; the workflow can also run manually or from an API.</p>
            </div>
          )}

          {fKind === "workflow_event" && (
            <div>
              <label className="manor-label">Workspace event</label>
              <input
                className="manor-input mono"
                value={fEvent}
                onChange={(event) => setFEvent(event.target.value)}
                placeholder="task.approval_decision"
                aria-label="Workspace event name"
                style={{ width: "100%" }}
              />
              <div className="workspace-workflow-event-suggestions" aria-label="Common workspace events">
                {WORKSPACE_EVENT_SUGGESTIONS.map((event) => (
                  <button key={event.value} type="button" onClick={() => setFEvent(event.value)} title={event.value}>
                    {event.label}
                  </button>
                ))}
              </div>
              <p className="workspace-workflow-automation-help">The event payload is available to workflow nodes as <span className="mono">trigger</span>.</p>
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
                      border: active ? "2px solid #6d6fb2" : "1px solid rgba(231,229,228,0.6)",
                      background: active ? "#f1f3f9" : "#fff", color: active ? "#5a55a6" : "#78716c",
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
                <span style={{ fontSize: 12, color: "#78716c", fontWeight: 500 }}>
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
                <span style={{ fontSize: 12, color: "#a8a29e" }}>:</span>
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
                <span style={{ fontSize: 11, color: "#78716c", fontWeight: 500, display: "block", marginBottom: 6 }}>{t("page.scheduled_jobs.on_these_days")}</span>
                <div style={{ display: "flex", gap: 4 }}>
                  {DAYS_OF_WEEK.map((d) => {
                    const active = fDays.includes(d.key);
                    return (
                      <button key={d.key} type="button"
                        onClick={() => setFDays(active ? fDays.filter((x) => x !== d.key) : [...fDays, d.key].sort())}
                        style={{
                          width: 38, height: 34, borderRadius: 8, fontSize: 11, fontWeight: 600, cursor: "pointer",
                          border: active ? "2px solid #6d6fb2" : "1px solid rgba(231,229,228,0.6)",
                          background: active ? "#f1f3f9" : "#fff", color: active ? "#5a55a6" : "#78716c",
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
                <span style={{ fontSize: 12, color: "#78716c", fontWeight: 500 }}>{t("page.scheduled_jobs.on_day")}</span>
                <Select
                  value={String(fDom)}
                  onChange={(v) => setFDom(Number(v))}
                  options={Array.from({ length: 28 }, (_, i) => ({ value: String(i + 1), label: String(i + 1) }))}
                  style={{ width: 70 }}
                />
                <span style={{ fontSize: 12, color: "#a8a29e" }}>{t("page.scheduled_jobs.of_each_month")}</span>
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
                        background: active ? "#f2f6f5" : "#fff", color: active ? "#436b65" : "#78716c",
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
              <div style={{ marginTop: 8, padding: "6px 10px", borderRadius: 8, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)", fontSize: 12, color: "#57534e" }}>
                <span style={{ fontWeight: 600, color: "#6d6fb2" }}>{describeCron(buildCron(fFreq, fHour, fMinute, fDays, fDom))}</span>
                <span style={{ color: "#a8a29e" }}> · {fTimezone}</span>
              </div>
            )}
          </div>}

          {/* What to do */}
          {fKind === "agent_schedule" && <div>
            <label className="manor-label">{t("page.scheduled_jobs.what_should_the_agent_do")}</label>
            <textarea className="manor-textarea" value={fMessage} onChange={(e) => setFMessage(e.target.value)} rows={3}
              placeholder={t("page.scheduled_jobs.e_g_generate_a_summary_of_all_tasks_completed_th")}
              style={{ width: "100%", fontSize: 13, lineHeight: 1.6 }} />
            <p style={{ fontSize: 10, color: "#78716c", margin: "6px 0 0", display: "flex", alignItems: "center", gap: 4 }}>
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
