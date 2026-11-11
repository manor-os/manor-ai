import { useEffect, useRef, useState } from "react";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import MediaPreview from "./MediaPreview";
import ReviewDeliverables from "./ReviewDeliverables";
import { workflowFinalMediaRefs } from "../../lib/workflowMedia";
import { workflowPatchSummary, type WorkflowPatchSummary } from "./workflowRunDisplay";
import { parseFileReference } from "../../lib/fileReferences";
import Input from "../ui/Input";
import Textarea from "../ui/Textarea";

function previewText(value: unknown, full = false): string | null {
  if (value == null) return null;
  let text = "";
  if (typeof value === "string") {
    text = value.trim();
  } else {
    try {
      text = JSON.stringify(value, null, 2);
    } catch {
      text = String(value || "").trim();
    }
  }
  if (!text || text === "{}" || text === "[]") return null;
  return !full && text.length > 1400 ? `${text.slice(0, 1400)}\n...` : text;
}

function reviewHref(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  if (/^https?:\/\/\S+$/i.test(candidate) || /^\/(?!\/)\S+$/.test(candidate)) {
    return candidate;
  }
  return null;
}

/**
 * A Knowledge file named in a review value: a bare address, `[name](address)`,
 * or `address (name)`. The person sees the file's name and opens it in place.
 */
function reviewFileReference(text: string): { reference: string; label?: string } | null {
  const linked = text.match(/^\[([^\]]+)\]\((\S+)\)$/);
  const named = text.match(/^(\S+)\s+\(([^()]+)\)$/);
  const [reference, label] = linked ? [linked[2], linked[1]] : named ? [named[1], named[2]] : [text, undefined];
  return parseFileReference(reference) ? { reference, label: label?.trim() || undefined } : null;
}

function asRecord(value: unknown): Record<string, any> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : null;
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}

function hasReviewValue(value: unknown): boolean {
  if (value == null || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function isConstrainedPreviewTechnicalField(key: string): boolean {
  return ["review_artifacts", "artifacts", "files", "checklist", "summary", "previews", "pages", "review_title"].includes(key)
    || /(?:^|_)(?:document|file|artifact)_id$/i.test(key)
    || /(?:^|_)(?:sha(?:256|512)?|hash|fs_path)$/i.test(key);
}

/** Labelled fields, lists, links and Knowledge files, as a person reads them. */
export function StructuredReviewValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    const files = value.map((item) => typeof item === "string" ? reviewFileReference(item.trim()) : null);
    if (files.length && files.every(Boolean)) {
      return <div className="chat-workflow-review-artifacts">
        {files.map((file, index) => (
          <InlineFileReferenceCard key={index} reference={file!.reference} label={file!.label} compact />
        ))}
      </div>;
    }
    const compact = value.length >= 6 && value.every((item) => typeof item === "string" && item.length < 70);
    return <ul className={`chat-workflow-review-items${compact ? " is-compact" : ""}`}>
      {value.filter(hasReviewValue).map((item, index) => (
        <li key={index}><StructuredReviewValue value={item} /></li>
      ))}
    </ul>;
  }
  const record = asRecord(value);
  if (record) {
    return <dl className="chat-workflow-review-pairs">
      {Object.entries(record).filter(([, item]) => hasReviewValue(item)).map(([key, item]) => (
        <div key={key}>
          <dt>{formatUserFacingLabel(key)}</dt>
          <dd><StructuredReviewValue value={item} /></dd>
        </div>
      ))}
    </dl>;
  }
  if (typeof value === "boolean") {
    return <span>{t(value ? "component.workflow_review.yes" : "component.workflow_review.no")}</span>;
  }
  const text = String(value ?? "").trim();
  const file = reviewFileReference(text);
  if (file) return <InlineFileReferenceCard reference={file.reference} label={file.label} compact />;
  const href = reviewHref(text);
  return href ? (
    <a className="chat-workflow-review-link" href={href} target={href.startsWith("http") ? "_blank" : undefined} rel={href.startsWith("http") ? "noreferrer" : undefined}>{text}</a>
  ) : <span className="chat-workflow-review-value">{formatUserFacingText(text)}</span>;
}

