import { useRef, useState } from "react";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { Document } from "../../lib/types";
import { linkWorkflowDocuments, prepareWorkflowDocuments, type WorkflowDocumentRef, type WorkflowUpload } from "../../lib/workflowRunInputs";
import { IconDocument, IconFolder, IconUpload } from "../icons";
import Button from "../ui/Button";
import HitlKnowledgePicker from "../ui/HitlKnowledgePicker";

function documentRef(document: Document): WorkflowDocumentRef {
  return { document_id: document.id, id: document.id, name: document.name, type: "knowledge", viewer_url: `/viewer/${document.id}`, fs_path: document.fs_path };
}

export default function WorkflowWaitDocumentField({
  id, value, workspaceId, disabled, onChange,
}: {
  id: string;
  value: unknown;
  workspaceId?: string;
  disabled?: boolean;
  onChange: (value: WorkflowDocumentRef[]) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uploads, setUploads] = useState<WorkflowUpload[]>([]);
  const [pendingSelected, setPendingSelected] = useState<WorkflowDocumentRef[]>([]);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const documents = Array.isArray(value) ? value.filter((item): item is WorkflowDocumentRef => Boolean(item && typeof item === "object" && "document_id" in item)) : [];

  const add = async (selected: WorkflowDocumentRef[], pendingUploads: WorkflowUpload[] = uploads) => {
    if (disabled || busy) return;
    setBusy(true);
    setError("");
    setPendingSelected(selected);
    setUploads(pendingUploads);
    try {
      const next = await prepareWorkflowDocuments(documents.concat(selected), pendingUploads, async (item) => documentRef(await api.documents.upload(item.file, null, {
        visibility: "private", classification: "internal", client_visible: false,
      }, { idempotencyKey: item.uploadKey })));
      if (workspaceId && next.length) {
        await linkWorkflowDocuments(next, () => api.workspaces.documents(workspaceId), (groupId, ids) => api.workspaces.knowledge.addDocuments(workspaceId, groupId, ids));
      }
      onChange(next);
      setUploads([]);
      setPendingSelected([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("component.workflow_wait_document.add_failed"));
    } finally {
      setBusy(false);
    }
  };

  return <div className="workflow-wait-document-field" aria-busy={busy}>
    <div
      className={`workflow-wait-document-surface${dragging ? " is-dragging" : ""}${documents.length ? " has-files" : ""}`}
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
      <div className="workflow-wait-document-intro">
        <span className="workflow-wait-document-icon" aria-hidden="true"><IconDocument size={20} /></span>
        <div>
          <strong>{busy ? t("component.workflow_wait_document.adding") : documents.length ? t(documents.length === 1 ? "component.workflow_wait_document.selected_one" : "component.workflow_wait_document.selected_many", { count: documents.length }) : t("component.workflow_wait_document.drop")}</strong>
          <span>{documents.length ? t("component.workflow_wait_document.add_more") : t("component.workflow_wait_document.choose_source")}</span>
        </div>
      </div>
      {documents.length > 0 && <div className="workflow-wait-document-list">
        {documents.map((document) => <div key={document.document_id} className="workflow-wait-document-row">
          <span aria-hidden="true"><IconDocument size={16} /></span>
          <span title={document.name || document.document_id}>{document.name || document.document_id}</span>
          <button type="button" disabled={disabled || busy} aria-label={t("component.workflow_wait_document.remove_named", { name: document.name || document.document_id })} onClick={() => onChange(documents.filter((item) => item.document_id !== document.document_id))}>{t("component.workflow_wait_document.remove")}</button>
        </div>)}
      </div>}
      <div className="workflow-wait-document-actions">
        <button type="button" disabled={disabled || busy} onClick={() => inputRef.current?.click()}><span aria-hidden="true"><IconUpload size={16} /></span> {t("component.workflow_file.upload_local")}</button>
        <button ref={buttonRef} type="button" disabled={disabled || busy} onClick={() => setPickerOpen(true)}><span aria-hidden="true"><IconFolder size={16} /></span> {t("component.workflow_file.choose_knowledge")}</button>
      </div>
    </div>
    <input id={id} ref={inputRef} type="file" multiple hidden onChange={(event) => {
      const pending = uploads.concat(Array.from(event.target.files || []).map((file) => ({ file, uploadKey: crypto.randomUUID() })));
      event.target.value = "";
      void add(pendingSelected, pending);
    }} />
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
    }} onClose={() => setPickerOpen(false)} restoreFocus={() => buttonRef.current?.focus()} />}
  </div>;
}
