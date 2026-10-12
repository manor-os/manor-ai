import ChatMarkdown from "../ChatMarkdown";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel } from "../../lib/taskDisplay";
import {
  commentResultEntries, commentResultFile, commentResultIsFiles,
  type CommentResultSection,
} from "../../lib/taskCommentDisplay";

const LABEL_KEYS = new Set([
  "files", "report", "summary", "topic", "publisher", "url", "observable_pattern",
  "repurposing_reason", "verification_status", "recommendation", "next_steps",
]);

function fieldLabel(key: string): string {
  const normalized = key.toLowerCase().replace(/[\s-]+/g, "_");
  return LABEL_KEYS.has(normalized)
    ? t(`component.task_comment_result.${normalized}`)
    : formatUserFacingLabel(key);
}

function ResultValue({ value, files, returnTo, depth = 0 }: {
  value: unknown; files: unknown[]; returnTo?: string; depth?: number;
}) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "object") {
    const text = typeof value === "boolean"
      ? t(`component.task_comment_result.${value ? "yes" : "no"}`) : String(value);
    return <ChatMarkdown content={text} returnTo={returnTo} />;
  }
  // Deep machine structures remain available verbatim in the raw-data disclosure.
  if (depth > 6) return <p>{t("component.task_comment_result.deep_data")}</p>;
  if (Array.isArray(value)) {
    return <div className="space-y-2">{value.map((item, index) => {
      if (item && typeof item === "object" && !Array.isArray(item)) {
        const record = item as Record<string, unknown>;
        const title = [record.title, record.topic, record.name].find(v => typeof v === "string" && v.trim());
        return <details key={index} className="task-comment-result-item">
          <summary>{title ? `${index + 1}. ${title}` : t("component.task_comment_result.item", { number: index + 1 })}</summary>
          <div className="mt-3"><ResultValue value={item} files={files} returnTo={returnTo} depth={depth + 1} /></div>
        </details>;
      }
      return <div key={index} className="flex min-w-0 gap-2"><span aria-hidden="true">•</span><div className="min-w-0 flex-1"><ResultValue value={item} files={files} returnTo={returnTo} depth={depth + 1} /></div></div>;
    })}</div>;
  }
  return <dl className="space-y-3">{commentResultEntries(value as Record<string, unknown>).map(([key, item]) => (
    <div key={key}>
      <dt className="mb-1 font-medium">{fieldLabel(key)}{Array.isArray(item) ? ` · ${item.length}` : ""}</dt>
      <dd className="m-0 min-w-0">
        {commentResultIsFiles(key) && Array.isArray(item) ? <div className="flex flex-wrap gap-2">{item.map((file, index) => {
          const entry = commentResultFile(file, files, t("component.task_comment_result.file", { number: index + 1 }));
          return entry.reference ? <InlineFileReferenceCard key={index} {...entry} returnTo={returnTo} compact />
            : <span key={index}>{entry.label}</span>;
        })}</div> : <ResultValue value={item} files={files} returnTo={returnTo} depth={depth + 1} />}
      </dd>
    </div>
  ))}</dl>;
}

export default function TaskCommentResult({ result, raw, files = [], returnTo }: {
  result: { heading: string; sections: CommentResultSection[] };
  raw: string;
  files?: unknown[];
  returnTo?: string;
}) {
  return <div className="task-comment-result">
    {result.heading && <p className="mb-3 font-semibold">{result.heading === "Task Completed"
      ? t("status.completed") : result.heading === "Work so far (not accepted as finished)"
        ? t("component.task_comment_result.partial") : result.heading}</p>}
    {result.sections.map((section, index) => <section key={index} className="mb-4 space-y-3">
      {/* A single internal step name adds no context to its delivered result. */}
      {result.sections.length > 1 && section.title && <p className="font-medium">{section.title}</p>}
      {"data" in section
        ? <ResultValue value={section.data} files={files} returnTo={returnTo} />
        : <ChatMarkdown content={section.text} returnTo={returnTo} />}
    </section>)}
    <details className="task-comment-result-raw">
      <summary>{t("component.task_comment_result.raw_data")}</summary>
      <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs">{raw}</pre>
    </details>
  </div>;
}
