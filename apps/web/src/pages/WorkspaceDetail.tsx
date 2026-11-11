import ApprovalSettingsPanel from "../components/approvals/ApprovalSettingsPanel";
import { displayReadInterval, setupReadInterval } from "../lib/readRefreshPolicy";
import { usePageVisible } from "../lib/usePageVisible";
import ApprovalModeTrigger from "../components/approvals/ApprovalModeTrigger";
import { approvalKeys } from "../lib/approvalQueries";
import { invalidateCatalogQueries } from "../lib/catalogInvalidation";
import { useState, useCallback, useEffect, useMemo } from "react";
import { Link, useLocation, useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type GovernancePolicy,
  type HumanBlockingEntry,
  type HumanContributionDigest,
  type HumanDecisionDigest,
  type HumanParticipantDigest,
  type HumanQueueCommitment,
  type StrategyProposalItem,
  type StrategyReviewListEntry,
  type WorkerRegisterResponse,
  type WorkspaceBudgetUpdate,
  type WorkspaceEvaluationSnapshot,
} from "../lib/api";
import { useToastStore } from "../stores/toast";
import type { AgentLearningCandidate, RuntimeEvidence, Workspace, WorkspaceStaff, WorkspaceActivity } from "../lib/types";
import { canManageWorkspace, canWriteWorkspace } from "../lib/permissions";
import { useAuthStore } from "../stores/auth";
import { useIsCollaborator } from "../lib/collaboratorSession";
import { agentListQueryOptions, useCatalogScope } from "../lib/catalogQueries";
import { useDisplayPreferences } from "../stores/displayPreferences";
import { usePreviewFeatureAccess } from "../lib/previewFeatureAccess";
import { openAgentEditModal } from "../stores/agentEditModal";
import { formatDate, getPreferredTimeZone, relativeTime } from "../lib/format";
import PageHeader from "../components/ui/PageHeader";
import { FeatureDiscoveryBadge } from "../components/ui/FeatureDiscoveryHint";
import useFeatureDiscovery from "../components/useFeatureDiscovery";
import { FEATURE_DISCOVERIES } from "../lib/featureDiscovery";
import StatusBadge from "../components/ui/StatusBadge";
import StatusPill from "../components/ui/StatusPill";
import Chip from "../components/ui/Chip";
import { ConfigAddRow, ConfigBlock, ConfigGroup, ConfigPage, ConfigRow } from "../components/ui/GroupedList";
import TabSwitcher from "../components/ui/TabSwitcher";
import Card from "../components/ui/Card";
import { WorkspaceCollaborationManagement } from "./WorkspaceCollaboration";
import ClientDeliveryDialog from "./clients/ClientDeliveryDialog";
import ClientTrialsTab, { useClientTrials } from "../components/clientTrials/ClientTrialsTab";
import { EMBED } from "../embed/embedContext";
import GlassCard from "../components/ui/GlassCard";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import CelebrationFirework from "../components/ui/CelebrationFirework";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import Button from "../components/ui/Button";
import Dropdown from "../components/ui/Dropdown";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Select from "../components/ui/Select";
import Toggle from "../components/ui/Toggle";
import Tooltip from "../components/ui/Tooltip";
import UserAvatar from "../components/ui/UserAvatar";
import AgentAvatar from "../components/ui/AgentAvatar";
import WebchatPageEditor from "../components/webchat/WebchatPageEditor";
import { webchatWorkspaceDisplayName } from "../lib/webchatPage";
import { PeoplePicker, type StaffOption } from "../components/permissions";
import {
  IconChat,
  IconCopy,
  IconDocument,
  IconDownload,
  IconHelp,
  IconChevronLeft,
  IconInfo,
  IconRefresh,
  IconShare,
} from "../components/icons";
import WorkspaceGoalGraph from "../components/ui/WorkspaceGoalGraph";
import WorkspaceWorkflows from "../components/workflows/WorkspaceWorkflows";
import WorkspaceStatsPanel from "../components/workspaces/WorkspaceStatsPanel";
import WorkspaceAppViewSettings from "../components/workspaces/WorkspaceAppViewSettings";
import WorkspaceConnectionNotice from "../components/workspaces/WorkspaceConnectionNotice";
import InlineIntegrationLink from "../components/InlineIntegrationLink";
import IntegrationConnectRow from "../components/integrations/IntegrationConnectRow";
import ScheduledJobs from "./ScheduledJobs";
import ExportBlueprintModal from "../components/blueprints/ExportBlueprintModal";
import BlueprintUpgradeDialog from "../components/blueprints/BlueprintUpgradeDialog";
import WorkspacePackageReleaseDialog from "../components/blueprints/WorkspacePackageReleaseDialog";
import BlueprintGuideDialog, { workspaceUsageGuide } from "../components/blueprints/BlueprintGuideDialog";
import PromoteWorkspaceModal from "../components/blueprints/PromoteWorkspaceModal";
import { SUPPORTED_LOCALES, t } from "../lib/i18n";
import {
  isHardBlockRuleType,
  softenHardBlockRule, buildRuntimeRuleFromText, runtimeRuleSelectorLabels, uniqueActionPatterns } from "../lib/runtimeRules";
import { formatUserFacingLabel, formatUserFacingText } from "../lib/taskDisplay";
import { goalIdentityDedupeKey } from "../lib/goalIdentity";
import { formatGoalNumber, goalProgressPercent } from "../lib/goalNumbers";
import { PageLoading } from "../components/ui/Skeleton";

/* ---- style constants ---- */

const GLASS: React.CSSProperties = {
  background: "rgba(255,255,255,0.85)",
  backdropFilter: "blur(16px)",
  WebkitBackdropFilter: "blur(16px)",
  border: "0.5px solid rgba(29, 29, 31,0.06)",
  borderRadius: 20,
  padding: "24px 28px",
};

const LABEL: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 400,
  color: "var(--text-muted)",
  marginBottom: 4,
};

const VALUE: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  color: "var(--text-strong)",
  wordBreak: "break-word" as const,
};

const SECTION_TITLE: React.CSSProperties = {
  fontSize: 15,
  fontWeight: 600,
  color: "var(--text-strong)",
  marginBottom: 12,
};

async function copyTextToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const el = document.createElement("textarea");
      el.value = text;
      el.style.position = "fixed";
      el.style.opacity = "0";
      document.body.appendChild(el);
      el.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(el);
      return ok;
    } catch {
      return false;
    }
  }
}

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
  color: "var(--text-faint)",
  padding: "10px 16px",
  textAlign: "left" as const,
  borderBottom: "1px solid #f5f5f7",
};

const TABLE_CELL: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 500,
  color: "var(--text-muted)",
  padding: "12px 16px",
  borderBottom: "1px solid #fafafa",
};

type WorkspaceAgentSource = "hosted" | "https" | "custom";

type WorkspaceAgentForm = {
  source: WorkspaceAgentSource;
  service_mode: "new" | "existing";
  service_name: string;
  service_key: string;
  agent_id: string;
  custom_prompt: string;
  agent_name: string;
  description: string;
  system_prompt: string;
  https_url: string;
  runtime_display_name: string;
  worker_registration: { key: string; registration: WorkerRegisterResponse } | null;
};

type AgentConnectionResult =
  | { type: "https"; registration: WorkerRegisterResponse; agentName: string; endpoint: string }
  | null;

const DEFAULT_AGENT_FORM: WorkspaceAgentForm = {
  source: "hosted",
  service_mode: "new",
  service_name: "",
  service_key: "",
  agent_id: "",
  custom_prompt: "",
  agent_name: "",
  description: "",
  system_prompt: "",
  https_url: "",
  runtime_display_name: "",
  worker_registration: null,
};

function filterWorkspaceAgentMappingOptions(agents: any[], workspaceId?: string): any[] {
  const visibleAgents = agents.filter((agent) => (
    !agent.workspace_id || agent.workspace_id === workspaceId
  ));
  const seenLegacyTemplateRoles = new Set<string>();
  return visibleAgents.filter((agent) => {
    if (agent.workspace_id || agent.source !== "marketplace_template") return true;
    const identity = JSON.stringify({
      name: agent.name || "",
      category: agent.category || "",
      system_prompt: agent.system_prompt || "",
      config: agent.config || {},
    });
    if (seenLegacyTemplateRoles.has(identity)) return false;
    seenLegacyTemplateRoles.add(identity);
    return true;
  });
}

