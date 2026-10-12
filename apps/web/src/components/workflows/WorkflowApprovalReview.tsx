import { useEffect, useRef, useState } from "react";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import MediaPreview from "./MediaPreview";
import { workflowFinalMediaRefs } from "../../lib/workflowMedia";
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

function StructuredReviewValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
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
  const href = reviewHref(text);
  return href ? (
    <a className="chat-workflow-review-link" href={href} target={href.startsWith("http") ? "_blank" : undefined} rel={href.startsWith("http") ? "noreferrer" : undefined}>{text}</a>
  ) : <span className="chat-workflow-review-value">{formatUserFacingText(text)}</span>;
}

export function workflowReviewIsEditable(review: unknown): review is Record<string, unknown> {
  return Boolean(review && typeof review === "object" && !Array.isArray(review));
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
}: WorkflowApprovalReviewProps & {
  disabled?: boolean;
  onDraftState: (draft: Record<string, unknown>, valid: boolean) => void;
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
  const [errors, setErrors] = useState<Record<string, string>>({});
  const editDetailsRef = useRef<HTMLDetailsElement>(null);
  const hasErrors = Object.values(errors).some(Boolean);
  const changed = JSON.stringify(draft) !== JSON.stringify(review);

  useEffect(() => {
    onDraftState(draft, !hasErrors);
  }, [draft, hasErrors, onDraftState]);

  useEffect(() => {
    if (hasErrors && editDetailsRef.current) editDetailsRef.current.open = true;
  }, [hasErrors]);

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

export interface ReviewArtifactReference {
  key: string;
  label?: string;
  reference: string;
}

function reviewArtifact(value: unknown, index: number): ReviewArtifactReference | null {
  if (typeof value === "string" && value.trim()) {
    const reference = value.trim();
    return { key: `${reference}:${index}`, reference };
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
  return { key: `${reference}:${index}`, reference, label };
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

export interface WorkflowApprovalReviewProps {
  prompt?: string;
  reviewTitle?: string;
  review: unknown;
  full?: boolean;
}

export default function WorkflowApprovalReview({
  prompt,
  reviewTitle,
  review,
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

  if (!hasProductVideoPlan) {
    const entries = plan
      ? Object.entries(plan).filter(([key, value]) => (
        !["review_artifacts", "artifacts", "files", "checklist"].includes(key)
        && hasReviewValue(value)
      ))
      : [];
    const facts = entries.filter(([, value]) => !Array.isArray(value) && !asRecord(value));
    const sections = entries.filter(([, value]) => Array.isArray(value) || Boolean(asRecord(value)));
    // "Review the actual generated video" needs a player, not its link.
    const media = workflowFinalMediaRefs(review);
    return (
      <div className="chat-hitl-summary chat-workflow-review">
        <div className="chat-hitl-title">
          {reviewTitle ? formatUserFacingLabel(reviewTitle) : t("component.chat_action_card.workflow_review_title")}
        </div>
        {prompt && <div className="chat-hitl-description">{formatUserFacingText(prompt)}</div>}
        {(media.length > 0 || artifacts.length > 0 || checklist.length > 0 || entries.length > 0 || hasReviewValue(review)) && (
          <div className="chat-workflow-review-scroll chat-workflow-review-scroll--structured">
            {media.length > 0 && (
              <section className="chat-workflow-review-section chat-workflow-review-media">
                {media.map((item, index) => <MediaPreview key={`${item.url}-${index}`} refItem={item} maxHeight={320} />)}
              </section>
            )}
            {artifacts.length > 0 && (
              <section className="chat-workflow-review-section">
                <span className="chat-workflow-review-label">
                  {t("component.chat_action_card.files_requiring_approval")}
                </span>
                <div className="chat-workflow-review-artifacts">
                  {artifacts.map((artifact) => (
                    <InlineFileReferenceCard
                      key={artifact.key}
                      reference={artifact.reference}
                      label={artifact.label}
                      compact
                    />
                  ))}
                </div>
              </section>
            )}
            <ReviewChecklist items={checklist} />
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
            {plan && <details className="chat-workflow-review-raw">
              <summary>{t("component.workflow_review.original_data")}</summary>
              <pre>{previewText(review, true)}</pre>
            </details>}
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
      {prompt && <div className="chat-hitl-description">{formatUserFacingText(prompt)}</div>}

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
