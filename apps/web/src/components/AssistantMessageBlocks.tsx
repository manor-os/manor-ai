import ChatMarkdown from "./ChatMarkdown";
import CollapsibleSentMessage from "./chat/CollapsibleSentMessage";
import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import type {
  AssistantBlock,
  AssistantProcessStep,
  AssistantResponseSurfaceBlock,
  HITLRequest,
  ResponseSurfaceSubmissionReceipt,
  ResponseSurfaceSubmissionResult,
  SubAgentEvent,
  ToolCall,
} from "../lib/chatStream";
import {
  formatByokProviderErrorMessage,
  formatFilePermissionErrorMessage,
  formatPublicToolResult,
  LLM_CONFIGURATION_ERROR_MESSAGE,
} from "../lib/chatStream";
import type { WorkspaceLedgerOverview } from "../lib/api";
import { groupConsecutiveByKey, hasUnrecoveredProcessError, isUnrecoveredProcessStep, shouldExpandAssistantProcessBlock } from "../lib/assistantProcessFlow";
import { isWebsiteAccessDenied, websiteAccessDeniedSource } from "../lib/websiteAccessDenied";
import {
  formatProcessDuration,
  isLiveAssistantProcess,
  processClockTiming,
  useLiveProcessClock,
} from "../lib/assistantProcessPresentation";
import {
  projectAssistantText,
  projectAssistantProcessTimeline,
  type AssistantProcessTimelineEntry,
  visibleAssistantText,
} from "../lib/assistantTextProjection";
import { t } from "../lib/i18n";
import { matchSubAgentRuns } from "../lib/subAgentDisplay";
import { formatUserFacingStructuredText } from "../lib/taskDisplay";
import { normalizeResponseSurfaceBlock } from "../lib/responseSurface";
import { PendingActionKind } from "../lib/pendingActionKinds";
import {
  coalesceWorkspaceLedgerQueryBlocks,
  isWorkspaceLedgerOverview,
  isWorkspaceLedgerQueryVisualization,
  workspaceLedgerOverviewFrameHeights,
  workspaceLedgerQueryFrameHeights,
} from "../lib/workspaceLedgerVisualization";
import AgentLoopStep from "./ui/AgentLoopStep";
import ToolActivityGroup from "./ui/ToolActivityGroup";
import { summarizeToolActivity } from "../lib/toolActivitySummary";
import AgentActivityOrb, { agentActivityLabel, inferAgentActivity } from "./ui/AgentActivityOrb";
import InteractiveResponseSurface from "./InteractiveResponseSurface";
import Button from "./ui/Button";
import { IconInfo } from "./icons";

export type AssistantPendingActionKind = "approval" | "input" | null;

type AssistantProcessHITLRequest = Pick<
  HITLRequest,
  "type" | "tool" | "resolved" | "resolution"
> & Partial<Pick<HITLRequest, "id">>;

type AssistantProcessDisplayStatus = "running" | "error" | "completed" | "not_run" | "unverified" | "unavailable";

const NOT_RUN_APPROVAL_RESOLUTIONS = new Set([
  "reject",
  "rejected",
  "deny",
  "denied",
  "decline",
  "no",
  "revise",
  "cancel",
  "cancelled",
  "canceled",
  "expired",
]);

type AssistantPendingActionMessage = {
  hitl_requests?: Array<{ resolved?: boolean; type?: string }> | null;
  pending_action?: { kind?: string; hitl_type?: unknown } | null;
  resolved_at?: unknown;
};

const INPUT_PENDING_ACTION_KINDS = new Set<string>([
  PendingActionKind.HUMAN_INPUT,
  PendingActionKind.NEEDS_INPUT,
  PendingActionKind.NEEDS_LOGIN,
  PendingActionKind.TASK_RECOVERY,
  PendingActionKind.RETRY_STRATEGIST_REVIEW,
  PendingActionKind.WORKFLOW_INPUT,
  PendingActionKind.WORKFLOW_STARTER_INPUT,
  PendingActionKind.WORKFLOW_RETRY,
]);

export function assistantPendingActionKindForMessage(
  message: AssistantPendingActionMessage,
): AssistantPendingActionKind {
  const unresolved = (message.hitl_requests || []).filter((request) => !request.resolved);
  if (unresolved.some((request) => request.type === "approval")) return "approval";
  if (unresolved.length > 0) return "input";
  if (message.resolved_at || !message.pending_action?.kind) return null;
  const hitlType = String(message.pending_action.hitl_type || "").toLowerCase();
  if (hitlType === "error" || hitlType === "failure") return null;
  return INPUT_PENDING_ACTION_KINDS.has(message.pending_action.kind)
    ? "input"
    : "approval";
}

function processStepToToolCall(step: AssistantProcessStep): ToolCall {
  const status =
    step.status === "running" || step.status === "pending"
      ? "pending"
      : step.status === "error"
        ? "error"
        : "success";
  return {
    name: step.name || "tool",
    arguments: step.arguments_preview,
    result: formatPublicToolResult(
      step.result_preview || step.summary || step.display_name,
    ),
    status,
    duration:
      typeof step.duration_ms === "number"
        ? `${(step.duration_ms / 1000).toFixed(2)}s`
        : undefined,
  };
}

function CollapsibleFinalMarkdown({
  content,
  isUser,
  streaming,
  returnTo,
  enabled,
  enableFileCards,
}: {
  content: string;
  isUser: boolean;
  streaming: boolean;
  returnTo?: string;
  enabled: boolean;
  enableFileCards?: boolean;
}) {
  return (
    <CollapsibleSentMessage text={content} enabled={enabled && !streaming} tone={isUser ? "user" : "assistant"}>
      <ChatMarkdown
        content={content}
        isUser={isUser}
        streaming={streaming}
        returnTo={returnTo}
        enableFileCards={enableFileCards}
      />
    </CollapsibleSentMessage>
  );
}

function normalizeStepStatus(status?: string): AssistantProcessDisplayStatus {
  if (status === "running" || status === "pending") return "running";
  if (status === "error") return "error";
  if (status === "unverified") return "unverified";
  if (status === "not_run") return "not_run";
  return "completed";
}

const PUBLIC_ATTEMPT_REASON_KEYS = {
  "The selected attachment is no longer accessible for approval.": "component.assistant_message_blocks.attachment_unavailable_for_approval",
  "The selected attachment is missing or no longer accessible.": "component.assistant_message_blocks.attachment_unavailable",
  "The requested file is missing or no longer accessible.": "component.assistant_message_blocks.source_file_unavailable",
  "The selected attachment exceeds the transfer size limit.": "component.assistant_message_blocks.attachment_too_large",
  "The selected attachment has changed. Select it again.": "component.assistant_message_blocks.attachment_changed",
  "The selected attachment is unavailable or has changed.": "component.assistant_message_blocks.attachment_unavailable_or_changed",
  "The selected attachment is currently in use. Try again shortly.": "component.assistant_message_blocks.attachment_busy",
  "Downloading the selected attachment is not allowed.": "component.assistant_message_blocks.attachment_download_denied",
  "The Chrome connection is unavailable.": "component.assistant_message_blocks.chrome_connection_unavailable",
  "The Chrome connection has changed. Start a new transfer.": "component.assistant_message_blocks.chrome_connection_changed",
} as const;

