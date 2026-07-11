/**
 * UploadOptionsDialog — wizard surfaced before any upload (drag-drop, file
 * picker, or "+" button). Lets the user pick visibility + classification +
 * client_visible before bytes leave the browser.
 *
 * Cloud-drive principle (UX spec §1): badges only appear in 3 places —
 * this dialog is one of them. Keeps the file list / viewer header clean.
 *
 * "Skip → use defaults" is a one-click escape hatch so power users who
 * just want to drag a file don't get yelled at.
 */
import { useEffect, useState } from "react";
import type { Classification, Visibility } from "../../lib/types";
import { t } from "../../lib/i18n";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import RadioCard from "../ui/RadioCard";
import Toggle from "../ui/Toggle";
import ClassificationBadge from "./ClassificationBadge";
import VisibilityIcon from "./VisibilityIcon";

export interface UploadOptionsValue {
  visibility: Visibility;
  classification: Classification;
  client_visible: boolean;
}

interface Props {
  open: boolean;
  files: File[];
  /** Default values to preselect (e.g. from folder rules). */
  defaults?: Partial<UploadOptionsValue>;
  /** Restrict pickable visibilities (e.g. folder caps at workspace). */
  visibilityCap?: Visibility;
  /** Restrict pickable min-classification (folder rule). */
  classificationFloor?: Classification;
  onCancel: () => void;
  onConfirm: (value: UploadOptionsValue) => void;
}

// Per-render so locale switches re-resolve labels.
function _visibilityOptions(): { value: Visibility; label: string; hint: string }[] {
  return [
    { value: "private", label: t("permissions.upload.visibility.private.label"), hint: t("permissions.upload.visibility.private.hint") },
    { value: "workspace", label: t("permissions.upload.visibility.workspace.label"), hint: t("permissions.upload.visibility.workspace.hint") },
    { value: "entity", label: t("permissions.upload.visibility.entity.label"), hint: t("permissions.upload.visibility.entity.hint") },
  ];
}

function _classificationOptions(): { value: Classification; label: string; hint: string }[] {
  return [
    { value: "internal", label: t("permissions.classification.internal.label"), hint: t("permissions.upload.classification.internal.hint") },
    { value: "confidential", label: t("permissions.classification.confidential.label"), hint: t("permissions.upload.classification.confidential.hint") },
    { value: "restricted", label: t("permissions.classification.restricted.label"), hint: t("permissions.upload.classification.restricted.hint") },
  ];
}

const CLASS_RANK: Record<Classification, number> = {
  public: 0,
  internal: 1,
  confidential: 2,
  restricted: 3,
};

