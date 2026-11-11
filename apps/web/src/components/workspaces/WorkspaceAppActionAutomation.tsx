import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type WorkspaceAppAction } from "../../lib/api";
import { t } from "../../lib/i18n";
import { workflowPath } from "../../lib/workflowRoutes";
import { IconArrowLeft, IconArrowRight, IconClock } from "../icons";
import Button from "../ui/Button";
import { getPreferredTimeZone } from "../../lib/format";
import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { usePageVisible } from "../../lib/usePageVisible";

function frequencyLabel(action: WorkspaceAppAction): string {
  const seconds = action.every_seconds;
  if (typeof seconds === "number" && seconds > 0 && (action.schedule_kind === "every" || action.job_type === "interval")) {
    if (seconds === 604_800) return t("component.workspace_app_view.automation_weekly");
    if (seconds === 86_400) return t("component.workspace_app_view.automation_daily");
    for (const [unit, key] of [
      [604_800, "automation_every_weeks"],
      [86_400, "automation_every_days"],
      [3_600, "automation_every_hours"],
      [60, "automation_every_minutes"],
    ] as const) {
      if (seconds % unit === 0) return t(`component.workspace_app_view.${key}`, { count: seconds / unit });
    }
    return t("component.workspace_app_view.automation_every_seconds", { count: seconds });
  }
  if (action.run_at) {
    const date = new Date(action.run_at);
    if (!Number.isNaN(date.getTime())) return t("component.workspace_app_view.automation_once", { date: date.toLocaleString(undefined, { timeZone: getPreferredTimeZone() }) });
  }
  if (action.cron_expr) return t("component.workspace_app_view.automation_custom_schedule");
  return t("component.workspace_app_view.automation_manual_only");
}

export default function WorkspaceAppActionAutomation({ action, canStart, onBack }: {
  action: WorkspaceAppAction;
  canStart: boolean;
  onBack: () => void;
}) {
  const [queueDiscoveryUntil, setQueueDiscoveryUntil] = useState<number | null>(null);
  const jobId = action.job_id || "";
  const visible = usePageVisible();
  const runs = useQuery({
    // Share the job history read with the Automation page and socket resync.
    queryKey: ["job-runs", jobId],
    queryFn: () => api.jobs.runs(jobId),
    enabled: visible && Boolean(jobId),
    refetchInterval: (query) => {
      const active = query.state.data?.some((run) => ["pending", "queued", "running"].includes(run.status));
      const discovering = queueDiscoveryUntil !== null && Date.now() < queueDiscoveryUntil;
      return displayReadInterval(query.state, active || discovering ? 5_000 : 60_000);
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  useEffect(() => {
    if (queueDiscoveryUntil === null) return;
    if (runs.data?.some((run) => ["pending", "queued", "running"].includes(run.status))) {
      setQueueDiscoveryUntil(null);
      return;
    }
    const remaining = queueDiscoveryUntil - Date.now();
    if (remaining <= 0) {
      setQueueDiscoveryUntil(null);
      return;
    }
    const timer = window.setTimeout(() => setQueueDiscoveryUntil(null), remaining);
    return () => window.clearTimeout(timer);
  }, [queueDiscoveryUntil, runs.data]);
  const process = useQuery({
    queryKey: ["workspace-app-automation-process", action.target_workflow_id],
    queryFn: () => api.workflows.get(action.target_workflow_id!),
    enabled: Boolean(action.target_workflow_id),
  });
  const start = useMutation({
    mutationFn: () => api.jobs.runNow(jobId),
    onSuccess: () => {
      // The worker creates the history row asynchronously. Keep a short,
      // bounded discovery window, then return to the normal idle cadence.
      setQueueDiscoveryUntil(Date.now() + 20_000);
      void runs.refetch();
    },
  });
  const recent = (runs.data || []).slice(0, 3) as Array<{ id: string; status: string; created_at?: string }>;
  const instructions = action.instructions?.trim() || "";
  const rawSteps: unknown[] = Array.isArray(process.data?.steps) ? process.data.steps : [];
  const steps = rawSteps
    .filter((step): step is { name: string } => typeof step === "object" && step !== null && typeof (step as { name?: unknown }).name === "string")
    .map((step) => step.name.trim())
    .filter(Boolean);
  return <div className="workspace-app-flow">
    <button type="button" className="workspace-app-flow__back" onClick={onBack}><IconArrowLeft size={16} />{t("component.workspace_app_view.all_actions")}</button>
    <span className="workspace-app-flow__mark" aria-hidden="true"><IconClock size={22} /></span>
    <h2>{action.title}</h2>
    <div className="workspace-app-flow__panel">
      <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.automation_about")}</span>
      <p className="workspace-app-automation__description">{action.description || (instructions ? instructions.slice(0, 240) : t("component.workspace_app_view.automation_no_description"))}</p>
      <dl className="workspace-app-automation__facts">
        <div><dt>{t("component.workspace_app_view.automation_frequency")}</dt><dd>{frequencyLabel(action)}</dd></div>
        <div><dt>{t("component.workspace_app_view.automation_auto_runs")}</dt><dd>{action.enabled ? t("component.workspace_app_view.automation_on") : t("component.workspace_app_view.automation_paused")}</dd></div>
        {action.target_title && <div className="workspace-app-automation__process"><dt>{t("component.workspace_app_view.automation_process")}</dt><dd>{action.target_title}{action.target_workflow_id && <Link to={workflowPath(action.target_workflow_id)}>{t("component.workspace_app_view.automation_see_steps")} <IconArrowRight size={14} /></Link>}</dd></div>}
      </dl>
      {steps.length > 0 && <div className="workspace-app-automation__steps">
        <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.automation_steps")}</span>
        <ol>{steps.map((step, index) => <li key={`${index}:${step}`}><span>{index + 1}</span>{step}</li>)}</ol>
      </div>}
      {instructions && <details className="workspace-app-automation__instructions" open={!action.description}><summary>{t("component.workspace_app_view.automation_instructions")}</summary><p>{instructions}</p></details>}
      {queueDiscoveryUntil !== null && <p role="status">{t("component.workspace_app_view.automation_queued")}</p>}
      {action.last_status && <p>{t("component.workspace_app_view.automation_last_status")}: {action.last_status}</p>}
      {start.isError && <p role="alert" className="workspace-app-flow__error">{start.error instanceof Error ? start.error.message : t("component.workspace_app_view.start_error")}</p>}
      <div className="workspace-app-flow__buttons workspace-app-automation__actions">
        <Link to={`/jobs?job=${encodeURIComponent(jobId)}`}>{t("component.workspace_app_view.automation_history")} <IconArrowRight size={15} /></Link>
        <Button variant="primary" onClick={() => start.mutate()} disabled={!canStart || start.isPending || !jobId} loading={start.isPending}>{t("component.workspace_app_view.run")}</Button>
      </div>
    </div>
    {recent.length > 0 && <div className="workspace-app-flow__panel">
      <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.history")}</span>
      <div className="workspace-app-automation__runs">{recent.map((run) => <Link key={run.id} to={`/jobs?job=${encodeURIComponent(jobId)}&run=${encodeURIComponent(run.id)}`}><span>{run.status}</span><span>{run.created_at ? new Date(run.created_at).toLocaleString(undefined, { timeZone: getPreferredTimeZone() }) : ""}</span><IconArrowRight size={15} /></Link>)}</div>
    </div>}
  </div>;
}
