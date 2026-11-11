/**
 * ToolCallList — fallback process card when assistant_blocks are missing.
 *
 * Collapsed by default after a run ends. First expand shows grouped human
 * steps; raw results stay behind a second click on a single step.
 */
import { useEffect, useMemo, useState } from "react";
import {
  formatPublicToolResult,
  type SubAgentEvent,
  type ToolCall,
} from "../../lib/chatStream";
import { groupConsecutiveByKey } from "../../lib/assistantProcessFlow";
import { isWebsiteAccessDenied, websiteAccessDeniedSource } from "../../lib/websiteAccessDenied";
import { summarizeToolActivity } from "../../lib/toolActivitySummary";
import { formatProcessDuration, useLiveProcessClock } from "../../lib/assistantProcessPresentation";
import { resolveDisplayMediaUrl } from "../../lib/api";
import {
  AudioGenerationStatus,
  parseAudioGenerationResult,
} from "../../lib/audioGeneration";
import { t } from "../../lib/i18n";
import { matchSubAgentRuns } from "../../lib/subAgentDisplay";
import { formatUserFacingLabel, formatUserFacingStructuredText } from "../../lib/taskDisplay";
import { parseMcpToolName } from "../../lib/toolRuntimeSurface";
import AgentLoopStep from "./AgentLoopStep";
import MediaJobCard from "./MediaJobCard";

const AI_EDIT_TOOL_LABELS: Record<string, string> = {
  ai_edit_read_current_file: "read current file",
  ai_edit_generate_patch: "generate patch",
  ai_edit_apply_patch: "apply patch",
  ai_edit_verify_patch: "verify edit",
};

const CHROME_TOOL_LABELS: Record<string, string> = {
  mcp__chrome__status: "check Chrome status",
  mcp__chrome__open: "open Chrome page",
  mcp__chrome__navigate: "navigate Chrome page",
  mcp__chrome__read_page: "read Chrome page",
  mcp__chrome__get_interactive_elements: "read Chrome page",
  mcp__chrome__get_web_content: "extract Chrome content",
  mcp__chrome__get_content: "extract Chrome content",
  mcp__chrome__wait: "wait for Chrome page",
  mcp__chrome__inject_script: "run page script",
  mcp__chrome__send_cdp: "run Chrome command",
  mcp__chrome__computer: "operate Chrome page",
  mcp__chrome__click_element: "click Chrome element",
  mcp__chrome__fill_or_select: "fill Chrome field",
  mcp__chrome__hover: "hover Chrome element",
  mcp__chrome__scroll: "scroll Chrome page",
  mcp__chrome__scroll_wheel: "scroll Chrome page",
  mcp__chrome__press_key: "press Chrome key",
  mcp__chrome__type_text: "type in Chrome",
  mcp__chrome__upload: "upload in Chrome",
  mcp__chrome__screenshot: "capture Chrome screenshot",
};

function ProtectedImage({ src, alt }: { src: string; alt: string }) {
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let revoke = () => {};
    resolveDisplayMediaUrl(src)
      .then((resolved) => {
        revoke = resolved.revoke;
        if (!cancelled) setDisplayUrl(resolved.url);
      })
      .catch(() => {
        if (!cancelled) setDisplayUrl(null);
      });
    return () => {
      cancelled = true;
      revoke();
    };
  }, [src]);

  if (!displayUrl) return null;
  return (
    <img
      src={displayUrl}
      alt={alt}
      style={{ width: "100%", borderRadius: 6, maxWidth: 260 }}
    />
  );
}

interface ToolCallListProps {
  tools: ToolCall[];
  /** Unique prefix for expand/collapse keys (e.g. message index) */
  keyPrefix: string | number;
  /** Inline styles vs CSS classes — FloatingChat uses inline, others use classes */
  variant?: "inline" | "class";
  /** Whether to compact completed tools */
  compactCompleted?: boolean;
  /** Simple chat still shows the quiet process card; media stays visible when collapsed. */
  hideDetails?: boolean;
  /** Delegated Agent runs rendered in place of their workspace_agent step. */
  subAgentRuns?: SubAgentEvent[];
  /** Live chat turn: keep the quiet card on Processing even between tools. */
  streaming?: boolean;
  /** Workspace chat: a finished, clean process reads "Process · N steps". */
  stepsSummary?: boolean;
}

