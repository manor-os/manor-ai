/**
 * FolderPropertiesDialog — set visibility / classification / client_visible
 * on a folder, with an optional "apply to existing contents" cascade.
 *
 * Used from Knowledge.tsx folder context menu "Properties...". Mirrors the
 * upload-options vocabulary so users see the same labels they used at
 * upload time.
 *
 * Cascade semantics (matches backend POST /folders/{id}/properties):
 *   - cascade=true (default): existing docs + subfolders get auto-adjusted
 *     to satisfy classification-floor + visibility-ceiling. Reported back
 *     in the response so we can show "12 docs updated, 3 subfolders updated".
 *   - cascade=false: only this folder row changes; children pick up the
 *     new constraints lazily on next upload/move.
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
  folderId: string;
  folderName: string;
  /** Current values. Used to pre-fill the form. */
  visibility?: Visibility | null;
  classification?: Classification | null;
  clientVisible?: boolean | null;
  onCancel: () => void;
  onSave: (data: {
    visibility?: Visibility;
    classification?: Classification;
    client_visible?: boolean;
    cascade: boolean;
  }) => Promise<{
    docs_updated: number;
    subfolders_updated: number;
  } | null>;
}

// Per-render: read the i18n strings. Functions (not module-level consts)
// so locale switches re-resolve labels without remounting the component.
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

export default function FolderPropertiesDialog({
  open,
  folderName,
  visibility,
  classification,
  clientVisible,
  onCancel,
  onSave,
}: Props) {
  const [vis, setVis] = useState<Visibility | undefined>(visibility ?? undefined);
  const [cls, setCls] = useState<Classification | undefined>(classification ?? undefined);
  const [cv, setCv] = useState<boolean>(!!clientVisible);
  const [cascade, setCascade] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ docs_updated: number; subfolders_updated: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setVis(visibility ?? undefined);
      setCls(classification ?? undefined);
      setCv(!!clientVisible);
      setCascade(true);
      setResult(null);
      setError(null);
    }
  }, [open, visibility, classification, clientVisible]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const updates: {
        visibility?: Visibility;
        classification?: Classification;
        client_visible?: boolean;
        cascade: boolean;
      } = { cascade };
      if (vis && vis !== visibility) updates.visibility = vis;
      if (cls && cls !== classification) updates.classification = cls;
      if (cv !== !!clientVisible) updates.client_visible = cv;
      const summary = await onSave(updates);
      setResult(summary);
    } catch (e: any) {
      setError(translateApiError(e, t("permissions.error.save_failed")));
    } finally {
      setBusy(false);
    }
  };

  // Cross-field invariants for the picker (mirrors backend):
  //   restricted + public  -> blocked
  //   confidential+ + client_visible -> blocked
  const restrictedPublicConflict =
    cls === "restricted" && vis === "public";
  const confidentialClientConflict =
    cv && (cls === "confidential" || cls === "restricted");
  const blocked = restrictedPublicConflict || confidentialClientConflict;

  const visibilityOptions = _visibilityOptions();
  const classificationOptions = _classificationOptions();

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={t("permissions.folder.title", { name: folderName })}
      maxWidth="520px"
    >
      {/* Visibility */}
      <fieldset style={{ border: "none", padding: 0, margin: "0 0 14px" }}>
        <legend style={{ fontSize: 11, fontWeight: 700, color: "#57534e", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
          {t("permissions.folder.section.visibility")}
        </legend>
        <p style={{ fontSize: 11, color: "#a8a29e", margin: "0 0 8px" }}>
          {t("permissions.folder.visibility_hint")}
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }} role="radiogroup" aria-label={t("permissions.folder.section.visibility")}>
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
          {t("permissions.folder.section.classification")}
        </legend>
        <p style={{ fontSize: 11, color: "#a8a29e", margin: "0 0 8px" }}>
          {t("permissions.folder.classification_hint")}
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }} role="radiogroup" aria-label={t("permissions.folder.section.classification")}>
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
            title={cvDisabled ? t("permissions.folder.conflict.confidential_client") : undefined}
          >
            <Toggle
              checked={cv && !cvDisabled}
              onChange={() => !cvDisabled && setCv(!cv)}
              disabled={cvDisabled}
              size="sm"
              aria-label={t("permissions.folder.client_visible_label")}
            />
            <span style={{ fontSize: 13, color: "#292524" }}>
              {t("permissions.folder.client_visible_label")}
            </span>
          </div>
        );
      })()}

      {/* Cascade option */}
      <div
        style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "10px 12px", borderRadius: 8, marginBottom: 4,
          background: "rgba(249,244,236,0.6)",
        }}
      >
        <Toggle
          checked={cascade}
          onChange={() => setCascade(!cascade)}
          size="sm"
          aria-label={t("permissions.folder.cascade_label")}
        />
        <span style={{ fontSize: 13, color: "#292524" }}>
          {t("permissions.folder.cascade_label")}
        </span>
      </div>
      <p style={{ fontSize: 11, color: "#a8a29e", margin: "6px 0 12px 12px" }}>
        {t("permissions.folder.cascade_hint")}
      </p>

      {/* Errors / result */}
      {blocked && (
        <div style={{ padding: "8px 12px", background: "#f8f0ef", color: "#a23e38", borderRadius: 6, fontSize: 12, marginBottom: 10 }}>
          {restrictedPublicConflict && t("permissions.folder.conflict.restricted_public")}
          {confidentialClientConflict && ` ${t("permissions.folder.conflict.confidential_client")}`}
        </div>
      )}
      {error && (
        <div style={{ padding: "8px 12px", background: "#f8f0ef", color: "#a23e38", borderRadius: 6, fontSize: 12, marginBottom: 10 }}>
          {error}
        </div>
      )}
      {result && (
        <div style={{ padding: "8px 12px", background: "#f1f6f3", color: "#3f7361", borderRadius: 6, fontSize: 12, marginBottom: 10 }}>
          {t("permissions.folder.cascade_result", { docs: result.docs_updated, subs: result.subfolders_updated })}
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
