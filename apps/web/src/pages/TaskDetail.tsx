import { useState, useEffect, useMemo, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useNavigate, useLocation } from "react-router-dom";
import { api } from "../lib/api";
import type { Task, Agent } from "../lib/types";
import { relativeTime, formatDateFull, formatDateLong, isDeadlineOverdue } from "../lib/format";
import { useAuthStore } from "../stores/auth";
import { useToastStore } from "../stores/toast";
import { MANOR_AGENT_ID, MANOR_AGENT_TYPE, MANOR_AGENT_NAME, isMasterAgent } from "../lib/constants";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import Select from "../components/ui/Select";
import DateTimePicker from "../components/ui/DateTimePicker";
import UserAvatar from "../components/ui/UserAvatar";
import InlineTips from "../components/ui/InlineTips";
import Modal from "../components/ui/Modal";
import Button from "../components/ui/Button";
import ChatMarkdown from "../components/ChatMarkdown";
import WorkspaceChat from "../components/WorkspaceChat";
import StatusPill from "../components/ui/StatusPill";
import PriorityPill from "../components/ui/PriorityPill";
import { STATUS_CONFIG } from "../components/ui/StatusPill";
import { PRIORITY_CONFIG } from "../components/ui/PriorityPill";
import { CATEGORIES } from "../lib/taskCategories";
import CategoryChip from "../components/ui/CategoryChip";
import TaskPropertiesPanel from "../components/task/TaskPropertiesPanel";
import TaskLogItem from "../components/task/TaskLogItem";
import TaskRecoveryPanel from "../components/task/TaskRecoveryPanel";
import TaskExecutionTimeline from "../components/task/TaskExecutionTimeline";
import { t } from "../lib/i18n";
import { getAgentDescription } from "../lib/localizedContent";
import ChatInputFooter, { type MentionOption, type AttachedItem } from "../components/ChatInputFooter";
import { inferRuntimeRuleFromText, shouldFallbackToWildcardRule } from "../lib/runtimeRules";
import { formatTaskDescriptionForDisplay, formatUserFacingLabel, formatUserFacingStructuredText, formatUserFacingText, friendlyPersonName } from "../lib/taskDisplay";
import {
  IconArrowLeft, IconClock, IconEdit, IconUser, IconAgent,
  IconCalendar, IconSend, IconFlag, IconCategory,
  IconCancel, IconTimeline, IconComment, IconCircleDot, IconUpload,
  IconDownload, IconDocument, IconManorLogo, IconList, IconTrash,
  IconChevronLeft, IconChevronRight,
} from "../components/icons";

/* ── Constants ──
   STATUS_CONFIG / PRIORITY_CONFIG / CATEGORIES are imported from the
   shared UI primitives so this page, the kanban drawer (Tasks.tsx),
   and the filter pills all stay in sync. Adding a new status / level
   / category happens in one place. */

const LOG_ICONS: Record<string, { color: string; icon: string }> = {
  ai_execution_started:   { color: "#5f84bd", icon: "M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z" },
  ai_agent_turn:          { color: "#9079c2", icon: "M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" },
  ai_supervisor_verdict:  { color: "#cf9b44", icon: "M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" },
  ai_execution_completed: { color: "#4f9c84", icon: "M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" },
  ai_execution_failed:    { color: "#d65f59", icon: "M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" },
  ai_hitl_requested:      { color: "#d3873f", icon: "M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" },
  ai_hitl_resumed:        { color: "#57534e", icon: "M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" },
  ai_needs_replan:        { color: "#a07fc0", icon: "M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126z" },
  status_change:          { color: "#6d6fb2", icon: "M7.5 21L3 16.5m0 0L7.5 12M3 16.5h13.5m0-13.5L21 7.5m0 0L16.5 12M21 7.5H7.5" },
  comment:                { color: "#4a7d96", icon: "M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" },
  evaluation:             { color: "#c3a63f", icon: "M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" },
};

/* StatusPill / PriorityPill / CATEGORIES are now imported from the
   shared UI primitives (with per-value icons). The inline
   implementations were deleted to avoid drift. */

const TASK_DETAIL_POLL_INTERVAL_MS = 60_000;
const LIVE_TASK_STATUSES = new Set(["pending", "in_progress"]);
const LIVE_PLAN_STATUSES = new Set(["pending_approval", "running", "paused", "needs_attention"]);
const LIVE_STEP_STATUSES = new Set(["pending", "running", "waiting_human"]);

function getTaskReturnTo(state: unknown): string | null {
  if (!state || typeof state !== "object") return null;
  const value = (state as { chatReturnTo?: unknown; returnTo?: unknown }).chatReturnTo
    ?? (state as { returnTo?: unknown }).returnTo;
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    ? value
    : null;
}
const HITL_RESUMABLE_PLAN_STATUSES = new Set(["running", "paused", "needs_attention"]);

function hasLiveTaskStatus(task: unknown): boolean {
  return LIVE_TASK_STATUSES.has(String((task as any)?.status || ""));
}

function hasLivePlanStatus(value: unknown): boolean {
  const plans = Array.isArray(value) ? value : [];
  return plans.some((plan: any) => LIVE_PLAN_STATUSES.has(String(plan?.status || "")));
}

function hasLiveStepStatus(value: unknown): boolean {
  const steps = Array.isArray(value) ? value : [];
  return steps.some((step: any) => LIVE_STEP_STATUSES.has(String(step?.step_status || step?.status || "")));
}

function canResumeStructuredHumanInput(task: Task | null | undefined, plan: any | null, steps: any[]): boolean {
  if (!task) return false;
  const planCanResume = HITL_RESUMABLE_PLAN_STATUSES.has(String(plan?.status || ""))
    && (Array.isArray(steps) ? steps : []).some((step: any) => String(step?.step_status || step?.status || "") === "waiting_human");
  if (planCanResume) return true;
  const hasLegacyHitlAgent = Boolean(task.agent_id || isMasterAgent(task.agent_id, task.agent_type));
  return task.status === "waiting_on_customer" && hasLegacyHitlAgent;
}

function outputFileIdentity(file: any): string {
  if (!file || typeof file !== "object") return String(file || "");
  const value = file.fs_path || file.saved_to || file.path || file.file_url || file.document_url || file.url || file.public_url || file.document_id || file.name || file.filename || file.original_name;
  return String(value || JSON.stringify(file));
}

function outputFileLabel(file: any, fallback = "File"): string {
  if (!file || typeof file !== "object") return String(file || fallback);
  const explicit = file.name || file.filename || file.original_name || file.title;
  if (explicit) return String(explicit);
  const pathish = file.fs_path || file.saved_to || file.path || file.file_url || file.document_url || file.url || file.public_url || file.document_id;
  if (pathish) {
    const parts = String(pathish).split(/[\\/]/).filter(Boolean);
    return parts[parts.length - 1] || String(pathish);
  }
  return String(file.type || fallback);
}

function isExternalOutputUrl(value: any): boolean {
  const text = String(value || "").trim();
  return /^https?:\/\//i.test(text) || text.startsWith("blob:") || text.startsWith("data:");
}

function normalizeOutputPath(value: any): string {
  return String(value || "").trim().replace(/\\/g, "/").replace(/^\/+/, "");
}

function outputFileDocumentId(file: any): string {
  if (!file || typeof file !== "object") return "";
  return String(file.document_id || file.documentId || file.doc_id || "").trim();
}

function outputFileLookupPath(file: any): string {
  if (!file || typeof file !== "object") return "";
  const value = file.fs_path || file.saved_to || file.path || file.file_url || file.document_url || file.result_url || file.output_url || file.url;
  if (isExternalOutputUrl(value)) return "";
  const path = normalizeOutputPath(value);
  if (!path) return "";
  return path;
}