function parseToolResult(result?: unknown): Record<string, any> | null {
  const text = formatPublicToolResult(result);
  if (!text) return null;
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return recoverPartialToolResult(text);
  }
}

function parseToolArguments(args?: unknown): Record<string, any> | null {
  if (!args) return null;
  if (typeof args === "object" && !Array.isArray(args))
    return args as Record<string, any>;
  if (typeof args !== "string") return null;
  try {
    const parsed = JSON.parse(args);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : null;
  } catch {
    return null;
  }
}

function formatToolName(name?: string) {
  return formatUserFacingLabel(name || "tool");
}

// ULID format: 26 chars, Crockford base32 (no I, L, O, U).
const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

function displayToolName(tc: ToolCall) {
  if (tc.displayLabel) return tc.displayLabel;
  if (isWebsiteAccessDenied(tc)) {
    const source = websiteAccessDeniedSource(tc);
    return source
      ? t("component.assistant_message_blocks.website_access_denied_source", { source })
      : t("component.assistant_message_blocks.website_access_denied");
  }
  if (tc.name && AI_EDIT_TOOL_LABELS[tc.name]) {
    return AI_EDIT_TOOL_LABELS[tc.name];
  }
  if (tc.name && CHROME_TOOL_LABELS[tc.name]) {
    return CHROME_TOOL_LABELS[tc.name];
  }
  if (tc.name === "invoke_skill") {
    const args = parseToolArguments(tc.arguments ?? tc.args);
    const skill = typeof args?.skill === "string" ? args.skill : "";
    if (!skill) return "skill workflow";
    // If the agent (or a legacy forced-tool-call) passed a raw ULID, hide it
    // rather than showing the opaque id to the user. Real slugs / names go
    // through the normalization below.
    if (ULID_RE.test(skill)) return "skill workflow";
    return `${skill.replace(/_/g, "-")} skill`;
  }
  const parsedMcp = parseMcpToolName(tc.name);
  if (parsedMcp) {
    return `${formatToolName(parsedMcp.serverKey)}: ${formatToolName(parsedMcp.actionKey)}`;
  }
  return formatToolName(tc.name);
}

function recoverPartialToolResult(result: string): Record<string, any> | null {
  const recovered: Record<string, any> = {};
  for (const key of [
    "kind",
    "status",
    "job_id",
    "id",
    "image_url",
    "result_url",
    "video_url",
    "url",
    "prompt",
    "name",
    "model",
    "size",
    "resolution",
  ]) {
    const value = extractJsonStringField(result, key);
    if (value) recovered[key] = value;
  }
  for (const key of [
    "duration",
    "duration_seconds",
    "credits",
    "credits_estimate",
  ]) {
    const value = extractJsonNumberField(result, key);
    if (value != null) recovered[key] = value;
  }
  return Object.keys(recovered).length > 0 ? recovered : null;
}

function extractJsonStringField(source: string, key: string): string | null {
  const match = source.match(
    new RegExp(`"${key}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`),
  );
  if (!match) return null;
  try {
    return JSON.parse(`"${match[1]}"`);
  } catch {
    return match[1];
  }
}

function extractJsonNumberField(source: string, key: string): number | null {
  const match = source.match(
    new RegExp(`"${key}"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)`),
  );
  return match ? Number(match[1]) : null;
}

function isVideoJobResult(r: Record<string, any> | null) {
  if (!r) return false;
  const status = typeof r.status === "string" ? r.status : "";
  const hasJobId = Boolean(r.job_id || r.id);
  return Boolean(
    r.kind === "video" ||
    (hasJobId &&
      ["pending", "processing", "completed", "failed"].includes(status) &&
      (r.model || r.duration || r.resolution || r.video_url || r.result_url)),
  );
}

