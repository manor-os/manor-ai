/**
 * DocumentPropertiesDialog — set visibility / classification / client_visible
 * on a single document.
 *
 * Mirror of FolderPropertiesDialog without the cascade option (a single
 * file has no children to recurse into). Same cross-field invariants
 * (Restricted ⇒ visibility ≠ public; Confidential/Restricted ⇒
 * client_visible=false) are enforced client-side for UX and re-checked
 * server-side per RFC §13.14.
 *
 * Used from:
 *   - Knowledge.tsx file context menu "File properties..."
 *   - Knowledge.tsx Get Info modal — "Properties..." button
 *
 * The dialog only emits the *changed* fields, so the caller can wire
 * each one to the appropriate /permissions/documents/{id}/* endpoint.
 * Sequential is fine — the three fields are independent and there's no
 * transactional requirement.
 */
import { useEffect, useState } from "react";
import type { Classification, Visibility } from "../../lib/types";
import { t } from "../../lib/i18n";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import RadioCard from "../ui/RadioCard";
import Toggle from "../ui/Toggle";
import { translateApiError } from "../../lib/api";
import ClassificationBadge from "./ClassificationBadge";
import VisibilityIcon from "./VisibilityIcon";

interface Props {
  open: boolean;
  docId: string;
  docName: string;
  /** Current values. Used to pre-fill the form. */
  visibility?: Visibility | null;
  classification?: Classification | null;
  clientVisible?: boolean | null;
  onCancel: () => void;
  /** Receives only the fields that the user actually changed. Caller wires
   *  each to the appropriate setter (visibility / classify / client_visible). */
  onSave: (changes: {
    visibility?: Visibility;
    classification?: Classification;
    client_visible?: boolean;
  }) => Promise<void>;
}

function _visibilityOptions(): { value: Visibility; label: string; hint: string }[] {
  return [
    { value: "private", label: t("permissions.upload.visibility.private.label"), hint: t("permissions.visibility.private.label") },
    { value: "workspace", label: t("permissions.upload.visibility.workspace.label"), hint: t("permissions.visibility.workspace.label") },
    { value: "entity", label: t("permissions.upload.visibility.entity.label"), hint: t("permissions.visibility.entity.label") },
  ];
}

function _classificationOptions(): { value: Classification; label: string; hint: string }[] {
  return [
    { value: "internal", label: t("permissions.classification.internal.label"), hint: t("permissions.upload.classification.internal.hint") },
    { value: "confidential", label: t("permissions.classification.confidential.label"), hint: t("permissions.upload.classification.confidential.hint") },
    { value: "restricted", label: t("permissions.classification.restricted.label"), hint: t("permissions.upload.classification.restricted.hint") },
  ];
}

