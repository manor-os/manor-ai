import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, type ClientRecord, type ClientContactRecord, type CrmProfile, type CrmActivity, type CrmFollowup, type CrmOpportunity } from "../lib/api";
import { t } from "../lib/i18n";
import { crmMoney } from "../lib/crmMoney";
import { cacheCrmProfile, crmQueryOptions, invalidateCrmQueries, seedCrmClient, type CrmChange } from "../lib/crmQueries";
import { useAuthStore } from "../stores/auth";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import PeopleNavigation from "../components/PeopleNavigation";
import { IconArrowLeft, IconEdit, IconTrash, IconUser, IconChat, IconBriefcase, IconCalendar, IconCheckCircle, IconExternalLink } from "../components/icons";
import IconTile from "../components/ui/IconTile";
import StatusPill from "../components/ui/StatusPill";
import { openDetail, closeDetail } from "../stores/detail";
import CrmDetailFields from "./clients/CrmDetailFields";
import Button from "../components/ui/Button";
import Tooltip from "../components/ui/Tooltip";
import Input from "../components/ui/Input";
import Select from "../components/ui/Select";
import Modal from "../components/ui/Modal";
import Card from "../components/ui/Card";
import CompactCard from "../components/ui/CompactCard";
import EmptyState from "../components/ui/EmptyState";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import TabSwitcher from "../components/ui/TabSwitcher";
import SmartToolbar from "../components/ui/SmartToolbar";
import ClientDeliveries from "./clients/ClientDeliveries";
import { ContactForm, ProfileForm, RecordForm, OwnerField, label, lifecycles, stages, options, profileOf, type CrmRecord, type RecordData, type RecordKind } from "./clients/CrmForms";

