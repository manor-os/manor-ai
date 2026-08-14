/**
 * Shared SSE stream processing for chat components.
 *
 * Used by: EmbeddedChat, FloatingChat
 */
import { useState, useEffect } from "react";
import { useUpgradeStore } from "../stores/upgrade";
import { api, normalizePlanLimitDetail, type PlanLimitDetail } from "./api";
import { t } from "./i18n";

/* ── Types ── */

export interface ToolCall {
  name: string;
  args?: unknown;
  arguments?: string;
  result?: string;
  status?: "pending" | "success" | "error";
  startedAt?: number;
  duration?: string;
  /** When a wrapper tool (invoke_skill, manor, code) is pending and a
   *  child sub-tool is running inside it, ``activeChild`` mirrors the
   *  child's name so the parent's spinner can render "invoke_skill →
   *  bash" instead of just "invoke_skill (loading)". Cleared when the
   *  child finishes. */
  activeChild?: string;
  lastChild?: string;
  childCount?: number;
  completedChildCount?: number;
}

export interface AssistantTextBlock {
  id: string;
  type: "text";
  phase?: "opening" | "progress" | "final" | string;
  after_step_seq?: number;
  text: string;
}

export interface AssistantProcessStep {
  id: string;
  seq?: number;
  kind?: "tool" | string;
  name: string;
  display_name?: string;
  display_key?: string;
  display_params?: Record<string, string | number>;
  status?: "running" | "pending" | "success" | "completed" | "error" | string;
  summary?: string;
  assistant_text?: string;
  arguments_preview?: string;
  result_preview?: string;
  duration_ms?: number;
}

export interface AssistantProcessBlock {
  id: string;
  type: "process";
  title?: string;
  note?: string;
  status?: "running" | "pending" | "completed" | "success" | "error" | string;
  default_collapsed?: boolean;
  duration_ms?: number;
  steps: AssistantProcessStep[];
}

export type AssistantBlock = AssistantTextBlock | AssistantProcessBlock;

// Tools that internally spawn nested agentic loops. While one of these
// is pending, sub-tool events that arrive should update the parent's
// ``activeChild`` so the spinner shows real progress instead of staying
// on a generic "loading" forever.
export const WRAPPER_TOOLS = new Set(["invoke_skill", "manor", "code"]);

export interface SubAgentEvent {
  run_id?: string;
  agent_id?: string;
  agent_subscription_id?: string;
  agent_name: string;
  agent_avatar?: string;
  service_key?: string;
  objective?: string;
  content: string;
  event_type?: string;
  status?: "running" | "completed" | "blocked" | "failed" | string;
  updated_at?: string;
  rounds?: number;
  tool_calls_made?: string[];
  tools?: {
    seq?: number;
    name: string;
    status?: "running" | "completed" | "error" | string;
    duration_ms?: number;
    arguments?: unknown;
  }[];
  tool?: {
    seq?: number;
    name: string;
    status?: "running" | "completed" | "error" | string;
    duration_ms?: number;
    arguments?: unknown;
  };
  stop_reason?: string;
  error?: string;
  timestamp?: string;
}

export interface HITLRequest {
  id: string;
  prompt: string;
  type?: "approval" | "input";
  action?: string;
  tool?: string;
  workspace?: { id?: string; name?: string };
  paths?: string[];
  content?: unknown;
  args_preview?: unknown;
  operation?: unknown;
  review?: unknown;
  review_title?: string;
  workflow?: {
    id?: string;
    name?: string;
    run_id?: string;
    url?: string;
  };
  node?: { id?: string; name?: string; type?: string };
  workflow_run_id?: string;
  workflow_step_id?: string;
  options?: string[];
  resolved?: boolean;
  resolution?: string;
}

export interface ChatMessageRef {
  type: string;
  id: string;
  title?: string;
  name?: string;
  [key: string]: unknown;
}

export interface ChatMessagePendingAction {
  kind: string;
  [key: string]: unknown;
}

export interface WorkflowResultReference {
  workflow_id: string;
  workflow_name: string;
  run_id: string;
  url?: string;
  status?: string;
  kind?: string;
  title?: string | null;
  summary?: string | null;
  publication_summary?: {
    count?: number;
    verified?: number;
    platforms?: string[];
  } | null;
}

export interface ChatMessage {
  id?: string;
  conversation_id?: string;
  role: "user" | "assistant";
  content: string;
  tool_calls?: ToolCall[];
  assistant_blocks?: AssistantBlock[];
  sub_agent_events?: SubAgentEvent[];
  hitl_requests?: HITLRequest[];
  workflow_result?: WorkflowResultReference;
  timestamp?: string;
  attachments?: {
    name: string;
    id?: string;
    type?: string;
    fileType?: string;
    mimeType?: string;
    previewUrl?: string;
    openUrl?: string;
    fsPath?: string;
  }[];
  mentions?: {
    id: string;
    type: "agent" | "user";
    name: string;
    subtitle?: string;
    avatarUrl?: string | null;
  }[];
  manualSkills?: { id: string; name: string; slug?: string }[];
  chatMode?: string;
  chatModePayload?: Record<string, unknown> | string;
  retryRequest?: {
    message: string;
    conversationId?: string;
    documentIds?: string[];
    agentId?: string;
    workspaceId?: string;
    manualSkillIds?: string[];
    chatMode?: string;
    chatModePayload?: Record<string, unknown>;
  };
  stream_error?: boolean;
  stop_reason?: string;
  limit_detail?: PlanLimitDetail;
  message_kind?: string | null;
  refs?: ChatMessageRef[] | null;
  meta?: Record<string, unknown> | null;
  pending_action?: ChatMessagePendingAction | null;
  resolved_at?: string | null;
  resolution?: Record<string, unknown> | null;
  updated_at?: string | null;
}

