import { useEffect, useRef, useState } from "react";
import AgentAvatar from "../ui/AgentAvatar";
import Button from "../ui/Button";
import GlassCard from "../ui/GlassCard";
import Input from "../ui/Input";
import Modal from "../ui/Modal";
import Select from "../ui/Select";
import TabSwitcher from "../ui/TabSwitcher";
import Textarea from "../ui/Textarea";
import { t } from "../../lib/i18n";
import { createWebchatModule, emptyWebchatPage, moveWebchatModule, safeWebchatUrl, validWebchatPage, webchatModuleVisible, webchatPageFingerprint, webchatPageForSave, withWorkspaceResourcePreviews, WEBCHAT_MODULE_TYPES, type WebchatModule, type WebchatPage, type WebchatSide, type WebchatWorkspaceResources } from "../../lib/webchatPage";
import WebchatPageLayout, { WebchatModuleContent, WebchatResponsiveLayout, WebchatSideRegion } from "./WebchatPageLayout";

function ModuleFields({ module, onChange, resources, resourceState, hasMoreDocuments, loadingMoreDocuments, onLoadMoreDocuments }: { module: WebchatModule; onChange: (module: WebchatModule) => void; resources?: WebchatWorkspaceResources; resourceState: "loading" | "ready" | "error"; hasMoreDocuments?: boolean; loadingMoreDocuments?: boolean; onLoadMoreDocuments?: () => void }) {
  const input = (key: string, value: string, update: (value: string) => void, limit = 160, isUrl = false) => <Input label={t(`webchat_page.${key}`)} value={value} maxLength={limit} type={isUrl ? "url" : "text"} error={isUrl && value && !safeWebchatUrl(value) ? t("webchat_page.invalid_url") : undefined} onChange={event => update(event.target.value)} />;
  const area = (key: string, value: string, update: (value: string) => void, limit: number) => <Textarea label={t(`webchat_page.${key}`)} ariaLabel={t(`webchat_page.${key}`)} rows={2} value={value} onChange={event => update(event.target.value.slice(0, limit))} />;
  const title = "title" in module && input("title", module.title, value => onChange({ ...module, title: value }));
  switch (module.type) {
    case "brand": return <>{input("name", module.name, name => onChange({ ...module, name }))}{input("logo_url", module.logo_url, logo_url => onChange({ ...module, logo_url }), 2048, true)}{input("website", module.website, website => onChange({ ...module, website }), 2048, true)}</>;
    case "text": return <>{title}{area("body", module.body, body => onChange({ ...module, body }), 4000)}</>;
    case "image": return <>{title}{input("image_url", module.url, url => onChange({ ...module, url }), 2048, true)}{area("caption", module.caption, caption => onChange({ ...module, caption }), 500)}</>;
    case "list": return <>{title}{module.items.map((item, index) => <div className="webchat-page-item-fields" key={index}>
      {input("item_title", item.title, value => onChange({ ...module, items: module.items.map((row, i) => i === index ? { ...row, title: value } : row) }))}
      {area("description", item.description, value => onChange({ ...module, items: module.items.map((row, i) => i === index ? { ...row, description: value } : row) }), 1000)}
      <Button variant="ghost" size="sm" onClick={() => onChange({ ...module, items: module.items.filter((_, i) => i !== index) })}>{t("webchat_page.remove_item")}</Button>
    </div>)}<Button variant="outline" size="sm" disabled={module.items.length >= 12} onClick={() => onChange({ ...module, items: [...module.items, { title: "", description: "" }] })}>{t("webchat_page.add_item")}</Button></>;
    case "links": return <>{title}{module.items.map((item, index) => <div className="webchat-page-item-fields" key={index}>
      {input("link_label", item.label, value => onChange({ ...module, items: module.items.map((row, i) => i === index ? { ...row, label: value } : row) }))}
      {input("website", item.url, value => onChange({ ...module, items: module.items.map((row, i) => i === index ? { ...row, url: value } : row) }), 2048, true)}
      <Button variant="ghost" size="sm" onClick={() => onChange({ ...module, items: module.items.filter((_, i) => i !== index) })}>{t("webchat_page.remove_item")}</Button>
    </div>)}<Button variant="outline" size="sm" disabled={module.items.length >= 12} onClick={() => onChange({ ...module, items: [...module.items, { label: "", url: "" }] })}>{t("webchat_page.add_item")}</Button></>;
    case "faq": return <>{title}{module.items.map((item, index) => <div className="webchat-page-item-fields" key={index}>
      {input("question", item.question, value => onChange({ ...module, items: module.items.map((row, i) => i === index ? { ...row, question: value } : row) }))}
      {area("answer", item.answer, value => onChange({ ...module, items: module.items.map((row, i) => i === index ? { ...row, answer: value } : row) }), 2000)}
      <Button variant="ghost" size="sm" onClick={() => onChange({ ...module, items: module.items.filter((_, i) => i !== index) })}>{t("webchat_page.remove_item")}</Button>
    </div>)}<Button variant="outline" size="sm" disabled={module.items.length >= 12} onClick={() => onChange({ ...module, items: [...module.items, { question: "", answer: "" }] })}>{t("webchat_page.add_item")}</Button></>;
    case "form": return <>{title}<p className="webchat-page-note">{t("webchat_page.form_hint")}</p>{module.fields.map((field, index) => <div className="webchat-page-item-fields" key={index}>
      {input("field_label", field, value => onChange({ ...module, fields: module.fields.map((row, i) => i === index ? value : row) }), 80)}
      <Button variant="ghost" size="sm" onClick={() => onChange({ ...module, fields: module.fields.filter((_, i) => i !== index) })}>{t("webchat_page.remove_item")}</Button>
    </div>)}<Button variant="outline" size="sm" disabled={module.fields.length >= 6} onClick={() => onChange({ ...module, fields: [...module.fields, ""] })}>{t("webchat_page.add_field")}</Button></>;
    case "workspace_content": {
      const documents = resources?.documents || [];
      return <>{title}<label className="webchat-page-note">{t("webchat_page.content_source")}</label><Select ariaLabel={t("webchat_page.content_source")} value={module.source} options={[
        { value: "profile", label: t("webchat_page.workspace_profile") },
        { value: "document", label: t("webchat_page.published_document") },
      ]} onChange={value => {
        if (value === "profile") onChange({ ...module, source: "profile", resource_id: "workspace", resolved: resources?.profile || null });
        else {
          const document = documents[0];
          onChange({ ...module, source: "document", resource_id: document?.id || "", resolved: document ? { name: document.name, body: document.body, image_url: "", items: [] } : null });
        }
      }} />
      {module.source === "document" && <><label className="webchat-page-note">{t("webchat_page.published_document")}</label><Select ariaLabel={t("webchat_page.published_document")} value={module.resource_id} options={documents.map(document => ({ value: document.id, label: document.name }))} onChange={resource_id => {
        const document = documents.find(item => item.id === resource_id);
        onChange({ ...module, resource_id, resolved: document ? { name: document.name, body: document.body, image_url: "", items: [] } : null });
      }} /></>}
      {resourceState === "loading" && <p className="webchat-page-note">{t("webchat_page.resources_loading")}</p>}
      {resourceState === "error" && <p className="webchat-page-error">{t("webchat_page.resources_failed")}</p>}
      {resourceState === "ready" && module.source === "document" && documents.length === 0 && !hasMoreDocuments && <p className="webchat-page-note">{t("webchat_page.no_public_documents")}</p>}
      {module.source === "document" && hasMoreDocuments && <Button variant="ghost" size="sm" loading={loadingMoreDocuments} disabled={loadingMoreDocuments} onClick={onLoadMoreDocuments}>{t("webchat_page.load_more_documents")}</Button>}</>;
    }
    case "workspace_action": {
      const actions = resources?.actions || [];
      return <>{title}{area("description", module.description, description => onChange({ ...module, description }), 1000)}
      <label className="webchat-page-note">{t("webchat_page.workspace_action")}</label><Select ariaLabel={t("webchat_page.workspace_action")} value={module.binding_id} options={actions.map(action => ({ value: action.id, label: action.name }))} onChange={binding_id => {
        const action = actions.find(item => item.id === binding_id);
        onChange({ ...module, binding_id, title: module.title || action?.name || "", description: module.description || action?.description || "" });
      }} />
      <p className="webchat-page-note">{t("webchat_page.action_warning")}</p>
      {resourceState === "loading" && <p className="webchat-page-note">{t("webchat_page.resources_loading")}</p>}
      {resourceState === "error" && <p className="webchat-page-error">{t("webchat_page.resources_failed")}</p>}
      {resourceState === "ready" && actions.length === 0 && <p className="webchat-page-note">{t("webchat_page.no_public_actions")}</p>}
      {module.fields.map((field, index) => <div className="webchat-page-item-fields" key={index}>
        {input("field_label", field, value => onChange({ ...module, fields: module.fields.map((row, i) => i === index ? value : row) }), 80)}
        <Button variant="ghost" size="sm" onClick={() => onChange({ ...module, fields: module.fields.filter((_, i) => i !== index) })}>{t("webchat_page.remove_item")}</Button>
      </div>)}<Button variant="outline" size="sm" disabled={module.fields.length >= 6} onClick={() => onChange({ ...module, fields: [...module.fields, ""] })}>{t("webchat_page.add_field")}</Button>
      {input("submit_label", module.submit_label, submit_label => onChange({ ...module, submit_label }), 80)}</>;
    }
  }
}

