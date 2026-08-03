import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";

function previewText(value: unknown): string | null {
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
  const friendly = formatUserFacingText(text);
  return friendly.length > 1400 ? `${friendly.slice(0, 1400)}\n...` : friendly;
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

export interface WorkflowApprovalReviewProps {
  prompt?: string;
  reviewTitle?: string;
  review: unknown;
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
  const sideEffects = stringList(plan?.listed_side_effects);
  const productPromise = String(plan?.product_promise || "").trim();
  const narration = String(plan?.canonical_narration || "").trim();
  const estimate = Number(plan?.estimated_duration_seconds || 0);
  const hasProductVideoPlan = Boolean(productPromise || narration || scenes.length || outputProfile);

  if (!hasProductVideoPlan) {
    const fallback = previewText(review);
    return (
      <div className="chat-hitl-summary chat-workflow-review">
        <div className="chat-hitl-title">
          {reviewTitle || t("component.chat_action_card.workflow_review_title")}
        </div>
        {prompt && <div className="chat-hitl-description">{formatUserFacingText(prompt)}</div>}
        {fallback && (
          <pre className="chat-hitl-content" aria-label={t("component.chat_action_card.approval_content_preview")}>
            {fallback}
          </pre>
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
        {reviewTitle || t("component.chat_action_card.workflow_review_title")}
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
