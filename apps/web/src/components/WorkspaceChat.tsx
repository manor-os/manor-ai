/**
 * WorkspaceChat — interactive group chat for workspace operations.
 *
 * Default: messages go to Manor AI (master agent).
 * @mention: type "@" to open agent picker dropdown, routes message to that agent.
 * Also displays workspace events: proposals, agent updates, step events, goal alerts.
 */
import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo, type CSSProperties } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  ApiError,
  api,
  type WorkspaceAgentMapping,
  type WorkspaceChatEntrypoint,
  type WorkspaceLedgerOverview,
} from "../lib/api";
import { invalidateKnowledgeQueries } from "../lib/knowledgeInvalidation";
import {
  manualSkillReferences,
  resolveManualSkillReferenceIds,
} from "../lib/manualSkillRefs";
import { MANOR_AGENT_NAME } from "../lib/constants";
import type { Workspace, Agent } from "../lib/types";
import { canManageWorkspace } from "../lib/permissions";
import { useWebSocket } from "../lib/websocket";
import { useAuthStore } from "../stores/auth";
import { useToastStore } from "../stores/toast";
import { t } from "../lib/i18n";
import { useChatAutoFollow } from "../lib/useChatAutoFollow";
import { openDetail, closeDetail, useDetailStore } from "../stores/detail";
import { openAgentEditModal } from "../stores/agentEditModal";
import ChatMarkdown from "./ChatMarkdown";
import {
  ArtifactSummaryCards,
  OutputPanel,
  artifactDedupKey,
  chatModeTemplateSamples,
  deriveMessageArtifacts,
  filterMessageArtifactsAlreadyRepresented,
  type OutputArtifact,
} from "./EmbeddedChat";
import {
  ChatMessageReferenceStrip,
  chatMessageReferencesFromAttachments,
  parseUserMessageDisplay,
} from "./ChatMessageDisplay";
import WorkflowResultCard from "./WorkflowResultCard";
import SimulationArtifactGallery from "./SimulationArtifactGallery";
import {
  WorkspaceSimulationRuntimeBar,
  useWorkspaceSimulationRuntime,
} from "./WorkspaceSimulationRuntime";
import AssistantMessageBlocks, {
  assistantPendingActionKindForMessage,
} from "./AssistantMessageBlocks";
import CollapsibleSentMessage from "./chat/CollapsibleSentMessage";
import ChatMessageActions, {
  chatMessageActionText,
  isRetryableAssistantMessage,
  type ChatMessageFeedbackRating,
} from "./chat/ChatMessageActions";
import useChatMessageFeedback from "./chat/useChatMessageFeedback";
import { chatFeedbackSubjectKey } from "../lib/chat-feedback-queue.mjs";
import ChatTimestamp from "./chat/ChatTimestamp";
import ChatScrollRail, {
  type ChatScrollRailMarker,
} from "./chat/ChatScrollRail";
import {
  buildChatScrollRailTurnMarkers,
  type ChatScrollRailTurnSource,
} from "./chat/chatScrollRailTurns";
import ManorAvatar from "./ui/ManorAvatar";
import AgentActivityOrb, { inferAgentActivity } from "./ui/AgentActivityOrb";
import { agentAvatarSeed } from "./ui/AgentAvatar";
import UserAvatar from "./ui/UserAvatar";
import WorkspaceIconTile from "./ui/WorkspaceIcon";
import WorkspaceConnectionNotice from "./workspaces/WorkspaceConnectionNotice";
import WorkspaceStatsQuickAccess from "./workspaces/WorkspaceStatsQuickAccess";
import WorkspaceLedgerConfigurationDialog from "./workspaces/WorkspaceLedgerConfigurationDialog";
import ChatActionCard, { ApprovalSummary } from "./ui/ChatActionCard";
import { isErrorHitlCard } from "../lib/approvalCopy";
import { PendingActionKind } from "../lib/pendingActionKinds";
import InlineTips from "./ui/InlineTips";
import ToolCallList from "./ui/ToolCallList";
import LoadingSpinner from "./ui/LoadingSpinner";
import Button from "./ui/Button";
import AnchoredPopover from "./ui/AnchoredPopover";
import ConfirmDialog from "./ui/ConfirmDialog";
import Input from "./ui/Input";
import Tooltip from "./ui/Tooltip";
import { ChatMessagesSkeleton, SkeletonLine } from "./ui/Skeleton";
import ResizablePaneGroup from "./ui/ResizablePaneGroup";
import ChatInputFooter, {
  manualSkillLabel,
  stripManualSkillTokens,
  stripWorkflowInvokeToken,
  type AttachedItem,
  type ChatComposerSendContext,
  type ManualSkillItem,
  type MentionOption,
  type WorkflowInvokeItem,
} from "./ChatInputFooter";
import ChatModeToolbar from "./ChatModeToolbar";
import ChatModeTemplateGallery from "./ChatModeTemplateGallery";
import { prepareTemplateRemix } from "./templateRemix";
import type { ChatBoxMode } from "./ChatModeSelector";
import {
  getDefaultChatModePayload,
  getChatModeInputPlaceholder,
  type ChatModePayload,
} from "./ChatModeBriefPanel";
import WorkspaceWorkflowRunHost, {
  buildWorkspaceWorkflowRunGroups,
  workflowRunIdForMessage,
  workflowHostOwnedMessageIds,
} from "./workflows/WorkspaceWorkflowRunHost";
import {
  IconBrain,
  IconChatBubble,
  IconCheck,
  IconClose,
  IconDocument,
  IconEdit,
  IconFlow,
  IconPause,
  IconPlay,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconSparkles,
  IconTimeline,
  IconTrash,
} from "./icons";
import {
  formatRuntimeQueueStatus,
  isRedundantApprovalResolutionReceipt,
  hitlActionTranscriptText,
  nextTypewriterSlice,
  pendingHITLIds,
  parseToolCalls,
  TYPEWRITER_TICK_MS,
  type ChatMessage,
  type ResponseSurfaceSubmissionReceipt,
  type ResponseSurfaceSubmissionResult,
  type SubAgentEvent,
  type ToolCall,
} from "../lib/chatStream";
import {
  collectResponseSurfaceSubmissionFailureMessageIds,
  collectResponseSurfaceSubmissionReceipts,
  isResponseSurfaceSubmissionMessage,
  rollbackResponseSurfaceSubmissionMessages,
  settleResponseSurfaceSubmissionFailure,
  responseSurfaceSubmissionMessage,
  responseSurfaceSubmissionMeta,
} from "../lib/responseSurface";
import {
  ChatStreamCompletionStatus,
  useChatStreamStore,
} from "../stores/chatStream";
import {
  formatUserFacingStructuredText,
  formatUserFacingText,
} from "../lib/taskDisplay";
import { relativeTime } from "../lib/format";
import Chip from "./ui/Chip";
import StatusBadge from "./ui/StatusBadge";
import {
  proposalApprovedRowIds,
  proposalBasisView,
  proposalImpactExplainer,
  proposalImpactLabel,
  proposalPriorityLabel,
  proposalTaskEntries,
} from "../lib/proposalDisplay";
import {
  INSERT_CHAT_COMPOSER_EVENT,
  type InsertChatComposerDetail,
} from "../lib/selectionActions";

/* ── Types ── */

function maybeLocalCodingRunNoticeForTools(_tools: ToolCall[]): string | null {
  return null;
}

interface WsMessage {
  id: string;
  conversation_id: string;
  created_at: string;
  updated_at?: string | null;
  body: string | null;
  tool_calls?: any;
  assistant_blocks?: any[] | null;
  message_kind: string;
  author_kind: string;
  author_user_id?: string | null;
  author_user_name?: string | null;
  author_user_email?: string | null;
  author_user_avatar_url?: string | null;
  author_subscription_id: string | null;
  refs: { type: string; id: string; title?: string; name?: string; status?: string; priority?: number }[] | null;
  attachments: any;
  meta: Record<string, any> | null;
  pending_action: { kind: string; [k: string]: any } | null;
  /** Tool-call HITL cards (the `__hitl__` envelope channel), read from
   *  `messages.metadata`. Separate from `pending_action`, which the
   *  governance/step gate writes — a gated tool call only ever lands here,
   *  and resolving one means replying into the chat stream, not POSTing to
   *  `/messages/{id}/resolve`. Same field name and shape as the main-chat
   *  `Message` type in lib/types.ts. */
  hitl_requests?: Record<string, any>[] | null;
  resolved_at: string | null;
  resolution: {
    choice: string;
    note?: string;
    payload?: Record<string, any>;
  } | null;
  resolved_by_user_id?: string | null;
  resolved_by_user_name?: string | null;
  resolved_by_user_email?: string | null;
  resolved_by_user_avatar_url?: string | null;
}

enum ChatFeedbackTargetKind {
  RESPONSE = "response",
  TASK_COMPLETION = "task_completion",
  PLAN_COMPLETION = "plan_completion",
  NONE = "none",
}

type WorkspaceLifecycleAction = "start" | "pause";

interface AgentInfo {
  id: string;
  name: string;
  avatar_url?: string;
  avatar_seed?: string;
}

interface TaskSessionPresentation {
  hostName: string;
  hostAvatarUrl?: string | null;
  hostAvailable?: boolean;
  objective?: string;
  phases?: string[];
}

interface WorkspaceChatProps {
  workspaceId: string;
  workspace?: Workspace;
  workspaceName?: string;
  workspaceCoverUrl?: string;
  threadRef?: { kind: "task" | "plan" | "goal"; id: string };
  agentMappings?: WorkspaceAgentMapping[];
  taskSession?: TaskSessionPresentation;
}

interface WorkspaceAutonomyGoal {
  id: string;
  title?: string | null;
}

interface WorkspaceAutonomyGoalEditor {
  id: string | null;
  title: string;
}

type WorkspaceAutonomyGoalEditorOrigin =
  | { kind: "add" }
  | { kind: "edit"; goalId: string };

/* ── Helpers ── */

const AGENT_COLORS = [
  "#6d6fb2",
  "#5a8ea6",
  "#9079c2",
  "#cf9b44",
  "#4f9c84",
  "#c96a98",
  "#d65f59",
];
function agentColor(name: string) {
  return AGENT_COLORS[
    (name || "").split("").reduce((a, c) => a + c.charCodeAt(0), 0) %
      AGENT_COLORS.length
  ];
}