export function hasActivePersistedChatStream(messages: ChatMessage[]): boolean {
  const latestAssistant = [...messages]
    .reverse()
    .find((message) => message.role === "assistant");
  const status = latestAssistant?.meta?.stream_status;
  return status === "running" || status === "streaming";
}

/**
 * A chat run can outlive the browser's SSE connection. Keep visual running
 * state separate from transport state so navigation or a reconnect does not
 * make an active backend turn look complete.
 */
export function isChatRunActive(
  messages: ChatMessage[],
  transportStreaming: boolean,
): boolean {
  return transportStreaming || hasActivePersistedChatStream(messages);
}

/**
 * In-progress reply pushed over the WebSocket for a turn this tab is not
 * streaming (`chat_stream_snapshot`, published by chat_service).
 *
 * A personal conversation streams over the SSE body of the POST that started
 * it, so a reloaded page has no connection to a turn that is still running.
 * These carry the reply itself rather than a "go refetch" ping.
 */
export interface ChatStreamSnapshot {
  conversation_id?: string;
  message_id?: string;
  seq?: number;
  status?: string;
  content?: string;
  tool_calls?: unknown;
  assistant_blocks?: unknown;
}

export function isTerminalStreamSnapshot(snapshot: ChatStreamSnapshot): boolean {
  return snapshot.status !== "streaming";
}

/**
 * Which row of a transcript a snapshot is describing, or -1 for none of them.
 *
 * The persisted id is the only trustworthy answer. "The last assistant row" is
 * not: a turn started from another tab has an id this transcript has never
 * seen, and treating it as the tail row overwrites the previous turn's finished
 * answer — dragging that answer's attachments and approval card onto a reply
 * that is still being written.
 */
function streamSnapshotTargetIndex(
  messages: ChatMessage[],
  snapshot: ChatStreamSnapshot,
): number {
  if (snapshot.message_id) {
    const byId = messages.findIndex((message) => message.id === snapshot.message_id);
    if (byId >= 0) return byId;
  }
  // No id match. Only a trailing assistant row that carries no id of its own can
  // still be this turn — an optimistic placeholder, never a persisted reply.
  const tailIndex = messages.length - 1;
  const tail = messages[tailIndex];
  return tail && tail.role === "assistant" && !tail.id ? tailIndex : -1;
}

/**
 * Whether this snapshot describes a turn the transcript does not contain.
 *
 * True when a turn was started somewhere else: the reader is missing that
 * turn's user message, so the transcript needs a reload rather than a merge.
 */
export function streamSnapshotNeedsHistory(
  messages: ChatMessage[],
  snapshot: ChatStreamSnapshot,
): boolean {
  return streamSnapshotTargetIndex(messages, snapshot) < 0;
}

/**
 * Fold a snapshot into a transcript loaded from the API.
 *
 * The in-progress assistant row is normally already in that transcript — the
 * history endpoint only hides a running placeholder once a newer finished reply
 * exists — so this replaces in place. It appends only for a turn the transcript
 * has never seen, where the alternative is destroying a finished reply.
 *
 * A snapshot is a projection of the live turn, not of the stored row: it has no
 * attachments, hitl_requests, pending_action or message_kind. Those only settle
 * when the turn ends, which is why callers refetch on a terminal snapshot
 * instead of trusting it — see `isTerminalStreamSnapshot`.
 */
export function mergeChatStreamSnapshot(
  messages: ChatMessage[],
  snapshot: ChatStreamSnapshot,
): ChatMessage[] {
  const content = typeof snapshot.content === "string" ? snapshot.content : "";
  const toolCalls = parseToolCalls(snapshot.tool_calls);
  const blocks = Array.isArray(snapshot.assistant_blocks)
    ? (snapshot.assistant_blocks as AssistantBlock[])
    : undefined;

  const targetIndex = streamSnapshotTargetIndex(messages, snapshot);
  const previous = targetIndex >= 0 ? messages[targetIndex] : undefined;
  const merged: ChatMessage = {
    ...(previous || { role: "assistant" as const, content: "" }),
    role: "assistant",
    id: snapshot.message_id || previous?.id,
    content,
    tool_calls: toolCalls || previous?.tool_calls,
    assistant_blocks: blocks || previous?.assistant_blocks,
    meta: {
      ...(previous?.meta || {}),
      stream_status: isTerminalStreamSnapshot(snapshot) ? undefined : "streaming",
    },
  };

  if (targetIndex < 0) return [...messages, merged];
  const updated = [...messages];
  updated[targetIndex] = merged;
  return updated;
}

