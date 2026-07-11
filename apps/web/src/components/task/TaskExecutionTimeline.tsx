import { useState } from "react";
import type { ExecutionPlan, ExecutionStep } from "../../lib/types";
import ChatMarkdown from "../ChatMarkdown";
import { IconCheck, IconCheckCircle, IconChevronRight, IconClock, IconCode, IconError, IconPlay, IconRefresh, IconTimeline, IconWarning } from "../icons";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingStructuredText } from "../../lib/taskDisplay";
import UserAvatar from "../ui/UserAvatar";


const FAILED_RETRYABLE_STEP_STATUSES = new Set(["failed", "skipped", "cancelled"]);
const WAITING_RETRYABLE_STEP_STATUSES = new Set(["waiting_human"]);
const RETRYABLE_STEP_STATUSES = new Set([
  ...FAILED_RETRYABLE_STEP_STATUSES,
  ...WAITING_RETRYABLE_STEP_STATUSES,
  "paused",
]);
const TERMINAL_PLAN_STATUSES = new Set(["completed"]);

const PLAN_STATUS_LABELS: Record<string, string> = {
  draft: t("component.task_execution_timeline.draft"),
  running: t("page.job_logs.running"),
  paused: t("page.workspaces.filter_paused"),
  needs_attention: t("component.task_execution_timeline.needs_attention"),
  pending_approval: t("component.task_execution_timeline.pending_approval"),
  completed: t("status.completed"),
  failed: t("page.dashboard.failed"),
  cancelled: t("status.cancelled"),
  replanned: t("component.task_execution_timeline.replanned"),
};

const STEP_STATUS_META: Record<string, { label: string; color: string; bg: string; Icon: typeof IconClock }> = {
  pending: { label: t("status.pending"), color: "#78716c", bg: "#fafaf9", Icon: IconClock },
  running: { label: t("page.job_logs.running"), color: "#4869ac", bg: "#e3e9f1", Icon: IconPlay },
  done: { label: t("page.team_people.done"), color: "#437f6b", bg: "#dceae3", Icon: IconCheckCircle },
  failed: { label: t("page.dashboard.failed"), color: "#be123c", bg: "#ffe4e6", Icon: IconError },
  skipped: { label: t("component.task_execution_timeline.skipped"), color: "#9a5630", bg: "#ffedd5", Icon: IconWarning },
  waiting_human: { label: t("component.task_execution_timeline.waiting_input"), color: "#b27c34", bg: "#f3ecd6", Icon: IconWarning },
  cancelled: { label: t("status.cancelled"), color: "#78716c", bg: "#f5f5f4", Icon: IconError },
  paused: { label: t("page.workspaces.filter_paused"), color: "#6f4ba8", bg: "#ece9f5", Icon: IconWarning },
};

function shortId(value?: string | null) {
  return value ? value.slice(-6) : "";
}

function humanize(value?: string | null) {
  return formatUserFacingLabel(value);
}

function stepSubtitle(step: ExecutionStep) {
  const providerAction = step.provider && step.action_key
    ? `${formatUserFacingLabel(String(step.provider))} · ${formatUserFacingLabel(String(step.action_key))}`
    : null;
  const bits = [
    step.kind,
    step.service_key,
    providerAction || step.provider || step.action_key,
  ].filter(Boolean);
  return bits.map((bit) => formatUserFacingLabel(String(bit))).join(" · ");
}

function stepExecutor(step: ExecutionStep) {
  const name =
    step.resolved_agent_name ||
    step.resolved_subscription_name ||
    (step.resolved_agent_id ? `Agent ${shortId(step.resolved_agent_id)}` : "");
  if (!name) return null;
  return {
    name,
    id: step.resolved_agent_id || step.resolved_subscription_id || name,
    avatarUrl: step.resolved_agent_avatar || undefined,
    title: [
      name,
      step.service_key ? `service: ${step.service_key}` : "",
      step.resolved_subscription_id ? `subscription: ${step.resolved_subscription_id}` : "",
      step.resolved_agent_id ? `agent: ${step.resolved_agent_id}` : "",
    ].filter(Boolean).join(" · "),
  };
}

