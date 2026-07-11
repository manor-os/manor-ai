import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { relativeTime, formatDateFull } from "../lib/format";
import { stringifyJobRunResult, summarizeJobRunResult } from "../lib/jobRunResult";
import PageHeader from "../components/ui/PageHeader";
import StatusBadge from "../components/ui/StatusBadge";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import { IconArrowLeft, IconClock } from "../components/icons";

/* -- types -------------------------------------------------- */

interface JobRun {
  id: string;
  job_id: string;
  status: string;
  trigger_type: string;
  duration_ms?: number;
  error?: string;
  result?: Record<string, any>;
  created_at?: string;
}

interface ScheduledJob {
  id: string;
  name: string;
  job_type: string;
  cron_expr?: string;
  interval_seconds?: number;
  enabled: boolean;
  last_run_at?: string;
}

/* -- helpers ------------------------------------------------ */

const STATUS_MAP: Record<string, { badge: string; label: string }> = {
  success: { badge: "green", label: "page.job_logs.success" },
  completed: { badge: "green", label: "page.job_logs.completed" },
  failed: { badge: "red", label: "page.job_logs.failed" },
  error: { badge: "red", label: "page.job_logs.error" },
  running: { badge: "blue", label: "page.job_logs.running" },
  pending: { badge: "warning", label: "page.job_logs.pending" },
};

const TRIGGER_MAP: Record<string, string> = {
  cron: "page.job_logs.trigger_cron",
  manual: "page.job_logs.trigger_manual",
  event: "page.job_logs.trigger_event",
  api: "page.job_logs.trigger_api",
};