const pageSize = 25;
const sections = ["profile", "contacts", "activities", "opportunities", "followups", "delivery"];
const message = (e:unknown)=>e instanceof Error ? e.message : t("status.error");
const dateTime = (value:string)=>new Date(value).toLocaleString();
const money = (amount:string,currency:string)=>crmMoney([amount],currency);
function FilterSelect({title,value,onChange,options:entries}:{title:string;value:string;onChange:(v:string)=>void;options:{value:string;label:string}[]}) {
  return <div className="space-y-1.5"><p className="text-xs font-semibold uppercase tracking-wide text-stone-500">{title}</p><Select ariaLabel={title} value={value} onChange={onChange} options={entries} /></div>;
}
function Pager({offset,total,onChange}:{offset:number;total:number;onChange:(n:number)=>void}) {
  return total > pageSize ? <div className="flex items-center justify-between gap-3 text-sm"><span>{offset+1}–{Math.min(offset+pageSize,total)} / {total}</span><div className="flex gap-2"><Button size="sm" variant="outline" disabled={!offset} onClick={()=>onChange(Math.max(0,offset-pageSize))}>{label("previous")}</Button><Button size="sm" variant="outline" disabled={offset+pageSize>=total} onClick={()=>onChange(offset+pageSize)}>{label("next")}</Button></div></div> : null;
}
export default function Clients() {
  const identity = useAuthStore(s=>`${s.user?.id}:${s.user?.entity_id}`);
  const [params,setParams] = useSearchParams(); const cache = useQueryClient();
  const clientId = params.get("client") || "";
  const section = sections.includes(params.get("section") || "") ? params.get("section")! : "profile";
  const view = ["clients","pipeline","queue"].includes(params.get("view") || "") ? params.get("view")! : "clients";
  const [search,setSearch] = useState(""); const [lifecycle,setLifecycle] = useState(""); const [owner,setOwner] = useState<string|null>(null);
  const [status,setStatus] = useState("active"); const [stage,setStage] = useState(""); const [queueStatus,setQueueStatus] = useState("open"); const [overdue,setOverdue] = useState(false);
  const [tag,setTag] = useState(""); const [offset,setOffset] = useState(0);
  const [appliedText,setAppliedText] = useState({search:"",tag:""});
  useEffect(()=>{
    if(search.trim()===appliedText.search && tag.trim()===appliedText.tag)return;
    const timer=setTimeout(()=>{setAppliedText({search:search.trim(),tag:tag.trim()});setOffset(0);},300);
    return ()=>clearTimeout(timer);
  },[search,tag,appliedText.search,appliedText.tag]);
  const [busy,setBusy] = useState(false); const [error,setError] = useState("");
  const [deliveryActions,setDeliveryActions] = useState<ReactNode>(null);
  const [profileOpen,setProfileOpen] = useState(false); const [contactOpen,setContactOpen] = useState(false); const [contactEdit,setContactEdit] = useState<ClientContactRecord>();
  const [editor,setEditor] = useState<{kind:RecordKind;record?:CrmRecord}|null>(null);
  const [confirmation,setConfirmation] = useState<{title:string;body:string;action:()=>Promise<void>}|null>(null);
  useEffect(()=>{
    setProfileOpen(false);setContactOpen(false);setContactEdit(undefined);setEditor(null);setConfirmation(null);setError("");
    return ()=>closeDetail();
  },[identity,clientId,section,view]);
  const owners = useQuery({...crmQueryOptions(["crm-owners",identity],signal=>api.people.directory(signal))});
  const ownerOptions = (owners.data || []).map(u=>({value:u.id,label:u.display_name || u.email}));
  const ownerName = (id:string|null|undefined)=>ownerOptions.find(o=>o.value===id)?.label || label(id ? "inactive_owner" : "unassigned");
  const overview = useQuery({...crmQueryOptions(["crm-overview",identity],signal=>api.crm.overview(signal)),refetchInterval:60000,enabled:!clientId});
  const filters = new URLSearchParams({search:appliedText.search,status,owner_id:owner||"",tag:appliedText.tag,limit:String(pageSize),offset:String(offset)});
  if(lifecycle)filters.set("lifecycle",lifecycle);
  const clients = useQuery({...crmQueryOptions(["crm-clients",identity,filters.toString()],signal=>api.crm.clients(filters.toString(),signal)),enabled:!clientId && view==="clients"});
  const selected = useQuery({...crmQueryOptions(["crm-client",identity,clientId],signal=>api.clients.get(clientId,signal)),enabled:!!clientId});
  const pipelineFilters = new URLSearchParams({search:appliedText.search,owner_id:owner||"",limit:String(pageSize),offset:String(offset)});if(stage)pipelineFilters.set("stage",stage);
  const pipeline = useQuery({...crmQueryOptions(["crm-pipeline",identity,pipelineFilters.toString()],signal=>api.crm.pipeline(pipelineFilters.toString(),signal)),enabled:!clientId && view==="pipeline"});
  const queueFilters = new URLSearchParams({status:queueStatus,owner_id:owner||"",overdue:String(overdue),limit:String(pageSize),offset:String(offset)});
  const queue = useQuery({...crmQueryOptions(["crm-queue",identity,queueFilters.toString()],signal=>api.crm.queue(queueFilters.toString(),signal)),refetchInterval:60000,enabled:!clientId && view==="queue"});
  const contacts = useQuery({...crmQueryOptions(["client-contacts",identity,clientId],signal=>api.clients.contacts(clientId,signal)),enabled:!!clientId && section==="contacts"});
  const activities = useQuery({...crmQueryOptions(["crm-activities",identity,clientId,offset],signal=>api.crm.activities(clientId,offset,signal)),enabled:!!clientId && section==="activities"});
  const opportunities = useQuery({...crmQueryOptions(["crm-opportunities",identity,clientId,offset],signal=>api.crm.opportunities(clientId,offset,signal)),enabled:!!clientId && section==="opportunities"});
  const followups = useQuery({...crmQueryOptions(["crm-followups",identity,clientId,offset],signal=>api.crm.followups(clientId,offset,signal)),refetchInterval:60000,enabled:!!clientId && section==="followups"});
  const active = selected.data?.status==="active";
  function route(changes:Record<string,string|null>) {const next=new URLSearchParams(params);for(const [key,value] of Object.entries(changes)){if(value===null)next.delete(key);else next.set(key,value);}setParams(next);setOffset(0);setError("");}
  function openClient(id:string,s="profile"){
    const row=clients.data?.items.find(item=>item.id===id);
    if(row)seedCrmClient(cache,identity,row,clients.dataUpdatedAt);
    route({client:id,section:s});
  }
  const refresh=(change:CrmChange,changedClient=clientId)=>invalidateCrmQueries(cache,identity,changedClient,change);
  async function perform(action:()=>Promise<void>) {setBusy(true);setError("");try{await action();closeDetail();}catch(e){setError(message(e));}finally{setBusy(false);}}
  function resetFilter(action:()=>void){action();setOffset(0);}
  async function saveProfile(data:CrmProfile) {const row=clientId ? await api.crm.profile(clientId,data) : await api.crm.create(data);await cacheCrmProfile(cache,identity,row);await refresh("profile",row.id);setProfileOpen(false);openClient(row.id);}
  async function saveRecord(data:RecordData){if(!editor)return;if("stage" in data)await api.crm.saveOpportunity(clientId,data,editor.record?.id);else if("due_at" in data)await api.crm.saveFollowup(clientId,data,editor.record?.id);else await api.crm.saveActivity(clientId,data,editor.record?.id);await refresh(editor.kind);setEditor(null);}
  function toggle(task:CrmFollowup){void perform(async()=>{const {title,body,due_at,owner_id,priority}=task;await api.crm.saveFollowup(task.client_id,{title,body,due_at,owner_id,priority,status:task.status==="done"?"open":"done"},task.id);await refresh("followups",task.client_id);});}
  function remove(kind:RecordKind,id:string){setError("");setConfirmation({title:label("remove"),body:label("remove_confirm"),action:async()=>{await api.crm.remove(clientId,kind,id);await refresh(kind);setConfirmation(null);}});}
  const retry = (error:unknown,action:()=>void)=><EmptyState title={message(error)} action={<Button onClick={action}>{label("retry")}</Button>} />;
  function contactRow(contact:ClientContactRecord) {
    const icon=<IconTile size={34}><IconUser size={18} /></IconTile>;
    const badge=contact.is_primary?<StatusPill size="sm" label={label("primary")} />:undefined;
    return <CompactCard key={contact.id} icon={icon} title={contact.name} subtitle={[contact.job_title,contact.email,contact.phone].filter(Boolean).join(" · ")} meta={badge} onClick={busy?undefined:()=>openDetail({
      key:`crm-contact-${contact.id}`, icon, title:contact.name, subtitle:contact.job_title||undefined, badges:badge,
      body:<CrmDetailFields fields={[{label:label("email"),value:contact.email},{label:label("phone"),value:contact.phone},{label:label("job_title"),value:contact.job_title}]} />,
      primaryAction:{label:label("edit_contact"),icon:<IconEdit size={16} />,disabled:!active||busy,onClick:()=>{setContactEdit(contact);setError("");setContactOpen(true);}},
      dangerAction:{label:label("remove"),icon:<IconTrash size={16} />,disabled:!active||busy,onClick:()=>{setError("");setConfirmation({title:label("remove"),body:label("contact_remove_confirm"),action:async()=>{await api.clients.removeContact(clientId,contact.id);await refresh("contacts");setConfirmation(null);}});}},
    })} />;
  }
  function activityRow(activity:CrmActivity) {
    const icon=<IconTile size={34}><IconChat size={18} /></IconTile>;
    return <CompactCard key={activity.id} icon={icon} title={activity.subject} subtitle={dateTime(activity.occurred_at)} meta={<StatusPill size="sm" label={label(activity.kind)} />} onClick={busy?undefined:()=>openDetail({
      key:`crm-activity-${activity.id}`,icon,title:activity.subject,
      body:<div className="space-y-4"><CrmDetailFields fields={[{label:label("activity_type"),value:label(activity.kind)},{label:label("occurred_at"),value:dateTime(activity.occurred_at)}]} /><p className="whitespace-pre-wrap break-words text-sm">{activity.body}</p></div>,
      ...(activity.kind!=="deal"?{
        primaryAction:{label:label("edit"),icon:<IconEdit size={16} />,disabled:!active||busy,onClick:()=>{setError("");setEditor({kind:"activities",record:activity});}},
        dangerAction:{label:label("remove"),icon:<IconTrash size={16} />,disabled:!active||busy,onClick:()=>remove("activities",activity.id)},
      }:{}),
    })} />;
  }
  function dealRow(deal:CrmOpportunity,detail=false) {
    const icon=<IconTile size={34}><IconBriefcase size={18} /></IconTile>;
    const badge=<StatusPill size="sm" label={label(deal.stage)} />;
    return <CompactCard key={deal.id} icon={icon} title={deal.title} subtitle={[detail?undefined:deal.client_name,money(deal.amount,deal.currency),ownerName(deal.owner_id)].filter(Boolean).join(" · ")} meta={badge} onClick={busy?undefined:()=>openDetail({
      key:`crm-opportunity-${deal.id}`,icon,title:deal.title,subtitle:detail?undefined:deal.client_name,badges:badge,
      body:<div className="space-y-4"><CrmDetailFields fields={[{label:label("amount"),value:money(deal.amount,deal.currency),mono:true},{label:label("probability"),value:`${deal.probability}%`,mono:true},{label:label("owner"),value:ownerName(deal.owner_id)},{label:label("expected_close"),value:deal.expected_close}]} /><p className="whitespace-pre-wrap break-words text-sm">{deal.description}</p>{deal.loss_reason&&<CrmDetailFields fields={[{label:label("loss_reason"),value:deal.loss_reason}]} />}</div>,
      primaryAction:detail?{label:label("edit"),icon:<IconEdit size={16} />,disabled:!active||busy,onClick:()=>{setError("");setEditor({kind:"opportunities",record:deal});}}:{label:label("open_client"),icon:<IconExternalLink size={16} />,onClick:()=>{closeDetail();openClient(deal.client_id,"opportunities");}},
      dangerAction:detail?{label:label("remove"),icon:<IconTrash size={16} />,disabled:!active||busy,onClick:()=>remove("opportunities",deal.id)}:undefined,
    })} />;
  }
  function taskRow(task:CrmFollowup,detail=false) {
    const late=task.status==="open"&&new Date(task.due_at).getTime()<Date.now();
    const icon=<IconTile size={34}><IconCalendar size={18} /></IconTile>;
    const badge=<StatusPill size="sm" status={late?"blocked":task.status==="done"?"completed":"pending"} label={label(late?"overdue":task.status)} />;
    return <CompactCard key={task.id} icon={icon} title={task.title} subtitle={[detail?undefined:task.client_name,dateTime(task.due_at),ownerName(task.owner_id)].filter(Boolean).join(" · ")} meta={badge} onClick={busy?undefined:()=>openDetail({
      key:`crm-followup-${task.id}`,icon,title:task.title,subtitle:detail?undefined:task.client_name,badges:badge,
      body:<div className="space-y-4"><CrmDetailFields fields={[{label:label("due_at"),value:dateTime(task.due_at)},{label:label("owner"),value:ownerName(task.owner_id)},{label:label("priority"),value:label(task.priority)},...(task.completed_at?[{label:label("done"),value:dateTime(task.completed_at)}]:[])]} /><p className="whitespace-pre-wrap break-words text-sm">{task.body}</p></div>,
      primaryAction:{label:label(task.status==="done"?"reopen":"mark_done"),icon:<IconCheckCircle size={16} />,disabled:busy||(detail&&!active),onClick:()=>{closeDetail();toggle(task);}},
      secondaryActions:[detail?{label:label("edit"),icon:<IconEdit size={16} />,disabled:!active||busy,onClick:()=>{setError("");setEditor({kind:"followups",record:task});}}:{label:label("open_client"),icon:<IconExternalLink size={16} />,onClick:()=>{closeDetail();openClient(task.client_id,"followups");}}],
      dangerAction:detail?{label:label("remove"),icon:<IconTrash size={16} />,disabled:!active||busy,onClick:()=>remove("followups",task.id)}:undefined,
    })} />;
  }
  const p=selected.data?profileOf(selected.data):null;
  const textPending=!clientId && view!=="queue" && (search.trim()!==appliedText.search || (view==="clients" && tag.trim()!==appliedText.tag));
  const listQuery=view==="clients"?clients:view==="pipeline"?pipeline:queue;
  const revalidating=clientId?selected.isFetching&&!selected.isPending:listQuery.isFetching&&!listQuery.isPending;
  return <div className="h-full flex flex-col min-w-0">
    <PageHeader
      title={t("nav.people")}
      subtitle={selected.data?.name}
      breadcrumb={clientId&&<Button variant="ghost" onClick={()=>route({client:null,section:null})}>
        <span aria-hidden="true"><IconArrowLeft size={16} /></span>
        {t("page.people.back_clients")}
      </Button>}
      tabs={<PeopleNavigation />}
      toolbar={!clientId&&view!=="queue" ? <SmartToolbar searchValue={search} onSearchChange={setSearch} searchPlaceholder={label(view==="clients"?"search_clients_hint":"search_opportunities_hint")} className="w-full sm:w-64" /> : undefined}
      actions={<>
        {(textPending||revalidating)&&<LoadingSpinner size={16} ariaLabel={t("status.loading")} />}
        {!clientId&&<PageHeaderAddButton label={t("page.clients.add_client")} disabled={owners.isError||owners.isPending} onClick={()=>{setError("");setProfileOpen(true);}} />}
        {clientId&&selected.data&&<>
          {section==="contacts"&&<PageHeaderAddButton label={label("add_contact")} disabled={!active||busy} onClick={()=>{setContactEdit(undefined);setError("");setContactOpen(true);}} />}
          {["activities","opportunities","followups"].includes(section)&&<PageHeaderAddButton label={label(`add_${section}`)} disabled={!active||busy||owners.isError||owners.isPending} onClick={()=>{setError("");setEditor({kind:section as RecordKind});}} />}
          {section==="delivery"&&deliveryActions}
        </>}
      </>}
    />
    <div className="mb-4 min-w-0 shrink-0">
      {clientId
        ? <TabSwitcher appearance="minimal" size="sm" ariaLabel={label("client_sections")} wrap tabs={sections.map(key=>({key,label:label(key)}))} value={section} onChange={v=>route({section:v})} />
        : <TabSwitcher appearance="minimal" size="sm" ariaLabel={label("crm_views")} wrap tabs={options(["clients","pipeline","queue"]).map(o=>({key:o.value,label:o.label}))} value={view} onChange={v=>route({view:v})} />}
    </div>
    <div className="flex-1 overflow-auto min-w-0 space-y-5 pb-6" aria-busy={busy||textPending||revalidating}>
      {error&&!profileOpen&&!contactOpen&&!editor&&!confirmation&&<p role="alert" className="text-sm text-red-700">{error}</p>}
      {owners.isError&&retry(owners.error,()=>void owners.refetch())}
      {!clientId ? <>
        {overview.isPending ? <LoadingSpinner ariaLabel={t("status.loading")} /> : overview.isError ? retry(overview.error,()=>void overview.refetch()) : <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Card minHeight={0} title={label("active_clients")} hoverable={false}><p className="text-2xl font-semibold mt-2">{Object.values(overview.data.clients).reduce((a,b)=>a+(b||0),0)}</p><p className="text-xs text-stone-500 mt-1">{lifecycles.map(l=>`${label(l)} ${overview.data.clients[l]||0}`).join(" · ")}</p></Card>
          <Card minHeight={0} title={label("open_pipeline")} hoverable={false}><div className="mt-2 space-y-2">{Array.from(new Set(overview.data.pipeline.filter(d=>!["won","lost"].includes(d.stage)).map(d=>d.currency))).map(c=>{const entries=overview.data.pipeline.filter(d=>d.currency===c&&!["won","lost"].includes(d.stage));return <div key={c}><p className="font-semibold">{crmMoney(entries.map(d=>d.amount),c)}</p><p className="text-xs text-stone-500">{label("weighted")}: {crmMoney(entries.map(d=>d.weighted),c)}</p></div>;})}{!overview.data.pipeline.some(d=>!["won","lost"].includes(d.stage))&&<p className="text-sm text-stone-500">{label("no_opportunities")}</p>}</div></Card>
          <Card minHeight={0} title={label("overdue_followups")} hoverable={false}><p className="text-2xl font-semibold mt-2">{overview.data.overdue}</p><Button size="sm" variant="ghost" onClick={()=>{setOverdue(true);setQueueStatus("open");route({view:"queue"});}}>{label("view_followups")}</Button></Card>
        </div>}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <OwnerField emptyLabel={label("all_owners")} value={owner} onChange={v=>resetFilter(()=>setOwner(v))} owners={ownerOptions} />
          {view==="clients"&&<><FilterSelect title={label("lifecycle")} value={lifecycle} onChange={v=>resetFilter(()=>setLifecycle(v))} options={[{value:"",label:label("all_lifecycles")},...options(lifecycles)]} /><FilterSelect title={label("client_status")} value={status} onChange={v=>resetFilter(()=>setStatus(v))} options={options(["active","inactive"])} /><Input label={label("tag_filter")} ariaLabel={label("tag_filter")} placeholder={label("tag_filter")} value={tag} onChange={e=>setTag(e.target.value)} /></>}
          {view==="pipeline"&&<FilterSelect title={label("stage")} value={stage} onChange={v=>resetFilter(()=>setStage(v))} options={[{value:"",label:label("all_stages")},...options(stages)]} />}
          {view==="queue"&&<><FilterSelect title={label("followup_status")} value={queueStatus} onChange={v=>resetFilter(()=>{setQueueStatus(v);if(v==="done")setOverdue(false);})} options={options(["open","done"])} /><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={overdue} disabled={queueStatus==="done"} onChange={e=>resetFilter(()=>setOverdue(e.target.checked))} />{label("overdue_only")}</label></>}
        </div>
        {view==="clients"&&(clients.isPending?<LoadingSpinner ariaLabel={t("status.loading")} />:clients.isError?retry(clients.error,()=>void clients.refetch()):<><p className="text-xs text-stone-500">{label("records")}: {clients.data.total}</p>{!clients.data.items.length?<EmptyState title={label("no_clients")} description={label("empty_clients_hint")} />:<div className="space-y-2">{clients.data.items.map(c=><CompactCard key={c.id} title={c.name} subtitle={[c.email,c.metadata?.crm?.industry].filter(Boolean).join(" · ")||label("no_details")} meta={`${label(c.metadata?.crm?.lifecycle||"lead")} · ${ownerName(c.metadata?.crm?.owner_id)}`} onClick={()=>openClient(c.id)} />)}</div>}<Pager offset={offset} total={clients.data.total} onChange={setOffset} /></>)}
        {view==="pipeline"&&(pipeline.isPending?<LoadingSpinner ariaLabel={t("status.loading")} />:pipeline.isError?retry(pipeline.error,()=>void pipeline.refetch()):<>{!pipeline.data.items.length?<EmptyState title={label("no_opportunities")} description={label("root_record_hint")} />:<div className="space-y-2">{pipeline.data.items.map(d=>dealRow(d))}</div>}<Pager offset={offset} total={pipeline.data.total} onChange={setOffset} /></>)}
        {view==="queue"&&<>{queue.isPending?<LoadingSpinner ariaLabel={t("status.loading")} />:queue.isError?retry(queue.error,()=>void queue.refetch()):<>{!queue.data.items.length?<EmptyState title={label("no_followups")} description={label("root_record_hint")} />:<div className="space-y-2">{queue.data.items.map(d=>taskRow(d))}</div>}<Pager offset={offset} total={queue.data.total} onChange={setOffset} /></>}</>}
      </> : selected.isPending ? <LoadingSpinner ariaLabel={t("status.loading")} /> : selected.isError ? retry(selected.error,()=>void selected.refetch()) : selected.data&&p&&<>
        {!active&&<p role="status" className="text-sm text-stone-500">{label("archived_hint")}</p>}
        {section==="profile"&&<><div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card minHeight={0} title={label("company_profile")} hoverable={false} headerAction={
            <Tooltip content={label("edit_profile")} position="bottom" hideOnClick>
              <Button variant="ghost" ariaLabel={label("edit_profile")} aria-haspopup="dialog" disabled={busy||owners.isError||owners.isPending} style={{width:36,padding:0,justifyContent:"center"}} onClick={()=>{setError("");setProfileOpen(true);}}>
                <span aria-hidden="true"><IconEdit size={16} /></span>
              </Button>
            </Tooltip>
          }>
            <div className="mt-4"><CrmDetailFields fields={[
              ...(["email","phone","industry","website","company_size","source","address"] as const).map(key=>({label:label(key),value:p[key]})),
              {label:label("lifecycle"),value:label(p.lifecycle)},
              {label:label("owner"),value:ownerName(p.owner_id)},
              {label:label("tags"),value:p.tags.join(" · ")},
            ]} /></div>
          </Card>
          <Card minHeight={0} title={label("notes")} hoverable={false}><p className="text-sm whitespace-pre-wrap break-words mt-4">{p.notes||label("no_notes")}</p></Card>
        </div>
          <Button variant="outline" disabled={busy} onClick={()=>{setError("");setConfirmation({title:label(active?"archive":"restore"),body:label(active?"archive_confirm":"restore_confirm"),action:async()=>{const row=await api.crm.profile(clientId,{...p,status:active?"inactive":"active"});await cacheCrmProfile(cache,identity,row);await refresh("archive");setConfirmation(null);}});}}>{label(active?"archive":"restore")}</Button>
        </>}
        {section==="contacts"&&<>{contacts.isPending?<LoadingSpinner ariaLabel={t("status.loading")} />:contacts.isError?retry(contacts.error,()=>void contacts.refetch()):!contacts.data.length?<EmptyState title={label("no_contacts")} />:<div className="space-y-2">{contacts.data.map(contactRow)}</div>}</>}
        {section==="activities"&&(activities.isPending?<LoadingSpinner ariaLabel={t("status.loading")} />:activities.isError?retry(activities.error,()=>void activities.refetch()):<>{!activities.data.items.length?<EmptyState title={label("no_activities")} />:<div className="space-y-2">{activities.data.items.map(activityRow)}</div>}<Pager offset={offset} total={activities.data.total} onChange={setOffset} /></>)}
        {section==="opportunities"&&(opportunities.isPending?<LoadingSpinner ariaLabel={t("status.loading")} />:opportunities.isError?retry(opportunities.error,()=>void opportunities.refetch()):<>{!opportunities.data.items.length?<EmptyState title={label("no_opportunities")} />:<div className="space-y-2">{opportunities.data.items.map(d=>dealRow(d,true))}</div>}<Pager offset={offset} total={opportunities.data.total} onChange={setOffset} /></>)}
        {section==="followups"&&<>{followups.isPending?<LoadingSpinner ariaLabel={t("status.loading")} />:followups.isError?retry(followups.error,()=>void followups.refetch()):<>{!followups.data.items.length?<EmptyState title={label("no_followups")} />:<div className="space-y-2">{followups.data.items.map(d=>taskRow(d,true))}</div>}<Pager offset={offset} total={followups.data.total} onChange={setOffset} /></>}</>}
        {section==="delivery"&&<ClientDeliveries key={clientId} client={selected.data} identity={identity} initialWorkspace={params.get("workspace")||""} setActions={setDeliveryActions} />}
      </>}
    </div>
    <Modal open={profileOpen} onClose={()=>{if(!busy)setProfileOpen(false);}} title={label(clientId?"edit_profile":"add_client")} maxWidth="720px">{profileOpen&&<ProfileForm client={selected.data} owners={ownerOptions} busy={busy} error={error} onSave={data=>void perform(()=>saveProfile(data))} />}</Modal>
    <Modal open={contactOpen} onClose={()=>{if(!busy)setContactOpen(false);}} title={label(contactEdit?"edit_contact":"add_contact")}>{contactOpen&&<ContactForm contact={contactEdit} busy={busy} error={error} onSave={data=>void perform(async()=>{if(contactEdit)await api.clients.editContact(clientId,contactEdit.id,data);else await api.clients.addContact(clientId,data);await refresh("contacts");setContactOpen(false);})} />}</Modal>
    <Modal open={!!editor} onClose={()=>{if(!busy)setEditor(null);}} title={label(editor?.record?"edit":`add_${editor?.kind||"activities"}`)}>{editor&&<RecordForm kind={editor.kind} record={editor.record} owners={ownerOptions} busy={busy} error={error} onSave={data=>void perform(()=>saveRecord(data))} />}</Modal>
    <Modal open={!!confirmation} onClose={()=>{if(!busy)setConfirmation(null);}} title={confirmation?.title||""}><p className="text-sm text-stone-500 mb-4">{confirmation?.body}</p><Button variant="danger" loading={busy} onClick={()=>{if(confirmation)void perform(confirmation.action);}}>{label("confirm")}</Button>{error&&<p role="alert" className="text-sm text-red-700 mt-3">{error}</p>}</Modal>
  </div>;
}
