import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { NavigateFunction } from "react-router-dom";
import { api, type WorkspaceAppChartData, type WorkspaceAppMetric, type WorkspaceStatDefinition, type WorkspaceStatObservation } from "../../lib/api";
import WorkspaceAppChart from "./WorkspaceAppChart";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import { getPreferredTimeZone, relativeTime } from "../../lib/format";
import { goalProgressPercent } from "../../lib/goalNumbers";
import { getLocale, t } from "../../lib/i18n";
import { workflowRunPath } from "../../lib/workflowRoutes";
import { usePreviewFeatureAccess } from "../../lib/previewFeatureAccess";
import { formatWorkspaceStatValue, workspaceStatAttentionPresentation } from "../../lib/workspaceStats";
import { closeDetail, openDetail } from "../../stores/detail";
import { useToastStore } from "../../stores/toast";
import { IconSettings, IconTrendingUp } from "../icons";
import Button from "../ui/Button";
import Input from "../ui/Input";
import { metricPhaseTone } from "./workspaceAppRunDisplay";

type Reading = { at: string; value: number | null; reasoning?: string; references: Array<{ document_id: string; reason: string }>; gaps: string[] };

function statShape(metric: WorkspaceAppMetric): WorkspaceStatDefinition {
  return {
    value_type: metric.value_type, unit: metric.unit, current_value: metric.value, freshness_status: metric.freshness_status,
  } as WorkspaceStatDefinition;
}

function when(value: string | null | undefined): string {
  if (!value) return "";
  return new Date(value).toLocaleString(getLocale(), {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: getPreferredTimeZone(),
  });
}

function readingFromObservation(row: WorkspaceStatObservation): Reading {
  const evidence = (row.evidence || {}) as { reasoning?: unknown; references?: unknown; gaps?: unknown };
  const references = Array.isArray(evidence.references) ? evidence.references.flatMap((ref) => {
    const item = ref as { document_id?: unknown; reason?: unknown };
    return typeof item.document_id === "string" ? [{ document_id: item.document_id, reason: String(item.reason || "") }] : [];
  }) : [];
  return {
    at: row.observed_at, value: row.value,
    reasoning: typeof evidence.reasoning === "string" ? evidence.reasoning : undefined,
    references, gaps: Array.isArray(evidence.gaps) ? evidence.gaps.map(String) : [],
  };
}

/** The readings over time on a scale: an axis of values, each reading's time and value on hover, the goal's target as a line. */
function TrendChart({ points, metric }: { points: Array<{ at: string; value: number }>; metric: WorkspaceAppMetric }) {
  if (points.length < 2) return null;
  const target = metric.goal?.target;
  const chart: WorkspaceAppChartData = {
    kind: "area", format: "number", unit: metric.value_type === "percent" ? "%" : metric.unit, records: points.length,
    categories: points.map((point, index) => ({ key: `${index}:${point.at}`, label: when(point.at) })),
    series: [{ key: "value", label: metric.title, values: points.map((point) => point.value) }],
    ...(typeof target === "number" ? { target: { value: target, label: t("component.workspace_app_view.metric_goal") } } : {}),
  };
  return <figure className="workspace-app-metric-detail__chart"><WorkspaceAppChart chart={chart} title={t("component.workspace_app_view.metric_trend")} /></figure>;
}

function RecordReading({ workspaceId, metric }: { workspaceId: string; metric: WorkspaceAppMetric }) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const record = useMutation({
    mutationFn: () => api.workspaces.stats.record(workspaceId, metric.id, { value: Number(value) }),
    onSuccess: () => {
      setValue("");
      toast.success(t("component.workspace_app_view.metric_recorded"));
      void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-app-metric-readings", workspaceId, metric.id] });
    },
    onError: (failure: Error) => setError(failure.message),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (value.trim() === "" || !Number.isFinite(Number(value))) {
      setError(t("component.workspace_app_view.metric_record_invalid"));
      return;
    }
    setError("");
    record.mutate();
  };
  return <form className="workspace-app-metric-detail__record" onSubmit={submit}>
    <label htmlFor={`metric-record-${metric.id}`}>{t("component.workspace_app_view.metric_record")}</label>
    <div>
      <Input id={`metric-record-${metric.id}`} inputMode="decimal" value={value}
        onChange={(event) => { setValue(event.target.value); setError(""); }}
        placeholder={metric.value !== null ? String(metric.value) : "0"} />
      <Button type="submit" variant="outline" loading={record.isPending}>{t("component.workspace_app_view.metric_record_save")}</Button>
    </div>
    {error && <p role="alert">{error}</p>}
  </form>;
}

