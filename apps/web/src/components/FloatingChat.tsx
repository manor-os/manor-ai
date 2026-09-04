/**
 * FloatingChat — a bottom-right floating chat button + slide-up panel.
 *
 * Visible on all workspace pages (hidden when on /chat).
 * Supports: file attachments (local + knowledge base), voice-to-text input.
 */
import {
  useState,
  useRef,
  useEffect,
  useCallback,
  useMemo,
  type MutableRefObject,
} from "react";
import { useMutation, useQueryClient, useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import {
  api,
  ApiError,
  ConversationSurfaceKind,
  type GlobalChatFlowEntrypoint,
} from "../lib/api";
import { invalidateKnowledgeQueries } from "../lib/knowledgeInvalidation";
import {
  type ChatMessage,
  type ChatStreamSnapshot,
  type ResponseSurfaceSubmissionReceipt,
  type ResponseSurfaceSubmissionResult,
  type ToolCall,
  isInternalFilePermissionMessage,
  isRedundantApprovalResolutionReceipt,
  isTerminalStreamSnapshot,
  mergeChatStreamSnapshot,
  mergeResolvedWorkflowMessage,
  normalizeWorkspaceRecommendation,
  streamSnapshotNeedsHistory,
  pendingHITLIds,
  hitlActionTranscriptText,
  parseToolCalls,
  useDebounced,
  resolveGlobalWorkflowMessageAction,
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
  hasLocallyStreamedConversation,
  shouldIgnoreLocallyStoppedStreamUpdate,
  useChatStreamStore,
} from "../stores/chatStream";
import { useAuthStore } from "../stores/auth";
import { useToastStore } from "../stores/toast";
import ChatMarkdown from "./ChatMarkdown";
import WorkflowResultCard from "./WorkflowResultCard";
import AssistantMessageBlocks from "./AssistantMessageBlocks";
import FloatingPanel from "./FloatingPanel";
import PanelHeader from "./chat/PanelHeader";
import MessageRow from "./chat/MessageRow";
import MessageBubble from "./chat/MessageBubble";
import ChatTimestamp from "./chat/ChatTimestamp";
import ChatMessageActions, {
  chatMessageActionText,
  type ChatMessageFeedbackRating,
  displayContentForAssistantMessage,
  isRetryableAssistantMessage,
} from "./chat/ChatMessageActions";
import useChatMessageFeedback from "./chat/useChatMessageFeedback";
import CollapsibleSentMessage from "./chat/CollapsibleSentMessage";
import ManorAvatar from "./ui/ManorAvatar";
import AgentActivityOrb, { inferAgentActivity } from "./ui/AgentActivityOrb";
import UserAvatar from "./ui/UserAvatar";
import ChatActionCard, { ApprovalSummary } from "./ui/ChatActionCard";
import ApprovalActionBar from "./ui/ApprovalActionBar";
import { chatMessageAnchorId } from "../lib/chatMessageAnchor";
import { DEFAULT_APPROVAL_OPTIONS } from "../lib/approvalOptions";
import SessionSwitcher from "./SessionSwitcher";
import ToolCallList from "./ui/ToolCallList";
import LoadingSpinner from "./ui/LoadingSpinner";
import { ChatMessagesSkeleton } from "./ui/Skeleton";
import CreditLimitNotice from "./ui/CreditLimitNotice";
import ChatInputFooter, {
  createChatMessageAttachmentSnapshot,
  manualSkillLabel,
  stripManualSkillTokens,
  stripWorkflowInvokeToken,
  workflowInvokeMessage,
  type AttachedItem,
  type ChatComposerSendContext,
  type ManualSkillItem,
  type MentionOption,
  type WorkflowInvokeItem,
} from "./ChatInputFooter";
import WorkflowRunHost, {
  buildWorkspaceWorkflowRunGroups,
  workflowHostOwnedMessageIds,
} from "./workflows/WorkflowRunHost";
import { type ChatBoxMode } from "./ChatModeSelector";
import ChatModeToolbar from "./ChatModeToolbar";
import ChatModeTemplateGallery from "./ChatModeTemplateGallery";
import { chatModeTemplateSamples } from "./EmbeddedChat";
import WorkspaceRecommendationCard from "./WorkspaceRecommendationCard";
import useWorkspaceRecommendationActions from "./useWorkspaceRecommendationActions";
import { prepareTemplateRemix } from "./templateRemix";
import {
  getDefaultChatModePayload,
  getChatModeInputPlaceholder,
  type ChatModePayload,
} from "./ChatModeBriefPanel";
import {
  ChatMessageMetaChips,
  ChatMessageReferenceStrip,
  parseUserMessageDisplay,
  type ChatMessageDisplayReference,
} from "./ChatMessageDisplay";
import {
  clearPendingChatRetry,
  consumePendingChatRetry,
  savePendingChatRetry,
  type PendingChatRetry,
} from "../lib/chatRetry";
import {
  manualSkillReferences,
  resolveManualSkillReferenceIds,
} from "../lib/manualSkillRefs";
import {
  OPEN_FLOATING_CHAT_EVENT,
  type OpenFloatingChatDetail,
} from "../lib/selectionActions";

function maybeLocalCodingRunNoticeForTools(_tools: ToolCall[]): string | null {
  return null;
}
import {
  applyEditorLivePatch,
  buildEditorLiveEditRequest,
  EDITOR_LIVE_CHAT_CLOSE_EVENT,
  EDITOR_LIVE_CHAT_EVENT,
  EDITOR_LIVE_CHAT_UPDATE_EVENT,
  AiEditApplyPhase,
  AiEditPatchStreamEventKind,
  AiEditSessionCleanupStatus,
  aiEditConversationOwnerKey,
  containsEditorLivePatchProtocol,
  countCompleteEditorLivePatchOperations,
  createAiEditConversationDeleteCoordinator,
  createAiEditSessionCleanupCoordinator,
  createEditorLiveChatSseProjector,
  createEditorLivePatchDeltaQueue,
  createEditorLivePatchStream,
  extractRecoverableEditorLivePatchOperationPayloads,
  extractEditorLivePatchPayloads,
  hasReviewableEditorLivePreview,
  isEditorLivePatchCommitAlreadyPreviewed,
  isSameAiEditConversationOwner,
  isSameEditorLiveTarget,
  shouldAttachEditorLiveSourceDocument,
  shouldStreamEditorLiveDeltaPreview,
  stripEditorLiveEditBlocks,
  type AiEditConversationOwnerScope,
  type EditorLiveChatDetail,
} from "../lib/editorLiveChat";
import { decodeAuthTokenClaims, getAuthToken } from "../lib/authToken";
import type { Agent, UserSummary } from "../lib/types";
import { useChatAutoFollow } from "../lib/useChatAutoFollow";
import { t } from "../lib/i18n";
import { getAgentDescription } from "../lib/localizedContent";


function toDisplayText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  try {
    const serialized = JSON.stringify(value);
    return serialized === undefined ? String(value) : serialized;
  } catch {
    return String(value);
  }
}

type ChatRetryRequest = Omit<PendingChatRetry, "createdAt">;
interface FloatingChatSendOptions {
  forceAutoMode?: boolean;
  forceOwnerChat?: boolean;
  responseSurfaceSubmission?: ResponseSurfaceSubmissionReceipt;
}
/* See EmbeddedChat: snapshots are the fast path, not a guaranteed one. */
const FOLLOWED_RUN_SILENCE_MS = 45_000;
/* A row can claim "streaming" forever if its API process was hard-killed (the
 * sweeper only runs at startup). Cap the polls so a zombie row costs a bounded
 * number of requests, not one every 45s for the life of the tab. */
const FOLLOWED_RUN_MAX_POLLS = 20;

/** Store-level truth — a component ref only knows this surface's session. */
function isConversationStreamingNow(convId: string): boolean {
  const state = useChatStreamStore.getState();
  const key = state.getSessionKeyForConversation(convId);
  return Boolean(key && state.sessions[key]?.streaming);
}

const GLOBAL_WORKFLOW_INVALIDATION_QUERY_KEYS = [["conversations"]] as const;

function parseLiveEditStreamFrame(
  data: string,
  currentEvent: string,
): string {
  if (!data || data === "[DONE]" || currentEvent === "error") return "";
  try {
    const parsed = JSON.parse(data);
    const token = parsed.text_delta ?? parsed.token ?? parsed.content;
    if (token == null) return "";
    if (typeof token === "string") return token;
    if (typeof token === "number" || typeof token === "boolean")
      return String(token);
    return "";
  } catch {
    return "";
  }
}

function parseJsonString(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function firstGeneratedImageUrl(value: unknown): string | null {
  const parsed = parseJsonString(value);
  if (!parsed || typeof parsed !== "object") return null;
  const record = parsed as Record<string, unknown>;
  for (const key of ["image_url", "result_url", "url"]) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate) return candidate;
  }
  const imageUrls = record.image_urls;
  if (Array.isArray(imageUrls)) {
    const candidate = imageUrls.find((item) => typeof item === "string" && item);
    if (typeof candidate === "string") return candidate;
  }
  const images = record.images;
  if (Array.isArray(images)) {
    const candidate = images.find((item) => typeof item === "string" && item);
    if (typeof candidate === "string") return candidate;
  }
  return null;
}

function generatedImageUrlFromSseFrame(parsed: unknown): string | null {
  if (!parsed || typeof parsed !== "object") return null;
  const frame = parsed as { tool_call?: { name?: string; result?: unknown; status?: string } };
  const tool = frame.tool_call;
  if (!tool || tool.status === "pending") return null;
  const name = (tool.name || "").toLowerCase();
  if (
    name !== "generate_image" &&
    name !== "generate_file" &&
    !name.endsWith("__generate_image")
  ) {
    return null;
  }
  return firstGeneratedImageUrl(tool.result);
}

type EditorLiveProgressStep =
  | "read_current_file"
  | "generate_patch"
  | "apply_patch"
  | "verify_patch";

const EDITOR_LIVE_PROGRESS_TOOLS: Record<EditorLiveProgressStep, string> = {
  read_current_file: "ai_edit_read_current_file",
  generate_patch: "ai_edit_generate_patch",
  apply_patch: "ai_edit_apply_patch",
  verify_patch: "ai_edit_verify_patch",
};

function stableJson(value: unknown) {
  try {
    return JSON.stringify(value);
  } catch {
    return undefined;
  }
}

function truncateToolText(value: string, maxLength = 9000) {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength)}\n... truncated`;
}

function diffLineParts(value: string) {
  return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
}

function truncateDiffLine(value: string, maxLength = 260) {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength)}...`;
}

function changedDiffLines(
  lines: string[],
  marker: "+" | "-",
  limit: number,
  label: "added" | "removed",
) {
  const boundedLimit = Math.max(0, Math.floor(limit));
  if (lines.length <= boundedLimit) {
    return lines.map((line) => `${marker}${truncateDiffLine(line)}`);
  }
  if (boundedLimit <= 1) {
    return [`${marker}... ${lines.length} ${label} lines truncated`];
  }

  const headCount = Math.max(1, Math.ceil((boundedLimit - 1) * 0.72));
  const tailCount = Math.max(0, boundedLimit - headCount - 1);
  const omitted = Math.max(0, lines.length - headCount - tailCount);
  return [
    ...lines.slice(0, headCount).map((line) => `${marker}${truncateDiffLine(line)}`),
    `${marker}... ${omitted} ${label} lines truncated`,
    ...lines.slice(lines.length - tailCount).map((line) => `${marker}${truncateDiffLine(line)}`),
  ];
}

function buildEditorLiveDiff(before: string, after: string, fileName: string) {
  if (before === after) return "";

  const beforeLines = diffLineParts(before);
  const afterLines = diffLineParts(after);
  let prefix = 0;
  while (
    prefix < beforeLines.length &&
    prefix < afterLines.length &&
    beforeLines[prefix] === afterLines[prefix]
  ) {
    prefix += 1;
  }

  let beforeEnd = beforeLines.length - 1;
  let afterEnd = afterLines.length - 1;
  while (
    beforeEnd >= prefix &&
    afterEnd >= prefix &&
    beforeLines[beforeEnd] === afterLines[afterEnd]
  ) {
    beforeEnd -= 1;
    afterEnd -= 1;
  }

  const context = 3;
  const prefixContextStart = Math.max(0, prefix - context);
  const suffixContextEnd = Math.min(beforeLines.length, beforeEnd + 1 + context);
  const beforeSpan = Math.max(1, suffixContextEnd - prefixContextStart);
  const afterSpan = Math.max(1, Math.min(afterLines.length, afterEnd + 1 + context) - prefixContextStart);
  const beforeChanged = beforeLines.slice(prefix, beforeEnd + 1);
  const afterChanged = afterLines.slice(prefix, afterEnd + 1);
  const prefixContextLines = beforeLines.slice(prefixContextStart, prefix);
  const suffixContextLines = beforeLines.slice(beforeEnd + 1, suffixContextEnd);
  const maxLines = 180;
  const changedBudget = Math.max(
    16,
    maxLines - 3 - prefixContextLines.length - suffixContextLines.length,
  );
  const removeLimit = beforeChanged.length > 0
    ? Math.min(
        beforeChanged.length,
        afterChanged.length > 0 ? Math.min(10, Math.max(4, changedBudget - Math.min(afterChanged.length, 40))) : changedBudget,
      )
    : 0;
  const addLimit = afterChanged.length > 0
    ? Math.min(afterChanged.length, Math.max(4, changedBudget - removeLimit))
    : 0;
  const lines: string[] = [
    `--- ${fileName}`,
    `+++ ${fileName}`,
    `@@ -${prefixContextStart + 1},${beforeSpan} +${prefixContextStart + 1},${afterSpan} @@`,
  ];

  prefixContextLines.forEach((line) => {
    lines.push(` ${truncateDiffLine(line)}`);
  });
  lines.push(...changedDiffLines(beforeChanged, "-", removeLimit, "removed"));
  lines.push(...changedDiffLines(afterChanged, "+", addLimit, "added"));
  suffixContextLines.forEach((line) => {
    lines.push(` ${truncateDiffLine(line)}`);
  });

  return lines.join("\n");
}

function makeEditorLiveProgressTool(
  step: EditorLiveProgressStep,
  status: ToolCall["status"],
  args: Record<string, unknown>,
  result?: Record<string, unknown>,
): ToolCall {
  return {
    name: EDITOR_LIVE_PROGRESS_TOOLS[step],
    arguments: stableJson(args),
    result: result ? stableJson(result) : undefined,
    status,
    startedAt: status === "pending" ? Date.now() : undefined,
  };
}

function withEditorLiveProgress(
  messages: ChatMessage[],
  tool: ToolCall,
): ChatMessage[] {
  const updated = [...messages];
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
  const existing = [...(last.tool_calls || [])];
  const idx = existing.findIndex((candidate) => candidate.name === tool.name);
  if (idx >= 0) existing[idx] = { ...existing[idx], ...tool };
  else existing.push(tool);
  updated[updated.length - 1] = { ...last, tool_calls: existing };
  return updated;
}

type EditorLiveTurnState = {
  sessionKey?: string;
  turnId?: string;
  content: string;
  phase: "idle" | "pending" | "streaming" | "complete" | "failed";
  operationCount?: number;
  appliedPatchCount?: number;
  basePatchCount?: number;
  abortController?: AbortController;
};

async function rollbackEditorLiveAdapter(detail: EditorLiveChatDetail | null) {
  if (!detail) return true;
  try {
    await detail.adapter.rollback();
    return true;
  } catch (error) {
    console.warn("Editor live edit rollback failed", error);
    return false;
  }
}

async function restoreEditorLiveTurn(
  detail: EditorLiveChatDetail,
  content: string,
  hadReviewablePreview: boolean,
  signal?: AbortSignal,
  turnId?: string,
  turnBasePatchCount = 0,
) {
  if (hadReviewablePreview && !signal?.aborted) {
    try {
      const restored = await detail.adapter.restore(content, {
        complete: true,
        phase: AiEditApplyPhase.Complete,
        source: "assistant-stream",
        turnId,
        turnBasePatchCount,
        patchCount: 0,
        mode: "patch",
        sourceLabel: "previous preview",
        signal,
      });
      if (restored !== false && !signal?.aborted) return true;
    } catch (error) {
      console.warn("Editor live edit turn restore failed", error);
    }
  }
  await rollbackEditorLiveAdapter(detail);
  return false;
}

const AI_EDIT_DELETE_QUEUE_PREFIX = "manor.ai-edit.pending-conversation-deletes.v3";
const AI_EDIT_DELETE_RETRY_DELAYS_MS = [0, 400, 1_600] as const;
const AI_EDIT_DELETE_QUEUE_EVENT = "manor:ai-edit-delete-queued";
const AI_EDIT_DELETE_RETRY_MIN_MS = 5_000;
const AI_EDIT_DELETE_RETRY_MAX_MS = 60_000;
const aiEditConversationDeleteCoordinator = createAiEditConversationDeleteCoordinator();

function currentAiEditConversationOwner(): AiEditConversationOwnerScope | null {
  const claims = decodeAuthTokenClaims(getAuthToken());
  if (!claims?.sub || !claims.entity_id) return null;
  return { userId: claims.sub, entityId: claims.entity_id };
}

function aiEditDeleteQueueKey(owner: AiEditConversationOwnerScope) {
  return `${AI_EDIT_DELETE_QUEUE_PREFIX}:${aiEditConversationOwnerKey(owner)}`;
}

function readPendingAiEditConversationDeletes(
  owner: AiEditConversationOwnerScope,
): string[] {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(aiEditDeleteQueueKey(owner)) || "[]");
    return Array.isArray(parsed)
      ? Array.from(new Set(
          parsed.filter((value): value is string => typeof value === "string" && Boolean(value)),
        ))
      : [];
  } catch {
    return [];
  }
}

function writePendingAiEditConversationDeletes(
  owner: AiEditConversationOwnerScope,
  ids: string[],
) {
  try {
    const key = aiEditDeleteQueueKey(owner);
    if (ids.length > 0) window.sessionStorage.setItem(key, JSON.stringify(Array.from(new Set(ids))));
    else window.sessionStorage.removeItem(key);
  } catch {
    // Storage can be unavailable; the bounded in-memory retry still runs.
  }
}

