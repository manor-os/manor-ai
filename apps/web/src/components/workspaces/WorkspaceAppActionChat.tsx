import { useState } from "react";
import { Link } from "react-router-dom";
import type { WorkspaceAppAction, WorkspaceAppFormField } from "../../lib/api";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import { t } from "../../lib/i18n";
import { IconArrowLeft, IconArrowRight, IconChatBubble } from "../icons";
import ChatMarkdown from "../ChatMarkdown";
import Button from "../ui/Button";
import Select from "../ui/Select";

export type WorkspaceAppChatResult = {
  content: string;
  files: Array<{ id: string; name: string }>;
};

export default function WorkspaceAppActionChat({
  action, canStart, onBack, onRun,
}: {
  action: WorkspaceAppAction;
  canStart: boolean;
  onBack: () => void;
  onRun: (request: string, files: File[]) => Promise<WorkspaceAppChatResult>;
}) {
  const configured = (action.inputs || []) as WorkspaceAppFormField[];
  const fields: WorkspaceAppFormField[] = configured.length ? configured : [{ key: "request", label: t("component.workspace_app_view.tell_us"), type: "long_text", required: false, placeholder: action.description || action.title, options: [] }];
  const [values, setValues] = useState<Record<string, string | File | null>>({});
  const [step, setStep] = useState(0);
  const [stage, setStage] = useState<"input" | "review" | "running" | "result">("input");
  const [result, setResult] = useState<WorkspaceAppChatResult | null>(null);
  const [error, setError] = useState("");
  const current = fields[step];
  const value = current ? values[current.key] : null;
  const display = (field: WorkspaceAppFormField) => {
    const item = values[field.key];
    return item instanceof File ? item.name : String(item || "").trim();
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
    setStage("running");
    try {
      const answers = fields.filter((field) => display(field)).map((field) => `${field.label}: ${display(field)}`);
      const files = fields.flatMap((field) => values[field.key] instanceof File ? [values[field.key] as File] : []);
      setResult(await onRun(answers.join("\n"), files));
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
      <label className="workspace-app-flow__request">{current.label}{current.required ? " *" : ""}
        {current.type === "file" ? <input type="file" onChange={(event) => setValues((previous) => ({ ...previous, [current.key]: event.target.files?.[0] || null }))} disabled={!canStart} />
          : current.type === "long_text" ? <textarea className="manor-input" value={typeof value === "string" ? value : ""} onChange={(event) => setValues((previous) => ({ ...previous, [current.key]: event.target.value }))} rows={5} placeholder={current.placeholder} disabled={!canStart} />
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
      </label>
      {current.type === "file" && value instanceof File && <p>{value.name}</p>}
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
    {stage === "running" && <div className="workspace-app-flow__panel" role="status"><span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_result")}</span><h3>{t("component.workspace_app_view.running")}</h3><p>{t("component.workspace_app_view.chat_running_hint")}</p></div>}
    {stage === "result" && <div className="workspace-app-flow__panel">
      <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_result")}</span>
      <h3>{t("component.workspace_app_view.latest_reply")}</h3>
      {result?.content ? <div className="workspace-app-flow__reply"><ChatMarkdown content={result.content} /></div> : <p>{t("component.workspace_app_view.chat_result_hint")}</p>}
      {files.length > 0 && <section><h4>{t("component.workspace_app_view.files")}</h4><div className="workspace-app-flow__files">{files.map((file) => <Link to={file.path} key={file.id}>{file.name}<IconArrowRight size={15} /></Link>)}</div></section>}
      <div className="workspace-app-flow__buttons"><Button variant="ghost" onClick={() => { setResult(null); setStage("input"); setStep(0); }}>{t("component.workspace_app_view.run_again")}</Button></div>
    </div>}
  </div>;
}