export function workflowReviewIsEditable(review: unknown): review is Record<string, unknown> {
  return Boolean(review && typeof review === "object" && !Array.isArray(review));
}

export function workflowReviewIsRevisionChoice(choice: string): boolean {
  return ["revise", "request_revision", "request_changes", "changes_requested"].includes(
    choice.trim().toLowerCase().replace(/[-\s]+/g, "_"),
  );
}

function cloneReviewRecord(review: unknown): Record<string, unknown> {
  try {
    return JSON.parse(JSON.stringify(review)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function isLongReviewField(key: string, value: string): boolean {
  return value.length > 120 || /(text|content|markdown|body|description|digest|steps)$/i.test(key);
}

export function EditableWorkflowApprovalReview({
  prompt,
  reviewTitle,
  review,
  disabled,
  onDraftState,
  editableFields,
  revisionField,
}: WorkflowApprovalReviewProps & {
  disabled?: boolean;
  onDraftState: (draft: Record<string, unknown>, valid: boolean) => void;
  editableFields?: string[];
  revisionField?: string;
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>(
    () => cloneReviewRecord(review),
  );
  const [rawComplex, setRawComplex] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      Object.entries(cloneReviewRecord(review))
        .filter(([, value]) => value != null && typeof value === "object")
        .map(([key, value]) => [key, JSON.stringify(value, null, 2)]),
    ),
  );
  const [rawStringLists, setRawStringLists] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      Object.entries(cloneReviewRecord(review))
        .filter(([, value]) => Array.isArray(value) && value.every((item) => typeof item === "string"))
        .map(([key, value]) => [key, (value as string[]).join(", ")]),
    ),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const editDetailsRef = useRef<HTMLDetailsElement>(null);
  const hasErrors = Object.values(errors).some(Boolean);
  const changed = JSON.stringify(draft) !== JSON.stringify(review);

  const updateValue = (key: string, value: unknown) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };
  const updateComplexValue = (key: string, source: string, original: unknown) => {
    setRawComplex((current) => ({ ...current, [key]: source }));
    try {
      const parsed = JSON.parse(source);
      const sameContainer = Array.isArray(original)
        ? Array.isArray(parsed)
        : parsed != null && typeof parsed === "object" && !Array.isArray(parsed);
      if (!sameContainer) throw new Error("Keep the same JSON shape");
      updateValue(key, parsed);
      setErrors((current) => ({ ...current, [key]: "" }));
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [key]: error instanceof Error ? error.message : "Invalid JSON",
      }));
    }
  };

  const constrainedFields = Array.isArray(editableFields) ? editableFields : null;
  const revisionRequested = Boolean(revisionField)
    && Boolean(String(draft[revisionField!] ?? "").trim());

  useEffect(() => {
    onDraftState(draft, !hasErrors);
  }, [draft, hasErrors, onDraftState]);

  useEffect(() => {
    if (hasErrors && editDetailsRef.current) editDetailsRef.current.open = true;
  }, [hasErrors]);

  if (constrainedFields) {
    const preview = Object.fromEntries(
      Object.entries(draft).filter(([key]) => (
        !constrainedFields.includes(key)
        && key !== revisionField
        && (["review_artifacts", "artifacts", "files", "previews", "pages"].includes(key)
          || !isConstrainedPreviewTechnicalField(key))
      )),
    );
    return (
      <div className="approval-review-editor">
        <WorkflowApprovalReview review={preview} reviewTitle={reviewTitle} prompt={prompt} />
        <div className="approval-review-editor__fields">
          {constrainedFields.filter((key) => key !== revisionField).map((key) => {
            const value = draft[key];
            const label = formatUserFacingLabel(key);
            if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
              return <Input
                key={key}
                className="approval-review-editor__field"
                label={label}
                value={rawStringLists[key] ?? value.join(", ")}
                onChange={(event) => {
                  const source = event.target.value;
                  setRawStringLists((current) => ({ ...current, [key]: source }));
                  updateValue(key, source.split(",").map((item) => item.trim()).filter(Boolean));
                }}
                disabled={disabled}
              />;
            }
            if (typeof value === "number") {
              return <Input
                key={key}
                className="approval-review-editor__field"
                label={label}
                type="number"
                value={String(value)}
                onChange={(event) => updateValue(key, Number(event.target.value))}
                disabled={disabled}
              />;
            }
            if (typeof value === "boolean") {
              return <label className="approval-review-editor__field" key={key}>
                <input
                  type="checkbox"
                  checked={value}
                  onChange={(event) => updateValue(key, event.target.checked)}
                  disabled={disabled}
                />
                {label}
              </label>;
            }
            if (typeof value !== "string") return null;
            const multiline = /(description|content|text|body)$/i.test(key);
            return multiline ? <Textarea
              key={key}
              className="approval-review-editor__field approval-review-editor__field--wide"
              label={label}
              ariaLabel={label}
              value={value}
              onChange={(event) => updateValue(key, event.target.value)}
              rows={4}
              disabled={disabled}
            /> : <Input
              key={key}
              className="approval-review-editor__field"
              label={label}
              value={value}
              onChange={(event) => updateValue(key, event.target.value)}
              disabled={disabled}
            />;
          })}
          {revisionField && <Textarea
            className="approval-review-editor__field approval-review-editor__field--wide"
            label={formatUserFacingLabel(revisionField)}
            ariaLabel={formatUserFacingLabel(revisionField)}
            value={String(draft[revisionField] ?? "")}
            onChange={(event) => updateValue(revisionField, event.target.value)}
            rows={3}
            disabled={disabled}
          />}
          {revisionRequested && <p className="approval-review-editor__notice" role="status">
            Submit the video change request or clear it before approving.
          </p>}
        </div>
      </div>
    );
  }

  return (
    <div className="approval-review-editor">
      <WorkflowApprovalReview review={draft} reviewTitle={reviewTitle} prompt={prompt} />
      <details ref={editDetailsRef} className="approval-review-editor__edit">
        <summary>{t("component.workflow_review.suggest_changes")}{changed && <strong>{t("component.approval_action_bar.edited")}</strong>}</summary>
        <div className="approval-review-editor__notice">{t("component.approval_action_bar.edit_then_approve")}</div>
        <div className="approval-review-editor__fields">
          {Object.entries(draft).map(([key, value]) => {
          if (["review_artifacts", "artifacts", "files"].includes(key)) return null;
          const label = formatUserFacingLabel(key);
          if (value != null && typeof value === "object") {
            return (
              <Textarea
                key={key}
                className="approval-review-editor__field approval-review-editor__field--wide"
                label={label}
                value={rawComplex[key] ?? JSON.stringify(value, null, 2)}
                onChange={(event) => updateComplexValue(key, event.target.value, value)}
                rows={Math.min(8, Math.max(3, String(rawComplex[key] || "").split("\n").length))}
                error={errors[key]}
                disabled={disabled}
              />
            );
          }
          const textValue = String(value ?? "");
          if (isLongReviewField(key, textValue)) {
            return (
              <Textarea
                key={key}
                className="approval-review-editor__field approval-review-editor__field--wide"
                label={label}
                value={textValue}
                onChange={(event) => updateValue(key, event.target.value)}
                rows={Math.min(10, Math.max(3, textValue.split("\n").length + 1))}
                disabled={disabled}
              />
            );
          }
          const isUrl = /(^|_)url$/i.test(key) || /^https?:\/\/\S+$/i.test(textValue);
          return (
            <div className="approval-review-editor__field" key={key}>
              <Input
                label={label}
                value={textValue}
                type={isUrl ? "url" : "text"}
                onChange={(event) => updateValue(key, event.target.value)}
                disabled={disabled}
              />
              {isUrl && /^https?:\/\/\S+$/i.test(textValue) && (
                <a
                  className="approval-review-editor__source-link"
                  href={textValue}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t("component.approval_action_bar.open_current_link")}
                </a>
              )}
            </div>
          );
          })}
        </div>
      </details>
    </div>
  );
}