export function mergeSubAgentEvents(
  existing: SubAgentEvent[],
  rawEvent: Record<string, any>,
): SubAgentEvent[] {
  const incoming: SubAgentEvent = {
    ...rawEvent,
    agent_name: String(rawEvent.agent_name || rawEvent.name || "Sub-Agent"),
    content: String(rawEvent.content || ""),
  };
  const runId = String(incoming.run_id || "").trim();
  if (!runId) return [...existing, incoming];

  const index = existing.findIndex((event) => event.run_id === runId);
  const current: SubAgentEvent =
    index >= 0
      ? { ...existing[index] }
      : {
          run_id: runId,
          agent_name: incoming.agent_name,
          content: "",
          tools: [],
        };
  const tools = [...(current.tools || [])];
  if (incoming.tool?.name) {
    const toolIndex = tools.findIndex(
      (tool) =>
        incoming.tool?.seq != null &&
        tool.seq === incoming.tool.seq,
    );
    if (toolIndex >= 0) {
      tools[toolIndex] = { ...tools[toolIndex], ...incoming.tool };
    } else {
      tools.push(incoming.tool);
    }
  }

  const merged: SubAgentEvent = {
    ...current,
    ...incoming,
    tools: tools.slice(-20),
  };
  delete merged.tool;
  const next = [...existing];
  if (index >= 0) next[index] = merged;
  else next.push(merged);
  return next;
}

/* ── Helpers ── */

export type SetMessages = React.Dispatch<React.SetStateAction<ChatMessage[]>>;
export type SetConvId = React.Dispatch<React.SetStateAction<string | undefined>>;

export interface SSEHandlers {
  setMessages: SetMessages;
  setCurrentConvId: SetConvId;
}

export interface StreamProcessResult {
  error?: {
    message: string;
    persisted: boolean;
    messageId?: string;
  };
  messageId?: string;
  persisted?: boolean;
  stopReason?: string;
  limitDetail?: PlanLimitDetail;
}

const INTERNAL_FILE_PERMISSION_RE = /^\[File permission(?:\s+[^\]]*)?\]$/i;
const APPROVAL_RESOLUTION_RECEIPT_RE =
  /^[✓✗]\s*(?:Approved|Rejected|Cancelled|Resolved)(?:\s+—[\s\S]*)?$/i;

export function isInternalFilePermissionMessage(content: unknown): boolean {
  return typeof content === "string" && INTERNAL_FILE_PERMISSION_RE.test(content.trim());
}

/**
 * Historical personal-Chat approvals may contain both a resolved HITL card
 * and the Workspace Chat service's linked system receipt. The card is the
 * durable audit surface; rendering the receipt as another assistant bubble
 * repeats the same status without adding information.
 */
export function isRedundantApprovalResolutionReceipt(message: {
  role?: unknown;
  content?: unknown;
  message_kind?: unknown;
  refs?: unknown;
}): boolean {
  if (message.role !== "system" || message.message_kind !== "system") return false;
  if (
    typeof message.content !== "string" ||
    !APPROVAL_RESOLUTION_RECEIPT_RE.test(message.content.trim())
  ) {
    return false;
  }
  return (
    Array.isArray(message.refs) &&
    message.refs.some(
      (ref) =>
        ref != null &&
        typeof ref === "object" &&
        (ref as Record<string, unknown>).type === "message" &&
        Boolean((ref as Record<string, unknown>).id),
    )
  );
}

export function pendingHITLIds(messages: ChatMessage[]): string[] {
  return messages.flatMap((msg) =>
    (msg.hitl_requests || [])
      .filter((hitl) => !hitl.resolved)
      .map((hitl) => hitl.id)
      .filter(Boolean),
  );
}

export function mergeResolvedWorkflowMessage(
  messages: ChatMessage[],
  messageId: string,
  resolvedValue: unknown,
): ChatMessage[] {
  const resolved = resolvedValue && typeof resolvedValue === "object"
    ? resolvedValue as Partial<ChatMessage>
    : {};
  return messages.map((message) => (
    message.id === messageId
      ? {
          ...message,
          message_kind: resolved.message_kind ?? message.message_kind,
          refs: resolved.refs ?? message.refs,
          meta: resolved.meta ?? message.meta,
          pending_action: Object.prototype.hasOwnProperty.call(
            resolved,
            "pending_action",
          )
            ? resolved.pending_action
            : message.pending_action,
          resolved_at: resolved.resolved_at || new Date().toISOString(),
          resolution: resolved.resolution ?? message.resolution,
          updated_at: resolved.updated_at ?? message.updated_at,
        }
      : message
  ));
}