const VIS_RANK: Record<Visibility, number> = {
  private: 0,
  workspace: 1,
  entity: 2,
  public: 3,
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function UploadOptionsDialog({
  open,
  files,
  defaults,
  visibilityCap,
  classificationFloor,
  onCancel,
  onConfirm,
}: Props) {
  const [visibility, setVisibility] = useState<Visibility>(
    defaults?.visibility ?? "workspace",
  );
  const [classification, setClassification] = useState<Classification>(
    defaults?.classification ?? "internal",
  );
  const [clientVisible, setClientVisible] = useState<boolean>(
    defaults?.client_visible ?? false,
  );
  const [confirmRestricted, setConfirmRestricted] = useState(false);

  // Reset whenever a new batch is opened (so previous selection doesn't bleed
  // across separate uploads).
  useEffect(() => {
    if (open) {
      setVisibility(defaults?.visibility ?? "workspace");
      setClassification(defaults?.classification ?? "internal");
      setClientVisible(defaults?.client_visible ?? false);
      setConfirmRestricted(false);
    }
  }, [open, defaults]);

  // Restricted classification can never be client-visible.
  useEffect(() => {
    if (classification === "restricted" || classification === "confidential") {
      setClientVisible(false);
    }
  }, [classification]);

  const totalBytes = files.reduce((sum, f) => sum + f.size, 0);
  const filesLabel = files.length === 1
    ? t("permissions.upload.files_summary_single", { name: files[0].name, size: formatSize(totalBytes) })
    : t("permissions.upload.files_summary_multi", { count: files.length, size: formatSize(totalBytes) });

  const isRestrictedNeedsConfirm = classification === "restricted" && !confirmRestricted;
  const clientVisibleAllowed = classification !== "confidential" && classification !== "restricted";
  const visibilityOptions = _visibilityOptions();
  const classificationOptions = _classificationOptions();

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={t("permissions.upload.title")}
      maxWidth="560px"
      footer={
        <>
          <Button variant="outline" onClick={onCancel}>{t("permissions.action.cancel")}</Button>
          <Button
            variant="ghost"
            onClick={() => onConfirm({ visibility: "workspace", classification: "internal", client_visible: false })}
          >
            {t("permissions.action.use_defaults")}
          </Button>
          <Button
            variant="primary"
            disabled={isRestrictedNeedsConfirm}
            onClick={() => onConfirm({ visibility, classification, client_visible: clientVisible })}
          >
            {isRestrictedNeedsConfirm ? t("permissions.upload.upload_button_needs_confirm") : t("permissions.upload.upload_button")}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {/* File summary */}
        <div style={{
          padding: "10px 12px", background: "rgba(245,245,244,0.6)",
          borderRadius: 8, fontSize: 12, color: "#57534e",
        }}>
          📎 {filesLabel}
        </div>

        {/* Visibility */}
        <fieldset style={{ border: "none", padding: 0, margin: 0 }}>
          <legend style={{ fontSize: 12, fontWeight: 600, color: "#57534e", marginBottom: 8 }}>
            {t("permissions.upload.section.visibility")}
          </legend>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }} role="radiogroup" aria-label={t("permissions.upload.section.visibility")}>
            {visibilityOptions.map((opt) => {
              const disabled = visibilityCap != null && VIS_RANK[opt.value] > VIS_RANK[visibilityCap];
              return (
                <RadioCard
                  key={opt.value}
                  selected={visibility === opt.value}
                  onSelect={() => setVisibility(opt.value)}
                  disabled={disabled}
                  title={disabled ? t("permissions.upload.cap_disabled_title", { cap: visibilityCap! }) : opt.hint}
                  ariaLabel={opt.label}
                >
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, color: "#292524" }}>
                    <VisibilityIcon visibility={opt.value} size={13} />
                    {opt.label}
                  </span>
                  <span style={{ fontSize: 11, color: "#a8a29e", marginLeft: "auto" }}>{opt.hint}</span>
                </RadioCard>
              );
            })}
          </div>
        </fieldset>

        {/* Classification */}
        <fieldset style={{ border: "none", padding: 0, margin: 0 }}>
          <legend style={{ fontSize: 12, fontWeight: 600, color: "#57534e", marginBottom: 8 }}>
            {t("permissions.upload.section.classification")}
          </legend>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }} role="radiogroup" aria-label={t("permissions.upload.section.classification")}>
            {classificationOptions.map((opt) => {
              const disabled = classificationFloor != null && CLASS_RANK[opt.value] < CLASS_RANK[classificationFloor];
              return (
                <RadioCard
                  key={opt.value}
                  selected={classification === opt.value}
                  onSelect={() => {
                    setClassification(opt.value);
                    if (opt.value !== "restricted") setConfirmRestricted(false);
                  }}
                  disabled={disabled}
                  title={disabled ? t("permissions.upload.floor_disabled_title", { floor: classificationFloor! }) : opt.hint}
                  ariaLabel={opt.label}
                >
                  <ClassificationBadge level={opt.value} size="sm" />
                  <span style={{ fontSize: 11, color: "#a8a29e", marginLeft: "auto" }}>{opt.hint}</span>
                </RadioCard>
              );
            })}
          </div>

          {classification === "restricted" && !confirmRestricted && (
            <div style={{
              marginTop: 10, padding: "10px 12px",
              background: "#f8f0ef", borderLeft: "3px solid #d65f59",
              color: "#a23e38", borderRadius: 8, fontSize: 12,
              display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8,
            }}>
              <span style={{ flex: 1, minWidth: 0 }}>
                {t("permissions.upload.restricted_confirm")}
              </span>
              <Button variant="danger" size="sm" onClick={() => setConfirmRestricted(true)}>
                {t("permissions.upload.confirm_restricted")}
              </Button>
            </div>
          )}
        </fieldset>

        {/* Client visibility */}
        <div
          style={{
            display: "flex", alignItems: "center", gap: 10,
            padding: "10px 12px", borderRadius: 8,
            background: "rgba(245,245,244,0.4)",
            opacity: clientVisibleAllowed ? 1 : 0.5,
          }}
          title={clientVisibleAllowed ? undefined : t("permissions.upload.client_visible_disabled_title")}
        >
          <Toggle
            checked={clientVisible && clientVisibleAllowed}
            onChange={() => clientVisibleAllowed && setClientVisible(!clientVisible)}
            disabled={!clientVisibleAllowed}
            size="sm"
            aria-label={t("permissions.upload.client_visible_label")}
          />
          <span style={{ fontSize: 13, color: "#292524" }}>{t("permissions.upload.client_visible_label")}</span>
          <span style={{ fontSize: 11, color: "#a8a29e", marginLeft: "auto" }}>{t("permissions.upload.client_visible_hint")}</span>
        </div>
      </div>
    </Modal>
  );
}
