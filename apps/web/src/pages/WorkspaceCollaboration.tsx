import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { api, type CollaborationGrant, type CollaborationInvite, type CollaborationCommunication } from "../lib/api";
import { t } from "../lib/i18n";
import { useAuthStore } from "../stores/auth";
import { crmQueryOptions } from "../lib/crmQueries";
import PageHeader from "../components/ui/PageHeader";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Select from "../components/ui/Select";
import Modal from "../components/ui/Modal";
import Card from "../components/ui/Card";
import CompactCard from "../components/ui/CompactCard";
import EmptyState from "../components/ui/EmptyState";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import CurrentEmailVerification from "../components/auth/CurrentEmailVerification";
import { openDetail, closeDetail } from "../stores/detail";
export const collaborationLabel = (key: string) => t(`page.collaboration.${key}`);
const c = collaborationLabel;
const errorText = (error: unknown) => error instanceof Error ? error.message : t("status.error");
function InvitationFields({
  value,
  change,
  locked = false,
  busy = false
}: {
  value: CollaborationInvite;
  change: (value: CollaborationInvite) => void;
  locked?: boolean;
  busy?: boolean;
}) {
  return <div className="space-y-4">
    <label className="block text-sm space-y-2"><span>{c("email")}</span><Input type="email" value={value.email} onChange={event => change({
        ...value,
        email: event.target.value
      })} required disabled={locked || busy} /></label>
    <label className="block text-sm space-y-2"><span>{c("role")}</span><Select ariaLabel={c("role")} value={value.role} onChange={role => change({
        ...value,
        role: role as CollaborationInvite["role"]
      })} disabled={locked || busy} options={[{
        value: "tester",
        label: c("tester")
      }, {
        value: "maintainer",
        label: c("maintainer")
      }]} /></label>
    <label className="block text-sm space-y-2"><span>{c("duration")}</span><Select ariaLabel={c("duration")} value={String(value.days)} onChange={days => change({
        ...value,
        days: Number(days)
      })} disabled={busy} options={[7, 14, 30, 90].map(days => ({
        value: String(days),
        label: c("days").replace("{days}", String(days))
      }))} /></label>
    <label className="flex gap-3 items-start text-sm"><input type="checkbox" checked={value.share_configuration} onChange={event => change({
        ...value,
        share_configuration: event.target.checked
      })} disabled={busy} className="mt-1" /><span>{c("share_configuration")}<span className="block mt-1 text-stone-500">{c("share_hint")}</span></span></label>
  </div>;
}
function GrantDetails({
  grant,
  workspaceId,
  refresh
}: {
  grant: CollaborationGrant;
  workspaceId: string;
  refresh: () => Promise<void>;
}) {
  const [value, setValue] = useState<CollaborationInvite>({
    email: grant.email,
    role: grant.role,
    days: 14,
    share_configuration: grant.share_configuration
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  async function change(action: "authorize" | "revoke" | "update") {
    setBusy(true);
    setError("");
    try {
      await api.collaboration.change(workspaceId, grant.id, action, action !== "revoke" ? value : undefined);
      await refresh();
      closeDetail();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return <div className="space-y-4">
    <p>{c(grant.role)} · {c(`status_${grant.status}`)}</p><p className="text-sm text-stone-500">{c("expires")}: {new Date(grant.expires_at).toLocaleString()}</p>
    {["requested", "invited", "active"].includes(grant.status) && <><InvitationFields value={value} change={setValue} locked busy={busy} /><Button loading={busy} onClick={() => void change(grant.status === "requested" ? "authorize" : "update")}>{c(grant.status === "requested" ? "authorize" : "update_authorization")}</Button></>}
    {["invited", "active"].includes(grant.status) && <Button variant="outline" disabled={busy} onClick={() => {
      setError("");
      void navigator.clipboard.writeText(new URL(grant.path, window.location.origin).href).then(() => setCopied(true)).catch(e => setError(errorText(e)));
    }}>{c(copied ? "copied" : "copy_invite")}</Button>}
    {["requested", "invited", "active"].includes(grant.status) && <Button variant="danger" disabled={busy} onClick={() => void change("revoke")}>{c("revoke")}</Button>}
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
  </div>;
}
function GroupCommunication({ value }: { value: CollaborationCommunication }) {
  const navigate = useNavigate();
  return <div className="space-y-3">
    <h2 className="font-semibold">{c("group_communication")}</h2>
    <p className="text-sm text-stone-500">{c("group_hint")}</p>
    {value.group ? <CompactCard title={value.group.title} subtitle={c("open_group")} onClick={() => navigate(value.group!.path)} />
      : <p role="status" className="text-sm text-stone-500">{c(value.linked ? "group_unavailable" : "no_group")}</p>}
    <p className="text-sm text-stone-500">{c("group_membership_policy")}</p>
  </div>;
}
export function WorkspaceCollaborationManagement({
  workspaceId,
  identity
}: {
  workspaceId: string;
  identity: string;
}) {
  const cache = useQueryClient();
  const key = ["workspace-collaboration-management", identity, workspaceId];
  const query = useQuery({
    ...crmQueryOptions(key, signal => api.collaboration.management(workspaceId, signal)),
    refetchInterval: query => query.state.data?.grants.some(grant => grant.status === "requested") ? 15000 : 60000
  });
  const [groupDialog, setGroupDialog] = useState(false);
  const [groupId, setGroupId] = useState("");
  const [invite, setInvite] = useState(false);
  const [value, setValue] = useState<CollaborationInvite>({
    email: "",
    role: "tester",
    days: 14,
    share_configuration: false
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [link, setLink] = useState("");
  const [phase, setPhase] = useState<"request_acceptance" | "accept" | null>(null);
  const [note, setNote] = useState("");
  const [copied, setCopied] = useState(false);
  async function refresh() {
    await cache.invalidateQueries({
      queryKey: key
    });
  }
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const data = query.data;
  return <div className="space-y-4">
    {query.isPending ? <LoadingSpinner /> : query.isError ? <EmptyState title={query.error.message} action={<Button onClick={() => void query.refetch()}>{c("retry")}</Button>} /> : data && <>
      {data.phase && <Card minHeight={0} hoverable={false} title={`${c("delivery")}: ${c(`phase_${data.phase}`)}`} description={c("acceptance_policy")}
        footer={["trial", "awaiting_acceptance"].includes(data.phase) ? <div className="flex gap-2 flex-wrap">
          {data.phase === "trial" && <Button variant="outline" onClick={() => { setNote(""); setPhase("request_acceptance"); }}>{c("request_acceptance")}</Button>}
          <Button onClick={() => { setNote(""); setPhase("accept"); }}>{c("acceptance")}</Button>
        </div> : undefined}>
        {!!data.acceptance?.confirmed_at && <p className="text-sm text-stone-500">{new Date(String(data.acceptance.confirmed_at)).toLocaleString()} · {String(data.acceptance.note || "")}</p>}
      </Card>}
      <Card minHeight={0} hoverable={false} title={c("people")} description={c("policy")} headerAction={<Button onClick={() => {
        setError(""); setValue({ email: "", role: "tester", days: 14, share_configuration: false }); setInvite(true);
      }}>{c("invite")}</Button>}>
        <div className="mt-3 space-y-3">{!data.grants.length ? <EmptyState compact title={c("no_people")} /> : data.grants.map(grant => <CompactCard key={grant.id} title={grant.email} subtitle={`${c(grant.role)} · ${c("expires")}: ${new Date(grant.expires_at).toLocaleDateString()}`} meta={c(`status_${grant.status}`)} onClick={() => openDetail({ title: grant.email, body: <GrantDetails grant={grant} workspaceId={workspaceId} refresh={refresh} /> })} />)}</div>
      </Card>
      <Card minHeight={0} hoverable={false}>
        <GroupCommunication value={data.communication} />
        <div className="mt-3"><Button variant="outline" onClick={() => {
          setError(""); setGroupId(data.communication.group?.id || ""); setGroupDialog(true);
        }}>{c(data.communication.linked ? "change_group" : "link_group")}</Button></div>
      </Card>
    </>}
    {error && !invite && !phase && !groupDialog && <p role="alert">{error}</p>}
    <Modal open={groupDialog} onClose={() => { if (!busy) setGroupDialog(false); }} title={c("link_group")}>
      <form className="space-y-4" onSubmit={event => {
        event.preventDefault();
        void perform(async () => {
          await api.collaboration.linkGroup(workspaceId, groupId || null);
          await refresh();
          setGroupDialog(false);
        });
      }}>
        <p className="text-sm text-stone-500">{c("link_group_hint")}</p>
        <Select ariaLabel={c("choose_group")} value={groupId} onChange={setGroupId} disabled={busy}
          options={[{value: "", label: c("unlink_group")}, ...(data?.group_options || []).map(group => ({value: group.id, label: group.title}))]} />
        {!data?.group_options.length && <p className="text-sm text-stone-500">{c("no_available_groups")}</p>}
        {error && <p role="alert">{error}</p>}
        <Button type="submit" loading={busy}>{c("save_group")}</Button>
      </form>
    </Modal>
    <Modal open={invite} onClose={() => {
        if (!busy) setInvite(false);
      }} title={c("invite")}><form className="space-y-4" onSubmit={event => {
          event.preventDefault();
          void perform(async () => {
            const grant = await api.collaboration.invite(workspaceId, value);
            setLink(new URL(grant.path, window.location.origin).href);
            setCopied(false);
            setInvite(false);
            await refresh();
          });
        }}><InvitationFields value={value} change={setValue} busy={busy} />{error && <p role="alert">{error}</p>}<Button type="submit" loading={busy}>{c("create_invite")}</Button></form></Modal>
    <Modal open={!!link} onClose={() => setLink("")} title={c("invite_ready")}><div className="space-y-4"><p className="text-sm text-stone-500">{c("invite_policy")}</p><Input ariaLabel={c("invite_link")} value={link} onChange={() => {}} /><Button onClick={() => void perform(async () => {
            await navigator.clipboard.writeText(link);
            setCopied(true);
          })}>{c(copied ? "copied" : "copy_invite")}</Button>{error && <p role="alert">{error}</p>}</div></Modal>
    <Modal open={!!phase} onClose={() => {
        if (!busy) setPhase(null);
      }} title={c(phase === "accept" ? "acceptance" : "request_acceptance")}><div className="space-y-4"><p className="text-sm text-stone-500">{c("acceptance_policy")}</p><label className="block text-sm space-y-2"><span>{c("note")}</span><Textarea value={note} onChange={event => setNote(event.target.value)} disabled={busy} /></label>{error && <p role="alert">{error}</p>}<Button loading={busy} onClick={() => void perform(async () => {
            await api.collaboration.phase(workspaceId, phase!, note);
            setPhase(null);
            await refresh();
          })}>{c("confirm")}</Button></div></Modal>
  </div>;
}
function Portal({
  grantId,
  identity
}: {
  grantId: string;
  identity: string;
}) {
  const [verified, setVerified] = useState(false);
  const onVerified = useCallback((value: boolean) => setVerified(value), []);
  const query = useQuery({
    ...crmQueryOptions(["workspace-collaboration-portal", identity, grantId, verified], signal => api.collaboration.portal(grantId, signal)),
    enabled: verified,
    refetchInterval: query => query.state.data?.grant.status === "requested" ? 15000 : 60000
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const data = query.data;
  const grant = data?.grant;
  return <div className="h-full min-w-0 overflow-auto"><PageHeader title={c("portal")} subtitle={data?.title} /><div className="max-w-2xl space-y-5 pb-6">
    <CurrentEmailVerification onVerified={onVerified} />
    {verified && (query.isPending ? <LoadingSpinner /> : query.isError ? <EmptyState title={c("unavailable")} description={query.error.message} action={<Button onClick={() => void query.refetch()}>{c("retry")}</Button>} /> : data && grant && <>
      <p className="text-sm">{c(grant.role)} · {c(`status_${grant.status}`)} · {c("expires")}: {new Date(grant.expires_at).toLocaleDateString()}</p><p className="text-sm text-stone-500">{c("portal_policy")}</p>
      {grant.status === "requested" ? <p role="status">{c("waiting_authorization")}</p> : grant.status === "invited" ? <Button loading={busy} onClick={() => void perform(async () => {
          await api.collaboration.accept(grantId);
          await query.refetch();
        })}>{c("accept_invite")}</Button> : grant.status === "active" && <>
        <GroupCommunication value={data.communication} />
        {data.configuration && <div className="space-y-3">
          <h2 className="font-semibold">{c("share_configuration")}</h2>
          {[...data.configuration.services, ...data.configuration.processes].map((row, index) =>
            <CompactCard key={String(row.service_key || row.process_key || index)} title={String(row.title || row.name || row.service_key || row.process_key || index + 1)} subtitle={String(row.description || "")} />)}
        </div>}
      </>}
    </>)}
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
  </div></div>;
}
export default function WorkspaceCollaboration() {
  const {
    workspaceId,
    grantId
  } = useParams();
  const identity = useAuthStore(s => `${s.user?.id}:${s.user?.entity_id}`);
  return workspaceId ? <Navigate to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=collaboration`} replace /> : <Portal key={`${identity}:${grantId}`} grantId={grantId!} identity={identity} />;
}