export async function resolveGlobalWorkflowMessageAction(
  messageId: string,
  choice: string,
  note?: string,
  payload?: Record<string, unknown>,
  files?: File[],
) {
  const uploadedAttachments = files?.length
    ? await Promise.all(files.map(async (file) => {
        const document = await api.documents.upload(file);
        if (!document?.id) throw new Error(`Failed to upload ${file.name}`);
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
  return api.chat.resolveAction(messageId, choice, note, resolvedPayload);
}

export function hitlActionTranscriptText(action: string): string {
  const normalized = String(action || "").trim().toLowerCase();
  if (normalized === "approve" || normalized === "always_approve") {
    return "Approved the requested action.";
  }
  if (normalized === "reject") {
    return "Rejected the requested action.";
  }
  if (normalized === "revise") {
    return "Requested revisions to the Workflow output.";
  }
  if (normalized === "cancel") {
    return "Cancelled the Workflow action.";
  }
  return "Responded to the approval request.";
}

export function normalizeToolResult(result: unknown): string | undefined {
  if (result == null) return undefined;
  if (typeof result === "string") return result;
  if (typeof result === "number" || typeof result === "boolean") return String(result);
  try {
    const serialized = JSON.stringify(result);
    return serialized === undefined ? String(result) : serialized;
  } catch {
    return String(result);
  }
}

function normalizeMessageAttachments(value: unknown): ChatMessage["attachments"] | undefined {
  if (!Array.isArray(value)) return undefined;
  const attachments = value
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      name: String(item.name || item.filename || item.title || "").trim(),
      id: item.id == null ? undefined : String(item.id),
      type: item.type == null ? undefined : String(item.type),
      fileType: item.fileType == null ? undefined : String(item.fileType),
      mimeType: item.mimeType == null ? undefined : String(item.mimeType),
      previewUrl: item.previewUrl == null ? undefined : String(item.previewUrl),
      openUrl: item.openUrl == null
        ? (item.open_url == null ? undefined : String(item.open_url))
        : String(item.openUrl),
      fsPath: item.fsPath == null
        ? (item.fs_path == null ? undefined : String(item.fs_path))
        : String(item.fsPath),
    }))
    .filter((item) => item.name);
  return attachments.length ? attachments : undefined;
}

function normalizeToolArguments(args: unknown): string | undefined {
  if (args == null) return undefined;
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

export function inferToolStatus(result: unknown): ToolCall["status"] {
  const text = normalizeToolResult(result);
  if (!text) return "success";
  if (text.startsWith("Tool error")) return "error";
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      const status = String((parsed as any).status || "").toLowerCase();
      if (
        status === "error" ||
        status === "failed" ||
        status === "timeout" ||
        (parsed as any).error
      ) {
        return "error";
      }
    }
  } catch {
    // Non-JSON results are ordinary successful tool output unless they
    // use the explicit Tool error prefix handled above.
  }
  return "success";
}

export function settlePendingAssistantToolCalls(
  messages: ChatMessage[],
  status: ToolCall["status"],
): ChatMessage[] {
  const updated = [...messages];
  const last = updated[updated.length - 1];
  if (!last || last.role !== "assistant" || !last.tool_calls?.length) {
    return messages;
  }
  let changed = false;
  const tool_calls = last.tool_calls.map((tool) => {
    const toolStatus = tool.status || (tool.result ? "success" : "pending");
    if (toolStatus !== "pending") return tool;
    changed = true;
    return {
      ...tool,
      status,
      activeChild: undefined,
    };
  });
  if (!changed) return messages;
  updated[updated.length - 1] = { ...last, tool_calls };
  return updated;
}

export function markAssistantProcessBlocksSummarizing(blocks: AssistantBlock[] | undefined): AssistantBlock[] | undefined {
  if (!Array.isArray(blocks) || blocks.length === 0) return blocks;
  let changed = false;
  const next = blocks.map((block) => {
    if (block.type !== "process") return block;
    const steps = (block.steps || []).map((step) => {
      const status = step.status === "running" || step.status === "pending" ? "success" : step.status;
      if (status !== step.status) changed = true;
      return status === step.status ? step : { ...step, status };
    });
    if (
      block.status === "completed" &&
      block.default_collapsed === true &&
      steps === block.steps
    ) {
      return block;
    }
    changed = true;
    return {
      ...block,
      status: "completed",
      default_collapsed: true,
      steps,
    };
  });
  return changed ? next : blocks;
}

export function markAssistantProcessBlocksStopped(blocks: AssistantBlock[] | undefined): AssistantBlock[] | undefined {
  if (!Array.isArray(blocks) || blocks.length === 0) return blocks;
  let changed = false;
  const next = blocks.map((block) => {
    if (block.type !== "process") return block;
    const steps = (block.steps || []).map((step) => {
      const status = step.status === "running" || step.status === "pending" ? "error" : step.status;
      if (status !== step.status) changed = true;
      return status === step.status ? step : { ...step, status };
    });
    if (
      block.status === "error" &&
      block.default_collapsed === true &&
      steps === block.steps
    ) {
      return block;
    }
    changed = true;
    return {
      ...block,
      status: "error",
      default_collapsed: true,
      steps,
    };
  });
  return changed ? next : blocks;
}

export function settlePendingAssistantProcess(messages: ChatMessage[]): ChatMessage[] {
  const withSettledTools = settlePendingAssistantToolCalls(messages, "error");
  const updated = [...withSettledTools];
  const last = updated[updated.length - 1];
  if (!last || last.role !== "assistant" || !last.assistant_blocks?.length) {
    return withSettledTools;
  }
  const assistant_blocks = markAssistantProcessBlocksStopped(last.assistant_blocks);
  if (assistant_blocks === last.assistant_blocks) return withSettledTools;
  updated[updated.length - 1] = { ...last, assistant_blocks };
  return updated;
}

export function formatPersistedStreamErrorMessage(message: unknown): string {
  const detail = normalizeToolResult(message)?.trim() || t("lib.chat_stream.unknown_error");
  return t("lib.chat_stream.request_failed_with_detail").replace("{detail}", detail);
}

function formatCreditLimitMessage(detail: PlanLimitDetail): string {
  return detail.message || t("component.upgrade_prompt.default_message");
}

