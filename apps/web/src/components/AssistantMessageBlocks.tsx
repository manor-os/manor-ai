import ChatMarkdown from "./ChatMarkdown";
import { useEffect, useMemo, useState } from "react";
import type { AssistantBlock, AssistantProcessStep, ToolCall } from "../lib/chatStream";
import { normalizeToolResult } from "../lib/chatStream";
import { shouldExpandAssistantProcessBlock } from "../lib/assistantProcessFlow";
import { t } from "../lib/i18n";
import { formatUserFacingStructuredText } from "../lib/taskDisplay";
import { processSurfaceSummary, runtimeToolBadge } from "../lib/toolRuntimeSurface";

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
    result: step.result_preview || step.summary || step.display_name,
    status,
    duration:
      typeof step.duration_ms === "number"
        ? `${(step.duration_ms / 1000).toFixed(2)}s`
        : undefined,
  };
}

function formatDuration(ms?: number) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return "";
  if (ms < 1000) return "";
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds <= 0) return "";
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (!minutes) return `${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

function normalizeStepStatus(status?: string) {
  if (status === "running" || status === "pending") return "running";
  if (status === "error") return "error";
  return "completed";
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
  const result = parsePreviewObject(step.result_preview);
  const genericTarget = stepTarget(step, args, display);
  if (name === "manor") {
    const action = String(args?.action || "").toLowerCase();
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
  if (name === "rag" || name === "search_documents" || name === "search_tasks") {
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
    "linkedin_browser",
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
  if (lowerServer.includes("notebooklm")) {
    if (lowerTool === "ask") {
      return t("component.assistant_process.tool.knowledge.ask", { target: target || serverText });
    }
    if (hasAny(lowerTool, ["create", "save"])) {
      return t("component.assistant_process.tool.knowledge.save", { target: target || serverText });
    }
    return t("component.assistant_process.tool.knowledge.search", { target: target || serverText });
  }
  if (lowerServer.includes("tavily") || lowerServer.includes("perplexity")) {
    if (lowerTool.includes("fetch") || lowerTool.includes("extract")) {
      return t("component.assistant_process.tool.web.fetch", { target: target || serverText });
    }
    return t("component.assistant_process.tool.web.search", { target: target || serverText });
  }
  if (
    lowerServer.includes("chatgpt_web") ||
    lowerServer.includes("gemini_web") ||
    lowerServer.includes("claude_ai_web") ||
    lowerServer.includes("openai_api")
  ) {
    if (lowerTool.includes("list") || lowerTool.includes("model")) {
      return t("component.assistant_process.tool.ai_web.list", { target: target || serverText });
    }
    return t("component.assistant_process.tool.ai_web.ask", { target: target || serverText });
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

function processMetaText(steps: AssistantProcessStep[]) {
  const completed = steps.filter((step) => normalizeStepStatus(step.status) === "completed").length;
  const running = steps.some((step) => normalizeStepStatus(step.status) === "running");
  const errored = steps.filter((step) => normalizeStepStatus(step.status) === "error").length;
  const parts: string[] = [];
  if (completed) parts.push(t("component.assistant_message_blocks.completed_actions", { count: completed }));
  if (running) parts.push(t("component.assistant_message_blocks.processing"));
  if (errored) parts.push(t("component.assistant_message_blocks.failed_actions", { count: errored }));
  return parts.join(" · ") || t("component.assistant_message_blocks.preparing");
}

function StepIcon({ status }: { status: string }) {
  if (status === "running") return <span className="assistant-process-spinner" />;
  if (status === "error") return <span className="assistant-process-step-icon assistant-process-step-icon--error" />;
  return <span className="assistant-process-step-icon" />;
}

function RuntimeSurfaceBadge({
  badge,
  compact,
}: {
  badge: { label: string; bg: string; color: string; border: string };
  compact?: boolean;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        flexShrink: 0,
        height: compact ? 16 : 18,
        borderRadius: 4,
        border: `1px solid ${badge.border}`,
        background: badge.bg,
        color: badge.color,
        fontSize: compact ? 9 : 10,
        fontWeight: 700,
        lineHeight: 1,
        padding: compact ? "0 4px" : "0 5px",
        whiteSpace: "nowrap",
      }}
    >
      {badge.label}
    </span>
  );
}

function AssistantProcessBlock({
  block,
  openingText,
  progressByStepSeq,
  minimal = false,
  hasFinalOutput = false,
  returnTo,
}: {
  block: Extract<AssistantBlock, { type: "process" }>;
  openingText: string;
  progressByStepSeq: Map<number, string>;
  minimal?: boolean;
  hasFinalOutput?: boolean;
  returnTo?: string;
}) {
  const steps = block.steps || [];
  const hasRunning = steps.some((step) => normalizeStepStatus(step.status) === "running");
  const hasError = steps.some((step) => normalizeStepStatus(step.status) === "error");
  const stepAssistantTexts = steps
    .map((step) => step.assistant_text?.trim())
    .filter(Boolean) as string[];
  const openingTextIsDuplicatedInSteps = Boolean(
    openingText.trim() && stepAssistantTexts.some((text) => text === openingText.trim()),
  );
  // Workspace chat (minimal) stays collapsed unless actively running — a failed
  // step must not auto-expand its traceback.
  const autoExpand = shouldExpandAssistantProcessBlock(block, minimal);
  const [expanded, setExpanded] = useState(autoExpand);
  const tools = useMemo(() => steps.map(processStepToToolCall), [steps]);
  const duration = formatDuration(block.duration_ms);
  const surfaceSummary = processSurfaceSummary(steps.map((step) => step.name));
  const title = hasRunning
    ? t("component.assistant_message_blocks.processing")
    : hasError
      ? hasFinalOutput
        ? t("component.assistant_message_blocks.process_recovered")
        : t("component.assistant_message_blocks.process_error")
      : t("component.assistant_message_blocks.processed");

  useEffect(() => {
    setExpanded(autoExpand);
  }, [autoExpand]);

  if (steps.length === 0 && !openingText && progressByStepSeq.size === 0) return null;

  return (
    <section className="assistant-process-flow">
      <button className="assistant-process-flow-header" type="button" onClick={() => setExpanded((value) => !value)}>
        <span>{title}</span>
        {duration && <span>{duration}</span>}
        <svg className={`assistant-process-flow-chevron${expanded ? " expanded" : ""}`} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {expanded && (
        <div className="assistant-process-flow-body">
          <div className="assistant-process-flow-summary">
            {[hasRunning ? t("component.assistant_message_blocks.working") : processMetaText(steps), surfaceSummary]
              .filter(Boolean)
              .join(" · ")}
          </div>
          {!minimal && block.note && (
            <div className="assistant-process-note">
              <ChatMarkdown content={formatUserFacingStructuredText(block.note)} streaming={hasRunning} returnTo={returnTo} />
            </div>
          )}
          {!minimal && openingText && !openingTextIsDuplicatedInSteps && (
            <div className="assistant-process-thinking">
              <ChatMarkdown content={formatUserFacingStructuredText(openingText)} streaming={hasRunning} returnTo={returnTo} />
            </div>
          )}
          {tools.map((tool, index) => {
            const step = steps[index];
            const status = normalizeStepStatus(step.status);
            const stepDuration = formatDuration(step.duration_ms);
            const badge = runtimeToolBadge(step.name);

            // Minimal (workspace chat): just the verb + status, never the
            // arguments / result / thinking — no AI "running code" surfaces.
            if (minimal) {
              return (
                <div className="assistant-process-step assistant-process-step--minimal" key={step.id || index}>
                  <StepIcon status={status} />
                  <RuntimeSurfaceBadge badge={badge} compact />
                  <span className="assistant-process-step-label">
                    {processVerb(step)}
                    {stepDuration && <span className="assistant-process-step-duration">{stepDuration}</span>}
                  </span>
                </div>
              );
            }

            const resultText = normalizeToolResult(tool.result);
            const argumentsText = step.arguments_preview ? formatUserFacingStructuredText(step.arguments_preview) : "";
            const displayResultText = resultText ? formatUserFacingStructuredText(resultText) : "";
            const progressText = progressByStepSeq.get(Number(step.seq || index + 1)) || "";
            return (
              <div className="assistant-process-step-group" key={step.id || index}>
                {step.assistant_text && (
                  <div className="assistant-process-thinking assistant-process-thinking--step">
                    <ChatMarkdown content={formatUserFacingStructuredText(step.assistant_text)} streaming={hasRunning && status === "running"} returnTo={returnTo} />
                  </div>
                )}
                <details
                  className="assistant-process-step"
                  title={t("component.assistant_message_blocks.view_tool_details")}
                >
                  <summary>
                    <StepIcon status={status} />
                    <RuntimeSurfaceBadge badge={badge} />
                    <span className="assistant-process-step-label">
                      {processVerb(step)}
                      {stepDuration && <span className="assistant-process-step-duration">{stepDuration}</span>}
                    </span>
                  </summary>
                  {(argumentsText || displayResultText) && (
                    <div className="assistant-process-step-detail">
                      {argumentsText && <pre>{argumentsText}</pre>}
                      {displayResultText && <pre>{displayResultText}</pre>}
                    </div>
                  )}
                </details>
                {progressText && (
                  <div className="assistant-process-thinking assistant-process-thinking--progress">
                    <ChatMarkdown content={formatUserFacingStructuredText(progressText)} streaming={hasRunning} returnTo={returnTo} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

export default function AssistantMessageBlocks({
  blocks,
  content,
  keyPrefix,
  isUser = false,
  streaming = false,
  minimal = false,
  returnTo,
}: {
  blocks?: AssistantBlock[] | null;
  content?: string | null;
  keyPrefix: string | number;
  isUser?: boolean;
  streaming?: boolean;
  returnTo?: string;
  /** Workspace chat: hide tool/step technical detail (args, results,
   *  thinking) — show only the agent's final text + a quiet step summary. */
  minimal?: boolean;
}) {
  if (!Array.isArray(blocks) || blocks.length === 0) return null;

  const firstProcessIndex = blocks.findIndex((block) => block.type === "process");
  const stepAssistantTexts = blocks
    .filter((block): block is Extract<AssistantBlock, { type: "process" }> => block.type === "process")
    .flatMap((block) => block.steps || [])
    .map((step) => step.assistant_text?.trim())
    .filter(Boolean) as string[];
  const textBlocks = blocks.filter(
    (block): block is Extract<AssistantBlock, { type: "text" }> => block.type === "text",
  );
  const isStepAssistantText = (text: string) => {
    const trimmed = text.trim();
    return Boolean(trimmed && stepAssistantTexts.some((stepText) => stepText === trimmed));
  };
  const processOpeningText = blocks
    .filter((block, index) => block.type === "text" && block.phase === "opening" && (firstProcessIndex < 0 || index < firstProcessIndex))
    .map((block) => ("text" in block ? block.text : ""))
    .filter(Boolean)
    .join("");
  const processProgressText = textBlocks
    .filter((block) => block.phase === "progress")
    .map((block) => block.text)
    .filter((text) => text && !isStepAssistantText(text))
    .join("\n\n");
  const progressByStepSeq = new Map<number, string>();
  textBlocks
    .filter((block) => block.phase === "progress")
    .forEach((block) => {
      const seq = typeof block.after_step_seq === "number" ? block.after_step_seq : 0;
      const text = block.text || "";
      if (!seq || !text || isStepAssistantText(text)) return;
      const existing = progressByStepSeq.get(seq);
      progressByStepSeq.set(seq, existing ? `${existing}\n\n${text}` : text);
    });
  const legacyPostProcessOpeningText = blocks
    .filter((block, index) => block.type === "text" && block.phase !== "opening" && block.phase !== "progress" && block.phase !== "final" && firstProcessIndex >= 0 && index > firstProcessIndex)
    .map((block) => ("text" in block ? block.text : ""))
    .filter((text) => text && !isStepAssistantText(text))
    .join("");
  const hasFinalText = textBlocks.some((block) => block.phase === "final" && Boolean(block.text));
  const finalText = textBlocks
    .filter((block) => block.phase === "final")
    .map((block) => block.text)
    .filter(Boolean)
    .join("");
  const authoritativeContent = (content || "").trim();
  const shouldUseAuthoritativeContent =
    Boolean(authoritativeContent) &&
    Boolean(finalText.trim()) &&
    authoritativeContent.length > finalText.trim().length &&
    authoritativeContent.endsWith(finalText.trim());
  const hasRunningProcess = blocks.some(
    (block) =>
      block.type === "process" &&
      (block.status === "running" ||
        block.status === "pending" ||
        (block.steps || []).some((step) => step.status === "running" || step.status === "pending")),
  );
  const recoveredFinalText = !hasRunningProcess && !shouldUseAuthoritativeContent ? legacyPostProcessOpeningText : "";
  const contentIsOnlyOpeningText = Boolean(
    processOpeningText.trim() &&
    authoritativeContent === processOpeningText.trim(),
  );
  const processText = `${processOpeningText}${processProgressText}`.trim();
  const contentIsOnlyProcessText = Boolean(
    processText &&
    (authoritativeContent === processText || processText.endsWith(authoritativeContent)),
  );
  const liveFinalText =
    streaming &&
    !hasFinalText &&
    !hasRunningProcess &&
    Boolean(authoritativeContent) &&
    !contentIsOnlyOpeningText &&
    !contentIsOnlyProcessText
      ? authoritativeContent
      : "";
  const fallbackFinalText =
    !hasFinalText && !hasRunningProcess && !recoveredFinalText && !liveFinalText && !authoritativeContent
      ? textBlocks
          .filter((block) => block.phase !== "opening" && block.phase !== "progress")
          .map((block) => block.text)
          .filter((text) => text && !isStepAssistantText(text))
          .filter(Boolean)
          .join("")
      : "";
  const openingText = fallbackFinalText ? "" : processOpeningText;
  const hasFinalOutput = Boolean(
    hasFinalText ||
      recoveredFinalText ||
      liveFinalText ||
      fallbackFinalText ||
      (shouldUseAuthoritativeContent && authoritativeContent),
  );
  const shouldSuppressFinalText = (text: string) => {
    const trimmed = text.trim();
    if (shouldUseAuthoritativeContent) return true;
    return Boolean(recoveredFinalText && trimmed && recoveredFinalText.includes(trimmed));
  };

  return (
    <>
      {blocks.map((block, index) => {
        if (block.type === "text") {
          if (!block.text) return null;
          if (block.phase !== "final") return null;
          if (shouldSuppressFinalText(block.text)) return null;
          return (
            <ChatMarkdown
              key={block.id || `${keyPrefix}-text-${index}`}
              content={isUser ? block.text : formatUserFacingStructuredText(block.text)}
              isUser={isUser}
              streaming={streaming && index === blocks.length - 1}
              returnTo={returnTo}
            />
          );
        }
        return (
          <AssistantProcessBlock
            key={block.id || `${keyPrefix}-process-${index}`}
            block={block}
            openingText={openingText}
            progressByStepSeq={progressByStepSeq}
            minimal={minimal}
            hasFinalOutput={hasFinalOutput}
            returnTo={returnTo}
          />
        );
      })}
      {recoveredFinalText && (
        <ChatMarkdown
          key={`${keyPrefix}-recovered-final`}
          content={isUser ? recoveredFinalText : formatUserFacingStructuredText(recoveredFinalText)}
          isUser={isUser}
          streaming={false}
          returnTo={returnTo}
        />
      )}
      {liveFinalText && (
        <ChatMarkdown
          key={`${keyPrefix}-live-final`}
          content={isUser ? liveFinalText : formatUserFacingStructuredText(liveFinalText)}
          isUser={isUser}
          streaming={streaming}
          returnTo={returnTo}
        />
      )}
      {shouldUseAuthoritativeContent && !liveFinalText && (
        <ChatMarkdown
          key={`${keyPrefix}-authoritative-final`}
          content={isUser ? authoritativeContent : formatUserFacingStructuredText(authoritativeContent)}
          isUser={isUser}
          streaming={streaming}
          returnTo={returnTo}
        />
      )}
      {fallbackFinalText && (
        <ChatMarkdown
          key={`${keyPrefix}-fallback-final`}
          content={isUser ? fallbackFinalText : formatUserFacingStructuredText(fallbackFinalText)}
          isUser={isUser}
          streaming={false}
          returnTo={returnTo}
        />
      )}
    </>
  );
}
