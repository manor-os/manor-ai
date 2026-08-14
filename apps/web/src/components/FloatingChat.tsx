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
import { api, type GlobalChatFlowEntrypoint } from "../lib/api";
import { invalidateKnowledgeQueries } from "../lib/knowledgeInvalidation";
import {
  type ChatMessage,
  type ChatStreamSnapshot,
  type ToolCall,
  isInternalFilePermissionMessage,
  isRedundantApprovalResolutionReceipt,
  isTerminalStreamSnapshot,
  mergeChatStreamSnapshot,
  mergeResolvedWorkflowMessage,
  streamSnapshotNeedsHistory,
  pendingHITLIds,
  hitlActionTranscriptText,
  parseToolCalls,
  useDebounced,
  resolveGlobalWorkflowMessageAction,
} from "../lib/chatStream";
import { hasLocallyStreamedConversation, useChatStreamStore } from "../stores/chatStream";
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
  type ChatMessageFeedbackRating,
  displayContentForAssistantMessage,
  isRetryableAssistantMessage,
} from "./chat/ChatMessageActions";
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
  OPEN_FLOATING_CHAT_EVENT,
  type OpenFloatingChatDetail,
} from "../lib/selectionActions";

function maybeLocalCodingRunNoticeForTools(_tools: ToolCall[]): string | null {
  return null;
}
import {
  applyEditorLivePatch,
  buildEditorLiveEditFallbackContent,
  buildEditorLiveEditRequest,
  EDITOR_LIVE_CHAT_CLOSE_EVENT,
  EDITOR_LIVE_CHAT_EVENT,
  extractEditorLivePatchPayloads,
  stripEditorLiveEditBlocks,
  type EditorLiveChatDetail,
} from "../lib/editorLiveChat";
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

function parseFencedCodeBlocks(text: string) {
  const blocks: Array<{ language: string; content: string }> = [];
  const re = /```([a-zA-Z0-9_+.-]*)[^\n]*\n([\s\S]*?)```/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    const content = (match[2] || "").trim();
    if (!content) continue;
    blocks.push({
      language: (match[1] || "").trim().toLowerCase(),
      content,
    });
  }
  return blocks;
}

function looksHtmlLike(text: string) {
  return /<(!doctype\s+html|html|head|body)\b/i.test(text);
}

function tryParseJsonObject(text: string) {
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object";
  } catch {
    return false;
  }
}

function looksMostlyCode(text: string) {
  const lines = text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length < 2) return false;
  const codeish = lines.filter((line) =>
    /^(import|export|from|function|class|const|let|var|return|if|for|while|switch|case|def|async|await|try|catch|finally|#|\/\/|\/\*|\*|<|{|}|\.|@)/.test(line) ||
    /[{};=<>]/.test(line),
  ).length;
  return codeish / lines.length >= 0.45;
}

function replacementCandidateIsComplete(
  candidate: string,
  currentContent: string,
  detail: EditorLiveChatDetail,
) {
  const trimmed = candidate.trim();
  if (!trimmed || trimmed === currentContent.trim()) return false;

  const name = (detail.documentName || "").toLowerCase();
  const type = `${detail.fileType || ""} ${detail.mimeType || ""} ${detail.editorType || ""}`.toLowerCase();

  if (
    name.endsWith(".html") ||
    name.endsWith(".htm") ||
    type.includes("html") ||
    looksHtmlLike(currentContent)
  ) {
    return looksHtmlLike(trimmed);
  }

  if (
    name.endsWith(".json") ||
    type.includes("json") ||
    tryParseJsonObject(currentContent)
  ) {
    return tryParseJsonObject(trimmed);
  }

  if (name.endsWith(".css") || type.includes("css")) {
    return /[{}:;]/.test(trimmed) && trimmed.split("\n").length >= 2;
  }

  return looksMostlyCode(trimmed);
}