function formatDuration(ms?: number): string {
  if (!ms) return t("page.job_logs.na_short");
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function formatSchedule(job: ScheduledJob): string {
  if (job.job_type === "cron" && job.cron_expr) return job.cron_expr;
  if (job.job_type === "interval" && job.interval_seconds) {
    const s = job.interval_seconds;
    if (s < 60) return `${t("page.job_logs.every")} ${s}s`;
    if (s < 3600) return `${t("page.job_logs.every")} ${Math.floor(s / 60)}m`;
    return `${t("page.job_logs.every")} ${Math.floor(s / 3600)}h`;
  }
  return t("page.job_logs.na_short");
}

/* -- component --------------------------------------------- */

export default function JobLogs() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const [expandedRun, setExpandedRun] = useState<string | null>(null);

  const { data: jobsData } = useQuery({
    queryKey: ["scheduled-jobs"],
    queryFn: () => api.jobs.list(),
  });

  const { data: runs = [], isLoading: runsLoading } = useQuery({
    queryKey: ["job-runs", jobId],
    queryFn: () => api.jobs.runs(jobId!),
    enabled: !!jobId,
  });

  const jobs = (jobsData?.items as ScheduledJob[] | undefined) ?? [];
  const job = jobs.find((j) => j.id === jobId);

  return (
    <div style={{ maxWidth: 900, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <button
          onClick={() => navigate("/jobs")}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 13,
            color: "#78716c",
            background: "transparent",
            border: "none",
            cursor: "pointer",
            marginBottom: 16,
            transition: "color 0.2s",
          }}
        >
          <IconArrowLeft size={16} />
          {t("page.job_logs.back_to_jobs")}
        </button>

        <PageHeader
          title={t("page.job_logs.title")}
          subtitle={job ? `${t("page.job_logs.runs_for")} "${job.name}"` : undefined}
        />
      </div>

      {/* Job info card */}
      {job && (
        <div className="glass-card" style={{ marginBottom: 20 }}>
          <div style={{ padding: 20 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
              <h2 style={{ fontSize: 16, fontWeight: 700, color: "#292524", margin: 0 }}>{job.name}</h2>
              <StatusBadge type="purple">{job.job_type}</StatusBadge>
              <StatusBadge type={job.enabled ? "active" : "inactive"} dot>
                {job.enabled ? t("page.job_logs.enabled") : t("page.job_logs.disabled")}
              </StatusBadge>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 16, marginTop: 12, fontSize: 13, color: "#78716c" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <IconClock size={14} />
                <span>{t("page.job_logs.schedule")}:</span>
                <code style={{ fontSize: 12, background: "#f5f5f4", padding: "2px 6px", borderRadius: 6, color: "#44403c" }}>
                  {formatSchedule(job)}
                </code>
              </div>
              {job.last_run_at && (
                <div>
                  <span>{t("page.job_logs.last_run")}: </span>
                  <span style={{ color: "#44403c" }}>{relativeTime(job.last_run_at)}</span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Runs list */}
      {runsLoading ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: 200 }}>
          <LoadingSpinner size={24} />
        </div>
      ) : (runs as JobRun[]).length === 0 ? (
        <EmptyState
          icon={<IconClock size={32} />}
          title={t("page.job_logs.no_runs")}
          description={t("page.job_logs.no_runs_desc")}
          action={
            <Button variant="outline" onClick={() => navigate("/jobs")}>
              {t("page.job_logs.back_to_jobs")}
            </Button>
          }
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {(runs as JobRun[]).map((run) => {
            const st = STATUS_MAP[run.status] || { badge: "gray", label: run.status };
            const isExpanded = expandedRun === run.id;
            const hasResult = run.result && Object.keys(run.result).length > 0;
            const resultSummary = summarizeJobRunResult(run.result);
            const resultText = stringifyJobRunResult(run.result);

            return (
              <div
                key={run.id}
                className="glass-card"
                style={{ cursor: hasResult ? "pointer" : "default", transition: "all 0.2s" }}
                onClick={() => hasResult && setExpandedRun(isExpanded ? null : run.id)}
              >
                <div style={{ padding: "14px 20px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                    {/* Status badge */}
                    <StatusBadge type={st.badge} dot pulse={run.status === "running"}>
                      {t(st.label)}
                    </StatusBadge>

                    {/* Trigger type */}
                    <span style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: "#78716c",
                      background: "#f5f5f4",
                      padding: "2px 8px",
                      borderRadius: 8,
                    }}>
                      {t(TRIGGER_MAP[run.trigger_type] || run.trigger_type)}
                    </span>

                    {/* Timestamp */}
                    {run.created_at && (
                      <span style={{ fontSize: 12, color: "#a8a29e" }} title={formatDateFull(run.created_at)}>
                        {relativeTime(run.created_at)}
                      </span>
                    )}

                    {/* Duration */}
                    <span style={{ fontSize: 12, color: "#78716c", marginLeft: "auto" }}>
                      {formatDuration(run.duration_ms)}
                    </span>

                    {/* Expand indicator */}
                    {hasResult && (
                      <svg
                        width={14}
                        height={14}
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="#a8a29e"
                        strokeWidth={2}
                        style={{ transition: "transform 0.2s", transform: isExpanded ? "rotate(180deg)" : "rotate(0deg)" }}
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
                      </svg>
                    )}
                  </div>

                  {/* Error message */}
                  {run.error && (
                    <div style={{
                      marginTop: 8,
                      padding: "8px 12px",
                      borderRadius: 8,
                      background: "#f8f0ef",
                      border: "1px solid #ecc8c5",
                      fontSize: 12,
                      color: "#c14a44",
                      fontFamily: "monospace",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}>
                      {run.error}
                    </div>
                  )}

                  {/* Collapsible run result */}
                  {isExpanded && hasResult && (
                    <div style={{
                      marginTop: 10,
                      padding: "10px 14px",
                      borderRadius: 10,
                      background: "#fafaf9",
                      border: "1px solid rgba(28,25,23,0.06)",
                      fontSize: 12,
                      fontFamily: "monospace",
                      color: "#44403c",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                      maxHeight: 320,
                      overflowY: "auto",
                    }}>
                      <div style={{ fontFamily: "inherit", fontWeight: 700, color: "#57534e", marginBottom: 8 }}>
                        {t("page.scheduled_jobs.execution_result")}
                      </div>
                      {resultSummary.length > 0 && (
                        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 8, fontFamily: "inherit" }}>
                          {resultSummary.map((line, i) => (
                            <div key={i} style={{
                              padding: "4px 7px",
                              borderRadius: 7,
                              background: "#fff",
                              border: "1px solid rgba(226,232,240,0.7)",
                            }}>
                              {line}
                            </div>
                          ))}
                        </div>
                      )}
                      {resultText}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
