import { useEffect, useRef, useState, type ReactNode } from "react";
import Button from "../ui/Button";
import GlassCard from "../ui/GlassCard";
import Input from "../ui/Input";
import { tForLocale } from "../../lib/i18n";
import { safeWebchatUrl, validWebchatPage, webchatModuleVisible, type WebchatModule, type WebchatSide } from "../../lib/webchatPage";
import "./webchatPage.css";

export type WebchatText = (key: string) => string;

function PublicImage({ url, alt, logo = false }: { url: string; alt: string; logo?: boolean }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  const src = safeWebchatUrl(url);
  if (!src || failed) return null;
  return <img src={src} alt={alt} className={logo ? "webchat-page-logo" : "webchat-page-image"} loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />;
}

function IntakeForm({ module, text, onPrepareMessage }: { module: Extract<WebchatModule, { type: "form" }>; text: WebchatText; onPrepareMessage?: (message: string) => void }) {
  const [values, setValues] = useState<Record<number, string>>({});
  const [prepared, setPrepared] = useState(false);
  return <form className="webchat-page-form" onSubmit={event => {
    event.preventDefault();
    if (!onPrepareMessage) return;
    const lines = module.fields.map((field, index) => `${field}: ${(values[index] || "").trim()}`);
    onPrepareMessage([module.title, ...lines].filter(Boolean).join("\n"));
    setPrepared(true);
  }}>
    {module.fields.map((field, index) => <Input key={index} label={field} value={values[index] || ""} maxLength={1000} required disabled={!onPrepareMessage}
      onChange={event => { setValues(previous => ({ ...previous, [index]: event.target.value })); setPrepared(false); }} />)}
    <Button type="submit" size="sm" disabled={!onPrepareMessage || module.fields.length === 0}>{text("webchat_page.prepare_message")}</Button>
    <p className="webchat-page-hint" role="status">{text(prepared ? "webchat_page.prepared" : onPrepareMessage ? "webchat_page.form_hint" : "webchat_page.start_first")}</p>
  </form>;
}

type RunWorkspaceAction = (moduleId: string, values: Record<string, string>, submissionId: string) => Promise<void>;

