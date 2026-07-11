/**
 * Shared SSE stream processing for chat components.
 *
 * Used by: EmbeddedChat, FloatingChat
 */
import { useState, useEffect } from "react";
import { useUpgradeStore } from "../stores/upgrade";
import type { PlanLimitDetail } from "./api";
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
  agent_name: string;
  agent_avatar?: string;
  content: string;
  event_type?: string;
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
  options?: string[];
  resolved?: boolean;
  resolution?: string;
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
  timestamp?: string;
  attachments?: {
    name: string;
    id?: string;
    type?: string;
    fileType?: string;
    mimeType?: string;
    previewUrl?: string;
  }[];
  mentions?: { id: string; type: "agent" | "user"; name: string; subtitle?: string }[];
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

export function isInternalFilePermissionMessage(content: unknown): boolean {
  return typeof content === "string" && INTERNAL_FILE_PERMISSION_RE.test(content.trim());
}

export function pendingHITLIds(messages: ChatMessage[]): string[] {
  return messages.flatMap((msg) =>
    (msg.hitl_requests || [])
      .filter((hitl) => !hitl.resolved)
      .map((hitl) => hitl.id)
      .filter(Boolean),
  );
}

export function hitlActionTranscriptText(action: string): string {
  const normalized = String(action || "").trim().toLowerCase();
  if (normalized === "approve" || normalized === "always_approve") {
    return "Approved the requested action.";
  }
  if (normalized === "reject") {
    return "Rejected the requested action.";
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

export function formatPersistedStreamErrorMessage(message: unknown): string {
  const detail = normalizeToolResult(message)?.trim() || t("lib.chat_stream.unknown_error");
  return t("lib.chat_stream.request_failed_with_detail").replace("{detail}", detail);
}

function normalizePlanLimitDetail(detail: unknown, fallback: string): PlanLimitDetail {
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    return {
      message: String(d.message || fallback),
      limit: typeof d.limit === "number" ? d.limit : null,
      current: typeof d.current === "number" ? d.current : null,
      plan: String(d.plan || "current"),
    };
  }
  return {
    message: typeof detail === "string" && detail ? detail : fallback,
    limit: null,
    current: null,
    plan: "current",
  };
}

function formatCreditLimitMessage(detail: PlanLimitDetail): string {
  return detail.message || t("component.upgrade_prompt.default_message");
}

const TYPEWRITER_TICK_MS = 18;
const TOOL_START_DISPLAY_DELAY_MS = 180;

function nextTypewriterSlice(text: string): [string, string] {
  const chars = Array.from(text);
  if (chars.length === 0) return ["", ""];

  const count =
    chars.length > 1000 ? 24 :
      chars.length > 400 ? 14 :
        chars.length > 160 ? 8 :
          3;
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
          continue;
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
                sub_agent_events: [
                  ...existing,
                  {
                    agent_name: parsed.sub_agent.name || "Sub-Agent",
                    content: parsed.sub_agent.content || "",
                    event_type: parsed.sub_agent.event_type,
                    timestamp: parsed.sub_agent.timestamp || new Date().toISOString(),
                  },
                ],
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
