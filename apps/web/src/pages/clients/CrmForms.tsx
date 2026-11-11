import { useState } from "react";
import { t } from "../../lib/i18n";
import type { ClientRecord, ClientContactRecord, ClientContactInput, CrmProfile, CrmActivity, CrmActivityInput, CrmOpportunity, CrmOpportunityInput, CrmFollowup, CrmFollowupInput } from "../../lib/api";
import Input from "../../components/ui/Input";
import Select from "../../components/ui/Select";
import Textarea from "../../components/ui/Textarea";
import Button from "../../components/ui/Button";

export const label = (key: string) => t(`page.crm.${key}`);
export const lifecycles = ["lead", "prospect", "customer", "partner"] as const;
export const stages = ["qualified", "discovery", "proposal", "negotiation", "won", "lost"] as const;
export const options = (values: readonly string[]) => values.map(value => ({ value, label: label(value) }));
export type OwnerOption = {value: string; label: string};
export function profileOf(client?: ClientRecord): CrmProfile {
  return { industry: "", website: "", source: "", company_size: "", lifecycle: "lead", owner_id: null, tags: [], notes: "",
    ...client?.metadata?.crm, name: client?.name || "", email: client?.email || "", phone: client?.phone || "", address: client?.address || "", status: client?.status === "inactive" ? "inactive" : "active" };
}
export function localTime(iso: string) {
  const date = new Date(iso);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
export function OwnerField({value, onChange, owners, emptyLabel}: {value: string | null; onChange: (v: string | null) => void; owners: OwnerOption[]; emptyLabel?: string}) {
  const entries = value && !owners.some(o => o.value === value) ? [...owners, { value, label: label("inactive_owner") }] : owners;
  return <div className="space-y-1.5"><p className="text-xs font-semibold text-stone-500">{label("owner")}</p><Select ariaLabel={label("owner")} value={value || ""} onChange={v => onChange(v || null)} filterable options={[{value: "", label: emptyLabel || label("unassigned")}, ...entries]} /></div>;
}
export function ProfileForm({client, owners, busy, error, onSave}: {client?: ClientRecord; owners: OwnerOption[]; busy: boolean; error: string; onSave: (v: CrmProfile) => void}) {
  const [draft, setDraft] = useState(() => profileOf(client));
  const [tags, setTags] = useState(draft.tags.join(", "));
  const field = (key: "name" | "email" | "phone" | "address" | "industry" | "website" | "source" | "company_size") => <Input key={key} label={label(key)} value={draft[key]} onChange={e => setDraft({...draft, [key]: e.target.value})} type={key === "email" ? "email" : "text"} required={key === "name"} disabled={busy} />;
  return <form className="space-y-4" onSubmit={e => { e.preventDefault(); onSave({...draft, tags: tags.split(/[,，]/).map(v => v.trim()).filter(Boolean)}); }}>
    <fieldset disabled={busy} className="grid grid-cols-1 sm:grid-cols-2 gap-4">
      {field("name")}{field("email")}{field("phone")}{field("industry")}{field("website")}{field("company_size")}{field("source")}{field("address")}
      <div className="space-y-1.5"><p className="text-xs font-semibold text-stone-500">{label("lifecycle")}</p><Select ariaLabel={label("lifecycle")} value={draft.lifecycle} onChange={v => setDraft({...draft, lifecycle: v as CrmProfile["lifecycle"]})} options={options(lifecycles)} /></div>
      <OwnerField owners={owners} value={draft.owner_id} onChange={v => setDraft({...draft, owner_id: v})} />
      <Input label={label("tags")} placeholder={label("tags_hint")} value={tags} onChange={e => setTags(e.target.value)} className="sm:col-span-2" />
      <Textarea id="crm-profile-notes" label={label("notes")} value={draft.notes} onChange={e => setDraft({...draft, notes: e.target.value})} className="sm:col-span-2" />
    </fieldset>
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}<Button type="submit" loading={busy}>{label("save")}</Button>
  </form>;
}
export function ContactForm({contact, busy, error, onSave}: {contact?: ClientContactRecord; busy: boolean; error: string; onSave: (v: ClientContactInput) => void}) {
  const [draft, setDraft] = useState<ClientContactInput>(contact ? {name:contact.name, email:contact.email, phone:contact.phone || "", job_title:contact.job_title || "", is_primary:contact.is_primary} : {name:"",email:"",phone:"",job_title:"",is_primary:false});
  return <form className="space-y-4" onSubmit={e => {e.preventDefault(); onSave(draft);}}><fieldset disabled={busy} className="space-y-4">
    {(["name", "email", "phone", "job_title"] as const).map(key => <Input key={key} label={label(key === "name" ? "contact_name" : key)} type={key === "email" ? "email" : "text"} required={key === "name" || key === "email"} value={draft[key]} onChange={e => setDraft({...draft, [key]:e.target.value})} />)}
    <label className="flex gap-2 text-sm"><input type="checkbox" checked={draft.is_primary} onChange={e => setDraft({...draft,is_primary:e.target.checked})} />{label("primary")}</label>
    <p className="text-xs text-stone-500">{label("contact_policy")}</p>
  </fieldset>{error && <p role="alert" className="text-sm text-red-700">{error}</p>}<Button type="submit" loading={busy}>{label("save")}</Button></form>;
}
export type RecordKind = "activities" | "opportunities" | "followups";
export type RecordData = CrmActivityInput | CrmOpportunityInput | CrmFollowupInput;
export type CrmRecord = CrmActivity | CrmOpportunity | CrmFollowup;
export function RecordForm({kind, record, owners, busy, error, onSave}: {kind: RecordKind; record?: CrmRecord; owners: OwnerOption[]; busy: boolean; error: string; onSave: (v: RecordData) => void}) {
  const deal = record && "stage" in record ? record : undefined;
  const task = record && "due_at" in record ? record : undefined;
  const activity = record && "subject" in record ? record : undefined;
  const [title,setTitle] = useState(deal?.title || task?.title || activity?.subject || "");
  const [body,setBody] = useState(deal?.description || task?.body || activity?.body || "");
  const [when,setWhen] = useState(localTime(task?.due_at || activity?.occurred_at || new Date().toISOString()));
  const [owner,setOwner] = useState(deal?.owner_id || task?.owner_id || null);
  const [stage,setStage] = useState(deal?.stage || "qualified");
  const [amount,setAmount] = useState(deal?.amount || "0");
  const [currency,setCurrency] = useState(deal?.currency || "USD");
  const [probability,setProbability] = useState(deal?.probability ?? 25);
  const [close,setClose] = useState(deal?.expected_close || "");
  const [reason,setReason] = useState(deal?.loss_reason || "");
  const [priority,setPriority] = useState(task?.priority || "normal");
  const [activityKind,setActivityKind] = useState<CrmActivityInput["kind"]>(activity?.kind === "deal" ? "note" : activity?.kind || "note");
  function submit() {
    if (kind === "opportunities") onSave({title,description:body,owner_id:owner,stage,amount,currency,probability,expected_close:close || null,loss_reason:reason});
    else if (kind === "followups") onSave({title,body,due_at:new Date(when).toISOString(),owner_id:owner,priority,status:task?.status || "open"});
    else onSave({subject:title,body,kind:activityKind,occurred_at:new Date(when).toISOString()});
  }
  return <form className="space-y-4" onSubmit={e => {e.preventDefault(); submit();}}><fieldset disabled={busy} className="space-y-4">
    <Input label={label("title")} value={title} onChange={e => setTitle(e.target.value)} required />
    {kind === "activities" ? <div className="space-y-1.5"><p className="text-xs font-semibold text-stone-500">{label("activity_type")}</p><Select ariaLabel={label("activity_type")} value={activityKind} onChange={v => setActivityKind(v as CrmActivityInput["kind"])} options={options(["note","call","email","meeting"])} /></div> : <OwnerField owners={owners} value={owner} onChange={setOwner} />}
    {kind === "opportunities" ? <>
      <div className="space-y-1.5"><p className="text-xs font-semibold text-stone-500">{label("stage")}</p><Select ariaLabel={label("stage")} value={stage} onChange={v => setStage(v as CrmOpportunity["stage"])} options={options(stages)} /></div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4"><Input label={label("amount")} type="number" min={0} step="0.01" required value={amount} onChange={e => setAmount(e.target.value)} /><Input label={label("currency")} value={currency} onChange={e => setCurrency(e.target.value.toUpperCase())} required />
      <Input label={label("probability")} type="number" min={0} max={100} step="1" value={String(stage === "won" ? 100 : stage === "lost" ? 0 : probability)} disabled={stage === "won" || stage === "lost"} onChange={e => setProbability(Number(e.target.value))} /><Input label={label("expected_close")} type="date" value={close} onChange={e => setClose(e.target.value)} /></div>
      {stage === "lost" && <Input label={label("loss_reason")} value={reason} onChange={e => setReason(e.target.value)} />}
    </> : <Input label={label(kind === "followups" ? "due_at" : "occurred_at")} type="datetime-local" value={when} onChange={e => setWhen(e.target.value)} required />}
    {kind === "followups" && <div className="space-y-1.5"><p className="text-xs font-semibold text-stone-500">{label("priority")}</p><Select ariaLabel={label("priority")} value={priority} onChange={v => setPriority(v as CrmFollowup["priority"])} options={options(["low","normal","high"])} /></div>}
    <Textarea id="crm-record-body" label={label("description")} value={body} onChange={e => setBody(e.target.value)} />
    {kind === "activities" && <p className="text-xs text-stone-500">{label("activity_policy")}</p>}
  </fieldset>{error && <p role="alert" className="text-sm text-red-700">{error}</p>}<Button type="submit" loading={busy}>{label("save")}</Button></form>;
}
