import { generatedFileLabel, generatedFileOpenReference, isOpenableFileReference } from "./fileReferences";

const EXECUTION_ONLY_LOG_TYPES = new Set(["ai_execution_started", "ai_agent_turn", "ai_supervisor_verdict"]);
const COORDINATOR_SUMMARY_LOG_TYPES = new Set(["ai_agent_turn", "ai_supervisor_verdict"]);
const COMMENT_EXECUTION_LOG_TYPES = new Set([
  "ai_hitl_requested", "ai_hitl_resumed", "ai_needs_replan", "ai_execution_completed", "ai_execution_failed",
]);

type CommentLog = { log_type?: string; meta?: Record<string, unknown> };

/** Only persisted, bounded coordinator summaries join the conversation.
 * Internal model rounds and supervisor traces remain in the execution log. */
export function taskLogVisibleInComments(log: CommentLog): boolean {
  return !EXECUTION_ONLY_LOG_TYPES.has(log.log_type || "")
    || (COORDINATOR_SUMMARY_LOG_TYPES.has(log.log_type || "") && log.meta?.coordinator_progress === true);
}

export function taskLogVisibleInExecution(log: CommentLog): boolean {
  return EXECUTION_ONLY_LOG_TYPES.has(log.log_type || "") || COMMENT_EXECUTION_LOG_TYPES.has(log.log_type || "");
}

export type CommentResultSection = {
  title: string;
  text: string;
  data?: unknown;
};

/** Only the executor's persisted summary marker opts into result presentation.
 * Never interpret a user's JSON/code example as a task result. */
export function taskCommentResult(log: { content?: unknown; meta?: { auto_summary?: unknown } }): {
  heading: string;
  sections: CommentResultSection[];
} | null {
  if (log.meta?.auto_summary !== true || typeof log.content !== "string") return null;
  const content = log.content.trim();
  const headings: { title: string; start: number; end: number }[] = [];
  let fence: string | undefined;
  let offset = 0;
  for (const line of content.split("\n")) {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = undefined;
    } else if (!fence && line.startsWith("### ")) {
      headings.push({ title: line.slice(4).trim(), start: offset, end: offset + line.length + 1 });
    }
    offset += line.length + 1;
  }
  const sections = headings.length ? headings.map((match, index) => ({
    title: match.title,
    text: content.slice(match.end, headings[index + 1]?.start ?? content.length)
      .replace(/\n\n---\s*$/, "").trim(),
  })) : [{ title: "", text: content.replace(/^## [^\n]+\n+/, "") }];
  let hasData = false;
  const parsed = sections.map((section): CommentResultSection => {
    const candidate = section.text.replace(/^```(?:json)?\s*\n([\s\S]*?)\n```$/i, "$1").trim();
    try {
      const data: unknown = JSON.parse(candidate);
      hasData = true;
      return { ...section, data };
    } catch {
      return section;
    }
  });
  // Ordinary Markdown summaries retain the existing renderer.
  return hasData ? { heading: content.match(/^## ([^\n]+)/)?.[1] || "", sections: parsed } : null;
}

const FILE_FIELDS = new Set(["files", "artifacts", "attachments", "documents"]);
const INTERNAL_FIELDS = new Set([
  "id", "document_id", "task_id", "plan_id", "step_id", "workspace_id", "entity_id",
  "agent_id", "worker_id", "run_id", "metadata", "meta", "params", "arguments", "raw",
]);

export function commentResultEntries(value: Record<string, unknown>): [string, unknown][] {
  return Object.entries(value).filter(([key, item]) =>
    !INTERNAL_FIELDS.has(key) && !key.startsWith("_") && item !== null && item !== undefined && item !== "",
  );
}

export function commentResultIsFiles(key: string): boolean {
  return FILE_FIELDS.has(key);
}

export function commentResultFile(value: unknown, knownFiles: unknown[], fallback: string): {
  reference: string;
  label: string;
} {
  const record = typeof value === "string" ? { url: value } : value;
  const file = record && typeof record === "object" ? record as Record<string, unknown> : {};
  const url = file.document_url || file.viewer_url || file.url;
  const candidate = generatedFileOpenReference(file)
    || (typeof url === "string" && isOpenableFileReference(url) ? url : "");
  const reference = isOpenableFileReference(candidate) ? candidate : "";
  // Enrich only an exact reference match using metadata already in the Task DTO.
  // No filename guessing or per-comment network lookups.
  const known = reference && knownFiles.find(item => generatedFileOpenReference(item) === reference);
  const label = generatedFileLabel(known || file, fallback);
  return { reference, label: /^(?:File )?01[A-Z0-9]{20,}$|^File [A-Z0-9]{6}$/i.test(label) ? fallback : label };
}
