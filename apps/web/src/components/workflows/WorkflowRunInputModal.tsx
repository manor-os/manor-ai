import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import type { Document } from "../../lib/types";
import { linkWorkflowDocuments, prepareWorkflowDocuments, workflowRunInputChosen, workflowRunInputs, workflowUploadFolderId, type WorkflowDocumentRef, type WorkflowUpload } from "../../lib/workflowRunInputs";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Textarea from "../ui/Textarea";
import Select from "../ui/Select";
import Modal from "../ui/Modal";
import HitlKnowledgePicker from "../ui/HitlKnowledgePicker";
import WorkflowRecordSelect from "./WorkflowRecordSelect";
import WorkflowUploadProgress, { type WorkflowUploadStatus } from "./WorkflowUploadProgress";

type RunInputFlow = Parameters<typeof workflowRunInputs>[0] & { id?: string; name: string };

function documentRef(document: Document): WorkflowDocumentRef {
  return { document_id: document.id, id: document.id, name: document.name, type: "knowledge", viewer_url: `/viewer/${document.id}`, fs_path: document.fs_path };
}

export default function WorkflowRunInputModal({
  flow: runInputFlow, workspaceId, initialValues = null, busy: streaming = false, onClose, onSubmit,
}: {
  flow: RunInputFlow | null;
  workspaceId?: string | null;
  /** Values from an earlier run, shown instead of the defaults ("run again"). */
  initialValues?: Record<string, unknown> | null;
  busy?: boolean;
  onClose: () => void;
  onSubmit: (triggerData: Record<string, unknown>) => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const [runInputValues, setRunInputValues] = useState<Record<string, string>>({});
  const [runInputErrors, setRunInputErrors] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<Record<string, WorkflowDocumentRef[]>>({});
  const [uploads, setUploads] = useState<Record<string, WorkflowUpload[]>>({});
  const [pickerKey, setPickerKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [progress, setProgress] = useState("");
  const [uploadStatuses, setUploadStatuses] = useState<Record<string, WorkflowUploadStatus>>({});
  const [submitError, setSubmitError] = useState("");
  const fileInputs = useRef<Record<string, HTMLInputElement | null>>({});
  const pickerButtons = useRef<Record<string, HTMLButtonElement | null>>({});
  const busy = streaming || submitting;
  useEffect(() => {
    setRunInputValues(runInputFlow
      ? Object.fromEntries(workflowRunInputs(runInputFlow).map((input) => {
        const previous = input.documents ? undefined : initialValues?.[input.key];
        const value = previous === undefined || previous === null ? input.defaultValue
          : typeof previous === "string" ? previous
            : input.type === "json" ? JSON.stringify(previous, null, 2) : String(previous);
        return [input.key, input.options && !input.options.some((option) => option.value === value) ? "" : value];
      }))
      : {});
    setRunInputErrors({});
    setSelected({});
    setUploads({});
    setPickerKey(null);
    setSubmitError("");
    setProgress("");
    setUploadStatuses({});
  }, [runInputFlow?.id, runInputFlow?.name, initialValues]);
  const submitWorkflowRun = async () => {
    if (!runInputFlow || busy || submittingRef.current) return;
    const inputs = workflowRunInputs(runInputFlow);
    const errors: Record<string, string> = {};
    const triggerData: Record<string, unknown> = {};
    for (const input of inputs) {
      if (input.documents) {
        if (input.required && !(selected[input.key]?.length || uploads[input.key]?.length)) {
          errors[input.key] = "Upload a file or choose one from Knowledge.";
        }
        continue;
      }
      const value = runInputValues[input.key] ?? "";
      const picksMany = Boolean(input.choices || input.records?.multiple);
      if (!value.trim() || (picksMany && !workflowRunInputChosen(value).length)) {
        if (input.required) errors[input.key] = picksMany ? "Choose at least one option." : "Provide a value before running this workflow.";
        continue;
      }
      if (input.type === "number") {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) errors[input.key] = "Enter a valid number.";
        else if (input.integer && !Number.isInteger(parsed)) errors[input.key] = "Enter a whole number.";
        else if (input.minimum !== undefined && parsed < input.minimum) errors[input.key] = `Enter ${input.minimum} or more.`;
        else if (input.maximum !== undefined && parsed > input.maximum) errors[input.key] = `Enter ${input.maximum} or less.`;
        else triggerData[input.key] = parsed;
      } else if (input.type === "boolean") {
        triggerData[input.key] = value === "true";
      } else if (input.type === "json") {
        try { triggerData[input.key] = JSON.parse(value); }
        catch { errors[input.key] = "Enter valid JSON."; }
      } else {
        triggerData[input.key] = value;
      }
    }
    setRunInputErrors(errors);
    if (Object.keys(errors).length) return;
    submittingRef.current = true;
    setSubmitting(true);
    setSubmitError("");
    setUploadStatuses(Object.fromEntries(Object.values(uploads).flat().map((item) => [item.uploadKey, {
      phase: item.document ? "uploaded" : "queued",
    }])));
    try {
      const allDocuments: WorkflowDocumentRef[] = [];
      for (const input of inputs.filter((item) => item.documents)) {
        const uploadFolderId = (uploads[input.key] || []).some((item) => !item.document)
          ? await workflowUploadFolderId(workspaceId, api.workspaces.get)
          : null;
        const documents = await prepareWorkflowDocuments(selected[input.key] || [], uploads[input.key] || [], async (item) => {
          setUploadStatuses((current) => ({ ...current, [item.uploadKey]: { phase: "uploading", percent: 0 } }));
          try {
            const document = await api.documents.upload(item.file, uploadFolderId, {
              visibility: "private", classification: "internal", client_visible: false,
            }, {
              idempotencyKey: item.uploadKey,
              onProgress: (status) => setUploadStatuses((current) => ({ ...current, [item.uploadKey]: status })),
            });
            setUploadStatuses((current) => ({ ...current, [item.uploadKey]: { phase: "uploaded" } }));
            void queryClient.invalidateQueries({ queryKey: ["documents"] });
            return documentRef(document);
          } catch (cause) {
            setUploadStatuses((current) => ({ ...current, [item.uploadKey]: {
              phase: "failed",
              error: translateApiError(cause, "Upload failed."),
            } }));
            throw cause;
          }
        });
        triggerData[input.key] = documents;
        allDocuments.push(...documents);
      }
      if (workspaceId && allDocuments.length) {
        setProgress("Adding materials to this Workspace…");
        await linkWorkflowDocuments(allDocuments,
          () => api.workspaces.documents(workspaceId),
          (groupId, ids) => api.workspaces.knowledge.addDocuments(workspaceId, groupId, ids));
        void queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspaceId] });
      }
      setProgress("Starting workflow…");
      await onSubmit(triggerData);
    } catch (error) {
      setSubmitError(translateApiError(error, "Couldn't start the workflow. Your inputs and uploaded files are kept here; try again."));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
      setProgress("");
    }
  };

  return (
    <>
    <Modal
      open={!!runInputFlow}
      onClose={() => { if (!busy) onClose(); }}
      title={runInputFlow ? `Run ${runInputFlow.name}` : "Run workflow"}
      maxWidth="560px"
      footer={(
        <>
          <Button variant="ghost" onClick={() => onClose()} disabled={busy}>Cancel</Button>
          <Button onClick={() => void submitWorkflowRun()} loading={busy}>Run workflow</Button>
        </>
      )}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13, lineHeight: 1.55 }}>
          Provide the information and materials for this run.
        </p>
        {submitError && <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>{submitError}</p>}
        {progress && <p role="status" style={{ margin: 0 }}>{progress}</p>}
        <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>
        {runInputFlow && workflowRunInputs(runInputFlow).map((input, index) => (
          <div key={input.key} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
              <label htmlFor={`workflow-run-input-${index}`} style={{ fontSize: 12.5, fontWeight: 700, color: "var(--text-strong)" }}>
                {input.label}{input.required ? " *" : ""}
              </label>
              {!input.documents && !input.choices && !input.records && <span style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 10.5, color: "var(--text-faint)" }}>{input.type}</span>}
            </div>
            {input.documents ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <input type="file" multiple hidden aria-label={`Upload files for ${input.label}`}
                  ref={(element) => { fileInputs.current[input.key] = element; }}
                  onChange={(event) => {
                    const files = Array.from(event.target.files || []);
                    setUploads((current) => {
                      const pending = [...(current[input.key] || [])];
                      for (const file of files) {
                        if (!pending.some((item) => item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified)) {
                          pending.push({ file, uploadKey: crypto.randomUUID() });
                        }
                      }
                      return { ...current, [input.key]: pending };
                    });
                    setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                    event.target.value = "";
                  }} />
                <div style={{ display: "flex", gap: 8 }}>
                  <Button variant="outline" onClick={() => fileInputs.current[input.key]?.click()}>Upload files</Button>
                  <button className="btn-manor-outline" type="button" ref={(element) => { pickerButtons.current[input.key] = element; }}
                    onClick={() => setPickerKey(input.key)}>Choose from Knowledge</button>
                </div>
                <small style={{ color: "var(--text-muted)" }}>
                  Files upload when you run. Saved files remain in Knowledge{workspaceId ? " and are added to this Workspace" : ""}.
                </small>
                {(selected[input.key] || []).map((document) => (
                  <div key={document.document_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ overflowWrap: "anywhere" }}>{document.name || document.document_id} · Knowledge</span>
                    <Button variant="ghost" size="sm" ariaLabel={`Remove ${document.name || document.document_id}`}
                      onClick={() => setSelected((current) => ({ ...current, [input.key]: current[input.key].filter((item) => item.document_id !== document.document_id) }))}>Remove</Button>
                  </div>
                ))}
                {(uploads[input.key] || []).map((item) => (
                  <WorkflowUploadProgress key={item.uploadKey} file={item.file}
                    status={uploadStatuses[item.uploadKey] || { phase: item.document ? "uploaded" : "queued" }}>
                    <Button variant="ghost" size="sm" ariaLabel={`Remove ${item.file.name}`}
                      onClick={() => setUploads((current) => ({ ...current, [input.key]: current[input.key].filter((other) => other !== item) }))}>Remove</Button>
                  </WorkflowUploadProgress>
                ))}
                {runInputErrors[input.key] && <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>{runInputErrors[input.key]}</p>}
              </div>
            ) : input.records ? (
              <div role="group" aria-label={input.label} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <WorkflowRecordSelect
                  id={`workflow-run-input-${index}`}
                  multiple={input.records.multiple}
                  inputKey={input.key}
                  value={input.records.multiple
                    ? workflowRunInputChosen(runInputValues[input.key] ?? "")
                    : runInputValues[input.key] ?? ""}
                  workflowId={runInputFlow?.id}
                  workspaceId={workspaceId || undefined}
                  disabled={busy}
                  required={input.required}
                  invalid={Boolean(runInputErrors[input.key])}
                  label={`${input.label}${input.required ? " *" : ""}`}
                  onChange={(next) => {
                    const multiple = input.records?.multiple;
                    setRunInputValues((current) => ({
                      ...current,
                      [input.key]: multiple ? JSON.stringify(next.split("\n").filter(Boolean)) : next,
                    }));
                    setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                  }}
                />
                {runInputErrors[input.key] && <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>{runInputErrors[input.key]}</p>}
              </div>
            ) : input.choices ? (
              <div role="group" aria-label={input.label} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 14px" }}>
                  {input.choices.map((option, optionIndex) => {
                    const chosen = workflowRunInputChosen(runInputValues[input.key] ?? "");
                    return (
                      <label key={option.value} style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        <input
                          id={optionIndex === 0 ? `workflow-run-input-${index}` : undefined}
                          type="checkbox"
                          checked={chosen.includes(option.value)}
                          onChange={(event) => {
                            const next = new Set(chosen);
                            if (event.target.checked) next.add(option.value);
                            else next.delete(option.value);
                            const ordered = input.choices!.map((choice) => choice.value).filter((choice) => next.has(choice));
                            setRunInputValues((current) => ({ ...current, [input.key]: JSON.stringify(ordered) }));
                            setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                          }}
                          className="workflow-starter-input-checkbox"
                        />
                        <span>{option.label}</span>
                      </label>
                    );
                  })}
                </div>
                {runInputErrors[input.key] && <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>{runInputErrors[input.key]}</p>}
              </div>
            ) : input.type === "json" ? (
              <Textarea
                value={runInputValues[input.key] ?? ""}
                onChange={(event) => {
                  setRunInputValues((current) => ({ ...current, [input.key]: event.target.value }));
                  setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                }}
                rows={4}
                placeholder={input.placeholder || "Enter a JSON value"}
                error={runInputErrors[input.key]}
                ariaLabel={`Run value for ${input.label}`}
              />
            ) : input.options && input.options.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <select
                  id={`workflow-run-input-${index}`}
                  value={runInputValues[input.key] ?? ""}
                  onChange={(event) => {
                    setRunInputValues((current) => ({ ...current, [input.key]: event.target.value }));
                    setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                  }}
                  className="manor-input"
                  required={input.required}
                  aria-required={input.required}
                  aria-invalid={Boolean(runInputErrors[input.key])}
                  aria-describedby={runInputErrors[input.key] ? `workflow-run-input-${index}-error` : undefined}
                  aria-label={`Run value for ${input.label}`}
                >
                  <option value="" disabled={input.required}>{input.required ? "Select…" : "—"}</option>
                  {input.options.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
                {runInputErrors[input.key] && (
                  <p id={`workflow-run-input-${index}-error`} role="alert" style={{ margin: 0, color: "var(--danger)" }}>
                    {runInputErrors[input.key]}
                  </p>
                )}
              </div>
            ) : input.type === "boolean" ? (
              <Select
                id={`workflow-run-input-${index}`}
                role="combobox"
                value={runInputValues[input.key] || "false"}
                onChange={(value) => setRunInputValues((current) => ({ ...current, [input.key]: value }))}
                ariaLabel={`Run value for ${input.label}`}
                options={[{ value: "false", label: "False" }, { value: "true", label: "True" }]}
              />
            ) : (
              <Input
                value={runInputValues[input.key] ?? ""}
                onChange={(event) => {
                  setRunInputValues((current) => ({ ...current, [input.key]: event.target.value }));
                  setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                }}
                type={input.type === "number" ? "number" : "text"}
                min={input.minimum}
                max={input.maximum}
                step={input.integer ? 1 : undefined}
                placeholder={input.placeholder || "Enter a value"}
                error={runInputErrors[input.key]}
                autoFocus={index === 0}
                ariaLabel={`Run value for ${input.label}`}
              />
            )}
          </div>
        ))}
        </fieldset>
      </div>
    </Modal>
    {pickerKey && <HitlKnowledgePicker
      selectedIds={new Set((selected[pickerKey] || []).map((document) => document.document_id))}
      onToggle={(document) => setSelected((current) => {
        const items = current[pickerKey] || [];
        return { ...current, [pickerKey]: items.some((item) => item.document_id === document.id)
          ? items.filter((item) => item.document_id !== document.id) : [...items, documentRef(document)] };
      })}
      onClose={() => setPickerKey(null)}
      restoreFocus={() => pickerButtons.current[pickerKey]?.focus()}
      workspaceId={workspaceId || undefined}
    />}
    </>
  );
}
