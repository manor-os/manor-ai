import { useEffect, useState } from "react";
import type { TaskLog } from "../../lib/types";
import { IconError, IconPlay, IconRefresh, IconWarning } from "../icons";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingStructuredText } from "../../lib/taskDisplay";


type RecoverableStatus = "waiting_on_customer" | "blocked" | "failed";

interface TaskRecoveryPanelProps {
  status: string;
  logs: TaskLog[];
  comment?: string;
  detailReason?: string;
  isPending?: boolean;
  variant?: "detail" | "compact";
  onRetry: (note?: string) => void;
  onRespond?: (response: string, fields?: Record<string, string>) => void;
}

interface HITLField {
  name: string;
  label?: string;
  type?: "text" | "textarea" | "select";
  required?: boolean;
  placeholder?: string;
  options?: string[];
}

function isRecoverableStatus(status: string): status is RecoverableStatus {
  return status === "waiting_on_customer" || status === "blocked" || status === "failed";
}

function findLatestRecoveryEvent(logs: TaskLog[], status: RecoverableStatus) {
  const targetType =
    status === "waiting_on_customer"
      ? "ai_hitl_requested"
      : status === "failed"
        ? "ai_execution_failed"
        : "ai_needs_replan";
  return [...logs].reverse().find((log) => log.log_type === targetType);
}

function hitlFieldsFromLog(log?: TaskLog): HITLField[] {
  const fields = log?.meta?.hitl?.fields;
  return Array.isArray(fields) ? fields.filter((field) => field?.name) : [];
}

function fieldLabel(field: HITLField) {
  return formatUserFacingLabel(field.label || field.name);
}

