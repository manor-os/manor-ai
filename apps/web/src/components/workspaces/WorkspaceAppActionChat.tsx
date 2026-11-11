import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { WorkspaceAppAction, WorkspaceAppFormField } from "../../lib/api";
import type { ChatMessage } from "../../lib/chatStream";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import { t } from "../../lib/i18n";
import type { WorkflowDocumentRef } from "../../lib/workflowRunInputs";
import WorkflowWaitDocumentField from "../workflows/WorkflowWaitDocumentField";
import ConfiguredImage from "../ui/ConfiguredImage";
import { IconArrowLeft, IconArrowRight, IconChatBubble } from "../icons";
import ChatMarkdown from "../ChatMarkdown";
import AssistantReply from "../AssistantReply";
import Button from "../ui/Button";
import Select from "../ui/Select";
import { fillSuggestionOnTab } from "../../lib/tabFillSuggestion";

export type WorkspaceAppChatResult = {
  content: string;
  files: Array<{ id: string; name: string }>;
};

/** The chat's latest reply: the one this entry's request is getting, live while it streams. */
export type WorkspaceAppLiveReply = { message: ChatMessage; streaming: boolean };

/** The label a picture choice shows for its stored option value. */
function pictureChoiceLabel(field: WorkspaceAppFormField, optionValue: string): string {
  const index = field.options.indexOf(optionValue);
  return index >= 0 ? field.option_labels?.[index] || optionValue : optionValue;
}

/**
 * One picture per option, picked by looking at it. The pictures are the page's own
 * assets; the chosen option's value is what the answer carries, as for a select.
 */
function PictureChoiceField({ field, value, disabled, invalid, onChange }: {
  field: WorkspaceAppFormField;
  value: string;
  disabled: boolean;
  invalid: boolean;
  onChange: (value: string) => void;
}) {
  const labelId = `app-action-${field.key}-label`;
  return <div className="workflow-starter-input-field workflow-starter-input-field--suggested">
    <span id={labelId} className="workspace-app-flow__request">{field.label}{field.required ? " *" : ""}</span>
    <div className="workflow-starter-input-suggestions" data-pictured role="radiogroup" aria-labelledby={labelId}
      aria-required={field.required} aria-invalid={invalid}>
      {field.options.map((option, index) => <button
        key={option}
        type="button"
        role="radio"
        className="workflow-starter-input-suggestion"
        aria-checked={value === option}
        disabled={disabled}
        onClick={() => onChange(option)}
      >
        <ConfiguredImage src={field.option_images?.[index] || ""} alt="" loading="lazy" decoding="async" />
        <strong>{field.option_labels?.[index] || option}</strong>
      </button>)}
    </div>
  </div>;
}