// Paint interval. Combined with the slice sizes below this lands around
// 55-150 chars/sec — brisk enough that a long answer never feels stuck,
// slow enough to read along. It used to be 18ms with slices that GREW with
// the backlog, so a long reply painted at ~1300 chars/sec: the answers that
// most need reading arrived fastest.
const TYPEWRITER_TICK_MS = 36;
const TOOL_START_DISPLAY_DELAY_MS = 180;

function nextTypewriterSlice(text: string): [string, string] {
  const chars = Array.from(text);
  if (chars.length === 0) return ["", ""];

  // The backlog still nudges the pace — a burst should not take minutes to
  // drain — but gently, and the ceiling stays inside reading speed rather
  // than dumping the screen.
  const count =
    chars.length > 1000 ? 6 :
      chars.length > 400 ? 4 :
        chars.length > 160 ? 3 :
          2;
  let end = Math.min(count, chars.length);

  // Keep a trailing run of spaces/newlines with the same paint so markdown
  // does not look like it stalls between words.
  while (end < chars.length && end < count + 8 && /\s/.test(chars[end])) {
    end += 1;
  }

  return [chars.slice(0, end).join(""), chars.slice(end).join("")];
}

/**
 * Parse persisted tool_calls from DB message into ToolCall[].
 * Handles both array [{name, result}] and object {name: result} shapes.
 */
export function parseToolCalls(raw: any): ToolCall[] | undefined {
  if (!raw) return undefined;
  const persistedDuration = (tc: any): string | undefined => {
    if (typeof tc?.duration === "string") return tc.duration;
    if (typeof tc?.duration_ms === "number" && Number.isFinite(tc.duration_ms)) {
      return (tc.duration_ms / 1000).toFixed(2) + "s";
    }
    return undefined;
  };
  if (Array.isArray(raw)) {
    return raw.map((tc: any) => ({
      name: tc.name || "tool",
      arguments: normalizeToolArguments(tc.arguments ?? tc.args),
      result: normalizeToolResult(tc.result),
      status: tc.status || inferToolStatus(tc.result),
      duration: persistedDuration(tc),
    }));
  }
  return Object.entries(raw).map(([name, result]) => ({
    name,
    result: normalizeToolResult(result),
    status: inferToolStatus(result),
  }));
}

/**
 * Process an SSE stream from the chat endpoint, updating messages state.
 * Handles: text tokens, tool_call start/end, sub_agent events, HITL requests.
 * Pass an AbortSignal to allow cancellation mid-stream.
 */
