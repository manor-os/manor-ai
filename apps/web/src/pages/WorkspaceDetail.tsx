import { useState, useCallback, useEffect } from "react";
import { Link, useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type GovernancePolicy,
  type WorkerRegisterResponse,
  type WorkspaceBudgetUpdate,
  type WorkspaceEvaluationSnapshot,
} from "../lib/api";
import { useToastStore } from "../stores/toast";
import type { AgentLearningCandidate, RuntimeEvidence, Workspace, WorkspaceStaff, WorkspaceActivity } from "../lib/types";
import { canManageWorkspace } from "../lib/permissions";
import { useAuthStore } from "../stores/auth";
import { formatDate, relativeTime } from "../lib/format";
import PageHeader from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import StatusBadge from "../components/ui/StatusBadge";
import Chip from "../components/ui/Chip";
import GlassCard from "../components/ui/GlassCard";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Select from "../components/ui/Select";
import Toggle from "../components/ui/Toggle";
import UserAvatar from "../components/ui/UserAvatar";
import AgentAvatar from "../components/ui/AgentAvatar";
import { PeoplePicker, type StaffOption } from "../components/permissions";
import { IconInfo, IconDocument } from "../components/icons";
import WorkspaceGoalGraph from "../components/ui/WorkspaceGoalGraph";
import ScheduledJobs from "./ScheduledJobs";
import ExportBlueprintModal from "../components/blueprints/ExportBlueprintModal";
import { SUPPORTED_LOCALES, t } from "../lib/i18n";
import { inferRuntimeRuleFromText, runtimeCapabilitiesForActionPatterns, uniqueActionPatterns } from "../lib/runtimeRules";
import { formatUserFacingLabel, formatUserFacingText } from "../lib/taskDisplay";

/* ---- style constants ---- */

const GLASS: React.CSSProperties = {
  background: "rgba(255,255,255,0.85)",
  backdropFilter: "blur(16px)",
  WebkitBackdropFilter: "blur(16px)",
  border: "1px solid rgba(28,25,23,0.06)",
  borderRadius: 20,
  padding: "24px 28px",
};

const LABEL: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 800,
  textTransform: "uppercase" as const,
  letterSpacing: "0.12em",
  color: "#a8a29e",
  marginBottom: 4,
};

const VALUE: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  color: "#292524",
  wordBreak: "break-word" as const,
};

const SECTION_TITLE: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 800,
  textTransform: "uppercase" as const,
  letterSpacing: "0.08em",
  color: "#78716c",
  marginBottom: 16,
};

const CHANNEL_LANGUAGE_OPTIONS = SUPPORTED_LOCALES.map((locale) => ({
  value: locale.code,
  label: locale.name,
}));

function _channelLanguageLabel(language?: string | null) {
  const code = (language || "en").toLowerCase().split(/[-_]/)[0];
  return SUPPORTED_LOCALES.find((locale) => locale.code === code)?.name || "English";
}

const TABLE_HEADER: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 800,
  textTransform: "uppercase" as const,
  letterSpacing: "0.1em",
  color: "#a8a29e",
  padding: "10px 16px",
  textAlign: "left" as const,
  borderBottom: "1px solid #f5f5f4",
};

const TABLE_CELL: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 500,
  color: "#44403c",
  padding: "12px 16px",
  borderBottom: "1px solid #fafaf9",
};

type WorkspaceAgentSource =
  | "hosted"
  | "https"
  | "custom";

type WorkspaceAgentForm = {
  source: WorkspaceAgentSource;
  service_key: string;
  agent_id: string;
  custom_prompt: string;
  agent_name: string;
  description: string;
  system_prompt: string;
  https_url: string;
  runtime_display_name: string;
};

type AgentConnectionResult =
  | { type: "https"; registration: WorkerRegisterResponse; agentName: string; endpoint: string }
  | null;

const DEFAULT_AGENT_FORM: WorkspaceAgentForm = {
  source: "hosted",
  service_key: "",
  agent_id: "",
  custom_prompt: "",
  agent_name: "",
  description: "",
  system_prompt: "",
  https_url: "",
  runtime_display_name: "",
};

const AGENT_SOURCE_OPTIONS: Array<{ key: WorkspaceAgentSource; title: string; body: string }> = [
  {
    key: "hosted",
    title: "Manor Hosted Agent",
    body: "Use an existing Manor agent or template. No external connection required.",
  },
  {
    key: "https",
    title: "Agent from HTTPS",
    body: "Connect a generic HTTPS agent endpoint, including Hermes, OpenClaw, or your own service.",
  },
  {
    key: "custom",
    title: "Create Custom Agent",
    body: "Create a workspace-specific agent profile first; it runs on Manor Hosted unless you connect it later.",
  },
];

const KPI_CARD: React.CSSProperties = {
  ...GLASS,
  display: "flex",
  alignItems: "center",
  gap: 16,
  padding: "20px 24px",
};

type Tab = "overview" | "staff" | "agents" | "capabilities" | "channels" | "documents" | "rules" | "goals" | "automations" | "learning" | "activity" | "settings";
type PrimaryTab = "overview" | "configure" | "activity" | "settings";

type EvaluationDimensionKey =
  | "goal_impact"
  | "cost_efficiency"
  | "time_efficiency"
  | "execution_health"
  | "output_quality"
  | "user_feedback"
  | "governance"
  | "learning";

function _isWorkspaceDetailTab(value: string | null): value is Tab {
  return Boolean(value && [
    "overview",
    "staff",
    "agents",
    "capabilities",
    "channels",
    "documents",
    "rules",
    "goals",
    "automations",
    "learning",
    "activity",
    "settings",
  ].includes(value));
}

function _normalizeWorkspaceDetailTab(value: string | null): Tab {
  if (value === "knowledge" || value === "docs" || value === "document") {
    return "documents";
  }
  return _isWorkspaceDetailTab(value) ? value : "overview";
}

const SETUP_TABS: Tab[] = [
  "staff",
  "agents",
  "capabilities",
  "channels",
  "documents",
  "rules",
  "goals",
  "automations",
  "learning",
];

function _isSetupTab(value: Tab): boolean {
  return SETUP_TABS.includes(value);
}

function _primaryTabFor(value: Tab): PrimaryTab {
  if (_isSetupTab(value)) return "configure";
  if (value === "activity") return "activity";
  if (value === "settings") return "settings";
  return "overview";
}

function _staffLabel(s: { id?: string; name?: string; display_name?: string; email?: string } | null | undefined): string {
  if (!s) return "";
  return s.display_name || s.name || s.email || s.id || t("page.workspace_detail.unknown");
}

function _agentLabel(a: { id?: string; name?: string; slug?: string; category?: string } | null | undefined): string {
  if (!a) return "";
  const base = a.name || a.slug || a.id || t("page.workspace_detail.unknown");
  return a.category ? `${base} · ${formatUserFacingLabel(a.category)}` : base;
}

/** "content_creator" → "Content Creator" / "follower_growth" → "Follower Growth".
 *  Used to render service_key / goal_key in human-friendly form when the
 *  underlying object has no ``name`` / ``title`` field. */
function _humanize(key: string | null | undefined): string {
  if (!key) return "";
  return key
    .replace(/[_\-]+/g, " ")
    .trim()
    .split(/\s+/)
    .map((w) => (w.length > 0 ? w[0].toUpperCase() + w.slice(1).toLowerCase() : w))
    .join(" ");
}

function _friendlyCodeLabel(value: string | null | undefined): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const labels: Record<string, string> = {
    "external_message.send": "Send external message",
    "external.message": "External messaging",
    "external.email": "External email",
    "external.social": "External social channel",
    "social_post.publish": "Publish social post",
    "social_post.delete": "Delete social post",
    "email.send": "Send email",
    "email.delete": "Delete email",
    "workspace.file.modify": "Edit workspace file",
    "workspace.file.delete": "Delete workspace file",
    "workspace.file.write": "Create workspace file",
    write_report_file: "Save report file",
    generate_pipeline_report: "Generate pipeline report",
    glob_files: "Find files",
    grep_files: "Search file text",
    manor: "Manor workspace actions",
    "manor action gateway": "Manor actions",
    rag: "Knowledge search",
    search_workspace_context: "Search workspace context",
    "workspace agent": "Workspace agent actions",
    workspace_agent: "Workspace agent actions",
    workspace_context: "Workspace context",
    "workspace operation": "Workspace operations",
    workspace_operation: "Workspace operations",
    workspace_update_task_runtime: "Update task guidance",
  };
  const normalized = raw.toLowerCase();
  const normalizedWords = normalized.replace(/[_\-.:/]+/g, " ").replace(/\s+/g, " ").trim();
  if (normalizedWords === "workspace update task runtime") return "Update task guidance";
  if (normalizedWords === "search workspace context") return "Review workspace context";
  const fallback = labels[normalized] || _humanize(raw.replace(/[.:/]+/g, " "));
  return fallback
    .replace(/\bWorkspace Request Strategist Review\b/g, "Request Manor AI review")
    .replace(/\bStrategist\b/g, "Manor AI")
    .replace(/\bRuntime\b/g, "Run")
    .replace(/\bScheduled Jobs\b/g, "Automations")
    .replace(/\bScheduled Job\b/g, "Automation")
    .replace(/\bGlob Files\b/g, "Find files")
    .replace(/\bGrep Files\b/g, "Search file text")
    .replace(/\bRag\b/g, "Knowledge search");
}

function _serviceLabelFromKey(
  serviceKey: string | null | undefined,
  services: Array<{ name?: string; service_key?: string; key?: string }> = [],
): string {
  const key = String(serviceKey || "").trim();
  const service = services.find((svc) => (svc.service_key || svc.key) === key);
  return _serviceLabel(service || (key ? { service_key: key } : null));
}

function _formatScheduleLabel(value: string | null | undefined): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const cron = raw.match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(\*|[0-7](?:-[0-7])?)$/);
  if (cron) {
    const minute = Number(cron[1]);
    const hour = Number(cron[2]);
    const dayPart = cron[3] === "*" ? "Daily" : cron[3] === "1-5" ? "Weekdays" : "Scheduled";
    if (Number.isFinite(minute) && Number.isFinite(hour)) {
      const suffix = hour >= 12 ? "PM" : "AM";
      const hour12 = hour % 12 || 12;
      return `${dayPart} at ${hour12}:${String(minute).padStart(2, "0")} ${suffix}`;
    }
  }
  return raw
    .replace(/^daily\s+(\d{1,2}):(\d{2})\s+mon-fri$/i, (_m, h, min) => {
      const hour = Number(h);
      const suffix = hour >= 12 ? "PM" : "AM";
      return `Weekdays at ${hour % 12 || 12}:${min} ${suffix}`;
    })
    .replace(/^weekly\s+mon\s+(\d{1,2}):(\d{2})$/i, (_m, h, min) => {
      const hour = Number(h);
      const suffix = hour >= 12 ? "PM" : "AM";
      return `Mondays at ${hour % 12 || 12}:${min} ${suffix}`;
    });
}

function _formatCredits(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return t("page.workspace_detail.credit_amount", {
    count: Math.max(0, Math.round(value)).toLocaleString(),
  });
}

function _isInternalChannel(ch: any): boolean {
  return ch?.channel_type === "internal_chat" || ch?.provider === "internal_chat";
}

function _isTechnicalChannelName(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  if (!normalized) return true;
  return (
    normalized === "internal_chat" ||
    normalized === "webchat" ||
    normalized === "internal: internal_chat" ||
    normalized.includes("secondary_external") ||
    normalized.includes("primary_external") ||
    /\binternal_chat\b/.test(normalized)
  );
}

function _channelKindLabel(ch: any): string {
  const kind = String(ch?.channel_type || ch?.provider || "").toLowerCase();
  if (kind === "internal_chat") return t("page.workspace_detail.channel_kind_internal");
  if (kind === "webchat") return t("page.workspace_detail.channel_kind_webchat");
  if (kind === "email") return t("page.workspace_detail.channel_kind_email");
  if (kind === "sms") return t("page.workspace_detail.channel_kind_sms");
  if (kind === "whatsapp") return t("page.workspace_detail.channel_kind_whatsapp");
  if (kind === "wechat") return t("page.workspace_detail.channel_kind_wechat");
  return _humanize(ch?.channel_type || ch?.provider) || t("page.workspace_detail.channel_2");
}

function _channelDisplayName(ch: any): string {
  if (_isInternalChannel(ch)) return t("page.workspace_detail.workspace_chat_channel");
  const raw = String(ch?.name || "").trim();
  if (raw && !_isTechnicalChannelName(raw)) return raw;
  if (ch?.channel_type === "webchat") return t("page.workspace_detail.public_web_chat_channel");
  return _channelKindLabel(ch);
}

function _serviceLabel(svc: { name?: string; service_key?: string; key?: string } | null | undefined): string {
  if (!svc) return t("page.workspace_detail.unnamed_service");
  return svc.name || _humanize(svc.service_key || svc.key) || t("page.workspace_detail.unnamed_service");
}

function _goalLabel(g: { title?: string; name?: string; goal_key?: string; key?: string } | null | undefined): string {
  if (!g) return t("page.workspace_detail.goal_fallback");
  return g.title || g.name || _humanize(g.goal_key || g.key) || t("page.workspace_detail.goal_fallback");
}

function _goalDedupeKey(g: any): string {
  const title = String(g?.title || g?.name || "").trim().toLowerCase();
  if (title) return `title:${title}`;
  const metric = String(g?.metric_key || g?.goal_key || g?.key || g?.id || "").trim().toLowerCase();
  return `metric:${metric}`;
}

function _goalCompletenessScore(g: any): number {
  let score = 0;
  if (g?.id) score += 1;
  if (g?.current_value !== null && g?.current_value !== undefined) score += 8;
  if (g?.target_value !== null && g?.target_value !== undefined && Number(g.target_value) !== 0) score += 4;
  if (g?.baseline_value !== null && g?.baseline_value !== undefined) score += 2;
  if (g?.pace_status && g.pace_status !== "unknown") score += 4;
  if (g?.measurement_source) score += 2;
  if (g?.measurement_cadence || g?.cadence) score += 1;
  if (g?.description) score += 1;
  return score;
}

function _goalProgressPercent(g: any): number {
  const current = Number(g?.current_value ?? 0);
  const target = Number(g?.target_value ?? 1);
  const baseline = Number(g?.baseline_value ?? 0);
  if (!Number.isFinite(current) || !Number.isFinite(target) || !Number.isFinite(baseline)) return 0;
  if (target === baseline) return current === target ? 100 : 0;
  return Math.min(100, Math.max(0, ((current - baseline) / (target - baseline)) * 100));
}

function _dedupeGoals(goals: any[]): any[] {
  const byKey = new Map<string, any>();
  for (const goal of goals || []) {
    const key = _goalDedupeKey(goal);
    const existing = byKey.get(key);
    if (!existing || _goalCompletenessScore(goal) > _goalCompletenessScore(existing)) {
      byKey.set(key, goal);
    }
  }
  return Array.from(byKey.values());
}

function _eventTypeChipVariant(eventType: string): "teal" | "orange" | "blue" | "green" | "red" | "slate" | "purple" {
  const t = eventType.toLowerCase();
  if (t.includes("created") || t.includes("added")) return "green";
  if (t.includes("deleted") || t.includes("removed") || t.includes("failed")) return "red";
  if (t.includes("updated") || t.includes("changed")) return "blue";
  if (t.includes("warning") || t.includes("alert")) return "orange";
  if (t.includes("paused") || t.includes("disabled")) return "slate";
  return "teal";
}

function _activityTaskSummaries(evt: WorkspaceActivity): any[] {
  const details = evt.details || {};
  return Array.isArray(details.task_summaries)
    ? details.task_summaries.filter((item: any) => item && typeof item === "object")
    : [];
}

function _activityActorLabel(evt: WorkspaceActivity): string {
  const name = evt.actor_name || evt.user_name || evt.user_email || evt.agent_name;
  if (name) return name;
  if (evt.user_id) return `User ${evt.user_id.slice(-6)}`;
  if (evt.agent_id) return `Agent ${evt.agent_id.slice(-6)}`;
  return "";
}

function _activityEventLabel(eventType: string): string {
  const normalized = String(eventType || "").trim().toLowerCase();
  const labels: Record<string, string> = {
    "workspace.created": "Workspace created",
    "workspace.updated": "Workspace updated",
    "workspace.member_added": "Member added",
    "workspace.member_updated": "Member updated",
    "workspace.member_removed": "Member removed",
    "workspace_work_batch.completed": "Task wave completed",
    "workspace_work_batch.started": "Task wave started",
    "strategist_proposal.approved": "Proposal approved",
    "strategist_proposal.rejected": "Proposal rejected",
    "strategist_proposal.feedback": "Feedback sent",
    "agent_learning_candidate": "Learning suggested",
    "agent_learning_applied": "Learning applied",
    "workspace_agent.task_runtime_updated": "Task guidance updated",
    "task.comment": "Task comment",
    "task.approval_decision": "Approval decision",
    "workspace_operation.runtime_repaired": "Operation repaired",
    "workspace_operation.resolved": "Operation reviewed",
    "external_message.approved": "Message approved",
    "external_message.rejected": "Message rejected",
    "external_message.resolved": "Message reviewed",
    "hitl.resolved": "Input answered",
    "workspace_created": "Workspace created",
  };
  if (labels[normalized]) return labels[normalized];
  const prefix = normalized.split(".")[0];
  if (prefix === "task") return "Task update";
  if (prefix === "workspace") return "Workspace update";
  if (prefix === "agent") return "Agent update";
  return _humanize(normalized.replace(/\./g, " "));
}

function WorkspaceWelcomeBurst() {
  const trails = ["1", "2", "3"];
  const pieces = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"];
  return (
    <div className="workspace-welcome-burst" aria-hidden="true">
      <span className="workspace-welcome-burst__core" />
      {trails.map((trail) => (
        <span
          key={trail}
          className={`workspace-welcome-burst__trail workspace-welcome-burst__trail--${trail}`}
        />
      ))}
      {pieces.map((piece) => (
        <span
          key={piece}
          className={`workspace-welcome-burst__piece workspace-welcome-burst__piece--${piece}`}
        />
      ))}
    </div>
  );
}

function _activityDecisionLabel(choice: any): string {
  const normalized = String(choice || "").trim().toLowerCase();
  if (["approve", "approved", "approve_all", "approve_selected", "yes", "accept", "confirm"].includes(normalized)) {
    return "approved";
  }
  if (["reject", "rejected", "reject_all", "no", "decline", "cancel"].includes(normalized)) {
    return "rejected";
  }
  if (normalized === "feedback") return "sent feedback";
  if (normalized === "request_changes" || normalized === "changes") return "requested changes";
  return "reviewed";
}

function _activitySummaryText(evt: WorkspaceActivity): string {
  const summary = String(evt.summary || "").trim();
  const details = evt.details || {};
  const choice = _activityDecisionLabel(details.choice);
  const type = String(evt.event_type || "").toLowerCase();
  if (summary.includes("choice=")) {
    if (type.startsWith("strategist_proposal.")) return `Workspace proposal ${choice}.`;
    if (type.startsWith("workspace_operation.")) return `Workspace operation ${choice}.`;
    if (type.startsWith("hitl.")) return "Input request answered.";
    return "Workspace action reviewed.";
  }
  if (type === "agent_learning_candidate") {
    const count = Number(details.candidate_count || 0);
    return count > 1 ? `${count} learning suggestions are ready to review.` : "A learning suggestion is ready to review.";
  }
  if (type === "workspace_agent.task_runtime_updated") {
    return formatUserFacingText(summary.replace("Workspace Agent updated runtime requirements for:", "Workspace AI updated task guidance for:"));
  }
  return formatUserFacingText(summary
    .replace(/\bcodex_auto_\d+\b/gi, "Workspace automation")
    .replace(/Workspace operation runtime repaired/gi, "Workspace operation repaired")
    .replace(/\bruntime requirements\b/gi, "task guidance")
    .replace(/\bruntime repaired\b/gi, "operation repaired")
    .replace(/\bruntime\b/gi, "run"));
}

function _activityStatusLabel(status: any): string {
  return _humanize(String(status || "").replace(/_/g, " "));
}

function _learningKindLabel(kind: any): string {
  const normalized = String(kind || "").trim().toLowerCase();
  const labels: Record<string, string> = {
    memory: "Memory",
    skill: "Reusable skill",
    rule: "Operating rule",
    tool_experience: "Successful workflow",
    agent_profile_patch: "Agent guidance",
    profile_patch: "Agent guidance",
    workspace_operation_decision: "Team decision",
    pending_action_resolution: "Team decision",
    strategist_review: "Manor AI review",
    hitl_resolution: "Input response",
    external_message_decision: "Message decision",
    retry_request: "Retry request",
    task_run: "Task run",
    task_comment: "Task comment",
    approval_decision: "Approval decision",
  };
  return labels[normalized] || _humanize(normalized);
}

function _learningRiskLabel(risk: any): string {
  const normalized = String(risk || "").trim().toLowerCase();
  if (normalized === "low") return "Low risk";
  if (normalized === "medium") return "Needs review";
  if (normalized === "high") return "High impact";
  return _humanize(normalized);
}

function _learningToolLabel(value: any): string {
  const raw = String(value || "").trim();
  if (/workspace_update_task/i.test(raw)) return "Task guidance update";
  const withoutPrefix = raw.replace(/^subagent:/i, "").replace(/^tool:/i, "");
  return _friendlyCodeLabel(withoutPrefix.replace(/\./g, " "));
}

function _learningTargetLabel(value: any): string {
  const normalized = String(value || "").trim().toLowerCase();
  if (!normalized) return "";
  if (normalized.includes("workspace_agent:tools.md")) return "Agent tool guidance";
  if (normalized.includes("workspace:rules.md")) return "Workspace guidance";
  if (normalized.includes("rules.md")) return "Operating guidance";
  if (normalized.includes("tools.md")) return "Tool guidance";
  return _humanize(normalized.replace(/[:/]/g, " "));
}

function _learningText(value: any): string {
  const cleaned = String(value || "")
    .replace(/Workspace chat resolved approve_proposals with choice=approve_all/gi, "Workspace proposal approved")
    .replace(/Workspace chat resolved approve_proposals with choice=reject_all/gi, "Workspace proposal rejected")
    .replace(/Workspace chat resolved [a-z0-9_ -]+ with choice=[a-z0-9_ -]+/gi, "Workspace action reviewed")
    .replace(/stop_reason=([^.\n]+)\.\s*error=/gi, "$1. ")
    .replace(/\berror=/gi, "")
    .replace(/`?credit_exhausted`?/gi, "credits ran out")
    .replace(/`?failed`?/gi, "failed")
    .replace(/workspace_agent:TOOLS\.md/gi, "agent tool guidance")
    .replace(/workspace:RULES\.md/gi, "workspace guidance")
    .replace(/workspace[_ ]update[_ ]task[_ ]runtime/gi, "task guidance update")
    .replace(/write_report_file/gi, "save report file")
    .replace(/generate_pipeline_report/gi, "generate pipeline report")
    .replace(/search_workspace_context/gi, "review workspace context")
    .replace(/workspace_context/gi, "workspace context")
    .replace(/\bruntime issue\b/gi, "run issue")
    .replace(/\bruntime\b/gi, "run")
    .replace(/\bRULES\.md\b/gi, "operating guidance")
    .replace(/\bTOOLS\.md\b/gi, "tool guidance")
    .replace(/Tool pattern worked:/gi, "Workflow worked:")
    .replace(/tool pattern/gi, "workflow")
    .replace(/Keep as evidence for future routing and skill extraction\./gi, "Keep it as a reusable pattern for future work.")
    .replace(/learning candidates?/gi, "learning suggestions")
    .replace(/DAG dependencies/gi, "task dependencies")
    .replace(/subagent:([a-z0-9_-]+)/gi, (_match, key) => `${_humanize(String(key))} agent`)
    .replace(/agent_profile_patch/gi, "agent guidance")
    .replace(/profile_patch/gi, "agent guidance")
    .replace(/tool_experience/gi, "successful workflow")
    .replace(/task_run/gi, "task run")
    .replace(/approval_decision/gi, "approval decision");
  return formatUserFacingText(cleaned);
}

function _evaluationText(value: any): string {
  return _learningText(value)
    .replace(/evidence record\(s\)/gi, "run records")
    .replace(/evidence-backed/gi, "run-backed")
    .replace(/runtime evidence/gi, "run records")
    .replace(/learning suggestions\(s\)/gi, "learning suggestions")
    .replace(/learning candidate\(s\)/gi, "learning suggestions")
    .replace(/schema failure\(s\)/gi, "output check issues")
    .replace(/artifact\(s\)/gi, "deliverables");
}

function _activityFileHref(file: any): string {
  const viewerId = file?.document_id || file?.viewer_id;
  if (viewerId) return `/viewer/${encodeURIComponent(String(viewerId))}`;
  const externalUrl = String(file?.external_url || file?.public_url || "").trim();
  return externalUrl.startsWith("http://") || externalUrl.startsWith("https://") ? externalUrl : "";
}

function _taskStatusChipVariant(status: string): "teal" | "orange" | "blue" | "green" | "red" | "slate" | "purple" {
  switch (status.toLowerCase()) {
    case "completed":
    case "done":
    case "success":
      return "green";
    case "in_progress":
    case "running":
      return "blue";
    case "pending":
    case "scheduled":
      return "orange";
    case "failed":
    case "error":
    case "cancelled":
      return "red";
    case "proposed":
      return "purple";
    default:
      return "slate";
  }
}

function _riskChipVariant(risk: string): "teal" | "orange" | "blue" | "green" | "red" | "slate" | "purple" {
  switch ((risk || "").toLowerCase()) {
    case "high":
      return "red";
    case "medium":
      return "orange";
    case "low":
      return "green";
    default:
      return "slate";
  }
}

const DEFAULT_GOVERNANCE_POLICY: GovernancePolicy = {
  never_allow_actions: [],
  hitl_required_actions: [],
  auto_approve_actions: [],
  never_allow_capabilities: [],
  hitl_required_capabilities: [],
  auto_approve_capabilities: [],
  max_risk_level: "high",
  budget_caps_per_kind: {},
};

const DEFAULT_KNOWLEDGE_POLICY = {
  auto_search: true,
  retrieval_mode: "auto",
  citation_required: true,
  strict_mode: false,
  default_group_ids: [] as string[],
  group_purposes: {} as Record<string, string>,
};

function _uniqueStrings(values: string[]): string[] {
  return uniqueActionPatterns(values);
}

function _mergeGovernancePolicy(policy?: Partial<GovernancePolicy> | null): GovernancePolicy {
  return {
    ...DEFAULT_GOVERNANCE_POLICY,
    ...(policy || {}),
    never_allow_actions: _uniqueStrings(policy?.never_allow_actions || []),
    hitl_required_actions: _uniqueStrings(policy?.hitl_required_actions || []),
    auto_approve_actions: _uniqueStrings(policy?.auto_approve_actions || []),
    never_allow_capabilities: _uniqueStrings(policy?.never_allow_capabilities || []),
    hitl_required_capabilities: _uniqueStrings(policy?.hitl_required_capabilities || []),
    auto_approve_capabilities: _uniqueStrings(policy?.auto_approve_capabilities || []),
    max_risk_level: (policy?.max_risk_level || "high") as GovernancePolicy["max_risk_level"],
    budget_caps_per_kind: policy?.budget_caps_per_kind || {},
  };
}

function _inferGovernanceRule(text: string): {
  field: "hitl_required_actions" | "never_allow_actions";
  patterns: string[];
  capabilityField: "hitl_required_capabilities" | "never_allow_capabilities";
  capabilityPatterns: string[];
  rule_type: string;
} {
  const inferred = inferRuntimeRuleFromText(text);
  return {
    field: inferred.field,
    patterns: inferred.patterns,
    capabilityField: inferred.capabilityField,
    capabilityPatterns: inferred.capabilityPatterns,
    rule_type: inferred.rule_type,
  };
}

function _ruleCopy() {
  return {
    title: t("page.workspace_detail.rule_copy.title"),
    subtitle: t("page.workspace_detail.rule_copy.subtitle"),
    settings: t("page.workspace_detail.rule_copy.settings"),
    revision: t("page.workspace_detail.rule_copy.revision"),
    addTitle: t("page.workspace_detail.rule_copy.add_title"),
    placeholder: t("page.workspace_detail.rule_copy.placeholder"),
    note: t("page.workspace_detail.rule_copy.note"),
    addRule: t("page.workspace_detail.rule_copy.add_rule"),
    examples: t("page.workspace_detail.rule_copy.examples"),
    currentTitle: t("page.workspace_detail.rule_copy.current_title"),
    empty: t("page.workspace_detail.rule_copy.empty"),
    remove: t("page.workspace_detail.rule_copy.remove"),
    ruleCount: t("page.workspace_detail.rule_copy.rule_count"),
    settingsTitle: t("page.workspace_detail.rule_copy.settings_title"),
    settingsSubtitle: t("page.workspace_detail.rule_copy.settings_subtitle"),
    maxRisk: t("page.workspace_detail.rule_copy.max_risk"),
    actionPatterns: t("page.workspace_detail.rule_copy.action_patterns"),
    quickTemplates: t("page.workspace_detail.rule_copy.quick_templates"),
    quickTemplatesHint: t("page.workspace_detail.rule_copy.quick_templates_hint"),
    done: t("page.workspace_detail.rule_copy.done"),
    noPatterns: t("page.workspace_detail.rule_copy.no_patterns"),
    enforcedAs: t("page.workspace_detail.rule_copy.enforced_as"),
    noEnforcement: t("page.workspace_detail.rule_copy.no_enforcement"),
    actionKeyHint: t("page.workspace_detail.rule_copy.action_key_hint"),
    patternNeedsApproval: t("page.workspace_detail.rule_copy.pattern_needs_approval"),
    patternNeedsApprovalDesc: t("page.workspace_detail.rule_copy.pattern_needs_approval_desc"),
    patternRequiresContext: t("page.workspace_detail.rule_copy.pattern_requires_context"),
    patternNeverAllow: t("page.workspace_detail.rule_copy.pattern_never_allow"),
    patternNeverAllowDesc: t("page.workspace_detail.rule_copy.pattern_never_allow_desc"),
    patternAutoApprove: t("page.workspace_detail.rule_copy.pattern_auto_approve"),
    patternAutoApproveDesc: t("page.workspace_detail.rule_copy.pattern_auto_approve_desc"),
    riskOptions: [
      { value: "low", label: t("page.workspace_detail.rule_copy.risk_low") },
      { value: "medium", label: t("page.workspace_detail.rule_copy.risk_medium") },
      { value: "high", label: t("page.workspace_detail.rule_copy.risk_high") },
    ],
    examplesList: [
      { label: t("page.workspace_detail.rule_copy.example_review_social_posts_label"), text: t("page.workspace_detail.rule_copy.example_review_social_posts_text") },
      { label: t("page.workspace_detail.rule_copy.example_confirm_client_messages_label"), text: t("page.workspace_detail.rule_copy.example_confirm_client_messages_text") },
      { label: t("page.workspace_detail.rule_copy.example_block_deletes_label"), text: t("page.workspace_detail.rule_copy.example_block_deletes_text") },
    ],
    templates: {
      social: t("page.workspace_detail.rule_copy.template_social"),
      email: t("page.workspace_detail.rule_copy.template_email"),
      message: t("page.workspace_detail.rule_copy.template_message"),
      delete: t("page.workspace_detail.rule_copy.template_delete"),
      files: t("page.workspace_detail.rule_copy.template_files"),
    },
  };
}

/** Lightweight field wrapper -- uppercase label + child control.
 *  Used when we need uncontrolled (defaultValue + onBlur) inputs that
 *  the strict-controlled `<Input>` / `<Textarea>` components don't allow. */
function _Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label
        style={{
          display: "block",
          fontSize: 11,
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          color: "#78716c",
          marginBottom: 6,
        }}
      >
        {label}
      </label>
      {children}
    </div>
  );
}

function KnowledgePurposeField({
  value,
  placeholder,
  disabled,
  onCommit,
}: {
  value: string;
  placeholder: string;
  disabled?: boolean;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  return (
    <Textarea
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        if (draft !== value) onCommit(draft);
      }}
      placeholder={placeholder}
      rows={2}
      disabled={disabled}
    />
  );
}

function KnowledgePolicyRow({
  title,
  description,
  checked,
  disabled,
  active,
  onToggle,
}: {
  title: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  active?: boolean;
  onToggle: () => void;
}) {
  const handleActivate = () => {
    if (!disabled) onToggle();
  };

  return (
    <div
      role="group"
      aria-label={title}
      onClick={handleActivate}
      style={{
        width: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 16,
        padding: "14px 16px",
        borderRadius: 14,
        border: active ? "1px solid rgba(95,146,138,0.25)" : "1px solid rgba(231,229,228,0.9)",
        background: active ? "rgba(242,246,245,0.7)" : "rgba(250,250,249,0.72)",
        textAlign: "left",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.65 : 1,
      }}
    >
      <span style={{ minWidth: 0 }}>
        <strong style={{ display: "block", fontSize: 13, color: "#1c1917", marginBottom: 3 }}>
          {title}
        </strong>
        <span style={{ display: "block", fontSize: 12, color: "#78716c", lineHeight: 1.45 }}>
          {description}
        </span>
      </span>
      <Toggle
        size="md"
        checked={checked}
        onChange={onToggle}
        disabled={disabled}
        aria-label={title}
      />
    </div>
  );
}

