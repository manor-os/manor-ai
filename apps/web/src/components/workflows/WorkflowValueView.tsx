import { formatUserFacingLabel } from "../../lib/taskDisplay";

/**
 * Readable view of any structured Workflow value (review material, final
 * outputs): text as text, nested objects as labelled sections, short lists as
 * chips, statuses as badges, and links / media as "Open" links. The raw JSON
 * stays one click away (see WorkflowValueWithJson).
 */

type ValueRecord = Record<string, unknown>;

const MEDIA_KEYS: Array<[string, string]> = [
  ["video_url", "video"],
  ["image_url", "image"],
  ["audio_url", "audio"],
];
const LINK_KEYS = ["publication_url", "url", "link", "href"];
const STATUS_TONE: Record<string, string> = {
  published: "success",
  completed: "success",
  approved: "success",
  succeeded: "success",
  verified: "success",
  failed: "danger",
  blocked: "danger",
  error: "danger",
  rejected: "danger",
  not_selected: "muted",
  skipped: "muted",
  cancelled: "muted",
  pending: "muted",
};
const MAX_DEPTH = 4;
const CHIP_MAX_LENGTH = 40;

function isRecord(value: unknown): value is ValueRecord {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return !value.trim();
  if (Array.isArray(value)) return value.length === 0;
  if (isRecord(value)) return Object.keys(value).length === 0;
  return false;
}

function isLink(value: unknown): value is string {
  return typeof value === "string" && /^(https?:\/\/|\/api\/|\/viewer\/)/.test(value.trim());
}

function linkLabel(url: string): string {
  if (!/^https?:\/\//.test(url)) return "Open";
  try {
    return `Open on ${new URL(url).hostname.replace(/^www\./, "")}`;
  } catch {
    return "Open";
  }
}

function mediaLink(record: ValueRecord): { url: string; kind: string } | null {
  for (const [key, kind] of MEDIA_KEYS) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return { url: value.trim(), kind };
  }
  return null;
}

function primaryLink(record: ValueRecord): { key: string; url: string } | null {
  for (const key of LINK_KEYS) {
    const value = record[key];
    if (isLink(value)) return { key, url: value.trim() };
  }
  return null;
}

function Scalar({ value }: { value: unknown }) {
  if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
  if (typeof value === "number") return <span className="mono">{value}</span>;
  const text = String(value ?? "");
  if (isLink(text)) {
    return (
      <a className="workflow-value-link" href={text} target={text.startsWith("http") ? "_blank" : undefined} rel="noreferrer">
        {text}
      </a>
    );
  }
  return <span className="workflow-value-text">{text}</span>;
}

export function WorkflowStatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONE[status.trim().toLowerCase()] || "neutral";
  return <span className={`workflow-value-status is-${tone}`}>{formatUserFacingLabel(status)}</span>;
}

export default function WorkflowValueView({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (isEmpty(value)) return <span className="workflow-value-empty">—</span>;
  if (!Array.isArray(value) && !isRecord(value)) return <Scalar value={value} />;
  if (depth >= MAX_DEPTH) return <pre className="workflow-value-json">{JSON.stringify(value, null, 2)}</pre>;

  if (Array.isArray(value)) {
    const scalars = value.every((item) => !Array.isArray(item) && !isRecord(item));
    if (scalars && value.every((item) => String(item ?? "").length <= CHIP_MAX_LENGTH)) {
      return (
        <div className="workflow-value-chips">
          {value.map((item, index) => <span key={`${String(item)}-${index}`}>{String(item)}</span>)}
        </div>
      );
    }
    return (
      <ol className="workflow-value-list">
        {value.map((item, index) => (
          <li key={index}><WorkflowValueView value={item} depth={depth + 1} /></li>
        ))}
      </ol>
    );
  }

  // A media file (image/video/audio object): the preview lives elsewhere,
  // so show what it is and a link to open it.
  const media = mediaLink(value);
  if (media) {
    const caption = [value.name, value.alt_text].find((item) => typeof item === "string" && item.trim());
    return (
      <div className="workflow-value-media">
        <a className="workflow-value-link" href={media.url} target="_blank" rel="noreferrer">
          Open {media.kind}
        </a>
        {typeof caption === "string" && <span className="workflow-value-text">{caption}</span>}
      </div>
    );
  }

  const status = typeof value.status === "string" && value.status.trim() ? value.status : "";
  const link = primaryLink(value);
  const shown = Object.entries(value).filter(([key, item]) => (
    !isEmpty(item) && key !== "status" && key !== link?.key
  ));
  const ordered = [
    ...shown.filter(([, item]) => !Array.isArray(item) && !isRecord(item)),
    ...shown.filter(([, item]) => Array.isArray(item) || isRecord(item)),
  ];
  return (
    <div className="workflow-value-record">
      {(status || link) && (
        <div className="workflow-value-headline">
          {status && <WorkflowStatusBadge status={status} />}
          {link && (
            <a className="workflow-value-link is-primary" href={link.url} target="_blank" rel="noreferrer">
              {linkLabel(link.url)} ↗
            </a>
          )}
        </div>
      )}
      {ordered.length > 0 && (
        <dl className="workflow-value-fields">
          {ordered.map(([key, item]) => (
            <div key={key} className={Array.isArray(item) || isRecord(item) ? "is-nested" : undefined}>
              <dt>{formatUserFacingLabel(key)}</dt>
              <dd><WorkflowValueView value={item} depth={depth + 1} /></dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

export function WorkflowValueWithJson({ value }: { value: unknown }) {
  return (
    <div className="workflow-value">
      <WorkflowValueView value={value} />
      <details className="workflow-value-raw">
        <summary>View JSON</summary>
        <pre className="workflow-value-json">{JSON.stringify(value, null, 2)}</pre>
      </details>
    </div>
  );
}