function extractEditorLiveReplacementContent(
  assistantText: string,
  currentContent: string,
  detail: EditorLiveChatDetail,
) {
  const blocks = parseFencedCodeBlocks(assistantText);
  const fileExt = (detail.documentName || "").split(".").pop()?.toLowerCase() || "";
  const preferred = blocks
    .filter((block) => {
      if (!block.language) return true;
      if (fileExt && (block.language === fileExt || block.language.includes(fileExt))) return true;
      if (fileExt === "html" || fileExt === "htm") return ["html", "markup", "xml"].includes(block.language);
      if (fileExt === "js") return ["js", "javascript"].includes(block.language);
      if (fileExt === "ts") return ["ts", "typescript"].includes(block.language);
      return true;
    })
    .sort((a, b) => b.content.length - a.content.length);

  for (const block of preferred) {
    if (replacementCandidateIsComplete(block.content, currentContent, detail)) {
      return block.content;
    }
  }

  const raw = assistantText.trim();
  if (replacementCandidateIsComplete(raw, currentContent, detail)) {
    return raw;
  }

  return null;
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

function pipeEditorLiveEditStream(
  response: Response,
  detail: EditorLiveChatDetail,
  sessionKey: string | undefined,
  appliedRef: MutableRefObject<{
    sessionKey?: string;
    content: string;
    complete?: boolean;
  }>,
  isActive: () => boolean,
  onProgress?: (tool: ToolCall) => void,
  onCompleteApplied?: () => void,
  userRequest = "",
) {
  if (!response.body || !detail.applyContent) return response;
  if (typeof response.body.tee !== "function") return response;

  const [chatBody, liveBody] = response.body.tee();
  const liveResponse = new Response(chatBody, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });

  void (async () => {
    const reader = liveBody.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let currentEvent = "";
    let assistantText = "";
    let completeNotified = false;
    let processedPatchCount = 0;
    let appliedGeneratedImageUrl = "";
    let workingContent = detail.getContent?.() || "";

    const finishLiveEdit = () => {
      if (completeNotified) return;
      completeNotified = true;
      onProgress?.(
        makeEditorLiveProgressTool(
          "verify_patch",
          "success",
          {
            file: detail.documentName || "current file",
            stage: "verified",
          },
          {
            status: "ok",
            patches: processedPatchCount,
          },
        ),
      );
      onCompleteApplied?.();
    };

    const applyGeneratedImageUrl = async (imageUrl: string) => {
      if (!detail.applyGeneratedImage || !imageUrl || !isActive()) return false;
      if (imageUrl === appliedGeneratedImageUrl) return true;
      appliedGeneratedImageUrl = imageUrl;
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
            patch: processedPatchCount + 1,
          },
        ),
      );

      try {
        await detail.applyGeneratedImage(imageUrl, {
          complete: true,
          source: "assistant-stream",
          mode: "patch",
          diff,
          patchCount: processedPatchCount + 1,
          sourceLabel: "generated image",
        });
        workingContent = detail.getContent?.() || workingContent;
        processedPatchCount += 1;
        appliedRef.current = {
          sessionKey,
          content: workingContent,
          complete: true,
        };
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "success",
            {
              file: detail.documentName || "current file",
              patch: processedPatchCount,
            },
            {
              status: "ok",
              source: "generated image",
            },
          ),
        );
        finishLiveEdit();
        return true;
      } catch (err) {
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "error",
            {
              file: detail.documentName || "current file",
              patch: processedPatchCount + 1,
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

    const applyFallbackEdit = () => {
      if (!isActive()) return false;
      let fallbackContent: string | null = null;
      try {
        fallbackContent = buildEditorLiveEditFallbackContent(
          detail,
          userRequest,
          workingContent,
        );
      } catch (err) {
        console.warn("Editor live edit fallback failed", err);
        return false;
      }
      if (
        typeof fallbackContent !== "string" ||
        !fallbackContent ||
        fallbackContent === workingContent
      ) {
        return false;
      }
      const beforeContent = workingContent;
      const patchJson = JSON.stringify([
        {
          op: "replace",
          find: beforeContent,
          replace: fallbackContent,
        },
      ]);
      const diff = buildEditorLiveDiff(
        beforeContent,
        fallbackContent,
        detail.documentName || "current file",
      );

      onProgress?.(
        makeEditorLiveProgressTool(
          "generate_patch",
          "success",
          {
            file: detail.documentName || "current file",
            stage: "fallback patch",
          },
          {
            status: "ok",
            source: "local fallback",
            patches: processedPatchCount + 1,
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
            patch: processedPatchCount + 1,
          },
        ),
      );

      try {
        workingContent = fallbackContent;
        processedPatchCount += 1;
        appliedRef.current = {
          sessionKey,
          content: workingContent,
          complete: true,
        };
        detail.applyContent?.(workingContent, {
          complete: true,
          source: "assistant-stream",
          mode: "patch",
          diff,
          patch: truncateToolText(patchJson, 5000),
          patchCount: processedPatchCount,
          sourceLabel: "local fallback",
        });
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "success",
            {
              file: detail.documentName || "current file",
              patch: processedPatchCount,
            },
            {
              status: "ok",
              source: "local fallback",
              operations: 1,
              patch: truncateToolText(patchJson, 5000),
            },
          ),
        );
        finishLiveEdit();
        return true;
      } catch (err) {
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "error",
            {
              file: detail.documentName || "current file",
              patch: processedPatchCount || 1,
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

    const applyAssistantReplacementEdit = () => {
      if (!isActive()) return false;
      const replacementContent = extractEditorLiveReplacementContent(
        assistantText,
        workingContent,
        detail,
      );
      if (!replacementContent) return false;

      const beforeContent = workingContent;
      const patchJson = JSON.stringify([
        {
          op: "replace",
          find: beforeContent,
          replace: replacementContent,
        },
      ]);
      const diff = buildEditorLiveDiff(
        beforeContent,
        replacementContent,
        detail.documentName || "current file",
      );

      onProgress?.(
        makeEditorLiveProgressTool(
          "generate_patch",
          "success",
          {
            file: detail.documentName || "current file",
            stage: "replacement inferred from assistant response",
          },
          {
            status: "ok",
            source: "assistant replacement",
            patches: processedPatchCount + 1,
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
            patch: processedPatchCount + 1,
          },
        ),
      );

      try {
        workingContent = replacementContent;
        processedPatchCount += 1;
        appliedRef.current = {
          sessionKey,
          content: workingContent,
          complete: true,
        };
        detail.applyContent?.(workingContent, {
          complete: true,
          source: "assistant-stream",
          mode: "patch",
          diff,
          patch: truncateToolText(patchJson, 5000),
          patchCount: processedPatchCount,
          sourceLabel: "assistant replacement",
        });
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "success",
            {
              file: detail.documentName || "current file",
              patch: processedPatchCount,
            },
            {
              status: "ok",
              source: "assistant replacement",
              operations: 1,
              patch: truncateToolText(patchJson, 5000),
            },
          ),
        );
        finishLiveEdit();
        return true;
      } catch (err) {
        onProgress?.(
          makeEditorLiveProgressTool(
            "apply_patch",
            "error",
            {
              file: detail.documentName || "current file",
              patch: processedPatchCount + 1,
            },
            {
              status: "failed",
              error: (err as Error).message,
              patch: truncateToolText(patchJson, 5000),
            },
          ),
        );
        return false;
      }
    };

    const applyPatchPayloads = () => {
      const payloads = extractEditorLivePatchPayloads(assistantText);
      if (payloads.length <= processedPatchCount) return;
      if (!isActive()) return;

      for (
        let patchIndex = processedPatchCount;
        patchIndex < payloads.length;
        patchIndex += 1
      ) {
        const patchJson = payloads[patchIndex]!;
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
              patches: patchIndex + 1,
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
              patch: patchIndex + 1,
            },
          ),
        );

        if (result.failed.length > 0) {
          processedPatchCount = patchIndex + 1;
          onProgress?.(
            makeEditorLiveProgressTool(
              "apply_patch",
              "error",
              {
                file: detail.documentName || "current file",
                patch: patchIndex + 1,
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
          continue;
        }

        workingContent = result.content;
        processedPatchCount = patchIndex + 1;
        appliedRef.current = {
          sessionKey,
          content: workingContent,
          complete: true,
        };
        try {
          detail.applyContent?.(workingContent, {
            complete: true,
            source: "assistant-stream",
            mode: "patch",
            diff,
            patch: truncateToolText(patchJson, 5000),
            patchCount: processedPatchCount,
            sourceLabel: "assistant patch",
          });
          onProgress?.(
            makeEditorLiveProgressTool(
              "apply_patch",
              "success",
              {
                file: detail.documentName || "current file",
                patch: patchIndex + 1,
              },
              {
                status: "ok",
                operations: result.applied,
                patch: truncateToolText(patchJson, 5000),
              },
            ),
          );
        } catch (err) {
          onProgress?.(
            makeEditorLiveProgressTool(
              "apply_patch",
              "error",
              {
                file: detail.documentName || "current file",
                patch: patchIndex + 1,
              },
              {
                status: "failed",
                error: (err as Error).message,
              },
            ),
          );
          console.warn("Editor live edit apply failed", err);
        }
      }
    };

    try {
      while (true) {
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
          const rawData = line.slice(6).trim();
          let parsedFrame: unknown = null;
          try {
            parsedFrame = JSON.parse(rawData);
          } catch {
            parsedFrame = null;
          }
          const generatedImageUrl = generatedImageUrlFromSseFrame(parsedFrame);
          if (generatedImageUrl) {
            await applyGeneratedImageUrl(generatedImageUrl);
          }
          const token = parseLiveEditStreamFrame(rawData, currentEvent);
          if (!token) continue;
          assistantText += token;
          applyPatchPayloads();
        }
      }
      if (processedPatchCount > 0) finishLiveEdit();
      else if (applyAssistantReplacementEdit()) {
        // A complete replacement was recovered from the assistant response.
      }
      else if (applyFallbackEdit()) {
        // Fallback already applied and marked complete.
      }
      else {
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
              error: "The assistant did not return a live patch.",
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
              error: "No patch was applied to the current file.",
            },
          ),
        );
      }
    } catch (err) {
      if (isActive()) console.warn("Editor live edit stream failed", err);
    }
  })();

  return liveResponse;
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
  const [messageFeedback, setMessageFeedback] = useState<
    Record<string, ChatMessageFeedbackRating>
  >({});
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
  const editorLiveAppliedRef = useRef<{
    sessionKey?: string;
    content: string;
    complete?: boolean;
  }>({ content: "" });
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
      message.id && hostOwnedWorkflowMessageIds.has(message.id)
        ? []
        : [{ message, index }]
    )),
    [hostOwnedWorkflowMessageIds, messages],
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
    (deleteConversation = false) => {
      const convId =
        editorLiveConversationIdRef.current ||
        currentConvId ||
        streamingConvId;
      const sessionKey = currentSessionKeyRef.current;
      editorLiveConversationIdRef.current = undefined;
      editorLiveDetailRef.current = null;
      editorLiveAppliedRef.current = { content: "" };
      setEditorLiveSessionActive(false);
      setEditorSessionLabel(null);
      setEditorLiveInfo(null);
      setComposerSeed(null);
      if (sessionKey) resetSession(sessionKey);
      setCurrentConvId(undefined);
      setDraftSessionKey(undefined);
      if (deleteConversation && convId) {
        api.chat
          .deleteConversation(convId)
          .then(() =>
            queryClient.invalidateQueries({ queryKey: ["conversations"] }),
          )
          .catch(() => {});
      }
    },
    [currentConvId, queryClient, resetSession, streamingConvId],
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
      if (!refItem.id) return;
      try {
        const blobUrl = await api.documents.download(refItem.id);
        window.open(blobUrl, "_blank", "noopener,noreferrer");
        window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
      } catch {
        // The thumbnail card remains visible even if the underlying file was removed.
      }
    },
    [],
  );

  /* ---- Auto-resume most recent conversation when first opened ---- */
  const resumedRef = useRef(false);
  const userIdRef = useRef<string | undefined>(currentUserId);
  const resetStream = useChatStreamStore((s) => s.reset);
  useEffect(() => {
    if (userIdRef.current !== currentUserId) {
      resetStream();
      setCurrentConvId(undefined);
      setDraftSessionKey(undefined);
      setAttachedFiles([]);
      setComposerSeed(null);
      setEditorSessionLabel(null);
      setEditorLiveInfo(null);
      setEditorLiveSessionActive(false);
      editorLiveConversationIdRef.current = undefined;
      editorLiveDetailRef.current = null;
      editorLiveAppliedRef.current = { content: "" };
      setSelectedMentions([]);
      setMentionedAgentId(undefined);
      setConversationLoading(false);
      resumedRef.current = false;
      userIdRef.current = currentUserId;
    }
  }, [currentUserId, resetStream]);

  useEffect(() => {
    if (!open || resumedRef.current) return;
    if (streamingRef.current) return;
    resumedRef.current = true;
    setConversationLoading(true);
    api.chat.listConversations()
      .then((convs) => {
        const latest = (convs || []).find(
          (conv: any) => !conv.agent_id && !conv.workspace_id,
        );
        if (!latest) return undefined;
        setCurrentConvId(latest.id);
        setDraftSessionKey(undefined);
        return loadRecentMessages(latest.id)
          .then((msgs) => {
            if (streamingRef.current) return;
            setSessionMessages(latest.id, msgs);
          })
          .catch(() => {});
      })
      .catch(() => {})
      .finally(() => setConversationLoading(false));
  }, [loadRecentMessages, open, setSessionMessages]);

  useEffect(() => {
    const handleOpenEditorLiveChat = (event: Event) => {
      const detail =
        (event as CustomEvent<EditorLiveChatDetail>).detail || {};
      const key = `editor-live-${Date.now()}-${Math.random()
        .toString(36)
        .slice(2)}`;
      const draftKey = createDraftSession();
      const attachments: AttachedItem[] =
        detail.documentId && detail.documentName
          ? [
              {
                type: "knowledge",
                id: detail.documentId,
                name: detail.documentName,
                fileType: detail.fileType || undefined,
                mimeType: detail.mimeType || undefined,
              },
            ]
          : [];

      resumedRef.current = true;
      setOpen(true);
      setCurrentConvId(undefined);
      setDraftSessionKey(draftKey);
      setSessionMessages(draftKey, []);
      setInput("");
      setAttachedFiles([]);
      setComposerSeed({ key, attachments });
      editorLiveConversationIdRef.current = undefined;
      editorLiveDetailRef.current = detail;
      editorLiveAppliedRef.current = { sessionKey: draftKey, content: "" };
      setEditorLiveSessionActive(true);
      setEditorLiveInfo(detail);
      setEditorSessionLabel(
        detail.sessionLabel?.trim() ||
          (detail.documentName ? `Live edit: ${detail.documentName}` : "Live edit"),
      );
      setSelectedMentions([]);
      setMentionedAgentId(undefined);
      window.setTimeout(() => textareaRef.current?.focus(), 0);
    };

    window.addEventListener(
      EDITOR_LIVE_CHAT_EVENT,
      handleOpenEditorLiveChat as EventListener,
    );
    return () =>
      window.removeEventListener(
        EDITOR_LIVE_CHAT_EVENT,
        handleOpenEditorLiveChat as EventListener,
      );
  }, [createDraftSession, setSessionMessages]);

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
      window.setTimeout(() => textareaRef.current?.focus(), 0);
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
    const detail = editorLiveDetailRef.current;
    if (!detail?.applyContent) return;
    const last = messages[messages.length - 1];
    if (!last || last.role !== "assistant") return;
    if (editorLiveAppliedRef.current.complete) return;
    const payloads = extractEditorLivePatchPayloads(toDisplayText(last.content));
    if (payloads.length === 0) return;
    const beforeContent = detail.getContent?.() || editorLiveAppliedRef.current.content;
    let nextContent = beforeContent;
    let applied = 0;
    let lastPatch = "";
    for (const payload of payloads) {
      const result = applyEditorLivePatch(nextContent, payload);
      if (result.failed.length > 0) continue;
      nextContent = result.content;
      applied += 1;
      lastPatch = payload;
    }
    if (applied === 0 || nextContent === editorLiveAppliedRef.current.content) return;
    editorLiveAppliedRef.current = {
      sessionKey: currentSessionKeyRef.current,
      content: nextContent,
      complete: true,
    };
    const diff = buildEditorLiveDiff(
      beforeContent,
      nextContent,
      detail.documentName || "current file",
    );
    detail.applyContent(nextContent, {
      complete: true,
      source: "assistant-stream",
      mode: "patch",
      diff,
      patch: truncateToolText(lastPatch, 5000),
      patchCount: applied,
      sourceLabel: "assistant patch",
    });
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
        loadRecentMessages(detail.conversation_id)
          .then((msgs) => setMessages(msgs))
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

  /* Focus textarea when opened */
  useEffect(() => {
    if (open) {
      setTimeout(() => textareaRef.current?.focus(), 200);
    }
  }, [open]);

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

  const addKbDoc = (doc: { id: string; name: string }) => {
    if (attachedFiles.some((f) => f.id === doc.id)) return;
    setAttachedFiles((prev) => [
      ...prev,
      { name: doc.name, id: doc.id, type: "knowledge" },
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
    (doc: { id: string; name: string }) => {
      // Remove the #query text from input
      const before = input.substring(0, hashTriggerPos);
      const cursorPos = textareaRef.current?.selectionStart || input.length;
      const after = input.substring(cursorPos);
      setInput(`${before}${after}`);
      // Add as attachment chip (same as KB doc picker)
      setAttachedFiles((prev) => {
        if (prev.some((f) => f.id === doc.id)) return prev;
        return [...prev, { name: doc.name, id: doc.id, type: "knowledge" }];
      });
      setHashDropdownOpen(false);
      setHashQuery("");
      setHashTriggerPos(-1);
      setTimeout(() => textareaRef.current?.focus(), 0);
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
    ) => {
      const rawText = (
        typeof textInput === "string" ? textInput : input
      ).trim();
      const selectedWorkflow = workflow || null;
      const effectiveManualSkills = selectedWorkflow ? [] : manualSkills;
      const text = stripWorkflowInvokeToken(
        stripManualSkillTokens(rawText, manualSkills),
        selectedWorkflow,
      );
      const attachmentSnapshot = footerAttachments || attachedFiles;
      if (
        !text &&
        attachmentSnapshot.length === 0 &&
        effectiveManualSkills.length === 0 &&
        !selectedWorkflow
      )
        return;
      let sessionKey = currentSessionKeyRef.current;
      if (!sessionKey) {
        sessionKey = createDraftSession();
        setDraftSessionKey(sessionKey);
      }
      if (useChatStreamStore.getState().sessions[sessionKey]?.streaming) return;

      // Sending is an explicit jump back to the newest message.
      autoFollowRef.current = true;

      // Stop voice if active — also drains the recorder so any in-flight
      // chunks transcribe before send (the user can edit the result
      // before pressing send if they want).
      if (listening) {
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

      setInput("");
      setSelectedMentions([]);
      setMentionedAgentId(undefined);
      const sentAttachments = [...attachmentSnapshot];
      setAttachedFiles([]);
      const requestChatMode = selectedWorkflow
        ? "flows"
        : !editorLiveSessionActive && chatMode !== "auto"
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
      const liveEditDetail = editorLiveSessionActive
        ? editorLiveDetailRef.current
        : null;
      const isLiveEdit = Boolean(liveEditDetail);
      const liveEditContent = liveEditDetail?.getContent?.();
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

      let liveEditAttachmentFiles: File[] = [];
      if (isLiveEdit && liveEditDetail?.getAttachmentFiles) {
        try {
          liveEditAttachmentFiles = await liveEditDetail.getAttachmentFiles();
        } catch (err) {
          console.warn("Editor live edit attachment capture failed", err);
        }
      }

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
            agentId: mentionedAgentId,
            localWorkerId: sendContext?.localWorkerId,
            chatMode: requestChatMode,
            chatModePayload: requestChatModePayload,
            manualSkillIds:
              effectiveManualSkills.length > 0
                ? effectiveManualSkills.map((skill) => skill.id)
                : undefined,
          }
        : undefined;
      if (retryRequest) savePendingChatRetry(retryRequest);

      const sentAttachmentSnapshots = await Promise.all(
        sentAttachments.map(createChatMessageAttachmentSnapshot),
      );

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
        },
        {
          role: "assistant" as const,
          content: "",
          timestamp: now,
          tool_calls: liveEditInitialTools,
          retryRequest,
        },
      ];

      if (liveEditDetail) {
        editorLiveAppliedRef.current = { sessionKey, content: "" };
      }
      const updateLiveEditProgress = (tool: ToolCall) => {
        setSessionMessages(sessionKey, (prev) =>
          withEditorLiveProgress(prev, tool),
        );
      };
      let liveEditClosed = false;
      const closeCompletedLiveEdit = () => {
        if (!isLiveEdit || liveEditClosed) return;
        const applied = editorLiveAppliedRef.current;
        if (
          applied.complete &&
          applied.content.trim()
        ) {
          liveEditClosed = true;
          // Keep the live-edit panel open after a successful apply so the
          // user can review the assistant's result, inspect tool status, and
          // continue with follow-up edits. The explicit close button still
          // clears the ephemeral live-edit session.
        }
      };

      await startStream(
        async () => {
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
                agentId: mentionedAgentId,
                localWorkerId: sendContext?.localWorkerId,
                chatMode: requestChatMode,
                chatModePayload: requestChatModePayload,
                manualSkillIds:
                  effectiveManualSkills.length > 0
                    ? effectiveManualSkills.map((skill) => skill.id)
                    : undefined,
                editorContext: isLiveEdit
                  ? {
                      path: liveEditDetail?.sourcePath,
                      sourcePath: liveEditDetail?.sourcePath,
                      documentId: liveEditDetail?.documentId,
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
                ephemeral: isLiveEdit,
              });
          if (!liveEditDetail?.applyContent) return response;
          return pipeEditorLiveEditStream(
            response,
            liveEditDetail,
            sessionKey,
            editorLiveAppliedRef,
            () =>
              editorLiveSessionActive &&
              editorLiveDetailRef.current === liveEditDetail,
            updateLiveEditProgress,
            closeCompletedLiveEdit,
            visibleRequest,
          );
        },
        currentConvId,
        msgsBeforeSend,
        (newConvId) => {
          if (editorLiveSessionActive) {
            editorLiveConversationIdRef.current = newConvId;
          }
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          }
        },
        sessionKey,
      );
      clearPendingChatRetry();
      if (requestChatMode) resetChatModeAfterTurn();
      if (isLiveEdit) {
        window.setTimeout(closeCompletedLiveEdit, 350);
        window.setTimeout(closeCompletedLiveEdit, 1200);
        return;
      }
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      // The agent may have created or changed Knowledge documents during this
      // turn; without this, a mounted Knowledge page never learns about them
      // (no polling, refetchOnWindowFocus off, 60s staleTime).
      invalidateKnowledgeQueries(queryClient);
    },
    [
      input,
      currentConvId,
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
    ],
  );

  const handleStopRequest = useCallback(() => {
    const convId = currentConvId || streamingConvId;
    const hitlIds = pendingHITLIds(messages);
    if (convId) {
      void api.chat.cancelPendingFileApprovals(convId, hitlIds).then(
        () => queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        () => undefined,
      );
    }
    stopStream(currentSessionKeyRef.current);
  }, [currentConvId, streamingConvId, messages, stopStream, queryClient]);

  const handleCloseChat = useCallback(() => {
    if (editorLiveSessionActive) {
      if (streamingRef.current) stopStream(currentSessionKeyRef.current);
      clearEditorLiveSession(true);
    }
    setOpen(false);
  }, [clearEditorLiveSession, editorLiveSessionActive, stopStream]);

  useEffect(() => {
    const handleCloseEditorLiveChat = () => handleCloseChat();
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
    if (streamingRef.current) stopStream(currentSessionKeyRef.current);
    clearEditorLiveSession(true);
    setOpen(false);
  }, [
    clearEditorLiveSession,
    editorLiveInfo?.sourcePath,
    editorLiveSessionActive,
    location.pathname,
    stopStream,
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
      { role: "user" as const, content: pending.message, timestamp: now },
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
          manualSkillIds: pending.manualSkillIds,
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
  const handleNewChat = () => {
    if (editorLiveSessionActive) clearEditorLiveSession(true);
    const key = createDraftSession();
    setCurrentConvId(undefined);
    setDraftSessionKey(key);
    setInput("");
    setAttachedFiles([]);
    setComposerSeed(null);
    setEditorSessionLabel(null);
    setEditorLiveInfo(null);
    editorLiveDetailRef.current = null;
    editorLiveAppliedRef.current = { content: "" };
    setSelectedMentions([]);
    setMentionedAgentId(undefined);
    setConversationLoading(false);
  };

  const handleSwitchSession = (convId: string) => {
    if (convId === currentConvId) return;
    if (editorLiveSessionActive) clearEditorLiveSession(true);
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
      editorLiveAppliedRef.current = { content: "" };
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
    editorLiveAppliedRef.current = { content: "" };
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
              manualSkillIds: retryRequest.manualSkillIds,
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
      const previous = messageFeedback[key] || null;
      setMessageFeedback((prev) => ({ ...prev, [key]: rating }));

      const fallbackRequestPreview = messages
        .slice(0, index)
        .reverse()
        .find((item) =>
          Boolean(item?.role === "user" && toDisplayText(item?.content).trim()),
        );

      try {
        await api.chat.feedback(conversationId, message.id, {
          rating,
          content_preview: contentPreview,
          request_preview: (toDisplayText(fallbackRequestPreview?.content) || "").slice(0, 1000),
        });
      } catch {
        setMessageFeedback((prev) => {
          const next = { ...prev };
          if (previous) next[key] = previous;
          else delete next[key];
          return next;
        });
      }
    },
    [
      currentConvId,
      feedbackKeyForMessage,
      messageFeedback,
      messages,
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
      <style>{`
        @keyframes float-chat-glow {
          0%, 100% { box-shadow: 0 4px 20px rgba(67,107,101,0.35); }
          50% { box-shadow: 0 4px 20px rgba(67,107,101,0.35), 0 0 24px rgba(67,107,101,0.4), 0 0 48px rgba(79,125,117,0.2); }
        }
        .float-chat-btn:hover {
          box-shadow: 0 6px 28px rgba(67,107,101,0.5), 0 0 24px rgba(67,107,101,0.35), 0 0 48px rgba(79,125,117,0.2) !important;
          transform: scale(1.08) !important;
          animation: float-chat-glow 4s ease-in-out infinite !important;
        }
      `}</style>
      <button
        className="float-chat-btn"
        data-tour="chat-input"
        onClick={() => {
          const nextOpen = !open;
          if (
            nextOpen &&
            !resumedRef.current &&
            !streamingRef.current &&
            messages.length === 0
          ) {
            setConversationLoading(true);
          }
          if (!nextOpen) setConversationLoading(false);
          setOpen(nextOpen);
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
          transition: "all 0.25s cubic-bezier(0.16,1,0.3,1)",
          transform: open ? "scale(0)" : "scale(1)",
          opacity: open ? 0 : 1,
          pointerEvents: open ? "none" : "auto",
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
        open={open}
        zIndex={1001}
        ariaLabel={editorLiveSessionActive ? "AI edit" : t("page.chat_history.manor_ai")}
      >
        {/* ── Header ── */}
        <PanelHeader
          avatar={<ManorAvatar size={34} />}
          title={editorLiveSessionActive ? "AI edit" : t("page.chat_history.manor_ai")}
          subtitle={editorSessionLabel ? (
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
                title={t("page.flows.close")}
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 8,
                  border: "none",
                  background: "transparent",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "var(--text-faint, #a8a29e)",
                  transition: "all 0.15s",
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
                      <span
                        key={example}
                        style={{
                          padding: "4px 8px",
                          borderRadius: 999,
                          border: "1px solid var(--accent-soft-border, rgba(67,107,101,0.18))",
                          background: "var(--accent-soft, #f2f6f5)",
                          color: "var(--accent, #436b65)",
                          fontSize: 10,
                          fontWeight: 700,
                        }}
                      >
                        {example}
                      </span>
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
            const suppressApprovalBubble =
              isApprovalBoilerplateContent(msg) && !isLatestStreaming;
            const showCreditLimitNotice =
              msg.role === "assistant" &&
              msg.stop_reason === "credit_exhausted";
            const actionCopyText = msg.role === "user" ? content : rawBubbleContent;
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
                            content={bubbleContent}
                            keyPrefix={i}
                            streaming={assistantWorking && i === messages.length - 1}
                          />
                          <ChatMessageReferenceStrip
                            references={parseUserMessageDisplay(msg).references}
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
                    const display = parseUserMessageDisplay({
                      ...msg,
                      content: bubbleContent,
                    });
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
                          disabled={streaming}
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
          value={input}
          onChange={handleComposerChange}
          streaming={streaming}
          onSend={(text, attachments, manualSkills, context) => {
            void handleSend(text, attachments, manualSkills, null, context);
          }}
          onSendWorkflow={(text, attachments, manualSkills, workflow, context) => {
            void handleSend(text, attachments, manualSkills, workflow, context);
          }}
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
                      window.setTimeout(() => textareaRef.current?.focus(), 0);
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
          seedAttachments={composerSeed?.attachments}
          seedAttachmentsKey={composerSeed?.key}
          className="floating-chat-footer"
        />
      </FloatingPanel>
    </>
  );
}