function enqueueAiEditConversationDelete(
  owner: AiEditConversationOwnerScope,
  conversationId: string,
) {
  const pending = readPendingAiEditConversationDeletes(owner);
  if (pending.includes(conversationId)) return;
  writePendingAiEditConversationDeletes(owner, [...pending, conversationId]);
  window.dispatchEvent(new Event(AI_EDIT_DELETE_QUEUE_EVENT));
}

function removePendingAiEditConversationDelete(
  owner: AiEditConversationOwnerScope,
  conversationId: string,
) {
  writePendingAiEditConversationDeletes(
    owner,
    readPendingAiEditConversationDeletes(owner).filter((id) => id !== conversationId),
  );
}

function deleteAiEditConversation(
  conversationId: string,
  owner?: AiEditConversationOwnerScope | null,
): Promise<boolean> {
  if (!owner) return Promise.resolve(false);
  enqueueAiEditConversationDelete(owner, conversationId);
  if (!isSameAiEditConversationOwner(currentAiEditConversationOwner(), owner)) {
    // Never send a previous entity's host-owned conversation id with the
    // current token. Its queue is retried when that exact owner returns.
    return Promise.resolve(false);
  }
  const deletion = aiEditConversationDeleteCoordinator.run(owner, conversationId, async () => {
    for (const delayMs of AI_EDIT_DELETE_RETRY_DELAYS_MS) {
      if (delayMs > 0) {
        await new Promise<void>((resolve) => window.setTimeout(resolve, delayMs));
      }
      if (!isSameAiEditConversationOwner(currentAiEditConversationOwner(), owner)) {
        return false;
      }
      try {
        await api.chat.deleteConversation(conversationId, { silent: true });
        return true;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return true;
        }
      }
    }

    console.warn("AI Edit conversation cleanup is queued for retry", conversationId);
    return false;
  });
  return deletion.then((deleted) => {
    if (deleted) removePendingAiEditConversationDelete(owner, conversationId);
    return deleted;
  });
}

async function waitForEditorLivePreviewPaint(signal?: AbortSignal, settleDelay = true) {
  if (signal?.aborted) return;
  await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
  if (
    !settleDelay
    || signal?.aborted
    || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  ) return;
  await new Promise<void>((resolve) => window.setTimeout(resolve, 70));
}