export async function processSSEStream(
  response: Response,
  { setMessages, setCurrentConvId }: SSEHandlers,
  currentConvId: string | undefined,
  signal?: AbortSignal,
): Promise<StreamProcessResult> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No reader available");

  // If aborted, cancel the reader so the connection closes immediately.
  signal?.addEventListener("abort", () => reader.cancel(), { once: true });

  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "";
  let resetBeforeNextText = false;
  let summaryStarted = false;
  const result: StreamProcessResult = {};
  let streamConversationId = currentConvId;
  let streamMessageId: string | undefined;
  let queuedText = "";
  let typewriterTimer: ReturnType<typeof setTimeout> | undefined;
  let typewriterIdleResolve: (() => void) | undefined;
  let pendingToolSeq = 0;
  type PendingToolStart = {
    id: number;
    key: string;
    tc: any;
    timer: ReturnType<typeof setTimeout>;
    startedAt: number;
    visible: boolean;
  };
  const pendingToolStarts: PendingToolStart[] = [];

  const appendVisibleText = (chunk: string, shouldReset = false) => {
    setMessages((prev) => {
      const updated = [...prev];
      let last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") {
        updated.push({
          role: "assistant",
          content: "",
          timestamp: new Date().toISOString(),
        });
        last = updated[updated.length - 1]!;
      }
      if (last.role === "assistant") {
        updated[updated.length - 1] = {
          ...last,
          content: shouldReset
            ? chunk
            : `${normalizeToolResult(last.content) || ""}${chunk}`,
        };
      }
      return updated;
    });
  };

  const notifyTypewriterIdle = () => {
    if (!queuedText && !typewriterTimer && typewriterIdleResolve) {
      const resolve = typewriterIdleResolve;
      typewriterIdleResolve = undefined;
      resolve();
    }
  };

  const scheduleTypewriter = () => {
    if (typewriterTimer || !queuedText) return;
    typewriterTimer = setTimeout(() => {
      typewriterTimer = undefined;
      const [chunk, rest] = nextTypewriterSlice(queuedText);
      queuedText = rest;
      if (chunk) appendVisibleText(chunk);
      if (queuedText) scheduleTypewriter();
      notifyTypewriterIdle();
    }, TYPEWRITER_TICK_MS);
  };

  const enqueueText = (token: string, shouldReset: boolean) => {
    if (shouldReset) {
      queuedText = "";
      if (typewriterTimer) {
        clearTimeout(typewriterTimer);
        typewriterTimer = undefined;
      }
      appendVisibleText("", true);
    }
    queuedText += token;
    scheduleTypewriter();
  };

  const waitForTypewriterIdle = async () => {
    if (!queuedText && !typewriterTimer) return;
    await new Promise<void>((resolve) => {
      typewriterIdleResolve = resolve;
      scheduleTypewriter();
    });
  };

  const toolEventKey = (tc: any) =>
    `${tc?.name || "tool"}\u0000${normalizeToolArguments(tc?.arguments) || ""}`;

  const findPendingToolStart = (tc: any) => {
    const key = toolEventKey(tc);
    return (
      pendingToolStarts.find((entry) => entry.key === key) ||
      pendingToolStarts.find((entry) => entry.tc?.name === tc?.name)
    );
  };

  const removePendingToolStart = (entry: PendingToolStart) => {
    const idx = pendingToolStarts.findIndex((item) => item.id === entry.id);
    if (idx >= 0) pendingToolStarts.splice(idx, 1);
  };

  const applyToolCall = (
    tc: any,
    statusOverride?: ToolCall["status"],
    startedAtOverride?: number,
  ) => {
    const resultText = normalizeToolResult(tc.result);
    const status =
      statusOverride || tc.status || (resultText ? "success" : "pending");
    const incomingArguments = normalizeToolArguments(tc.arguments);
    setMessages((prev) => {
      const updated = [...prev];
      let last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") {
        updated.push({
          role: "assistant",
          content: "",
          tool_calls: [],
          timestamp: new Date().toISOString(),
        });
        last = updated[updated.length - 1]!;
      }
      if (last.role === "assistant") {
        const existing = [...(last.tool_calls || [])];

        // Find a still-pending wrapper (invoke_skill / manor / code)
        // anywhere in the list — if present, sub-tool events that
        // arrive should update its ``activeChild`` so the user sees
        // "invoke_skill → bash" instead of a stuck spinner.
        let pendingWrapperIdx = -1;
        for (let k = existing.length - 1; k >= 0; k--) {
          const t = existing[k];
          if (t.status === "pending" && WRAPPER_TOOLS.has(t.name)) {
            pendingWrapperIdx = k;
            break;
          }
        }
        const isChildOfWrapper =
          pendingWrapperIdx >= 0 && !WRAPPER_TOOLS.has(tc.name);

        if (status === "success" || status === "error") {
          let idx = -1;
          if (incomingArguments) {
            for (let k = existing.length - 1; k >= 0; k--) {
              if (
                existing[k].name === tc.name &&
                existing[k].status === "pending" &&
                existing[k].arguments === incomingArguments
              ) {
                idx = k;
                break;
              }
            }
          }
          for (let k = existing.length - 1; k >= 0; k--) {
            if (idx >= 0) break;
            if (existing[k].name === tc.name && existing[k].status === "pending") { idx = k; break; }
          }
          if (idx < 0 && WRAPPER_TOOLS.has(tc.name)) {
            for (let k = existing.length - 1; k >= 0; k--) {
              const candidate = existing[k];
              if (
                candidate.name === tc.name &&
                candidate.status === "success" &&
                (!incomingArguments || candidate.arguments === incomingArguments) &&
                !!normalizeToolResult(candidate.result)?.includes('"status": "delegated"')
              ) {
                idx = k;
                break;
              }
            }
          }
          const startedAt = existing[idx]?.startedAt || startedAtOverride;
          const elapsed = startedAt
            ? ((Date.now() - startedAt) / 1000).toFixed(1) + "s"
            : undefined;
          // Server may now include duration_ms — prefer that
          // (real wall-clock from agentic_loop) over the
          // client-side timer which loses time spent in queues.
          const dur = (tc as any).duration_ms
            ? ((tc as any).duration_ms / 1000).toFixed(2) + "s"
            : elapsed;
          if (idx >= 0) {
            existing[idx] = {
              ...existing[idx],
              arguments: incomingArguments || existing[idx].arguments,
              result: resultText,
              status,
              duration: dur,
            };
          } else {
            existing.push({
              name: tc.name || "tool",
              arguments: incomingArguments,
              result: resultText,
              status,
              duration: dur,
            });
          }
          // Child finished — clear the wrapper's activeChild
          // marker if it was pointing at this tool.
          if (
            isChildOfWrapper &&
            existing[pendingWrapperIdx]?.activeChild === tc.name
          ) {
            existing[pendingWrapperIdx] = {
              ...existing[pendingWrapperIdx],
              activeChild: undefined,
              lastChild: tc.name,
              completedChildCount:
                (existing[pendingWrapperIdx].completedChildCount || 0) + 1,
            };
          }
        } else {
          // Pending event — push as own card AND mirror onto the
          // wrapper's activeChild if this is a sub-tool.
          existing.push({
            name: tc.name || "tool",
            arguments: incomingArguments,
            status: "pending",
            startedAt: startedAtOverride || Date.now(),
          });
          if (isChildOfWrapper) {
            existing[pendingWrapperIdx] = {
              ...existing[pendingWrapperIdx],
              activeChild: tc.name,
              lastChild: tc.name,
              childCount: (existing[pendingWrapperIdx].childCount || 0) + 1,
            };
          }
        }
        updated[updated.length - 1] = { ...last, tool_calls: existing };
      }
      return updated;
    });
  };

  const applyAssistantBlocks = (blocks: unknown) => {
    if (!Array.isArray(blocks) || blocks.length === 0) return;
    const nextBlocks = summaryStarted
      ? markAssistantProcessBlocksSummarizing(blocks as AssistantBlock[]) || blocks
      : blocks;
    setMessages((prev) => {
      const updated = [...prev];
      let last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") {
        updated.push({
          role: "assistant",
          content: "",
          assistant_blocks: nextBlocks as AssistantBlock[],
          timestamp: new Date().toISOString(),
        });
        return updated;
      }
      if (last.role === "assistant") {
        updated[updated.length - 1] = {
          ...last,
          assistant_blocks: nextBlocks as AssistantBlock[],
        };
      }
      return updated;
    });
  };

  const markSummaryStarted = () => {
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (!last || last.role !== "assistant" || !Array.isArray(last.assistant_blocks)) {
        return prev;
      }
      const assistantBlocks = markAssistantProcessBlocksSummarizing(last.assistant_blocks);
      if (assistantBlocks === last.assistant_blocks) return prev;
      updated[updated.length - 1] = {
        ...last,
        assistant_blocks: assistantBlocks,
      };
      return updated;
    });
  };

  const queueToolStart = (tc: any) => {
    const entry: PendingToolStart = {
      id: ++pendingToolSeq,
      key: toolEventKey(tc),
      tc,
      startedAt: Date.now(),
      visible: false,
      timer: setTimeout(() => {
        entry.visible = true;
        applyToolCall(entry.tc, "pending", entry.startedAt);
      }, TOOL_START_DISPLAY_DELAY_MS),
    };
    pendingToolStarts.push(entry);
  };

  const settleToolCall = (tc: any, status: ToolCall["status"]) => {
    const pending = findPendingToolStart(tc);
    if (pending) {
      clearTimeout(pending.timer);
      removePendingToolStart(pending);
      applyToolCall(tc, status, pending.startedAt);
      return;
    }
    applyToolCall(tc, status);
  };

  const flushPendingToolStarts = () => {
    for (const entry of [...pendingToolStarts]) {
      clearTimeout(entry.timer);
      removePendingToolStart(entry);
      if (!entry.visible) applyToolCall(entry.tc, "pending", entry.startedAt);
    }
  };

  const clearPendingToolStarts = () => {
    for (const entry of [...pendingToolStarts]) {
      clearTimeout(entry.timer);
      removePendingToolStart(entry);
    }
  };

  const clearQueuedText = () => {
    queuedText = "";
    if (typewriterTimer) {
      clearTimeout(typewriterTimer);
      typewriterTimer = undefined;
    }
    notifyTypewriterIdle();
  };

  const tagLastAssistantMessage = (messageId: unknown) => {
    const id = typeof messageId === "string" ? messageId.trim() : "";
    if (!id) return;
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") return prev;
      updated[updated.length - 1] = { ...last, id };
      return updated;
    });
  };

  const normalizeEventId = (value: unknown): string | undefined => {
    if (typeof value !== "string") return undefined;
    const trimmed = value.trim();
    return trimmed || undefined;
  };

  const isForeignStreamEvent = (parsed: Record<string, unknown>) => {
    const eventConversationId = normalizeEventId(parsed.conversation_id);
    const eventMessageId = normalizeEventId(parsed.message_id);
    if (eventConversationId && streamConversationId && eventConversationId !== streamConversationId) {
      return true;
    }
    if (eventMessageId && streamMessageId && eventMessageId !== streamMessageId) {
      return true;
    }
    return false;
  };

  while (true) {
    if (signal?.aborted) break;
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("event: ")) {
        currentEvent = line.slice(7).trim();
        continue;
      }
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (data === "[DONE]") continue;
      try {
        const parsed = JSON.parse(data);

        if (isForeignStreamEvent(parsed)) {
          continue;
        }

        if (currentEvent === "error") {
          const message = parsed.message || parsed.error || "Chat stream failed";
          const persisted = Boolean(parsed.persisted || parsed.message_id);
          result.error = {
            message: String(message),
            persisted,
            messageId: parsed.message_id,
          };
          setMessages((prev) => {
            const updated = [...prev];
            const last = updated[updated.length - 1];
            if (!last || last.role !== "assistant") {
              updated.push({
                role: "assistant",
                content: persisted ? formatPersistedStreamErrorMessage(message) : String(message),
                stream_error: true,
                timestamp: new Date().toISOString(),
              });
            } else {
              updated[updated.length - 1] = {
                ...last,
                content: persisted ? formatPersistedStreamErrorMessage(message) : String(message),
                stream_error: true,
              };
            }
            return updated;
          });
          continue;
        }

        if (parsed.conversation_id && !currentConvId) {
          setCurrentConvId(parsed.conversation_id);
          currentConvId = parsed.conversation_id;
          streamConversationId = parsed.conversation_id;
        } else if (parsed.conversation_id && !streamConversationId) {
          streamConversationId = parsed.conversation_id;
        }

        if (Array.isArray(parsed.assistant_blocks)) {
          applyAssistantBlocks(parsed.assistant_blocks);
        }

        if (currentEvent === "stream_start") {
          streamMessageId = normalizeEventId(parsed.message_id) || streamMessageId;
          tagLastAssistantMessage(parsed.message_id);
          continue;
        }

        if (currentEvent === "text_reset") {
          resetBeforeNextText = true;
          clearQueuedText();
          continue;
        }

        if (currentEvent === "summary_start") {
          summaryStarted = true;
          resetBeforeNextText = true;
          clearQueuedText();
          markSummaryStarted();
          continue;
        }

        if (currentEvent === "process_note") {
          continue;
        }

        if (currentEvent === "stream_end") {
          streamMessageId = normalizeEventId(parsed.message_id) || streamMessageId;
          if (parsed.message_id) result.messageId = String(parsed.message_id);
          if (typeof parsed.persisted === "boolean") result.persisted = parsed.persisted;
          tagLastAssistantMessage(parsed.message_id);
          const attachments = normalizeMessageAttachments(parsed.attachments);
          if (attachments) {
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (!last || last.role !== "assistant") {
                updated.push({
                  role: "assistant",
                  content: "",
                  attachments,
                  timestamp: new Date().toISOString(),
                });
              } else {
                updated[updated.length - 1] = { ...last, attachments };
              }
              return updated;
            });
          }
          const stopReason = String(parsed.stop_reason || "");
          if (stopReason === "credit_exhausted") {
            const detail = normalizePlanLimitDetail(
              parsed.limit_detail || parsed.detail,
              parsed.error || t("component.upgrade_prompt.default_message"),
            );
            result.stopReason = stopReason;
            result.limitDetail = detail;
            useUpgradeStore.getState().show(detail);
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (!last || last.role !== "assistant") {
                updated.push({
                  role: "assistant",
                  content: formatCreditLimitMessage(detail),
                  stop_reason: stopReason,
                  limit_detail: detail,
                  timestamp: new Date().toISOString(),
                });
              } else {
                updated[updated.length - 1] = {
                  ...last,
                  content: formatCreditLimitMessage(detail),
                  stop_reason: stopReason,
                  limit_detail: detail,
                };
              }
              return updated;
            });
          }
          // stream_end is the protocol terminal event. Some deployments keep
          // the HTTP response open briefly after it, so waiting for EOF leaves
          // the composer disabled despite a completed assistant turn.
          flushPendingToolStarts();
          setMessages((prev) => settlePendingAssistantToolCalls(
            prev,
            stopReason ? "error" : "success",
          ));
          markSummaryStarted();
          void reader.cancel();
          await waitForTypewriterIdle();
          return result;
        }

        const token = normalizeToolResult(parsed.text_delta ?? parsed.token ?? parsed.content) || "";
        if (token) {
          const shouldReset = resetBeforeNextText;
          resetBeforeNextText = false;
          enqueueText(token, shouldReset);
        }

        if (parsed.tool_call) {
          const tc = parsed.tool_call;
          const result = normalizeToolResult(tc.result);
          const status = tc.status || (result ? "success" : "pending");
          if (status === "pending") {
            queueToolStart(tc);
          } else {
            settleToolCall(tc, status);
          }
        }

        if (parsed.sub_agent) {
          setMessages((prev) => {
            const updated = [...prev];
            let last = updated[updated.length - 1];
            if (!last || last.role !== "assistant") {
              updated.push({
                role: "assistant",
                content: "",
                sub_agent_events: [],
                timestamp: new Date().toISOString(),
              });
              last = updated[updated.length - 1]!;
            }
            if (last.role === "assistant") {
              const existing = last.sub_agent_events || [];
              updated[updated.length - 1] = {
                ...last,
                sub_agent_events: mergeSubAgentEvents(existing, {
                  ...parsed.sub_agent,
                  agent_name:
                    parsed.sub_agent.agent_name ||
                    parsed.sub_agent.name ||
                    "Sub-Agent",
                  timestamp:
                    parsed.sub_agent.updated_at ||
                    parsed.sub_agent.timestamp ||
                    new Date().toISOString(),
                }),
              };
            }
            return updated;
          });
        }

        if (parsed.hitl) {
          setMessages((prev) => {
            const updated = [...prev];
            let last = updated[updated.length - 1];
            if (!last || last.role !== "assistant") {
              updated.push({
                role: "assistant",
                content: "",
                hitl_requests: [],
                timestamp: new Date().toISOString(),
              });
              last = updated[updated.length - 1]!;
            }
            if (last.role === "assistant") {
              const existing = last.hitl_requests || [];
              updated[updated.length - 1] = {
                ...last,
                hitl_requests: [
                  ...existing,
                  {
                    id: parsed.hitl.id || String(Date.now()),
                    prompt: parsed.hitl.prompt || "",
                    type: parsed.hitl.type || "approval",
                    action: parsed.hitl.action,
                    tool: parsed.hitl.tool,
                    workspace: parsed.hitl.workspace,
                    paths: Array.isArray(parsed.hitl.paths) ? parsed.hitl.paths : undefined,
                    content: parsed.hitl.content,
                    args_preview: parsed.hitl.args_preview,
                    operation: parsed.operation,
                    options: Array.isArray(parsed.hitl.options) ? parsed.hitl.options : undefined,
                  },
                ],
              };
            }
            return updated;
          });
        }
      } catch {
        /* skip unparseable */
      }
    }
  }
  if (signal?.aborted) {
    clearPendingToolStarts();
    clearQueuedText();
  } else {
    flushPendingToolStarts();
  }
  await waitForTypewriterIdle();
  return result;
}

/**
 * Debounce hook — shared between chat components.
 */
export function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}
