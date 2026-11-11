import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ClientRecord, type ClientDeliveryReceipt, type ClientDeliveryUpdatePreview } from "../../lib/api";
import { t } from "../../lib/i18n";
import { clientDeliveryPollInterval, crmQueryOptions } from "../../lib/crmQueries";
import Button from "../../components/ui/Button";
import { PageHeaderAddButton } from "../../components/ui/PageHeader";
import Input from "../../components/ui/Input";
import Modal from "../../components/ui/Modal";
import CompactCard from "../../components/ui/CompactCard";
import IconTile from "../../components/ui/IconTile";
import StatusPill from "../../components/ui/StatusPill";
import TabSwitcher from "../../components/ui/TabSwitcher";
import { IconWorkspace, IconMail, IconTrash, IconSend, IconUpload } from "../../components/icons";
import Textarea from "../../components/ui/Textarea";
import Checkbox from "../../components/ui/Checkbox";
import { openDetail, closeDetail } from "../../stores/detail";
import CrmDetailFields from "./CrmDetailFields";
import EmptyState from "../../components/ui/EmptyState";
import LoadingSpinner from "../../components/ui/LoadingSpinner";
import { label } from "./CrmForms";
import ClientDeliveryDialog from "./ClientDeliveryDialog";
import { getPreferredTimeZone } from "../../lib/format";

const c = (key: string) => t(`page.collaboration.${key}`);