interface Props {
  initialPage: unknown;
  workspaceName: string;
  agentName: string;
  agentAvatar?: string | null;
  welcomeMessage?: string;
  resources?: WebchatWorkspaceResources;
  resourceState?: "loading" | "ready" | "error";
  hasMoreDocuments?: boolean;
  loadingMoreDocuments?: boolean;
  onLoadMoreDocuments?: () => void;
  canEdit: boolean;
  onReview: (page: WebchatPage) => Promise<WebchatPage>;
  onSave: (page: WebchatPage) => Promise<void>;
  onClose: () => void;
}
type DropTarget = { side: WebchatSide; beforeId: string | null };
type BuilderFocusReturn = { kind: "module"; id: string } | { kind: "side"; side: WebchatSide };

export default function WebchatPageEditor({ initialPage, workspaceName, agentName, agentAvatar, welcomeMessage, resources, resourceState: resourceStateProp, hasMoreDocuments, loadingMoreDocuments, onLoadMoreDocuments, canEdit, onReview, onSave, onClose }: Props) {
  const initialPageValid = validWebchatPage(initialPage);
  const [page, setPage] = useState<WebchatPage>(() => initialPageValid ? structuredClone(initialPage) : emptyWebchatPage());
  const original = useRef<string | null>(initialPageValid ? webchatPageFingerprint(page) : null);
  const [mode, setMode] = useState(canEdit ? "edit" : "review");
  const [selected, setSelected] = useState<string | null>(null);
  const [paletteSide, setPaletteSide] = useState<WebchatSide | null>(null);
  const [saving, setSaving] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [reviewPage, setReviewPage] = useState<WebchatPage | null>(null);
  const [error, setError] = useState(() => initialPage != null && !validWebchatPage(initialPage) ? t("webchat_page.invalid_config") : "");
  const [notice, setNotice] = useState("");
  const [dragId, setDragId] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null);
  const pointer = useRef<{ id: string; x: number; y: number; active: boolean } | null>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const builderPanelRef = useRef<HTMLElement>(null);
  const builderFocusReturn = useRef<BuilderFocusReturn | null>(null);
  const initialReviewStarted = useRef(false);
  const editing = mode === "edit" && canEdit;
  const resourceState = resourceStateProp || (resources ? "ready" : "loading");
  const dirty = original.current === null || webchatPageFingerprint(page) !== original.current;
  const selectedModule = page.modules.find(module => module.id === selected) || null;

  useEffect(() => {
    if (resources) setPage(previous => withWorkspaceResourcePreviews(previous, resources));
  }, [resources]);

  useEffect(() => {
    if (canEdit || initialReviewStarted.current) return;
    initialReviewStarted.current = true;
    void enterReview();
  }, [canEdit]);

  useEffect(() => {
    if (!selected && !paletteSide) return;
    const frame = requestAnimationFrame(() => builderPanelRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [selected, paletteSide]);

  function update(module: WebchatModule) {
    setPage(previous => ({ ...previous, modules: previous.modules.map(item => item.id === module.id ? module : item) }));
    setReviewPage(null);
    setError("");
  }
  function move(id: string, side: WebchatSide, beforeId: string | null) {
    setPage(previous => moveWebchatModule(previous, id, side, beforeId));
    setReviewPage(null);
    setNotice(t("webchat_page.moved"));
  }
  function shift(module: WebchatModule, direction: number) {
    const siblings = page.modules.filter(item => item.side === module.side);
    const index = siblings.findIndex(item => item.id === module.id);
    if (index + direction < 0 || index + direction >= siblings.length) return;
    move(module.id, module.side, direction < 0 ? siblings[index - 1].id : siblings[index + 2]?.id || null);
  }
  function targetAt(x: number, y: number): DropTarget | null {
    const element = document.elementFromPoint(x, y);
    if (!element || !editorRef.current?.contains(element)) return null;
    const column = element.closest<HTMLElement>("[data-webchat-side]");
    if (!column) return null;
    const side = column.dataset.webchatSide as WebchatSide;
    const item = element.closest<HTMLElement>("[data-module-id]");
    if (!item) return { side, beforeId: null };
    const rect = item.getBoundingClientRect();
    const siblings = page.modules.filter(module => module.side === side);
    const index = siblings.findIndex(module => module.id === item.dataset.moduleId);
    return { side, beforeId: y > rect.top + rect.height / 2 ? siblings[index + 1]?.id || null : item.dataset.moduleId! };
  }
  function finishDrag() { setDragId(null); setDropTarget(null); pointer.current = null; }
  function restoreBuilderFocus(target: BuilderFocusReturn | null) {
    if (!target) return;
    requestAnimationFrame(() => {
      const selector = target.kind === "module"
        ? `[data-module-id="${target.id}"] .webchat-page-configure`
        : `[data-webchat-side="${target.side}"] .webchat-page-add-module`;
      editorRef.current?.querySelector<HTMLButtonElement>(selector)?.focus();
    });
  }
  function closeBuilderPanel(target = builderFocusReturn.current) {
    setSelected(null);
    setPaletteSide(null);
    restoreBuilderFocus(target);
  }
  async function enterReview() {
    if (reviewing) return;
    if (!validWebchatPage(page)) { setError(t("webchat_page.invalid_config")); return; }
    setReviewing(true); setError("");
    try {
      const reviewed = await onReview(webchatPageForSave(page));
      const stored = webchatPageForSave(reviewed);
      setPage(stored);
      setReviewPage(reviewed);
      setMode("review");
      setSelected(null);
      setPaletteSide(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("webchat_page.review_failed"));
    } finally {
      setReviewing(false);
    }
  }
  async function discardChanges() {
    if (original.current === null) return;
    const restored = withWorkspaceResourcePreviews(JSON.parse(original.current), resources);
    setPage(restored);
    setReviewPage(null);
    setSelected(null);
    setPaletteSide(null);
    setError("");
    if (mode !== "review") return;
    setReviewing(true);
    try {
      const reviewed = await onReview(webchatPageForSave(restored));
      setPage(webchatPageForSave(reviewed));
      setReviewPage(reviewed);
    } catch (cause) {
      setMode("edit");
      setError(cause instanceof Error ? cause.message : t("webchat_page.review_failed"));
    } finally {
      setReviewing(false);
    }
  }
  function addModule(type: WebchatModule["type"], side: WebchatSide) {
    let module = createWebchatModule(type, side, resources?.brand || { name: workspaceName, logo_url: "", website: "" });
    if (module.type === "workspace_content") module = { ...module, resolved: resources?.profile || null };
    if (module.type === "workspace_action" && resources?.actions[0]) module = { ...module, binding_id: resources.actions[0].id, title: resources.actions[0].name, description: resources.actions[0].description };
    setPage(previous => ({ ...previous, modules: [...previous.modules, module] }));
    setReviewPage(null);
    builderFocusReturn.current = { kind: "module", id: module.id };
    setSelected(module.id);
    setPaletteSide(null);
  }
  async function save() {
    if (!canEdit || saving || !validWebchatPage(page)) return;
    setSaving(true); setError("");
    try { await onSave(webchatPageForSave(page)); onClose(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : t("webchat_page.save_failed")); }
    finally { setSaving(false); }
  }
  const preview = <>
    <div className="webchat-page-preview" aria-label={t("webchat_page.chat_preview")}>
      <div className="webchat-page-preview-header"><AgentAvatar name={agentName} avatarUrl={agentAvatar} size={32} shape="rounded" /><div><strong>{agentName}</strong><small>{workspaceName}</small></div></div>
      <div className="webchat-page-preview-body">{welcomeMessage}</div>
      <div className="webchat-page-preview-composer"><span aria-hidden="true">＋</span><input className="manor-input" disabled placeholder={t("webchat_page.preview_only")} aria-label={t("webchat_page.chat_preview")} /><Button disabled size="sm" ariaLabel={t("webchat_page.preview_only")}>↑</Button></div>
    </div><div className="webchat-page-preview-footer">{t("page.public_chat.powered_by_manor_ai")}</div>
  </>;
  const editSide = (side: WebchatSide) => <WebchatSideRegion key={side} side={side} label={t(`webchat_page.${side}`)} editing>
    <div data-webchat-side={side}>
      <p className="webchat-page-edit-label">{t(`webchat_page.${side}`)}</p>
      <div className="webchat-page-stack">{page.modules.filter(module => module.side === side).map(module => <div key={module.id} data-module-id={module.id}>
        <GlassCard hoverable={false} className={`webchat-page-module${selected === module.id ? " is-selected" : ""}${dragId === module.id ? " is-dragging" : ""}${dropTarget?.beforeId === module.id ? " is-drop-target" : ""}`}>
          <div className="webchat-page-module-bar">
            <button type="button" className="btn-manor-ghost webchat-page-grip" draggable aria-label={`${t("webchat_page.move")} ${t(`webchat_page.type_${module.type}`)}`} onDragStart={event => {
              event.dataTransfer.setData("text/plain", module.id); event.dataTransfer.effectAllowed = "move"; setDragId(module.id);
            }} onDragEnd={finishDrag} onKeyDown={event => {
              if (!event.altKey) return;
              if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) event.preventDefault();
              if (event.key === "ArrowUp" || event.key === "ArrowDown") shift(module, event.key === "ArrowUp" ? -1 : 1);
              if (event.key === "ArrowLeft" || event.key === "ArrowRight") move(module.id, event.key === "ArrowLeft" ? "left" : "right", null);
              if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) requestAnimationFrame(() => {
                editorRef.current?.querySelector<HTMLButtonElement>(`[data-module-id="${module.id}"] .webchat-page-grip`)?.focus();
              });
            }} onPointerDown={event => {
              if (event.pointerType === "mouse") return;
              pointer.current = { id: module.id, x: event.clientX, y: event.clientY, active: false }; event.currentTarget.setPointerCapture(event.pointerId);
            }} onPointerMove={event => {
              const start = pointer.current; if (!start) return;
              if (Math.hypot(event.clientX - start.x, event.clientY - start.y) > 8) start.active = true;
              if (start.active) { setDragId(start.id); setDropTarget(targetAt(event.clientX, event.clientY)); }
            }} onPointerUp={event => {
              const start = pointer.current;
              if (start?.active) { const target = targetAt(event.clientX, event.clientY); if (target) move(start.id, target.side, target.beforeId); }
              finishDrag();
            }} onPointerCancel={finishDrag}>⠿</button>
            <span>{t(`webchat_page.type_${module.type}`)}</span>
            <Button className="webchat-page-configure" size="sm" variant="ghost" ariaExpanded={selected === module.id} onClick={() => {
              builderFocusReturn.current = { kind: "module", id: module.id };
              setPaletteSide(null);
              setSelected(module.id);
            }}>{t("webchat_page.configure")}</Button>
          </div>
          {webchatModuleVisible(module) ? <WebchatModuleContent module={module} text={t} /> : <div className="webchat-page-empty">{t("webchat_page.empty_module")}</div>}
        </GlassCard>
      </div>)}</div>
      <div className={`webchat-page-drop-end${dropTarget?.side === side && dropTarget.beforeId === null ? " is-drop-target" : ""}`}>
        <Button className="webchat-page-add-module" variant="outline" size="sm" ariaLabel={t("webchat_page.add_module")} disabled={page.modules.length >= 12} ariaExpanded={paletteSide === side} onClick={() => {
          builderFocusReturn.current = { kind: "side", side };
          setSelected(null);
          setPaletteSide(side);
        }}>＋ {t("webchat_page.add_module")}</Button>
      </div>
    </div>
  </WebchatSideRegion>;
  return <Modal open onClose={() => { if (!saving) onClose(); }} title={t("webchat_page.page_title")} maxWidth="1160px" footer={<>
    <Button variant="outline" disabled={saving} onClick={onClose}>{t("action.cancel")}</Button>
    {canEdit && mode === "edit" && <Button disabled={saving || reviewing} loading={reviewing} onClick={enterReview}>{t("webchat_page.review")}</Button>}
    {canEdit && mode === "review" && <Button disabled={!reviewPage || !dirty || !validWebchatPage(page)} loading={saving} onClick={save}>{t("webchat_page.save")}</Button>}
  </>}>
    <div className="webchat-page-editor" ref={editorRef}>
      <div className="webchat-page-toolbar">
        <TabSwitcher ariaLabel={t("webchat_page.mode")} tabs={[...(canEdit ? [{ key: "edit", label: t("action.edit") }] : []), { key: "review", label: t("webchat_page.review") }]} value={mode} onChange={next => {
          if (saving) return;
          if (next === "review") { void enterReview(); return; }
          setMode(next); setReviewPage(null); setSelected(null); setPaletteSide(null);
        }} />
        {canEdit && dirty && original.current !== null && <Button variant="ghost" size="sm" disabled={saving || reviewing} onClick={discardChanges}>{t("webchat_page.discard")}</Button>}
      </div>
      <div className="webchat-page-guidance">
        <p>{t(editing ? "webchat_page.edit_hint" : "webchat_page.review_hint")}</p>
        <p>{t("webchat_page.public_warning")}</p>
      </div>
      {error && <div role="alert" className="webchat-page-error">{error}</div>}
      {(!editing || (!selectedModule && !paletteSide)) && <div className="webchat-page-stage" onDragOver={event => {
        if (!dragId) return;
        const target = targetAt(event.clientX, event.clientY);
        setDropTarget(target);
        if (target) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; }
      }} onDrop={event => {
        if (!dragId) return;
        event.preventDefault(); const target = targetAt(event.clientX, event.clientY);
        if (target) move(dragId, target.side, target.beforeId);
        finishDrag();
      }}>
        {!editing ? <WebchatPageLayout page={reviewPage}>{preview}</WebchatPageLayout> : <WebchatResponsiveLayout
          className="webchat-page-edit-layout"
          compactCenterFirst={false}
          left={editSide("left")}
          center={<div key="center" className="webchat-page-center"><p className="webchat-page-edit-label">{t("webchat_page.chat_unchanged")}</p>{preview}</div>}
          right={editSide("right")}
        />}
      </div>}
      {editing && paletteSide && <section ref={builderPanelRef} tabIndex={-1} className="webchat-page-builder-panel webchat-page-library" aria-label={t("webchat_page.add_module")}>
        <div className="webchat-page-builder-panel-header">
          <div><span>{t(`webchat_page.${paletteSide}`)}</span><strong>{t("webchat_page.add_module")}</strong></div>
          <Button variant="ghost" size="sm" onClick={() => closeBuilderPanel()}>{t("webchat_page.done")}</Button>
        </div>
        <div className="webchat-page-palette">{WEBCHAT_MODULE_TYPES.map(type => <Button key={type} variant="outline" size="sm" onClick={() => addModule(type, paletteSide)}>{t(`webchat_page.type_${type}`)}</Button>)}</div>
      </section>}
      {editing && selectedModule && <section ref={builderPanelRef} tabIndex={-1} className="webchat-page-builder-panel webchat-page-inspector" aria-label={t("webchat_page.configure")}>
        <div className="webchat-page-builder-panel-header">
          <div><span>{t(`webchat_page.type_${selectedModule.type}`)}</span><strong>{("title" in selectedModule && selectedModule.title) || (selectedModule.type === "brand" && selectedModule.name) || t("webchat_page.configure")}</strong></div>
          <Button variant="outline" size="sm" onClick={() => closeBuilderPanel()}>{t("webchat_page.done")}</Button>
        </div>
        <div className="webchat-page-inspector-body">
          <div className="webchat-page-position-field">
            <label className="webchat-page-note">{t("webchat_page.position")}</label>
            <Select ariaLabel={t("webchat_page.position")} value={selectedModule.side} onChange={value => move(selectedModule.id, value as WebchatSide, null)} options={[{ value: "left", label: t("webchat_page.left") }, { value: "right", label: t("webchat_page.right") }]} />
          </div>
          <div className="webchat-page-inspector-fields"><ModuleFields module={selectedModule} onChange={update} resources={resources} resourceState={resourceState} hasMoreDocuments={hasMoreDocuments} loadingMoreDocuments={loadingMoreDocuments} onLoadMoreDocuments={onLoadMoreDocuments} /></div>
        </div>
        <div className="webchat-page-config-actions">
          <Button variant="ghost" size="sm" disabled={page.modules.filter(item => item.side === selectedModule.side)[0]?.id === selectedModule.id} onClick={() => shift(selectedModule, -1)}>{t("webchat_page.up")}</Button>
          <Button variant="ghost" size="sm" disabled={page.modules.filter(item => item.side === selectedModule.side).at(-1)?.id === selectedModule.id} onClick={() => shift(selectedModule, 1)}>{t("webchat_page.down")}</Button>
          <Button variant="ghost" size="sm" onClick={() => {
            setPage(previous => ({ ...previous, modules: previous.modules.filter(item => item.id !== selectedModule.id) }));
            setReviewPage(null);
            closeBuilderPanel({ kind: "side", side: selectedModule.side });
          }}>{t("webchat_page.remove")}</Button>
        </div>
      </section>}
      <div className="webchat-page-note" role="status">{notice}</div>
    </div>
  </Modal>;
}
