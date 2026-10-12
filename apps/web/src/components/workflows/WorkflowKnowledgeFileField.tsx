import { useRef, useState } from "react";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { Document } from "../../lib/types";
import { IconFolder, IconUpload } from "../icons";
import HitlKnowledgePicker from "../ui/HitlKnowledgePicker";

export type WorkflowFileKind = "audio" | "image";

const FILE_TYPES: Record<WorkflowFileKind, { extensions: string[]; accept: string }> = {
  audio: { extensions: ["wav", "flac", "mp3", "aiff", "aif"], accept: ".wav,.flac,.mp3,.aiff,.aif" },
  image: { extensions: ["png", "jpg", "jpeg"], accept: ".png,.jpg,.jpeg" },
};

export function workflowFileName(path: string): string {
  const name = path.split("/").pop() || path;
  return name.replace(/_\d{10}(?=\.[^.]+$)/, "");
}

function supported(path: string, kind: WorkflowFileKind): boolean {
  return FILE_TYPES[kind].extensions.includes(path.split(".").pop()?.toLowerCase() || "");
}

export default function WorkflowKnowledgeFileField({
  value, kind, disabled, onChange, onBusyChange,
}: {
  value: string;
  kind: WorkflowFileKind;
  disabled?: boolean;
  onChange: (path: string) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const uploadRef = useRef<HTMLInputElement>(null);
  const knowledgeRef = useRef<HTMLButtonElement>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  const selectDocument = (document: Document) => {
    if (!document.fs_path || !supported(document.fs_path, kind)) return;
    onChange(document.fs_path);
    setError("");
    setPickerOpen(false);
  };

  const upload = async (file: File) => {
    if (!supported(file.name, kind)) {
      setError(t("component.workflow_file.unsupported_type"));
      return;
    }
    setUploading(true);
    onBusyChange?.(true);
    setError("");
    try {
      const document = await api.documents.upload(file, undefined, { visibility: "private" });
      if (!document.fs_path) throw new Error(t("component.workflow_file.missing_path"));
      onChange(document.fs_path);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : t("component.workflow_file.upload_failed"));
    } finally {
      setUploading(false);
      onBusyChange?.(false);
    }
  };

  return (
    <div className="workflow-knowledge-file">
      {value && (
        <div className="workflow-knowledge-file__selected">
          <span title={workflowFileName(value)}>{workflowFileName(value)}</span>
          <button type="button" onClick={() => onChange("")} disabled={disabled || uploading} aria-label={t("component.workflow_file.remove")}>×</button>
        </div>
      )}
      <div className="workflow-knowledge-file__actions">
        <button ref={knowledgeRef} type="button" onClick={() => setPickerOpen(true)} disabled={disabled || uploading}>
          <IconFolder size={16} /> {t("component.workflow_file.choose_knowledge")}
        </button>
        <button type="button" onClick={() => uploadRef.current?.click()} disabled={disabled || uploading}>
          <IconUpload size={16} /> {uploading ? t("component.media_insert.uploading") : t("component.workflow_file.upload_local")}
        </button>
      </div>
      <input
        ref={uploadRef}
        type="file"
        accept={FILE_TYPES[kind].accept}
        hidden
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) void upload(file);
          event.currentTarget.value = "";
        }}
      />
      {error && <small role="alert">{error}</small>}
      {pickerOpen && (
        <HitlKnowledgePicker
          selectedIds={new Set()}
          acceptDocument={(document) => Boolean(document.fs_path && supported(document.fs_path, kind))}
          onToggle={selectDocument}
          onClose={() => setPickerOpen(false)}
          restoreFocus={() => knowledgeRef.current?.focus()}
        />
      )}
    </div>
  );
}