function pipeEditorLiveEditStream(
  response: Response,
  detail: EditorLiveChatDetail,
  sessionKey: string | undefined,
  turnId: string,
  appliedRef: MutableRefObject<EditorLiveTurnState>,
  isActive: () => boolean,
  onProgress?: (tool: ToolCall) => void,
  onCompleteApplied?: () => void,
  onSettled?: () => void,
  initialContent = "",
  hadReviewablePreview = false,
  turnBasePatchCount = 0,
) {
  const settleUnfinishedTurn = () => {
    const applied = appliedRef.current;
    if (
      applied.turnId === turnId
      && (applied.phase === "pending" || applied.phase === "streaming")
    ) {
      appliedRef.current = { ...applied, phase: "failed" };
    }
    onSettled?.();
  };
  if (!response.body) {
    settleUnfinishedTurn();
    return response;
  }
  if (typeof response.body.tee !== "function") {
    settleUnfinishedTurn();
    return response;
  }

  const [chatBody, liveBody] = response.body.tee();
  const liveProcessing = (async () => {
    const reader = liveBody.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let currentEvent = "";
    let assistantText = "";
    let completeNotified = false;
    let processedOperationCount = 0;
    let appliedPatchCount = 0;
    let patchFailureCount = 0;
    let sawStreamError = false;
    let lastAppliedPatch = "";
    let appliedGeneratedImageUrl = "";
    let lastDeltaOperationIndex: number | null = null;
    const patchStream = createEditorLivePatchStream();
    // The caller reads once at turn start. Re-reading here could retarget a
    // Project edit if the user switches tabs while the response is opening.
    const baselineContent = initialContent;
    let workingContent = baselineContent;
    let lastPreviewContent = baselineContent;
    const signal = appliedRef.current.turnId === turnId
      ? appliedRef.current.abortController?.signal
      : undefined;
    const cancelLiveReader = () => {
      void reader.cancel();
    };
    signal?.addEventListener("abort", cancelLiveReader, { once: true });
    if (!isActive() || signal?.aborted) {
      await reader.cancel();
      signal?.removeEventListener("abort", cancelLiveReader);
      settleUnfinishedTurn();
      return;
    }
    appliedRef.current = {
      ...appliedRef.current,
      sessionKey,
      turnId,
      content: workingContent,
      phase: "streaming",
      operationCount: 0,
      appliedPatchCount: 0,
    };

    const rollbackFailedLiveEdit = async () => {
      if (!isActive() || signal?.aborted) return false;
      const restoredPreviousPreview = await restoreEditorLiveTurn(
        detail,
        baselineContent,
        hadReviewablePreview,
        signal,
        turnId,
        turnBasePatchCount,
      );
      if (!isActive() || signal?.aborted) return false;
      appliedRef.current = {
        sessionKey,
        content: baselineContent,
        phase: restoredPreviousPreview ? "complete" : "failed",
        operationCount: 0,
        appliedPatchCount: 0,
        basePatchCount: turnBasePatchCount,
      };
      return true;
    };

    const finishLiveEdit = async (patch = "", sourceLabel = "assistant patch") => {
      if (completeNotified) return true;
      if (!isActive() || signal?.aborted) return false;
      const diff = buildEditorLiveDiff(
        baselineContent,
        workingContent,
        detail.documentName || "current file",
      );
      const accepted = await detail.adapter.complete(workingContent, {
        complete: true,
        phase: AiEditApplyPhase.Complete,
        source: "assistant-stream",
        turnId,
        turnBasePatchCount,
        mode: "patch",
        diff,
        patch: truncateToolText(patch, 5000),
        patchCount: appliedPatchCount,
        sourceLabel,
        signal,
      });
      if (accepted === false) throw new Error("The editor rejected the completed edit.");
      if (!isActive() || signal?.aborted) return false;
      completeNotified = true;
      appliedRef.current = {
        sessionKey,
        turnId,
        content: workingContent,
        phase: "complete",
        operationCount: processedOperationCount,
        appliedPatchCount,
        basePatchCount: turnBasePatchCount,
        abortController: appliedRef.current.abortController,
      };
      onProgress?.(
        makeEditorLiveProgressTool(
          "verify_patch",
          "success",
          {
            file: detail.documentName || "current file",
            stage: "verified",
          },
          {
            status: patchFailureCount > 0 ? "partial" : "ok",
            patches: appliedPatchCount,
            failed: patchFailureCount,
          },
        ),
      );
      onCompleteApplied?.();
      return true;
    };

    const applyGeneratedImageUrl = async (imageUrl: string) => {
      if (!detail.applyGeneratedImage || !imageUrl || !isActive() || signal?.aborted) return false;
      if (imageUrl === appliedGeneratedImageUrl) return true;
      const fileLabel = detail.documentName || "current file";
      const diff = [
        `--- ${fileLabel}`,
        `+++ ${fileLabel}`,
        "@@ generated image @@",
        "- current image preview",
        "+ AI generated image preview",
      ].join("\n");

      onProgress?.(
        makeEditorLiveProgressTool(
          "generate_patch",
          "success",
          {
            file: fileLabel,
            stage: "generated replacement image",
          },
          {
            status: "ok",
            source: "image generation tool",
            image_url: imageUrl,
          },
        ),
      );
      onProgress?.(
        makeEditorLiveProgressTool(
          "apply_patch",
          "pending",
          {
            file: detail.documentName || "current file",
            patch: appliedPatchCount + 1,
          },
        ),
      );

      try {
        const accepted = await detail.applyGeneratedImage(imageUrl, {
          complete: false,
          phase: AiEditApplyPhase.Preview,
          source: "assistant-stream",
          turnId,
          turnBasePatchCount,
          mode: "patch",
          diff,
          patchCount: appliedPatchCount + 1,
          sourceLabel: "generated image",
          signal,
        });
        if (accepted === false) throw new Error("The editor rejected the generated image.");
        if (!isActive() || signal?.aborted) return false;
        appliedGeneratedImageUrl = imageUrl;
        appliedPatchCount += 1;
        appliedRef.current = {
          sessionKey,
          turnId,
          content: workingContent,
          phase: "streaming",
          appliedPatchCount,
          basePatchCount: turnBasePatchCount,
          abortController: appliedRef.current.abortController,
        };
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "success",
            {
              file: detail.documentName || "current file",
              patch: appliedPatchCount,
            },
            {
              status: "ok",
              source: "generated image",
            },
          ),
        );
        return true;
      } catch (err) {
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "error",
            {
              file: detail.documentName || "current file",
              patch: appliedPatchCount + 1,
            },
            {
              status: "failed",
              error: (err as Error).message,
            },
          ),
        );
        return false;
      }
    };

    const applyPatchCommit = async (event: ReturnType<typeof patchStream.push>[number]) => {
      if (!isActive() || signal?.aborted) return;
      const patchJson = event.patch;
      const patchNumber = processedOperationCount + 1;
      processedOperationCount = patchNumber;
      const beforeContent = workingContent;
      const result = applyEditorLivePatch(beforeContent, patchJson);
      const diff = result.failed.length
        ? ""
        : buildEditorLiveDiff(
            beforeContent,
            result.content,
            detail.documentName || "current file",
          );
      onProgress?.(
        makeEditorLiveProgressTool(
          "generate_patch",
          "success",
          {
            file: detail.documentName || "current file",
            stage: "patch streamed",
          },
          {
            status: "ok",
            patches: patchNumber,
            patch: truncateToolText(patchJson, 5000),
          },
        ),
      );
      onProgress?.(
        makeEditorLiveProgressTool(
          "apply_patch",
          "pending",
          {
            file: detail.documentName || "current file",
            patch: patchNumber,
          },
        ),
      );

      if (result.failed.length > 0) {
        appliedRef.current = {
          ...appliedRef.current,
          operationCount: processedOperationCount,
        };
        patchFailureCount += 1;
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "error",
            {
              file: detail.documentName || "current file",
              patch: patchNumber,
            },
            {
              status: "failed",
              failed: result.failed,
              patch: truncateToolText(patchJson, 5000),
            },
          ),
        );
        onProgress?.(
          makeEditorLiveProgressTool(
            "verify_patch",
            "error",
            {
              file: detail.documentName || "current file",
              stage: "patch failed",
            },
            {
              status: "failed",
              failed: result.failed,
              patch: truncateToolText(patchJson, 5000),
            },
          ),
        );
        return;
      }

      try {
        const continuesStreamedDelta = event.operationIndex === lastDeltaOperationIndex;
        const alreadyPreviewed = isEditorLivePatchCommitAlreadyPreviewed(
          event.operationIndex,
          lastDeltaOperationIndex,
          result.content,
          lastPreviewContent,
        );
        if (!alreadyPreviewed) {
          const accepted = await detail.adapter.preview(result.content, {
            complete: false,
            phase: AiEditApplyPhase.Preview,
            source: "assistant-stream",
            streamEvent: continuesStreamedDelta
              ? AiEditPatchStreamEventKind.Delta
              : undefined,
            turnId,
            turnBasePatchCount,
            mode: "patch",
            diff,
            patch: truncateToolText(patchJson, 5000),
            patchCount: appliedPatchCount + 1,
            sourceLabel: "assistant patch",
            signal,
          });
          if (accepted === false) throw new Error("The editor rejected the generated change.");
        }
        await waitForEditorLivePreviewPaint(signal);
        if (!isActive() || signal?.aborted) return;
        workingContent = result.content;
        lastPreviewContent = result.content;
        lastDeltaOperationIndex = null;
        appliedPatchCount += 1;
        lastAppliedPatch = patchJson;
        appliedRef.current = {
          sessionKey,
          turnId,
          content: workingContent,
          phase: "streaming",
          operationCount: processedOperationCount,
          appliedPatchCount,
          basePatchCount: turnBasePatchCount,
          abortController: appliedRef.current.abortController,
        };
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "success",
            {
              file: detail.documentName || "current file",
              patch: patchNumber,
            },
            {
              status: "ok",
              operations: result.applied,
              patch: truncateToolText(patchJson, 5000),
            },
          ),
        );
      } catch (err) {
        appliedRef.current = {
          ...appliedRef.current,
          operationCount: processedOperationCount,
        };
        patchFailureCount += 1;
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "error",
            {
              file: detail.documentName || "current file",
              patch: patchNumber,
            },
            {
              status: "failed",
              error: (err as Error).message,
            },
          ),
        );
        onProgress?.(
          makeEditorLiveProgressTool(
            "verify_patch",
            "error",
            {
              file: detail.documentName || "current file",
              stage: "editor rejected patch",
            },
            {
              status: "failed",
              error: (err as Error).message,
              patch: truncateToolText(patchJson, 5000),
            },
          ),
        );
        console.warn("Editor live edit apply failed", err);
      }
    };

    const applyPatchDelta = async (event: ReturnType<typeof patchStream.push>[number]) => {
      if (!isActive() || signal?.aborted) return;
      if (!shouldStreamEditorLiveDeltaPreview(workingContent.length)) return;
      try {
        const result = applyEditorLivePatch(workingContent, event.patch);
        if (result.failed.length > 0 || result.content === lastPreviewContent) return;
        const accepted = await detail.adapter.preview(result.content, {
          complete: false,
          phase: AiEditApplyPhase.Preview,
          streamEvent: AiEditPatchStreamEventKind.Delta,
          source: "assistant-stream",
          turnId,
          turnBasePatchCount,
          mode: "patch",
          patch: truncateToolText(event.patch, 5000),
          patchCount: appliedPatchCount + 1,
          sourceLabel: "assistant delta",
          signal,
        });
        if (accepted !== false && isActive() && !signal?.aborted) {
          lastPreviewContent = result.content;
          lastDeltaOperationIndex = event.operationIndex;
        }
      } catch {
        // A semantic editor may reject an intermediate but accept the validated commit.
      }
    };

    const deltaPreviewQueue = createEditorLivePatchDeltaQueue(applyPatchDelta);

    const applyPatchStreamEvents = async (
      events: ReturnType<typeof patchStream.push>,
    ) => {
      for (const event of events) {
        if (!isActive() || signal?.aborted) return;
        if (event.kind === AiEditPatchStreamEventKind.Delta) {
          deltaPreviewQueue.enqueue(event);
          continue;
        }
        if (event.kind === AiEditPatchStreamEventKind.Commit) {
          await deltaPreviewQueue.flush();
          await applyPatchCommit(event);
        }
      }
    };

    const resetPatchStreamAttempt = async () => {
      await deltaPreviewQueue.reset();
      if (!isActive() || signal?.aborted) return;
      await restoreEditorLiveTurn(
        detail,
        baselineContent,
        hadReviewablePreview,
        signal,
        turnId,
        turnBasePatchCount,
      );
      if (!isActive() || signal?.aborted) return;
      await detail.adapter.beginTurn({
        complete: false,
        phase: AiEditApplyPhase.Preview,
        source: "assistant-stream",
        turnId,
        turnBasePatchCount,
        patchCount: 0,
        mode: "patch",
        sourceLabel: "assistant stream retry",
        signal,
      });
      if (!isActive() || signal?.aborted) return;
      patchStream.reset();
      assistantText = "";
      completeNotified = false;
      processedOperationCount = 0;
      appliedPatchCount = 0;
      patchFailureCount = 0;
      sawStreamError = false;
      lastAppliedPatch = "";
      appliedGeneratedImageUrl = "";
      lastDeltaOperationIndex = null;
      workingContent = baselineContent;
      lastPreviewContent = baselineContent;
      appliedRef.current = {
        sessionKey,
        turnId,
        content: baselineContent,
        phase: "streaming",
        operationCount: 0,
        appliedPatchCount: 0,
        basePatchCount: turnBasePatchCount,
        abortController: appliedRef.current.abortController,
      };
    };

    const processSseLine = async (rawLine: string) => {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (!line) {
        currentEvent = "";
        return;
      }
      if (line.startsWith("event: ")) {
        currentEvent = line.slice(7).trim();
        return;
      }
      if (!line.startsWith("data: ")) return;
      const rawData = line.slice(6).trim();
      let parsedFrame: unknown = null;
      try {
        parsedFrame = JSON.parse(rawData);
      } catch {
        parsedFrame = null;
      }
      if (currentEvent === "text_reset") {
        await resetPatchStreamAttempt();
        return;
      }
      if (currentEvent === "error") sawStreamError = true;
      const generatedImageUrl = generatedImageUrlFromSseFrame(parsedFrame);
      if (generatedImageUrl) await applyGeneratedImageUrl(generatedImageUrl);
      const token = parseLiveEditStreamFrame(rawData, currentEvent);
      if (!token) return;
      assistantText += token;
      await applyPatchStreamEvents(patchStream.push(token));
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) await processSseLine(line);
      }
      buffer += decoder.decode();
      if (buffer) await processSseLine(buffer);
      await deltaPreviewQueue.flush();
      const completePatchOperationCount = countCompleteEditorLivePatchOperations(assistantText);
      const hasCompletePatchProtocol = processedOperationCount > 0
        && completePatchOperationCount === processedOperationCount;
      const hasGeneratedImageOnly = Boolean(appliedGeneratedImageUrl)
        && processedOperationCount === 0
        && !containsEditorLivePatchProtocol(assistantText);
      if (sawStreamError) {
        await deltaPreviewQueue.cancel();
        await rollbackFailedLiveEdit();
      } else if (
        appliedPatchCount > 0
        && (hasCompletePatchProtocol || hasGeneratedImageOnly)
      ) {
        await finishLiveEdit(
          lastAppliedPatch || extractEditorLivePatchPayloads(assistantText).at(-1) || "",
          hasGeneratedImageOnly
            ? "generated image"
            : patchFailureCount > 0
              ? "assistant patch (partial)"
              : "assistant patch",
        );
      } else {
        const invalidPatchProtocol = appliedPatchCount > 0 && !hasCompletePatchProtocol;
        onProgress?.(
          makeEditorLiveProgressTool(
            "generate_patch",
            "error",
            {
              file: detail.documentName || "current file",
              stage: "no patch returned",
            },
            {
              status: "failed",
              error: invalidPatchProtocol
                ? "The live patch stream ended with an incomplete or invalid payload."
                : "The assistant did not return a live patch.",
              response: truncateToolText(stripEditorLiveEditBlocks(assistantText), 1500),
            },
          ),
        );
        onProgress?.(
          makeEditorLiveProgressTool(
            "verify_patch",
            "error",
            {
              file: detail.documentName || "current file",
              stage: "nothing applied",
            },
            {
              status: "failed",
              error: invalidPatchProtocol
                ? "The temporary preview was rolled back because the patch protocol was invalid."
                : "No patch was applied to the current file.",
            },
          ),
        );
        await rollbackFailedLiveEdit();
      }
    } catch (err) {
      if (isActive() && !signal?.aborted) {
        await deltaPreviewQueue.cancel();
        await rollbackFailedLiveEdit();
        console.warn("Editor live edit stream failed", err);
      }
    } finally {
      await deltaPreviewQueue.cancel();
      signal?.removeEventListener("abort", cancelLiveReader);
      settleUnfinishedTurn();
    }
  })();

  // Project hidden protocol out of Chat as each model chunk arrives, while the
  // editor branch consumes the same tee concurrently. Keep the response body
  // open until the editor catches up so input unlock and preview completion
  // still share one terminal boundary.
  const chatProtocolDecoder = new TextDecoder();
  const chatProtocolEncoder = new TextEncoder();
  const chatSseProjector = createEditorLiveChatSseProjector();
  let chatSseBuffer = "";

  const enqueueProjectedChatText = (
    decoded: string,
    controller: TransformStreamDefaultController<Uint8Array>,
    final = false,
  ) => {
    chatSseBuffer += decoded;
    const lines = chatSseBuffer.split("\n");
    chatSseBuffer = final ? "" : lines.pop() || "";
    let projected = lines.map(chatSseProjector.projectLine).join("\n");
    if (!final && lines.length > 0) projected += "\n";
    if (final && projected && !projected.endsWith("\n")) projected += "\n";
    if (projected) controller.enqueue(chatProtocolEncoder.encode(projected));
  };

  const synchronizedChatBody = chatBody.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        const decoded = chatProtocolDecoder.decode(chunk, { stream: true });
        enqueueProjectedChatText(decoded, controller);
      },
      async flush(controller) {
        enqueueProjectedChatText(chatProtocolDecoder.decode(), controller, true);
        const pendingText = chatSseProjector.flush();
        if (pendingText) controller.enqueue(chatProtocolEncoder.encode(pendingText));
        await liveProcessing;
      },
    }),
  );

  return new Response(synchronizedChatBody, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

function hasApprovalRequest(msg: ChatMessage) {
  return Boolean(msg.hitl_requests?.some((hitl) => hitl.type === "approval"));
}

function approvalPromptSignals(content: unknown) {
  const lower = toDisplayText(content).toLowerCase();
  const mentionsApproval = /审批|批准|确认|approval|approve|permission/.test(
    lower,
  );
  const mentionsAction =
    /删除|写入|修改|移动|覆盖|创建|生成|保存|发布|发表|delete|write|modify|move|overwrite|create|generate|save|publish|publicat|post/.test(
      lower,
    );
  return mentionsApproval && mentionsAction;
}

function isInlineApprovalPrompt(msg: ChatMessage) {
  if (msg.role !== "assistant" || hasApprovalRequest(msg)) return false;
  const content = toDisplayText(msg.content).trim();
  return Boolean(
    content && content.length <= 700 && approvalPromptSignals(content),
  );
}

function messageHasApprovalPrompt(msg: ChatMessage) {
  return hasApprovalRequest(msg);
}

function toolStatus(tool: ToolCall) {
  return tool.status || (tool.result ? "success" : "pending");
}

function visibleToolCallsForMessage(msg: ChatMessage) {
  const tools = msg.tool_calls || [];
  if (!messageHasApprovalPrompt(msg)) return tools;
  return tools.filter((tool) => toolStatus(tool) !== "success");
}

function isApprovalBoilerplateContent(msg: ChatMessage) {
  if (!messageHasApprovalPrompt(msg)) return false;
  const content = toDisplayText(msg.content).trim();
  if (!content || content.length > 700) return false;
  return approvalPromptSignals(content);
}

function inferApprovalAction(content: unknown) {
  const lower = toDisplayText(content).toLowerCase();
  if (/删除|移除|delete|remove|trash/.test(lower)) return "delete";
  if (/修改|编辑|更新|edit|modify|update/.test(lower)) return "edit";
  if (/创建|生成|create|generate/.test(lower)) return "create";
  if (/移动|move/.test(lower)) return "move";
  if (/保存|写入|save|write/.test(lower)) return "write";
  return "change";
}

function extractApprovalPaths(content: unknown) {
  const extensions =
    "md|txt|csv|json|html|docx|xlsx|pptx|pdf|png|jpg|jpeg|webp|mp4|mov";
  const paths = new Set<string>();
  const text = toDisplayText(content);
  const backtickRe = new RegExp("`([^`]+\\.(" + extensions + "))`", "gi");
  let match: RegExpExecArray | null;
  while ((match = backtickRe.exec(text)) && paths.size < 3) {
    paths.add(match[1].trim());
  }
  if (paths.size === 0) {
    const bareRe = new RegExp(
      "(?:^|[\\s（(])([^\\s,，。；;:：\"'`]+\\.(" +
        extensions +
        "))(?=$|[\\s,，。；;:：\"'`?？!！）)])",
      "gi",
    );
    while ((match = bareRe.exec(text)) && paths.size < 3) {
      paths.add(match[1].trim());
    }
  }
  return Array.from(paths);
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function FloatingChat() {
  const toast = useToastStore();
  const queryClient = useQueryClient();
  const location = useLocation();
  const currentUserId = useAuthStore((s) => s.user?.id);
  const currentAuthToken = useAuthStore((s) => s.token);
  const currentAiEditOwner = useMemo<AiEditConversationOwnerScope | null>(() => {
    const claims = decodeAuthTokenClaims(currentAuthToken);
    return claims?.sub && claims.entity_id
      ? { userId: claims.sub, entityId: claims.entity_id }
      : null;
  }, [currentAuthToken]);
  const [open, setOpen] = useState(false);

  const [input, setInput] = useState("");
  const [chatMode, setChatMode] = useState<ChatBoxMode>("auto");
  const [chatModePayload, setChatModePayload] = useState<ChatModePayload>(() =>
    getDefaultChatModePayload("auto"),
  );
  const [attachedFiles, setAttachedFiles] = useState<AttachedItem[]>([]);
  const [composerSeed, setComposerSeed] = useState<{
    key: string;
    attachments: AttachedItem[];
  } | null>(null);
  const [editorSessionLabel, setEditorSessionLabel] = useState<string | null>(
    null,
  );
  const [editorLiveInfo, setEditorLiveInfo] =
    useState<EditorLiveChatDetail | null>(null);
  const [editorLiveSessionActive, setEditorLiveSessionActive] = useState(false);
  const [editorLiveCleanupStatus, setEditorLiveCleanupStatus] =
    useState(AiEditSessionCleanupStatus.Idle);
  const [selectedMentions, setSelectedMentions] = useState<MentionOption[]>([]);
  const [mentionedAgentId, setMentionedAgentId] = useState<
    string | undefined
  >();

  const handleChatModeChange = useCallback((mode: ChatBoxMode) => {
    setChatMode(mode);
    setChatModePayload(getDefaultChatModePayload(mode));
  }, []);

  const resetChatModeAfterTurn = useCallback(() => {
    setChatMode("auto");
    setChatModePayload(getDefaultChatModePayload("auto"));
  }, []);

  // Persistent per-session stream store — survives close/reopen.
  const initialStreamState = useMemo(() => useChatStreamStore.getState(), []);
  const initialSession = initialStreamState.latestSessionKey
    ? initialStreamState.sessions[initialStreamState.latestSessionKey]
    : undefined;
  const [currentConvId, setCurrentConvId] = useState<string | undefined>(
    initialSession?.convId,
  );
  const [draftSessionKey, setDraftSessionKey] = useState<string | undefined>(
    initialSession && !initialSession.convId ? initialSession.key : undefined,
  );
  const currentSessionKey = currentConvId || draftSessionKey;
  const currentSession = useChatStreamStore((s) =>
    currentSessionKey
      ? s.sessions[currentSessionKey] ||
        s.sessions[s.sessionAliases[currentSessionKey]]
      : undefined,
  );
  const streaming = Boolean(currentSession?.streaming);
  const messages = currentSession?.messages || [];
  const responseSurfaceSubmissionReceipts = useMemo(
    () => collectResponseSurfaceSubmissionReceipts(messages),
    [messages],
  );
  const responseSurfaceSubmissionFailureMessageIds = useMemo(
    () => collectResponseSurfaceSubmissionFailureMessageIds(
      messages,
      responseSurfaceSubmissionReceipts,
    ),
    [messages, responseSurfaceSubmissionReceipts],
  );
  /* A server-side turn this tab is following rather than streaming. Display
   * only — `streaming` still means "this tab owns the connection". */
  const [followedRunActive, setFollowedRunActive] = useState(false);
  const [followedRunSilenceKey, setFollowedRunSilenceKey] = useState(0);
  const lastSnapshotSeqRef = useRef<Record<string, number>>({});
  const snapshotRefetchedRef = useRef<string | undefined>(undefined);
  const followedRunPollsRef = useRef(0);
  const lastAssistantMessage = [...messages]
    .reverse()
    .find((message) => message.role === "assistant");
  /* The meta path covers a reload that lands mid-turn: no snapshot has arrived
   * yet, but the stored row already says the turn is running. Without it the
   * watchdog can never arm and a lost terminal edge strands the checkpoint
   * as if it were the final answer. */
  const lastAssistantStreamStatus = (
    lastAssistantMessage?.meta as Record<string, unknown> | null | undefined
  )?.stream_status;
  const remoteRunInFlight =
    !streaming &&
    (followedRunActive ||
      lastAssistantStreamStatus === "streaming" ||
      lastAssistantStreamStatus === "running");
  const assistantWorking = streaming || remoteRunInFlight;
  const activeAssistantMessage = useMemo(
    () => assistantWorking
      ? [...messages].reverse().find((message) => message.role === "assistant") || null
      : null,
    [messages, assistantWorking],
  );
  const activeAgentActivity = useMemo(
    () => inferAgentActivity(activeAssistantMessage),
    [activeAssistantMessage],
  );
  const streamingConvId = currentSession?.convId;
  const {
    hydrateConversation: hydrateMessageFeedback,
    submit: submitMessageFeedback,
    values: messageFeedback,
  } = useChatMessageFeedback(currentUserId);
  useEffect(() => {
    const conversationId = currentConvId || streamingConvId;
    if (!conversationId) return;
    void hydrateMessageFeedback(conversationId).catch(() => {});
  }, [currentConvId, hydrateMessageFeedback, streamingConvId]);
  const [conversationLoading, setConversationLoading] = useState(false);
  const setSessionMessages = useChatStreamStore((s) => s.setSessionMessages);
  const createDraftSession = useChatStreamStore((s) => s.createDraftSession);
  const startStream = useChatStreamStore((s) => s.startStream);
  const stopStream = useChatStreamStore((s) => s.stopStream);
  const resetSession = useChatStreamStore((s) => s.resetSession);
  const streamingRef = useRef(false);
  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);
  const currentSessionKeyRef = useRef<string | undefined>(currentSessionKey);
  const editorLiveConversationIdRef = useRef<string | undefined>();
  const editorLiveDetailRef = useRef<EditorLiveChatDetail | null>(null);
  const editorLivePendingDetailRef = useRef<EditorLiveChatDetail | null>(null);
  const editorLiveSessionTokenRef = useRef<string | null>(null);
  const editorLiveOwnerRef = useRef<AiEditConversationOwnerScope | null>(null);
  const editorLiveCleanupCoordinator = useMemo(
    () => createAiEditSessionCleanupCoordinator(),
    [],
  );
  const editorLiveOpenRevisionRef = useRef(0);
  const conversationSelectionRevisionRef = useRef(0);
  const resumedRef = useRef(false);
  const editorLiveAppliedRef = useRef<EditorLiveTurnState>({
    content: "",
    phase: "idle",
  });
  useEffect(() => {
    currentSessionKeyRef.current = currentSessionKey;
  }, [currentSessionKey]);
  const showConversationSkeleton = conversationLoading && messages.length === 0;
  const setMessages = useCallback(
    (updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => {
      const key = currentSessionKeyRef.current;
      if (!key) return;
      setSessionMessages(key, updater);
    },
    [setSessionMessages],
  );
  const workflowRunGroups = useMemo(
    () => buildWorkspaceWorkflowRunGroups(messages),
    [messages],
  );
  const hostOwnedWorkflowMessageIds = useMemo(
    () => workflowHostOwnedMessageIds(workflowRunGroups),
    [workflowRunGroups],
  );
  const visibleWorkflowMessageEntries = useMemo(
    () => messages.flatMap((message, index) => (
      isResponseSurfaceSubmissionMessage(message, responseSurfaceSubmissionReceipts)
        || Boolean(message.id && responseSurfaceSubmissionFailureMessageIds.has(message.id))
        || (message.id && hostOwnedWorkflowMessageIds.has(message.id))
        ? []
        : [{ message, index }]
    )),
    [
      hostOwnedWorkflowMessageIds,
      messages,
      responseSurfaceSubmissionFailureMessageIds,
      responseSurfaceSubmissionReceipts,
    ],
  );
  const visibleWorkflowMessages = useMemo(
    () => visibleWorkflowMessageEntries.map(({ message }) => message),
    [visibleWorkflowMessageEntries],
  );
  const workflowMessageResolveMutation = useMutation({
    mutationFn: ({
      messageId,
      choice,
      note,
      payload,
      files,
    }: {
      messageId: string;
      choice: string;
      note?: string;
      payload?: Record<string, unknown>;
      files?: File[];
    }) => resolveGlobalWorkflowMessageAction(
      messageId,
      choice,
      note,
      payload,
      files,
    ),
    onSuccess: (resolved, { messageId }) => {
      setMessages((current) => mergeResolvedWorkflowMessage(
        current,
        messageId,
        resolved,
      ));
    },
  });
  const handleWorkflowMessageResolve = (
    messageId: string,
    choice: string,
    note?: string,
    payload?: Record<string, unknown>,
    files?: File[],
  ) => workflowMessageResolveMutation.mutateAsync({
    messageId,
    choice,
    note,
    payload,
    files,
  });

  const clearEditorLiveSession = useCallback(
    (deleteConversation = false, notifyFailure = true) => {
      // Only the explicit AI Edit ownership ref is deletable. `currentConvId`
      // may still point at the normal chat that was visible one render ago.
      const convId = editorLiveConversationIdRef.current;
      const sessionKey = currentSessionKeyRef.current;
      const sessionToken = editorLiveSessionTokenRef.current;
      const owner = editorLiveOwnerRef.current;
      const detail = editorLiveDetailRef.current;
      const ownsCapturedSession = () => (
        sessionToken
          ? editorLiveSessionTokenRef.current === sessionToken
          : Boolean(detail && editorLiveDetailRef.current === detail)
      );
      conversationSelectionRevisionRef.current += 1;
      editorLiveAppliedRef.current.abortController?.abort();
      if (ownsCapturedSession()) {
        setEditorLiveCleanupStatus(AiEditSessionCleanupStatus.Closing);
      }
      return editorLiveCleanupCoordinator.run(async (): Promise<boolean> => {
        const failCleanup = (error?: unknown) => {
          if (error) console.warn("AI Edit session cleanup failed", error);
          if (ownsCapturedSession()) {
            setEditorLiveCleanupStatus(AiEditSessionCleanupStatus.Failed);
            if (notifyFailure) {
              useToastStore.getState().error(
                t("component.floating_chat.ai_edit_close_failed"),
              );
            }
          }
          return false;
        };
        try {
          if (detail) {
            await detail.adapter.waitForCommit();
            const rolledBack = await rollbackEditorLiveAdapter(detail);
            if (!rolledBack) return failCleanup();
          }
          if (deleteConversation && convId) {
            const deleted = await deleteAiEditConversation(convId, owner);
            if (!deleted) return failCleanup();
            void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          }
        } catch (error) {
          return failCleanup(error);
        }

        if (ownsCapturedSession()) {
          editorLiveConversationIdRef.current = undefined;
          editorLiveDetailRef.current = null;
          editorLivePendingDetailRef.current = null;
          editorLiveSessionTokenRef.current = null;
          editorLiveOwnerRef.current = null;
          editorLiveAppliedRef.current = { content: "", phase: "idle" };
          setEditorLiveSessionActive(false);
          setEditorLiveCleanupStatus(AiEditSessionCleanupStatus.Idle);
          setEditorSessionLabel(null);
          setEditorLiveInfo(null);
          setComposerSeed(null);
          resumedRef.current = false;
          currentSessionKeyRef.current = undefined;
          setCurrentConvId(undefined);
          setDraftSessionKey(undefined);
        }
        if (sessionKey) resetSession(sessionKey);
        return true;
      });
    },
    [editorLiveCleanupCoordinator, queryClient, resetSession],
  );

  // Attach menu
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [kbPickerOpen, setKbPickerOpen] = useState(false);
  const [kbSearch, setKbSearch] = useState("");

  // # file reference autocomplete
  const [hashDropdownOpen, setHashDropdownOpen] = useState(false);
  const [hashQuery, setHashQuery] = useState("");
  const [hashActiveIdx, setHashActiveIdx] = useState(0);
  const [hashTriggerPos, setHashTriggerPos] = useState(-1);

  // Voice — MediaRecorder + Whisper backend.
  // (Replaces window.SpeechRecognition. Works in Firefox + offers
  // billable, controlled-quality transcription via /api/v1/audio/transcribe.)
  const [listening, setListening] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<BlobPart[]>([]);
  const audioStreamRef = useRef<MediaStream | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const composerEditorRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);
  const { autoFollowRef, handleAutoFollowScroll } = useChatAutoFollow();

  const { data: workspaceUsers = [] } = useQuery({
    queryKey: ["floating-chat-mention-users"],
    queryFn: () => api.users.directory(),
  });
  const { data: allAgents = [] } = useQuery({
    queryKey: ["floating-chat-mention-agents"],
    queryFn: () => api.agents.list(),
  });
  const { data: globalFlowEntrypoints = [] } = useQuery({
    queryKey: ["global-chat-flow-entrypoints"],
    queryFn: () => api.chat.listFlowEntrypoints(),
  });
  const workflowInvokeOptions = useMemo<WorkflowInvokeItem[]>(
    () =>
      globalFlowEntrypoints.map((entrypoint: GlobalChatFlowEntrypoint) => ({
        bindingId: entrypoint.binding_id,
        workflowId: entrypoint.workflow_id,
        title: entrypoint.title,
        description: [entrypoint.workspace_name, entrypoint.description]
          .filter(Boolean)
          .join(" · "),
        placeholder: entrypoint.placeholder,
      })),
    [globalFlowEntrypoints],
  );

  const mentionOptions = useMemo<MentionOption[]>(() => {
    const agentOptions = (allAgents as Agent[]).map((agent) => ({
      id: agent.id,
      type: "agent" as const,
      name: agent.name,
      subtitle:
        getAgentDescription(agent) ||
        agent.category ||
        t("component.embedded_chat.assign_this_message_to_an_agent"),
      avatarUrl: agent.avatar_url,
    }));
    const userOptions = (workspaceUsers as UserSummary[]).map((user) => {
      const name =
        user.display_name ||
        user.email;
      return {
        id: user.id,
        type: "user" as const,
        name,
        subtitle: user.email,
        avatarUrl: user.avatar_url,
      };
    });
    return [...agentOptions, ...userOptions];
  }, [allAgents, workspaceUsers]);

  const handleMentionSelect = useCallback((mention: MentionOption) => {
    setSelectedMentions((prev) => {
      if (
        prev.some(
          (item) => item.id === mention.id && item.type === mention.type,
        )
      )
        return prev;
      const next =
        mention.type === "agent"
          ? prev.filter((item) => item.type !== "agent")
          : prev;
      return [...next, mention];
    });
    if (mention.type === "agent") {
      setMentionedAgentId(mention.id);
    }
  }, []);

  const handleComposerChange = useCallback(
    (nextValue: string) => {
      setInput(nextValue);
      setSelectedMentions((prev) =>
        prev.filter((mention) => nextValue.includes(`@${mention.name}`)),
      );
      setMentionedAgentId((prev) => {
        if (!prev) return undefined;
        const mention = selectedMentions.find(
          (item) => item.type === "agent" && item.id === prev,
        );
        return mention && nextValue.includes(`@${mention.name}`)
          ? prev
          : undefined;
      });
    },
    [selectedMentions],
  );

  const handleMentionRemove = useCallback((mention: MentionOption) => {
    setSelectedMentions((prev) =>
      prev.filter(
        (item) => !(item.id === mention.id && item.type === mention.type),
      ),
    );
    if (mention.type === "agent") {
      setMentionedAgentId(undefined);
    }
  }, []);

  /* Helper: parse DB messages into ChatMessage[] */
  const parseMessages = (msgs: any[]): ChatMessage[] =>
    msgs
      .filter(
        (m: any) =>
          !(m.role === "user" && isInternalFilePermissionMessage(m.content)) &&
          !isRedundantApprovalResolutionReceipt(m),
      )
      .map((m: any) => ({
        id: m.id,
        conversation_id: m.conversation_id,
        message_kind: m.message_kind,
        refs: m.refs,
        meta: m.meta,
        pending_action: m.pending_action,
        resolved_at: m.resolved_at,
        resolution: m.resolution,
        // Workflow progress and HITL rows are persisted as system messages.
        // They belong to Manor, never to the signed-in user.
        role: (m.role === "user" ? "user" : "assistant") as "user" | "assistant",
        content: toDisplayText(m.content),
        timestamp: m.created_at,
        updated_at: m.updated_at,
        tool_calls: parseToolCalls(m.tool_calls),
        assistant_blocks: Array.isArray(m.assistant_blocks) ? m.assistant_blocks : undefined,
        hitl_requests: Array.isArray(m.hitl_requests) ? m.hitl_requests : undefined,
        workflow_result:
          m.workflow_result && typeof m.workflow_result === "object"
            ? m.workflow_result
            : undefined,
        attachments: Array.isArray(m.attachments) ? m.attachments : undefined,
        stop_reason: m.stop_reason,
        limit_detail: m.limit_detail,
      }));

  const loadRecentMessages = useCallback(async (convId: string) => {
    const page = await api.chat.getMessagesPage(convId, {
      silent: true,
      limit: 75,
    });
    return parseMessages(page.items || []);
  }, []);

  const handleOpenMessageReference = useCallback(
    async (refItem: ChatMessageDisplayReference) => {
      const directUrl = refItem.openUrl || refItem.previewUrl || refItem.url;
      if (directUrl) {
        window.open(directUrl, "_blank", "noopener,noreferrer");
        return;
      }
      if (!refItem.document_id) return;
      try {
        const blobUrl = await api.documents.download(refItem.document_id);
        window.open(blobUrl, "_blank", "noopener,noreferrer");
        window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
      } catch {
        // The thumbnail card remains visible even if the underlying file was removed.
      }
    },
    [],
  );

  /* ---- Auto-resume most recent conversation when first opened ---- */
  const authOwnerRef = useRef<AiEditConversationOwnerScope | null>(currentAiEditOwner);
  const resetStream = useChatStreamStore((s) => s.reset);
  useEffect(() => {
    if (!currentAiEditOwner) return undefined;
    let cancelled = false;
    let flushing = false;
    let retryDelayMs = AI_EDIT_DELETE_RETRY_MIN_MS;
    let retryTimer: number | undefined;
    const scheduleFlush = (delayMs = 0) => {
      if (cancelled) return;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(() => {
        retryTimer = undefined;
        void flushPendingDeletes();
      }, delayMs);
    };
    const flushPendingDeletes = async () => {
      if (cancelled || flushing) return;
      const activeConversationId = isSameAiEditConversationOwner(
        editorLiveOwnerRef.current,
        currentAiEditOwner,
      ) ? editorLiveConversationIdRef.current : undefined;
      const ids = readPendingAiEditConversationDeletes(currentAiEditOwner).filter(
        (id) => id !== activeConversationId,
      );
      if (ids.length === 0) return;
      flushing = true;
      try {
        const results = await Promise.all(
          ids.map((id) => deleteAiEditConversation(id, currentAiEditOwner)),
        );
        if (!cancelled && results.some(Boolean)) {
          await queryClient.invalidateQueries({ queryKey: ["conversations"] });
        }
      } finally {
        flushing = false;
      }
      const remainingActiveConversationId = isSameAiEditConversationOwner(
        editorLiveOwnerRef.current,
        currentAiEditOwner,
      ) ? editorLiveConversationIdRef.current : undefined;
      const remaining = readPendingAiEditConversationDeletes(currentAiEditOwner).filter(
        (id) => id !== remainingActiveConversationId,
      );
      if (remaining.length > 0) {
        scheduleFlush(retryDelayMs);
        retryDelayMs = Math.min(retryDelayMs * 2, AI_EDIT_DELETE_RETRY_MAX_MS);
      } else {
        retryDelayMs = AI_EDIT_DELETE_RETRY_MIN_MS;
      }
    };
    const handleQueued = () => scheduleFlush();
    const handleOnline = () => {
      retryDelayMs = AI_EDIT_DELETE_RETRY_MIN_MS;
      scheduleFlush();
    };
    scheduleFlush();
    window.addEventListener(AI_EDIT_DELETE_QUEUE_EVENT, handleQueued);
    window.addEventListener("online", handleOnline);
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      window.removeEventListener(AI_EDIT_DELETE_QUEUE_EVENT, handleQueued);
      window.removeEventListener("online", handleOnline);
    };
  }, [currentAiEditOwner, queryClient]);

  useEffect(() => {
    if (!isSameAiEditConversationOwner(authOwnerRef.current, currentAiEditOwner)) {
      const previousEditorConversationId = editorLiveConversationIdRef.current;
      const previousEditorOwner = editorLiveOwnerRef.current || authOwnerRef.current;
      const cleanup = editorLiveDetailRef.current || previousEditorConversationId
        ? clearEditorLiveSession(true, false)
        : Promise.resolve(true);
      conversationSelectionRevisionRef.current += 1;
      editorLiveOpenRevisionRef.current += 1;
      editorLiveAppliedRef.current.abortController?.abort();
      void cleanup.then(async (cleaned) => {
        if (!cleaned && previousEditorConversationId && previousEditorOwner) {
          const deleted = await deleteAiEditConversation(
            previousEditorConversationId,
            previousEditorOwner,
          );
          if (deleted) {
            await queryClient.invalidateQueries({ queryKey: ["conversations"] });
          }
        }
      }).catch((error) => {
        console.warn("AI Edit user-change cleanup failed", error);
      });
      resetStream();
      currentSessionKeyRef.current = undefined;
      setCurrentConvId(undefined);
      setDraftSessionKey(undefined);
      setAttachedFiles([]);
      setComposerSeed(null);
      setEditorSessionLabel(null);
      setEditorLiveInfo(null);
      setEditorLiveSessionActive(false);
      setEditorLiveCleanupStatus(AiEditSessionCleanupStatus.Idle);
      editorLiveConversationIdRef.current = undefined;
      editorLiveDetailRef.current = null;
      editorLivePendingDetailRef.current = null;
      editorLiveSessionTokenRef.current = null;
      editorLiveOwnerRef.current = null;
      editorLiveAppliedRef.current = { content: "", phase: "idle" };
      setSelectedMentions([]);
      setMentionedAgentId(undefined);
      setConversationLoading(false);
      resumedRef.current = false;
    }
    authOwnerRef.current = currentAiEditOwner;
  }, [clearEditorLiveSession, currentAiEditOwner, queryClient, resetStream]);

  useEffect(() => {
    if (!open || resumedRef.current) return;
    if (streamingRef.current) return;
    resumedRef.current = true;
    const selectionRevision = conversationSelectionRevisionRef.current + 1;
    conversationSelectionRevisionRef.current = selectionRevision;
    setConversationLoading(true);
    api.chat.listConversations()
      .then((convs) => {
        if (
          conversationSelectionRevisionRef.current !== selectionRevision
          || editorLiveDetailRef.current
        ) return undefined;
        const latest = (convs || []).find(
          (conv: any) => !conv.agent_id && !conv.workspace_id,
        );
        if (!latest) return undefined;
        setCurrentConvId(latest.id);
        setDraftSessionKey(undefined);
        return loadRecentMessages(latest.id)
          .then((msgs) => {
            if (
              streamingRef.current
              || conversationSelectionRevisionRef.current !== selectionRevision
              || editorLiveDetailRef.current
            ) return;
            setSessionMessages(latest.id, msgs);
          })
          .catch(() => {});
      })
      .catch(() => {})
      .finally(() => setConversationLoading(false));
  }, [loadRecentMessages, open, setSessionMessages]);

  useEffect(() => {
    const handleOpenEditorLiveChat = async (event: Event) => {
      const openRevision = editorLiveOpenRevisionRef.current + 1;
      editorLiveOpenRevisionRef.current = openRevision;
      const detail =
        (event as CustomEvent<EditorLiveChatDetail>).detail || {};
      const pendingCleanup = editorLiveCleanupCoordinator.current();
      if (
        pendingCleanup
        && !(await pendingCleanup)
        && editorLiveSessionTokenRef.current
      ) {
        setOpen(true);
        return;
      }
      if (editorLiveOpenRevisionRef.current !== openRevision) return;
      if (isSameEditorLiveTarget(editorLiveDetailRef.current, detail)) {
        if (streamingRef.current) editorLivePendingDetailRef.current = detail;
        else editorLiveDetailRef.current = detail;
        setEditorLiveInfo(editorLiveDetailRef.current || detail);
        setOpen(true);
        window.setTimeout(() => composerEditorRef.current?.focus(), 0);
        return;
      }
      if (
        editorLiveDetailRef.current
        && !(await clearEditorLiveSession(true))
      ) {
        setOpen(true);
        return;
      }
      if (editorLiveOpenRevisionRef.current !== openRevision) return;
      const owner = currentAiEditOwner || currentAiEditConversationOwner();
      if (!owner) return;
      const key = `editor-live-${Date.now()}-${Math.random()
        .toString(36)
        .slice(2)}`;
      const draftKey = createDraftSession();
      const attachments: AttachedItem[] =
        shouldAttachEditorLiveSourceDocument(detail)
          ? [
              {
                type: "knowledge",
                id: detail.documentId!,
                name: detail.documentName!,
                fileType: detail.fileType || undefined,
                mimeType: detail.mimeType || undefined,
              },
            ]
          : [];

      resumedRef.current = true;
      conversationSelectionRevisionRef.current += 1;
      setOpen(true);
      setCurrentConvId(undefined);
      setDraftSessionKey(draftKey);
      currentSessionKeyRef.current = draftKey;
      setSessionMessages(draftKey, []);
      setInput("");
      setAttachedFiles([]);
      setComposerSeed({ key, attachments });
      editorLiveConversationIdRef.current = undefined;
      editorLiveDetailRef.current = detail;
      editorLivePendingDetailRef.current = null;
      editorLiveSessionTokenRef.current = draftKey;
      editorLiveOwnerRef.current = owner;
      editorLiveAppliedRef.current = {
        sessionKey: draftKey,
        content: "",
        phase: "idle",
      };
      setEditorLiveSessionActive(true);
      setEditorLiveCleanupStatus(AiEditSessionCleanupStatus.Idle);
      setEditorLiveInfo(detail);
      setEditorSessionLabel(
        detail.sessionLabel?.trim() ||
          (detail.documentName ? `Live edit: ${detail.documentName}` : "Live edit"),
      );
      setSelectedMentions([]);
      setMentionedAgentId(undefined);
      window.setTimeout(() => composerEditorRef.current?.focus(), 0);
    };

    const handleUpdateEditorLiveChat = (event: Event) => {
      const detail = (event as CustomEvent<EditorLiveChatDetail>).detail;
      if (!isSameEditorLiveTarget(editorLiveDetailRef.current, detail)) return;
      if (
        streamingRef.current
        || editorLiveAppliedRef.current.phase === "pending"
        || editorLiveAppliedRef.current.phase === "streaming"
      ) {
        editorLivePendingDetailRef.current = detail;
        return;
      }
      editorLiveDetailRef.current = detail;
      setEditorLiveInfo(detail);
    };

    window.addEventListener(
      EDITOR_LIVE_CHAT_EVENT,
      handleOpenEditorLiveChat as EventListener,
    );
    window.addEventListener(
      EDITOR_LIVE_CHAT_UPDATE_EVENT,
      handleUpdateEditorLiveChat as EventListener,
    );
    return () => {
      window.removeEventListener(
        EDITOR_LIVE_CHAT_EVENT,
        handleOpenEditorLiveChat as EventListener,
      );
      window.removeEventListener(
        EDITOR_LIVE_CHAT_UPDATE_EVENT,
        handleUpdateEditorLiveChat as EventListener,
      );
    };
  }, [
    clearEditorLiveSession,
    createDraftSession,
    currentAiEditOwner,
    editorLiveCleanupCoordinator,
    setSessionMessages,
  ]);

  useEffect(() => {
    if (streaming) return;
    const pending = editorLivePendingDetailRef.current;
    if (!pending || !isSameEditorLiveTarget(editorLiveDetailRef.current, pending)) return;
    editorLivePendingDetailRef.current = null;
    editorLiveDetailRef.current = pending;
    setEditorLiveInfo(pending);
  }, [streaming]);

  useEffect(() => {
    const handleOpenFloatingChat = (event: Event) => {
      const detail =
        (event as CustomEvent<OpenFloatingChatDetail>).detail || {};
      const prompt = (detail.prompt || "").trim();
      if (!prompt) return;

      setOpen(true);
      setInput((prev) => {
        const current = prev.trim();
        return current ? `${current}\n\n${prompt}` : prompt;
      });
      window.setTimeout(() => composerEditorRef.current?.focus(), 0);
    };

    window.addEventListener(
      OPEN_FLOATING_CHAT_EVENT,
      handleOpenFloatingChat as EventListener,
    );
    return () =>
      window.removeEventListener(
        OPEN_FLOATING_CHAT_EVENT,
        handleOpenFloatingChat as EventListener,
      );
  }, []);

  /* ---- Reload messages from DB when reopened (catches interrupted streams) ---- */
  const prevOpenRef = useRef(false);
  useEffect(() => {
    const wasOpen = prevOpenRef.current;
    prevOpenRef.current = open;
    // Only reload when transitioning from closed → open
    if (open && !wasOpen) {
      if (streaming) {
        // Stream is still running — sync convId from store
        if (streamingConvId) setCurrentConvId(streamingConvId);
        // Messages are already live from the store — no DB reload needed
      } else if (currentConvId) {
        // Not streaming — reload from DB to get final state
        setConversationLoading(true);
        loadRecentMessages(currentConvId)
          .then((msgs) => {
            if (streamingRef.current) return;
            setSessionMessages(currentConvId, msgs);
          })
          .catch(() => {})
          .finally(() => setConversationLoading(false));
      }
    }
  }, [open, currentConvId, loadRecentMessages, setSessionMessages, streaming, streamingConvId]);

  /* Auto-scroll — throttled during streaming to avoid queuing hundreds of scroll
     animations, and only while the user hasn't scrolled away from the bottom. */
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (scrollTimerRef.current) return; // already scheduled
    scrollTimerRef.current = setTimeout(
      () => {
        scrollTimerRef.current = null;
        if (!autoFollowRef.current) return;
        messagesEndRef.current?.scrollIntoView({
          behavior: "auto",
          block: "end",
        });
      },
      streaming ? 240 : 0,
    );
  }, [messages, streaming, autoFollowRef]);

  /* Opening the panel or switching sessions is a user-initiated jump to the
     latest message — resume following even if they had scrolled up before. */
  useEffect(() => {
    autoFollowRef.current = true;
  }, [open, currentConvId, draftSessionKey, autoFollowRef]);

  useEffect(() => {
    if (!editorLiveSessionActive) return;
    if (streamingRef.current) return;
    const detail = editorLiveDetailRef.current;
    if (!detail) return;
    const last = messages[messages.length - 1];
    if (!last || last.role !== "assistant") return;
    const turn = editorLiveAppliedRef.current;
    if (!["pending", "failed"].includes(turn.phase) || !turn.turnId) return;
    if (last.stream_error || last.stop_reason) return;
    const assistantContent = toDisplayText(last.content);
    const allPayloads = extractRecoverableEditorLivePatchOperationPayloads(assistantContent);
    if (!allPayloads) return;
    const processedOperationCount = Math.min(turn.operationCount || 0, allPayloads.length);
    const payloads = allPayloads.slice(processedOperationCount);
    const recoveredAppliedPatchCount = turn.appliedPatchCount || 0;
    const canFinalizeRecoveredPreview = turn.phase === "failed"
      && recoveredAppliedPatchCount > 0
      && payloads.length === 0;
    if (payloads.length === 0 && !canFinalizeRecoveredPreview) return;
    const beforeContent = turn.content;
    const sessionKey = currentSessionKeyRef.current;
    const turnId = turn.turnId;
    const signal = turn.abortController?.signal;
    if (signal?.aborted) return;
    editorLiveAppliedRef.current = { ...turn, phase: "streaming" };
    void (async () => {
      try {
        let nextContent = beforeContent;
        let applied = 0;
        let consumed = 0;
        let lastPatch = "";
        for (const payload of payloads) {
          consumed += 1;
          const result = applyEditorLivePatch(nextContent, payload);
          if (result.failed.length > 0 || result.content === nextContent) {
            editorLiveAppliedRef.current = {
              ...editorLiveAppliedRef.current,
              operationCount: processedOperationCount + consumed,
            };
            continue;
          }
          const operationDiff = buildEditorLiveDiff(
            nextContent,
            result.content,
            detail.documentName || "current file",
          );
          const accepted = await detail.adapter.preview(result.content, {
            complete: false,
            phase: AiEditApplyPhase.Preview,
            source: "assistant-stream",
            turnId,
            turnBasePatchCount: turn.basePatchCount || 0,
            mode: "patch",
            diff: operationDiff,
            patch: truncateToolText(payload, 5000),
            patchCount: recoveredAppliedPatchCount + applied + 1,
            sourceLabel: "assistant patch",
            signal,
          });
          if (
            accepted === false
            || !isSameEditorLiveTarget(editorLiveDetailRef.current, detail)
            || editorLiveAppliedRef.current.turnId !== turnId
            || signal?.aborted
          ) return;
          await waitForEditorLivePreviewPaint(signal);
          if (
            !isSameEditorLiveTarget(editorLiveDetailRef.current, detail)
            || editorLiveAppliedRef.current.turnId !== turnId
            || signal?.aborted
          ) return;
          nextContent = result.content;
          applied += 1;
          lastPatch = payload;
          editorLiveAppliedRef.current = {
            sessionKey,
            turnId,
            content: nextContent,
            phase: "streaming",
            operationCount: processedOperationCount + consumed,
            appliedPatchCount: recoveredAppliedPatchCount + applied,
            basePatchCount: turn.basePatchCount || 0,
            abortController: turn.abortController,
          };
        }
        if (applied === 0 && !canFinalizeRecoveredPreview) {
          editorLiveAppliedRef.current = {
            ...editorLiveAppliedRef.current,
            phase: "failed",
          };
          return;
        }
        const diff = buildEditorLiveDiff(
          beforeContent,
          nextContent,
          detail.documentName || "current file",
        );
        const completed = await detail.adapter.complete(nextContent, {
          complete: true,
          phase: AiEditApplyPhase.Complete,
          source: "assistant-stream",
          turnId,
          turnBasePatchCount: turn.basePatchCount || 0,
          mode: "patch",
          diff,
          patch: truncateToolText(lastPatch, 5000),
          patchCount: recoveredAppliedPatchCount + applied,
          sourceLabel: "assistant patch",
          signal,
        });
        if (
          completed === false
          || !isSameEditorLiveTarget(editorLiveDetailRef.current, detail)
          || editorLiveAppliedRef.current.turnId !== turnId
          || signal?.aborted
        ) return;
        editorLiveAppliedRef.current = {
          sessionKey,
          turnId,
          content: nextContent,
          phase: "complete",
          operationCount: processedOperationCount + consumed,
          appliedPatchCount: recoveredAppliedPatchCount + applied,
          basePatchCount: turn.basePatchCount || 0,
          abortController: turn.abortController,
        };
      } catch (error) {
        if (editorLiveAppliedRef.current.turnId === turnId && !signal?.aborted) {
          editorLiveAppliedRef.current = {
            ...editorLiveAppliedRef.current,
            phase: "failed",
          };
        }
        console.warn("Recovered editor live edit apply failed", error);
      }
    })();
  }, [editorLiveSessionActive, messages]);

  /* Listen for video completion — reload messages so VideoCard picks up result */
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (
        detail?.conversation_id &&
        detail.conversation_id === currentConvId &&
        !streamingRef.current
      ) {
        loadRecentMessages(detail.conversation_id)
          .then((msgs) => {
            setMessages(msgs);
          })
          .catch(() => {});
      }
    };
    window.addEventListener("manor:video-ready", handler);
    return () => window.removeEventListener("manor:video-ready", handler);
  }, [currentConvId, loadRecentMessages, setMessages]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (
        detail?.conversation_id === currentConvId &&
        !streamingRef.current
      ) {
        const messageId =
          typeof detail?.message_id === "string" ? detail.message_id : undefined;
        if (shouldIgnoreLocallyStoppedStreamUpdate(currentConvId, messageId)) {
          return;
        }
        loadRecentMessages(detail.conversation_id)
          .then((msgs) => {
            if (shouldIgnoreLocallyStoppedStreamUpdate(currentConvId, messageId)) {
              return;
            }
            setMessages(msgs);
          })
          .catch(() => {});
      }
    };
    window.addEventListener("manor:conversation-message", handler);
    return () => window.removeEventListener("manor:conversation-message", handler);
  }, [currentConvId, loadRecentMessages, setMessages]);

  /* Follow a turn this tab is not streaming — see EmbeddedChat for the full note. */
  useEffect(() => {
    if (!currentConvId) return;
    const handler = (event: Event) => {
      const snapshot = (event as CustomEvent).detail as ChatStreamSnapshot | undefined;
      if (!snapshot || snapshot.conversation_id !== currentConvId) return;
      const streamState = useChatStreamStore.getState();
      const liveKey = streamState.getSessionKeyForConversation(currentConvId);
      if (liveKey && streamState.sessions[liveKey]?.streaming) return;
      if (hasLocallyStreamedConversation(currentConvId)) return;
      if (
        shouldIgnoreLocallyStoppedStreamUpdate(
          currentConvId,
          snapshot.message_id,
          snapshot.status,
        )
      ) {
        setFollowedRunActive(false);
        followedRunPollsRef.current = 0;
        return;
      }

      const seqKey = snapshot.message_id || currentConvId;
      const seq = typeof snapshot.seq === "number" ? snapshot.seq : 0;
      if (seq && seq <= (lastSnapshotSeqRef.current[seqKey] || 0)) return;
      lastSnapshotSeqRef.current[seqKey] = seq;

      const known = liveKey ? streamState.sessions[liveKey]?.messages || [] : [];
      const needsHistory = streamSnapshotNeedsHistory(known, snapshot);
      setSessionMessages(currentConvId, (prev) =>
        mergeChatStreamSnapshot(prev, snapshot),
      );
      const terminal = isTerminalStreamSnapshot(snapshot);
      setFollowedRunActive(!terminal);
      setFollowedRunSilenceKey((value) => value + 1);
      followedRunPollsRef.current = 0;

      // A snapshot projects the live turn only; the stored row is the authority
      // for attachments and approval cards, which settle at the end.
      const refetchKey = `${snapshot.message_id || ""}:${terminal ? "final" : "history"}`;
      if ((terminal || needsHistory) && snapshotRefetchedRef.current !== refetchKey) {
        snapshotRefetchedRef.current = refetchKey;
        loadRecentMessages(currentConvId)
          .then((msgs) => {
            // Guard at resolution time: the user may have started their own
            // turn during the fetch, and replacing the transcript then glues
            // the next SSE token onto the previous reply.
            if (isConversationStreamingNow(currentConvId)) return;
            setSessionMessages(currentConvId, msgs);
          })
          .catch(() => {});
      }
    };
    window.addEventListener("manor:chat-stream-snapshot", handler);
    return () =>
      window.removeEventListener("manor:chat-stream-snapshot", handler);
  }, [currentConvId, loadRecentMessages, setSessionMessages]);

  /* Nothing replays a snapshot lost to a socket reconnect — ask the API rather
   * than spin forever over a reply that already finished. Gated on
   * remoteRunInFlight so it arms straight from the stored row's stream_status
   * after a reload, and disarms the moment this tab streams for itself. */
  useEffect(() => {
    if (!currentConvId || !remoteRunInFlight) return;
    const timer = window.setTimeout(() => {
      if (shouldIgnoreLocallyStoppedStreamUpdate(currentConvId)) {
        setFollowedRunActive(false);
        return;
      }
      if (followedRunPollsRef.current >= FOLLOWED_RUN_MAX_POLLS) {
        setFollowedRunActive(false);
        return;
      }
      followedRunPollsRef.current += 1;
      loadRecentMessages(currentConvId)
        .then((msgs) => {
          if (isConversationStreamingNow(currentConvId)) return;
          setSessionMessages(currentConvId, msgs);
          setFollowedRunActive(false);
          // Re-arm: the refetched row may still claim to be streaming.
          setFollowedRunSilenceKey((value) => value + 1);
        })
        .catch(() => {
          // The poll failing is the offline case the watchdog exists for —
          // keep trying at the same cadence rather than freezing mid-run.
          setFollowedRunSilenceKey((value) => value + 1);
        });
    }, FOLLOWED_RUN_SILENCE_MS);
    return () => window.clearTimeout(timer);
  }, [
    currentConvId,
    followedRunSilenceKey,
    loadRecentMessages,
    remoteRunInFlight,
    setSessionMessages,
  ]);

  /* This tab streaming for itself ends any followed run: its own SSE is the
   * authority now, and a stale "still working" flag would outlive the reply. */
  useEffect(() => {
    if (streaming) setFollowedRunActive(false);
  }, [streaming]);

  useEffect(() => {
    setFollowedRunActive(false);
    lastSnapshotSeqRef.current = {};
    snapshotRefetchedRef.current = undefined;
    followedRunPollsRef.current = 0;
  }, [currentConvId]);

  /* Auto-resize textarea */
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height =
        Math.min(textareaRef.current.scrollHeight, 120) + "px";
    }
  }, [input]);

  /* Close attach menu on outside click */
  useEffect(() => {
    if (!attachMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (
        attachMenuRef.current &&
        !attachMenuRef.current.contains(e.target as Node)
      ) {
        setAttachMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [attachMenuOpen]);

  /* ---- Knowledge base docs ---- */
  const { data: kbDocs } = useQuery({
    queryKey: ["documents", "floating-kb", kbSearch, "user-visible"],
    queryFn: () =>
      api.documents.list({
        search: kbSearch || undefined,
        include_generated_assets: false,
        limit: 30,
      }),
    enabled: kbPickerOpen,
  });

  /* ---- # file reference autocomplete (debounced to avoid per-keystroke fetches) ---- */
  const debouncedHashQuery = useDebounced(hashQuery, 250);
  const { data: hashDocs } = useQuery({
    queryKey: ["documents", "hash-autocomplete", debouncedHashQuery, "floating", "user-visible"],
    queryFn: () =>
      api.documents.list({
        search: debouncedHashQuery || undefined,
        include_generated_assets: false,
        limit: 50,
      }),
    enabled: hashDropdownOpen,
  });
  const hashFiltered = (hashDocs?.items || []).slice(0, 20);

  /* ---- File select (kept as raw File objects until send) ---- */
  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files;
      if (files) {
        Array.from(files).forEach((file) => {
          setAttachedFiles((prev) => [
            ...prev,
            { name: file.name, type: "file", file },
          ]);
        });
      }
      e.target.value = "";
    },
    [],
  );

  const addKbDoc = (doc: { id: string; name: string; fs_path?: string | null }) => {
    if (attachedFiles.some((f) => f.id === doc.id)) return;
    setAttachedFiles((prev) => [
      ...prev,
      {
        name: doc.name,
        id: doc.id,
        fsPath: doc.fs_path || undefined,
        type: "knowledge",
      },
    ]);
    setKbPickerOpen(false);
    setKbSearch("");
  };

  const removeAttachment = (idx: number) => {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  /* ---- # trigger detection ---- */
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const val = e.target.value;
      setInput(val);

      // Detect # trigger: find last # that starts a word
      const cursorPos = e.target.selectionStart || val.length;
      const textBeforeCursor = val.substring(0, cursorPos);
      const hashIdx = textBeforeCursor.lastIndexOf("#");

      if (
        hashIdx >= 0 &&
        (hashIdx === 0 || /\s/.test(textBeforeCursor[hashIdx - 1]))
      ) {
        const query = textBeforeCursor.substring(hashIdx + 1);
        // Close if space found after query start (user moved on)
        if (query.includes(" ") || query.includes("\n")) {
          setHashDropdownOpen(false);
        } else {
          setHashDropdownOpen(true);
          setHashQuery(query);
          setHashTriggerPos(hashIdx);
          setHashActiveIdx(0);
        }
      } else {
        setHashDropdownOpen(false);
      }
    },
    [],
  );

  const selectHashDoc = useCallback(
    (doc: { id: string; name: string; fs_path?: string | null }) => {
      // Remove the #query text from input
      const before = input.substring(0, hashTriggerPos);
      const cursorPos = textareaRef.current?.selectionStart || input.length;
      const after = input.substring(cursorPos);
      setInput(`${before}${after}`);
      // Add as attachment chip (same as KB doc picker)
      setAttachedFiles((prev) => {
        if (prev.some((f) => f.id === doc.id)) return prev;
        return [
          ...prev,
          {
            name: doc.name,
            id: doc.id,
            fsPath: doc.fs_path || undefined,
            type: "knowledge",
          },
        ];
      });
      setHashDropdownOpen(false);
      setHashQuery("");
      setHashTriggerPos(-1);
      setTimeout(() => composerEditorRef.current?.focus(), 0);
    },
    [input, hashTriggerPos],
  );

  /* ---- Voice recording ----
   *
   * Records mic audio in-browser via MediaRecorder, then uploads to
   * /api/v1/audio/transcribe which runs Whisper server-side and bills
   * the call. Replaces the previous browser-side SpeechRecognition
   * path (Chrome-only, free, but uncontrolled quality + privacy).
   */
  const stopRecording = useCallback(() => {
    const rec = mediaRecorderRef.current;
    if (rec && rec.state !== "inactive") {
      rec.stop();
    }
    audioStreamRef.current?.getTracks().forEach((t) => t.stop());
    audioStreamRef.current = null;
    setListening(false);
  }, []);

  const toggleVoice = useCallback(async () => {
    if (listening) {
      stopRecording();
      return;
    }

    if (
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === "undefined"
    ) {
      setInput(
        (prev) => prev || "Voice input is not supported in this browser.",
      );
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      console.warn("Microphone permission denied", err);
      setInput((prev) => prev || "Microphone permission denied.");
      return;
    }

    audioStreamRef.current = stream;
    audioChunksRef.current = [];

    // webm/opus is the broadly-supported default; Whisper accepts it.
    // Fall back to whatever MediaRecorder picks if the explicit MIME
    // isn't available (e.g. Safari < 17).
    const preferred = "audio/webm;codecs=opus";
    const mimeType = MediaRecorder.isTypeSupported(preferred) ? preferred : "";
    const recorder = new MediaRecorder(
      stream,
      mimeType ? { mimeType } : undefined,
    );
    mediaRecorderRef.current = recorder;

    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunksRef.current.push(e.data);
    };

    recorder.onstop = async () => {
      const blob = new Blob(audioChunksRef.current, {
        type: recorder.mimeType || "audio/webm",
      });
      audioChunksRef.current = [];
      mediaRecorderRef.current = null;

      if (blob.size < 1024) {
        // <1 KB = mic was open for almost no time / silent. Skip.
        return;
      }

      setTranscribing(true);
      try {
        const ext = (recorder.mimeType || "audio/webm").includes("mp4")
          ? "mp4"
          : "webm";
        const fd = new FormData();
        fd.append("file", blob, `voice.${ext}`);
        const lang = navigator.language?.split("-")[0];
        if (lang) fd.append("language", lang);

        const token = localStorage.getItem("manor_token");
        const res = await fetch("/api/v1/audio/transcribe", {
          method: "POST",
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          body: fd,
        });
        if (!res.ok) {
          const detail = await res.text();
          console.warn("Transcribe failed:", res.status, detail);
          setInput((prev) => prev || `(Transcription failed: ${res.status})`);
          return;
        }
        const data = await res.json();
        const text = (data.text || "").trim();
        if (text) {
          // Append rather than replace so the user can record multiple
          // segments and refine with typing in between.
          setInput((prev) => (prev ? `${prev.trimEnd()} ${text}` : text));
        }
      } catch (err) {
        console.warn("Transcribe error", err);
      } finally {
        setTranscribing(false);
      }
    };

    recorder.start();
    setListening(true);
  }, [listening, stopRecording]);

  // Stop the mic if the chat closes mid-recording.
  useEffect(() => {
    return () => {
      if (mediaRecorderRef.current?.state === "recording") {
        mediaRecorderRef.current.stop();
      }
      audioStreamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  /* ---- Send message (SSE streaming) ---- */
  const handleSend = useCallback(
    async (
      textInput?: string,
      footerAttachments?: AttachedItem[],
      manualSkills: ManualSkillItem[] = [],
      workflow?: WorkflowInvokeItem | null,
      sendContext?: ChatComposerSendContext,
      sendOptions: FloatingChatSendOptions = {},
    ) => {
      const isResponseSurfaceSubmission = Boolean(sendOptions.responseSurfaceSubmission);
      const rawText = (
        typeof textInput === "string" ? textInput : input
      ).trim();
      const selectedWorkflow = isResponseSurfaceSubmission ? null : workflow || null;
      const effectiveManualSkills = selectedWorkflow || isResponseSurfaceSubmission ? [] : manualSkills;
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
      const text = stripWorkflowInvokeToken(
        stripManualSkillTokens(rawText, manualSkills),
        selectedWorkflow,
      );
      const attachmentSnapshot = isResponseSurfaceSubmission
        ? []
        : footerAttachments || attachedFiles;
      if (
        !text &&
        attachmentSnapshot.length === 0 &&
        effectiveManualSkills.length === 0 &&
        !selectedWorkflow
      )
        return false;
      let sessionKey = currentSessionKeyRef.current;
      if (!sessionKey) {
        sessionKey = createDraftSession();
        setDraftSessionKey(sessionKey);
      }
      if (useChatStreamStore.getState().sessions[sessionKey]?.streaming) return false;

      // Sending is an explicit jump back to the newest message.
      autoFollowRef.current = true;

      // Stop voice if active — also drains the recorder so any in-flight
      // chunks transcribe before send (the user can edit the result
      // before pressing send if they want).
      if (listening && !isResponseSurfaceSubmission) {
        stopRecording();
      }

      const now = new Date().toISOString();

      const mentionsSnapshot = selectedMentions.filter((mention) =>
        rawText.includes(`@${mention.name}`),
      );
      const peopleMentions = mentionsSnapshot.filter(
        (mention) => mention.type === "user",
      );
      const mentionMeta = mentionsSnapshot.map((mention) => ({
        id: mention.id,
        type: mention.type,
        name: mention.name,
        subtitle: mention.subtitle,
        avatarUrl: mention.avatarUrl,
      }));
      const mentionContext =
        peopleMentions.length > 0
          ? `\n\n[Referenced people: ${peopleMentions.map((mention) => `${mention.name} <id:${mention.id}>${mention.subtitle ? ` ${mention.subtitle}` : ""}`).join(", ")}]`
          : "";

      const sentAttachments = [...attachmentSnapshot];
      if (!isResponseSurfaceSubmission) {
        setInput("");
        setSelectedMentions([]);
        setMentionedAgentId(undefined);
        setAttachedFiles([]);
      }
      const requestChatMode = sendOptions.responseSurfaceSubmission
        ? undefined
        : selectedWorkflow
        ? "flows"
        : !sendOptions.forceAutoMode && !editorLiveSessionActive && chatMode !== "auto"
          ? chatMode
          : undefined;
      const requestChatModePayload =
        !requestChatMode || selectedWorkflow ? undefined : chatModePayload;

      // Extract inline #[name](doc:id) refs in a single pass (used for both display and send)
      const inlineDocIds: string[] = [];
      const cleanText = text.replace(
        /#\[([^\]]*)\]\(doc:([^)]+)\)/g,
        (_match, name, docId) => {
          inlineDocIds.push(docId);
          return `#${name}`;
        },
      );
      const displayContent = [
        cleanText,
        selectedWorkflow ? `[${t("nav.flows")}: ${selectedWorkflow.title}]` : "",
      ].filter(Boolean).join("\n\n");

      const visibleRequest =
        `${cleanText}${mentionContext}`.trim() ||
        (selectedWorkflow
          ? workflowInvokeMessage(selectedWorkflow)
          : "Use the manually selected skill with the current conversation context.");
      const liveEditSessionDetail = editorLiveSessionActive
        ? editorLiveDetailRef.current
        : null;
      const liveEditSessionToken = liveEditSessionDetail
        ? editorLiveSessionTokenRef.current
        : null;
      const liveEditOwner = liveEditSessionDetail
        ? editorLiveOwnerRef.current
        : null;
      const isLiveEdit = Boolean(liveEditSessionDetail);
      const liveEditContent = liveEditSessionDetail?.adapter.read();
      const liveEditTurnBasePatchCount = Math.max(
        0,
        liveEditSessionDetail?.adapter.getTurnPreviewState().changeCount || 0,
      );
      const liveEditHadReviewablePreview = hasReviewableEditorLivePreview(
        { changeCount: liveEditTurnBasePatchCount },
      );
      const liveEditDetail = liveEditSessionDetail
        ? {
            ...liveEditSessionDetail,
            ...(liveEditSessionDetail.getTurnMetadata?.() || {}),
          }
        : null;
      const liveEditTurnId = liveEditSessionDetail
        ? `turn-${Date.now()}-${Math.random().toString(36).slice(2)}`
        : undefined;
      const sendText =
        liveEditDetail && typeof liveEditContent === "string"
          ? buildEditorLiveEditRequest(
              liveEditDetail,
              visibleRequest,
              liveEditContent,
            )
          : visibleRequest;
      const liveEditInitialTools = isLiveEdit
        ? [
            makeEditorLiveProgressTool(
              "read_current_file",
              typeof liveEditContent === "string" ? "success" : "error",
              {
                file: liveEditDetail?.documentName || "current file",
              },
              {
                status: typeof liveEditContent === "string" ? "ok" : "failed",
                bytes:
                  typeof liveEditContent === "string"
                    ? liveEditContent.length
                    : 0,
              },
            ),
            makeEditorLiveProgressTool(
              "generate_patch",
              "pending",
              {
                file: liveEditDetail?.documentName || "current file",
                request: visibleRequest,
              },
            ),
          ]
        : undefined;

      if (liveEditDetail) {
        editorLiveAppliedRef.current.abortController?.abort();
        const abortController = new AbortController();
        editorLiveAppliedRef.current = {
          sessionKey,
          turnId: liveEditTurnId,
          content: liveEditContent || "",
          phase: "pending",
          operationCount: 0,
          appliedPatchCount: 0,
          basePatchCount: liveEditTurnBasePatchCount,
          abortController,
        };
        const beganTurn = await liveEditDetail.adapter.beginTurn({
          complete: false,
          phase: AiEditApplyPhase.Preview,
          source: "assistant-stream",
          turnId: liveEditTurnId,
          turnBasePatchCount: liveEditTurnBasePatchCount,
          patchCount: 0,
          mode: "patch",
          sourceLabel: "assistant turn",
          signal: abortController.signal,
        });
        if (beganTurn === false || abortController.signal.aborted) {
          editorLiveAppliedRef.current = {
            sessionKey,
            content: liveEditContent || "",
            phase: liveEditHadReviewablePreview ? "complete" : "failed",
            operationCount: 0,
            appliedPatchCount: 0,
            basePatchCount: liveEditTurnBasePatchCount,
          };
          return false;
        }
      }

      const ownsLiveEditTurn = () => !liveEditSessionDetail || Boolean(
        liveEditSessionToken
        && editorLiveSessionTokenRef.current === liveEditSessionToken
        && isSameEditorLiveTarget(editorLiveDetailRef.current, liveEditSessionDetail)
        && editorLiveAppliedRef.current.turnId === liveEditTurnId
        && !editorLiveAppliedRef.current.abortController?.signal.aborted
      );

      let liveEditAttachmentFiles: File[] = [];
      if (isLiveEdit && liveEditDetail?.getAttachmentFiles) {
        try {
          liveEditAttachmentFiles = await liveEditDetail.getAttachmentFiles();
        } catch (err) {
          console.warn("Editor live edit attachment capture failed", err);
        }
      }
      if (!ownsLiveEditTurn()) return false;

      // Separate local files and KB document IDs (merge inline refs). Live
      // editor files are hidden from the visible message but available to the
      // model/tooling for the current turn.
      const localFiles = [
        ...sentAttachments
        .filter((a) => a.type === "file" && a.file)
        .map((a) => a.file!),
        ...liveEditAttachmentFiles,
      ];
      const documentIds = [
        ...sentAttachments
          .filter((a) => a.type === "knowledge" && a.id)
          .map((a) => a.id!),
        ...inlineDocIds,
      ];
      const retryRequest: ChatRetryRequest | undefined = !isLiveEdit && !selectedWorkflow
        ? {
            message: sendText,
            conversationId: currentConvId,
            documentIds: documentIds.length > 0 ? documentIds : undefined,
            agentId: isResponseSurfaceSubmission
              ? undefined
              : sendOptions.forceOwnerChat
                ? undefined
                : mentionedAgentId,
            localWorkerId: sendContext?.localWorkerId,
            chatMode: requestChatMode,
            chatModePayload: requestChatModePayload,
            manualSkillRefs,
            responseSurfaceSubmission: sendOptions.responseSurfaceSubmission,
          }
        : undefined;
      if (retryRequest) savePendingChatRetry(retryRequest);

      const sentAttachmentSnapshots = await Promise.all(
        sentAttachments.map(createChatMessageAttachmentSnapshot),
      );
      if (!ownsLiveEditTurn()) return false;

      const msgsBeforeSend = [
        ...messages,
        {
          role: "user" as const,
          content: displayContent,
          timestamp: now,
          attachments:
            sentAttachmentSnapshots.length > 0 ? sentAttachmentSnapshots : undefined,
          mentions: mentionMeta.length > 0 ? mentionMeta : undefined,
          manualSkills:
            manualSkills.length > 0
              ? manualSkills.map((skill) => ({
                  id: skill.id,
                  name: manualSkillLabel(skill),
                  slug: skill.slug || undefined,
                }))
              : undefined,
          chatMode: requestChatMode,
          chatModePayload: requestChatMode ? chatModePayload : undefined,
          meta: sendOptions.responseSurfaceSubmission
            ? responseSurfaceSubmissionMeta(sendOptions.responseSurfaceSubmission)
            : undefined,
        },
        {
          role: "assistant" as const,
          content: "",
          timestamp: now,
          tool_calls: liveEditInitialTools,
          retryRequest,
        },
      ];

      const updateLiveEditProgress = (tool: ToolCall) => {
        setSessionMessages(sessionKey, (prev) =>
          withEditorLiveProgress(prev, tool),
        );
      };
      const adoptPendingLiveEditDetail = () => {
        const pending = editorLivePendingDetailRef.current;
        if (!pending || !isSameEditorLiveTarget(editorLiveDetailRef.current, pending)) return;
        editorLivePendingDetailRef.current = null;
        editorLiveDetailRef.current = pending;
        setEditorLiveInfo(pending);
      };
      let liveEditClosed = false;
      const closeCompletedLiveEdit = () => {
        if (!isLiveEdit || liveEditClosed) return;
        const applied = editorLiveAppliedRef.current;
        if (
          applied.phase === "complete" &&
          applied.content.trim()
        ) {
          liveEditClosed = true;
          // Keep the live-edit panel open after a successful apply so the
          // user can review the assistant's result, inspect tool status, and
          // continue with follow-up edits. The explicit close button still
          // rolls back any unaccepted preview and deletes the temporary
          // conversation that carries this live-edit session's context.
        }
      };

      let sendSucceeded = true;
      let responseSurfaceResult: ResponseSurfaceSubmissionResult = {
        status: "succeeded",
        serverAccepted: false,
        terminalObserved: false,
      };
      if (!ownsLiveEditTurn()) return false;
      await startStream(
        async (streamSignal) => {
          const response = selectedWorkflow
            ? await api.chat.streamFlowEntrypoint(
                selectedWorkflow.bindingId,
                cleanText || workflowInvokeMessage(selectedWorkflow),
                currentConvId,
                {
                  files: localFiles.length > 0 ? localFiles : undefined,
                  documentIds: documentIds.length > 0 ? documentIds : undefined,
                  localWorkerId: sendContext?.localWorkerId,
                },
              )
            : await api.chat.stream(sendText, currentConvId, {
                files: localFiles.length > 0 ? localFiles : undefined,
                documentIds: documentIds.length > 0 ? documentIds : undefined,
                agentId: isResponseSurfaceSubmission
                  ? undefined
                  : sendOptions.forceOwnerChat
                    ? undefined
                    : mentionedAgentId,
                localWorkerId: sendContext?.localWorkerId,
                chatMode: requestChatMode,
                chatModePayload: requestChatModePayload,
                manualSkillRefs,
                responseSurfaceSubmission: sendOptions.responseSurfaceSubmission,
                editorContext: isLiveEdit
                  ? {
                      target_kind: liveEditDetail?.adapter.target.kind,
                      target_id: liveEditDetail?.adapter.target.id,
                      path: liveEditDetail?.sourcePath,
                      sourcePath: liveEditDetail?.sourcePath,
                      document_id: liveEditDetail?.documentId,
                      documentName: liveEditDetail?.documentName,
                      fileType: liveEditDetail?.fileType,
                      mimeType: liveEditDetail?.mimeType,
                      editorType: liveEditDetail?.editorType,
                      supportsImageGeneration: Boolean(
                        liveEditDetail?.supportsImageGeneration ||
                        liveEditDetail?.applyGeneratedImage,
                      ),
                      currentDocumentContent:
                        typeof liveEditContent === "string" ? liveEditContent : undefined,
                    }
                  : undefined,
                conversationSurface: isLiveEdit
                  ? ConversationSurfaceKind.AiEdit
                  : undefined,
                signal: streamSignal,
              });
          if (!liveEditDetail || !liveEditSessionDetail || !liveEditTurnId) return response;
          return pipeEditorLiveEditStream(
            response,
            liveEditDetail,
            sessionKey,
            liveEditTurnId,
            editorLiveAppliedRef,
            () =>
              isSameEditorLiveTarget(
                editorLiveDetailRef.current,
                liveEditSessionDetail,
              )
              && editorLiveAppliedRef.current.turnId === liveEditTurnId
              && !editorLiveAppliedRef.current.abortController?.signal.aborted,
            updateLiveEditProgress,
            closeCompletedLiveEdit,
            adoptPendingLiveEditDetail,
            liveEditContent || "",
            liveEditHadReviewablePreview,
            liveEditTurnBasePatchCount,
          );
        },
        currentConvId,
        msgsBeforeSend,
        (newConvId) => {
          const ownsLiveEditSession = Boolean(
            liveEditSessionDetail
            && isSameEditorLiveTarget(
              editorLiveDetailRef.current,
              liveEditSessionDetail,
            )
            && editorLiveAppliedRef.current.turnId === liveEditTurnId
            && !editorLiveAppliedRef.current.abortController?.signal.aborted,
          );
          if (ownsLiveEditSession) {
            editorLiveConversationIdRef.current = newConvId;
            if (liveEditOwner) {
              // An active AI Edit conversation must not sit in this tab's
              // cleanup queue. Explicit close/user change queues and deletes
              // it; sessionStorage keeps other tabs' sessions isolated.
              removePendingAiEditConversationDelete(liveEditOwner, newConvId);
            }
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          } else if (isLiveEdit) {
            // Closing during the first response can happen before the server
            // yields its new conversation id. Delete that late id instead of
            // leaving a resumable AI Edit conversation behind.
            void deleteAiEditConversation(newConvId, liveEditOwner);
          }
          if (!isLiveEdit && currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          }
        },
        sessionKey,
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
      if (!sendSucceeded) {
        const applied = editorLiveAppliedRef.current;
        const ownsFailedLiveEdit = Boolean(
          isLiveEdit
          && liveEditSessionDetail
          && applied.turnId === liveEditTurnId
          && isSameEditorLiveTarget(editorLiveDetailRef.current, liveEditSessionDetail)
        );
        if (ownsFailedLiveEdit && liveEditSessionDetail) {
          applied.abortController?.abort();
          const restoredPreviousPreview = await restoreEditorLiveTurn(
            liveEditSessionDetail,
            liveEditContent || "",
            liveEditHadReviewablePreview,
            undefined,
            liveEditTurnId,
            liveEditTurnBasePatchCount,
          );
          if (
            editorLiveAppliedRef.current.turnId === liveEditTurnId
            && isSameEditorLiveTarget(editorLiveDetailRef.current, liveEditSessionDetail)
          ) {
            editorLiveAppliedRef.current = {
              sessionKey,
              content: liveEditContent || "",
              phase: restoredPreviousPreview ? "complete" : "failed",
              operationCount: 0,
              appliedPatchCount: 0,
              basePatchCount: liveEditTurnBasePatchCount,
            };
          }
        } else if (
          applied.turnId === liveEditTurnId
          && (applied.phase === "pending" || applied.phase === "streaming")
        ) {
          editorLiveAppliedRef.current = { ...applied, phase: "failed" };
        }
        adoptPendingLiveEditDetail();
        return isResponseSurfaceSubmission ? responseSurfaceResult : false;
      }
      clearPendingChatRetry();
      if (!isResponseSurfaceSubmission && requestChatMode) resetChatModeAfterTurn();
      if (isLiveEdit) {
        window.setTimeout(closeCompletedLiveEdit, 350);
        window.setTimeout(closeCompletedLiveEdit, 1200);
        return true;
      }
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      // The agent may have created or changed Knowledge documents during this
      // turn; without this, a mounted Knowledge page never learns about them
      // (no polling, refetchOnWindowFocus off, 60s staleTime).
      invalidateKnowledgeQueries(queryClient);
      return isResponseSurfaceSubmission
        ? responseSurfaceResult
        : true;
    },
    [
      input,
      currentConvId,
      currentUserId,
      attachedFiles,
      queryClient,
      listening,
      messages,
      startStream,
      setSessionMessages,
      selectedMentions,
      mentionedAgentId,
      editorLiveSessionActive,
      chatMode,
      chatModePayload,
      resetChatModeAfterTurn,
      stopRecording,
      createDraftSession,
      toast,
    ],
  );

  const handleResponseSurfaceSubmit = useCallback(
    async (receipt: ResponseSurfaceSubmissionReceipt) => {
      const rollback = () => {
        setMessages((current) => (
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
            setMessages((current) => settleResponseSurfaceSubmissionFailure(
              current,
              receipt.eventId,
              accepted.status === "cancelled" ? "interrupted" : "failed",
            ));
          }
        } else if (accepted === false) rollback();
        return accepted;
      } catch (error) {
        rollback();
        throw error;
      }
    },
    [handleSend, setMessages],
  );

  const createWorkspaceFromRecommendation = useCallback(
    async (prompt: string) => {
      await handleSend(prompt, [], [], null, undefined, {
        forceAutoMode: true,
        forceOwnerChat: true,
      });
    },
    [handleSend],
  );
  const {
    dismissedWorkspaceRecommendations,
    dismissWorkspaceRecommendation,
    handleWorkspaceRecommendationAdd,
    handleWorkspaceRecommendationCreate,
    handleWorkspaceRecommendationOpen,
    handleWorkspaceRecommendationOptOut,
    workspaceRecommendationBusyKey,
    workspaceRecommendationPreferenceBusy,
    workspaceRecommendationPreferencesReady,
    workspaceRecommendationsSuppressed,
  } = useWorkspaceRecommendationActions({
    enabled: open,
    createWorkspace: createWorkspaceFromRecommendation,
  });

  const handleStopRequest = useCallback(() => {
    const convId = currentConvId || streamingConvId;
    const hitlIds = pendingHITLIds(messages);
    if (convId) {
      void api.chat.cancelPendingFileApprovals(convId, hitlIds).then(
        () => queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        () => undefined,
      );
    }
    editorLiveAppliedRef.current.abortController?.abort();
    void stopStream(currentSessionKeyRef.current);
  }, [currentConvId, streamingConvId, messages, stopStream, queryClient]);

  const closeEditorLiveSession = useCallback(async () => {
    const pendingCleanup = editorLiveCleanupCoordinator.current();
    if (pendingCleanup) return pendingCleanup;
    setEditorLiveCleanupStatus(AiEditSessionCleanupStatus.Closing);
    editorLiveAppliedRef.current.abortController?.abort();
    if (streamingRef.current) {
      const stopped = await stopStream(currentSessionKeyRef.current);
      if (!stopped) {
        setEditorLiveCleanupStatus(AiEditSessionCleanupStatus.Failed);
        useToastStore.getState().error(
          t("component.floating_chat.ai_edit_close_failed"),
        );
        return false;
      }
    }
    return clearEditorLiveSession(true);
  }, [clearEditorLiveSession, editorLiveCleanupCoordinator, stopStream]);

  const handleCloseChat = useCallback(async () => {
    if (editorLiveSessionActive && !(await closeEditorLiveSession())) {
      setOpen(true);
      return;
    }
    setOpen(false);
  }, [
    closeEditorLiveSession,
    editorLiveSessionActive,
  ]);

  useEffect(() => {
    const handleCloseEditorLiveChat = () => {
      void handleCloseChat();
    };
    window.addEventListener(
      EDITOR_LIVE_CHAT_CLOSE_EVENT,
      handleCloseEditorLiveChat,
    );
    return () =>
      window.removeEventListener(
        EDITOR_LIVE_CHAT_CLOSE_EVENT,
        handleCloseEditorLiveChat,
      );
  }, [handleCloseChat]);

  useEffect(() => {
    if (!editorLiveSessionActive) return;
    const sourcePath = editorLiveInfo?.sourcePath;
    if (!sourcePath || location.pathname === sourcePath) return;
    void (async () => {
      const cleaned = await closeEditorLiveSession();
      setOpen(!cleaned);
    })();
  }, [
    closeEditorLiveSession,
    editorLiveInfo?.sourcePath,
    editorLiveSessionActive,
    location.pathname,
  ]);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if (params.get("retry_chat") !== "1" || streamingRef.current) return;
    const pending = consumePendingChatRetry();
    if (!pending) return;
    const now = new Date().toISOString();
    const sessionKey = pending.conversationId || createDraftSession();
    setOpen(true);
    setCurrentConvId(pending.conversationId);
    setDraftSessionKey(pending.conversationId ? undefined : sessionKey);
    const msgsBeforeSend = [
      ...messages,
      {
        role: "user" as const,
        content: pending.message,
        timestamp: now,
        meta: pending.responseSurfaceSubmission
          ? responseSurfaceSubmissionMeta(pending.responseSurfaceSubmission)
          : undefined,
      },
      { role: "assistant" as const, content: "", timestamp: now },
    ];
    startStream(
      () =>
        api.chat.stream(pending.message, pending.conversationId, {
          documentIds: pending.documentIds,
          agentId: pending.agentId,
          localWorkerId: pending.localWorkerId,
          workspaceId: pending.workspaceId,
          chatMode: pending.chatMode,
          chatModePayload: pending.chatModePayload,
          manualSkillRefs: pending.manualSkillRefs,
          manualSkillIds: pending.manualSkillIds,
          responseSurfaceSubmission: pending.responseSurfaceSubmission,
        }),
      pending.conversationId,
      msgsBeforeSend,
      (newConvId) => {
        if (currentSessionKeyRef.current === sessionKey) {
          setCurrentConvId(newConvId);
          setDraftSessionKey(undefined);
        }
      },
      sessionKey,
    )
      .then(() => {
        queryClient.invalidateQueries({ queryKey: ["conversations"] });
      })
      .catch(() => {});
  }, [location.search, messages, queryClient, startStream, createDraftSession]);

  /* ---- New conversation ---- */
  const handleNewChat = async () => {
    conversationSelectionRevisionRef.current += 1;
    if (
      editorLiveSessionActive
      && !(await clearEditorLiveSession(true))
    ) {
      setOpen(true);
      return;
    }
    const key = createDraftSession();
    setCurrentConvId(undefined);
    setDraftSessionKey(key);
    setInput("");
    setAttachedFiles([]);
    setComposerSeed(null);
    setEditorSessionLabel(null);
    setEditorLiveInfo(null);
    editorLiveDetailRef.current = null;
    editorLiveAppliedRef.current = { content: "", phase: "idle" };
    setSelectedMentions([]);
    setMentionedAgentId(undefined);
    setConversationLoading(false);
  };

  const handleSwitchSession = async (convId: string) => {
    if (convId === currentConvId) return;
    conversationSelectionRevisionRef.current += 1;
    if (
      editorLiveSessionActive
      && !(await clearEditorLiveSession(true))
    ) {
      setOpen(true);
      return;
    }
    // A session that is still streaming owns its transcript: clearing it and
    // reloading from the API would drop everything streamed so far and race
    // the live writer, which only checkpoints to the DB every few seconds.
    const streamState = useChatStreamStore.getState();
    const liveKey = streamState.getSessionKeyForConversation(convId);
    const isLiveConversation = Boolean(
      liveKey && streamState.sessions[liveKey]?.streaming,
    );
    setCurrentConvId(convId);
    setDraftSessionKey(undefined);
    if (isLiveConversation) {
      setAttachedFiles([]);
      setComposerSeed(null);
      setEditorSessionLabel(null);
      setEditorLiveInfo(null);
      editorLiveDetailRef.current = null;
      editorLiveAppliedRef.current = { content: "", phase: "idle" };
      setSelectedMentions([]);
      setMentionedAgentId(undefined);
      setConversationLoading(false);
      return;
    }
    setSessionMessages(convId, []);
    setAttachedFiles([]);
    setComposerSeed(null);
    setEditorSessionLabel(null);
    setEditorLiveInfo(null);
    editorLiveDetailRef.current = null;
    editorLiveAppliedRef.current = { content: "", phase: "idle" };
    setSelectedMentions([]);
    setMentionedAgentId(undefined);
    setConversationLoading(true);
    loadRecentMessages(convId)
      .then((msgs) => {
        setSessionMessages(convId, msgs);
      })
      .catch(() => {})
      .finally(() => setConversationLoading(false));
  };

  /* ---- HITL action handler ---- */
  const handleHITLAction = useCallback(
    async (hitlId: string, action: string, review?: unknown) => {
      const markResolved = (items: ChatMessage[]) =>
        items.map((msg) => ({
          ...msg,
          hitl_requests: msg.hitl_requests?.map((h) =>
            h.id === hitlId ? { ...h, resolved: true, resolution: action } : h,
          ),
        }));
      const updatedMessages = markResolved(messages);
      setMessages(updatedMessages);

      const hitlMessage = JSON.stringify({
        hitl_id: hitlId,
        action,
        ...(review !== undefined ? { payload: { review } } : {}),
      });
      const now = new Date().toISOString();
      const msgsForHitl = [
        ...updatedMessages,
        {
          role: "user" as const,
          content: hitlActionTranscriptText(action),
          timestamp: now,
        },
        { role: "assistant" as const, content: "", timestamp: now },
      ];
      const sessionKey =
        currentSessionKeyRef.current || currentConvId || createDraftSession();
      if (!currentSessionKeyRef.current && !currentConvId) {
        setDraftSessionKey(sessionKey);
      }

      await startStream(
        () => api.chat.stream(hitlMessage, currentConvId),
        currentConvId,
        msgsForHitl,
        (newConvId) => {
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          }
        },
        sessionKey,
      );
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    [
      currentConvId,
      queryClient,
      messages,
      startStream,
      createDraftSession,
      setMessages,
    ],
  );

  const handleRetryMessage = useCallback(
    async (message: ChatMessage, index: number) => {
      if (streamingRef.current) return;
      const fallbackUserMessage = messages
        .slice(0, index)
        .reverse()
        .find((item) => Boolean(item?.role === "user" && toDisplayText(item?.content).trim()));
      const fallbackUserContent = toDisplayText(fallbackUserMessage?.content).trim();
      const retryRequest: ChatRetryRequest | undefined =
        message.retryRequest ||
        (fallbackUserContent
          ? {
              message: fallbackUserContent,
              conversationId: currentConvId,
            }
          : undefined);
      if (!retryRequest?.message?.trim()) return;

      const now = new Date().toISOString();
      const sessionKey =
        currentSessionKeyRef.current ||
        retryRequest.conversationId ||
        currentConvId ||
        createDraftSession();
      if (!currentSessionKeyRef.current && !currentConvId) {
        setDraftSessionKey(sessionKey);
      }

      const retryUserContent =
        fallbackUserContent || retryRequest.message;
      const msgsBeforeSend: ChatMessage[] = [
        ...messages,
        {
          role: "user",
          content: retryUserContent,
          timestamp: now,
          attachments: fallbackUserMessage?.attachments,
          mentions: fallbackUserMessage?.mentions,
          manualSkills: fallbackUserMessage?.manualSkills,
          chatMode: fallbackUserMessage?.chatMode,
          chatModePayload: fallbackUserMessage?.chatModePayload,
          meta: retryRequest.responseSurfaceSubmission
            ? responseSurfaceSubmissionMeta(retryRequest.responseSurfaceSubmission)
            : fallbackUserMessage?.meta,
        },
        {
          role: "assistant",
          content: "",
          timestamp: now,
          retryRequest,
        },
      ];

      await startStream(
        () =>
          api.chat.stream(
            retryRequest.message,
            retryRequest.conversationId || currentConvId,
            {
              documentIds: retryRequest.documentIds,
              agentId: retryRequest.agentId,
              workspaceId: retryRequest.workspaceId,
              chatMode: retryRequest.chatMode,
              chatModePayload: retryRequest.chatModePayload,
              manualSkillRefs: retryRequest.manualSkillRefs,
              manualSkillIds: retryRequest.manualSkillIds,
              responseSurfaceSubmission: retryRequest.responseSurfaceSubmission,
            },
          ),
        retryRequest.conversationId || currentConvId,
        msgsBeforeSend,
        (newConvId) => {
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          }
        },
        sessionKey,
      );
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    [
      currentConvId,
      createDraftSession,
      messages,
      queryClient,
      setMessages,
      startStream,
    ],
  );

  const feedbackKeyForMessage = useCallback(
    (message: ChatMessage, index: number) =>
      message.id || `${currentSessionKey || "draft"}:${index}`,
    [currentSessionKey],
  );

  const handleMessageFeedback = useCallback(
    async (
      message: ChatMessage,
      index: number,
      rating: ChatMessageFeedbackRating,
      contentPreview: string,
    ) => {
      const conversationId =
        message.retryRequest?.conversationId ||
        message.conversation_id ||
        currentConvId ||
        streamingConvId;
      if (message.role !== "assistant" || !message.id || !conversationId) return;

      const key = feedbackKeyForMessage(message, index);
      const fallbackRequestPreview = messages
        .slice(0, index)
        .reverse()
        .find((item) =>
          Boolean(item?.role === "user" && toDisplayText(item?.content).trim()),
        );

      try {
        await submitMessageFeedback(key, rating, (queuedRating) =>
          api.chat.feedback(conversationId, message.id!, {
            rating: queuedRating,
            content_preview: contentPreview,
            request_preview: (toDisplayText(fallbackRequestPreview?.content) || "").slice(0, 1000),
          }),
        );
      } catch {}
    },
    [
      currentConvId,
      feedbackKeyForMessage,
      messages,
      submitMessageFeedback,
      streamingConvId,
    ],
  );

  /* ---- Helpers ---- */
  const iconBtnStyle = (hoverColor: string): React.CSSProperties => ({
    width: 30,
    height: 30,
    borderRadius: 8,
    border: "none",
    background: "transparent",
    cursor: streaming ? "not-allowed" : "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    color: "var(--text-faint, #a8a29e)",
    flexShrink: 0,
    transition: "color 0.15s",
    opacity: streaming ? 0.4 : 1,
  });

  const editorLiveFileName =
    editorLiveInfo?.documentName?.trim() || "this file";
  const editorLiveEmptyDescription =
    editorLiveInfo?.emptyDescription?.trim() ||
    `Tell me what you want changed. I will update ${editorLiveFileName} directly in the editor as the answer streams.`;
  const editorLivePlaceholder =
    editorLiveInfo?.placeholder?.trim() ||
    `Describe the edit you want in ${editorLiveFileName}...`;
  const editorLiveExamples = editorLiveInfo?.examples?.length
    ? editorLiveInfo.examples
    : ["Rewrite", "Format", "Add content", "Fix layout"];

  /* ================================================================ */
  /*  Render                                                           */
  /* ================================================================ */

  return (
    <>
      {/* ---- Floating Button ---- */}
      <button
        type="button"
        className="float-chat-btn"
        data-tour="chat-input"
        data-open={open ? "true" : "false"}
        aria-label={t("component.floating_chat.chat_with_manor_ai")}
        aria-expanded={open}
        aria-controls="floating-chat-panel"
        aria-hidden={open}
        tabIndex={open ? -1 : 0}
        onClick={() => {
          const nextOpen = !open;
          if (!nextOpen) {
            setConversationLoading(false);
            void handleCloseChat();
            return;
          }
          if (
            !resumedRef.current &&
            !streamingRef.current &&
            messages.length === 0
          ) {
            setConversationLoading(true);
          }
          setOpen(true);
        }}
        style={{
          position: "fixed",
          bottom: 24,
          right: 24,
          zIndex: 1000,
          width: 52,
          height: 52,
          borderRadius: "50%",
          background: "linear-gradient(135deg, #436b65, #4f7d75)",
          border: "none",
          boxShadow: "0 4px 20px rgba(67,107,101,0.35)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
        }}
      >
        <svg
          width="22"
          height="22"
          viewBox="0 0 24 24"
          fill="none"
          stroke="white"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
        </svg>
      </button>

      {/* ---- Chat Panel ---- */}
      <FloatingPanel
        id="floating-chat-panel"
        open={open}
        zIndex={1001}
        ariaLabel={editorLiveSessionActive ? "AI edit" : t("page.chat_history.manor_ai")}
        onClose={handleCloseChat}
        initialFocusRef={composerEditorRef}
      >
        {/* ── Header ── */}
        <PanelHeader
          avatar={<ManorAvatar size={34} />}
          title={editorLiveSessionActive ? "AI edit" : t("page.chat_history.manor_ai")}
          subtitle={editorLiveSessionActive
            && editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing ? (
                <div
                  role="status"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 5,
                    maxWidth: 220,
                    marginTop: 1,
                    color: "var(--text-muted, #78716c)",
                    fontSize: 10,
                    fontWeight: 650,
                  }}
                >
                  <LoadingSpinner size={10} />
                  <span>{t("component.floating_chat.ai_edit_closing")}</span>
                </div>
              ) : editorLiveSessionActive
                && editorLiveCleanupStatus === AiEditSessionCleanupStatus.Failed ? (
                    <div
                      role="alert"
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 5,
                        maxWidth: 220,
                        marginTop: 1,
                        color: "var(--editor-danger-text, #a23e38)",
                        fontSize: 10,
                        fontWeight: 650,
                      }}
                    >
                      <span
                        aria-hidden="true"
                        style={{
                          width: 5,
                          height: 5,
                          flexShrink: 0,
                          borderRadius: "50%",
                          background: "currentColor",
                        }}
                      />
                      <span>{t("component.floating_chat.ai_edit_close_failed")}</span>
                    </div>
                  ) : editorSessionLabel ? (
                <div
                  title={editorSessionLabel}
                  style={{
                    maxWidth: 220,
                    marginTop: 1,
                    fontSize: 10,
                    color: "var(--accent, #436b65)",
                    fontWeight: 700,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {editorSessionLabel}
                </div>
              ) : assistantWorking ? (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    marginTop: 1,
                  }}
                >
                  <AgentActivityOrb activity={activeAgentActivity} />
                </div>
              ) : listening ? (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                    marginTop: 1,
                  }}
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background: "var(--editor-danger-text, #d65f59)",
                      display: "inline-block",
                      animation: "pulse 1s infinite",
                    }}
                  />
                  <span
                    style={{
                      fontSize: 10,
                      color: "var(--editor-danger-text, #d65f59)",
                      fontWeight: 600,
                    }}
                  >
                    {t("component.floating_chat.listening")}</span>
                </div>
              ) : (
                <div
                  style={{
                    marginTop: 1,
                    fontSize: 10,
                    color: "var(--text-faint, #78716c)",
                  }}
                >
                  {t("page.app_layout.your_ai_chief_of_staff")}
                </div>
              )}
          actions={
            <>
              {!editorLiveSessionActive && (
                <SessionSwitcher
                  currentConvId={currentConvId}
                  onNewChat={handleNewChat}
                  onSwitchSession={handleSwitchSession}
                />
              )}
              <button
                onClick={handleCloseChat}
                disabled={editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing}
                aria-busy={editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing}
                aria-label={editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing
                  ? t("component.floating_chat.ai_edit_closing")
                  : t("page.flows.close")}
                title={editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing
                  ? t("component.floating_chat.ai_edit_closing")
                  : t("page.flows.close")}
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 8,
                  border: "none",
                  background: "transparent",
                  cursor: editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing
                    ? "wait"
                    : "pointer",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "var(--text-faint, #a8a29e)",
                  transition: "all 0.15s",
                  opacity: editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing
                    ? 0.65
                    : 1,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--modal-control-hover-bg, #f5f5f4)";
                  e.currentTarget.style.color = "var(--text-strong, #57534e)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "var(--text-faint, #a8a29e)";
                }}
              >
                {editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing ? (
                  <LoadingSpinner size={14} />
                ) : (
                  <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2.5}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  >
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                )}
              </button>
            </>
          }
        />

        {/* ── Messages ── */}
        <div
          onScroll={handleAutoFollowScroll}
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "12px 14px",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {showConversationSkeleton ? (
            <ChatMessagesSkeleton rows={4} compact maxWidth="100%" />
          ) : messages.length === 0 && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                height: "100%",
                padding: 24,
              }}
            >
              <div style={{ textAlign: "center" }}>
                <div
                  style={{
                    width: 48,
                    height: 48,
                    borderRadius: 14,
                    background: "var(--accent-soft, #f2f6f5)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    margin: "0 auto 12px",
                  }}
                >
                  <svg
                    width="22"
                    height="22"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="var(--accent, #4f7d75)"
                    strokeWidth={1.5}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 01-.825-.242m9.345-8.334a2.126 2.126 0 00-.476-.095 48.64 48.64 0 00-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0011.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
                  </svg>
                </div>
                <p
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: "var(--text-strong, #44403c)",
                    margin: 0,
                  }}
                >
                  {editorLiveSessionActive
                    ? "Tell me what to change"
                    : t("component.floating_chat.chat_with_manor_ai")}</p>
                <p
                  style={{
                    fontSize: 11,
                    color: "var(--text-muted, #a8a29e)",
                    marginTop: 4,
                    lineHeight: 1.4,
                    maxWidth: 260,
                  }}
                >
                  {editorLiveSessionActive
                    ? editorLiveEmptyDescription
                    : t("component.floating_chat.ask_anything_attach_files_or_use_voice_input")}</p>
                {editorLiveSessionActive && (
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      justifyContent: "center",
                      gap: 6,
                      marginTop: 12,
                    }}
                  >
                    {editorLiveExamples.map((example) => (
                      <button
                        type="button"
                        key={example}
                        className="floating-chat-suggestion"
                        disabled={
                          assistantWorking
                          || editorLiveCleanupStatus === AiEditSessionCleanupStatus.Closing
                        }
                        onClick={() => {
                          setInput(example);
                          window.requestAnimationFrame(() => composerEditorRef.current?.focus());
                        }}
                      >
                        {example}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {!showConversationSkeleton && visibleWorkflowMessageEntries.map(({ message: msg, index: i }) => {
            const rawContent = toDisplayText(msg.content);
            const content = msg.role === "assistant"
              ? stripEditorLiveEditBlocks(rawContent)
              : rawContent;
            const visibleTools = visibleToolCallsForMessage(msg);
            const hasAssistantBlocks =
              msg.role === "assistant" &&
              Array.isArray(msg.assistant_blocks) &&
              msg.assistant_blocks.length > 0;
            const localCodingNotice =
              msg.role === "assistant"
                ? maybeLocalCodingRunNoticeForTools(visibleTools)
                : null;
            const rawBubbleContent = localCodingNotice || content;
            const canRetryFromContent = isRetryableAssistantMessage(msg, rawBubbleContent);
            const bubbleContent =
              msg.role === "assistant"
                ? displayContentForAssistantMessage(msg, rawBubbleContent)
                : rawBubbleContent;
            const isLatestStreaming = assistantWorking && i === messages.length - 1;
            const renderedBubbleDisplay = parseUserMessageDisplay(
              { ...msg, content: bubbleContent },
              { streaming: isLatestStreaming },
            );
            const renderedBubbleContent = renderedBubbleDisplay.cleanContent;
            const assistantReferenceDisplay = localCodingNotice
              ? parseUserMessageDisplay(msg, {
                  renderedContent: renderedBubbleContent,
                  streaming: isLatestStreaming,
                })
              : renderedBubbleDisplay;
            const suppressApprovalBubble =
              isApprovalBoilerplateContent(msg) && !isLatestStreaming;
            const showCreditLimitNotice =
              msg.role === "assistant" &&
              msg.stop_reason === "credit_exhausted";
            const actionCopyText = msg.role === "user"
              ? content
              : chatMessageActionText(msg, rawBubbleContent);
            const showMessageActions = Boolean(
              !suppressApprovalBubble &&
                !showCreditLimitNotice &&
                actionCopyText.trim(),
            );
            const hasRetryTarget =
              Boolean(msg.retryRequest) ||
              messages
                .slice(0, i)
                .some(
                  (item) =>
                    Boolean(item?.role === "user" && toDisplayText(item?.content).trim()),
                );
            const canRetryMessage =
              canRetryFromContent && hasRetryTarget;
            const workspaceRecommendation = msg.role === "assistant"
              ? normalizeWorkspaceRecommendation(msg.meta?.workspace_recommendation)
              : null;
            const workspaceRecommendationKey = `${msg.id || currentConvId || "draft"}:${i}`;
            // Only backend HITL cards have an id that can be safely resolved.
            const showInlineApproval = false;
            return (
              <MessageRow
                key={i}
                id={chatMessageAnchorId(msg.id, i)}
                role={msg.role === "user" ? "user" : "other"}
                avatar={msg.role === "assistant" ? <ManorAvatar size={26} /> : undefined}
              >
                  {/* Tool calls */}
                  {!hasAssistantBlocks && visibleTools.length > 0 && (
                    <ToolCallList
                      tools={visibleTools}
                      keyPrefix={i}
                      variant="inline"
                    />
                  )}

                  {showCreditLimitNotice && (
                    <CreditLimitNotice detail={msg.limit_detail} compact />
                  )}

                  {/* HITL requests — see EmbeddedChat for the full
                    rationale. Unresolved approvals get their buttons
                    in the sticky <ApprovalActionBar> at the bottom;
                    inline cards only show the description and
                    resolved-state badge. */}
                  {msg.hitl_requests && msg.hitl_requests.length > 0 && (
                    <div className="chat-hitl-cards">
                      {msg.hitl_requests.map((hitl) => {
                        const isUnresolvedApproval =
                          hitl.type === "approval" && !hitl.resolved;
                        return (
                          <div
                            key={hitl.id}
                            className={`chat-hitl-card ${hitl.type === "approval" ? "chat-hitl-card--approval" : ""}`}
                          >
                            {hitl.type === "approval" ? (
                              <ApprovalSummary
                                prompt={hitl.prompt}
                                action={hitl.action}
                                tool={hitl.tool}
                                hasWorkspace={Boolean(hitl.workspace?.id || hitl.workspace?.name)}
                                paths={hitl.paths}
                                content={hitl.content}
                                argsPreview={hitl.args_preview}
                                operation={hitl.operation}
                              />
                            ) : (
                              <p className="chat-hitl-prompt">{hitl.prompt}</p>
                            )}
                            {!isUnresolvedApproval && (
                              <ChatActionCard
                                action={{
                                  kind:
                                    hitl.type === "approval"
                                      ? "approve"
                                      : "human_input",
                                  options: hitl.options || [
                                    "approve",
                                    "reject",
                                  ],
                                }}
                                resolved={hitl.resolved}
                                resolution={
                                  hitl.resolved
                                    ? { choice: hitl.resolution || "approved" }
                                    : null
                                }
                                disabled={streaming || hitl.resolved}
                                onResolve={(choice) =>
                                  handleHITLAction(hitl.id, choice)
                                }
                              />
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {showInlineApproval && (
                    <div className="chat-hitl-cards">
                      <div className="chat-hitl-card chat-hitl-card--approval">
                        <ApprovalSummary
                          action={inferApprovalAction(content)}
                          paths={extractApprovalPaths(content)}
                          content={content}
                        />
                        <ChatActionCard
                          action={{
                            kind: "approve",
                            options: DEFAULT_APPROVAL_OPTIONS,
                          }}
                          disabled={streaming}
                          onResolve={(choice) =>
                            handleSend(
                              choice.includes("reject")
                                ? t("chat.approval.reject")
                                : t("chat.approval.approve"),
                              [],
                            )
                          }
                        />
                      </div>
                    </div>
                  )}

                  {/* Bubble */}
                  {(() => {
                    if (
                      hasAssistantBlocks &&
                      !canRetryFromContent &&
                      !suppressApprovalBubble &&
                      !showCreditLimitNotice
                    ) {
                      return (
                        <MessageBubble
                          role="other"
                          className="chat-bubble chat-bubble--bot"
                        >
                          <AssistantMessageBlocks
                            blocks={msg.assistant_blocks}
                            content={renderedBubbleContent}
                            keyPrefix={i}
                            streaming={assistantWorking && i === messages.length - 1}
                            onResponseSurfaceSubmit={handleResponseSurfaceSubmit}
                            sourceMessageId={msg.id || ""}
                            responseSurfaceSubmissionReceipts={responseSurfaceSubmissionReceipts}
                          />
                          <ChatMessageReferenceStrip
                            references={assistantReferenceDisplay.references}
                            inlineFileCards
                            returnTo={`${location.pathname}${location.search}${location.hash}`}
                          />
                        </MessageBubble>
                      );
                    }
                    if (suppressApprovalBubble || showCreditLimitNotice)
                      return null;
                    // Attachment-only turns still need a bubble for the file card.
                    if (!bubbleContent && (msg.attachments?.length ?? 0) === 0 && msg.role !== "user") return null;
                    const display = parseUserMessageDisplay(
                      {
                        ...msg,
                        content: bubbleContent,
                      },
                      { streaming: isLatestStreaming },
                    );
                    const { cleanContent, chips, references } = display;
                    if (!cleanContent && chips.length === 0 && references.length === 0) return null;
                    return (
                      <MessageBubble
                        role={msg.role === "user" ? "user" : "other"}
                        className={`chat-bubble ${msg.role === "user" ? "chat-bubble--user" : "chat-bubble--bot"}`}
                      >
                        {cleanContent && (
                          <>
                            {msg.role === "user" ? (
                              <CollapsibleSentMessage text={cleanContent}>
                                <ChatMarkdown content={cleanContent} isUser />
                              </CollapsibleSentMessage>
                            ) : (
                              <>
                                <ChatMarkdown
                                  content={cleanContent}
                                  isUser={false}
                                  streaming={
                                    assistantWorking &&
                                    i === messages.length - 1 &&
                                    msg.role === "assistant"
                                  }
                                />
                                {msg.workflow_result && (
                                  <WorkflowResultCard
                                    result={msg.workflow_result}
                                    returnTo={`${location.pathname}${location.search}${location.hash}`}
                                  />
                                )}
                              </>
                            )}
                            {assistantWorking &&
                              i === messages.length - 1 &&
                              msg.role === "assistant" && (
                                <span className="chat-streaming-cursor" />
                              )}
                          </>
                        )}
                        {(msg.role === "user" || references.length > 0) && (
                          <>
                            <ChatMessageReferenceStrip
                              references={references}
                              align={msg.role === "user" ? "right" : "left"}
                              onOpenReference={handleOpenMessageReference}
                              inlineFileCards={msg.role === "assistant"}
                              returnTo={`${location.pathname}${location.search}${location.hash}`}
                            />
                            <ChatMessageMetaChips
                              chips={chips}
                              align={msg.role === "user" ? "right" : "left"}
                            />
                          </>
                        )}
                      </MessageBubble>
                    );
                  })()}

                  {/* Streaming cursor when no content yet. Tool-only turns already
                    show progress via ToolCallList, so avoid a second empty bubble. */}
                  {!content &&
                    visibleTools.length === 0 &&
                    !hasAssistantBlocks &&
                    assistantWorking &&
                    i === messages.length - 1 &&
                    msg.role === "assistant" && (
                      <MessageBubble
                        role="other"
                        className="chat-bubble chat-bubble--bot chat-bubble--activity"
                      >
                        <AgentActivityOrb
                          activity={inferAgentActivity(msg)}
                          className="agent-activity-orb--message"
                        />
                      </MessageBubble>
                    )}

                  {workspaceRecommendation &&
                    workspaceRecommendationPreferencesReady &&
                    !workspaceRecommendationsSuppressed &&
                    !dismissedWorkspaceRecommendations.has(workspaceRecommendationKey) && (
                      <WorkspaceRecommendationCard
                        recommendation={workspaceRecommendation}
                        loading={workspaceRecommendationBusyKey === workspaceRecommendationKey}
                        preferenceLoading={workspaceRecommendationPreferenceBusy}
                        disabled={assistantWorking || workspaceRecommendationPreferenceBusy}
                        onCreate={() => void handleWorkspaceRecommendationCreate(
                          workspaceRecommendation,
                          workspaceRecommendationKey,
                        )}
                        onOpen={() => handleWorkspaceRecommendationOpen(
                          workspaceRecommendation,
                          workspaceRecommendationKey,
                        )}
                        onAdd={() => void handleWorkspaceRecommendationAdd(
                          workspaceRecommendation,
                          workspaceRecommendationKey,
                        )}
                        onContinue={() => dismissWorkspaceRecommendation(
                          workspaceRecommendationKey,
                        )}
                        onDontSuggestAgain={() => void handleWorkspaceRecommendationOptOut()}
                      />
                    )}

                  {(!isLatestStreaming || Boolean(content) || visibleTools.length > 0 || hasAssistantBlocks) && (
                    <div
                    className={`chat-message-meta-row ${
                      msg.role === "user" ? "chat-message-meta-row--user" : ""
                    } ${showMessageActions ? "chat-message-meta-row--actions" : ""}`}
                  >
                    <ChatTimestamp timestamp={msg.timestamp} />
                    {showMessageActions && (
                      <span className="chat-message-meta-actions">
                        <ChatMessageActions
                          align="right"
                          copyText={actionCopyText}
                          speechText={msg.role === "assistant" ? actionCopyText : undefined}
                          voiceScope={{ conversationId: msg.conversation_id || currentConvId || streamingConvId }}
                          copyLabel={t(
                            msg.role === "user"
                              ? "component.chat_message_actions.copy_request"
                              : "component.chat_message_actions.copy_response",
                          )}
                          canRetry={canRetryMessage}
                          feedbackValue={
                            msg.role === "assistant"
                              ? messageFeedback[feedbackKeyForMessage(msg, i)] || null
                              : null
                          }
                          disabled={streaming || !open}
                          onRetry={() => handleRetryMessage(msg, i)}
                          onFeedback={
                            msg.role === "assistant" &&
                            Boolean(
                              msg.id &&
                                (msg.conversation_id || currentConvId || streamingConvId),
                            )
                              ? (rating) =>
                                  handleMessageFeedback(
                                    msg,
                                    i,
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
              </MessageRow>
            );
          })}

          <div ref={messagesEndRef} />
        </div>

        {/* Sticky approval bar (same component used by EmbeddedChat). The
            "floating" variant matches the 12 px padding and 100% width that
            .floating-chat-footer uses for its own composer. */}
        <ApprovalActionBar
          messages={visibleWorkflowMessages}
          disabled={streaming}
          onResolve={handleHITLAction}
          variant="floating"
        />

        <WorkflowRunHost
          conversationId={currentConvId || streamingConvId}
          groups={workflowRunGroups}
          onResolveMessage={handleWorkflowMessageResolve}
          resolveLoading={workflowMessageResolveMutation.isPending}
          resolveError={workflowMessageResolveMutation.error}
          resolveMessageId={workflowMessageResolveMutation.variables?.messageId || null}
          invalidationQueryKeys={GLOBAL_WORKFLOW_INVALIDATION_QUERY_KEYS}
          onRunChange={workflowMessageResolveMutation.reset}
        />

        <ChatInputFooter
          disabled={!open}
          voiceScope={{ conversationId: currentConvId || streamingConvId }}
          onVoiceConversation={(id) => {
            setCurrentConvId(id);
            setDraftSessionKey(undefined);
            void loadRecentMessages(id).then(msgs => setSessionMessages(id, msgs)).catch(() => {});
            void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          }}
          value={input}
          onChange={handleComposerChange}
          streaming={streaming}
          onSend={(text, attachments, manualSkills, context) => {
            void handleSend(text, attachments, manualSkills, null, context);
          }}
          onSendWorkflow={(text, attachments, manualSkills, workflow, context) => {
            void handleSend(text, attachments, manualSkills, workflow, context);
          }}
          onKeyDown={
            editorLiveSessionActive
              ? (event) => event.stopPropagation()
              : undefined
          }
          onStop={handleStopRequest}
          topSlot={
            messages.length > 0 && !editorLiveSessionActive ? (
                <ChatModeTemplateGallery
                  mode={chatMode}
                  disabled={streaming}
                  samples={chatModeTemplateSamples(chatMode)}
                  onSelect={async (sample) => {
                    try {
                      const remix = await prepareTemplateRemix(sample);
                      setInput(remix.prompt);
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
          placeholder={
            editorLiveSessionActive
              ? editorLivePlaceholder
              : chatMode !== "auto"
              ? getChatModeInputPlaceholder(chatMode, chatModePayload)
              : messages.length === 0
              ? t("component.floating_chat.ask_manor_ai_mention_attach_skill")
              : t("component.floating_chat.message_manor_ai_mention_attach_skill")
          }
          modeSlot={
            editorLiveSessionActive ? undefined : (
              <ChatModeToolbar
                mode={chatMode}
                payload={chatModePayload}
                onModeChange={handleChatModeChange}
                onPayloadChange={setChatModePayload}
                disabled={streaming}
              />
            )
          }
          replaceActionButtons={
            !editorLiveSessionActive && chatMode !== "auto"
          }
          mentions={mentionOptions}
          workflows={editorLiveSessionActive ? [] : workflowInvokeOptions}
          selectedMentions={selectedMentions}
          onMentionSelect={handleMentionSelect}
          onMentionRemove={handleMentionRemove}
          textareaRef={textareaRef}
          editorRef={composerEditorRef}
          seedAttachments={composerSeed?.attachments}
          seedAttachmentsKey={composerSeed?.key}
          className="floating-chat-footer"
        />
      </FloatingPanel>
    </>
  );
}