const PRIMARY_TABS: { key: PrimaryTab; label: string }[] = [
  { key: "overview", label: t("page.agent_detail.overview") },
  { key: "configure", label: t("page.workspace_detail.configure") },
  { key: "activity", label: t("nav.activity") },
  { key: "settings", label: t("nav.settings") },
];

const SETUP_TAB_ITEMS: { key: Tab; label: string }[] = [
  { key: "staff", label: t("page.workspace_detail.staff") },
  { key: "agents", label: t("nav.agents") },
  { key: "capabilities", label: t("page.workspace_detail.capabilities") },
  { key: "channels", label: t("page.workspace_detail.channels") },
  { key: "documents", label: t("nav.knowledge") },
  { key: "rules", label: t("page.workspace_detail.rules") },
  { key: "goals", label: t("nav.goals") },
  { key: "automations", label: t("page.scheduled_jobs.automations") },
  { key: "learning", label: t("page.workspace_detail.learning") },
];


// ── Notification routing card ────────────────────────────────────────────
//
// Reads + writes ``workspace.settings.notification_policy``:
//   - default_routes:        channel keys that get any event without an override
//   - routes[<event_kind>]:  per-kind channel list (overrides default_routes)
//   - hitl_notify_user_ids:  who gets paged when a HITL pause fires.
//                            Empty array = nobody (suppress); absent = entity
//                            owners + admins via the backend fallback.
//
// Backend reads the same JSONB shape — see notification_routing.select_channels
// and notification_workspace_callbacks.notify_workspace_hitl_approvers.

const _NOTIF_SUPPORTED_CHANNELS = [
  "inapp", "email", "telegram", "wechat", "whatsapp", "slack", "discord", "twilio_sms",
];
const _NOTIF_CHANNEL_LABELS: Record<string, string> = {
  inapp: "In-app", email: "Email", telegram: "Telegram", wechat: "WeChat",
  whatsapp: "WhatsApp", slack: "Slack", discord: "Discord", twilio_sms: "SMS",
};
const _NOTIF_EVENTS = [
  { kind: "task_hitl_requested", label: "Task needs input" },
  { kind: "task_hitl_reminder", label: "Approval reminder" },
  { kind: "task_failed", label: "Task failed" },
  { kind: "task_succeeded", label: "Task completed" },
  { kind: "task_assigned", label: "Task assigned" },
  { kind: "system_health", label: "System health alert" },
];

function _normaliseRouteList(values: any): string[] {
  if (!Array.isArray(values)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of values) {
    if (typeof v === "string" && _NOTIF_SUPPORTED_CHANNELS.includes(v) && !seen.has(v)) {
      seen.add(v);
      out.push(v);
    }
  }
  return out;
}