export default function TaskRecoveryPanel({
  status,
  logs,
  comment = "",
  detailReason = "",
  isPending = false,
  variant = "detail",
  onRetry,
  onRespond,
}: TaskRecoveryPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const isRecoverable = isRecoverableStatus(status);
  const lastEvent = isRecoverable ? findLatestRecoveryEvent(logs, status) : undefined;

  useEffect(() => {
    setExpanded(false);
  }, [status]);

  useEffect(() => {
    setFieldValues({});
  }, [lastEvent?.id]);

  if (!isRecoverable) return null;

  const isWaiting = status === "waiting_on_customer";
  const isFailed = status === "failed";
  const reason = formatUserFacingStructuredText(detailReason || lastEvent?.meta?.reason || lastEvent?.content || "");
  const question = formatUserFacingStructuredText(lastEvent?.meta?.question || "");
  const hitlFields = isWaiting ? hitlFieldsFromLog(lastEvent) : [];
  const hasStructuredHITL = hitlFields.length > 0;
  const hasDetails = Boolean(question || (reason && reason !== question) || hasStructuredHITL);
  const StatusIcon = isFailed ? IconError : IconWarning;
  const ActionIcon = isWaiting ? IconPlay : IconRefresh;

  const palette = isWaiting
    ? { bg: "#faf7ef", border: "#ecdca4", text: "#76502c", icon: "#b27c34", iconBg: "#f3ecd6" }
    : isFailed
      ? { bg: "#f8f0ef", border: "#ecc8c5", text: "#883a35", icon: "#c14a44", iconBg: "#f1dddb" }
      : { bg: "#f9f4ec", border: "#ecdac2", text: "#7c4a2e", icon: "#b66a3c", iconBg: "#ffedd5" };

  const title = isWaiting ? t("component.task_recovery_panel.agent_waiting_for_input") : isFailed ? t("component.task_recovery_panel.agent_run_failed") : t("component.task_recovery_panel.agent_is_blocked");
  const compactTitle = isWaiting ? t("component.task_recovery_panel.waiting_for_input") : title;
  const retryLabel = isWaiting ? t("component.task_recovery_panel.resume_agent") : t("component.task_recovery_panel.retry_task");
  const compactRetryLabel = isWaiting ? t("component.task_recovery_panel.resume") : t("component.task_recovery_panel.retry");
  const structuredFields = Object.fromEntries(
    Object.entries(fieldValues).filter(([, value]) => value.trim()),
  );
  const responseValue = (fieldValues.response || comment).trim();
  const missingRequired = hitlFields.some((field) => field.required && !String(fieldValues[field.name] || "").trim());
  const hasResponsePayload = Boolean(responseValue || Object.keys(structuredFields).length);
  const actionDisabled = isPending || (isWaiting && !!onRespond && (!hasResponsePayload || missingRequired));
  const helper = isWaiting
    ? hasStructuredHITL
      ? t("component.task_recovery_panel.fill_requested_fields")
      : variant === "compact" ? t("component.task_recovery_panel.add_response_comment_box") : t("component.task_recovery_panel.add_guidance_comment_box")
    : isFailed
      ? variant === "compact" ? t("component.task_recovery_panel.review_details_retry") : t("component.task_recovery_panel.review_error_adjust_retry")
      : variant === "compact" ? t("component.task_recovery_panel.review_details_retry") : t("component.task_recovery_panel.edit_description_retry");

  const submitAction = () => {
    if (isWaiting && onRespond) {
      onRespond(responseValue, structuredFields);
    } else {
      onRetry(comment.trim() || undefined);
    }
  };

  const renderStructuredFields = (compact = false) => {
    if (!isWaiting || !hasStructuredHITL) return null;
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: compact ? 6 : 8, marginTop: compact ? 8 : 10 }}>
        {hitlFields.map((field) => {
          const commonStyle = {
            width: "100%",
            border: "1px solid rgba(28,25,23,0.06)",
            borderRadius: compact ? 7 : 8,
            padding: compact ? "6px 8px" : "8px 10px",
            fontSize: compact ? 11 : 12,
            color: "#1c1917",
            background: "#fff",
            outline: "none",
            boxSizing: "border-box" as const,
          };
          return (
            <label key={field.name} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: compact ? 10 : 11, fontWeight: 750, color: "#57534e", textTransform: "capitalize" }}>
                {fieldLabel(field)}
                {field.required && <span style={{ color: "#c14a44" }}> *</span>}
              </span>
              {field.type === "select" ? (
                <select
                  value={fieldValues[field.name] || ""}
                  onChange={(e) => setFieldValues((prev) => ({ ...prev, [field.name]: e.target.value }))}
                  style={commonStyle}
                >
                  <option value="">{t("page.skill_form.select")}</option>
                  {(field.options || []).map((option) => (
                    <option key={option} value={option}>{formatUserFacingStructuredText(option)}</option>
                  ))}
                </select>
              ) : field.type === "textarea" ? (
                <textarea
                  value={fieldValues[field.name] || ""}
                  onChange={(e) => setFieldValues((prev) => ({ ...prev, [field.name]: e.target.value }))}
                  rows={compact ? 2 : 3}
                  placeholder={formatUserFacingStructuredText(field.placeholder)}
                  style={{ ...commonStyle, resize: "vertical", lineHeight: 1.5, fontFamily: "inherit" }}
                />
              ) : (
                <input
                  value={fieldValues[field.name] || ""}
                  onChange={(e) => setFieldValues((prev) => ({ ...prev, [field.name]: e.target.value }))}
                  placeholder={formatUserFacingStructuredText(field.placeholder)}
                  style={commonStyle}
                />
              )}
            </label>
          );
        })}
        {missingRequired && (
          <div style={{ fontSize: compact ? 10 : 11, color: "#a23e38" }}>{t("component.task_recovery_panel.please_complete_required_fields_before_resuming")}</div>
        )}
      </div>
    );
  };

  if (variant === "compact") {
    return (
      <div className={`task-recovery-panel task-recovery-panel--compact task-recovery-panel--${status}`} style={{
        border: `1px solid ${palette.border}`,
        background: palette.bg,
        borderRadius: 10,
        padding: 10,
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 8,
      }}>
        <div className="task-recovery-icon" style={{ width: 28, height: 28, borderRadius: 8, background: palette.iconBg, color: palette.icon, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          <StatusIcon size={14} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="task-recovery-title" style={{ fontSize: 12, fontWeight: 750, color: "#1c1917" }}>{compactTitle}</div>
          {expanded && <div className="task-recovery-helper" style={{ fontSize: 11, color: "#78716c", lineHeight: 1.45, marginTop: 2 }}>{helper}</div>}
        </div>
        {hasDetails && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? t("page.tasks.hide_details") : t("component.task_recovery_panel.show_details")}
            style={{
              padding: "6px 8px", borderRadius: 8, border: "1px solid rgba(28,25,23,0.06)",
              background: "#fff", color: "#57534e", cursor: "pointer",
              fontSize: 11, fontWeight: 650, whiteSpace: "nowrap",
            }}
          >
            {expanded ? t("page.team_people.hide") : t("page.agent_detail.details")}
          </button>
        )}
        <button
          type="button"
          onClick={submitAction}
          disabled={actionDisabled}
          title={retryLabel}
          style={{
            display: "inline-flex", alignItems: "center", gap: 5,
            padding: "6px 10px", borderRadius: 8, border: "none",
            background: actionDisabled ? "#a8a29e" : "#436b65", color: "#fff", cursor: actionDisabled ? "default" : "pointer",
            fontSize: 11, fontWeight: 700, whiteSpace: "nowrap",
          }}
        >
          <ActionIcon size={12} />
          {isPending ? t("component.task_recovery_panel.starting") : compactRetryLabel}
        </button>
        {expanded && (question || reason) && (
          <div className="task-recovery-details" style={{
            width: "100%", marginTop: 8, padding: "8px 10px", borderRadius: 8,
            background: "rgba(255,255,255,0.72)", color: question ? "#292524" : "#78716c",
            fontSize: 11, lineHeight: 1.5,
          }}>
            {question || reason}
            {question && reason && reason !== question && <div style={{ marginTop: 6, color: "#78716c" }}>{reason}</div>}
          </div>
        )}
        {expanded && renderStructuredFields(true)}
      </div>
    );
  }

  return (
    <div className={`task-recovery-panel task-recovery-panel--detail task-recovery-panel--${status}`} style={{
      background: palette.bg,
      border: `1px solid ${palette.border}`,
      borderRadius: 12,
      padding: 12,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div className="task-recovery-icon" style={{ width: 30, height: 30, borderRadius: 8, background: palette.iconBg, color: palette.icon, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          <StatusIcon size={15} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="task-recovery-title" style={{ fontSize: 13, fontWeight: 750, color: "#1c1917" }}>{title}</div>
          {expanded && <div className="task-recovery-helper" style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5, marginTop: 2 }}>{helper}</div>}
        </div>
        {hasDetails && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="btn-manor-ghost"
            style={{ fontSize: 12, height: 30, padding: "0 10px", flexShrink: 0 }}
          >
            {expanded ? t("page.team_people.hide") : t("page.agent_detail.details")}
          </button>
        )}
        <button
          className="btn-manor"
          style={{ fontSize: 12, padding: "0 14px", height: 30, flexShrink: 0 }}
          onClick={submitAction}
          disabled={actionDisabled}
        >
          <ActionIcon size={12} />
          {isPending ? t("page.flows.starting") : retryLabel}
        </button>
      </div>
      {expanded && question && (
        <div className="task-recovery-details" style={{
          fontSize: 14, color: "#292524", lineHeight: 1.55,
          padding: "10px 12px", background: "#ffffff",
          borderRadius: 8, marginTop: 10, fontWeight: 500,
        }}>
          {question}
        </div>
      )}
      {expanded && reason && reason !== question && (
        <div className="task-recovery-reason" style={{ fontSize: 12, color: palette.text, lineHeight: 1.5, marginTop: 8 }}>{reason}</div>
      )}
      {renderStructuredFields(false)}
    </div>
  );
}