const AGENT_SOURCE_OPTIONS: Array<{ key: WorkspaceAgentSource; title: string; body: string }> = [
  {
    key: "hosted",
    title: t("ui.manor_hosted_agent"),
    body: "Use an existing Manor agent or template. No external connection required.",
  },
  {
    key: "https",
    title: t("ui.agent_from_https"),
    body: "Connect a generic HTTPS agent endpoint, including Hermes, OpenClaw, or your own service.",
  },
  {
    key: "custom",
    title: t("ui.create_custom_agent"),
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

type Tab = "app" | "staff" | "channels" | "documents" | "rules" | "goals" | "workflows" | "records" | "usage" | "settings" | "collaboration" | "client-trials";
type RecordView = "activity" | "data" | "review" | "learning";
const RECORD_VIEWS: RecordView[] = ["activity", "data", "review", "learning"];

function blueprintSetupDestination(check: { todo_kind?: string; kind?: string }): Tab | "/integrations" {
  const kind = check.todo_kind || check.kind || "";
  if (["missing_integration", "browser_session"].includes(kind)) return "/integrations";
  if (kind === "channel") return "channels";
  if (kind === "missing_agent") return "staff";
  if (kind === "knowledge_pack_content") return "documents";
  if (["mcp_server", "mcp_configuration", "missing_skill"].includes(kind)) return "channels";
  return "settings";
}

type EvaluationDimensionKey =
  | "goal_impact"
  | "cost_efficiency"
  | "time_efficiency"
  | "execution_health"
  | "output_quality"
  | "user_feedback"
  | "governance"
  | "learning";

const WORKSPACE_DETAIL_TABS: Tab[] = [
  "app",
  "staff",
  "channels",
  "documents",
  "rules",
  "goals",
  "workflows",
  "records",
  "usage",
  "settings",
  "collaboration",
  "client-trials",
];

// Older links keep working: each page they named now lives inside one of these.
const RETIRED_TABS: Record<string, Tab> = {
  overview: "staff",
  agents: "staff",
  capabilities: "channels",
  automations: "workflows",
  activity: "records",
  learning: "records",
  knowledge: "documents",
  docs: "documents",
  document: "documents",
};

function _normalizeWorkspaceDetailTab(value: string | null): Tab {
  if (value && RETIRED_TABS[value]) return RETIRED_TABS[value];
  return WORKSPACE_DETAIL_TABS.includes(value as Tab) ? value as Tab : "staff";
}

function _staffLabel(s: { id?: string; name?: string; display_name?: string; email?: string } | null | undefined): string {
  if (!s) return "";
  return s.display_name || s.name || s.email || s.id || t("page.workspace_detail.unknown");
}

// A seat derived from a provider's collaboration grant. The grant decides it
// (Collaboration tab, or keep/remove at acceptance), so it has no remove control.
/** What DELETE /workspaces/{id}/staff/{x} names: the Staff row, or the seat itself when it has none. */
function _seatRemovalId(s: WorkspaceStaff): string {
  return s.staff_id || s.id;
}

function _CollaborationSeatPill({ role, team }: { role: NonNullable<WorkspaceStaff["collaboration_role"]>; team?: boolean }) {
  const label = team
    ? t("collaboration.seat_provider_team", { access: t(role === "provider_trial" ? "page.client_trials.access_trial" : "page.client_trials.access_maintainer") })
    : t(role === "provider_trial" ? "collaboration.seat_provider_trial" : "collaboration.seat_maintainer");
  return <StatusPill size="sm" status="active" label={label} />;
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

/** Settings → Approval automation: the Strategist proposal kinds, in the
 *  order the M8 catalog lists them. Rows the server sends for an unknown
 *  kind are simply not grouped (the catalog is the source of truth). */
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

function _usdAmount(value: number): string {
  if (value > 0 && value < 0.01) return "<$0.01";
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(value);
}

function _CreditMetricValue({ value, creditsPerUsd }: { value: number; creditsPerUsd?: number }) {
  const credits = Math.max(0, Math.round(value));
  return (
    <div className="workspace-credit-metric-value">
      <span>{credits.toLocaleString()}</span>
      <span className="workspace-credit-metric-unit">
        {t("page.workspace_detail.credits_unit")}
        {creditsPerUsd ? ` · ${_usdAmount(credits / creditsPerUsd)}` : ""}
      </span>
    </div>
  );
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

function _channelIntegrationLabel(ch: any): string {
  return _humanize(ch?.integration_provider || ch?.provider || ch?.channel_type)
    || t("page.workspace_detail.integration");
}

function _channelAccountLabel(ch: any): string {
  return String(ch?.account_label || "").trim()
    || _channelDisplayName(ch)
    || t("page.workspace_detail.account");
}

function _serviceLabel(svc: { name?: string; service_key?: string; key?: string } | null | undefined): string {
  if (!svc) return t("page.workspace_detail.unnamed_service");
  return svc.name || _humanize(svc.service_key || svc.key) || t("page.workspace_detail.unnamed_service");
}

function _goalLabel(g: { title?: string; name?: string; goal_key?: string; key?: string } | null | undefined): string {
  if (!g) return t("page.workspace_detail.goal_fallback");
  return g.title || g.name || _humanize(g.goal_key || g.key) || t("page.workspace_detail.goal_fallback");
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

function _dedupeGoals(goals: any[]): any[] {
  const byKey = new Map<string, any>();
  for (const goal of goals || []) {
    const key = goalIdentityDedupeKey(goal);
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

function _evaluationHours(hours: number): string {
  return hours < 48
    ? t("page.workspace_detail.eval_hours").replace("{count}", String(Math.max(1, Math.round(hours))))
    : t("page.workspace_detail.eval_days").replace("{count}", String(Math.round(hours / 24)));
}

/** A dimension's one-line note, built from its numbers so it reads in the viewer's language. */
function _evaluationNote(key: string, d: Record<string, any>, days: number): string {
  const n = (value: unknown) => Number(value || 0);
  const parts = (items: [boolean, string][]) => items.filter(([shown]) => shown).map(([, text]) => text).join(" · ");
  const count = (k: string, value: unknown, extra: Record<string, unknown> = {}) =>
    Object.entries({ count: n(value), ...extra }).reduce(
      (text, [name, v]) => text.replace(`{${name}}`, typeof v === "number" ? v.toLocaleString() : String(v)),
      t(`page.workspace_detail.${k}`),
    );
  switch (key) {
    case "goal_impact": {
      const agg = d.aggregate || {};
      if (!n(agg.goal_count)) return t("page.workspace_detail.eval_note_no_goals");
      return count("eval_note_goals", agg.measured_goal_count, {
        total: n(agg.goal_count), progress: Math.round(n(agg.average_progress_pct)),
      });
    }
    case "cost_efficiency":
      return parts([
        [true, count("eval_note_credits", d.window_credits, { days })],
        [d.monthly_budget_credits != null && d.budget_pct_used != null,
          count("eval_note_budget", Math.round(n(d.budget_pct_used) * 100))],
      ]);
    case "time_efficiency":
      return parts([
        [d.avg_completed_task_cycle_hours != null,
          t("page.workspace_detail.eval_note_cycle").replace("{time}", _evaluationHours(n(d.avg_completed_task_cycle_hours)))],
        [n(d.waiting_human_step_count) > 0, count("eval_note_waiting", d.waiting_human_step_count)],
      ]) || t("page.workspace_detail.eval_note_nothing_finished");
    case "execution_health": {
      const failures = n(d.failed_task_count) + n(d.failed_step_count) + n(d.failed_workflow_run_count);
      return parts([
        [n(d.task_count) > 0, count("eval_note_tasks", d.completed_task_count, { total: n(d.task_count) })],
        [n(d.workflow_run_count) > 0, count("eval_note_runs", d.completed_workflow_run_count, { total: n(d.workflow_run_count) })],
        [failures > 0, count("eval_note_failures", failures)],
      ]) || t("page.workspace_detail.eval_note_no_work");
    }
    case "output_quality":
      return parts([
        [n(d.approval_count) > 0, count("eval_note_approved", d.approval_count)],
        [n(d.rejection_count) > 0, count("eval_note_rejected", d.rejection_count)],
        [n(d.artifact_count) > 0, count("eval_note_deliverables", d.artifact_count)],
      ]) || t("page.workspace_detail.eval_note_no_reviews");
    case "user_feedback":
      return n(d.signal_count) > 0
        ? count("eval_note_feedback", d.positive_signal_count, { negative: n(d.negative_signal_count) })
        : t("page.workspace_detail.eval_note_no_feedback");
    case "governance":
      return parts([
        [n(d.approval_required_step_count) > 0, count("eval_note_needs_approval", d.approval_required_step_count)],
        [n(d.waiting_human_step_count) > 0, count("eval_note_waiting", d.waiting_human_step_count)],
        [n(d.policy_violation_count) > 0, count("eval_note_violations", d.policy_violation_count)],
      ]) || t("page.workspace_detail.eval_note_nothing_to_approve");
    case "learning":
      return count("eval_note_learning", d.runtime_evidence_count, { suggestions: n(d.candidate_count) });
    default:
      return _evaluationText(d.summary || "");
  }
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

function _strategyStatusChipVariant(status: string): "teal" | "orange" | "blue" | "green" | "red" | "slate" | "purple" {
  switch (String(status || "").toLowerCase()) {
    case "succeeded":
    case "approved":
      return "green";
    case "failed":
    case "rejected":
      return "red";
    case "running":
    case "executing":
      return "blue";
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
  return { max_risk_level: policy?.max_risk_level || DEFAULT_GOVERNANCE_POLICY.max_risk_level, budget_caps_per_kind: policy?.budget_caps_per_kind || {} };
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
    hardBlockNote: t("page.workspace_detail.rule_copy.hard_block_note"),
    softenToApproval: t("page.workspace_detail.rule_copy.soften_to_approval"),
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
          color: "var(--text-muted)",
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
        <strong style={{ display: "block", fontSize: 13, color: "var(--text-strong)", marginBottom: 3 }}>
          {title}
        </strong>
        <span style={{ display: "block", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.45 }}>
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

// One source list, grouped by what you are deciding.
const SOURCE_LIST_GROUPS: { key: string; label: string; items: Tab[] }[] = [
  { key: "team", label: t("page.workspace_detail.group_team"), items: ["staff"] },
  { key: "abilities", label: t("page.workspace_detail.group_abilities"), items: ["app", "channels", "documents", "workflows"] },
  { key: "rules", label: t("page.workspace_detail.group_rules"), items: ["rules", "goals"] },
  { key: "records", label: t("page.workspace_config.group_records"), items: ["records", "usage"] },
  { key: "share", label: t("page.workspace_detail.group_share"), items: ["collaboration", "client-trials"] },
  { key: "general", label: t("page.workspace_detail.group_general"), items: ["settings"] },
];

const TAB_LABELS: Record<Tab, string> = {
  app: t("component.workspace_app_view.page_tab"),
  staff: t("page.workspace_detail.staff"),
  channels: t("page.workspace_config.connected_accounts"),
  documents: t("page.workspace_config.materials"),
  workflows: t("page.workspace_config.flows_and_automations"),
  rules: t("page.workspace_config.needs_approval"),
  goals: t("page.workspace_config.goals_nav"),
  records: t("page.workspace_config.activity_and_review"),
  usage: t("page.workspace_config.usage_and_budget"),
  collaboration: t("page.collaboration.tab"),
  "client-trials": t("page.client_trials.tab"),
  settings: t("page.workspace_config.general"),
};

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
  "inapp", "email", "telegram", "wechat", "slack", "discord", "twilio_sms",
];
const _NOTIF_CHANNEL_LABELS: Record<string, string> = {
  inapp: "In-app", email: "Email", telegram: "Telegram", wechat: "WeChat",
  slack: "Slack", discord: "Discord", twilio_sms: "SMS",
};
const _NOTIF_EVENTS = [
  { kind: "task_hitl_requested", label: t("ui.task_needs_input") },
  { kind: "task_hitl_reminder", label: t("ui.approval_reminder") },
  { kind: "task_failed", label: t("ui.task_failed") },
  { kind: "task_succeeded", label: t("ui.task_completed") },
  { kind: "task_assigned", label: t("ui.task_assigned") },
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
      <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 0, marginBottom: 12, lineHeight: 1.5 }}>
        {t("page.workspace_detail.notification_routing_desc")}
      </p>

      {/* Default channels */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 600, marginBottom: 6 }}>
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
                color: locked ? "#aeaeb2" : (checked ? "#1d1d1f" : "#6e6e73"),
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
        <div style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 600, marginBottom: 6 }}>
          {t("page.settings.event_routing")}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {_NOTIF_EVENTS.map((evt) => {
            const override = _normaliseRouteList(routes[evt.kind]);
            const customised = override.length > 0;
            const effective = customised ? new Set([...override, "inapp"]) : new Set([...defaultRoutes, "inapp"]);
            return (
              <div key={evt.kind} style={{
                padding: 10, border: "0.5px solid rgba(29, 29, 31,0.06)", borderRadius: 10,
                background: "rgba(255,255,255,0.5)",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-strong)" }}>{evt.label}</div>
                  {customised && (
                    <button
                      type="button" onClick={() => resetKind(evt.kind)}
                      style={{
                        fontSize: 11, padding: "3px 8px", borderRadius: 6,
                        border: "0.5px solid rgba(29, 29, 31,0.06)",
                        background: "rgba(255,255,255,0.7)", color: "var(--text-muted)", cursor: "pointer",
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
                        color: locked ? "#aeaeb2" : (checked ? "#1d1d1f" : "#6e6e73"),
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
          <div style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 600 }}>
            {t("page.workspace_detail.hitl_recipients")}
          </div>
          {hitlExplicit && (
            <button
              type="button" onClick={clearHitlOverride}
              style={{
                fontSize: 11, padding: "3px 8px", borderRadius: 6,
                border: "0.5px solid rgba(29, 29, 31,0.06)",
                background: "rgba(255,255,255,0.7)", color: "var(--text-muted)", cursor: "pointer",
              }}
            >
              {t("page.settings.use_default")}
            </button>
          )}
        </div>
        <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--text-muted)" }}>
          {t("page.workspace_detail.hitl_recipients_desc")}
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {members.length === 0 ? (
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
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
                color: checked ? "#1d1d1f" : "#6e6e73",
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
          <div style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 600 }}>
            {t("page.workspace_detail.inbound_recipients")}
          </div>
          {inboundExplicit && (
            <button
              type="button" onClick={clearInboundOverride}
              style={{
                fontSize: 11, padding: "3px 8px", borderRadius: 6,
                border: "0.5px solid rgba(29, 29, 31,0.06)",
                background: "rgba(255,255,255,0.7)", color: "var(--text-muted)", cursor: "pointer",
              }}
            >
              {t("page.settings.use_default")}
            </button>
          )}
        </div>
        <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--text-muted)" }}>
          {t("page.workspace_detail.inbound_recipients_desc")}
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {members.length === 0 ? (
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
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
                color: checked ? "#1d1d1f" : "#6e6e73",
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
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const simpleMode = useDisplayPreferences((state) => state.interfaceMode === "simple");
  const simpleSetupTab = simpleMode && searchParams.get("setup") === "1"
    && (requestedTab === "channels" || requestedTab === "capabilities")
    ? "channels" as const
    : null;
  const simpleSetupMode = simpleSetupTab !== null;
  const queryClient = useQueryClient();
  const catalogScope = useCatalogScope();
  const toast = useToastStore();
  const currentUser = useAuthStore((s) => s.user);
  // An external collaborator works in the Workspace but never decides who is
  // in it or its delivery; those routes and the company directory refuse them.
  const collaborator = useIsCollaborator();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const flowsAccess = usePreviewFeatureAccess("flows");
  const configLoaded = flowsAccess.loaded;
  const flowsAvailable = flowsAccess.enabled;
  const [tab, setTab] = useState<Tab>(() => _normalizeWorkspaceDetailTab(requestedTab === "chat" ? null : requestedTab));
  const shouldShowWorkspaceSetup = searchParams.get("setup") === "1";
  const showBlueprintGuideOnArrival = searchParams.get("guide") === "1";
  const shouldShowWorkspaceWelcome = (
    searchParams.get("created") === "1" || searchParams.get("welcome") === "1"
  ) && !shouldShowWorkspaceSetup && !showBlueprintGuideOnArrival;
  const [showWorkspaceWelcome, setShowWorkspaceWelcome] = useState(shouldShowWorkspaceWelcome);
  const [guideOpen, setGuideOpen] = useState(false);
  useEffect(() => setGuideOpen(false), [workspaceId]);

  const appDiscovery = useFeatureDiscovery({
    feature: FEATURE_DISCOVERIES.workspaceApp,
    userId: currentUser?.id,
    enabled: privateApiEnabled,
    alreadyUsed: tab === "app",
    autoShow: false,
  });

  const commitTab = useCallback((nextTab: Tab, options?: { replace?: boolean }) => {
    setTab(nextTab);
    const nextParams = new URLSearchParams(searchParams);
    if (nextTab === "staff") {
      nextParams.delete("tab");
    } else {
      nextParams.set("tab", nextTab);
    }
    if (nextTab !== "records") nextParams.delete("view");
    // An open customer trial belongs to the Customer trials tab only.
    if (nextTab !== "client-trials") nextParams.delete("trial");
    setSearchParams(nextParams, { replace: options?.replace ?? true });
  }, [searchParams, setSearchParams]);

  // Reset/sync local state when workspace or URL tab changes.
  useEffect(() => {
    if (requestedTab === "chat" && workspaceId) {
      setTab("staff");
      navigate(`/chat?workspace=${encodeURIComponent(workspaceId)}`, { replace: true });
      return;
    }
    const nextTab = _normalizeWorkspaceDetailTab(requestedTab);
    setTab(nextTab);
    if (requestedTab && requestedTab !== nextTab) {
      const nextParams = new URLSearchParams(searchParams);
      if (nextTab === "staff") {
        nextParams.delete("tab");
      } else {
        nextParams.set("tab", nextTab);
      }
      if (requestedTab === "learning") nextParams.set("view", "learning");
      setSearchParams(nextParams, { replace: true });
    }
  }, [workspaceId, requestedTab, searchParams, setSearchParams, navigate]);

  useEffect(() => {
    setShowWorkspaceWelcome(shouldShowWorkspaceWelcome);
  }, [shouldShowWorkspaceWelcome]);

  useEffect(() => {
    const targetId = tab === "rules" && location.hash === "#workspace-approval-automation"
      ? "workspace-approval-automation"
      : tab === "settings" && location.hash === "#workspace-cover-image"
        ? "workspace-cover-image"
      : tab === "records" && location.hash === "#workspace-stats"
        ? "workspace-stats"
        : "";
    if (!targetId) return undefined;
    const timer = window.setTimeout(() => {
      document.getElementById(targetId)?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }, 100);
    return () => window.clearTimeout(timer);
  }, [location.hash, tab]);

  const handleTabChange = useCallback((t: string) => {
    commitTab(_normalizeWorkspaceDetailTab(t));
  }, [commitTab]);

  const dismissWorkspaceWelcome = useCallback(() => {
    setShowWorkspaceWelcome(false);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("created");
    nextParams.delete("welcome");
    setSearchParams(nextParams, { replace: true });
  }, [searchParams, setSearchParams]);

  const dismissWorkspaceSetup = useCallback(() => {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("created");
    nextParams.delete("welcome");
    nextParams.delete("setup");
    setSearchParams(nextParams, { replace: true });
  }, [searchParams, setSearchParams]);

  const dismissBlueprintGuide = useCallback(() => {
    setGuideOpen(false);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("guide");
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
  const visibleAgentSourceOptions = AGENT_SOURCE_OPTIONS;

  const [showChannelModal, setShowChannelModal] = useState(false);
  const [editingChannel, setEditingChannel] = useState<any | null>(null);
  const [pageChannel, setPageChannel] = useState<any | null>(null);
  const [residentPmsEnabled, setResidentPmsEnabled] = useState(false);
  const [residentPmsAccountId, setResidentPmsAccountId] = useState("");
  const [channelForm, setChannelForm] = useState({
    mode: "existing",
    channel_type: "",
    integration_provider: "",
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
  const [goalGraphExpanded, setGoalGraphExpanded] = useState(false);
  const [newGoalDraft, setNewGoalDraft] = useState<{ title: string; target_value: string } | null>(null);
  const [abandonedGoalsOpen, setAbandonedGoalsOpen] = useState(false);
  const [settingsIdentityOpen, setSettingsIdentityOpen] = useState(false);
  const [settingsAdvancedOpen, setSettingsAdvancedOpen] = useState(false);
  const [recordsHistoryOpen, setRecordsHistoryOpen] = useState(false);
  const [evaluationDetailOpen, setEvaluationDetailOpen] = useState(false);
  const [learningReviewedOpen, setLearningReviewedOpen] = useState(false);
  const [learningEvidenceOpen, setLearningEvidenceOpen] = useState(false);
  const [expandedChannelKey, setExpandedChannelKey] = useState<string | null>(null);
  const [expandedCapabilityKey, setExpandedCapabilityKey] = useState<string | null>(null);
  const [workspaceToolsOpen, setWorkspaceToolsOpen] = useState(false);
  const [goalsDraft, setGoalsDraft] = useState("");
  const [showSettingsEditor, setShowSettingsEditor] = useState(false);
  const [settingsDraft, setSettingsDraft] = useState("");
  const [rulePrompt, setRulePrompt] = useState("");
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
  const [deliveryOpen, setDeliveryOpen] = useState(false);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [confirmDeleteWs, setConfirmDeleteWs] = useState(false);

  /* ---- queries ---- */
  const pageVisible = usePageVisible();

  const { data: workspace, isLoading, error } = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => api.workspaces.get(workspaceId!),
    enabled: !!workspaceId,
  });

  const {
    data: setupStatus,
    isLoading: setupStatusLoading,
    isError: setupStatusError,
    refetch: refetchSetupStatus,
  } = useQuery({
    queryKey: ["workspace-setup-status", workspaceId],
    queryFn: ({ signal }) => api.workspaces.setupStatus(workspaceId!, AbortSignal.any([signal, AbortSignal.timeout(15_000)])),
    enabled: pageVisible && !!workspaceId && (tab === "settings" || shouldShowWorkspaceSetup),
    staleTime: 10_000,
    retry: false,
    refetchOnWindowFocus: "always",
    refetchInterval: (query) => displayReadInterval(query.state, setupReadInterval(query.state.data)),
  });
  const hasAutomaticBlueprintSetup = Boolean(
    setupStatus?.incomplete_checks.some((check) => check.setup_job_id),
  );

  const { data: workspaceStatsData } = useQuery({
    queryKey: ["workspace-stats", workspaceId],
    queryFn: () => api.workspaces.stats.list(workspaceId!),
    enabled: !!workspaceId && tab === "goals",
  });

  const { data: heartbeatStatus } = useQuery({
    queryKey: ["workspace-heartbeat", workspaceId],
    queryFn: () => api.workspaces.heartbeat.status(workspaceId!),
    enabled: !!workspaceId && tab === "settings",
  });

  const { data: staffList, isFetched: staffFetched } = useQuery({
    queryKey: ["workspace-staff", workspaceId],
    queryFn: () => api.workspaces.staff.list(workspaceId!),
    enabled: !!workspaceId,
    staleTime: 30_000,
  });
  const canManageWs = canManageWorkspace(currentUser, staffList || []);
  // Membership, collaboration, delivery and Blueprint export stay the customer's.
  const canAdministerWs = canManageWs && !collaborator;
  const identity = `${currentUser?.id}:${currentUser?.entity_id}`;
  // Every manager of a source Workspace can open its customer trials.
  const clientTrials = useClientTrials(workspaceId || "", identity, !!workspaceId && canAdministerWs);
  const showClientTrials = canAdministerWs && (!!clientTrials.data?.length || tab === "client-trials");
  // The launch review page judges a live Workspace's runs; a simulation has none yet.
  let launchReviewPage = false;
  const canWriteWs = canWriteWorkspace(currentUser, staffList || []);
  // The server never offers deletion on a collaboration-derived seat; this keeps
  // the danger zone hidden for collaborators even from a stale response.
  const canDeleteWs = workspace?.can_delete === true && !collaborator;
  const [showBlueprintUpgrade, setShowBlueprintUpgrade] = useState(false);
  const [showPackageRelease, setShowPackageRelease] = useState(false);
  // The update-notice email links here with ?blueprint_upgrade=1: open the
  // existing upgrade dialog once, then drop the param so a refresh doesn't reopen it.
  // Once the workspace and staff have loaded the param is always consumed, even
  // when there is nothing to upgrade or the viewer can't manage the workspace.
  const blueprintUpdateStatus = workspace?.blueprint_update?.status;
  const blueprintUpgradeReady = !!workspace && staffFetched;
  useEffect(() => {
    if (searchParams.get("blueprint_upgrade") !== "1" || !blueprintUpgradeReady) return;
    if (
      canManageWs
      && (blueprintUpdateStatus === "update_available" || blueprintUpdateStatus === "reconfigure_required")
    ) {
      setShowBlueprintUpgrade(true);
    }
    const next = new URLSearchParams(searchParams);
    next.delete("blueprint_upgrade");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams, blueprintUpgradeReady, blueprintUpdateStatus, canManageWs]);

  useEffect(() => {
    if (canManageWs) return;
    setShowChannelModal(false);
    setEditingChannel(null);
    setConfirmRemoveChannel(null);
  }, [canManageWs]);

  useEffect(() => {
    if (!canDeleteWs) setConfirmDeleteWs(false);
  }, [canDeleteWs]);

  // Pool of staff members in the entity that can be picked for assignment.
  // Only fetched when the assign modal is open to avoid a hot query on tab load.
  const { data: entityStaff } = useQuery({
    queryKey: ["entity-staff"],
    queryFn: () => api.staff.list(),
    enabled: !collaborator && (showStaffModal || showWorkspaceShareModal || (!!workspaceId && tab === "staff")),
  });

  const { data: agentMappings } = useQuery({
    queryKey: ["workspace-agents", workspaceId],
    queryFn: () => api.workspaces.agents.list(workspaceId!),
    // Also load on overview/channels so we can show service<->agent matches there.
    enabled: !!workspaceId && (tab === "staff" || tab === "channels"),
  });

  // Pool of agents the user could map -- also used to render the agent
  // *name* (instead of bare ULID) on the agent mappings table and the
  // Overview "Services + Matched Agents" panel.
  const { data: entityAgents } = useQuery({
    ...agentListQueryOptions(catalogScope),
    enabled: !!catalogScope && !!workspaceId && (tab === "staff" || tab === "channels"),
  });

  const { data: channels } = useQuery({
    queryKey: ["workspace-channels", workspaceId],
    queryFn: () => api.workspaces.channels(workspaceId!),
    enabled: !!workspaceId && tab === "channels",
  });

  const {
    data: webchatResourcePages,
    isLoading: webchatResourcesLoading,
    isError: webchatResourcesFailed,
    fetchNextPage: fetchNextWebchatResourcePage,
    hasNextPage: hasMoreWebchatDocuments,
    isFetchingNextPage: loadingMoreWebchatDocuments,
  } = useInfiniteQuery({
    queryKey: ["workspace-webchat-resources", workspaceId],
    queryFn: ({ pageParam }) => api.workspaces.webchatResources(workspaceId!, pageParam),
    initialPageParam: 0,
    getNextPageParam: lastPage => lastPage.documents_next_cursor ?? undefined,
    enabled: !!workspaceId && !!pageChannel && canManageWs,
    staleTime: 10_000,
  });
  const webchatResources = useMemo(() => {
    const pages = webchatResourcePages?.pages;
    if (!pages?.length) return undefined;
    const documents = new Map(pages.flatMap(page => page.documents).map(document => [document.id, document]));
    return {
      ...pages[0],
      documents: [...documents.values()],
      documents_next_cursor: pages.at(-1)?.documents_next_cursor ?? null,
    };
  }, [webchatResourcePages]);

  const {
    data: availableChannels,
    isLoading: availableChannelsLoading,
    isError: availableChannelsFailed,
    refetch: refetchAvailableChannels,
  } = useQuery({
    queryKey: ["workspace-available-channels", workspaceId],
    queryFn: () => api.workspaces.availableChannels(workspaceId!),
    enabled: !!workspaceId && canManageWs && tab === "channels",
  });

  const {
    data: workspaceConnectionStatus,
    isPending: workspaceConnectionStatusLoading,
    isError: workspaceConnectionStatusFailed,
  } = useQuery({
    queryKey: [
      "workspace-connection-status",
      currentUser?.entity_id,
      currentUser?.id,
      workspaceId,
    ],
    queryFn: ({ signal }) => api.workspaces.connectionStatus(workspaceId!, signal),
    enabled: !!currentUser && !!workspaceId && tab === "channels",
    staleTime: 0,
    refetchOnWindowFocus: "always",
  });

  const residentPmsAccounts = useQuery({
    queryKey: ["workspace-resident-pms-accounts", workspaceId],
    queryFn: () => api.workspaces.residentPmsAccounts(workspaceId!),
    enabled: !!workspaceId && canManageWs && showChannelModal && channelForm.mode === "webchat",
    staleTime: 0,
  });

  const { data: workspaceCapabilities } = useQuery({
    queryKey: ["workspace-capabilities", workspaceId],
    queryFn: () => api.workspaces.capabilities(workspaceId!),
    enabled: !!workspaceId && tab === "channels",
  });

  const { data: capabilityIntegrationStatus } = useQuery({
    queryKey: ["workspace-capability-integration-status"],
    queryFn: () => api.integrations.mcpServers(),
    // The company's MCP servers are not part of a collaborator's session.
    enabled: privateApiEnabled && !collaborator && !!workspaceId && tab === "channels",
  });

  const { data: documents } = useQuery({
    queryKey: ["workspace-documents", workspaceId],
    queryFn: () => api.workspaces.documents(workspaceId!),
    enabled: !!workspaceId && tab === "documents",
    staleTime: 30_000,
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
    enabled: !!workspaceId && tab === "goals",
  });

  const { data: activityFeed } = useQuery({
    queryKey: ["workspace-activity", workspaceId],
    queryFn: () => api.workspaces.activity(workspaceId!, { limit: 50 }),
    enabled: !!workspaceId && tab === "records",
  });

  const { data: learningCandidates, isLoading: learningLoading } = useQuery({
    queryKey: ["workspace-learning-candidates", workspaceId],
    queryFn: () => api.workspaces.learningCandidates(workspaceId!, { limit: 30, status: null }),
    enabled: !!workspaceId && tab === "records",
  });

  const { data: runtimeEvidence, isLoading: evidenceLoading } = useQuery({
    queryKey: ["workspace-runtime-evidence", workspaceId],
    queryFn: () => api.workspaces.runtimeEvidence(workspaceId!, { limit: 30 }),
    enabled: !!workspaceId && tab === "records",
  });

  /* ---- M14 observability (Strategy + Automation health) ---- */

  const [expandedReviewId, setExpandedReviewId] = useState<string | null>(null);
  const [strategyWhyItemId, setStrategyWhyItemId] = useState<string | null>(null);

  const { data: strategyReviews } = useQuery({
    queryKey: ["workspace-strategy-reviews", workspaceId],
    queryFn: () => api.workspaces.observability.strategyReviews(workspaceId!, 10),
    enabled: !!workspaceId && tab === "records",
  });

  const { data: strategyReviewDetail, isLoading: strategyDetailLoading } = useQuery({
    queryKey: ["workspace-strategy-review-detail", workspaceId, expandedReviewId],
    queryFn: () => api.workspaces.observability.strategyReviewDetail(workspaceId!, expandedReviewId!),
    enabled: !!workspaceId && !!expandedReviewId,
  });

  // M14 Human Participation — lives in the Activity tab: it is the
  // "what happened / what is still waiting on a person" surface, and the
  // decisions + contributions it lists are activity facts.
  const { data: humanParticipation } = useQuery({
    queryKey: ["workspace-human-participation", workspaceId],
    queryFn: () => api.workspaces.humanParticipation(workspaceId!),
    enabled: !!workspaceId && tab === "records",
  });

  const { data: workspaceEvaluation, isLoading: evaluationLoading } = useQuery({
    queryKey: ["workspace-evaluation", workspaceId],
    queryFn: () => api.workspaces.evaluation(workspaceId!, 30),
    enabled: !!workspaceId && tab === "records",
    staleTime: 30_000,
  });

  const { data: budgetStatus } = useQuery({
    queryKey: ["workspace-budget", workspaceId],
    queryFn: () => api.workspaces.budget.get(workspaceId!),
    enabled: !!workspaceId && tab === "usage",
  });

  const {
    data: creditUsageSummary,
    isLoading: creditUsageSummaryLoading,
    isError: creditUsageSummaryFailed,
    refetch: refetchCreditUsageSummary,
  } = useQuery({
    queryKey: ["workspace-credit-usage-summary", workspaceId],
    queryFn: () => api.workspaces.creditUsageSummary(workspaceId!),
    enabled: !!workspaceId && tab === "usage",
    staleTime: 30_000,
  });

  const { data: workspaceAutomationSummary } = useQuery({
    queryKey: ["workspace-scheduled-job-summary", workspaceId],
    queryFn: () => api.jobs.list({ workspace_id: workspaceId!, include_workflows: true, limit: 1 }),
    enabled: !!workspaceId && tab === "settings",
    staleTime: 30_000,
  });

  const { data: workspaceAutomationBindings } = useQuery({
    queryKey: ["workspace-automation-bindings-summary", workspaceId],
    queryFn: () => api.workflows.listBindings({ workspace_id: workspaceId! }),
    enabled: !!workspaceId && tab === "settings" && flowsAvailable,
    staleTime: 30_000,
  });

  // The /operating-model endpoint wraps the model:
  //   { workspace_id, operating_model: { services, goals, rules, ... } }
  // Unwrap once here so consumers (Goals tab, Agent picker, Settings,
  // Overview summary) can read fields directly without touching the
  // envelope shape.
  const { data: operatingModelRaw } = useQuery({
    queryKey: ["workspace-operating-model", workspaceId],
    queryFn: () => api.workspaces.operatingModel(workspaceId!),
    enabled: !!workspaceId && (tab === "settings" || tab === "staff" || tab === "channels" || tab === "documents" || tab === "rules"),
  });
  const operatingModel: Record<string, any> | undefined =
    (operatingModelRaw as any)?.operating_model ?? operatingModelRaw;

  /* ---- mutations ---- */

  const reconcileBlueprintStartup = useMutation({
    mutationFn: () => api.workspaces.reconcileBlueprintStartup(workspaceId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-setup-status", workspaceId] });
      toast.success(t("page.workspace_detail.blueprint_setup_started"));
    },
    onError: (err: Error) => toast.error(
      t("page.workspace_detail.blueprint_setup_start_failed"),
      err.message,
    ),
  });

  const openWorkspaceChatDuringSetup = useCallback(() => {
    if (!workspaceId) return;
    reconcileBlueprintStartup.mutate();
    navigate(`/chat?workspace=${encodeURIComponent(workspaceId)}&setup=1${showBlueprintGuideOnArrival ? "&guide=1" : ""}`);
  }, [navigate, reconcileBlueprintStartup, showBlueprintGuideOnArrival, workspaceId]);

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
      if (!serviceKey) throw new Error("Choose or name a workspace service first.");
      if (data.source === "https" && !data.https_url.trim()) {
        throw new Error("Enter the HTTPS agent endpoint.");
      }
      const services = ((operatingModel?.services as any[]) || []);
      const newService = data.service_mode === "new"
        ? { key: serviceKey, name: data.service_name.trim() }
        : undefined;
      if (newService && services.some((svc: any) => (svc.service_key || svc.key) === serviceKey)) {
        throw new Error(t("page.workspace_detail.service_already_exists"));
      }
      const service = newService || services.find((svc: any) => (svc.service_key || svc.key) === serviceKey) || {};
      const serviceName = service.name || _humanize(serviceKey) || "Workspace Agent";
      const customPrompt = data.custom_prompt.trim() || undefined;

      const bindWorkerToMappedSubscription = async (workerId: string) => {
        const mappings = await api.workspaces.agents.list(workspaceId!);
        const mapped = (mappings || []).find((m: any) => m.service_key === serviceKey);
        if (!mapped?.id) throw new Error("The workspace agent mapping could not be found. Try connecting again.");
        await api.agents.bindSubscriptionWorker(mapped.id, {
          worker_id: workerId,
          priority: 50,
          is_preferred: true,
        });
      };

      if (data.source === "hosted") {
        if (!data.agent_id) throw new Error("Choose a Manor hosted agent.");
        await api.workspaces.agents.map(workspaceId!, {
          service_key: serviceKey,
          agent_id: data.agent_id,
          custom_prompt: customPrompt,
          new_service: newService,
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

      const agentPayload = {
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
      };
      const created = data.agent_id
        ? await api.agents.update(data.agent_id, agentPayload)
        : await api.agents.create({ ...agentPayload, workspace_id: workspaceId });
      // A failed mapping retry reconnects the Agent already created here.
      setAgentForm((current) => ({ ...current, agent_id: created.id }));

      await api.workspaces.agents.map(workspaceId!, {
        service_key: serviceKey,
        agent_id: created.id,
        custom_prompt: customPrompt,
        new_service: newService,
      });
      // The service and mapping have committed even if the connection below
      // fails. Retrying must use that existing service, not create it again.
      setAgentForm((current) => ({ ...current, service_mode: "existing" }));
      queryClient.invalidateQueries({ queryKey: ["workspace-agents", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-operating-model", workspaceId] });
      invalidateCatalogQueries(queryClient, "agents", { queryKey: ["agents", "list"] });
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
      queryClient.invalidateQueries({ queryKey: ["agent-deployments"] });

      if (data.source === "https") {
        const endpoint = data.https_url.trim();
        const displayName = data.runtime_display_name.trim() || `${created.name} HTTPS`;
        const registrationKey = JSON.stringify([created.id, serviceKey, endpoint, created.name, displayName]);
        const registration = data.worker_registration?.key === registrationKey
          ? data.worker_registration.registration
          : await api.workers.register({
          kind: "custom_http",
          display_name: displayName,
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
            protocol_version: 2,
            runtime: "https",
            endpoint_url: endpoint,
            agent_id: created.id,
            workspace_id: workspaceId,
            service_key: serviceKey,
          },
        });
        // Retain the registration and its one-time secret when only binding
        // fails, so a retry does not create another Worker.
        setAgentForm((current) => ({ ...current, worker_registration: { key: registrationKey, registration } }));
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
      invalidateCatalogQueries(queryClient, "agents", { queryKey: ["agents", "list"] });
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
      queryClient.invalidateQueries({ queryKey: ["agent-deployments"] });
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
              // The Workspace owns its service Agents; an external collaborator
              // may only create Agents inside the Workspace they were granted.
              workspace_id: workspaceId,
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
      invalidateCatalogQueries(queryClient, "agents", { queryKey: ["agents", "list"] });
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
      queryClient.invalidateQueries({ queryKey: ["agent-deployments"] });
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
      invalidateCatalogQueries(queryClient, "agents", { queryKey: ["agents", "list"] });
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
      queryClient.invalidateQueries({ queryKey: ["agent-deployments"] });
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
          ...(existing ? {} : { resident_pms: residentPmsEnabled
            ? { enabled: true, integration_account_id: residentPmsAccountId }
            : { enabled: false } }),
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
        channel_type: "",
        integration_provider: "",
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
        channel_config_id: channelForm.mode === "existing"
          ? channelForm.channel_config_id
          : undefined,
        name: channelForm.name.trim() || undefined,
        purpose: channelForm.purpose.trim(),
        role: channelForm.role,
        linked_service_key: channelForm.linked_service_key || "",
        agent_subscription_id: selectedMapping?.id,
        agent_id: selectedMapping?.agent_id,
        config: {
          login_required: channelForm.login_required,
          language: channelForm.language || "en",
          ...(channelForm.mode === "webchat" ? { resident_pms: residentPmsEnabled
            ? { enabled: true, integration_account_id: residentPmsAccountId }
            : { enabled: false } } : {}),
        },
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-available-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-dashboard", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-connection-status"] });
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

  const createGoalMut = useMutation({
    mutationFn: (data: { workspace_id: string; title: string; target_value?: string }) => api.goals.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-goals", workspaceId] });
      setNewGoalDraft(null);
    },
    onError: (e: Error) => toast.error(t("page.workspace_config.could_not_add_goal"), e.message),
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
      invalidateCatalogQueries(queryClient, "agents", { queryKey: ["agents", "list"] });
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
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
      invalidateCatalogQueries(queryClient, "agents", { queryKey: ["agents", "list"] });
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
      queryClient.invalidateQueries({ queryKey: ["agent-deployments"] });
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
      queryClient.invalidateQueries({ queryKey: ["workspace-governance", workspaceId] });
      if (currentUser?.entity_id) {
        queryClient.invalidateQueries({ queryKey: approvalKeys.entity(currentUser.entity_id) });
      }
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
      queryClient.invalidateQueries({ queryKey: ["agent-deployments"] });
      toast.success(ws?.status === "active" ? t("page.workspace_detail.workspace_paused") : t("page.workspace_detail.workspace_resumed"));
    },
    onError: (err: Error) => toast.error(t("page.dashboard.failed"), err.message),
  });

  /* ---- loading / error states ---- */

  if (isLoading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", gap: 12, color: "var(--text-muted)" }}>
        <LoadingSpinner size={20} />
        <span className="sr-only">{t("page.workspace_detail.loading")}</span>
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
          action={EMBED ? undefined : <Button variant="outline" onClick={() => navigate("/workspaces")}>{t("page.workspace_detail.back_to_workspaces")}</Button>}
        />
      </div>
    );
  }

  const ws = workspace;
  const usageGuide = workspaceUsageGuide(ws);
  const isSimulation = (ws.settings as Record<string, unknown> | undefined)?.sandbox === true || ws.kind === "sandbox";

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
    if (typeof score !== "number") return "var(--text-muted)";
    if (score >= 60) return "var(--text-strong)";
    return "#b7791f";
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

  /* ---- tab renderers ---- */

  function renderStaff() {
    const items = staffList || [];
    const accessMode = ((workspace?.settings as any)?.access_mode || "members_only") as string;
    const accessCopy = accessMode === "entity_visible"
      ? {
        title: t("ui.everyone_in_this_organization"),
        body: "Any member of this Manor organization can find and open this workspace. Only assigned members can manage it.",
      }
      : {
        title: t("ui.only_invited_members"),
        body: t("ui.only_listed_people_can_open"),
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
        title: t("ui.only_invited_members"),
        body: "Best for client work, private projects, and workspace-specific files.",
      },
      {
        value: "entity_visible",
        title: t("ui.everyone_in_organization"),
        body: "Useful for open team rooms. Anyone in this Manor organization can see it.",
      },
    ];
    const roleOptions = [
      { value: "owner", label: t("ui.owner") },
      { value: "editor", label: t("ui.editor") },
      { value: "contributor", label: t("ui.contributor") },
      { value: "viewer", label: t("ui.viewer") },
    ];
    const workspaceUrl = `${window.location.origin}/workspaces/${workspaceId || workspace?.id || ""}`;
    const workspaceChatUrl = `${window.location.origin}/chat?workspace=${encodeURIComponent(workspaceId || workspace?.id || "")}`;
    const closeShareModal = () => {
      setShowWorkspaceShareModal(false);
      setPickedStaff(null);
    };
    const shareActionStyle: React.CSSProperties = {
      width: "100%",
      minHeight: 74,
      borderRadius: 14,
      border: "0.5px solid var(--border-subtle)",
      background: "var(--surface-panel)",
      padding: "12px 14px",
      display: "grid",
      gridTemplateColumns: "34px minmax(0, 1fr)",
      gap: 12,
      alignItems: "center",
      textAlign: "left",
      cursor: "pointer",
      color: "var(--text-default)",
    };
    const shareIconStyle: React.CSSProperties = {
      width: 34,
      height: 34,
      borderRadius: 10,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      background: "var(--surface-muted)",
      color: "var(--text-muted)",
    };
    const shareAction = (
      title: string,
      body: string,
      icon: React.ReactNode,
      onClick: () => void,
    ) => (
      <button type="button" style={shareActionStyle} onClick={onClick}>
        <span style={shareIconStyle}>{icon}</span>
        <span style={{ minWidth: 0 }}>
          <span style={{ display: "block", fontSize: 13, fontWeight: 800, color: "var(--text-strong)" }}>{title}</span>
          <span style={{ display: "block", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.35, marginTop: 3 }}>{body}</span>
        </span>
      </button>
    );
    return (
      <ConfigPage title={t("page.workspace_detail.staff")}>
        <ConfigGroup title={`${t("page.workspace_config.people")} · ${items.length}`}>
          {items.map((member: WorkspaceStaff) => {
            const matched = (entityStaff || []).find((es: any) => es.id === member.staff_id);
            const name = matched ? _staffLabel(matched) : (member.display_name || member.email || member.staff_id || member.user_id || "—");
            const expired = !!member.expires_at && new Date(member.expires_at) < new Date();
            return (
              <ConfigRow
                key={member.id}
                leading={<UserAvatar name={name} avatarUrl={matched?.avatar_url} size={32} />}
                label={name}
                note={[
                  member.role,
                  expired
                    ? t("page.workspace_config.expired")
                    : member.expires_at
                      ? `${t("ui.expires")} ${new Date(member.expires_at).toLocaleDateString(undefined, { timeZone: getPreferredTimeZone() })}`
                      : "",
                  member.status && member.status !== "active" ? member.status : "",
                  formatDate(member.added_at || member.created_at),
                ].filter(Boolean).join(" · ")}
                meta={member.collaboration_role ? <_CollaborationSeatPill role={member.collaboration_role} team={member.collaboration_team} /> : undefined}
                control={canAdministerWs && !member.collaboration_role ? (
                  <Button variant="ghost" size="sm" onClick={() => setConfirmRemoveStaff(_seatRemovalId(member))}>
                    {t("page.task_detail.runtime.remove_rule")}
                  </Button>
                ) : undefined}
              />
            );
          })}
          {canAdministerWs && <ConfigAddRow label={t("page.workspace_config.add_colleague")} onClick={() => setShowStaffModal(true)} />}
          {items.length === 0 && !canAdministerWs && (
            <ConfigBlock><p className="config-empty">{t("page.workspace_detail.assign_staff_members_to_this_workspace")}</p></ConfigBlock>
          )}
        </ConfigGroup>

        {renderAgents()}

        <ConfigGroup title={t("ui.workspace_access")}>
          <ConfigRow
            label={accessCopy.title}
            note={accessCopy.body}
            control={canAdministerWs ? (
              <Button variant="outline" size="sm" onClick={() => setShowWorkspaceShareModal(true)}>
                {t("ui.share_workspace")}
              </Button>
            ) : undefined}
          />
        </ConfigGroup>

        <Modal
          open={canAdministerWs && showWorkspaceShareModal}
          onClose={() => { setShowWorkspaceShareModal(false); setPickedStaff(null); }}
          title={t("ui.share_workspace")}
          footer={
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <Button variant="outline" onClick={() => { setShowWorkspaceShareModal(false); setPickedStaff(null); }}>
                {t("ui.cancel")}
              </Button>
              <Button variant="primary" onClick={() => { setShowWorkspaceShareModal(false); setPickedStaff(null); }}>
                {t("ui.done")}
              </Button>
            </div>
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <div style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
              gap: 10,
              padding: 12,
              borderRadius: 18,
              background: "var(--surface-muted)",
              border: "0.5px solid var(--border-subtle)",
            }}>
              {shareAction(
                "Copy workspace link",
                "Send people to this workspace. Access still follows the rules below.",
                <IconCopy size={16} />,
                async () => {
                  const ok = await copyTextToClipboard(workspaceUrl);
                  if (ok) toast.success("Workspace link copied");
                  else toast.error("Could not copy link");
                },
              )}
              {shareAction(
                "Open workspace chat",
                "Jump to the shared operating room conversation.",
                <IconChat size={16} />,
                () => {
                  closeShareModal();
                  navigate(`/chat?workspace=${encodeURIComponent(workspaceId || workspace?.id || "")}`);
                },
              )}
              {shareAction(
                "Export blueprint",
                "Package this workspace setup so another team can install it.",
                <IconDownload size={16} />,
                () => {
                  closeShareModal();
                  setExportOpen(true);
                },
              )}
              <div style={{
                gridColumn: "1 / -1",
                display: "flex",
                alignItems: "center",
                gap: 8,
                minWidth: 0,
                color: "var(--text-muted)",
                fontSize: 11,
                fontWeight: 700,
                padding: "0 2px",
              }}>
                <IconShare size={13} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {workspaceChatUrl}
                </span>
              </div>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div>
                <div style={LABEL}>{t("ui.add_people")}</div>
                <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.45 }}>
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
                      {t("ui.add")}
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
                  <div style={LABEL}>{t("ui.people_with_access")}</div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    {t("ui.these_people_can_open_this_workspace_even_when_g")}
                  </div>
                </div>
                <span style={{ fontSize: 11, fontWeight: 700, color: "var(--text-faint)" }}>
                  {items.length} {items.length === 1 ? "member" : "members"}
                </span>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {items.length === 0 ? (
                  <div style={{ fontSize: 12, color: "var(--text-faint)", padding: "12px 4px" }}>
                    {t("ui.no_invited_members_yet")}
                  </div>
                ) : items.map((s: WorkspaceStaff) => {
                  const matched = staffById.get(s.staff_id || "");
                  const displayName = matched ? _staffLabel(matched) : (s.display_name || s.email || s.staff_id || s.user_id || "Unknown member");
                  const email = matched?.email || s.email || s.user_id || "Workspace member";
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
                        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-default)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {displayName}
                          {expired && <span style={{ color: "#a23e38", marginLeft: 6, fontSize: 10, fontWeight: 500 }}>expired</span>}
                        </div>
                        <div style={{ fontSize: 11, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {email}
                          {s.expires_at && !expired && ` · Expires ${new Date(s.expires_at).toLocaleDateString(undefined, { timeZone: getPreferredTimeZone() })}`}
                        </div>
                      </div>
                      {s.collaboration_role ? (
                        <>
                          <_CollaborationSeatPill role={s.collaboration_role} team={s.collaboration_team} />
                          <span />
                        </>
                      ) : (
                        <>
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
                            // A seat without a Staff row is removed by its own id; a
                            // collaboration seat (above) follows its grant instead.
                            disabled={removeStaff.isPending}
                            onClick={() => removeStaff.mutate(_seatRemovalId(s))}
                            aria-label={t("ui.remove_access")}
                            title={t("ui.remove_access")}
                            style={{
                              width: 26,
                              height: 26,
                              borderRadius: 6,
                              border: "none",
                              background: "transparent",
                              color: "var(--text-faint)",
                              cursor: removeStaff.isPending ? "not-allowed" : "pointer",
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              fontSize: 18,
                              lineHeight: 1,
                            }}
                          >
                            ×
                          </button>
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            <div>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", marginBottom: 8 }}>
                <div>
                  <div style={LABEL}>{t("ui.general_access")}</div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    {t("ui.controls_whether_this_workspace_can_be_discovere")}
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
                        border: selected ? "0.5px solid var(--border-subtle)" : "0.5px solid var(--border-subtle)",
                        background: selected ? "var(--surface-muted)" : "var(--surface-panel)",
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
                          border: selected ? "5px solid var(--text-default)" : "0.5px solid var(--border-subtle)",
                          background: "var(--surface-panel)",
                          marginTop: 1,
                        }}
                      />
                      <span>
                        <span style={{ display: "block", fontSize: 13, fontWeight: 700, color: "var(--text-default)" }}>
                          {opt.title}
                        </span>
                        <span style={{ display: "block", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.45, marginTop: 3 }}>
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
          open={canAdministerWs && showStaffModal}
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
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 6 }}>
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
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 6 }}>
                  {t("page.workspace_detail.no_staff_in_this_entity_yet_invite_one_from_the")}
                </div>
              )}
            </div>
            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 6 }}>
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
                ]}
              />
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
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
      </ConfigPage>
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
      !operatingModel || !agentForm.service_key ||
      (agentForm.service_mode === "new" && (!agentForm.service_name.trim() || serviceByKey.has(agentForm.service_key) || existingByKey.has(agentForm.service_key))) ||
      (agentForm.source === "hosted" && !agentForm.agent_id) ||
      (agentForm.source === "https" && !agentForm.https_url.trim()) ||
      mapAgent.isPending;
    const replacingAgent = agentForm.service_mode === "existing" && existingByKey.has(agentForm.service_key);
    const autoMapLabel = (() => {
      const withRec = pendingSuggestions.filter((suggestion) => suggestion.recommended_agent_id).length;
      const toCreate = pendingSuggestions.length - withRec;
      if (withRec > 0 && toCreate > 0) return t("page.workspace_config.auto_map_and_create", { map: withRec, create: toCreate });
      if (withRec > 0) return t("page.workspace_config.auto_map", { count: withRec });
      return t("page.workspace_config.auto_create", { count: toCreate });
    })();
    return (
      <>
        <ConfigGroup title={`${t("page.workspace_config.ai_staff")} · ${items.length}`}>
          {(items as any[]).map((mapping: any, i: number) => {
            const matched = (entityAgents || []).find((agent: any) => agent.id === mapping.agent_id);
            const external = String(matched?.config?.runtime_connection?.source || "manor_hosted") === "https";
            const service = mapping.service_key
              ? _serviceLabel(serviceByKey.get(mapping.service_key) || { service_key: mapping.service_key })
              : "";
            return (
              <ConfigRow
                key={mapping.service_key || i}
                leading={<AgentAvatar name={matched ? _agentLabel(matched) : "AI"} avatarUrl={matched?.avatar_url} size={32} />}
                label={matched ? _agentLabel(matched) : (mapping.agent_id || "—")}
                note={[
                  service ? t("page.workspace_config.responsible_for", { service }) : "",
                  external ? "HTTPS" : "",
                  String(mapping.custom_prompt || "").trim() ? t("ui.workspace_override") : "",
                ].filter(Boolean).join(" · ")}
                control={(
                  <span className="config-row-actions">
                    {matched && (
                      <Button variant="ghost" size="sm" onClick={() => openAgentEditModal(matched.id)}>
                        {t("action.edit")}
                      </Button>
                    )}
                    <Button variant="ghost" size="sm" onClick={() => setConfirmUnmapAgent(mapping.service_key)}>
                      {t("page.workspace_detail.unmap")}
                    </Button>
                  </span>
                )}
              />
            );
          })}
          {pendingSuggestions.length > 0 && (
            <ConfigRow
              label={t("page.workspace_config.services_without_staff", { count: pendingSuggestions.length })}
              control={(
                <Button variant="outline" size="sm" onClick={() => autoMapAgents.mutate(pendingSuggestions)} loading={autoMapAgents.isPending}>
                  {autoMapLabel}
                </Button>
              )}
            />
          )}
          <ConfigAddRow
            label={t("page.workspace_config.add_ai_staff")}
            onClick={() => {
              setAgentForm(DEFAULT_AGENT_FORM);
              setAgentConnectionResult(null);
              setShowAgentModal(true);
            }}
          />
        </ConfigGroup>

        <Modal
          open={showAgentModal}
          onClose={closeAgentModal}
          title={t("ui.add_agent_to_workspace")}
          footer={
            agentConnectionResult ? (
              <Button variant="primary" onClick={closeAgentModal}>{t("ui.done")}</Button>
            ) : (
              <>
                <Button variant="outline" onClick={closeAgentModal}>{t("action.cancel")}</Button>
                <Button
                  variant="primary"
                  disabled={submitAgentDisabled}
                  loading={mapAgent.isPending}
                  onClick={() => mapAgent.mutate(agentForm)}
                >
                  {mapAgent.isPending ? t("page.agents.saving") : replacingAgent ? t("page.workspace_detail.replace_service_agent") : "Add agent"}
                </Button>
              </>
            )
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {agentConnectionResult ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <div style={{ border: "1px solid rgba(79,125,117,0.22)", borderRadius: 14, background: "var(--surface-muted)", padding: 14 }}>
                  <div style={{ fontSize: 13, fontWeight: 800, color: "var(--text-muted)" }}>
                    {agentConnectionResult.agentName} is connected to this workspace.
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 6 }}>
                    Use this connection secret in the HTTPS agent once. Manor only stores its hash.
                  </div>
                  <pre style={{ margin: "10px 0 0", padding: 12, borderRadius: 10, background: "var(--surface-panel)", color: "var(--text-default)", whiteSpace: "pre-wrap", wordBreak: "break-all", fontSize: 12 }}>
                    {agentConnectionResult.registration.worker_secret}
                  </pre>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    Endpoint: {agentConnectionResult.endpoint}
                  </div>
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
                          background: selected ? "#f5f5f7" : "#fff",
                          borderRadius: 14,
                          padding: 14,
                          cursor: "pointer",
                          minHeight: 104,
                        }}
                      >
                        <div style={{ fontSize: 13, fontWeight: 800, color: selected ? "#1d1d1f" : "#48484a" }}>{source.title}</div>
                        <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.45, marginTop: 6 }}>{source.body}</div>
                      </button>
                    );
                  })}
                </div>

            <div className="flex flex-col gap-3">
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)" }}>
                {t("page.workspace_detail.service")}
              </div>
              <Select
                ariaLabel={t("page.workspace_detail.service_assignment")}
                value={agentForm.service_mode}
                onChange={(value) => setAgentForm({ ...agentForm, service_mode: value as "new" | "existing", service_key: "", service_name: "", agent_id: agentForm.source === "hosted" ? "" : agentForm.agent_id })}
                options={[
                  { value: "new", label: t("page.workspace_detail.new_service") },
                  { value: "existing", label: t("page.workspace_detail.existing_service") },
                ]}
              />
              {agentForm.service_mode === "new" ? (
                <>
                  <Input
                    label={t("page.workspace_detail.new_service_name")}
                    value={agentForm.service_name}
                    onChange={(event) => {
                      const name = event.target.value;
                      setAgentForm({ ...agentForm, service_name: name, service_key: name.trim().toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "_").replace(/^_+|_+$/g, "") });
                    }}
                  />
                  {(serviceByKey.has(agentForm.service_key) || existingByKey.has(agentForm.service_key)) && (
                    <p role="alert" className="text-xs text-stone-500">{t("page.workspace_detail.service_already_exists")}</p>
                  )}
                </>
              ) : (
              <Select
                ariaLabel={t("page.workspace_detail.existing_service")}
                value={agentForm.service_key}
                onChange={(v) => setAgentForm({ ...agentForm, service_key: v, agent_id: agentForm.source === "hosted" ? "" : agentForm.agent_id })}
                placeholder={
                  operatingModel
                    ? t("page.workspace_detail.select_a_service_from_the_operating_model")
                    : t("page.workspace_detail.loading_services")
                }
                filterable
                options={(((operatingModel as any)?.services as any[]) || []).map((svc: any) => ({
                  value: svc.service_key || svc.key || "",
                  label: `${_serviceLabel(svc)}${existingByKey.has(svc.service_key || svc.key) ? ` · ${t("page.workspace_detail.replace_service_agent")}` : ""}`,
                }))}
              />
              )}
              {replacingAgent && <p role="status" className="text-xs text-stone-500">{t("page.workspace_detail.replace_service_agent_description")}</p>}
              {operatingModel && (((operatingModel as any).services as any[]) || []).length === 0 && (
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 6 }}>
                  {t("page.workspace_detail.no_services_defined_in_this_workspace_s_operatin")}
                </div>
              )}
            </div>

            {agentForm.source === "hosted" ? (
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 6 }}>
                  {t("ui.manor_hosted_agent")}
                </div>
                <Select
                  ariaLabel={t("ui.manor_hosted_agent")}
                  value={agentForm.agent_id}
                  onChange={(v) => setAgentForm({ ...agentForm, agent_id: v })}
                  placeholder={entityAgents ? t("page.workspace_detail.select_an_agent") : t("page.workspace_detail.loading_agents")}
                  filterable
                  options={filterWorkspaceAgentMappingOptions(entityAgents || [], workspaceId).filter((agent: any) => !items.some((mapping: any) => (
                    mapping.agent_id === agent.id && (agentForm.service_mode === "new" || mapping.service_key !== agentForm.service_key)
                  ))).map((a: any) => ({
                    value: a.id,
                    label: _agentLabel(a),
                  }))}
                />
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))", gap: 12 }}>
                <Input
                  label={t("ui.agent_name")}
                  value={agentForm.agent_name}
                  onChange={(e) => setAgentForm({ ...agentForm, agent_name: e.target.value })}
                  placeholder={t("ui.example_leasing_follow_up_agent")}
                />
                {agentForm.source === "https" && (
                  <Input
                    label={t("ui.https_connection_name")}
                    value={agentForm.runtime_display_name}
                    onChange={(e) => setAgentForm({ ...agentForm, runtime_display_name: e.target.value })}
                    placeholder={t("ui.example_hermes_endpoint")}
                  />
                )}
              </div>
            )}

            {agentForm.source === "https" && (
              <Input
                label={t("ui.https_endpoint")}
                value={agentForm.https_url}
                onChange={(e) => setAgentForm({ ...agentForm, https_url: e.target.value })}
                placeholder="https://agent.example.com/manor"
              />
            )}

            {agentForm.source !== "hosted" && (
              <>
                <Input
                  label={t("ui.description")}
                  value={agentForm.description}
                  onChange={(e) => setAgentForm({ ...agentForm, description: e.target.value })}
                  placeholder={t("ui.what_this_agent_owns_in_the_workspace")}
                />
                <Textarea
                  label={t("ui.system_prompt")}
                  rows={4}
                  value={agentForm.system_prompt}
                  onChange={(e) => setAgentForm({ ...agentForm, system_prompt: e.target.value })}
                  placeholder={t("ui.leave_blank_to_generate_a_focused_workspace_prom")}
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
      </>
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

    const toolNames = (items: any[]) => items
      .map((tool) => _friendlyCodeLabel(typeof tool === "string" ? tool : (tool.display_name || tool.name)))
      .join("、");

    return (
      <ConfigGroup title={t("page.workspace_config.what_staff_can_use")}>
        {!hasAnyCapabilities && (
          <ConfigBlock><p className="config-empty">{t("page.workspace_detail.map_agents_to_workspace_services_then_bind_tools")}</p></ConfigBlock>
        )}
        {workspaceMissing.length > 0 && (
          <ConfigRow
            label={t("page.workspace_detail.workspace_wide_integrations_needed")}
            note={workspaceMissing.map((flag: any) => String(flag.provider || "integration").replace(/[_\-]+/g, " ")).join("、")}
            control={!collaborator ? <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>{t("page.apps.connect")}</Button> : undefined}
          />
        )}
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
          const rowKey = svc.agent_subscription_id || serviceKey || String(idx);
          const open = expandedCapabilityKey === rowKey;
          return (
            <div key={rowKey}>
              <ConfigRow
                leading={agent
                  ? <AgentAvatar name={agent.name} avatarUrl={agent.avatar_url} seed={agent.id} size={28} shape="rounded" />
                  : undefined}
                label={agent?.name || t("page.workspace_detail.no_agent")}
                note={_serviceLabel(service || { service_key: serviceKey })}
                meta={[
                  `${tools.length + alwaysTools.length + contextualTools.length} ${t("page.workspace_detail.tools")}`,
                  `${skills.length} ${t("nav.skills")}`,
                  missing.length > 0 ? t("page.workspace_detail.needs_setup_2") : "",
                ].filter(Boolean).join(" · ")}
                expanded={open}
                onClick={() => setExpandedCapabilityKey(open ? null : rowKey)}
              />
              {open && (
                <>
                  {alwaysTools.length > 0 && <ConfigRow label={t("page.workspace_detail.always_loaded")} note={toolNames(alwaysTools)} />}
                  {contextualTools.length > 0 && <ConfigRow label={t("page.workspace_detail.contextual_tools")} note={toolNames(contextualTools)} />}
                  {integrations.map((integration: any) => {
                    const live = integrationStatusByKey.get(integration.server_key);
                    const ready = live ? Boolean(live.agent_can_use) : Boolean(integration.ready);
                    return (
                      <ConfigRow
                        key={integration.binding_id || integration.server_key}
                        label={integration.name || _friendlyCodeLabel(integration.server_key)}
                        note={ready ? t("page.workspaces.ready") : t("page.workspace_detail.needs_setup_2")}
                      />
                    );
                  })}
                  {tools.length > 0 && <ConfigRow label={t("page.workspace_detail.tools")} note={toolNames(tools)} />}
                  {skills.length > 0 && (
                    <ConfigRow
                      label={t("nav.skills")}
                      note={skills.map((skill: any) => skill.display_name || skill.name || _friendlyCodeLabel(skill.slug)).join("、")}
                    />
                  )}
                  {missing.length > 0 && (
                    <ConfigRow
                      label={t("page.workspace_detail.needs_setup_2")}
                      note={missing.map((flag: any) => String(flag.provider || "integration").replace(/[_\-]+/g, " ")).join("、")}
                    />
                  )}
                  {agent && (
                    <ConfigRow label={t("page.workspace_detail.edit_agent")} chevron onClick={() => openAgentEditModal(agent.id)} />
                  )}
                </>
              )}
            </div>
          );
        })}
        {(workspaceRuntimeTools.length > 0 || workspaceContextualTools.length > 0) && (
          <>
            <ConfigRow
              label={t("page.workspace_detail.workspace_runtime_tools")}
              expanded={workspaceToolsOpen}
              onClick={() => setWorkspaceToolsOpen((open) => !open)}
            />
            {workspaceToolsOpen && workspaceRuntimeTools.length > 0 && (
              <ConfigRow label={t("page.workspace_detail.always_loaded")} note={toolNames(workspaceRuntimeTools)} />
            )}
            {workspaceToolsOpen && workspaceContextualTools.length > 0 && (
              <ConfigRow label={t("page.workspace_detail.contextual_tools")} note={toolNames(workspaceContextualTools)} />
            )}
          </>
        )}
      </ConfigGroup>
    );
  }

  function renderChannels() {
    const channelConfigs: any[] = channels || [];
    const items = channelConfigs.filter((ch) => ch.channel_binding_id);
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
    const toolIntegrationByProvider = new Map<string, any>();
    for (const flag of extraIntegrationFlags) {
      const provider = String(flag.provider || "").trim().toLowerCase();
      if (provider) toolIntegrationByProvider.set(provider, { ...flag, provider, live: false });
    }
    for (const requirement of (workspaceConnectionStatus?.requirements || [])) {
      if (requirement.kind !== "integration") continue;
      const provider = String(requirement.provider || "").trim().toLowerCase();
      if (!provider) continue;
      const current = toolIntegrationByProvider.get(provider);
      if (!current || (requirement.required && !requirement.ready)) {
        toolIntegrationByProvider.set(provider, { ...current, ...requirement, provider, live: true });
      }
    }
    const toolIntegrationEntries = Array.from(toolIntegrationByProvider.values());
    const toolIntegrationIssueCount = toolIntegrationEntries.filter((entry) => (
      entry.live && entry.required && !entry.ready
    )).length;
    const serviceOptions = mappings
      .filter((m: any) => m.service_key)
      .map((m: any) => {
        const agent = agentById.get(m.agent_id);
        const serviceLabel = _serviceLabel(serviceByKey.get(m.service_key) || { service_key: m.service_key });
        const label = agent ? `${serviceLabel} → ${_agentLabel(agent)}` : serviceLabel;
        return { value: m.service_key, label };
      });
    const currentExternalChannel = editingChannel && editingChannel.channel_type !== "webchat"
      ? editingChannel
      : null;
    const externalConnectionItems = availableItems
      .filter((ch) => ch.channel_type !== "webchat" && ch.channel_type !== "internal_chat");
    if (
      currentExternalChannel
      && !externalConnectionItems.some((ch) => ch.id === currentExternalChannel.id)
    ) {
      externalConnectionItems.unshift(currentExternalChannel);
    }
    const compatibleConnectionItems = externalConnectionItems.filter((ch) => (
      !channelForm.channel_type || ch.channel_type === channelForm.channel_type
    ));
    const selectedConnection = compatibleConnectionItems.find(
      (ch) => ch.id === channelForm.channel_config_id,
    );
    const selectedIntegrationProvider = String(
      selectedConnection?.integration_provider
      || channelForm.integration_provider
      || "",
    );
    const integrationOptions = Array.from(new Set(
      compatibleConnectionItems
        .map((ch) => String(ch.integration_provider || ch.provider || ch.channel_type || ""))
        .filter(Boolean),
    )).map((provider) => ({ value: provider, label: _humanize(provider) }));
    const accountItems = compatibleConnectionItems.filter((ch) => (
      String(ch.integration_provider || ch.provider || ch.channel_type || "")
      === selectedIntegrationProvider
    ));
    const canAttachChannel =
      !attachChannel.isPending &&
      (channelForm.mode !== "existing" || !!channelForm.channel_config_id);
    const isEditingChannel = !!editingChannel;
    const canSaveChannel =
      canManageWs
      && (channelForm.mode !== "webchat" || !residentPmsEnabled || (!residentPmsAccounts.isFetching && !residentPmsAccounts.isError && !!residentPmsAccounts.data?.accounts.some(account => account.id === residentPmsAccountId)))
      && (channelForm.mode !== "existing" || !!channelForm.channel_config_id)
      && (isEditingChannel ? !updateChannel.isPending : canAttachChannel);
    const closeChannelModal = () => {
      setShowChannelModal(false);
      setEditingChannel(null);
    };
    const openAddChannel = () => {
      if (!canManageWs) return;
      setEditingChannel(null);
      setResidentPmsEnabled(false); setResidentPmsAccountId("");
      setChannelForm({
        mode: "existing",
        channel_type: "",
        integration_provider: "",
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
      if (!canManageWs) return;
      const cfg = ch.config || {};
      setResidentPmsEnabled(cfg.resident_pms?.enabled === true);
      setResidentPmsAccountId(cfg.resident_pms?.integration_account_id || "");
      const linkedServiceKey = cfg.linked_service_key || cfg.service_key || "";
      setEditingChannel(ch);
      setChannelForm({
        mode: ch.channel_type === "webchat" ? "webchat" : "existing",
        channel_type: ch.channel_type || "",
        integration_provider: ch.integration_provider || ch.provider || "",
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
    const openPlannedChannel = (entry: typeof plannedChannels[number]) => {
      if (!canManageWs || entry.isInternal) return;
      setResidentPmsEnabled(false); setResidentPmsAccountId("");
      const boundChannel = items.find((ch) => (
        ch.channel_binding_id
        && String(ch.channel_type || ch.provider || "").toLowerCase() === entry.channelType
      ));
      if (boundChannel) {
        openEditChannel(boundChannel);
        return;
      }
      const candidates = availableItems.filter((ch) => (
        String(ch.channel_type || ch.provider || "").toLowerCase() === entry.channelType
      ));
      const providerKeys = Array.from(new Set(candidates.map((ch) => (
        String(ch.integration_provider || ch.provider || ch.channel_type || "")
      )).filter(Boolean)));
      const planChannel = entry.channel || {};
      setEditingChannel(null);
      setChannelForm({
        mode: "existing",
        channel_type: entry.channelType,
        integration_provider: providerKeys.length === 1 ? providerKeys[0] : "",
        channel_config_id: candidates.length === 1 ? candidates[0].id : "",
        name: planChannel.name || "",
        purpose: planChannel.purpose || entry.missingFlag?.purpose || "",
        linked_service_key: planChannel.linked_service_key
          || planChannel.service_key
          || entry.missingFlag?.linked_service_keys?.[0]
          || mappings[0]?.service_key
          || "",
        role: String(entry.role || "primary_external").toLowerCase().replace(/\s+/g, "_"),
        login_required: Boolean(planChannel.login_required),
        language: (planChannel.language || "en").toLowerCase().split(/[-_]/)[0],
      });
      setShowChannelModal(true);
    };

    const copyWithToast = (value: string, message: string) => {
      void navigator.clipboard.writeText(value);
      toast.success(message);
    };
    const copyField = (label: string, help: string | null, value: string, message: string) => (
      <ConfigBlock>
        <span className="config-block-label">{label}</span>
        {help && <p className="config-block-text">{help}</p>}
        <div className="config-copy">
          <code>{value}</code>
          <Button variant="ghost" size="sm" onClick={() => copyWithToast(value, message)}>{t("page.workspace_detail.copy")}</Button>
        </div>
      </ConfigBlock>
    );
    const unboundPlans = plannedChannels.filter((entry) => !entry.isInternal && !items.some((ch) => (
      String(ch.channel_type || ch.provider || "").toLowerCase() === entry.channelType
    )));

    return (
      <ConfigPage title={TAB_LABELS.channels}>
        <ConfigGroup title={t("page.workspace_config.accounts_for_staff")}>
          {workspaceConnectionStatusLoading ? (
            <ConfigRow label={t("page.workspace_detail.checking_integration_status")} />
          ) : workspaceConnectionStatusFailed ? (
            <ConfigRow label={t("page.workspace_detail.integration_status_unavailable")} />
          ) : toolIntegrationEntries.map((entry: any, idx: number) => {
            const needsSetup = entry.live && entry.required && !entry.ready;
            const providerLabel = _humanize(entry.label || entry.provider);
            // One that needs setup is connected here with the same row as everywhere.
            if (needsSetup && entry.provider) {
              return (
                <div key={`${entry.provider}-${idx}`} className="config-connect-row">
                  <IntegrationConnectRow
                    as="div"
                    workspaceId={workspaceId}
                    requirement={{ kind: "integration", provider: entry.provider, label: providerLabel, setup_kind: entry.setup_kind }}
                    hint={entry.reason || undefined}
                  />
                </div>
              );
            }
            return (
              <ConfigRow
                key={`${entry.provider || "integration"}-${idx}`}
                label={<InlineIntegrationLink provider={entry.provider} label={providerLabel} />}
                note={entry.reason || (entry.live
                  ? entry.ready
                    ? t("page.workspace_detail.ready_for_agents")
                    : entry.required
                      ? t("page.workspace_detail.needs_setup_2")
                      : t("page.workspace_detail.optional")
                  : t("page.workspace_detail.required_by_plan"))}
              />
            );
          })}
          {!collaborator
            ? <ConfigAddRow label={t("page.workspace_config.add_account")} onClick={() => navigate("/integrations")} />
            : toolIntegrationEntries.length === 0 && <ConfigBlock><p className="config-empty">{t("page.workspace_detail.no_integration_setup_required")}</p></ConfigBlock>}
        </ConfigGroup>

        <ConfigGroup title={t("page.workspace_config.how_people_reach_here")}>
          {unboundPlans.map((entry, idx) => {
            const compatibleAccounts = availableItems.filter((ch) => (
              String(ch.channel_type || ch.provider || "").toLowerCase() === entry.channelType
            ));
            return (
              <ConfigRow
                key={`${entry.role}-${entry.channelType || idx}`}
                label={_channelKindLabel({ channel_type: entry.channelType, provider: entry.channelType, name: entry.channel?.name })}
                note={[
                  entry.channel?.purpose || entry.missingFlag?.purpose,
                  entry.missingFlag?.required ? t("page.login.required") : "",
                  entry.channel?.login_required ? t("page.workspace_detail.login_required") : "",
                ].filter(Boolean).join(" · ") || t("page.workspace_detail.no_channel_account_bound")}
                control={canManageWs ? (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={availableChannelsLoading}
                    onClick={() => {
                      if (availableChannelsFailed) void refetchAvailableChannels();
                      else if (compatibleAccounts.length > 0) openPlannedChannel(entry);
                      else navigate("/integrations");
                    }}
                  >
                    {availableChannelsFailed
                      ? t("page.workspace_detail.retry")
                      : compatibleAccounts.length > 0
                        ? t("page.workspace_detail.choose_connection")
                        : t("page.workspace_detail.connect_account")}
                  </Button>
                ) : undefined}
              />
            );
          })}
          {items.map((ch, i) => {
            const cfg = ch.config || {};
            const linkedServiceKey = cfg.linked_service_key || cfg.service_key || null;
            // Prefer backend-resolved bound_agent, fallback to subscription lookup
            const boundAgent = ch.bound_agent || (() => {
              if (!linkedServiceKey) return null;
              const mapping = mappings.find((m) => m.service_key === linkedServiceKey);
              return mapping ? agentById.get(mapping.agent_id) : null;
            })();
            const isWebchat = ch.channel_type === "webchat";
            const isInternal = _isInternalChannel(ch);
            const channelTitle = _channelDisplayName(ch);
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
            const open = expandedChannelKey === channelKey;
            return (
              <div key={ch.id || i}>
                <ConfigRow
                  label={channelTitle}
                  note={[
                    _channelKindLabel(ch),
                    accessLabel,
                    boundAgent ? (boundAgent.name || _agentLabel(boundAgent)) : isInternal ? t("page.workspace_detail.built_in_workspace_chat") : t("page.workspace_detail.no_agent_bound"),
                  ].filter(Boolean).join(" · ")}
                  expanded={open}
                  onClick={() => setExpandedChannelKey(open ? null : channelKey)}
                />
                {open && (
                  <>
                    {cfg.purpose && <ConfigRow label={t("page.workspace_detail.purpose")} note={cfg.purpose} />}
                    {!isWebchat && !isInternal && (
                      <ConfigRow
                        label={_channelIntegrationLabel(ch)}
                        note={[_channelAccountLabel(ch), ch.source_scope === "shared" ? t("page.workspace_detail.shared_integration") : t("page.workspace_detail.workspace_channel")].join(" · ")}
                      />
                    )}
                    <ConfigRow label={t("settings.language")} control={<span className="config-value">{_channelLanguageLabel(cfg.language || "en")}</span>} />
                    <ConfigRow
                      label={t("page.workspace_detail.handled_by")}
                      note={boundAgent && linkedServiceKey ? _serviceLabel(serviceByKey.get(linkedServiceKey) || { service_key: linkedServiceKey }) : undefined}
                      control={boundAgent
                        ? <span className="config-value">{boundAgent.name || _agentLabel(boundAgent)}</span>
                        : !isInternal
                          ? <Button variant="ghost" size="sm" onClick={() => handleTabChange("staff")}>{t("page.workspace_detail.map_agent_2")}</Button>
                          : undefined}
                    />
                    {!isInternal && (
                      <ConfigRow
                        label={t("page.workspace_detail.view_conversations")}
                        chevron
                        onClick={() => navigate(`/settings?tab=history&workspace=${encodeURIComponent(workspaceId!)}&channel=${encodeURIComponent(ch.channel_type || "")}`)}
                      />
                    )}
                    {isWebchat && publicToken && (
                      <>
                        <ConfigBlock>
                          <span className="config-block-label">{t("page.workspace_detail.public_chat_link")}</span>
                          <div className="config-copy">
                            <code>{publicChatUrl}</code>
                            <Button variant="ghost" size="sm" onClick={() => copyWithToast(publicChatUrl, t("page.workspace_detail.link_copied"))}>
                              {t("page.workspace_detail.copy")}
                            </Button>
                          </div>
                          <img
                            className="config-qr"
                            src={qrCodeApiUrl}
                            alt={t("page.workspace_detail.qr_code")}
                            onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                          />
                        </ConfigBlock>
                        <ConfigRow
                          label={t("page.workspace_config.embed_and_api")}
                          note={t("page.workspace_detail.public_token_no_api_key")}
                          expanded={developerSettingsOpen}
                          onClick={() => setExpandedChannelDeveloperSettings((prev) => ({ ...prev, [channelKey]: !prev[channelKey] }))}
                        />
                        {developerSettingsOpen && (
                          <>
                            {copyField(t("page.workspace_detail.embed_script"), t("page.workspace_detail.paste_embed_script_before_body"), embedSnippet, t("page.workspace_detail.embed_code_copied"))}
                            {copyField(t("page.workspace_detail.embed_api"), t("page.workspace_detail.embed_api_help"), embedApiUrl, t("page.workspace_detail.api_endpoint_copied"))}
                            {copyField(t("page.workspace_detail.qr_code_api"), t("page.workspace_detail.qr_code_api_help"), qrCodeApiUrl, t("page.workspace_detail.api_endpoint_copied"))}
                          </>
                        )}
                      </>
                    )}
                    {canManageWs && ch.channel_binding_id && (
                      <ConfigBlock>
                        <div className="config-block-actions config-block-actions--end">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setConfirmRemoveChannel({ id: ch.channel_binding_id, name: channelTitle || t("page.workspace_detail.this_channel") })}
                          >
                            {t("page.task_detail.runtime.remove_rule")}
                          </Button>
                          {isWebchat && <Button variant="ghost" size="sm" onClick={() => setPageChannel(ch)}>{t("webchat_page.page_title")}</Button>}
                          <Button variant="outline" size="sm" onClick={() => openEditChannel(ch)}>{t("action.edit")}</Button>
                        </div>
                      </ConfigBlock>
                    )}
                  </>
                )}
              </div>
            );
          })}
          {canManageWs && <ConfigAddRow label={t("page.workspace_config.add_entry")} onClick={openAddChannel} />}
          {!canManageWs && items.length === 0 && unboundPlans.length === 0 && (
            <ConfigBlock><p className="config-empty">{t("page.workspace_detail.attach_a_ready_integration_or_create_a_public_we")}</p></ConfigBlock>
          )}
        </ConfigGroup>

        {renderCapabilities()}

        <Modal
          open={canManageWs && showChannelModal}
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
                  if (!canManageWs) return;
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
                  onClick={() => setChannelForm({
                    ...channelForm,
                    mode: "existing",
                    channel_type: "",
                    integration_provider: "",
                    channel_config_id: "",
                  })}
                  style={{
                    textAlign: "left",
                    padding: "12px 14px",
                    borderRadius: 14,
                    border: channelForm.mode === "existing" ? "1px solid #5f928a" : "0.5px solid #e5e5ea",
                    background: channelForm.mode === "existing" ? "#f5f5f7" : "#fff",
                    color: "var(--text-strong)",
                    cursor: "pointer",
                  }}
                >
                  <strong style={{ display: "block", fontSize: 13 }}>{t("page.workspace_detail.use_existing_integration")}</strong>
                  <span style={{ display: "block", marginTop: 4, color: "var(--text-muted)", fontSize: 12 }}>
                    {t("page.workspace_detail.attach_wechat_whatsapp_sms_email_or_another_read")}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => setChannelForm({
                    ...channelForm,
                    mode: "webchat",
                    channel_type: "webchat",
                    integration_provider: "",
                    channel_config_id: "",
                  })}
                  style={{
                    textAlign: "left",
                    padding: "12px 14px",
                    borderRadius: 14,
                    border: channelForm.mode === "webchat" ? "1px solid #5f928a" : "0.5px solid #e5e5ea",
                    background: channelForm.mode === "webchat" ? "#f5f5f7" : "#fff",
                    color: "var(--text-strong)",
                    cursor: "pointer",
                  }}
                >
                  <strong style={{ display: "block", fontSize: 13 }}>{t("page.workspace_detail.create_webchat")}</strong>
                  <span style={{ display: "block", marginTop: 4, color: "var(--text-muted)", fontSize: 12 }}>
                    {t("page.workspace_detail.create_a_public_chat_link_and_qr_code_for_this_w")}
                  </span>
                </button>
              </div>
            )}

            {channelForm.mode === "existing" ? (
              <>
                {isEditingChannel && (
                  <Input
                    label={t("page.workspace_detail.channel_name")}
                    value={channelForm.name}
                    onChange={(e) => setChannelForm({ ...channelForm, name: e.target.value })}
                    placeholder={t("page.workspace_detail.channel_2")}
                  />
                )}
                <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 12, lineHeight: 1.5 }}>
                  {t("page.workspace_detail.connection_picker_description")}
                </p>
                {availableChannelsLoading ? (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, minHeight: 48, color: "var(--text-muted)", fontSize: 12 }}>
                    <LoadingSpinner size={16} />
                    <span className="sr-only">{t("page.workspace_detail.loading_channel_accounts")}</span>
                  </div>
                ) : availableChannelsFailed ? (
                  <div style={{ padding: 12, borderRadius: 10, background: "var(--surface-muted)", color: "var(--text-muted)", fontSize: 12 }}>
                    <div>{t("page.workspace_detail.failed_to_load_channel_accounts")}</div>
                    <Button variant="outline" size="sm" style={{ marginTop: 8 }} onClick={() => void refetchAvailableChannels()}>
                      {t("page.workspace_detail.retry")}
                    </Button>
                  </div>
                ) : (
                  <>
                    <_Field label={t("page.workspace_detail.integration")}>
                      <Select
                        value={selectedIntegrationProvider}
                        onChange={(value) => setChannelForm({
                          ...channelForm,
                          integration_provider: value,
                          channel_config_id: "",
                        })}
                        placeholder={t("page.workspace_detail.select_integration")}
                        filterable
                        options={integrationOptions}
                      />
                    </_Field>
                    <_Field label={t("page.workspace_detail.account")}>
                      <Select
                        value={channelForm.channel_config_id}
                        onChange={(value) => {
                          const selected = accountItems.find((ch) => ch.id === value);
                          setChannelForm({
                            ...channelForm,
                            channel_config_id: value,
                            channel_type: selected?.channel_type || channelForm.channel_type,
                          });
                        }}
                        placeholder={selectedIntegrationProvider
                          ? t("page.workspace_detail.select_account")
                          : t("page.workspace_detail.select_integration_first")}
                        filterable
                        disabled={!selectedIntegrationProvider}
                        options={accountItems.map((ch: any) => ({
                          value: ch.id,
                          label: `${_channelAccountLabel(ch)} · ${_channelKindLabel(ch)}${ch.attached ? ` · ${t("page.workspace_detail.already_attached")}` : ""}`,
                        }))}
                      />
                    </_Field>
                    {compatibleConnectionItems.length === 0 && (
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, padding: 12, borderRadius: 10, background: "var(--surface-muted)" }}>
                        <span style={{ color: "var(--text-muted)", fontSize: 12 }}>
                          {t("page.workspace_detail.no_compatible_channel_accounts")}
                        </span>
                        <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
                          {t("page.workspace_detail.connect_account")}
                        </Button>
                      </div>
                    )}
                  </>
                )}
              </>
            ) : (
              <>
                <Input
                  label={isEditingChannel
                    ? t("page.workspace_detail.channel_name")
                    : t("page.workspace_detail.webchat_name")}
                  value={channelForm.name}
                  onChange={(e) => setChannelForm({ ...channelForm, name: e.target.value })}
                  placeholder={t("page.workspace_detail.website_chat")}
                />
                <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-default)" }}>
                  <input
                    type="checkbox"
                    checked={channelForm.login_required}
                    onChange={(e) => setChannelForm({ ...channelForm, login_required: e.target.checked })}
                  />
                  {t("page.workspace_detail.require_visitor_login_before_chat_starts")}
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
                  <input type="checkbox" checked={residentPmsEnabled} onChange={event => setResidentPmsEnabled(event.target.checked)} />
                  {t("webchat_resident.enable")}
                </label>
                {residentPmsEnabled && <>
                  <p className="webchat-page-note">{t("webchat_resident.owner_hint")}</p>
                  {residentPmsAccounts.isPending ? <PageLoading label={t("webchat_resident.accounts_loading")} minHeight={96} />
                    : residentPmsAccounts.isError ? <><p role="alert">{t("webchat_resident.accounts_failed")}</p><Button variant="outline" size="sm" onClick={() => void residentPmsAccounts.refetch()}>{t("webchat_resident.retry")}</Button></>
                    : <Select ariaLabel={t("webchat_resident.account")} value={residentPmsAccountId} placeholder={t("webchat_resident.select_account")}
                        onChange={setResidentPmsAccountId} options={(residentPmsAccounts.data?.accounts || []).map(account => ({ value: account.id, label: account.display_name }))} />}
                  {!residentPmsAccounts.isPending && !residentPmsAccounts.isError && !residentPmsAccounts.data?.accounts.length && <p>{t("webchat_resident.no_accounts")}</p>}
                </>}
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
                <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-muted)" }}>
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
        {pageChannel && <WebchatPageEditor
          key={pageChannel.channel_binding_id}
          initialPage={pageChannel.config?.public_page}
          workspaceName={webchatWorkspaceDisplayName(ws)}
          agentName={pageChannel.bound_agent?.name || pageChannel.name || "Chat"}
          agentAvatar={pageChannel.bound_agent?.avatar_url}
          welcomeMessage={pageChannel.config?.welcome_message}
          resources={canManageWs ? webchatResources : undefined}
          resourceState={!canManageWs ? "ready" : webchatResourcesFailed ? "error" : webchatResourcesLoading || !webchatResources ? "loading" : "ready"}
          hasMoreDocuments={hasMoreWebchatDocuments}
          loadingMoreDocuments={loadingMoreWebchatDocuments}
          onLoadMoreDocuments={() => { void fetchNextWebchatResourcePage(); }}
          canEdit={canManageWs}
          onClose={() => setPageChannel(null)}
          onReview={(publicPage) => canManageWs
            ? api.workspaces.reviewWebchatPage(workspaceId!, publicPage)
            : api.workspaces.reviewSavedWebchatPage(workspaceId!, pageChannel.channel_binding_id)}
          onSave={async (publicPage) => {
            await api.workspaces.updateChannel(workspaceId!, pageChannel.channel_binding_id, {
              config: { public_page: publicPage },
            });
            await queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
            queryClient.invalidateQueries({ queryKey: ["workspace-activity", workspaceId] });
            toast.success(t("webchat_page.saved"));
          }}
        />}
      </ConfigPage>
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
        <p style={{ fontSize: 13, color: "var(--text-muted)", margin: "0 0 14px", lineHeight: 1.5 }}>
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
          <label style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 700 }}>{t("page.workspace_detail.retrieval_mode")}</label>
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
            <div style={{ fontSize: 12, fontWeight: 800, color: "var(--text-muted)", marginBottom: 8 }}>
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
            <div style={{ padding: 12, borderRadius: 14, background: "var(--surface-muted)", border: "0.5px solid rgba(29, 29, 31,0.06)", display: "flex", alignItems: "center", gap: 12 }}>
              <Toggle
                size="sm"
                checked={defaultGroupIds.has(settingsGroup.id)}
                onChange={() => toggleDefaultGroup(settingsGroup.id, !defaultGroupIds.has(settingsGroup.id))}
                disabled={updateOperatingModel.isPending}
              />
              <div>
                <div style={{ fontSize: 13, fontWeight: 800, color: "var(--text-strong)" }}>{t("page.workspace_detail.use_by_default")}</div>
                <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.45 }}>
                  {t("page.workspace_detail.prefer_this_folder_when_agents_search_workspace")}
                </div>
              </div>
            </div>
          )}

          {settingsGroup.is_default_collection && (
            <div style={{ padding: 12, borderRadius: 14, background: "var(--surface-muted)", border: "0.5px solid rgba(29, 29, 31,0.06)", color: "var(--text-muted)", fontSize: 12, lineHeight: 1.5 }}>
              {t("page.workspace_detail.this_is_the_main_workspace_knowledge_collection")}
            </div>
          )}
        </div>
      </Modal>
    ) : null;

    return (
      <ConfigPage
        title={TAB_LABELS.documents}
        action={<Link className="config-page-link" to="/knowledge">{t("page.workspace_detail.browse_all_knowledge")}</Link>}
      >
        {items.length === 0 && (
          <ConfigGroup>
            <ConfigBlock><p className="config-empty">{t("page.workspace_detail.attach_existing_knowledge_documents_a_default_wo")}</p></ConfigBlock>
            {canManageWs && <ConfigAddRow label={t("page.workspace_detail.add_documents")} onClick={openAddKnowledge} />}
          </ConfigGroup>
        )}
        {items.map((group, i) => {
          const editable = canManageWs && !group.is_workspace_file_bucket;
          return (
            <ConfigGroup
              key={group.id || i}
              title={`${group.is_default_collection ? t("page.workspace_config.main_materials") : (group.name || t("page.workspace_detail.untitled_folder"))} · ${group.document_count ?? 0}`}
              action={editable ? (
                <button type="button" className="config-group-link" onClick={() => setKnowledgeFolderSettingsGroupId(group.id)}>
                  {t("nav.settings")}
                </button>
              ) : undefined}
            >
              {(group.documents || []).map((doc: any) => (
                <div key={doc.id} className="config-doc-row">
                  <Link to={`/viewer/${doc.id}`} state={{ returnTo: `/workspaces/${ws.id}?tab=documents` }}>
                    <IconDocument size={16} />
                    <span className="config-row-label">{doc.name}</span>
                    <span className="config-row-meta">{doc.file_type || ""}</span>
                  </Link>
                  {editable && !group.settings?.readonly && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setConfirmRemoveKnowledgeDoc({ groupId: group.id, documentId: doc.id, name: doc.name || t("page.workspace_detail.this_document") });
                      }}
                    >
                      {t("page.task_detail.runtime.remove_rule")}
                    </Button>
                  )}
                </div>
              ))}
              {(!group.documents || group.documents.length === 0) && (
                <ConfigBlock>
                  <p className="config-empty">
                    {group.is_default_collection ? t("page.workspace_detail.no_documents_in_workspace_knowledge_yet") : t("page.workspace_detail.no_documents_in_this_folder_yet")}
                  </p>
                </ConfigBlock>
              )}
              {group.is_default_collection && canManageWs && (
                <ConfigAddRow label={t("page.workspace_detail.add_documents")} onClick={openAddKnowledge} />
              )}
            </ConfigGroup>
          );
        })}
        {canManageWs && items.length > 0 && (
          <ConfigGroup>
            <ConfigAddRow label={t("page.workspace_config.new_group")} onClick={openCreateKnowledgeFolderModal} />
            <ConfigRow label={t("page.workspace_config.how_staff_use_materials")} chevron onClick={() => setShowKnowledgePolicyModal(true)} />
          </ConfigGroup>
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
	              <div style={{ padding: 12, borderRadius: 12, background: "var(--surface-muted)", color: "var(--text-muted)", fontSize: 13, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
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
	                <p style={{ fontSize: 13, color: "var(--text-muted)", margin: "12px 0", textAlign: "center" }}>
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
	                        border: "0.5px solid rgba(29, 29, 31,0.06)",
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
	                      <span style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 700, color: "var(--text-default)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
	                        {doc.name}
	                      </span>
	                      {alreadyInTargetGroup ? (
	                        <Chip variant="slate" size="sm">{t("page.workspace_detail.already_in_folder")}</Chip>
	                      ) : attachedElsewhere ? (
	                        <Chip variant="teal" size="sm">{t("page.workspace_detail.attached_elsewhere")}</Chip>
	                      ) : (
	                        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{doc.file_type || ""}</span>
	                      )}
	                    </label>
	                  );
	                })
	              )}
	            </div>
	          </div>
	        </Modal>
	      </ConfigPage>
	    );
	  }

  function renderRules() {
    const policy = _mergeGovernancePolicy(governancePolicy?.policy);
    const operatingRules = ((operatingModel?.rules as any[]) || []).filter(Boolean);
    const copy = _ruleCopy();
    const commitPolicy = (next: GovernancePolicy, summary: string) => {
      if (!canManageWs || updateGovernance.isPending) return;
      updateGovernance.mutate({ policy: next, summary });
    };
    const addNaturalRule = () => {
      if (!canManageWs || updateOperatingModel.isPending) return;
      const text = rulePrompt.trim();
      if (!text) return;
      const nextRules = [...operatingRules, buildRuntimeRuleFromText(text)];
      updateOperatingModel.mutate({ ...(operatingModel || {}), rules: nextRules });
      setRulePrompt("");
    };
    const removeOperatingRule = (idx: number) => {
      if (!canManageWs || updateOperatingModel.isPending) return;
      const nextRules = operatingRules.filter((_, i) => i !== idx);
      updateOperatingModel.mutate({ ...(operatingModel || {}), rules: nextRules });
    };
    // draft_only / deny are hard blocks: the agent never acts, a person
    // does it by hand. Offer the one-click switch to an approval gate so
    // the owner is never stuck with a rule they did not mean to make final.
    const softenOperatingRule = (idx: number) => {
      if (!canManageWs || updateOperatingModel.isPending) return;
      const rule = operatingRules[idx];
      const next = softenHardBlockRule(rule);
      const nextRules = operatingRules.map((item, i) => (i === idx ? next : item));
      updateOperatingModel.mutate({ ...(operatingModel || {}), rules: nextRules });
    };

    const ruleDescription = (rule: any) => (
      rule.description || rule.summary || rule.title || _humanize(rule.rule_key)
    );
    const ruleActionPatterns = runtimeRuleSelectorLabels;
    const enforcementMeta = (pattern: string, rule?: any) => {
      const enforcement = String(rule?.enforcement || rule?.rule_type || "").toLowerCase();
      if (enforcement === "blocked" || enforcement === "deny" || enforcement === "never_allow") {
        return {
          label: copy.patternNeverAllow,
          bg: "var(--surface-muted)",
          border: "var(--border-subtle)",
          color: "var(--text-strong)",
        };
      }
      if (enforcement === "required_context" || enforcement === "context_required") {
        return {
          label: copy.patternRequiresContext,
          bg: "var(--surface-muted)",
          border: "var(--border-subtle)",
          color: "var(--text-muted)",
        };
      }
      return {
        label: copy.patternNeedsApproval,
        bg: "var(--surface-muted)",
        border: "var(--border-subtle)",
        color: "var(--text-muted)",
      };
    };

    return (
      <ConfigPage title={t("page.workspace_config.needs_approval")}>
        <ConfigGroup title={t("page.workspace_config.approval_mode")} id="workspace-approval-automation">
          <ConfigBlock>
            <ApprovalSettingsPanel scope={{ kind: "workspace", id: workspaceId }} />
          </ConfigBlock>
          <ConfigRow
            label={copy.maxRisk}
            control={(
              <Select
                value={policy.max_risk_level}
                onChange={(value) => commitPolicy({ ...policy, max_risk_level: value as GovernancePolicy["max_risk_level"] }, t("page.workspace_detail.change_max_risk_level"))}
                options={copy.riskOptions}
                disabled={!canManageWs || updateGovernance.isPending}
                ariaLabel={copy.maxRisk}
              />
            )}
          />
        </ConfigGroup>

        <ConfigGroup title={`${copy.currentTitle} · ${operatingRules.length}`}>
          {operatingRules.length === 0 && (
            <ConfigBlock><p className="config-empty">{copy.empty}</p></ConfigBlock>
          )}
          {operatingRules.map((rule, idx) => {
            const patterns = ruleActionPatterns(rule);
            return (
              <ConfigRow
                key={rule.rule_key || idx}
                label={ruleDescription(rule)}
                note={(
                  <>
                    {patterns.length > 0
                      ? patterns.map((pattern: string) => `${enforcementMeta(pattern, rule).label}：${_friendlyCodeLabel(pattern)}`).join("　")
                      : copy.noEnforcement}
                    {isHardBlockRuleType(rule.rule_type) && <span className="config-row-note-line">{copy.hardBlockNote}</span>}
                  </>
                )}
                control={canManageWs ? (
                  <span className="config-row-actions">
                    {isHardBlockRuleType(rule.rule_type) && (
                      <Button variant="outline" size="sm" onClick={() => softenOperatingRule(idx)} disabled={updateOperatingModel.isPending}>
                        {copy.softenToApproval}
                      </Button>
                    )}
                    <Button variant="ghost" size="sm" onClick={() => removeOperatingRule(idx)} disabled={updateOperatingModel.isPending}>
                      {copy.remove}
                    </Button>
                  </span>
                ) : undefined}
              />
            );
          })}
          {canManageWs && (
            <ConfigBlock>
              <label className="config-block-label" htmlFor="workspace-rule-prompt">{copy.addTitle}</label>
              <textarea
                id="workspace-rule-prompt"
                className="config-textarea"
                rows={2}
                value={rulePrompt}
                onChange={(e) => setRulePrompt(e.target.value)}
                placeholder={copy.placeholder}
                disabled={updateOperatingModel.isPending}
              />
              <div className="config-block-actions">
                <div className="config-chips">
                  {copy.examplesList.map((example) => (
                    <button key={example.label} type="button" className="config-chip" onClick={() => setRulePrompt(example.text)}>
                      {example.label}
                    </button>
                  ))}
                </div>
                <Button
                  size="sm"
                  onClick={addNaturalRule}
                  disabled={!rulePrompt.trim() || updateOperatingModel.isPending}
                  loading={updateOperatingModel.isPending}
                >
                  {copy.addRule}
                </Button>
              </div>
            </ConfigBlock>
          )}
        </ConfigGroup>
      </ConfigPage>
    );
  }

  function renderUsage() {
    const budgetPercent = Math.min(100, Math.round((budgetStatus?.pct_used ?? 0) * 100));
    const creditsPerUsd = budgetStatus?.credits_per_usd;
    return (
      <ConfigPage title={TAB_LABELS.usage}>
        <ConfigGroup title={t("page.workspace_detail.usage")}>
          {budgetStatus && (
            <ConfigRow
              label={t("page.workspace_detail.used_this_month")}
              note={creditUsageSummary?.paid_by_provider
                ? t("page.workspace_detail.trial_usage_paid_by_provider")
                : budgetStatus.monthly_budget_credits != null
                  ? t("page.workspace_detail.resets_in_days", { days: budgetStatus.days_until_month_end })
                  : t("page.workspace_detail.no_cap")}
              control={(
                <span className="config-meter">
                  {budgetStatus.monthly_budget_credits != null && (
                    <span className="config-meter-track"><span style={{ width: `${budgetPercent}%` }} /></span>
                  )}
                  <_CreditMetricValue value={budgetStatus.monthly_spent_credits} creditsPerUsd={creditsPerUsd} />
                </span>
              )}
            />
          )}
          {creditUsageSummaryLoading ? (
            <ConfigBlock><LoadingSpinner /></ConfigBlock>
          ) : creditUsageSummaryFailed ? (
            <ConfigRow
              label={t("page.workspace_detail.usage_unavailable")}
              control={<Button variant="ghost" size="sm" onClick={() => { void refetchCreditUsageSummary(); }}>{t("page.workspace_detail.retry_usage")}</Button>}
            />
          ) : creditUsageSummary ? (
            <>
              <ConfigRow label={t("page.workspace_detail.today")} control={<_CreditMetricValue value={creditUsageSummary.today_credits} creditsPerUsd={creditsPerUsd} />} />
              <ConfigRow label={t("page.workspace_detail.this_week")} control={<_CreditMetricValue value={creditUsageSummary.week_credits} creditsPerUsd={creditsPerUsd} />} />
              <ConfigRow label={t("page.workspace_detail.all_time")} control={<_CreditMetricValue value={creditUsageSummary.all_time_credits} creditsPerUsd={creditsPerUsd} />} />
            </>
          ) : null}
        </ConfigGroup>

        {canManageWs && (
          <ConfigGroup title={t("page.workspace_config.budget")} id="workspace-budget">
            <ConfigRow
              label={t("page.workspace_config.monthly_cap")}
              note={t("page.workspace_config.monthly_cap_note")}
              htmlFor="workspace-budget-cap"
              control={(
                <input
                  id="workspace-budget-cap"
                  className="config-input config-input--short"
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
              )}
            />
            <ConfigRow
              label={t("page.workspace_detail.auto_pause_workspace_when_cap_is_hit")}
              control={(
                <Toggle
                  checked={!!budgetStatus?.auto_pause_on_budget}
                  onChange={() => updateBudget.mutate({ auto_pause_on_budget: !budgetStatus?.auto_pause_on_budget })}
                  disabled={updateBudget.isPending}
                  aria-label={t("page.workspace_detail.auto_pause_workspace_when_cap_is_hit")}
                />
              )}
            />
          </ConfigGroup>
        )}
      </ConfigPage>
    );
  }

  function renderGoals() {
    const rawGoalsList: any[] = Array.isArray(goals) ? goals : (goals as any)?.items ?? [];
    const goalsList = _dedupeGoals(rawGoalsList);
    const openGoals = goalsList.filter((g: any) => g.status !== "abandoned");
    const abandonedGoals = goalsList.filter((g: any) => g.status === "abandoned");
    const availableStats = workspaceStatsData?.items || [];
    const measurementStats = availableStats.filter((stat) => stat.goal_eligible && stat.status === "active");
    const measurementStatById = new Map(availableStats.map((stat) => [stat.id, stat]));

    const formatMeasurementValue = (value: unknown, stat?: (typeof availableStats)[number]) => {
      const formatted = formatGoalNumber(value);
      if (formatted === null) {
        return t("page.workspace_detail.not_measured_yet");
      }
      if (stat?.value_type === "percent") return `${formatted}%`;
      if (stat?.value_type === "currency") return `${stat.unit || ""}${formatted}`;
      return stat?.unit ? `${formatted} ${stat.unit}` : formatted;
    };

    const openEdit = (g: any) => {
      setEditingGoal(g);
      setGoalForm({
        title: g.title || "",
        description: g.description || "",
        target_value: g.target_value ?? "",
        deadline: g.deadline || "",
        status: g.status || "active",
        measurement_cadence: g.measurement_cadence || "",
        stat_id: g.stat_id || "",
        priority: g.priority ?? 3,
      });
    };

    const computeProgress = (g: any) => {
      return goalProgressPercent(g);
    };

    return (
      <ConfigPage title={t("nav.goals")}>
        <p className="config-page-note">{t("page.workspace_config.goals_optional")}</p>

        <ConfigGroup>
          {goalsLoading ? (
            <ConfigBlock><LoadingSpinner /></ConfigBlock>
          ) : openGoals.length === 0 ? (
            <ConfigBlock><p className="config-empty">{t("page.workspace_config.no_goals")}</p></ConfigBlock>
          ) : null}
          {!goalsLoading && [...openGoals, ...(abandonedGoalsOpen ? abandonedGoals : [])].map((g: any) => {
            const progress = computeProgress(g);
            const isEditing = editingGoal?.id === g.id;
            const linkedStat = g.stat_id ? measurementStatById.get(g.stat_id) : undefined;
            const linkedTaskCount = Array.isArray(g.linked_task_ids) ? g.linked_task_ids.length : 0;
            const completedTaskCount = Number((g.task_status_counts || {}).completed || 0);
            const source = linkedStat?.name
              || (g.metric_key ? formatUserFacingLabel(g.metric_key) : t("page.workspace_stats.no_measurement_source"));
            const noteParts = [
              source,
              g.measurement_cadence,
              g.deadline ? `${t("page.task_process.due")} ${g.deadline}` : "",
              linkedTaskCount > 0 ? `${completedTaskCount}/${linkedTaskCount}` : "",
              g.priority && g.priority !== 3 ? `${t("page.workspace_detail.p")}${g.priority}` : "",
            ].filter(Boolean);
            return (
              <div key={g.id} className="config-goal">
                <button
                  type="button"
                  className="config-goal-summary"
                  aria-expanded={isEditing}
                  onClick={() => isEditing ? setEditingGoal(null) : openEdit(g)}
                >
                  <span className="config-goal-head">
                    <span className="config-row-label">{g.title}</span>
                    <span className="config-row-meta mono">
                      {formatMeasurementValue(g.current_value, linkedStat)} / {formatMeasurementValue(g.target_value, linkedStat)}
                    </span>
                  </span>
                  <span className="config-meter-track config-meter-track--full"><span style={{ width: `${progress}%` }} /></span>
                  <span className="config-row-note">{noteParts.join(" · ")}</span>
                </button>
                {isEditing && (
                  <div className="config-goal-editor">
                    <ConfigRow
                      label={t("page.team_people.title")}
                      htmlFor="goal-title"
                      control={<input id="goal-title" className="config-input" value={goalForm.title || ""} onChange={(e) => setGoalForm({ ...goalForm, title: e.target.value })} />}
                    />
                    <ConfigRow
                      label={t("page.workspace_detail.target_value")}
                      htmlFor="goal-target"
                      control={<input id="goal-target" className="config-input config-input--short" type="number" step="any" value={String(goalForm.target_value ?? "")} onChange={(e) => setGoalForm({ ...goalForm, target_value: e.target.value })} />}
                    />
                    <ConfigRow
                      label={t("page.workspace_stats.measure_with")}
                      note={goalForm.stat_id ? t("page.workspace_stats.stat_controls_measurement") : t("page.workspace_stats.goal_collector_description")}
                      control={(
                        <Select
                          value={goalForm.stat_id || ""}
                          onChange={(value) => setGoalForm({ ...goalForm, stat_id: value })}
                          options={[
                            { value: "", label: t("page.workspace_stats.goal_collector") },
                            ...measurementStats.map((stat) => ({ value: stat.id, label: stat.name })),
                          ]}
                          filterable={measurementStats.length > 6}
                          ariaLabel={t("page.workspace_stats.measure_with")}
                        />
                      )}
                    />
                    <ConfigRow
                      label={t("page.workspace_detail.cadence_2")}
                      control={(
                        <Select
                          disabled={Boolean(goalForm.stat_id)}
                          value={goalForm.measurement_cadence || ""}
                          onChange={(value) => setGoalForm({ ...goalForm, measurement_cadence: value })}
                          options={[
                            { value: "", label: t("page.workspace_detail.none") },
                            { value: "hourly", label: t("page.workspace_detail.hourly") },
                            { value: "daily", label: t("page.workspace_detail.daily") },
                            { value: "weekly", label: t("page.workspace_detail.weekly") },
                            { value: "monthly", label: t("page.workspace_detail.monthly") },
                          ]}
                          ariaLabel={t("page.workspace_detail.cadence_2")}
                        />
                      )}
                    />
                    <ConfigRow
                      label={t("page.workspace_detail.deadline")}
                      htmlFor="goal-deadline"
                      control={<input id="goal-deadline" className="config-input config-input--date" type="date" value={goalForm.deadline || ""} onChange={(e) => setGoalForm({ ...goalForm, deadline: e.target.value })} />}
                    />
                    <ConfigRow
                      label={t("page.agent_dashboard.status")}
                      control={(
                        <Select
                          value={goalForm.status || "active"}
                          onChange={(value) => setGoalForm({ ...goalForm, status: value })}
                          options={[
                            { value: "active", label: t("page.workspaces.filter_active") },
                            { value: "paused", label: t("page.workspaces.filter_paused") },
                            { value: "abandoned", label: t("page.workspace_detail.abandoned") },
                          ]}
                          ariaLabel={t("page.agent_dashboard.status")}
                        />
                      )}
                    />
                    <ConfigBlock>
                      <label className="config-block-label" htmlFor="goal-description">{t("page.task_collections.description")}</label>
                      <textarea id="goal-description" className="config-textarea" rows={2} value={goalForm.description || ""} onChange={(e) => setGoalForm({ ...goalForm, description: e.target.value })} />
                      <div className="config-block-actions config-block-actions--end">
                        <Button variant="ghost" size="sm" onClick={() => setEditingGoal(null)}>{t("action.cancel")}</Button>
                        <Button
                          size="sm"
                          loading={updateGoalMut.isPending}
                          onClick={() => {
                            const payload: any = {};
                            if (goalForm.title !== g.title) payload.title = goalForm.title;
                            if (goalForm.description !== (g.description || "")) payload.description = goalForm.description;
                            if (String(goalForm.target_value) !== String(g.target_value ?? "")) payload.target_value = String(goalForm.target_value).trim();
                            if (goalForm.deadline !== (g.deadline || "")) payload.deadline = goalForm.deadline || null;
                            if (goalForm.status !== g.status) payload.status = goalForm.status;
                            if ((goalForm.stat_id || "") !== (g.stat_id || "")) payload.stat_id = goalForm.stat_id || null;
                            if (goalForm.measurement_cadence !== (g.measurement_cadence || "")) payload.measurement_cadence = goalForm.measurement_cadence || null;
                            if (Object.keys(payload).length === 0) { setEditingGoal(null); return; }
                            updateGoalMut.mutate({ id: g.id, data: payload });
                          }}
                        >
                          {t("action.save")}
                        </Button>
                      </div>
                    </ConfigBlock>
                  </div>
                )}
              </div>
            );
          })}
          {abandonedGoals.length > 0 && (
            <ConfigRow
              label={t("page.workspace_config.abandoned_goals")}
              meta={String(abandonedGoals.length)}
              expanded={abandonedGoalsOpen}
              onClick={() => setAbandonedGoalsOpen((open) => !open)}
            />
          )}
          {canManageWs && (newGoalDraft ? (
            <ConfigBlock>
              <label className="config-block-label" htmlFor="new-goal-title">{t("page.team_people.title")}</label>
              <input
                id="new-goal-title"
                className="config-input config-input--block"
                autoFocus
                value={newGoalDraft.title}
                onChange={(e) => setNewGoalDraft({ ...newGoalDraft, title: e.target.value })}
              />
              <label className="config-block-label" htmlFor="new-goal-target">{t("page.workspace_detail.target_value")}</label>
              <input
                id="new-goal-target"
                className="config-input config-input--block"
                type="number"
                step="any"
                value={newGoalDraft.target_value}
                onChange={(e) => setNewGoalDraft({ ...newGoalDraft, target_value: e.target.value })}
              />
              <div className="config-block-actions config-block-actions--end">
                <Button variant="ghost" size="sm" onClick={() => setNewGoalDraft(null)}>{t("action.cancel")}</Button>
                <Button
                  size="sm"
                  disabled={!newGoalDraft.title.trim()}
                  loading={createGoalMut.isPending}
                  onClick={() => createGoalMut.mutate({
                    workspace_id: workspaceId!,
                    title: newGoalDraft.title.trim(),
                    ...(newGoalDraft.target_value.trim() ? { target_value: newGoalDraft.target_value.trim() } : {}),
                  })}
                >
                  {t("action.save")}
                </Button>
              </div>
            </ConfigBlock>
          ) : (
            <ConfigAddRow label={t("page.workspace_config.add_goal")} onClick={() => setNewGoalDraft({ title: "", target_value: "" })} />
          ))}
        </ConfigGroup>

        {openGoals.length > 0 && workspaceId && (
          <ConfigGroup>
            <ConfigRow
              label={t("page.workspace_config.goal_breakdown")}
              expanded={goalGraphExpanded}
              onClick={() => setGoalGraphExpanded((expanded) => !expanded)}
            />
            {goalGraphExpanded && <ConfigBlock><WorkspaceGoalGraph workspaceId={workspaceId} /></ConfigBlock>}
          </ConfigGroup>
        )}

        {workspaceId && <WorkspaceStatsPanel workspaceId={workspaceId} canManage={canManageWs} context="goals" />}
      </ConfigPage>
    );
  }

  /* M14 Human Participation view. Privacy boundary (M9.6 / invariant 8):
     the API never returns a per-person latency or efficiency metric and
     this view must never derive one — counts and declared facts only. */
  function renderHumanParticipation() {
    const queue: HumanQueueCommitment[] = humanParticipation?.queue || [];
    const blocking: HumanBlockingEntry[] = humanParticipation?.blocking || [];
    const participants: HumanParticipantDigest[] = humanParticipation?.participants || [];
    const decisions: HumanDecisionDigest[] = humanParticipation?.decisions || [];
    const contributions: HumanContributionDigest[] = humanParticipation?.recent_contributions || [];
    const blockedByCommitment = new Map(blocking.map((b) => [b.commitment_id, b]));

    return (
      <>
        {queue.length > 0 && (
          <ConfigGroup title={`${t("page.workspace_config.waiting_on_people")} · ${queue.length}`}>
            {queue.map((c) => {
              const blocked = blockedByCommitment.get(c.id);
              const firstBlocked = blocked?.blocked.find((item) => item.title);
              const note = [
                c.blocking ? t("page.workspace_detail.human_blocking") : "",
                c.role_required ? _activityStatusLabel(c.role_required) : "",
                c.expected_by ? t("page.workspace_detail.human_expected_by").replace("{when}", relativeTime(c.expected_by)) : "",
                firstBlocked?.title ? `${t("page.workspace_detail.human_blocks")} ${formatUserFacingText(firstBlocked.title)}` : "",
              ].filter(Boolean).join(" · ");
              return (
                <ConfigRow
                  key={c.id}
                  label={c.expected_input ? formatUserFacingText(c.expected_input) : _activityStatusLabel(c.request_kind)}
                  note={note || undefined}
                  chevron={!!firstBlocked}
                  to={firstBlocked ? `/tasks/${encodeURIComponent(firstBlocked.id)}` : undefined}
                />
              );
            })}
          </ConfigGroup>
        )}

        {participants.length > 0 && (
          <ConfigGroup title={t("page.workspace_detail.human_participants")}>
            {participants.map((p) => (
              <ConfigRow
                key={p.user_id}
                label={p.display_name || p.user_id}
                note={[
                  ...p.roles.map((role) => _activityStatusLabel(String(role))),
                  ...p.declared_capabilities.map((cap) => _activityStatusLabel(String(cap))),
                  p.availability.out_of_office ? t("page.workspace_detail.human_out_of_office") : p.availability.timezone,
                ].filter(Boolean).join(" · ") || undefined}
                meta={t("page.workspace_detail.human_open_requests").replace("{count}", String(p.open_commitments_count))}
              />
            ))}
          </ConfigGroup>
        )}

        {(decisions.length > 0 || contributions.length > 0) && (
          <ConfigGroup>
            <ConfigRow
              label={t("page.workspace_config.decisions_and_contributions")}
              expanded={recordsHistoryOpen}
              onClick={() => setRecordsHistoryOpen((open) => !open)}
            />
            {recordsHistoryOpen && decisions.map((d) => (
              <ConfigRow
                key={`${d.item_id}-${d.decision}`}
                label={[_activityStatusLabel(d.decision), d.kind ? _activityStatusLabel(d.kind) : ""].filter(Boolean).join(" · ")}
                note={d.reason_code ? _activityStatusLabel(d.reason_code) : undefined}
                meta={d.decided_at ? relativeTime(d.decided_at) : undefined}
              />
            ))}
            {recordsHistoryOpen && contributions.map((c, i) => (
              <ConfigRow
                key={`${c.target_kind}-${c.target_id}-${i}`}
                label={`${_activityStatusLabel(c.kind)} · ${_activityStatusLabel(c.target_kind)}`}
                note={c.fields_changed.length > 0
                  ? t("page.workspace_detail.human_fields_changed").replace("{fields}", c.fields_changed.join(", "))
                  : undefined}
                meta={c.created_at ? relativeTime(c.created_at) : undefined}
              />
            ))}
          </ConfigGroup>
        )}
      </>
    );
  }

  function renderActivity() {
    const items = activityFeed || [];
    return (
      <>
        {renderHumanParticipation()}
        <ConfigGroup title={t("page.workspace_config.recent_activity")}>
          {items.length === 0 ? (
            <ConfigBlock><p className="config-empty">{t("page.workspace_detail.no_activity_recorded_for_this_workspace_yet")}</p></ConfigBlock>
          ) : items.map((evt: WorkspaceActivity, i: number) => {
            const taskSummaries = _activityTaskSummaries(evt);
            const actorLabel = _activityActorLabel(evt);
            return (
              <div key={evt.id || i} className="config-activity">
                <ConfigRow
                  label={_activitySummaryText(evt)}
                  note={[_activityEventLabel(evt.event_type), actorLabel ? `${t("page.activity.by")} ${actorLabel}` : ""].filter(Boolean).join(" · ")}
                  meta={relativeTime(evt.created_at)}
                />
                {taskSummaries.slice(0, 3).map((task: any) => {
                  const files = Array.isArray(task.files) ? task.files.filter(Boolean).slice(0, 4) : [];
                  return (
                    <div key={task.id} className="config-activity-task">
                      <Link to={`/tasks/${encodeURIComponent(String(task.id))}`}>{formatUserFacingText(task.title || task.id)}</Link>
                      {task.status && <span className="config-row-note">{_activityStatusLabel(task.status)}</span>}
                      {task.owner_service_key && <span className="config-row-note">{formatUserFacingText(_humanize(task.owner_service_key))}</span>}
                      {files.map((file: any, idx: number) => {
                        const href = _activityFileHref(file);
                        const label = String(file.label || file.name || file.filename || file.fs_path || `File ${idx + 1}`);
                        const content = <><IconDocument size={13} /><span>{label}</span></>;
                        if (!href) return <span key={`${task.id}-file-${idx}`} className="config-file-chip">{content}</span>;
                        if (href.startsWith("http://") || href.startsWith("https://")) {
                          return <a key={`${task.id}-file-${idx}`} className="config-file-chip" href={href} target="_blank" rel="noreferrer">{content}</a>;
                        }
                        return <Link key={`${task.id}-file-${idx}`} className="config-file-chip" to={href}>{content}</Link>;
                      })}
                    </div>
                  );
                })}
                {taskSummaries.length > 3 && (
                  <div className="config-activity-task"><span className="config-row-note">+{taskSummaries.length - 3}</span></div>
                )}
              </div>
            );
          })}
        </ConfigGroup>
        <Link className="config-page-link" to={`/tasks?workspaceId=${encodeURIComponent(ws!.id)}`}>{t("page.workspace_config.view_workspace_tasks")}</Link>
      </>
    );
  }

  function renderEvaluation() {
    const evaluation = workspaceEvaluation as WorkspaceEvaluationSnapshot | undefined;
    if (evaluationLoading) {
      return <ConfigGroup title={t("page.workspace_config.how_it_is_going")}><ConfigBlock><LoadingSpinner /></ConfigBlock></ConfigGroup>;
    }
    if (!evaluation) {
      return (
        <ConfigGroup title={t("page.workspace_config.how_it_is_going")}>
          <ConfigBlock><p className="config-empty">{t("page.workspace_detail.no_workspace_evaluation_desc")}</p></ConfigBlock>
        </ConfigGroup>
      );
    }
    const overall = evaluation.overall;
    const dimensions = evaluation.dimensions || {};
    const trendLabel = trendText(evaluation.trend);
    const summary = evaluation.evidence_summary || {};
    const records = (summary.task_count || 0) + (summary.workflow_run_count || 0) + (summary.runtime_evidence_count || 0);
    const scored = typeof overall?.score === "number";
    const code = evaluation.recommendation_codes?.[0];
    const codeKey = code ? `page.workspace_detail.eval_rec_${code}` : "";
    const nextText = codeKey && t(codeKey) !== codeKey ? t(codeKey) : _evaluationText(evaluation.recommendations[0] || "");
    return (
      <ConfigGroup title={t("page.workspace_config.how_it_is_going")}>
        <ConfigBlock>
          <div className="config-score">
            <strong>{scored ? Math.round(overall!.score as number) : t("page.workspace_detail.eval_too_early")}</strong>
            <span>
              {[
                scored ? t("page.workspace_detail.eval_points") : "",
                t("page.workspace_detail.eval_records").replace("{count}", String(records)),
                trendLabel,
              ].filter(Boolean).join(" · ")}
            </span>
          </div>
          <p className="config-block-text">
            {!scored
              ? t("page.workspace_detail.eval_not_enough_runs")
              : nextText
                ? t("page.workspace_detail.eval_next_line").replace("{text}", nextText)
                : t("page.workspace_detail.eval_all_good")}
          </p>
        </ConfigBlock>
        <ConfigRow
          label={t("page.workspace_config.full_evaluation")}
          expanded={evaluationDetailOpen}
          onClick={() => setEvaluationDetailOpen((open) => !open)}
        />
        {evaluationDetailOpen && evaluationDimensions.map((item) => {
          const section = dimensions[item.key] || {};
          const score = section.score as number | null | undefined;
          return (
            <ConfigRow
              key={item.key}
              label={item.label}
              note={_evaluationNote(item.key, section, evaluation.window.days)}
              control={(
                <span className="config-meter">
                  <span className="config-meter-track"><span style={{ width: `${typeof score === "number" ? Math.max(3, Math.min(100, score)) : 0}%` }} /></span>
                  <span className="mono">{scoreText(score)}</span>
                </span>
              )}
            />
          );
        })}
      </ConfigGroup>
    );
  }

  function renderStrategySection() {
    const reviews: StrategyReviewListEntry[] = strategyReviews?.reviews || [];
    const detail = expandedReviewId ? strategyReviewDetail : undefined;
    const coverageGaps: string[] = Array.isArray(detail?.review?.briefing?.coverage_gaps)
      ? detail!.review.briefing!.coverage_gaps
      : [];
    return (
      <ConfigGroup title={`${t("page.workspace_config.reviews")} · ${reviews.length}`}>
        {reviews.length === 0 && (
          <ConfigBlock><p className="config-empty">{t("page.workspace_detail.no_strategy_reviews_desc")}</p></ConfigBlock>
        )}
        {reviews.map((review) => {
          const expanded = expandedReviewId === review.id;
          const totalReports = review.reports.complete + review.reports.partial + review.reports.failed;
          const suggestions = review.item_counts.reduce((sum, c) => sum + c.count, 0);
          const skipped = review.status === "skipped";
          const note = (skipped
            ? [review.skip_reason
              ? t("page.workspace_detail.strategy_skipped_reason").replace("{reason}", _activityStatusLabel(review.skip_reason))
              : _activityStatusLabel(review.status)]
            : [
              review.status === "failed" ? _activityStatusLabel(review.status) : "",
              totalReports > 0
                ? review.reports.complete === totalReports
                  ? t("page.workspace_detail.strategy_reports_all").replace("{count}", String(totalReports))
                  : t("page.workspace_detail.strategy_reports_label")
                    .replace("{complete}", String(review.reports.complete))
                    .replace("{total}", String(totalReports))
                : "",
              suggestions > 0
                ? t("page.workspace_detail.strategy_suggestion_count").replace("{count}", String(suggestions))
                : t("page.workspace_detail.strategy_no_new_suggestions"),
            ]).filter(Boolean).join(" · ");
          return (
            <div key={review.id}>
              <ConfigRow
                label={relativeTime(review.created_at || "")}
                note={note}
                expanded={expanded}
                onClick={() => {
                  setStrategyWhyItemId(null);
                  setExpandedReviewId(expanded ? null : review.id);
                }}
              />
              {expanded && (
                strategyDetailLoading || !detail ? (
                  <ConfigBlock><LoadingSpinner /></ConfigBlock>
                ) : (
                  <>
                    {coverageGaps.length > 0 && (
                      <ConfigRow label={t("page.workspace_detail.coverage_gaps")} note={coverageGaps.join("、")} />
                    )}
                    {(!detail.proposal || detail.proposal.items.length === 0) && (
                      <ConfigBlock><p className="config-empty">{t("page.workspace_detail.strategy_no_items")}</p></ConfigBlock>
                    )}
                    {detail.proposal?.items.map((item: StrategyProposalItem) => {
                      const basisReports = item.basis?.report_refs || [];
                      const basisEvidence = item.basis?.evidence_refs || [];
                      const hasBasis = basisReports.length > 0 || basisEvidence.length > 0;
                      const whyOpen = strategyWhyItemId === item.id;
                      return (
                        <ConfigRow
                          key={item.id}
                          label={formatUserFacingText(String(item.payload?.title || item.item_key))}
                          note={(
                            <>
                              {[
                                _activityStatusLabel(item.kind),
                                _activityStatusLabel(item.status),
                                item.decision?.reason_code ? _activityStatusLabel(item.decision.reason_code) : "",
                              ].filter(Boolean).join(" · ")}
                              {whyOpen && basisReports.length > 0 && <span className="config-row-note-line">{t("page.workspace_detail.strategy_basis_reports")}: <span className="mono">{basisReports.join(", ")}</span></span>}
                              {whyOpen && basisEvidence.length > 0 && <span className="config-row-note-line">{t("page.workspace_detail.strategy_basis_evidence")}: <span className="mono">{basisEvidence.join(", ")}</span></span>}
                            </>
                          )}
                          control={hasBasis ? (
                            <Button size="sm" variant="ghost" onClick={() => setStrategyWhyItemId(whyOpen ? null : item.id)}>
                              {t("page.workspace_detail.strategy_why")}
                            </Button>
                          ) : undefined}
                        />
                      );
                    })}
                    {detail.proposal?.notes && (
                      <ConfigBlock>
                        <span className="config-block-label">{t("page.workspace_detail.strategist_notes")}</span>
                        <p className="config-block-text" style={{ whiteSpace: "pre-wrap" }}>{detail.proposal.notes}</p>
                      </ConfigBlock>
                    )}
                  </>
                )
              )}
            </div>
          );
        })}
      </ConfigGroup>
    );
  }

  function renderLearning() {
    const candidates: AgentLearningCandidate[] = learningCandidates || [];
    const evidence: RuntimeEvidence[] = runtimeEvidence || [];
    const openCandidates = candidates.filter((c) => c.status === "proposed");
    const recentCandidates = candidates.filter((c) => c.status !== "proposed");
    const metricSummary = (ev: RuntimeEvidence) => {
      const metrics = ev.metrics || {};
      return [
        metrics.total_tokens ? `${metrics.total_tokens.toLocaleString()} tokens` : "",
        metrics.rounds ? `${metrics.rounds} rounds` : "",
        metrics.tool_call_count ? `${metrics.tool_call_count} tools` : "",
      ].filter(Boolean).join(" · ");
    };
    const payloadPreview = (payload: Record<string, any>) => {
      const content = payload?.content || payload?.seed_prompt || "";
      if (content) return _learningText(content).slice(0, 220);
      const target = _learningTargetLabel(payload?.apply_target);
      return target ? `Updates ${target}` : "";
    };

    if (learningLoading || evidenceLoading) {
      return <ConfigGroup><ConfigBlock><LoadingSpinner /></ConfigBlock></ConfigGroup>;
    }

    return (
      <>
        <ConfigGroup title={`${t("page.workspace_config.waiting_for_you")} · ${openCandidates.length}`}>
          {openCandidates.length === 0 && (
            <ConfigBlock><p className="config-empty">{t("page.workspace_detail.no_proposed_learning_desc")}</p></ConfigBlock>
          )}
          {openCandidates.map((candidate) => (
            <ConfigRow
              key={candidate.id}
              label={_learningText(candidate.title)}
              note={(
                <>
                  {_learningText(candidate.summary)}
                  {payloadPreview(candidate.payload) && <span className="config-row-note-line">{payloadPreview(candidate.payload)}</span>}
                  <span className="config-row-note-line">
                    {[_learningKindLabel(candidate.candidate_type), _learningRiskLabel(candidate.risk_level), `${Math.round((candidate.confidence || 0) * 100)}%`].join(" · ")}
                  </span>
                </>
              )}
              control={(
                <span className="config-row-actions">
                  <Button size="sm" variant="ghost" loading={resolveLearningCandidate.isPending} onClick={() => resolveLearningCandidate.mutate({ id: candidate.id, status: "archived" })}>
                    {t("action.archive")}
                  </Button>
                  <Button size="sm" variant="ghost" loading={resolveLearningCandidate.isPending} onClick={() => resolveLearningCandidate.mutate({ id: candidate.id, status: "rejected" })}>
                    {t("action.reject")}
                  </Button>
                  <Button variant="outline" size="sm" loading={resolveLearningCandidate.isPending} onClick={() => resolveLearningCandidate.mutate({ id: candidate.id, status: "accepted" })}>
                    {t("page.workspace_detail.mark_accepted")}
                  </Button>
                </span>
              )}
            />
          ))}
        </ConfigGroup>

        <ConfigGroup>
          <ConfigRow
            label={t("page.workspace_detail.reviewed_candidates")}
            meta={String(recentCandidates.length)}
            expanded={learningReviewedOpen}
            onClick={() => setLearningReviewedOpen((open) => !open)}
          />
          {learningReviewedOpen && recentCandidates.slice(0, 8).map((candidate) => {
            const applyStatus = String(candidate.resolution?.apply_status || "");
            return (
              <ConfigRow
                key={candidate.id}
                label={_learningText(candidate.title)}
                note={[
                  _activityStatusLabel(candidate.status),
                  applyStatus ? _activityStatusLabel(applyStatus) : "",
                  _learningKindLabel(candidate.candidate_type),
                  relativeTime(candidate.updated_at || candidate.created_at),
                  candidate.status === "applied" && candidate.resolution?.applied_result?.kind
                    ? t("page.workspace_detail.applied_to").replace("{target}", String(candidate.resolution.applied_result.kind).replace(/_/g, " "))
                    : "",
                ].filter(Boolean).join(" · ")}
                control={candidate.status === "accepted" && applyStatus !== "queued" ? (
                  <Button variant="outline" size="sm" loading={applyLearningCandidate.isPending} onClick={() => applyLearningCandidate.mutate(candidate.id)}>
                    {applyStatus === "failed" ? t("page.workspace_detail.retry_apply") : t("page.workspace_detail.queue_apply")}
                  </Button>
                ) : undefined}
              />
            );
          })}
          <ConfigRow
            label={t("page.workspace_config.learning_evidence")}
            meta={String(evidence.length)}
            expanded={learningEvidenceOpen}
            onClick={() => setLearningEvidenceOpen((open) => !open)}
          />
          {learningEvidenceOpen && evidence.slice(0, 12).map((ev) => {
            const tools = Array.isArray(ev.details?.tool_calls_made) ? ev.details.tool_calls_made.slice(0, 5) : [];
            return (
              <ConfigRow
                key={ev.id}
                label={_learningText(ev.summary)}
                note={[
                  _activityStatusLabel(ev.status),
                  _learningKindLabel(ev.evidence_type),
                  metricSummary(ev),
                  tools.map((tool: string) => _learningToolLabel(tool)).join("、"),
                ].filter(Boolean).join(" · ")}
                meta={relativeTime(ev.created_at)}
              />
            );
          })}
        </ConfigGroup>
      </>
    );
  }

  function renderRecords() {
    const view = RECORD_VIEWS.includes(searchParams.get("view") as RecordView) ? searchParams.get("view") as RecordView : "activity";
    const selectView = (next: string) => {
      const params = new URLSearchParams(searchParams);
      if (next === "activity") params.delete("view");
      else params.set("view", next);
      setSearchParams(params, { replace: true });
    };
    return (
      <ConfigPage
        title={t("page.workspace_config.activity_and_review")}
        action={(
          <TabSwitcher
            size="sm"
            value={view}
            onChange={selectView}
            ariaLabel={t("page.workspace_config.activity_and_review")}
            tabs={[
              { key: "activity", label: t("page.workspace_config.view_activity") },
              { key: "data", label: t("page.workspace_config.view_data") },
              { key: "review", label: t("page.workspace_config.view_review") },
              { key: "learning", label: t("page.workspace_config.view_learning") },
            ]}
          />
        )}
      >
        {view === "activity" && renderActivity()}
        {view === "data" && workspaceId && <WorkspaceStatsPanel workspaceId={workspaceId} canManage={canManageWs} />}
        {view === "review" && <>{renderEvaluation()}{renderStrategySection()}</>}
        {view === "learning" && renderLearning()}
      </ConfigPage>
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
      { value: "task.succeeded", label: t("page.workspace_detail.succeeded") },
      { value: "task.retried", label: t("page.workspace_detail.retried") },
    ];
    // Reminders go to each person through their own notification settings,
    // not through these Task-event email/chat sinks.
    const defaultExternalEvents = ["task.failed", "task.hitl_requested"];
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

    const editableRow = (field: keyof Workspace, label: string, placeholder?: string) => (
      <ConfigRow
        label={label}
        htmlFor={`workspace-${String(field)}`}
        control={(
          <input
            id={`workspace-${String(field)}`}
            className="config-input"
            defaultValue={String((ws as any)[field] || "")}
            key={fieldDraftKey(String(field), (ws as any)[field])}
            onBlur={(e) => commitWs(field, field === "name" ? e.target.value : e.target.value || null)}
            placeholder={placeholder}
            disabled={!canManageWs}
          />
        )}
      />
    );
    const readOnlyRow = (label: string, value: React.ReactNode) => value
      ? <ConfigRow label={label} control={<span className="config-value">{value}</span>} />
      : null;
    const blueprintUpdate = ws.blueprint_update;
    const hasBlueprintNotice = blueprintUpdate?.status === "update_available" || blueprintUpdate?.status === "reconfigure_required";
    const incompleteChecks = setupStatus && !setupStatus.ready ? setupStatus.incomplete_checks : [];
    const notificationChannel = (
      channel: "email" | "external_chat",
      policy: Record<string, any>,
      title: string,
      description: string,
    ) => (
      <>
        <ConfigRow
          label={title}
          note={description}
          control={(
            <Toggle
              checked={policy.enabled === true}
              onChange={() => commitNotificationPolicy(channel, {
                enabled: policy.enabled !== true,
                ...(channel === "external_chat" ? { channel_types: policy.channel_types || ["slack"] } : {}),
              })}
              disabled={updateWorkspace.isPending || !canManageWs}
              aria-label={title}
            />
          )}
        />
        {channel === "external_chat" && policy.enabled === true && (
          <ConfigRow
            label={t("page.workspace_detail.slack_channel_id_or_name")}
            htmlFor="workspace-notification-target"
            control={(
              <input
                id="workspace-notification-target"
                className="config-input"
                defaultValue={policy.target || policy.channel_id || ""}
                key={`notif-chat-target-${policy.target || policy.channel_id || "none"}`}
                placeholder={t("page.workspace_detail.c0123abc_or_ops")}
                onBlur={(e) => {
                  const target = e.target.value.trim();
                  if (target === (policy.target || policy.channel_id || "")) return;
                  commitNotificationPolicy("external_chat", {
                    target: target || undefined,
                    channel_types: policy.channel_types || ["slack"],
                  });
                }}
              />
            )}
          />
        )}
        {policy.enabled === true && (
          <ConfigBlock>
            <div className="config-checks">
              {notificationEvents.map((evt) => {
                const events = ((policy.events as string[]) || defaultExternalEvents);
                return (
                  <label key={`${channel}-${evt.value}`} className="config-check">
                    <input
                      type="checkbox"
                      checked={events.includes(evt.value)}
                      onChange={(e) => toggleNotificationEvent(channel, evt.value, e.target.checked)}
                      disabled={updateWorkspace.isPending || !canManageWs}
                    />
                    {evt.label}
                  </label>
                );
              })}
            </div>
          </ConfigBlock>
        )}
      </>
    );

    return (
      <ConfigPage title={t("page.workspace_config.general")}>
        <ConfigGroup title={t("page.workspace_config.basics")}>
          {editableRow("name", t("page.task_collections.name"))}
          {editableRow("description", t("page.task_collections.description"), t("page.workspace_detail.what_does_this_workspace_do"))}
          {editableRow("category", t("page.workspaces.category"), t("page.workspaces.category_placeholder"))}
          <div id="workspace-cover-image" className="workspace-settings-anchor">
            <ConfigRow
              label={t("page.workspace_config.cover")}
              htmlFor="workspace-cover_image_url"
              leading={ws.cover_image_url ? <img className="config-cover-thumb" src={ws.cover_image_url} alt="" /> : undefined}
              control={(
                <input
                  id="workspace-cover_image_url"
                  className="config-input"
                  defaultValue={ws.cover_image_url || ""}
                  key={fieldDraftKey("cover_image_url", ws.cover_image_url)}
                  onBlur={(e) => commitWs("cover_image_url", e.target.value || null)}
                  placeholder={t("page.workspace_detail.https")}
                  disabled={!canManageWs}
                />
              )}
            />
          </div>
        </ConfigGroup>

        <ConfigGroup title={t("page.workspace_config.context")}>
          <ConfigBlock>
            <label className="config-block-label" htmlFor="workspace-operating-context">{t("page.workspace_config.who_for")}</label>
            <textarea
              id="workspace-operating-context"
              className="config-textarea"
              rows={2}
              defaultValue={ws.operating_context || ""}
              key={fieldDraftKey("operating_context", ws.operating_context)}
              onBlur={(e) => commitWs("operating_context", e.target.value || null)}
              placeholder={t("page.workspace_detail.where_for_whom_does_this_run")}
              disabled={!canManageWs}
            />
          </ConfigBlock>
          <ConfigBlock>
            <label className="config-block-label" htmlFor="workspace-primary-work">{t("page.workspace_config.main_work")}</label>
            <textarea
              id="workspace-primary-work"
              className="config-textarea"
              rows={2}
              defaultValue={ws.primary_work || ""}
              key={fieldDraftKey("primary_work", ws.primary_work)}
              onBlur={(e) => commitWs("primary_work", e.target.value || null)}
              placeholder={t("page.workspace_detail.core_responsibilities")}
              disabled={!canManageWs}
            />
          </ConfigBlock>
          <ConfigRow
            label={t("page.workspace_config.type_identity_address")}
            meta={ws.kind || undefined}
            expanded={settingsIdentityOpen}
            onClick={() => setSettingsIdentityOpen((open) => !open)}
          />
          {settingsIdentityOpen && (
            <>
              {editableRow("kind", t("page.team_people.kind"), t("page.workspace_detail.property_project_campaign"))}
              {editableRow("identity_label", t("page.workspace_detail.identity_label"))}
              {editableRow("address", t("page.team_people.address"))}
              {editableRow("property_type", t("page.workspace_detail.property_type"))}
              {readOnlyRow(t("page.workspace_detail.field_occupancy_status"), ws.occupancy_status)}
              {ws.longitude != null && ws.latitude != null && readOnlyRow(t("page.workspace_config.coordinates"), `${ws.latitude}, ${ws.longitude}`)}
              {readOnlyRow(t("page.workspace_config.pms_property"), ws.pms_property_id)}
              {readOnlyRow(t("page.workspace_config.pms_unit"), ws.pms_unit_id)}
              {readOnlyRow(t("page.workspace_detail.attribute_tags"), ws.attribute_tags?.join("、"))}
            </>
          )}
        </ConfigGroup>

        <ConfigGroup>
          {/* One switch: the server turns reviews off exactly when the Workspace goes manual. */}
          <ConfigRow
            label={t("page.workspace_config.self_driven")}
            note={t("page.workspace_config.self_driven_note")}
            control={(
              <Toggle
                checked={ws.status === "active" && heartbeatOn}
                onChange={() => {
                  if (ws.status === "active" && !heartbeatOn) heartbeatEnable.mutate();
                  else togglePause.mutate();
                }}
                disabled={!canManageWs || togglePause.isPending || heartbeatEnable.isPending}
                aria-label={t("page.workspace_config.self_driven")}
              />
            )}
          />
          <ConfigRow
            label={t("page.workspace_config.how_often")}
            note={ws.last_heartbeat_at ? `${t("page.workspace_detail.last")} ${relativeTime(ws.last_heartbeat_at)}` : undefined}
            control={(
              <Select
                value={ws.heartbeat_cadence || ""}
                onChange={(v) => commitWs("heartbeat_cadence", v || null)}
                placeholder={t("page.workspace_detail.select_cadence")}
                options={heartbeatCadenceOptions}
                disabled={!canManageWs}
                ariaLabel={t("page.workspace_config.how_often")}
              />
            )}
          />
        </ConfigGroup>

        {(hasBlueprintNotice || incompleteChecks.length > 0) && (
          <ConfigGroup title={t("page.workspace_config.source_and_version")}>
            {hasBlueprintNotice && (
              <ConfigRow
                label={blueprintUpdate?.status === "reconfigure_required"
                  ? t("page.workspaces.blueprint_reconfigure_title")
                  : t("page.workspaces.blueprint_update_title")}
                note={blueprintUpdate?.status === "reconfigure_required"
                  ? t("page.workspaces.blueprint_reconfigure_body")
                  : (blueprintUpdate?.changed?.length ?? 0) > 0
                    ? t("page.workspaces.blueprint_update_changed").replace(
                        "{sections}",
                        (blueprintUpdate?.changed || [])
                          .map((section) => t(`page.workspaces.blueprint_section_${section}`) || section)
                          .join("、"),
                      )
                    : t("page.workspaces.blueprint_update_body")
                        .replace("{slug}", blueprintUpdate?.blueprint_slug || "—")
                        .replace("{date}", formatDate(blueprintUpdate?.installed_at || "") || "—")}
                control={canManageWs ? (
                  <Button variant="outline" size="sm" onClick={() => setShowBlueprintUpgrade(true)}>
                    {blueprintUpdate?.status === "reconfigure_required"
                      ? t("page.workspaces.blueprint_reconfigure_review")
                      : t("page.workspaces.blueprint_update_review")}
                  </Button>
                ) : undefined}
              />
            )}
            {incompleteChecks.map((check, index) => (
              <ConfigRow
                key={String(check.key || `${check.todo_kind || check.kind || "setup"}-${index}`)}
                label={String(check.reason || t("page.workspace_detail.setup_item_incomplete"))}
                chevron
                onClick={() => {
                  const target = blueprintSetupDestination(check);
                  if (target === "/integrations") navigate(target);
                  else handleTabChange(target);
                }}
              />
            ))}
          </ConfigGroup>
        )}

        <ConfigGroup>
          <ConfigRow
            label={t("page.workspace_config.advanced")}
            expanded={settingsAdvancedOpen}
            onClick={() => setSettingsAdvancedOpen((open) => !open)}
          />
          {settingsAdvancedOpen && (
            <>
              <ConfigBlock><span className="config-block-label">{t("page.workspace_config.retry_after_failure")}</span></ConfigBlock>
              <ConfigRow
                label={t("page.workspace_detail.max_attempts")}
                htmlFor="workspace-retry-max"
                control={(
                  <input
                    id="workspace-retry-max"
                    className="config-input config-input--short"
                    type="number"
                    min="1"
                    max="10"
                    defaultValue={retryPolicy.max_attempts ?? 3}
                    key={`retry-max-${retryPolicy.max_attempts ?? 3}`}
                    onBlur={(e) => commitRetryNumber("max_attempts", e.target.value, 3, 1, 10)}
                  />
                )}
              />
              <ConfigRow
                label={t("page.workspace_detail.retry_strategy")}
                control={(
                  <Select
                    value={retryPolicy.strategy || "immediate"}
                    onChange={(v) => commitRetryPolicy({ strategy: v || "immediate" })}
                    options={[
                      { value: "immediate", label: t("page.workspace_detail.immediate") },
                      { value: "fixed", label: t("page.workspace_detail.fixed_delay") },
                      { value: "exponential", label: t("page.workspace_detail.exponential_backoff") },
                    ]}
                    ariaLabel={t("page.workspace_detail.retry_strategy")}
                  />
                )}
              />
              <ConfigRow
                label={t("page.workspace_detail.base_delay_seconds")}
                htmlFor="workspace-retry-base"
                control={(
                  <input
                    id="workspace-retry-base"
                    className="config-input config-input--short"
                    type="number"
                    min="0"
                    max="3600"
                    defaultValue={retryPolicy.base_delay_seconds ?? 0}
                    key={`retry-base-${retryPolicy.base_delay_seconds ?? 0}`}
                    onBlur={(e) => commitRetryNumber("base_delay_seconds", e.target.value, 0, 0, 3600)}
                  />
                )}
              />
              <ConfigRow
                label={t("page.workspace_detail.max_delay_seconds")}
                htmlFor="workspace-retry-delay"
                control={(
                  <input
                    id="workspace-retry-delay"
                    className="config-input config-input--short"
                    type="number"
                    min="0"
                    max="86400"
                    defaultValue={retryPolicy.max_delay_seconds ?? 900}
                    key={`retry-delay-${retryPolicy.max_delay_seconds ?? 900}`}
                    onBlur={(e) => commitRetryNumber("max_delay_seconds", e.target.value, 900, 0, 86400)}
                  />
                )}
              />
              <ConfigRow
                label={t("page.workspace_detail.send_step_to_human_input_when_retries_are_exhaus")}
                control={(
                  <Toggle
                    checked={!!retryPolicy.auto_human_on_exhausted}
                    onChange={() => commitRetryPolicy({ auto_human_on_exhausted: !retryPolicy.auto_human_on_exhausted })}
                    disabled={updateWorkspace.isPending}
                    aria-label={t("page.workspace_detail.send_step_to_human_input_when_retries_are_exhaus")}
                  />
                )}
              />
              <ConfigBlock><span className="config-block-label">{t("page.workspace_detail.task_notification_policy")}</span></ConfigBlock>
              {notificationChannel("email", emailPolicy, t("page.workspace_detail.email_involved_users"), t("page.workspace_detail.sends_to_active_task_creator_assignee_using_the"))}
              {notificationChannel("external_chat", chatPolicy, t("page.workspace_detail.slack_external_chat"), t("page.workspace_detail.uses_active_slack_channelconfig_credentials_inco"))}
              {canManageWs && (
                <ConfigBlock>
                  <WorkspaceNotificationRoutingCard ws={ws} updateWorkspace={updateWorkspace} />
                </ConfigBlock>
              )}
              <ConfigBlock><span className="config-block-label">{t("page.workspace_config.other")}</span></ConfigBlock>
              <ConfigRow
                label={t("page.workspace_detail.runtime_learning_controls")}
                note={t("page.workspace_detail.runtime_learning_desc")}
                control={(
                  <Toggle
                    checked={workspaceLearningEnabled}
                    onChange={() => commitRuntimeLearning(!workspaceLearningEnabled)}
                    disabled={updateWorkspace.isPending}
                    aria-label={t("page.workspace_detail.runtime_learning_controls")}
                  />
                )}
              />
              {canManageWs && (
                <ConfigRow
                  label={t("page.workspace_detail.operating_model_advanced")}
                  note={`${settingsServices.length} ${t("page.workspace_detail.services")} · ${settingsGoals.length} ${t("nav.goals")} · ${settingsRules.length} ${t("page.workspace_detail.rule_copy.rule_count")} · ${workspaceAutomationSummary
                    ? (workspaceAutomationSummary.summary_total || 0)
                      + ((workspaceAutomationBindings || []).filter((binding: any) => binding.trigger_type !== "manual").length)
                    : settingsAutomations.length} ${t("page.scheduled_jobs.automations")}`}
                  chevron
                  onClick={() => {
                    setSettingsDraft(JSON.stringify(operatingModel || {}, null, 2));
                    setShowSettingsEditor(true);
                  }}
                />
              )}
              {readOnlyRow(t("page.workspace_detail.field_created_by"), ws.created_by_name || ws.created_by_email)}
              {readOnlyRow(t("page.workspace_detail.field_created"), formatDate(ws.created_at))}
              {readOnlyRow(t("page.workspace_detail.field_updated"), formatDate(ws.updated_at))}
              {readOnlyRow(t("page.workspace_detail.field_workspace_id"), <span className="mono">{ws.id}</span>)}
            </>
          )}
        </ConfigGroup>

        <ConfigGroup title={t("page.workspace_config.state")}>
          {canDeleteWs && (
            <ConfigRow
              label={t("page.workspace_detail.delete_workspace")}
              note={t("page.workspace_detail.deleting_this_workspace_removes_its_operating_mo")}
              danger
              disabled={deleteWorkspace.isPending}
              onClick={() => setConfirmDeleteWs(true)}
            />
          )}
        </ConfigGroup>

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
      </ConfigPage>
    );
  }

  /* ---- render ---- */

  const tabContent: Record<Tab, () => React.ReactNode> = {
    app: () => <WorkspaceAppViewSettings workspaceId={workspaceId!} canManage={canManageWs} />,
    staff: renderStaff,
    channels: renderChannels,
    documents: renderDocuments,
    rules: renderRules,
    goals: renderGoals,
    workflows: () => (
      <ConfigPage title={TAB_LABELS.workflows} wide>
        {flowsAvailable && <WorkspaceWorkflows workspaceId={workspaceId!} canManage={canWriteWs} />}
        <ScheduledJobs workspaceId={workspaceId!} workspaceStatus={ws.status} workflowsEnabled={flowsAvailable} canWriteWorkspace={canWriteWs} />
      </ConfigPage>
    ),
    records: renderRecords,
    usage: renderUsage,
    settings: renderSettings,
    collaboration: () => canAdministerWs ? (
      <WorkspaceCollaborationManagement key={`${currentUser?.id}:${workspaceId}`} workspaceId={workspaceId!} identity={`${currentUser?.id}:${currentUser?.entity_id}`}
        deliverCard={(
          <ConfigGroup>
            <ConfigRow label={t("page.clients.deliver")} note={t("page.clients.delivery_policy")} chevron onClick={() => setDeliveryOpen(true)} />
          </ConfigGroup>
        )} />
    ) : <EmptyState title={t("page.collaboration.manager_required")} />,
    "client-trials": () => canAdministerWs ? (
      <ClientTrialsTab
        workspaceId={workspaceId!}
        identity={identity}
        trialId={searchParams.get("trial")}
        onOpen={(deliveryId) => {
          const next = new URLSearchParams(searchParams);
          next.set("tab", "client-trials");
          next.set("trial", deliveryId);
          setSearchParams(next);
        }}
        onClose={() => {
          const next = new URLSearchParams(searchParams);
          next.delete("trial");
          setSearchParams(next);
        }}
      />
    ) : <EmptyState title={t("page.collaboration.manager_required")} />,
  };
  const trialFrameOpen = tab === "client-trials" && !!searchParams.get("trial");

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "clamp(0.5rem, 2.5vw, 1rem)", overflow: "hidden", position: "relative", zIndex: 10 }}>
      {/* Header */}
      <PageHeader
        title={simpleSetupMode ? t("page.workspace_detail.blueprint_setup_title") : ws.name}
        subtitle={simpleSetupMode ? ws.name : undefined}
        // The customer-trial frame has one Workspace: no list to go back to.
        breadcrumb={!simpleSetupMode && !EMBED ? (
          <button type="button" className="page-header-back" onClick={() => navigate("/workspaces")}>
            <IconChevronLeft size={14} />
            {t("nav.workspaces")}
          </button>
        ) : undefined}
        // Working on its own is the normal state; only a manual Workspace says so.
        meta={!simpleSetupMode && ws.status !== "active" ? (
          <StatusBadge type="inactive" dot>{t("page.workspaces.mode_manual")}</StatusBadge>
        ) : undefined}
        actions={<>
        {usageGuide && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setGuideOpen(true)}
            ariaLabel={t("page.blueprint_detail.workspace_usage_guide", { name: ws.name })}
            title={t("page.blueprint_detail.workspace_usage_guide", { name: ws.name })}
            style={{ width: 30, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" }}
          >
            <IconHelp size={18} />
          </Button>
        )}
        {simpleSetupMode && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => navigate(`/chat?workspace=${encodeURIComponent(ws.id)}&setup=1`)}
          >
            {t("page.workspace_detail.open_workspace_chat")}
          </Button>
        )}
        {!simpleSetupMode && canManageWs && isSimulation && (
          <Button variant="primary" size="sm" onClick={() => setPromoteOpen(true)}>
            {t("page.workspace_detail.promote_to_live")}
          </Button>
        )}
        {!simpleSetupMode && (() => {
          // Everything else a Workspace can do lives behind one menu.
          const items = [
            ...(launchReviewPage && !isSimulation ? [{
              key: "report",
              label: t("page.simulation_report.launch_review"),
            }] : []),
            ...(canManageWs && ws.blueprint_update?.blueprint_id ? [{
              key: "updates", label: t("page.workspaces.blueprint_check_updates"),
            }] : []),
            // A Workspace on its own package (or with none yet) takes a newer file built anywhere.
            ...(canManageWs && (!ws.blueprint_update?.blueprint_id || ws.blueprint_update.blueprint_id.startsWith("team-package:")) ? [{
              key: "package", label: t("page.workspaces.package_release_title"),
            }] : []),
            ...(canAdministerWs ? [{ key: "export", label: t("page.workspace_detail.export_as_blueprint") }] : []),
            ...(canAdministerWs && tab !== "collaboration" ? [{ key: "deliver", label: t("page.clients.deliver") }] : []),
          ];
          if (!items.length) return null;
          return (
            <Dropdown
              align="right"
              trigger={(
                <Button variant="ghost" size="sm" ariaLabel={t("nav.more")} style={{ width: 30, padding: 0 }}>
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                    <circle cx="3.5" cy="8" r="1.3" /><circle cx="8" cy="8" r="1.3" /><circle cx="12.5" cy="8" r="1.3" />
                  </svg>
                </Button>
              )}
              items={items}
              onSelect={(key: string) => {
                if (key === "updates") setShowBlueprintUpgrade(true);
                if (key === "package") setShowPackageRelease(true);
                if (key === "export") setExportOpen(true);
                if (key === "deliver") setDeliveryOpen(true);
              }}
            />
          );
        })()}
        </>}
      />

      {canAdministerWs && <ClientDeliveryDialog key={ws.id} open={deliveryOpen} onClose={() => setDeliveryOpen(false)} workspace={{ id: ws.id, name: ws.name }} />}
      <WorkspaceConnectionNotice workspaceId={ws.id} />

      <BlueprintGuideDialog
        open={Boolean(usageGuide && (guideOpen || showBlueprintGuideOnArrival))}
        guide={usageGuide}
        workspaceName={ws.name}
        onClose={dismissBlueprintGuide}
      />

      {canManageWs && (
        <ExportBlueprintModal
          open={exportOpen}
          onClose={() => setExportOpen(false)}
          workspaceId={ws.id}
          workspaceName={ws.name}
        />
      )}
      {canManageWs && (
        <WorkspacePackageReleaseDialog
          open={showPackageRelease}
          onClose={() => setShowPackageRelease(false)}
          onPublished={() => { setShowPackageRelease(false); setShowBlueprintUpgrade(true); }}
          workspaceId={ws.id}
          needsAdoption={!ws.blueprint_update?.blueprint_id}
        />
      )}
      {showBlueprintUpgrade && (
        <BlueprintUpgradeDialog
          open
          onClose={() => setShowBlueprintUpgrade(false)}
          workspaceId={ws.id}
          workspaceName={ws.name}
        />
      )}
      {canManageWs && isSimulation && (
        <PromoteWorkspaceModal
          open={promoteOpen}
          onClose={() => setPromoteOpen(false)}
          workspaceId={ws.id}
          workspaceName={ws.name}
        />
      )}

      <Modal
        open={!simpleSetupMode && shouldShowWorkspaceSetup && !showBlueprintGuideOnArrival && setupStatus?.status !== "not_required"}
        onClose={dismissWorkspaceSetup}
        title={t(
          setupStatus?.ready
            ? "page.workspace_detail.blueprint_setup_complete_title"
            : "page.workspace_detail.blueprint_setup_title",
        )}
        maxWidth="560px"
        footer={
          setupStatus?.ready ? (
            <>
              <Button
                variant="primary"
                onClick={() => {
                  dismissWorkspaceSetup();
                  if (workspaceId) navigate(`/chat?workspace=${encodeURIComponent(workspaceId)}${showBlueprintGuideOnArrival ? "&guide=1" : ""}`);
                }}
              >
                {t("page.workspace_detail.open_workspace_chat")}
              </Button>
              <Button variant="ghost" onClick={dismissWorkspaceSetup}>
                {t("page.workspace_detail.stay_on_details")}
              </Button>
            </>
          ) : setupStatusError ? (
            <>
              <Button variant="primary" onClick={() => refetchSetupStatus()}>
                {t("page.workspace_detail.check_setup_again")}
              </Button>
              <Button variant="ghost" onClick={dismissWorkspaceSetup}>
                {t("action.close")}
              </Button>
            </>
          ) : setupStatus && !setupStatus.ready ? (
            <>
              {hasAutomaticBlueprintSetup && canManageWs ? (
                <Button
                  variant="primary"
                  onClick={openWorkspaceChatDuringSetup}
                >
                  {t("page.workspace_detail.open_workspace_chat")}
                </Button>
              ) : (
                <Button
                  variant="primary"
                  onClick={() => {
                    const target = blueprintSetupDestination(
                      setupStatus.incomplete_checks[0] || {},
                    );
                    dismissWorkspaceSetup();
                    if (target === "/integrations") navigate(target);
                    else handleTabChange(target);
                  }}
                >
                  {t("page.workspace_detail.review_setup")}
                </Button>
              )}
              <Button variant="ghost" onClick={dismissWorkspaceSetup}>
                {t("page.workspace_detail.stay_on_details")}
              </Button>
            </>
          ) : null
        }
      >
        {setupStatusLoading ? (
          <div style={{ display: "flex", alignItems: "center", gap: 10, color: "var(--text-muted)", fontSize: 13 }}>
            <LoadingSpinner size={16} />
            <span className="sr-only">{t("page.workspace_detail.checking_blueprint_setup")}</span>
          </div>
        ) : setupStatusError ? (
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, color: "var(--text-muted)" }}>
            {t("page.workspace_detail.blueprint_setup_check_failed")}
          </p>
        ) : setupStatus?.ready ? (
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, color: "var(--text-muted)" }}>
            {t("page.workspace_detail.blueprint_setup_complete_body")}
          </p>
        ) : setupStatus ? (
          <div>
            <p style={{ margin: "0 0 14px", fontSize: 13, lineHeight: 1.6, color: "var(--text-muted)" }}>
              {hasAutomaticBlueprintSetup
                ? t("page.workspace_detail.blueprint_setup_automatic_body")
                : t("page.workspace_detail.blueprint_setup_manual_body")}
            </p>
            <div style={{ display: "grid", gap: 8 }}>
              {setupStatus.incomplete_checks.map((check, index) => (
                <div
                  key={String(check.key || `${check.todo_kind || check.kind || "setup"}-${index}`)}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 9,
                    padding: "10px 12px",
                    borderRadius: 8,
                    background: "var(--surface-muted)",
                    border: "0.5px solid var(--border-subtle)",
                    color: "var(--text-default)",
                    fontSize: 12,
                    lineHeight: 1.5,
                  }}
                >
                  <IconInfo size={14} />
                  <span>{String(check.reason || t("page.workspace_detail.setup_item_incomplete"))}</span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </Modal>

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
          <CelebrationFirework />
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
          <div style={{ padding: "10px 12px", borderRadius: 12, background: "var(--surface-muted)", border: "0.5px solid rgba(29, 29, 31,0.06)", color: "var(--text-muted)", fontSize: 12, lineHeight: 1.5 }}>
            {t("page.workspace_detail.folders_do_not_move_files_out_of_knowledge_base")}
          </div>
        </form>
      </Modal>

      <div className={simpleSetupMode ? "workspace-detail-body workspace-detail-body--plain" : "workspace-detail-body"}>
      {!simpleSetupMode && (
        <nav className="workspace-source-list" aria-label={ws.name}>
          {SOURCE_LIST_GROUPS.map((group) => {
            const items = group.items.flatMap((key) => {
              if (key === "client-trials" && !showClientTrials) return [];
              if (key === "collaboration" && !canAdministerWs) return [];
              return [{ key, label: TAB_LABELS[key] }];
            });
            if (items.length === 0) return null;
            return (
              <div key={group.key} className="workspace-source-list-group">
                <div className="workspace-source-list-label">{group.label}</div>
                {items.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    className={`workspace-source-list-item${tab === item.key ? " is-active" : ""}`}
                    aria-current={tab === item.key ? "page" : undefined}
                    onClick={() => handleTabChange(item.key)}
                  >
                    {item.label}
                    {item.key === "app" && appDiscovery.badgeVisible && <FeatureDiscoveryBadge label={t("component.feature_discovery.new")} />}
                  </button>
                ))}
              </div>
            );
          })}
          <button
            type="button"
            className="workspace-source-list-hint"
            onClick={() => navigate(`/chat?workspace=${encodeURIComponent(ws.id)}`)}
          >
            {t("page.workspace_detail.config_hint")}
          </button>
        </nav>
      )}

      {/* Tab content; an open customer trial fills it (its frame scrolls inside). */}
      <div style={trialFrameOpen
        ? { flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden", padding: "4px 4px 8px" }
        : { flex: 1, minWidth: 0, overflowY: "auto", padding: "4px 4px 20px" }}>
        {tabContent[simpleSetupTab || tab]()}
      </div>
      </div>

      <ConfirmDialog
        open={!!confirmRemoveStaff}
        onClose={() => setConfirmRemoveStaff(null)}
        onConfirm={() => { if (canAdministerWs && confirmRemoveStaff) removeStaff.mutate(confirmRemoveStaff); }}
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
        open={canManageWs && !!confirmRemoveChannel}
        onClose={() => setConfirmRemoveChannel(null)}
        onConfirm={() => { if (canManageWs && confirmRemoveChannel) removeChannel.mutate(confirmRemoveChannel.id); }}
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
        open={canDeleteWs && confirmDeleteWs}
        onClose={() => setConfirmDeleteWs(false)}
        onConfirm={() => { if (canDeleteWs) deleteWorkspace.mutate(); }}
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
        border: "1px solid rgba(29, 29, 31,0.3)",
        background: "rgba(29, 29, 31,0.06)",
        borderRadius: 8,
      }}
    >
      <UserAvatar name={staff.name} avatarUrl={staff.avatar_url} size={28} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "var(--text-muted)",
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
            color: "var(--text-faint)",
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
        aria-label={t("ui.clear")}
        style={{
          width: 22,
          height: 22,
          borderRadius: 6,
          background: "transparent",
          border: "none",
          cursor: "pointer",
          color: "var(--text-muted)",
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
