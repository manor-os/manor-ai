import { useState } from "react";
import Button from "../ui/Button";
import Card from "../ui/Card";
import Input from "../ui/Input";
import Select from "../ui/Select";
import Textarea from "../ui/Textarea";
import { tForLocale } from "../../lib/i18n";
import { maintenanceCategories, maintenancePriorities, residentDraftCanConfirm, residentDraftFieldsEqual, type ResidentDraft, type ResidentDraftCollection } from "../../lib/residentMaintenanceDrafts";
import type { useResidentMaintenanceDrafts } from "../../lib/useResidentMaintenanceDrafts";

type DraftController = ReturnType<typeof useResidentMaintenanceDrafts>;
type Translate = (key: string) => string;

function DraftCard({ draft, locations, controller, text }: {
  draft: ResidentDraft; locations: ResidentDraftCollection["locations"]; controller: DraftController; text: Translate;
}) {
  const [fields, setFields] = useState(draft.fields);
  const uncertain = controller.uncertain.has(draft.draft_id);
  const state = uncertain ? "outcome_unknown" : draft.state;
  const editable = state === "draft";
  const busy = controller.busyDraftId !== null;
  const dirty = !residentDraftFieldsEqual(fields, draft.fields);
  const valid = fields.title.trim().length > 0 && fields.title.trim().length <= 255
    && fields.description.trim().length > 0 && fields.description.trim().length <= 5000;
  const confirmable = !uncertain && residentDraftCanConfirm(draft, fields, locations);
  return <Card minHeight={0} hoverable={false} borderColor="transparent" title={text("draft_title")} style={{ marginTop: 12 }}>
    <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 16, overflowWrap: "anywhere" }}>
      <p role="status" style={{ margin: 0, fontSize: 13 }}>{text(`state_${state}`)}</p>
      {editable ? <>
        <div>
          <label className="block text-xs font-semibold text-stone-500 mb-1.5">{text("address")}</label>
          <Select ariaLabel={text("address")} value={fields.location_ref || ""} placeholder={text("select_address")}
            options={locations.map(item => ({ value: item.location_ref, label: item.label }))} disabled={busy}
            onChange={value => setFields(previous => ({ ...previous, location_ref: value || null }))} />
        </div>
        {locations.length === 0 && <p role="alert" style={{ margin: 0, fontSize: 12 }}>{text("no_address")}</p>}
        <Input label={text("title_field")} value={fields.title} disabled={busy} maxLength={255}
          onChange={event => setFields(previous => ({ ...previous, title: event.target.value }))} />
        <Textarea label={text("description")} ariaLabel={text("description")} value={fields.description} rows={4} disabled={busy}
          onChange={event => setFields(previous => ({ ...previous, description: event.target.value }))} />
        {fields.description.length > 5000 && <p role="alert">{text("description_limit")}</p>}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 }}>
          <div><label className="block text-xs font-semibold text-stone-500 mb-1.5">{text("category")}</label>
            <Select ariaLabel={text("category")} value={fields.category} disabled={busy}
              options={maintenanceCategories.map(value => ({ value, label: text(`category_${value}`) }))}
              onChange={value => setFields(previous => ({ ...previous, category: value as typeof fields.category }))} />
          </div>
          <div><label className="block text-xs font-semibold text-stone-500 mb-1.5">{text("priority")}</label>
            <Select ariaLabel={text("priority")} value={fields.priority} disabled={busy}
              options={maintenancePriorities.map(value => ({ value, label: text(`priority_${value}`) }))}
              onChange={value => setFields(previous => ({ ...previous, priority: value as typeof fields.priority }))} />
          </div>
        </div>
        {dirty && <p role="status" style={{ margin: 0, fontSize: 12 }}>{text("unsaved")}</p>}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {dirty && <Button size="sm" variant="outline" disabled={busy || !valid} onClick={() => void controller.mutate(draft, "edit", fields)}>{text("save")}</Button>}
          <Button size="sm" disabled={busy || !confirmable} loading={controller.busyDraftId === draft.draft_id}
            onClick={() => void controller.mutate(draft, "confirm")}>{text("confirm")}</Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void controller.mutate(draft, "cancel")}>{text("cancel")}</Button>
        </div>
      </> : <>
        <strong>{draft.fields.title}</strong>
        {draft.location_label && <p style={{ margin: 0 }}>{text("address")}: {draft.location_label}</p>}
        <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{draft.fields.description}</p>
        <p style={{ margin: 0 }}>{text(`category_${draft.fields.category}`)} · {text(`priority_${draft.fields.priority}`)}</p>
        {state === "submitted" && draft.request && <p style={{ margin: 0 }}>{text("request_id")} {draft.request.id} · {draft.request.status.replaceAll("_", " ")}</p>}
        {["confirmed", "submitting", "outcome_unknown"].includes(state) && <Button size="sm" variant="outline" disabled={busy}
          loading={controller.busyDraftId === draft.draft_id} onClick={() => void controller.mutate(draft, "recover")}>{text("recover")}</Button>}
      </>}
    </div>
  </Card>;
}

export default function ResidentMaintenanceDraftPanel({ controller, language }: { controller: DraftController; language?: string | null }) {
  const text: Translate = key => tForLocale(`webchat_maintenance.${key}`, language || "en");
  const collection = controller.active ? controller.collection : null;
  return <section aria-label={text("title")} style={{ marginTop: 12, padding: 12, borderRadius: "var(--radius-card)", background: "var(--surface-muted)", overflowWrap: "anywhere" }}>
    <strong style={{ fontSize: 13 }}>{text("title")}</strong>
    {controller.notice && <p role="alert" style={{ fontSize: 12 }}>{text(controller.notice)}</p>}
    {!collection ? <>
      <p role="status" style={{ fontSize: 12 }}>{text(controller.phase)}</p>
      {!["checking", "redirecting"].includes(controller.phase) && <Button size="sm" onClick={() => void controller.start()}>{text("enable")}</Button>}
      {controller.phase === "unavailable" && <Button size="sm" variant="outline" onClick={() => void controller.load()}>{text("retry")}</Button>}
    </> : <>
      <p style={{ fontSize: 12 }}>{text("enabled_until")} {new Date(collection.create_access.expires_at!).toLocaleTimeString(language || "en")}</p>
      <Button size="sm" variant="outline" disabled={controller.loading || controller.busyDraftId !== null} onClick={() => void controller.load()}>{text("refresh")}</Button>
      {controller.loading && <p role="status" style={{ fontSize: 12 }}>{text("checking")}</p>}
      {collection.items.length === 0 && <p style={{ fontSize: 12 }}>{text("empty")}</p>}
      {collection.items.map(draft => <DraftCard key={`${draft.draft_id}:${draft.revision}:${draft.state}`} draft={draft} locations={collection.locations} controller={controller} text={text} />)}
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        {collection.skip > 0 && <Button size="sm" variant="ghost" disabled={controller.loading} onClick={() => void controller.load(Math.max(0, collection.skip - collection.limit))}>{text("previous")}</Button>}
        {collection.has_more && <Button size="sm" variant="ghost" disabled={controller.loading} onClick={() => void controller.load(collection.skip + collection.limit)}>{text("more")}</Button>}
      </div>
    </>}
  </section>;
}