function newPublicActionSubmissionId() {
  return typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function WorkspaceActionForm({ module, text, onRunAction }: { module: Extract<WebchatModule, { type: "workspace_action" }>; text: WebchatText; onRunAction?: RunWorkspaceAction }) {
  const [values, setValues] = useState<Record<number, string>>({});
  const [state, setState] = useState<"idle" | "running" | "sent" | "failed">("idle");
  const submissionId = useRef(newPublicActionSubmissionId());
  return <form className="webchat-page-form" onSubmit={async event => {
    event.preventDefault();
    if (!onRunAction || state === "running") return;
    setState("running");
    const submittedValues = Object.fromEntries(module.fields.map((field, index) => [field, values[index] || ""]));
    try { await onRunAction(module.id, submittedValues, submissionId.current); setState("sent"); }
    catch { setState("failed"); }
  }}>
    {module.fields.map((field, index) => <Input key={field} label={field} value={values[index] || ""} maxLength={1000} required disabled={!onRunAction || state === "running"}
      onChange={event => { setValues(previous => ({ ...previous, [index]: event.target.value })); submissionId.current = newPublicActionSubmissionId(); setState("idle"); }} />)}
    <Button type="submit" size="sm" loading={state === "running"} disabled={!onRunAction || state === "sent"}>{module.submit_label || text("webchat_page.run_action")}</Button>
    <p className="webchat-page-hint" role="status">{text(state === "sent" ? "webchat_page.action_sent" : state === "failed" ? "webchat_page.action_failed" : onRunAction ? "webchat_page.action_hint" : "webchat_page.start_first")}</p>
  </form>;
}

export function WebchatModuleContent({ module, text, onPrepareMessage, onRunAction }: { module: WebchatModule; text: WebchatText; onPrepareMessage?: (message: string) => void; onRunAction?: RunWorkspaceAction }) {
  if (module.type === "brand") {
    const website = safeWebchatUrl(module.website);
    return <div className="webchat-page-brand">
      <PublicImage url={module.logo_url} alt={module.name} logo />
      <div>{module.name && <h2>{module.name}</h2>}{website && <a href={website} target="_blank" rel="noopener noreferrer" className="webchat-page-website">{new URL(website).hostname} ↗</a>}</div>
    </div>;
  }
  return <>
    {module.title && <h3>{module.title}</h3>}
    {module.type === "text" && <p className="webchat-page-copy">{module.body}</p>}
    {module.type === "image" && <figure><PublicImage url={module.url} alt={module.caption || module.title} />{module.caption && <figcaption>{module.caption}</figcaption>}</figure>}
    {module.type === "list" && <ul className="webchat-page-list">{module.items.filter(item => item.title.trim() || item.description.trim()).map((item, index) => <li key={index}><strong>{item.title}</strong>{item.description && <p>{item.description}</p>}</li>)}</ul>}
    {module.type === "links" && <ul className="webchat-page-list">{module.items.filter(item => item.label.trim() && safeWebchatUrl(item.url)).map((item, index) => <li key={index}><a href={safeWebchatUrl(item.url)} target="_blank" rel="noopener noreferrer">{item.label}<span aria-hidden="true"> ↗</span></a></li>)}</ul>}
    {module.type === "faq" && module.items.filter(item => item.question.trim() && item.answer.trim()).map((item, index) => <details className="webchat-page-faq" key={index}><summary>{item.question}</summary><p>{item.answer}</p></details>)}
    {module.type === "form" && <IntakeForm module={module} text={text} onPrepareMessage={onPrepareMessage} />}
    {module.type === "workspace_content" && module.resolved && <div className="webchat-page-workspace-content">
      <PublicImage url={module.resolved.image_url} alt={module.resolved.name} />
      {module.resolved.name && <strong>{module.resolved.name}</strong>}
      {module.resolved.body && <p>{module.resolved.body}</p>}
      {module.resolved.items.length > 0 && <ul className="webchat-page-list">{module.resolved.items.map((item, index) => <li key={index}>{item}</li>)}</ul>}
    </div>}
    {module.type === "workspace_action" && <><p>{module.description}</p><WorkspaceActionForm module={module} text={text} onRunAction={onRunAction} /></>}
  </>;
}

export function WebchatSideRegion({ side, label, editing = false, children }: { side: WebchatSide; label: string; editing?: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(() => editing || window.matchMedia("(min-width: 761px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(min-width: 761px)");
    const update = () => setOpen(editing || media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [editing]);
  return <details className={`webchat-page-side webchat-page-side--${side}${editing ? " is-editing" : ""}`} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    <div className="webchat-page-stack">{children}</div>
  </details>;
}

function useCompactWebchatLayout() {
  const [compact, setCompact] = useState(() => window.matchMedia("(max-width: 760px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const update = () => setCompact(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return compact;
}

export function WebchatResponsiveLayout({ left, center, right, className = "", compactCenterFirst = true }: { left: ReactNode; center: ReactNode; right: ReactNode; className?: string; compactCenterFirst?: boolean }) {
  const compact = useCompactWebchatLayout();
  let regions = [left, center, right];
  if (compact) regions = compactCenterFirst ? [center, left, right] : [left, right, center];
  return <div className={`webchat-page-layout${className ? ` ${className}` : ""}`}>{regions}</div>;
}

export default function WebchatPageLayout({ page, children, language = "en", enabled = true, onPrepareMessage, onRunAction }: { page: unknown; children: ReactNode; language?: string | null; enabled?: boolean; onPrepareMessage?: (message: string) => void; onRunAction?: RunWorkspaceAction }) {
  const modules = enabled && validWebchatPage(page) ? page.modules.filter(webchatModuleVisible) : [];
  if (!modules.length) return <>{children}</>;
  const text: WebchatText = key => tForLocale(key, language || "en");
  const sideRegion = (side: WebchatSide) => {
    const items = modules.filter(module => module.side === side);
    return items.length > 0 ? <WebchatSideRegion key={side} side={side} label={text(side === "left" ? "webchat_page.about" : "webchat_page.more")}>
      {items.map(module => <GlassCard key={module.id} hoverable={false} className="webchat-page-module"><WebchatModuleContent module={module} text={text} onPrepareMessage={onPrepareMessage} onRunAction={onRunAction} /></GlassCard>)}
    </WebchatSideRegion> : null;
  };
  return <WebchatResponsiveLayout
    left={sideRegion("left")}
    center={<div key="center" className="webchat-page-center">{children}</div>}
    right={sideRegion("right")}
  />;
}
