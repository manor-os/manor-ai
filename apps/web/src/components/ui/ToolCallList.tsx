/**
 * ToolCallList — shared tool call rendering for all chat components.
 *
 * Shows the latest 3 tools by default; older tools are collapsed behind
 * a "Show N more" button. Each tool result can be expanded/collapsed.
 */
import { useEffect, useState } from "react";
import {
  normalizeToolResult,
  WRAPPER_TOOLS,
  type ToolCall,
} from "../../lib/chatStream";
import { resolveDisplayMediaUrl } from "../../lib/api";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingStructuredText } from "../../lib/taskDisplay";
import { parseMcpToolName, processSurfaceSummary, runtimeToolBadge } from "../../lib/toolRuntimeSurface";
import VideoCard from "./VideoCard";

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
  /** Workspace chat: collapse the whole list to one quiet line of friendly
   *  tool names — no arguments, results, or expanders. */
  minimal?: boolean;
}

function parseToolResult(result?: unknown): Record<string, any> | null {
  const text = normalizeToolResult(result);
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

function compactText(value: unknown, maxLength = 96) {
  const text = formatUserFacingStructuredText(value)
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 1))}…`;
}

function pathBasename(value: unknown) {
  const text = String(value || "").trim();
  if (!text) return "";
  const normalized = text.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).pop() || text;
}

function countInputList(value: unknown) {
  if (Array.isArray(value)) return value.length;
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.length;
    } catch {
      return value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean).length;
    }
  }
  return 0;
}

// ULID format: 26 chars, Crockford base32 (no I, L, O, U).
const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

function displayToolName(tc: ToolCall) {
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

function displayToolInput(tc: ToolCall) {
  const args = parseToolArguments(tc.arguments ?? tc.args);
  if (!args) return "";

  if (tc.name && AI_EDIT_TOOL_LABELS[tc.name]) {
    const result = parseToolResult(tc.result);
    const target = args.file ? pathBasename(args.file) : "";
    const detail =
      result?.error ||
      result?.changes ||
      result?.patch ||
      result?.stage ||
      args.stage ||
      args.request ||
      args.summary;
    return [target, detail ? compactText(detail, 88) : ""]
      .filter(Boolean)
      .join(" · ");
  }

  if (tc.name === "generate_file") {
    const kind = String(args.kind || "file");
    const params =
      args.params && typeof args.params === "object"
        ? (args.params as Record<string, any>)
        : {};
    const name =
      args.name ||
      args.path ||
      args.output_name ||
      args.filename ||
      params.name ||
      params.path ||
      params.output_name ||
      params.filename;
    const label = name ? `${kind}: ${pathBasename(name)}` : kind;
    return label;
  }

  if (tc.name === "manor") {
    const action = String(args.action || "action");
    const params =
      args.params && typeof args.params === "object"
        ? (args.params as Record<string, any>)
        : {};
    const target =
      params.name ||
      params.folder_name ||
      params.folder_path ||
      params.path ||
      params.query;
    return target ? `${action}: ${compactText(target, 80)}` : action;
  }

  if (tc.name === "wait_media_jobs") {
    const jobs = countInputList(args.job_ids);
    const interval = args.poll_interval_seconds;
    const timeout = args.timeout_seconds;
    const bits = [`${jobs || "media"} job${jobs === 1 ? "" : "s"}`];
    if (interval) bits.push(`every ${interval}s`);
    if (timeout) bits.push(`timeout ${timeout}s`);
    return bits.join(" · ");
  }

  if (tc.name === "merge_videos") {
    const output = args.output_name || args.name || args.path;
    const inputCount =
      countInputList(args.job_ids) ||
      countInputList(args.document_ids) ||
      countInputList(args.paths);
    const bits = [];
    if (inputCount) bits.push(`${inputCount} clips`);
    if (output) bits.push(pathBasename(output));
    return bits.join(" → ");
  }

  if (tc.name === "invoke_skill") {
    // displayToolName already renders the skill label (e.g. "paper-writing skill"),
    // so the input subtitle should only show the user input — not repeat the
    // skill name (which produced "<id> Skill - <id>" duplication when the
    // backend passed a raw ULID as the skill argument).
    const input = args.input ? compactText(args.input, 96) : "";
    return input;
  }

  const parsedMcp = parseMcpToolName(tc.name);
  if (parsedMcp) {
    const target =
      args.summary ||
      args.title ||
      args.name ||
      args.guest_email ||
      args.guest_name ||
      args.email ||
      args.query ||
      args.calendar_id ||
      args.booking_link_slug ||
      args.path;
    return target ? compactText(target, 96) : "";
  }

  const target =
    args.name ||
    args.path ||
    args.output_name ||
    args.filename ||
    args.query ||
    args.action;
  return target ? compactText(target, 96) : "";
}

function wrapperProgressText(tc: ToolCall, status: string) {
  if (status !== "pending" || !WRAPPER_TOOLS.has(tc.name)) return "";

  const activeChild = tc.activeChild || "";
  const lastChild = !activeChild ? tc.lastChild || "" : "";
  const completed = tc.completedChildCount || 0;
  const started = tc.childCount || 0;
  const parts: string[] = [];

  if (activeChild) {
    parts.push(`running ${formatToolName(activeChild)}`);
  } else if (lastChild) {
    parts.push(`last ${formatToolName(lastChild)}`);
  }
  if (started > 0) {
    parts.push(`${completed}/${started} internal steps`);
  }
  return parts.join(" · ");
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
  const { tools, keyPrefix, variant = "class" } = props;
  const shouldCompactCompleted = props.compactCompleted ?? true;
  const [expandedResults, setExpandedResults] = useState<
    Record<string, boolean>
  >({});
  const displayTools = (() => {
    return tools;
  })();

  const hasError = displayTools.some((tc) => tc.status === "error");
  const hasRunning = displayTools.some(
    (tc) => (tc.status || (tc.result ? "success" : "pending")) === "pending",
  );
  const durationSeconds = displayTools.reduce((sum, tc) => {
    const text = String(tc.duration || "");
    const value = Number(text.replace(/s$/, ""));
    return Number.isFinite(value) ? sum + value : sum;
  }, 0);
  const [processExpanded, setProcessExpanded] = useState(
    !shouldCompactCompleted || hasRunning || hasError,
  );
  const useInline = variant === "inline";

  useEffect(() => {
    setProcessExpanded(!shouldCompactCompleted || hasRunning || hasError);
  }, [hasRunning, hasError, shouldCompactCompleted]);

  if (displayTools.length === 0) return null;

  if (props.minimal) {
    const names = Array.from(
      new Set(displayTools.map((tc) => `${runtimeToolBadge(tc.name).compactLabel}: ${displayToolName(tc)}`)),
    ).filter(Boolean);
    const shown = names.slice(0, 4);
    const label =
      shown.join(", ") +
      (names.length > shown.length ? ` +${names.length - shown.length}` : "");
    return (
      <div className="ws-activity-line-row">
        <span className="ws-activity-line">
          <span className="ws-activity-line-glyph" aria-hidden>
            ▸
          </span>
          <span className="ws-activity-line-text">{label}</span>
        </span>
      </div>
    );
  }

  const toggleResult = (key: string) => {
    setExpandedResults((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const processStatus = hasRunning ? "pending" : hasError ? "error" : "success";
  const processTitle = hasRunning
    ? "Processing"
    : hasError
      ? "Process completed with issues"
      : "Process completed";
  const durationLabel = durationSeconds > 0 ? ` · ${durationSeconds.toFixed(1)}s` : "";
  const surfaceSummary = processSurfaceSummary(displayTools.map((tool) => tool.name));
  const surfaceLabel = surfaceSummary ? ` · ${surfaceSummary}` : "";
  const processLabel = `${processTitle} · ${displayTools.length} step${displayTools.length === 1 ? "" : "s"}${surfaceLabel}${durationLabel}`;

  return (
    <div
      className={useInline ? undefined : "chat-tool-calls chat-process-block"}
      style={
        useInline
          ? {
              display: "flex",
              flexDirection: "column",
              gap: 2,
              marginBottom: 4,
              minWidth: 0,
              overflow: "hidden",
              border: "1px solid rgba(28,25,23,0.06)",
              borderRadius: 8,
              background: "#fff",
            }
          : undefined
      }
    >
      <button
        className={useInline ? undefined : "chat-process-header"}
        onClick={() => setProcessExpanded((value) => !value)}
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
                color: hasRunning ? "#4f7d75" : hasError ? "#d65f59" : "#78716c",
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
          {displayTools.map((tc, j) => {
            const key = `${keyPrefix}-${j}`;
            const resultText = normalizeToolResult(tc.result);
            const displayResultText = resultText ? formatUserFacingStructuredText(resultText) : "";
            const status = tc.status || (resultText ? "success" : "pending");
            const isExpanded = expandedResults[key];
            const label = displayToolName(tc);
            const inputDetail = displayToolInput(tc);
            const progress = wrapperProgressText(tc, status);
            const badge = runtimeToolBadge(tc.name);


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
                    onClick={() => displayResultText && toggleResult(key)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "5px 8px",
                      cursor: displayResultText ? "pointer" : "default",
                      fontSize: 11,
                      fontWeight: 600,
                      color: "#57534e",
                    }}
                  >
                    <StatusIcon status={status} />
                    <RuntimeSurfaceBadge badge={badge} compact />
                    <span
                      style={{
                        flex: 1,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {label}
                      {progress && (
                        <span style={{ color: "#a8a29e", fontWeight: 500 }}>
                          {" · "}
                          {progress}
                        </span>
                      )}
                      {inputDetail && (
                        <span style={{ color: "#78716c", fontWeight: 500 }}>
                          {" · "}
                          {inputDetail}
                        </span>
                      )}
                    </span>
                    {tc.duration && (
                      <span
                        style={{ fontSize: 10, color: "#a8a29e", fontWeight: 500 }}
                      >
                        {tc.duration}
                      </span>
                    )}
                    {displayResultText && <ChevronIcon expanded={isExpanded} />}
                  </div>
                  <MediaPreview tc={tc} />
                  {isExpanded && displayResultText && (
                    <div
                      style={{
                        padding: "4px 8px 6px",
                        borderTop: "1px solid rgba(0,0,0,0.05)",
                        maxHeight: 120,
                        overflowY: "auto",
                      }}
                    >
                      <pre
                        style={{
                          fontSize: 10,
                          color: "#78716c",
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
                  onClick={() => displayResultText && toggleResult(key)}
                  style={{ cursor: displayResultText ? "pointer" : "default" }}
                >
                  <StatusIcon status={status} />
                  <RuntimeSurfaceBadge badge={badge} />
                  <span className="chat-tool-entry-name">
                    {label}
                    {progress && (
                      <span style={{ color: "#a8a29e", fontWeight: 500 }}>
                        {" · "}
                        {progress}
                      </span>
                    )}
                    {inputDetail && (
                      <span style={{ color: "#78716c", fontWeight: 500 }}>
                        {" · "}
                        {inputDetail}
                      </span>
                    )}
                  </span>
                  {tc.duration && (
                    <span className="chat-tool-entry-duration">{tc.duration}</span>
                  )}
                  {displayResultText && (
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
                <MediaPreview tc={tc} />
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

function StatusIcon({ status }: { status: string }) {
  if (status === "pending") return <span className="chat-tool-spinner" />;
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
      stroke="#a8a29e"
      strokeWidth="2"
      style={{
        transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
        transition: "transform 0.15s",
      }}
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

function MediaPreview({ tc }: { tc: ToolCall }) {
  const resultText = normalizeToolResult(tc.result);
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
    const audioUrl =
      parsed?.audio_url ||
      (parsed?.kind === "audio" ? parsed?.result_url : undefined);
    if (audioUrl) {
      return (
        <div style={{ display: "block", padding: "4px 8px 6px" }}>
          <audio controls src={audioUrl} style={{ width: "100%" }} />
        </div>
      );
    }
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
      <VideoCard resultJson={parsed ? JSON.stringify(parsed) : resultText} />
    );
  }
  return null;
}