function WorkspaceNotificationRoutingCard({
  ws, updateWorkspace,
}: { ws: any; updateWorkspace: any }) {
  const policy = ((ws.settings as any)?.notification_policy || {}) as Record<string, any>;
  const defaultRoutes = _normaliseRouteList(policy.default_routes);
  const routes = (typeof policy.routes === "object" && policy.routes !== null
    ? policy.routes
    : {}) as Record<string, any>;
  const hitlNotifyUsers: string[] = Array.isArray(policy.hitl_notify_user_ids)
    ? policy.hitl_notify_user_ids.filter((u: any) => typeof u === "string")
    : [];
  const hitlExplicit = Array.isArray(policy.hitl_notify_user_ids);
  const inboundNotifyUsers: string[] = Array.isArray(policy.inbound_notify_user_ids)
    ? policy.inbound_notify_user_ids.filter((u: any) => typeof u === "string")
    : [];
  const inboundExplicit = Array.isArray(policy.inbound_notify_user_ids);

  // Members list for the HITL recipient picker.
  const { data: staffData } = useQuery({
    queryKey: ["workspace-staff", ws.id],
    queryFn: () => api.workspaces.staff.list(ws.id),
    staleTime: 30_000,
  });
  const members: Array<{ user_id?: string; display_name?: string; email?: string }> =
    (staffData as any)?.items || (staffData as any) || [];

  function commitPolicy(patch: Record<string, any>) {
    const nextSettings = { ...(ws.settings || {}) } as Record<string, any>;
    const np = { ...(nextSettings.notification_policy as Record<string, any> || {}) };
    Object.assign(np, patch);
    nextSettings.notification_policy = np;
    updateWorkspace.mutate({ settings: nextSettings });
  }

  function toggleDefault(channel: string, checked: boolean) {
    const next = new Set(defaultRoutes);
    if (checked) next.add(channel); else next.delete(channel);
    next.delete("inapp");
    commitPolicy({ default_routes: Array.from(next) });
  }

  function toggleKind(kind: string, channel: string, checked: boolean) {
    const current = _normaliseRouteList(routes[kind]);
    const next = new Set(current);
    if (checked) next.add(channel); else next.delete(channel);
    next.delete("inapp");
    const nextRoutes = { ...routes };
    if (next.size === 0) {
      delete nextRoutes[kind];
    } else {
      nextRoutes[kind] = Array.from(next);
    }
    commitPolicy({ routes: nextRoutes });
  }

  function resetKind(kind: string) {
    const nextRoutes = { ...routes };
    delete nextRoutes[kind];
    commitPolicy({ routes: nextRoutes });
  }

  function toggleHitlUser(user_id: string, checked: boolean) {
    const next = new Set(hitlNotifyUsers);
    if (checked) next.add(user_id); else next.delete(user_id);
    commitPolicy({ hitl_notify_user_ids: Array.from(next) });
  }

  function clearHitlOverride() {
    const nextPolicy = { ...policy };
    delete nextPolicy.hitl_notify_user_ids;
    const nextSettings = { ...(ws.settings || {}) } as Record<string, any>;
    nextSettings.notification_policy = nextPolicy;
    updateWorkspace.mutate({ settings: nextSettings });
  }

  function toggleInboundUser(user_id: string, checked: boolean) {
    const next = new Set(inboundNotifyUsers);
    if (checked) next.add(user_id); else next.delete(user_id);
    commitPolicy({ inbound_notify_user_ids: Array.from(next) });
  }

  function clearInboundOverride() {
    const nextPolicy = { ...policy };
    delete nextPolicy.inbound_notify_user_ids;
    const nextSettings = { ...(ws.settings || {}) } as Record<string, any>;
    nextSettings.notification_policy = nextPolicy;
    updateWorkspace.mutate({ settings: nextSettings });
  }

  return (
    <GlassCard hoverable={false}>
      <div style={SECTION_TITLE}>{t("page.workspace_detail.notification_routing")}</div>
      <p style={{ fontSize: 12, color: "#78716c", marginTop: 0, marginBottom: 12, lineHeight: 1.5 }}>
        {t("page.workspace_detail.notification_routing_desc")}
      </p>

      {/* Default channels */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 12, color: "#57534e", fontWeight: 600, marginBottom: 6 }}>
          {t("page.settings.default_routes")}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {_NOTIF_SUPPORTED_CHANNELS.map((ct) => {
            const checked = ct === "inapp" || defaultRoutes.includes(ct);
            const locked = ct === "inapp";
            return (
              <label key={ct} style={{
                display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 10px",
                borderRadius: 999, border: "1px solid",
                borderColor: checked ? "rgba(79,125,117,0.45)" : "rgba(214,211,209,0.7)",
                background: checked ? "rgba(79,125,117,0.08)" : "rgba(255,255,255,0.6)",
                color: locked ? "#a8a29e" : (checked ? "#1c1917" : "#57534e"),
                fontSize: 12, fontWeight: 500, cursor: locked ? "not-allowed" : "pointer",
                userSelect: "none", whiteSpace: "nowrap",
              }}>
                <input
                  type="checkbox" checked={checked} disabled={locked}
                  onChange={(e) => !locked && toggleDefault(ct, e.target.checked)}
                  style={{ accentColor: "#4f7d75", margin: 0 }}
                />
                {_NOTIF_CHANNEL_LABELS[ct] || ct}
              </label>
            );
          })}
        </div>
      </div>

      {/* Per-event overrides */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 12, color: "#57534e", fontWeight: 600, marginBottom: 6 }}>
          {t("page.settings.event_routing")}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {_NOTIF_EVENTS.map((evt) => {
            const override = _normaliseRouteList(routes[evt.kind]);
            const customised = override.length > 0;
            const effective = customised ? new Set([...override, "inapp"]) : new Set([...defaultRoutes, "inapp"]);
            return (
              <div key={evt.kind} style={{
                padding: 10, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 10,
                background: "rgba(255,255,255,0.5)",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: "#292524" }}>{evt.label}</div>
                  {customised && (
                    <button
                      type="button" onClick={() => resetKind(evt.kind)}
                      style={{
                        fontSize: 11, padding: "3px 8px", borderRadius: 6,
                        border: "1px solid rgba(28,25,23,0.06)",
                        background: "rgba(255,255,255,0.7)", color: "#78716c", cursor: "pointer",
                      }}
                    >
                      {t("page.settings.use_default")}
                    </button>
                  )}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {_NOTIF_SUPPORTED_CHANNELS.map((ct) => {
                    const checked = effective.has(ct);
                    const locked = ct === "inapp";
                    return (
                      <label key={ct} style={{
                        display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 8px",
                        borderRadius: 999, border: "1px solid",
                        borderColor: checked ? "rgba(79,125,117,0.45)" : "rgba(214,211,209,0.7)",
                        background: checked ? "rgba(79,125,117,0.08)" : "rgba(255,255,255,0.6)",
                        color: locked ? "#a8a29e" : (checked ? "#1c1917" : "#57534e"),
                        fontSize: 11, fontWeight: 500, cursor: locked ? "not-allowed" : "pointer",
                        userSelect: "none",
                      }}>
                        <input
                          type="checkbox" checked={checked} disabled={locked}
                          onChange={(e) => !locked && toggleKind(evt.kind, ct, e.target.checked)}
                          style={{ accentColor: "#4f7d75", margin: 0 }}
                        />
                        {_NOTIF_CHANNEL_LABELS[ct] || ct}
                      </label>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* HITL recipients */}
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
          <div style={{ fontSize: 12, color: "#57534e", fontWeight: 600 }}>
            {t("page.workspace_detail.hitl_recipients")}
          </div>
          {hitlExplicit && (
            <button
              type="button" onClick={clearHitlOverride}
              style={{
                fontSize: 11, padding: "3px 8px", borderRadius: 6,
                border: "1px solid rgba(28,25,23,0.06)",
                background: "rgba(255,255,255,0.7)", color: "#78716c", cursor: "pointer",
              }}
            >
              {t("page.settings.use_default")}
            </button>
          )}
        </div>
        <p style={{ margin: "0 0 8px", fontSize: 12, color: "#78716c" }}>
          {t("page.workspace_detail.hitl_recipients_desc")}
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {members.length === 0 ? (
            <span style={{ fontSize: 12, color: "#a8a29e" }}>
              {t("page.workspace_detail.no_members")}
            </span>
          ) : members.map((m) => {
            const uid = m.user_id || "";
            if (!uid) return null;
            const checked = hitlNotifyUsers.includes(uid);
            const label = m.display_name || m.email || uid;
            return (
              <label key={uid} style={{
                display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 10px",
                borderRadius: 999, border: "1px solid",
                borderColor: checked ? "rgba(79,125,117,0.45)" : "rgba(214,211,209,0.7)",
                background: checked ? "rgba(79,125,117,0.08)" : "rgba(255,255,255,0.6)",
                color: checked ? "#1c1917" : "#57534e",
                fontSize: 12, fontWeight: 500, cursor: "pointer", userSelect: "none",
              }}>
                <input
                  type="checkbox" checked={checked}
                  onChange={(e) => toggleHitlUser(uid, e.target.checked)}
                  style={{ accentColor: "#4f7d75", margin: 0 }}
                />
                {label}
              </label>
            );
          })}
        </div>
      </div>

      {/* Inbound message recipients */}
      <div style={{ marginTop: 18 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
          <div style={{ fontSize: 12, color: "#57534e", fontWeight: 600 }}>
            {t("page.workspace_detail.inbound_recipients")}
          </div>
          {inboundExplicit && (
            <button
              type="button" onClick={clearInboundOverride}
              style={{
                fontSize: 11, padding: "3px 8px", borderRadius: 6,
                border: "1px solid rgba(28,25,23,0.06)",
                background: "rgba(255,255,255,0.7)", color: "#78716c", cursor: "pointer",
              }}
            >
              {t("page.settings.use_default")}
            </button>
          )}
        </div>
        <p style={{ margin: "0 0 8px", fontSize: 12, color: "#78716c" }}>
          {t("page.workspace_detail.inbound_recipients_desc")}
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {members.length === 0 ? (
            <span style={{ fontSize: 12, color: "#a8a29e" }}>
              {t("page.workspace_detail.no_members")}
            </span>
          ) : members.map((m) => {
            const uid = m.user_id || "";
            if (!uid) return null;
            const checked = inboundNotifyUsers.includes(uid);
            const label = m.display_name || m.email || uid;
            return (
              <label key={uid} style={{
                display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 10px",
                borderRadius: 999, border: "1px solid",
                borderColor: checked ? "rgba(79,125,117,0.45)" : "rgba(214,211,209,0.7)",
                background: checked ? "rgba(79,125,117,0.08)" : "rgba(255,255,255,0.6)",
                color: checked ? "#1c1917" : "#57534e",
                fontSize: 12, fontWeight: 500, cursor: "pointer", userSelect: "none",
              }}>
                <input
                  type="checkbox" checked={checked}
                  onChange={(e) => toggleInboundUser(uid, e.target.checked)}
                  style={{ accentColor: "#4f7d75", margin: 0 }}
                />
                {label}
              </label>
            );
          })}
        </div>
      </div>
    </GlassCard>
  );
}


export default function WorkspaceDetail() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const currentUser = useAuthStore((s) => s.user);
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const [tab, setTab] = useState<Tab>(
    requestedTab === "chat" ? "overview" : _normalizeWorkspaceDetailTab(requestedTab),
  );
  const [lastSetupTab, setLastSetupTab] = useState<Tab>(() => {
    const initialTab = _normalizeWorkspaceDetailTab(requestedTab);
    return _isSetupTab(initialTab) ? initialTab : "agents";
  });
  const shouldShowWorkspaceWelcome = searchParams.get("created") === "1" || searchParams.get("welcome") === "1";
  const [showWorkspaceWelcome, setShowWorkspaceWelcome] = useState(shouldShowWorkspaceWelcome);


  const commitTab = useCallback((nextTab: Tab, options?: { replace?: boolean }) => {
    setTab(nextTab);
    if (_isSetupTab(nextTab)) setLastSetupTab(nextTab);
    const nextParams = new URLSearchParams(searchParams);
    if (nextTab === "overview") {
      nextParams.delete("tab");
    } else {
      nextParams.set("tab", nextTab);
    }
    setSearchParams(nextParams, { replace: options?.replace ?? true });
  }, [searchParams, setSearchParams]);

  // Reset/sync local state when workspace or URL tab changes.
  useEffect(() => {
    if (requestedTab === "chat" && workspaceId) {
      setTab("overview");
      navigate(`/chat?workspace=${encodeURIComponent(workspaceId)}`, { replace: true });
      return;
    }
    const nextTab = _normalizeWorkspaceDetailTab(requestedTab);
    setTab(nextTab);
    if (_isSetupTab(nextTab)) setLastSetupTab(nextTab);
    if (requestedTab && requestedTab !== nextTab) {
      const nextParams = new URLSearchParams(searchParams);
      if (nextTab === "overview") {
        nextParams.delete("tab");
      } else {
        nextParams.set("tab", nextTab);
      }
      setSearchParams(nextParams, { replace: true });
    }
  }, [workspaceId, requestedTab, searchParams, setSearchParams, navigate]);

  useEffect(() => {
    if (shouldShowWorkspaceWelcome) setShowWorkspaceWelcome(true);
  }, [shouldShowWorkspaceWelcome]);

  const handleTabChange = useCallback((t: string) => {
    const nextTab = _normalizeWorkspaceDetailTab(t);
    commitTab(nextTab);
  }, [commitTab]);

  const handlePrimaryTabChange = useCallback((t: string) => {
    if (t === "configure") {
      commitTab(_isSetupTab(tab) ? tab : lastSetupTab);
      return;
    }
    commitTab(_normalizeWorkspaceDetailTab(t));
  }, [commitTab, lastSetupTab, tab]);

  const dismissWorkspaceWelcome = useCallback(() => {
    setShowWorkspaceWelcome(false);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("created");
    nextParams.delete("welcome");
    setSearchParams(nextParams, { replace: true });
  }, [searchParams, setSearchParams]);

  const openWorkspaceChatFromWelcome = useCallback(() => {
    dismissWorkspaceWelcome();
    if (workspaceId) navigate(`/chat?workspace=${encodeURIComponent(workspaceId)}`);
  }, [dismissWorkspaceWelcome, navigate, workspaceId]);

  const openWorkspaceGoalsFromWelcome = useCallback(() => {
    setShowWorkspaceWelcome(false);
    setTab("goals");
    setLastSetupTab("goals");
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("created");
    nextParams.delete("welcome");
    nextParams.set("tab", "goals");
    setSearchParams(nextParams, { replace: true });
  }, [searchParams, setSearchParams]);

  /* ---- modal state ---- */
  const [showStaffModal, setShowStaffModal] = useState(false);
  const [showWorkspaceShareModal, setShowWorkspaceShareModal] = useState(false);
  // Permission-v1: role is one of the 5 workspace roles; expires_at is
  // an optional ISO date (yyyy-mm-dd) — empty string = permanent.
  const [staffForm, setStaffForm] = useState({ staff_id: "", role: "", expires_at: "" });
  // Picked staff snapshot — drives the pending-chip preview between
  // "user clicked someone in the picker" and "user clicked Assign".
  const [pickedStaff, setPickedStaff] = useState<StaffOption | null>(null);
  const [confirmRemoveStaff, setConfirmRemoveStaff] = useState<string | null>(null);
  const [showAgentModal, setShowAgentModal] = useState(false);
  const [agentForm, setAgentForm] = useState<WorkspaceAgentForm>(DEFAULT_AGENT_FORM);
  const [agentConnectionResult, setAgentConnectionResult] = useState<AgentConnectionResult>(null);
  const [confirmUnmapAgent, setConfirmUnmapAgent] = useState<string | null>(null);
  let visibleAgentSourceOptions = AGENT_SOURCE_OPTIONS;

  const [showChannelModal, setShowChannelModal] = useState(false);
  const [editingChannel, setEditingChannel] = useState<any | null>(null);
  const [channelForm, setChannelForm] = useState({
    mode: "existing",
    channel_config_id: "",
    name: "",
    purpose: "",
    linked_service_key: "",
    role: "primary_external",
    login_required: false,
    language: "en",
  });
  const [confirmRemoveChannel, setConfirmRemoveChannel] = useState<{ id: string; name: string } | null>(null);
  const [expandedChannelDeveloperSettings, setExpandedChannelDeveloperSettings] = useState<Record<string, boolean>>({});
  const [showGoalEditor, setShowGoalEditor] = useState(false);
  const [editingGoal, setEditingGoal] = useState<any>(null);
  const [goalForm, setGoalForm] = useState<any>({});
  const [goalsDraft, setGoalsDraft] = useState("");
  const [showSettingsEditor, setShowSettingsEditor] = useState(false);
  const [settingsDraft, setSettingsDraft] = useState("");
  const [rulePrompt, setRulePrompt] = useState("");
  const [showRuleSettings, setShowRuleSettings] = useState(false);
  const [showAddKnowledgeModal, setShowAddKnowledgeModal] = useState(false);
  const [showCreateKnowledgeFolderModal, setShowCreateKnowledgeFolderModal] = useState(false);
  const [showKnowledgePolicyModal, setShowKnowledgePolicyModal] = useState(false);
  const [knowledgeFolderSettingsGroupId, setKnowledgeFolderSettingsGroupId] = useState("");
  const [knowledgeFolderForm, setKnowledgeFolderForm] = useState({ name: "", purpose: "" });
  const [knowledgeFolderError, setKnowledgeFolderError] = useState("");
  const [knowledgeSearch, setKnowledgeSearch] = useState("");
  const [knowledgeTargetGroupId, setKnowledgeTargetGroupId] = useState("");
  const [selectedKnowledgeDocIds, setSelectedKnowledgeDocIds] = useState<string[]>([]);
  const [confirmRemoveKnowledgeDoc, setConfirmRemoveKnowledgeDoc] = useState<{ groupId: string; documentId: string; name: string } | null>(null);
  const [confirmDeleteKnowledgeGroup, setConfirmDeleteKnowledgeGroup] = useState<{ groupId: string; name: string } | null>(null);
  // M12 Workspace Blueprint flow.
  const [exportOpen, setExportOpen] = useState(false);
  const [confirmDeleteWs, setConfirmDeleteWs] = useState(false);

  /* ---- queries ---- */

  const { data: workspace, isLoading, error } = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => api.workspaces.get(workspaceId!),
    enabled: !!workspaceId,
  });

  const { data: dashboardStats } = useQuery({
    queryKey: ["workspace-dashboard", workspaceId],
    queryFn: () => api.workspaces.dashboard(workspaceId!),
    enabled: !!workspaceId && tab === "overview",
  });

  const { data: heartbeatStatus } = useQuery({
    queryKey: ["workspace-heartbeat", workspaceId],
    queryFn: () => api.workspaces.heartbeat.status(workspaceId!),
    enabled: !!workspaceId && (tab === "overview" || tab === "settings"),
  });

  const { data: staffList } = useQuery({
    queryKey: ["workspace-staff", workspaceId],
    queryFn: () => api.workspaces.staff.list(workspaceId!),
    enabled: !!workspaceId,
    staleTime: 30_000,
  });
  const canManageWs = canManageWorkspace(currentUser, staffList || []);

  // Pool of staff members in the entity that can be picked for assignment.
  // Only fetched when the assign modal is open to avoid a hot query on tab load.
  const { data: entityStaff } = useQuery({
    queryKey: ["entity-staff"],
    queryFn: () => api.staff.list(),
    enabled: showStaffModal || showWorkspaceShareModal || (!!workspaceId && tab === "staff"),
  });

  const { data: agentMappings } = useQuery({
    queryKey: ["workspace-agents", workspaceId],
    queryFn: () => api.workspaces.agents.list(workspaceId!),
    // Also load on overview/channels so we can show service<->agent matches there.
    enabled: !!workspaceId && (tab === "agents" || tab === "overview" || tab === "channels"),
  });

  // Pool of agents the user could map -- also used to render the agent
  // *name* (instead of bare ULID) on the agent mappings table and the
  // Overview "Services + Matched Agents" panel.
  const { data: entityAgents } = useQuery({
    queryKey: ["entity-agents-for-mapping"],
    queryFn: () => api.agents.list(),
    enabled: !!workspaceId && (tab === "agents" || tab === "overview" || tab === "channels"),
  });

  const { data: channels } = useQuery({
    queryKey: ["workspace-channels", workspaceId],
    queryFn: () => api.workspaces.channels(workspaceId!),
    enabled: !!workspaceId && tab === "channels",
  });

  const { data: availableChannels } = useQuery({
    queryKey: ["workspace-available-channels", workspaceId],
    queryFn: () => api.workspaces.availableChannels(workspaceId!),
    enabled: !!workspaceId && showChannelModal,
  });

  const { data: workspaceCapabilities } = useQuery({
    queryKey: ["workspace-capabilities", workspaceId],
    queryFn: () => api.workspaces.capabilities(workspaceId!),
    enabled: !!workspaceId && tab === "capabilities",
  });

  const { data: capabilityIntegrationStatus } = useQuery({
    queryKey: ["workspace-capability-integration-status"],
    queryFn: () => api.integrations.mcpServers(),
    enabled: privateApiEnabled && !!workspaceId && tab === "capabilities",
  });

  const { data: documents } = useQuery({
    queryKey: ["workspace-documents", workspaceId],
    queryFn: () => api.workspaces.documents(workspaceId!),
    enabled: !!workspaceId && tab === "documents",
  });

  const { data: availableKnowledgeDocs } = useQuery({
    queryKey: ["workspace-available-documents", workspaceId, knowledgeSearch],
    queryFn: () => api.documents.list({
      search: knowledgeSearch.trim() || undefined,
      include_generated_assets: true,
      limit: 100,
    }),
    enabled: !!workspaceId && showAddKnowledgeModal,
    staleTime: 10_000,
  });

  const { data: governancePolicy } = useQuery({
    queryKey: ["workspace-governance", workspaceId],
    queryFn: () => api.workspaces.governance.get(workspaceId!),
    enabled: !!workspaceId && tab === "rules",
  });

  const { data: goals, isLoading: goalsLoading } = useQuery({
    queryKey: ["workspace-goals", workspaceId],
    queryFn: () => api.goals.list({ workspace_id: workspaceId!, limit: 50 }),
    enabled: !!workspaceId && (tab === "goals" || tab === "overview"),
  });

  const { data: activityFeed } = useQuery({
    queryKey: ["workspace-activity", workspaceId],
    queryFn: () => api.workspaces.activity(workspaceId!, { limit: 50 }),
    enabled: !!workspaceId && tab === "activity",
  });

  const { data: learningCandidates, isLoading: learningLoading } = useQuery({
    queryKey: ["workspace-learning-candidates", workspaceId],
    queryFn: () => api.workspaces.learningCandidates(workspaceId!, { limit: 30, status: null }),
    enabled: !!workspaceId && tab === "learning",
  });

  const { data: runtimeEvidence, isLoading: evidenceLoading } = useQuery({
    queryKey: ["workspace-runtime-evidence", workspaceId],
    queryFn: () => api.workspaces.runtimeEvidence(workspaceId!, { limit: 30 }),
    enabled: !!workspaceId && tab === "learning",
  });

  const { data: workspaceEvaluation, isLoading: evaluationLoading } = useQuery({
    queryKey: ["workspace-evaluation", workspaceId],
    queryFn: () => api.workspaces.evaluation(workspaceId!, 30),
    enabled: !!workspaceId && (tab === "overview" || tab === "learning"),
    staleTime: 30_000,
  });

  const { data: budgetStatus } = useQuery({
    queryKey: ["workspace-budget", workspaceId],
    queryFn: () => api.workspaces.budget.get(workspaceId!),
    enabled: !!workspaceId && (tab === "overview" || tab === "settings"),
  });

  // The /operating-model endpoint wraps the model:
  //   { workspace_id, operating_model: { services, goals, rules, ... } }
  // Unwrap once here so consumers (Goals tab, Agent picker, Settings,
  // Overview summary) can read fields directly without touching the
  // envelope shape.
  const { data: operatingModelRaw } = useQuery({
    queryKey: ["workspace-operating-model", workspaceId],
    queryFn: () => api.workspaces.operatingModel(workspaceId!),
    enabled: !!workspaceId && (tab === "settings" || tab === "agents" || tab === "capabilities" || tab === "overview" || tab === "documents" || tab === "rules"),
  });
  const operatingModel: Record<string, any> | undefined =
    (operatingModelRaw as any)?.operating_model ?? operatingModelRaw;

  /* ---- mutations ---- */

  const heartbeatEnable = useMutation({
    mutationFn: () => api.workspaces.heartbeat.enable(workspaceId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-heartbeat", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      toast.success(t("page.workspace_detail.heartbeat_enabled"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_enable_heartbeat"), err.message),
  });

  const heartbeatDisable = useMutation({
    mutationFn: () => api.workspaces.heartbeat.disable(workspaceId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-heartbeat", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      toast.success(t("page.workspace_detail.heartbeat_disabled"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_disable_heartbeat"), err.message),
  });

  const assignStaff = useMutation({
    mutationFn: (data: { staff_id: string; role?: string; expires_at?: string }) =>
      api.workspaces.staff.assign(workspaceId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-staff", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      setShowStaffModal(false);
      setStaffForm({ staff_id: "", role: "", expires_at: "" });
      setPickedStaff(null);
      toast.success(t("page.workspace_detail.staff_assigned"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_assign_staff"), err.message),
  });

  const removeStaff = useMutation({
    mutationFn: (staffId: string) => api.workspaces.staff.remove(workspaceId!, staffId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-staff", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      setConfirmRemoveStaff(null);
      toast.success(t("page.workspace_detail.staff_removed"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_remove_staff"), err.message),
  });

  const mapAgent = useMutation({
    mutationFn: async (data: WorkspaceAgentForm) => {
      const serviceKey = data.service_key.trim();
      if (!serviceKey) throw new Error("Choose a workspace service first.");
      if (data.source === "https" && !data.https_url.trim()) {
        throw new Error("Enter the HTTPS agent endpoint.");
      }

      const services = ((operatingModel?.services as any[]) || []);
      const service = services.find((svc: any) => (svc.service_key || svc.key) === serviceKey) || {};
      const serviceName = service.name || _humanize(serviceKey) || "Workspace Agent";
      const customPrompt = data.custom_prompt.trim() || undefined;

      const bindWorkerToMappedSubscription = async (workerId: string) => {
        const mappings = await api.workspaces.agents.list(workspaceId!);
        const mapped = (mappings || []).find((m: any) => m.service_key === serviceKey);
        if (mapped?.id) {
          await api.agents.bindSubscriptionWorker(mapped.id, {
            worker_id: workerId,
            priority: 50,
            is_preferred: true,
          });
        }
      };

      if (data.source === "hosted") {
        if (!data.agent_id) throw new Error("Choose a Manor hosted agent.");
        await api.workspaces.agents.map(workspaceId!, {
          service_key: serviceKey,
          agent_id: data.agent_id,
          custom_prompt: customPrompt,
        });
        return { source: data.source, connection: null as AgentConnectionResult };
      }

      const agentName = data.agent_name.trim() || data.runtime_display_name.trim() || serviceName;
      const systemPrompt = data.system_prompt.trim() || [
        `You are the ${agentName} agent for the "${ws.name}" workspace.`,
        service.description ? `Service responsibility: ${service.description}` : null,
        ws.primary_work ? `Workspace primary work: ${ws.primary_work}` : null,
        ws.operating_context ? `Operating context: ${ws.operating_context}` : null,
        "Stay focused on this service and ask for operator input when the next action is unclear.",
      ].filter(Boolean).join("\n\n");
      const runtimeConnection =
        data.source === "https"
          ? { source: "https", endpoint_url: data.https_url.trim() }
            : { source: "manor_hosted" };

      const created = await api.agents.create({
        name: agentName,
        description: data.description.trim() || `${serviceName} agent for ${ws.name}.`,
        system_prompt: systemPrompt,
        category: ws.category || ws.kind || undefined,
        tags: [
          serviceKey,
          data.source === "https"
            ? "https-agent"
              : "workspace-agent",
        ],
        config: {
          runtime_connection: runtimeConnection,
          runtime_learning: { enabled: true },
        },
      });

      await api.workspaces.agents.map(workspaceId!, {
        service_key: serviceKey,
        agent_id: created.id,
        custom_prompt: customPrompt,
      });


      if (data.source === "https") {
        const endpoint = data.https_url.trim();
        const registration = await api.workers.register({
          kind: "custom_http",
          display_name: data.runtime_display_name.trim() || `${created.name} HTTPS`,
          description: `Generic HTTPS agent endpoint for ${created.name}: ${endpoint}`,
          version: "https-1",
          trust_level: "standard",
          capabilities: {
            supported_kinds: ["llm", "action", "subagent"],
            supported_providers: ["https_agent"],
            max_concurrent_leases: 1,
            max_risk_level: "medium",
            uses_manor_credentials: false,
            deployment: "remote",
            protocol_version: 1,
            runtime: "https",
            endpoint_url: endpoint,
            agent_id: created.id,
            workspace_id: workspaceId,
            service_key: serviceKey,
          },
        });
        await bindWorkerToMappedSubscription(registration.worker_id);
        return { source: data.source, connection: { type: "https", registration, agentName: created.name, endpoint } as AgentConnectionResult };
      }

      return { source: data.source, connection: null as AgentConnectionResult };
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-agents", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-capabilities", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["entity-agents-for-mapping"] });
    },
    onSettled: (result) => {
      if (!result) return;
      if (result.connection) {
        setAgentConnectionResult(result.connection);
      } else {
        setShowAgentModal(false);
        setAgentForm(DEFAULT_AGENT_FORM);
        setAgentConnectionResult(null);
      }
      toast.success(t("page.workspace_detail.agent_mapped"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_map_agent"), err.message),
  });

  const autoMapAgents = useMutation({
    mutationFn: async (suggestions: any[]) => {
      const results = { mapped: 0, created: 0, failed: 0 };
      // Build a quick lookup of existing entity agents so we can verify
      // that a recommended_agent_id actually resolves to a real agent
      // before trying to map; if not, fall through to auto-create.
      const knownAgents = new Set<string>(
        ((entityAgents as any[]) || []).map((a: any) => a.id),
      );
      // Index workspace services by key so we can pull a description /
      // owner_role to seed the auto-created agent's prompt.
      const servicesByKey = new Map<string, any>();
      for (const svc of (operatingModel?.services as any[]) || []) {
        const k = svc.service_key || svc.key;
        if (k) servicesByKey.set(k, svc);
      }

      for (const s of suggestions) {
        const service_key = s.service_key;
        if (!service_key) continue;

        let agent_id: string | undefined =
          s.recommended_agent_id || s.agent_id;

        // The recommendation pointed at an agent that no longer exists
        // (or never existed) -- treat as "no recommendation" and create
        // a fresh custom agent for this service.
        if (agent_id && !knownAgents.has(agent_id)) agent_id = undefined;

        if (!agent_id) {
          const svc = servicesByKey.get(service_key) || {};
          const displayName = svc.name || _humanize(service_key) || "Service Agent";
          const promptParts = [
            `You are the ${displayName} agent for the "${ws.name}" workspace.`,
            svc.description ? `Service responsibility: ${svc.description}` : null,
            ws.primary_work ? `Workspace primary work: ${ws.primary_work}` : null,
            ws.operating_context ? `Operating context: ${ws.operating_context}` : null,
            "Stay focused on this service. Defer to the operator on out-of-scope requests.",
          ].filter(Boolean) as string[];
          try {
            const created = await api.agents.create({
              name: displayName,
              description:
                svc.description || `Auto-created agent for the ${displayName} service.`,
              system_prompt: promptParts.join("\n\n"),
              category: ws.category || ws.kind || undefined,
              tags: [service_key],
            });
            agent_id = created.id;
            knownAgents.add(created.id);
            results.created += 1;
          } catch {
            results.failed += 1;
            continue;
          }
        }

        try {
          await api.workspaces.agents.map(workspaceId!, { service_key, agent_id });
          queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
          queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
          queryClient.invalidateQueries({ queryKey: ["workspace-capabilities", workspaceId] });
          results.mapped += 1;
        } catch {
          results.failed += 1;
        }
      }
      return results;
    },
    onSuccess: ({ mapped, created, failed }) => {
      queryClient.invalidateQueries({ queryKey: ["workspace-agents", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["entity-agents-for-mapping"] });
      const summary: string[] = [];
      if (mapped > 0) summary.push(`mapped ${mapped}`);
      if (created > 0) summary.push(`created ${created} new agent${created === 1 ? "" : "s"}`);
      if (failed > 0) summary.push(`${failed} failed`);
      if (failed === 0 && (mapped > 0 || created > 0)) {
        toast.success(t("page.workspace_detail.auto_map_complete"), summary.join(" · "));
      } else if (failed > 0) {
        toast.error(t("page.workspace_detail.auto_map_partially_failed"), summary.join(" · "));
      }
    },
  });

  const unmapAgent = useMutation({
    mutationFn: (serviceKey: string) => api.workspaces.agents.unmap(workspaceId!, serviceKey),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-agents", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-capabilities", workspaceId] });
      setConfirmUnmapAgent(null);
      toast.success(t("page.workspace_detail.agent_unmapped"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_unmap_agent"), err.message),
  });

  const attachChannel = useMutation({
    mutationFn: () => {
      const selectedMapping = ((agentMappings as any[]) || []).find(
        (m: any) => m.service_key === channelForm.linked_service_key,
      );
      const existing = channelForm.mode === "existing";
      return api.workspaces.attachChannel(workspaceId!, {
        channel_config_id: existing ? channelForm.channel_config_id : undefined,
        channel_type: existing ? undefined : "webchat",
        name: channelForm.name.trim() || undefined,
        purpose: channelForm.purpose.trim() || undefined,
        role: channelForm.role,
        linked_service_key: channelForm.linked_service_key || undefined,
        agent_subscription_id: selectedMapping?.id,
        agent_id: selectedMapping?.agent_id,
        config: {
          language: channelForm.language || "en",
          ...(existing ? {} : { login_required: channelForm.login_required }),
        },
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-available-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-dashboard", workspaceId] });
      setShowChannelModal(false);
      setChannelForm({
        mode: "existing",
        channel_config_id: "",
        name: "",
        purpose: "",
        linked_service_key: "",
        role: "primary_external",
        login_required: false,
        language: "en",
      });
      toast.success(t("page.workspace_detail.channel_attached"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_attach_channel"), err.message),
  });

  const updateChannel = useMutation({
    mutationFn: () => {
      if (!editingChannel?.channel_binding_id) throw new Error("Channel binding is missing");
      const selectedMapping = ((agentMappings as any[]) || []).find(
        (m: any) => m.service_key === channelForm.linked_service_key,
      );
      return api.workspaces.updateChannel(workspaceId!, editingChannel.channel_binding_id, {
        name: channelForm.name.trim() || undefined,
        purpose: channelForm.purpose.trim(),
        role: channelForm.role,
        linked_service_key: channelForm.linked_service_key || "",
        agent_subscription_id: selectedMapping?.id,
        agent_id: selectedMapping?.agent_id,
        config: {
          login_required: channelForm.login_required,
          language: channelForm.language || "en",
        },
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-available-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-dashboard", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      setShowChannelModal(false);
      setEditingChannel(null);
      toast.success(t("page.workspace_detail.channel_updated"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_update_channel"), err.message),
  });

  const removeChannel = useMutation({
    mutationFn: (channelBindingId: string) => api.workspaces.removeChannel(workspaceId!, channelBindingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-available-channels", workspaceId] });
      setConfirmRemoveChannel(null);
      toast.success(t("page.workspace_detail.channel_removed_from_workspace"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_remove_channel"), err.message),
  });

  const updateGoals = useMutation({
    mutationFn: (goals: any[]) => api.workspaces.goals(workspaceId!, goals),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-evaluation", workspaceId] });
      setShowGoalEditor(false);
      toast.success(t("page.workspace_detail.goals_saved"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_save_goals"), err.message),
  });

  const updateGoalMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => api.goals.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-goals", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-evaluation", workspaceId] });
      setEditingGoal(null);
      toast.success(t("page.workspace_detail.goal_updated"));
    },
    onError: (e: Error) => toast.error(t("page.workspace_detail.failed_to_update_goal"), e.message),
  });

  const updateWorkspace = useMutation({
    mutationFn: (data: Partial<Workspace>) => api.workspaces.update(workspaceId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      toast.success(t("page.workspace_detail.workspace_updated"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_update_workspace"), err.message),
  });

  const updateBudget = useMutation({
    mutationFn: (data: WorkspaceBudgetUpdate) =>
      api.workspaces.budget.update(workspaceId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-budget", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-evaluation", workspaceId] });
      toast.success(t("page.workspace_detail.budget_updated"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_update_budget"), err.message),
  });

  const resolveLearningCandidate = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "accepted" | "rejected" | "archived" }) =>
      api.workspaces.resolveLearningCandidate(workspaceId!, id, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-learning-candidates", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-evaluation", workspaceId] });
      toast.success("Learning suggestion updated");
    },
    onError: (err: Error) => toast.error("Could not update learning suggestion", err.message),
  });

  const applyLearningCandidate = useMutation({
    mutationFn: (id: string) => api.workspaces.applyLearningCandidate(workspaceId!, id),
    onSuccess: (candidate) => {
      queryClient.invalidateQueries({ queryKey: ["workspace-learning-candidates", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-capabilities", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-evaluation", workspaceId] });
      const applyStatus = String(candidate.resolution?.apply_status || "").toLowerCase();
      if (applyStatus === "failed") {
        const message =
          typeof candidate.resolution?.apply_error === "string" ? candidate.resolution.apply_error : undefined;
        toast.error(t("page.workspace_detail.learning_apply_failed"), message);
        return;
      }
      if (candidate.status === "applied") {
        toast.success(t("page.workspace_detail.learning_applied"));
        return;
      }
      toast.success(t("page.workspace_detail.learning_apply_queued"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_apply_learning"), err.message),
  });

  const deleteWorkspace = useMutation({
    mutationFn: () => api.workspaces.delete(workspaceId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      toast.success(t("page.workspace_detail.workspace_deleted"));
      navigate("/workspaces");
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_delete_workspace"), err.message),
  });

  const createDocGroup = useMutation({
    mutationFn: ({ name, purpose }: { name: string; purpose?: string }) => {
      return api.workspaces.knowledge.createGroup(workspaceId!, {
        name: name.trim(),
        kind: "knowledge_net",
        purpose: purpose?.trim() || t("page.workspace_detail.optional_workspace_knowledge_subset"),
      });
    },
    onSuccess: async (group: any) => {
      queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspaceId] });
      setKnowledgeTargetGroupId(group?.id || "");
      setShowCreateKnowledgeFolderModal(false);
      setKnowledgeFolderForm({ name: "", purpose: "" });
      setKnowledgeFolderError("");
      toast.success(t("page.workspace_detail.knowledge_folder_created"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_create_knowledge_folder"), err.message),
  });

  const openCreateKnowledgeFolderModal = useCallback(() => {
    if (!canManageWs) return;
    setKnowledgeFolderForm({ name: "", purpose: "" });
    setKnowledgeFolderError("");
    setShowCreateKnowledgeFolderModal(true);
  }, [canManageWs]);

  const submitCreateKnowledgeFolder = useCallback(() => {
    if (!canManageWs) return;
    const name = knowledgeFolderForm.name.trim();
    if (!name) {
      setKnowledgeFolderError(t("page.workspace_detail.folder_name_required"));
      return;
    }
    createDocGroup.mutate({
      name,
      purpose: knowledgeFolderForm.purpose,
    });
  }, [canManageWs, createDocGroup, knowledgeFolderForm.name, knowledgeFolderForm.purpose]);

  const addKnowledgeDocuments = useMutation({
    mutationFn: ({ groupId, documentIds }: { groupId: string; documentIds: string[] }) =>
      api.workspaces.knowledge.addDocuments(workspaceId!, groupId, documentIds),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-dashboard", workspaceId] });
      setSelectedKnowledgeDocIds([]);
      setShowAddKnowledgeModal(false);
      toast.success(
        result.added === 1
          ? t("page.workspace_detail.added_one_document")
          : t("page.workspace_detail.added_documents").replace("{count}", String(result.added)),
      );
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_add_documents"), err.message),
  });

  const removeKnowledgeDocument = useMutation({
    mutationFn: ({ groupId, documentId }: { groupId: string; documentId: string }) =>
      api.workspaces.knowledge.removeDocument(workspaceId!, groupId, documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-dashboard", workspaceId] });
      setConfirmRemoveKnowledgeDoc(null);
      toast.success(t("page.workspace_detail.document_removed_from_workspace_knowledge"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_remove_document"), err.message),
  });

  const deleteKnowledgeGroup = useMutation({
    mutationFn: (groupId: string) => api.workspaces.knowledge.deleteGroup(workspaceId!, groupId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-dashboard", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
      setConfirmDeleteKnowledgeGroup(null);
      toast.success(t("page.workspace_detail.knowledge_folder_removed"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_remove_knowledge_folder"), err.message),
  });

  const updateOperatingModel = useMutation({
    mutationFn: (model: Record<string, any>) => api.workspaces.updateOperatingModel(workspaceId!, model),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      setShowSettingsEditor(false);
      toast.success(t("page.workspace_detail.operating_model_saved"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_save_operating_model"), err.message),
  });

  const updateGovernance = useMutation({
    mutationFn: ({ policy, summary }: { policy: GovernancePolicy; summary?: string }) =>
      api.workspaces.governance.update(workspaceId!, policy, summary),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-governance", workspaceId] });
      toast.success(t("page.workspace_detail.guardrails_saved"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_detail.could_not_save_guardrails"), err.message),
  });

  const togglePause = useMutation({
    mutationFn: () => ws?.status === "active"
      ? api.workspaces.pause(workspaceId!)
      : api.workspaces.resume(workspaceId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-heartbeat", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      toast.success(ws?.status === "active" ? t("page.workspace_detail.workspace_paused") : t("page.workspace_detail.workspace_resumed"));
    },
    onError: (err: Error) => toast.error(t("page.dashboard.failed"), err.message),
  });

  /* ---- loading / error states ---- */

  if (isLoading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", gap: 12, color: "#a8a29e" }}>
        <LoadingSpinner size={20} />
        <span style={{ fontSize: 14 }}>{t("page.workspace_detail.loading")}</span>
      </div>
    );
  }

  if (error || !workspace) {
    return (
      <div style={{ padding: "2rem" }}>
        <EmptyState
          icon={
            <IconInfo size={32} className="text-stone-300" />
          }
          title={t("page.workspace_detail.not_found")}
          description={t("page.workspace_detail.not_found_desc")}
          action={<Button variant="outline" onClick={() => navigate("/workspaces")}>{t("page.workspace_detail.back_to_workspaces")}</Button>}
        />
      </div>
    );
  }

  const ws = workspace;

  /* ---- helpers ---- */

  function infoRow(label: string, value: React.ReactNode) {
    if (!value) return null;
    return (
      <div style={{ marginBottom: 16 }}>
        <div style={LABEL}>{label}</div>
        <div style={VALUE}>{value}</div>
      </div>
    );
  }

  const evaluationDimensions: { key: EvaluationDimensionKey; label: string }[] = [
    { key: "goal_impact", label: t("page.workspace_detail.eval_goal_impact") },
    { key: "cost_efficiency", label: t("page.workspace_detail.eval_cost_efficiency") },
    { key: "time_efficiency", label: t("page.workspace_detail.eval_time_efficiency") },
    { key: "execution_health", label: t("page.workspace_detail.eval_execution_health") },
    { key: "output_quality", label: t("page.workspace_detail.eval_output_quality") },
    { key: "user_feedback", label: t("page.workspace_detail.eval_user_feedback") },
    { key: "governance", label: t("page.workspace_detail.eval_governance") },
    { key: "learning", label: t("page.workspace_detail.eval_learning") },
  ];

  function scoreText(score: number | null | undefined) {
    return typeof score === "number" ? `${Math.round(score)}%` : t("page.workspace_detail.eval_unknown");
  }

  function scoreColor(score: number | null | undefined) {
    if (typeof score !== "number") return "#a8a29e";
    if (score >= 80) return "#436b65";
    if (score >= 60) return "#4869ac";
    if (score >= 40) return "#9a5630";
    return "#a23e38";
  }

  function scoreChipVariant(score: number | null | undefined): "green" | "blue" | "orange" | "red" | "slate" {
    if (typeof score !== "number") return "slate";
    if (score >= 80) return "green";
    if (score >= 60) return "blue";
    if (score >= 40) return "orange";
    return "red";
  }

  function trendChipVariant(direction: string | undefined): "green" | "orange" | "slate" {
    if (direction === "improving") return "green";
    if (direction === "declining") return "orange";
    return "slate";
  }

  function trendText(trend: WorkspaceEvaluationSnapshot["trend"] | undefined) {
    if (!trend || typeof trend.delta !== "number") return "";
    const delta = Math.round(trend.delta);
    if (trend.direction === "flat") return t("page.workspace_detail.eval_trend_flat");
    return t("page.workspace_detail.eval_trend_delta").replace("{delta}", `${delta >= 0 ? "+" : ""}${delta}`);
  }

  function renderWorkspaceEvaluationCard(options?: { compact?: boolean }) {
    const compact = !!options?.compact;
    const evaluation = workspaceEvaluation as WorkspaceEvaluationSnapshot | undefined;
    const overall = evaluation?.overall;
    const overallScore = overall?.score;
    const dimensions = evaluation?.dimensions || {};
    const shownDimensions = compact ? evaluationDimensions.slice(0, 4) : evaluationDimensions;
    const evidence = evaluation?.evidence_summary || {};
    const historyCount = evaluation?.history?.length || 0;
    const trendLabel = trendText(evaluation?.trend);

    if (evaluationLoading) {
      return (
        <GlassCard hoverable={false} className="workspace-evaluation-card">
          <div style={{ display: "grid", placeItems: "center", padding: compact ? 20 : 32 }}>
            <LoadingSpinner />
          </div>
        </GlassCard>
      );
    }

    return (
      <GlassCard hoverable={false} className="workspace-evaluation-card">
        <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
          <div>
            <div className="workspace-evaluation-title" style={SECTION_TITLE}>{t("page.workspace_detail.workspace_evaluation")}</div>
            <div className="workspace-evaluation-description" style={{ fontSize: 13, color: "#78716c", lineHeight: 1.6, maxWidth: 720 }}>
              {t("page.workspace_detail.workspace_evaluation_desc")}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <Chip variant={scoreChipVariant(overallScore)} size="sm">{scoreText(overallScore)}</Chip>
            {overall?.confidence && <Chip variant="slate" size="sm">{overall.confidence}</Chip>}
            {trendLabel && (
              <Chip variant={trendChipVariant(evaluation?.trend?.direction)} size="sm">{trendLabel}</Chip>
            )}
            {historyCount > 0 && (
              <Chip variant="slate" size="sm">
                {t("page.workspace_detail.eval_history_count").replace("{count}", String(historyCount))}
              </Chip>
            )}
          </div>
        </div>

        {!evaluation ? (
          <EmptyState
            title={t("page.workspace_detail.no_workspace_evaluation")}
            description={t("page.workspace_detail.no_workspace_evaluation_desc")}
          />
        ) : (
          <div style={{ marginTop: 18, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 18, alignItems: "stretch" }}>
            <div className="workspace-eval-overall-card" style={{
              border: "1px solid rgba(28,25,23,0.06)",
              borderRadius: 18,
              padding: 18,
              background: "linear-gradient(135deg, rgba(242,246,245,0.9), rgba(250,250,249,0.95))",
              display: "flex",
              flexDirection: "column",
              justifyContent: "space-between",
              minHeight: 150,
            }}>
              <div>
                <div className="workspace-eval-kicker" style={{ fontSize: 11, fontWeight: 800, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  {t("page.workspace_detail.eval_overall")}
                </div>
                <div className="workspace-eval-score" style={{ fontSize: 42, lineHeight: 1, fontWeight: 900, color: scoreColor(overallScore), marginTop: 10 }}>
                  {scoreText(overallScore)}
                </div>
                <div className="workspace-eval-summary" style={{ fontSize: 12, color: "#78716c", marginTop: 8, lineHeight: 1.45 }}>
                  {_evaluationText(overall?.summary || t("page.workspace_detail.eval_not_enough_data"))}
                </div>
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 16 }}>
                <Chip variant="slate" size="sm">{evaluation.window.days}d</Chip>
                <Chip variant="teal" size="sm">
                  {t("page.workspace_detail.eval_evidence_count").replace("{count}", String(evidence.runtime_evidence_count ?? 0))}
                </Chip>
              </div>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
                {shownDimensions.map((item) => {
                  const section = dimensions[item.key] || {};
                  const score = section.score as number | null | undefined;
                  const width = typeof score === "number" ? Math.max(3, Math.min(100, score)) : 0;
                  return (
                    <div key={item.key} className="workspace-eval-metric-card" style={{ border: "1px solid #eef2f7", borderRadius: 14, padding: 12, background: "rgba(250,250,249,0.64)" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center", marginBottom: 8 }}>
                        <span className="workspace-eval-metric-label" style={{ fontSize: 12, fontWeight: 750, color: "#44403c" }}>{item.label}</span>
                        <span className="workspace-eval-metric-score" style={{ fontSize: 12, fontWeight: 850, color: scoreColor(score) }}>{scoreText(score)}</span>
                      </div>
                      <div className="workspace-eval-track" style={{ height: 6, borderRadius: 999, background: "#e7e5e4", overflow: "hidden" }}>
                        <div style={{ height: "100%", width: `${width}%`, borderRadius: 999, background: scoreColor(score), transition: "width 360ms ease" }} />
                      </div>
                      {!compact && section.summary && (
                        <div className="workspace-eval-metric-summary" style={{ fontSize: 11, color: "#a8a29e", lineHeight: 1.4, marginTop: 8 }}>{_evaluationText(section.summary)}</div>
                      )}
                    </div>
                  );
                })}
              </div>

              {evaluation.recommendations.length > 0 && (
                <div className="workspace-eval-next-attention" style={{ borderTop: "1px solid #f5f5f4", paddingTop: 12 }}>
                  <div className="workspace-eval-kicker" style={{ fontSize: 11, fontWeight: 850, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
                    {t("page.workspace_detail.eval_next_attention")}
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {evaluation.recommendations.slice(0, compact ? 2 : 5).map((rec, i) => (
                      <div key={`${rec}-${i}`} className="workspace-eval-recommendation" style={{ fontSize: 12, color: "#57534e", lineHeight: 1.45 }}>
                        {_evaluationText(rec)}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {compact && (
                <div>
                  <Button variant="outline" size="sm" onClick={() => handleTabChange("learning")}>
                    {t("page.workspace_detail.view_full_evaluation")}
                  </Button>
                </div>
              )}
            </div>
          </div>
        )}
      </GlassCard>
    );
  }

  /* ---- tab renderers ---- */

  function renderOverview() {
    const stats = dashboardStats;
    // Setup progress banner — shown for freshly created workspaces
    const agentCount = (agentMappings as any[])?.length ?? 0;
    const goalCount = (operatingModel?.goals as any[])?.length ?? 0;
    const bannerDismissKey = `ws_setup_dismissed_${workspaceId}`;
    const bannerDismissed = localStorage.getItem(bannerDismissKey) === "1";
    const isNew = ws.created_at && (Date.now() - new Date(ws.created_at).getTime()) < 7 * 24 * 60 * 60 * 1000; // < 7 days old
    const showSetupBanner = isNew && !bannerDismissed;

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
        {/* Setup progress banner */}
        {showSetupBanner && (
          <div style={{
            borderRadius: 16, padding: "16px 20px",
            background: "linear-gradient(135deg, rgba(28,25,23,0.06), rgba(109,111,178,0.06))",
            border: "1px solid rgba(28,25,23,0.15)",
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16,
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flex: 1 }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, background: "rgba(28,25,23,0.1)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="#436b65" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
              </div>
              <div>
                <span style={{ fontSize: 13, fontWeight: 700, color: "#1c1917" }}>{t("page.workspace_detail.workspace_created")}</span>
                <span style={{ fontSize: 12, color: "#78716c", marginLeft: 8 }}>
                  {agentCount > 0 && <>{agentCount} {t("page.agent_dashboard.agent_singular")}{agentCount > 1 ? "s" : ""} {t("page.workspace_detail.mapped")}</>}
                  {agentCount > 0 && goalCount > 0 && " · "}
                  {goalCount > 0 && <>{goalCount} {t("page.workspace_detail.goal")}{goalCount > 1 ? "s" : ""} {t("page.workspace_detail.tracking")}</>}
                  {agentCount === 0 && goalCount === 0 && t("page.workspace_detail.set_up_agents_and_goals")}
                </span>
              </div>
            </div>
            <button
              onClick={() => { localStorage.setItem(bannerDismissKey, "1"); handleTabChange("overview"); /* force re-render */ }}
              style={{ background: "none", border: "none", cursor: "pointer", color: "#a8a29e", padding: 4, flexShrink: 0 }}
              title={t("page.workspace_detail.dismiss")}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" d="M18 6L6 18M6 6l12 12" /></svg>
            </button>
          </div>
        )}

        {/* KPI cards — counts of *this workspace's* tasks / documents / agents */}
        {stats && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 16 }}>
            <div
              style={{ ...KPI_CARD, cursor: "pointer" }}
              onClick={() => navigate(`/tasks?workspaceId=${ws.id}`)}
              title={t("page.workspace_detail.all_tasks_scheduled_in_this_workspace_click_to_v")}
            >
              <div style={{ width: 44, height: 44, borderRadius: 14, background: "#f3f6fa", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <svg style={{ width: 22, height: 22, color: "#5f84bd" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" />
                </svg>
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 24, fontWeight: 800, color: "#292524" }}>{stats.total_tasks}</div>
                <div style={LABEL}>{t("nav.tasks")}</div>
                <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 2, lineHeight: 1.3 }}>
                  {t("page.workspace_detail.work_items_in_this_workspace")}
                </div>
              </div>
            </div>
            <div
              style={{ ...KPI_CARD, cursor: "pointer" }}
              onClick={() => handleTabChange("documents")}
              title={t("page.workspace_detail.knowledge_documents_attached_to_this_workspace_c")}
            >
              <div style={{ width: 44, height: 44, borderRadius: 14, background: "#f1f6f3", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <IconDocument size={22} className="text-emerald-500" />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 24, fontWeight: 800, color: "#292524" }}>{stats.total_documents}</div>
                <div style={LABEL}>{t("page.workspace_detail.documents")}</div>
                <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 2, lineHeight: 1.3 }}>
                  {t("page.workspace_detail.knowledge_attached")}
                </div>
              </div>
            </div>
            <div
              style={{ ...KPI_CARD, cursor: "pointer" }}
              onClick={() => handleTabChange("agents")}
              title={t("page.workspace_detail.agents_subscribed_to_services_in_this_workspace")}
            >
              <div style={{ width: 44, height: 44, borderRadius: 14, background: "#f7f4fa", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <svg style={{ width: 22, height: 22, color: "#9079c2" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
                </svg>
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 24, fontWeight: 800, color: "#292524" }}>{stats.total_agents}</div>
                <div style={LABEL}>{t("nav.agents")}</div>
                <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 2, lineHeight: 1.3 }}>
                  {t("page.workspace_detail.mapped_to_services")}
                </div>
              </div>
            </div>
            {budgetStatus && (
              <div
                style={{ ...KPI_CARD, cursor: "pointer" }}
                onClick={() => handleTabChange("settings")}
                title={t("page.workspace_detail.workspace_credit_usage")}
              >
                <div style={{ width: 44, height: 44, borderRadius: 14, background: "#f5f5f4", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <svg style={{ width: 22, height: 22, color: "#57534e" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m-6-6h12M6.75 4.5h10.5a2.25 2.25 0 012.25 2.25v10.5a2.25 2.25 0 01-2.25 2.25H6.75a2.25 2.25 0 01-2.25-2.25V6.75A2.25 2.25 0 016.75 4.5z" />
                  </svg>
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 24, fontWeight: 800, color: "#292524" }}>{budgetStatus.monthly_spent_credits.toLocaleString()}</div>
                  <div style={LABEL}>{t("page.workspace_detail.credits_used")}</div>
                  <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 2, lineHeight: 1.3 }}>
                    {budgetStatus.monthly_budget_credits != null
                      ? t("page.workspace_detail.of_credit_cap", { cap: budgetStatus.monthly_budget_credits.toLocaleString() })
                      : t("page.workspace_detail.no_monthly_credit_cap")}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {renderWorkspaceEvaluationCard({ compact: true })}

        {/* Cover image */}
        {ws.cover_image_url && (
          <GlassCard hoverable={false} className="!p-0 overflow-hidden">
            <img
              src={ws.cover_image_url}
              alt={ws.name}
              style={{ width: "100%", maxHeight: 240, objectFit: "cover", display: "block" }}
            />
          </GlassCard>
        )}

        {/* Info grid */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 24 }}>
          <GlassCard hoverable={false}>
            <div style={SECTION_TITLE}>{t("page.workspace_detail.details")}</div>
            {infoRow(t("page.workspace_detail.field_name"), ws.name)}
            {infoRow(t("page.workspace_detail.field_description"), ws.description)}
            {infoRow(t("page.workspace_detail.field_category"), ws.category)}
            {infoRow(t("page.workspace_detail.field_kind"), ws.kind)}
            {infoRow(t("page.workspace_detail.field_status"), <StatusBadge type={ws.status === "active" ? "active" : "inactive"} dot>{ws.status}</StatusBadge>)}
            {infoRow(t("page.workspace_detail.field_identity_label"), ws.identity_label)}
            {infoRow(t("page.workspace_detail.field_property_type"), ws.property_type)}
            {infoRow(t("page.workspace_detail.field_occupancy_status"), ws.occupancy_status)}
            {infoRow(t("page.workspace_detail.field_created_by"), ws.created_by_name || ws.created_by_email)}
            {infoRow(t("page.workspace_detail.field_created"), formatDate(ws.created_at))}
            {infoRow(t("page.workspace_detail.field_updated"), formatDate(ws.updated_at))}
            {infoRow(t("page.workspace_detail.field_workspace_id"), ws.id)}
          </GlassCard>

          <GlassCard hoverable={false}>
            <div style={SECTION_TITLE}>{t("page.memories.context")}</div>
            {infoRow(t("page.workspace_detail.field_operating_context"), ws.operating_context)}
            {infoRow(t("page.workspace_detail.field_primary_work"), ws.primary_work)}
            {infoRow("Address", ws.address)}
            {ws.longitude != null && ws.latitude != null && (
              infoRow("Coordinates", `${ws.latitude}, ${ws.longitude}`)
            )}
            {infoRow("PMS Property ID", ws.pms_property_id)}
            {infoRow("PMS Unit ID", ws.pms_unit_id)}
            {ws.attribute_tags && ws.attribute_tags.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div style={LABEL}>{t("page.workspace_detail.attribute_tags")}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                  {ws.attribute_tags.map((tag, i) => (
                    <Chip key={i} variant="teal" size="sm">{tag}</Chip>
                  ))}
                </div>
              </div>
            )}

            {/* Heartbeat */}
            <div style={{ marginTop: 16, paddingTop: 16, borderTop: "1px solid #f5f5f4" }}>
              <div style={SECTION_TITLE}>{t("page.workspace_detail.heartbeat")}</div>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <StatusBadge
                  type={(heartbeatStatus?.enabled ?? ws.heartbeat_enabled) ? "active" : "gray"}
                  dot
                  pulse={!!(heartbeatStatus?.enabled ?? ws.heartbeat_enabled)}
                >
                  {(heartbeatStatus?.enabled ?? ws.heartbeat_enabled) ? t("page.job_logs.enabled") : t("page.job_logs.disabled")}
                </StatusBadge>
                {ws.heartbeat_cadence && (
                  <Chip variant="slate" size="sm">{_formatScheduleLabel(ws.heartbeat_cadence)}</Chip>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    if (heartbeatStatus?.enabled ?? ws.heartbeat_enabled) {
                      heartbeatDisable.mutate();
                    } else {
                      heartbeatEnable.mutate();
                    }
                  }}
                  disabled={heartbeatEnable.isPending || heartbeatDisable.isPending}
                  className="ml-auto"
                >
                  {heartbeatStatus?.enabled ?? ws.heartbeat_enabled ? t("page.workspace_detail.disable") : t("page.workspace_detail.enable")}
                </Button>
              </div>
              {ws.last_heartbeat_at && (
                <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 6 }}>
                  {t("page.workspace_detail.last_heartbeat")} {relativeTime(ws.last_heartbeat_at)}
                </div>
              )}
            </div>
          </GlassCard>
        </div>

        {/* Services + Matched Agents */}
        {(() => {
          const services = (operatingModel?.services as any[]) || [];
          const mappingsBySvc = new Map<string, any>();
          for (const m of (agentMappings as any[]) || []) {
            if (m.service_key) mappingsBySvc.set(m.service_key, m);
          }
          const agentById = new Map<string, any>();
          for (const a of (entityAgents as any[]) || []) agentById.set(a.id, a);

          if (services.length === 0) return null;
          return (
            <GlassCard hoverable={false}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <div style={SECTION_TITLE}>{t("page.workspace_detail.services_matched_agents")}</div>
                <Button variant="outline" size="sm" onClick={() => handleTabChange("agents")}>
                  {t("page.workspace_detail.manage_mappings")}
                </Button>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {services.map((svc: any, i: number) => {
                  const key = svc.service_key || svc.key || "";
                  const m = mappingsBySvc.get(key);
                  const agent = m ? agentById.get(m.agent_id) : null;
                  return (
                    <div key={key || i} style={{
                      display: "flex", alignItems: "center", gap: 14,
                      padding: "10px 12px",
                      borderRadius: 12,
                      background: "rgba(250, 250, 249, 0.6)",
                      border: "1px solid rgba(28,25,23,0.06)",
                    }}>
                      <div style={{
                        width: 32, height: 32, borderRadius: 10, background: "#f5f5f4",
                        display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                        fontSize: 13, fontWeight: 800, color: "#57534e",
                      }}>
                        {i + 1}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 14, fontWeight: 700, color: "#1c1917" }}>
                          {_serviceLabel(svc)}
                        </div>
                        {svc.description && (
                          <div style={{ fontSize: 12, color: "#78716c", marginTop: 4, lineHeight: 1.4 }}>
                            {svc.description}
                          </div>
                        )}
                      </div>
                      <div style={{ flexShrink: 0, textAlign: "right" }}>
                        {agent ? (
                          <>
                            <div style={{ fontSize: 13, fontWeight: 600, color: "#1c1917" }}>
                              {_agentLabel(agent)}
                            </div>
                            <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 2 }}>{t("page.workspace_detail.mapped_agent")}</div>
                          </>
                        ) : (
                          <StatusBadge type="warning" dot>{t("page.workspace_detail.unmapped")}</StatusBadge>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </GlassCard>
          );
        })()}

        {/* Goals quick view */}
        {(() => {
          const runtimeGoals: any[] = Array.isArray(goals) ? goals : (goals as any)?.items ?? [];
          const goalsSource = runtimeGoals.length > 0 ? runtimeGoals : ((operatingModel?.goals as any[]) || []);
          const displayGoals = _dedupeGoals(goalsSource);
          if (displayGoals.length === 0) return null;
          return (
            <GlassCard hoverable={false} className="workspace-overview-card workspace-overview-goals-summary">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <div className="workspace-overview-section-heading" style={SECTION_TITLE}>{t("nav.goals")}</div>
                <Button variant="outline" size="sm" onClick={() => handleTabChange("goals")}>
                  {t("page.workspace_detail.edit_goals")}
                </Button>
              </div>
              <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 8 }}>
                {displayGoals.slice(0, 6).map((g: any, i: number) => (
                  <li className="workspace-overview-goal-summary-row" key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                    <span className="workspace-overview-goal-bullet" style={{ color: "#57534e", fontWeight: 800, marginTop: 2 }}>•</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="workspace-overview-goal-summary-title" style={{ fontSize: 13, fontWeight: 600, color: "#1c1917" }}>
                        {_goalLabel(g)}
                      </div>
                      {g.description && _goalLabel(g) !== g.description && (
                        <div className="workspace-overview-goal-summary-copy" style={{ fontSize: 12, color: "#78716c", marginTop: 2, lineHeight: 1.4 }}>
                          {g.description}
                        </div>
                      )}
                      {(g.target || g.cadence) && (
                        <div className="workspace-overview-goal-summary-meta" style={{ fontSize: 11, color: "#a8a29e", marginTop: 2 }}>
                          {g.target && <span>{t("page.workspace_detail.target")} {g.target}</span>}
                          {g.target && g.cadence && <span>{" · "}</span>}
                          {g.cadence && <span>{t("page.workspace_detail.cadence")} {g.cadence}</span>}
                        </div>
                      )}
                    </div>
                  </li>
                ))}
                {displayGoals.length > 6 && (
                  <li className="workspace-overview-goal-summary-more" style={{ fontSize: 12, color: "#a8a29e" }}>+ {displayGoals.length - 6} {t("page.workspace_detail.more")}</li>
                )}
              </ul>
            </GlassCard>
          );
        })()}

        {/* Missing integrations — flagged by the architect during creation */}
        {(() => {
          const flagged = (((ws.settings as any)?.flagged_integrations) || []) as any[];
          if (!flagged || flagged.length === 0) return null;
          return (
            <GlassCard hoverable={false} className="workspace-overview-card workspace-overview-setup-card">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <div className="workspace-overview-setup-title" style={{ ...SECTION_TITLE, color: "#936027" }}>
                  {t("page.workspace_detail.needs_setup")} {flagged.length} {t("page.apps.integration")}{flagged.length === 1 ? "" : "s"}
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={async () => {
                      try {
                        const result = await api.workspaces.resolveIntegrations(workspaceId!);
                        if (result.resolved.length > 0) {
                          toast.success(`${t("page.workspace_detail.connected")}: ${result.resolved.join(", ")}`);
                          queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
                          queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
                        } else {
                          toast.info(t("page.workspace_detail.no_new_integrations_found_set_them_up_first"));
                        }
                      } catch {
                        toast.error(t("page.workspace_detail.failed_to_check_integrations"));
                      }
                    }}
                  >
                    {t("page.workspace_detail.check_again")}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
                    {t("page.workspace_detail.open_integrations")}
                  </Button>
                </div>
              </div>
              <p className="workspace-overview-setup-copy" style={{ fontSize: 12, color: "#76502c", margin: "0 0 12px", lineHeight: 1.5 }}>
                {t("page.workspace_detail.these_integrations_were_referenced_when_the_work")}
              </p>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 240px), 1fr))", gap: 10 }}>
                {flagged.map((f: any, i: number) => {
                  const source = String(f.source || "");
                  const providerKey = String(f.provider || "").toLowerCase();
                  const legacyChannelProviders = new Set([
                    "telegram", "slack", "discord", "whatsapp", "email", "wechat",
                    "wechat_official", "wechat_personal", "twilio", "twilio_sms",
                    "twilio_voice", "facebook", "webchat", "in_app", "inapp",
                  ]);
                  const isChannelSetup = source === "channel_setup" || (!source && legacyChannelProviders.has(providerKey));
                  return (
                    <div className="workspace-overview-setup-item" key={i} style={{
                      padding: "10px 12px",
                      borderRadius: 10,
                      background: "rgba(243, 236, 214, 0.4)",
                      border: "1px solid rgba(207, 155, 68, 0.3)",
                    }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "space-between" }}>
                        <span className="workspace-overview-setup-item-title" style={{ fontSize: 13, fontWeight: 700, color: "#1c1917" }}>
                          {String(f.provider || "").replace(/[_\-]+/g, " ").replace(/\b\w/g, (c: string) => c.toUpperCase())}
                        </span>
                        <div style={{ display: "flex", gap: 4 }}>
                          <Chip variant={isChannelSetup ? "blue" : "slate"} size="sm">
                            {isChannelSetup ? t("page.workspace_detail.channel") : t("page.workspace_detail.capability")}
                          </Chip>
                          {f.required && <Chip variant="red" size="sm">{t("page.login.required")}</Chip>}
                        </div>
                      </div>
                      {f.purpose && (
                        <div className="workspace-overview-setup-item-copy" style={{ fontSize: 11, color: "#76502c", marginTop: 4, lineHeight: 1.4 }}>
                          {f.purpose}
                        </div>
                      )}
                      {Array.isArray(f.linked_service_keys) && f.linked_service_keys.length > 0 && (
                        <div className="workspace-overview-setup-chip-row" style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
                          {f.linked_service_keys.map((sk: string) => (
                            <Chip key={sk} variant="orange" size="sm">
                              {_serviceLabelFromKey(sk, (operatingModel?.services as any[]) || [])}
                            </Chip>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </GlassCard>
          );
        })()}

        {/* Goal progress — compact inline cards */}
        {(() => {
          const goalsData: any[] = (goals as any)?.items ?? goals ?? [];
          const activeGoals = Array.isArray(goalsData) ? _dedupeGoals(goalsData).filter((g: any) => g.status === "active") : [];
          if (activeGoals.length === 0) return null;
          const paceColors: Record<string, { bg: string; fg: string }> = {
            on_track: { bg: "#e4efe8", fg: "#3d7351" },
            ahead: { bg: "#e3e9f1", fg: "#3f57a0" },
            achieved: { bg: "#dceae3", fg: "#065f46" },
            behind: { bg: "#f3ecd6", fg: "#76502c" },
            at_risk: { bg: "#f1dddb", fg: "#c14a44" },
            unknown: { bg: "#f5f5f4", fg: "#78716c" },
          };
          return (
            <GlassCard hoverable={false} className="workspace-overview-card workspace-overview-goals-progress">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <div className="workspace-overview-section-heading" style={SECTION_TITLE}>{t("nav.goals")}</div>
                <Button variant="outline" size="sm" onClick={() => handleTabChange("goals")}>
                  {t("page.workspace_detail.view_all")}
                </Button>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {activeGoals.slice(0, 5).map((g: any) => {
                  const pct = _goalProgressPercent(g);
                  const pace = g.pace_status || "unknown";
                  const pc = paceColors[pace] || paceColors.unknown;
                  return (
                    <div className="workspace-overview-goal-row" key={g.id} style={{
                      display: "flex", alignItems: "center", gap: 12,
                      padding: "10px 12px", borderRadius: 12,
                      background: "rgba(250,250,249,0.6)", border: "1px solid rgba(28,25,23,0.06)",
                    }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="workspace-overview-goal-title" style={{ fontSize: 13, fontWeight: 700, color: "#1c1917", marginBottom: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {g.title}
                        </div>
                        <div className="workspace-overview-goal-track" style={{ height: 4, borderRadius: 2, background: "#e7e5e4", overflow: "hidden" }}>
                          <div className="workspace-overview-goal-fill" style={{
                            height: "100%", borderRadius: 2,
                            background: pace === "at_risk" ? "#d65f59" : pace === "behind" ? "#cf9b44" : "#5f928a",
                            width: `${pct}%`, transition: "width 0.5s",
                          }} />
                        </div>
                      </div>
                      <div style={{ textAlign: "right", flexShrink: 0 }}>
                        <div className="workspace-overview-goal-percent" style={{ fontSize: 14, fontWeight: 800, color: "#1c1917" }}>{Math.round(pct)}%</div>
                        <span className="workspace-overview-goal-status" style={{
                          fontSize: 9, fontWeight: 700, padding: "2px 6px", borderRadius: 4,
                          background: pc.bg, color: pc.fg,
                        }}>
                          {pace.replace(/_/g, " ")}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </GlassCard>
          );
        })()}

        {/* Tasks by status */}
        {stats && stats.tasks_by_status && Object.keys(stats.tasks_by_status).length > 0 && (
          <GlassCard hoverable={false}>
            <div style={SECTION_TITLE}>{t("page.workspace_detail.tasks_by_status")}</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {Object.entries(stats.tasks_by_status).map(([status, count]) => (
                <Chip key={status} variant={_taskStatusChipVariant(status)} size="md">
                  <strong style={{ marginRight: 6 }}>{count as number}</strong>
                  <span style={{ textTransform: "capitalize" }}>{status.replace(/_/g, " ")}</span>
                </Chip>
              ))}
            </div>
          </GlassCard>
        )}
      </div>
    );
  }

  function renderStaff() {
    const items = staffList || [];
    const accessMode = ((workspace?.settings as any)?.access_mode || "members_only") as string;
    const accessCopy = accessMode === "entity_visible"
      ? {
        title: "Everyone in this organization",
        body: "Any member of this Manor organization can find and open this workspace. Only assigned members can manage it.",
      }
      : {
        title: "Only invited members",
        body: "Only people listed below, plus organization owners/admins, can open this workspace.",
      };
    const staffById = new Map((entityStaff || []).map((s: any) => [s.id, s]));
    const setWorkspaceAccessMode = (mode: "members_only" | "entity_visible") => {
      const nextSettings = { ...((workspace?.settings || {}) as Record<string, any>), access_mode: mode };
      updateWorkspace.mutate({ settings: nextSettings } as Partial<Workspace>);
    };
    const accessOptions: Array<{
      value: "members_only" | "entity_visible";
      title: string;
      body: string;
    }> = [
      {
        value: "members_only",
        title: "Only invited members",
        body: "Best for client work, private projects, and workspace-specific files.",
      },
      {
        value: "entity_visible",
        title: "Everyone in organization",
        body: "Useful for open team rooms. Anyone in this Manor organization can see it.",
      },
    ];
    const roleOptions = [
      { value: "owner", label: "Owner" },
      { value: "editor", label: "Editor" },
      { value: "contributor", label: "Contributor" },
      { value: "viewer", label: "Viewer" },
      { value: "external_client", label: "External client" },
    ];
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ minWidth: 240, flex: 1 }}>
              <div style={SECTION_TITLE}>Workspace access</div>
              <div style={{ fontSize: 16, fontWeight: 800, color: "#1c1917", marginTop: 6 }}>
                {accessCopy.title}
              </div>
              <p style={{ margin: "6px 0 0", color: "#78716c", fontSize: 13, lineHeight: 1.5 }}>
                {accessCopy.body}
              </p>
            </div>
            {canManageWs && (
              <Button variant="outline" size="sm" onClick={() => setShowWorkspaceShareModal(true)}>
                Share workspace
              </Button>
            )}
          </div>
        </GlassCard>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={SECTION_TITLE}>{t("page.workspace_detail.staff_assignments")}</div>
          {canManageWs && (
            <Button variant="primary" size="sm" onClick={() => setShowStaffModal(true)}>
              {t("page.workspace_detail.add_staff")}
            </Button>
          )}
        </div>

        {items.length === 0 ? (
          <EmptyState title={t("page.workspace_detail.no_staff_assigned")} description={t("page.workspace_detail.assign_staff_members_to_this_workspace")} />
        ) : (
          <GlassCard hoverable={false} className="!p-0 overflow-hidden">
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: "#fafbfc" }}>
                  <th style={TABLE_HEADER}>{t("page.workspace_detail.staff")}</th>
                  <th style={TABLE_HEADER}>{t("page.users.role")}</th>
                  <th style={TABLE_HEADER}>Status</th>
                  <th style={TABLE_HEADER}>Expires</th>
                  <th style={TABLE_HEADER}>{t("page.workspace_detail.assigned")}</th>
                  {canManageWs && <th style={{ ...TABLE_HEADER, width: 80 }}>{t("page.custom_fields.actions")}</th>}
                </tr>
              </thead>
              <tbody>
                {items.map((s: WorkspaceStaff) => {
                  const matched = (entityStaff || []).find((es: any) => es.id === s.staff_id);
                  const expired = !!s.expires_at && new Date(s.expires_at) < new Date();
                  return (
                  <tr key={s.id}>
                    <td style={TABLE_CELL}>{matched ? _staffLabel(matched) : (s.staff_id || s.user_id || "—")}</td>
                    <td style={TABLE_CELL}>{s.role || "—"}</td>
                    <td style={TABLE_CELL}>
                      <span style={{
                        fontSize: 11, color: expired ? "#a23e38" : (s.status === "active" ? "#3f7361" : "#a8a29e"),
                        fontWeight: 600,
                      }}>
                        {expired ? "expired" : (s.status || "active")}
                      </span>
                    </td>
                    <td style={TABLE_CELL}>
                      {s.expires_at
                        ? <span style={{ color: expired ? "#a23e38" : undefined }}>{new Date(s.expires_at).toLocaleDateString()}</span>
                        : <span style={{ color: "#d6d3d1" }}>permanent</span>}
                    </td>
                    <td style={TABLE_CELL}>{formatDate(s.added_at || s.created_at)}</td>
                    {canManageWs && (
                      <td style={TABLE_CELL}>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setConfirmRemoveStaff(s.staff_id || s.user_id || "")}
                        >
                          <span style={{ color: "#ef4444", fontWeight: 700 }}>{t("page.task_detail.runtime.remove_rule")}</span>
                        </Button>
                      </td>
                    )}
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </GlassCard>
        )}

        <Modal
          open={canManageWs && showWorkspaceShareModal}
          onClose={() => { setShowWorkspaceShareModal(false); setPickedStaff(null); }}
          title="Share workspace"
          footer={
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <Button variant="outline" onClick={() => { setShowWorkspaceShareModal(false); setPickedStaff(null); }}>
                Cancel
              </Button>
              <Button variant="primary" onClick={() => { setShowWorkspaceShareModal(false); setPickedStaff(null); }}>
                Done
              </Button>
            </div>
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div>
                <div style={LABEL}>Add people</div>
                <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.45 }}>
                  Pick teammates or agents from this organization. They will be added to this workspace only.
                </div>
              </div>

              {pickedStaff ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <StaffPickedChip
                    staff={pickedStaff}
                    onClear={() => {
                      setPickedStaff(null);
                      setStaffForm({ ...staffForm, staff_id: "" });
                    }}
                  />
                  <div style={{ display: "grid", gridTemplateColumns: "minmax(140px, 1fr) auto", gap: 8, alignItems: "center" }}>
                    <Select
                      value={staffForm.role || "viewer"}
                      onChange={(v) => setStaffForm({ ...staffForm, role: v })}
                      options={roleOptions}
                    />
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={!staffForm.staff_id || assignStaff.isPending}
                      loading={assignStaff.isPending}
                      onClick={() => assignStaff.mutate({
                        staff_id: staffForm.staff_id,
                        role: staffForm.role || "viewer",
                        expires_at: staffForm.expires_at || undefined,
                      })}
                    >
                      Add
                    </Button>
                  </div>
                </div>
              ) : (
                <PeoplePicker
                  allowExternalEmail={false}
                  excludeStaffIds={(staffList || [])
                    .map((s) => s.staff_id)
                    .filter((x): x is string => !!x)}
                  onPick={(pick) => {
                    if (pick.kind !== "staff") return;
                    setPickedStaff(pick.staff);
                    setStaffForm({ staff_id: pick.staff.id, role: "viewer", expires_at: "" });
                  }}
                />
              )}
            </div>

            <div>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-end", marginBottom: 8 }}>
                <div>
                  <div style={LABEL}>People with access</div>
                  <div style={{ fontSize: 12, color: "#78716c" }}>
                    These people can open this workspace even when general access is restricted.
                  </div>
                </div>
                <span style={{ fontSize: 11, fontWeight: 700, color: "#a8a29e" }}>
                  {items.length} {items.length === 1 ? "member" : "members"}
                </span>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {items.length === 0 ? (
                  <div style={{ fontSize: 12, color: "#a8a29e", padding: "12px 4px", borderTop: "1px solid rgba(28,25,23,0.06)" }}>
                    No invited members yet.
                  </div>
                ) : items.map((s: WorkspaceStaff) => {
                  const matched = staffById.get(s.staff_id || "");
                  const displayName = matched ? _staffLabel(matched) : (s.staff_id || s.user_id || "Unknown member");
                  const email = matched?.email || s.user_id || "Workspace member";
                  const expired = !!s.expires_at && new Date(s.expires_at) < new Date();
                  return (
                    <div
                      key={s.id}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "32px 1fr 150px 28px",
                        alignItems: "center",
                        gap: 10,
                        padding: "8px 6px",
                        borderRadius: 6,
                        opacity: removeStaff.isPending || assignStaff.isPending ? 0.72 : 1,
                      }}
                    >
                      <UserAvatar name={displayName} avatarUrl={matched?.avatar_url} size={32} />
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, color: "#292524", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {displayName}
                          {expired && <span style={{ color: "#a23e38", marginLeft: 6, fontSize: 10, fontWeight: 500 }}>expired</span>}
                        </div>
                        <div style={{ fontSize: 11, color: "#78716c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {email}
                          {s.expires_at && !expired && ` · Expires ${new Date(s.expires_at).toLocaleDateString()}`}
                        </div>
                      </div>
                      <Select
                        value={s.role || "viewer"}
                        onChange={(role) => assignStaff.mutate({
                          staff_id: s.staff_id || "",
                          role,
                          expires_at: s.expires_at || undefined,
                        })}
                        options={roleOptions}
                        disabled={!s.staff_id || assignStaff.isPending}
                      />
                      <button
                        type="button"
                        disabled={!s.staff_id || removeStaff.isPending}
                        onClick={() => s.staff_id && removeStaff.mutate(s.staff_id)}
                        aria-label="Remove access"
                        title="Remove access"
                        style={{
                          width: 26,
                          height: 26,
                          borderRadius: 6,
                          border: "none",
                          background: "transparent",
                          color: "#a8a29e",
                          cursor: !s.staff_id || removeStaff.isPending ? "not-allowed" : "pointer",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          fontSize: 18,
                          lineHeight: 1,
                        }}
                      >
                        ×
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>

            <div>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", marginBottom: 8 }}>
                <div>
                  <div style={LABEL}>General access</div>
                  <div style={{ fontSize: 12, color: "#78716c" }}>
                    Controls whether this workspace can be discovered by the whole organization.
                  </div>
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {accessOptions.map((opt) => {
                  const selected = accessMode === opt.value;
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => setWorkspaceAccessMode(opt.value)}
                      disabled={updateWorkspace.isPending}
                      style={{
                        textAlign: "left",
                        width: "100%",
                        borderRadius: 12,
                        border: selected ? "1px solid rgba(79,125,117,0.36)" : "1px solid rgba(28,25,23,0.08)",
                        background: selected ? "rgba(79,125,117,0.08)" : "#fff",
                        padding: "12px 14px",
                        cursor: updateWorkspace.isPending ? "wait" : "pointer",
                        display: "grid",
                        gridTemplateColumns: "18px 1fr",
                        gap: 12,
                        alignItems: "flex-start",
                      }}
                    >
                      <span
                        style={{
                          width: 18,
                          height: 18,
                          borderRadius: 999,
                          border: selected ? "5px solid #4f7d75" : "1px solid #d6d3d1",
                          background: "#fff",
                          marginTop: 1,
                        }}
                      />
                      <span>
                        <span style={{ display: "block", fontSize: 13, fontWeight: 700, color: "#292524" }}>
                          {opt.title}
                        </span>
                        <span style={{ display: "block", fontSize: 12, color: "#78716c", lineHeight: 1.45, marginTop: 3 }}>
                          {opt.body}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </Modal>

        <Modal
          open={canManageWs && showStaffModal}
          onClose={() => { setShowStaffModal(false); setPickedStaff(null); }}
          title={t("page.workspace_detail.assign_staff")}
          footer={
            <>
              <Button variant="outline" onClick={() => { setShowStaffModal(false); setPickedStaff(null); }}>
                {t("action.cancel")}
              </Button>
              <Button
                variant="primary"
                disabled={!staffForm.staff_id || assignStaff.isPending}
                loading={assignStaff.isPending}
                onClick={() => assignStaff.mutate({
                  staff_id: staffForm.staff_id,
                  role: staffForm.role || undefined,
                  expires_at: staffForm.expires_at || undefined,
                })}
              >
                {assignStaff.isPending ? t("page.workspace_detail.assigning") : t("page.workspace_detail.assign")}
              </Button>
            </>
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
                {t("page.workspace_detail.staff_member")}
              </div>
              {pickedStaff ? (
                <StaffPickedChip
                  staff={pickedStaff}
                  onClear={() => {
                    setPickedStaff(null);
                    setStaffForm({ ...staffForm, staff_id: "" });
                  }}
                />
              ) : (
                <PeoplePicker
                  allowExternalEmail={false}
                  excludeStaffIds={(staffList || [])
                    .map((s) => s.staff_id)
                    .filter((x): x is string => !!x)}
                  onPick={(pick) => {
                    if (pick.kind !== "staff") return;
                    setPickedStaff(pick.staff);
                    setStaffForm({ ...staffForm, staff_id: pick.staff.id });
                  }}
                />
              )}
              {entityStaff && entityStaff.length === 0 && (
                <div style={{ fontSize: 12, color: "#a8a29e", marginTop: 6 }}>
                  {t("page.workspace_detail.no_staff_in_this_entity_yet_invite_one_from_the")}
                </div>
              )}
            </div>
            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
                {t("page.workspace_detail.workspace_role")}
              </div>
              <Select
                value={staffForm.role}
                onChange={(v) => setStaffForm({ ...staffForm, role: v })}
                placeholder={t("page.workspace_detail.workspace_role_placeholder")}
                options={[
                  { value: "owner", label: t("page.workspace_detail.role.owner") },
                  { value: "editor", label: t("page.workspace_detail.role.editor") },
                  { value: "contributor", label: t("page.workspace_detail.role.contributor") },
                  { value: "viewer", label: t("page.workspace_detail.role.viewer") },
                  { value: "external_client", label: t("page.workspace_detail.role.external_client") },
                ]}
              />
              <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 4 }}>
                {t("page.workspace_detail.workspace_role_hint")}
              </div>
            </div>

            <Input
              label={t("page.workspace_detail.expires_optional") || "Expires (optional)"}
              type="date"
              value={staffForm.expires_at}
              onChange={(e) => setStaffForm({ ...staffForm, expires_at: e.target.value })}
              placeholder={t("page.workspace_detail.expires_permanent_placeholder")}
            />
          </div>
        </Modal>
      </div>
    );
  }

  function renderAgents() {
    const items = agentMappings || [];

    // Build the list of services that still need an agent. Each entry
    // either points to an AI-recommended agent (preferred) or carries
    // just the service so autoMapAgents can create a custom agent for
    // it. The end-result is the same from the user's perspective:
    // "Auto-map" leaves no service unmapped.
    const services: any[] = (operatingModel?.services as any[]) || [];
    const serviceByKey = new Map<string, any>();
    for (const svc of services) {
      const key = svc.service_key || svc.key || "";
      if (key) serviceByKey.set(key, svc);
    }
    const aiSuggestionsByKey = new Map<string, any>();
    for (const s of (operatingModel?.agent_mappings as any[]) || []) {
      if (s.service_key) aiSuggestionsByKey.set(s.service_key, s);
    }
    const existingByKey = new Map<string, any>();
    for (const m of items as any[]) {
      if (m.service_key) existingByKey.set(m.service_key, m);
    }
    const pendingSuggestions = services
      .map((svc: any) => {
        const sk = svc.service_key || svc.key;
        if (!sk || existingByKey.has(sk)) return null;
        const ai = aiSuggestionsByKey.get(sk) || {};
        return {
          service_key: sk,
          recommended_agent_id: ai.recommended_agent_id || ai.agent_id || null,
          recommended_agent_name: ai.recommended_agent_name || null,
        };
      })
      .filter(Boolean) as any[];
    const closeAgentModal = () => {
      setShowAgentModal(false);
      setAgentConnectionResult(null);
      setAgentForm(DEFAULT_AGENT_FORM);
    };
    const submitAgentDisabled =
      !agentForm.service_key ||
      (agentForm.source === "hosted" && !agentForm.agent_id) ||
      (agentForm.source === "https" && !agentForm.https_url.trim()) ||
      mapAgent.isPending;

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <div style={SECTION_TITLE}>{t("page.workspace_detail.agent_service_mappings")}</div>
          <div style={{ display: "flex", gap: 8 }}>
          {pendingSuggestions.length > 0 && (() => {
            const withRec = pendingSuggestions.filter((s) => s.recommended_agent_id).length;
            const toCreate = pendingSuggestions.length - withRec;
            const label =
              withRec > 0 && toCreate > 0
                ? `Auto-map ${withRec} & create ${toCreate}`
                : withRec > 0
                  ? `Auto-map ${withRec} suggested`
                  : `Auto-create ${toCreate} agent${toCreate === 1 ? "" : "s"}`;
            return (
              <Button
                variant="outline"
                size="sm"
                onClick={() => autoMapAgents.mutate(pendingSuggestions)}
                loading={autoMapAgents.isPending}
              >
                {label}
              </Button>
            );
          })()}
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              setAgentForm(DEFAULT_AGENT_FORM);
              setAgentConnectionResult(null);
              setShowAgentModal(true);
            }}
          >
            Add agent
          </Button>
          </div>
        </div>

        {items.length === 0 ? (
          <EmptyState
            title={t("page.workspace_detail.no_agents_mapped")}
            description={
              pendingSuggestions.length > 0
                ? (() => {
                    const withRec = pendingSuggestions.filter((s) => s.recommended_agent_id).length;
                    const toCreate = pendingSuggestions.length - withRec;
                    const parts: string[] = [];
                    if (withRec > 0) parts.push(`${withRec} suggested by the setup wizard`);
                    if (toCreate > 0) parts.push(`${toCreate} need a custom agent`);
                    return `${pendingSuggestions.length} service${pendingSuggestions.length === 1 ? "" : "s"} waiting to be mapped — ${parts.join(", ")}.`;
                  })()
                : t("page.workspace_detail.map_agents_to_services_in_this_workspace")
            }
            action={
              pendingSuggestions.length > 0 ? (
                <Button
                  variant="primary"
                  onClick={() => autoMapAgents.mutate(pendingSuggestions)}
                  loading={autoMapAgents.isPending}
                >
                  {t("page.workspace_detail.auto_map_create")} {pendingSuggestions.length}
                </Button>
              ) : (
                <Button
                  variant="primary"
                  onClick={() => {
                    setAgentForm(DEFAULT_AGENT_FORM);
                    setAgentConnectionResult(null);
                    setShowAgentModal(true);
                  }}
                >
                  {t("page.workspace_detail.map_first_agent")}
                </Button>
              )
            }
          />
        ) : (
          <GlassCard hoverable={false} className="!p-0 overflow-hidden">
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: "#fafbfc" }}>
                  <th style={TABLE_HEADER}>{t("page.workspace_detail.service")}</th>
                  <th style={TABLE_HEADER}>{t("page.workspace_detail.agent")}</th>
                  <th style={TABLE_HEADER}>Source</th>
                  <th style={TABLE_HEADER}>{t("page.workspace_detail.custom_prompt")}</th>
                  <th style={{ ...TABLE_HEADER, width: 144 }}>{t("page.custom_fields.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((m: any, i: number) => {
                  const matched = (entityAgents || []).find((a: any) => a.id === m.agent_id);
                  const runtimeSource = String(matched?.config?.runtime_connection?.source || "manor_hosted");
                  const sourceLabel =
                    runtimeSource === "cli"
                      ? "CLI"
                      : runtimeSource === "https"
                        ? "HTTPS"
                        : "Manor Hosted";
                  const promptText = String(m.custom_prompt || "").trim();
                  return (
                  <tr key={m.service_key || i}>
                    <td style={TABLE_CELL}>
                      {m.service_key ? _serviceLabel(serviceByKey.get(m.service_key) || { service_key: m.service_key }) : "--"}
                    </td>
                    <td style={TABLE_CELL}>{matched ? _agentLabel(matched) : (m.agent_id || "--")}</td>
                    <td style={TABLE_CELL}>
                      <StatusBadge type={runtimeSource === "manor_hosted" ? "success" : "info"}>
                        {sourceLabel}
                      </StatusBadge>
                    </td>
                    <td style={{ ...TABLE_CELL, maxWidth: 260 }}>
                      {promptText ? (
                        <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
                          <span style={{
                            alignSelf: "flex-start",
                            padding: "3px 7px",
                            borderRadius: 999,
                            background: "rgba(95,146,138,0.1)",
                            color: "#57534e",
                            fontSize: 10,
                            fontWeight: 800,
                            textTransform: "uppercase",
                            letterSpacing: "0.05em",
                          }}>
                            Workspace override
                          </span>
                          <span style={{ color: "#78716c", fontSize: 12, lineHeight: 1.45 }}>
                            Service-specific instructions configured.
                          </span>
                        </div>
                      ) : (
                        <span style={{ color: "#a8a29e" }}>Default agent instructions</span>
                      )}
                    </td>
                    <td style={TABLE_CELL}>
                      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                        {matched && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => navigate(`/agents?edit=${matched.id}`)}
                          >
                            {t("page.workspace_detail.edit_agent")}
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setConfirmUnmapAgent(m.service_key)}
                        >
                          <span style={{ color: "#d65f59", fontWeight: 700 }}>{t("page.workspace_detail.unmap")}</span>
                        </Button>
                      </div>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </GlassCard>
        )}

        <Modal
          open={showAgentModal}
          onClose={closeAgentModal}
          title="Add agent to workspace"
          footer={
            agentConnectionResult ? (
              <Button variant="primary" onClick={closeAgentModal}>Done</Button>
            ) : (
              <>
                <Button variant="outline" onClick={closeAgentModal}>{t("action.cancel")}</Button>
                <Button
                  variant="primary"
                  disabled={submitAgentDisabled}
                  loading={mapAgent.isPending}
                  onClick={() => mapAgent.mutate(agentForm)}
                >
                  {mapAgent.isPending ? "Adding..." : "Add agent"}
                </Button>
              </>
            )
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {agentConnectionResult ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <div style={{ border: "1px solid rgba(79,125,117,0.22)", borderRadius: 14, background: "#f5f5f4", padding: 14 }}>
                  <div style={{ fontSize: 13, fontWeight: 800, color: "#57534e" }}>
                    {agentConnectionResult.agentName} is connected to this workspace.
                  </div>
                    <>
                      <div style={{ fontSize: 12, color: "#57534e", marginTop: 6 }}>
                        Use this connection secret in the HTTPS agent once. Manor only stores its hash.
                      </div>
                      <pre style={{ margin: "10px 0 0", padding: 12, borderRadius: 10, background: "#fff", color: "#44403c", whiteSpace: "pre-wrap", wordBreak: "break-all", fontSize: 12 }}>
                        {agentConnectionResult.registration.worker_secret}
                      </pre>
                      <div style={{ fontSize: 11, color: "#78716c" }}>
                        Endpoint: {agentConnectionResult.endpoint}
                      </div>
                    </>
                </div>
              </div>
            ) : null}

            {!agentConnectionResult && (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 190px), 1fr))", gap: 10 }}>
                  {visibleAgentSourceOptions.map((source) => {
                    const selected = agentForm.source === source.key;
                    return (
                      <button
                        key={source.key}
                        type="button"
                        onClick={() => setAgentForm({ ...agentForm, source: source.key, agent_id: source.key === "hosted" ? agentForm.agent_id : "" })}
                        style={{
                          textAlign: "left",
                          border: selected ? "1px solid rgba(79,125,117,0.45)" : "1px solid rgba(231,229,228,0.9)",
                          background: selected ? "#f5f5f4" : "#fff",
                          borderRadius: 14,
                          padding: 14,
                          cursor: "pointer",
                          minHeight: 104,
                        }}
                      >
                        <div style={{ fontSize: 13, fontWeight: 800, color: selected ? "#1c1917" : "#44403c" }}>{source.title}</div>
                        <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.45, marginTop: 6 }}>{source.body}</div>
                      </button>
                    );
                  })}
                </div>

            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
                {t("page.workspace_detail.service")}
              </div>
              <Select
                value={agentForm.service_key}
                onChange={(v) => setAgentForm({ ...agentForm, service_key: v })}
                placeholder={
                  operatingModel
                    ? t("page.workspace_detail.select_a_service_from_the_operating_model")
                    : t("page.workspace_detail.loading_services")
                }
                filterable
                options={(((operatingModel as any)?.services as any[]) || []).map((svc: any) => ({
                  value: svc.service_key || svc.key || "",
                  label: _serviceLabel(svc),
                }))}
              />
              {operatingModel && (((operatingModel as any).services as any[]) || []).length === 0 && (
                <div style={{ fontSize: 12, color: "#a8a29e", marginTop: 6 }}>
                  {t("page.workspace_detail.no_services_defined_in_this_workspace_s_operatin")}
                </div>
              )}
            </div>

            {agentForm.source === "hosted" ? (
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
                  Manor hosted agent
                </div>
                <Select
                  value={agentForm.agent_id}
                  onChange={(v) => setAgentForm({ ...agentForm, agent_id: v })}
                  placeholder={entityAgents ? t("page.workspace_detail.select_an_agent") : t("page.workspace_detail.loading_agents")}
                  filterable
                  options={(entityAgents || []).map((a: any) => ({
                    value: a.id,
                    label: _agentLabel(a),
                  }))}
                />
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))", gap: 12 }}>
                <Input
                  label="Agent name"
                  value={agentForm.agent_name}
                  onChange={(e) => setAgentForm({ ...agentForm, agent_name: e.target.value })}
                  placeholder="Example: Leasing Follow-up Agent"
                />
                <Input
                  label={agentForm.source === "https" ? "HTTPS connection name" : "CLI connector name"}
                  value={agentForm.runtime_display_name}
                  onChange={(e) => setAgentForm({ ...agentForm, runtime_display_name: e.target.value })}
                  placeholder={agentForm.source === "https" ? "Example: Hermes endpoint" : "Example: Leasing MacBook"}
                />
              </div>
            )}

            {agentForm.source === "https" && (
              <Input
                label="HTTPS endpoint"
                value={agentForm.https_url}
                onChange={(e) => setAgentForm({ ...agentForm, https_url: e.target.value })}
                placeholder="https://agent.example.com/manor"
              />
            )}

            {agentForm.source !== "hosted" && (
              <>
                <Input
                  label="Description"
                  value={agentForm.description}
                  onChange={(e) => setAgentForm({ ...agentForm, description: e.target.value })}
                  placeholder="What this agent owns in the workspace"
                />
                <Textarea
                  label="System prompt"
                  rows={4}
                  value={agentForm.system_prompt}
                  onChange={(e) => setAgentForm({ ...agentForm, system_prompt: e.target.value })}
                  placeholder="Leave blank to generate a focused workspace prompt."
                />
              </>
            )}

            <Textarea
              label={t("page.workspace_detail.custom_prompt_optional")}
              rows={3}
              value={agentForm.custom_prompt}
              onChange={(e) => setAgentForm({ ...agentForm, custom_prompt: e.target.value })}
              placeholder={t("page.workspace_detail.override_agent_prompt_for_this_workspace")}
            />
              </>
            )}
          </div>
        </Modal>
      </div>
    );
  }

  function renderCapabilities() {
    const services: any[] = (workspaceCapabilities as any)?.services || [];
    const workspaceRuntimeTools: any[] = (workspaceCapabilities as any)?.workspace_runtime_tools || [];
    const workspaceContextualTools: any[] = (workspaceCapabilities as any)?.workspace_contextual_tools || [];
    const workspaceMissing: any[] = (workspaceCapabilities as any)?.workspace_missing_integrations || [];
    const operatingServices = ((operatingModel?.services as any[]) || []);
    const serviceByKey = new Map<string, any>();
    for (const svc of operatingServices) {
      const key = svc.service_key || svc.key || "";
      if (key) serviceByKey.set(key, svc);
    }
    const integrationStatusByKey = new Map<string, any>();
    for (const status of (capabilityIntegrationStatus as any[]) || []) {
      if (status.server_key) integrationStatusByKey.set(status.server_key, status);
    }
    const hasAnyCapabilities = services.some((svc) =>
      (svc.tools || []).length || (svc.skills || []).length || (svc.integrations || []).length || (svc.missing_integrations || []).length,
    ) || workspaceMissing.length > 0 || workspaceRuntimeTools.length > 0 || workspaceContextualTools.length > 0;

    const renderSmallList = (items: any[], empty: string, labelFn: (item: any) => React.ReactNode) => (
      items.length === 0 ? (
        <div style={{ fontSize: 12, color: "#a8a29e" }}>{empty}</div>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {items.map((item: any, idx: number) => (
            <Chip key={item.id || item.slug || item.name || item.server_key || idx} variant="slate" size="sm">
              {labelFn(item)}
            </Chip>
          ))}
        </div>
      )
    );

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <div>
            <div style={SECTION_TITLE}>{t("page.workspace_detail.tool_integrations_capabilities")}</div>
            <p style={{ margin: "-8px 0 0", color: "#78716c", fontSize: 13 }}>
              {t("page.workspace_detail.these_are_execution_capabilities_for_workspace_a")}
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
              {t("page.workspace_detail.open_integrations_2")}
            </Button>
            <Button variant="outline" size="sm" onClick={() => handleTabChange("agents")}>
              {t("page.workspace_detail.manage_agents")}
            </Button>
          </div>
        </div>

        {!hasAnyCapabilities ? (
          <EmptyState
            title={t("page.workspace_detail.no_tool_integrations_configured")}
            description={t("page.workspace_detail.map_agents_to_workspace_services_then_bind_tools")}
            action={
              <Button variant="primary" onClick={() => handleTabChange("agents")}>
                {t("page.workspace_detail.manage_agents")}
              </Button>
            }
          />
        ) : (
          <>
            {(workspaceRuntimeTools.length > 0 || workspaceContextualTools.length > 0) && (
              <GlassCard hoverable={false}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 12 }}>
                  <div>
                    <div style={{ fontSize: 14, fontWeight: 800, color: "#1c1917" }}>
                      {t("page.workspace_detail.workspace_runtime_tools")}
                    </div>
                    <p style={{ margin: "5px 0 0", fontSize: 12, color: "#78716c", lineHeight: 1.5 }}>
                      {t("page.workspace_detail.workspace_runtime_tools_desc")}
                    </p>
                  </div>
                  <Chip variant="teal" size="sm">{t("page.workspace_detail.built_in")}</Chip>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 14 }}>
                  <div>
                    <div style={LABEL}>{t("page.workspace_detail.always_loaded")}</div>
                    {renderSmallList(workspaceRuntimeTools, t("page.workspace_detail.no_runtime_tools"), (tool) => _friendlyCodeLabel(tool.display_name || tool.name))}
                  </div>
                  <div>
                    <div style={LABEL}>{t("page.workspace_detail.contextual_tools")}</div>
                    {renderSmallList(workspaceContextualTools, t("page.workspace_detail.no_contextual_tools"), (tool) => _friendlyCodeLabel(tool.display_name || tool.name))}
                  </div>
                </div>
              </GlassCard>
            )}

            {workspaceMissing.length > 0 && (
              <GlassCard hoverable={false}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 800, color: "#936027", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                      {t("page.workspace_detail.workspace_wide_integrations_needed")}
                    </div>
                    <p style={{ margin: "6px 0 0", fontSize: 12, color: "#76502c" }}>
                      {t("page.workspace_detail.these_are_capability_integrations_referenced_by")}
                    </p>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
                    {t("page.apps.connect")}
                  </Button>
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
                  {workspaceMissing.map((flag: any, idx: number) => (
                    <Chip key={`${flag.provider || "integration"}-${idx}`} variant="orange" size="sm">
                      {String(flag.provider || "integration").replace(/[_\-]+/g, " ")}
                    </Chip>
                  ))}
                </div>
              </GlassCard>
            )}

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 360px), 1fr))", gap: 16 }}>
              {services.map((svc: any, idx: number) => {
                const serviceKey = svc.service_key || "";
                const service = serviceByKey.get(serviceKey);
                const agent = svc.agent || null;
                const tools = svc.tools || [];
                const skills = svc.skills || [];
                const integrations = svc.integrations || [];
                const missing = svc.missing_integrations || [];
                const toolProfile = (service?.tool_profile || svc.config?.tool_profile || {}) as any;
                const alwaysTools = Array.isArray(toolProfile.always) ? toolProfile.always : [];
                const contextualTools = Array.isArray(toolProfile.contextual) ? toolProfile.contextual : [];
                const inheritedToolCount = alwaysTools.length + contextualTools.length;

                return (
                  <GlassCard key={svc.agent_subscription_id || serviceKey || idx} hoverable={false}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", marginBottom: 12 }}>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 15, fontWeight: 800, color: "#1c1917", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {_serviceLabel(service || { service_key: serviceKey })}
                        </div>
                      </div>
                      {agent ? (
                        <Button variant="outline" size="sm" onClick={() => navigate(`/agents?edit=${agent.id}`)}>
                          {t("page.workspace_detail.edit_agent")}
                        </Button>
                      ) : (
                        <StatusBadge type="warning" dot>{t("page.workspace_detail.no_agent")}</StatusBadge>
                      )}
                    </div>

                    {agent && (
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                        <AgentAvatar
                          name={agent.name}
                          avatarUrl={agent.avatar_url}
                          seed={agent.id}
                          size={24}
                          shape="rounded"
                        />
                        <span style={{ fontSize: 13, fontWeight: 700, color: "#44403c" }}>{agent.name}</span>
                      </div>
                    )}

                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 8, marginBottom: 14 }}>
                      <div style={{ padding: "8px 10px", borderRadius: 10, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)" }}>
                        <div style={LABEL}>{t("page.workspace_detail.tools")}</div>
                        <div style={{ fontSize: 16, fontWeight: 800, color: "#1c1917" }}>{tools.length + inheritedToolCount}</div>
                      </div>
                      <div style={{ padding: "8px 10px", borderRadius: 10, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)" }}>
                        <div style={LABEL}>{t("nav.skills")}</div>
                        <div style={{ fontSize: 16, fontWeight: 800, color: "#1c1917" }}>{skills.length}</div>
                      </div>
                      <div style={{ padding: "8px 10px", borderRadius: 10, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)" }}>
                        <div style={LABEL}>{t("nav.integrations")}</div>
                        <div style={{ fontSize: 16, fontWeight: 800, color: "#1c1917" }}>{integrations.length}</div>
                      </div>
                    </div>

                    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                      {inheritedToolCount > 0 && (
                        <div>
                          <div style={LABEL}>{t("page.workspace_detail.service_tool_profile")}</div>
                          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                            {alwaysTools.length > 0 && (
                              <div>
                                <div style={{ fontSize: 11, color: "#78716c", fontWeight: 800, marginBottom: 5 }}>
                                  {t("page.workspace_detail.always_loaded")}
                                </div>
                                {renderSmallList(alwaysTools, t("page.workspace_detail.no_runtime_tools"), (tool) => _friendlyCodeLabel(String(tool)))}
                              </div>
                            )}
                            {contextualTools.length > 0 && (
                              <div>
                                <div style={{ fontSize: 11, color: "#78716c", fontWeight: 800, marginBottom: 5 }}>
                                  {t("page.workspace_detail.contextual_tools")}
                                </div>
                                {renderSmallList(contextualTools, t("page.workspace_detail.no_contextual_tools"), (tool) => _friendlyCodeLabel(String(tool)))}
                              </div>
                            )}
                          </div>
                        </div>
                      )}

                      <div>
                        <div style={LABEL}>{t("page.workspace_detail.mcp_external_integrations")}</div>
                        {integrations.length === 0 ? (
                          <div style={{ fontSize: 12, color: "#a8a29e" }}>{t("page.workspace_detail.no_mcp_integrations_bound")}</div>
                        ) : (
                          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                            {integrations.map((integration: any) => {
                              const live = integrationStatusByKey.get(integration.server_key);
                              const ready = live ? Boolean(live.agent_can_use) : Boolean(integration.ready);
                              return (
                                <div key={integration.binding_id || integration.server_key} style={{
                                  display: "flex",
                                  justifyContent: "space-between",
                                  gap: 8,
                                  alignItems: "center",
                                  padding: "8px 10px",
                                  borderRadius: 10,
                                  background: "#fff",
                                  border: "1px solid rgba(28,25,23,0.06)",
                                }}>
                                  <div style={{ minWidth: 0 }}>
                                    <div style={{ fontSize: 13, fontWeight: 700, color: "#1c1917", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                      {integration.name || integration.server_key}
                                    </div>
                                    {!integration.name && integration.server_key && (
                                      <div style={{ fontSize: 11, color: "#a8a29e" }}>
                                        {_friendlyCodeLabel(integration.server_key)}
                                      </div>
                                    )}
                                  </div>
                                  <StatusBadge type={ready ? "active" : "warning"} dot>
                                    {ready ? t("page.workspaces.ready") : t("page.workspace_detail.needs_setup_2")}
                                  </StatusBadge>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>

                      <div>
                        <div style={LABEL}>{t("page.workspace_detail.tools")}</div>
                        {renderSmallList(tools, t("page.workspace_detail.no_direct_tools_bound"), (tool) => _friendlyCodeLabel(tool.display_name || tool.name))}
                      </div>

                      <div>
                        <div style={LABEL}>{t("nav.skills")}</div>
                        {renderSmallList(skills, t("page.workspace_detail.no_private_skills_bound"), (skill) => skill.display_name || skill.name || _friendlyCodeLabel(skill.slug))}
                      </div>

                      {missing.length > 0 && (
                        <div>
                          <div style={LABEL}>{t("page.workspace_detail.needs_setup_2")}</div>
                          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                            {missing.map((flag: any, i: number) => (
                              <Chip key={`${flag.provider || "missing"}-${i}`} variant="orange" size="sm">
                                {String(flag.provider || "integration").replace(/[_\-]+/g, " ")}
                              </Chip>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </GlassCard>
                );
              })}
            </div>
          </>
        )}
      </div>
    );
  }

  function renderChannels() {
    const items: any[] = channels || [];
    const mappings = (agentMappings as any[]) || [];
    const availableItems: any[] = availableChannels || [];
    const agentById = new Map<string, any>();
    for (const a of (entityAgents as any[]) || []) agentById.set(a.id, a);
    const effectiveOperatingModel = (operatingModel || ws.operating_model || {}) as Record<string, any>;
    const channelServices: any[] = (effectiveOperatingModel.services as any[]) || [];
    const serviceByKey = new Map<string, any>();
    for (const svc of channelServices) {
      const key = svc.service_key || svc.key || "";
      if (key) serviceByKey.set(key, svc);
    }
    const channelConfig = ((effectiveOperatingModel.channel_config || {}) as Record<string, any>);
    const flaggedIntegrations = (((ws.settings as any)?.flagged_integrations) || []) as any[];
    const channelPlanSources = [
      channelConfig.primary_external_channel ? { role: "Primary external", channel: channelConfig.primary_external_channel } : null,
      ...((channelConfig.secondary_external_channels || []) as any[]).map((channel) => ({ role: "Secondary external", channel })),
      channelConfig.internal_channel ? { role: "Internal", channel: channelConfig.internal_channel } : null,
    ].filter(Boolean) as Array<{ role: string; channel: any }>;
    const boundChannelTypes = new Set(
      items
        .map((ch) => String(ch.channel_type || ch.provider || "").toLowerCase())
        .filter(Boolean),
    );
    const availableChannelTypes = new Set(
      availableItems
        .filter((ch) => ch.attached)
        .map((ch) => String(ch.channel_type || ch.provider || "").toLowerCase())
        .filter(Boolean),
    );
    const flagForProvider = (provider: string) =>
      flaggedIntegrations.find((flag: any) => String(flag.provider || "").toLowerCase() === provider);
    const plannedChannels = channelPlanSources.map(({ role, channel }) => {
      const channelType = String(channel.channel_type || channel.provider || "").toLowerCase();
      const isInternal = channelType === "internal_chat";
      const isConnected = isInternal || boundChannelTypes.has(channelType) || availableChannelTypes.has(channelType);
      const missingFlag = channelType ? flagForProvider(channelType) : null;
      return {
        role,
        channel,
        channelType,
        isInternal,
        isConnected,
        missingFlag,
      };
    });
    const plannedProviderTypes = new Set(plannedChannels.map((entry) => entry.channelType).filter(Boolean));
    const extraIntegrationFlags = flaggedIntegrations.filter((flag: any) => {
      const provider = String(flag.provider || "").toLowerCase();
      return provider && !plannedProviderTypes.has(provider) && !boundChannelTypes.has(provider) && !availableChannelTypes.has(provider);
    });
    const serviceOptions = mappings
      .filter((m: any) => m.service_key)
      .map((m: any) => {
        const agent = agentById.get(m.agent_id);
        const serviceLabel = _serviceLabel(serviceByKey.get(m.service_key) || { service_key: m.service_key });
        const label = agent ? `${serviceLabel} → ${_agentLabel(agent)}` : serviceLabel;
        return { value: m.service_key, label };
      });
    const canAttachChannel =
      !attachChannel.isPending &&
      (channelForm.mode !== "existing" || !!channelForm.channel_config_id);
    const isEditingChannel = !!editingChannel;
    const canSaveChannel =
      isEditingChannel
        ? !updateChannel.isPending
        : canAttachChannel;
    const closeChannelModal = () => {
      setShowChannelModal(false);
      setEditingChannel(null);
    };
    const openAddChannel = () => {
      setEditingChannel(null);
      setChannelForm({
        mode: "existing",
        channel_config_id: "",
        name: "",
        purpose: "",
        linked_service_key: mappings[0]?.service_key || "",
        role: "primary_external",
        login_required: false,
        language: "en",
      });
      setShowChannelModal(true);
    };
    const openEditChannel = (ch: any) => {
      const cfg = ch.config || {};
      const linkedServiceKey = cfg.linked_service_key || cfg.service_key || "";
      setEditingChannel(ch);
      setChannelForm({
        mode: ch.channel_type === "webchat" ? "webchat" : "existing",
        channel_config_id: ch.id || "",
        name: _channelDisplayName(ch),
        purpose: cfg.purpose || "",
        linked_service_key: linkedServiceKey,
        role: cfg.role || "primary_external",
        login_required: Boolean(cfg.login_required),
        language: (cfg.language || "en").toLowerCase().split(/[-_]/)[0],
      });
      setShowChannelModal(true);
    };

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <div>
            <div style={SECTION_TITLE}>{t("page.workspace_detail.channels")}</div>
            <p style={{ margin: "-8px 0 0", color: "#78716c", fontSize: 13 }}>
              {t("page.workspace_detail.connect_inbound_surfaces_such_as_webchat_wechat")}
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
              {t("page.workspace_detail.manage_integrations")}
            </Button>
            <Button variant="primary" size="sm" onClick={openAddChannel}>
              {t("page.workspace_detail.add_channel")}
            </Button>
          </div>
        </div>

        {(plannedChannels.length > 0 || extraIntegrationFlags.length > 0) && (
          <GlassCard hoverable={false} style={{ padding: 18, borderRadius: 18 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
              <div>
                <div style={{ ...SECTION_TITLE, marginBottom: 6 }}>Designed channel plan</div>
                <p style={{ margin: 0, color: "#78716c", fontSize: 12, lineHeight: 1.55, maxWidth: 760 }}>
                  Workspace setup generated these surfaces. Ready items can receive or route work now; missing providers need to be connected in Integrations.
                </p>
              </div>
              {flaggedIntegrations.length > 0 && (
                <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
                  {t("page.workspace_detail.open_integrations")}
                </Button>
              )}
            </div>

            {plannedChannels.length > 0 && (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))", gap: 10 }}>
                {plannedChannels.map((entry, idx) => {
                  const planChannel = entry.channel || {};
                  const planAsChannel = {
                    channel_type: entry.channelType,
                    provider: entry.channelType,
                    name: planChannel.name,
                  };
                  const statusType = entry.isConnected ? "active" : "warning";
                  const statusLabel = entry.isInternal
                    ? "Built-in"
                    : entry.isConnected
                      ? t("page.workspace_detail.connected")
                      : t("page.workspace_detail.needs_setup_2");
                  const serviceKey = planChannel.linked_service_key || planChannel.service_key || entry.missingFlag?.linked_service_keys?.[0];
                  return (
                    <div
                      key={`${entry.role}-${entry.channelType || idx}`}
                      style={{
                        border: "1px solid rgba(28,25,23,0.06)",
                        background: "rgba(250,250,249,0.74)",
                        borderRadius: 14,
                        padding: "12px 14px",
                        minWidth: 0,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                        <span style={{ fontSize: 13, fontWeight: 800, color: "#1c1917", flex: 1, minWidth: 0 }}>
                          {_channelKindLabel(planAsChannel)}
                        </span>
                        <StatusBadge type={statusType} dot>{statusLabel}</StatusBadge>
                      </div>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                        <Chip variant="slate" size="sm">{entry.role}</Chip>
                        {planChannel.login_required && <Chip variant="blue" size="sm">{t("page.workspace_detail.login_required")}</Chip>}
                        {entry.missingFlag?.required && <Chip variant="red" size="sm">{t("page.login.required")}</Chip>}
                      </div>
                      {(planChannel.purpose || entry.missingFlag?.purpose) && (
                        <p style={{ margin: 0, color: "#57534e", fontSize: 12, lineHeight: 1.45 }}>
                          {planChannel.purpose || entry.missingFlag?.purpose}
                        </p>
                      )}
                      {serviceKey && (
                        <div style={{ marginTop: 8, fontSize: 11, color: "#a8a29e", fontWeight: 700 }}>
                          Routes to {_serviceLabel(serviceByKey.get(serviceKey) || { service_key: serviceKey })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {extraIntegrationFlags.length > 0 && (
              <div style={{ marginTop: plannedChannels.length > 0 ? 12 : 0, display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ fontSize: 11, color: "#a8a29e", fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Tool integrations needed by agents
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {extraIntegrationFlags.map((flag: any, idx: number) => (
                    <span
                      key={`${flag.provider || "integration"}-${idx}`}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        padding: "6px 9px",
                        borderRadius: 999,
                        background: "rgba(249,244,236,0.78)",
                        border: "1px solid rgba(251,146,60,0.32)",
                        color: "#7c4a2e",
                        fontSize: 12,
                        fontWeight: 800,
                      }}
                      title={flag.purpose || ""}
                    >
                      {_humanize(flag.provider)}
                      <span style={{ color: "#9a5630" }}>{t("page.workspace_detail.needs_setup_2")}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </GlassCard>
        )}

        {items.length === 0 ? (
          <EmptyState
            title={t("page.workspace_detail.no_channels")}
            description={t("page.workspace_detail.attach_a_ready_integration_or_create_a_public_we")}
            action={
              <Button variant="primary" onClick={openAddChannel}>
                {t("page.workspace_detail.add_channel")}
              </Button>
            }
          />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 360px), 1fr))", gap: 16, alignItems: "start" }}>
            {items.map((ch, i) => {
              const cfg = ch.config || {};
              const linkedServiceKey = cfg.linked_service_key || cfg.service_key || null;
              // Prefer backend-resolved bound_agent, fallback to subscription lookup
              const boundAgent = ch.bound_agent || (() => {
                if (!linkedServiceKey) return null;
                const m = mappings.find((m) => m.service_key === linkedServiceKey);
                return m ? agentById.get(m.agent_id) : null;
              })();
              const isWebchat = ch.channel_type === "webchat";
              const isInternal = _isInternalChannel(ch);
              const channelTitle = _channelDisplayName(ch);
              const channelKind = _channelKindLabel(ch);
              const channelLanguage = cfg.language || "en";
              const accessLabel = isWebchat
                ? (cfg.login_required ? t("page.workspace_detail.login_required") : t("page.workspace_detail.anyone_with_link"))
                : (isInternal ? t("page.workspace_detail.workspace_members") : (cfg.login_required ? t("page.workspace_detail.login_required") : t("page.workspace_detail.connected_integration")));
              const publicToken = ch.channel_binding_id ? (ch.public_token || cfg.public_token) : "";
              const publicChatUrl = publicToken ? `${window.location.origin}/chat/public/${publicToken}` : "";
              const qrCodeApiUrl = publicToken ? `${window.location.origin}/api/v1/public/chat/${publicToken}/qr` : "";
              const embedApiUrl = publicToken ? `${window.location.origin}/api/v1/public/chat/${publicToken}/embed` : "";
              const embedScriptUrl = publicToken ? `${window.location.origin}/api/v1/public/chat/${publicToken}/embed.js` : "";
              const embedSnippet = embedScriptUrl ? `<script async src="${embedScriptUrl}"></script>` : "";
              const channelKey = ch.channel_binding_id || ch.id || String(i);
              const developerSettingsOpen = Boolean(expandedChannelDeveloperSettings[channelKey]);
              const sourceScope = ch.source_scope === "shared" ? t("page.workspace_detail.shared_integration") : t("page.workspace_detail.workspace_channel");

              return (
                <GlassCard key={ch.id || i} hoverable={false} style={{ padding: 20, borderRadius: 18 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
                    {isWebchat ? (
                      <svg style={{ width: 18, height: 18, color: "#4869ac", flexShrink: 0 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 4.875c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5A1.125 1.125 0 013.75 9.375v-4.5zM3.75 14.625c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5a1.125 1.125 0 01-1.125-1.125v-4.5zM13.5 4.875c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5A1.125 1.125 0 0113.5 9.375v-4.5z" />
                        <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 14.625c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5a1.125 1.125 0 01-1.125-1.125v-4.5z" />
                      </svg>
                    ) : (
                      <svg style={{ width: 18, height: 18, color: "#57534e", flexShrink: 0 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.076-4.076a1.526 1.526 0 011.037-.443 48.282 48.282 0 005.68-.494c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
                      </svg>
                    )}
                    <span style={{ fontSize: 14, fontWeight: 700, color: "#292524", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {channelTitle}
                    </span>
                    <Chip variant={ch.source_scope === "shared" ? "slate" : "teal"} size="sm">
                      {sourceScope}
                    </Chip>
                    {isWebchat && <Chip variant="blue" size="sm">{t("page.workspace_detail.public")}</Chip>}
                  </div>

                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    <p style={{ margin: 0, minHeight: 38, color: "#44403c", fontSize: 13, lineHeight: 1.45, fontWeight: 600 }}>
                      {cfg.purpose || t("page.workspace_detail.channel_default_purpose")}
                    </p>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      <span style={{
                        display: "inline-flex", alignItems: "center", minHeight: 24, padding: "3px 9px",
                        borderRadius: 999, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                        color: "#57534e", fontSize: 11, fontWeight: 700,
                      }}>
                        {channelKind}
                      </span>
                      <span style={{
                        display: "inline-flex", alignItems: "center", minHeight: 24, padding: "3px 9px",
                        borderRadius: 999, background: isWebchat ? "#f3f6fa" : "#f5f5f4",
                        border: `1px solid ${isWebchat ? "#bfdbfe" : "#efedea"}`,
                        color: isWebchat ? "#3f57a0" : "#436b65", fontSize: 11, fontWeight: 700,
                      }}>
                        {accessLabel}
                      </span>
                      <span style={{
                        display: "inline-flex", alignItems: "center", minHeight: 24, padding: "3px 9px",
                        borderRadius: 999, background: "#f9f4ec", border: "1px solid #ecdac2",
                        color: "#7c4a2e", fontSize: 11, fontWeight: 700,
                      }}>
                        {t("settings.language")}: {_channelLanguageLabel(channelLanguage)}
                      </span>
                    </div>
                  </div>

                  {/* Webchat: QR code + public link */}
                  {isWebchat && publicToken && (
                    <div style={{
                      marginTop: 14,
                      padding: 12,
                      borderRadius: 14,
                      background: "#fafaf9",
                      border: "1px solid rgba(28,25,23,0.06)",
                    }}>
                      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 112px", alignItems: "start", gap: 12 }}>
                        <div style={{ minWidth: 0 }}>
                          <div style={LABEL}>{t("page.workspace_detail.public_chat_link")}</div>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                            <code style={{
                              flex: 1, minWidth: 0, fontSize: 11, padding: "6px 8px", borderRadius: 8,
                              background: "#fff", border: "1px solid rgba(28,25,23,0.06)",
                              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                              color: "#44403c",
                            }}>
                              {publicChatUrl}
                            </code>
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={(e) => {
                                e.stopPropagation();
                                navigator.clipboard.writeText(publicChatUrl);
                                toast.success(t("page.workspace_detail.link_copied"));
                              }}
                            >
                              {t("page.workspace_detail.copy")}
                            </Button>
                          </div>
                          <div style={{ fontSize: 10, color: "#a8a29e", marginTop: 6, lineHeight: 1.4 }}>
                            {t("page.workspace_detail.share_link_or_scan_qr")}
                          </div>
                        </div>
                        <div style={{ textAlign: "center" }}>
                          <img
                            src={qrCodeApiUrl}
                            alt={t("page.workspace_detail.qr_code")}
                            style={{
                              width: 112, height: 112, borderRadius: 10,
                              border: "1px solid rgba(28,25,23,0.06)", background: "#fff",
                            }}
                            onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                          />
                          <div style={{ fontSize: 10, color: "#a8a29e", marginTop: 4 }}>
                            {t("page.workspace_detail.scan_to_start_chatting")}
                          </div>
                        </div>
                      </div>

                      <div style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 10,
                        marginTop: 12,
                        paddingTop: 10,
                        borderTop: "1px solid rgba(28,25,23,0.06)",
                      }}>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontSize: 12, fontWeight: 700, color: "#44403c" }}>
                            {t("page.workspace_detail.developer_settings")}
                          </div>
                          <div style={{ fontSize: 10, lineHeight: 1.4, color: "#a8a29e" }}>
                            {t("page.workspace_detail.developer_settings_hint")}
                          </div>
                        </div>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            setExpandedChannelDeveloperSettings((prev) => ({
                              ...prev,
                              [channelKey]: !prev[channelKey],
                            }));
                          }}
                        >
                          {developerSettingsOpen ? t("page.workspace_detail.hide_developer_settings") : t("page.workspace_detail.show_developer_settings")}
                        </Button>
                      </div>

                      {developerSettingsOpen && (
                        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 10 }}>
                          <div style={{ fontSize: 11, color: "#78716c", lineHeight: 1.5 }}>
                            {t("page.workspace_detail.public_token_no_api_key")}
                          </div>
                          <div>
                            <div style={LABEL}>{t("page.workspace_detail.embed_script")}</div>
                            <div style={{ display: "flex", alignItems: "flex-start", gap: 8, marginTop: 4 }}>
                              <pre style={{
                                flex: 1, minWidth: 0, margin: 0, fontSize: 11, lineHeight: 1.45,
                                padding: "8px 10px", borderRadius: 6,
                                background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                                whiteSpace: "pre-wrap", wordBreak: "break-all",
                                color: "#44403c",
                              }}>{embedSnippet}</pre>
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  navigator.clipboard.writeText(embedSnippet);
                                  toast.success(t("page.workspace_detail.embed_code_copied"));
                                }}
                              >
                                {t("page.workspace_detail.copy_embed_code")}
                              </Button>
                            </div>
                            <div style={{ fontSize: 10, color: "#a8a29e", marginTop: 4 }}>
                              {t("page.workspace_detail.paste_embed_script_before_body")}
                            </div>
                          </div>
                          <div>
                            <div style={LABEL}>{t("page.workspace_detail.embed_api")}</div>
                            <div style={{ fontSize: 10, color: "#a8a29e", marginTop: -2, marginBottom: 4 }}>
                              {t("page.workspace_detail.embed_api_help")}
                            </div>
                            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                              <code style={{
                                flex: 1, minWidth: 0, fontSize: 11, padding: "6px 8px", borderRadius: 6,
                                background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                                color: "#44403c",
                              }}>
                                {embedApiUrl}
                              </code>
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  navigator.clipboard.writeText(embedApiUrl);
                                  toast.success(t("page.workspace_detail.api_endpoint_copied"));
                                }}
                              >
                                {t("page.workspace_detail.copy")}
                              </Button>
                            </div>
                          </div>
                          <div>
                            <div style={LABEL}>{t("page.workspace_detail.qr_code_api")}</div>
                            <div style={{ fontSize: 10, color: "#a8a29e", marginTop: -2, marginBottom: 4 }}>
                              {t("page.workspace_detail.qr_code_api_help")}
                            </div>
                            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                              <code style={{
                                flex: 1, minWidth: 0, fontSize: 11, padding: "6px 8px", borderRadius: 6,
                                background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)",
                                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                                color: "#44403c",
                              }}>
                                {qrCodeApiUrl}
                              </code>
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  navigator.clipboard.writeText(qrCodeApiUrl);
                                  toast.success(t("page.workspace_detail.api_endpoint_copied"));
                                }}
                              >
                                {t("page.workspace_detail.copy")}
                              </Button>
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Agent binding */}
                  <div style={{
                    marginTop: 12, paddingTop: 12,
                    borderTop: "1px solid rgba(28,25,23,0.06)",
                  }}>
                    {boundAgent ? (
                      <div>
                        <div style={LABEL}>{t("page.workspace_detail.handled_by")}</div>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                          <AgentAvatar
                            name={boundAgent.name || _agentLabel(boundAgent)}
                            avatarUrl={boundAgent.avatar_url}
                            seed={boundAgent.id}
                            size={22}
                            shape="rounded"
                          />
                          <span style={{ fontSize: 13, fontWeight: 600, color: "#1c1917" }}>
                            {boundAgent.name || _agentLabel(boundAgent)}
                          </span>
                          {linkedServiceKey && (
                            <span style={{ color: "#a8a29e", fontWeight: 500, fontSize: 11 }}>
                              · {_serviceLabel(serviceByKey.get(linkedServiceKey) || { service_key: linkedServiceKey })}
                            </span>
                          )}
                        </div>
                      </div>
                    ) : ch.channel_type === "internal_chat" ? (
                      <div style={{ fontSize: 12, color: "#78716c" }}>
                        {t("page.workspace_detail.built_in_workspace_chat")}
                      </div>
                    ) : (
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                        <StatusBadge type="warning" dot>{t("page.workspace_detail.no_agent_bound")}</StatusBadge>
                        <Button variant="ghost" size="sm" onClick={() => handleTabChange("agents")}>
                          {t("page.workspace_detail.map_agent_2")}
                        </Button>
                      </div>
                    )}
                  </div>

                  {ch.channel_binding_id && (
                    <div style={{
                      marginTop: 12,
                      display: "flex",
                      gap: 8,
                      justifyContent: "flex-end",
                      borderTop: "1px solid rgba(28,25,23,0.06)",
                      paddingTop: 10,
                    }}>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => openEditChannel(ch)}
                      >
                        {t("action.edit")}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setConfirmRemoveChannel({
                          id: ch.channel_binding_id,
                          name: channelTitle || t("page.workspace_detail.this_channel"),
                        })}
                      >
                        <span style={{ color: "#d65f59", fontWeight: 700 }}>{t("page.task_detail.runtime.remove_rule")}</span>
                      </Button>
                    </div>
                  )}
                </GlassCard>
              );
            })}
          </div>
        )}

        <Modal
          open={showChannelModal}
          onClose={closeChannelModal}
          title={isEditingChannel ? t("page.workspace_detail.edit_channel") : t("page.workspace_detail.add_channel")}
          maxWidth="560px"
          footer={
            <>
              <Button variant="outline" onClick={closeChannelModal}>{t("action.cancel")}</Button>
              <Button
                variant="primary"
                disabled={!canSaveChannel}
                loading={isEditingChannel ? updateChannel.isPending : attachChannel.isPending}
                onClick={() => {
                  if (isEditingChannel) updateChannel.mutate();
                  else attachChannel.mutate();
                }}
              >
                {isEditingChannel
                  ? (updateChannel.isPending ? t("page.workspace_detail.updating") : t("page.workspace_detail.update_channel"))
                  : (attachChannel.isPending ? t("page.workspace_detail.adding") : t("page.workspace_detail.add_channel"))}
              </Button>
            </>
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {!isEditingChannel && (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 10 }}>
                <button
                  type="button"
                  onClick={() => setChannelForm({ ...channelForm, mode: "existing" })}
                  style={{
                    textAlign: "left",
                    padding: "12px 14px",
                    borderRadius: 14,
                    border: channelForm.mode === "existing" ? "1px solid #5f928a" : "1px solid #e7e5e4",
                    background: channelForm.mode === "existing" ? "#f5f5f4" : "#fff",
                    color: "#1c1917",
                    cursor: "pointer",
                  }}
                >
                  <strong style={{ display: "block", fontSize: 13 }}>{t("page.workspace_detail.use_existing_integration")}</strong>
                  <span style={{ display: "block", marginTop: 4, color: "#78716c", fontSize: 12 }}>
                    {t("page.workspace_detail.attach_wechat_whatsapp_sms_email_or_another_read")}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => setChannelForm({ ...channelForm, mode: "webchat" })}
                  style={{
                    textAlign: "left",
                    padding: "12px 14px",
                    borderRadius: 14,
                    border: channelForm.mode === "webchat" ? "1px solid #5f928a" : "1px solid #e7e5e4",
                    background: channelForm.mode === "webchat" ? "#f5f5f4" : "#fff",
                    color: "#1c1917",
                    cursor: "pointer",
                  }}
                >
                  <strong style={{ display: "block", fontSize: 13 }}>{t("page.workspace_detail.create_webchat")}</strong>
                  <span style={{ display: "block", marginTop: 4, color: "#78716c", fontSize: 12 }}>
                    {t("page.workspace_detail.create_a_public_chat_link_and_qr_code_for_this_w")}
                  </span>
                </button>
              </div>
            )}

            {isEditingChannel ? (
              <>
                <Input
                  label={t("page.workspace_detail.channel_name")}
                  value={channelForm.name}
                  onChange={(e) => setChannelForm({ ...channelForm, name: e.target.value })}
                  placeholder={t("page.workspace_detail.channel_2")}
                />
                {channelForm.mode === "webchat" && (
                  <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "#44403c" }}>
                    <input
                      type="checkbox"
                      checked={channelForm.login_required}
                      onChange={(e) => setChannelForm({ ...channelForm, login_required: e.target.checked })}
                    />
                    {t("page.workspace_detail.require_visitor_login_before_chat_starts")}
                  </label>
                )}
              </>
            ) : channelForm.mode === "existing" ? (
              <_Field label={t("page.workspace_detail.channel_integration")}>
                <Select
                  value={channelForm.channel_config_id}
                  onChange={(v) => setChannelForm({ ...channelForm, channel_config_id: v })}
                  placeholder={availableItems.length ? t("page.workspace_detail.select_a_ready_channel") : t("page.workspace_detail.no_ready_channel_integrations_found")}
                  filterable
                  options={availableItems.map((ch: any) => ({
                    value: ch.id,
                    label: `${_channelDisplayName(ch)} · ${_channelKindLabel(ch)}${ch.attached ? ` · ${t("page.workspace_detail.already_attached")}` : ""}`,
                  }))}
                />
                {availableItems.length === 0 && (
                  <div style={{ marginTop: 8, fontSize: 12, color: "#78716c" }}>
                    {t("page.workspace_detail.connect_external_providers_in_integrations_first")}
                  </div>
                )}
              </_Field>
            ) : (
              <>
                <Input
                  label={t("page.workspace_detail.webchat_name")}
                  value={channelForm.name}
                  onChange={(e) => setChannelForm({ ...channelForm, name: e.target.value })}
                  placeholder={t("page.workspace_detail.website_chat")}
                />
                <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "#44403c" }}>
                  <input
                    type="checkbox"
                    checked={channelForm.login_required}
                    onChange={(e) => setChannelForm({ ...channelForm, login_required: e.target.checked })}
                  />
                  {t("page.workspace_detail.require_visitor_login_before_chat_starts")}
                </label>
              </>
            )}

            <_Field label={t("settings.language")}>
              <Select
                value={channelForm.language || "en"}
                onChange={(v) => setChannelForm({ ...channelForm, language: v || "en" })}
                options={CHANNEL_LANGUAGE_OPTIONS}
              />
            </_Field>

            <_Field label={t("page.workspace_detail.route_to_workspace_service")}>
              <Select
                value={channelForm.linked_service_key}
                onChange={(v) => setChannelForm({ ...channelForm, linked_service_key: v })}
                placeholder={serviceOptions.length ? t("page.workspace_detail.select_service_agent") : t("page.workspace_detail.no_service_agent_mapping_yet")}
                filterable
                options={serviceOptions}
              />
              {serviceOptions.length === 0 && (
                <div style={{ marginTop: 8, fontSize: 12, color: "#78716c" }}>
                  {t("page.workspace_detail.you_can_still_add_the_channel_now_then_map_a_ser")}
                </div>
              )}
            </_Field>

            <Textarea
              label={t("page.workspace_detail.purpose")}
              rows={3}
              value={channelForm.purpose}
              onChange={(e) => setChannelForm({ ...channelForm, purpose: e.target.value })}
              placeholder={t("page.workspace_detail.example_customer_support_entry_point_for_inbound")}
            />
          </div>
        </Modal>
      </div>
    );
  }

  function renderDocuments() {
    const items: any[] = documents || [];
    const knowledgeFolders = items.filter((g) => !g.is_workspace_file_bucket);
    const workspaceDocIds = new Set<string>();
    for (const group of items) {
      for (const doc of group.documents || []) workspaceDocIds.add(doc.id);
    }
    const targetGroupDocIds = new Set<string>(
      (items.find((g) => g.id === knowledgeTargetGroupId)?.documents || []).map((doc: any) => doc.id),
    );
    const availableItems = ((availableKnowledgeDocs as any)?.items || []) as any[];
    const settingsGroup = knowledgeFolderSettingsGroupId
      ? items.find((g) => g.id === knowledgeFolderSettingsGroupId)
      : null;
    const knowledge = {
      ...DEFAULT_KNOWLEDGE_POLICY,
      ...((operatingModel?.knowledge as Record<string, any>) || {}),
    };
    const defaultGroupIds = new Set<string>((knowledge.default_group_ids || []).filter(Boolean));
    const commitKnowledge = (patch: Record<string, any>) => {
      if (!canManageWs) return;
      const nextKnowledge = { ...knowledge, ...patch };
      updateOperatingModel.mutate({
        ...(operatingModel || {}),
        knowledge: nextKnowledge,
      });
    };
    const toggleDefaultGroup = (groupId: string, checked: boolean) => {
      const next = new Set(defaultGroupIds);
      if (checked) next.add(groupId);
      else next.delete(groupId);
      commitKnowledge({ default_group_ids: Array.from(next) });
    };
    const setGroupPurpose = (groupId: string, purpose: string) => {
      const groupPurposes = { ...(knowledge.group_purposes || {}) };
      if (purpose.trim()) groupPurposes[groupId] = purpose.trim();
      else delete groupPurposes[groupId];
      commitKnowledge({ group_purposes: groupPurposes });
    };
    const ensureDefaultKnowledgeFolder = async () => {
      if (!canManageWs) return "";
      const existing = knowledgeFolders.find((g) => g.is_default_collection) || knowledgeFolders.find((g) => g.name === t("page.workspace_detail.workspace_knowledge")) || knowledgeFolders[0];
      if (existing?.id) return existing.id;
      const folder = await api.workspaces.knowledge.createGroup(workspaceId!, {
        name: t("page.workspace_detail.workspace_knowledge"),
        kind: "workspace_collection",
        purpose: t("page.knowledge.general_workspace_knowledge_available"),
      });
      queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspaceId] });
      if (folder?.id) {
        try {
          const nextDefaultGroupIds = _uniqueStrings([
            ...((knowledge.default_group_ids || []) as string[]),
            folder.id,
          ]);
          await api.workspaces.updateOperatingModel(workspaceId!, {
            ...(operatingModel || {}),
            knowledge: {
              ...knowledge,
              default_group_ids: nextDefaultGroupIds,
            },
          });
          queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
          queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
        } catch {
          // Folder creation is the critical path; default-source marking can be retried from the UI.
        }
      }
      return folder?.id || "";
    };
    const openAddKnowledge = async () => {
      if (!canManageWs) return;
      setSelectedKnowledgeDocIds([]);
      setKnowledgeSearch("");
      try {
        const targetFolderId = await ensureDefaultKnowledgeFolder();
        setKnowledgeTargetGroupId(targetFolderId);
        setShowAddKnowledgeModal(true);
      } catch (err: any) {
        toast.error(t("page.workspace_detail.could_not_prepare_workspace_knowledge_folder"), err?.message);
      }
    };
    const knowledgePolicyModal = (
      <Modal
        open={showKnowledgePolicyModal}
        onClose={() => setShowKnowledgePolicyModal(false)}
        title={t("page.workspace_detail.agent_usage_policy")}
        maxWidth="760px"
        footer={
          <Button variant="primary" onClick={() => setShowKnowledgePolicyModal(false)}>
            {t("page.team_people.done")}
          </Button>
        }
      >
        <p style={{ fontSize: 13, color: "#78716c", margin: "0 0 14px", lineHeight: 1.5 }}>
          {t("page.workspace_detail.separate_from_the_folders_above_these_settings_c")}
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <KnowledgePolicyRow
            title={t("page.workspace_detail.auto_search_when_relevant")}
            description={t("page.workspace_detail.agents_may_search_workspace_knowledge_before_ans")}
            checked={knowledge.auto_search !== false}
            active={knowledge.auto_search !== false}
            disabled={updateOperatingModel.isPending}
            onToggle={() => commitKnowledge({ auto_search: knowledge.auto_search === false })}
          />
          <KnowledgePolicyRow
            title={t("page.workspace_detail.cite_sources")}
            description={t("page.workspace_detail.prefer_document_names_or_source_snippets_when_kn")}
            checked={knowledge.citation_required !== false}
            active={knowledge.citation_required !== false}
            disabled={updateOperatingModel.isPending}
            onToggle={() => commitKnowledge({ citation_required: knowledge.citation_required === false })}
          />
          <KnowledgePolicyRow
            title={t("page.workspace_detail.strict_mode")}
            description={t("page.workspace_detail.for_knowledge_tasks_stay_inside_workspace_knowle")}
            checked={knowledge.strict_mode === true}
            active={knowledge.strict_mode === true}
            disabled={updateOperatingModel.isPending}
            onToggle={() => {
              const checked = knowledge.strict_mode !== true;
              commitKnowledge({ strict_mode: checked, retrieval_mode: checked ? "strict" : "auto" });
            }}
          />
        </div>
        <div style={{ marginTop: 14, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))", gap: 12, alignItems: "center" }}>
          <label style={{ fontSize: 12, color: "#78716c", fontWeight: 700 }}>{t("page.workspace_detail.retrieval_mode")}</label>
          <Select
            value={knowledge.retrieval_mode || "auto"}
            onChange={(value) => commitKnowledge({ retrieval_mode: value, strict_mode: value === "strict" })}
            options={[
              { value: "auto", label: t("page.workspace_detail.auto_search_when_useful") },
              { value: "manual", label: t("page.workspace_detail.manual_only_when_user_references_docs") },
              { value: "strict", label: t("page.workspace_detail.strict_stay_inside_workspace_knowledge") },
            ]}
            style={updateOperatingModel.isPending ? { pointerEvents: "none", opacity: 0.6 } : undefined}
          />
        </div>
      </Modal>
    );
    const folderSettingsModal = settingsGroup ? (
      <Modal
        open={!!settingsGroup}
        onClose={() => setKnowledgeFolderSettingsGroupId("")}
        title={`${settingsGroup.name || t("page.workspace_detail.knowledge_folder")} ${t("page.workspace_detail.settings")}`}
        maxWidth="640px"
        footer={
          <>
            {!settingsGroup.is_workspace_file_bucket && !settingsGroup.is_default_collection && (
              <Button
                variant="danger"
                onClick={() => {
                  setKnowledgeFolderSettingsGroupId("");
                  setConfirmDeleteKnowledgeGroup({
                    groupId: settingsGroup.id,
                    name: settingsGroup.name || t("page.workspace_detail.untitled_folder"),
                  });
                }}
              >
                {t("page.workspace_detail.remove_folder")}
              </Button>
            )}
            <Button variant="primary" onClick={() => setKnowledgeFolderSettingsGroupId("")}>
              {t("page.team_people.done")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {settingsGroup.is_default_collection ? (
              <Chip variant="teal" size="sm">{t("page.workspace_detail.workspace_collection")}</Chip>
            ) : (
              <Chip variant="teal" size="sm">{t("page.workspace_detail.folder")}</Chip>
            )}
            {settingsGroup.is_default_collection && <Chip variant="slate" size="sm">{t("page.api_keys.default")}</Chip>}
            <Chip variant="slate" size="sm">{settingsGroup.document_count ?? 0} {t("page.workspace_detail.files")}</Chip>
          </div>

          <div>
            <div style={{ fontSize: 12, fontWeight: 800, color: "#78716c", marginBottom: 8 }}>
              {t("page.workspace_detail.folder_purpose")}
            </div>
            <KnowledgePurposeField
              value={(knowledge.group_purposes || {})[settingsGroup.id] || ""}
              placeholder={settingsGroup.is_default_collection ? t("page.workspace_detail.general_workspace_knowledge_notes") : t("page.workspace_detail.how_agents_should_use_this_folder_e_g_brand_voice_prod")}
              disabled={updateOperatingModel.isPending}
              onCommit={(value) => setGroupPurpose(settingsGroup.id, value)}
            />
          </div>

          {!settingsGroup.is_default_collection && (
            <div style={{ padding: 12, borderRadius: 14, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)", display: "flex", alignItems: "center", gap: 12 }}>
              <Toggle
                size="sm"
                checked={defaultGroupIds.has(settingsGroup.id)}
                onChange={() => toggleDefaultGroup(settingsGroup.id, !defaultGroupIds.has(settingsGroup.id))}
                disabled={updateOperatingModel.isPending}
              />
              <div>
                <div style={{ fontSize: 13, fontWeight: 800, color: "#1c1917" }}>{t("page.workspace_detail.use_by_default")}</div>
                <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.45 }}>
                  {t("page.workspace_detail.prefer_this_folder_when_agents_search_workspace")}
                </div>
              </div>
            </div>
          )}

          {settingsGroup.is_default_collection && (
            <div style={{ padding: 12, borderRadius: 14, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)", color: "#78716c", fontSize: 12, lineHeight: 1.5 }}>
              {t("page.workspace_detail.this_is_the_main_workspace_knowledge_collection")}
            </div>
          )}
        </div>
      </Modal>
    ) : null;

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
	          <div>
	            <div style={SECTION_TITLE}>{t("page.workspace_detail.workspace_knowledge")}</div>
	            <p style={{ fontSize: 12, color: "#78716c", margin: "-8px 0 0", lineHeight: 1.5 }}>
	              {t("page.workspace_detail.documents_attached_to_this_workspace_folders_are")}
	            </p>
	          </div>
		          <div style={{ display: "flex", gap: 8 }}>
		            {canManageWs && (
		              <Button variant="outline" size="sm" onClick={() => setShowKnowledgePolicyModal(true)}>
		                {t("page.workspace_detail.policy_settings")}
		              </Button>
		            )}
		            <Button variant="outline" size="sm" onClick={() => navigate("/knowledge")}>
		              {t("page.workspace_detail.browse_all_knowledge")}
		            </Button>
		            {canManageWs && (
		              <>
	                <Button variant="outline" size="sm" onClick={openAddKnowledge}>
	                  {t("page.workspace_detail.add_documents")}
	                </Button>
	                <Button
	                  variant="primary"
	                  size="sm"
	                  onClick={openCreateKnowledgeFolderModal}
	                  loading={createDocGroup.isPending}
	                >
	                  {t("page.workspace_detail.new_folder")}
	                </Button>
		              </>
		            )}
          </div>
        </div>

	        {items.length === 0 ? (
	          <EmptyState
	            title={t("page.workspace_detail.no_workspace_knowledge_yet")}
	            description={t("page.workspace_detail.attach_existing_knowledge_documents_a_default_wo")}
	            action={canManageWs ? (
	              <div style={{ display: "flex", gap: 8 }}>
	                <Button variant="outline" onClick={openAddKnowledge}>
	                  {t("page.workspace_detail.add_existing_documents")}
	                </Button>
	                <Button variant="primary" onClick={openCreateKnowledgeFolderModal}>
	                  {t("page.workspace_detail.new_folder")}
	                </Button>
	              </div>
	            ) : undefined}
	          />
	        ) : (
	          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
	            {items.map((group, i) => (
              <GlassCard key={group.id || i}>
	                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
	                  <IconDocument size={18} className="text-manor-700" />
	                  <span style={{ fontSize: 14, fontWeight: 700, color: "#292524", flex: 1 }}>
	                    {group.name || t("page.workspace_detail.untitled_folder")}
	                  </span>
	                  {group.is_workspace_file_bucket ? (
	                    <Chip variant="slate" size="sm">{t("page.workspace_detail.workspace_files")}</Chip>
	                  ) : group.is_default_collection ? (
	                    <Chip variant="teal" size="sm">{t("page.workspace_detail.workspace_collection")}</Chip>
	                  ) : (
	                    <Chip variant="teal" size="sm">{t("page.workspace_detail.folder")}</Chip>
	                  )}
		                  {!group.is_workspace_file_bucket && group.is_default_collection && (
		                    <Chip variant="slate" size="sm">{t("page.api_keys.default")}</Chip>
		                  )}
		                  <span style={{ fontSize: 12, color: "#78716c" }}>
		                    {group.document_count ?? 0} {t("page.workspace_detail.file")}{(group.document_count ?? 0) !== 1 ? "s" : ""}
		                  </span>
		                  {canManageWs && !group.is_workspace_file_bucket && (
		                    <Button
		                      variant="outline"
		                      size="sm"
		                      onClick={() => setKnowledgeFolderSettingsGroupId(group.id)}
		                    >
		                      {t("nav.settings")}
		                    </Button>
		                  )}
		                </div>
                {group.documents && group.documents.length > 0 ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {group.documents.map((doc: any) => (
                      <div
                        key={doc.id}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          padding: 4,
                          borderRadius: 12,
                          border: "1px solid rgba(28,25,23,0.06)",
                          background: "rgba(255,255,255,0.72)",
                          transition: "background 0.15s, border-color 0.15s",
                        }}
                        onMouseEnter={(e) => {
                          e.currentTarget.style.background = "rgba(242,246,245,0.72)";
                          e.currentTarget.style.borderColor = "rgba(95,146,138,0.22)";
                        }}
                        onMouseLeave={(e) => {
                          e.currentTarget.style.background = "rgba(255,255,255,0.72)";
                          e.currentTarget.style.borderColor = "rgba(231,229,228,0.72)";
                        }}
                      >
                        <Link
                          to={`/viewer/${doc.id}`}
                          state={{ returnTo: `/workspaces/${ws.id}?tab=documents` }}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            flex: 1,
                            minWidth: 0,
                            padding: "7px 8px",
                            color: "inherit",
                            textDecoration: "none",
                          }}
                        >
                          <IconDocument size={14} style={{ color: "#57534e", flexShrink: 0 }} />
                          <span style={{ fontSize: 13, fontWeight: 650, color: "#44403c", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {doc.name}
                          </span>
                          <span style={{ fontSize: 11, color: "#a8a29e", flexShrink: 0 }}>{doc.file_type || ""}</span>
                          {doc.vector_status === "ready" || doc.vector_status === "indexed" ? (
                            <StatusBadge type="success" dot>{t("page.workspace_detail.indexed")}</StatusBadge>
                          ) : doc.vector_status === "pending" ? (
                            <StatusBadge type="warning" dot>{t("page.workspace_detail.queued")}</StatusBadge>
                          ) : null}
                        </Link>
                        {canManageWs && !group.is_workspace_file_bucket && !group.settings?.readonly && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              setConfirmRemoveKnowledgeDoc({ groupId: group.id, documentId: doc.id, name: doc.name || t("page.workspace_detail.this_document") });
                            }}
                          >
                            <span style={{ color: "#d65f59", fontWeight: 700 }}>{t("page.task_detail.runtime.remove_rule")}</span>
                          </Button>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>
                    {group.is_default_collection ? t("page.workspace_detail.no_documents_in_workspace_knowledge_yet") : t("page.workspace_detail.no_documents_in_this_folder_yet")}
                  </p>
                )}
              </GlassCard>
	            ))}
	          </div>
	        )}
        {canManageWs && knowledgePolicyModal}
        {canManageWs && folderSettingsModal}
	        <Modal
	          open={canManageWs && showAddKnowledgeModal}
	          onClose={() => setShowAddKnowledgeModal(false)}
	          title={t("page.workspace_detail.add_documents_to_workspace")}
	          footer={
	            <>
	              <Button variant="outline" onClick={() => setShowAddKnowledgeModal(false)}>{t("action.cancel")}</Button>
	              <Button
	                variant="primary"
	                disabled={!knowledgeTargetGroupId || selectedKnowledgeDocIds.length === 0 || addKnowledgeDocuments.isPending}
	                loading={addKnowledgeDocuments.isPending}
	                onClick={() => addKnowledgeDocuments.mutate({
	                  groupId: knowledgeTargetGroupId,
	                  documentIds: selectedKnowledgeDocIds,
	                })}
	              >
	                {t("page.team_people.add")} {selectedKnowledgeDocIds.length || ""} {t("page.dashboard.document")}{selectedKnowledgeDocIds.length === 1 ? "" : "s"}
	              </Button>
	            </>
	          }
	        >
	          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
	            {knowledgeFolders.length === 0 ? (
	              <div style={{ padding: 12, borderRadius: 12, background: "#fafaf9", color: "#78716c", fontSize: 13, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
	                <span>{t("page.workspace_detail.preparing_the_default_workspace_knowledge_collec")}</span>
	                <Button
	                  variant="outline"
	                  size="sm"
                  onClick={() => {
                    setShowAddKnowledgeModal(false);
                    openCreateKnowledgeFolderModal();
                  }}
	                >
	                  {t("page.workspace_detail.new_folder")}
	                </Button>
	              </div>
	            ) : (
	              <Select
	                value={knowledgeTargetGroupId}
	                onChange={setKnowledgeTargetGroupId}
	                placeholder={t("page.workspace_detail.choose_destination")}
	                options={knowledgeFolders.map((group) => ({
	                  value: group.id,
	                  label: group.is_default_collection ? `${group.name || t("page.workspace_detail.workspace_knowledge")} (${t("page.workspace_detail.default")})` : (group.name || t("page.workspace_detail.untitled_folder")),
	                }))}
	              />
	            )}
	            <Input
	              value={knowledgeSearch}
	              onChange={(e) => setKnowledgeSearch(e.target.value)}
	              placeholder={t("page.workspace_detail.search_knowledge_documents")}
	            />
	            <div style={{ maxHeight: 360, overflowY: "auto", display: "flex", flexDirection: "column", gap: 6 }}>
	              {availableItems.length === 0 ? (
	                <p style={{ fontSize: 13, color: "#a8a29e", margin: "12px 0", textAlign: "center" }}>
	                  {t("page.workspace_detail.no_matching_documents_found")}
	                </p>
	              ) : (
	                availableItems.map((doc: any) => {
	                  const alreadyInTargetGroup = targetGroupDocIds.has(doc.id);
	                  const attachedElsewhere = !alreadyInTargetGroup && workspaceDocIds.has(doc.id);
	                  const checked = selectedKnowledgeDocIds.includes(doc.id);
	                  return (
	                    <label
	                      key={doc.id}
	                      style={{
	                        display: "flex",
	                        alignItems: "center",
	                        gap: 10,
	                        padding: "9px 10px",
	                        borderRadius: 10,
	                        border: "1px solid rgba(28,25,23,0.06)",
	                        background: alreadyInTargetGroup ? "rgba(250,250,249,0.7)" : "#fff",
	                        opacity: alreadyInTargetGroup ? 0.65 : 1,
	                      }}
	                    >
	                      <input
	                        type="checkbox"
	                        disabled={alreadyInTargetGroup}
	                        checked={checked || alreadyInTargetGroup}
	                        onChange={(e) => {
	                          if (e.target.checked) {
	                            setSelectedKnowledgeDocIds((ids) => _uniqueStrings([...ids, doc.id]));
	                          } else {
	                            setSelectedKnowledgeDocIds((ids) => ids.filter((id) => id !== doc.id));
	                          }
	                        }}
	                      />
	                      <IconDocument size={14} className="text-stone-400" />
	                      <span style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 700, color: "#44403c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
	                        {doc.name}
	                      </span>
	                      {alreadyInTargetGroup ? (
	                        <Chip variant="slate" size="sm">{t("page.workspace_detail.already_in_folder")}</Chip>
	                      ) : attachedElsewhere ? (
	                        <Chip variant="teal" size="sm">{t("page.workspace_detail.attached_elsewhere")}</Chip>
	                      ) : (
	                        <span style={{ fontSize: 11, color: "#a8a29e" }}>{doc.file_type || ""}</span>
	                      )}
	                    </label>
	                  );
	                })
	              )}
	            </div>
	          </div>
	        </Modal>
	      </div>
	    );
	  }

  function renderRules() {
    const policy = _mergeGovernancePolicy(governancePolicy?.policy);
    const operatingRules = ((operatingModel?.rules as any[]) || []).filter(Boolean);
    const copy = _ruleCopy();
    const commitPolicy = (next: GovernancePolicy, summary: string) => {
      updateGovernance.mutate({ policy: next, summary });
    };
    type GovernanceActionField = "hitl_required_actions" | "never_allow_actions" | "auto_approve_actions";
    type GovernanceCapabilityField = "hitl_required_capabilities" | "never_allow_capabilities" | "auto_approve_capabilities";
    const capabilityFieldFor = (field: GovernanceActionField): GovernanceCapabilityField => (
      field === "never_allow_actions"
        ? "never_allow_capabilities"
        : field === "auto_approve_actions"
          ? "auto_approve_capabilities"
          : "hitl_required_capabilities"
    );
    const addPatterns = (
      field: GovernanceActionField,
      patterns: string[],
      summary: string,
      capabilityPatterns = runtimeCapabilitiesForActionPatterns(patterns),
    ) => {
      if (updateGovernance.isPending) return;
      const capabilityField = capabilityFieldFor(field);
      const next = {
        ...policy,
        [field]: _uniqueStrings([...(policy[field] || []), ...patterns]),
        [capabilityField]: _uniqueStrings([...(policy[capabilityField] || []), ...capabilityPatterns]),
      };
      commitPolicy(next, summary);
    };
    const removePattern = (
      field: GovernanceActionField | GovernanceCapabilityField,
      pattern: string,
    ) => {
      const next = {
        ...policy,
        [field]: (policy[field] || []).filter((p) => p !== pattern),
      };
      commitPolicy(next, `Remove ${pattern} from ${field}`);
    };
    const addNaturalRule = () => {
      const text = rulePrompt.trim();
      if (!text) return;
      const inferred = _inferGovernanceRule(text);
      addPatterns(inferred.field, inferred.patterns, `Natural-language rule: ${text}`, inferred.capabilityPatterns);
      const nextRules = [
        ...operatingRules,
        {
          rule_key: `operator_${Date.now()}`,
          rule_type: inferred.rule_type,
          description: text,
          severity: inferred.field === "never_allow_actions" ? "high" : "medium",
          action_patterns: inferred.patterns,
          capability_patterns: inferred.capabilityPatterns,
          source: "operator",
        },
      ];
      updateOperatingModel.mutate({ ...(operatingModel || {}), rules: nextRules });
      setRulePrompt("");
    };
    const removeOperatingRule = (idx: number) => {
      const nextRules = operatingRules.filter((_, i) => i !== idx);
      updateOperatingModel.mutate({ ...(operatingModel || {}), rules: nextRules });
    };

    const PatternList = ({
      title,
      description,
      field,
      tone,
    }: {
      title: string;
      description: string;
      field: GovernanceActionField | GovernanceCapabilityField;
      tone: "orange" | "red" | "green";
    }) => {
      const patterns = policy[field] || [];
      const bg = tone === "red" ? "rgba(241,221,219,0.55)" : tone === "green" ? "rgba(228,239,232,0.55)" : "rgba(249,244,236,0.7)";
      const border = tone === "red" ? "rgba(209,139,134,0.35)" : tone === "green" ? "rgba(84,161,118,0.3)" : "rgba(251,146,60,0.35)";
      return (
        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 8 }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 800, color: "#1c1917" }}>{title}</div>
              <p style={{ fontSize: 12, color: "#78716c", margin: "4px 0 0", lineHeight: 1.5 }}>{description}</p>
            </div>
            <Chip variant={tone} size="sm">{patterns.length}</Chip>
          </div>
          {patterns.length === 0 ? (
            <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>{copy.noPatterns}</p>
          ) : (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {patterns.map((pattern) => (
                <span key={pattern} style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 8px", borderRadius: 999, background: bg, border: `1px solid ${border}`, fontSize: 12, fontWeight: 700, color: "#44403c" }}>
                  {_friendlyCodeLabel(pattern)}
                  <button
                    type="button"
                    onClick={() => removePattern(field, pattern)}
                    disabled={updateGovernance.isPending}
                    style={{ border: "none", background: "transparent", color: "#78716c", cursor: "pointer", fontWeight: 900 }}
                    title={copy.remove}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
        </GlassCard>
      );
    };
    const ruleDescription = (rule: any) => (
      rule.description || rule.summary || rule.title || _humanize(rule.rule_key)
    );
    const ruleActionPatterns = (rule: any) => _uniqueStrings([
      ...(Array.isArray(rule.action_patterns) ? rule.action_patterns : []),
      ...(Array.isArray(rule.action_keys) ? rule.action_keys : []),
      ...(Array.isArray(rule.capability_patterns) ? rule.capability_patterns : []),
    ]);
    const enforcementMeta = (pattern: string, rule?: any) => {
      if ((policy.never_allow_actions || []).includes(pattern) || (policy.never_allow_capabilities || []).includes(pattern)) {
        return {
          label: copy.patternNeverAllow,
          bg: "rgba(241,221,219,0.62)",
          border: "rgba(209,139,134,0.4)",
          color: "#883a35",
        };
      }
      if ((policy.auto_approve_actions || []).includes(pattern) || (policy.auto_approve_capabilities || []).includes(pattern)) {
        return {
          label: copy.patternAutoApprove,
          bg: "rgba(228,239,232,0.62)",
          border: "rgba(84,161,118,0.35)",
          color: "#3a6047",
        };
      }
      const enforcement = String(rule?.enforcement || rule?.rule_type || "").toLowerCase();
      if (enforcement === "blocked" || enforcement === "deny" || enforcement === "never_allow") {
        return {
          label: copy.patternNeverAllow,
          bg: "rgba(241,221,219,0.62)",
          border: "rgba(209,139,134,0.4)",
          color: "#883a35",
        };
      }
      if (enforcement === "required_context" || enforcement === "context_required") {
        return {
          label: copy.patternRequiresContext,
          bg: "rgba(227,233,241,0.62)",
          border: "rgba(138,169,209,0.34)",
          color: "#3f57a0",
        };
      }
      return {
        label: copy.patternNeedsApproval,
        bg: "rgba(249,244,236,0.8)",
        border: "rgba(251,146,60,0.38)",
        color: "#7c4a2e",
      };
    };

    const ruleSettingsModal = (
      <Modal
        open={showRuleSettings}
        onClose={() => setShowRuleSettings(false)}
        title={copy.settingsTitle}
        maxWidth="920px"
        footer={
          <Button variant="primary" onClick={() => setShowRuleSettings(false)}>
            {copy.done}
          </Button>
        }
      >
        <p style={{ fontSize: 13, color: "#78716c", margin: "0 0 18px", lineHeight: 1.6 }}>
          {copy.settingsSubtitle}
        </p>
        <div style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 10,
          padding: "10px 12px",
          borderRadius: 12,
          border: "1px solid rgba(95,146,138,0.18)",
          background: "rgba(242,246,245,0.62)",
          color: "#57534e",
          fontSize: 12,
          lineHeight: 1.5,
          marginBottom: 18,
        }}>
          <IconInfo size={14} className="text-manor-700" />
          <span>{copy.actionKeyHint}</span>
        </div>

        <div style={{ marginBottom: 18 }}>
          <label style={{ display: "block", fontSize: 12, color: "#78716c", fontWeight: 800, marginBottom: 8 }}>
            {copy.maxRisk}
          </label>
          <select
            className="manor-input"
            value={policy.max_risk_level}
            onChange={(e) => commitPolicy({ ...policy, max_risk_level: e.target.value as GovernancePolicy["max_risk_level"] }, t("page.workspace_detail.change_max_risk_level"))}
            disabled={updateGovernance.isPending}
          >
            {copy.riskOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>

        <div style={{ marginBottom: 12 }}>
          <div style={SECTION_TITLE}>{copy.quickTemplates}</div>
          <p style={{ fontSize: 12, color: "#a8a29e", margin: "-8px 0 10px" }}>{copy.quickTemplatesHint}</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <Button variant="outline" size="sm" disabled={updateGovernance.isPending} onClick={() => addPatterns("hitl_required_actions", ["social_post.publish"], t("page.workspace_detail.rule_copy.summary_require_social_posts"))}>
              {copy.templates.social}
            </Button>
            <Button variant="outline" size="sm" disabled={updateGovernance.isPending} onClick={() => addPatterns("hitl_required_actions", ["email.send"], t("page.workspace_detail.rule_copy.summary_require_email_send"))}>
              {copy.templates.email}
            </Button>
            <Button variant="outline" size="sm" disabled={updateGovernance.isPending} onClick={() => addPatterns("hitl_required_actions", ["external_message.send"], t("page.workspace_detail.rule_copy.summary_require_external_messages"))}>
              {copy.templates.message}
            </Button>
            <Button variant="outline" size="sm" disabled={updateGovernance.isPending} onClick={() => addPatterns("never_allow_actions", ["social_post.delete", "email.delete"], t("page.workspace_detail.rule_copy.summary_block_external_deletes"))}>
              {copy.templates.delete}
            </Button>
            <Button variant="outline" size="sm" disabled={updateGovernance.isPending} onClick={() => addPatterns("never_allow_actions", ["workspace.file.modify", "workspace.file.delete", "workspace.file.write"], t("page.workspace_detail.rule_copy.summary_allow_only_new_files"))}>
              {copy.templates.files}
            </Button>
          </div>
        </div>

        <div style={SECTION_TITLE}>{copy.actionPatterns}</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12 }}>
          <PatternList
            title={copy.patternNeedsApproval}
            description={copy.patternNeedsApprovalDesc}
            field="hitl_required_actions"
            tone="orange"
          />
          <PatternList
            title={copy.patternNeverAllow}
            description={copy.patternNeverAllowDesc}
            field="never_allow_actions"
            tone="red"
          />
          <PatternList
            title={copy.patternAutoApprove}
            description={copy.patternAutoApproveDesc}
            field="auto_approve_actions"
            tone="green"
          />
          <PatternList
            title={`${copy.patternNeedsApproval} · capability`}
            description={copy.patternNeedsApprovalDesc}
            field="hitl_required_capabilities"
            tone="orange"
          />
          <PatternList
            title={`${copy.patternNeverAllow} · capability`}
            description={copy.patternNeverAllowDesc}
            field="never_allow_capabilities"
            tone="red"
          />
          <PatternList
            title={`${copy.patternAutoApprove} · capability`}
            description={copy.patternAutoApproveDesc}
            field="auto_approve_capabilities"
            tone="green"
          />
        </div>
      </Modal>
    );

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
            <div>
              <div style={SECTION_TITLE}>{copy.title}</div>
              <p style={{ fontSize: 13, color: "#78716c", margin: "-8px 0 0", lineHeight: 1.6, maxWidth: 760 }}>
                {copy.subtitle}
              </p>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Chip variant="slate" size="md">{copy.revision} {governancePolicy?.revision ?? 0}</Chip>
              <Button variant="outline" size="sm" onClick={() => setShowRuleSettings(true)}>
                {copy.settings}
              </Button>
            </div>
          </div>
        </GlassCard>

        <GlassCard hoverable={false}>
          <div style={SECTION_TITLE}>{copy.addTitle}</div>
          <Textarea
            rows={3}
            value={rulePrompt}
            onChange={(e) => setRulePrompt(e.target.value)}
            placeholder={copy.placeholder}
            disabled={updateGovernance.isPending || updateOperatingModel.isPending}
          />
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, marginTop: 10 }}>
            <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>
              {copy.note}
            </p>
            <Button
              variant="primary"
              size="sm"
              onClick={addNaturalRule}
              disabled={!rulePrompt.trim() || updateGovernance.isPending || updateOperatingModel.isPending}
              loading={updateGovernance.isPending || updateOperatingModel.isPending}
            >
              {copy.addRule}
            </Button>
          </div>
          <div style={{ marginTop: 14 }}>
            <div style={{ fontSize: 11, color: "#a8a29e", fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
              {copy.examples}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {copy.examplesList.map((example) => (
                <Button key={example.label} variant="outline" size="sm" onClick={() => setRulePrompt(example.text)}>
                  {example.label}
                </Button>
              ))}
            </div>
          </div>
        </GlassCard>

        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div style={SECTION_TITLE}>{copy.currentTitle}</div>
            <span style={{ fontSize: 12, color: "#a8a29e" }}>{operatingRules.length} {copy.ruleCount}</span>
          </div>
          {operatingRules.length === 0 ? (
            <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>
              {copy.empty}
            </p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {operatingRules.map((rule, idx) => (
                <div key={rule.rule_key || idx} style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: 12, borderRadius: 12, background: "rgba(250,250,249,0.8)", border: "1px solid rgba(28,25,23,0.06)" }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#1c1917", lineHeight: 1.45 }}>{ruleDescription(rule)}</div>
                    {ruleActionPatterns(rule).length > 0 ? (
                      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, marginTop: 8 }}>
                        <span style={{ fontSize: 11, color: "#78716c", fontWeight: 800 }}>
                          {copy.enforcedAs}
                        </span>
                        {ruleActionPatterns(rule).map((pattern: string) => {
                          const meta = enforcementMeta(pattern, rule);
                          return (
                            <span
                              key={pattern}
                              style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 5,
                                padding: "4px 7px",
                                borderRadius: 999,
                                border: `1px solid ${meta.border}`,
                                background: meta.bg,
                                color: meta.color,
                                fontSize: 11,
                                fontWeight: 800,
                              }}
                              title={copy.actionKeyHint}
                            >
                              {meta.label}
                              <span style={{ color: "#78716c" }}>{"→"}</span>
                              <span>{_friendlyCodeLabel(pattern)}</span>
                            </span>
                          );
                        })}
                      </div>
                    ) : (
                      <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 6 }}>
                        {copy.noEnforcement}
                      </div>
                    )}
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => removeOperatingRule(idx)}>
                    <span style={{ color: "#d65f59", fontWeight: 800 }}>{copy.remove}</span>
                  </Button>
                </div>
              ))}
            </div>
          )}
        </GlassCard>

        {ruleSettingsModal}
      </div>
    );
  }

  function renderGoals() {
    const rawGoalsList: any[] = Array.isArray(goals) ? goals : (goals as any)?.items ?? [];
    const goalsList = _dedupeGoals(rawGoalsList);

    const openEdit = (g: any) => {
      setEditingGoal(g);
      setGoalForm({
        title: g.title || "",
        description: g.description || "",
        target_value: g.target_value ?? "",
        deadline: g.deadline || "",
        status: g.status || "active",
        measurement_cadence: g.measurement_cadence || "",
        priority: g.priority ?? 3,
      });
    };

    const paceColors: Record<string, { bg: string; fg: string }> = {
      on_track: { bg: "#e4efe8", fg: "#3d7351" },
      ahead: { bg: "#e3e9f1", fg: "#3f57a0" },
      achieved: { bg: "#dceae3", fg: "#065f46" },
      behind: { bg: "#f3ecd6", fg: "#76502c" },
      at_risk: { bg: "#f1dddb", fg: "#c14a44" },
      tracking: { bg: "#e0f2f1", fg: "#436b65" },
      paused: { bg: "#f5f5f4", fg: "#78716c" },
      unknown: { bg: "#f5f5f4", fg: "#78716c" },
    };

    const computeProgress = (g: any) => {
      return _goalProgressPercent(g);
    };

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {workspaceId && (
          <GlassCard hoverable={false}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 16 }}>
              <div style={SECTION_TITLE}>{t("page.workspace_detail.goal_execution_canvas")}</div>
              <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.55, maxWidth: 760 }}>
                {t("page.workspace_detail.goal_execution_canvas_desc")}
              </div>
            </div>
            <WorkspaceGoalGraph workspaceId={workspaceId} />
          </GlassCard>
        )}

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={SECTION_TITLE}>{t("page.workspace_detail.goal_list")}{goalsList.length})</div>
        </div>

        {goalsLoading ? (
          <div style={{ textAlign: "center", padding: 32, color: "#a8a29e", fontSize: 13 }}>{t("page.workspace_detail.loading_goals")}</div>
        ) : goalsList.length === 0 ? (
          <EmptyState
            title={t("page.workspace_detail.no_goals_yet")}
            description={t("page.workspace_detail.goals_are_created_when_the_workspace_is_set_up_t")}
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {goalsList.map((g: any) => {
              const progress = computeProgress(g);
              const hasCurrentValue = g.current_value !== null && g.current_value !== undefined;
              const current = Number(g.current_value ?? 0);
              const target = Number(g.target_value ?? 0);
              const rawPace = String(g.pace_status || "").toLowerCase();
              const pace = rawPace && rawPace !== "unknown" ? rawPace : (g.status === "achieved" ? "achieved" : g.status === "paused" ? "paused" : "tracking");
              const paceLabel = pace === "tracking" ? t("page.workspace_detail.tracking") : pace.replace(/_/g, " ");
              const pc = paceColors[pace] || paceColors.unknown;
              const isEditing = editingGoal?.id === g.id;
              const linkedTaskCount = Array.isArray(g.linked_task_ids) ? g.linked_task_ids.length : 0;
              const taskCounts = g.task_status_counts || {};
              const completedTaskCount = Number(taskCounts.completed || 0);
              const executionLabel = linkedTaskCount > 0
                ? `${completedTaskCount}/${linkedTaskCount} ${linkedTaskCount === 1 ? "task" : "tasks"} complete`
                : "";

              return (
                <GlassCard key={g.id} hoverable={false}>
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {/* Header */}
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "stretch", gap: 8 }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 4, width: "100%" }}>
                        <div style={{ fontSize: 14, fontWeight: 700, color: "#1c1917", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {g.title}
                        </div>
                        <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
                          <span style={{
                            fontSize: 9, fontWeight: 700, padding: "2px 7px", borderRadius: 5,
                            background: pc.bg, color: pc.fg, textTransform: "uppercase",
                          }}>
                            {paceLabel}
                          </span>
                          <Button variant="ghost" size="sm" onClick={() => isEditing ? setEditingGoal(null) : openEdit(g)}
                            style={{ padding: "2px 8px", height: 24, fontSize: 11 }}>
                            {isEditing ? t("page.flows.close") : t("action.edit")}
                          </Button>
                        </div>
                      </div>

                      {g.description && !isEditing && (
                        <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.4, marginBottom: 6 }}>
                          {g.description}
                        </div>
                      )}

                      {/* Progress bar + values */}
                      <div style={{ marginBottom: 6 }}>
                        <div style={{ height: 5, borderRadius: 3, background: "#e7e5e4", overflow: "hidden" }}>
                          <div style={{
                            height: "100%", borderRadius: 3,
                            background: pace === "at_risk" ? "#d65f59" : pace === "behind" ? "#cf9b44" : pace === "achieved" ? "#4f9c84" : "#5f928a",
                            width: `${progress}%`, transition: "width 0.5s",
                          }} />
                        </div>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#a8a29e", marginTop: 3 }}>
                          <span>{t("page.workspace_detail.current")} <strong style={{ color: "#1c1917" }}>{hasCurrentValue ? current.toLocaleString() : t("page.workspace_detail.not_measured_yet")}</strong></span>
                          <span>{t("page.workspace_detail.target")} <strong style={{ color: "#1c1917" }}>{target.toLocaleString()}</strong></span>
                        </div>
                      </div>

                      {/* Meta chips */}
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                        {g.metric_key && <Chip variant="slate" size="sm">{formatUserFacingLabel(g.metric_key)}</Chip>}
                        {g.measurement_cadence && <Chip variant="teal" size="sm">{g.measurement_cadence}</Chip>}
                        {g.deadline && <Chip variant="orange" size="sm">{t("page.task_process.due")} {g.deadline}</Chip>}
                        <Chip variant={g.status === "active" ? "green" : g.status === "achieved" ? "blue" : "slate"} size="sm">{g.status}</Chip>
                        {linkedTaskCount > 0 && (
                          <Chip variant={completedTaskCount >= linkedTaskCount ? "green" : "blue"} size="sm">
                            Execution {executionLabel}
                          </Chip>
                        )}
                        {g.priority && g.priority !== 3 && <Chip variant="red" size="sm">{t("page.workspace_detail.p")}{g.priority}</Chip>}
                      </div>
                    </div>
                  </div>

                  {/* Inline edit panel */}
                  {isEditing && (
                    <div style={{
                      marginTop: 14, paddingTop: 14,
                      borderTop: "1px solid rgba(28,25,23,0.06)",
                    }}>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 10, marginBottom: 10 }}>
                        <div>
                          <label style={{ fontSize: 11, fontWeight: 600, color: "#78716c", display: "block", marginBottom: 3 }}>{t("page.team_people.title")}</label>
                          <input className="manor-input" value={goalForm.title} onChange={(e) => setGoalForm({ ...goalForm, title: e.target.value })} />
                        </div>
                        <div>
                          <label style={{ fontSize: 11, fontWeight: 600, color: "#78716c", display: "block", marginBottom: 3 }}>{t("page.workspace_detail.target_value")}</label>
                          <input className="manor-input" type="number" value={goalForm.target_value} onChange={(e) => setGoalForm({ ...goalForm, target_value: e.target.value })} />
                        </div>
                      </div>
                      <div style={{ marginBottom: 10 }}>
                        <label style={{ fontSize: 11, fontWeight: 600, color: "#78716c", display: "block", marginBottom: 3 }}>{t("page.task_collections.description")}</label>
                        <textarea className="manor-input" rows={2} value={goalForm.description} onChange={(e) => setGoalForm({ ...goalForm, description: e.target.value })} />
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 10, marginBottom: 12 }}>
                        <div>
                          <label style={{ fontSize: 11, fontWeight: 600, color: "#78716c", display: "block", marginBottom: 3 }}>{t("page.workspace_detail.deadline")}</label>
                          <input className="manor-input" type="date" value={goalForm.deadline} onChange={(e) => setGoalForm({ ...goalForm, deadline: e.target.value })} />
                        </div>
                        <div>
                          <label style={{ fontSize: 11, fontWeight: 600, color: "#78716c", display: "block", marginBottom: 3 }}>{t("page.workspace_detail.cadence_2")}</label>
                          <select className="manor-input" value={goalForm.measurement_cadence} onChange={(e) => setGoalForm({ ...goalForm, measurement_cadence: e.target.value })}>
                            <option value="">{t("page.workspace_detail.none")}</option>
                            <option value="hourly">{t("page.workspace_detail.hourly")}</option>
                            <option value="daily">{t("page.workspace_detail.daily")}</option>
                            <option value="weekly">{t("page.workspace_detail.weekly")}</option>
                            <option value="monthly">{t("page.workspace_detail.monthly")}</option>
                          </select>
                        </div>
                        <div>
                          <label style={{ fontSize: 11, fontWeight: 600, color: "#78716c", display: "block", marginBottom: 3 }}>{t("page.agent_dashboard.status")}</label>
                          <select className="manor-input" value={goalForm.status} onChange={(e) => setGoalForm({ ...goalForm, status: e.target.value })}>
                            <option value="active">{t("page.workspaces.filter_active")}</option>
                            <option value="paused">{t("page.workspaces.filter_paused")}</option>
                            <option value="achieved">{t("page.workspace_detail.achieved")}</option>
                            <option value="abandoned">{t("page.workspace_detail.abandoned")}</option>
                          </select>
                        </div>
                      </div>
                      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                        <Button variant="outline" size="sm" onClick={() => setEditingGoal(null)}>{t("action.cancel")}</Button>
                        <Button variant="primary" size="sm" loading={updateGoalMut.isPending}
                          onClick={() => {
                            const payload: any = {};
                            if (goalForm.title !== g.title) payload.title = goalForm.title;
                            if (goalForm.description !== (g.description || "")) payload.description = goalForm.description;
                            if (String(goalForm.target_value) !== String(g.target_value ?? "")) payload.target_value = Number(goalForm.target_value);
                            if (goalForm.deadline !== (g.deadline || "")) payload.deadline = goalForm.deadline || null;
                            if (goalForm.status !== g.status) payload.status = goalForm.status;
                            if (goalForm.measurement_cadence !== (g.measurement_cadence || "")) payload.measurement_cadence = goalForm.measurement_cadence || null;
                            if (Object.keys(payload).length === 0) { setEditingGoal(null); return; }
                            updateGoalMut.mutate({ id: g.id, data: payload });
                          }}
                        >{t("action.save")}</Button>
                      </div>
                    </div>
                  )}
                </GlassCard>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  function renderActivity() {
    const items = activityFeed || [];
    return (
      <div className="workspace-activity-feed" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div className="workspace-activity-feed-title" style={SECTION_TITLE}>{t("page.workspace_detail.activity_feed")}</div>
        {items.length === 0 ? (
          <EmptyState title={t("page.workspace_detail.no_activity")} description={t("page.workspace_detail.no_activity_recorded_for_this_workspace_yet")} />
        ) : (
          <GlassCard hoverable={false} className="workspace-activity-feed-card !p-0">
            {items.map((evt: WorkspaceActivity, i: number) => {
              const taskSummaries = _activityTaskSummaries(evt);
              const activitySummary = _activitySummaryText(evt);
              const actorLabel = _activityActorLabel(evt);
              return (
                <div
                  key={evt.id || i}
                  className="workspace-activity-feed-item"
                  style={{
                    padding: "14px 20px",
                    borderBottom: i < items.length - 1 ? "1px solid #fafaf9" : "none",
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 14,
                  }}
                >
                  <div className="workspace-activity-feed-dot" style={{
                    width: 8, height: 8, borderRadius: "50%", background: "#436b65",
                    marginTop: 7, flexShrink: 0,
                  }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="workspace-activity-feed-meta" style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
                      <Chip variant={_eventTypeChipVariant(evt.event_type)} size="sm">
                        {_activityEventLabel(evt.event_type)}
                      </Chip>
                      <span className="workspace-activity-feed-time" style={{ fontSize: 11, color: "#a8a29e" }}>{relativeTime(evt.created_at)}</span>
                      {actorLabel && (
                        <span className="workspace-activity-feed-actor" style={{ fontSize: 11, color: "#78716c", fontWeight: 650 }}>
                          {t("page.activity.by")} {actorLabel}
                        </span>
                      )}
                    </div>
                    <div className="workspace-activity-feed-summary" style={{ fontSize: 13, fontWeight: 500, color: "#44403c", lineHeight: 1.5 }}>
                      {activitySummary}
                    </div>
                    {taskSummaries.length > 0 && (
                      <div className="workspace-activity-task-list" style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
                        {taskSummaries.slice(0, 3).map((task: any) => {
                          const files = Array.isArray(task.files) ? task.files.filter(Boolean).slice(0, 4) : [];
                          return (
                            <div
                              key={task.id}
                              className="workspace-activity-task-card"
                              style={{
                                border: "1px solid rgba(226,232,240,0.72)",
                                background: "rgba(248,250,252,0.62)",
                                borderRadius: 10,
                                padding: "9px 10px",
                              }}
                            >
                              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                                <Link
                                  to={`/tasks/${encodeURIComponent(String(task.id))}`}
                                  className="workspace-activity-task-title"
                                  style={{ color: "#0f172a", fontSize: 13, fontWeight: 750, textDecoration: "none" }}
                                >
                                  {formatUserFacingText(task.title || task.id)}
                                </Link>
                                {task.status && (
                                  <Chip variant={_taskStatusChipVariant(String(task.status))} size="sm">
                                    {_activityStatusLabel(task.status)}
                                  </Chip>
                                )}
                                {task.owner_service_key && (
                                  <span className="workspace-activity-task-owner" style={{ fontSize: 11, color: "#78716c", fontWeight: 650 }}>
                                    {formatUserFacingText(_humanize(task.owner_service_key))}
                                  </span>
                                )}
                              </div>
                              {files.length > 0 && (
                                <div className="workspace-activity-file-list" style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
                                  {files.map((file: any, idx: number) => {
                                    const href = _activityFileHref(file);
                                    const label = file.label || file.name || file.filename || file.fs_path || `File ${idx + 1}`;
                                    const content = (
                                      <>
                                        <IconDocument size={13} style={{ flexShrink: 0 }} />
                                        <span style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                          {String(label)}
                                        </span>
                                      </>
                                    );
                                    const style = {
                                      display: "inline-flex",
                                      alignItems: "center",
                                      gap: 6,
                                      padding: "5px 8px",
                                      borderRadius: 8,
                                      border: "1px solid rgba(15,118,110,0.16)",
                                      background: "rgba(240,253,250,0.7)",
                                      color: "#436b65",
                                      fontSize: 12,
                                      fontWeight: 650,
                                      textDecoration: "none",
                                      minWidth: 0,
                                    } as const;
                                    if (!href) return <span key={`${task.id}-file-${idx}`} className="workspace-activity-file-chip" style={style}>{content}</span>;
                                    if (href.startsWith("http://") || href.startsWith("https://")) {
                                      return <a key={`${task.id}-file-${idx}`} className="workspace-activity-file-chip" href={href} target="_blank" rel="noopener noreferrer" style={style}>{content}</a>;
                                    }
                                    return <Link key={`${task.id}-file-${idx}`} className="workspace-activity-file-chip" to={href} style={style}>{content}</Link>;
                                  })}
                                </div>
                              )}
                            </div>
                          );
                        })}
                        {taskSummaries.length > 3 && (
                          <div className="workspace-activity-more-count" style={{ fontSize: 12, color: "#a8a29e", fontWeight: 650 }}>
                            +{taskSummaries.length - 3} more tasks
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </GlassCard>
        )}
      </div>
    );
  }

  function renderLearning() {
    const candidates: AgentLearningCandidate[] = learningCandidates || [];
    const evidence: RuntimeEvidence[] = runtimeEvidence || [];
    const openCandidates = candidates.filter((c) => c.status === "proposed");
    const recentCandidates = candidates.filter((c) => c.status !== "proposed");
    const metricSummary = (ev: RuntimeEvidence) => {
      const metrics = ev.metrics || {};
      const pieces = [
        metrics.total_tokens ? `${metrics.total_tokens.toLocaleString()} tokens` : "",
        metrics.rounds ? `${metrics.rounds} rounds` : "",
        metrics.tool_call_count ? `${metrics.tool_call_count} tools` : "",
      ].filter(Boolean);
      return pieces.join(" · ");
    };
    const payloadPreview = (payload: Record<string, any>) => {
      const content = payload?.content || payload?.seed_prompt || "";
      if (content) return _learningText(content).slice(0, 220);
      const target = _learningTargetLabel(payload?.apply_target);
      return target ? `Updates ${target}` : "";
    };

    if (learningLoading || evidenceLoading) {
      return (
        <div style={{ display: "grid", placeItems: "center", padding: 48 }}>
          <LoadingSpinner />
        </div>
      );
    }

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
            <div>
              <div style={SECTION_TITLE}>{t("page.workspace_detail.agent_learning")}</div>
              <div style={{ fontSize: 13, color: "#78716c", lineHeight: 1.6, maxWidth: 760 }}>
                {t("page.workspace_detail.agent_learning_desc")}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Chip variant={openCandidates.length ? "orange" : "green"} size="sm">
                {t("page.workspace_detail.proposed_count").replace("{count}", String(openCandidates.length))}
              </Chip>
              <Chip variant="blue" size="sm">
                {t("page.workspace_detail.evidence_count").replace("{count}", String(evidence.length))}
              </Chip>
            </div>
          </div>
        </GlassCard>

        {renderWorkspaceEvaluationCard()}

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))", gap: 16 }}>
          <GlassCard hoverable={false}>
            <div style={{ ...SECTION_TITLE, marginBottom: 12 }}>{t("page.workspace_detail.review_queue")}</div>
            {openCandidates.length === 0 ? (
              <EmptyState
                title={t("page.workspace_detail.no_proposed_learning")}
                description={t("page.workspace_detail.no_proposed_learning_desc")}
              />
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {openCandidates.map((candidate) => (
                  <div key={candidate.id} style={{ border: "1px solid rgba(28,25,23,0.06)", borderRadius: 16, padding: 14, background: "rgba(250,250,249,0.72)" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                      <Chip variant="purple" size="sm">{_learningKindLabel(candidate.candidate_type)}</Chip>
                      <Chip variant={_riskChipVariant(candidate.risk_level)} size="sm">{_learningRiskLabel(candidate.risk_level)}</Chip>
                      <Chip variant="slate" size="sm">{Math.round((candidate.confidence || 0) * 100)}%</Chip>
                    </div>
                    <div style={{ fontSize: 14, fontWeight: 800, color: "#0f172a", marginBottom: 6 }}>{_learningText(candidate.title)}</div>
                    <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5, marginBottom: 8 }}>{_learningText(candidate.summary)}</div>
                    {payloadPreview(candidate.payload) && (
                      <div style={{ fontSize: 11, color: "#a8a29e", lineHeight: 1.45, marginBottom: 12 }}>
                        {payloadPreview(candidate.payload)}
                      </div>
                    )}
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      <Button
                        size="sm"
                        variant="primary"
                        loading={resolveLearningCandidate.isPending}
                        onClick={() => resolveLearningCandidate.mutate({ id: candidate.id, status: "accepted" })}
                      >
                        {t("page.workspace_detail.mark_accepted")}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        loading={resolveLearningCandidate.isPending}
                        onClick={() => resolveLearningCandidate.mutate({ id: candidate.id, status: "rejected" })}
                      >
                        {t("action.reject")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        loading={resolveLearningCandidate.isPending}
                        onClick={() => resolveLearningCandidate.mutate({ id: candidate.id, status: "archived" })}
                      >
                        {t("action.archive")}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </GlassCard>

          <GlassCard hoverable={false}>
            <div style={{ ...SECTION_TITLE, marginBottom: 12 }}>{t("page.workspace_detail.recent_evidence")}</div>
            {evidence.length === 0 ? (
              <EmptyState
                title={t("page.workspace_detail.no_runtime_evidence")}
                description={t("page.workspace_detail.no_runtime_evidence_desc")}
              />
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {evidence.slice(0, 12).map((ev) => {
                  const metrics = metricSummary(ev);
                  return (
                    <div key={ev.id} style={{ padding: "12px 0", borderBottom: "1px solid #f5f5f4" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center", marginBottom: 6 }}>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          <Chip variant={_taskStatusChipVariant(ev.status)} size="sm">{_activityStatusLabel(ev.status)}</Chip>
                          <Chip variant="slate" size="sm">{_learningKindLabel(ev.evidence_type)}</Chip>
                        </div>
                        <span style={{ fontSize: 11, color: "#a8a29e", whiteSpace: "nowrap" }}>{relativeTime(ev.created_at)}</span>
                      </div>
                      <div style={{ fontSize: 13, fontWeight: 650, color: "#44403c", lineHeight: 1.45 }}>{_learningText(ev.summary)}</div>
                      {metrics && (
                        <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 4 }}>{metrics}</div>
                      )}
                      {Array.isArray(ev.details?.tool_calls_made) && ev.details.tool_calls_made.length > 0 && (
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 8 }}>
                          {ev.details.tool_calls_made.slice(0, 5).map((tool: string, i: number) => (
                            <Chip key={`${ev.id}-${tool}-${i}`} variant="teal" size="sm">{_learningToolLabel(tool)}</Chip>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </GlassCard>
        </div>

        {recentCandidates.length > 0 && (
          <GlassCard hoverable={false}>
            <div style={{ ...SECTION_TITLE, marginBottom: 12 }}>{t("page.workspace_detail.reviewed_candidates")}</div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 10 }}>
              {recentCandidates.slice(0, 8).map((candidate) => {
                const applyStatus = String(candidate.resolution?.apply_status || "");
                return (
                  <div key={candidate.id} style={{ border: "1px solid #edf2f7", borderRadius: 14, padding: 12 }}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                      <Chip variant={_taskStatusChipVariant(candidate.status)} size="sm">{_activityStatusLabel(candidate.status)}</Chip>
                      {applyStatus && <Chip variant="blue" size="sm">{_activityStatusLabel(applyStatus)}</Chip>}
                      <Chip variant="slate" size="sm">{_learningKindLabel(candidate.candidate_type)}</Chip>
                    </div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#44403c", marginBottom: 4 }}>{_learningText(candidate.title)}</div>
                    <div style={{ fontSize: 11, color: "#a8a29e" }}>{relativeTime(candidate.updated_at || candidate.created_at)}</div>
                    {candidate.status === "accepted" && applyStatus !== "queued" && (
                      <div style={{ marginTop: 10 }}>
                        <Button
                          size="sm"
                          variant="primary"
                          loading={applyLearningCandidate.isPending}
                          onClick={() => applyLearningCandidate.mutate(candidate.id)}
                        >
                          {applyStatus === "failed"
                            ? t("page.workspace_detail.retry_apply")
                            : t("page.workspace_detail.queue_apply")}
                        </Button>
                      </div>
                    )}
                    {candidate.status === "applied" && candidate.resolution?.applied_result?.kind && (
                      <div style={{ fontSize: 11, color: "#78716c", marginTop: 8 }}>
                        {t("page.workspace_detail.applied_to").replace(
                          "{target}",
                          String(candidate.resolution.applied_result.kind).replace(/_/g, " "),
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </GlassCard>
        )}
      </div>
    );
  }

  function renderSettings() {
    // Commit-on-blur helper for top-level Workspace columns.
    const commitWs = (key: keyof Workspace, newValue: any) => {
      if (!canManageWs) return;
      const old = (ws as any)[key];
      const oldStr = old == null ? "" : String(old);
      const newStr = newValue == null ? "" : String(newValue);
      if (newStr === oldStr) return;
      updateWorkspace.mutate({ [key]: newValue } as Partial<Workspace>);
    };

    const heartbeatOn = !!(heartbeatStatus?.enabled ?? ws.heartbeat_enabled);
    const retryPolicy = (((ws.settings as any)?.execution_policy || {}).retry_policy || {}) as Record<string, any>;
    const runtimeLearningPolicy = (((ws.settings as any)?.runtime_learning || {})) as Record<string, any>;
    const workspaceLearningEnabled = runtimeLearningPolicy.enabled !== false;
    const notificationPolicy = (((ws.settings as any)?.notification_policy || {}).task_events || {}) as Record<string, any>;
    const emailPolicy = ((notificationPolicy.email || {}) as Record<string, any>);
    const chatPolicy = ((notificationPolicy.external_chat || {}) as Record<string, any>);
    const notificationEvents = [
      { value: "task.failed", label: t("page.dashboard.failed") },
      { value: "task.hitl_requested", label: t("page.workspace_detail.needs_input") },
      { value: "task.hitl_reminder", label: t("page.workspace_detail.input_reminder") },
      { value: "task.succeeded", label: t("page.workspace_detail.succeeded") },
      { value: "task.retried", label: t("page.workspace_detail.retried") },
    ];
    const defaultExternalEvents = ["task.failed", "task.hitl_requested", "task.hitl_reminder"];
    const settingsServices = ((operatingModel?.services as any[]) || []);
    const settingsGoals = ((operatingModel?.goals as any[]) || []);
    const settingsRules = ((operatingModel?.rules as any[]) || []);
    const settingsAutomations = ((operatingModel?.automations as any[]) || []);
    const baseHeartbeatOptions = [
      { value: "", label: t("page.workspace_detail.none_2") },
      { value: "5m", label: t("page.workspace_detail.every_5_minutes") },
      { value: "15m", label: t("page.workspace_detail.every_15_minutes") },
      { value: "hourly", label: t("page.workspace_detail.hourly") },
      { value: "daily", label: t("page.workspace_detail.daily") },
      { value: "weekly", label: t("page.workspace_detail.weekly") },
    ];
    const heartbeatCadenceOptions =
      ws.heartbeat_cadence && !baseHeartbeatOptions.some((option) => option.value === ws.heartbeat_cadence)
        ? [
            ...baseHeartbeatOptions,
            { value: ws.heartbeat_cadence, label: _formatScheduleLabel(ws.heartbeat_cadence) },
          ]
        : baseHeartbeatOptions;
    const fieldDraftKey = (field: string, value: unknown) =>
      `ws-${ws.id}-${field}-${value == null ? "" : String(value)}`;
    const commitRetryPolicy = (patch: Record<string, any>) => {
      if (!canManageWs) return;
      const nextSettings = { ...(ws.settings || {}) } as Record<string, any>;
      const executionPolicy = { ...((nextSettings.execution_policy as Record<string, any>) || {}) };
      executionPolicy.retry_policy = { ...(retryPolicy || {}), ...patch };
      nextSettings.execution_policy = executionPolicy;
      updateWorkspace.mutate({ settings: nextSettings } as Partial<Workspace>);
    };
    const commitRuntimeLearning = (enabled: boolean) => {
      if (!canManageWs) return;
      const nextSettings = { ...(ws.settings || {}) } as Record<string, any>;
      nextSettings.runtime_learning = { ...(runtimeLearningPolicy || {}), enabled };
      updateWorkspace.mutate({ settings: nextSettings } as Partial<Workspace>);
    };
    const commitRetryNumber = (key: string, value: string, fallback: number, min = 0, max = 86400) => {
      const trimmed = value.trim();
      const parsed = trimmed === "" ? fallback : Number(trimmed);
      if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
        toast.error(
          t("page.workspace_detail.invalid_retry_policy"),
          t("page.workspace_detail.retry_policy_integer_range")
            .replace("{min}", String(min))
            .replace("{max}", String(max)),
        );
        return;
      }
      if (parsed === (retryPolicy[key] ?? fallback)) return;
      commitRetryPolicy({ [key]: parsed });
    };
    const commitNotificationPolicy = (channel: "email" | "external_chat", patch: Record<string, any>) => {
      if (!canManageWs) return;
      const nextSettings = { ...(ws.settings || {}) } as Record<string, any>;
      const notificationRoot = { ...((nextSettings.notification_policy as Record<string, any>) || {}) };
      const taskEvents = { ...(((notificationRoot.task_events || {}) as Record<string, any>)) };
      taskEvents[channel] = { ...(((taskEvents[channel] || {}) as Record<string, any>)), ...patch };
      notificationRoot.task_events = taskEvents;
      nextSettings.notification_policy = notificationRoot;
      updateWorkspace.mutate({ settings: nextSettings } as Partial<Workspace>);
    };
    const toggleNotificationEvent = (
      channel: "email" | "external_chat",
      eventName: string,
      checked: boolean,
    ) => {
      const current = channel === "email" ? emailPolicy : chatPolicy;
      const events = new Set<string>(((current.events as string[]) || defaultExternalEvents).filter(Boolean));
      if (checked) events.add(eventName);
      else events.delete(eventName);
      commitNotificationPolicy(channel, { events: Array.from(events) });
    };

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
        {/* ── Identity ── */}
        <GlassCard hoverable={false}>
          <div style={SECTION_TITLE}>{t("page.workspace_detail.identity")}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12 }}>
            <_Field label={t("page.task_collections.name")}>
              <input
                className="manor-input"
                defaultValue={ws.name || ""}
                key={fieldDraftKey("name", ws.name)}
                onBlur={(e) => commitWs("name", e.target.value)}
              />
            </_Field>
            <_Field label={t("page.workspaces.category")}>
              <input
                className="manor-input"
                defaultValue={ws.category || ""}
                key={fieldDraftKey("category", ws.category)}
                onBlur={(e) => commitWs("category", e.target.value || null)}
                placeholder={t("page.workspaces.category_placeholder")}
              />
            </_Field>
            <_Field label={t("page.team_people.kind")}>
              <input
                className="manor-input"
                defaultValue={ws.kind || ""}
                key={fieldDraftKey("kind", ws.kind)}
                onBlur={(e) => commitWs("kind", e.target.value || null)}
                placeholder={t("page.workspace_detail.property_project_campaign")}
              />
            </_Field>
            <_Field label={t("page.workspace_detail.identity_label")}>
              <input
                className="manor-input"
                defaultValue={ws.identity_label || ""}
                key={fieldDraftKey("identity_label", ws.identity_label)}
                onBlur={(e) => commitWs("identity_label", e.target.value || null)}
              />
            </_Field>
            <div style={{ gridColumn: "1 / -1" }}>
              <_Field label={t("page.task_collections.description")}>
                <textarea
                  className="manor-input"
                  rows={2}
                  defaultValue={ws.description || ""}
                  key={fieldDraftKey("description", ws.description)}
                  onBlur={(e) => commitWs("description", e.target.value || null)}
                  placeholder={t("page.workspace_detail.what_does_this_workspace_do")}
                />
              </_Field>
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <_Field label={t("page.workspace_detail.cover_image_url")}>
                <input
                  className="manor-input"
                  defaultValue={ws.cover_image_url || ""}
                  key={fieldDraftKey("cover_image_url", ws.cover_image_url)}
                  onBlur={(e) => commitWs("cover_image_url", e.target.value || null)}
                  placeholder={t("page.workspace_detail.https")}
                />
              </_Field>
            </div>
          </div>
        </GlassCard>

        {/* ── Context ── */}
        <GlassCard hoverable={false}>
          <div style={SECTION_TITLE}>{t("page.memories.context")}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12 }}>
            <div style={{ gridColumn: "1 / -1" }}>
              <_Field label={t("page.workspace_detail.operating_context")}>
                <textarea
                  className="manor-input"
                  rows={2}
                  defaultValue={ws.operating_context || ""}
                  key={fieldDraftKey("operating_context", ws.operating_context)}
                  onBlur={(e) => commitWs("operating_context", e.target.value || null)}
                  placeholder={t("page.workspace_detail.where_for_whom_does_this_run")}
                />
              </_Field>
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <_Field label={t("page.workspace_detail.primary_work")}>
                <textarea
                  className="manor-input"
                  rows={2}
                  defaultValue={ws.primary_work || ""}
                  key={fieldDraftKey("primary_work", ws.primary_work)}
                  onBlur={(e) => commitWs("primary_work", e.target.value || null)}
                  placeholder={t("page.workspace_detail.core_responsibilities")}
                />
              </_Field>
            </div>
            <_Field label={t("page.team_people.address")}>
              <input
                className="manor-input"
                defaultValue={ws.address || ""}
                key={fieldDraftKey("address", ws.address)}
                onBlur={(e) => commitWs("address", e.target.value || null)}
              />
            </_Field>
            <_Field label={t("page.workspace_detail.property_type")}>
              <input
                className="manor-input"
                defaultValue={ws.property_type || ""}
                key={fieldDraftKey("property_type", ws.property_type)}
                onBlur={(e) => commitWs("property_type", e.target.value || null)}
              />
            </_Field>
          </div>
        </GlassCard>

        {/* ── Heartbeat ── */}
        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <div style={SECTION_TITLE}>{t("page.workspace_detail.heartbeat")}</div>
            {canManageWs && (
              <Button
                variant={heartbeatOn ? "outline" : "primary"}
                size="sm"
                onClick={() => (heartbeatOn ? heartbeatDisable.mutate() : heartbeatEnable.mutate())}
                loading={heartbeatEnable.isPending || heartbeatDisable.isPending}
              >
                {heartbeatOn ? t("page.workspace_detail.disable") : t("page.workspace_detail.enable")}
              </Button>
            )}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12 }}>
            <_Field label={t("page.workspace_detail.cadence_2")}>
              <Select
                value={ws.heartbeat_cadence || ""}
                onChange={(v) => commitWs("heartbeat_cadence", v || null)}
                placeholder={t("page.workspace_detail.select_cadence")}
                options={heartbeatCadenceOptions}
              />
            </_Field>
            <div style={{ display: "flex", flexDirection: "column", justifyContent: "flex-end", gap: 4 }}>
              <span style={{ fontSize: 12, color: "#a8a29e" }}>
                {t("page.workspace_detail.status")} {heartbeatOn ? t("page.job_logs.enabled") : t("page.job_logs.disabled")}
              </span>
              {ws.last_heartbeat_at && (
                <span style={{ fontSize: 11, color: "#a8a29e" }}>
                  {t("page.workspace_detail.last")} {relativeTime(ws.last_heartbeat_at)}
                </span>
              )}
            </div>
          </div>
        </GlassCard>

        {/* ── Learning ── */}
        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ ...SECTION_TITLE, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <span>{t("page.workspace_detail.runtime_learning_controls")}</span>
                <span style={{
                  display: "inline-flex",
                  alignItems: "center",
                  height: 20,
                  padding: "0 8px",
                  borderRadius: 999,
                  fontSize: 10,
                  fontWeight: 800,
                  color: workspaceLearningEnabled ? "#436b65" : "#92400e",
                  background: workspaceLearningEnabled ? "#f2f6f5" : "#fef3c7",
                  border: `1px solid ${workspaceLearningEnabled ? "#99f6e4" : "#fde68a"}`,
                }}>
                  {workspaceLearningEnabled ? t("page.workspace_detail.learning_on") : t("page.workspace_detail.learning_paused")}
                </span>
              </div>
              <p style={{ fontSize: 13, color: "#78716c", margin: "-4px 0 0", lineHeight: 1.5 }}>
                {t("page.workspace_detail.runtime_learning_desc")}
              </p>
            </div>
            <Toggle
              checked={workspaceLearningEnabled}
              onChange={() => commitRuntimeLearning(!workspaceLearningEnabled)}
              disabled={updateWorkspace.isPending}
              aria-label={t("page.workspace_detail.runtime_learning_controls")}
            />
          </div>
        </GlassCard>

        {/* ── Budget ── */}
        <GlassCard hoverable={false}>
          <div style={SECTION_TITLE}>{t("page.workspace_detail.budget")}</div>
          {budgetStatus && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 150px), 1fr))", gap: 10 }}>
                <div style={{ padding: "10px 12px", borderRadius: 12, background: "rgba(250,250,249,0.7)", border: "1px solid rgba(28,25,23,0.06)" }}>
                  <div style={LABEL}>{t("page.workspace_detail.spent_this_month")}</div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#1c1917" }}>
                    {_formatCredits(budgetStatus.monthly_spent_credits)}
                  </div>
                </div>
                <div style={{ padding: "10px 12px", borderRadius: 12, background: "rgba(250,250,249,0.7)", border: "1px solid rgba(28,25,23,0.06)" }}>
                  <div style={LABEL}>{t("page.workspace_detail.cap")}</div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#1c1917" }}>
                    {budgetStatus.monthly_budget_credits != null
                      ? _formatCredits(budgetStatus.monthly_budget_credits)
                      : t("page.workspace_detail.no_cap")}
                  </div>
                </div>
                <div style={{ padding: "10px 12px", borderRadius: 12, background: "rgba(250,250,249,0.7)", border: "1px solid rgba(28,25,23,0.06)" }}>
                  <div style={LABEL}>{t("page.workspace_detail.remaining")}</div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#1c1917" }}>
                    {budgetStatus.monthly_remaining_credits != null
                      ? _formatCredits(budgetStatus.monthly_remaining_credits)
                      : "—"}
                  </div>
                </div>
              </div>
              {budgetStatus.monthly_budget_credits != null && (
                <div style={{ marginTop: 10 }}>
                  <div style={{ height: 6, borderRadius: 999, background: "#e7e5e4", overflow: "hidden" }}>
                    <div
                      style={{
                        height: "100%",
                        width: `${Math.min(100, Math.round((budgetStatus.pct_used ?? 0) * 100))}%`,
                        background: (budgetStatus.pct_used ?? 0) >= 1 ? "#c14a44" : (budgetStatus.pct_used ?? 0) >= 0.8 ? "#cf9b44" : "#436b65",
                      }}
                    />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4, fontSize: 11, color: "#a8a29e" }}>
                    <span>{Math.round((budgetStatus.pct_used ?? 0) * 100)}%</span>
                    <span>{t("page.workspace_detail.resets_in_days", { days: budgetStatus.days_until_month_end })}</span>
                  </div>
                </div>
              )}
              {budgetStatus.alert_state && budgetStatus.alert_state !== "normal" && (
                <div style={{ marginTop: 10 }}>
                  <StatusBadge type={budgetStatus.alert_state === "critical_100" ? "danger" : "warning"} dot>
                    {budgetStatus.alert_state}
                  </StatusBadge>
                </div>
              )}
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12, alignItems: "end" }}>
            <_Field label={t("page.workspace_detail.monthly_cap_credits_empty_no_cap")}>
              <input
                className="manor-input"
                type="number"
                step="1"
                min="0"
                defaultValue={budgetStatus?.monthly_budget_credits ?? ""}
                key={`ws-budget-${budgetStatus?.monthly_budget_credits ?? "none"}`}
                onBlur={(e) => {
                  const v = e.target.value.trim();
                  const rawNext = v === "" ? null : Number(v);
                  if (rawNext !== null && (!Number.isFinite(rawNext) || rawNext < 0)) {
                    toast.error(t("page.workspace_detail.invalid_amount"), t("page.workspace_detail.enter_non_negative_number_or_leave_empty"));
                    return;
                  }
                  const next = rawNext === null || rawNext <= 0 ? null : Math.floor(rawNext);
                  if (next === (budgetStatus?.monthly_budget_credits ?? null)) return;
                  updateBudget.mutate({ monthly_budget_credits: next });
                }}
                disabled={updateBudget.isPending}
              />
            </_Field>
            <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, color: "#44403c", paddingBottom: 10 }}>
              <input
                type="checkbox"
                checked={!!budgetStatus?.auto_pause_on_budget}
                onChange={(e) => updateBudget.mutate({ auto_pause_on_budget: e.target.checked })}
                disabled={updateBudget.isPending}
              />
              {t("page.workspace_detail.auto_pause_workspace_when_cap_is_hit")}
            </label>
          </div>
        </GlassCard>

        {/* ── Execution policy ── */}
        <GlassCard hoverable={false}>
          <div style={SECTION_TITLE}>{t("page.workspace_detail.execution_policy")}</div>
          <p style={{ fontSize: 12, color: "#78716c", marginTop: 0, marginBottom: 12, lineHeight: 1.5 }}>
            {t("page.workspace_detail.controls_how_background_agent_steps_retry_in_thi")}
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12, alignItems: "end" }}>
            <_Field label={t("page.workspace_detail.max_attempts")}>
              <input
                className="manor-input"
                type="number"
                min="1"
                max="10"
                defaultValue={retryPolicy.max_attempts ?? 3}
                key={`retry-max-${retryPolicy.max_attempts ?? 3}`}
                onBlur={(e) => commitRetryNumber("max_attempts", e.target.value, 3, 1, 10)}
              />
            </_Field>
            <_Field label={t("page.workspace_detail.retry_strategy")}>
              <Select
                value={retryPolicy.strategy || "immediate"}
                onChange={(v) => commitRetryPolicy({ strategy: v || "immediate" })}
                options={[
                  { value: "immediate", label: t("page.workspace_detail.immediate") },
                  { value: "fixed", label: t("page.workspace_detail.fixed_delay") },
                  { value: "exponential", label: t("page.workspace_detail.exponential_backoff") },
                ]}
              />
            </_Field>
            <_Field label={t("page.workspace_detail.base_delay_seconds")}>
              <input
                className="manor-input"
                type="number"
                min="0"
                max="3600"
                defaultValue={retryPolicy.base_delay_seconds ?? 0}
                key={`retry-base-${retryPolicy.base_delay_seconds ?? 0}`}
                onBlur={(e) => commitRetryNumber("base_delay_seconds", e.target.value, 0, 0, 3600)}
              />
            </_Field>
            <_Field label={t("page.workspace_detail.max_delay_seconds")}>
              <input
                className="manor-input"
                type="number"
                min="0"
                max="86400"
                defaultValue={retryPolicy.max_delay_seconds ?? 900}
                key={`retry-delay-${retryPolicy.max_delay_seconds ?? 900}`}
                onBlur={(e) => commitRetryNumber("max_delay_seconds", e.target.value, 900, 0, 86400)}
              />
            </_Field>
            <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, color: "#44403c", gridColumn: "1 / -1" }}>
              <input
                type="checkbox"
                checked={!!retryPolicy.auto_human_on_exhausted}
                onChange={(e) => commitRetryPolicy({ auto_human_on_exhausted: e.target.checked })}
                disabled={updateWorkspace.isPending}
              />
              {t("page.workspace_detail.send_step_to_human_input_when_retries_are_exhaus")}
            </label>
          </div>
        </GlassCard>

        {/* ── Notification policy ── */}
        <GlassCard hoverable={false}>
          <div style={SECTION_TITLE}>{t("page.workspace_detail.task_notification_policy")}</div>
          <p style={{ fontSize: 12, color: "#78716c", marginTop: 0, marginBottom: 12, lineHeight: 1.5 }}>
            {t("page.workspace_detail.in_app_notifications_and_webhooks_always_follow")}
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 16 }}>
            <div style={{ border: "1px solid rgba(28,25,23,0.06)", borderRadius: 14, padding: 14 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, fontWeight: 700, color: "#1c1917" }}>
                <input
                  type="checkbox"
                  checked={emailPolicy.enabled === true}
                  onChange={(e) => commitNotificationPolicy("email", { enabled: e.target.checked })}
                  disabled={updateWorkspace.isPending}
                />
                {t("page.workspace_detail.email_involved_users")}
              </label>
              <p style={{ fontSize: 12, color: "#a8a29e", margin: "8px 0 10px", lineHeight: 1.45 }}>
                {t("page.workspace_detail.sends_to_active_task_creator_assignee_using_the")}
              </p>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {notificationEvents.map((evt) => {
                  const events = ((emailPolicy.events as string[]) || defaultExternalEvents);
                  return (
                    <label key={`email-${evt.value}`} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#44403c" }}>
                      <input
                        type="checkbox"
                        checked={events.includes(evt.value)}
                        onChange={(e) => toggleNotificationEvent("email", evt.value, e.target.checked)}
                        disabled={updateWorkspace.isPending}
                      />
                      {evt.label}
                    </label>
                  );
                })}
              </div>
            </div>
            <div style={{ border: "1px solid rgba(28,25,23,0.06)", borderRadius: 14, padding: 14 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, fontWeight: 700, color: "#1c1917" }}>
                <input
                  type="checkbox"
                  checked={chatPolicy.enabled === true}
                  onChange={(e) => commitNotificationPolicy("external_chat", {
                    enabled: e.target.checked,
                    channel_types: chatPolicy.channel_types || ["slack"],
                  })}
                  disabled={updateWorkspace.isPending}
                />
                {t("page.workspace_detail.slack_external_chat")}
              </label>
              <p style={{ fontSize: 12, color: "#a8a29e", margin: "8px 0 10px", lineHeight: 1.45 }}>
                {t("page.workspace_detail.uses_active_slack_channelconfig_credentials_inco")}
              </p>
              <_Field label={t("page.workspace_detail.slack_channel_id_or_name")}>
                <input
                  className="manor-input"
                  defaultValue={chatPolicy.target || chatPolicy.channel_id || ""}
                  key={`notif-chat-target-${chatPolicy.target || chatPolicy.channel_id || "none"}`}
                  placeholder={t("page.workspace_detail.c0123abc_or_ops")}
                  onBlur={(e) => {
                    const target = e.target.value.trim();
                    if (target === (chatPolicy.target || chatPolicy.channel_id || "")) return;
                    commitNotificationPolicy("external_chat", {
                      target: target || undefined,
                      channel_types: chatPolicy.channel_types || ["slack"],
                    });
                  }}
                />
              </_Field>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10 }}>
                {notificationEvents.map((evt) => {
                  const events = ((chatPolicy.events as string[]) || defaultExternalEvents);
                  return (
                    <label key={`chat-${evt.value}`} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#44403c" }}>
                      <input
                        type="checkbox"
                        checked={events.includes(evt.value)}
                        onChange={(e) => toggleNotificationEvent("external_chat", evt.value, e.target.checked)}
                        disabled={updateWorkspace.isPending}
                      />
                      {evt.label}
                    </label>
                  );
                })}
              </div>
            </div>
          </div>
        </GlassCard>

        {/* ── Notification routing (channel fan-out) ── */}
        {canManageWs && <WorkspaceNotificationRoutingCard ws={ws} updateWorkspace={updateWorkspace} />}

        {/* ── Operating Model (advanced editor) ── */}
        <GlassCard hoverable={false}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <div style={SECTION_TITLE}>{t("page.workspace_detail.operating_model_advanced")}</div>
            {canManageWs && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setSettingsDraft(JSON.stringify(operatingModel || {}, null, 2));
                  setShowSettingsEditor(true);
                }}
              >
                {t("page.workspace_detail.edit_configuration")}
              </Button>
            )}
          </div>
          <p style={{ fontSize: 12, color: "#a8a29e", marginTop: 0, marginBottom: 12 }}>
            {t("page.workspace_detail.use_the_dedicated_tabs_goals_agents_for_routine")}
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 140px), 1fr))", gap: 10 }}>
            {[
              { label: t("page.workspace_detail.services"), value: settingsServices.length },
              { label: t("nav.goals"), value: settingsGoals.length },
              { label: t("page.workspace_detail.rule_copy.rule_count"), value: settingsRules.length },
              { label: t("page.scheduled_jobs.automations"), value: settingsAutomations.length },
            ].map((item) => (
              <div key={item.label} style={{ padding: "10px 12px", borderRadius: 12, background: "#fafaf9", border: "1px solid #e7e5e4" }}>
                <div style={LABEL}>{item.label}</div>
                <div style={{ fontSize: 18, fontWeight: 850, color: "#0f172a" }}>{item.value}</div>
              </div>
            ))}
          </div>
        </GlassCard>

        {/* ── Danger zone ── */}
        {canManageWs && (
          <GlassCard hoverable={false} className="border-red-200">
            <div style={{ ...SECTION_TITLE, color: "#b91c1c" }}>{t("page.workspace_detail.danger_zone")}</div>
            <p style={{ fontSize: 13, color: "#78716c", marginTop: 0, marginBottom: 12, lineHeight: 1.5 }}>
              {t("page.workspace_detail.deleting_this_workspace_removes_its_operating_mo")}
            </p>
            <Button
              variant="danger"
              size="sm"
              onClick={() => setConfirmDeleteWs(true)}
              disabled={deleteWorkspace.isPending}
              loading={deleteWorkspace.isPending}
            >
              {t("page.workspace_detail.delete_workspace")}
            </Button>
          </GlassCard>
        )}

        <Modal
          open={showSettingsEditor}
          onClose={() => setShowSettingsEditor(false)}
          title={t("page.workspace_detail.edit_operating_model")}
          footer={
            <>
              <Button variant="outline" onClick={() => setShowSettingsEditor(false)}>{t("action.cancel")}</Button>
              <Button
                variant="primary"
                disabled={updateOperatingModel.isPending}
                loading={updateOperatingModel.isPending}
                onClick={() => {
                  try {
                    const parsed = JSON.parse(settingsDraft);
                    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
                      toast.error(t("page.workspace_detail.invalid_model"), t("page.workspace_detail.top_level_value_must_be_json_object"));
                      return;
                    }
                    updateOperatingModel.mutate(parsed);
                  } catch (e) {
                    toast.error(t("page.workspace_detail.invalid_json"), (e as Error).message);
                  }
                }}
              >
                {updateOperatingModel.isPending ? t("page.task_collections.saving") : t("action.save")}
              </Button>
            </>
          }
        >
          <Textarea
            label={t("page.workspace_detail.operating_model_json")}
            rows={16}
            value={settingsDraft}
            onChange={(e) => setSettingsDraft(e.target.value)}
          />
        </Modal>
      </div>
    );
  }

  /* ---- render ---- */

  const tabContent: Record<Tab, () => React.ReactNode> = {
    overview: renderOverview,
    staff: renderStaff,
    agents: renderAgents,
    capabilities: renderCapabilities,
    channels: renderChannels,
    documents: renderDocuments,
    rules: renderRules,
    goals: renderGoals,
    automations: () => <ScheduledJobs workspaceId={workspaceId!} />,
    learning: renderLearning,
    activity: renderActivity,
    settings: renderSettings,
  };
  const primaryTab = _primaryTabFor(tab);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "clamp(0.5rem, 2.5vw, 1rem)", overflow: "hidden", position: "relative", zIndex: 10 }}>
      {/* Header */}
      <PageHeader
        title={ws.name}
        subtitle={
          <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <StatusBadge type={ws.status === "active" ? "active" : "inactive"} dot pulse={ws.status === "active"}>
              {ws.status}
            </StatusBadge>
            {ws.category && <Chip variant="teal" size="sm">{ws.category}</Chip>}
          </span>
        }
      >
        {canManageWs && (
          <Button
            variant={ws.status === "active" ? "ghost" : "primary"}
            size="sm"
            loading={togglePause.isPending}
            onClick={() => togglePause.mutate()}
          >
            {ws.status === "active" ? t("page.workspace_detail.pause") : t("page.workspace_detail.resume")}
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate("/workspaces")}
        >
          {t("page.workspace_detail.back_to_workspaces")}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setExportOpen(true)}
        >
          {t("page.workspace_detail.export_as_blueprint")}
        </Button>
      </PageHeader>

      <ExportBlueprintModal
        open={exportOpen}
        onClose={() => setExportOpen(false)}
        workspaceId={ws.id}
        workspaceName={ws.name}
      />

      <Modal
        open={showWorkspaceWelcome}
        onClose={dismissWorkspaceWelcome}
        title={t("page.workspace_detail.welcome_title")}
        maxWidth="520px"
        footer={
          <>
            <Button variant="primary" onClick={openWorkspaceChatFromWelcome}>
              {t("page.workspace_detail.open_workspace_chat")}
            </Button>
            <Button variant="outline" onClick={openWorkspaceGoalsFromWelcome}>
              {t("page.workspace_detail.open_goal_page")}
            </Button>
            <Button variant="ghost" onClick={dismissWorkspaceWelcome}>
              {t("page.workspace_detail.stay_on_details")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 14 }}>
          <WorkspaceWelcomeBurst />
          <div style={{ minWidth: 0 }}>
            <p style={{ margin: "0 0 8px", fontSize: 14, lineHeight: 1.65, color: "var(--text-strong)" }}>
              {t("page.workspace_detail.welcome_description")}
            </p>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, color: "var(--text-muted)" }}>
              {t("page.workspace_detail.welcome_details_note")}
            </p>
          </div>
        </div>
      </Modal>

      <Modal
        open={canManageWs && showCreateKnowledgeFolderModal}
        onClose={() => {
          if (!createDocGroup.isPending) setShowCreateKnowledgeFolderModal(false);
        }}
        title={t("page.workspace_detail.new_knowledge_folder")}
        maxWidth="520px"
        footer={
          <>
            <Button
              variant="outline"
              onClick={() => setShowCreateKnowledgeFolderModal(false)}
              disabled={createDocGroup.isPending}
            >
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={submitCreateKnowledgeFolder}
              loading={createDocGroup.isPending}
              disabled={!knowledgeFolderForm.name.trim()}
            >
              {t("page.workspace_detail.create_folder")}
            </Button>
          </>
        }
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submitCreateKnowledgeFolder();
          }}
          style={{ display: "flex", flexDirection: "column", gap: 14 }}
        >
          <Input
            label={t("page.workspace_detail.folder_name")}
            value={knowledgeFolderForm.name}
            onChange={(e) => {
              setKnowledgeFolderForm((form) => ({ ...form, name: e.target.value }));
              if (knowledgeFolderError) setKnowledgeFolderError("");
            }}
            placeholder={t("page.workspace_detail.e_g_customer_faq_brand_voice_public_sales_deck")}
            error={knowledgeFolderError}
            disabled={createDocGroup.isPending}
          />
          <Textarea
            label={t("page.workspace_detail.purpose")}
            value={knowledgeFolderForm.purpose}
            onChange={(e) => setKnowledgeFolderForm((form) => ({ ...form, purpose: e.target.value }))}
            placeholder={t("page.workspace_detail.tell_agents_when_to_use_this_folder_who_it_is_sa")}
            rows={3}
            disabled={createDocGroup.isPending}
          />
          <div style={{ padding: "10px 12px", borderRadius: 12, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)", color: "#78716c", fontSize: 12, lineHeight: 1.5 }}>
            {t("page.workspace_detail.folders_do_not_move_files_out_of_knowledge_base")}
          </div>
        </form>
      </Modal>

      <div style={{ marginBottom: 18, display: "flex", flexDirection: "column", gap: 10, overflowX: "auto" }}>
        <TabSwitcher
          tabs={PRIMARY_TABS}
          value={primaryTab}
          onChange={handlePrimaryTabChange}
          className="w-full sm:w-auto"
        />
        {primaryTab === "configure" && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              flexWrap: "wrap",
              padding: "10px 12px",
              borderRadius: 14,
              border: "1px solid rgba(28,25,23,0.06)",
              background: "rgba(255,255,255,0.58)",
            }}
          >
            <div style={{ minWidth: 140 }}>
              <div style={{ ...LABEL, marginBottom: 2 }}>
                {t("page.workspace_detail.detailed_configuration")}
              </div>
              <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.35 }}>
                {t("page.workspace_detail.configure_hint")}
              </div>
            </div>
            <TabSwitcher
              tabs={SETUP_TAB_ITEMS}
              value={tab}
              onChange={handleTabChange}
              size="sm"
              wrap
            />
          </div>
        )}
      </div>

      {/* Tab content */}
      <div style={{ flex: 1, overflowY: "auto", padding: "4px 4px 20px" }}>
        {tabContent[tab]()}
      </div>

      <ConfirmDialog
        open={!!confirmRemoveStaff}
        onClose={() => setConfirmRemoveStaff(null)}
        onConfirm={() => { if (canManageWs && confirmRemoveStaff) removeStaff.mutate(confirmRemoveStaff); }}
        title={t("page.workspace_detail.remove_staff")}
        message={t("page.workspace_detail.remove_this_staff_member_from_the_workspace_they")}
        confirmLabel={t("page.task_detail.runtime.remove_rule")}
        danger
      />

      <ConfirmDialog
        open={!!confirmUnmapAgent}
        onClose={() => setConfirmUnmapAgent(null)}
        onConfirm={() => { if (confirmUnmapAgent) unmapAgent.mutate(confirmUnmapAgent); }}
        title={t("page.workspace_detail.unmap_agent")}
        message={t("page.workspace_detail.unmap_this_agent_from_the_service_existing_tasks")}
        confirmLabel={t("page.workspace_detail.unmap")}
        danger
      />

      <ConfirmDialog
        open={!!confirmRemoveChannel}
        onClose={() => setConfirmRemoveChannel(null)}
        onConfirm={() => { if (confirmRemoveChannel) removeChannel.mutate(confirmRemoveChannel.id); }}
        title={t("page.workspace_detail.remove_channel")}
        message={t("page.workspace_detail.remove_channel_message").replace("{name}", confirmRemoveChannel?.name || t("page.workspace_detail.this_channel"))}
        confirmLabel={t("page.task_detail.runtime.remove_rule")}
        danger
      />

      <ConfirmDialog
        open={!!confirmRemoveKnowledgeDoc}
        onClose={() => setConfirmRemoveKnowledgeDoc(null)}
        onConfirm={() => {
          if (canManageWs && confirmRemoveKnowledgeDoc) {
            removeKnowledgeDocument.mutate({
              groupId: confirmRemoveKnowledgeDoc.groupId,
              documentId: confirmRemoveKnowledgeDoc.documentId,
            });
          }
        }}
        title={t("page.workspace_detail.remove_document")}
        message={t("page.workspace_detail.remove_document_message").replace("{name}", confirmRemoveKnowledgeDoc?.name || t("page.workspace_detail.this_document"))}
        confirmLabel={t("page.task_detail.runtime.remove_rule")}
        danger
      />

      <ConfirmDialog
        open={!!confirmDeleteKnowledgeGroup}
        onClose={() => setConfirmDeleteKnowledgeGroup(null)}
        onConfirm={() => {
          if (canManageWs && confirmDeleteKnowledgeGroup) {
            deleteKnowledgeGroup.mutate(confirmDeleteKnowledgeGroup.groupId);
          }
        }}
        title={t("page.workspace_detail.remove_knowledge_folder")}
        message={t("page.workspace_detail.remove_knowledge_folder_message").replace(
          "{name}",
          confirmDeleteKnowledgeGroup?.name || t("page.workspace_detail.this_folder"),
        )}
        confirmLabel={t("page.workspace_detail.remove_folder")}
        danger
      />

      <ConfirmDialog
        open={confirmDeleteWs}
        onClose={() => setConfirmDeleteWs(false)}
        onConfirm={() => { if (canManageWs) deleteWorkspace.mutate(); }}
        title={t("page.workspaces.delete_workspace")}
        message={`Move "${ws.name}" to trash? Recoverable for 30 days from the Workspaces page (Trash section). After that, the operating model, agent subscriptions, memory, and channel configs are permanently deleted.`}
        confirmLabel={t("page.workspaces.move_to_trash")}
        danger
      />
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────

/** Pending-chip preview after the user picks someone in the staff
 *  PeoplePicker but before they click Assign. Mirrors the chip pattern
 *  used in ShareDialog so the visual feels consistent across the app. */
function StaffPickedChip({ staff, onClear }: { staff: StaffOption; onClear: () => void }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "6px 10px",
        border: "1px solid rgba(28,25,23,0.3)",
        background: "rgba(28,25,23,0.06)",
        borderRadius: 8,
      }}
    >
      <UserAvatar name={staff.name} avatarUrl={staff.avatar_url} size={28} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "#57534e",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {staff.name}
        </div>
        <div
          style={{
            fontSize: 11,
            color: "#a8a29e",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {staff.email || "—"}
          {staff.department && ` · ${staff.department}`}
          {staff.title && ` · ${staff.title}`}
        </div>
      </div>
      <button
        type="button"
        onClick={onClear}
        aria-label="Clear"
        style={{
          width: 22,
          height: 22,
          borderRadius: 6,
          background: "transparent",
          border: "none",
          cursor: "pointer",
          color: "#57534e",
          fontSize: 16,
          lineHeight: 1,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        ×
      </button>
    </div>
  );
}