function normalizedToolName(name?: string) {
  return String(name || "").toLowerCase();
}


export default function ToolCallList(props: ToolCallListProps) {
  const {
    tools,
    keyPrefix,
    variant = "class",
    subAgentRuns = [],
    streaming = false,
  } = props;
  const [expandedResults, setExpandedResults] = useState<
    Record<string, boolean>
  >({});
  const displayTools = (() => {
    return tools;
  })();
  const subAgentRunByTool = useMemo(
    () => matchSubAgentRuns(displayTools, subAgentRuns),
    [displayTools, subAgentRuns],
  );

  const hasError = displayTools.some((tc) => tc.status === "error" && !isWebsiteAccessDenied(tc));
  const hasUnavailable = displayTools.some(isWebsiteAccessDenied);
  const hasRunning = displayTools.some(
    (tc) => (tc.status || (tc.result ? "success" : "pending")) === "pending",
  );
  const isLiveTurn = streaming;
  const hasUnsettledHistory = hasRunning && !isLiveTurn;
  const durationSeconds = displayTools.reduce((sum, tc) => {
    const text = String(tc.duration || "");
    const value = Number(text.replace(/s$/, ""));
    return Number.isFinite(value) ? sum + value : sum;
  }, 0);
  const elapsedMs = useLiveProcessClock({
    live: isLiveTurn,
    serverDurationMs: durationSeconds * 1000,
  });
  const [processExpanded, setProcessExpanded] = useState(false);
  const useInline = variant === "inline";

  if (displayTools.length === 0) return null;

  const toggleResult = (key: string) => {
    setExpandedResults((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const processStatus = isLiveTurn ? "pending" : hasError ? "error" : hasUnsettledHistory ? "unverified" : hasUnavailable ? "unavailable" : "success";
  const processTitle = isLiveTurn
    ? t("component.assistant_message_blocks.processing")
    : hasError
      ? t("component.assistant_message_blocks.process_error")
      : hasUnsettledHistory
        ? t("component.assistant_message_blocks.process_status_pending")
        : t("component.assistant_message_blocks.processed");
  const durationLabel = formatProcessDuration(elapsedMs);
  const activityLabel = summarizeToolActivity(displayTools.map((tool) => ({
    ...tool, status: isWebsiteAccessDenied(tool) ? "unavailable" : tool.status, label: displayToolName(tool),
  })));
  const processLabel = props.stepsSummary && processStatus === "success"
    ? `${t(
      displayTools.length === 1
        ? "component.assistant_message_blocks.process_steps_one"
        : "component.assistant_message_blocks.process_steps_other",
      { count: displayTools.length },
    )}${durationLabel ? ` · ${durationLabel}` : ""}`
    : `${activityLabel} · ${processTitle}${durationLabel ? ` · ${durationLabel}` : ""}`;
  const toolGroups = groupConsecutiveByKey(displayTools, (tc, index) => {
    if (subAgentRunByTool.get(index)) return null;
    if (tc.status === "error" || tc.status === "unverified") return null;
    return displayToolName(tc) || null;
  });

  const collapsedArtifacts = displayTools.map((tool, index) => {
    return <MediaPreview key={`${keyPrefix}-media-${index}`} tc={tool} />;
  });

  return (
    <div
      className={useInline ? undefined : "chat-tool-calls chat-process-block"}
      role={hasError ? "alert" : undefined}
      style={
        useInline
          ? {
              display: "flex",
              flexDirection: "column",
              gap: 2,
              marginBottom: 4,
              minWidth: 0,
              overflow: "hidden",
              border: "0.5px solid rgba(29, 29, 31,0.06)",
              borderRadius: 8,
              background: "var(--surface-panel)",
            }
          : undefined
      }
    >
      <button
        className={`assistant-tool-activity-toggle${useInline ? "" : " chat-process-header"}`}
        onClick={() => setProcessExpanded((value) => !value)}
        aria-expanded={processExpanded}
        type="button"
        style={
          useInline
            ? {
                display: "flex",
                alignItems: "center",
                gap: 7,
                width: "100%",
                border: "none",
                background: "transparent",
                color: isLiveTurn ? "#4f7d75" : hasError ? "#d65f59" : "#8e8e93",
                cursor: "pointer",
                fontSize: 11,
                fontWeight: 650,
                padding: "6px 8px",
                textAlign: "left",
              }
            : undefined
        }
      >
        <StatusIcon status={processStatus} />
        <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {processLabel}
        </span>
        <ChevronIcon expanded={processExpanded} />
      </button>

      {!processExpanded && collapsedArtifacts}

      {processExpanded && (
        <div
          className={useInline ? undefined : "chat-process-steps"}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 1,
            padding: useInline ? "0 4px 4px" : undefined,
          }}
        >
          {toolGroups.map((group) => {
            const j = group.startIndex;
            const tc = group.items[0];
            const key = `${keyPrefix}-${j}`;
            const grouped = group.items.length > 1;
            const subAgentRun = subAgentRunByTool.get(j);
            const resultText = grouped ? "" : formatPublicToolResult(tc.result);
            const displayResultText = resultText && !isWebsiteAccessDenied(tc) ? formatUserFacingStructuredText(resultText) : "";
            const status = isWebsiteAccessDenied(tc) ? "unavailable" : group.items.some((item) => (item.status || (item.result ? "success" : "pending")) === "pending")
              ? (isLiveTurn ? "pending" : "unverified")
              : (tc.status || (resultText ? "success" : "pending"));
            const canInspect = Boolean(displayResultText) && !grouped;
            const isExpanded = Boolean(canInspect && expandedResults[key]);
            const label = displayToolName(tc);
            const repeatLabel = grouped
              ? t("component.assistant_message_blocks.repeated_attempts", { count: group.items.length })
              : "";

            if (subAgentRun) {
              return <AgentLoopStep key={key} run={subAgentRun} live={isLiveTurn} />;
            }


            const media = group.items.map((item, itemIndex) => (
              <MediaPreview key={`${key}-media-${itemIndex}`} tc={item} />
            ));

            if (useInline) {
              return (
                <div
                  key={j}
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    background: "transparent",
                    borderRadius: 8,
                    overflow: "hidden",
                  }}
                >
                  <div
                    onClick={() => canInspect && toggleResult(key)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "5px 8px",
                      cursor: canInspect ? "pointer" : "default",
                      fontSize: 11,
                      fontWeight: 600,
                      color: "var(--text-muted)",
                    }}
                  >
                    <StatusIcon status={status} />
                    <span
                      style={{
                        flex: 1,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {label}
                      {repeatLabel && (
                        <span className="chat-tool-entry-repeat">{repeatLabel}</span>
                      )}
                    </span>
                    {!grouped && tc.duration && (
                      <span
                        style={{ fontSize: 10, color: "var(--text-muted)", fontWeight: 500 }}
                      >
                        {tc.duration}
                      </span>
                    )}
                    {canInspect && <ChevronIcon expanded={isExpanded} />}
                  </div>
                  {media}
                  {isExpanded && displayResultText && (
                    <div
                      style={{
                        padding: "4px 8px 6px",
                        borderTop: "0.5px solid rgba(0,0,0,0.05)",
                        maxHeight: 120,
                        overflowY: "auto",
                      }}
                    >
                      <pre
                        style={{
                          fontSize: 10,
                          color: "var(--text-muted)",
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-all",
                          margin: 0,
                          fontFamily: '"JetBrains Mono", monospace',
                        }}
                      >
                        {displayResultText}
                      </pre>
                    </div>
                  )}
                </div>
              );
            }

            return (
              <div
                key={j}
                className={`chat-tool-entry chat-tool-entry--${status}`}
              >
                <div
                  className="chat-tool-entry-header"
                  onClick={() => canInspect && toggleResult(key)}
                  style={{ cursor: canInspect ? "pointer" : "default" }}
                >
                  <StatusIcon status={status} />
                  <span className="chat-tool-entry-name">
                    {label}
                    {repeatLabel && (
                      <span className="chat-tool-entry-repeat">{repeatLabel}</span>
                    )}
                  </span>
                  {!grouped && tc.duration && (
                    <span className="chat-tool-entry-duration">{tc.duration}</span>
                  )}
                  {canInspect && (
                    <svg
                      className={`chat-tool-entry-chevron${isExpanded ? " expanded" : ""}`}
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path d="M9 5l7 7-7 7" />
                    </svg>
                  )}
                </div>
                {media}
                {isExpanded && displayResultText && (
                  <div className="chat-tool-entry-result">
                    <pre>{displayResultText}</pre>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ── Small helpers ── */

function StatusIcon({ status }: { status: string }) {
  if (status === "pending") return <span className="chat-tool-spinner" />;
  if (status === "unavailable") return <span className="assistant-process-step-icon assistant-process-step-icon--unavailable" aria-hidden="true" />;
  if (status === "unverified") return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="9" strokeDasharray="3 3" />
    </svg>
  );
  if (status === "error")
    return (
      <svg
        width="12"
        height="12"
        viewBox="0 0 24 24"
        fill="none"
        stroke="#d65f59"
        strokeWidth="2.5"
        strokeLinecap="round"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="15" y1="9" x2="9" y2="15" />
        <line x1="9" y1="9" x2="15" y2="15" />
      </svg>
    );
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="#4f9c84"
      strokeWidth="2.5"
      strokeLinecap="round"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function ChevronIcon({ expanded }: { expanded?: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="#aeaeb2"
      strokeWidth="2"
      style={{
        transform: expanded ? "rotate(90deg)" : "rotate(0deg)",
        transition: "transform 0.15s",
      }}
    >
      <polyline points="9 5 16 12 9 19" />
    </svg>
  );
}

function MediaPreview({ tc }: { tc: ToolCall }) {
  const resultText = formatPublicToolResult(tc.result);
  if (
    (tc.name === "generate_image" || tc.name === "generate_file") &&
    resultText
  ) {
    const r = parseToolResult(resultText);
    if (r?.image_url)
      return (
        <div style={{ display: "block", padding: "4px 8px 6px" }}>
          <ProtectedImage
            src={r.image_url}
            alt={r.prompt || t("component.tool_call_list.generated_image")}
          />
        </div>
      );
  }
  if (tc.name === "generate_file" && resultText) {
    const parsed = parseToolResult(resultText);
    const audioResult = parseAudioGenerationResult(parsed);
    const audioUrl =
      audioResult?.status === AudioGenerationStatus.Completed
        ? audioResult.audio_url || audioResult.result_url
        : parsed?.audio_url ||
          (parsed?.kind === "audio" ? parsed?.result_url : undefined);
    if (audioUrl) {
      return (
        <div style={{ display: "block", padding: "4px 8px 6px" }}>
          <audio controls src={audioUrl} style={{ width: "100%" }} />
        </div>
      );
    }
  }
  if (tc.name === "publish_site" && resultText) {
    const parsed = parseToolResult(resultText);
    if (!parsed?.published || !parsed?.url) return null;
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 8px 6px" }}>
        <a
          href={String(parsed.url)}
          target="_blank"
          rel="noreferrer"
          className="mono"
          style={{ fontSize: 12, textDecoration: "underline", overflowWrap: "anywhere" }}
        >
          {String(parsed.url)}
        </a>
      </div>
    );
  }
  if (
    (tc.name === "generate_video" ||
      tc.name === "generate_file" ||
      tc.name === "merge_videos") &&
    resultText
  ) {
    const parsed = parseToolResult(resultText);
    if (tc.name === "generate_file") {
      if (!isVideoJobResult(parsed)) return null;
    }
    return (
      <MediaJobCard resultJson={parsed ? JSON.stringify(parsed) : resultText} />
    );
  }
  return null;
}
