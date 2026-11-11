import { useEffect, useRef, useState } from "react";
import { api, type DocumentMediaKind } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { Document } from "../../lib/types";
import { linkWorkflowDocuments, prepareWorkflowDocuments, workflowUploadFolderId, type WorkflowDocumentRef, type WorkflowUpload } from "../../lib/workflowRunInputs";
import { IconClose, IconDocument, IconFolder, IconImage, IconMicrophone, IconUpload } from "../icons";
import Button from "../ui/Button";
import HitlKnowledgePicker from "../ui/HitlKnowledgePicker";
import LoadingSpinner from "../ui/LoadingSpinner";
import WorkflowUploadProgress, { type WorkflowUploadStatus } from "./WorkflowUploadProgress";
import "./WorkflowWaitDocumentField.css";

function documentRef(document: Document): WorkflowDocumentRef {
  return { document_id: document.id, id: document.id, name: document.name, type: "knowledge", viewer_url: `/viewer/${document.id}`, fs_path: document.fs_path };
}

/**
 * One file a control made itself (a drawn signature) saved the way this field
 * saves an upload: into the Workspace's files for a run input, else Knowledge,
 * and linked to the Workspace's materials.
 */
export async function uploadWorkflowFile(file: File, { workspaceId, workflowRunInput = false }: {
  workspaceId?: string;
  workflowRunInput?: boolean;
}): Promise<WorkflowDocumentRef> {
  const folderId = workflowRunInput ? await workflowUploadFolderId(workspaceId, api.workspaces.get) : null;
  const document = documentRef(await api.documents.upload(file, folderId, {
    visibility: "private", classification: "internal", client_visible: false,
  }, { idempotencyKey: crypto.randomUUID() }));
  if (workspaceId) {
    await linkWorkflowDocuments([document], () => api.workspaces.documents(workspaceId),
      (groupId, ids) => api.workspaces.knowledge.addDocuments(workspaceId, groupId, ids));
  }
  return document;
}

