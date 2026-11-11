import { t } from "../../lib/i18n";

/** Says a form reopened with the answers left unsent last time, and offers to start over. */
export default function FormDraftNotice({ onDiscard, disabled }: { onDiscard: () => void; disabled?: boolean }) {
  return (
    <p className="form-draft-notice" role="status">
      <span>{t("component.workflow_schema.draft_restored")}</span>
      <button type="button" className="workflow-form-link" disabled={disabled} onClick={onDiscard}>
        {t("component.workflow_schema.draft_discard")}
      </button>
    </p>
  );
}