function outputFileExternalUrl(file: any): string {
  if (!file || typeof file !== "object") return "";
  const value = String(file.url || file.public_url || file.file_url || file.document_url || file.image_url || "").trim();
  if (!value) return "";
  if (/^https?:\/\//i.test(value) || value.startsWith("blob:") || value.startsWith("data:")) return value;
  return "";
}

const OUTPUT_INLINE_CONTENT_KEYS = [
  "markdown_document",
  "markdown",
  "html_document",
  "html",
  "json_document",
  "csv_document",
  "document",
  "content",
  "text",
  "body",
];

function outputFileInlineContent(value: any): string {
  if (!value || typeof value !== "object") return "";
  for (const key of OUTPUT_INLINE_CONTENT_KEYS) {
    const content = value[key];
    if (typeof content === "string" && content.trim()) return content;
  }
  return "";
}

function inferOutputFilePreviewType(label: string, path: string, source: any): { file_type: string; mime_type: string } {
  const ext = (label || path || "").split(".").pop()?.toLowerCase() || "";
  const explicitType = String(source?.file_type || source?.type || "").toLowerCase();
  const explicitMime = String(source?.mime_type || source?.mime || "").toLowerCase();
  if (explicitMime) {
    return { file_type: explicitType || ext || "text", mime_type: explicitMime };
  }
  if (ext === "md" || ext === "markdown" || source?.markdown_document || source?.markdown) return { file_type: "markdown", mime_type: "text/markdown" };
  if (ext === "html" || ext === "htm" || source?.html_document || source?.html) return { file_type: "html", mime_type: "text/html" };
  if (ext === "json" || source?.json_document) return { file_type: "json", mime_type: "application/json" };
  if (ext === "csv" || source?.csv_document) return { file_type: "csv", mime_type: "text/csv" };
  return { file_type: explicitType || ext || "text", mime_type: "text/plain" };
}

function buildTaskOutputPreview(source: any, fallbackLabel: string, fallbackPath = "", fallbackId = "") {
  const content = outputFileInlineContent(source);
  if (!content) return null;
  const path = outputFileLookupPath(source) || fallbackPath;
  const label = outputFileLabel(source, fallbackLabel);
  const docId = outputFileDocumentId(source) || fallbackId;
  const inferred = inferOutputFilePreviewType(label, path, source);
  return {
    id: docId || path || outputFileIdentity(source),
    name: label,
    fs_path: path,
    content,
    ...inferred,
  };
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

function logTimestamp(log: any): number {
  const parsed = Date.parse(String(log?.created_at || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function latestActionableInputRequest(logs: any[], taskOutput?: Record<string, any> | null, taskStatus?: string) {
  if (taskStatus === "completed" || taskStatus === "cancelled") {
    return null;
  }
  const sorted = [...(logs || [])].sort((a, b) => logTimestamp(a) - logTimestamp(b));
  let request: any | null = null;
  for (const log of sorted) {
    const meta = log?.meta || {};
    if (log?.log_type === "ai_hitl_resumed") {
      const requestPlanId = request?.meta?.plan_id;
      const resumePlanId = meta.plan_id;
      if (!requestPlanId || !resumePlanId || requestPlanId === resumePlanId) {
        request = null;
      }
      continue;
    }
    if (log?.log_type === "ai_hitl_requested" || log?.log_type === "ai_hitl_reminder") {
      request = log;
    }
  }
  if (!request && taskOutput?.needs_input) {
    request = {
      content: t("page.task_detail.input_request_missing_artifact"),
      meta: { verdict: taskOutput.supervisor_verdict || "needs_human" },
    };
  }
  return request;
}

function inputRequestPrompt(log: any): string {
  const meta = log?.meta || {};
  return String(meta.question || meta.prompt || meta.reason || log?.content || "").trim();
}

function localizedRuntimeRuleDescription(rule: any): string {
  const patterns = Array.isArray(rule.action_patterns) ? rule.action_patterns : [];
  if (rule.source === "quick_action") {
    if (
      rule.rule_type === "approval_required"
      && patterns.includes("social_post.publish")
      && patterns.includes("email.send")
    ) {
      return t("page.task_detail.runtime.quick_require_approval_description");
    }
    if (
      rule.rule_type === "draft_only"
      && patterns.includes("social_post.publish")
      && patterns.includes("email.send")
    ) {
      return t("page.task_detail.runtime.quick_draft_only_description");
    }
    if (
      rule.rule_type === "deny"
      && patterns.includes("workspace.file.modify")
      && patterns.includes("workspace.file.delete")
    ) {
      return t("page.task_detail.runtime.quick_add_files_only_description");
    }
  }
  return rule.description || rule.rule_type;
}

type TaskDependencyDetail = {
  dependencyIds: string[];
  statuses: Record<string, string>;
  outputs: any[];
  status: "completed" | "blocked" | "waiting";
};

function taskDependencyDetail(task?: Task | null): TaskDependencyDetail | null {
  const details = task?.details || {};
  const dependencyIds = Array.isArray(details.depends_on_task_ids)
    ? details.depends_on_task_ids.map((id: unknown) => String(id || "").trim()).filter(Boolean)
    : [];
  const outputs = Array.isArray(details.dep_outputs) ? details.dep_outputs : [];
  if (dependencyIds.length === 0 && outputs.length === 0) return null;
  const rawStatuses = details.dependency_statuses && typeof details.dependency_statuses === "object"
    ? details.dependency_statuses
    : {};
  const statuses = Object.fromEntries(
    Object.entries(rawStatuses).map(([id, status]) => [id, String(status || "")]),
  );
  const rawStatus = String(details.dependency_status || "");
  const status: TaskDependencyDetail["status"] =
    rawStatus === "blocked" ? "blocked" : rawStatus === "completed" ? "completed" : "waiting";
  return { dependencyIds, statuses, outputs, status };
}

function humanizeServiceKey(value?: string | null): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  return raw
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b[a-z]/g, (char) => char.toUpperCase());
}

function dependencyTone(status: TaskDependencyDetail["status"]) {
  if (status === "blocked") {
    return {
      label: t("page.tasks.dependency_blocked"),
      bg: "#f8f0ef",
      border: "#ecc8c5",
      fg: "#a23e38",
      dot: "#d65f59",
      note: t("page.task_detail.dependency_blocked_note"),
    };
  }
  if (status === "completed") {
    return {
      label: t("page.tasks.dependency_ready"),
      bg: "#f5f5f4",
      border: "#d6d3d1",
      fg: "#436b65",
      dot: "#5f928a",
      note: t("page.task_detail.dependency_ready_note"),
    };
  }
  return {
    label: t("page.tasks.dependency_waiting"),
    bg: "#faf7ef",
    border: "#ecdca4",
    fg: "#76502c",
    dot: "#cf9b44",
    note: t("page.tasks.waiting_to_start_when_ready"),
  };
}

function TaskDependencyPanel({ task, onOpenTask }: { task: Task; onOpenTask: (taskId: string) => void }) {
  const info = taskDependencyDetail(task);
  if (!info) return null;
  const tone = dependencyTone(info.status);

  return (
    <div className="glass-card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
        padding: "12px 20px", background: "rgba(250,250,249,0.72)", borderBottom: "1px solid rgba(28,25,23,0.06)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <IconTimeline size={13} style={{ color: "#78716c" }} />
          <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.upstream_task_flow")}</span>
        </div>
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          fontSize: 10, fontWeight: 800, padding: "4px 9px", borderRadius: 999,
          color: tone.fg, background: tone.bg, border: `1px solid ${tone.border}`,
          textTransform: "uppercase", letterSpacing: "0.04em",
        }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: tone.dot }} />
          {tone.label}
        </span>
      </div>
      <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={{ margin: 0, fontSize: 12, color: "#78716c", lineHeight: 1.6 }}>
          {tone.note}
        </p>
        {info.dependencyIds.length > 0 && (
          <div>
            <div style={{ fontSize: 11, fontWeight: 800, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>
              {t("page.task_detail.depends_on_tasks")}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {info.dependencyIds.map((depId) => {
                const depStatus = info.statuses[depId] || t("page.task_detail.dependency_status_unknown");
                return (
                  <button
                    key={depId}
                    type="button"
                    onClick={() => onOpenTask(depId)}
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 8,
                      border: "1px solid rgba(28,25,23,0.06)", background: "#fff", borderRadius: 999,
                      padding: "6px 10px", cursor: "pointer", color: "#44403c",
                      boxShadow: "0 1px 2px rgba(28,25,23,0.04)",
                    }}
                  >
                    <span style={{ fontSize: 11, fontWeight: 800 }}>#{depId.slice(-6)}</span>
                    <span style={{ fontSize: 10, color: "#78716c", fontWeight: 700 }}>{depStatus}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}
        {info.outputs.length > 0 && (
          <div>
            <div style={{ fontSize: 11, fontWeight: 800, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>
              {t("page.tasks.predecessor_outputs")}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {info.outputs.map((output: any, idx: number) => {
                const summary = output.result_summary || output.summary || output.result || output.text || "";
                const files = dedupeOutputFiles(Array.isArray(output.files) ? output.files : []);
                return (
                  <div key={`${output.task_id || idx}`} style={{
                    border: "1px solid rgba(28,25,23,0.06)", borderRadius: 14, background: "#fff",
                    padding: "10px 12px", boxShadow: "0 1px 2px rgba(28,25,23,0.035)",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: summary || files.length ? 6 : 0 }}>
                      <button
                        type="button"
                        onClick={() => output.task_id && onOpenTask(String(output.task_id))}
                        style={{
                          border: "none", background: "transparent", padding: 0, cursor: output.task_id ? "pointer" : "default",
                          fontSize: 12, fontWeight: 800, color: "#1c1917", textAlign: "left",
                        }}
                      >
                        {formatUserFacingText(output.task_title || output.task_id || t("page.task_detail.upstream_task"))}
                      </button>
                      {output.status && (
                        <span style={{ fontSize: 10, fontWeight: 800, color: "#436b65", background: "#f0fdfa", border: "1px solid #f2f6f5", borderRadius: 999, padding: "2px 7px" }}>
                          {formatUserFacingLabel(output.status)}
                        </span>
                      )}
                    </div>
                    {summary ? (
                      <div style={{ fontSize: 12, color: "#57534e", lineHeight: 1.55 }}>
                        <ChatMarkdown content={formatUserFacingStructuredText(summary)} />
                      </div>
                    ) : (
                      <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>
                        {t("page.task_detail.no_dependency_summary")}
                      </p>
                    )}
                    {files.length > 0 && (
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                        {files.slice(0, 6).map((file: any, fileIdx: number) => {
                          const url = file.url || file.public_url || file.path || "";
                          const label = file.name || file.filename || file.original_name || file.type || t("page.task_detail.generated_file");
                          return url ? (
                            <a key={fileIdx} href={url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 11, color: "#57534e", fontWeight: 800, textDecoration: "none" }}>
                              {label}
                            </a>
                          ) : (
                            <span key={fileIdx} style={{ fontSize: 11, color: "#78716c", fontWeight: 700 }}>{label}</span>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function taskShortId(task?: Pick<Task, "id"> | null) {
  return task?.id ? `#${task.id.slice(-6)}` : "";
}

function taskStatusLabel(status?: string | null) {
  const normalized = String(status || "").trim();
  if (!normalized) return t("page.agent_dashboard.status");
  const cfg = STATUS_CONFIG[normalized];
  return cfg?.labelKey ? t(cfg.labelKey) : formatUserFacingLabel(normalized);
}

function TaskSiblingSwitcher({
  task,
  tasks,
  columnLabel,
  isLoading,
  onOpenTask,
}: {
  task: Task;
  tasks: Task[];
  columnLabel: string;
  isLoading: boolean;
  onOpenTask: (taskId: string) => void;
}) {
  if (isLoading) {
    return (
      <div className="task-sibling-switcher task-sibling-switcher--loading">
        {t("status.loading")}
      </div>
    );
  }
  if (tasks.length <= 1) return null;
  const currentIndex = tasks.findIndex((item) => item.id === task.id);
  const safeIndex = currentIndex >= 0 ? currentIndex : 0;
  const prevTask = safeIndex > 0 ? tasks[safeIndex - 1] : null;
  const nextTask = safeIndex < tasks.length - 1 ? tasks[safeIndex + 1] : null;
  const taskCountLabel = t("page.task_detail.task_group_count")
    .replace("{current}", String(safeIndex + 1))
    .replace("{total}", String(tasks.length));

  return (
    <div
      className="task-sibling-switcher"
      aria-label={`${columnLabel}: ${t("page.task_detail.task_nav_count")
        .replace("{current}", String(safeIndex + 1))
        .replace("{total}", String(tasks.length))}`}
    >
      <button
        type="button"
        onClick={() => prevTask && onOpenTask(prevTask.id)}
        disabled={!prevTask}
        className="task-sibling-nav-btn"
        aria-label={t("page.task_detail.previous_task")}
        title={prevTask ? `${t("page.task_detail.previous_task")}: ${prevTask.title}` : t("page.task_detail.previous_task")}
      >
        <IconChevronLeft size={15} />
      </button>

      <div className="task-sibling-picker">
        <div className="task-sibling-meta" title={`${columnLabel} · ${taskCountLabel}`}>
          <strong className="task-sibling-count">
            {taskCountLabel}
          </strong>
        </div>
        <Select
          value={task.id}
          onChange={(value) => value && value !== task.id && onOpenTask(value)}
          filterable={tasks.length > 8}
          dropdownMinWidth={360}
          options={tasks.map((item) => ({
            value: item.id,
            label: item.title,
          }))}
          buttonStyle={{
            height: 30,
            minHeight: 30,
            borderRadius: 8,
            background: "transparent",
            borderColor: "transparent",
            boxShadow: "none",
            fontSize: 11,
            fontWeight: 700,
            padding: "0 6px",
          }}
          openButtonStyle={{
            background: "rgba(255,255,255,0.92)",
            borderColor: "rgba(67,107,101,0.18)",
            boxShadow: "0 0 0 3px rgba(67,107,101,0.07)",
          }}
          dropdownStyle={{
            border: "1px solid rgba(28,25,23,0.08)",
            borderRadius: 14,
            background: "rgba(255,255,255,0.98)",
            boxShadow: "0 18px 44px rgba(28,25,23,0.14), 0 3px 10px rgba(28,25,23,0.06)",
            padding: 6,
            maxHeight: 360,
          }}
          optionStyle={{
            height: 36,
            padding: "0 10px",
            borderRadius: 10,
            fontSize: 12,
            fontWeight: 650,
          }}
          style={{ minWidth: 0 }}
        />
      </div>

      <button
        type="button"
        onClick={() => nextTask && onOpenTask(nextTask.id)}
        disabled={!nextTask}
        className="task-sibling-nav-btn task-sibling-nav-btn--next"
        aria-label={t("page.task_detail.next_task")}
        title={nextTask ? `${t("page.task_detail.next_task")}: ${nextTask.title}` : t("page.task_detail.next_task")}
      >
        <IconChevronRight size={15} />
      </button>
    </div>
  );
}

function TaskActionPanel({
  task,
  pendingInputPrompt,
  hasPendingInput,
  canResumePendingInput,
  hitlReply,
  onHitlReplyChange,
  onSubmitHitlReply,
  hitlPending,
  isApprovalTask,
  approvalDecision,
  approvalNote,
  onApprovalNoteChange,
  onApproveTask,
  onRequestTaskChanges,
  approvalPending,
  plan,
  planStepCount,
  onApprovePlan,
  planPending,
}: {
  task: Task;
  pendingInputPrompt: string;
  hasPendingInput: boolean;
  canResumePendingInput: boolean;
  hitlReply: string;
  onHitlReplyChange: (value: string) => void;
  onSubmitHitlReply: () => void;
  hitlPending: boolean;
  isApprovalTask: boolean;
  approvalDecision: any;
  approvalNote: string;
  onApprovalNoteChange: (value: string) => void;
  onApproveTask: () => void;
  onRequestTaskChanges: () => void;
  approvalPending: boolean;
  plan: any | null;
  planStepCount: number;
  onApprovePlan: () => void;
  planPending: boolean;
}) {
  const needsPlanApproval = plan?.status === "pending_approval";
  const showTaskApproval = isApprovalTask || approvalDecision;
  const showPanel = hasPendingInput || showTaskApproval || needsPlanApproval;
  if (!showPanel) return null;
  const taskIsClosed = ["completed", "cancelled", "failed"].includes(task.status);
  const hasAction = hasPendingInput || (showTaskApproval && !taskIsClosed) || needsPlanApproval;

  return (
    <div
      className="glass-card"
      style={{
        padding: 0,
        overflow: "hidden",
        borderColor: hasAction ? "rgba(207,155,68,0.34)" : "rgba(28,25,23,0.08)",
        background: hasAction
          ? "linear-gradient(135deg, rgba(255,250,239,0.94), rgba(255,255,255,0.9))"
          : "rgba(255,255,255,0.78)",
      }}
    >
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        padding: "13px 18px",
        borderBottom: "1px solid rgba(207,155,68,0.22)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
          <div style={{
            width: 30,
            height: 30,
            borderRadius: 10,
            background: hasAction ? "#f3ecd6" : "#f5f5f4",
            color: hasAction ? "#8c5e25" : "#78716c",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}>
            <IconFlag size={15} />
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13.5, fontWeight: 850, color: "#1c1917" }}>
              {hasAction ? t("page.task_detail.action_required") : t("page.task_detail.decision_record")}
            </div>
            <div style={{ marginTop: 1, fontSize: 11, color: "#76502c", lineHeight: 1.4 }}>
              {hasAction ? t("page.task_detail.action_required_hint") : t("page.task_detail.decision_record_hint")}
            </div>
          </div>
        </div>
        {hasAction && (
          <span style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "4px 9px",
            borderRadius: 999,
            background: "rgba(255,255,255,0.74)",
            border: "1px solid rgba(207,155,68,0.24)",
            color: "#76502c",
            fontSize: 10,
            fontWeight: 820,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
            whiteSpace: "nowrap",
          }}>
            {t("page.task_detail.needs_input")}
          </span>
        )}
      </div>

      <div style={{ padding: "14px 18px 16px", display: "flex", flexDirection: "column", gap: 12 }}>
        {hasPendingInput && (
          <section style={{ display: "flex", flexDirection: "column", gap: 9 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
              <div>
                <div style={{ fontSize: 12, fontWeight: 820, color: "#1c1917" }}>
                  {t("page.task_detail.input_requested_title")}
                </div>
                <div style={{ fontSize: 11, color: "#76502c", marginTop: 1 }}>
                  {canResumePendingInput
                    ? t("page.task_detail.input_requested_hint")
                    : task.status === "completed"
                      ? t("page.task_detail.input_requested_completed_hint")
                      : t("page.task_detail.input_requested_comment_hint")}
                </div>
              </div>
            </div>
            {pendingInputPrompt && (
              <div style={{
                padding: "9px 11px",
                borderRadius: 10,
                border: "1px solid rgba(207,155,68,0.18)",
                background: "rgba(255,255,255,0.68)",
                color: "#44403c",
                fontSize: 12.5,
                lineHeight: 1.58,
              }}>
                <ChatMarkdown content={pendingInputPrompt} />
              </div>
            )}
            <textarea
              className="manor-textarea"
              rows={3}
              value={hitlReply}
              onChange={(e) => onHitlReplyChange(e.target.value)}
              placeholder={t("page.task_detail.hitl_reply_placeholder")}
              style={{ background: "rgba(255,255,255,0.82)" }}
            />
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Button
                variant="primary"
                size="sm"
                onClick={onSubmitHitlReply}
                loading={hitlPending}
                disabled={hitlPending || !hitlReply.trim()}
              >
                <IconSend size={12} />
                {canResumePendingInput
                  ? t("page.task_detail.send_reply_and_continue")
                  : t("page.task_detail.save_reply_as_comment")}
              </Button>
            </div>
          </section>
        )}

        {needsPlanApproval && (
          <section style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            padding: "11px 12px",
            borderRadius: 12,
            border: "1px solid rgba(79,113,105,0.16)",
            background: "rgba(242,248,246,0.7)",
          }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 12, fontWeight: 850, color: "#1c1917" }}>
                {t("page.task_detail.plan_approval_title")}
              </div>
              <div style={{ marginTop: 2, fontSize: 11, color: "#3f665e", lineHeight: 1.45 }}>
                {t("page.task_detail.plan_approval_desc")
                  .replace("{id}", taskShortId(plan))
                  .replace("{count}", String(planStepCount))}
              </div>
            </div>
            <Button
              variant="primary"
              size="sm"
              onClick={onApprovePlan}
              loading={planPending}
              disabled={planPending}
            >
              {t("page.task_detail.approve_plan_and_run")}
            </Button>
          </section>
        )}

        {showTaskApproval && (
          <section style={{ display: "flex", flexDirection: "column", gap: 9 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
              <div>
                <div style={{ fontSize: 12, fontWeight: 820, color: "#1c1917" }}>
                  {t("page.task_detail.approval_gate")}
                </div>
                <div style={{ fontSize: 11, color: "#57534e", marginTop: 1, lineHeight: 1.45 }}>
                  {t("page.task_detail.approval_gate_desc")}
                </div>
              </div>
              {approvalDecision && (
                <span style={{
                  fontSize: 10,
                  fontWeight: 820,
                  color: approvalDecision.approved ? "#355f57" : "#8c5e25",
                  background: approvalDecision.approved ? "rgba(242,248,246,0.95)" : "#f3ecd6",
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 999,
                  padding: "3px 8px",
                  whiteSpace: "nowrap",
                }}>
                  {approvalDecision.approved ? t("page.task_detail.approved") : t("page.task_detail.changes_requested")}
                </span>
              )}
            </div>
            {approvalDecision?.note && (
              <div style={{
                padding: "8px 10px",
                borderRadius: 10,
                background: "rgba(250,250,249,0.82)",
                border: "1px solid rgba(28,25,23,0.06)",
                color: "#44403c",
                fontSize: 12,
              }}>
                {approvalDecision.note}
              </div>
            )}
            {!taskIsClosed && (
              <>
                <textarea
                  className="manor-textarea"
                  rows={3}
                  value={approvalNote}
                  onChange={(e) => onApprovalNoteChange(e.target.value)}
                  placeholder={t("page.task_detail.approval_note_placeholder")}
                  style={{ background: "rgba(255,255,255,0.82)" }}
                />
                <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, flexWrap: "wrap" }}>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={onRequestTaskChanges}
                    disabled={approvalPending}
                  >
                    {t("page.task_detail.request_changes")}
                  </Button>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={onApproveTask}
                    loading={approvalPending}
                    disabled={approvalPending}
                  >
                    {t("page.task_detail.approve_and_continue")}
                  </Button>
                </div>
              </>
            )}
          </section>
        )}
      </div>
    </div>
  );
}

/* ── Execution log (collapsible, compact) ── */
function ExecutionLog({ logs }: { logs: any[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div style={{ borderRadius: 16, border: "1px solid rgba(28,25,23,0.06)", background: "rgba(250,250,249,0.6)", overflow: "hidden" }}>
      <button type="button" onClick={() => setExpanded(!expanded)}
        style={{
          width: "100%", display: "flex", alignItems: "center", gap: 8,
          padding: "10px 16px", border: "none", background: "transparent", cursor: "pointer",
          fontSize: 11, fontWeight: 700, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.04em",
        }}>
        <IconTimeline size={12} style={{ color: "#a8a29e" }} />
        {t("page.task_detail.execution_log")}{logs.length})
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#a8a29e" strokeWidth={2.5}
          style={{ marginLeft: "auto", transition: "transform 0.2s", transform: expanded ? "rotate(180deg)" : "none" }}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
        </svg>
      </button>
      {expanded && (
        <div style={{ padding: "4px 16px 14px" }}>
          {logs.map((log: any, i: number) => {
            const lcfg = LOG_ICONS[log.log_type] || LOG_ICONS.ai_agent_turn;
            return (
              <div key={log.id || i} style={{ display: "flex", gap: 8, alignItems: "flex-start", padding: "6px 0", borderTop: i > 0 ? "1px solid rgba(231,229,228,0.25)" : "none" }}>
                <div style={{ width: 18, height: 18, borderRadius: "50%", flexShrink: 0, marginTop: 1, background: `${lcfg.color}10`, display: "flex", alignItems: "center", justifyContent: "center", border: `1px solid ${lcfg.color}20` }}>
                  <svg width="8" height="8" fill="none" viewBox="0 0 24 24" stroke={lcfg.color} strokeWidth={2.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d={lcfg.icon} />
                  </svg>
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{ fontSize: 10, fontWeight: 700, color: lcfg.color, textTransform: "uppercase", letterSpacing: "0.03em" }}>
                      {formatUserFacingLabel(log.log_type.replace(/^ai_/, ""))}
                    </span>
                    <span style={{ fontSize: 10, color: "#a8a29e" }}>{log.created_at ? relativeTime(log.created_at) : ""}</span>
                  </div>
                  <p style={{ fontSize: 11, color: "#78716c", margin: "2px 0 0", lineHeight: 1.5 }}>{formatUserFacingStructuredText(log.content)}</p>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}



export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const currentUser = useAuthStore((s) => s.user);
  const taskReturnTo = getTaskReturnTo(location.state);
  const goBack = () => navigate(taskReturnTo || "/tasks");

  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [editingDesc, setEditingDesc] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  const [descriptionExpanded, setDescriptionExpanded] = useState(false);
  const [newComment, setNewComment] = useState("");
  const [hitlReply, setHitlReply] = useState("");
  const [approvalNote, setApprovalNote] = useState("");
  const [selectedMentions, setSelectedMentions] = useState<MentionOption[]>([]);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [slaAdminOpen, setSlaAdminOpen] = useState(false);
  const [showMoreProperties, setShowMoreProperties] = useState(false);
  const [runtimeRulePrompt, setRuntimeRulePrompt] = useState("");

  const { data: task, isLoading, error } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.tasks.get(taskId!),
    enabled: !!taskId,
    refetchInterval: (query) => hasLiveTaskStatus(query.state.data) ? TASK_DETAIL_POLL_INTERVAL_MS : false,
  });
  const { data: taskConstants } = useQuery({ queryKey: ["task-constants"], queryFn: () => api.tasks.constants() });
  const { data: taskPlans = [], isLoading: plansLoading } = useQuery({
    queryKey: ["task-plans", taskId],
    queryFn: () => api.plans.list({ task_id: taskId!, limit: 5 }),
    enabled: !!taskId,
    refetchInterval: (query) => hasLiveTaskStatus(task) || hasLivePlanStatus(query.state.data) ? TASK_DETAIL_POLL_INTERVAL_MS : false,
  });
  const latestPlan = taskPlans[0] || null;
  const { data: planSteps = [] } = useQuery({
    queryKey: ["plan-steps", latestPlan?.id],
    queryFn: () => api.plans.steps(latestPlan!.id),
    enabled: !!latestPlan?.id,
    refetchInterval: (query) => hasLivePlanStatus([latestPlan]) || hasLiveStepStatus(query.state.data) ? TASK_DETAIL_POLL_INTERVAL_MS : false,
  });
  const { data: agents = [] } = useQuery({ queryKey: ["agents"], queryFn: () => api.agents.list() });
  const { data: logs = [] } = useQuery({
    queryKey: ["task-logs", taskId],
    queryFn: () => api.tasks.logs(taskId!),
    enabled: !!taskId,
    refetchInterval: () => hasLiveTaskStatus(task) || hasLivePlanStatus([latestPlan]) ? TASK_DETAIL_POLL_INTERVAL_MS : false,
  });
  // Lookups so the sidebar can render names instead of opaque ULIDs and
  // link out to the parent task / source chat.
  const { data: workspaces = [] } = useQuery({ queryKey: ["workspaces"], queryFn: () => api.workspaces.list() });
  const { data: users = [] } = useQuery({
    queryKey: ["users", "directory"],
    queryFn: () => api.users.directory(),
  });
  const { data: staffList = [] } = useQuery({ queryKey: ["entity-staff-for-assignee"], queryFn: () => api.staff.list() });

  const commentMentionOptions = useMemo<MentionOption[]>(() => {
    const agentOptions = (agents as Agent[]).map((agent) => ({
      id: agent.id,
      type: "agent" as const,
      name: agent.name,
      subtitle: getAgentDescription(agent) || agent.category || "",
      avatarUrl: agent.avatar_url,
    }));
    const staffOptions = ((staffList as any[]) || [])
      .filter((s) => s.user_id || s.id)
      .map((s) => ({
        id: s.user_id || s.id,
        type: "user" as const,
        name: s.display_name || s.name || s.email,
        subtitle: s.email,
        avatarUrl: s.avatar_url,
      }));
    return [...agentOptions, ...staffOptions];
  }, [agents, staffList]);

  // Comments allow mentioning MULTIPLE agents (unlike FloatingChat's single-agent routing)
  const handleCommentMentionSelect = (m: MentionOption) => {
    setSelectedMentions((prev) =>
      prev.some((x) => x.id === m.id && x.type === m.type) ? prev : [...prev, m]);
  };
  const handleCommentMentionRemove = (m: MentionOption) => {
    setSelectedMentions((prev) =>
      prev.filter((x) => !(x.id === m.id && x.type === m.type)));
  };

  const { data: parentTask } = useQuery({
    queryKey: ["task", task?.parent_task_id],
    queryFn: () => api.tasks.get(task!.parent_task_id!),
    enabled: !!task?.parent_task_id,
  });
  // Subtasks — direct children of this task. The new parent_task_id
  // query param is honored by /tasks → backend filter.
  const { data: subtasksData } = useQuery({
    queryKey: ["tasks", "children", taskId],
    queryFn: () => api.tasks.list({ parent_task_id: taskId, limit: 50 }),
    enabled: !!taskId,
  });
  const subtasks = (subtasksData?.items as Task[] | undefined) || [];
  const siblingStatus = String(task?.status || "").trim();
  const siblingStatusLabel = taskStatusLabel(siblingStatus);
  const { data: siblingTasksData, isLoading: siblingTasksLoading } = useQuery({
    queryKey: [
      "tasks",
      "siblings",
      "status",
      task?.workspace_id || "entity",
      siblingStatus,
    ],
    queryFn: () => api.tasks.list({
      limit: 200,
      status: siblingStatus,
      workspace_id: task?.workspace_id || undefined,
    }),
    enabled: !!task?.id && !!siblingStatus,
  });
  const siblingTasks = useMemo(() => {
    if (!task) return [];
    const rows = ((siblingTasksData?.items || []) as Task[]).filter((candidate) => {
      const sameWorkspace = task.workspace_id
        ? candidate.workspace_id === task.workspace_id
        : !candidate.workspace_id;
      return sameWorkspace && candidate.status === task.status;
    });
    if (!rows.some((candidate) => candidate.id === task.id)) {
      rows.unshift(task);
    }
    return rows;
  }, [siblingTasksData?.items, task]);
  // SLA catalogue — drives both the picker and the header chip.
  const { data: slaPolicies = [] } = useQuery({
    queryKey: ["sla-policies"],
    queryFn: () => api.tasks.slaPolicies.list(),
  });

  const updateMutation = useMutation({
    mutationFn: (data: Partial<Task>) => api.tasks.update(taskId!, data),
    onSuccess: (updated) => {
      // 1) Write the API response straight into the cache so every
      //    binding to ``task`` (sidebar Details, header chips, Properties
      //    selects, AI result panel) re-renders synchronously — no
      //    refetch round-trip wait for fields the backend just changed
      //    (started_at, completed_at, sla_breached, escalation_level,
      //    description after edit, etc.).
      if (updated) queryClient.setQueryData(["task", taskId], updated);
      // 2) Still invalidate to pick up anything the backend mutated as
      //    a side effect that wouldn't be in the PUT response (logs,
      //    the kanban board count, the sidebar list).
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.tasks.delete(taskId!),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["tasks"] }); queryClient.invalidateQueries({ queryKey: ["taskBoard"] }); navigate("/tasks"); },
  });

  const retryMutation = useMutation({
    mutationFn: (note?: string) => api.tasks.retry(taskId!, note),
    onSuccess: (result) => {
      queryClient.setQueryData(["task", taskId], result.task);
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(
        result.dispatched ? t("page.tasks.retry_started") : t("page.tasks.retry_queued"),
        `${t("page.tasks.mode")}: ${result.mode}`,
      );
    },
    onError: (err: any) => {
      toast.error(t("page.task_detail.retry_failed"), err?.message || t("page.tasks.could_not_retry_this_task"));
    },
  });

  const hitlMutation = useMutation({
    mutationFn: ({ response, fields }: { response: string; fields?: Record<string, string> }) => api.tasks.respondHITL(taskId!, { response, fields }),
    onSuccess: (result) => {
      queryClient.setQueryData(["task", taskId], result.task);
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      window.dispatchEvent(new CustomEvent("manor:workspace-actions-refresh", { detail: { workspaceId: result.task.workspace_id } }));
      setNewComment("");
      setHitlReply("");
      toast.success(
        result.dispatched ? t("page.tasks.input_sent") : t("page.tasks.input_saved"),
        result.mode ? `${t("page.tasks.mode")}: ${result.mode}` : undefined,
      );
    },
    onError: (err: any) => {
      toast.error(t("page.task_detail.resume_failed"), err?.message || t("page.tasks.could_not_resume_this_task"));
    },
  });

  const actionReplyCommentMutation = useMutation({
    mutationFn: (response: string) => api.tasks.addLog(taskId!, response, "comment"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      if (task?.workspace_id) {
        window.dispatchEvent(new CustomEvent("manor:workspace-actions-refresh", { detail: { workspaceId: task.workspace_id } }));
      }
      setHitlReply("");
      toast.success(t("page.task_detail.reply_saved_as_comment"));
    },
    onError: (err: any) => {
      toast.error(t("page.task_detail.resume_failed"), err?.message || t("page.task_detail.try_again"));
    },
  });

  const approvalMutation = useMutation({
    mutationFn: ({ choice, note }: { choice: string; note?: string }) => (
      api.tasks.decideApproval(taskId!, { choice, note })
    ),
    onSuccess: (updated) => {
      queryClient.setQueryData(["task", taskId], updated);
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["workspace-chat", updated.workspace_id] });
      window.dispatchEvent(new CustomEvent("manor:workspace-actions-refresh", { detail: { workspaceId: updated.workspace_id } }));
      setApprovalNote("");
      toast.success(t("page.task_detail.approval_decision_saved"));
    },
    onError: (err: any) => {
      toast.error(t("page.task_detail.approval_decision_failed"), err?.message || t("page.task_detail.try_again"));
    },
  });

  const approvePlanMutation = useMutation({
    mutationFn: ({ planId }: { planId: string }) => api.plans.approve(planId),
    onSuccess: (updatedPlan) => {
      queryClient.setQueryData(["task-plans", taskId], (prev: any[] | undefined) => {
        const rows = prev || [];
        if (rows.some((plan) => plan.id === updatedPlan.id)) {
          return rows.map((plan) => plan.id === updatedPlan.id ? updatedPlan : plan);
        }
        return [updatedPlan, ...rows];
      });
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-plans", taskId] });
      queryClient.invalidateQueries({ queryKey: ["plan-steps", updatedPlan.id] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(t("page.task_detail.approval_decision_saved"));
    },
    onError: (err: any) => {
      toast.error(t("page.task_detail.approval_decision_failed"), err?.message || t("page.task_detail.try_again"));
    },
  });

  const retryPlanMutation = useMutation({
    mutationFn: ({ planId, note }: { planId: string; note?: string }) => api.plans.retryFailedSteps(planId, note),
    onSuccess: (result) => {
      queryClient.setQueryData(["task-plans", taskId], (prev: any[] | undefined) => {
        const rows = prev || [];
        return rows.map((plan) => plan.id === result.plan.id ? result.plan : plan);
      });
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-plans", taskId] });
      queryClient.invalidateQueries({ queryKey: ["plan-steps", result.plan.id] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(
        result.dispatched ? t("page.task_detail.failed_steps_retry_started") : t("page.task_detail.failed_steps_reset"),
        `${result.reset_steps} ${t("page.task_detail.steps_reset")}`,
      );
    },
    onError: (err: any) => {
      toast.error(t("page.task_detail.retry_failed"), err?.message || t("page.task_detail.could_not_retry_failed_steps"));
    },
  });

  const retryStepMutation = useMutation({
    mutationFn: ({ stepId, note }: { stepId: string; note?: string }) => api.plans.retryStep(stepId, note),
    onSuccess: (result) => {
      queryClient.setQueryData(["task-plans", taskId], (prev: any[] | undefined) => {
        const rows = prev || [];
        return rows.map((plan) => plan.id === result.plan.id ? result.plan : plan);
      });
      queryClient.setQueryData(["plan-steps", result.plan.id], (prev: any[] | undefined) => {
        const rows = prev || [];
        return rows.map((step) => step.id === result.step.id ? result.step : step);
      });
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-plans", taskId] });
      queryClient.invalidateQueries({ queryKey: ["plan-steps", result.plan.id] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(
        result.dispatched ? t("page.task_detail.step_retry_started") : t("page.task_detail.step_reset"),
        `${t("component.task_execution_timeline.step")}: ${formatUserFacingLabel(result.step.step_key)}`,
      );
    },
    onError: (err: any) => {
      toast.error(t("page.task_detail.retry_failed"), err?.message || t("page.task_detail.could_not_retry_this_step"));
    },
  });

  const sendComment = async (text: string, footerAttachments: AttachedItem[]) => {
    // Upload any file attachments from the footer
    const attachments: any[] = [];
    const failedUploads: string[] = [];
    for (const item of footerAttachments) {
      if (item.file) {
        try {
          const result = await api.tasks.uploadAttachment(taskId!, item.file);
          attachments.push(result);
        } catch (e: any) {
          failedUploads.push(item.name);
          console.error("Upload failed:", item.name, e);
        }
      }
    }
    let content = text.trim();
    if (!content && attachments.length > 0) {
      content = `${t("page.tasks.attached")} ${attachments.length} ${attachments.length > 1 ? t("page.tasks.files") : t("page.tasks.file")}`;
    }
    if (failedUploads.length > 0) {
      content += (content ? "\n" : "") + `(Failed to upload: ${failedUploads.join(", ")})`;
    }
    if (!content && attachments.length === 0) return; // nothing to send
    const mentions = selectedMentions.map((m) => ({ type: m.type, id: m.id }));
    await api.tasks.addLog(
      taskId!, content, "comment",
      attachments.length > 0 ? attachments : undefined,
      mentions.length > 0 ? mentions : undefined,
    );
    setNewComment("");
    setSelectedMentions([]);
    queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
  };

  const addLogMutation = useMutation({
    mutationFn: ({ text, footerAttachments }: { text: string; footerAttachments: AttachedItem[] }) => sendComment(text, footerAttachments),
    onError: (err: any) => {
      toast.error(t("page.task_detail.comment_send_failed"), err?.message || t("page.task_detail.try_again"));
    },
  });

  useEffect(() => {
    if (task) {
      setTitleDraft(task.title || "");
      setDescDraft(task.description || "");
    }
  }, [task?.id, task?.title, task?.description]);
  useEffect(() => { setDescriptionExpanded(false); }, [task?.id]);
  useEffect(() => { setHitlReply(""); }, [task?.id]);
  useEffect(() => { setSelectedMentions([]); }, [task?.id]);

  const taskOutput = (task?.actual_output || null) as Record<string, any> | null;
  const taskOutputFiles = useMemo(
    () => dedupeOutputFiles(Array.isArray(taskOutput?.files) ? taskOutput.files : []),
    [taskOutput],
  );
  const taskOutputPreviews = useMemo(() => {
    const byKey: Record<string, ReturnType<typeof buildTaskOutputPreview>> = {};
    const list: NonNullable<ReturnType<typeof buildTaskOutputPreview>>[] = [];
    const addPreview = (preview: ReturnType<typeof buildTaskOutputPreview> | null, source: any) => {
      if (!preview) return;
      const keys = [
        preview.id,
        preview.fs_path,
        source?.step,
        source?.step_key,
        source?.key,
        outputFileDocumentId(source),
        outputFileLookupPath(source),
        outputFileIdentity(source),
      ].filter(Boolean) as string[];
      if (!list.some((item) => item.content === preview.content && item.fs_path === preview.fs_path)) {
        list.push(preview);
      }
      for (const key of keys) byKey[key] = preview;
    };

    for (const file of taskOutputFiles) {
      addPreview(buildTaskOutputPreview(file, outputFileLabel(file)), file);
    }
    for (const step of planSteps as any[]) {
      const result = step?.result;
      if (!result || typeof result !== "object") continue;
      const stepLabel = String(step?.title || formatUserFacingLabel(step?.step_key || step?.key) || "Generated output");
      const stepKey = step?.step_key || step?.key;
      const resultSource = stepKey ? { ...result, step: stepKey } : result;
      const resultPreview = buildTaskOutputPreview(resultSource, outputFileLabel(resultSource, stepLabel));
      addPreview(resultPreview, resultSource);
      const resultContent = outputFileInlineContent(result);
      const resultPath = outputFileLookupPath(result);
      const resultDocId = outputFileDocumentId(result);
      for (const key of ["files", "artifacts", "documents", "images"]) {
        const values = result[key];
        if (!Array.isArray(values)) continue;
        for (const item of values) {
          if (!item || typeof item !== "object") continue;
          const source = {
            ...(resultContent && !outputFileInlineContent(item) ? { ...item, markdown_document: resultContent } : item),
            ...(stepKey ? { step: stepKey } : {}),
          };
          addPreview(
            buildTaskOutputPreview(
              source,
              outputFileLabel(item, stepLabel),
              outputFileLookupPath(item) || resultPath,
              outputFileDocumentId(item) || resultDocId,
            ),
            item,
          );
        }
      }
    }
    return { byKey, list };
  }, [planSteps, taskOutputFiles]);
  const taskOutputLookupPaths = useMemo(
    () => Array.from(new Set(
      taskOutputFiles
        .filter((file) => !outputFileDocumentId(file))
        .map(outputFileLookupPath)
        .filter(Boolean),
    )),
    [taskOutputFiles],
  );
  const outputFileDocsQuery = useQuery({
    queryKey: ["task-output-file-docs", task?.id, taskOutputLookupPaths],
    enabled: taskOutputLookupPaths.length > 0,
    staleTime: 60_000,
    queryFn: async () => {
      const entries = await Promise.all(taskOutputLookupPaths.map(async (path) => {
        const label = outputFileLabel({ fs_path: path }, path);
        const searches = Array.from(new Set([path, label].filter(Boolean)));
        for (const search of searches) {
          const res = await api.documents.list({ search, include_generated_assets: true, limit: 50 });
          const docs = res.items || [];
          const exact = docs.find((doc: any) => normalizeOutputPath(doc.fs_path) === path)
            || docs.find((doc: any) => doc.name === label);
          if (exact?.id) return [path, exact.id] as const;
        }
        return [path, ""] as const;
      }));
      return Object.fromEntries(entries);
    },
  });
  const outputFileDocIdByPath = (outputFileDocsQuery.data || {}) as Record<string, string>;

  /* Loading / error */
  if (isLoading) return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", padding: 64 }}>
      <LoadingSpinner size={28} />
    </div>
  );
  if (error || !task) return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 12, padding: 64 }}>
      <div style={{ width: 48, height: 48, borderRadius: 16, background: "#f1dddb", display: "flex", alignItems: "center", justifyContent: "center", color: "#d65f59" }}>
        <IconCancel size={24} />
      </div>
      <p style={{ color: "#78716c", fontSize: 14, margin: 0 }}>{t("page.task_detail.not_found")}</p>
      <button onClick={goBack} className="btn-manor-ghost" style={{ fontSize: 13 }}>{t("page.task_detail.back_to_tasks")}</button>
    </div>
  );

  const pcfg = PRIORITY_CONFIG[task.priority] || PRIORITY_CONFIG[3];
  const scfg = STATUS_CONFIG[task.status] || STATUS_CONFIG.pending;
  const isMaster = isMasterAgent(task.agent_id, task.agent_type);
  const isAI = !!task.agent_id || isMaster;
  const ownerServiceLabel = humanizeServiceKey(task.owner_service_key);
  const isRuntimeOwner = Boolean(ownerServiceLabel && !task.agent_id && !task.assignee_id);
  const assigneeLabel = friendlyPersonName(task.agent_name || task.assignee_name || (isMaster ? MANOR_AGENT_NAME
    : task.agent_id ? t("page.tasks.ai_agent")
    : task.assignee_id === currentUser?.id ? (currentUser?.display_name || currentUser?.email)
    : ownerServiceLabel || task.assignee_id), "");
  const assigneeAvatarUrl = task.agent_avatar || task.assignee_avatar
    || (task.assignee_id === currentUser?.id ? currentUser?.avatar_url : null);
  const isAutomatedOwner = isAI || isRuntimeOwner;
  const assigneeType: "manor" | "agent" | "user" | "none" = isMaster ? "manor" : isAutomatedOwner ? "agent" : assigneeLabel ? "user" : "none";
  const isTerminal = ["completed", "cancelled", "failed"].includes(task.status);
  const isOverdue = isDeadlineOverdue(task.deadline, task.status);
  const taskDetails = ((task.details || {}) as Record<string, any>);
  const taskResolutionRaw = String(taskDetails.obsolete_reason || taskDetails.rejection_reason || "").trim();
  const taskResolutionNotice = taskResolutionRaw
    ? {
        title: taskDetails.obsolete_reason ? "Task no longer needed" : "Task closed",
        body: taskResolutionRaw === "fulfilled_by_workspace_starter_document"
          ? "Workspace setup already generated the starter knowledge document. This proposal was closed to avoid duplicating existing knowledge."
          : taskResolutionRaw,
      }
    : null;
  const taskAiResult = ((taskDetails.ai_result || {}) as Record<string, any>);
  const taskSupervisorVerdict = ((taskAiResult.supervisor_verdict || taskOutput?.supervisor_verdict || {}) as Record<string, any>);
  const taskOutputErrorMessage = String(
    taskOutput?.error_message
    || taskAiResult.error_message
    || taskSupervisorVerdict.reason
    || "",
  ).trim();
  const taskOutputAgentResponse = String(taskOutput?.response || taskAiResult.response || "").trim();
  const runtimeContext = ((taskDetails.runtime_context || {}) as Record<string, any>);
  const runtimeRules = Array.isArray(runtimeContext.rules) ? runtimeContext.rules : [];
  const runtimeRefs = Array.isArray(runtimeContext.required_refs) ? runtimeContext.required_refs : [];
  const runtimeInstructions = String(runtimeContext.instructions || "").trim();
  const hasRuntimeSettings = runtimeRules.length > 0 || runtimeRefs.length > 0 || !!runtimeInstructions;
  const runtimeSummary = [
    runtimeRules.length > 0
      ? `${runtimeRules.length} ${t(runtimeRules.length === 1 ? "page.task_detail.runtime.rule_singular" : "page.task_detail.runtime.rule_plural")}`
      : "",
    runtimeInstructions ? t("page.task_detail.runtime.instructions_saved") : "",
    runtimeRefs.length > 0
      ? `${runtimeRefs.length} ${t(runtimeRefs.length === 1 ? "page.task_detail.runtime.ref_singular" : "page.task_detail.runtime.ref_plural")}`
      : "",
  ].filter(Boolean).join(" · ");
  const formattedDescription = formatTaskDescriptionForDisplay(task.description);
  const isLongDescription = formattedDescription.length > 720;
  const taskOutputSteps = Array.isArray(taskOutput?.steps) ? taskOutput.steps : [];
  const taskOutputSummary = taskOutput
    ? String(
      taskOutput.summary
      || taskOutput.result_summary
      || taskOutput.message
      || taskOutput.text
      || taskOutputErrorMessage
      || formatUserFacingStructuredText(taskOutput)
      || "",
    ).trim()
    : "";
  const shouldShowAgentResponseInOutput = Boolean(
    taskOutputAgentResponse
    && taskOutputAgentResponse !== taskOutputSummary
    && taskOutputAgentResponse !== taskOutputErrorMessage
  );
  const isApprovalTask = (
    task.task_type === "approval" ||
    String(task.title || "").toLowerCase().includes("approval") ||
    String(runtimeContext.instructions || "").toLowerCase().includes("approval")
  );
  const approvalDecision = taskDetails.approval_decision || task.actual_output?.approval;
  const taskWorkspace = task.workspace_id
    ? (workspaces as any[]).find((w) => w.id === task.workspace_id)
    : null;
  const taskWorkspaceName = taskWorkspace?.name || (task.workspace_id ? task.workspace_id.slice(0, 8) : t("nav.workspaces"));
  const commitRuntimeContext = (patch: Record<string, any>) => {
    updateMutation.mutate({
      details: {
        ...taskDetails,
        runtime_context: {
          ...runtimeContext,
          ...patch,
        },
      },
    });
  };
  const addRuntimeRule = (rule: Record<string, any>) => {
    commitRuntimeContext({ rules: [...runtimeRules, { ...rule, rule_key: rule.rule_key || `task_${Date.now()}`, enabled: true }] });
  };
  const removeRuntimeRule = (idx: number) => {
    commitRuntimeContext({ rules: runtimeRules.filter((_: any, i: number) => i !== idx) });
  };
  const appendRuntimeInstruction = (existing: string | undefined, addition: string) => {
    const current = String(existing || "").trim();
    if (!current) return addition;
    if (current.includes(addition)) return current;
    return `${current}\n${addition}`;
  };
  const addNaturalRuntimeRequirement = () => {
    const text = runtimeRulePrompt.trim();
    if (!text) return;
    const inferred = inferRuntimeRuleFromText(text, shouldFallbackToWildcardRule(text));
    const patch: Record<string, any> = {
      instructions: appendRuntimeInstruction(runtimeContext.instructions, text),
    };
    if (inferred.patterns.length > 0) {
      patch.rules = [
        ...runtimeRules,
        {
          rule_key: `task_${Date.now()}`,
          rule_type: inferred.rule_type,
          description: text,
          severity: inferred.field === "never_allow_actions" ? "high" : "medium",
          action_patterns: inferred.patterns,
          capability_patterns: inferred.capabilityPatterns,
          source: "operator",
          enabled: true,
        },
      ];
    }
    commitRuntimeContext(patch);
    setRuntimeRulePrompt("");
  };
  const canResumePendingInput = canResumeStructuredHumanInput(task, latestPlan, planSteps as any[]);
  const submitHumanInputReply = (response: string, fields?: Record<string, string>) => {
    const cleanResponse = response.trim();
    const cleanFields = Object.fromEntries(
      Object.entries(fields || {})
        .map(([key, value]) => [key, String(value || "").trim()])
        .filter(([, value]) => value),
    );
    if (canResumePendingInput) {
      hitlMutation.mutate({ response: cleanResponse, fields: cleanFields });
      return;
    }
    const fieldLines = Object.entries(cleanFields).map(([key, value]) => `${formatUserFacingLabel(key)}: ${value}`);
    const comment = [cleanResponse, ...fieldLines].filter(Boolean).join("\n");
    if (comment) actionReplyCommentMutation.mutate(comment);
  };
  const pendingInputRequest = latestActionableInputRequest(logs as any[], taskOutput, task.status);
  const pendingInputPrompt = inputRequestPrompt(pendingInputRequest);
  const showTaskRecoveryPanel = !pendingInputRequest;
  const submitActionReply = () => {
    submitHumanInputReply(hitlReply);
  };
  const workspaceAgentActivityCount = (logs as any[]).filter((log: any) =>
    log?.log_type === "workspace_agent_response" || log?.log_type === "workspace_agent_error"
  ).length;
  const hasWorkspaceAgentActivity = workspaceAgentActivityCount > 0;
  return (
    <div className="task-detail-page" style={{ height: "100%", overflowY: "auto", padding: "1.5rem 2rem 3rem" }}>
      {/* ── Breadcrumb ── */}
      <button onClick={goBack} className="btn-manor-ghost"
        style={{ padding: "4px 10px", fontSize: 13, marginBottom: 20, gap: 4 }}>
        <IconArrowLeft size={14} /> {t("page.task_detail.back_to_tasks")}
      </button>

      {/* ══════════════════════════════════════════════════════════
          HEADER CARD — title + status pill + action buttons
          ══════════════════════════════════════════════════════════ */}
      <div className="glass-panel task-detail-hero">
        <div className="task-detail-hero-main">
          {editingTitle ? (
            <input autoFocus value={titleDraft} onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={() => { if (titleDraft.trim() && titleDraft !== task.title) updateMutation.mutate({ title: titleDraft.trim() }); setEditingTitle(false); }}
              onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); if (e.key === "Escape") { setTitleDraft(task.title || ""); setEditingTitle(false); } }}
              className="manor-input task-detail-title-input" />
          ) : (
            <h1 onClick={() => setEditingTitle(true)}
              className="task-detail-title"
              title={t("page.task_detail.click_to_edit_title")}>
              <span>{task.title}</span>
              <span className="task-detail-title-id">
                #{task.id.slice(-6)}
              </span>
            </h1>
          )}

          {/* Status chips */}
          <div className="task-detail-chip-row">
            <StatusPill status={task.status} />
            {pendingInputRequest && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 10px", borderRadius: 20, fontSize: 11, fontWeight: 800, color: "#936027", background: "#f3ecd6", border: "1px solid #ecdca4" }}>
                <IconFlag size={10} /> {t("page.task_detail.needs_input")}
              </span>
            )}
            <PriorityPill priority={task.priority} />
            <CategoryChip categoryKey={task.category_id} />
            {isOverdue && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 10px", borderRadius: 20, fontSize: 11, fontWeight: 700, color: "#c14a44", background: "#f1dddb" }}>
                <IconClock size={10} /> {t("page.task_detail.overdue")}
              </span>
            )}
            {/* SLA pill — only renders when a policy is attached */}
            {task.sla_policy_id && (() => {
              const policy = (slaPolicies as any[]).find((p) => p.id === task.sla_policy_id);
              if (!policy) return null;
              const respH = Math.round(policy.response_seconds / 3600 * 10) / 10;
              const resolH = Math.round(policy.resolution_seconds / 3600 * 10) / 10;
              return (
                <span
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 4,
                    padding: "3px 10px", borderRadius: 20, fontSize: 11, fontWeight: 700,
                    color: task.sla_breached ? "#a23e38" : "#436b65",
                    background: task.sla_breached ? "#f1dddb" : "#f5f5f4",
                  }}
                  title={`${policy.name}: respond in ${respH}h, resolve in ${resolH}h`}
                >
                  {task.sla_breached ? t("page.task_detail.sla_breached") : `SLA · ${respH}h/${resolH}h`}
                </span>
              );
            })()}
            {/* Escalation level — only when > 0 */}
            {(task.escalation_level || 0) > 0 && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 10px", borderRadius: 20, fontSize: 11, fontWeight: 700, color: "#936027", background: "#f3ecd6" }}>
                {t("page.task_detail.escalated_l")}{task.escalation_level}
              </span>
            )}
            {(isMaster || task.agent_id || assigneeLabel) && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 10px 2px 2px", borderRadius: 20, fontSize: 11, fontWeight: 600, color: isAutomatedOwner ? "#436b65" : "#57534e", background: isAutomatedOwner ? "#f2f6f5" : "#f6f5f3" }}>
                <UserAvatar
                  type={assigneeType}
                  name={assigneeLabel}
                  avatarUrl={assigneeAvatarUrl}
                  seed={assigneeType === "agent" ? task.agent_id || undefined : undefined}
                  size={18}
                />
                {assigneeLabel || t("page.task_detail.ai_agent")}
              </span>
            )}
          </div>
        </div>

        <div className="task-detail-hero-actions">
          <TaskSiblingSwitcher
            task={task}
            tasks={siblingTasks}
            columnLabel={siblingStatusLabel}
            isLoading={siblingTasksLoading}
            onOpenTask={(id) => navigate(`/tasks/${id}`)}
          />
          <button
            onClick={() => setShowDeleteConfirm(true)}
            title={t("page.task_detail.delete_task")}
            className="task-detail-delete-btn"
          >
            <IconTrash size={13} />
            {t("action.delete")}
          </button>
        </div>
      </div>

      {/* ══════════════════════════════════════════════════════════
          TWO-COLUMN LAYOUT
          ══════════════════════════════════════════════════════════ */}
      <div className="task-detail-layout">
        {/* ── LEFT COLUMN ── */}
        <div className="task-detail-main-column">

          {/* Properties card — no hover transform to avoid dropdown shift */}
          <div className="glass-card task-detail-section-card task-detail-properties-card" style={{ padding: 0, overflow: "visible", transform: "none" }}>
            <div className="task-detail-section-header" style={{ padding: "12px 20px", background: "rgba(245,245,244,0.5)", borderBottom: "1px solid rgba(28,25,23,0.06)", display: "flex", alignItems: "center", gap: 8, borderRadius: "20px 20px 0 0" }}>
              <IconCircleDot size={13} style={{ color: "#78716c" }} />
              <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.properties")}</span>
            </div>
            <div style={{ padding: "8px 20px 16px" }}>
              {/* Property rows — shared component (Status / Priority /
                  Assignee / Deadline). Category lives behind the "More"
                  toggle; SLA is its own row below since it pulls from
                  the policy list and offers a "Manage" link. */}
              <TaskPropertiesPanel
                task={task as any}
                agents={agents as any[]}
                users={(users as any[]) || []}
                staff={(staffList as any[]) || []}
                currentUser={currentUser as any}
                variant="full"
                showPriority
                showCategory={showMoreProperties}
                statusTransitions={taskConstants?.status_transitions}
                onUpdate={(patch) => updateMutation.mutate(patch as any)}
              />
              {showMoreProperties && (
                <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
                    {/* SLA Policy */}
                    <div style={{ display: "flex", alignItems: "center", padding: "12px 0" }}>
                      <div style={{ width: 120, display: "flex", alignItems: "center", gap: 8, color: "#78716c", fontSize: 13, fontWeight: 500, flexShrink: 0 }}>
                        <IconClock size={14} /> {t("page.task_detail.sla")}
                      </div>
                      <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 8 }}>
                        <Select
                          value={task.sla_policy_id || ""}
                          onChange={(v) => updateMutation.mutate({ sla_policy_id: v || "" } as any)}
                          options={[
                            { value: "", label: t("page.workspace_detail.none") },
                            ...(slaPolicies as any[]).map((p) => ({
                              value: p.id,
                              label: `${p.name} · ${Math.round(p.response_seconds/3600*10)/10}h / ${Math.round(p.resolution_seconds/3600*10)/10}h`,
                            })),
                          ]}
                          placeholder={t("page.task_detail.no_sla")}
                          style={{ maxWidth: 280, flex: 1 }}
                        />
                        <button
                          type="button"
                          onClick={() => setSlaAdminOpen(true)}
                          style={{
                            fontSize: 11, fontWeight: 600, color: "#57534e",
                            background: "transparent", border: "none", cursor: "pointer",
                            padding: "0 6px", whiteSpace: "nowrap" as const,
                          }}
                          title={t("page.task_detail.create_edit_sla_policies")}
                        >
                          {t("page.agent_dashboard.manage")}
                        </button>
                      </div>
                    </div>
                </div>
              )}
              {/* More / Less toggle */}
                <button
                  onClick={() => setShowMoreProperties((v) => !v)}
                  style={{
                    alignSelf: "flex-start",
                    fontSize: 12, fontWeight: 600, color: "#78716c",
                    background: "transparent", border: "none", cursor: "pointer",
                    padding: "10px 0 4px",
                  }}
                >
                  {showMoreProperties ? t("page.task_detail.hide_options") : t("page.task_detail.more_options")}
                </button>
            </div>
          </div>

          {showTaskRecoveryPanel && (
            <TaskRecoveryPanel
              key={`${task.id}:${task.status}`}
              status={task.status}
              logs={logs}
              comment={newComment}
              detailReason={formatUserFacingStructuredText(taskOutputErrorMessage)}
              isPending={retryMutation.isPending || hitlMutation.isPending || actionReplyCommentMutation.isPending}
              onRetry={(note) => retryMutation.mutate(note)}
              onRespond={submitHumanInputReply}
            />
          )}

          <TaskActionPanel
            task={task}
            pendingInputPrompt={pendingInputPrompt}
            hasPendingInput={!!pendingInputRequest}
            canResumePendingInput={canResumePendingInput}
            hitlReply={hitlReply}
            onHitlReplyChange={setHitlReply}
            onSubmitHitlReply={submitActionReply}
            hitlPending={hitlMutation.isPending || actionReplyCommentMutation.isPending}
            isApprovalTask={isApprovalTask}
            approvalDecision={approvalDecision}
            approvalNote={approvalNote}
            onApprovalNoteChange={setApprovalNote}
            onApproveTask={() => approvalMutation.mutate({ choice: "approve", note: approvalNote })}
            onRequestTaskChanges={() => approvalMutation.mutate({ choice: "request_changes", note: approvalNote })}
            approvalPending={approvalMutation.isPending}
            plan={latestPlan}
            planStepCount={(planSteps as any[]).length}
            onApprovePlan={() => latestPlan?.id && approvePlanMutation.mutate({ planId: latestPlan.id })}
            planPending={approvePlanMutation.isPending}
          />

          {taskResolutionNotice && (
            <div className="glass-card task-resolution-notice" style={{
              order: -1,
              padding: "12px 16px",
              display: "flex",
              alignItems: "flex-start",
              gap: 10,
              borderColor: "rgba(153,246,228,0.62)",
              background: "linear-gradient(135deg, rgba(240,253,250,0.9), rgba(255,255,255,0.95))",
            }}>
              <div className="task-resolution-notice-icon" style={{
                width: 28,
                height: 28,
                borderRadius: 9,
                background: "#f2f6f5",
                color: "#436b65",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}>
                <IconFlag size={14} />
              </div>
              <div style={{ minWidth: 0 }}>
                <div className="task-resolution-notice-title" style={{ fontSize: 13, fontWeight: 850 }}>
                  {taskResolutionNotice.title}
                </div>
                <div className="task-resolution-notice-body" style={{ fontSize: 12, lineHeight: 1.55, marginTop: 3 }}>
                  {taskResolutionNotice.body}
                </div>
              </div>
            </div>
          )}

          {/* Description card */}
          <div className="glass-card task-detail-section-card task-detail-brief-card" style={{ padding: 0, overflow: "hidden", order: -1 }}>
            <div className="task-detail-section-header" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "12px 20px", background: "rgba(250,250,249,0.68)", borderBottom: "1px solid rgba(28,25,23,0.06)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <IconDocument size={13} style={{ color: "#78716c" }} />
                <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.task_brief")}</span>
              </div>
              {!editingDesc && (
                <button onClick={() => setEditingDesc(true)} className="btn-manor-ghost"
                  style={{ fontSize: 12, padding: "4px 12px", height: 28 }}>
                  <IconEdit size={11} /> {t("action.edit")}
                </button>
              )}
            </div>
            <div style={{ padding: "18px 20px" }}>
              {editingDesc ? (
                <div>
                  <textarea autoFocus value={descDraft} onChange={(e) => setDescDraft(e.target.value)}
                    rows={6} className="manor-textarea" style={{ width: "100%", marginBottom: 10 }}
                    placeholder={t("page.task_detail.describe_the_task")} />
                  <div style={{ display: "flex", gap: 8 }}>
                    <button className="btn-manor" style={{ fontSize: 12, padding: "0 16px", height: 32 }}
                      onClick={() => { if (descDraft !== task.description) updateMutation.mutate({ description: descDraft }); setEditingDesc(false); }}>
                      {t("action.save")}
                    </button>
                    <button className="btn-manor-ghost" style={{ fontSize: 12, padding: "0 12px", height: 32 }}
                      onClick={() => { setDescDraft(task.description || ""); setEditingDesc(false); }}>
                      {t("action.cancel")}
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div
                    className={`task-detail-description-frame${isLongDescription && !descriptionExpanded ? " is-collapsed" : ""}`}
                  >
                    <div className={`task-detail-description-body${formattedDescription ? "" : " is-empty"}`} style={{
                      fontSize: 14,
                      color: formattedDescription ? "#26364d" : "#a8a29e",
                      lineHeight: 1.72,
                      minHeight: 48,
                    }}>
                      {formattedDescription ? (
                        <ChatMarkdown content={formattedDescription} />
                      ) : t("page.task_detail.no_description_added_yet_click_edit_to_add_one")}
                    </div>
                    {isLongDescription && !descriptionExpanded && (
                      <div className="task-detail-description-fade" />
                    )}
                  </div>
                  {isLongDescription && (
                    <button
                      type="button"
                      className="btn-manor-ghost"
                      onClick={() => setDescriptionExpanded((value) => !value)}
                      style={{ marginTop: 10, height: 30, padding: "0 12px", fontSize: 12 }}
                    >
                      {descriptionExpanded ? t("page.task_detail.collapse_brief") : t("page.task_detail.show_full_brief")}
                    </button>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Task runtime requirements */}
          <details
            className="glass-card"
            style={{ padding: 0, overflow: "hidden", borderColor: "rgba(231,229,228,0.72)" }}
          >
            <summary style={{
              display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
              padding: "10px 18px", background: "rgba(250,250,249,0.5)", cursor: "pointer",
              listStyle: "none",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                <IconFlag size={13} style={{ color: "#78716c", flexShrink: 0 }} />
                <div style={{ minWidth: 0 }}>
                  <span className="manor-label" style={{ margin: 0, color: "#57534e" }}>
                    {t("page.task_detail.runtime.title")}
                  </span>
                  <div style={{
                    fontSize: 11,
                    color: "#a8a29e",
                    fontWeight: 600,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    marginTop: 2,
                  }}>
                    {hasRuntimeSettings ? runtimeSummary : t("page.task_detail.runtime.optional_summary")}
                  </div>
                </div>
              </div>
              <span style={{
                fontSize: 11, color: "#78716c", fontWeight: 700,
                background: "#fff",
                border: "1px solid rgba(28,25,23,0.06)",
                borderRadius: 999, padding: "3px 9px", whiteSpace: "nowrap",
              }}>
                {t("page.task_detail.runtime.configure")}
              </span>
            </summary>
            <div style={{ padding: "14px 20px 16px", display: "flex", flexDirection: "column", gap: 12, borderTop: "1px solid rgba(28,25,23,0.06)" }}>
              <p style={{ fontSize: 12, color: "#78716c", margin: 0, lineHeight: 1.55 }}>
                {t("page.task_detail.runtime.description")}
              </p>
              <div>
                <label className="manor-label" style={{ marginBottom: 6, display: "block" }}>{t("page.task_detail.runtime.temporary_instructions")}</label>
                <textarea
                  className="manor-textarea"
                  rows={3}
                  defaultValue={runtimeInstructions}
                  key={`runtime-instructions-${task.id}-${runtimeInstructions}`}
                  placeholder={t("page.task_detail.runtime.temporary_instructions_placeholder")}
                  onBlur={(e) => {
                    if (runtimeInstructions !== e.target.value) {
                      commitRuntimeContext({ instructions: e.target.value });
                    }
                  }}
                />
              </div>
              <div style={{ padding: 12, borderRadius: 12, background: "rgba(250,250,249,0.58)", border: "1px solid rgba(28,25,23,0.06)" }}>
                <label className="manor-label" style={{ marginBottom: 6, display: "block" }}>{t("page.task_detail.runtime.add_requirement_label")}</label>
                <textarea
                  className="manor-textarea"
                  rows={2}
                  value={runtimeRulePrompt}
                  onChange={(e) => setRuntimeRulePrompt(e.target.value)}
                  placeholder={t("page.task_detail.runtime.requirement_placeholder")}
                />
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", marginTop: 10 }}>
                  <p style={{ fontSize: 11, color: "#78716c", margin: 0, lineHeight: 1.5 }}>
                    {t("page.task_detail.runtime.requirement_help")}
                  </p>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={addNaturalRuntimeRequirement}
                    disabled={!runtimeRulePrompt.trim() || updateMutation.isPending}
                    loading={updateMutation.isPending}
                  >
                    {t("page.task_detail.runtime.add_requirement")}
                  </Button>
                </div>
              </div>
              <div>
                <label className="manor-label" style={{ marginBottom: 6, display: "block" }}>{t("page.task_detail.runtime.required_refs")}</label>
                <input
                  className="manor-input"
                  defaultValue={runtimeRefs.join(", ")}
                  key={`runtime-refs-${JSON.stringify(runtimeRefs)}`}
                  placeholder={t("page.task_detail.runtime.required_refs_placeholder")}
                  onBlur={(e) => {
                    const refs = e.target.value.split(",").map((v) => v.trim()).filter(Boolean);
                    if (JSON.stringify(refs) !== JSON.stringify(runtimeRefs)) {
                      commitRuntimeContext({ required_refs: refs });
                    }
                  }}
                />
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRuntimeRule({
                    rule_type: "approval_required",
                    description: t("page.task_detail.runtime.quick_require_approval_description"),
                    severity: "medium",
                    action_patterns: ["social_post.publish", "email.send", "external_message.send"],
                    source: "quick_action",
                  })}
                >
                  {t("page.task_detail.runtime.quick_require_approval")}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRuntimeRule({
                    rule_type: "draft_only",
                    description: t("page.task_detail.runtime.quick_draft_only_description"),
                    severity: "high",
                    action_patterns: ["social_post.publish", "email.send", "external_message.send"],
                    source: "quick_action",
                  })}
                >
                  {t("page.task_detail.runtime.quick_draft_only")}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRuntimeRule({
                    rule_type: "deny",
                    description: t("page.task_detail.runtime.quick_add_files_only_description"),
                    severity: "high",
                    action_patterns: ["workspace.file.modify", "workspace.file.delete", "workspace.file.write"],
                    source: "quick_action",
                  })}
                >
                  {t("page.task_detail.runtime.quick_add_files_only")}
                </Button>
              </div>
              {runtimeRules.length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {runtimeRules.map((rule: any, idx: number) => (
                    <div key={rule.rule_key || idx} style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "8px 10px", borderRadius: 10, background: "rgba(255,255,255,0.78)", border: "1px solid rgba(28,25,23,0.06)" }}>
                      <div style={{ flex: 1 }}>
                        <div style={{ fontSize: 12, fontWeight: 700, color: "#1c1917" }}>{localizedRuntimeRuleDescription(rule)}</div>
                        {Array.isArray(rule.action_patterns) && (
                          <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 2 }}>{rule.action_patterns.join(", ")}</div>
                        )}
                        {Array.isArray(rule.capability_patterns) && rule.capability_patterns.length > 0 && (
                          <div style={{ fontSize: 11, color: "#78716c", marginTop: 2 }}>{rule.capability_patterns.join(", ")}</div>
                        )}
                      </div>
                      <button
                        type="button"
                        onClick={() => removeRuntimeRule(idx)}
                        style={{ border: "none", background: "transparent", color: "#d65f59", fontSize: 12, fontWeight: 800, cursor: "pointer" }}
                      >
                        {t("page.task_detail.runtime.remove_rule")}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </details>

          <TaskDependencyPanel task={task} onOpenTask={(id) => navigate(`/tasks/${id}`)} />

          {/* Final result — the durable summary/files saved by the run. */}
          {taskOutput && (
            <div className="glass-card task-detail-section-card task-detail-output-card" style={{ padding: 0, overflow: "hidden" }}>
              <div className="task-detail-section-header" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "12px 20px", background: "rgba(250,250,249,0.68)", borderBottom: "1px solid rgba(28,25,23,0.06)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <IconDocument size={13} style={{ color: "#78716c" }} />
                  <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.task_output")}</span>
                </div>
                {taskOutput.plan_status && (
                  <span style={{
                    fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 6,
                    background: taskOutput.plan_status === "completed" ? "#e4efe8" : taskOutput.plan_status === "failed" ? "#f1dddb" : "#f5f5f4",
                    color: taskOutput.plan_status === "completed" ? "#3d7351" : taskOutput.plan_status === "failed" ? "#c14a44" : "#78716c",
                  }}>
                    {t("page.task_detail.run_status_prefix")} {formatUserFacingLabel(taskOutput.plan_status)}
                  </span>
                )}
              </div>
              <div className="task-detail-section-body" style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>
                {taskOutputSummary ? (
                  <div>
                    <div style={{ fontSize: 11, fontWeight: 800, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 8 }}>
                      {t("page.task_detail.result_summary")}
                    </div>
                    <div style={{ fontSize: 13, color: "#44403c", lineHeight: 1.65 }}>
                      <ChatMarkdown content={formatUserFacingStructuredText(taskOutputSummary)} />
                    </div>
                  </div>
                ) : taskOutputFiles.length === 0 ? (
                  <p style={{ fontSize: 13, color: "#78716c", lineHeight: 1.6, margin: 0 }}>
                    {taskOutputSteps.length > 0
                      ? t("page.task_detail.no_result_saved_steps_recorded")
                      : t("page.task_detail.no_result_saved")}
                  </p>
                ) : null}

                {shouldShowAgentResponseInOutput && (
                  <details style={{
                    border: "1px solid rgba(28,25,23,0.06)",
                    borderRadius: 12,
                    background: "rgba(250,250,249,0.58)",
                    overflow: "hidden",
                  }}>
                    <summary style={{
                      padding: "9px 12px",
                      cursor: "pointer",
                      listStyle: "none",
                      color: "#57534e",
                      fontSize: 12,
                      fontWeight: 800,
                      textTransform: "uppercase",
                      letterSpacing: "0.04em",
                    }}>
                      {t("page.scheduled_jobs.agent_response")}
                    </summary>
                    <div style={{
                      padding: "0 12px 12px",
                      maxHeight: 360,
                      overflowY: "auto",
                      color: "#44403c",
                      fontSize: 13,
                      lineHeight: 1.65,
                    }}>
                      <ChatMarkdown content={formatUserFacingStructuredText(taskOutputAgentResponse)} />
                    </div>
                  </details>
                )}

                {taskOutputFiles.length > 0 && (
                  <div>
                    <div style={{ fontSize: 11, fontWeight: 800, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 8 }}>
                      {t("page.task_detail.generated_files")}
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      {taskOutputFiles.map((f: any, i: number) => {
                        const label = outputFileLabel(f, `File ${i + 1}`);
                        const lookupPath = outputFileLookupPath(f);
                        const docId = outputFileDocumentId(f) || (lookupPath ? outputFileDocIdByPath[lookupPath] : "");
                        const externalUrl = outputFileExternalUrl(f);
                        const preview =
                          (docId ? taskOutputPreviews.byKey[docId] : null)
                          || (lookupPath ? taskOutputPreviews.byKey[lookupPath] : null)
                          || (f.step ? taskOutputPreviews.byKey[String(f.step)] : null)
                          || taskOutputPreviews.byKey[outputFileIdentity(f)]
                          || (!externalUrl && taskOutputPreviews.list.length === 1 ? taskOutputPreviews.list[0] : null);
                        const viewerId = docId || lookupPath || (preview ? preview.id || outputFileIdentity(f) : "");
                        const viewerState = preview
                          ? { returnTo: `/tasks/${task.id}`, taskOutputPreview: preview }
                          : { returnTo: `/tasks/${task.id}` };
                        const fileContent = (
                          <>
                            <IconDocument size={14} style={{ flexShrink: 0 }} />
                            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
                            {f.step && <span style={{ fontSize: 10, color: "#a8a29e", fontWeight: 500 }}>{String(f.step).replace(/_/g, " ")}</span>}
                          </>
                        );
                        const fileStyle = {
                          display: "flex", alignItems: "center", gap: 10,
                          padding: "8px 12px", borderRadius: 10,
                          background: "rgba(242,246,245,0.56)", border: "1px solid rgba(28,25,23,0.14)",
                          color: "#57534e", fontSize: 13, fontWeight: 650,
                          cursor: viewerId || externalUrl ? "pointer" : "default",
                        } as const;
                        return viewerId ? (
                          <Link
                            key={outputFileIdentity(f) || i}
                            to={`/viewer/${encodeURIComponent(viewerId)}`}
                            state={viewerState}
                            style={{ ...fileStyle, textDecoration: "none" }}
                          >
                            {fileContent}
                          </Link>
                        ) : externalUrl ? (
                          <a
                            key={outputFileIdentity(f) || i}
                            href={externalUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            style={{ ...fileStyle, textDecoration: "none" }}
                          >
                            {fileContent}
                          </a>
                        ) : (
                          <span key={outputFileIdentity(f) || i} style={fileStyle}>
                            {fileContent}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          <TaskExecutionTimeline
            plan={latestPlan}
            steps={planSteps}
            isLoading={plansLoading}
            isPending={approvePlanMutation.isPending || retryPlanMutation.isPending || retryStepMutation.isPending}
            note={newComment}
            onRetryFailedSteps={(planId, note) => retryPlanMutation.mutate({ planId, note })}
            onRetryStep={(stepId, note) => retryStepMutation.mutate({ stepId, note })}
          />

          {/* Subtasks card — direct children of this task. Hidden when
              there are no children, so leaf tasks stay clean. */}
          {subtasks.length > 0 && (
            <div className="glass-card" style={{ padding: 0, overflow: "hidden" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 20px", background: "rgba(245,245,244,0.5)", borderBottom: "1px solid rgba(28,25,23,0.06)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <IconList size={13} style={{ color: "#78716c" }} />
                  <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.subtasks")}</span>
                  <span style={{ fontSize: 11, color: "#a8a29e", fontWeight: 500 }}>({subtasks.length})</span>
                </div>
              </div>
              <div style={{ padding: "8px 8px" }}>
                {subtasks.map((st) => {
                  const stTerminal = ["completed", "cancelled", "failed"].includes(st.status);
                  const stOverdue = isDeadlineOverdue(st.deadline, st.status);
                  return (
                    <button
                      key={st.id}
                      onClick={() => navigate(`/tasks/${st.id}`)}
                      style={{
                        width: "100%", display: "flex", alignItems: "center", gap: 10,
                        padding: "8px 12px", borderRadius: 8,
                        background: "transparent", border: "none", cursor: "pointer",
                        textAlign: "left" as const,
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = "#fafaf9"; }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                    >
                      <span style={{
                        width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                        background: stTerminal ? "#4f9c84" : st.status === "in_progress" ? "#cf9b44" : "#d6d3d1",
                      }} />
                      <span style={{
                        flex: 1, fontSize: 13, fontWeight: 500,
                        color: stTerminal ? "#a8a29e" : "#1c1917",
                        textDecoration: stTerminal ? "line-through" : undefined,
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      }}>
                        {st.title}
                      </span>
                      <PriorityPill priority={st.priority} />
                      {stOverdue && (
                        <span style={{ fontSize: 10, fontWeight: 700, color: "#c14a44" }}>{t("page.task_detail.overdue_2")}</span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Attachments card */}
          {(() => {
            const allAttachments = (logs as any[]).flatMap((l: any) =>
              (l.attachments || []).map((a: any) => ({ ...a, from: l.created_by, at: l.created_at }))
            );
            return (
              <div className="glass-card task-detail-section-card task-detail-attachments-card" style={{ padding: 0, overflow: "hidden" }}>
                <div className="task-detail-section-header" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 20px", background: "rgba(245,245,244,0.5)", borderBottom: "1px solid rgba(28,25,23,0.06)" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <IconDocument size={13} style={{ color: "#78716c" }} />
                    <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.attachments")}</span>
                    {allAttachments.length > 0 && <span style={{ fontSize: 11, color: "#a8a29e", fontWeight: 500 }}>({allAttachments.length})</span>}
                  </div>
                  <label className="btn-manor-ghost task-detail-upload-action" style={{ fontSize: 12, padding: "4px 12px", height: 28, cursor: "pointer" }}>
                    <IconUpload size={11} /> {t("action.upload")}
                    <input type="file" multiple hidden onChange={async (e) => {
                      if (!e.target.files?.length) return;
                      const files = Array.from(e.target.files);
                      e.target.value = "";
                      const uploaded: any[] = [];
                      for (const f of files) {
                        try { uploaded.push(await api.tasks.uploadAttachment(taskId!, f)); } catch { }
                      }
                      if (uploaded.length > 0) {
                        await api.tasks.addLog(taskId!, `Attached ${uploaded.length} file${uploaded.length > 1 ? "s" : ""}`, "comment", uploaded);
                        queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
                      }
                    }} />
                  </label>
                </div>
                <div className="task-detail-section-body" style={{ padding: "12px 20px" }}>
                  {allAttachments.length === 0 ? (
                    <p className="task-detail-empty-note" style={{ fontSize: 13, color: "#a8a29e", margin: 0, textAlign: "center", padding: "8px 0" }}>{t("page.task_detail.no_files_attached")}</p>
                  ) : (
                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      {allAttachments.map((att: any, i: number) => (
                        <a key={i} className="task-detail-attachment-row" href={att.url} target="_blank" rel="noopener noreferrer"
                          style={{
                            display: "flex", alignItems: "center", gap: 10, padding: "8px 12px", borderRadius: 10,
                            background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                            textDecoration: "none", transition: "border-color 0.15s",
                          }}
                          onMouseEnter={(e) => { e.currentTarget.style.borderColor = "#436b65"; }}
                          onMouseLeave={(e) => { e.currentTarget.style.borderColor = "#e7e5e4"; }}
                        >
                          <div className="task-detail-attachment-icon" style={{ width: 28, height: 28, borderRadius: 8, background: "#e7e5e4", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, color: "#78716c" }}>
                            <IconDocument size={14} />
                          </div>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div className="task-detail-attachment-title" style={{ fontSize: 12, fontWeight: 600, color: "#292524", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {att.original_name || att.filename}
                            </div>
                            <div className="task-detail-attachment-meta" style={{ fontSize: 10, color: "#a8a29e" }}>
                              {att.size ? `${(att.size / 1024).toFixed(0)} KB` : ""}{att.from ? ` · ${att.from}` : ""}
                            </div>
                          </div>
                          <IconDownload size={14} style={{ color: "#a8a29e", flexShrink: 0 }} />
                        </a>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })()}

          {task.workspace_id && (
            <details
              className="glass-card task-detail-section-card task-detail-agent-thread-card"
              open={hasWorkspaceAgentActivity ? true : undefined}
              style={{ padding: 0, overflow: "hidden", borderColor: "rgba(231,229,228,0.72)" }}
            >
              <summary className="task-detail-section-header task-detail-agent-thread-summary" style={{
                padding: "10px 18px",
                background: "rgba(250,250,249,0.5)",
                borderBottom: hasWorkspaceAgentActivity ? "1px solid rgba(231,229,228,0.48)" : "none",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
                cursor: "pointer",
                listStyle: "none",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                  <IconAgent size={13} style={{ color: "#78716c", flexShrink: 0 }} />
                  <div style={{ minWidth: 0 }}>
                    <span className="manor-label" style={{ margin: 0, color: "#57534e" }}>{t("page.task_detail.workspace_agent_thread")}</span>
                    <div style={{
                      fontSize: 11,
                      color: "#a8a29e",
                      fontWeight: 600,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      marginTop: 2,
                    }}>
                      {hasWorkspaceAgentActivity
                        ? `${workspaceAgentActivityCount} ${t(workspaceAgentActivityCount === 1
                          ? "page.task_detail.workspace_agent_thread_update_singular"
                          : "page.task_detail.workspace_agent_thread_update_plural")}`
                        : t("page.task_detail.workspace_agent_thread_ready")}
                    </div>
                  </div>
                </div>
                <span style={{
                  fontSize: 11,
                  color: "#78716c",
                  fontWeight: 700,
                  background: "#fff",
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 999,
                  padding: "3px 9px",
                  whiteSpace: "nowrap",
                }}>
                  {taskWorkspaceName}
                </span>
              </summary>
              <div className="task-detail-section-body task-detail-agent-thread-body" style={{ padding: 12, background: "rgba(255,255,255,0.76)" }}>
                <p className="task-detail-muted-copy" style={{ fontSize: 12, color: "#78716c", margin: "0 0 10px", lineHeight: 1.6 }}>
                  {t("page.task_detail.task_scoped_chat_messages_here_carry_this_task_s")}
                </p>
                <div style={{ height: hasWorkspaceAgentActivity ? 460 : 380, minHeight: 360, borderRadius: 14, overflow: "hidden", border: "1px solid rgba(28,25,23,0.06)", background: "#fff" }}>
                  <WorkspaceChat
                    key={`${task.workspace_id}:${task.id}:workspace-agent-thread`}
                    workspaceId={task.workspace_id}
                    workspaceName={`Task: ${task.title}`}
                    threadRef={{ kind: "task", id: task.id }}
                  />
                </div>
              </div>
            </details>
          )}

          {/* ── Comments (human + status changes + AI agent voice) ──
              Comments now includes the agent-side moments users care
              about: HITL requests, HITL resumes, replan signals, and
              the final execution completion / failure summary. The
              per-turn supervisor noise stays in the Execution Log
              section so the Comments stream reads like a conversation. */}
          {(() => {
            // Low-signal exec events (turn-by-turn loop) — stay in Execution Log only
            const EXEC_ONLY_TYPES = new Set([
              "ai_execution_started",
              "ai_agent_turn",
              "ai_supervisor_verdict",
            ]);
            // Conversation-grade exec events — surface in Comments AND in Execution Log
            const COMMENT_EXEC_TYPES = new Set([
              "ai_hitl_requested",
              "ai_hitl_resumed",
              "ai_needs_replan",
              "ai_execution_completed",
              "ai_execution_failed",
            ]);
            const comments = (logs as any[]).filter((l: any) =>
              !EXEC_ONLY_TYPES.has(l.log_type) || COMMENT_EXEC_TYPES.has(l.log_type)
            );
            // Execution Log keeps the full stream for traceability
            const execLogs = (logs as any[]).filter((l: any) =>
              EXEC_ONLY_TYPES.has(l.log_type) || COMMENT_EXEC_TYPES.has(l.log_type)
            );
            return (<>
          <div className="glass-card task-detail-section-card task-detail-comments-card" style={{ padding: 0, overflow: "hidden" }}>
            <div className="task-detail-section-header" style={{ padding: "12px 20px", background: "rgba(245,245,244,0.5)", borderBottom: "1px solid rgba(28,25,23,0.06)", display: "flex", alignItems: "center", gap: 8 }}>
              <IconComment size={13} style={{ color: "#78716c" }} />
              <span className="manor-label" style={{ margin: 0 }}>{t("page.tasks.comments")}</span>
              <span style={{ fontSize: 11, color: "#a8a29e", fontWeight: 500 }}>({comments.length})</span>
            </div>
            <div className="task-detail-section-body" style={{ padding: "16px 20px 20px" }}>
              <div style={{ display: "flex", gap: 10, alignItems: "flex-start", marginBottom: comments.length > 0 ? 12 : 0 }}>
                <UserAvatar
                  type="user"
                  name={currentUser?.display_name || currentUser?.email || t("component.workspace_chat.you")}
                  avatarUrl={currentUser?.avatar_url}
                  size={32}
                />
                <div className="task-detail-comment-chatfooter" style={{ flex: 1, minWidth: 0 }}>
                  <ChatInputFooter
                    value={newComment}
                    onChange={setNewComment}
                    streaming={addLogMutation.isPending}
                    showStopButton={false}
                    onStop={() => {}}
                    placeholder={t("page.tasks.write_comment")}
                    mentions={commentMentionOptions}
                    selectedMentions={selectedMentions}
                    onMentionSelect={handleCommentMentionSelect}
                    onMentionRemove={handleCommentMentionRemove}
                    onSend={(text, footerAttachments) => {
                      if (!text.trim() && footerAttachments.length === 0) return;
                      addLogMutation.mutate({ text, footerAttachments });
                    }}
                  />
                </div>
              </div>

              {/* Comment thread — shared TaskLogItem (same as drawer) */}
              {comments.map((log: any, i: number) => (
                <TaskLogItem
                  key={log.id || i}
                  log={log}
                  index={i}
                  variant="full"
                  formatTime={formatDateLong}
                  task={task}
                  users={(users as any[]) || []}
                  agents={(agents as any[]) || []}
                  staff={(staffList as any[]) || []}
                />
              ))}
              {comments.length === 0 && (
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8, padding: "8px 0" }}>
                  <p className="task-detail-empty-note" style={{ fontSize: 13, color: "#a8a29e", margin: 0, textAlign: "center" }}>{t("page.task_detail.no_comments_yet")}</p>
                  <InlineTips surface="task_comment" placement="empty_state" />
                </div>
              )}
            </div>
          </div>

          {/* ── Execution Log (collapsible, lower priority) ── */}
          {execLogs.length > 0 && (
            <ExecutionLog logs={execLogs} />
          )}
            </>);
          })()}
        </div>

        {/* ── RIGHT SIDEBAR ── */}
        <div className="task-detail-side-column">
          {/* Details card */}
          <div className="glass-card task-detail-meta-card">
            <div className="task-detail-meta-header">
              <IconClock size={12} style={{ color: "#8b8178" }} />
              <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.details")}</span>
            </div>
            <div className="task-detail-meta-body">
              <div className="task-detail-meta-list">
                {/* Priority — pill so it visually matches the header chip */}
                <div className="task-detail-meta-row">
                  <span className="task-detail-meta-label">{t("page.task_detail.priority")}</span>
                  <PriorityPill priority={task.priority} />
                </div>
                {/* Deadline — formatted date + overdue marker */}
                <div className="task-detail-meta-row">
                  <span className="task-detail-meta-label">{t("page.task_detail.deadline")}</span>
                  <span style={{
                    color: isOverdue ? "#c14a44" : task.deadline ? "#292524" : "#a8a29e",
                    display: "inline-flex", alignItems: "center", gap: 4,
                  }} className="task-detail-meta-value">
                    {task.deadline ? formatDateFull(task.deadline) : t("page.tasks.no_deadline")}
                    {isOverdue && (
                      <IconClock size={11} style={{ color: "#c14a44" }} />
                    )}
                  </span>
                </div>
                {(() => {
                  const creatorUser = task.creator_id
                    ? (users as any[]).find((u) => u.id === task.creator_id)
                    : null;
                  const creatorStaff = task.creator_id
                    ? (staffList as any[]).find((s) => s.id === task.creator_id || s.user_id === task.creator_id || s.email === task.creator_id)
                    : null;
                  const creatorAgent = task.creator_id
                    ? (agents as any[]).find((a) => a.id === task.creator_id)
                    : null;
                  const creatorName = task.creator_id
                    ? ((task as any).creator_name
                       || creatorUser?.display_name
                       || creatorUser?.email
                       || creatorStaff?.name
                       || creatorStaff?.display_name
                       || creatorStaff?.email
                       || creatorAgent?.name
                       || (task.creator_id === "system" || isMasterAgent(task.creator_id) ? MANOR_AGENT_NAME : null))
                    : null;
                  const creatorDisplayName = creatorName ? friendlyPersonName(creatorName, t("page.users.user")) : null;
                  const workspaceName = task.workspace_id
                    ? ((workspaces as any[]).find((w) => w.id === task.workspace_id)?.name
                       || task.workspace_id.slice(0, 8))
                    : null;
                  return [
                    { label: t("page.dashboard.created"), value: task.created_at ? formatDateFull(task.created_at) : null },
                    { label: t("page.tasks.started"), value: task.started_at ? formatDateFull(task.started_at) : null },
                    { label: t("status.completed"), value: task.completed_at ? formatDateFull(task.completed_at) : null },
                    { label: t("page.task_detail.created_by"), value: creatorDisplayName },
                    { label: t("page.workspace_detail.service"), value: ownerServiceLabel },
                    { label: t("page.custom_fields.type"), value: task.task_type ? formatUserFacingLabel(task.task_type) : null, capitalize: true },
                    { label: t("page.knowledge.workspace"), value: workspaceName },
                    { label: t("page.dashboard.estimated"), value: task.estimated_hours ? `${task.estimated_hours}h` : null },
                  ];
                })().filter((r) => r.value).map((row) => (
                  <div key={row.label} className="task-detail-meta-row">
                    <span className="task-detail-meta-label">{row.label}</span>
                    <span
                      className="task-detail-meta-value"
                      style={{ textTransform: row.capitalize ? "capitalize" : undefined }}
                    >
                      {row.value}
                    </span>
                  </div>
                ))}
                {/* Required skills — chip row, only when set */}
                {(task.required_skills && task.required_skills.length > 0) && (
                  <div className="task-detail-meta-row task-detail-meta-row--top">
                    <span className="task-detail-meta-label">{t("nav.skills")}</span>
                    <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 4, justifyContent: "flex-end" }}>
                      {task.required_skills.map((s: string) => (
                        <span key={s} style={{
                          fontSize: 10, fontWeight: 600, padding: "2px 7px", borderRadius: 999,
                          background: "#f5f5f4", color: "#57534e",
                        }}>
                          {s}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {/* Tags row removed — Task.tags isn't a column on the
                    SQL model yet. Re-add once a migration adds it. */}
                {/* Parent task link */}
                {task.parent_task_id && (
                  <div className="task-detail-meta-row">
                    <span className="task-detail-meta-label">{t("page.task_detail.parent")}</span>
                    <button
                      onClick={() => navigate(`/tasks/${task.parent_task_id}`)}
                      style={{
                        background: "none", border: "none", cursor: "pointer", padding: 0,
                        textAlign: "right" as const, maxWidth: 200,
                        whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis",
                      }}
                      className="task-detail-meta-link"
                      title={parentTask?.title || task.parent_task_id}
                    >
                      ↗ {parentTask?.title || task.parent_task_id.slice(0, 8)}
                    </button>
                  </div>
                )}
                {/* Source conversation link — task surfaced from chat */}
                {task.conversation_id && (
                  <div className="task-detail-meta-row">
                    <span className="task-detail-meta-label">{t("page.task_detail.from_chat")}</span>
                    <button
                      onClick={() => navigate(`/chat?conversation=${task.conversation_id}`)}
                      style={{
                        background: "none", border: "none", cursor: "pointer", padding: 0,
                      }}
                      className="task-detail-meta-link"
                    >
                      {t("page.task_detail.open_conversation")}
                    </button>
                  </div>
                )}
                {/* Automation link — when this task was spawned by a scheduled job */}
                {(task.details as any)?.scheduled_job_id && (
                  <div className="task-detail-meta-row">
                    <span className="task-detail-meta-label">{t("page.task_detail.automation")}</span>
                    <button
                      onClick={() => navigate("/automations")}
                      style={{
                        background: "none", border: "none", cursor: "pointer", padding: 0,
                      }}
                      className="task-detail-meta-link"
                    >
                      ↗ {(task.details as any).scheduled_job_id}
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Evaluation */}
          {(task.details as any)?.evaluation && (
            <div className="glass-card" style={{ padding: 0, overflow: "hidden" }}>
              <div style={{ padding: "12px 16px", background: "rgba(195,166,63,0.04)", borderBottom: "1px solid rgba(195,166,63,0.1)", display: "flex", alignItems: "center", gap: 8 }}>
                <IconStar size={13} />
                <span className="manor-label" style={{ margin: 0 }}>{t("page.task_detail.customer_evaluation")}</span>
              </div>
              <div style={{ padding: 16, textAlign: "center" }}>
                <div style={{ display: "flex", justifyContent: "center", gap: 4, marginBottom: 8 }}>
                  {[1, 2, 3, 4, 5].map((s) => (
                    <svg key={s} width="20" height="20" viewBox="0 0 24 24"
                      fill={s <= ((task.details as any).evaluation.score || 0) ? "#c3a63f" : "#e7e5e4"}
                      stroke="none">
                      <path d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" />
                    </svg>
                  ))}
                </div>
                <p style={{ fontSize: 13, fontWeight: 700, color: "#292524", margin: "0 0 4px" }}>
                  {[
                    "",
                    t("page.task_detail.evaluation.poor"),
                    t("page.task_detail.evaluation.fair"),
                    t("page.task_detail.evaluation.good"),
                    t("page.task_detail.evaluation.very_good"),
                    t("page.task_detail.evaluation.excellent"),
                  ][(task.details as any).evaluation.score] || ""}
                </p>
                {(task.details as any).evaluation.review && (
                  <p style={{ fontSize: 12, color: "#78716c", margin: 0, fontStyle: "italic", lineHeight: 1.5 }}>
                    "{(task.details as any).evaluation.review}"
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Delete moved to header overflow menu (⋯) — keeps the
              right sidebar focused on task context, not destructive
              actions. */}

        </div>
      </div>

      <ConfirmDialog
        open={showDeleteConfirm}
        onClose={() => setShowDeleteConfirm(false)}
        onConfirm={() => deleteMutation.mutate()}
        title={t("page.task_detail.delete_task_2")}
        message={t("page.task_detail.this_will_permanently_delete_this_task_all_comme")}
        confirmLabel={t("action.delete")}
        danger
      />

      <SlaAdminModal
        open={slaAdminOpen}
        onClose={() => setSlaAdminOpen(false)}
      />
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   SlaAdminModal — minimal CRUD for TaskSlaPolicy. Lives next to the
   SLA picker on the task detail page so admins can create / edit /
   delete policies without leaving the task they're configuring.
   ══════════════════════════════════════════════════════════════════════════ */

function SlaAdminModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: policies = [] } = useQuery({
    queryKey: ["sla-policies"],
    queryFn: () => api.tasks.slaPolicies.list(),
    enabled: open,
  });

  const [draftName, setDraftName] = useState("");
  const [draftRespH, setDraftRespH] = useState("1");
  const [draftResolH, setDraftResolH] = useState("24");
  const [editingId, setEditingId] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => api.tasks.slaPolicies.create({
      name: draftName.trim(),
      response_seconds: Math.round(Number(draftRespH) * 3600),
      resolution_seconds: Math.round(Number(draftResolH) * 3600),
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sla-policies"] });
      setDraftName(""); setDraftRespH("1"); setDraftResolH("24");
    },
  });

  const update = useMutation({
    mutationFn: ({ id, fields }: { id: string; fields: Record<string, any> }) =>
      api.tasks.slaPolicies.update(id, fields),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sla-policies"] });
      setEditingId(null);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.tasks.slaPolicies.delete(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sla-policies"] }),
  });

  if (!open) return null;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("page.task_detail.sla_policies")}
      maxWidth="560px"
      footer={
        <Button variant="primary" onClick={onClose}>{t("page.team_people.done")}</Button>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {/* Existing policies */}
        <div>
          <SectionLabel>{t("page.task_detail.active_policies")}</SectionLabel>
          {(policies as any[]).length === 0 ? (
            <div style={{
              padding: "16px", borderRadius: 10,
              background: "#fafaf9", color: "#a8a29e",
              fontSize: 13, textAlign: "center" as const,
            }}>
              {t("page.task_detail.no_sla_policies_yet_add_your_first_one_below")}
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {(policies as any[]).map((p) => (
                <SlaPolicyRow
                  key={p.id}
                  policy={p}
                  isEditing={editingId === p.id}
                  onStartEdit={() => setEditingId(p.id)}
                  onCancelEdit={() => setEditingId(null)}
                  onSave={(fields) => update.mutate({ id: p.id, fields })}
                  onDelete={() => remove.mutate(p.id)}
                  saving={update.isPending}
                />
              ))}
            </div>
          )}
        </div>

        {/* Create new */}
        <div>
          <SectionLabel>{t("page.task_detail.new_policy")}</SectionLabel>
          <div style={{
            padding: 14, borderRadius: 10,
            background: "#fafaf9",
            display: "flex", flexDirection: "column", gap: 10,
          }}>
            <input
              className="manor-input"
              placeholder={t("page.task_detail.name_e_g_standard_premium_vip")}
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
            />
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 10 }}>
              <div>
                <label style={{ fontSize: 11, color: "#78716c", fontWeight: 600, display: "block", marginBottom: 4 }}>
                  {t("page.task_detail.response_hours")}
                </label>
                <input
                  className="manor-input"
                  type="number" min="0" step="0.25"
                  value={draftRespH}
                  onChange={(e) => setDraftRespH(e.target.value)}
                />
              </div>
              <div>
                <label style={{ fontSize: 11, color: "#78716c", fontWeight: 600, display: "block", marginBottom: 4 }}>
                  {t("page.task_detail.resolution_hours")}
                </label>
                <input
                  className="manor-input"
                  type="number" min="0" step="0.25"
                  value={draftResolH}
                  onChange={(e) => setDraftResolH(e.target.value)}
                />
              </div>
            </div>
            <Button
              variant="primary"
              size="sm"
              disabled={!draftName.trim() || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? t("page.task_detail.creating") : t("page.task_detail.add_policy")}
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 800, letterSpacing: "0.08em",
      textTransform: "uppercase" as const, color: "#78716c",
      marginBottom: 8,
    }}>
      {children}
    </div>
  );
}

function SlaPolicyRow({
  policy, isEditing, onStartEdit, onCancelEdit, onSave, onDelete, saving,
}: {
  policy: any;
  isEditing: boolean;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSave: (fields: Record<string, any>) => void;
  onDelete: () => void;
  saving: boolean;
}) {
  const [name, setName] = useState(policy.name);
  const [respH, setRespH] = useState(String(Math.round(policy.response_seconds / 3600 * 10) / 10));
  const [resolH, setResolH] = useState(String(Math.round(policy.resolution_seconds / 3600 * 10) / 10));

  // Reset draft when entering edit mode
  useEffect(() => {
    if (isEditing) {
      setName(policy.name);
      setRespH(String(Math.round(policy.response_seconds / 3600 * 10) / 10));
      setResolH(String(Math.round(policy.resolution_seconds / 3600 * 10) / 10));
    }
  }, [isEditing, policy]);

  if (isEditing) {
    return (
      <div style={{
        padding: 12, borderRadius: 10,
        border: "1px solid rgba(28,25,23,0.06)", background: "#ffffff",
        display: "flex", flexDirection: "column", gap: 8,
      }}>
        <input className="manor-input" value={name} onChange={(e) => setName(e.target.value)} />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 8 }}>
          <input className="manor-input" type="number" min="0" step="0.25" value={respH} onChange={(e) => setRespH(e.target.value)} placeholder={t("page.task_detail.response_h")} />
          <input className="manor-input" type="number" min="0" step="0.25" value={resolH} onChange={(e) => setResolH(e.target.value)} placeholder={t("page.task_detail.resolve_h")} />
        </div>
        <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onCancelEdit}>{t("action.cancel")}</Button>
          <Button
            variant="primary" size="sm" disabled={saving || !name.trim()}
            onClick={() => onSave({
              name: name.trim(),
              response_seconds: Math.round(Number(respH) * 3600),
              resolution_seconds: Math.round(Number(resolH) * 3600),
            })}
          >
            {t("action.save")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div style={{
      padding: "10px 12px", borderRadius: 10,
      border: "1px solid rgba(28,25,23,0.06)", background: "#ffffff",
      display: "flex", alignItems: "center", gap: 12,
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#1c1917" }}>{policy.name}</div>
        <div style={{ fontSize: 11, color: "#78716c", marginTop: 2 }}>
          {t("page.task_detail.respond")} {Math.round(policy.response_seconds / 3600 * 10) / 10}{t("page.task_detail.h")}
          {" · "}{t("page.task_detail.resolve")} {Math.round(policy.resolution_seconds / 3600 * 10) / 10}{t("page.task_detail.h")}
        </div>
      </div>
      <button
        onClick={onStartEdit}
        style={{ fontSize: 11, fontWeight: 600, color: "#57534e", background: "none", border: "none", cursor: "pointer" }}
      >
        {t("action.edit")}
      </button>
      <button
        onClick={() => { if (confirm(`Remove SLA policy "${policy.name}"?`)) onDelete(); }}
        style={{ fontSize: 11, fontWeight: 600, color: "#d65f59", background: "none", border: "none", cursor: "pointer" }}
      >
        {t("page.task_detail.runtime.remove_rule")}
      </button>
    </div>
  );
}

/* Used in evaluation stars — need IconStar available */
function IconStar(props: { size?: number; className?: string }) {
  return (
    <svg width={props.size || 20} height={props.size || 20} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" className={props.className}>
      <path d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" />
    </svg>
  );
}