export default function WorkflowWaitDocumentField({
  id, value, workspaceId, workflowRunInput = false, disabled, compact = false, onChange, onBusyChange, accept, capture,
}: {
  /** Also offer the phone's camera (``"environment"`` / ``"user"``) or, for audio, its microphone. */
  capture?: "environment" | "user" | boolean;
  id: string;
  value: unknown;
  workspaceId?: string;
  /** Set only for Workflow run inputs: their uploads go to the Workspace root. Other callers keep the Knowledge root. */
  workflowRunInput?: boolean;
  /** The only kinds of file the field takes (``x-ui.accept``); the picker and file chooser show just those. */
  accept?: DocumentMediaKind[];
  disabled?: boolean;
  /** One row instead of a drop area, for forms that ask for several kinds of files. */
  compact?: boolean;
  onChange: (value: WorkflowDocumentRef[]) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const captureRef = useRef<HTMLInputElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  // What the phone takes directly: a recording when the field takes audio only, else a photo.
  const captureKind = !capture ? null : accept?.length && accept.every((kind) => kind === "audio") ? "audio" : "image";
  const [pickerOpen, setPickerOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [linking, setLinking] = useState(false);
  const [uploads, setUploads] = useState<WorkflowUpload[]>([]);
  const [progress, setProgress] = useState<Record<string, WorkflowUploadStatus>>({});
  const [pendingSelected, setPendingSelected] = useState<WorkflowDocumentRef[]>([]);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const busyChangeRef = useRef(onBusyChange);
  busyChangeRef.current = onBusyChange;
  const documents = Array.isArray(value) ? value.filter((item): item is WorkflowDocumentRef => Boolean(item && typeof item === "object" && "document_id" in item)) : [];

  useEffect(() => {
    busyChangeRef.current?.(busy);
    return () => { if (busy) busyChangeRef.current?.(false); };
  }, [busy]);

  const add = async (selected: WorkflowDocumentRef[], pendingUploads: WorkflowUpload[] = uploads) => {
    if (disabled || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setPendingSelected(selected);
    setUploads(pendingUploads);
    setProgress(Object.fromEntries(pendingUploads.map((item) => [item.uploadKey, { phase: item.document ? "uploaded" : "queued" }])));
    const update = (item: WorkflowUpload, status: WorkflowUploadStatus) => setProgress((current) => ({ ...current, [item.uploadKey]: status }));
    try {
      const uploadFolderId = workflowRunInput && pendingUploads.some((item) => !item.document)
        ? await workflowUploadFolderId(workspaceId, api.workspaces.get)
        : null;
      const next = await prepareWorkflowDocuments(documents.concat(selected), pendingUploads, async (item) => {
        update(item, { phase: "uploading", percent: 0 });
        try {
          const document = await api.documents.upload(item.file, uploadFolderId, {
            visibility: "private", classification: "internal", client_visible: false,
          }, { idempotencyKey: item.uploadKey, onProgress: (status) => update(item, status) });
          update(item, { phase: "uploaded" });
          return documentRef(document);
        } catch (cause) {
          update(item, { phase: "failed", error: cause instanceof Error ? cause.message : t("component.workflow_file.upload_failed") });
          throw cause;
        }
      });
      if (workspaceId && next.length) {
        setLinking(true);
        await linkWorkflowDocuments(next, () => api.workspaces.documents(workspaceId), (groupId, ids) => api.workspaces.knowledge.addDocuments(workspaceId, groupId, ids));
      }
      onChange(next);
      setUploads([]);
      setPendingSelected([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("component.workflow_wait_document.add_failed"));
    } finally {
      busyRef.current = false;
      setLinking(false);
      setBusy(false);
    }
  };

  return <div className="workflow-wait-document-field" aria-busy={busy}>
    <div
      className={`workflow-wait-document-surface${dragging ? " is-dragging" : ""}${documents.length ? " has-files" : ""}${compact ? " is-compact" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled && !busy) setDragging(true);
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        if (disabled || busy) return;
        const pending = uploads.concat(Array.from(event.dataTransfer.files).map((file) => ({ file, uploadKey: crypto.randomUUID() })));
        if (pending.length) void add(pendingSelected, pending);
      }}
    >
      {compact ? <div className="workflow-wait-document-compact">
        {documents.length > 0
          ? documents.map((document) => <span key={document.document_id} className="workflow-wait-document-chip">
            <span aria-hidden="true"><IconDocument size={14} /></span>
            <span title={document.name || document.document_id}>{document.name || document.document_id}</span>
            <button type="button" disabled={disabled || busy} aria-label={t("component.workflow_wait_document.remove_named", { name: document.name || document.document_id })} onClick={() => onChange(documents.filter((item) => item.document_id !== document.document_id))}><IconClose size={12} /></button>
          </span>)
          : <span className="workflow-wait-document-empty">
            <span aria-hidden="true">{busy ? <LoadingSpinner size={15} /> : <IconDocument size={15} />}</span>
            {linking ? t("component.workflow_upload.linking") : busy ? t("component.workflow_wait_document.adding") : t("component.workflow_wait_document.drop_compact")}
          </span>}
      </div> : <div className="workflow-wait-document-intro">
        <span className="workflow-wait-document-icon" aria-hidden="true">{busy ? <LoadingSpinner size={20} /> : <IconDocument size={20} />}</span>
        <div>
          <strong role="status">{linking ? t("component.workflow_upload.linking") : busy ? t("component.workflow_wait_document.adding") : documents.length ? t(documents.length === 1 ? "component.workflow_wait_document.selected_one" : "component.workflow_wait_document.selected_many", { count: documents.length }) : t("component.workflow_wait_document.drop")}</strong>
          <span>{documents.length ? t("component.workflow_wait_document.add_more") : t("component.workflow_wait_document.choose_source")}</span>
        </div>
      </div>}
      {!compact && documents.length > 0 && <div className="workflow-wait-document-list">
        {documents.map((document) => <div key={document.document_id} className="workflow-wait-document-row">
          <span aria-hidden="true"><IconDocument size={16} /></span>
          <span title={document.name || document.document_id}>{document.name || document.document_id}</span>
          <button type="button" disabled={disabled || busy} aria-label={t("component.workflow_wait_document.remove_named", { name: document.name || document.document_id })} onClick={() => onChange(documents.filter((item) => item.document_id !== document.document_id))}>{t("component.workflow_wait_document.remove")}</button>
        </div>)}
      </div>}
      {uploads.length > 0 && <div className="workflow-upload-list">
        {compact && linking && documents.length > 0 && <span role="status" className="workflow-upload-summary">{t("component.workflow_upload.linking")}</span>}
        <span className="workflow-upload-summary">{t("component.workflow_upload.summary", {
          completed: uploads.filter((item) => progress[item.uploadKey]?.phase === "uploaded").length,
          total: uploads.length,
        })}</span>
        {uploads.map((item) => <WorkflowUploadProgress key={item.uploadKey} file={item.file}
          status={progress[item.uploadKey] || { phase: item.document ? "uploaded" : "queued" }} />)}
      </div>}
      <div className="workflow-wait-document-actions">
        {captureKind && <button type="button" disabled={disabled || busy} onClick={() => captureRef.current?.click()}>
          <span aria-hidden="true">{captureKind === "audio" ? <IconMicrophone size={compact ? 14 : 16} /> : <IconImage size={compact ? 14 : 16} />}</span>
          {" "}{t(captureKind === "audio" ? "component.workflow_schema.capture_audio" : "component.workflow_schema.capture_photo")}
        </button>}
        <button type="button" disabled={disabled || busy} onClick={() => inputRef.current?.click()}><span aria-hidden="true"><IconUpload size={compact ? 14 : 16} /></span> {t(compact ? "component.workflow_wait_document.upload_compact" : "component.workflow_file.upload_local")}</button>
        <button ref={buttonRef} type="button" disabled={disabled || busy} onClick={() => setPickerOpen(true)}><span aria-hidden="true"><IconFolder size={compact ? 14 : 16} /></span> {t(compact ? "component.workflow_wait_document.knowledge_compact" : "component.workflow_file.choose_knowledge")}</button>
      </div>
    </div>
    <input id={id} ref={inputRef} type="file" multiple hidden
      accept={accept?.length && !accept.includes("document") ? accept.map((kind) => `${kind}/*`).join(",") : undefined}
      onChange={(event) => {
      const pending = uploads.concat(Array.from(event.target.files || []).map((file) => ({ file, uploadKey: crypto.randomUUID() })));
      event.target.value = "";
      void add(pendingSelected, pending);
    }} />
    {captureKind && <input ref={captureRef} type="file" hidden aria-hidden="true" tabIndex={-1}
      accept={captureKind === "audio" ? "audio/*" : "image/*"}
      // On a phone this opens the camera or recorder; a desktop browser shows its file chooser.
      capture={capture === "user" ? "user" : "environment"}
      onChange={(event) => {
        const pending = uploads.concat(Array.from(event.target.files || []).map((file) => ({ file, uploadKey: crypto.randomUUID() })));
        event.target.value = "";
        void add(pendingSelected, pending);
      }} />}
    {error && <div className="workflow-wait-document-error" role="alert">
      <span>{error}</span>
      <Button size="sm" variant="outline" disabled={disabled || busy} onClick={() => void add(pendingSelected, uploads)}>{t("component.workflow_wait_document.retry")}</Button>
    </div>}
    {pickerOpen && <HitlKnowledgePicker selectedIds={new Set([...documents, ...pendingSelected].map((item) => item.document_id))} onToggle={(document) => {
      const selected = documentRef(document);
      if (documents.some((item) => item.document_id === selected.document_id)) {
        onChange(documents.filter((item) => item.document_id !== selected.document_id));
        setPendingSelected((current) => current.filter((item) => item.document_id !== selected.document_id));
        return;
      }
      if (pendingSelected.some((item) => item.document_id === selected.document_id)) {
        setPendingSelected((current) => current.filter((item) => item.document_id !== selected.document_id));
        return;
      }
      void add([...pendingSelected, selected], uploads);
    }} onClose={() => setPickerOpen(false)} restoreFocus={() => buttonRef.current?.focus()} workspaceId={workspaceId || undefined}
      kinds={accept} />}
  </div>;
}
