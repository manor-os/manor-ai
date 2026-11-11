import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { Document } from "../../lib/types";
import { IconFolder, IconUpload } from "../icons";
import Button from "../ui/Button";
import HitlKnowledgePicker from "../ui/HitlKnowledgePicker";
import { PageLoading } from "../ui/Skeleton";

export default function WorkspaceAppSetupMaterials({ workspaceId, checkKey, acceptsText, canStart }: {
  workspaceId: string;
  checkKey: string;
  acceptsText: boolean;
  canStart: boolean;
}) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const pickerButton = useRef<HTMLButtonElement>(null);
  const uploaded = useRef(new Map<File, Document>());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [files, setFiles] = useState<File[]>([]);
  const [answer, setAnswer] = useState("");
  const [savedIncomplete, setSavedIncomplete] = useState(false);
  const workspace = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => api.workspaces.get(workspaceId),
  });
  const groups = useQuery({
    queryKey: ["workspace-documents", workspaceId],
    queryFn: () => api.workspaces.documents(workspaceId),
  });
  const canManage = workspace.data?.can_manage === true;
  const save = useMutation({
    mutationFn: async () => {
      const chosen = [...documents];
      for (const file of files) {
        let document = uploaded.current.get(file);
        if (!document) {
          document = await api.documents.upload(file, undefined, { visibility: "private" });
          uploaded.current.set(file, document);
        }
        chosen.push(document);
      }
      const ids = [...new Set(chosen.map((document) => document.id))];
      if (ids.length) {
        const available = await api.workspaces.documents(workspaceId);
        let group = available.find((item) => item.is_default_collection);
        if (!group) {
          group = await api.workspaces.knowledge.createGroup(workspaceId, {
            name: "Workspace Knowledge",
            kind: "workspace_collection",
          });
        }
        await api.workspaces.knowledge.addDocuments(workspaceId, group.id, ids);
        const refreshed = await api.workspaces.documents(workspaceId);
        const attached = new Set((refreshed.find((item) => item.id === group.id)?.documents || []).map((document: Document) => document.id));
        if (ids.some((id) => !attached.has(id))) {
          throw new Error(t("component.workspace_app_view.setup_file_access_error"));
        }
      }
      if (acceptsText && answer.trim()) await api.workspaces.submitSetupInput(workspaceId, checkKey, answer.trim());
      return api.workspaces.setupStatus(workspaceId);
    },
    onSuccess: (status) => {
      queryClient.setQueryData(["workspace-setup-status", workspaceId], status);
      // Each Workflow page reads its own copy (whether that Workflow may start); refresh them too.
      void queryClient.invalidateQueries({
        queryKey: ["workspace-setup-status", workspaceId],
        predicate: (query) => query.queryKey.length > 2,
      });
      void queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspaceId] });
      setSavedIncomplete(!status.ready);
      setDocuments([]);
      setFiles([]);
      setAnswer("");
    },
  });
  const hasSelection = documents.length > 0 || files.length > 0;
  const existingFiles = (groups.data?.find((group) => group.is_default_collection)?.documents || []) as Document[];

  return <div className="workspace-app-setup">
    {existingFiles.length > 0 && <p>{t("component.workspace_app_view.setup_existing_files")}: {existingFiles.slice(0, 3).map((document) => document.name).join(", ")}{existingFiles.length > 3 ? ` +${existingFiles.length - 3}` : ""}</p>}
    {workspace.isPending ? <PageLoading label={t("status.loading")} minHeight={96} /> : workspace.isError ? <div><p role="alert">{t("component.workspace_app_view.setup_check_failed")}</p><Button variant="ghost" onClick={() => void workspace.refetch()}>{t("component.workspace_app_view.check_again")}</Button></div> : !canManage ? <p>{t("component.workspace_app_view.setup_owner_needed")}</p> : <>
      <div className="workspace-app-setup__actions">
        <button ref={pickerButton} type="button" onClick={() => setPickerOpen(true)} disabled={!canStart || save.isPending}><IconFolder size={16} />{t("component.workflow_file.choose_knowledge")}</button>
        <button type="button" onClick={() => fileInput.current?.click()} disabled={!canStart || save.isPending}><IconUpload size={16} />{t("component.workflow_file.upload_local")}</button>
      </div>
      <input ref={fileInput} type="file" multiple hidden onChange={(event) => {
        const nextFiles = Array.from(event.currentTarget.files || []);
        setFiles((previous) => [...previous, ...nextFiles]);
        event.currentTarget.value = "";
        setSavedIncomplete(false);
      }} />
      {hasSelection && <div className="workspace-app-setup__selected" aria-live="polite">
        {documents.map((document) => <span key={document.id}>{document.name}<button type="button" onClick={() => setDocuments((previous) => previous.filter((item) => item.id !== document.id))} disabled={save.isPending} aria-label={`${t("component.workflow_file.remove")}: ${document.name}`}>×</button></span>)}
        {files.map((file, index) => <span key={`${file.name}-${index}`}>{file.name}<button type="button" onClick={() => setFiles((previous) => previous.filter((_, fileIndex) => fileIndex !== index))} disabled={save.isPending} aria-label={`${t("component.workflow_file.remove")}: ${file.name}`}>×</button></span>)}
      </div>}
      {acceptsText && <label className="workspace-app-setup__answer">
        <span>{t("component.workspace_app_view.setup_answer_label")}</span>
        <textarea className="manor-input" rows={3} value={answer} onChange={(event) => { setAnswer(event.target.value); setSavedIncomplete(false); }} placeholder={t("component.workspace_app_view.setup_answer_placeholder")} disabled={!canStart || save.isPending} />
      </label>}
      {savedIncomplete && <p role="status">{t("component.workspace_app_view.setup_still_missing")}</p>}
      {save.isError && <p role="alert" className="workspace-app-flow__error">{save.error instanceof Error ? save.error.message : t("component.workspace_app_view.setup_save_error")}</p>}
      <div className="workspace-app-flow__buttons">
        <Button variant="primary" onClick={() => save.mutate()} disabled={!canStart || save.isPending || (!hasSelection && !(acceptsText && answer.trim()))} loading={save.isPending}>{t("component.workspace_app_view.setup_save_check")}</Button>
      </div>
      {pickerOpen && <HitlKnowledgePicker
        workspaceId={workspaceId}
        selectedIds={new Set(documents.map((document) => document.id))}
        onToggle={(document) => {
          setDocuments((previous) => previous.some((item) => item.id === document.id)
            ? previous.filter((item) => item.id !== document.id)
            : [...previous, document]);
          setSavedIncomplete(false);
        }}
        onClose={() => setPickerOpen(false)}
        restoreFocus={() => pickerButton.current?.focus()}
      />}
    </>}
  </div>;
}