function stripWorkspaceAgentMention(value: string, agentName: string) {
  const token = `@${agentName}`.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return value
    .replace(new RegExp(`(^|\\s)${token}(?=\\s|$)`, "u"), "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .trim();
}

const WORKSPACE_FILE_ARTIFACT_KINDS = new Set<OutputArtifact["kind"]>([
  "presentation",
  "document",
  "pdf",
  "spreadsheet",
  "diagram",
  "code",
  "file",
  "image",
  "video",
  "audio",
  "page",
]);

function workspaceFileArtifacts(
  message: ChatMessage,
  streaming: boolean,
): OutputArtifact[] {
  return deriveMessageArtifacts(message, streaming).filter((artifact) =>
    WORKSPACE_FILE_ARTIFACT_KINDS.has(artifact.kind),
  );
}

function isGovernanceApprovalMessage(msg: WsMessage) {
  // An `error` card rides the same pending_action kind but is not a policy
  // pause — nothing about it came from the workspace rules, so it must not be
  // attributed to them.
  return (
    msg.pending_action?.kind === PendingActionKind.GOVERNANCE_APPROVAL
    && !isErrorHitlCard(msg.pending_action?.hitl_type)
  );
}

function systemSenderName(msg: WsMessage) {
  if (isGovernanceApprovalMessage(msg)) {
    return t("component.workspace_chat.workspace_rules");
  }
  return MANOR_AGENT_NAME;
}
function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

const WORKSPACE_CHAT_DRAFT_PREFIX = "manor_workspace_chat_draft:";
const WORKSPACE_CHAT_PAGE_SIZE = 75;
const STRATEGIST_ACTIVITY_RECONCILE_MS = 3_000;
const STRATEGIST_ACTIVITY_RECONCILE_TIMEOUT_MS = 10_000;
const STRATEGIST_ACTIVITY_RECONCILE_MAX_DELAY_MS = 30_000;
const STRATEGIST_ACTIVITY_RECONCILE_MAX_MS = 11 * 60 * 1_000;
const AGENT_GREETING_PLAYBACK_PREFIX = "manor_workspace_agent_greetings_played:";
const AGENT_GREETING_FRESH_WORKSPACE_WINDOW_MS = 30 * 60 * 1000;
const AGENT_GREETING_BETWEEN_MESSAGE_DELAY_MS = 280;

interface AgentGreetingPlayback {
  messageIds: string[];
  activeIndex: number;
  visibleCharacters: number;
  expectedTotal: number;
}

function isAgentGreetingMessage(message: WsMessage) {
  return message.meta?.agent_greeting === true;
}

function agentGreetingSequence(message: WsMessage) {
  const value = Number(message.meta?.agent_greeting_sequence);
  return Number.isFinite(value) ? value : Number.MAX_SAFE_INTEGER;
}

function agentGreetingPlaybackKey(workspaceId: string) {
  return `${AGENT_GREETING_PLAYBACK_PREFIX}${workspaceId}`;
}

function hasAgentGreetingPlaybackCompleted(workspaceId: string) {
  try {
    return window.localStorage.getItem(agentGreetingPlaybackKey(workspaceId)) === "1";
  } catch {
    return false;
  }
}

function playedAgentGreetingMessageIds(workspaceId: string) {
  try {
    const stored = window.localStorage.getItem(agentGreetingPlaybackKey(workspaceId));
    if (!stored || stored === "1") return new Set<string>();
    const parsed = JSON.parse(stored);
    const messageIds = Array.isArray(parsed?.message_ids) ? parsed.message_ids : [];
    return new Set(messageIds.filter((value: unknown) => typeof value === "string"));
  } catch {
    return new Set<string>();
  }
}

function hasAgentGreetingMessagePlayed(workspaceId: string, messageId: string) {
  return playedAgentGreetingMessageIds(workspaceId).has(messageId);
}

function markAgentGreetingMessagePlayed(
  workspaceId: string,
  messageId: string,
  expectedTotal: number,
) {
  try {
    if (hasAgentGreetingPlaybackCompleted(workspaceId)) return;
    const messageIds = playedAgentGreetingMessageIds(workspaceId);
    messageIds.add(messageId);
    window.localStorage.setItem(
      agentGreetingPlaybackKey(workspaceId),
      expectedTotal > 0 && messageIds.size >= expectedTotal
        ? "1"
        : JSON.stringify({ message_ids: Array.from(messageIds) }),
    );
  } catch {
    // Storage restrictions should not break workspace chat rendering.
  }
}

function isFreshWorkspaceForAgentGreetings(createdAt?: string) {
  if (!createdAt) return false;
  const timestamp = Date.parse(createdAt);
  if (!Number.isFinite(timestamp)) return false;
  const age = Date.now() - timestamp;
  return age >= -60_000 && age <= AGENT_GREETING_FRESH_WORKSPACE_WINDOW_MS;
}

function prefersReducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

function workspaceChatDraftKey(workspaceId: string) {
  return `${WORKSPACE_CHAT_DRAFT_PREFIX}${workspaceId}`;
}

function loadWorkspaceChatDraft(workspaceId: string) {
  try {
    return (
      window.sessionStorage.getItem(workspaceChatDraftKey(workspaceId)) || ""
    );
  } catch {
    return "";
  }
}

function saveWorkspaceChatDraft(workspaceId: string, value: string) {
  try {
    const key = workspaceChatDraftKey(workspaceId);
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch {
    // Private browsing/storage restrictions should not break chat input.
  }
}

function isStrategistActivityTerminalState(state: unknown) {
  return state === "completed" || state === "skipped" || state === "failed";
}

function canApplyStrategistActivityState(
  currentState: unknown,
  nextState: unknown,
) {
  if (!isStrategistActivityTerminalState(currentState)) return true;
  return (
    currentState === nextState ||
    (currentState === "skipped" && nextState === "failed")
  );
}

function strategistActivityReconciliationStopped(activity: unknown) {
  return Boolean(
    activity &&
      typeof activity === "object" &&
      typeof (activity as Record<string, any>).reconciliation_stopped_at ===
        "string",
  );
}

function mergeWorkspaceMessages(existing: WsMessage[], incoming: WsMessage[]) {
  const byId = new Map<string, WsMessage>();
  existing.forEach((message) => {
    if (message.id) byId.set(message.id, message);
  });
  incoming.forEach((message) => {
    if (!message.id) return;
    const current = byId.get(message.id);
    const currentActivity = current?.meta?.strategist_activity;
    const nextActivity = message.meta?.strategist_activity;
    const preserveReconciliationStop = Boolean(
      currentActivity?.state === "running" &&
        nextActivity?.state === "running" &&
        strategistActivityReconciliationStopped(currentActivity),
    );
    const nextMessage = preserveReconciliationStop
      ? {
          ...message,
          meta: {
            ...(message.meta || {}),
            strategist_activity: {
              ...nextActivity,
              reconciliation_stopped_at:
                currentActivity.reconciliation_stopped_at,
            },
          },
        }
      : message;
    if (
      current &&
      !canApplyStrategistActivityState(
        currentActivity?.state,
        nextActivity?.state,
      )
    ) {
      byId.set(message.id, {
        ...message,
        meta: {
          ...(message.meta || {}),
          strategist_activity: currentActivity,
        },
      });
      return;
    }
    byId.set(message.id, nextMessage);
  });
  return Array.from(byId.values());
}

function applyStrategistActivityTransition(
  messages: WsMessage[],
  messageId: string,
  activity: Record<string, any>,
) {
  let changed = false;
  const next = messages.map((message) => {
    if (message.id !== messageId) return message;
    const currentState = message.meta?.strategist_activity?.state;
    if (!canApplyStrategistActivityState(currentState, activity.state)) {
      return message;
    }
    changed = true;
    const nextActivity = {
      ...(message.meta?.strategist_activity || {}),
      ...activity,
    };
    if (isStrategistActivityTerminalState(nextActivity.state)) {
      delete nextActivity.reconciliation_stopped_at;
    }
    return {
      ...message,
      meta: {
        ...(message.meta || {}),
        strategist_activity: nextActivity,
      },
    };
  });
  return changed ? next : messages;
}

function createWorkspaceLifecycleActivityMessage(
  workspaceId: string,
  conversationId: string | undefined,
  body: string,
  phase: "starting" | "pausing" | "completed" | "failed",
  action: WorkspaceLifecycleAction,
  id?: string,
): WsMessage {
  const createdAt = new Date().toISOString();
  return {
    id: id || `local-workspace-lifecycle-${workspaceId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    conversation_id: conversationId || "",
    created_at: createdAt,
    updated_at: createdAt,
    body,
    message_kind: "strategist_activity",
    author_kind: "system",
    author_user_id: null,
    author_user_name: null,
    author_user_email: null,
    author_user_avatar_url: null,
    author_subscription_id: null,
    refs: null,
    attachments: [],
    tool_calls: null,
    assistant_blocks: null,
    meta: {
      workspace_lifecycle: true,
      workspace_lifecycle_phase: phase,
      workspace_lifecycle_action: action,
    },
    pending_action: null,
    hitl_requests: null,
    resolved_at: null,
    resolution: null,
  };
}

/** Mark locally-remembered open action cards closed when an authoritative
 *  page omits them. `mergeWorkspaceMessages` is a union — it can update a
 *  card only if the server hands it back, and an answered card leaves the
 *  pinned set instead of returning marked resolved. Callers must only pass a
 *  page the server flagged as carrying the complete open set. */
function closeActionsMissingFrom(existing: WsMessage[], authoritative: WsMessage[]) {
  const present = new Set(authoritative.map((m) => m.id));
  let changed = false;
  const next = existing.map((msg) => {
    if (!isOpenPendingAction(msg) || present.has(msg.id)) return msg;
    changed = true;
    return { ...msg, resolved_at: new Date().toISOString() };
  });
  return changed ? next : existing;
}

/* ── Local streaming message ── */
type WorkspaceLocalMsg = ChatMessage & {
  id?: string;
  agentName?: string;
  agentColor?: string;
};

function delegatedAgentRunsFromMeta(meta: Record<string, any> | null) {
  const runs = meta?.sub_agent_events;
  return Array.isArray(runs)
    ? runs.filter((run): run is SubAgentEvent => Boolean(run && typeof run === "object"))
    : [];
}

const LOCAL_MESSAGE_DEDUPE_WINDOW_MS = 2 * 60 * 1000;

function normalizeMessageText(value: string | null | undefined) {
  return (value || "").replace(/\s+/g, " ").trim();
}

function compactWorkspaceRailText(value: unknown) {
  const raw =
    typeof value === "string"
      ? value
      : value == null
        ? ""
        : JSON.stringify(value);
  return raw
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[#>*_~]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function workspaceRailPreviewFromText(value: unknown, fallbackTitle: string) {
  const compact = compactWorkspaceRailText(value);
  if (!compact) return { title: fallbackTitle, excerpt: "" };
  const titleLength = compact.length > 58 ? 58 : compact.length;
  const title =
    compact.length > titleLength
      ? `${compact.slice(0, titleLength).trim()}...`
      : compact;
  const excerptSource =
    compact.length > titleLength ? compact.slice(titleLength).trim() : compact;
  const excerpt =
    excerptSource.length > 150
      ? `${excerptSource.slice(0, 150).trim()}...`
      : excerptSource;
  return { title, excerpt };
}

function messageTimestampMs(value: string | null | undefined) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function persistedMessageSortRank(msg: WsMessage) {
  if (msg.author_kind === "user") return 0;
  if (msg.author_kind === "agent") return 1;
  return 2;
}

function localMessageSortRank(msg: WorkspaceLocalMsg) {
  return msg.role === "user" ? 0 : 1;
}

function isRunningStreamPlaceholder(msg: WsMessage) {
  const status = msg.meta?.stream_status;
  return (
    msg.author_kind === "agent" &&
    (status === "running" || status === "streaming")
  );
}

function isDuplicatePersistedUserMessage(
  persisted: WsMessage,
  local: WorkspaceLocalMsg,
) {
  if (persisted.author_kind !== "user" || local.role !== "user") return false;

  const persistedText = normalizeMessageText(persisted.body);
  const localText = normalizeMessageText(local.content);
  if (!persistedText || !localText) return false;

  const sameText =
    persistedText === localText ||
    localText.startsWith(`${persistedText} [Attached:`);
  if (!sameText) return false;

  const persistedAt = messageTimestampMs(persisted.created_at);
  const localAt = messageTimestampMs(local.timestamp || "");
  if (!Number.isFinite(persistedAt) || !Number.isFinite(localAt)) return true;
  return Math.abs(persistedAt - localAt) <= LOCAL_MESSAGE_DEDUPE_WINDOW_MS;
}

function isDuplicatePersistedAssistantMessage(
  persisted: WsMessage,
  local: WorkspaceLocalMsg,
) {
  if (persisted.author_kind === "user" || local.role !== "assistant") return false;
  if (local.id && persisted.id === local.id) return !isRunningStreamPlaceholder(persisted);

  const persistedText = normalizeMessageText(persisted.body);
  const localText = normalizeMessageText(local.content);
  if (!persistedText || !localText) return false;
  if (persistedText !== localText) return false;

  const persistedAt = messageTimestampMs(persisted.created_at);
  const localAt = messageTimestampMs(local.timestamp || "");
  if (!Number.isFinite(persistedAt) || !Number.isFinite(localAt)) return true;
  return Math.abs(persistedAt - localAt) <= LOCAL_MESSAGE_DEDUPE_WINDOW_MS;
}

function shouldCollapseWorkspaceMessage(msg: WsMessage, isUser: boolean) {
  if (isUser) return true;
  return msg.message_kind !== "proposal" && !msg.pending_action;
}

function messageRefId(msg: WsMessage, refType: string) {
  const ref = (msg.refs || []).find((item) => item.type === refType && item.id);
  return ref?.id || null;
}

function taskRefs(msg: WsMessage) {
  const seen = new Set<string>();
  return (msg.refs || []).filter((ref) => {
    if (ref.type !== "task" || !ref.id || seen.has(ref.id)) return false;
    seen.add(ref.id);
    return true;
  });
}

function taskRefLabel(msg: WsMessage, index: number) {
  const ref = taskRefs(msg)[index];
  const pendingTitles = Array.isArray(msg.pending_action?.task_titles)
    ? msg.pending_action?.task_titles
    : [];
  const label = ref?.title || ref?.name || pendingTitles[index];
  if (typeof label === "string" && label.trim()) return formatUserFacingText(label.trim());
  if (ref?.id) return `#${ref.id.slice(-6)}`;
  return t("component.workspace_chat.task_link_label").replace(
    "{index}",
    String(index + 1),
  );
}

function taskRefMeta(ref: { id?: string; status?: string }) {
  const parts = [];
  if (ref.status) parts.push(formatUserFacingText(ref.status.replace(/_/g, " ")));
  if (ref.id) parts.push(`#${ref.id.slice(-6)}`);
  return parts.join(" · ");
}

type ParsedProposalTask = {
  rank?: string;
  title: string;
  impact?: string;
  detail?: string;
};

type ParsedProposal = {
  summary: string;
  tasks: ParsedProposalTask[];
  notes: string[];
};

function cleanProposalLine(value: string) {
  return formatUserFacingText(
    value
      .replace(/~~/g, "")
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/\*([^*]+)\*/g, "$1")
      .replace(/^[\s>]+/, "")
      .replace(/\s+/g, " ")
      .trim(),
  );
}

function parseProposalTaskLine(line: string): ParsedProposalTask | null {
  const text = cleanProposalLine(line).replace(/^[•*-]\s+/, "").trim();
  const match = text.match(/^(?:\[(\d+)\]\s*)?(.+?)(?:\s*\(([+-]\d+)\))?$/);
  if (!match) return null;
  const [, rank, rawTitle, impact] = match;
  const title = rawTitle.trim();
  if (!title || (!rank && !impact && title.length < 8)) return null;
  return { rank, title, impact };
}

function splitProposalNotes(value: string) {
  const text = cleanProposalLine(value);
  if (!text) return [];
  const firstNumberedIndex = text.search(/(?:^|\s)(?:\d+\.|\(\d+\))\s+/);
  const preface =
    firstNumberedIndex > 0
      ? cleanProposalLine(text.slice(0, firstNumberedIndex).replace(/[:;,\s-]+$/, ""))
      : "";
  const numberedBody =
    firstNumberedIndex > 0 ? text.slice(firstNumberedIndex).trim() : text;
  const numbered = numberedBody
    .split(/(?=(?:\d+\.|\(\d+\))\s+)/)
    .map((item) => cleanProposalLine(item.replace(/^(?:\d+\.|\(\d+\))\s*/, "")))
    .filter(Boolean);
  if (numbered.length > 1) return [preface, ...numbered].filter(Boolean);
  return [text];
}

function parseWorkspaceProposal(content: string): ParsedProposal | null {
  const lines = content
    .replace(/\r/g, "")
    .replace(/~~/g, "")
    .split("\n")
    .map(cleanProposalLine)
    .filter(Boolean);

  if (!lines.length) return null;

  const summaryParts: string[] = [];
  const tasks: ParsedProposalTask[] = [];
  const notes: string[] = [];
  let currentTask: ParsedProposalTask | null = null;
  let readingNotes = false;

  const flushTask = () => {
    if (currentTask) {
      currentTask.detail = cleanProposalLine(currentTask.detail || "");
      tasks.push(currentTask);
      currentTask = null;
    }
  };

  lines.forEach((line, index) => {
    const trimmedLine = line.trim();
    const withoutIcon = /^[•*-]\s+/.test(trimmedLine)
      ? trimmedLine
      : trimmedLine.replace(/^[^\p{L}\p{N}\[]+\s*/u, "").trim();
    const notesMatch = withoutIcon.match(/^notes?\s*:\s*(.*)$/i);
    if (notesMatch) {
      flushTask();
      readingNotes = true;
      notes.push(...splitProposalNotes(notesMatch[1]));
      return;
    }

    if (readingNotes) {
      notes.push(...splitProposalNotes(withoutIcon));
      return;
    }

    const task = /^[•*-]\s+/.test(withoutIcon)
      ? parseProposalTaskLine(withoutIcon)
      : null;
    if (task) {
      flushTask();
      currentTask = task;
      return;
    }

    if (currentTask) {
      currentTask.detail = [currentTask.detail, withoutIcon].filter(Boolean).join(" ");
      return;
    }

    const summaryLine =
      index === 0
        ? withoutIcon.replace(/^workspace proposal\s*[—-]\s*/i, "").trim()
        : withoutIcon;
    if (summaryLine) summaryParts.push(summaryLine);
  });

  flushTask();

  const summary = cleanProposalLine(summaryParts.join(" "));
  const cleanNotes = notes.map(cleanProposalLine).filter(Boolean);
  if (!summary && tasks.length === 0 && cleanNotes.length === 0) return null;
  return { summary, tasks, notes: cleanNotes };
}

function messageFeedbackTargetKind(msg: WsMessage): ChatFeedbackTargetKind {
  const value = msg.meta?.feedback_target_kind;
  if (value == null) {
    return msg.message_kind === "agent_update" &&
      (messageRefId(msg, "task") || messageRefId(msg, "plan"))
      ? ChatFeedbackTargetKind.NONE
      : ChatFeedbackTargetKind.RESPONSE;
  }
  switch (value) {
    case ChatFeedbackTargetKind.RESPONSE:
    case ChatFeedbackTargetKind.TASK_COMPLETION:
    case ChatFeedbackTargetKind.PLAN_COMPLETION:
    case ChatFeedbackTargetKind.NONE:
      return value;
    default:
      return ChatFeedbackTargetKind.NONE;
  }
}

function completionFeedbackTargetKind(
  msg: WsMessage,
):
  | ChatFeedbackTargetKind.TASK_COMPLETION
  | ChatFeedbackTargetKind.PLAN_COMPLETION
  | null {
  const targetKind = messageFeedbackTargetKind(msg);
  return targetKind === ChatFeedbackTargetKind.TASK_COMPLETION ||
    targetKind === ChatFeedbackTargetKind.PLAN_COMPLETION
    ? targetKind
    : null;
}

function isCompletionFeedbackMessage(msg: WsMessage) {
  return Boolean(
    msg.message_kind === "agent_update" && completionFeedbackTargetKind(msg),
  );
}

function planRefId(msg: WsMessage): string | null {
  const ref = (msg.refs || []).find((item) => item.type === "plan" && item.id);
  return ref?.id || null;
}

function completionFeedbackSubjectKey(msg: WsMessage): string | null {
  const targetKind = completionFeedbackTargetKind(msg);
  if (!targetKind) return null;
  const targetId = planRefId(msg) || messageRefId(msg, "task");
  return chatFeedbackSubjectKey(targetKind, targetId);
}

// Per-step / plan-lifecycle status updates ("▶ Plan started", "✗ Step … failed",
// step receipts). These are machine status, not conversation, so they render as
// quiet centered system lines rather than chat bubbles. Task-completion receipts
// are excluded — they carry user feedback and stay conversational.
function isActivityMessage(msg: WsMessage): boolean {
  if (isCompletionFeedbackMessage(msg)) return false;
  if (msg.message_kind === "strategist_activity") return true;
  if (msg.message_kind === "step_event") return true;
  return msg.message_kind === "agent_update" && Boolean(planRefId(msg));
}

function workspaceLifecycleActivityTranslationKey(msg: WsMessage): string | null {
  if (msg.meta?.workspace_lifecycle !== true) return null;
  const action = msg.meta?.workspace_lifecycle_action === "pause" ? "pause" : "start";
  const phase = typeof msg.meta?.workspace_lifecycle_phase === "string"
    ? msg.meta.workspace_lifecycle_phase
    : "completed";
  if (phase === "starting" && action === "start") {
    return "component.workspace_chat.workspace_runtime_starting";
  }
  if (phase === "pausing" && action === "pause") {
    return "component.workspace_chat.workspace_runtime_pausing";
  }
  if (phase === "failed") {
    return action === "pause"
      ? "component.workspace_chat.workspace_runtime_pause_failed"
      : "component.workspace_chat.workspace_runtime_start_failed";
  }
  return action === "pause"
    ? "component.workspace_chat.workspace_runtime_paused"
    : "component.workspace_chat.workspace_runtime_started";
}

function activityLineText(msg: WsMessage): string {
  const body = msg.body || "";
  const willRetry = /will retry/i.test(body);
  const workspaceLifecycleActivity = msg.meta?.workspace_lifecycle === true;
  const lifecycleTranslationKey = workspaceLifecycleActivity
    ? workspaceLifecycleActivityTranslationKey(msg)
    : null;
  let firstLine = (body.split(/\r?\n/)[0] || "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/[✅✔️❌⚠️🎉🚀🟢🔴🟡🧭]/g, "")
    .replace(/^\s*[▶✓✗•·]\s*/, "")
    .trim();
  // Drop the internal error / traceback after "failed:" — never surface code,
  // function names, or stack details to the user.
  firstLine = firstLine.replace(/\bfailed\b\s*:.*$/i, "failed");
  // Lifecycle copy is already curated in the locale files. Keep terms such as
  // "Strategist" and "autonomously" intact instead of applying the generic
  // machine-status vocabulary substitutions used for agent step receipts.
  let text = lifecycleTranslationKey
    ? t(lifecycleTranslationKey)
    : workspaceLifecycleActivity
      ? firstLine
      : formatUserFacingText(firstLine);
  if (willRetry && !/retry/i.test(text)) text = `${text} — will retry`;
  return text;
}

function isOpenPendingAction(msg: WsMessage) {
  return Boolean(msg.pending_action?.kind && !msg.resolved_at);
}

function compactPendingActionSubject(value: unknown) {
  if (typeof value !== "string") return "";
  const text = formatUserFacingText(value).replace(/\s+/g, " ").trim();
  if (text.length <= 72) return text;
  return `${text.slice(0, 69).trimEnd()}…`;
}

function pendingActionLabel(msg: WsMessage) {
  const action = msg.pending_action;
  const kind = action?.kind || "unknown";
  if (kind === PendingActionKind.APPROVE_PROPOSALS) {
    return t("component.workspace_chat.pending_action_approve_proposals");
  }

  const taskRef = taskRefs(msg)[0];
  const taskTitle = taskRef?.title || taskRef?.name || action?.task_titles?.[0];
  const payload = action?.payload && typeof action.payload === "object"
    ? action.payload
    : null;
  const subject = [
    action?.review_title,
    taskTitle,
    action?.title,
    payload?.action_description,
    payload?.question,
    payload?.headline,
    action?.prompt,
  ].map(compactPendingActionSubject).find(Boolean) || "";
  const isRetry =
    kind === PendingActionKind.TASK_RECOVERY
    || kind === PendingActionKind.WORKFLOW_RETRY
    || kind === PendingActionKind.RETRY_STRATEGIST_REVIEW
    || action?.hitl_type === "error";
  if (isRetry && subject) {
    return t("component.workspace_chat.pending_action_retry_named").replace(
      "{title}",
      subject,
    );
  }
  if (subject) return subject;

  const translated = t(`component.workspace_chat.pending_action_${kind}`);
  return translated === `component.workspace_chat.pending_action_${kind}`
    ? t("component.workspace_chat.pending_action_review_requested")
    : translated;
}

function workflowStarterAction(msg: WsMessage): WsMessage["pending_action"] {
  const action = msg.pending_action;
  if (!action || action.kind !== PendingActionKind.WORKFLOW_STARTER_INPUT || action.title) {
    return action;
  }
  const workflowRef = (msg.refs || []).find((ref) => ref.type === "workflow");
  const metadataTitle = String(msg.meta?.workflow_title || "").trim();
  if (!workflowRef?.title && !metadataTitle) return action;
  return {
    ...action,
    title: workflowRef?.title || metadataTitle,
  };
}

function pendingActionsLabel(count: number) {
  if (count === 1) return t("component.workspace_chat.pending_action_one");
  return t("component.workspace_chat.pending_action_many").replace("{count}", String(count));
}

function isExternalCustomerMessage(msg: WsMessage) {
  return msg.author_kind === "external" || msg.message_kind === "external_message";
}

function externalCustomerName(msg: WsMessage) {
  const raw =
    msg.meta?.sender_name ||
    msg.meta?.external_sender_name ||
    msg.meta?.visitor_name ||
    msg.meta?.sender_id;
  return typeof raw === "string" && raw.trim()
    ? raw.trim()
    : t("component.workspace_chat.customer");
}

function configuredLedgerContractIds(settings: object | null | undefined): string[] | null {
  if (!settings || typeof settings !== "object" || Array.isArray(settings)) return null;
  const record = settings as Record<string, unknown>;
  if (!("ledger_contracts" in record)) return null;
  const aliases: Record<string, string> = {
    content_ledger: "manor.content_ledger/v1",
    finance_ledger: "manor.finance_ledger/v1",
    recruiting_ledger: "manor.recruiting_ledger/v1",
    hr_ledger: "manor.recruiting_ledger/v1",
    people_ledger: "manor.recruiting_ledger/v1",
    relationship_ledger: "manor.relationship_ledger/v1",
  };
  return (Array.isArray(record.ledger_contracts) ? record.ledger_contracts : [])
    .map((item) => (
      typeof item === "string"
        ? item
        : item && typeof item === "object" && !Array.isArray(item)
          ? String((item as Record<string, unknown>).contract_id || "")
          : ""
    ))
    .map((contractId) => aliases[contractId] || contractId)
    .filter(Boolean);
}

/* ── Component ── */

export default function WorkspaceChat({
  workspaceId,
  workspace,
  workspaceName,
  workspaceCoverUrl,
  threadRef,
  agentMappings,
  taskSession,
}: WorkspaceChatProps) {
  const isTaskSession = Boolean(taskSession);
  const queryClient = useQueryClient();
  const autonomousRunning = Boolean(
    workspace?.status === "active" && workspace.heartbeat_enabled,
  );
  const [autonomyGoalEditor, setAutonomyGoalEditor] =
    useState<WorkspaceAutonomyGoalEditor | null>(null);
  const [autonomyGoalToDelete, setAutonomyGoalToDelete] =
    useState<WorkspaceAutonomyGoal | null>(null);
  const workspaceLifecycleTriggerRef = useRef<HTMLButtonElement>(null);
  const autonomyGoalAddButtonRef = useRef<HTMLButtonElement>(null);
  const autonomyGoalEditButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const autonomyGoalEditorOriginRef = useRef<WorkspaceAutonomyGoalEditorOrigin | null>(null);
  const closeAutonomyGoalEditor = useCallback((
    origin: WorkspaceAutonomyGoalEditorOrigin | null = autonomyGoalEditorOriginRef.current,
  ) => {
    setAutonomyGoalEditor(null);
    autonomyGoalEditorOriginRef.current = null;
    window.requestAnimationFrame(() => {
      const target = origin?.kind === "edit"
        ? autonomyGoalEditButtonRefs.current.get(origin.goalId)
        : autonomyGoalAddButtonRef.current;
      (target || workspaceLifecycleTriggerRef.current)?.focus();
    });
  }, []);
  const toast = useToastStore();
  const navigate = useNavigate();
  const location = useLocation();
  const [agentsExpanded, setAgentsExpanded] = useState(false);
  const [promoteSimulationOpen, setPromoteSimulationOpen] = useState(false);
  const [ledgerConfiguration, setLedgerConfiguration] = useState<{
    initialContractIds: string[];
  } | null>(null);
  const [outputOpen, setOutputOpen] = useState(false);
  const [selectedArtifact, setSelectedArtifact] =
    useState<OutputArtifact | null>(null);
  const [selectedArtifactAnchor, setSelectedArtifactAnchor] =
    useState<string | null>(null);
  const openWorkspaceArtifact = useCallback(
    (artifact: OutputArtifact, sourceAnchor?: string) => {
      setSelectedArtifact(artifact);
      setSelectedArtifactAnchor(sourceAnchor || null);
      setOutputOpen(true);
    },
    [],
  );
  const closeWorkspaceArtifact = useCallback(() => {
    setOutputOpen(false);
    setSelectedArtifactAnchor(null);
  }, []);
  const selectedArtifactReturnTo = `${location.pathname}${location.search}${
    selectedArtifactAnchor ? `#${selectedArtifactAnchor}` : location.hash || ""
  }`;
  let promoteSimulationToLive: (() => void) | undefined;
  const currentUser = useAuthStore((s) => s.user);
  const currentUserName =
    currentUser?.display_name ||
    [currentUser?.first_name, currentUser?.last_name]
      .filter(Boolean)
      .join(" ") ||
    currentUser?.email ||
    t("component.workspace_chat.you");
  const currentUserAvatar = currentUser?.avatar_url;
  const isWorkspaceMainChat = !threadRef;
  const showSimulationRuntime = Boolean(
    isWorkspaceMainChat &&
      ((workspace?.settings as Record<string, any> | undefined)?.sandbox === true ||
        workspace?.kind === "sandbox"),
  );
  // Pausing a Workspace stops autonomous runtime, not direct user chat.
  // Simulation remains read-only until it is promoted to a live Workspace.
  const composerDisabled = showSimulationRuntime;
  const bottomRef = useRef<HTMLDivElement>(null);
  const chatBodyRef = useRef<HTMLDivElement>(null);
  const { autoFollowRef, handleAutoFollowScroll } = useChatAutoFollow();
  const didInitialScrollRef = useRef(false);
  const initialPendingActionIdsRef = useRef<Set<string>>(new Set());
  const hasInitialMessagesPageRef = useRef(false);
  const realtimeAgentGreetingIdsRef = useRef<Set<string>>(new Set());
  const strategistActivityWorkspaceIdRef = useRef(workspaceId);
  strategistActivityWorkspaceIdRef.current = workspaceId;
  const [realtimeStrategistActivityIds, setRealtimeStrategistActivityIds] =
    useState<Set<string>>(() => new Set());
  const [realtimeStrategistActivityById, setRealtimeStrategistActivityById] =
    useState<Map<string, Record<string, any>>>(() => new Map());
  const strategistActivityReconciliationStateRef = useRef<
    Map<string, { startedAt: number; attempt: number }>
  >(new Map());
  const agentGreetingPlaybackStartedRef = useRef(false);
  const streamScrollFrameRef = useRef<number | null>(null);
  const lastStreamScrollAtRef = useRef(0);
  const composerEditorRef = useRef<HTMLDivElement>(null);
  const draftScope = threadRef
    ? `${workspaceId}:${threadRef.kind}:${threadRef.id}`
    : workspaceId;
  const streamSessionKey = `workspace-chat:${draftScope}`;
  const [input, setInput] = useState(() => loadWorkspaceChatDraft(draftScope));
  const [composerSeed, setComposerSeed] = useState<{
    key: string;
    attachments: AttachedItem[];
  } | null>(null);
  const [chatMode, setChatMode] = useState<ChatBoxMode>("auto");
  const [chatModePayload, setChatModePayload] = useState<ChatModePayload>(() =>
    getDefaultChatModePayload("auto"),
  );
  const currentSession = useChatStreamStore(
    (s) => s.sessions[streamSessionKey],
  );
  const streaming = Boolean(currentSession?.streaming);
  const localMsgs = (currentSession?.messages || []) as WorkspaceLocalMsg[];
  const conversationId = currentSession?.convId;
  const runtimeQueueStatus = currentSession?.runtimeQueue
    ? formatRuntimeQueueStatus(currentSession.runtimeQueue)
    : null;
  const startStream = useChatStreamStore((s) => s.startStream);
  const stopStream = useChatStreamStore((s) => s.stopStream);
  const setSessionMessages = useChatStreamStore((s) => s.setSessionMessages);
  const streamingRef = useRef(false);

  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);

  useEffect(() => {
    didInitialScrollRef.current = false;
    initialPendingActionIdsRef.current = new Set();
    hasInitialMessagesPageRef.current = false;
    lastStreamScrollAtRef.current = 0;
    if (streamScrollFrameRef.current != null) {
      window.cancelAnimationFrame(streamScrollFrameRef.current);
      streamScrollFrameRef.current = null;
    }
  }, [streamSessionKey]);

  const setInputDraft = useCallback(
    (value: string) => {
      setInput(value);
      saveWorkspaceChatDraft(draftScope, value);
    },
    [draftScope],
  );

  // Keep drafts scoped to workspace/thread, but let the global stream store keep
  // in-flight messages alive across route changes just like normal chat.
  useEffect(() => {
    setInput(loadWorkspaceChatDraft(draftScope));
    setComposerSeed(null);
    setChatMode("auto");
    setChatModePayload(getDefaultChatModePayload("auto"));
    setMentionAgent(null);
  }, [draftScope]);

  useEffect(() => {
    setOutputOpen(false);
    setSelectedArtifact(null);
    setSelectedArtifactAnchor(null);
  }, [workspaceId, threadRef?.kind, threadRef?.id]);

  // @mention state
  const [mentionAgent, setMentionAgent] = useState<AgentInfo | null>(null);

  // The workspace endpoint owns both membership and display identity. This
  // includes public template agents that are intentionally absent from the
  // entity-wide agent list.
  const { data: fetchedMappings, isLoading: fetchedMappingsLoading } = useQuery({
    queryKey: ["workspace-agents", workspaceId],
    queryFn: () => api.workspaces.agents.list(workspaceId),
    enabled: !!workspaceId,
  });
  const { data: workflowEntrypoints = [] } = useQuery({
    queryKey: ["workspace-chat-entrypoints", workspaceId],
    queryFn: () => api.workspaces.chat.listEntrypoints(workspaceId),
    enabled: Boolean(workspaceId),
  });
  const { data: workspaceStaff } = useQuery({
    queryKey: ["workspace-staff", workspaceId],
    queryFn: () => api.workspaces.staff.list(workspaceId),
    enabled: Boolean(workspaceId && !threadRef),
    staleTime: 30_000,
  });
  const workflowInvokeOptions = useMemo<WorkflowInvokeItem[]>(
    () =>
      workflowEntrypoints.map((entrypoint: WorkspaceChatEntrypoint) => ({
        bindingId: entrypoint.binding_id,
        workflowId: entrypoint.workflow_id,
        title: entrypoint.title,
        description: entrypoint.description,
        placeholder: entrypoint.placeholder,
      })),
    [workflowEntrypoints],
  );
  const canToggleWorkspace = Boolean(
    workspace
      && (workspace.status === "active" || workspace.status === "paused")
      && canManageWorkspace(currentUser, workspaceStaff || []),
  );
  const openLedgerConfiguration = useCallback((overview: WorkspaceLedgerOverview) => {
    if (!canToggleWorkspace || threadRef) return;
    setLedgerConfiguration({
      initialContractIds: configuredLedgerContractIds(workspace?.settings)
        ?? overview.ledgers.map((ledger) => ledger.contract_id),
    });
  }, [canToggleWorkspace, threadRef, workspace?.settings]);
  const handleLedgersConfigured = useCallback(() => {
    setLedgerConfiguration(null);
    toast.success(t("component.workspace_chat.ledger_configuration_saved"));
    void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
    void queryClient.invalidateQueries({
      queryKey: ["workspace-ledger-overview", workspaceId],
    });
  }, [queryClient, toast, workspaceId]);
  const workspaceAutonomyGoalsQuery = useQuery({
    queryKey: ["workspace-goals", workspaceId, "active"],
    queryFn: () => api.goals.list({ workspace_id: workspaceId, status: "active", limit: 20 }),
    enabled: Boolean(canToggleWorkspace),
    staleTime: 30_000,
  });
  const workspaceAutonomyGoals = (
    workspaceAutonomyGoalsQuery.data?.items || []
  ) as WorkspaceAutonomyGoal[];

  useEffect(() => {
    setAutonomyGoalEditor(null);
    setAutonomyGoalToDelete(null);
    autonomyGoalEditorOriginRef.current = null;
  }, [workspaceId]);

  const saveWorkspaceAutonomyGoal = useMutation({
    mutationFn: (editor: WorkspaceAutonomyGoalEditor) => {
      const title = editor.title.trim();
      if (editor.id) return api.goals.update(editor.id, { title });
      return api.goals.create({ workspace_id: workspaceId, title, target_value: 1 });
    },
    onSuccess: async (_goal, editor) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workspace-goals", workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ["workspace-goals-graph", workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ["goals"] }),
      ]);
      closeAutonomyGoalEditor(editor.id
        ? { kind: "edit", goalId: editor.id }
        : { kind: "add" });
      toast.success(t(
        editor.id
          ? "page.workspace_detail.goal_updated"
          : "page.workspace_detail.goals_saved",
      ));
    },
    onError: (error: Error) => {
      toast.error(t("page.workspace_detail.failed_to_update_goal"), error.message);
    },
  });

  const deleteWorkspaceAutonomyGoal = useMutation({
    mutationFn: (goal: WorkspaceAutonomyGoal) => api.goals.delete(goal.id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workspace-goals", workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ["workspace-goals-graph", workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ["goals"] }),
      ]);
      setAutonomyGoalEditor(null);
      setAutonomyGoalToDelete(null);
      toast.success(t("component.workspace_chat.goal_deleted"));
    },
  });

  // Props override fetched data when a parent already owns the query.
  const mappings = agentMappings || fetchedMappings || [];

  // Build subscription → agent lookup
  const subToAgent = useMemo(() => {
    const map = new Map<string, AgentInfo>();
    for (const m of mappings) {
      if (m.agent) {
        map.set(m.id, {
          id: m.agent.id,
          name: m.agent.name,
          avatar_url: m.agent.avatar_url || undefined,
          avatar_seed: m.agent.avatar_seed,
        });
      }
    }
    return map;
  }, [mappings]);

  const agentList = useMemo(() => {
    const seen = new Map<string, AgentInfo>();
    subToAgent.forEach((a) => {
      if (!seen.has(a.id)) seen.set(a.id, a);
    });
    return Array.from(seen.values());
  }, [subToAgent]);

  const mentionOptions = useMemo<MentionOption[]>(
    () =>
      agentList.map((agent) => ({
        id: agent.id,
        type: "agent",
        name: agent.name,
        avatarUrl: agent.avatar_url,
        avatarSeed: agentAvatarSeed(agent),
      })),
    [agentList],
  );
  const selectedMentions = useMemo<MentionOption[]>(
    () =>
      mentionAgent
        ? [
            {
              id: mentionAgent.id,
              type: "agent",
              name: mentionAgent.name,
              avatarUrl: mentionAgent.avatar_url,
              avatarSeed: agentAvatarSeed(mentionAgent),
            },
          ]
        : [],
    [mentionAgent],
  );

  const simulationRuntime = useWorkspaceSimulationRuntime({
    enabled: showSimulationRuntime,
    workspaceId,
  });

  const latestAgentQuickViewRequestRef = useRef(0);

  const openAgentQuickView = async (agent: AgentInfo) => {
    const requestId = ++latestAgentQuickViewRequestRef.current;
    openDetail({
      icon: (
        <UserAvatar
          name={agent.name}
          avatarUrl={agent.avatar_url}
          type="agent"
          seed={agentAvatarSeed(agent)}
          size={48}
        />
      ),
      title: agent.name,
      subtitle: t("component.workspace_chat.loading"),
      body: <LoadingSpinner size={20} />,
    });
    // Discard the late result if a newer chip click superseded this one, or
    // if the user already closed the drawer while the fetch was in flight
    // (re-calling openDetail would pop it back open).
    const isStale = () =>
      latestAgentQuickViewRequestRef.current !== requestId ||
      useDetailStore.getState().payload === null;
    try {
      const full: Agent = await api.agents.get(agent.id);
      if (isStale()) return;
      const tags = Array.isArray(full.tags) ? full.tags : [];
      const readOnlyTemplate = full.is_template && !full.entity_id;
      openDetail({
        icon: (
          <UserAvatar
            name={full.name}
            avatarUrl={full.avatar_url}
            type="agent"
            seed={agentAvatarSeed(full)}
            size={48}
          />
        ),
        title: full.name,
        subtitle: full.category || undefined,
        badges: (
          <>
            <StatusBadge
              type={full.status === "active" ? "active" : "inactive"}
              dot
              pulse={full.status === "active"}
            >
              {full.status === "active" ? t("page.agents.live") : t("page.agents.off")}
            </StatusBadge>
            {tags.slice(0, 4).map((tag, i) => (
              <Chip key={`${full.id}-chip-${tag}-${i}`} variant="slate" size="sm">
                {tag}
              </Chip>
            ))}
          </>
        ),
        body: <p style={{ margin: 0, color: "#44403c" }}>{full.description}</p>,
        primaryAction: readOnlyTemplate
          ? undefined
          : {
              label: t("page.agents.manage"),
              onClick: () => {
                closeDetail();
                navigate(`/agents/${full.id}`);
              },
            },
        secondaryActions: readOnlyTemplate
          ? undefined
          : [
              {
                label: t("action.edit"),
                icon: <IconEdit size={16} />,
                onClick: () => {
                  closeDetail();
                  openAgentEditModal(full.id);
                },
              },
            ],
      });
    } catch {
      if (isStale()) return;
      openDetail({
        icon: (
          <UserAvatar
            name={agent.name}
            avatarUrl={agent.avatar_url}
            type="agent"
            seed={agentAvatarSeed(agent)}
            size={48}
          />
        ),
        title: agent.name,
        body: (
          <p style={{ margin: 0, color: "#a8a29e" }}>
            {t("component.workspace_chat.failed_to_load_agent_details")}
          </p>
        ),
      });
    }
  };

  const workspaceAgentsLoading =
    !agentMappings &&
    fetchedMappingsLoading;

  const [wsMessages, setWsMessages] = useState<WsMessage[]>([]);
  const [voiceConversationId, setVoiceConversationId] = useState<string | null>(null);
  const {
    hydrateConversation: hydrateMessageFeedback,
    submit: submitMessageFeedback,
    values: messageFeedback,
  } = useChatMessageFeedback(currentUser?.id);
  const [agentGreetingPlayback, setAgentGreetingPlayback] =
    useState<AgentGreetingPlayback | null>(null);
  const [workspaceHistoryState, setWorkspaceHistoryState] = useState({
    hasMore: false,
    nextCursor: null as string | null,
  });
  const [loadingOlderMessages, setLoadingOlderMessages] = useState(false);
  const workspaceLifecycleTransitionIdRef = useRef<string | null>(null);
  const suppressNextAutoScrollRef = useRef(false);

  useEffect(() => {
    workspaceLifecycleTransitionIdRef.current = null;
    realtimeAgentGreetingIdsRef.current = new Set();
    setRealtimeStrategistActivityIds(new Set());
    setRealtimeStrategistActivityById(new Map());
    strategistActivityReconciliationStateRef.current = new Map();
    agentGreetingPlaybackStartedRef.current = false;
    setWsMessages([]);
    setAgentGreetingPlayback(null);
    setWorkspaceHistoryState({ hasMore: false, nextCursor: null });
    setLoadingOlderMessages(false);
    setVoiceConversationId(null);
  }, [conversationId, workspaceId, threadRef?.kind, threadRef?.id]);

  // Fetch the latest workspace chat page; older pages are loaded on demand.
  const { data: workspaceMessagesPage, isLoading: workspaceMessagesLoading } = useQuery({
    queryKey: [
      "workspace-chat",
      workspaceId,
      threadRef?.kind || "main",
      threadRef?.id || "",
    ],
    queryFn: () =>
      api.workspaces.chat.listMessagesPage(workspaceId, {
        limit: WORKSPACE_CHAT_PAGE_SIZE,
        thread_ref_kind: threadRef?.kind,
        thread_ref_id: threadRef?.id,
      }),
  });

  useEffect(() => {
    if (!workspaceMessagesPage) return;
    const items = workspaceMessagesPage.items || [];
    if (!hasInitialMessagesPageRef.current) {
      initialPendingActionIdsRef.current = new Set(
        items.filter(isOpenPendingAction).map((message) => message.id),
      );
      hasInitialMessagesPageRef.current = true;
    }
    setWsMessages((prev) =>
      mergeWorkspaceMessages(
        // When the server says this page carries every open action card, any
        // open card we still remember but it did not send has been answered
        // (from another tab, another device, or by the system). Merge-by-id
        // can only overwrite what it is handed, so mark those closed here or
        // they stay "waiting" until a reload.
        workspaceMessagesPage.open_actions_complete
          ? closeActionsMissingFrom(prev, items)
          : prev,
        items,
      ),
    );
    setWorkspaceHistoryState({
      hasMore: Boolean(workspaceMessagesPage.has_more),
      nextCursor: workspaceMessagesPage.next_cursor || null,
    });
  }, [workspaceMessagesPage]);

  const handleLoadOlderMessages = useCallback(async () => {
    if (
      !workspaceHistoryState.hasMore ||
      !workspaceHistoryState.nextCursor ||
      loadingOlderMessages
    ) {
      return;
    }
    const container = chatBodyRef.current;
    const previousHeight = container?.scrollHeight || 0;
    const previousTop = container?.scrollTop || 0;
    setLoadingOlderMessages(true);
    try {
      const page = await api.workspaces.chat.listMessagesPage(workspaceId, {
        limit: WORKSPACE_CHAT_PAGE_SIZE,
        thread_ref_kind: threadRef?.kind,
        thread_ref_id: threadRef?.id,
        before: workspaceHistoryState.nextCursor,
      });
      suppressNextAutoScrollRef.current = true;
      setWsMessages((prev) =>
        mergeWorkspaceMessages(page.items || [], prev),
      );
      setWorkspaceHistoryState({
        hasMore: Boolean(page.has_more),
        nextCursor: page.next_cursor || null,
      });
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          const nextContainer = chatBodyRef.current;
          if (!nextContainer) return;
          const heightDelta = nextContainer.scrollHeight - previousHeight;
          nextContainer.scrollTop = previousTop + heightDelta;
        });
      });
    } catch {
      // request() already reports API failures; keep the timeline stable.
    } finally {
      setLoadingOlderMessages(false);
    }
  }, [
    loadingOlderMessages,
    threadRef?.id,
    threadRef?.kind,
    workspaceHistoryState.hasMore,
    workspaceHistoryState.nextCursor,
    workspaceId,
  ]);

  // The conversation other members type into is the one their messages live
  // in; fall back to our own stream conversation before any message exists.
  const wsConversationId = useMemo(() => {
    for (const m of wsMessages as WsMessage[]) {
      if (m.conversation_id) return m.conversation_id;
    }
    return conversationId || null;
  }, [wsMessages, conversationId]);
  const voiceScopeConversationId = voiceConversationId || wsConversationId;
  const activeConversationId = conversationId || wsConversationId || undefined;

  const feedbackConversationScope = useMemo(() => {
    const ids = new Set<string>();
    if (conversationId) ids.add(conversationId);
    for (const message of wsMessages as WsMessage[]) {
      if (message.conversation_id) ids.add(message.conversation_id);
    }
    for (const message of localMsgs) {
      if (message.conversation_id) ids.add(message.conversation_id);
    }
    return Array.from(ids).sort().join(",");
  }, [conversationId, localMsgs, wsMessages]);

  useEffect(() => {
    const conversationIds = feedbackConversationScope.split(",").filter(Boolean);
    if (!conversationIds.length) return;
    void Promise.all(
      conversationIds.map((id) => hydrateMessageFeedback(id)),
    ).catch(() => {});
  }, [feedbackConversationScope, hydrateMessageFeedback]);

  const appendWorkspaceLifecycleActivity = useCallback(
    (
      body: string,
      phase: "starting" | "pausing" | "completed" | "failed",
      action: WorkspaceLifecycleAction,
      transitionId?: string,
      persistedMessageId?: string,
    ) => {
      const activity = createWorkspaceLifecycleActivityMessage(
        workspaceId,
        wsConversationId || conversationId,
        body,
        phase,
        action,
        persistedMessageId || transitionId,
      );
      setWsMessages((prev) => {
        // A server receipt can arrive through WebSocket before the mutation
        // response. Remove that copy before re-keying the optimistic row so a
        // refetch cannot leave two lifecycle cards for one transition.
        const next = prev.filter(
          (message) => !persistedMessageId || message.id === transitionId || message.id !== persistedMessageId,
        );
        const existingIndex = transitionId
          ? next.findIndex((message) => message.id === transitionId)
          : -1;
        if (existingIndex === -1) return mergeWorkspaceMessages(next, [activity]);

        const previous = next[existingIndex];
        next[existingIndex] = {
          ...previous,
          id: persistedMessageId || previous.id,
          body: activity.body,
          created_at: activity.created_at,
          updated_at: activity.updated_at,
          meta: {
            ...(previous.meta || {}),
            ...activity.meta,
          },
        };
        return next;
      });
    },
    [conversationId, workspaceId, wsConversationId],
  );

  type WorkspaceLifecycleMutationVariables = {
    action: WorkspaceLifecycleAction;
    transitionId: string;
    withGoal?: boolean;
  };
  type WorkspaceLifecycleMutationContext = WorkspaceLifecycleMutationVariables & {
    workspaceId: string;
    timelineKey: string;
  };

  const toggleWorkspaceLifecycle = useMutation({
    mutationFn: async ({
      action,
      transitionId,
    }: WorkspaceLifecycleMutationVariables) => {
      if (action === "pause") return api.workspaces.pause(workspaceId, transitionId);
      return api.workspaces.resume(workspaceId, transitionId);
    },
    onMutate: ({
      action,
      transitionId,
      withGoal,
    }): WorkspaceLifecycleMutationContext => {
      workspaceLifecycleTransitionIdRef.current = transitionId;
      appendWorkspaceLifecycleActivity(
        action === "start"
          ? t(
              withGoal === false
                ? "component.workspace_chat.workspace_runtime_starting_without_goals"
                : "component.workspace_chat.workspace_runtime_starting",
            )
          : t("component.workspace_chat.workspace_runtime_pausing"),
        action === "start" ? "starting" : "pausing",
        action,
        transitionId,
      );
      return {
        action,
        transitionId,
        withGoal,
        workspaceId,
        timelineKey: streamSessionKey,
      };
    },
    onSuccess: (
      result,
      variables,
      mutationContext?: WorkspaceLifecycleMutationContext,
    ) => {
      const context = mutationContext || {
        ...variables,
        workspaceId,
        timelineKey: streamSessionKey,
      };
      const targetWorkspaceId = context.workspaceId;
      const nextStatus = String(
        result?.status || (context.action === "pause" ? "paused" : "active"),
      );
      const nextHeartbeatEnabled = context.action === "start";
      queryClient.setQueryData<Workspace[]>(["workspaces"], (current) =>
        current?.map((item) =>
          item.id === targetWorkspaceId
            ? { ...item, status: nextStatus, heartbeat_enabled: nextHeartbeatEnabled }
            : item,
        ),
      );
      queryClient.setQueryData<Workspace>(["workspace", targetWorkspaceId], (current) =>
        current
          ? { ...current, status: nextStatus, heartbeat_enabled: nextHeartbeatEnabled }
          : current,
      );
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      void queryClient.invalidateQueries({ queryKey: ["workspace", targetWorkspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-heartbeat", targetWorkspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-activity", targetWorkspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-chat", targetWorkspaceId] });
      if (context.action === "start") {
        void queryClient.invalidateQueries({ queryKey: ["workspace-goals", targetWorkspaceId] });
        void queryClient.invalidateQueries({ queryKey: ["workspace-goals-graph", targetWorkspaceId] });
        void queryClient.invalidateQueries({ queryKey: ["goals"] });
      }
      const isCurrentTimeline =
        context.workspaceId === workspaceId && context.timelineKey === streamSessionKey;
      if (isCurrentTimeline) {
        appendWorkspaceLifecycleActivity(
          context.action === "pause"
            ? t("component.workspace_chat.workspace_runtime_paused")
            : t(
                result?.use_goals === false
                  ? "component.workspace_chat.workspace_runtime_started_without_goals"
                  : "component.workspace_chat.workspace_runtime_started",
              ),
          "completed",
          context.action,
          context.transitionId,
          result?.lifecycle_message_id,
        );
      }
      if (workspaceLifecycleTransitionIdRef.current === context.transitionId) {
        workspaceLifecycleTransitionIdRef.current = null;
      }
      if (!isCurrentTimeline) return;
      toast.success(
        context.action === "pause"
          ? t("page.workspace_detail.workspace_paused")
          : t("page.workspace_detail.workspace_resumed"),
      );
    },
    onError: (
      err: Error,
      _variables,
      mutationContext?: WorkspaceLifecycleMutationContext,
    ) => {
      if (!mutationContext) return;
      const isCurrentTimeline =
        mutationContext.workspaceId === workspaceId &&
        mutationContext.timelineKey === streamSessionKey;
      if (isCurrentTimeline) {
        appendWorkspaceLifecycleActivity(
          mutationContext.action === "pause"
            ? t("component.workspace_chat.workspace_runtime_pause_failed")
            : t("component.workspace_chat.workspace_runtime_start_failed"),
          "failed",
          mutationContext.action,
          mutationContext.transitionId,
        );
      }
      if (workspaceLifecycleTransitionIdRef.current === mutationContext.transitionId) {
        workspaceLifecycleTransitionIdRef.current = null;
      }
      if (!isCurrentTimeline) return;
      toast.error(t("page.dashboard.failed"), err.message);
    },
  });
  const workspaceLifecycleActionLabel = toggleWorkspaceLifecycle.isPending
    ? t("component.workspace_chat.updating_workspace_runtime")
    : autonomousRunning
      ? t("component.workspace_chat.pause_workspace_runtime")
      : workspace?.status === "paused"
        ? t("component.workspace_chat.resume_workspace_runtime")
        : t("component.workspace_chat.start_workspace_runtime");
  const startWorkspaceAutonomy = (close: () => void) => {
    const transitionId = `local-workspace-lifecycle-${workspaceId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    toggleWorkspaceLifecycle.mutate({
      action: "start",
      transitionId,
      withGoal: workspaceAutonomyGoalsQuery.isSuccess
        ? workspaceAutonomyGoals.length > 0
        : undefined,
    }, { onSuccess: close });
  };
  const runWorkspaceLifecycleFromPanel = (close: () => void) => {
    if (!autonomousRunning) {
      startWorkspaceAutonomy(close);
      return;
    }
    const transitionId = `local-workspace-lifecycle-${workspaceId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    toggleWorkspaceLifecycle.mutate({
      action: "pause",
      transitionId,
    }, { onSuccess: close });
  };

  const autonomyGoalEditorForm = autonomyGoalEditor ? (
    <form
      className="workspace-chat-autonomy-editor"
      data-existing-goal={Boolean(autonomyGoalEditor.id)}
      aria-label={autonomyGoalEditor.id
        ? t("component.workspace_chat.autonomy_goal_edit_label")
        : t("page.goal_explorer.add_goal")}
      onSubmit={(event) => {
        event.preventDefault();
        if (!autonomyGoalEditor.title.trim() || saveWorkspaceAutonomyGoal.isPending) return;
        saveWorkspaceAutonomyGoal.mutate(autonomyGoalEditor);
      }}
    >
      <Input
        className="workspace-chat-autonomy-editor-input"
        ariaLabel={t("component.workspace_chat.goal_title_label")}
        placeholder={t("component.workspace_chat.autonomy_goal_placeholder")}
        value={autonomyGoalEditor.title}
        onChange={(event) => setAutonomyGoalEditor({
          ...autonomyGoalEditor,
          title: event.target.value,
        })}
        onKeyDown={(event) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          event.stopPropagation();
          closeAutonomyGoalEditor();
        }}
        disabled={saveWorkspaceAutonomyGoal.isPending}
        autoFocus
        required
        maxLength={200}
      />
      {autonomyGoalEditor.id && (
        <button
          type="button"
          className="workspace-chat-autonomy-editor-action workspace-chat-autonomy-editor-action--delete"
          aria-label={t("component.workspace_chat.delete_goal")}
          disabled={saveWorkspaceAutonomyGoal.isPending}
          onClick={() => {
            const persistedGoal = workspaceAutonomyGoals.find(
              (goal) => goal.id === autonomyGoalEditor.id,
            );
            setAutonomyGoalToDelete(persistedGoal || {
              id: autonomyGoalEditor.id as string,
              title: autonomyGoalEditor.title,
            });
          }}
        >
          <IconTrash size={15} />
        </button>
      )}
      <button
        type="submit"
        className="workspace-chat-autonomy-editor-action workspace-chat-autonomy-editor-action--save"
        aria-label={t("component.workspace_chat.save_goal")}
        disabled={!autonomyGoalEditor.title.trim() || saveWorkspaceAutonomyGoal.isPending}
      >
        {saveWorkspaceAutonomyGoal.isPending ? (
          <LoadingSpinner size={14} />
        ) : (
          <IconCheck size={15} />
        )}
      </button>
      <button
        type="button"
        className="workspace-chat-autonomy-editor-action"
        aria-label={t("component.workspace_chat.cancel_goal_edit")}
        disabled={saveWorkspaceAutonomyGoal.isPending}
        onClick={() => closeAutonomyGoalEditor()}
      >
        <IconClose size={15} />
      </button>
    </form>
  ) : null;

  // Resolve typing user ids → display names from messages we already have.
  const memberNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of wsMessages as WsMessage[]) {
      if (m.author_user_id && m.author_user_name) {
        map.set(m.author_user_id, m.author_user_name);
      }
    }
    return map;
  }, [wsMessages]);

  // Live "X is typing…" — other members only.
  const [typingUserIds, setTypingUserIds] = useState<string[]>([]);
  const typingTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const lastTypingSentRef = useRef(0);

  const handleTyping = useCallback(
    (data: Record<string, any>) => {
      const uid = data?.user_id;
      if (!uid || uid === currentUser?.id) return;
      if (!wsConversationId || data?.conversation_id !== wsConversationId) return;
      setTypingUserIds((prev) => (prev.includes(uid) ? prev : [...prev, uid]));
      if (typingTimersRef.current[uid]) clearTimeout(typingTimersRef.current[uid]);
      typingTimersRef.current[uid] = setTimeout(() => {
        setTypingUserIds((prev) => prev.filter((x) => x !== uid));
        delete typingTimersRef.current[uid];
      }, 4000);
    },
    [currentUser?.id, wsConversationId],
  );

  useEffect(
    () => () => {
      Object.values(typingTimersRef.current).forEach(clearTimeout);
    },
    [],
  );

  const typingLabel = useMemo(() => {
    if (typingUserIds.length === 0) return null;
    if (typingUserIds.length === 1) {
      const name = memberNames.get(typingUserIds[0]) || t("page.users.role_member");
      return t("component.workspace_chat.is_typing").replace("{name}", name);
    }
    return t("component.workspace_chat.several_typing");
  }, [typingUserIds, memberNames]);

  // WebSocket: auto-refresh + typing
  const { sendTyping } = useWebSocket({
    onWorkspaceChatMessage: useCallback(
      (data: Record<string, any>) => {
        if (data.workspace_id === workspaceId) {
          if (data.agent_greeting === true && data.message_id) {
            realtimeAgentGreetingIdsRef.current.add(String(data.message_id));
          }
          if (
            isWorkspaceMainChat &&
            data.message_kind === "strategist_activity" &&
            data.message_id &&
            data.strategist_activity &&
            typeof data.strategist_activity === "object"
          ) {
            const messageId = String(data.message_id);
            const activity = data.strategist_activity as Record<string, any>;
            const terminalActivity = isStrategistActivityTerminalState(
              activity.state,
            );
            const loadedActivity = (wsMessages as WsMessage[]).find(
              (message) => message.id === messageId,
            )?.meta?.strategist_activity;
            const reconciliationStopped = Boolean(
              activity.state === "running" &&
                strategistActivityReconciliationStopped(loadedActivity),
            );
            if (!reconciliationStopped) {
              setRealtimeStrategistActivityById((current) => {
                const currentActivity = current.get(messageId);
                if (!canApplyStrategistActivityState(
                  currentActivity?.state,
                  activity.state,
                )) {
                  return current;
                }
                const next = new Map(current);
                next.set(messageId, {
                  ...(currentActivity || {}),
                  ...activity,
                });
                return next;
              });
              if (activity.state === "running") {
                setRealtimeStrategistActivityIds((current) => {
                  if (current.has(messageId)) return current;
                  const next = new Set(current);
                  next.add(messageId);
                  return next;
                });
              }
            }
            if (terminalActivity) {
              strategistActivityReconciliationStateRef.current.delete(messageId);
              // A terminal event is emitted after the database commit. Apply
              // it to the local union as well as the realtime overlay so a
              // message that has aged out of the latest 75-row refetch cannot
              // remain locally "running" forever.
              setWsMessages((current) =>
                applyStrategistActivityTransition(
                  current,
                  messageId,
                  activity,
                ),
              );
            }
          }
          queryClient.invalidateQueries({
            queryKey: ["workspace-chat", workspaceId],
          });
          const changedRunId = workflowRunIdForMessage(
            (wsMessages as WsMessage[]).find((message) => message.id === data.message_id)
              || {
                id: String(data.message_id || ""),
                meta: { workflow_run_id: data.workflow_run_id },
              },
          );
          if (changedRunId) {
            queryClient.invalidateQueries({
              queryKey: ["workflow-run", changedRunId],
            });
          }
        }
      },
      [isWorkspaceMainChat, workspaceId, queryClient, wsMessages],
    ),
    onTyping: handleTyping,
  });

  const completeRealtimeStrategistActivityStream = useCallback(
    (messageId: string) => {
      setRealtimeStrategistActivityIds((current) => {
        if (!current.has(messageId)) return current;
        const next = new Set(current);
        next.delete(messageId);
        return next;
      });
    },
    [],
  );

  const strategistActivityReconciliationIdsKey = useMemo(() => {
    const stoppedMessageIds = new Set(
      (wsMessages as WsMessage[])
        .filter((message) =>
          strategistActivityReconciliationStopped(
            message.meta?.strategist_activity,
          ),
        )
        .map((message) => message.id),
    );
    const ids = new Set<string>(
      Array.from(realtimeStrategistActivityById.keys()).filter(
        (messageId) => !stoppedMessageIds.has(messageId),
      ),
    );
    for (const message of wsMessages as WsMessage[]) {
      if (
        message.meta?.strategist_activity?.state === "running" &&
        !strategistActivityReconciliationStopped(
          message.meta?.strategist_activity,
        )
      ) {
        ids.add(message.id);
      }
    }
    return Array.from(ids).sort().join(",");
  }, [realtimeStrategistActivityById, wsMessages]);

  useEffect(() => {
    if (!isWorkspaceMainChat || !strategistActivityReconciliationIdsKey) return;
    const reconciliationWorkspaceId = workspaceId;
    const messageIds = strategistActivityReconciliationIdsKey.split(",");
    const missingMessageIds = new Set<string>();
    const terminalMessageIds = new Set<string>();
    let cancelled = false;
    let timer: number | null = null;
    const reconciliationAbortController = new AbortController();
    const reconciliationState =
      strategistActivityReconciliationStateRef.current;
    const reconciliationStartedAt = Date.now();
    messageIds.forEach((messageId) => {
      if (!reconciliationState.has(messageId)) {
        reconciliationState.set(messageId, {
          startedAt: reconciliationStartedAt,
          attempt: 0,
        });
      }
    });

    const stopRemainingReconciliation = (remainingMessageIds: string[]) => {
      if (
        cancelled ||
        strategistActivityWorkspaceIdRef.current !== reconciliationWorkspaceId ||
        remainingMessageIds.length === 0
      ) {
        return;
      }
      const stoppedAt = new Date().toISOString();
      const remainingMessageIdSet = new Set(remainingMessageIds);
      setWsMessages((current) => {
        let changed = false;
        const next = current.map((message) => {
          const activity = message.meta?.strategist_activity;
          if (
            !remainingMessageIdSet.has(message.id) ||
            activity?.state !== "running" ||
            strategistActivityReconciliationStopped(activity)
          ) {
            return message;
          }
          changed = true;
          return {
            ...message,
            meta: {
              ...(message.meta || {}),
              strategist_activity: {
                ...activity,
                reconciliation_stopped_at: stoppedAt,
              },
            },
          };
        });
        return changed ? next : current;
      });
      setRealtimeStrategistActivityIds((current) => {
        const next = new Set(current);
        remainingMessageIds.forEach((messageId) => next.delete(messageId));
        return next;
      });
      setRealtimeStrategistActivityById((current) => {
        const next = new Map(current);
        remainingMessageIds.forEach((messageId) => next.delete(messageId));
        return next;
      });
      remainingMessageIds.forEach((messageId) => {
        reconciliationState.delete(messageId);
      });
    };

    const reconcile = async () => {
      if (cancelled || reconciliationAbortController.signal.aborted) return;
      const roundAbortController = new AbortController();
      const abortRound = () => roundAbortController.abort();
      reconciliationAbortController.signal.addEventListener("abort", abortRound, {
        once: true,
      });
      const roundTimeout = window.setTimeout(
        abortRound,
        STRATEGIST_ACTIVITY_RECONCILE_TIMEOUT_MS,
      );
      try {
        await Promise.all(
          messageIds
            .filter((messageId) => !missingMessageIds.has(messageId))
            .map(async (messageId) => {
              try {
                const message = await api.workspaces.chat.getMessage(
                  reconciliationWorkspaceId,
                  messageId,
                  roundAbortController.signal,
                ) as WsMessage;
                if (
                  cancelled ||
                  strategistActivityWorkspaceIdRef.current !== reconciliationWorkspaceId
                ) {
                  return;
                }
                const activity = message.meta?.strategist_activity;
                setWsMessages((current) => {
                  const loaded = current.find((item) => item.id === messageId);
                  if (!canApplyStrategistActivityState(
                    loaded?.meta?.strategist_activity?.state,
                    activity?.state,
                  )) {
                    return current;
                  }
                  return mergeWorkspaceMessages(current, [message]);
                });
                if (isStrategistActivityTerminalState(activity?.state)) {
                  terminalMessageIds.add(messageId);
                  reconciliationState.delete(messageId);
                  setRealtimeStrategistActivityById((current) => {
                    if (!canApplyStrategistActivityState(
                      current.get(messageId)?.state,
                      activity?.state,
                    )) {
                      return current;
                    }
                    const next = new Map(current);
                    next.set(messageId, {
                      ...(current.get(messageId) || {}),
                      ...activity,
                    });
                    return next;
                  });
                }
              } catch (error) {
                if (
                  !cancelled &&
                  strategistActivityWorkspaceIdRef.current === reconciliationWorkspaceId &&
                  error instanceof ApiError &&
                  error.status === 404
                ) {
                  missingMessageIds.add(messageId);
                  reconciliationState.delete(messageId);
                  setWsMessages((current) => {
                    const next = current.filter((message) => message.id !== messageId);
                    return next.length === current.length ? current : next;
                  });
                  setRealtimeStrategistActivityIds((current) => {
                    if (!current.has(messageId)) return current;
                    const next = new Set(current);
                    next.delete(messageId);
                    return next;
                  });
                  setRealtimeStrategistActivityById((current) => {
                    if (!current.has(messageId)) return current;
                    const next = new Map(current);
                    next.delete(messageId);
                    return next;
                  });
                }
                // A transient reconciliation failure must not discard a live
                // streaming row. The next bounded poll retries the durable receipt.
              }
            }),
        );
      } finally {
        window.clearTimeout(roundTimeout);
        reconciliationAbortController.signal.removeEventListener("abort", abortRound);
        if (
          !cancelled &&
          strategistActivityWorkspaceIdRef.current === reconciliationWorkspaceId
        ) {
          const remainingMessageIds = messageIds.filter(
            (messageId) =>
              !missingMessageIds.has(messageId) &&
              !terminalMessageIds.has(messageId),
          );
          const now = Date.now();
          const expiredMessageIds = remainingMessageIds.filter(
            (messageId) =>
              now - (reconciliationState.get(messageId)?.startedAt || now) >=
              STRATEGIST_ACTIVITY_RECONCILE_MAX_MS,
          );
          stopRemainingReconciliation(expiredMessageIds);
          const retryMessageIds = remainingMessageIds.filter(
            (messageId) => !expiredMessageIds.includes(messageId),
          );
          if (retryMessageIds.length > 0) {
            const nextDelay = Math.min(
              ...retryMessageIds.map((messageId) => {
                const state = reconciliationState.get(messageId);
                return Math.min(
                  STRATEGIST_ACTIVITY_RECONCILE_MS *
                    2 ** Math.min(state?.attempt || 0, 4),
                  STRATEGIST_ACTIVITY_RECONCILE_MAX_DELAY_MS,
                );
              }),
            );
            retryMessageIds.forEach((messageId) => {
              const state = reconciliationState.get(messageId);
              if (state) state.attempt += 1;
            });
            timer = window.setTimeout(
              () => void reconcile(),
              nextDelay,
            );
          }
        }
      }
    };

    void reconcile();
    return () => {
      cancelled = true;
      reconciliationAbortController.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    isWorkspaceMainChat,
    strategistActivityReconciliationIdsKey,
    workspaceId,
  ]);

  useEffect(() => {
    const terminalPersistedStrategistActivityIds = new Set(
      (wsMessages as WsMessage[])
        .filter((message) => {
          const state = message.meta?.strategist_activity?.state;
          return isStrategistActivityTerminalState(state);
        })
        .map((message) => message.id),
    );
    if (terminalPersistedStrategistActivityIds.size === 0) return;

    terminalPersistedStrategistActivityIds.forEach((messageId) => {
      strategistActivityReconciliationStateRef.current.delete(messageId);
    });

    setRealtimeStrategistActivityById((current) => {
      let next = current;
      for (const messageId of terminalPersistedStrategistActivityIds) {
        if (
          realtimeStrategistActivityIds.has(messageId) ||
          !current.has(messageId)
        ) {
          continue;
        }
        if (next === current) next = new Map(current);
        next.delete(messageId);
      }
      return next;
    });
  }, [realtimeStrategistActivityIds, wsMessages]);

  const sorted = useMemo(
    () =>
      [...(wsMessages as WsMessage[])].sort((a, b) => {
        const aTime = messageTimestampMs(a.created_at);
        const bTime = messageTimestampMs(b.created_at);
        const timeDelta =
          (Number.isFinite(aTime) ? aTime : 0) -
          (Number.isFinite(bTime) ? bTime : 0);
        if (timeDelta !== 0) return timeDelta;

        const rankDelta = persistedMessageSortRank(a) - persistedMessageSortRank(b);
        if (rankDelta !== 0) return rankDelta;

        return a.id.localeCompare(b.id);
      }),
    [wsMessages],
  );

  useLayoutEffect(() => {
    if (threadRef || hasAgentGreetingPlaybackCompleted(workspaceId)) return;
    const allGreetings = sorted
      .filter(isAgentGreetingMessage)
      .sort((a, b) => agentGreetingSequence(a) - agentGreetingSequence(b));
    const greetings = allGreetings.filter(
      (message) => !hasAgentGreetingMessagePlayed(workspaceId, message.id),
    );
    if (greetings.length === 0) return;

    const hasRealtimeGreeting = greetings.some((message) =>
      realtimeAgentGreetingIdsRef.current.has(message.id),
    );
    if (
      !agentGreetingPlaybackStartedRef.current &&
      !hasRealtimeGreeting &&
      !isFreshWorkspaceForAgentGreetings(workspace?.created_at)
    ) {
      return;
    }
    agentGreetingPlaybackStartedRef.current = true;

    const messageIds = greetings.map((message) => message.id);
    const expectedTotal = allGreetings.reduce((total, message) => {
      const declared = Number(message.meta?.agent_greeting_total);
      return Number.isFinite(declared) ? Math.max(total, declared) : total;
    }, greetings.length);
    setAgentGreetingPlayback((previous) => {
      if (!previous) {
        return {
          messageIds,
          activeIndex: 0,
          visibleCharacters: 0,
          expectedTotal,
        };
      }
      const nextIds = Array.from(new Set([...previous.messageIds, ...messageIds]));
      if (
        nextIds.length === previous.messageIds.length &&
        expectedTotal === previous.expectedTotal
      ) {
        return previous;
      }
      return {
        ...previous,
        messageIds: nextIds,
        expectedTotal: Math.max(previous.expectedTotal, expectedTotal),
      };
    });
  }, [sorted, threadRef, workspace?.created_at, workspaceId]);

  const activeAgentGreetingId = agentGreetingPlayback
    ? agentGreetingPlayback.messageIds[agentGreetingPlayback.activeIndex] || null
    : null;
  const activeAgentGreeting = useMemo(
    () => sorted.find((message) => message.id === activeAgentGreetingId) || null,
    [activeAgentGreetingId, sorted],
  );
  const activeAgentGreetingCharacters = useMemo(
    () => Array.from(String(activeAgentGreeting?.body || "")),
    [activeAgentGreeting?.body],
  );

  useEffect(() => {
    if (!agentGreetingPlayback || !activeAgentGreetingId || !activeAgentGreeting) return;
    const visibleCharacters = agentGreetingPlayback.visibleCharacters;
    const totalCharacters = activeAgentGreetingCharacters.length;
    const reduceMotion = prefersReducedMotion();

    if (visibleCharacters < totalCharacters) {
      const timer = window.setTimeout(() => {
        setAgentGreetingPlayback((current) => {
          if (
            !current ||
            current.messageIds[current.activeIndex] !== activeAgentGreetingId
          ) {
            return current;
          }
          if (reduceMotion) {
            return { ...current, visibleCharacters: totalCharacters };
          }
          const remaining = activeAgentGreetingCharacters
            .slice(current.visibleCharacters)
            .join("");
          const [nextChunk] = nextTypewriterSlice(remaining);
          return {
            ...current,
            visibleCharacters: Math.min(
              totalCharacters,
              current.visibleCharacters + Math.max(1, Array.from(nextChunk).length),
            ),
          };
        });
      }, reduceMotion ? 0 : TYPEWRITER_TICK_MS);
      return () => window.clearTimeout(timer);
    }

    markAgentGreetingMessagePlayed(
      workspaceId,
      activeAgentGreetingId,
      agentGreetingPlayback.expectedTotal,
    );

    const timer = window.setTimeout(() => {
      setAgentGreetingPlayback((current) => {
        if (
          !current ||
          current.messageIds[current.activeIndex] !== activeAgentGreetingId
        ) {
          return current;
        }
        return {
          ...current,
          activeIndex: current.activeIndex + 1,
          visibleCharacters: 0,
        };
      });
    }, reduceMotion ? 0 : AGENT_GREETING_BETWEEN_MESSAGE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [
    activeAgentGreeting,
    activeAgentGreetingCharacters,
    activeAgentGreetingId,
    agentGreetingPlayback,
    workspaceId,
  ]);

  useEffect(() => {
    if (
      !agentGreetingPlayback ||
      agentGreetingPlayback.activeIndex < agentGreetingPlayback.messageIds.length
    ) {
      return;
    }
    setAgentGreetingPlayback(null);
  }, [agentGreetingPlayback]);

  const workflowRunGroups = useMemo(
    () => buildWorkspaceWorkflowRunGroups(sorted),
    [sorted],
  );
  const hostOwnedWorkflowMessageIds = useMemo(
    () => workflowHostOwnedMessageIds(workflowRunGroups),
    [workflowRunGroups],
  );
  const responseSurfaceSubmissionReceipts = useMemo(
    () => collectResponseSurfaceSubmissionReceipts([...sorted, ...localMsgs]),
    [localMsgs, sorted],
  );
  const responseSurfaceSubmissionFailureMessageIds = useMemo(
    () => collectResponseSurfaceSubmissionFailureMessageIds(
      [...sorted, ...localMsgs],
      responseSurfaceSubmissionReceipts,
    ),
    [localMsgs, responseSurfaceSubmissionReceipts, sorted],
  );
  const visibleSortedMessages = useMemo(
    () => sorted.filter((msg) => (
      !hostOwnedWorkflowMessageIds.has(msg.id)
      && !isResponseSurfaceSubmissionMessage(msg, responseSurfaceSubmissionReceipts)
      && !responseSurfaceSubmissionFailureMessageIds.has(msg.id)
    ))
      .filter((msg) =>
        !isRedundantApprovalResolutionReceipt({
          role: msg.author_kind,
          content: msg.body,
          message_kind: msg.message_kind,
          refs: msg.refs,
        }),
      ),
    [
      hostOwnedWorkflowMessageIds,
      responseSurfaceSubmissionFailureMessageIds,
      responseSurfaceSubmissionReceipts,
      sorted,
    ],
  );
  const hiddenAgentGreetingIds = useMemo(
    () => new Set(
      agentGreetingPlayback?.messageIds.slice(agentGreetingPlayback.activeIndex + 1) || [],
    ),
    [agentGreetingPlayback],
  );
  const presentedVisibleSortedMessages = useMemo(
    () => visibleSortedMessages
      .filter((message) => !hiddenAgentGreetingIds.has(message.id))
      .map((message) =>
        message.id === activeAgentGreetingId
          ? {
              ...message,
              body: activeAgentGreetingCharacters
                .slice(0, agentGreetingPlayback?.visibleCharacters || 0)
                .join(""),
            }
          : message,
      ),
    [
      activeAgentGreetingCharacters,
      activeAgentGreetingId,
      agentGreetingPlayback?.visibleCharacters,
      hiddenAgentGreetingIds,
      visibleSortedMessages,
    ],
  );

  const pendingActions = useMemo(
    () => sorted.filter((msg) =>
      isOpenPendingAction(msg) && !hostOwnedWorkflowMessageIds.has(msg.id)
    ),
    [hostOwnedWorkflowMessageIds, sorted],
  );
  const latestPendingAction = pendingActions[pendingActions.length - 1];
  const latestPendingActionId = latestPendingAction?.id;
  const latestPendingActionIsLatest = Boolean(
    latestPendingActionId
    && visibleSortedMessages[visibleSortedMessages.length - 1]?.id === latestPendingActionId,
  );

  // Auto-scroll: initial load jumps to the latest message; streaming follows at a calmer pace.
  useLayoutEffect(() => {
    if (wsMessages.length === 0 && localMsgs.length === 0) return;
    const scrollToBottom = () => {
      const container = chatBodyRef.current;
      if (container) {
        // This surface uses smooth scrolling for user-initiated jumps. Avoid
        // animating through a long history while the workspace first opens.
        const previousScrollBehavior = container.style.scrollBehavior;
        container.style.scrollBehavior = "auto";
        container.scrollTop = container.scrollHeight;
        container.style.scrollBehavior = previousScrollBehavior;
        return;
      }
      bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    };

    if (suppressNextAutoScrollRef.current) {
      suppressNextAutoScrollRef.current = false;
      return;
    }

    if (!didInitialScrollRef.current) {
      didInitialScrollRef.current = true;
      scrollToBottom();
      return;
    }

    // Respect the user's scroll position: only follow new content while they
    // are at the bottom (they resume following by scrolling back down).
    if (!autoFollowRef.current) return;

    if (!streaming && !activeAgentGreetingId) {
      if (!latestPendingActionIsLatest) scrollToBottom();
      return;
    }

    const now = Date.now();
    if (now - lastStreamScrollAtRef.current < 240) return;
    lastStreamScrollAtRef.current = now;
    if (streamScrollFrameRef.current != null) return;
    streamScrollFrameRef.current = window.requestAnimationFrame(() => {
      streamScrollFrameRef.current = null;
      scrollToBottom();
    });
  }, [
    wsMessages.length,
    localMsgs.length,
    streaming,
    activeAgentGreetingId,
    agentGreetingPlayback?.visibleCharacters,
    latestPendingActionIsLatest,
    autoFollowRef,
  ]);

  // Resolve pending action
  const resolvingActionIdsRef = useRef(new Set<string>());
  const [actionResetTokens, setActionResetTokens] = useState<Record<string, number>>({});
  const resolveMutation = useMutation({
    mutationFn: async ({
      msgId,
      choice,
      note,
      payload,
      files,
    }: {
      msgId: string;
      choice: string;
      note?: string;
      payload?: Record<string, any>;
      files?: File[];
    }) => {
      const uploadedAttachments = files?.length
        ? await Promise.all(files.map(async (file) => {
            const document = await api.documents.upload(file);
            if (!document?.id) {
              throw new Error(`Failed to upload ${file.name}`);
            }
            return { name: file.name, id: document.id, type: "knowledge" };
          }))
        : [];
      const existingAttachments = Array.isArray(payload?.attachments)
        ? payload.attachments
        : [];
      const resolvedPayload = uploadedAttachments.length > 0
        ? {
            ...(payload || {}),
            attachments: [...existingAttachments, ...uploadedAttachments],
          }
        : payload;
      return api.workspaces.chat.resolveAction(
        workspaceId,
        msgId,
        choice,
        note,
        resolvedPayload,
      );
    },
    onSuccess: (resolved, { msgId }) => {
      // Close the card locally on the server's own response. The refetch below
      // cannot be relied on for this: a card pinned from outside the page
      // window drops out of the pinned set the instant it is answered, so the
      // next page simply omits it and merge-by-id has nothing to overwrite.
      setWsMessages((prev) =>
        prev.map((msg) =>
          msg.id === msgId
            ? {
                ...msg,
                resolved_at:
                  (resolved as WsMessage | undefined)?.resolved_at ||
                  new Date().toISOString(),
                resolution:
                  (resolved as WsMessage | undefined)?.resolution ?? msg.resolution,
              }
            : msg,
        ),
      );
      queryClient.invalidateQueries({
        queryKey: ["workspace-chat", workspaceId],
      });
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.invalidateQueries({
        queryKey: ["workspace-simulation-run", workspaceId],
      });
      // Task updates are broadcast before the approval transaction commits,
      // so a realtime refetch can still see the old `proposed` status.  The
      // resolve response is returned after commit; refresh once more here.
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-run"] });
      queryClient.invalidateQueries({
        queryKey: ["workspace-workflow-runs", workspaceId],
      });
      window.dispatchEvent(
        new CustomEvent("manor:workspace-actions-refresh", {
          detail: { workspaceId },
        }),
      );
      resolveMutation.reset();
    },
    onError: (error: Error, { msgId }) => {
      // A failed card may retry without unlocking other in-flight approvals.
      setActionResetTokens((previous) => ({
        ...previous,
        [msgId]: (previous[msgId] || 0) + 1,
      }));
      // The shared API client reports other failures, but leaves 403s to the
      // caller so permission failures on background reads do not create noise.
      if (error instanceof ApiError && error.status === 403) {
        toast.error(
          t("lib.api.request_failed"),
          error.code ? t(error.code, error.vars) : error.message,
        );
      }
    },
    onSettled: (_data, _error, { msgId }) => {
      resolvingActionIdsRef.current.delete(msgId);
    },
  });
  const handleResolve = useCallback(
    (
      msgId: string,
      choice: string,
      note?: string,
      payload?: Record<string, any>,
      files?: File[],
    ) => {
      if (resolvingActionIdsRef.current.has(msgId)) return;
      resolvingActionIdsRef.current.add(msgId);
      resolveMutation.mutate({ msgId, choice, note, payload, files });
    },
    [resolveMutation],
  );
  /* ── Tool-call HITL (metadata channel) ──
   *
   * These cards have no `pending_action` row, so `/messages/{id}/resolve`
   * cannot see them — that endpoint keys off `Message.pending_action`.
   * The runtime resolves them the way FloatingChat/EmbeddedChat do: reply
   * into the chat stream with a structured `{hitl_id, action}` body, which
   * `/chat/stream` hands to `resolve_chat_approval_turn` before the model
   * ever sees it. Workspace chat already sends every normal message through
   * that same endpoint (see `handleSend`), so this is the existing path, not
   * a new one. The server persists `resolved` back into the message metadata
   * and saves a readable transcript line in place of the raw JSON.
   */
  const handleHitlAction = useCallback(
    async (hitlId: string, action: string) => {
      if (!hitlId || streamingRef.current) return;
      autoFollowRef.current = true;
      const now = new Date().toISOString();
      const initialMessages: WorkspaceLocalMsg[] = [
        {
          id: `local-user-${Date.now()}`,
          role: "user",
          content: hitlActionTranscriptText(action),
          timestamp: now,
        },
        {
          id: `local-bot-${Date.now()}`,
          role: "assistant",
          content: "",
          agentName: MANOR_AGENT_NAME,
          agentColor: "#1c1917",
          timestamp: now,
        },
      ];
      try {
        await startStream(
          () =>
            api.chat.stream(
              JSON.stringify({ hitl_id: hitlId, action }),
              wsConversationId || undefined,
              { workspaceId, workspaceContext: true, threadRef },
            ),
          wsConversationId || undefined,
          initialMessages,
          () => {},
          streamSessionKey,
        );
      } catch {
        // startStream owns user-visible error state in the shared session.
      }
      await queryClient.invalidateQueries({
        queryKey: ["workspace-chat", workspaceId],
      });
      window.dispatchEvent(
        new CustomEvent("manor:workspace-actions-refresh", {
          detail: { workspaceId },
        }),
      );
    },
    [
      autoFollowRef,
      queryClient,
      startStream,
      streamSessionKey,
      threadRef,
      workspaceId,
      wsConversationId,
    ],
  );
  const handleTaskCompletionFeedback = useCallback(
    async (
      msgId: string,
      feedbackKey: string,
      rating: ChatMessageFeedbackRating,
    ) => {
      try {
        await submitMessageFeedback(feedbackKey, rating, (queuedRating) =>
          api.workspaces.chat.feedback(workspaceId, msgId, queuedRating),
        );
        await Promise.all([
          queryClient.invalidateQueries({
            queryKey: ["workspace-chat", workspaceId],
          }),
          queryClient.invalidateQueries({
            queryKey: ["workspace-runtime-evidence", workspaceId],
          }),
        ]);
      } catch {}
    },
    [queryClient, submitMessageFeedback, workspaceId],
  );
  const handleMessageFeedback = useCallback(
    async (
      messageId: string,
      messageConversationId: string | null | undefined,
      rating: ChatMessageFeedbackRating,
      contentPreview: string,
    ) => {
      const conversationId = messageConversationId || wsConversationId;
      if (!messageId || !conversationId) return;

      try {
        await submitMessageFeedback(messageId, rating, (queuedRating) =>
          api.chat.feedback(conversationId, messageId, {
            rating: queuedRating,
            content_preview: contentPreview.slice(0, 1000),
          }),
        );
      } catch {}
    },
    [submitMessageFeedback, wsConversationId],
  );

  /* ── @mention detection on input change ── */
  function handleInputChange(val: string) {
    setInputDraft(val);
    setMentionAgent((current) =>
      current && !val.includes(`@${current.name}`) ? null : current,
    );
    // Throttle typing pings so other members see "X is typing…" live.
    if (wsConversationId && val.trim()) {
      const now = Date.now();
      if (now - lastTypingSentRef.current > 2500) {
        lastTypingSentRef.current = now;
        sendTyping(wsConversationId);
      }
    }
  }

  useEffect(() => {
    const handleInsertChatPrompt = (event: Event) => {
      const detail =
        (event as CustomEvent<InsertChatComposerDetail>).detail || {};
      const prompt = (detail.prompt || "").trim();
      if (!prompt) return;
      const current = input.trim();
      setInputDraft(current ? `${input.trimEnd()}\n\n${prompt}` : prompt);
      window.setTimeout(() => composerEditorRef.current?.focus(), 0);
    };

    window.addEventListener(
      INSERT_CHAT_COMPOSER_EVENT,
      handleInsertChatPrompt as EventListener,
    );
    return () =>
      window.removeEventListener(
        INSERT_CHAT_COMPOSER_EVENT,
        handleInsertChatPrompt as EventListener,
      );
  }, [input, setInputDraft]);

  function handleMentionSelect(mention: MentionOption) {
    const agent = agentList.find((item) => item.id === mention.id);
    if (!agent) return;
    setMentionAgent(agent);
  }

  function handleMentionRemove() {
    setMentionAgent(null);
  }

  /* ── Resolve inline @mention on send ── */
  function resolveInlineMention(value: string): AgentInfo | null {
    if (mentionAgent) return mentionAgent;
    const val = value;
    const atIdx = val.lastIndexOf("@");
    if (atIdx < 0 || (atIdx > 0 && !/\s/.test(val[atIdx - 1]))) return null;
    const q = val
      .substring(atIdx + 1)
      .trim()
      .toLowerCase();
    if (!q) return null;
    const match =
      agentList.find((a) => a.name.toLowerCase() === q) ||
      agentList.find((a) => a.name.toLowerCase().startsWith(q)) ||
      agentList.find((a) => a.name.toLowerCase().includes(q));
    return match || null;
  }

  const requestChatMode = chatMode === "auto" ? undefined : chatMode;

  const handleChatModeChange = useCallback((mode: ChatBoxMode) => {
    setChatMode(mode);
    setChatModePayload(getDefaultChatModePayload(mode));
  }, []);

  const resetChatModeAfterTurn = useCallback(() => {
    setChatMode("auto");
    setChatModePayload(getDefaultChatModePayload("auto"));
  }, []);

  /* ── Send message with SSE streaming ── */
  const handleSend = useCallback(
    async (
      rawText: string,
      attachments: AttachedItem[],
      manualSkills: ManualSkillItem[] = [],
      workflow?: WorkflowInvokeItem | null,
      sendContext?: ChatComposerSendContext,
      sendOptions: {
        responseSurfaceSubmission?: ResponseSurfaceSubmissionReceipt;
      } = {},
    ) => {
      if (streamingRef.current) return false;
      const isResponseSurfaceSubmission = Boolean(sendOptions.responseSurfaceSubmission);

      // Sending is an explicit jump back to the newest message.
      autoFollowRef.current = true;

      // Resolve @mention if typed inline
      const resolvedAgent = isTaskSession
        ? null
        : isResponseSurfaceSubmission
          ? null
          : resolveInlineMention(rawText);
      const effectiveManualSkills = workflow || isResponseSurfaceSubmission ? [] : manualSkills;
      const requestedManualSkillRefs =
        effectiveManualSkills.length > 0
          ? manualSkillReferences(effectiveManualSkills)
          : undefined;
      let manualSkillRefs = requestedManualSkillRefs;
      if (requestedManualSkillRefs?.some((reference) => reference.kind === "slug")) {
        try {
          const availableSkills = await queryClient.fetchQuery({
            queryKey: ["skills", "chat-manual-skill-resolution", "platform"],
            queryFn: () => api.skills.list({ include_platform: true }),
            staleTime: 60_000,
          });
          manualSkillRefs = resolveManualSkillReferenceIds(
            requestedManualSkillRefs,
            availableSkills,
          );
        } catch (error) {
          toast.error(
            t("lib.api.chat_failed"),
            error instanceof Error ? error.message : undefined,
          );
          return false;
        }
      }
      let text = stripWorkflowInvokeToken(
        stripManualSkillTokens(rawText, manualSkills),
        workflow,
      ).trim();

      // The mention is a routing control, not part of the message body.
      if (resolvedAgent) {
        text = stripWorkspaceAgentMention(text, resolvedAgent.name);
      }
      if (
        !text &&
        attachments.length === 0 &&
        effectiveManualSkills.length === 0 &&
        !workflow
      )
        return false;

      const now = new Date().toISOString();
      // `% Flow` is an explicit invocation and takes precedence over agent and
      // Skill routing when users combine control tokens in one draft.
      const targetAgent = workflow ? null : resolvedAgent;
      const starterBindingId = workflow?.bindingId || "";
      const targetName = isTaskSession
        ? taskSession?.hostName || MANOR_AGENT_NAME
        : targetAgent?.name || MANOR_AGENT_NAME;
      const targetColor = targetName === MANOR_AGENT_NAME
        ? "#1c1917"
        : agentColor(targetName);

      if (!isResponseSurfaceSubmission) {
        setInputDraft("");
        setMentionAgent(null);
      }

      // Display content reflects attached file names
      const displayContent = [
        text,
        attachments.length > 0
          ? `[${t("component.workspace_chat.attached")}: ${attachments.map((f) => f.name).join(", ")}]`
          : "",
        effectiveManualSkills.length > 0
          ? `[${t("component.chat_input_footer.skill")}: ${effectiveManualSkills.map(manualSkillLabel).join(", ")}]`
          : "",
        workflow
          ? `[${t("nav.flows")}: ${workflow.title}]`
          : "",
      ]
        .filter(Boolean)
        .join("\n\n");

      // Optimistic messages live in the shared stream store, so they survive
      // closing/reopening the workspace panel while the request is running.
      const initialMessages: WorkspaceLocalMsg[] = [
        {
          id: `local-user-${Date.now()}`,
          role: "user",
          content: displayContent,
          timestamp: now,
          attachments: attachments.map((attachment) => ({
            name: attachment.name,
            document_id: attachment.id,
            type: attachment.type,
            fileType: attachment.fileType,
            mimeType: attachment.mimeType || attachment.file?.type,
            previewUrl: attachment.previewUrl,
          })),
          meta: sendOptions.responseSurfaceSubmission
            ? responseSurfaceSubmissionMeta(sendOptions.responseSurfaceSubmission)
            : undefined,
        },
        {
          id: `local-bot-${Date.now()}`,
          role: "assistant",
          content: "",
          agentName: targetName,
          agentColor: targetColor,
          timestamp: now,
        },
      ];

      const localFiles = attachments
        .filter((a) => a.type === "file" && a.file)
        .map((a) => a.file!);
      const documentIds = attachments
        .filter((a) => a.type === "knowledge" && a.id)
        .map((a) => a.id!);

      let sendSucceeded = true;
      let responseSurfaceResult: ResponseSurfaceSubmissionResult = {
        status: "succeeded",
        serverAccepted: false,
        terminalObserved: false,
      };
      try {
        await startStream(
          () =>
            starterBindingId
              ? api.workspaces.chat.streamEntrypoint(
                  workspaceId,
                  starterBindingId,
                  text || "Use the attached context to run this workflow.",
                  activeConversationId,
                  {
                    files: localFiles.length > 0 ? localFiles : undefined,
                    documentIds: documentIds.length > 0 ? documentIds : undefined,
                    localWorkerId: sendContext?.localWorkerId,
                    threadRef,
                  },
                )
              : api.chat.stream(
              text ||
                "Use the manually selected skill with the current conversation context.",
              activeConversationId,
              {
                workspaceId,
                workspaceContext: true,
                agentId: targetAgent?.id,
                localWorkerId: sendContext?.localWorkerId,
                threadRef,
                files: localFiles.length > 0 ? localFiles : undefined,
                documentIds: documentIds.length > 0 ? documentIds : undefined,
                manualSkillRefs,
                chatMode: sendOptions.responseSurfaceSubmission
                  ? undefined
                  : requestChatMode,
                chatModePayload: !sendOptions.responseSurfaceSubmission && requestChatMode
                  ? chatModePayload
                  : undefined,
                responseSurfaceSubmission: sendOptions.responseSurfaceSubmission,
              },
            ),
          activeConversationId,
          initialMessages,
          () => {},
          streamSessionKey,
          (status, details) => {
            sendSucceeded = status === ChatStreamCompletionStatus.Succeeded;
            responseSurfaceResult = {
              status: status === ChatStreamCompletionStatus.Succeeded
                ? "succeeded"
                : status === ChatStreamCompletionStatus.Cancelled
                  ? "cancelled"
                  : "failed",
              serverAccepted: details.serverAccepted,
              terminalObserved: details.terminalObserved,
            };
          },
        );
        if (sendSucceeded && !isResponseSurfaceSubmission && requestChatMode) {
          resetChatModeAfterTurn();
        }
      } catch {
        // startStream owns user-visible error state in the shared session.
        sendSucceeded = false;
        responseSurfaceResult = {
          status: "failed",
          serverAccepted: false,
          terminalObserved: false,
        };
      }

      await queryClient.invalidateQueries({
        queryKey: ["workspace-chat", workspaceId],
      });
      // The agent may have written artifacts during this turn (not only when
      // the user attached files), so refresh file/knowledge views every turn.
      await queryClient.invalidateQueries({
        queryKey: ["workspace-documents", workspaceId],
      });
      await invalidateKnowledgeQueries(queryClient);
      return isResponseSurfaceSubmission ? responseSurfaceResult : sendSucceeded;
    },
    [
      activeConversationId,
      localMsgs,
      workspaceId,
      threadRef,
      mentionAgent,
      agentList,
      queryClient,
      setInputDraft,
      startStream,
      streamSessionKey,
      requestChatMode,
      chatModePayload,
      resetChatModeAfterTurn,
      isTaskSession,
      taskSession?.hostName,
      toast,
    ],
  );

  const handleResponseSurfaceSubmit = useCallback(
    async (receipt: ResponseSurfaceSubmissionReceipt) => {
      const rollback = () => {
        setSessionMessages(streamSessionKey, (current) => (
          rollbackResponseSurfaceSubmissionMessages(current, receipt.eventId)
        ));
      };
      try {
        const accepted = await handleSend(
          responseSurfaceSubmissionMessage(receipt),
          [],
          [],
          null,
          undefined,
          { responseSurfaceSubmission: receipt },
        );
        if (typeof accepted === "object") {
          if (!accepted.serverAccepted) {
            rollback();
          } else if (accepted.status !== "succeeded" && accepted.terminalObserved) {
            setSessionMessages(streamSessionKey, (current) => (
              settleResponseSurfaceSubmissionFailure(
                current,
                receipt.eventId,
                accepted.status === "cancelled" ? "interrupted" : "failed",
              )
            ));
          }
        } else if (accepted === false) rollback();
        return accepted;
      } catch (error) {
        rollback();
        throw error;
      }
    },
    [handleSend, setSessionMessages, streamSessionKey],
  );

  const handleStopRequest = useCallback(() => {
    const convId = conversationId || wsConversationId;
    if (convId) {
      void api.chat.cancelPendingFileApprovals(convId, pendingHITLIds(localMsgs)).then(
        () =>
          queryClient.invalidateQueries({
            queryKey: ["workspace-chat", workspaceId],
          }),
        () => undefined,
      );
    }
    void stopStream(streamSessionKey);
  }, [
    conversationId,
    localMsgs,
    queryClient,
    stopStream,
    streamSessionKey,
    workspaceId,
    wsConversationId,
  ]);

  useEffect(() => {
    if (!latestPendingActionId) return;
    if (initialPendingActionIdsRef.current.has(latestPendingActionId)) return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(
        `workspace-chat-message-${latestPendingActionId}`,
      )?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [latestPendingActionId]);

  // The COUNT comes from the server, never from `pendingActions.length`. This
  // list is whatever the client has accumulated, and it drifts above the truth
  // the moment a card is answered: the answered card leaves the pinned set, so
  // no later page hands it back for merge-by-id to overwrite. The sidebar badge
  // re-counts in the DB and drops — which is how one answer produced two
  // numbers. Fall back to the local length only before the first page lands.
  const openActionCount =
    workspaceMessagesPage?.open_action_count ?? pendingActions.length;
  const showPendingActionsBanner =
    autonomousRunning && openActionCount > 0 && pendingActions.length > 0;
  // Jump to the OLDEST open action, not the newest. `sorted` is ascending, so
  // the newest sits nearest the bottom where the reader already is, while the
  // one that has waited longest is buried far above — the only one that
  // genuinely needs a shortcut. (Background plans file their cards in their
  // own thread; those get merged into this view by created_at, so a card that
  // has been blocked for days lands above days of newer chat.)
  const oldestPendingAction = pendingActions[0];
  // How long the longest-blocked item has waited. Silence is the failure mode
  // here — work sat blocked for days behind a badge that only said "13".
  const oldestPendingWaitLabel = useMemo(
    () => (oldestPendingAction ? relativeTime(oldestPendingAction.created_at, "") : ""),
    [oldestPendingAction],
  );
  const jumpToOldestPendingAction = useCallback(() => {
    if (!oldestPendingAction) return;
    const el = document.getElementById(
      `workspace-chat-message-${oldestPendingAction.id}`,
    );
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [oldestPendingAction]);

  const visibleLocalMsgs = useMemo(
    () =>
      localMsgs.filter(
        (msg) =>
          !isResponseSurfaceSubmissionMessage(msg, responseSurfaceSubmissionReceipts)
          && !(typeof msg.id === "string"
            && responseSurfaceSubmissionFailureMessageIds.has(msg.id))
          && !sorted.some((persisted) =>
            msg.role === "user"
              ? isDuplicatePersistedUserMessage(persisted, msg)
              : isDuplicatePersistedAssistantMessage(persisted, msg),
          ),
      ),
    [
      localMsgs,
      responseSurfaceSubmissionFailureMessageIds,
      responseSurfaceSubmissionReceipts,
      sorted,
    ],
  );
  const activeWorkspaceMessage = useMemo(
    () => streaming
      ? [...visibleLocalMsgs].reverse().find((message) => message.role === "assistant") || null
      : null,
    [streaming, visibleLocalMsgs],
  );
  const activeWorkspaceActivity = useMemo(
    () => inferAgentActivity(activeWorkspaceMessage),
    [activeWorkspaceMessage],
  );

  const hasVisibleLocalAssistant = visibleLocalMsgs.some(
    (msg) => msg.role === "assistant",
  );
  const timelinePersistedMessages = useMemo(
    () =>
      hasVisibleLocalAssistant
        ? presentedVisibleSortedMessages.filter((msg) => !isRunningStreamPlaceholder(msg))
        : presentedVisibleSortedMessages,
    [hasVisibleLocalAssistant, presentedVisibleSortedMessages],
  );

  const timelineItems = useMemo(() => {
    const persistedItems = timelinePersistedMessages.map((msg, index) => {
      const time = messageTimestampMs(msg.created_at);
      return {
        kind: "persisted" as const,
        key: `persisted-${msg.id}`,
        msg,
        time: Number.isFinite(time) ? time : 0,
        rank: persistedMessageSortRank(msg),
        order: index,
      };
    });
    const latestPersistedTime = persistedItems.reduce(
      (latest, item) => (item.time > latest ? item.time : latest),
      Number.NEGATIVE_INFINITY,
    );
    const localItems = visibleLocalMsgs.map((msg, index) => {
      const time = messageTimestampMs(msg.timestamp || "");
      const localTime = Number.isFinite(time) ? time : Number.MAX_SAFE_INTEGER;
      const minLocalTime =
        Number.isFinite(latestPersistedTime) && latestPersistedTime > 0
          ? latestPersistedTime + index + 1
          : localTime;
      return {
        kind: "local" as const,
        key: msg.id || `local-${msg.role}-${msg.timestamp || index}`,
        msg,
        localIndex: index,
        time: Math.max(localTime, minLocalTime),
        rank: localMessageSortRank(msg),
        order: visibleSortedMessages.length + index,
      };
    });
    return [...persistedItems, ...localItems].sort((a, b) => {
      const timeDelta = a.time - b.time;
      if (timeDelta !== 0) return timeDelta;
      const rankDelta = a.rank - b.rank;
      if (rankDelta !== 0) return rankDelta;
      return a.order - b.order;
    });
  }, [timelinePersistedMessages, visibleLocalMsgs, visibleSortedMessages.length]);

  // Fold every step/plan status message of the same plan into ONE collapsible
  // activity-run line, regardless of interleaving with other plans or chat.
  // The run is anchored at the plan's first appearance; all later same-plan
  // status messages are absorbed. This is the core de-noising step.
  const renderTimeline = useMemo(() => {
    type RunItem = { kind: "activity-run"; key: string; msgs: WsMessage[] };
    type Folded = (typeof timelineItems)[number] | RunItem;
    const out: Folded[] = [];
    const runByPlan = new Map<string, RunItem>();
    for (const it of timelineItems) {
      if (it.kind === "persisted" && isActivityMessage(it.msg)) {
        const pid = planRefId(it.msg);
        if (pid) {
          const existing = runByPlan.get(pid);
          if (existing) {
            existing.msgs.push(it.msg);
          } else {
            const run: RunItem = {
              kind: "activity-run",
              key: `run-${pid}`,
              msgs: [it.msg],
            };
            runByPlan.set(pid, run);
            out.push(run);
          }
          continue;
        }
      }
      out.push(it);
    }
    return out;
  }, [timelineItems]);

  const activeOutputArtifact = useMemo(() => {
    if (!selectedArtifact || !selectedArtifactAnchor) return selectedArtifact;
    const selectedKey = artifactDedupKey(selectedArtifact);
    for (const item of renderTimeline) {
      if (item.kind === "activity-run") continue;
      const itemAnchor =
        item.kind === "local"
          ? `workspace-chat-message-${item.msg.id || item.key}`
          : `workspace-chat-message-${item.msg.id}`;
      if (itemAnchor !== selectedArtifactAnchor) continue;
      let artifacts: OutputArtifact[] = [];
      if (item.kind === "local") {
        if (item.msg.role === "assistant") {
          artifacts = workspaceFileArtifacts(
            {
              ...item.msg,
              tool_calls: (item.msg.tool_calls || []) as ToolCall[],
            },
            streaming && item.localIndex === visibleLocalMsgs.length - 1,
          );
        }
      } else {
        const isUser =
          item.msg.author_kind === "user" &&
          !isExternalCustomerMessage(item.msg);
        if (!isUser) {
          const visibleTools = parseToolCalls(item.msg.tool_calls) || [];
          const fileReferences = chatMessageReferencesFromAttachments(
            item.msg.attachments,
          );
          artifacts = workspaceFileArtifacts(
            {
              role: "assistant",
              content: String(item.msg.body || ""),
              tool_calls: visibleTools,
              attachments: fileReferences.map((reference) => ({
                name: reference.name,
                document_id: reference.document_id,
                type: reference.kind,
                fileType: reference.fileType,
                mimeType: reference.mimeType,
                previewUrl: reference.previewUrl || reference.url,
                openUrl: reference.openUrl,
                fsPath: reference.fsPath,
              })),
            },
            streaming,
          );
        }
      }
      const current = artifacts.find(
        (artifact) => artifactDedupKey(artifact) === selectedKey,
      );
      if (current) return current;
    }
    return selectedArtifact;
  }, [
    renderTimeline,
    selectedArtifact,
    selectedArtifactAnchor,
    streaming,
    visibleLocalMsgs.length,
  ]);

  const workspaceThreadLoading = workspaceMessagesLoading && timelineItems.length === 0;

  const chatScrollRailMarkers = useMemo<ChatScrollRailMarker[]>(
    () => {
      const sources = renderTimeline.map<ChatScrollRailTurnSource>((item, index) => {
        if (item.kind === "activity-run") {
          const first = item.msgs[0];
          const body = first ? formatUserFacingStructuredText(first.body) : "";
          const preview = workspaceRailPreviewFromText(
            body,
            "Action update",
          );
          return {
            id: item.key,
            sourceIndex: index,
            role: "action",
            tone: "action",
            title: preview.title,
            excerpt: preview.excerpt,
            text: body,
          };
        }
        if (item.kind === "local") {
          const fallbackTitle =
            item.msg.role === "user"
              ? currentUserName || "You"
              : item.msg.agentName || MANOR_AGENT_NAME;
          const body =
            item.msg.role === "user"
              ? item.msg.content
              : formatUserFacingStructuredText(item.msg.content);
          const preview = workspaceRailPreviewFromText(body, fallbackTitle);
          const attachment =
            Array.isArray(item.msg.attachments) && item.msg.attachments.length > 0
              ? item.msg.attachments[0]
              : null;
          const attachmentRecord = attachment as Record<string, unknown> | null;
          const fileLabel =
            String(
              attachmentRecord?.name ||
                attachmentRecord?.filename ||
                attachmentRecord?.title ||
                "",
            ) || "";
          const fileKindSource =
            fileLabel.includes(".") ? fileLabel.split(".").pop() : "file";
          return {
            id: item.key || item.msg.id || `workspace-chat-local-${index}`,
            sourceIndex: index,
            role: item.msg.role,
            tone: item.msg.role === "user" ? "user" : "assistant",
            title: preview.title,
            excerpt: preview.excerpt,
            text: body,
            fileKind: String(fileKindSource).slice(0, 4).toUpperCase(),
            fileLabel,
          };
        }
        const msg = item.msg;
        const hasArtifacts =
          Array.isArray(msg.attachments) && msg.attachments.length > 0;
        const agent = msg.author_subscription_id
          ? subToAgent.get(msg.author_subscription_id)
          : null;
        const fallbackTitle =
          msg.author_kind === "user"
            ? msg.author_user_name || currentUserName || "You"
            : msg.author_kind === "system"
              ? systemSenderName(msg)
              : agent?.name || MANOR_AGENT_NAME;
        const body =
          msg.author_kind === "user"
            ? msg.body || ""
            : formatUserFacingStructuredText(msg.body);
        const preview = workspaceRailPreviewFromText(body, fallbackTitle);
        const attachment =
          Array.isArray(msg.attachments) && msg.attachments.length > 0
            ? msg.attachments[0]
            : null;
        const ref =
          Array.isArray(msg.refs) && msg.refs.length > 0 ? msg.refs[0] : null;
        const fileLabel =
          attachment?.name ||
          attachment?.filename ||
          attachment?.title ||
          ref?.title ||
          ref?.name ||
          "";
        const fileKindSource =
          attachment?.type ||
          ref?.type ||
          (fileLabel.includes(".") ? fileLabel.split(".").pop() : "") ||
          "file";
        const role =
          msg.author_kind === "user"
            ? "user"
            : msg.pending_action
              ? "action"
              : hasArtifacts
                ? "artifact"
                : isActivityMessage(msg)
                  ? "system"
                  : "assistant";
        return {
          id: item.key || msg.id || `workspace-chat-${index}`,
          sourceIndex: index,
          role,
          tone: role,
          title: preview.title,
          excerpt: preview.excerpt,
          text: body,
          fileKind: String(fileKindSource).slice(0, 4).toUpperCase(),
          fileLabel,
        };
      });
      return buildChatScrollRailTurnMarkers(sources);
    },
    [currentUserName, renderTimeline, subToAgent],
  );

  useEffect(() => {
    if (
      streaming ||
      localMsgs.length === 0 ||
      visibleLocalMsgs.length === localMsgs.length
    ) {
      return;
    }
    setSessionMessages(streamSessionKey, visibleLocalMsgs);
  }, [
    streaming,
    localMsgs.length,
    visibleLocalMsgs,
    streamSessionKey,
    setSessionMessages,
  ]);

  const chatSurface = (
    <div className="embedded-chat-root">
      {!isTaskSession && <WorkspaceConnectionNotice workspaceId={workspaceId} />}
      {taskSession && (
        <details
          className="task-session-chat-header"
          aria-label={t("page.task_detail.interactive_session")}
          data-host-available={taskSession.hostAvailable !== false}
        >
          <summary className="task-session-chat-summary">
            <div className="task-session-chat-host">
              {taskSession.hostName === MANOR_AGENT_NAME ? (
                <ManorAvatar size={28} />
              ) : (
                <UserAvatar
                  name={taskSession.hostName}
                  avatarUrl={taskSession.hostAvatarUrl}
                  type="agent"
                  size={28}
                />
              )}
              <div>
                <span className="task-session-chat-eyebrow">
                  {t("page.task_detail.session_host_label")}
                </span>
                <strong className="task-session-chat-host-name">{taskSession.hostName}</strong>
              </div>
            </div>
            {taskSession.objective && (
              <p className="task-session-chat-summary-objective">{taskSession.objective}</p>
            )}
            <span
              className={`task-session-chat-context-trigger ${
                taskSession.hostAvailable === false
                  ? "task-session-chat-context-trigger--unavailable"
                  : ""
              }`}
            >
              {t(taskSession.hostAvailable === false
                ? "page.task_detail.session_host_unavailable"
                : "page.task_detail.session_plan_label")}
              <span className="task-session-chat-context-chevron" aria-hidden="true" />
            </span>
          </summary>
          <div className="task-session-chat-context">
            {taskSession.hostAvailable === false && (
              <p className="task-session-chat-host-error" role="status">
                {t("page.task_detail.session_host_unavailable_copy")}
              </p>
            )}
            {taskSession.objective && (
              <div className="task-session-chat-objective">
                <span className="task-session-chat-eyebrow">
                  {t("page.task_detail.session_goal_label")}
                </span>
                <p>{taskSession.objective}</p>
              </div>
            )}
            {Boolean(taskSession.phases?.length) && (
              <div className="task-session-chat-plan">
                <span className="task-session-chat-eyebrow">
                  {t("page.task_detail.session_plan_label")}
                </span>
                <ol className="task-session-chat-phases">
                  {taskSession.phases?.map((phase, index) => (
                    <li className="task-session-chat-phase" key={`${index}:${phase}`}>
                      <span className="task-session-chat-phase-index" aria-hidden="true">
                        {index + 1}
                      </span>
                      <span>{phase}</span>
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        </details>
      )}
      {/* ── Header ── */}
      {!isTaskSession && (
      <div className="embedded-chat-header embedded-chat-header--workspace">
        <div className="workspace-chat-header-main">
          {(() => {
            if (workspaceCoverUrl) {
              return (
                <img
                  src={workspaceCoverUrl}
                  alt=""
                  className="workspace-chat-header-icon"
                  style={{
                    objectFit: "cover",
                  }}
                />
              );
            }
            if (workspace) {
              return (
                <WorkspaceIconTile
                  workspace={workspace}
                  size={32}
                  iconSize={16}
                  style={{ borderRadius: 10, flexShrink: 0 }}
                />
              );
            }
            const abbr = (workspaceName || "WS")
              .split(/\s+/)
              .map((w) => w[0])
              .join("")
              .slice(0, 2)
              .toUpperCase();
            const colors = [
              "#6d6fb2",
              "#9079c2",
              "#c96a98",
              "#cf9b44",
              "#4f9c84",
              "#5f84bd",
            ];
            const colorIdx =
              (workspaceName || "")
                .split("")
                .reduce((a, c) => a + c.charCodeAt(0), 0) % colors.length;
            return (
              <div
                style={{
                  width: 32,
                  height: 32,
                  borderRadius: 10,
                  flexShrink: 0,
                  background: colors[colorIdx],
                  color: "#fff",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 15,
                  fontWeight: 700,
                }}
              >
                {abbr}
              </div>
            );
          })()}
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <h2
                className="workspace-chat-title"
                style={{
                  fontSize: 15,
                  fontWeight: 600,
                  lineHeight: 1.2,
                  margin: 0,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {workspaceName || t("component.workspace_chat.workspace_chat")}
              </h2>
              <span className="chat-model-badge">
                {agentList.length + 1} {t("component.workspace_chat.members")}
              </span>
            </div>
            {streaming ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 2,
                }}
              >
                <AgentActivityOrb activity={activeWorkspaceActivity} />
              </div>
            ) : (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  marginTop: 2,
                }}
              >
                <span className="workspace-chat-subtitle">
                  {t("component.workspace_chat.type_to_mention_an_agent")}</span>
              </div>
            )}
          </div>
        </div>
        {!threadRef && (
          <div className="workspace-chat-header-actions">
            {canToggleWorkspace && !showSimulationRuntime && (
              <AnchoredPopover
                align="right"
                width={360}
                openOnHover
                onOpenChange={(open) => {
                  if (open && workspaceAutonomyGoalsQuery.data !== undefined) {
                    void workspaceAutonomyGoalsQuery.refetch();
                  }
                }}
                ariaLabel={workspaceLifecycleActionLabel}
                panelClassName="workspace-chat-autonomy-popover"
                trigger={(
                  <button
                    type="button"
                    ref={workspaceLifecycleTriggerRef}
                    className="btn-manor-ghost workspace-chat-lifecycle-trigger"
                    disabled={toggleWorkspaceLifecycle.isPending}
                    aria-label={workspaceLifecycleActionLabel}
                  >
                    {toggleWorkspaceLifecycle.isPending ? (
                      <LoadingSpinner size={16} />
                    ) : (
                      <span
                        className="workspace-chat-lifecycle-icon"
                        data-state={autonomousRunning ? "active" : "paused"}
                        aria-hidden="true"
                      >
                        {autonomousRunning ? <IconPause size={17} /> : <IconPlay size={17} />}
                      </span>
                    )}
                  </button>
                )}
              >
                  {({ close }) => (
                    <div className="workspace-chat-autonomy-panel">
                      <div className="workspace-chat-autonomy-header">
                        <strong>{t("component.workspace_chat.workspace_goals_title")}</strong>
                        <button
                          type="button"
                          ref={autonomyGoalAddButtonRef}
                          className="workspace-chat-autonomy-header-add"
                          aria-label={t("page.goal_explorer.add_goal")}
                          title={t("page.goal_explorer.add_goal")}
                          disabled={
                            workspaceAutonomyGoalsQuery.isLoading
                            || workspaceAutonomyGoalsQuery.isError
                            || Boolean(autonomyGoalEditor)
                            || saveWorkspaceAutonomyGoal.isPending
                          }
                          onClick={() => {
                            autonomyGoalEditorOriginRef.current = { kind: "add" };
                            setAutonomyGoalEditor({ id: null, title: "" });
                          }}
                        >
                          <IconPlus size={17} />
                        </button>
                      </div>

                      <div className="workspace-chat-autonomy-body">
                        {workspaceAutonomyGoalsQuery.isLoading ? (
                          <div className="workspace-chat-autonomy-state" aria-live="polite">
                            <LoadingSpinner size={18} />
                            <span>{t("page.workspace_detail.loading_goals")}</span>
                          </div>
                        ) : workspaceAutonomyGoalsQuery.isError ? (
                          <div className="workspace-chat-autonomy-state workspace-chat-autonomy-state--error" role="alert">
                            <strong>{t("component.workspace_chat.workspace_goals_load_failed")}</strong>
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => void workspaceAutonomyGoalsQuery.refetch()}
                            >
                              <IconRefresh size={14} />
                              {t("page.workspace_stats.try_again")}
                            </Button>
                          </div>
                        ) : (
                          <>
                            {autonomyGoalEditor?.id === null && autonomyGoalEditorForm}
                            {workspaceAutonomyGoals.length === 0 && !autonomyGoalEditor && (
                              <div className="workspace-chat-autonomy-state workspace-chat-autonomy-state--empty">
                                <strong>{t("page.workspace_detail.no_goals_yet")}</strong>
                              </div>
                            )}
                            {workspaceAutonomyGoals.length > 0 && (
                              <div className="workspace-chat-autonomy-list" role="list">
                                {workspaceAutonomyGoals.map((goal) => (
                                  autonomyGoalEditor?.id === goal.id ? (
                                    <div role="listitem" key={goal.id}>
                                      {autonomyGoalEditorForm}
                                    </div>
                                  ) : (
                                    <div className="workspace-chat-autonomy-row" role="listitem" key={goal.id}>
                                      <strong title={goal.title || undefined}>
                                        {goal.title || t("component.session_switcher.untitled")}
                                      </strong>
                                      <button
                                        type="button"
                                        ref={(node) => {
                                          if (node) autonomyGoalEditButtonRefs.current.set(goal.id, node);
                                          else autonomyGoalEditButtonRefs.current.delete(goal.id);
                                        }}
                                        className="workspace-chat-autonomy-row-action"
                                        aria-label={t("component.workspace_chat.edit_goal_named").replace(
                                          "{name}",
                                          goal.title || t("component.session_switcher.untitled"),
                                        )}
                                        disabled={Boolean(autonomyGoalEditor) || saveWorkspaceAutonomyGoal.isPending}
                                        onClick={() => {
                                          autonomyGoalEditorOriginRef.current = {
                                            kind: "edit",
                                            goalId: goal.id,
                                          };
                                          setAutonomyGoalEditor({
                                            id: goal.id,
                                            title: goal.title || "",
                                          });
                                        }}
                                      >
                                        <IconEdit size={15} />
                                      </button>
                                    </div>
                                  )
                                ))}
                              </div>
                            )}
                          </>
                        )}
                      </div>

                      <div className="workspace-chat-autonomy-footer">
                        <Button
                          variant="outline"
                          size="sm"
                          className="workspace-chat-autonomy-start"
                          ariaLabel={workspaceLifecycleActionLabel}
                          disabled={
                            Boolean(autonomyGoalEditor)
                            || saveWorkspaceAutonomyGoal.isPending
                            || toggleWorkspaceLifecycle.isPending
                          }
                          loading={toggleWorkspaceLifecycle.isPending}
                          onClick={() => runWorkspaceLifecycleFromPanel(close)}
                        >
                          {!toggleWorkspaceLifecycle.isPending && (
                            autonomousRunning ? <IconPause size={14} /> : <IconPlay size={14} />
                          )}
                          {workspaceLifecycleActionLabel}
                        </Button>
                      </div>
                    </div>
                  )}
              </AnchoredPopover>
            )}
            <WorkspaceStatsQuickAccess
              workspaceId={workspaceId}
              workspaceName={workspaceName}
            />
          </div>
        )}
      </div>
      )}

      {/* ── Agent chips (display only — DM via @mention) ── */}
      {!isTaskSession && (workspaceAgentsLoading ? (
        <div className="embedded-agents-row" aria-hidden="true">
          <SkeletonLine width={112} height={24} radius={999} />
          <SkeletonLine width={92} height={24} radius={999} />
          <SkeletonLine width={84} height={24} radius={999} />
        </div>
      ) : agentList.length > 0 && (
        <div className="embedded-agents-row">
          <span className="agent-chip agent-chip--selected">
            <ManorAvatar size={14} />
            {MANOR_AGENT_NAME}
          </span>
          {(agentsExpanded ? agentList : agentList.slice(0, 5)).map((agent) => (
            <button
              key={agent.id}
              type="button"
              className="agent-chip"
              onClick={() => openAgentQuickView(agent)}
            >
              <UserAvatar
                name={agent.name}
                avatarUrl={agent.avatar_url}
                type="agent"
                seed={agentAvatarSeed(agent)}
                size={14}
              />
              {agent.name}
            </button>
          ))}
          {agentList.length > 5 && (
            <button
              type="button"
              className="agent-chip agent-chip--more"
              onClick={() => setAgentsExpanded((v) => !v)}
            >
              {agentsExpanded
                ? t("component.workspace_chat.show_less")
                : `+${agentList.length - 5}`}
            </button>
          )}
        </div>
      ))}

      {!isTaskSession && (
        <WorkspaceSimulationRuntimeBar
          runtime={simulationRuntime}
          onPromoteToLive={canToggleWorkspace ? promoteSimulationToLive : undefined}
        />
      )}


      {/* ── Messages ── */}
      <div className="embedded-chat-body-wrap workspace-chat-body-wrap chat-scroll-rail-host">
        <div
          ref={chatBodyRef}
          onScroll={handleAutoFollowScroll}
          className={`embedded-chat-body ${
            timelineItems.length === 0 && !showSimulationRuntime
              ? "embedded-chat-body--empty workspace-chat-body--empty"
              : ""
          }`}
        >
        {showPendingActionsBanner && (
          <div className="workspace-pending-actions-banner">
            <div>
              <div className="workspace-pending-actions-title">
                {pendingActionsLabel(openActionCount)}
              </div>
              <div className="workspace-pending-actions-copy">
                {/* Lead with what has waited longest — the oldest is what the
                    jump targets, and how long it has been stuck is the part
                    worth reacting to. */}
                {pendingActions
                  .slice(0, 3)
                  .map((msg) => pendingActionLabel(msg))
                  .join(" · ")}
                {oldestPendingWaitLabel ? ` · ${oldestPendingWaitLabel}` : ""}
              </div>
            </div>
            <Button
              variant="outline"
              size="sm"
              className="workspace-pending-actions-jump"
              onClick={jumpToOldestPendingAction}
            >
              {t("component.workspace_chat.jump_to_oldest_action")}
            </Button>
          </div>
        )}
        {workspaceThreadLoading ? (
          <ChatMessagesSkeleton rows={5} />
        ) : timelineItems.length === 0 && !showSimulationRuntime && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
            }}
          >
            <div style={{ textAlign: "center" }}>
              <div
                style={{
                  width: 64,
                  height: 64,
                  borderRadius: 16,
                  background: "linear-gradient(135deg, #f2f6f5, #e5eeeb)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  margin: "0 auto 16px",
                }}
              >
                <IconChatBubble size={32} style={{ color: "#4f7d75" }} />
              </div>
              <p className="workspace-chat-empty-title">
                {t(isTaskSession
                  ? taskSession?.hostAvailable === false
                    ? "page.task_detail.session_host_unavailable"
                    : "page.task_detail.session_ready_title"
                  : "component.workspace_chat.workspace_group_chat")}
              </p>
              <p className="workspace-chat-empty-copy">
                {isTaskSession
                  ? taskSession?.hostAvailable === false
                    ? t("page.task_detail.session_host_unavailable_copy")
                    : t("page.task_detail.session_ready_copy", {
                        agent: taskSession?.hostName || MANOR_AGENT_NAME,
                      })
                  : `${t("component.workspace_chat.talk_to")}${MANOR_AGENT_NAME} ${t("component.workspace_chat.or_mention_a_specific_agent")}`}
              </p>
            </div>
          </div>
        )}

        {timelineItems.length > 0 && workspaceHistoryState.hasMore && (
          <div className="chat-history-load-more">
            <button
              type="button"
              className="chat-history-load-more-button"
              disabled={loadingOlderMessages}
              onClick={handleLoadOlderMessages}
            >
              {loadingOlderMessages && <LoadingSpinner size={13} />}
              {t("page.chat_history.load_earlier_messages")}
            </button>
          </div>
        )}

        {renderTimeline.map((item) => {
          if (item.kind === "activity-run") {
            return (
              <WsActivityRun
                key={item.key}
                markerId={item.key}
                msgs={item.msgs}
                subToAgent={subToAgent}
              />
            );
          }
          if (item.kind === "persisted") {
            if (isActivityMessage(item.msg)) {
              const persistedStrategistActivity =
                item.msg.meta?.strategist_activity;
              const realtimeStrategistActivity =
                realtimeStrategistActivityById.get(item.msg.id);
              const persistedStrategistState =
                persistedStrategistActivity?.state;
              const persistedStrategistActivityIsTerminal =
                isStrategistActivityTerminalState(persistedStrategistState);
              const strategistActivity =
                realtimeStrategistActivity &&
                !persistedStrategistActivityIsTerminal
                ? {
                    ...(persistedStrategistActivity || {}),
                    ...realtimeStrategistActivity,
                  }
                : persistedStrategistActivity;
              const strategistActivityReconciliationIsStopped =
                strategistActivityReconciliationStopped(strategistActivity);
              return (
                <WsActivityLine
                  key={item.key}
                  markerId={item.key}
                  msg={item.msg}
                  strategistActivity={strategistActivity}
                  announceFailure={realtimeStrategistActivity?.state === "failed"}
                  processing={
                    strategistActivity?.state === "running" &&
                    !strategistActivityReconciliationIsStopped &&
                    (Boolean(realtimeStrategistActivity) ||
                      typeof strategistActivity?.started_at === "string")
                  }
                  stream={
                    item.msg.meta?.workspace_lifecycle !== true &&
                    Boolean(item.msg.meta?.strategist_activity) &&
                    !strategistActivityReconciliationIsStopped &&
                    realtimeStrategistActivityIds.has(item.msg.id)
                  }
                  onStreamComplete={completeRealtimeStrategistActivityStream}
                />
              );
            }
            const completionFeedbackKey = completionFeedbackSubjectKey(item.msg);
            return (
              <WsMessageRow
                key={item.key}
                markerId={item.key}
                msg={item.msg}
                subToAgent={subToAgent}
                currentUserName={currentUserName}
                currentUserAvatar={currentUserAvatar}
                currentUserId={currentUser?.id || null}
                onResolve={handleResolve}
                onHitlAction={handleHitlAction}
                streaming={streaming}
                workspacePaused={workspace?.status === "paused"}
                presentingAgentGreeting={item.msg.id === activeAgentGreetingId}
                actionResetToken={actionResetTokens[item.msg.id] || 0}
                onFeedback={handleTaskCompletionFeedback}
                messageFeedbackValue={
                  messageFeedback[completionFeedbackKey || item.msg.id] || null
                }
                onMessageFeedback={handleMessageFeedback}
                onArtifactOpen={openWorkspaceArtifact}
                suppressedTaskReferenceId={isTaskSession ? threadRef?.id : undefined}
                onResponseSurfaceSubmit={handleResponseSurfaceSubmit}
                responseSurfaceSubmissionReceipts={responseSurfaceSubmissionReceipts}
                onConfigureLedgers={
                  canToggleWorkspace && !threadRef
                    ? openLedgerConfiguration
                    : undefined
                }
              />
            );
          }

          const msg = item.msg;
          const delegatedRuns =
            msg.role === "assistant" ? msg.sub_agent_events || [] : [];
          const localTools =
            msg.role === "assistant"
              ? ((msg.tool_calls || []) as ToolCall[])
              : [];
          const localCodingNotice =
            msg.role === "assistant" ? maybeLocalCodingRunNoticeForTools(localTools) : null;
          const bubbleContent = localCodingNotice || msg.content;
          const isStreamingAssistant =
            streaming &&
            item.localIndex === visibleLocalMsgs.length - 1 &&
            msg.role === "assistant";
          const renderedBubbleDisplay = parseUserMessageDisplay(
            { ...msg, content: bubbleContent },
            { streaming: isStreamingAssistant },
          );
          const renderedBubbleContent = renderedBubbleDisplay.cleanContent;
          const messageDisplay = localCodingNotice
            ? parseUserMessageDisplay(msg, {
                renderedContent: renderedBubbleContent,
                streaming: isStreamingAssistant,
              })
            : renderedBubbleDisplay;
          const retryableLocalAssistant = isRetryableAssistantMessage(
            msg,
            renderedBubbleContent,
          );
          const localAssistantBlocks =
            msg.role === "assistant" &&
            !retryableLocalAssistant &&
            Array.isArray(msg.assistant_blocks) &&
            msg.assistant_blocks.length > 0;
          const actionCopyText = msg.role === "user"
            ? String(bubbleContent || "")
            : formatUserFacingStructuredText(
              chatMessageActionText(msg, bubbleContent),
            );
          const visibleMessageFileReferences = messageDisplay.references;
          const messageArtifacts =
            msg.role === "assistant"
              ? filterMessageArtifactsAlreadyRepresented(
                  { ...msg, tool_calls: localTools },
                  workspaceFileArtifacts(
                    { ...msg, tool_calls: localTools },
                    isStreamingAssistant,
                  ),
                  true,
                  {
                    renderedContent: renderedBubbleContent,
                    streaming: isStreamingAssistant,
                  },
                )
              : [];
          const canRateLocalMessage = Boolean(
            msg.role === "assistant" &&
              msg.id &&
              (msg.conversation_id || wsConversationId),
          );
          const showLocalMessageActions = Boolean(
            actionCopyText.trim() || canRateLocalMessage,
          );
          const showLocalMessageMeta = Boolean(
            !isStreamingAssistant ||
              renderedBubbleContent ||
              localTools.length > 0 ||
              visibleMessageFileReferences.length > 0 ||
              messageArtifacts.length > 0,
          );
          const messageAnchorId = `workspace-chat-message-${msg.id || item.key}`;
          const messageReturnTo = `${location.pathname}${location.search}#${messageAnchorId}`;
          return (
            <div
              key={item.key}
              id={messageAnchorId}
              data-chat-scroll-marker-id={item.key}
              className={`chat-message-row chat-message-shell ${msg.role === "user" ? "chat-message-row--user" : ""}`}
            >
              {msg.role === "user" ? (
                <UserAvatar
                  name={currentUserName}
                  avatarUrl={currentUserAvatar}
                  type="user"
                  size={32}
                />
              ) : msg.agentName === MANOR_AGENT_NAME || !msg.agentName ? (
                <ManorAvatar size={32} />
              ) : (
                <UserAvatar name={msg.agentName} type="agent" seed={msg.agentName} size={32} />
              )}
              <div
                className={`chat-message-col ${msg.role === "user" ? "chat-message-col--user" : ""}`}
              >
                <span
                  className={`chat-sender-name ${msg.role === "user" ? "chat-sender-name--user" : "chat-sender-name--agent"}`}
                  style={
                    msg.role !== "user" && msg.agentColor
                      ? ({ "--chat-agent-name-color": msg.agentColor } as CSSProperties)
                      : undefined
                  }
                >
                  {msg.role === "user"
                    ? t("page.chat_history.you")
                    : msg.agentName || MANOR_AGENT_NAME}
                </span>
                <div
                  className={`chat-bubble ${msg.role === "user" ? "chat-bubble--user" : "chat-bubble--bot"} ${
                    msg.role === "assistant" && isStreamingAssistant && !renderedBubbleContent
                      ? "chat-bubble--activity"
                      : ""
                  }`}
                >
                  {!localAssistantBlocks && localTools.length > 0 && (
                    <ToolCallList
                      tools={localTools}
                      keyPrefix={item.key}
                      subAgentRuns={delegatedRuns}
                    />
                  )}
                  {isStreamingAssistant && runtimeQueueStatus && (
                    <div
                      className="chat-runtime-queue-status"
                      role="status"
                      aria-live="polite"
                    >
                      <span
                        className="chat-runtime-queue-status-dot"
                        aria-hidden="true"
                      />
                      <span>{runtimeQueueStatus}</span>
                    </div>
                  )}
                  {localAssistantBlocks ? (
                    <AssistantMessageBlocks
                      blocks={msg.assistant_blocks}
                      content={renderedBubbleContent}
                      keyPrefix={item.key}
                      streaming={isStreamingAssistant}
                      minimal
                      collapseLongFinal
                      subAgentRuns={delegatedRuns}
                      onResponseSurfaceSubmit={handleResponseSurfaceSubmit}
                      sourceMessageId={msg.id || ""}
                      pendingActionKind={assistantPendingActionKindForMessage(msg)}
                      responseSurfaceSubmissionReceipts={responseSurfaceSubmissionReceipts}
                      onConfigureWorkspaceLedgers={
                        canToggleWorkspace && !threadRef
                          ? openLedgerConfiguration
                          : undefined
                      }
                    />
                  ) : renderedBubbleContent ? (
                    <ExpandableWorkspaceMarkdown
                      content={msg.role === "user" ? renderedBubbleContent : formatUserFacingStructuredText(renderedBubbleContent)}
                      isUser={msg.role === "user"}
                      collapsible
                      streaming={isStreamingAssistant}
                    />
                  ) : isStreamingAssistant && !runtimeQueueStatus ? (
                    <AgentActivityOrb
                      activity={inferAgentActivity(msg)}
                      className="agent-activity-orb--message"
                    />
                  ) : null}
                  <ChatMessageReferenceStrip
                    references={visibleMessageFileReferences}
                    align={msg.role === "user" ? "right" : "left"}
                    inlineFileCards
                    returnTo={messageReturnTo}
                  />
                </div>
                {msg.role === "assistant" && (
                  <ArtifactSummaryCards
                    artifacts={messageArtifacts}
                    onOpen={(artifact) =>
                      openWorkspaceArtifact(artifact, messageAnchorId)
                    }
                  />
                )}
                {showLocalMessageMeta && (
                  <div
                    className={`chat-message-meta-row ${
                      msg.role === "user" ? "chat-message-meta-row--user" : ""
                    } ${showLocalMessageActions ? "chat-message-meta-row--actions" : ""}`}
                  >
                    <ChatTimestamp timestamp={msg.timestamp} />
                    {showLocalMessageActions && (
                      <span className="chat-message-meta-actions">
                        <ChatMessageActions
                          align="right"
                          copyText={actionCopyText}
                          speechText={msg.role === "assistant" ? actionCopyText : undefined}
                          voiceScope={{ workspaceId, conversationId: msg.conversation_id || wsConversationId }}
                          copyLabel={t(
                            msg.role === "user"
                              ? "component.chat_message_actions.copy_request"
                              : "component.chat_message_actions.copy_response",
                          )}
                          feedbackValue={
                            msg.id ? messageFeedback[msg.id] || null : null
                          }
                          disabled={streaming}
                          onFeedback={
                            canRateLocalMessage
                              ? (rating) =>
                                  void handleMessageFeedback(
                                    msg.id || "",
                                    msg.conversation_id || wsConversationId,
                                    rating,
                                    actionCopyText,
                                  )
                              : undefined
                          }
                        />
                      </span>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}

        <div ref={bottomRef} />
        </div>
        <ChatScrollRail
          containerRef={chatBodyRef}
          markers={chatScrollRailMarkers}
        />
      </div>

      {/* Live typing indicator for other members. */}
      {!isTaskSession && typingLabel && (
        <div className="workspace-chat-typing">
          <span className="chat-typing-dots">
            <span />
            <span />
            <span />
          </span>
          <span>{typingLabel}</span>
        </div>
      )}

      {/* Persistent feature tip — visible even in active chats so users keep
          discovering what the workspace can do. */}
      {!isTaskSession && (
        <div className="chat-tip-bar">
          <InlineTips
            surface="workspace_chat"
            context={{ hasAgents: agentList.length > 0 }}
            placement="composer"
          />
        </div>
      )}

      {!isTaskSession && (
        <WorkspaceWorkflowRunHost
          workspaceId={workspaceId}
          workspacePaused={workspace?.status === "paused"}
          groups={workflowRunGroups}
          onResolveMessage={handleResolve}
          resolveLoading={resolveMutation.isPending}
          resolveError={resolveMutation.error}
          resolveMessageId={resolveMutation.variables?.msgId || null}
          onRunChange={resolveMutation.reset}
        />
      )}

      {/* ── Footer / Input (shared composer with attach + voice + #) ── */}
      <ChatInputFooter
        voiceScope={{ workspaceId, conversationId: voiceScopeConversationId, threadRefKind: threadRef?.kind, threadRefId: threadRef?.id }}
        onVoiceConversation={(id) => {
          setVoiceConversationId(id);
          void queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] });
        }}
        value={input}
        onChange={handleInputChange}
        enterToSend
        streaming={streaming}
        disabled={composerDisabled || taskSession?.hostAvailable === false}
        onSend={(text, attachments, manualSkills, context) => {
          void handleSend(text, attachments, manualSkills, null, context);
        }}
        onSendWorkflow={(text, attachments, manualSkills, workflow, context) => {
          void handleSend(text, attachments, manualSkills, workflow, context);
        }}
        onStop={handleStopRequest}
        topSlot={
          !isTaskSession && timelineItems.length > 0 ? (
              <ChatModeTemplateGallery
                mode={chatMode}
                disabled={streaming || composerDisabled}
                samples={chatModeTemplateSamples(chatMode)}
                onSelect={async (sample) => {
                  try {
                    const remix = await prepareTemplateRemix(sample);
                    setInputDraft(remix.prompt);
                    setComposerSeed(
                      remix.attachments.length > 0
                        ? {
                            key: `artifact-template-${remix.attachments[0]?.id || Date.now()}-${Date.now()}`,
                            attachments: remix.attachments,
                          }
                        : null,
                    );
                    toast.success(
                      t("component.embedded_chat.template_ready").replace(
                        "{name}", sample.title,
                      ),
                    );
                    window.setTimeout(() => composerEditorRef.current?.focus(), 0);
                  } catch (error) {
                    toast.error(
                      t("component.embedded_chat.template_create_failed"),
                      error instanceof Error ? error.message : undefined,
                    );
                  }
                }}
            />
          ) : undefined
        }
        modeSlot={
          !isTaskSession ? (
            <ChatModeToolbar
              mode={chatMode}
              payload={chatModePayload}
              onModeChange={handleChatModeChange}
              onPayloadChange={setChatModePayload}
              disabled={streaming || composerDisabled}
            />
          ) : undefined
        }
        replaceActionButtons={isTaskSession || chatMode !== "auto"}
        placeholder={
          isTaskSession
            ? taskSession?.hostAvailable === false
              ? t("page.task_detail.session_host_unavailable_copy")
              : t("page.task_detail.session_reply_placeholder", {
                  agent: taskSession?.hostName || MANOR_AGENT_NAME,
                })
            : composerDisabled
              ? t("page.workspace_detail.workspace_paused")
              : mentionAgent
                ? `Message ${mentionAgent.name}... / skill, % flow`
                : requestChatMode
                  ? getChatModeInputPlaceholder(chatMode, chatModePayload)
                  : `Message ${MANOR_AGENT_NAME}... @ mention, # attach, / skill, % flow`
        }
        mentions={isTaskSession ? [] : mentionOptions}
        workflows={isTaskSession ? [] : workflowInvokeOptions}
        selectedMentions={isTaskSession ? [] : selectedMentions}
        onMentionSelect={handleMentionSelect}
        onMentionRemove={handleMentionRemove}
        editorRef={composerEditorRef}
        seedAttachments={composerSeed?.attachments}
        seedAttachmentsKey={composerSeed?.key}
      />
    </div>
  );

  return (
    <div
      className={`embedded-chat-workbench workspace-chat-workbench ${
        isTaskSession ? "workspace-chat-workbench--task-session" : ""
      } ${
        outputOpen ? "embedded-chat-workbench--output-open" : ""
      }`}
    >
      <ResizablePaneGroup
        panes={[
          {
            id: "chat",
            label: workspaceName || t("component.workspace_chat.workspace_chat"),
            initialSize: 2,
            minSize: outputOpen ? 420 : undefined,
            className: "embedded-chat-pane workspace-chat-pane",
            children: chatSurface,
          },
          ...(outputOpen
            ? [
                {
                  id: "output",
                  label: t("component.embedded_chat.output_panel"),
                  initialSize: 1,
                  minSize: 360,
                  className: "embedded-chat-pane embedded-chat-pane--output",
                  children: (
                    <OutputPanel
                      artifact={activeOutputArtifact}
                      updating={streaming}
                      onStop={closeWorkspaceArtifact}
                      returnTo={selectedArtifactReturnTo}
                    />
                  ),
                },
              ]
            : []),
        ]}
        storageKey="workspace-chat-output-panes"
        className="embedded-chat-output-panes workspace-chat-output-panes"
      />
      <WorkspaceLedgerConfigurationDialog
        open={Boolean(ledgerConfiguration)}
        workspaceId={workspaceId}
        initialContractIds={ledgerConfiguration?.initialContractIds || []}
        onClose={() => setLedgerConfiguration(null)}
        onConfigured={handleLedgersConfigured}
      />
      <ConfirmDialog
        open={Boolean(autonomyGoalToDelete)}
        onClose={() => {
          if (deleteWorkspaceAutonomyGoal.isPending) return;
          deleteWorkspaceAutonomyGoal.reset();
          setAutonomyGoalToDelete(null);
        }}
        onConfirm={() => {
          if (autonomyGoalToDelete) {
            deleteWorkspaceAutonomyGoal.mutate(autonomyGoalToDelete);
          }
        }}
        title={t("component.workspace_chat.delete_goal")}
        message={t("component.workspace_chat.delete_goal_confirmation").replace(
          "{name}",
          autonomyGoalToDelete?.title || t("component.session_switcher.untitled"),
        )}
        confirmLabel={t("action.delete")}
        cancelLabel={t("action.cancel")}
        danger
        loading={deleteWorkspaceAutonomyGoal.isPending}
        closeOnConfirm={false}
        error={deleteWorkspaceAutonomyGoal.isError
          ? deleteWorkspaceAutonomyGoal.error.message
          : undefined}
        restoreFocusFallback={() => workspaceLifecycleTriggerRef.current?.focus()}
      />
    </div>
  );
}

/* ── Workspace event message row ── */

/** Exported for `scripts/workspace-chat-hitl-card.test.mjs`, which renders this
 *  row for real rather than grepping the source — a tool-call HITL card that
 *  stops rendering must fail a test, not ship. */
export function WsMessageRow({
  markerId,
  msg,
  subToAgent,
  currentUserName,
  currentUserAvatar,
  currentUserId,
  onResolve,
  onHitlAction,
  streaming,
  workspacePaused,
  presentingAgentGreeting,
  actionResetToken,
  onFeedback,
  messageFeedbackValue,
  onMessageFeedback,
  onArtifactOpen,
  suppressedTaskReferenceId,
  onResponseSurfaceSubmit,
  responseSurfaceSubmissionReceipts = [],
  onConfigureLedgers,
}: {
  markerId?: string;
  msg: WsMessage;
  subToAgent: Map<string, AgentInfo>;
  currentUserName: string;
  currentUserAvatar?: string | null;
  currentUserId?: string | null;
  onResolve: (
    msgId: string,
    choice: string,
    note?: string,
    payload?: Record<string, any>,
    files?: File[],
  ) => void;
  /** Answer a tool-call HITL card — a different path from `onResolve`; see
   *  `handleHitlAction`. */
  onHitlAction?: (hitlId: string, action: string) => void;
  streaming?: boolean;
  workspacePaused: boolean;
  presentingAgentGreeting?: boolean;
  actionResetToken?: number;
  onFeedback: (
    msgId: string,
    feedbackKey: string,
    rating: "up" | "down",
  ) => void;
  messageFeedbackValue?: ChatMessageFeedbackRating | null;
  onMessageFeedback?: (
    messageId: string,
    conversationId: string,
    rating: ChatMessageFeedbackRating,
    contentPreview: string,
  ) => void | Promise<void>;
  onArtifactOpen: (artifact: OutputArtifact, sourceAnchor?: string) => void;
  suppressedTaskReferenceId?: string;
  onResponseSurfaceSubmit?: (
    submission: ResponseSurfaceSubmissionReceipt,
  ) => void | boolean | ResponseSurfaceSubmissionResult
    | Promise<void | boolean | ResponseSurfaceSubmissionResult>;
  responseSurfaceSubmissionReceipts?: ResponseSurfaceSubmissionReceipt[];
  onConfigureLedgers?: (overview: WorkspaceLedgerOverview) => void;
}) {
  const location = useLocation();
  const isExternalCustomer = isExternalCustomerMessage(msg);
  const isUser = msg.author_kind === "user" && !isExternalCustomer;
  const authorUserId = msg.author_user_id || msg.meta?.author_user_id || null;
  const isCurrentUser = isUser && (!authorUserId || authorUserId === currentUserId);
  const userSenderName = isCurrentUser
    ? t("component.workspace_chat.you")
    : msg.author_user_name ||
      msg.author_user_email ||
      t("page.users.role_member");
  const agent = msg.author_subscription_id
    ? subToAgent.get(msg.author_subscription_id)
    : null;
  const senderName = isUser
    ? userSenderName
    : isExternalCustomer
      ? externalCustomerName(msg)
    : agent?.name ||
      (msg.author_kind === "system" ? systemSenderName(msg) : MANOR_AGENT_NAME);
  const color = isExternalCustomer ? "#436b65" : agent ? agentColor(agent.name) : "#1c1917";
  const collapseBody = shouldCollapseWorkspaceMessage(msg, isUser);
  const taskId = messageRefId(msg, "task");
  const linkedTaskRefs = taskRefs(msg);
  const delegatedRuns = delegatedAgentRunsFromMeta(msg.meta);
  const visibleTools = parseToolCalls(msg.tool_calls) || [];
  const fileReferences = chatMessageReferencesFromAttachments(msg.attachments);
  const messageAttachments = fileReferences.map((reference) => ({
    name: reference.name,
    document_id: reference.document_id,
    type: reference.kind,
    fileType: reference.fileType,
    mimeType: reference.mimeType,
    previewUrl: reference.previewUrl || reference.url,
    openUrl: reference.openUrl,
    fsPath: reference.fsPath,
  }));
  const localCodingNotice = !isUser ? maybeLocalCodingRunNoticeForTools(visibleTools) : null;
  const bodyContent = String(localCodingNotice || msg.body || "");
  const baseWorkspaceChatMessage: ChatMessage = {
    role: isUser ? "user" : "assistant",
    content: String(msg.body || ""),
    tool_calls: visibleTools,
    attachments: messageAttachments,
    stop_reason: String(msg.meta?.stop_reason || "") || undefined,
    stream_error: msg.meta?.stream_status === "error",
  };
  const retryableAssistant = !isUser && isRetryableAssistantMessage(
    baseWorkspaceChatMessage,
    bodyContent,
  );
  const hasAssistantBlocks =
    !isUser &&
    !retryableAssistant &&
    Array.isArray(msg.assistant_blocks) &&
    msg.assistant_blocks.length > 0;
  const workspaceChatMessage: ChatMessage = {
    ...baseWorkspaceChatMessage,
    assistant_blocks: hasAssistantBlocks
      ? msg.assistant_blocks as ChatMessage["assistant_blocks"]
      : undefined,
  };
  const messageReturnTo = `${location.pathname}${location.search}#workspace-chat-message-${msg.id}`;
  const renderedBodyDisplay = parseUserMessageDisplay(
    { ...workspaceChatMessage, content: bodyContent },
    { streaming: Boolean(streaming) },
  );
  const renderedBodyContent = renderedBodyDisplay.cleanContent;
  const messageDisplay = localCodingNotice
    ? parseUserMessageDisplay(workspaceChatMessage, {
        renderedContent: renderedBodyContent,
        streaming: Boolean(streaming),
      })
    : renderedBodyDisplay;
  const visibleFileReferences = messageDisplay.references;
  const messageArtifacts = !isUser
    ? filterMessageArtifactsAlreadyRepresented(
        workspaceChatMessage,
        workspaceFileArtifacts(workspaceChatMessage, Boolean(streaming)),
        true,
        {
          renderedContent: renderedBodyContent,
          streaming: Boolean(streaming),
        },
      )
    : [];
  const messageFeedbackTarget = messageFeedbackTargetKind(msg);
  const isCompletionReceipt = !isUser && isCompletionFeedbackMessage(msg);
  const completionFeedbackKey = completionFeedbackSubjectKey(msg);
  const visibleTaskRefs = linkedTaskRefs
    .map((ref, index) => ({ ref, index }))
    .filter(({ ref }) => ref.id !== suppressedTaskReferenceId);
  const showCompletionTaskLink = Boolean(
    isCompletionReceipt && taskId && taskId !== suppressedTaskReferenceId,
  );
  const actionCopyText = isUser || isExternalCustomer
    ? String(bodyContent || "")
    : formatUserFacingStructuredText(
        chatMessageActionText(
          {
            role: "assistant",
            content: msg.body,
            assistant_blocks: msg.assistant_blocks,
          },
          bodyContent,
        ),
      );
  const canRateMessage = Boolean(
    !isUser &&
      !isExternalCustomer &&
      msg.author_kind === "agent" &&
      messageFeedbackTarget === ChatFeedbackTargetKind.RESPONSE &&
      onMessageFeedback,
  );
  const canRateCompletion = Boolean(
    isCompletionReceipt && completionFeedbackKey,
  );
  const showMessageActions = Boolean(
    actionCopyText.trim() || canRateMessage || canRateCompletion,
  );
  const feedback = messageFeedbackValue || null;
  const canRetryFailedProposalApproval = Boolean(
    msg.resolved_at &&
      msg.pending_action?.kind === PendingActionKind.APPROVE_PROPOSALS &&
      ["approve", "approve_all"].includes(
        String(msg.resolution?.choice || "").toLowerCase(),
      ) &&
      linkedTaskRefs.some((ref) => ref.status === "proposed"),
  );
  const displayPendingAction = workflowStarterAction(msg);
  const structuredProposalPayload = structuredProposal(msg);
  const resolvedByName =
    msg.resolved_by_user_id && msg.resolved_by_user_id !== currentUserId
      ? msg.resolved_by_user_name || msg.resolved_by_user_email || undefined
      : undefined;
  const inlineProposalDecision = Boolean(
    !canRetryFailedProposalApproval &&
      msg.message_kind === "proposal" &&
      msg.resolved_at &&
      proposalApprovedRowIds(msg.resolution, []) !== null,
  );
  const proposalActionRowIds = displayPendingAction?.kind === PendingActionKind.APPROVE_PROPOSALS
    ? [
        ...(Array.isArray(displayPendingAction.task_ids)
          ? displayPendingAction.task_ids.map((id: unknown) => String(id || ""))
          : []),
        ...(Array.isArray(displayPendingAction.items)
          ? displayPendingAction.items.map((item: any) => String(item?.item_id || ""))
          : []),
      ].filter(Boolean)
    : [];
  const proposalActionRowKey = proposalActionRowIds.join("|");
  const [proposalSelectedRowIds, setProposalSelectedRowIds] = useState<Set<string>>(
    () => new Set(proposalActionRowIds),
  );
  const [proposalSelectionLocked, setProposalSelectionLocked] = useState(false);
  useEffect(() => {
    setProposalSelectedRowIds(new Set(proposalActionRowIds));
    setProposalSelectionLocked(false);
  }, [msg.id, proposalActionRowKey, actionResetToken]);
  const proposalRowsRenderedInline = Boolean(
    !hasAssistantBlocks &&
      renderedBodyContent &&
      msg.message_kind === "proposal" &&
      !isUser &&
      structuredProposalPayload &&
      displayPendingAction?.kind === PendingActionKind.APPROVE_PROPOSALS &&
      !msg.resolved_at,
  );
  const toggleProposalRow = (rowId: string) => {
    if (workspacePaused || proposalSelectionLocked) return;
    setProposalSelectedRowIds((previous) => {
      const next = new Set(previous);
      if (next.has(rowId)) next.delete(rowId); else next.add(rowId);
      return next;
    });
  };
  // Every entry needs an id: it is what the resolve reply is keyed on, so a
  // card without one would render buttons that can never resolve anything.
  const hitlCards = (
    Array.isArray(msg.hitl_requests) ? msg.hitl_requests : []
  ).filter((hitl) => hitl && typeof hitl === "object" && hitl.id);

  return (
    <div
      id={`workspace-chat-message-${msg.id}`}
      data-chat-scroll-marker-id={markerId || msg.id}
      className={`chat-message-row chat-message-shell ${isCurrentUser ? "chat-message-row--user" : ""}`}
    >
      {isUser ? (
        <UserAvatar
          name={isCurrentUser ? currentUserName : userSenderName}
          avatarUrl={isCurrentUser ? currentUserAvatar : msg.author_user_avatar_url}
          type="user"
          size={32}
        />
      ) : isExternalCustomer ? (
        <UserAvatar
          name={senderName}
          type="user"
          size={32}
        />
      ) : msg.author_kind === "system" ? (
        isGovernanceApprovalMessage(msg) ? (
          <UserAvatar type="governance" name={senderName} size={32} />
        ) : (
          <ManorAvatar size={32} />
        )
      ) : agent ? (
        <UserAvatar
          name={agent.name}
          avatarUrl={agent.avatar_url}
          type="agent"
          seed={agentAvatarSeed(agent)}
          size={32}
        />
      ) : (
        <ManorAvatar size={32} />
      )}

      <div
        className={`chat-message-col ${isCurrentUser ? "chat-message-col--user" : ""}`}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span
            className={`chat-sender-name ${isCurrentUser ? "chat-sender-name--user" : "chat-sender-name--agent"}`}
            style={
              (!isUser || !isCurrentUser) && color
                ? ({ "--chat-agent-name-color": color } as CSSProperties)
                : undefined
            }
          >
            {senderName}
          </span>
          {!isUser &&
            msg.message_kind !== "text" &&
            msg.message_kind !== "agent_update" &&
            msg.message_kind !== "external_message" && (
              <KindBadge kind={msg.message_kind} />
            )}
          {isExternalCustomer && (
            <KindBadge kind="external_message" />
          )}
        </div>

        <div
          className={`chat-bubble ${isCurrentUser ? "chat-bubble--user" : "chat-bubble--bot"} ${!isUser && msg.message_kind === "proposal" ? "chat-bubble--proposal" : ""}`}
          aria-busy={presentingAgentGreeting || undefined}
          style={
            !isUser && msg.message_kind === "goal_alert"
                ? {
                    background: "rgba(255,250,239,0.94)",
                    border: "1px solid rgba(207,155,68,0.2)",
                  }
                : !isUser && msg.message_kind === "step_event"
                  ? {
                      background: "rgba(250,249,247,0.95)",
                      border: "1px solid rgba(28,25,23,0.085)",
                      fontSize: 12,
                    }
                  : isExternalCustomer
                    ? {
                        background: "rgba(242,248,246,0.95)",
                        border: "1px solid rgba(79,113,105,0.2)",
                      }
                  : undefined
          }
        >
          {!hasAssistantBlocks && visibleTools.length > 0 && (
            <ToolCallList
              tools={visibleTools}
              keyPrefix={`ws-${msg.id}`}
              subAgentRuns={delegatedRuns}
            />
          )}

          {hasAssistantBlocks && (
            <AssistantMessageBlocks
              blocks={msg.assistant_blocks}
              content={renderedBodyContent}
              keyPrefix={`ws-${msg.id}`}
              minimal
              collapseLongFinal={collapseBody}
              subAgentRuns={delegatedRuns}
              onResponseSurfaceSubmit={onResponseSurfaceSubmit}
              sourceMessageId={msg.id}
              pendingActionKind={assistantPendingActionKindForMessage(msg)}
              responseSurfaceSubmissionReceipts={responseSurfaceSubmissionReceipts}
              onConfigureWorkspaceLedgers={onConfigureLedgers}
            />
          )}

          {!hasAssistantBlocks && msg.message_kind === "workflow_activity" && (
            <WorkflowActivityContent msg={msg} subToAgent={subToAgent} />
          )}

          {!hasAssistantBlocks &&
            msg.message_kind !== "workflow_activity" &&
            renderedBodyContent &&
            // An open approval renders a clean action card below; suppress the
            // raw governance body so internal keys/payloads never leak. Proposal
            // approvals are the exception: ProposalMessageContent reads their
            // typed payload and gives the operator the rationale before approval.
            // Keep the body for human_input too — there the body IS the question.
            !(
              msg.pending_action?.kind &&
              msg.pending_action.kind !== PendingActionKind.HUMAN_INPUT &&
              !(
                msg.message_kind === "proposal" &&
                msg.pending_action.kind === PendingActionKind.APPROVE_PROPOSALS
              ) &&
              !msg.resolved_at
            ) && (
            msg.message_kind === "proposal" && !isUser ? (
              <ProposalMessageContent
                content={formatUserFacingStructuredText(renderedBodyContent)}
                structured={structuredProposalPayload}
                action={displayPendingAction}
                resolution={msg.resolution}
                resolvedByName={resolvedByName}
                selectedRowIds={proposalRowsRenderedInline ? proposalSelectedRowIds : undefined}
                onToggleRow={proposalRowsRenderedInline ? toggleProposalRow : undefined}
                selectionDisabled={workspacePaused || proposalSelectionLocked}
              />
            ) : (
              <ExpandableWorkspaceMarkdown
                content={isUser ? renderedBodyContent : formatUserFacingStructuredText(renderedBodyContent)}
                isUser={isCurrentUser}
                collapsible={collapseBody}
                streaming={presentingAgentGreeting}
              />
            )
          )}

          {presentingAgentGreeting && (
            <span className="chat-streaming-cursor" aria-hidden="true" />
          )}

          <ChatMessageReferenceStrip
            references={visibleFileReferences}
            align={isCurrentUser ? "right" : "left"}
            inlineFileCards
            returnTo={messageReturnTo}
          />

          {!isUser && msg.meta?.workflow_result && (
            <WorkflowResultCard
              result={msg.meta.workflow_result}
              returnTo={`${location.pathname}${location.search}#workspace-chat-message-${msg.id}`}
            />
          )}

          {!isUser && Array.isArray(msg.meta?.simulation_artifacts) && (
            <SimulationArtifactGallery artifacts={msg.meta.simulation_artifacts} />
          )}

          {/* Tool-call HITL cards. Unlike FloatingChat/EmbeddedChat — which
              park the buttons in a sticky <ApprovalActionBar> because their
              panel can scroll the card out of reach — workspace chat renders
              a self-contained card with its buttons inline. It has no such
              bar, and every other actionable card here (`pending_action`,
              just below) is already inline; a second, floating affordance for
              one card type would contradict the surface's own idiom and put
              two different Approve buttons on screen at once. */}
          {hitlCards.length > 0 && (
            <div className="chat-hitl-cards">
              {hitlCards.map((hitl) => (
                <div
                  key={String(hitl.id)}
                  className={`chat-hitl-card ${hitl.type === "approval" ? "chat-hitl-card--approval" : ""}`}
                >
                  {hitl.type === "approval" ? (
                    <ApprovalSummary
                      prompt={hitl.prompt}
                      action={hitl.action}
                      tool={hitl.tool}
                      hasWorkspace={Boolean(
                        hitl.workspace?.id || hitl.workspace?.name,
                      )}
                      paths={hitl.paths}
                      content={hitl.content}
                      argsPreview={hitl.args_preview}
                      operation={hitl.operation}
                      hitlType={hitl.hitl_type}
                      payload={hitl.payload}
                    />
                  ) : (
                    <p className="chat-hitl-prompt">{hitl.prompt}</p>
                  )}
                  <ChatActionCard
                    action={{
                      kind:
                        hitl.type === "approval" ? "approve" : "human_input",
                      options: hitl.options || ["approve", "reject"],
                    }}
                    resolved={Boolean(hitl.resolved)}
                    resolution={
                      hitl.resolved
                        ? { choice: hitl.resolution || "approved" }
                        : null
                    }
                    currentUserName={currentUserName}
                    disabled={workspacePaused || Boolean(streaming) || Boolean(hitl.resolved)}
                    onResolve={(choice) =>
                      onHitlAction?.(String(hitl.id), choice)
                    }
                  />
                </div>
              ))}
            </div>
          )}

          {/* Persisted actions use their own endpoint; only their own
              submission and Workspace pause state should lock them. */}
          {!inlineProposalDecision &&
            ((msg.pending_action && msg.pending_action.kind) ||
              (msg.resolved_at && msg.resolution)) && (
              <ChatActionCard
                action={displayPendingAction || { kind: "unknown" }}
                resolved={!!msg.resolved_at && !canRetryFailedProposalApproval}
                resolution={
                  canRetryFailedProposalApproval ? null : msg.resolution
                }
                resolvedByName={resolvedByName}
                currentUserName={currentUserName}
                resetToken={actionResetToken}
                proposalSelectedRowIds={
                  proposalRowsRenderedInline ? proposalSelectedRowIds : undefined
                }
                onProposalSelectedRowIdsChange={
                  proposalRowsRenderedInline ? setProposalSelectedRowIds : undefined
                }
                proposalRowsRenderedElsewhere={proposalRowsRenderedInline}
                disabled={workspacePaused}
                onResolve={(choice, note, payload, files) => {
                  if (proposalRowsRenderedInline) setProposalSelectionLocked(true);
                  onResolve(msg.id, choice, note, payload, files)
                }}
              />
            )}

          {showCompletionTaskLink && (
            <div className="task-completion-actions">
              <Link
                className="task-completion-link"
                to={`/tasks/${taskId || ""}`}
              >
                {t("component.workspace_chat.view_task")}
              </Link>
            </div>
          )}

          {!showCompletionTaskLink && visibleTaskRefs.length > 0 && (
            <div
              className={`task-reference-actions ${isCurrentUser ? "task-reference-actions--user" : ""}`}
              aria-label={t("component.workspace_chat.related_tasks")}
            >
              <span className="task-reference-label">
                {t("component.workspace_chat.related_tasks")}
              </span>
              <div className="task-reference-links">
                {visibleTaskRefs.slice(0, 5).map(({ ref, index }) => (
                  <Link
                    key={`${ref.id}-${index}`}
                    className="task-reference-link"
                    to={`/tasks/${ref.id}`}
                    title={`${taskRefLabel(msg, index)}${taskRefMeta(ref) ? ` · ${taskRefMeta(ref)}` : ""}`}
                    aria-label={`${t("component.workspace_chat.view_task")}: ${taskRefLabel(msg, index)}`}
                  >
                    <span className="task-reference-link-title">{taskRefLabel(msg, index)}</span>
                    {taskRefMeta(ref) && (
                      <span className="task-reference-link-meta">{taskRefMeta(ref)}</span>
                    )}
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
        {!isUser && (
          <ArtifactSummaryCards
            artifacts={messageArtifacts}
            onOpen={(artifact) =>
              onArtifactOpen(artifact, `workspace-chat-message-${msg.id}`)
            }
          />
        )}
        <div
          className={`chat-message-meta-row ${
            isCurrentUser ? "chat-message-meta-row--user" : ""
          } ${showMessageActions ? "chat-message-meta-row--actions" : ""}`}
        >
          <ChatTimestamp timestamp={msg.created_at} />
          {showMessageActions && (
            <span className="chat-message-meta-actions">
              <ChatMessageActions
                align="right"
                copyText={actionCopyText}
                speechText={!isUser && !isExternalCustomer ? actionCopyText : undefined}
                voiceScope={{ conversationId: msg.conversation_id }}
                copyLabel={t(
                  isUser || isExternalCustomer
                    ? "component.chat_message_actions.copy_request"
                    : "component.chat_message_actions.copy_response",
                )}
                feedbackValue={feedback}
                disabled={Boolean(streaming)}
                onFeedback={
                  canRateCompletion && completionFeedbackKey
                    ? (rating) =>
                        onFeedback(msg.id, completionFeedbackKey, rating)
                    : canRateMessage
                    ? (rating) =>
                        void onMessageFeedback?.(
                          msg.id,
                          msg.conversation_id,
                          rating,
                          actionCopyText,
                        )
                    : undefined
                }
              />
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function WorkflowActivityContent({
  msg,
  subToAgent,
}: {
  msg: WsMessage;
  subToAgent: Map<string, AgentInfo>;
}) {
  const meta = msg.meta || {};
  const steps = Array.isArray(meta.workflow_steps)
    ? meta.workflow_steps.filter((step: unknown) => step && typeof step === "object")
    : [];
  const status = String(meta.workflow_status || "queued").toLowerCase();
  const businessOutcome = String(meta.workflow_business_outcome || "in_progress").toLowerCase();
  const attemptNumber = Math.max(1, Number(meta.workflow_attempt_number || 1));
  const currentStepId = String(meta.workflow_current_step_id || "");
  const currentStep = steps.find(
    (step: Record<string, unknown>) => String(step.id || "") === currentStepId,
  ) as Record<string, unknown> | undefined;
  const currentSubscriptionId = String(meta.workflow_current_subscription_id || "");
  const currentAgent = currentSubscriptionId
    ? subToAgent.get(currentSubscriptionId) || null
    : null;
  const completedCount = steps.filter(
    (step: Record<string, unknown>) => String(step.status || "") === "completed",
  ).length;

  return (
    <div className="workspace-workflow-activity" data-status={status}>
      <div className="workspace-workflow-activity-header">
        <span className="workspace-workflow-activity-icon" aria-hidden="true">
          <IconFlow size={15} />
        </span>
        <strong>{String(meta.workflow_title || t("component.workspace_chat.workflow"))}</strong>
        <span className="workspace-workflow-activity-status">
          <span aria-hidden="true" />
          {formatUserFacingText(status)}
        </span>
      </div>

      <div className="workspace-workflow-activity-outcome">
        <span>
          {t("component.workspace_chat.workflow_outcome", {
            outcome: formatUserFacingText(businessOutcome),
          })}
        </span>
        <span className="mono">
          {t("component.workspace_chat.workflow_attempt", { count: attemptNumber })}
        </span>
      </div>

      {(currentStep || currentAgent) && (
        <div className="workspace-workflow-activity-current">
          <AgentMiniAvatar agent={currentAgent} size={18} />
          <span>
            {currentAgent?.name || MANOR_AGENT_NAME}
            {currentStep?.name ? ` · ${formatUserFacingText(String(currentStep.name))}` : ""}
          </span>
        </div>
      )}

      {steps.length > 0 && (
        <div
          className="workspace-workflow-activity-steps"
          aria-label={t("component.workspace_chat.activity_steps", { count: steps.length })}
        >
          {steps.map((step: Record<string, unknown>) => {
            const stepStatus = String(step.status || "queued").toLowerCase();
            return (
              <span
                key={String(step.id || step.name)}
                className="workspace-workflow-activity-step"
                data-status={stepStatus}
                title={`${formatUserFacingText(String(step.name || step.id))}: ${formatUserFacingText(stepStatus)}`}
              >
                <span aria-hidden="true" />
                {formatUserFacingText(String(step.name || step.id))}
              </span>
            );
          })}
          <span className="workspace-workflow-activity-count mono">
            {completedCount}/{steps.length}
          </span>
        </div>
      )}

      {meta.workflow_error && (
        <div className="workspace-workflow-activity-error">
          {formatUserFacingText(String(meta.workflow_error))}
        </div>
      )}
    </div>
  );
}

function activityRunTitle(msgs: WsMessage[]): string {
  for (const m of msgs) {
    const match = (m.body || "").match(/for task:\s*\*?(.+?)\*?\s*$/im);
    if (match) return match[1].trim();
  }
  for (const m of msgs) {
    const ref = (m.refs || []).find(
      (r) => r.type === "task" && (r.title || r.name),
    );
    if (ref) return (ref.title || ref.name || "").trim();
  }
  return t("component.workspace_chat.activity_default_title");
}

function msgAgent(
  msg: WsMessage,
  subToAgent: Map<string, AgentInfo>,
): AgentInfo | null {
  const sid = msg.author_subscription_id;
  return sid ? subToAgent.get(sid) || null : null;
}

// All distinct agents that ran a step in this run, in order of first
// appearance. A run can span several agents — one per step is common — so the
// summary shows the whole cast, not just the busiest one.
function activityRunAgents(
  msgs: WsMessage[],
  subToAgent: Map<string, AgentInfo>,
): AgentInfo[] {
  const seen = new Set<string>();
  const agents: AgentInfo[] = [];
  for (const m of msgs) {
    const agent = msgAgent(m, subToAgent);
    if (agent && !seen.has(agent.id)) {
      seen.add(agent.id);
      agents.push(agent);
    }
  }
  return agents;
}

function activityRunTaskId(msgs: WsMessage[]): string | null {
  for (const m of msgs) {
    const id = messageRefId(m, "task");
    if (id) return id;
  }
  return null;
}

function AgentMiniAvatar({
  agent,
  size,
}: {
  agent: AgentInfo | null;
  size: number;
}) {
  return agent ? (
    <UserAvatar
      name={agent.name}
      avatarUrl={agent.avatar_url}
      type="agent"
      seed={agentAvatarSeed(agent)}
      size={size}
    />
  ) : (
    <ManorAvatar size={size} />
  );
}

// Collapses all step/plan status messages of one plan into a single quiet
// expandable line — attributed to every agent that ran a step and linking to
// the task — so a burst of step receipts and retries reads as one background
// activity rather than a wall of bubbles.
function WsActivityRun({
  markerId,
  msgs,
  subToAgent,
}: {
  markerId?: string;
  msgs: WsMessage[];
  subToAgent: Map<string, AgentInfo>;
}) {
  const [expanded, setExpanded] = useState(false);
  const stepMsgs = msgs.filter((m) => m.message_kind === "step_event");
  const failed = stepMsgs.filter((m) => /failed|✗/i.test(m.body || "")).length;
  // Prefer the planned step count announced by "Plan started — N step(s)";
  // fall back to however many step receipts we've actually seen.
  let total = stepMsgs.length;
  for (const m of msgs) {
    const declared = (m.body || "").match(/—\s*(\d+)\s*step/i);
    if (declared) total = Math.max(total, parseInt(declared[1], 10));
  }
  const title = activityRunTitle(msgs);
  const last = msgs[msgs.length - 1];
  const detailMsgs = stepMsgs.length > 0 ? stepMsgs : msgs;
  const agents = activityRunAgents(msgs, subToAgent);
  const taskId = activityRunTaskId(msgs);
  const stackAgents: (AgentInfo | null)[] =
    agents.length > 0 ? agents.slice(0, 3) : [null];
  const nameLabel =
    agents.length > 1
      ? t("component.workspace_chat.activity_agents", { count: agents.length })
      : agents[0]?.name || MANOR_AGENT_NAME;
  const summary = (
    <>
      <span className="ws-activity-run-agent">{nameLabel}</span>
      {" · "}
      <span className="ws-activity-run-title">{title}</span>
      <span className="ws-activity-run-meta">
        {" · "}
        {t("component.workspace_chat.activity_steps", { count: total })}
        {failed > 0 &&
          ` · ${t("component.workspace_chat.activity_failed", { count: failed })}`}
      </span>
    </>
  );
  return (
    <div
      className="ws-activity-line-row"
      data-chat-scroll-marker-id={markerId || last?.id || title}
    >
      <div className="ws-activity-run">
        <div className="ws-activity-line ws-activity-run-bar">
          <button
            type="button"
            className="ws-activity-run-chevron"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            aria-label={expanded ? t("chat.show_less") : t("chat.show_more")}
          >
            <span aria-hidden>{expanded ? "▾" : "▸"}</span>
          </button>
          <span className="ws-activity-run-avatars">
            {stackAgents.map((a, i) => (
              <span className="ws-activity-run-avatar" key={a?.id || i}>
                <AgentMiniAvatar agent={a} size={16} />
              </span>
            ))}
            {agents.length > 3 && (
              <span className="ws-activity-run-avatar-more">
                +{agents.length - 3}
              </span>
            )}
          </span>
          {taskId ? (
            <Link
              to={`/tasks/${taskId}`}
              className="ws-activity-run-main ws-activity-run-main--link"
              title={title}
            >
              {summary}
            </Link>
          ) : (
            <span className="ws-activity-run-main">{summary}</span>
          )}
          <span className="ws-activity-line-time mono">
            {formatTime(last.created_at)}
          </span>
        </div>
        {expanded && (
          <div className="ws-activity-run-detail">
            {detailMsgs.map((m, idx) => {
              const stepAgent = msgAgent(m, subToAgent);
              const prevAgent =
                idx > 0 ? msgAgent(detailMsgs[idx - 1], subToAgent) : undefined;
              const showName =
                idx === 0 ||
                (stepAgent?.id || null) !== (prevAgent?.id || null);
              return (
                <div className="ws-activity-run-step" key={m.id || idx}>
                  <span className="ws-activity-line-glyph" aria-hidden>
                    {/failed|✗/i.test(m.body || "") ? "✗" : "·"}
                  </span>
                  <span className="ws-activity-run-step-avatar">
                    <AgentMiniAvatar agent={stepAgent} size={14} />
                  </span>
                  <span className="ws-activity-run-step-text">
                    {showName && (
                      <span className="ws-activity-run-step-agent">
                        {stepAgent?.name || MANOR_AGENT_NAME} ·{" "}
                      </span>
                    )}
                    {activityLineText(m)}
                  </span>
                  <span className="ws-activity-line-time mono">
                    {formatTime(m.created_at)}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function WsActivityLine({
  markerId,
  msg,
  strategistActivity,
  announceFailure = false,
  processing = false,
  stream = false,
  onStreamComplete,
}: {
  markerId?: string;
  msg: WsMessage;
  strategistActivity?: Record<string, any> | null;
  announceFailure?: boolean;
  processing?: boolean;
  stream?: boolean;
  onStreamComplete?: (messageId: string) => void;
}) {
  const text = activityLineText(msg);
  const strategistState = typeof strategistActivity?.state === "string"
    ? strategistActivity.state
    : undefined;
  const displayText = strategistState === "failed" && !/failed|✗/i.test(text)
    ? `${text} — failed`
    : text;
  const textCharacters = useMemo(() => Array.from(displayText), [displayText]);
  const [visibleCharacters, setVisibleCharacters] = useState(() =>
    stream ? 0 : textCharacters.length,
  );
  const streamCompleteNotifiedRef = useRef(!stream);
  const previousStreamRef = useRef(stream);
  const previousStrategistStateRef = useRef(strategistState);
  const [failureShouldAnnounce, setFailureShouldAnnounce] = useState(
    () => announceFailure && strategistState === "failed",
  );
  const isStrategistActivity = msg.message_kind === "strategist_activity";
  const strategistStage = typeof strategistActivity?.stage === "string"
    ? strategistActivity.stage
    : "";
  const StrategistStageIcon = strategistStage === "collecting_feedback"
    ? IconSearch
    : strategistStage === "analyzing_context"
      ? IconBrain
      : strategistStage === "generating_plan"
        ? IconSparkles
        : strategistStage === "finalizing_plan"
          ? IconDocument
          : IconTimeline;
  const workspaceLifecycleActivity = msg.meta?.workspace_lifecycle === true;
  const lifecyclePhase = typeof msg.meta?.workspace_lifecycle_phase === "string"
    ? msg.meta.workspace_lifecycle_phase
    : undefined;
  const failed =
    lifecyclePhase === "failed" ||
    strategistState === "failed" ||
    /failed|✗/i.test(msg.body || "");
  const lifecycleAction: WorkspaceLifecycleAction = msg.meta?.workspace_lifecycle_action === "pause"
    ? "pause"
    : "start";
  const lifecycleGlyph = workspaceLifecycleActivity
    ? lifecyclePhase === "failed"
      ? "!"
      : lifecycleAction === "pause"
        ? "⏸"
        : "▶"
    : null;

  useLayoutEffect(() => {
    const streamStarted = stream && !previousStreamRef.current;
    previousStreamRef.current = stream;
    if (streamStarted) {
      streamCompleteNotifiedRef.current = false;
      setVisibleCharacters(0);
      return;
    }
    if (!stream) {
      streamCompleteNotifiedRef.current = true;
      setVisibleCharacters(textCharacters.length);
    }
  }, [stream, textCharacters.length]);

  useEffect(() => {
    const transitionedToFailure =
      strategistState === "failed" &&
      previousStrategistStateRef.current !== "failed";
    previousStrategistStateRef.current = strategistState;
    if (transitionedToFailure || (announceFailure && strategistState === "failed")) {
      setFailureShouldAnnounce(true);
    }
  }, [announceFailure, strategistState]);

  useEffect(() => {
    if (!stream) return;
    if (visibleCharacters >= textCharacters.length) {
      if (!streamCompleteNotifiedRef.current) {
        streamCompleteNotifiedRef.current = true;
        onStreamComplete?.(msg.id);
      }
      return;
    }

    const reduceMotion = prefersReducedMotion();
    const timer = window.setTimeout(() => {
      setVisibleCharacters((current) => {
        if (reduceMotion) return textCharacters.length;
        const remaining = textCharacters.slice(current).join("");
        const [nextChunk] = nextTypewriterSlice(remaining);
        return Math.min(
          textCharacters.length,
          current + Math.max(1, Array.from(nextChunk).length),
        );
      });
    }, reduceMotion ? 0 : TYPEWRITER_TICK_MS);
    return () => window.clearTimeout(timer);
  }, [
    msg.id,
    onStreamComplete,
    stream,
    textCharacters,
    visibleCharacters,
  ]);

  const visibleText = stream
    ? textCharacters.slice(0, visibleCharacters).join("")
    : displayText;
  const activelyStreaming = stream && visibleCharacters < textCharacters.length;
  const shouldAnnounceStrategistStatus =
    isStrategistActivity &&
    ((stream && !activelyStreaming) || failureShouldAnnounce);

  return (
    <div
      className={`ws-activity-line-row${isStrategistActivity ? " ws-activity-line-row--strategist" : ""}${workspaceLifecycleActivity ? " ws-activity-line-row--workspace-lifecycle" : ""}`}
      data-chat-scroll-marker-id={markerId || msg.id}
      data-lifecycle-phase={lifecyclePhase}
      data-lifecycle-action={workspaceLifecycleActivity ? lifecycleAction : undefined}
      data-strategist-stage={isStrategistActivity ? strategistStage : undefined}
      data-strategist-state={isStrategistActivity ? strategistState : undefined}
      data-processing={(isStrategistActivity && processing) || undefined}
      data-streaming={activelyStreaming || undefined}
      aria-live={workspaceLifecycleActivity ? "polite" : undefined}
    >
      <span className="ws-activity-line">
        <span className="ws-activity-line-glyph" aria-hidden>
          {failed
            ? "✗"
            : lifecycleGlyph || (isStrategistActivity
              ? <StrategistStageIcon size={14} />
              : "▸")}
        </span>
        <span
          className="ws-activity-line-text"
          aria-hidden={
            (isStrategistActivity && (stream || shouldAnnounceStrategistStatus)) ||
            undefined
          }
        >
          {visibleText}
          {activelyStreaming && (
            <span className="chat-streaming-cursor" aria-hidden="true" />
          )}
        </span>
        {shouldAnnounceStrategistStatus && (
          <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
            {displayText}
          </span>
        )}
        <span className="ws-activity-line-time mono">
          {formatTime(msg.created_at)}
        </span>
      </span>
    </div>
  );
}

/** The typed payload the backend attaches to every proposal card. */
type StructuredProposal = {
  summary?: string | null;
  notes?: string | null;
  tasks?: unknown;
  /** Non-task cohort members (changes / experiments). */
  items?: unknown;
  /** Informational footers: auto-approval reason, governance block notice. */
  footnotes?: unknown;
};

function structuredProposalItems(source: unknown): {
  item_id?: string;
  kind: string;
  summary: string;
}[] {
  if (!Array.isArray(source)) return [];
  return source
    .filter((item): item is Record<string, any> =>
      Boolean(item && typeof item === "object"),
    )
    .map((item) => ({
      item_id: item.item_id ? String(item.item_id) : undefined,
      kind: formatUserFacingText(String(item.kind || "item").replace(/_/g, " ")),
      summary: formatUserFacingText(String(item.summary || "")),
    }))
    .filter((item) => item.summary || item.kind);
}

function ProposalDecisionStatus({
  approved,
  by,
}: {
  approved: boolean;
  by?: string;
}) {
  return (
    <span
      className={`workspace-proposal-decision ${approved ? "is-approved" : "is-not-approved"}`}
    >
      <span className="workspace-proposal-decision-icon" aria-hidden="true">
        {approved ? "✓" : "—"}
      </span>
      <span>
        {approved && by
          ? t("component.chat_action_card.approved_by").replace("{name}", by)
          : t(
              approved
                ? "component.chat_action_card.approved_items"
                : "component.chat_action_card.not_approved_items",
            )}
      </span>
    </span>
  );
}

function ProposalSelectionToggle({
  rowId,
  label,
  selected,
  disabled,
  onToggle,
}: {
  rowId: string;
  label: string;
  selected: boolean;
  disabled?: boolean;
  onToggle: (rowId: string) => void;
}) {
  return (
    <label className="workspace-proposal-task-select">
      <input
        type="checkbox"
        checked={selected}
        disabled={disabled}
        onChange={() => onToggle(rowId)}
        aria-label={t("component.chat_action_card.select_for_approval").replace(
          "{item}",
          formatUserFacingText(label),
        )}
      />
    </label>
  );
}

/** `meta.proposal` is written for every card; `pending_action.tasks` is the
 *  same list, kept for cards whose meta was pruned by an older writer. */
function structuredProposal(msg: WsMessage): StructuredProposal | null {
  const fromMeta = msg.meta?.proposal;
  const actionTasks = msg.pending_action?.tasks;
  const actionItems = msg.pending_action?.items;
  if (fromMeta && typeof fromMeta === "object") {
    const proposal = fromMeta as StructuredProposal;
    return {
      ...proposal,
      tasks: Array.isArray(proposal.tasks) && proposal.tasks.length > 0
        ? proposal.tasks
        : actionTasks,
      items: Array.isArray(proposal.items) && proposal.items.length > 0
        ? proposal.items
        : actionItems,
    };
  }
  if (Array.isArray(actionTasks) || Array.isArray(actionItems)) {
    return { tasks: actionTasks, items: actionItems };
  }
  return null;
}

function ProposalMessageContent({
  content,
  structured,
  action,
  resolution,
  resolvedByName,
  selectedRowIds,
  onToggleRow,
  selectionDisabled,
}: {
  content: string;
  structured?: StructuredProposal | null;
  action?: WsMessage["pending_action"];
  resolution?: WsMessage["resolution"];
  resolvedByName?: string;
  selectedRowIds?: Set<string>;
  onToggleRow?: (rowId: string) => void;
  selectionDisabled?: boolean;
}) {
  const structuredTasks = proposalTaskEntries(structured?.tasks);
  const proposal: ParsedProposal | null = structured
    ? {
        summary: cleanProposalLine(structured.summary || ""),
        tasks: [],
        notes: splitProposalNotes(structured.notes || "").filter(Boolean),
      }
    : // `parseWorkspaceProposal` survives for ONE reason: proposal cards
      // posted before the structured payload shipped still have to render.
      // Cards written today never reach it — do not extend it.
      parseWorkspaceProposal(content);
  const legacyTasks = structured ? [] : proposal?.tasks || [];
  const hasProposalTasks = structuredTasks.length + legacyTasks.length > 0;
  const structuredItems = structuredProposalItems(structured?.items);
  const actionTaskIds = Array.isArray(action?.task_ids)
    ? action!.task_ids.map((id: unknown) => String(id || ""))
    : [];
  const actionItems = Array.isArray(action?.items) ? action!.items : [];
  const structuredTaskRowIds = structuredTasks.map(
    (task, index) => task.task_id || actionTaskIds[index] || `proposal-task-${index}`,
  );
  const legacyTaskRowIds = legacyTasks.map(
    (_task, index) => actionTaskIds[index] || `proposal-legacy-task-${index}`,
  );
  const itemRowIds = structuredItems.map(
    (item, index) =>
      item.item_id ||
      String(actionItems[index]?.item_id || "") ||
      `proposal-item-${index}`,
  );
  const approvedRowIds = proposalApprovedRowIds(resolution, [
    ...structuredTaskRowIds,
    ...legacyTaskRowIds,
    ...itemRowIds,
  ]);
  const footnotes = Array.isArray(structured?.footnotes)
    ? structured!.footnotes.map((line) => formatUserFacingText(String(line))).filter(Boolean)
    : [];
  const structuredEmpty =
    Boolean(structured) &&
    !proposal?.summary &&
    !structuredTasks.length &&
    !structuredItems.length &&
    !(proposal?.notes.length || 0);
  if (!proposal || structuredEmpty) {
    return (
      <ExpandableWorkspaceMarkdown
        content={content.replace(/~~/g, "")}
        isUser={false}
        collapsible={false}
      />
    );
  }

  return (
    <div className="workspace-proposal-card">
      {proposal.summary && (
        <div className="workspace-proposal-summary">
          <div className="workspace-proposal-section-title">
            {t("component.workspace_chat.proposal_summary")}
          </div>
          <p>{proposal.summary}</p>
        </div>
      )}

      {(structuredTasks.length > 0 || legacyTasks.length > 0) && (
        <div className="workspace-proposal-section">
          <div className="workspace-proposal-section-title">
            {t("component.workspace_chat.proposed_work")}
          </div>
          <div className="workspace-proposal-task-list">
            {/* Typed payload: the ordinal stays an ordinal, the priority
                becomes a word, and the predicted delta says what it moves. */}
            {structuredTasks.map((task, index) => {
              const priorityLabel = proposalPriorityLabel(task.priority);
              const impact = proposalImpactLabel(task);
              const basis = proposalBasisView(task);
              const rowId = structuredTaskRowIds[index];
              const isSelectable = Boolean(selectedRowIds && onToggleRow);
              const isSelected = selectedRowIds?.has(rowId) ?? true;
              return (
                <div
                  className={`workspace-proposal-task ${isSelectable ? "is-selectable" : ""} ${!isSelected ? "is-unselected" : ""}`}
                  key={task.task_id || `${task.title}-${index}`}
                >
                  {isSelectable && onToggleRow && (
                    <ProposalSelectionToggle
                      rowId={rowId}
                      label={task.title}
                      selected={isSelected}
                      disabled={selectionDisabled}
                      onToggle={onToggleRow}
                    />
                  )}
                  <div className="workspace-proposal-task-rank">{index + 1}</div>
                  <div className="workspace-proposal-task-body">
                    <div className="workspace-proposal-task-title-row">
                      <span className="workspace-proposal-task-title">
                        {formatUserFacingText(task.title)}
                      </span>
                      {approvedRowIds && (
                        <ProposalDecisionStatus
                          approved={approvedRowIds.has(structuredTaskRowIds[index])}
                          by={resolvedByName}
                        />
                      )}
                      {priorityLabel && (
                        <Chip size="sm" variant={task.priority === 5 ? "red" : "slate"}>
                          {priorityLabel}
                        </Chip>
                      )}
                      {impact && (
                        <span
                          className="workspace-proposal-impact"
                          title={proposalImpactExplainer()}
                        >
                          {impact}
                        </span>
                      )}
                    </div>
                    {task.rationale && (
                      <p className="workspace-proposal-task-detail">
                        {formatUserFacingText(task.rationale)}
                      </p>
                    )}
                    {basis && (
                      <details className="workspace-proposal-basis">
                        <summary>
                          <span>{t("component.workspace_chat.proposal_basis")}</span>
                        </summary>
                        <div className="workspace-proposal-basis-content">
                          {basis.sources.length > 0 && (
                            <div className="workspace-proposal-basis-row">
                              <span className="workspace-proposal-basis-label">
                                {t("component.workspace_chat.proposal_basis_reports")}
                              </span>
                              <span className="workspace-proposal-basis-sources">
                                {basis.sources.join(" · ")}
                              </span>
                            </div>
                          )}
                          {basis.signals.length > 0 && (
                            <div className="workspace-proposal-basis-row">
                              <span className="workspace-proposal-basis-label">
                                {t("component.workspace_chat.proposal_basis_evidence")}
                              </span>
                              <ul className="workspace-proposal-basis-signals">
                                {basis.signals.map((signal) => (
                                  <li key={signal}>{formatUserFacingText(signal)}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      </details>
                    )}
                  </div>
                </div>
              );
            })}
            {legacyTasks.map((task, index) => {
              const rowId = legacyTaskRowIds[index];
              const isSelectable = Boolean(selectedRowIds && onToggleRow);
              const isSelected = selectedRowIds?.has(rowId) ?? true;
              return (
                <div
                  className={`workspace-proposal-task ${isSelectable ? "is-selectable" : ""} ${!isSelected ? "is-unselected" : ""}`}
                  key={`${task.title}-${index}`}
                >
                  {isSelectable && onToggleRow && (
                    <ProposalSelectionToggle
                      rowId={rowId}
                      label={task.title}
                      selected={isSelected}
                      disabled={selectionDisabled}
                      onToggle={onToggleRow}
                    />
                  )}
                  <div className="workspace-proposal-task-rank">
                    {task.rank || index + 1}
                  </div>
                  <div className="workspace-proposal-task-body">
                    <div className="workspace-proposal-task-title-row">
                      <span className="workspace-proposal-task-title">{task.title}</span>
                      {approvedRowIds && (
                        <ProposalDecisionStatus
                          approved={approvedRowIds.has(legacyTaskRowIds[index])}
                          by={resolvedByName}
                        />
                      )}
                      {task.impact && (
                        <span className="workspace-proposal-impact">{task.impact}</span>
                      )}
                    </div>
                    {task.detail && (
                      <p className="workspace-proposal-task-detail">{task.detail}</p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Changes / experiments that ride the same cohort. */}
      {structuredItems.length > 0 && (
        <div className="workspace-proposal-section">
          <div className="workspace-proposal-section-title">
            {t("component.workspace_chat.proposed_changes")}
          </div>
          <div className="workspace-proposal-task-list">
            {structuredItems.map((item, index) => (
              <div
                className={`workspace-proposal-task workspace-proposal-task--item ${selectedRowIds && onToggleRow ? "is-selectable" : ""} ${selectedRowIds && !selectedRowIds.has(itemRowIds[index]) ? "is-unselected" : ""}`}
                key={`${item.summary}-${index}`}
              >
                {selectedRowIds && onToggleRow && (
                  <ProposalSelectionToggle
                    rowId={itemRowIds[index]}
                    label={item.summary}
                    selected={selectedRowIds.has(itemRowIds[index])}
                    disabled={selectionDisabled}
                    onToggle={onToggleRow}
                  />
                )}
                <div className="workspace-proposal-task-body">
                  <div className="workspace-proposal-task-title-row">
                    <Chip size="sm" variant="slate">{item.kind}</Chip>
                    <span className="workspace-proposal-task-title">{item.summary}</span>
                    {approvedRowIds && (
                      <ProposalDecisionStatus
                        approved={approvedRowIds.has(itemRowIds[index])}
                        by={resolvedByName}
                      />
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {proposal.notes.length > 0 && !hasProposalTasks && (
        <div className="workspace-proposal-section workspace-proposal-blocking-notes">
          <div className="workspace-proposal-section-title">
            {t("component.workspace_chat.blocking_reasons")}
          </div>
          <ol>
            {proposal.notes.slice(0, 5).map((note, index) => (
              <li key={`${note}-${index}`}>{note}</li>
            ))}
          </ol>
        </div>
      )}

      {footnotes.map((line, index) => (
        <p className="workspace-proposal-footnote" key={`${line}-${index}`}>{line}</p>
      ))}
      {approvedRowIds && resolution?.note && (
        <p className="workspace-proposal-footnote">
          {formatUserFacingText(resolution.note)}
        </p>
      )}

      {proposal.notes.length > 0 && hasProposalTasks && (
        <details className="workspace-proposal-notes">
          <summary>{t("component.workspace_chat.proposal_context")}</summary>
          <ol>
            {proposal.notes.slice(0, 5).map((note, index) => (
              <li key={`${note}-${index}`}>{note}</li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}

function ExpandableWorkspaceMarkdown({
  content,
  isUser,
  collapsible,
  streaming,
}: {
  content: string;
  isUser: boolean;
  collapsible: boolean;
  streaming?: boolean;
}) {
  return (
    <CollapsibleSentMessage text={content} enabled={collapsible && !streaming} tone={isUser ? "user" : "assistant"}>
      <ChatMarkdown content={content} isUser={isUser} streaming={streaming} />
    </CollapsibleSentMessage>
  );
}

function KindBadge({ kind }: { kind: string }) {
  const config: Record<string, { label: string; color: string; bg: string }> = {
    proposal: {
      label: t("component.workspace_chat.proposal"),
      color: "var(--message-kind-proposal-fg)",
      bg: "var(--message-kind-proposal-bg)",
    },
    step_event: {
      label: t("component.workspace_chat.step"),
      color: "#5f574f",
      bg: "rgba(120,113,108,0.11)",
    },
    workflow_activity: {
      label: t("component.workspace_chat.workflow"),
      color: "#5f574f",
      bg: "rgba(120,113,108,0.11)",
    },
    goal_alert: { label: t("component.embedded_chat.goal"), color: "#8c5e25", bg: "rgba(207,155,68,0.14)" },
    hitl_request: {
      label: t("component.workspace_chat.input_needed"),
      color: "#af3f3a",
      bg: "rgba(214,95,89,0.12)",
    },
    external_message: {
      label: t("component.workspace_chat.customer"),
      color: "#3f665e",
      bg: "rgba(79,113,105,0.13)",
    },
    system: { label: t("page.team_roles.system_2"), color: "#5f574f", bg: "rgba(120,113,108,0.11)" },
  };
  const c = config[kind] || {
    label: kind,
    color: "#78716c",
    bg: "rgba(120,113,108,0.08)",
  };
  return (
    <span
      style={{
        display: "inline-block",
        fontSize: 9,
        fontWeight: 800,
        padding: "2px 7px",
        borderRadius: 4,
        color: c.color,
        background: c.bg,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
      }}
    >
      {c.label}
    </span>
  );
}