function MetricDetailBody({ workspaceId, metric, canStart }: { workspaceId: string; metric: WorkspaceAppMetric; canStart: boolean }) {
  const isStat = metric.kind === "stat";
  const flowsAccess = usePreviewFeatureAccess("flows");
  const readings = useQuery({
    queryKey: ["workspace-app-metric-readings", workspaceId, metric.id],
    queryFn: () => api.workspaces.stats.observations(workspaceId, metric.id, 30),
    enabled: isStat,
  });
  const stat = statShape(metric);
  const rows: Reading[] = isStat
    ? (readings.data?.items || []).map(readingFromObservation)
    : [...metric.series].reverse().map((point) => ({ at: point.at, value: point.value, references: [], gaps: [] }));
  const chartPoints = (isStat
    ? [...(readings.data?.items || [])].reverse().filter((row) => row.value !== null).map((row) => ({ at: row.observed_at, value: row.value as number }))
    : metric.series);
  const goal = metric.goal;
  const percent = goal && goal.target !== null && metric.value !== null
    ? Math.round(goalProgressPercent({ current_value: metric.value, target_value: goal.target, baseline_value: goal.baseline ?? 0 }))
    : null;
  const assessment = metric.assessment;

  return <div className="workspace-app-metric-detail">
    <TrendChart points={chartPoints.length >= 2 ? chartPoints : metric.series} metric={metric} />

    {metric.description && <section>
      <h4>{t("component.workspace_app_view.metric_how_measured")}</h4>
      <p className="workspace-app-metric-detail__muted">{metric.description}</p>
      {metric.window && <p className="workspace-app-metric-detail__muted">{t("component.workspace_app_view.metric_window", { window: metric.window.replace(/_/g, " ") })}</p>}
    </section>}

    {goal && goal.target !== null && <section>
      <h4>{t("component.workspace_app_view.metric_goal")}</h4>
      <p className="workspace-app-metric-detail__goal-title">{goal.title}</p>
      <div className="workspace-app-metric__track" aria-hidden="true"><span style={{ width: `${percent ?? 0}%` }} /></div>
      <p className="workspace-app-metric-detail__muted">
        {t("component.workspace_app_view.metrics_target", { target: formatWorkspaceStatValue(stat, goal.target) })}
        {percent !== null && ` · ${percent}%`}
        {goal.deadline && ` · ${t("component.workspace_app_view.metric_deadline", { date: goal.deadline })}`}
      </p>
    </section>}

    {metric.collector_type === "agent_review" && <section>
      <h4>{t("component.workspace_app_view.metric_assessment")}</h4>
      <p className="workspace-app-metric-detail__muted">
        {assessment?.next_run_at
          ? t("component.workspace_app_view.metric_next_assessment", { time: when(assessment.next_run_at) })
          : t("component.workspace_app_view.metric_no_schedule")}
      </p>
      {flowsAccess.enabled && assessment?.workflow_id && assessment.run_id && <Link className="workspace-app-metric-detail__link"
        to={workflowRunPath(assessment.workflow_id, assessment.run_id, workspaceId)} onClick={() => closeDetail()}>
        {t("component.workspace_app_view.metric_open_assessment")}
      </Link>}
    </section>}

    {metric.gaps.length > 0 && <section>
      <h4>{t("component.workspace_app_view.metric_gaps")}</h4>
      <ul className="workspace-app-metric-detail__gaps">{metric.gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul>
    </section>}

    {isStat && metric.collector_type === "manual" && canStart && <RecordReading workspaceId={workspaceId} metric={metric} />}

    <section>
      <h4>{t("component.workspace_app_view.metric_readings")}</h4>
      {isStat && readings.isPending ? <p className="workspace-app-metric-detail__muted">{t("component.workspace_app_view.loading")}</p>
        : isStat && readings.isError ? <p className="workspace-app-metric-detail__muted">{t("component.workspace_app_view.section_error")}</p>
          : !rows.length ? <p className="workspace-app-metric-detail__muted">{t("component.workspace_app_view.metric_no_readings")}</p>
            : <ol className="workspace-app-metric-detail__readings">{rows.slice(0, 10).map((row, index) => <li key={`${row.at}:${index}`}>
              <div>
                <strong>{row.value === null ? t("page.workspace_stats.insufficient_evidence") : formatWorkspaceStatValue(stat, row.value)}</strong>
                <small>{when(row.at)} · {relativeTime(row.at, "")}</small>
              </div>
              {row.reasoning && <p>{row.reasoning}</p>}
              {row.gaps.length > 0 && <p className="workspace-app-metric-detail__muted">{row.gaps.join(" · ")}</p>}
              {row.references.length > 0 && <ul className="workspace-app-metric-detail__refs">{row.references.slice(0, 4).map((ref) => {
                const href = viewerPathForDocumentId(ref.document_id);
                return <li key={ref.document_id + ref.reason}>{href ? <Link to={href} onClick={() => closeDetail()}>{ref.reason || t("component.workspace_app_view.file")}</Link> : ref.reason}</li>;
              })}</ul>}
            </li>)}</ol>}
    </section>
  </div>;
}

/** Open a tracked number in the global detail drawer: its history, evidence and how to refresh it. */
export function openMetricDetail({ workspaceId, metric, canStart, navigate, queryClient, returnFocusTo }: {
  workspaceId: string;
  metric: WorkspaceAppMetric;
  canStart: boolean;
  navigate: NavigateFunction;
  queryClient: QueryClient;
  returnFocusTo?: HTMLElement | null;
}) {
  const toast = useToastStore.getState();
  const stat = statShape(metric);
  const attention = workspaceStatAttentionPresentation(metric.freshness_status);
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["workspace-app-metric-readings", workspaceId, metric.id] });
  };
  const fail = (error: unknown) => toast.error(t("component.workspace_app_view.metric_refresh_failed"), error instanceof Error ? error.message : undefined);
  const jobId = metric.assessment?.job_id;
  const primaryAction = !canStart || metric.kind !== "stat" ? undefined
    : metric.collector_type === "agent_review" && jobId ? {
      label: t("component.workspace_app_view.metric_assess_now"),
      onClick: () => api.jobs.runNow(jobId).then(() => {
        toast.success(t("component.workspace_app_view.metric_assess_started"));
        refresh();
      }, fail),
    }
    : metric.collector_type === "integration" || metric.collector_type === "workspace_internal" ? {
      label: t("component.workspace_app_view.metric_collect_now"),
      onClick: () => api.workspaces.stats.collect(workspaceId, metric.id).then(() => {
        toast.success(t("component.workspace_app_view.metric_collected"));
        refresh();
      }, fail),
    }
    : undefined;
  // Drawer secondary actions are icon buttons whose label is the tooltip.
  const manage = metric.kind === "goal"
    ? { label: t("component.workspace_app_view.metric_open_goals"), icon: <IconTrendingUp size={16} />,
      onClick: () => { closeDetail(); navigate(`/workspaces/${workspaceId}?tab=goals`); } }
    : { label: t("component.workspace_app_view.metric_manage"), icon: <IconSettings size={16} />,
      onClick: () => { closeDetail(); navigate(`/workspaces/${workspaceId}?tab=records&view=data#workspace-stats`); } };
  openDetail({
    key: `workspace-app-metric:${metric.kind}:${metric.id}`,
    title: metric.title,
    subtitle: <span className="workspace-app-metric-detail__value">{formatWorkspaceStatValue(stat, metric.value)}</span>,
    badges: attention ? <span className={`workspace-app-status workspace-app-status--${metricPhaseTone(metric.freshness_status)}`}>{attention.label}</span> : undefined,
    body: <MetricDetailBody workspaceId={workspaceId} metric={metric} canStart={canStart} />,
    primaryAction,
    secondaryActions: [manage],
    returnFocusTo,
  });
}
