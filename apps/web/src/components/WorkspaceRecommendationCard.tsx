import { t } from "../lib/i18n";
import type { WorkspaceRecommendation } from "../lib/chatStream";
import Button from "./ui/Button";

interface WorkspaceRecommendationCardProps {
  recommendation: WorkspaceRecommendation;
  loading?: boolean;
  preferenceLoading?: boolean;
  disabled?: boolean;
  onCreate: () => void;
  onOpen: () => void;
  onAdd: () => void;
  onContinue: () => void;
  onDontSuggestAgain: () => void;
}

export default function WorkspaceRecommendationCard({
  recommendation,
  loading = false,
  preferenceLoading = false,
  disabled = false,
  onCreate,
  onOpen,
  onAdd,
  onContinue,
  onDontSuggestAgain,
}: WorkspaceRecommendationCardProps) {
  const targetName = recommendation.workspace_name || t(
    "component.workspace_recommendation.this_workspace",
  );
  const primaryAction = recommendation.action === "create_new"
    ? onCreate
    : recommendation.action === "add_to_existing"
      ? onAdd
      : onOpen;
  const primaryLabel = recommendation.action === "create_new"
    ? t("component.workspace_recommendation.create_workspace")
    : recommendation.action === "add_to_existing"
      ? t("component.workspace_recommendation.add_to_workspace", { name: targetName })
      : t("component.workspace_recommendation.open_workspace", { name: targetName });

  return (
    <section
      className="workspace-recommendation-card"
      aria-label={t("component.workspace_recommendation.title")}
      data-testid="workspace-recommendation-card"
    >
      <div className="workspace-recommendation-card__eyebrow">
        {t("component.workspace_recommendation.eyebrow")}
      </div>
      <h3>{t("component.workspace_recommendation.title")}</h3>
      <p>{recommendation.reason}</p>
      <div className="workspace-recommendation-card__actions">
        <Button
          size="sm"
          loading={loading}
          disabled={disabled}
          onClick={primaryAction}
          ariaLabel={primaryLabel}
        >
          {primaryLabel}
        </Button>
        {recommendation.action === "add_to_existing" && (
          <Button
            variant="ghost"
            size="sm"
            disabled={disabled || loading}
            onClick={onOpen}
          >
            {t("component.workspace_recommendation.open_only")}
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          disabled={disabled || loading || preferenceLoading}
          onClick={onContinue}
        >
          {t("component.workspace_recommendation.continue_in_chat")}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          loading={preferenceLoading}
          disabled={disabled || loading}
          onClick={onDontSuggestAgain}
        >
          {t("component.workspace_recommendation.dont_suggest_again")}
        </Button>
      </div>
    </section>
  );
}