function stepKeyLabel(stepKey: string, stepsByKey: Map<string, ExecutionStep>) {
  const step = stepsByKey.get(stepKey);
  return humanize(step?.step_key || stepKey);
}

const RESULT_TEXT_KEYS = [
  "result_summary",
  "summary",
  "message",
  "text",
  "value",
  "content",
  "answer",
  "final_message",
  "final",
  "stdout",
  "output",
  "result",
];

const RESULT_FILE_KEYS = ["files", "artifacts", "attachments", "documents", "outputs"];

function trimPreview(value: string, max = 3000) {
  const normalized = value.replace(/\s+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  return normalized.length > max ? `${normalized.slice(0, max - 1).trim()}...` : normalized;
}

function compactStructuredPreview(value: any, max = 520): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return trimPreview(formatUserFacingStructuredText(value), max);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return trimPreview(formatUserFacingStructuredText(value), max);
}

function textFromResult(value: any, depth = 0): string {
  if (value === null || value === undefined || depth > 3) return "";
  if (typeof value === "string") return trimPreview(formatUserFacingStructuredText(value));
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) {
    const textItems = value
      .map((item) => textFromResult(item, depth + 1))
      .filter(Boolean);
    return trimPreview(textItems.join("\n"));
  }
  if (typeof value !== "object") return "";

  for (const key of RESULT_TEXT_KEYS) {
    if (Object.prototype.hasOwnProperty.call(value, key)) {
      const candidate = textFromResult(value[key], depth + 1);
      if (candidate) return candidate;
    }
  }
  return "";
}

function collectOutputFiles(value: any, depth = 0): any[] {
  if (!value || depth > 3) return [];
  if (Array.isArray(value)) {
    return value.flatMap((item) => collectOutputFiles(item, depth + 1));
  }
  if (typeof value !== "object") return [];

  const files: any[] = [];
  if (outputFileIdentity(value)) {
    const hasFileShape = ["fs_path", "path", "file_url", "document_url", "url", "public_url", "document_id", "filename", "original_name"].some((key) => value[key]);
    if (hasFileShape) files.push(value);
  }
  for (const key of RESULT_FILE_KEYS) {
    const nested = value[key];
    if (Array.isArray(nested)) files.push(...nested);
    else if (nested && typeof nested === "object") files.push(...collectOutputFiles(nested, depth + 1));
  }
  for (const key of ["result", "output", "data"]) {
    if (value[key] && typeof value[key] === "object") {
      files.push(...collectOutputFiles(value[key], depth + 1));
    }
  }
  return files;
}

function extractOutputSummary(result?: Record<string, any> | null) {
  if (!result) return null;
  const summary = textFromResult(result) || compactStructuredPreview(result);
  const files = dedupeOutputFiles(collectOutputFiles(result));
  return {
    summary,
    files: files
      .filter((file: any) => file && typeof file === "object")
      .slice(0, 4),
  };
}

function isLocalCodeStep(step: ExecutionStep) {
  return step.kind === "code" || step.action_key === "code.run" || step.provider === "codex_cli" || step.provider === "claude_code";
}

function localCodeEvents(result?: Record<string, any> | null) {
  const events = Array.isArray(result?.events) ? result?.events : [];
  return events
    .filter((event: any) => event && typeof event === "object")
    .slice(-16);
}

function localCodeEventLabel(event: any) {
  return formatUserFacingLabel(String(event.raw_type || event.type || "event")).slice(0, 96);
}

function localCodeEventText(event: any) {
  const text = formatUserFacingStructuredText(event.message || event.tool_name || event.status || "");
  return text.length > 420 ? `${text.slice(0, 420).trimEnd()}...` : text;
}

