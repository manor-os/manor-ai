/**
 * WorkspaceChat — interactive group chat for workspace operations.
 *
 * Default: messages go to Manor AI (master agent).
 * @mention: type "@" to open agent picker dropdown, routes message to that agent.
 * Also displays workspace events: proposals, agent updates, step events, goal alerts.
 */
import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo, type CSSProperties } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api, type WorkspaceChatEntrypoint } from "../lib/api";
import { MANOR_AGENT_NAME } from "../lib/constants";
import type { Workspace, Agent } from "../lib/types";
import { useWebSocket } from "../lib/websocket";
import { useAuthStore } from "../stores/auth";
import { t } from "../lib/i18n";
import { openDetail, closeDetail, useDetailStore } from "../stores/detail";
import { openAgentEditModal } from "../stores/agentEditModal";
import ChatMarkdown from "./ChatMarkdown";
import AssistantMessageBlocks from "./AssistantMessageBlocks";
import CollapsibleSentMessage from "./chat/CollapsibleSentMessage";
import ChatScrollRail, {
  type ChatScrollRailMarker,
} from "./chat/ChatScrollRail";
import ManorAvatar from "./ui/ManorAvatar";
import UserAvatar from "./ui/UserAvatar";
import WorkspaceIconTile from "./ui/WorkspaceIcon";
import ChatActionCard from "./ui/ChatActionCard";
import { isErrorHitlCard } from "../lib/approvalCopy";
import { PendingActionKind } from "../lib/pendingActionKinds";
import InlineTips from "./ui/InlineTips";
import ToolCallList from "./ui/ToolCallList";
import LoadingSpinner from "./ui/LoadingSpinner";
import Select from "./ui/Select";
import { ChatMessagesSkeleton, SkeletonLine } from "./ui/Skeleton";
import ChatInputFooter, {
  manualSkillLabel,
  stripManualSkillTokens,
  type AttachedItem,
  type ManualSkillItem,
} from "./ChatInputFooter";
import WorkspaceWorkflowRunHost, {
  buildWorkspaceWorkflowRunGroups,
  workflowRunIdForMessage,
  workflowHostOwnedMessageIds,
} from "./workflows/WorkspaceWorkflowRunHost";
import {
  IconChatBubble,
  IconEdit,
  IconFlow,
  IconThumbDown,
  IconThumbUp,
} from "./icons";
import {
  parseToolCalls,
  type ChatMessage,
  type SubAgentEvent,
  type ToolCall,
} from "../lib/chatStream";
import { useChatStreamStore } from "../stores/chatStream";
import {
  formatUserFacingStructuredText,
  formatUserFacingText,
} from "../lib/taskDisplay";
import { relativeTime } from "../lib/format";
import Chip from "./ui/Chip";
import StatusBadge from "./ui/StatusBadge";
import {
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

interface AgentInfo {
  id: string;
  name: string;
  avatar_url?: string;
}

interface WorkspaceChatProps {
  workspaceId: string;
  workspace?: Workspace;
  workspaceName?: string;
  workspaceCoverUrl?: string;
  threadRef?: { kind: "task" | "plan" | "goal"; id: string };
  agentMappings?: { agent_id: string; service_key: string; id: string }[];
  entityAgents?: AgentInfo[];
}

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

function mergeWorkspaceMessages(existing: WsMessage[], incoming: WsMessage[]) {
  const byId = new Map<string, WsMessage>();
  [...existing, ...incoming].forEach((message) => {
    if (message.id) byId.set(message.id, message);
  });
  return Array.from(byId.values());
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
const COLLAPSIBLE_MESSAGE_MAX_CHARS = 900;
const COLLAPSIBLE_MESSAGE_MAX_LINES = 12;
const COLLAPSED_MESSAGE_MAX_HEIGHT = 260;

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

function isLongWorkspaceMessage(content: string) {
  return (
    content.length > COLLAPSIBLE_MESSAGE_MAX_CHARS ||
    content.split(/\r?\n/).length > COLLAPSIBLE_MESSAGE_MAX_LINES
  );
}

function shouldCollapseWorkspaceMessage(msg: WsMessage, isUser: boolean) {
  if (!msg.body || !isLongWorkspaceMessage(msg.body)) return false;
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

function isTaskCompletionMessage(msg: WsMessage) {
  const body = normalizeMessageText(msg.body).toLowerCase();
  return (
    msg.message_kind === "agent_update" &&
    body.includes("task complete") &&
    Boolean(messageRefId(msg, "task"))
  );
}

function planRefId(msg: WsMessage): string | null {
  const ref = (msg.refs || []).find((item) => item.type === "plan" && item.id);
  return ref?.id || null;
}

// Per-step / plan-lifecycle status updates ("▶ Plan started", "✗ Step … failed",
// step receipts). These are machine status, not conversation, so they render as
// quiet centered system lines rather than chat bubbles. Task-completion receipts
// are excluded — they carry user feedback and stay conversational.
function isActivityMessage(msg: WsMessage): boolean {
  if (isTaskCompletionMessage(msg)) return false;
  if (msg.message_kind === "step_event") return true;
  return msg.message_kind === "agent_update" && Boolean(planRefId(msg));
}

function activityLineText(msg: WsMessage): string {
  const body = msg.body || "";
  const willRetry = /will retry/i.test(body);
  let firstLine = (body.split(/\r?\n/)[0] || "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/[✅✔️❌⚠️🎉🚀🟢🔴🟡]/g, "")
    .replace(/^\s*[▶✓✗•·]\s*/, "")
    .trim();
  // Drop the internal error / traceback after "failed:" — never surface code,
  // function names, or stack details to the user.
  firstLine = firstLine.replace(/\bfailed\b\s*:.*$/i, "failed");
  let text = formatUserFacingText(firstLine);
  if (willRetry && !/retry/i.test(text)) text = `${text} — will retry`;
  return text;
}

function taskCompletionFeedback(
  msg: WsMessage,
  userId?: string | null,
): "up" | "down" | null {
  const feedbackByUser = msg.meta?.task_completion_feedback;
  const userRating =
    userId && feedbackByUser && typeof feedbackByUser === "object"
      ? feedbackByUser[userId]
      : null;
  const rating =
    userRating || msg.meta?.latest_task_completion_feedback?.rating || null;
  return rating === "up" || rating === "down" ? rating : null;
}

function isOpenPendingAction(msg: WsMessage) {
  return Boolean(msg.pending_action?.kind && !msg.resolved_at);
}

function pendingActionLabel(action: WsMessage["pending_action"]) {
  const kind = action?.kind || "unknown";
  const translated = t(`component.workspace_chat.pending_action_${kind}`);
  return translated === `component.workspace_chat.pending_action_${kind}`
    ? formatUserFacingText(kind.replace(/_/g, " "))
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

/* ── Component ── */

export default function WorkspaceChat({
  workspaceId,
  workspace,
  workspaceName,
  workspaceCoverUrl,
  threadRef,
  agentMappings,
  entityAgents,
}: WorkspaceChatProps) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [agentsExpanded, setAgentsExpanded] = useState(false);
  const currentUser = useAuthStore((s) => s.user);
  const currentUserName =
    currentUser?.display_name ||
    [currentUser?.first_name, currentUser?.last_name]
      .filter(Boolean)
      .join(" ") ||
    currentUser?.email ||
    t("component.workspace_chat.you");
  const currentUserAvatar = currentUser?.avatar_url;
  const bottomRef = useRef<HTMLDivElement>(null);
  const chatBodyRef = useRef<HTMLDivElement>(null);
  const didInitialScrollRef = useRef(false);
  const streamScrollFrameRef = useRef<number | null>(null);
  const lastStreamScrollAtRef = useRef(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const draftScope = threadRef
    ? `${workspaceId}:${threadRef.kind}:${threadRef.id}`
    : workspaceId;
  const streamSessionKey = `workspace-chat:${draftScope}`;
  const [input, setInput] = useState(() => loadWorkspaceChatDraft(draftScope));
  const [selectedEntrypointId, setSelectedEntrypointId] = useState("");
  const currentSession = useChatStreamStore(
    (s) => s.sessions[streamSessionKey],
  );
  const streaming = Boolean(currentSession?.streaming);
  const localMsgs = (currentSession?.messages || []) as WorkspaceLocalMsg[];
  const conversationId = currentSession?.convId;
  const startStream = useChatStreamStore((s) => s.startStream);
  const stopStream = useChatStreamStore((s) => s.stopStream);
  const setSessionMessages = useChatStreamStore((s) => s.setSessionMessages);
  const streamingRef = useRef(false);

  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);

  useEffect(() => {
    didInitialScrollRef.current = false;
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
    setSelectedEntrypointId("");
    setMentionAgent(null);
    setMentionDropdownOpen(false);
  }, [draftScope]);

  // @mention state
  const [mentionAgent, setMentionAgent] = useState<AgentInfo | null>(null);
  const [mentionDropdownOpen, setMentionDropdownOpen] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  const [mentionActiveIdx, setMentionActiveIdx] = useState(0);
  const mentionRef = useRef<HTMLDivElement>(null);

  // Fetch agent mappings + entity agents for this workspace (self-contained)
  const { data: fetchedMappings, isLoading: fetchedMappingsLoading } = useQuery({
    queryKey: ["workspace-agents", workspaceId],
    queryFn: () => api.workspaces.agents.list(workspaceId),
    enabled: !!workspaceId,
  });
  const { data: fetchedAgents, isLoading: fetchedAgentsLoading } = useQuery({
    queryKey: ["entity-agents"],
    queryFn: () => api.agents.list(),
    enabled: !!workspaceId,
  });
  const { data: workflowEntrypoints = [] } = useQuery({
    queryKey: ["workspace-chat-entrypoints", workspaceId],
    queryFn: () => api.workspaces.chat.listEntrypoints(workspaceId),
    enabled: Boolean(workspaceId && !threadRef),
  });
  const selectedEntrypoint = workflowEntrypoints.find(
    (entrypoint: WorkspaceChatEntrypoint) => entrypoint.binding_id === selectedEntrypointId,
  );

  // Merge props with fetched data (props override if provided)
  const mappings = (agentMappings || fetchedMappings || []) as any[];
  const agents = (entityAgents || fetchedAgents || []) as any[];

  // Build subscription → agent lookup
  const subToAgent = useMemo(() => {
    const map = new Map<string, AgentInfo>();
    for (const m of mappings) {
      const agent = agents.find((a: any) => a.id === m.agent_id);
      if (agent) map.set(m.id, agent as AgentInfo);
    }
    return map;
  }, [mappings, agents]);

  const agentList = useMemo(() => {
    const seen = new Map<string, AgentInfo>();
    subToAgent.forEach((a) => {
      if (!seen.has(a.id)) seen.set(a.id, a);
    });
    return Array.from(seen.values());
  }, [subToAgent]);

  const latestAgentQuickViewRequestRef = useRef(0);

  const openAgentQuickView = async (agent: AgentInfo) => {
    const requestId = ++latestAgentQuickViewRequestRef.current;
    openDetail({
      icon: (
        <UserAvatar
          name={agent.name}
          avatarUrl={agent.avatar_url}
          type="agent"
          seed={agent.id}
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
      openDetail({
        icon: (
          <UserAvatar
            name={full.name}
            avatarUrl={full.avatar_url}
            type="agent"
            seed={full.id}
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
        primaryAction: {
          label: t("page.agents.manage"),
          onClick: () => {
            closeDetail();
            navigate(`/agents/${full.id}`);
          },
        },
        secondaryActions: [
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
            seed={agent.id}
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
    !entityAgents &&
    (fetchedMappingsLoading || fetchedAgentsLoading);

  // Filtered agents for @mention dropdown
  const mentionFiltered = useMemo(() => {
    if (!mentionQuery) return agentList;
    const q = mentionQuery.toLowerCase();
    return agentList.filter((a) => (a.name || "").toLowerCase().includes(q));
  }, [agentList, mentionQuery]);

  const [wsMessages, setWsMessages] = useState<WsMessage[]>([]);
  const [workspaceHistoryState, setWorkspaceHistoryState] = useState({
    hasMore: false,
    nextCursor: null as string | null,
  });
  const [loadingOlderMessages, setLoadingOlderMessages] = useState(false);
  const suppressNextAutoScrollRef = useRef(false);

  useEffect(() => {
    setWsMessages([]);
    setWorkspaceHistoryState({ hasMore: false, nextCursor: null });
    setLoadingOlderMessages(false);
  }, [workspaceId, threadRef?.kind, threadRef?.id]);

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
      [workspaceId, queryClient, wsMessages],
    ),
    onTyping: handleTyping,
  });

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

  const workflowRunGroups = useMemo(
    () => buildWorkspaceWorkflowRunGroups(sorted),
    [sorted],
  );
  const hostOwnedWorkflowMessageIds = useMemo(
    () => workflowHostOwnedMessageIds(workflowRunGroups),
    [workflowRunGroups],
  );
  const visibleSortedMessages = useMemo(
    () => sorted.filter((msg) => !hostOwnedWorkflowMessageIds.has(msg.id)),
    [hostOwnedWorkflowMessageIds, sorted],
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

    if (!streaming) {
      if (!latestPendingActionIsLatest) scrollToBottom();
      return;
    }

    const endRect = bottomRef.current?.getBoundingClientRect();
    const userIsNearBottom = !endRect || endRect.top <= window.innerHeight + 240;
    if (!userIsNearBottom) return;

    const now = Date.now();
    if (now - lastStreamScrollAtRef.current < 240) return;
    lastStreamScrollAtRef.current = now;
    if (streamScrollFrameRef.current != null) return;
    streamScrollFrameRef.current = window.requestAnimationFrame(() => {
      streamScrollFrameRef.current = null;
      scrollToBottom();
    });
  }, [wsMessages.length, localMsgs.length, streaming, latestPendingActionIsLatest]);

  // Resolve pending action
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
  });
  const feedbackMutation = useMutation({
    mutationFn: ({
      msgId,
      rating,
    }: {
      msgId: string;
      rating: "up" | "down";
    }) => api.workspaces.chat.feedback(workspaceId, msgId, rating),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace-chat", workspaceId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace-runtime-evidence", workspaceId],
      });
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
      resolveMutation.mutate({ msgId, choice, note, payload, files });
    },
    [resolveMutation],
  );
  const handleTaskCompletionFeedback = useCallback(
    (msgId: string, rating: "up" | "down") => {
      feedbackMutation.mutate({ msgId, rating });
    },
    [feedbackMutation],
  );

  /* ── @mention detection on input change ── */
  function handleInputChange(val: string) {
    setInputDraft(val);
    // Throttle typing pings so other members see "X is typing…" live.
    if (wsConversationId && val.trim()) {
      const now = Date.now();
      if (now - lastTypingSentRef.current > 2500) {
        lastTypingSentRef.current = now;
        sendTyping(wsConversationId);
      }
    }
    if (agentList.length <= 1) {
      setMentionDropdownOpen(false);
      return;
    }
    const atIdx = val.lastIndexOf("@");
    if (atIdx >= 0 && (atIdx === 0 || /\s/.test(val[atIdx - 1]))) {
      setMentionQuery(val.substring(atIdx + 1));
      setMentionDropdownOpen(true);
      setMentionActiveIdx(0);
    } else {
      setMentionDropdownOpen(false);
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
      setMentionDropdownOpen(false);
      window.setTimeout(() => textareaRef.current?.focus(), 0);
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

  /* ── Select agent from @mention dropdown ── */
  function selectMention(agent: AgentInfo) {
    const atIdx = input.lastIndexOf("@");
    const cleaned = atIdx >= 0 ? input.substring(0, atIdx).trimEnd() : input;
    setInputDraft(cleaned);
    setSelectedEntrypointId("");
    setMentionAgent(agent);
    setMentionDropdownOpen(false);
    textareaRef.current?.focus();
  }

  /* ── Clear @mention ── */
  function clearMention() {
    setMentionAgent(null);
    textareaRef.current?.focus();
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

  /* ── Send message with SSE streaming ── */
  const handleSend = useCallback(
    async (
      rawText: string,
      attachments: AttachedItem[],
      manualSkills: ManualSkillItem[] = [],
    ) => {
      if (streamingRef.current) return;

      // Resolve @mention if typed inline
      const resolvedAgent = resolveInlineMention(rawText);
      let text = stripManualSkillTokens(rawText, manualSkills).trim();
      if (!text && attachments.length === 0 && manualSkills.length === 0)
        return;

      // Strip @mention text from message
      if (resolvedAgent && !mentionAgent) {
        const atIdx = text.lastIndexOf("@");
        if (atIdx >= 0) text = text.substring(0, atIdx).trimEnd();
      }

      const now = new Date().toISOString();
      const targetAgent = resolvedAgent;
      const starterBindingId =
        !targetAgent && manualSkills.length === 0 ? selectedEntrypointId : "";
      const targetName = targetAgent?.name || MANOR_AGENT_NAME;
      const targetColor = targetAgent
        ? agentColor(targetAgent.name)
        : "#1c1917";

      setInputDraft("");
      setMentionAgent(null);
      setMentionDropdownOpen(false);

      // Display content reflects attached file names
      const displayContent = [
        text,
        attachments.length > 0
          ? `[${t("component.workspace_chat.attached")}: ${attachments.map((f) => f.name).join(", ")}]`
          : "",
        manualSkills.length > 0
          ? `[${t("component.chat_input_footer.skill")}: ${manualSkills.map(manualSkillLabel).join(", ")}]`
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

      try {
        await startStream(
          () =>
            starterBindingId
              ? api.workspaces.chat.streamEntrypoint(
                  workspaceId,
                  starterBindingId,
                  text || "Use the attached context to run this workflow.",
                  conversationId,
                  {
                    files: localFiles.length > 0 ? localFiles : undefined,
                    documentIds: documentIds.length > 0 ? documentIds : undefined,
                  },
                )
              : api.chat.stream(
              text ||
                "Use the manually selected skill with the current conversation context.",
              conversationId,
              {
                workspaceId,
                workspaceContext: true,
                agentId: targetAgent?.id,
                threadRef,
                files: localFiles.length > 0 ? localFiles : undefined,
                documentIds: documentIds.length > 0 ? documentIds : undefined,
                manualSkillIds:
                  manualSkills.length > 0
                    ? manualSkills.map((skill) => skill.id)
                    : undefined,
              },
            ),
          conversationId,
          initialMessages,
          () => {},
          streamSessionKey,
        );
        setSelectedEntrypointId("");
      } catch {
        // startStream owns user-visible error state in the shared session.
      }

      await queryClient.invalidateQueries({
        queryKey: ["workspace-chat", workspaceId],
      });
      if (localFiles.length > 0) {
        await queryClient.invalidateQueries({
          queryKey: ["workspace-documents", workspaceId],
        });
        await queryClient.invalidateQueries({ queryKey: ["documents"] });
      }
    },
    [
      conversationId,
      localMsgs,
      workspaceId,
      threadRef,
      mentionAgent,
      agentList,
      queryClient,
      setInputDraft,
      startStream,
      streamSessionKey,
      selectedEntrypointId,
    ],
  );

  /* ── Key handling for @mention dropdown — runs *before* the footer's
   *  default keydown logic. preventDefault() to claim the event. */
  function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (mentionDropdownOpen && mentionFiltered.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionActiveIdx((i) => Math.min(i + 1, mentionFiltered.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMentionActiveIdx((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        selectMention(mentionFiltered[mentionActiveIdx]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMentionDropdownOpen(false);
        return;
      }
    }
  }

  useEffect(() => {
    if (!latestPendingActionId) return;
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
          !sorted.some((persisted) =>
            msg.role === "user"
              ? isDuplicatePersistedUserMessage(persisted, msg)
              : isDuplicatePersistedAssistantMessage(persisted, msg),
          ),
      ),
    [localMsgs, sorted],
  );

  const hasVisibleLocalAssistant = visibleLocalMsgs.some(
    (msg) => msg.role === "assistant",
  );
  const timelinePersistedMessages = useMemo(
    () =>
      hasVisibleLocalAssistant
        ? visibleSortedMessages.filter((msg) => !isRunningStreamPlaceholder(msg))
        : visibleSortedMessages,
    [hasVisibleLocalAssistant, visibleSortedMessages],
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

  const workspaceThreadLoading = workspaceMessagesLoading && timelineItems.length === 0;

  const chatScrollRailMarkers = useMemo<ChatScrollRailMarker[]>(
    () =>
      renderTimeline.map((item, index) => {
        if (item.kind === "activity-run") {
          const first = item.msgs[0];
          const preview = workspaceRailPreviewFromText(
            first ? formatUserFacingStructuredText(first.body) : "",
            "Action update",
          );
          return {
            id: item.key,
            tone: "action",
            title: preview.title,
            excerpt: preview.excerpt,
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
            tone: item.msg.role === "user" ? "user" : "assistant",
            title: preview.title,
            excerpt: preview.excerpt,
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
            ? msg.body
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
        return {
          id: item.key || msg.id || `workspace-chat-${index}`,
          tone:
            msg.author_kind === "user"
              ? "user"
              : msg.pending_action
                ? "action"
                : hasArtifacts
                  ? "artifact"
                  : isActivityMessage(msg)
                    ? "system"
                    : "assistant",
          title: preview.title,
          excerpt: preview.excerpt,
          fileKind: String(fileKindSource).slice(0, 4).toUpperCase(),
          fileLabel,
        };
      }),
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

  return (
    <div className="embedded-chat-root">
      {/* ── Header ── */}
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
                <span className="chat-typing-dots">
                  <span />
                  <span />
                  <span />
                </span>
                <span className="workspace-chat-subtitle">
                  {t("component.embedded_chat.replying")}</span>
              </div>
            ) : (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 2,
                }}
              >
                <span className="chat-status-dot chat-status-dot--online" />
                <span className="workspace-chat-subtitle">
                  {t("component.workspace_chat.type_to_mention_an_agent")}</span>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Agent chips (display only — DM via @mention) ── */}
      {workspaceAgentsLoading ? (
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
                seed={agent.id}
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
      )}

      {/* ── Messages ── */}
      <div className="embedded-chat-body-wrap workspace-chat-body-wrap chat-scroll-rail-host">
        <div
          ref={chatBodyRef}
          className={`embedded-chat-body ${
            timelineItems.length === 0 ? "embedded-chat-body--empty workspace-chat-body--empty" : ""
          }`}
        >
        {openActionCount > 0 && pendingActions.length > 0 && (
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
                  .map((msg) => pendingActionLabel(msg.pending_action))
                  .join(" · ")}
                {oldestPendingWaitLabel ? ` · ${oldestPendingWaitLabel}` : ""}
              </div>
            </div>
            <button
              type="button"
              className="workspace-pending-actions-jump"
              onClick={jumpToOldestPendingAction}
            >
              {t("component.workspace_chat.jump_to_oldest_action")}
            </button>
          </div>
        )}
        {workspaceThreadLoading ? (
          <ChatMessagesSkeleton rows={5} />
        ) : timelineItems.length === 0 && (
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
                {t("component.workspace_chat.workspace_group_chat")}</p>
              <p className="workspace-chat-empty-copy">
                {t("component.workspace_chat.talk_to")}{MANOR_AGENT_NAME} {t("component.workspace_chat.or_mention_a_specific_agent")}</p>
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
              return (
                <WsActivityLine
                  key={item.key}
                  markerId={item.key}
                  msg={item.msg}
                />
              );
            }
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
                actionResetToken={resolveMutation.failureCount}
                onFeedback={handleTaskCompletionFeedback}
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
          return (
            <div
              key={item.key}
              data-chat-scroll-marker-id={item.key}
              className={`chat-message-row ${msg.role === "user" ? "chat-message-row--user" : ""}`}
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
                  className={`chat-bubble ${msg.role === "user" ? "chat-bubble--user" : "chat-bubble--bot"}`}
                >
                  {localTools.length > 0 && (
                    <ToolCallList
                      tools={localTools}
                      keyPrefix={item.key}
                      subAgentRuns={delegatedRuns}
                    />
                  )}
                  {bubbleContent ? (
                    <ChatMarkdown
                      content={msg.role === "user" ? bubbleContent : formatUserFacingStructuredText(bubbleContent)}
                      isUser={msg.role === "user"}
                      streaming={isStreamingAssistant}
                    />
                  ) : isStreamingAssistant ? (
                    <span className="chat-streaming-cursor" />
                  ) : null}
                </div>
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
      {typingLabel && (
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
      <div className="chat-tip-bar">
        <InlineTips
          surface="workspace_chat"
          context={{ hasAgents: agentList.length > 0 }}
          placement="composer"
        />
      </div>

      <WorkspaceWorkflowRunHost
        workspaceId={workspaceId}
        groups={workflowRunGroups}
        onResolveMessage={handleResolve}
        resolveLoading={resolveMutation.isPending}
        resolveError={resolveMutation.error}
        resolveMessageId={resolveMutation.variables?.msgId || null}
        onRunChange={resolveMutation.reset}
      />

      {/* ── Footer / Input (shared composer with attach + voice + #) ── */}
      <ChatInputFooter
        value={input}
        onChange={handleInputChange}
        onKeyDown={handleKeyDown}
        enterToSend
        streaming={streaming}
        onSend={handleSend}
        onStop={() => stopStream(streamSessionKey)}
        modeSlot={
          !threadRef && workflowEntrypoints.length > 0 ? (
            <div
              className="workspace-workflow-starter"
              title={selectedEntrypoint?.description || t("component.workspace_chat.workflow_starter_help")}
            >
              <IconFlow size={14} aria-hidden="true" />
              <span className="sr-only">{t("component.workspace_chat.workflow_starter")}</span>
              <Select
                value={selectedEntrypointId}
                onChange={(value) => {
                  setSelectedEntrypointId(value);
                  if (value) {
                    setMentionAgent(null);
                    setMentionDropdownOpen(false);
                  }
                }}
                disabled={streaming}
                ariaLabel={t("component.workspace_chat.workflow_starter")}
                options={[
                  {
                    value: "",
                    label: t("component.workspace_chat.workflow_starter_auto"),
                  },
                  ...workflowEntrypoints.map((entrypoint: WorkspaceChatEntrypoint) => ({
                    value: entrypoint.binding_id,
                    label: entrypoint.title,
                  })),
                ]}
                dropdownMinWidth={260}
                style={{ flex: "1 1 auto", minWidth: 0 }}
                openButtonStyle={{
                  borderColor: "transparent",
                  background: "transparent",
                  boxShadow: "none",
                }}
              />
            </div>
          ) : undefined
        }
        placeholder={
          mentionAgent
            ? `Message ${mentionAgent.name}... / skill`
            : selectedEntrypoint?.placeholder
              ? selectedEntrypoint.placeholder
            : `Message ${MANOR_AGENT_NAME}... @ mention, # attach, / skill`
        }
        textareaRef={textareaRef}
        topSlot={
          mentionDropdownOpen && mentionFiltered.length > 0 ? (
            <div
              ref={mentionRef}
              className="workspace-chat-agent-menu"
            >
              {mentionFiltered.map((agent, idx) => (
                <div
                  key={agent.id}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    selectMention(agent);
                  }}
                  className="workspace-chat-agent-menu-item"
                  data-active={idx === mentionActiveIdx}
                  onMouseEnter={() => setMentionActiveIdx(idx)}
                >
                  <UserAvatar
                    name={agent.name}
                    avatarUrl={agent.avatar_url}
                    type="agent"
                    seed={agent.id}
                    size={24}
                  />
                  <span>
                    {agent.name}
                  </span>
                </div>
              ))}
            </div>
          ) : null
        }
        beforeTextarea={
          mentionAgent ? (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                background: "rgba(67,107,101,0.1)",
                color: "#436b65",
                fontSize: 11,
                fontWeight: 600,
                padding: "3px 6px 3px 3px",
                borderRadius: 8,
                whiteSpace: "nowrap",
                flexShrink: 0,
              }}
            >
              <UserAvatar
                name={mentionAgent.name}
                avatarUrl={mentionAgent.avatar_url}
                type="agent"
                seed={mentionAgent.id}
                size={18}
              />
              <span>@{mentionAgent.name}</span>
              <button
                type="button"
                aria-label={t("component.chat_input_footer.cancel_mention")}
                title={t("component.chat_input_footer.cancel_mention")}
                onClick={clearMention}
                style={{
                  cursor: "pointer",
                  border: "none",
                  background: "transparent",
                  color: "inherit",
                  opacity: 0.5,
                  fontSize: 13,
                  marginLeft: 2,
                  padding: 0,
                  lineHeight: 1,
                }}
              >
                {t("component.workspace_chat.and_times")}
              </button>
            </div>
          ) : null
        }
      />
    </div>
  );
}

/* ── Workspace event message row ── */

function WsMessageRow({
  markerId,
  msg,
  subToAgent,
  currentUserName,
  currentUserAvatar,
  currentUserId,
  onResolve,
  actionResetToken,
  onFeedback,
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
  actionResetToken?: number;
  onFeedback: (msgId: string, rating: "up" | "down") => void;
}) {
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
  const hasAssistantBlocks =
    !isUser &&
    Array.isArray(msg.assistant_blocks) &&
    msg.assistant_blocks.length > 0;
  const localCodingNotice = !isUser ? maybeLocalCodingRunNoticeForTools(visibleTools) : null;
  const bodyContent = localCodingNotice || msg.body;
  const showTaskCompletionActions =
    !isUser && isTaskCompletionMessage(msg) && Boolean(taskId);
  const feedback = taskCompletionFeedback(msg, currentUserId);
  const canRetryFailedProposalApproval = Boolean(
    msg.resolved_at &&
      msg.pending_action?.kind === PendingActionKind.APPROVE_PROPOSALS &&
      ["approve", "approve_all"].includes(
        String(msg.resolution?.choice || "").toLowerCase(),
      ) &&
      linkedTaskRefs.some((ref) => ref.status === "proposed"),
  );
  const displayPendingAction = workflowStarterAction(msg);

  return (
    <div
      id={`workspace-chat-message-${msg.id}`}
      data-chat-scroll-marker-id={markerId || msg.id}
      className={`chat-message-row ${isCurrentUser ? "chat-message-row--user" : ""}`}
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
          seed={agent.id}
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
          <span className="chat-message-time">
            {formatTime(msg.created_at)}
          </span>
        </div>

        <div
          className={`chat-bubble ${isCurrentUser ? "chat-bubble--user" : "chat-bubble--bot"} ${!isUser && msg.message_kind === "proposal" ? "chat-bubble--proposal" : ""} ${showTaskCompletionActions ? "chat-bubble--task-complete" : ""}`}
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
              content={bodyContent}
              keyPrefix={`ws-${msg.id}`}
              minimal
              collapseLongFinal={collapseBody}
              subAgentRuns={delegatedRuns}
            />
          )}

          {!hasAssistantBlocks && msg.message_kind === "workflow_activity" && (
            <WorkflowActivityContent msg={msg} subToAgent={subToAgent} />
          )}

          {!hasAssistantBlocks &&
            msg.message_kind !== "workflow_activity" &&
            bodyContent &&
            // An open approval renders a clean action card below; suppress the
            // raw governance body so internal keys/payloads never leak. Keep the
            // body for human_input — there the body IS the question/context and
            // the card is only an input box.
            !(
              msg.pending_action?.kind &&
              msg.pending_action.kind !== PendingActionKind.HUMAN_INPUT &&
              !msg.resolved_at
            ) && (
            msg.message_kind === "proposal" && !isUser ? (
              <ProposalMessageContent
                content={formatUserFacingStructuredText(bodyContent)}
                structured={structuredProposal(msg)}
              />
            ) : (
              <ExpandableWorkspaceMarkdown
                content={isUser ? bodyContent : formatUserFacingStructuredText(bodyContent)}
                isUser={isCurrentUser}
                collapsible={collapseBody}
              />
            )
          )}

          {((msg.pending_action && msg.pending_action.kind) ||
            (msg.resolved_at && msg.resolution)) && (
            <ChatActionCard
              action={displayPendingAction || { kind: "unknown" }}
              resolved={!!msg.resolved_at && !canRetryFailedProposalApproval}
              resolution={
                canRetryFailedProposalApproval ? null : msg.resolution
              }
              resolvedByName={
                msg.resolved_by_user_id
                  ? msg.resolved_by_user_id === currentUserId
                    ? t("component.workspace_chat.you")
                    : msg.resolved_by_user_name ||
                      msg.resolved_by_user_email ||
                      undefined
                  : undefined
              }
              currentUserName={currentUserName}
              resetToken={actionResetToken}
              onResolve={(choice, note, payload, files) =>
                onResolve(msg.id, choice, note, payload, files)
              }
            />
          )}

          {showTaskCompletionActions && (
            <div className="task-completion-actions">
              <Link
                className="task-completion-link"
                to={`/tasks/${taskId || ""}`}
              >
                {t("component.workspace_chat.view_task")}
              </Link>
              <div
                className={`task-completion-feedback ${feedback ? "task-completion-feedback--selected" : ""}`}
                aria-label={t("component.workspace_chat.task_completion_feedback")}
              >
                <button
                  type="button"
                  className={`task-completion-feedback-button ${feedback === "up" ? "is-selected" : ""}`}
                  title={t("component.workspace_chat.task_feedback_helpful")}
                  aria-label={t("component.workspace_chat.task_feedback_helpful")}
                  aria-pressed={feedback === "up"}
                  onClick={() => onFeedback(msg.id, "up")}
                >
                  <IconThumbUp size={14} />
                </button>
                <button
                  type="button"
                  className={`task-completion-feedback-button ${feedback === "down" ? "is-selected" : ""}`}
                  title={t("component.workspace_chat.task_feedback_not_helpful")}
                  aria-label={t("component.workspace_chat.task_feedback_not_helpful")}
                  aria-pressed={feedback === "down"}
                  onClick={() => onFeedback(msg.id, "down")}
                >
                  <IconThumbDown size={14} />
                </button>
              </div>
            </div>
          )}

          {!showTaskCompletionActions && linkedTaskRefs.length > 0 && (
            <div
              className={`task-reference-actions ${isCurrentUser ? "task-reference-actions--user" : ""}`}
              aria-label={t("component.workspace_chat.related_tasks")}
            >
              <span className="task-reference-label">
                {t("component.workspace_chat.related_tasks")}
              </span>
              <div className="task-reference-links">
                {linkedTaskRefs.slice(0, 5).map((ref, index) => (
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
      seed={agent.id}
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
}: {
  markerId?: string;
  msg: WsMessage;
}) {
  const text = activityLineText(msg);
  const failed = /failed|✗/i.test(msg.body || "");
  return (
    <div
      className="ws-activity-line-row"
      data-chat-scroll-marker-id={markerId || msg.id}
    >
      <span className="ws-activity-line">
        <span className="ws-activity-line-glyph" aria-hidden>
          {failed ? "✗" : "▸"}
        </span>
        <span className="ws-activity-line-text">{text}</span>
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

function structuredProposalItems(source: unknown): { kind: string; summary: string }[] {
  if (!Array.isArray(source)) return [];
  return source
    .filter((item): item is Record<string, any> => Boolean(item && typeof item === "object"))
    .map((item) => ({
      kind: formatUserFacingText(String(item.kind || "item").replace(/_/g, " ")),
      summary: formatUserFacingText(String(item.summary || "")),
    }))
    .filter((item) => item.summary || item.kind);
}

/** `meta.proposal` is written for every card; `pending_action.tasks` is the
 *  same list, kept for cards whose meta was pruned by an older writer. */
function structuredProposal(msg: WsMessage): StructuredProposal | null {
  const fromMeta = msg.meta?.proposal;
  if (fromMeta && typeof fromMeta === "object") return fromMeta as StructuredProposal;
  const fromAction = msg.pending_action?.tasks;
  if (Array.isArray(fromAction)) return { tasks: fromAction };
  return null;
}

function ProposalMessageContent({
  content,
  structured,
}: {
  content: string;
  structured?: StructuredProposal | null;
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
  const structuredItems = structuredProposalItems(structured?.items);
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
              return (
                <div
                  className="workspace-proposal-task"
                  key={task.task_id || `${task.title}-${index}`}
                >
                  <div className="workspace-proposal-task-rank">{index + 1}</div>
                  <div className="workspace-proposal-task-body">
                    <div className="workspace-proposal-task-title-row">
                      <span className="workspace-proposal-task-title">
                        {formatUserFacingText(task.title)}
                      </span>
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
                  </div>
                </div>
              );
            })}
            {legacyTasks.map((task, index) => (
              <div className="workspace-proposal-task" key={`${task.title}-${index}`}>
                <div className="workspace-proposal-task-rank">
                  {task.rank || index + 1}
                </div>
                <div className="workspace-proposal-task-body">
                  <div className="workspace-proposal-task-title-row">
                    <span className="workspace-proposal-task-title">{task.title}</span>
                    {task.impact && (
                      <span className="workspace-proposal-impact">{task.impact}</span>
                    )}
                  </div>
                  {task.detail && (
                    <p className="workspace-proposal-task-detail">{task.detail}</p>
                  )}
                </div>
              </div>
            ))}
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
              <div className="workspace-proposal-task" key={`${item.summary}-${index}`}>
                <div className="workspace-proposal-task-body">
                  <div className="workspace-proposal-task-title-row">
                    <Chip size="sm" variant="slate">{item.kind}</Chip>
                    <span className="workspace-proposal-task-title">{item.summary}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {proposal.notes.length > 0 && (
        <div className="workspace-proposal-section workspace-proposal-notes">
          <div className="workspace-proposal-section-title">
            {t(
              structuredTasks.length + legacyTasks.length > 0
                ? "component.workspace_chat.proposal_context"
                : "component.workspace_chat.blocking_reasons",
            )}
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
    </div>
  );
}

function ExpandableWorkspaceMarkdown({
  content,
  isUser,
  collapsible,
}: {
  content: string;
  isUser: boolean;
  collapsible: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const shouldCollapse = collapsible && isLongWorkspaceMessage(content);

  if (!shouldCollapse) {
    return <ChatMarkdown content={content} isUser={isUser} />;
  }

  if (isUser) {
    return (
      <CollapsibleSentMessage text={content}>
        <ChatMarkdown content={content} isUser />
      </CollapsibleSentMessage>
    );
  }

  return (
    <div className="workspace-markdown-expandable">
      <div
        className={`workspace-markdown-clamp${expanded ? "" : " workspace-markdown-clamp--collapsed"}`}
        style={
          expanded
            ? undefined
            : ({
                "--workspace-markdown-clamp-height": `${COLLAPSED_MESSAGE_MAX_HEIGHT}px`,
              } as CSSProperties)
        }
      >
        <ChatMarkdown content={content} isUser={isUser} />
        {!expanded && (
          <button
            type="button"
            className="workspace-markdown-expand-button"
            onClick={() => setExpanded(true)}
            aria-label={t("chat.show_more")}
          >
            <span aria-hidden="true">...</span>
          </button>
        )}
      </div>
    </div>
  );
}

function KindBadge({ kind }: { kind: string }) {
  const config: Record<string, { label: string; color: string; bg: string }> = {
    proposal: {
      label: t("component.workspace_chat.proposal"),
      color: "#5757a6",
      bg: "rgba(109,111,178,0.13)",
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