function visibleProcessStepError(
  status: AssistantProcessDisplayStatus,
  resultText?: string,
): string {
  if (status !== "error") return "";
  const result = String(resultText || "").trim();
  const structured = parsePreviewObject(result);
  // A failed command can still print a document to stdout. Only explicit
  // diagnostics belong in the inline error; the complete output stays in details.
  const diagnostic = structured
    ? [structured.stderr, structured.error, structured.message]
      .find((value) => typeof value === "string" && value.trim())
    : result;
  const text = String(formatPublicToolResult(diagnostic) || "").trim();
  if (!text || text.length > 280 || /[\r\n]/.test(text)
    || /^(?:[#{[>`]|[-+*]\s|\d+[.)]\s)/.test(text)) return "";
  // A generic failure sentence says nothing the step state does not; the
  // step reads as what was done. Specific, actionable reasons stay below.
  if (text === t("lib.chat_stream.request_failed_with_detail").split("\n\n", 1)[0]
    || text === "Sorry, the request failed. Please try again.") {
    return "";
  }
  if (Object.prototype.hasOwnProperty.call(PUBLIC_ATTEMPT_REASON_KEYS, text)) {
    return t(PUBLIC_ATTEMPT_REASON_KEYS[text as keyof typeof PUBLIC_ATTEMPT_REASON_KEYS]);
  }
  const publicMessage = formatFilePermissionErrorMessage(text) || formatByokProviderErrorMessage(text);
  if (publicMessage) return publicMessage;
  if (!structured
    && text !== LLM_CONFIGURATION_ERROR_MESSAGE
    && text !== t("lib.chat_stream.request_failed_with_detail").split("\n\n", 1)[0]
    && !/^(?:(?:error|failure|failed|timeout|timed out|permission denied|authentication required|authorization required|rate limit|quota|credits? exhausted)\b|(?:[\w.]+(?:Error|Exception))[:\s]|command exited with (?:status|code) -?[1-9]\d*\b|(?:(?:this|the)\s+)?(?:execution|command|request|operation|render(?:ing)?|sandbox|file|connection)\b.{0,40}\b(?:failed|denied|unavailable|blocked|not found|timed out)\b)/i.test(text)) return "";
  return formatUserFacingStructuredText(text);
}

function neutralProcessStepCaption(step: AssistantProcessStep): string {
  if (step.presentation?.tone !== "neutral") return "";
  if (step.presentation.kind === "chrome_wait_observation_timeout") {
    return t("component.assistant_message_blocks.chrome_wait_observation_timeout");
  }
  if (step.presentation.kind === "chrome_stale_snapshot_rejection") {
    return t("component.assistant_message_blocks.chrome_stale_snapshot_rejection");
  }
  return "";
}

function completedProcessOutcomeTitle(outcomeStatus: unknown): string | undefined {
  if (outcomeStatus === "answered" || outcomeStatus === "completed") {
    return t("component.assistant_message_blocks.processed");
  }
  if (outcomeStatus === "partial") {
    return t("component.assistant_message_blocks.process_partial");
  }
  if (outcomeStatus === "blocked") {
    return t("component.assistant_message_blocks.process_needs_attention");
  }
  if (outcomeStatus === "not_attempted") {
    return t("component.assistant_message_blocks.process_not_run");
  }
  return undefined;
}

function displayProcessStepStatus(
  step: AssistantProcessStep,
  steps: AssistantProcessStep[],
  index: number,
  isLiveTurn: boolean,
): AssistantProcessDisplayStatus {
  if (isWebsiteAccessDenied(step)) return "unavailable";
  const normalized = normalizeStepStatus(step.status);
  if (normalized === "running") return isLiveTurn ? "running" : "completed";
  if (normalized === "unverified") return "completed";
  if (step.status === "error" && !isUnrecoveredProcessStep(steps, index)) return "completed";
  return normalized;
}

function isApprovalGateStep(step: AssistantProcessStep) {
  const preview = String(step.result_preview || "");
  return (
    /"__hitl__"\s*:\s*true/.test(preview) ||
    /"(?:error|status)"\s*:\s*"approval_required"/.test(preview)
  );
}

function approvalGateStepStatusByIndex(
  steps: AssistantProcessStep[],
  hitlRequests?: AssistantProcessHITLRequest[] | null,
) {
  const statuses = new Map<number, "running" | "completed" | "not_run">();
  const requests = hitlRequests || [];
  for (let requestIndex = requests.length - 1; requestIndex >= 0; requestIndex -= 1) {
    const request = requests[requestIndex];
    const tool = String(request?.tool || "").trim();
    if (request?.type !== "approval" || !tool) continue;
    for (let stepIndex = steps.length - 1; stepIndex >= 0; stepIndex -= 1) {
      const step = steps[stepIndex];
      if (
        statuses.has(stepIndex) ||
        (!(step.approval_request_id === request.id && request.id) &&
          (step.name !== tool ||
            normalizeStepStatus(step.status) !== "error" ||
            !isApprovalGateStep(step)))
      ) {
        continue;
      }
      const resolution = String(request.resolution || "").trim().toLowerCase();
      const wasNotRun = NOT_RUN_APPROVAL_RESOLUTIONS.has(resolution);
      statuses.set(
        stepIndex,
        request.resolved ? (wasNotRun ? "not_run" : "completed") : "running",
      );
      break;
    }
  }
  return statuses;
}

function hasAny(value: string, tokens: string[]) {
  return tokens.some((token) => value.includes(token));
}

function processVerb(step: AssistantProcessStep) {
  if (step.display_key) {
    return t(step.display_key, step.display_params);
  }
  const name = String(step.name || "").toLowerCase();
  const display = step.display_name || step.name || "tool";
  const args = parsePreviewObject(step.arguments_preview);
  const result = parsePreviewObject(formatPublicToolResult(step.result_preview));
  const genericTarget = stepTarget(step, args, display);
  if (name === "manor") {
    const manorParams =
      args?.params && typeof args.params === "object" && !Array.isArray(args.params)
        ? args.params
        : null;
    const action = String(args?.action || "").toLowerCase();
    const manorQuery = String(
      args?.query ||
        args?.q ||
        args?.search ||
        args?.keyword ||
        manorParams?.query ||
        manorParams?.q ||
        manorParams?.search ||
        manorParams?.keyword ||
        "",
    ).trim();
    if (action === "list_tasks") {
      return t("component.assistant_process.tool.manor.list_tasks");
    }
    if (action === "create_task" || action === "add_task") {
      return t("component.assistant_process.tool.manor.create_task", {
        target: step.summary || t("component.assistant_message_blocks.knowledge_base"),
      });
    }
    if (action === "update_task" || action === "complete_task" || action === "delete_task") {
      return t("component.assistant_process.tool.manor.update_task", {
        target: step.summary || t("component.assistant_message_blocks.knowledge_base"),
      });
    }
    if (action === "list_documents") {
      const count = typeof result?.count === "number" ? result.count : undefined;
      return t("component.assistant_process.tool.manor.list_documents", {
        target: step.summary || t("component.assistant_message_blocks.knowledge_base"),
        ...(count ? { count } : {}),
      });
    }
    if (action === "search_documents") {
      return t("component.assistant_process.tool.manor.search_documents", {
        target:
          manorQuery ||
          step.summary ||
          t("component.assistant_message_blocks.knowledge_base"),
      });
    }
    if (action.includes("search")) {
      return t("component.assistant_process.tool.manor.search", {
        target: step.summary || t("component.assistant_message_blocks.knowledge_base"),
      });
    }
    if (action) {
      return t("component.assistant_message_blocks.ran_action", {
        action: action.replace(/_/g, " "),
      });
    }
    return t("component.assistant_process.tool.manor.action", { action: "Manor" });
  }
  if (name.startsWith("mcp__")) {
    return mcpProcessVerb(name, args, genericTarget);
  }
  if (name === "search_tools") {
    return t("component.assistant_process.tool.discovery.search_tools", { target: genericTarget || "tools" });
  }
  if (name === "list_skills") {
    return t("component.assistant_process.tool.discovery.list_skills", { target: "skills" });
  }
  if (name === "get_skill_details") {
    return t("component.assistant_process.tool.discovery.skill_details", { target: genericTarget || "skill" });
  }
  if (name === "rag") {
    const displayKey =
      result?.content_evidence_available === false
        ? "component.assistant_process.tool.knowledge.rag_no_content"
        : "component.assistant_process.tool.knowledge.rag";
    return t(displayKey, {
      target: genericTarget || "knowledge",
    });
  }
  if (name === "search_documents") {
    return t("component.assistant_process.tool.knowledge.document_search", {
      target: genericTarget || "documents",
    });
  }
  if (name === "search_tasks") {
    return t("component.assistant_process.tool.workspace.search", { target: genericTarget || "workspace" });
  }
  if (name.startsWith("workspace_")) {
    if (name === "workspace_search" || name === "workspace_list_knowledge" || name === "rag") {
      return t("component.assistant_process.tool.workspace.search", { target: genericTarget || "workspace" });
    }
    if (name === "workspace_create_task") {
      return t("component.assistant_process.tool.workspace.create_task", { target: genericTarget || "task" });
    }
    if (name === "workspace_update_task_runtime") {
      return t("component.assistant_process.tool.workspace.update_task", { target: genericTarget || "task" });
    }
    if (name.includes("knowledge")) {
      return t("component.assistant_process.tool.workspace.knowledge", { target: genericTarget || "knowledge" });
    }
    if (name.includes("rule")) {
      return t("component.assistant_process.tool.workspace.rule", { target: genericTarget || "rule" });
    }
    if (name.includes("review")) {
      return t("component.assistant_process.tool.workspace.review", { target: genericTarget || "review" });
    }
    return t("component.assistant_process.tool.workspace.operate", { target: genericTarget || "workspace" });
  }
  if (name.startsWith("ws_")) {
    if (name.includes("search")) {
      return t("component.assistant_process.tool.workspace.search", { target: genericTarget || "workspace" });
    }
    if (name.includes("lint") || name.includes("get_draft")) {
      return t("component.assistant_process.tool.workspace.review", { target: genericTarget || "workspace" });
    }
    if (name.includes("rule")) {
      return t("component.assistant_process.tool.workspace.rule", { target: genericTarget || "rule" });
    }
    return t("component.assistant_process.tool.workspace.operate", { target: genericTarget || "workspace" });
  }
  if (name === "web_search" || name === "browse_web") {
    return t("component.assistant_process.tool.web.search", { target: genericTarget || "web" });
  }
  if (name === "web_fetch") {
    return t("component.assistant_process.tool.web.fetch", { target: genericTarget || "web page" });
  }
  if (name.startsWith("sandbox_") || name === "list_sandbox_files" || name === "save_sandbox_file") {
    if (name === "sandbox_exec") {
      return t("component.assistant_process.tool.sandbox.exec", { target: genericTarget || "sandbox" });
    }
    if (name === "sandbox_read_file" || name === "list_sandbox_files") {
      return t("component.assistant_process.tool.sandbox.read", { target: genericTarget || "sandbox" });
    }
    if (name === "sandbox_write_file" || name === "sandbox_save_result" || name === "save_sandbox_file") {
      return t("component.assistant_process.tool.sandbox.write", { target: genericTarget || "sandbox" });
    }
    if (name === "sandbox_create") {
      return t("component.assistant_process.tool.sandbox.create", { target: genericTarget || "sandbox" });
    }
    if (name === "sandbox_destroy") {
      return t("component.assistant_process.tool.sandbox.destroy", { target: genericTarget || "sandbox" });
    }
    return t("component.assistant_process.tool.sandbox.use", { target: genericTarget || "sandbox" });
  }
  if (["read_file", "list_files", "glob_files", "grep_files", "delete_file", "write_file", "edit_file"].includes(name)) {
    if (name === "grep_files" || name === "glob_files") {
      return t("component.assistant_process.tool.file.search", { target: genericTarget || "file" });
    }
    if (name === "read_file" || name === "list_files") {
      return t("component.assistant_process.tool.file.read", { target: genericTarget || "file" });
    }
    if (name === "delete_file") {
      return t("component.assistant_process.tool.file.delete", { target: genericTarget || "file" });
    }
    if (name === "edit_file") {
      return t("component.assistant_process.tool.file.edit", { target: genericTarget || "file" });
    }
    return t("component.assistant_process.tool.file.write", { target: genericTarget || "file" });
  }
  if (["browse_web", "take_screenshot", "interact_with_page"].includes(name)) {
    if (name.includes("screenshot") || name.includes("extract")) {
      return t("component.assistant_process.tool.browser.observe", { target: genericTarget || "browser" });
    }
    if (name.includes("interact") || name.includes("perform") || name.includes("login")) {
      return t("component.assistant_process.tool.browser.interact", { target: genericTarget || "browser" });
    }
    return t("component.assistant_process.tool.browser.open", { target: genericTarget || "browser" });
  }
  if (["send_email", "delete_message", "move_message", "mark_read", "mark_unread"].includes(name)) {
    if (name === "send_email") {
      return t("component.assistant_process.tool.email.send", { target: genericTarget || "email" });
    }
    return t("component.assistant_process.tool.email.use", { target: genericTarget || "email" });
  }
  if (
    [
      "send_message",
      "send_direct_message",
      "send_group_message",
      "send_document",
      "send_photo",
      "send_image_message",
      "send_template_message",
      "send_text_message",
    ].includes(name)
  ) {
    return t("component.assistant_process.tool.message.send", { target: genericTarget || "message" });
  }
  if (["post_tweet", "delete_tweet", "comment_on_post", "post_comment", "add_reaction", "daily_posts", "me"].includes(name)) {
    if (name === "daily_posts" || name === "me") {
      return t("component.assistant_process.tool.social.search", { target: genericTarget || "profile" });
    }
    if (name.includes("delete") || name.includes("comment") || name.includes("reaction")) {
      return t("component.assistant_process.tool.social.interact", { target: genericTarget || "social" });
    }
    return t("component.assistant_process.tool.social.publish", { target: genericTarget || "social" });
  }
  if (["new_chat", "continue_chat", "ask", "follow_up", "code", "review", "check_path"].includes(name)) {
    if (name === "check_path") {
      return t("component.assistant_process.tool.coding.check", { target: genericTarget || display });
    }
    if (name === "code" || name === "review") {
      return t(name === "review" ? "component.assistant_process.tool.coding.review" : "component.assistant_process.tool.coding.run", {
        target: genericTarget || display,
      });
    }
    return t("component.assistant_process.tool.ai_web.ask", { target: genericTarget || display });
  }
  if (name === "extract" || name === "extract_data") {
    return t("component.assistant_process.tool.web.fetch", { target: genericTarget || "content" });
  }
  if (["provision_agent", "find_team_members", "notify_user", "start_workspace_draft"].includes(name)) {
    return t("component.assistant_process.tool.workspace.operate", { target: genericTarget || "workspace" });
  }
  if (["nango_proxy", "nango_list_connections", "nango_list_providers"].includes(name)) {
    return t(name.includes("list") ? "component.assistant_process.tool.integration.list" : "component.assistant_process.tool.integration.manage", {
      target: genericTarget || "integration",
    });
  }
  if (["create_skill", "update_skill", "delete_skill"].includes(name)) {
    return t("component.assistant_process.tool.skill", { target: genericTarget || "skill" });
  }
  if (
    [
      "create_scheduled_job",
      "list_scheduled_jobs",
      "cancel_scheduled_job",
      "toggle_scheduled_job",
      "run_scheduled_job_now",
    ].includes(name)
  ) {
    return t("component.assistant_process.tool.automation.manage", { target: genericTarget || "automation" });
  }
  if (
    [
      "generate_file",
      "generate_image",
      "generate_video",
      "wait_media_jobs",
      "merge_videos",
      "align_subtitles",
      "compose_video_timeline",
    ].includes(name)
  ) {
    return t("component.assistant_process.tool.media.generate", { target: genericTarget || display });
  }
  if (name.includes("list") || name.includes("browse")) {
    return t("component.assistant_process.tool.browse", { target: genericTarget || display });
  }
  if (name.includes("search") || name.includes("grep") || name === "rg") {
    return t("component.assistant_process.tool.search", { target: genericTarget || display });
  }
  if (name.includes("read")) {
    return t("component.assistant_process.tool.read", { target: genericTarget || display });
  }
  if (name.includes("write") || name.includes("generate") || name.includes("create")) {
    return t("component.assistant_process.tool.generate", { target: genericTarget || display });
  }
  if (name.includes("edit") || name.includes("patch") || name.includes("update")) {
    return t("component.assistant_process.tool.modify", { target: genericTarget || display });
  }
  return t("component.assistant_process.tool.generic", { target: display });
}

function websiteAccessDeniedLabel(step: AssistantProcessStep) {
  const source = websiteAccessDeniedSource(step);
  return source
    ? t("component.assistant_message_blocks.website_access_denied_source", { source })
    : t("component.assistant_message_blocks.website_access_denied");
}

function prettyIdentifier(value?: string) {
  return String(value || "").replace(/[_-]+/g, " ").trim();
}

function basename(value?: unknown) {
  const text = String(value || "").trim();
  if (!text) return "";
  if (text.includes("://")) return text;
  return text.replace(/\\/g, "/").split("/").filter(Boolean).pop() || text;
}

function stepTarget(step: AssistantProcessStep, args: Record<string, any> | null, fallback = "") {
  const value =
    args?.query ??
    args?.q ??
    args?.keyword ??
    args?.pattern ??
    args?.prompt ??
    args?.question ??
    args?.url ??
    args?.uri ??
    args?.path ??
    args?.file ??
    args?.filename ??
    args?.output_name ??
    args?.name ??
    args?.title ??
    args?.to ??
    args?.recipient ??
    args?.channel ??
    args?.chat ??
    args?.customer ??
    args?.repo ??
    args?.repository ??
    args?.worksheet ??
    args?.sheet ??
    args?.cmd ??
    args?.command ??
    args?.username ??
    args?.user ??
    args?.cwd ??
    step.summary ??
    fallback;
  return basename(value);
}

function mcpProcessVerb(name: string, args: Record<string, any> | null, target: string) {
  const parts = name.split("__").filter(Boolean);
  const server = parts[1] || "";
  const tool = parts[parts.length - 1] || "";
  const serverText = prettyIdentifier(server);
  const toolText = prettyIdentifier(tool);
  const lowerServer = server.toLowerCase();
  const lowerTool = tool.toLowerCase();
  const isSocial = [
    "twitter",
    "twitter_x",
    "x",
    "xiaohongshu",
    "linkedin",
    "facebook",
    "instagram",
    "tiktok",
    "tiktok_shop",
    "youtube",
    "wechat",
    "wechat_personal",
  ].some((platform) => lowerServer.includes(platform));

  if (isSocial) {
    if (["search", "list", "get", "observe"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.social.search", { target: target || serverText });
    }
    if (["publish", "post", "create", "tweet", "draft", "upload"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.social.publish", { target: serverText });
    }
    if (["comment", "reply", "like", "follow", "retweet", "share"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.social.interact", { target: serverText });
    }
    return t("component.assistant_process.tool.social.use", { target: target || serverText });
  }
  if (lowerServer === "chrome" || lowerServer.includes("browser")) {
    if (["open", "goto", "navigate"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.browser.open", { target: target || serverText });
    }
    if (lowerTool === "read_page" || lowerTool === "get_interactive_elements") {
      return t("component.assistant_process.tool.browser.read_page", { target: target || serverText });
    }
    if (lowerTool === "get_web_content" || lowerTool === "get_content") {
      return t("component.assistant_process.tool.browser.extract", { target: target || serverText });
    }
    if (lowerTool === "wait") {
      return t("component.assistant_process.tool.browser.wait", { target: target || serverText });
    }
    if (lowerTool === "inject_script" || lowerTool === "send_cdp") {
      return t("component.assistant_process.tool.browser.script", { target: target || serverText });
    }
    if (["observe", "screenshot", "status"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.browser.observe", { target: target || serverText });
    }
    if (["click", "fill", "type", "scroll", "press"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.browser.interact", { target: target || serverText });
    }
    if (["computer", "hover", "upload", "set_cursor", "hide_cursor", "set_badge"].includes(lowerTool)) {
      return t("component.assistant_process.tool.browser.interact", { target: target || serverText });
    }
    return t("component.assistant_process.tool.browser.use", { target: target || serverText });
  }
  if (
    lowerServer.includes("codex") ||
    lowerServer.includes("claude_code") ||
    lowerServer.includes("aider") ||
    lowerServer.includes("continue_cli") ||
    lowerServer.includes("cursor_cli") ||
    lowerServer.includes("gemini_cli")
  ) {
    if (lowerTool.includes("check_path")) {
      return t("component.assistant_process.tool.coding.check", { target: target || serverText });
    }
    if (lowerTool.includes("review") || lowerTool.includes("diff")) {
      return t("component.assistant_process.tool.coding.review", { target: target || serverText });
    }
    return t("component.assistant_process.tool.coding.run", { target: target || serverText });
  }
  if (lowerServer.includes("calendar")) {
    if (hasAny(lowerTool, ["create", "quick_add", "accept", "respond"])) {
      return t("component.assistant_process.tool.calendar.create", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["update", "move", "tentatively"])) {
      return t("component.assistant_process.tool.calendar.update", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["delete", "cancel", "decline"])) {
      return t("component.assistant_process.tool.calendar.delete", { target: target || serverText });
    }
    return t("component.assistant_process.tool.calendar.search", { target: target || serverText });
  }
  if (lowerServer.includes("teams") || lowerServer.includes("telegram")) {
    if (hasAny(lowerTool, ["send", "reply", "answer", "create_chat", "meeting"])) {
      return t("component.assistant_process.tool.message.send", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["list", "get", "channel", "chat", "presence"])) {
      return t("component.assistant_process.tool.message.read", { target: target || serverText });
    }
    return t("component.assistant_process.tool.message.use", { target: target || serverText });
  }
  if (["gmail", "outlook", "mail", "email"].some((token) => lowerServer.includes(token))) {
    if (["send", "draft", "compose", "reply"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.email.send", { target: target || serverText });
    }
    if (["search", "list", "get", "read"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.email.read", { target: target || serverText });
    }
    return t("component.assistant_process.tool.email.use", { target: target || serverText });
  }
  if (lowerServer.includes("excel") || lowerServer.includes("sheet")) {
    if (hasAny(lowerTool, ["read", "get", "list"])) {
      return t("component.assistant_process.tool.spreadsheet.read", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["write", "update", "add", "create", "rename", "clear", "delete"])) {
      return t("component.assistant_process.tool.spreadsheet.write", { target: target || serverText });
    }
    return t("component.assistant_process.tool.spreadsheet.analyze", { target: target || serverText });
  }
  if (["knowledge", "drive", "notion", "docs"].some((token) => lowerServer.includes(token))) {
    if (["save", "write", "create", "upload"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.knowledge.save", { target: target || serverText });
    }
    if (["search", "list", "find"].some((token) => lowerTool.includes(token))) {
      return t("component.assistant_process.tool.knowledge.search", { target: target || serverText });
    }
    return t("component.assistant_process.tool.knowledge.read", { target: target || serverText });
  }
  if (lowerServer.includes("tavily") || lowerServer.includes("perplexity")) {
    if (lowerTool.includes("fetch") || lowerTool.includes("extract")) {
      return t("component.assistant_process.tool.web.fetch", { target: target || serverText });
    }
    return t("component.assistant_process.tool.web.search", { target: target || serverText });
  }
  if (lowerServer.includes("nango")) {
    if (hasAny(lowerTool, ["list", "providers", "connections"])) {
      return t("component.assistant_process.tool.integration.list", { target: target || serverText });
    }
    return t("component.assistant_process.tool.integration.manage", { target: target || serverText });
  }
  if (lowerServer.includes("github")) {
    if (hasAny(lowerTool, ["search", "list", "get", "read", "repo_info", "compare"])) {
      return t("component.assistant_process.tool.repo.search", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["create", "push", "merge", "update", "add", "remove", "rerun", "run", "request", "fork"])) {
      return t("component.assistant_process.tool.repo.create", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["delete", "close", "cancel"])) {
      return t("component.assistant_process.tool.repo.modify", { target: target || serverText });
    }
    return t("component.assistant_process.tool.repo.use", { target: target || serverText });
  }
  if (["shopify", "woocommerce", "tiktok_shop", "amazon", "square"].some((token) => lowerServer.includes(token))) {
    if (hasAny(lowerTool, ["list", "get", "search", "query"])) {
      return t("component.assistant_process.tool.commerce.search", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["create", "put", "add"])) {
      return t("component.assistant_process.tool.commerce.create", { target: target || serverText });
    }
    return t("component.assistant_process.tool.commerce.update", { target: target || serverText });
  }
  if (lowerServer.includes("stripe") || lowerServer.includes("quickbooks")) {
    if (hasAny(lowerTool, ["list", "get", "search", "query", "report"])) {
      return t("component.assistant_process.tool.finance.search", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["create", "send"])) {
      return t("component.assistant_process.tool.finance.create", { target: target || serverText });
    }
    return t("component.assistant_process.tool.finance.update", { target: target || serverText });
  }
  if (["replicate", "jimeng", "elevenlabs"].some((token) => lowerServer.includes(token))) {
    if (hasAny(lowerTool, ["list", "voice", "model"])) {
      return t("component.assistant_process.tool.media.search", { target: target || serverText });
    }
    return t("component.assistant_process.tool.media.generate", { target: target || serverText });
  }
  return t("component.assistant_process.tool.mcp", {
    server: serverText,
    tool: toolText,
    target: target || toolText,
  });
}

function parsePreviewObject(value?: string) {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, any>)
      : null;
  } catch {
    return null;
  }
}

function StepIcon({ status, neutral = false }: { status: string; neutral?: boolean }) {
  if (status === "running") return <span className="assistant-process-spinner" />;
  if (status === "unavailable") return <span className="assistant-process-step-icon assistant-process-step-icon--unavailable" aria-hidden="true" />;
  if (status === "error" && neutral) {
    return <IconInfo size={12} className="assistant-process-step-icon assistant-process-step-icon--neutral" />;
  }
  if (status === "error") return <span className="assistant-process-step-icon assistant-process-step-icon--error" />;
  if (status === "unverified") return <span className="assistant-process-step-icon assistant-process-step-icon--unverified" title={t("component.assistant_message_blocks.process_unverified")} />;
  if (status === "not_run") return <span className="assistant-process-step-icon assistant-process-step-icon--not-run" />;
  return <span className="assistant-process-step-icon" />;
}

function notRunStepLabel(step: AssistantProcessStep) {
  const target = stepTarget(step, parsePreviewObject(step.arguments_preview));
  return target
    ? t("component.assistant_message_blocks.not_run_target", { target })
    : t("component.assistant_message_blocks.not_run_action");
}

function stripInternalProtocolText(value?: string | null) {
  return visibleAssistantText(value);
}

function sanitizeAssistantBlocks(blocks?: AssistantBlock[] | null): AssistantBlock[] {
  if (!Array.isArray(blocks)) return [];
  const sanitized = blocks.flatMap<AssistantBlock>((block) => {
    if (block.type === "text") {
      return [{
        ...block,
        text: stripInternalProtocolText(block.text),
      }];
    }
    if (block.type === "visualization") {
      if (block.kind === "workspace_ledger_overview") {
        if (!isWorkspaceLedgerOverview(block.data)) return [];
      } else if (block.kind === "ledger_query_result") {
        if (!isWorkspaceLedgerQueryVisualization(block.data)) return [];
      } else return [];
      return [block];
    }
    if (block.type === "surface") {
      const surface = normalizeResponseSurfaceBlock(block);
      return surface ? [surface] : [];
    }
    return [{
      ...block,
      title: stripInternalProtocolText(block.title),
      note: stripInternalProtocolText(block.note),
      steps: (block.steps || []).map((step) => ({
        ...step,
        display_name: stripInternalProtocolText(step.display_name),
        summary: stripInternalProtocolText(step.summary),
        assistant_text: stripInternalProtocolText(step.assistant_text),
        arguments_preview: stripInternalProtocolText(step.arguments_preview),
        result_preview: formatPublicToolResult(
          stripInternalProtocolText(step.result_preview),
        ),
      })),
    }];
  });
  return coalesceWorkspaceLedgerQueryBlocks(sanitized);
}

function AssistantLedgerVisualization({
  block,
  onConfigure,
}: {
  block: Extract<AssistantBlock, { type: "visualization" }>;
  onConfigure?: (data: WorkspaceLedgerOverview) => void;
}) {
  const isOverview = block.kind === "workspace_ledger_overview";
  const heights = block.kind === "workspace_ledger_overview"
    ? workspaceLedgerOverviewFrameHeights(block.data)
    : workspaceLedgerQueryFrameHeights(block.data);
  const title = isOverview
    ? t("component.workspace_chat.business_ledger_overview")
    : t("component.workspace_chat.ledger_query_result");
  const surface = useMemo<AssistantResponseSurfaceBlock>(() => ({
    id: block.id,
    type: "surface",
    version: 1,
    title,
    render: {
      kind: "template",
      template_id: isOverview
        ? "workspace.ledger.overview"
        : "workspace.ledger.query",
      template_version: 1,
      props: block.data as unknown as Record<string, unknown>,
    },
    display: {
      preferred: "inline",
      inline_height: heights.desktop,
      focusable: false,
    },
    actions: [],
    fallback_markdown: title,
  }), [block.data, block.id, heights.desktop, isOverview, title]);

  return (
    <section className="assistant-ledger-visualization" aria-label={title}>
      <InteractiveResponseSurface
        surface={surface}
        sourceMessageId={block.id}
        chrome={false}
        frameClassName="assistant-ledger-visualization__frame"
        frameStyle={{
          "--assistant-ledger-height": `${heights.desktop}px`,
          "--assistant-ledger-mobile-height": `${heights.mobile}px`,
        } as CSSProperties}
      />
      {onConfigure && block.kind === "workspace_ledger_overview" && (
        <div className="assistant-ledger-visualization__actions">
          <Button size="sm" onClick={() => onConfigure(block.data)}>
            {t("component.workspace_chat.configure_ledgers")}
          </Button>
        </div>
      )}
    </section>
  );
}

/** A completed turn with no tool step and an answer on screen: nothing to show. */
export function isToolFreeCompletedProcess(
  block: Extract<AssistantBlock, { type: "process" }>,
  { hasFinalOutput, pending, live }: { hasFinalOutput: boolean; pending: boolean; live: boolean },
): boolean {
  return block.lifecycle_status === "completed"
    && !(block.steps || []).length
    && !String(block.note || "").trim()
    && hasFinalOutput && !pending && !live;
}

function AssistantProcessBlock({
  block,
  timeline,
  hasFinalOutput = false,
  minimal = false,
  pendingActionKind = null,
  hitlRequests,
  returnTo,
  subAgentRuns = [],
  streaming = false,
  queueStatus,
  enableFileCards,
}: {
  block: Extract<AssistantBlock, { type: "process" }>;
  timeline: AssistantProcessTimelineEntry[];
  hasFinalOutput?: boolean;
  minimal?: boolean;
  pendingActionKind?: AssistantPendingActionKind;
  hitlRequests?: AssistantProcessHITLRequest[] | null;
  returnTo?: string;
  subAgentRuns?: SubAgentEvent[];
  streaming?: boolean;
  queueStatus?: string;
  enableFileCards?: boolean;
}) {
  const steps = block.steps || [];
  const approvalGateStatuses = approvalGateStepStatusByIndex(steps, hitlRequests);
  const displaySteps = steps.map((step, index) => {
    const status = approvalGateStatuses.get(index);
    return status ? { ...step, status } : step;
  });
  const hasRunning = displaySteps.some((step) => normalizeStepStatus(step.status) === "running");
  const hasError = hasUnrecoveredProcessError(displaySteps);
  const hasNotRun = displaySteps.some((step) => normalizeStepStatus(step.status) === "not_run");
  const hasNotRunApproval = [...approvalGateStatuses.values()].includes("not_run");
  const hasRecoveredError = displaySteps.some((step) => step.status === "error" && step.recovered_by_step_id && !isWebsiteAccessDenied(step));
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null);
  const subAgentRunByStep = useMemo(
    () => matchSubAgentRuns(steps, subAgentRuns),
    [steps, subAgentRuns],
  );
  const isLiveTurn = !queueStatus && isLiveAssistantProcess(block, streaming, Boolean(pendingActionKind));
  const expanded = expandedOverride ?? (!queueStatus && shouldExpandAssistantProcessBlock(block, {
    streaming,
    waitingForHuman: Boolean(pendingActionKind),
    hasFinalOutput,
  }));
  const lifecycle = block.lifecycle_status;
  const summarizing = hasFinalOutput || block.default_collapsed === true;
  const activity = queueStatus ? "breathing" : summarizing ? "composing" : inferAgentActivity({
    content: "",
    assistant_blocks: [{ ...block, steps: displaySteps }],
    sub_agent_events: [...subAgentRunByStep.values()],
  });
  const processFailed = lifecycle === "failed" || (!lifecycle && hasError);
  // A persisted "waiting" block whose every request was answered is not waiting.
  const waitAnswered = !pendingActionKind
    && Boolean(hitlRequests?.length)
    && Boolean(hitlRequests?.every((request) => request.resolved));
  const completedOutcomeTitle = lifecycle === "completed"
    ? completedProcessOutcomeTitle(block.outcome_status)
    : undefined;
  let title: string | null = null;
  const plainSuccess = completedOutcomeTitle === t("component.assistant_message_blocks.processed");
  if (queueStatus) {
    title = queueStatus;
  } else if (pendingActionKind === "approval") {
    title = t("component.assistant_message_blocks.process_waiting_approval");
  } else if (pendingActionKind === "input" || (lifecycle === "waiting" && !waitAnswered)) {
    title = t("component.assistant_message_blocks.process_waiting_input");
  } else if (isLiveTurn) {
    title = summarizing
      ? t("component.assistant_message_blocks.summarizing")
      : agentActivityLabel(activity);
  } else if (lifecycle === "cancelled") {
    title = t("component.assistant_message_blocks.process_stopped");
  } else if (processFailed) {
    title = t("component.assistant_message_blocks.process_error");
  } else if (completedOutcomeTitle && hasNotRunApproval) {
    title = t("component.assistant_message_blocks.process_not_run");
  } else if (completedOutcomeTitle && !(minimal && plainSuccess && displaySteps.length > 0)) {
    // In Workspace chat a plain success reads "Process · N steps" (below).
    title = completedOutcomeTitle;
  } else if (lifecycle === "completed" && (hasError || hasNotRun)) {
    title = t("component.assistant_message_blocks.processed_with_issues");
  } else if (hasNotRun) {
    title = t("component.assistant_message_blocks.process_not_run");
  } else if ((!lifecycle && hasRunning) || lifecycle === "running") {
    title = t("component.assistant_message_blocks.process_status_pending");
  } else if (hasRecoveredError) {
    title = t("component.assistant_message_blocks.processed_after_retry");
  }
  // A finished turn in Workspace chat folds into one quiet "Process · N steps".
  title ??= minimal && displaySteps.length > 0
    ? t(
      displaySteps.length === 1
        ? "component.assistant_message_blocks.process_steps_one"
        : "component.assistant_message_blocks.process_steps_other",
      { count: displaySteps.length },
    )
    : t("component.assistant_message_blocks.processed");

  const clockTiming = processClockTiming(block);
  const elapsedMs = useLiveProcessClock({
    live: isLiveTurn,
    ...clockTiming,
  });
  const duration = queueStatus || (!clockTiming.startedAtMs && !clockTiming.serverDurationMs)
    ? "" : formatProcessDuration(elapsedMs);

  const renderStep = (index: number) => {
    const step = displaySteps[index];
    const tool = processStepToToolCall(step);
    const subAgentRun = subAgentRunByStep.get(index);
    const status = displayProcessStepStatus(step, displaySteps, index, isLiveTurn);
    const stepDuration = formatProcessDuration(step.duration_ms);
    const label = status === "unavailable"
      ? websiteAccessDeniedLabel(step)
      : status === "not_run" ? notRunStepLabel(step) : processVerb(step);

    if (subAgentRun) {
      return (
        <AgentLoopStep
          key={step.id || index}
          run={subAgentRun}
          live={isLiveTurn}
        />
      );
    }

    const resultText = formatPublicToolResult(tool.result);
    const argumentsText = step.arguments_preview ? formatUserFacingStructuredText(step.arguments_preview) : "";
    const displayResultText = resultText ? formatUserFacingStructuredText(resultText) : "";
    const neutralCaption = status === "error" ? neutralProcessStepCaption(step) : "";
    const visibleError = neutralCaption || visibleProcessStepError(status, resultText);
    const detailResultText = neutralCaption || status === "unavailable" ? ""
      : status === "error" ? resultText || "" : displayResultText;
    const canInspect = !minimal && Boolean(argumentsText || detailResultText);
    const stepLabel = (
      <>
        <StepIcon status={status} neutral={status === "error"} />
        <span className="assistant-process-step-label">
          {label}
          {stepDuration && <span className="assistant-process-step-duration">{stepDuration}</span>}
        </span>
      </>
    );
    return (
      <div className="assistant-process-step-group" key={step.id || index}>
        {canInspect ? (
          <details
            className="assistant-process-step"
            title={t("component.assistant_message_blocks.view_tool_details")}
          >
            <summary>{stepLabel}</summary>
            <div className="assistant-process-step-detail">
              {argumentsText && <pre>{argumentsText}</pre>}
              {detailResultText && <pre>{detailResultText}</pre>}
            </div>
          </details>
        ) : (
          <div className="assistant-process-step--minimal">
            {stepLabel}
          </div>
        )}
        {visibleError && (
          <div className="assistant-process-step-error assistant-process-step-error--neutral">{visibleError}</div>
        )}
        {step.drafting && step.draft_preview && isLiveTurn && (
          // What the model is writing into this call right now.
          <div className="assistant-process-step-draft">{step.draft_preview}</div>
        )}
      </div>
    );
  };
  const timelineGroups = groupConsecutiveByKey(timeline, (entry) => {
    if (entry.type !== "step" || subAgentRunByStep.has(entry.stepIndex)) return null;
    const kind = displaySteps[entry.stepIndex].kind;
    return !kind || kind === "tool" ? "tools" : null;
  });

  const headerContent = (
    <>
      {(queueStatus || isLiveTurn) && <span aria-hidden="true"><AgentActivityOrb activity={activity} label={title} iconOnly paused={Boolean(queueStatus)} /></span>}
      <span className="assistant-process-flow-title" aria-live={queueStatus ? undefined : "polite"}>{title}</span>
      {duration && <span className="assistant-process-flow-duration">{duration}</span>}
      {timeline.length > 0 && (
        <svg className={`assistant-process-flow-chevron${expanded ? " expanded" : ""}`} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      )}
    </>
  );

  return (
    <section className="assistant-process-flow" role={!isLiveTurn && processFailed ? "alert" : undefined}>
      {timeline.length > 0 ? <button
        className="assistant-process-flow-header"
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpandedOverride(!expanded)}
      >
        {headerContent}
      </button> : <div className="assistant-process-flow-header" role="status">{headerContent}</div>}
      {expanded && timeline.length > 0 && (
        <div className="assistant-process-flow-body">
          {timelineGroups.map((group) => {
            const entry = group.items[0];
            if (entry.type === "commentary") {
              const liveCommentary = isLiveTurn && !hasFinalOutput && group.startIndex === timeline.length - 1;
              return (
                <div key={entry.id} className={`assistant-process-commentary${liveCommentary ? " assistant-process-commentary--live" : ""}`}>
                  <ChatMarkdown content={formatUserFacingStructuredText(entry.text)} streaming={liveCommentary} returnTo={returnTo} enableFileCards={enableFileCards} />
                </div>
              );
            }
            if (!group.key) return renderStep(entry.stepIndex);
            const indices = group.items.flatMap((item) => item.type === "step" ? [item.stepIndex] : []);
            const runningIndex = [...indices].reverse().find((index) => displayProcessStepStatus(displaySteps[index], displaySteps, index, isLiveTurn) === "running");
            return (
              <ToolActivityGroup
                key={entry.id}
                summary={summarizeToolActivity(indices.map((index) => ({
                  ...displaySteps[index],
                  status: displayProcessStepStatus(displaySteps[index], displaySteps, index, isLiveTurn),
                  label: isWebsiteAccessDenied(displaySteps[index])
                    ? websiteAccessDeniedLabel(displaySteps[index])
                    : displaySteps[index].status === "not_run" ? notRunStepLabel(displaySteps[index]) : processVerb(displaySteps[index]),
                })))}
                runningLabel={runningIndex === undefined ? undefined : processVerb(displaySteps[runningIndex])}
                liveDetail={isLiveTurn && runningIndex !== undefined && displaySteps[runningIndex].drafting
                  ? displaySteps[runningIndex].draft_preview : undefined}
              >
                {indices.map(renderStep)}
              </ToolActivityGroup>
            );
          })}
        </div>
      )}
    </section>
  );
}

export default function AssistantMessageBlocks({
  blocks: rawBlocks,
  content: rawContent,
  keyPrefix,
  isUser = false,
  streaming = false,
  minimal = false,
  collapseLongFinal = false,
  returnTo,
  subAgentRuns = [],
  onConfigureWorkspaceLedgers,
  onResponseSurfaceSubmit,
  responseSurfaceSubmissionReceipts = [],
  sourceMessageId,
  pendingActionKind = null,
  hitlRequests,
  queueStatus,
  enableFileCards,
  part = "all",
}: {
  blocks?: AssistantBlock[] | null;
  content?: string | null;
  keyPrefix: string | number;
  isUser?: boolean;
  streaming?: boolean;
  returnTo?: string;
  subAgentRuns?: SubAgentEvent[];
  onConfigureWorkspaceLedgers?: (data: WorkspaceLedgerOverview) => void;
  onResponseSurfaceSubmit?: (
    submission: ResponseSurfaceSubmissionReceipt,
  ) => void | boolean | ResponseSurfaceSubmissionResult
    | Promise<void | boolean | ResponseSurfaceSubmissionResult>;
  responseSurfaceSubmissionReceipts?: ResponseSurfaceSubmissionReceipt[];
  sourceMessageId: string;
  /** The persisted HITL/action state still waiting for the user. */
  pendingActionKind?: AssistantPendingActionKind;
  /** Exact tool approvals attached to this message's process steps. */
  hitlRequests?: AssistantProcessHITLRequest[] | null;
  /** Current resource/worker wait, projected inside the shared process card. */
  queueStatus?: string;
  /** Group files are delivered through room-scoped attachment controls. */
  enableFileCards?: boolean;
  /** Workspace chat keeps user-facing progress and tool labels, hiding raw
   *  arguments/results while preserving the same process order. */
  minimal?: boolean;
  /** Simple chat shares the same process timeline and collapsed tool details. */
  hideProcess?: boolean;
  /** Collapse long final answers on compact surfaces such as Workspace Chat. */
  collapseLongFinal?: boolean;
  /** Workspace chat puts the process at the top of the bubble, live or
   *  finished: "process" renders only it, "content" leaves it out. */
  part?: "all" | "content" | "process";
}) {
  const hasLiveProcess = streaming && !isUser && !pendingActionKind;
  const activeQueueStatus = hasLiveProcess ? queueStatus : undefined;
  const content = isUser ? rawContent : stripInternalProtocolText(rawContent);
  const blocks = useMemo(() => {
    const sanitized = sanitizeAssistantBlocks(rawBlocks);
    if (!isUser && !sanitized.some((block) => block.type === "process")
      && (hasLiveProcess || pendingActionKind || sanitized.length || content)) {
      // Live placeholders and legacy prose share the same presentation. An
      // older message has no authoritative duration or tool history to invent.
      const process: AssistantBlock = {
        id: `${keyPrefix}-live-process`, type: "process",
        lifecycle_status: activeQueueStatus || pendingActionKind ? "waiting" : hasLiveProcess ? "running" : "completed",
        default_collapsed: !hasLiveProcess, steps: [],
      };
      const finalIndex = sanitized.findIndex((block) => block.type === "text" && block.phase === "final");
      const index = finalIndex < 0 ? sanitized.length : finalIndex;
      return [...sanitized.slice(0, index), process, ...sanitized.slice(index),
        ...(!sanitized.length && content ? [{ id: `${keyPrefix}-final`, type: "text" as const,
          phase: "final" as const, text: content }] : [])];
    }
    return sanitized;
  }, [rawBlocks, content, activeQueueStatus, hasLiveProcess, pendingActionKind, isUser, keyPrefix]);

  if (!Array.isArray(blocks) || blocks.length === 0) return null;

  const {
    authoritativeContent,
    shouldUseAuthoritativeContent,
    recoveredFinalText,
    liveFinalText,
    liftedFinalText,
    fallbackFinalText,
    hasFinalOutput,
    processOpeningText,
    progressByStepSeq,
    trailingProgressText,
    shouldSuppressFinalText,
  } = projectAssistantText(content, blocks, streaming);
  const firstProcessIndex = blocks.findIndex((block) => block.type === "process");
  const lastProcessIndex = blocks.reduce((last, block, index) => block.type === "process" ? index : last, -1);

  const renderProcess = (block: Extract<AssistantBlock, { type: "process" }>, index: number) => {
    if (isToolFreeCompletedProcess(block, {
      hasFinalOutput: Boolean(hasFinalOutput),
      pending: Boolean(pendingActionKind),
      live: Boolean(activeQueueStatus) || (streaming && index === lastProcessIndex),
    })) {
      // A finished answer that called no tools has no process to report.
      return null;
    }
    return (
      <AssistantProcessBlock
        key={block.id || `${keyPrefix}-process-${index}`}
        block={block}
        timeline={projectAssistantProcessTimeline({
          steps: block.steps || [],
          openingText: index === firstProcessIndex ? processOpeningText : "",
          processNote: block.note,
          progressByStepSeq,
          trailingProgressText: index === lastProcessIndex ? trailingProgressText : "",
        })}
        hasFinalOutput={Boolean(hasFinalOutput)}
        minimal={minimal}
        pendingActionKind={pendingActionKind}
        hitlRequests={hitlRequests}
        returnTo={returnTo}
        subAgentRuns={subAgentRuns}
        streaming={streaming}
        queueStatus={!hasFinalOutput && index === lastProcessIndex ? activeQueueStatus : undefined}
        enableFileCards={enableFileCards}
      />
    );
  };

  if (part === "process") {
    return (
      <>
        {blocks.map((block, index) => (
          block.type === "process" ? renderProcess(block, index) : null
        ))}
      </>
    );
  }

  return (
    <>
      {blocks.map((block, index) => {
        if (block.type === "text") {
          if (!block.text) return null;
          if (firstProcessIndex < 0 && block.phase === "opening" && streaming && !hasFinalOutput) {
            return (
              <div key={block.id || `${keyPrefix}-opening-${index}`} className={`assistant-process-commentary${streaming ? " assistant-process-commentary--live" : ""}`}>
                <ChatMarkdown content={formatUserFacingStructuredText(block.text)} streaming={streaming} returnTo={returnTo} enableFileCards={enableFileCards} />
              </div>
            );
          }
          if (block.phase !== "final") return null;
          if (shouldSuppressFinalText(block.text)) return null;
          return (
            <CollapsibleFinalMarkdown
              key={block.id || `${keyPrefix}-text-${index}`}
              content={isUser ? block.text : formatUserFacingStructuredText(block.text)}
              isUser={isUser}
              streaming={streaming && index === blocks.length - 1}
              returnTo={returnTo}
              enabled={collapseLongFinal}
              enableFileCards={enableFileCards}
            />
          );
        }
        if (block.type === "visualization") {
          return (
            <AssistantLedgerVisualization
              key={block.id || `${keyPrefix}-visualization-${index}`}
              block={block}
              onConfigure={onConfigureWorkspaceLedgers}
            />
          );
        }
        if (block.type === "surface") {
          return (
            <InteractiveResponseSurface
              key={block.id || `${keyPrefix}-surface-${index}`}
              surface={block}
              sourceMessageId={sourceMessageId}
              submissionReceipts={responseSurfaceSubmissionReceipts}
              submissionDisabled={streaming || !sourceMessageId}
              onSubmit={onResponseSurfaceSubmit}
            />
          );
        }
        if (part === "content") return null;
        return renderProcess(block, index);
      })}
      {recoveredFinalText && (
        <CollapsibleFinalMarkdown
          key={`${keyPrefix}-recovered-final`}
          content={isUser ? recoveredFinalText : formatUserFacingStructuredText(recoveredFinalText)}
          isUser={isUser}
          streaming={false}
          returnTo={returnTo}
          enabled={collapseLongFinal}
          enableFileCards={enableFileCards}
        />
      )}
      {liveFinalText && (
        <CollapsibleFinalMarkdown
          key={`${keyPrefix}-live-final`}
          content={isUser ? liveFinalText : formatUserFacingStructuredText(liveFinalText)}
          isUser={isUser}
          streaming={streaming}
          returnTo={returnTo}
          enabled={collapseLongFinal}
          enableFileCards={enableFileCards}
        />
      )}
      {liftedFinalText && !liveFinalText && (
        <CollapsibleFinalMarkdown
          key={`${keyPrefix}-lifted-final`}
          content={isUser ? liftedFinalText : formatUserFacingStructuredText(liftedFinalText)}
          isUser={isUser}
          streaming={streaming}
          returnTo={returnTo}
          enabled={collapseLongFinal}
          enableFileCards={enableFileCards}
        />
      )}
      {shouldUseAuthoritativeContent && !liveFinalText && !liftedFinalText && (
        <CollapsibleFinalMarkdown
          key={`${keyPrefix}-authoritative-final`}
          content={isUser ? authoritativeContent : formatUserFacingStructuredText(authoritativeContent)}
          isUser={isUser}
          streaming={streaming}
          returnTo={returnTo}
          enabled={collapseLongFinal}
          enableFileCards={enableFileCards}
        />
      )}
      {fallbackFinalText && (
        <CollapsibleFinalMarkdown
          key={`${keyPrefix}-fallback-final`}
          content={isUser ? fallbackFinalText : formatUserFacingStructuredText(fallbackFinalText)}
          isUser={isUser}
          streaming={false}
          returnTo={returnTo}
          enabled={collapseLongFinal}
          enableFileCards={enableFileCards}
        />
      )}
    </>
  );
}