export default function ClientDeliveries({client, identity, initialWorkspace, setActions}: {client: ClientRecord; identity: string; initialWorkspace: string; setActions: (actions: ReactNode) => void}) {
  const cache = useQueryClient(); const navigate = useNavigate();
  const contacts = useQuery({...crmQueryOptions(["client-contacts",identity,client.id],signal=>api.clients.contacts(client.id,signal))});
  const deliveries = useQuery({...crmQueryOptions(["client-deliveries",identity,client.id],signal=>api.clients.deliveries(client.id,signal)),refetchInterval:query=>displayReadInterval(query.state,clientDeliveryPollInterval(query.state.data))});
  const [open,setOpen] = useState(false);
  const [busy,setBusy] = useState(false); const [error,setError] = useState("");
  const [link,setLink] = useState(""); const [copied,setCopied] = useState(false);
  const [revoke,setRevoke] = useState<ClientDeliveryReceipt | null>(null);
  // A newer release of the source Workspace; the customer applies it through the Blueprint upgrade.
  const [update,setUpdate] = useState<ClientDeliveryReceipt | null>(null);
  const [notes,setNotes] = useState(""); const [updateResult,setUpdateResult] = useState("");
  // The release comes from the source Workspace, or from a Blueprint file built elsewhere (git, staging).
  const [updateSource,setUpdateSource] = useState<"workspace"|"file">("workspace");
  const [updateFile,setUpdateFile] = useState<File | null>(null);
  const [preview,setPreview] = useState<ClientDeliveryUpdatePreview | null>(null);
  // During a trial the provider may apply the release to the customer's Workspace right away.
  const [applyTrial,setApplyTrial] = useState(true);
  const inTrial = !!update?.target_workspace_id && ["trial","awaiting_acceptance"].includes(update.phase || "");
  const canDeliver = client.status === "active" && !!contacts.data?.length && !busy;
  const deliverLabel = t("page.clients.deliver");
  useEffect(()=>{
    setActions(<PageHeaderAddButton label={deliverLabel} disabled={!canDeliver} onClick={()=>{setError("");setOpen(true);}} />);
    return ()=>setActions(null);
  },[setActions,canDeliver,deliverLabel]);
  async function perform(action:()=>Promise<void>) { setBusy(true);setError("");try{await action();closeDetail();}catch(e){setError(e instanceof Error ? e.message : t("status.error"));}finally{setBusy(false);} }
  async function refresh(){ await cache.invalidateQueries({queryKey:["client-deliveries",identity,client.id]}); }
  // A trial update goes through the customer trial's own scoped session, as if opened from there.
  async function sentMessage(item:ClientDeliveryReceipt,version:string):Promise<string> {
    const sent=`${t("page.clients.update_published",{version})} ${t("page.clients.update_published_chat")}`;
    if(!applyTrial||!item.target_workspace_id||!["trial","awaiting_acceptance"].includes(item.phase || "")) return sent;
    try {
      const session=await api.clientTrials.session(item.source_workspace_id,item.id);
      await api.workspaces.applyRecommendedBlueprintUpgrade(item.target_workspace_id,{target_version:version},session.access_token);
      return t("page.clients.update_applied_trial",{version});
    } catch(e) {
      return t("page.clients.update_trial_not_applied",{version,reason:e instanceof Error?e.message:t("status.error")});
    }
  }
  function showLink(path?:string){if(path){setLink(new URL(path,window.location.origin).href);setCopied(false);}}
  function deliveryRow(item:ClientDeliveryReceipt) {
    const icon=<IconTile size={34}><IconWorkspace size={18} /></IconTile>;
    const status=item.target_workspace_id?c(`phase_${item.phase || "delivered"}`):t(`page.clients.status_${item.status}`);
    const badge=<StatusPill size="sm" status={item.status==="delivered"?"completed":item.status==="pending"?"pending":item.status==="needs_setup"?"needs_setup":undefined} label={status} />;
    const canResend=!!item.can_manage&&["pending","expired"].includes(item.status);
    const accepted=!!item.target_workspace_id;
    const canPublish=!!item.can_manage&&["pending","delivered","needs_setup"].includes(item.status);
    const publish={label:t("page.clients.publish_update"),icon:<IconSend size={16} />,disabled:busy,onClick:()=>{closeDetail();setError("");setNotes("");setUpdateResult("");setUpdateSource("workspace");setUpdateFile(null);setPreview(null);setApplyTrial(true);setUpdate(item);}};
    // The customer trial opens from the source Workspace, in the provider's own company.
    const openTrial={label:t("page.client_trials.open_customer_trial"),icon:<IconWorkspace size={16} />,disabled:busy,onClick:()=>{closeDetail();navigate(`/workspaces/${encodeURIComponent(item.source_workspace_id)}?tab=client-trials&trial=${encodeURIComponent(item.id)}`);}};
    return <CompactCard key={item.id} icon={icon} title={item.title} subtitle={item.recipient_email} meta={badge} onClick={busy?undefined:()=>openDetail({
      key:`crm-delivery-${item.id}`,icon,title:item.title,subtitle:item.recipient_email,badges:badge,
      // An accepted invitation no longer expires and its email no longer matters.
      body:<div className="space-y-4"><CrmDetailFields fields={[{label:t("page.clients.recipient"),value:item.recipient_email},{label:c("delivery"),value:status},{label:t("page.clients.invitation_status"),value:t(`page.clients.status_${item.status}`)},{label:t("page.clients.content_version"),value:item.content_version || "1.0.0",mono:true},...(accepted?[]:[{label:c("expires"),value:new Date(item.expires_at).toLocaleString(undefined, { timeZone: getPreferredTimeZone() })}])]} />{!accepted&&<p className="text-sm text-stone-500">{t(`page.clients.email_${item.email_delivery?.status || "not_sent"}`, {email:item.recipient_email})}</p>}</div>,
      primaryAction:item.can_open_trial?openTrial:item.target_workspace_id&&item.can_manage?{label:c(item.maintenance?"open_maintenance":"request_maintenance"),icon:<IconWorkspace size={16} />,disabled:busy,onClick:()=>{closeDetail();void perform(async()=>{const grant=await api.clients.requestMaintenance(client.id,item.id);await refresh();navigate(grant.path);});}}:canResend?{label:t("page.clients.resend_email"),icon:<IconMail size={16} />,disabled:busy,onClick:()=>{closeDetail();void perform(async()=>{showLink((await api.clients.changeInvite(client.id,item.id,"rotate")).invite_path);await refresh();});}}:undefined,
      secondaryActions:canPublish?[publish]:undefined,
      dangerAction:canResend?{label:t("page.clients.revoke"),icon:<IconTrash size={16} />,disabled:busy,onClick:()=>{setError("");setRevoke(item);}}:undefined,
    })} />;
  }
  return <div className="space-y-4">
    {error && !open && !link && !revoke && !update && <p role="alert" className="text-sm text-red-700">{error}</p>}
    {contacts.isError && <EmptyState title={contacts.error.message} action={<Button onClick={()=>void contacts.refetch()}>{label("retry")}</Button>} />}
    {deliveries.isPending ? <LoadingSpinner ariaLabel={t("status.loading")} /> : deliveries.isError ? <EmptyState title={deliveries.error.message} action={<Button onClick={()=>void deliveries.refetch()}>{label("retry")}</Button>} /> : !deliveries.data.length ? <EmptyState title={t("page.clients.no_deliveries")} /> : <div className="space-y-2">{deliveries.data.map(deliveryRow)}</div>}
    <ClientDeliveryDialog open={open} onClose={()=>setOpen(false)} client={client} initialWorkspace={initialWorkspace} />
    <Modal open={!!link} onClose={()=>setLink("")} title={t("page.clients.invite_ready")}><div className="space-y-4"><p className="text-sm text-stone-500">{t("page.clients.link_policy")}</p><Input ariaLabel={t("page.clients.invite_link")} value={link} onChange={()=>{}} /><Button onClick={()=>void perform(async()=>{await navigator.clipboard.writeText(link);setCopied(true);})}>{t(copied?"page.clients.copied":"page.clients.copy")}</Button>{error && <p role="alert">{error}</p>}</div></Modal>
    <Modal open={!!revoke} onClose={()=>{if(!busy)setRevoke(null);}} title={t("page.clients.revoke")}><p className="text-sm text-stone-500 mb-4">{t("page.clients.revoke_confirm")}</p><Button variant="danger" loading={busy} onClick={()=>{if(revoke)void perform(async()=>{await api.clients.changeInvite(client.id,revoke.id,"revoke");setRevoke(null);await refresh();});}}>{t("page.clients.revoke")}</Button>{error && <p role="alert">{error}</p>}</Modal>
    <Modal open={!!update} onClose={()=>{if(!busy)setUpdate(null);}} title={t("page.clients.publish_update")}><div className="space-y-4">{updateResult ? <p className="text-sm text-stone-500">{updateResult}</p> : <>
      {!inTrial && <p className="text-sm text-stone-500">{t("page.clients.publish_update_confirm",{email:update?.recipient_email || ""})}</p>}
      <TabSwitcher size="sm" tabs={[{key:"workspace",label:t("page.clients.update_source_workspace")},{key:"file",label:t("page.clients.update_source_file")}]} value={updateSource} onChange={key=>{setUpdateSource(key==="file"?"file":"workspace");setPreview(null);setError("");}} />
      {updateSource==="file" && <label className="block cursor-pointer rounded-xl bg-stone-50 p-4">
        <input type="file" accept="application/json,.json,.manor" className="hidden" disabled={busy} onChange={e=>{setUpdateFile(e.target.files?.[0] || null);setPreview(null);setError("");}} />
        <span className="flex items-center gap-2 text-sm font-medium text-stone-800"><IconUpload size={15} />{updateFile?.name || t("page.clients.update_choose_file")}</span>
        <span className="mt-1 block text-xs text-stone-500">{t("page.clients.update_file_help")}</span>
      </label>}
      {updateSource==="file" && preview && <div className="space-y-1 rounded-xl bg-stone-50 p-4 text-sm">
        <p className="font-medium text-stone-800">{preview.would_publish ? t("page.clients.update_preview_version",{version:preview.version || ""}) : t("page.clients.update_unchanged",{version:preview.content_version || "1.0.0"})}</p>
        {preview.would_publish && preview.level && <p className="text-stone-500">{t(`page.blueprints.upgrade_version_${preview.level}`)}</p>}
        {preview.components && <p className="mono text-xs text-stone-500">{t("page.clients.update_preview_components",{added:preview.components.added.length,changed:preview.components.changed.length,removed:preview.components.removed.length})}</p>}
        {!!preview.reasons?.length && <ul className="list-disc pl-5 text-xs text-stone-500">{preview.reasons.slice(0,6).map(reason=><li key={reason}>{reason}</li>)}</ul>}
      </div>}
      <Textarea label={t("page.clients.release_notes")} value={notes} rows={3} onChange={e=>setNotes(e.target.value)} disabled={busy} />
      {inTrial && <div><Checkbox checked={applyTrial} onChange={setApplyTrial} disabled={busy} label={t("page.clients.update_apply_trial")} /></div>}
      {updateSource==="file" && !preview
        ? <Button loading={busy} disabled={!updateFile} onClick={()=>{if(update&&updateFile)void perform(async()=>{setPreview(await api.clients.publishDeliveryUpdateFile(client.id,update.id,updateFile,{dry_run:true}));});}}>{t("page.clients.update_preview")}</Button>
        : <Button loading={busy} disabled={updateSource==="file"&&!preview?.would_publish} onClick={()=>{if(update)void perform(async()=>{const data=notes.trim()?{release_notes:notes.trim()}:{};const result=updateSource==="file"&&updateFile?await api.clients.publishDeliveryUpdateFile(client.id,update.id,updateFile,data):await api.clients.publishDeliveryUpdate(client.id,update.id,data);const version=result.content_version || "1.0.0";setUpdateResult(result.published?await sentMessage(update,version):t("page.clients.update_unchanged",{version}));await refresh();});}}>{t("page.clients.publish_update")}</Button>}
    </>}{error && <p role="alert">{error}</p>}</div></Modal>
  </div>;
}