export default function DocumentPropertiesDialog({
  open,
  docName,
  visibility,
  classification,
  clientVisible,
  onCancel,
  onSave,
}: Props) {
  const [vis, setVis] = useState<Visibility | undefined>(visibility ?? undefined);
  const [cls, setCls] = useState<Classification | undefined>(classification ?? undefined);
  const [cv, setCv] = useState<boolean>(!!clientVisible);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setVis(visibility ?? undefined);
      setCls(classification ?? undefined);
      setCv(!!clientVisible);
      setError(null);
    }
  }, [open, visibility, classification, clientVisible]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const changes: {
        visibility?: Visibility;
        classification?: Classification;
        client_visible?: boolean;
      } = {};
      if (vis && vis !== visibility) changes.visibility = vis;
      if (cls && cls !== classification) changes.classification = cls;
      if (cv !== !!clientVisible) changes.client_visible = cv;
      await onSave(changes);
      onCancel(); // close on success
    } catch (e: any) {
      setError(translateApiError(e, t("permissions.error.save_failed")));
    } finally {
      setBusy(false);
    }
  };

  // Same cross-field invariants as the folder dialog.
  const restrictedPublicConflict = cls === "restricted" && vis === "public";
  const confidentialClientConflict =
    cv && (cls === "confidential" || cls === "restricted");
  const blocked = restrictedPublicConflict || confidentialClientConflict;

  const visibilityOptions = _visibilityOptions();
  const classificationOptions = _classificationOptions();

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={t("permissions.doc.title", { name: docName })}
      maxWidth="520px"
    >
      {/* Visibility */}
      <fieldset style={{ border: "none", padding: 0, margin: "0 0 14px" }}>
        <legend style={{ fontSize: 11, fontWeight: 700, color: "#57534e", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
          {t("permissions.doc.section.visibility")}
        </legend>
        <p style={{ fontSize: 11, color: "#a8a29e", margin: "0 0 8px" }}>
          {t("permissions.doc.visibility_hint")}
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }} role="radiogroup" aria-label={t("permissions.doc.section.visibility")}>
          {visibilityOptions.map((opt) => (
            <RadioCard
              key={opt.value}
              selected={vis === opt.value}
              onSelect={() => setVis(opt.value)}
              ariaLabel={opt.label}
            >
              <VisibilityIcon visibility={opt.value} size={13} />
              <span style={{ fontSize: 13 }}>{opt.label}</span>
              <span style={{ fontSize: 11, color: "#a8a29e", marginLeft: "auto" }}>{opt.hint}</span>
            </RadioCard>
          ))}
        </div>
      </fieldset>

      {/* Classification */}
      <fieldset style={{ border: "none", padding: 0, margin: "0 0 14px" }}>
        <legend style={{ fontSize: 11, fontWeight: 700, color: "#57534e", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
          {t("permissions.doc.section.classification")}
        </legend>
        <p style={{ fontSize: 11, color: "#a8a29e", margin: "0 0 8px" }}>
          {t("permissions.doc.classification_hint")}
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }} role="radiogroup" aria-label={t("permissions.doc.section.classification")}>
          {classificationOptions.map((opt) => (
            <RadioCard
              key={opt.value}
              selected={cls === opt.value}
              onSelect={() => setCls(opt.value)}
              ariaLabel={opt.label}
            >
              <ClassificationBadge level={opt.value} size="sm" />
              <span style={{ fontSize: 11, color: "#a8a29e", marginLeft: "auto" }}>{opt.hint}</span>
            </RadioCard>
          ))}
        </div>
      </fieldset>

      {/* Client visible */}
      {(() => {
        const cvDisabled = cls === "confidential" || cls === "restricted";
        return (
          <div
            style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "10px 12px", borderRadius: 8, marginBottom: 14,
              background: "rgba(245,245,244,0.4)",
              opacity: cvDisabled ? 0.5 : 1,
            }}
            title={cvDisabled ? t("permissions.doc.conflict.confidential_client") : undefined}
          >
            <Toggle
              checked={cv && !cvDisabled}
              onChange={() => !cvDisabled && setCv(!cv)}
              disabled={cvDisabled}
              size="sm"
              aria-label={t("permissions.doc.client_visible_label")}
            />
            <span style={{ fontSize: 13, color: "#292524" }}>
              {t("permissions.doc.client_visible_label")}
            </span>
          </div>
        );
      })()}

      {/* Errors */}
      {blocked && (
        <div style={{ padding: "8px 12px", background: "#f8f0ef", color: "#a23e38", borderRadius: 6, fontSize: 12, marginBottom: 10 }}>
          {restrictedPublicConflict && t("permissions.doc.conflict.restricted_public")}
          {confidentialClientConflict && ` ${t("permissions.doc.conflict.confidential_client")}`}
        </div>
      )}
      {error && (
        <div style={{ padding: "8px 12px", background: "#f8f0ef", color: "#a23e38", borderRadius: 6, fontSize: 12, marginBottom: 10 }}>
          {error}
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
        <Button variant="outline" onClick={onCancel}>{t("permissions.action.close")}</Button>
        <Button variant="primary" onClick={submit} disabled={busy || blocked}>
          {busy ? t("permissions.action.saving") : t("permissions.action.save")}
        </Button>
      </div>
    </Modal>
  );
}