export default function WorkspaceAppActionChat({
  action, workspaceId, canStart, onBack, onRun, live,
}: {
  action: WorkspaceAppAction;
  workspaceId: string;
  canStart: boolean;
  onBack: () => void;
  onRun: (request: string, documents: WorkflowDocumentRef[]) => Promise<WorkspaceAppChatResult>;
  live?: WorkspaceAppLiveReply | null;
}) {
  const configured = (action.inputs || []) as WorkspaceAppFormField[];
  const fields: WorkspaceAppFormField[] = configured.length ? configured : [{ key: "request", label: t("component.workspace_app_view.tell_us"), type: "long_text", required: false, placeholder: action.description || action.title, options: [] }];
  // A file field holds Knowledge documents, picked or uploaded like every other App form.
  const [values, setValues] = useState<Record<string, string | WorkflowDocumentRef[]>>({});
  const [step, setStep] = useState(0);
  const [stage, setStage] = useState<"input" | "review" | "running" | "result">("input");
  const [result, setResult] = useState<WorkspaceAppChatResult | null>(null);
  const [error, setError] = useState("");
  // The finished reply, kept as it was when the request completed.
  const [finalReply, setFinalReply] = useState<ChatMessage | null>(null);
  // The reply that was already there before this request, never taken for this request's answer.
  const [earlierReplyId, setEarlierReplyId] = useState<string | undefined>();
  useEffect(() => {
    if (stage === "result" && !finalReply && live && !live.streaming && live.message.id !== earlierReplyId) {
      setFinalReply(live.message);
    }
  }, [stage, finalReply, live, earlierReplyId]);
  const current = fields[step];
  const value = current ? values[current.key] : null;
  // With no form of its own, the entry's description is the request it was made for.
  // A configured field's placeholder may only describe what to write, so it is never filled in.
  const suggestion = configured.length ? undefined : current?.placeholder;
  const display = (field: WorkspaceAppFormField) => {
    const item = values[field.key];
    if (Array.isArray(item)) return item.map((document) => document.name || document.document_id).join(", ");
    const text = String(item || "").trim();
    return field.type === "picture_choice" ? pictureChoiceLabel(field, text) : text;
  };
  const next = () => {
    if (current.required && !display(current)) { setError(t("component.workspace_app_view.required_hint")); return; }
    setError("");
    if (step + 1 < fields.length) setStep(step + 1);
    else setStage("review");
  };
  const run = async () => {
    if (!canStart || stage === "running") return;
    setError("");
    setEarlierReplyId(live?.message.id);
    setStage("running");
    try {
      const answers = fields.filter((field) => display(field)).map((field) => `${field.label}: ${display(field)}`);
      const documents = fields.flatMap((field) => { const item = values[field.key]; return Array.isArray(item) ? item : []; });
      setResult(await onRun(answers.join("\n"), documents));
      setStage("result");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("component.workspace_app_view.start_error"));
      setStage("review");
    }
  };
  const files = result?.files.flatMap((file) => {
    const path = viewerPathForDocumentId(file.id);
    return path ? [{ ...file, path }] : [];
  }) || [];
  return <div className="workspace-app-flow">
    <button type="button" className="workspace-app-flow__back" onClick={onBack}><IconArrowLeft size={16} />{t("component.workspace_app_view.all_actions")}</button>
    <span className="workspace-app-flow__mark" aria-hidden="true"><IconChatBubble size={22} /></span>
    <h2>{action.title}</h2>
    {action.description && <p className="workspace-app-flow__intro">{action.description}</p>}
    <div className="workspace-app-flow__steps" aria-label={t("component.workspace_app_view.progress")}>
      <span className={stage === "input" ? "is-current" : "is-complete"}>{t("component.workspace_app_view.step_input")}</span>
      <span className={stage === "review" ? "is-current" : stage === "result" ? "is-complete" : ""}>{t("component.workspace_app_view.step_review")}</span>
      <span className={stage === "running" || stage === "result" ? "is-current" : ""}>{t("component.workspace_app_view.step_result")}</span>
    </div>
    {stage === "input" && <div className="workspace-app-flow__panel">
      <span className="workspace-app-flow__kicker">{step + 1} / {fields.length}</span>
      {current.type === "picture_choice" ? <PictureChoiceField
        field={current}
        value={typeof value === "string" ? value : ""}
        disabled={!canStart}
        invalid={Boolean(error)}
        onChange={(nextValue) => { setValues((previous) => ({ ...previous, [current.key]: nextValue })); setError(""); }}
      /> : <label className="workspace-app-flow__request">{current.label}{current.required ? " *" : ""}
        {current.type === "file" ? <WorkflowWaitDocumentField id={`app-action-${current.key}`} value={value} workspaceId={workspaceId} disabled={!canStart} onChange={(documents) => { setValues((previous) => ({ ...previous, [current.key]: documents })); setError(""); }} />
          : current.type === "long_text" ? <textarea className="manor-input" value={typeof value === "string" ? value : ""} onKeyDown={(event) => fillSuggestionOnTab(event, typeof value === "string" ? value : "", suggestion, (text) => setValues((previous) => ({ ...previous, [current.key]: text })))} onChange={(event) => setValues((previous) => ({ ...previous, [current.key]: event.target.value }))} rows={5} placeholder={current.placeholder} disabled={!canStart} />
          : current.type === "select" ? <Select
            role="combobox"
            value={typeof value === "string" ? value : ""}
            onChange={(nextValue) => { setValues((previous) => ({ ...previous, [current.key]: nextValue })); setError(""); }}
            disabled={!canStart}
            ariaLabel={`${current.label}${current.required ? " *" : ""}`}
            ariaRequired={current.required}
            ariaInvalid={Boolean(error)}
            placeholder={t("component.workspace_app_view.select_one")}
            options={[...(!current.required ? [{ value: "", label: t("component.workspace_app_view.select_one") }] : []), ...current.options.map((option) => ({ value: option, label: option }))]}
          />
          : <input className="manor-input" type={current.type === "number" ? "number" : "text"} value={typeof value === "string" ? value : ""} onChange={(event) => setValues((previous) => ({ ...previous, [current.key]: event.target.value }))} placeholder={current.placeholder} disabled={!canStart} />}
      </label>}
      {suggestion && !value && <p className="workspace-app-flow__tab-hint">{t("component.workspace_app_view.tab_to_fill")}</p>}
      {error && <p role="alert" className="workspace-app-flow__error">{error}</p>}
      <div className="workspace-app-flow__buttons">
        {step > 0 && <Button variant="ghost" onClick={() => { setStep(step - 1); setError(""); }}>{t("component.workspace_app_view.back")}</Button>}
        <Button variant="primary" onClick={next} disabled={!canStart}>{t("component.workspace_app_view.next")} <IconArrowRight size={15} /></Button>
      </div>
    </div>}
    {stage === "review" && <div className="workspace-app-flow__panel">
      <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_review")}</span>
      <h3>{t("component.workspace_app_view.ready_to_run")}</h3>
      <dl className="workspace-app-flow__review">{fields.map((field) => <div key={field.key}><dt>{field.label}</dt><dd>{display(field) || "—"}</dd></div>)}</dl>
      {error && <p role="alert" className="workspace-app-flow__error">{error}</p>}
      <div className="workspace-app-flow__buttons"><Button variant="ghost" onClick={() => { setStep(fields.length - 1); setStage("input"); }}>{t("component.workspace_app_view.back")}</Button><Button variant="primary" onClick={() => void run()} disabled={!canStart}>{t("component.workspace_app_view.run")}</Button></div>
    </div>}
    {stage === "running" && <div className="workspace-app-flow__panel" role="status"><span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_result")}</span><h3>{t("component.workspace_app_view.running")}</h3>
      {/* The App is where this request was made, so its work shows here as the chat bubble shows it. */}
      {live?.streaming ? <div className="workspace-app-flow__reply"><AssistantReply message={live.message} streaming keyPrefix={`app-${action.key}`} /></div>
        : <p>{t("component.workspace_app_view.chat_running_hint")}</p>}
    </div>}
    {stage === "result" && <div className="workspace-app-flow__panel">
      <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_result")}</span>
      <h3>{t("component.workspace_app_view.latest_reply")}</h3>
      {finalReply ? <div className="workspace-app-flow__reply"><AssistantReply message={finalReply} streaming={false} keyPrefix={`app-${action.key}`} /></div>
        : result?.content ? <div className="workspace-app-flow__reply"><ChatMarkdown content={result.content} /></div> : <p>{t("component.workspace_app_view.chat_result_hint")}</p>}
      {files.length > 0 && <section><h4>{t("component.workspace_app_view.files")}</h4><div className="workspace-app-flow__files">{files.map((file) => <Link to={file.path} key={file.id}>{file.name}<IconArrowRight size={15} /></Link>)}</div></section>}
      <div className="workspace-app-flow__buttons"><Button variant="ghost" onClick={() => { setResult(null); setFinalReply(null); setStage("input"); setStep(0); }}>{t("component.workspace_app_view.run_again")}</Button></div>
    </div>}
  </div>;
}