function LocalCodeRunPanel({ step }: { step: ExecutionStep }) {
  const result = step.result || {};
  const events = localCodeEvents(result);
  const sessionId = String(result.session_id || result.input_session_id || step.params?.session_id || "").trim();
  const changedFiles = Array.isArray(result.changed_files) ? result.changed_files.map((v: any) => String(v)) : [];
  const diffStat = String(result.diff_stat || "").trim();
  const stderr = String(result.stderr || "").trim();
  const hasDetails = events.length > 0 || sessionId || changedFiles.length > 0 || diffStat || stderr;
  if (!hasDetails) return null;

  return (
    <details
      open={step.step_status === "running"}
      style={{
        marginTop: 8,
        border: "1px solid rgba(67,107,101,0.16)",
        borderRadius: 10,
        background: "#fafaf9",
        overflow: "hidden",
      }}
    >
      <summary
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          padding: "8px 10px",
          cursor: "pointer",
          color: "#1c1917",
          fontSize: 11.5,
          fontWeight: 800,
          userSelect: "none",
        }}
      >
        <IconCode size={13} style={{ color: "#436b65" }} />
        {t("component.task_execution_timeline.local_coding_run")}
        {sessionId && (
          <span style={{ color: "#78716c", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontWeight: 700 }}>
            {t("component.task_execution_timeline.session")} {sessionId.slice(-8)}
          </span>
        )}
      </summary>
      <div style={{ padding: "0 10px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
        {events.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {events.map((event: any, index: number) => (
              <div
                key={`${event.seq || index}-${event.raw_type || event.type || "event"}`}
                style={{
                  display: "grid",
                  gridTemplateColumns: "96px 1fr",
                  gap: 8,
                  alignItems: "start",
                  fontSize: 11,
                  lineHeight: 1.45,
                }}
              >
                <span style={{ color: "#78716c", fontWeight: 800 }}>
                  {localCodeEventLabel(event)}
                </span>
                <span style={{ color: "#44403c", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  {localCodeEventText(event) || " "}
                </span>
              </div>
            ))}
          </div>
        )}
        {changedFiles.length > 0 && (
          <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
            {changedFiles.slice(0, 10).map((file) => (
              <span
                key={file}
                style={{
                  padding: "2px 6px",
                  borderRadius: 7,
                  background: "#ffffff",
                  border: "1px solid rgba(67,107,101,0.18)",
                  color: "#436b65",
                  fontSize: 10,
                  fontWeight: 750,
                }}
              >
                {file}
              </span>
            ))}
          </div>
        )}
        {diffStat && (
          <pre style={{ margin: 0, color: "#57534e", fontSize: 10.5, lineHeight: 1.45, whiteSpace: "pre-wrap" }}>
            {diffStat}
          </pre>
        )}
        {stderr && (
          <pre style={{ margin: 0, color: "#883a35", fontSize: 10.5, lineHeight: 1.45, whiteSpace: "pre-wrap" }}>
            {formatUserFacingStructuredText(stderr).slice(0, 8000)}
          </pre>
        )}
      </div>
    </details>
  );
}

function outputFileIdentity(file: any): string {
  if (!file || typeof file !== "object") return String(file || "");
  const value = file.fs_path || file.saved_to || file.path || file.file_url || file.document_url || file.url || file.public_url || file.document_id || file.name || file.filename || file.original_name;
  return String(value || JSON.stringify(file));
}

function outputFileHref(file: any): string {
  if (!file || typeof file !== "object") return "";
  if (file.document_id) return `/viewer/${encodeURIComponent(String(file.document_id))}`;
  const external = file.url || file.public_url || file.file_url || file.document_url || file.path;
  if (/^https?:\/\//i.test(String(external || "")) || String(external || "").startsWith("blob:") || String(external || "").startsWith("data:")) {
    return String(external);
  }
  return "";
}

function outputFileLabel(file: any): string {
  return String(file?.name || file?.filename || file?.original_name || file?.fs_path || file?.saved_to || file?.path || file?.file_url || file?.document_url || file?.url || t("component.task_execution_timeline.file"));
}

function dedupeOutputFiles(files: any[]): any[] {
  const seen = new Set<string>();
  const out: any[] = [];
  for (const file of Array.isArray(files) ? files : []) {
    const key = outputFileIdentity(file);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(file);
  }
  return out;
}

interface TaskExecutionTimelineProps {
  plan?: ExecutionPlan | null;
  steps: ExecutionStep[];
  isLoading?: boolean;
  isPending?: boolean;
  note?: string;
  onApprovePlan?: (planId: string) => void;
  onRetryFailedSteps?: (planId: string, note?: string) => void;
  onRetryStep?: (stepId: string, note?: string) => void;
}

export default function TaskExecutionTimeline({
  plan,
  steps,
  isLoading = false,
  isPending = false,
  note,
  onApprovePlan,
  onRetryFailedSteps,
  onRetryStep,
}: TaskExecutionTimelineProps) {
  const [collapsed, setCollapsed] = useState(false);

  if (isLoading) {
    return (
      <div className="glass-card" style={{ padding: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, color: "#78716c", fontSize: 13 }}>
          <IconTimeline size={14} />
          {t("component.task_execution_timeline.loading_execution_timeline")}</div>
      </div>
    );
  }

  if (!plan) return null;

  const retryableSteps = steps.filter((step) => RETRYABLE_STEP_STATUSES.has(step.step_status));
  const failedRetryableSteps = steps.filter((step) => FAILED_RETRYABLE_STEP_STATUSES.has(step.step_status));
  const waitingRetryableSteps = steps.filter((step) => WAITING_RETRYABLE_STEP_STATUSES.has(step.step_status));
  const otherRetryableSteps = retryableSteps.filter(
    (step) => !FAILED_RETRYABLE_STEP_STATUSES.has(step.step_status) && !WAITING_RETRYABLE_STEP_STATUSES.has(step.step_status),
  );
  const hasFailedRetryableSteps = failedRetryableSteps.length > 0;
  const hasWaitingRetryableSteps = waitingRetryableSteps.length > 0;
  const hasOtherRetryableSteps = otherRetryableSteps.length > 0;
  const doneCount = steps.filter((step) => step.step_status === "done").length;
  const failedCount = steps.filter((step) => ["failed", "skipped", "cancelled"].includes(step.step_status)).length;
  const waitingCount = steps.filter((step) => step.step_status === "waiting_human").length;
  const canRetryPlan = retryableSteps.length > 0 && !TERMINAL_PLAN_STATUSES.has(plan.status);
  const canApprovePlan = plan.status === "pending_approval" && !!onApprovePlan;
  const trimmedNote = note?.trim() || undefined;
  const stepsByKey = new Map(steps.map((step) => [step.step_key, step]));
  const toggleCollapsed = () => setCollapsed((value) => !value);
  const retryPlanLabel = hasFailedRetryableSteps
    ? hasWaitingRetryableSteps || hasOtherRetryableSteps
      ? t("component.task_execution_timeline.retry_blocked_steps")
      : t("component.task_execution_timeline.retry_failed_steps")
    : hasWaitingRetryableSteps && !hasOtherRetryableSteps
      ? t("component.task_execution_timeline.resume_waiting_steps")
      : t("component.task_execution_timeline.retry_blocked_steps");
  const retryPlanTitle = trimmedNote
    ? hasFailedRetryableSteps
      ? hasWaitingRetryableSteps || hasOtherRetryableSteps
        ? t("component.task_execution_timeline.retry_blocked_steps_with_the_comment_box_as_note")
        : t("component.task_execution_timeline.retry_failed_steps_with_the_comment_box_as_note")
      : hasWaitingRetryableSteps && !hasOtherRetryableSteps
        ? t("component.task_execution_timeline.resume_waiting_steps_with_the_comment_box_as_input")
        : t("component.task_execution_timeline.retry_blocked_steps_with_the_comment_box_as_note")
    : retryPlanLabel;

  return (
    <div className="glass-card" style={{ padding: 0, overflow: "hidden" }}>
      <div
        role="button"
        tabIndex={0}
        aria-expanded={!collapsed}
        onClick={toggleCollapsed}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            toggleCollapsed();
          }
        }}
        style={{
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap",
          padding: "12px 20px",
          background: "rgba(250,250,249,0.68)",
          borderBottom: collapsed ? "none" : "1px solid rgba(28,25,23,0.06)",
          cursor: "pointer",
          userSelect: "none",
        }}
      >
        <div style={{ flex: 1, minWidth: 180 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <IconChevronRight
              size={12}
              style={{
                color: "#a8a29e",
                transform: collapsed ? "none" : "rotate(90deg)",
                transition: "transform 0.15s ease",
                flexShrink: 0,
              }}
            />
            <IconTimeline size={13} style={{ color: "#78716c" }} />
            <span className="manor-label" style={{ margin: 0 }}>{t("component.task_execution_timeline.execution_timeline")}</span>
          </div>
          <div style={{ fontSize: 11, color: "#78716c", marginTop: 2 }}>
            {t("component.task_execution_timeline.plan")}{shortId(plan.id)} · {PLAN_STATUS_LABELS[plan.status] || humanize(plan.status)}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          <span style={{ fontSize: 10, fontWeight: 750, padding: "3px 8px", borderRadius: 999, color: "#436b65", background: "#e5eeeb" }}>
            {doneCount}/{steps.length} {t("component.task_execution_timeline.done")}
          </span>
          {failedCount > 0 && (
            <span style={{ fontSize: 10, fontWeight: 750, padding: "3px 8px", borderRadius: 999, color: "#be123c", background: "#ffe4e6" }}>
              {failedCount} {t("component.task_execution_timeline.failed")}
            </span>
          )}
          {waitingCount > 0 && (
            <span style={{ fontSize: 10, fontWeight: 750, padding: "3px 8px", borderRadius: 999, color: "#b27c34", background: "#f3ecd6" }}>
              {waitingCount} {t("component.task_execution_timeline.waiting")}
            </span>
          )}
          {canApprovePlan && (
            <button
              type="button"
              className="btn-manor"
              onClick={(event) => {
                event.stopPropagation();
                onApprovePlan?.(plan.id);
              }}
              onKeyDown={(event) => event.stopPropagation()}
              disabled={isPending}
              style={{ height: 30, padding: "0 12px", fontSize: 12, flexShrink: 0 }}
              title={t("component.task_execution_timeline.approve_plan_and_run")}
            >
              <IconCheck size={12} />
              {isPending ? t("component.task_execution_timeline.approving") : t("component.task_execution_timeline.approve_and_run")}
            </button>
          )}
          {canRetryPlan && onRetryFailedSteps && (
            <button
              type="button"
              className="btn-manor"
              onClick={(event) => {
                event.stopPropagation();
                onRetryFailedSteps(plan.id, trimmedNote);
              }}
              onKeyDown={(event) => event.stopPropagation()}
              disabled={isPending}
              style={{ height: 30, padding: "0 12px", fontSize: 12, flexShrink: 0 }}
              title={retryPlanTitle}
            >
              <IconRefresh size={12} />
              {isPending ? t("component.task_execution_timeline.retrying") : retryPlanLabel}
            </button>
          )}
        </div>
      </div>

      {!collapsed && (
      <div style={{ padding: "10px 16px 14px" }}>
        {steps.length === 0 ? (
          <div style={{ fontSize: 12, color: "#a8a29e", padding: "8px 4px" }}>{t("component.task_execution_timeline.no_execution_steps_recorded_yet")}</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column" }}>
            {steps.map((step, index) => {
              const meta = STEP_STATUS_META[step.step_status] || STEP_STATUS_META.pending;
              const Icon = meta.Icon;
              const canRetryStep = RETRYABLE_STEP_STATUSES.has(step.step_status) && !TERMINAL_PLAN_STATUSES.has(plan.status) && !!onRetryStep;
              const isWaitingRetryStep = WAITING_RETRYABLE_STEP_STATUSES.has(step.step_status);
              const output = extractOutputSummary(step.result);
              const executor = stepExecutor(step);
              return (
                <div
                  key={step.id}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "28px 1fr auto",
                    gap: 10,
                    alignItems: "start",
                    padding: "10px 4px",
                    borderTop: index > 0 ? "1px solid rgba(231,229,228,0.45)" : "none",
                  }}
                >
                  <div style={{ position: "relative", display: "flex", justifyContent: "center" }}>
                    {index < steps.length - 1 && (
                      <span style={{ position: "absolute", top: 24, width: 1, height: 28, background: "rgba(214,211,209,0.75)" }} />
                    )}
                    <div style={{ width: 22, height: 22, borderRadius: "50%", background: meta.bg, color: meta.color, display: "flex", alignItems: "center", justifyContent: "center", border: `1px solid ${meta.color}22`, zIndex: 1 }}>
                      <Icon size={11} />
                    </div>
                  </div>

                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" }}>
                      <span style={{ fontSize: 13, fontWeight: 750, color: "#1c1917" }}>
                        {humanize(step.step_key)}
                      </span>
                      <span style={{ fontSize: 10, fontWeight: 750, padding: "2px 7px", borderRadius: 999, color: meta.color, background: meta.bg }}>
                        {meta.label}
                      </span>
                      {executor && (
                        <span
                          title={executor.title}
                          style={{
                            display: "inline-flex", alignItems: "center", gap: 5,
                            minWidth: 0, maxWidth: 240,
                            padding: "2px 7px 2px 3px", borderRadius: 999,
                            background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                            color: "#1c1917", fontSize: 10, fontWeight: 750,
                          }}
                        >
                          <UserAvatar
                            name={executor.name}
                            avatarUrl={executor.avatarUrl}
                            type="agent"
                            seed={executor.id}
                            size={15}
                            style={{ border: "1px solid rgba(255,255,255,0.9)" }}
                          />
                          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {executor.name}
                          </span>
                        </span>
                      )}
                      <span style={{ fontSize: 10, color: "#a8a29e", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}>
                        {t("component.task_execution_timeline.step")}{shortId(step.id)}
                      </span>
                    </div>
                    <div style={{ fontSize: 11, color: "#78716c", lineHeight: 1.5, marginTop: 3 }}>
                      {stepSubtitle(step) || t("component.task_execution_timeline.plan_step")}
                      {step.attempt_count > 0 && (
                        <span style={{ marginLeft: 8, color: "#a8a29e" }}>
                          {t("component.task_execution_timeline.attempt")} {step.attempt_count}/{step.max_attempts}
                        </span>
                      )}
                    </div>
                    {(step.depends_on || []).length > 0 && (
                      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
                        <span style={{ fontSize: 10, color: "#a8a29e", fontWeight: 750, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                          {t("component.task_execution_timeline.depends_on")}
                        </span>
                        {(step.depends_on || []).map((depKey) => (
                          <span
                            key={depKey}
                            style={{
                              display: "inline-flex", alignItems: "center", gap: 4,
                              padding: "2px 7px", borderRadius: 999,
                              background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                              color: "#57534e", fontSize: 10, fontWeight: 700,
                            }}
                          >
                            {stepKeyLabel(depKey, stepsByKey)}
                          </span>
                        ))}
                      </div>
                    )}
                    {output && (output.summary || output.files.length > 0) && (
                      <div style={{
                        marginTop: 7, padding: "7px 9px", borderRadius: 10,
                        background: "rgba(250,250,249,0.78)", border: "1px solid rgba(28,25,23,0.06)",
                        color: "#57534e", fontSize: 11, lineHeight: 1.45,
                      }}>
                        <div style={{ fontWeight: 800, marginBottom: output.summary ? 3 : 0 }}>
                          {t("component.task_execution_timeline.output")}
                        </div>
                        {output.summary && (
                          <div
                            className="task-step-output-markdown"
                            style={{
                              color: "#44403c",
                              maxHeight: 220,
                              overflowY: "auto",
                              overflowX: "auto",
                            }}
                          >
                            <ChatMarkdown content={formatUserFacingStructuredText(output.summary)} />
                          </div>
                        )}
                        {output.files.length > 0 && (
                          <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginTop: output.summary ? 6 : 0 }}>
                            {output.files.map((file: any, fileIndex: number) => {
                              const href = outputFileHref(file);
                              const fileLabel = outputFileLabel(file);
                              const chipStyle = {
                                padding: "2px 6px", borderRadius: 7,
                                background: "white", border: "1px solid rgba(79,125,117,0.18)",
                                color: "#436b65", fontSize: 10, fontWeight: 750,
                                textDecoration: "none",
                              } as const;
                              return href ? (
                                <a
                                  key={`${outputFileIdentity(file) || fileIndex}`}
                                  href={href}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  style={chipStyle}
                                >
                                  {fileLabel}
                                </a>
                              ) : (
                                <span
                                  key={`${outputFileIdentity(file) || fileIndex}`}
                                  style={chipStyle}
                                >
                                  {fileLabel}
                                </span>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    )}
                    {isLocalCodeStep(step) && <LocalCodeRunPanel step={step} />}
                    {step.error && (
                      <div style={{
                        marginTop: 6, padding: "6px 8px", borderRadius: 8,
                        background: "#f8f0ef", border: "1px solid #ecc8c5",
                        color: "#883a35", fontSize: 11, lineHeight: 1.45,
                      }}>
                        {formatUserFacingLabel(String((step.error as any).error_type || (step.error as any).type || t("component.task_execution_timeline.error")))}
                        {((step.error as any).message || (step.error as any).error) ? `: ${formatUserFacingStructuredText((step.error as any).message || (step.error as any).error)}` : ""}
                      </div>
                    )}
                    {step.human_input_prompt && (
                      <div style={{
                        marginTop: 6, padding: "6px 8px", borderRadius: 8,
                        background: "#faf7ef", border: "1px solid #ecdca4",
                        color: "#76502c", fontSize: 11, lineHeight: 1.45,
                      }}>
                        {formatUserFacingStructuredText(step.human_input_prompt)}
                      </div>
                    )}
                  </div>

                  {canRetryStep && (
                    <button
                      type="button"
                      onClick={() => onRetryStep?.(step.id, trimmedNote)}
                      disabled={isPending}
                      style={{
                        display: "inline-flex", alignItems: "center", gap: 5,
                        height: 28, padding: "0 10px", borderRadius: 8,
                        border: "1px solid rgba(67,107,101,0.22)", background: "#fff",
                        color: "#436b65", cursor: isPending ? "default" : "pointer",
                        fontSize: 11, fontWeight: 750, whiteSpace: "nowrap",
                      }}
                      title={trimmedNote
                        ? isWaitingRetryStep
                          ? t("component.task_execution_timeline.resume_this_step_with_the_comment_box_as_input")
                          : t("component.task_execution_timeline.retry_this_step_with_the_comment_box_as_note")
                        : isWaitingRetryStep
                          ? t("component.task_execution_timeline.resume_this_step")
                          : t("component.task_execution_timeline.retry_this_step")}
                    >
                      <IconRefresh size={11} />
                      {isWaitingRetryStep ? t("component.task_execution_timeline.resume") : t("page.announcements.retry")}</button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
      )}
    </div>
  );
}