function reviewChecklist(value: unknown): string[] {
  const values = Array.isArray(value) ? value : value == null ? [] : [value];
  return values.flatMap((candidate) => {
    if (typeof candidate === "string") {
      const text = candidate.trim();
      return text ? [text] : [];
    }
    const item = asRecord(candidate);
    if (!item) return [];
    const text = String(
      item.text
        || item.label
        || item.title
        || item.criterion
        || item.description
        || item.name
        || "",
    ).trim();
    if (text) return [text];
    return Object.entries(item).flatMap(([key, itemValue]) => {
      if (itemValue === false || itemValue == null || itemValue === "") return [];
      if (itemValue === true) return [formatUserFacingLabel(key)];
      const detail = previewText(itemValue, true);
      return detail ? [`${formatUserFacingLabel(key)}: ${detail}`] : [];
    });
  });
}

function ReviewChecklist({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  const label = formatUserFacingLabel("checklist");
  return (
    <section className="chat-workflow-review-section">
      <span className="chat-workflow-review-label">{label}</span>
      <ul className="chat-workflow-review-checklist" aria-label={label}>
        {items.map((item, index) => (
          <li key={`${item}:${index}`}>{formatUserFacingText(item)}</li>
        ))}
      </ul>
    </section>
  );
}

export type ReviewArtifactKind = "image" | "video" | "audio" | "presentation" | "document" | "text" | null;

export interface ReviewArtifactReference {
  key: string;
  label?: string;
  reference: string;
  documentId?: string;
  kind: ReviewArtifactKind;
  /** A picture addressed directly (e.g. a signed screenshot link), shown as is. */
  imageSrc?: string;
  /** What this file shows, written by the step that asks for review (e.g. "Shot 03 · 7.7–11.5 s"). */
  caption?: string;
  /** What happens in it, read beside the picture (e.g. a storyboard shot's scene and action). */
  description?: string;
}

const KIND_BY_EXTENSION: Array<[RegExp, ReviewArtifactKind]> = [
  [/\.(png|jpe?g|webp|gif|avif)$/i, "image"],
  [/\.(mp4|mov|webm|m4v)$/i, "video"],
  [/\.(mp3|wav|m4a|aac|ogg|flac)$/i, "audio"],
  [/\.(pptx?|key)$/i, "presentation"],
  [/\.(pdf|docx?)$/i, "document"],
  [/\.(md|markdown|txt)$/i, "text"],
];

export function artifactKind(declared: unknown, name: string): ReviewArtifactKind {
  const fromName = KIND_BY_EXTENSION.find(([pattern]) => pattern.test(name))?.[1] ?? null;
  // A deck's verify step reports it as a document; its pages still preview as slides.
  if (fromName) return fromName;
  return ["image", "video", "audio"].includes(String(declared)) ? declared as ReviewArtifactKind : null;
}

/**
 * The address an image artifact is displayed from when it is a plain link
 * (a signed snapshot, a hosted picture) rather than a Manor file, which keeps
 * opening through its file card.
 */
function directImageSrc(kind: ReviewArtifactKind, reference: string, documentId?: string): string | undefined {
  if (kind !== "image" || documentId) return undefined;
  if (!/^https?:\/\/\S+$/i.test(reference) && !/^\/api\/\S+$/.test(reference)) return undefined;
  return parseFileReference(reference) ? undefined : reference;
}

function reviewArtifact(value: unknown, index: number): ReviewArtifactReference | null {
  if (typeof value === "string" && value.trim()) {
    const reference = value.trim();
    const kind = artifactKind(null, reference);
    return { key: `${reference}:${index}`, reference, kind, imageSrc: directImageSrc(kind, reference) };
  }
  const item = asRecord(value);
  if (!item) return null;
  const document = asRecord(item.document);
  const reference = String(
    item.document_id
      || (item.kind === "document" ? item.id : "")
      || document?.id
      || item.fs_path
      || item.saved_to
      || item.path
      || item.file_url
      || item.document_url
      || item.download_url
      || item.url
      || document?.fs_path
      || "",
  ).trim();
  if (!reference) return null;
  const label = String(
    item.name
      || item.filename
      || item.original_name
      || item.title
      || document?.name
      || "",
  ).trim() || undefined;
  const documentId = String(item.document_id || document?.id || "").trim() || undefined;
  const kind = artifactKind(item.kind, String(label || item.fs_path || item.path || reference));
  const caption = typeof item.caption === "string" && item.caption.trim() ? item.caption.trim() : undefined;
  const description = typeof item.description === "string" && item.description.trim() ? item.description.trim() : undefined;
  return {
    key: `${reference}:${index}`, reference, label, documentId, kind,
    imageSrc: directImageSrc(kind, reference, documentId),
    ...(caption ? { caption } : {}),
    ...(description ? { description } : {}),
  };
}

export function workflowReviewArtifacts(value: unknown): ReviewArtifactReference[] {
  const review = asRecord(value);
  if (!review) return [];
  const outputs = asRecord(review.outputs);
  const candidates = [
    review.review_artifacts,
    review.artifacts,
    review.files,
    outputs?.artifacts,
    outputs?.files,
  ];
  const seen = new Set<string>();
  return candidates.flatMap((candidate) => (
    Array.isArray(candidate) ? candidate : candidate == null ? [] : [candidate]
  )).flatMap((candidate, index) => {
    const artifact = reviewArtifact(candidate, index);
    if (!artifact || seen.has(artifact.reference)) return [];
    seen.add(artifact.reference);
    return [artifact];
  });
}

/** A review of described pictures (storyboard frames, slides with notes) needs room to show them side by side. */
export function workflowReviewHasFrames(review: unknown): boolean {
  return workflowReviewArtifacts(review).some((artifact) => (
    Boolean(artifact.description) && Boolean(artifact.imageSrc || (artifact.documentId && artifact.kind === "image"))
  ));
}

/** A dialog showing such a review widens to fit; any other review keeps its usual size. */
export const WORKFLOW_REVIEW_FRAMES_DIALOG_WIDTH = "min(1080px, calc(100vw - 32px))";

interface PermissionRequest {
  action: string;
  paths: string[];
  edits: Array<{ before: string; after: string }>;
  written: string;
  /** Structured edits (a JSON key, a cell, a row, a slide…): what, where, with which value. */
  patches: WorkflowPatchSummary[];
}

function parsedPreview(value: unknown): Record<string, any> | null {
  const record = asRecord(value);
  if (!record) return null;
  if (typeof record.preview !== "string") return record;
  try {
    return asRecord(JSON.parse(record.preview)) ?? record;
  } catch {
    return record;
  }
}

/** A step asking to act on files: what it will do, to which file, and the change itself. */
function permissionRequest(review: Record<string, any> | null): PermissionRequest | null {
  if (!review || typeof review.tool !== "string" || !(Array.isArray(review.paths) || review.args_preview)) return null;
  const preview = parsedPreview(review.args_preview);
  const paths = stringList(review.paths).length ? stringList(review.paths) : stringList(preview?.path || preview?.name);
  const operations = Array.isArray(preview?.operations) ? preview.operations.map(asRecord) : [];
  const edits = operations.flatMap((operation) => (
    operation && (typeof operation.old_text === "string" || typeof operation.new_text === "string")
      ? [{ before: String(operation.old_text || ""), after: String(operation.new_text || "") }]
      : []
  ));
  const written = typeof preview?.content === "string" ? preview.content : "";
  const patches = operations.flatMap((operation) => {
    const summary = operation ? workflowPatchSummary(operation) : null;
    return summary ? [summary] : [];
  });
  return { action: String(review.action || review.tool), paths, edits, written, patches };
}

/** "Modify CANVAS.md" from the action key workspace.file.modify and its file. */
function permissionTitle(request: PermissionRequest): string {
  const names = request.paths.slice(0, 2).map((path) => path.split("/").pop() || path).join(", ");
  const verb = /^[a-z_]+(\.[a-z_]+)+$/.test(request.action)
    ? request.action.split(".").pop()!.replace(/_/g, " ")
    : request.action;
  const label = `${verb.charAt(0).toUpperCase()}${verb.slice(1)}`;
  return formatUserFacingText(names ? `${label} ${names}` : label);
}

function PermissionChanges({ edits, written, patches }: Pick<PermissionRequest, "edits" | "written" | "patches">) {
  if (edits.length > 0 || patches.length > 0) {
    return <ol className="workflow-review-edits" aria-label={t("component.workflow_review.changes")}>
      {edits.map((edit, index) => <li key={`edit-${index}`}>
        {edit.before && <del>{edit.before}</del>}
        {edit.after && <ins>{edit.after}</ins>}
      </li>)}
      {patches.map((patch, index) => <li key={`patch-${index}`} className="workflow-review-patch">
        <span className="workflow-review-patch-head">
          <strong>{patch.verbKey ? t(patch.verbKey) : formatUserFacingLabel(patch.verb || "")}</strong>
          {patch.target && <code>{patch.target}</code>}
        </span>
        {patch.value !== undefined && <StructuredReviewValue value={patch.value} />}
      </li>)}
    </ol>;
  }
  return written
    ? <pre className="workflow-review-write" aria-label={t("component.workflow_review.content_to_write")}>{written}</pre>
    : null;
}

/** The change a tool approval would make, read from its arguments preview. */
export function ApprovalChangePreview({ argsPreview }: { argsPreview: unknown }) {
  const request = permissionRequest({ tool: "tool", args_preview: argsPreview });
  return request ? <PermissionChanges edits={request.edits} written={request.written} patches={request.patches} /> : null;
}

function PermissionRequestReview({ request }: { request: PermissionRequest }) {
  return (
    <div className="chat-hitl-summary chat-workflow-review">
      <div className="chat-hitl-title">{permissionTitle(request)}</div>
      {request.paths.length > 0 && <div className="chat-workflow-review-artifacts">
        {request.paths.map((path) => (
          <InlineFileReferenceCard key={path} reference={path} label={path.split("/").pop() || path} compact />
        ))}
      </div>}
      <PermissionChanges edits={request.edits} written={request.written} patches={request.patches} />
    </div>
  );
}

export interface WorkflowApprovalReviewProps {
  prompt?: string;
  reviewTitle?: string;
  review: unknown;
  full?: boolean;
  /** The prompt already reads above the card (a chat message's text), so it is not said twice. */
  promptShown?: boolean;
  /** Decided: what was reviewed stays, the instructions for deciding go. */
  resolved?: boolean;
}

export default function WorkflowApprovalReview({
  prompt,
  reviewTitle,
  review,
  promptShown = false,
  resolved = false,
}: WorkflowApprovalReviewProps) {
  const plan = asRecord(review);
  const scenes = Array.isArray(plan?.scenes)
    ? plan.scenes.map(asRecord).filter((scene): scene is Record<string, any> => Boolean(scene))
    : [];
  const outputProfile = asRecord(plan?.output_profile);
  const durationRange = asRecord(outputProfile?.target_duration_seconds);
  const artifacts = workflowReviewArtifacts(plan);
  const checklist = reviewChecklist(plan?.checklist);
  const sideEffects = stringList(plan?.listed_side_effects);
  const productPromise = String(plan?.product_promise || "").trim();
  const narration = String(plan?.canonical_narration || "").trim();
  const estimate = Number(plan?.estimated_duration_seconds || 0);
  const hasProductVideoPlan = Boolean(productPromise || narration || scenes.length || outputProfile);

  const permission = permissionRequest(plan);
  if (permission) return <PermissionRequestReview request={permission} />;

  if (!hasProductVideoPlan) {
    const pages = workflowReviewArtifacts({ artifacts: plan?.previews ?? plan?.pages });
    const summary = typeof plan?.summary === "string" ? plan.summary.trim() : "";
    const entries = plan
      ? Object.entries(plan).filter(([key, value]) => (
        !["review_artifacts", "artifacts", "files", "checklist", "summary", "previews", "pages"].includes(key)
        && hasReviewValue(value)
      ))
      : [];
    const facts = entries.filter(([, value]) => !Array.isArray(value) && !asRecord(value));
    const sections = entries.filter(([, value]) => Array.isArray(value) || Boolean(asRecord(value)));
    // A deliverable with its own player needs no second player from its link.
    const playable = artifacts.some((item) => item.documentId && (item.kind === "video" || item.kind === "audio"));
    const media = playable ? [] : workflowFinalMediaRefs(review);
    const guidance = prompt && !promptShown && !resolved && prompt.trim() !== summary ? prompt : "";
    const frames = workflowReviewHasFrames(plan);
    // Something to listen to or look at speaks for itself: its description folds away, and goes
    // once decided. A review with nothing to show keeps it as its text.
    const showsWork = artifacts.length > 0 || pages.length > 0 || media.length > 0;
    const foldedSummary = showsWork && !resolved ? summary : "";
    const checks = resolved ? [] : checklist;
    return (
      <div className={`chat-hitl-summary chat-workflow-review chat-workflow-review--compact${frames ? " chat-workflow-review--frames" : ""}`}>
        <div className="chat-hitl-title">
          {reviewTitle ? formatUserFacingText(reviewTitle) : t("component.chat_action_card.workflow_review_title")}
        </div>
        {summary && !showsWork && <div className="workflow-review-summary">{formatUserFacingText(summary)}</div>}
        {media.length > 0 && (
          <section className="chat-workflow-review-section chat-workflow-review-media">
            {media.map((item, index) => <MediaPreview key={`${item.url}-${index}`} refItem={item} maxHeight={320} />)}
          </section>
        )}
        <ReviewDeliverables artifacts={artifacts} pages={pages} compact />
        {(foldedSummary || guidance || checks.length > 0) && (
          <details className="workflow-review-guidance">
            <summary>{t(guidance || checks.length > 0 ? "component.workflow_review.what_to_check" : "component.workflow_review.details")}</summary>
            {foldedSummary && <p>{formatUserFacingText(foldedSummary)}</p>}
            {guidance && <p>{formatUserFacingText(guidance)}</p>}
            <ReviewChecklist items={checks} />
          </details>
        )}
        {(facts.length > 0 || sections.length > 0 || !plan) && (
          <div className="chat-workflow-review-scroll chat-workflow-review-scroll--structured">
            {facts.length > 0 && <div className="chat-workflow-review-facts">
              {facts.map(([key, value]) => <div key={key} className={String(value).length > 100 ? "is-wide" : undefined}>
                <span className="chat-workflow-review-label">{formatUserFacingLabel(key)}</span>
                <StructuredReviewValue value={value} />
              </div>)}
            </div>}
            {sections.map(([key, value]) => <section className="chat-workflow-review-section" key={key}>
              <span className="chat-workflow-review-label">{formatUserFacingLabel(key)}</span>
              <StructuredReviewValue value={value} />
            </section>)}
            {!plan && <StructuredReviewValue value={review} />}
          </div>
        )}
      </div>
    );
  }

  const outputBits = [
    outputProfile?.aspect_ratio,
    outputProfile?.width && outputProfile?.height
      ? `${outputProfile.width} x ${outputProfile.height}`
      : "",
    durationRange?.min && durationRange?.max
      ? `${durationRange.min}-${durationRange.max}s`
      : estimate > 0 ? `${estimate}s` : "",
    outputProfile?.language,
  ].map((value) => String(value || "").trim()).filter(Boolean);

  return (
    <div className="chat-hitl-summary chat-workflow-review">
      <div className="chat-hitl-title">
        {reviewTitle ? formatUserFacingLabel(reviewTitle) : t("component.chat_action_card.workflow_review_title")}
      </div>
      {prompt && !promptShown && !resolved && <div className="chat-hitl-description">{formatUserFacingText(prompt)}</div>}

      <div className="chat-workflow-review-scroll">
        {productPromise && (
          <section className="chat-workflow-review-section">
            <span className="chat-workflow-review-label">{t("component.chat_action_card.product_promise")}</span>
            <p>{formatUserFacingText(productPromise)}</p>
          </section>
        )}

        {outputBits.length > 0 && (
          <div className="chat-workflow-review-meta" aria-label={t("component.chat_action_card.output_profile")}>
            {outputBits.map((item) => <code key={item}>{item}</code>)}
          </div>
        )}

        {narration && (
          <section className="chat-workflow-review-section">
            <span className="chat-workflow-review-label">{t("component.chat_action_card.canonical_narration")}</span>
            <p>{formatUserFacingText(narration)}</p>
          </section>
        )}

        <ReviewChecklist items={checklist} />

        <section className="chat-workflow-review-section">
          <span className="chat-workflow-review-label">
            {t("component.chat_action_card.side_effects")}
          </span>
          <p>
            {sideEffects.length > 0
              ? sideEffects.map(formatUserFacingText).join("; ")
              : t("component.chat_action_card.no_side_effects")}
          </p>
        </section>

        {scenes.length > 0 && (
          <section className="chat-workflow-review-section">
            <span className="chat-workflow-review-label">
              {t("component.chat_action_card.capture_scope").replace("{count}", String(scenes.length))}
            </span>
            <div className="chat-workflow-scene-list">
              {scenes.map((scene, index) => {
                const actions = Array.isArray(scene.browser_actions)
                  ? scene.browser_actions.map(asRecord).filter((action): action is Record<string, any> => Boolean(action))
                  : [];
                const privacyRules = stringList(scene.privacy_rules);
                const criteria = stringList(scene.acceptance_criteria);
                const purpose = String(scene.narrative_purpose || scene.scene_id || "").trim();
                const sceneNarration = String(scene.narration_text || "").trim();
                return (
                  <details className="chat-workflow-scene" key={String(scene.scene_id || index)} open={index === 0}>
                    <summary>
                      <span>{index + 1}</span>
                      <strong>{formatUserFacingText(purpose) || `${t("component.chat_action_card.scene")} ${index + 1}`}</strong>
                      <code>{[scene.capture_type, scene.target_duration_seconds ? `${scene.target_duration_seconds}s` : ""].filter(Boolean).join(" · ")}</code>
                    </summary>
                    <div className="chat-workflow-scene-body">
                      {sceneNarration && <p>{formatUserFacingText(sceneNarration)}</p>}
                      {actions.length > 0 && (
                        <div>
                          <b>{t("component.chat_action_card.actions")}</b>
                          <ul>{actions.map((action, actionIndex) => (
                            <li key={`${String(action.action || "action")}-${actionIndex}`}>
                              {formatUserFacingLabel(String(action.action || "action"))}
                              {action.side_effect ? ` (${t("component.chat_action_card.side_effect")})` : ""}
                            </li>
                          ))}</ul>
                        </div>
                      )}
                      {privacyRules.length > 0 && (
                        <div><b>{t("component.chat_action_card.privacy_rules")}</b><ul>{privacyRules.map((rule) => <li key={rule}>{formatUserFacingText(rule)}</li>)}</ul></div>
                      )}
                      {criteria.length > 0 && (
                        <div><b>{t("component.chat_action_card.acceptance_criteria")}</b><ul>{criteria.map((item) => <li key={item}>{formatUserFacingText(item)}</li>)}</ul></div>
                      )}
                    </div>
                  </details>
                );
              })}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
