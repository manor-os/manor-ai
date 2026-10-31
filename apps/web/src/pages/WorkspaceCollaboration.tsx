import { useCallback, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { api, type CollaborationGrant, type CollaborationInvite, type CollaborationCommunication, type ProviderTeam } from "../lib/api";
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
import RadioCard from "../components/ui/RadioCard";
import CompactCard from "../components/ui/CompactCard";
import EmptyState from "../components/ui/EmptyState";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import CurrentEmailVerification from "../components/auth/CurrentEmailVerification";
import { openDetail, closeDetail } from "../stores/detail";
import ClientTrialFrame from "../components/clientTrials/ClientTrialFrame";
import { getPreferredTimeZone } from "../lib/format";
export const collaborationLabel = (key: string) => t(`page.collaboration.${key}`);
const c = collaborationLabel;
const errorText = (error: unknown) => error instanceof Error ? error.message : t("status.error");
// A null expiry never lapses: the provider's trial grant and a kept maintainer.
const expiryText = (grant: CollaborationGrant, withTime = false) => grant.expires_at === null ? c("no_expiry")
  : withTime ? new Date(grant.expires_at).toLocaleString(undefined, { timeZone: getPreferredTimeZone() }) : new Date(grant.expires_at).toLocaleDateString(undefined, { timeZone: getPreferredTimeZone() });
const NO_EXPIRY = "none";
function InvitationFields({
  value,
  change,
  locked = false,
  busy = false,
  allowNoExpiry = false
}: {
  value: CollaborationInvite;
  change: (value: CollaborationInvite) => void;
  locked?: boolean;
  busy?: boolean;
  // Only a grant that already has no expiry may keep none.
  allowNoExpiry?: boolean;
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
    <label className="block text-sm space-y-2"><span>{c("duration")}</span><Select ariaLabel={c("duration")} value={value.days === null ? NO_EXPIRY : String(value.days)} onChange={days => change({
        ...value,
        days: days === NO_EXPIRY ? null : Number(days)
      })} disabled={busy} options={[...(allowNoExpiry ? [{
        value: NO_EXPIRY,
        label: c("no_expiry")
      }] : []), ...[7, 14, 30, 90].map(days => ({
        value: String(days),
        label: c("days").replace("{days}", String(days))
      }))]} /></label>
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
    role: grant.role === "provider_trial" ? "maintainer" : grant.role,
    // A grant without expiry (a kept maintainer) keeps none unless a duration is chosen.
    days: grant.expires_at === null ? null : 14,
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
  // The trial grant is decided only at acceptance; every grant action is refused (409).
  const managed = grant.role !== "provider_trial";
  return <div className="space-y-4">
    <p>{c(grant.role)} · {c(`status_${grant.status}`)}</p><p className="text-sm text-stone-500">{c("expires")}: {expiryText(grant, true)}</p>
    {!managed && <p className="text-sm text-stone-500">{c("provider_trial_note")}</p>}
    {managed && ["requested", "invited", "active"].includes(grant.status) && <><InvitationFields value={value} change={setValue} locked busy={busy} allowNoExpiry={grant.expires_at === null} /><Button loading={busy} onClick={() => void change(grant.status === "requested" ? "authorize" : "update")}>{c(grant.status === "requested" ? "authorize" : "update_authorization")}</Button></>}
    {managed && ["invited", "active"].includes(grant.status) && <Button variant="outline" disabled={busy} onClick={() => {
      setError("");
      void navigator.clipboard.writeText(new URL(grant.path, window.location.origin).href).then(() => setCopied(true)).catch(e => setError(errorText(e)));
    }}>{c(copied ? "copied" : "copy_invite")}</Button>}
    {managed && ["requested", "invited", "active"].includes(grant.status) && <Button variant="danger" disabled={busy} onClick={() => void change("revoke")}>{c("revoke")}</Button>}
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
  </div>;
}
function GroupCommunication({ value }: { value: CollaborationCommunication }) {
  const navigate = useNavigate();
  return <div className="space-y-3">
    <h2 className="font-semibold">{c("group_communication")}</h2>
    {/* One line: the linked Group, or one short sentence. */}
    {value.group ? <CompactCard title={value.group.title} subtitle={c("open_group")} onClick={() => navigate(value.group!.path)} />
      : <p role="status" className="text-sm text-stone-500">{c(value.linked ? "group_unavailable" : "no_group")}</p>}
  </div>;
}
/**
 * Everyone of the provider company who works in this Workspace for the
 * delivery. The customer decides on them as one team: during the trial they
 * stay (acceptance decides), after a kept delivery Update and Remove apply to all.
 */
function ProviderTeamCard({ team, grants, workspaceId, refresh }: {
  team: ProviderTeam;
  grants: CollaborationGrant[];
  workspaceId: string;
  refresh: () => Promise<void>;
}) {
  const [dialog, setDialog] = useState<"update" | "remove" | null>(null);
  const [value, setValue] = useState<CollaborationInvite>({ email: "", role: "maintainer", days: null, share_configuration: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const kept = team.access === "maintainer";
  const anchor = team.members[0];
  const anchorGrant = grants.find(grant => grant.id === anchor.grant_id);
  async function run(action: "update" | "revoke") {
    setBusy(true);
    setError("");
    try {
      // The server applies either action to every grant of the team.
      await api.collaboration.change(workspaceId, anchor.grant_id, action, action === "update" ? value : undefined);
      await refresh();
      setDialog(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return <>
    <Card minHeight={0} hoverable={false} title={t("page.collaboration.provider_team", { count: team.members.length })}
      headerAction={kept ? <div className="flex gap-2">
        <Button variant="outline" size="sm" onClick={() => {
          setError("");
          setValue({ email: anchor.email, role: "maintainer", days: anchorGrant?.expires_at ? 14 : null, share_configuration: !!anchorGrant?.share_configuration });
          setDialog("update");
        }}>{t("action.update")}</Button>
        <Button variant="danger" size="sm" onClick={() => { setError(""); setDialog("remove"); }}>{c("remove_provider_team")}</Button>
      </div> : undefined}>
      <ul aria-label={c("provider_team_members")} className="mt-2 space-y-1 text-sm">
        {team.members.map(member => <li key={member.grant_id} className="truncate">{member.email}</li>)}
      </ul>
      {!kept && <p className="mt-2 text-sm text-stone-500">{c("provider_trial_note")}</p>}
    </Card>
    <Modal open={dialog === "update"} onClose={() => { if (!busy) setDialog(null); }} title={c("update_authorization")}>
      <div className="space-y-4">
        <InvitationFields value={value} change={setValue} locked busy={busy} allowNoExpiry={!anchorGrant?.expires_at} />
        {error && <p role="alert">{error}</p>}
        <Button loading={busy} onClick={() => void run("update")}>{t("action.update")}</Button>
      </div>
    </Modal>
    <Modal open={dialog === "remove"} onClose={() => { if (!busy) setDialog(null); }} title={c("remove_provider_team")}>
      <p className="text-sm text-stone-500 mb-4">{c("remove_provider_team_confirm")}</p>
      <Button variant="danger" loading={busy} onClick={() => void run("revoke")}>{c("remove_provider_team")}</Button>
      {error && <p role="alert">{error}</p>}
    </Modal>
  </>;
}
export function WorkspaceCollaborationManagement({
  workspaceId,
  identity,
  deliverCard
}: {
  workspaceId: string;
  identity: string;
  /** Shown only for a Workspace this company made, not one delivered to it. */
  deliverCard?: ReactNode;
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
  const [provider, setProvider] = useState<"keep" | "remove" | null>(null);
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
  // The provider team is decided on as a whole, in its own card.
  const team = data?.provider_team;
  const people = (data?.grants || []).filter(grant => !grant.team);
  // Acceptance must decide the provider's live trial access: keep or remove.
  const decideProvider = phase === "accept" && !!data?.grants.some(grant => grant.role === "provider_trial" && grant.status === "active");
  function openPhase(next: "request_acceptance" | "accept") {
    setNote(""); setProvider(null); setPhase(next);
  }
  return <div className="space-y-4">
    {query.isPending ? <LoadingSpinner /> : query.isError ? <EmptyState title={query.error.message} action={<Button onClick={() => void query.refetch()}>{c("retry")}</Button>} /> : data && <>
      {!data.phase && deliverCard}
      {data.phase && <Card minHeight={0} hoverable={false} title={`${c("delivery")}: ${c(`phase_${data.phase}`)}`}
        footer={["trial", "awaiting_acceptance"].includes(data.phase) ? <div className="flex gap-2 flex-wrap">
          {data.phase === "trial" && <Button variant="outline" onClick={() => openPhase("request_acceptance")}>{c("request_acceptance")}</Button>}
          <Button onClick={() => openPhase("accept")}>{c("acceptance")}</Button>
        </div> : undefined}>
        {!!data.acceptance?.confirmed_at && <p className="text-sm text-stone-500">{[`${c("accepted_on")}: ${new Date(String(data.acceptance.confirmed_at)).toLocaleString(undefined, { timeZone: getPreferredTimeZone() })}`, String(data.acceptance.note || "").trim()].filter(Boolean).join(" · ")}</p>}
      </Card>}
      {!!team?.members.length && <ProviderTeamCard team={team} grants={data.grants} workspaceId={workspaceId} refresh={refresh} />}
      <Card minHeight={0} hoverable={false} title={c("people")} description={c("policy")} headerAction={<Button onClick={() => {
        setError(""); setValue({ email: "", role: "tester", days: 14, share_configuration: false }); setInvite(true);
      }}>{c("invite")}</Button>}>
        <div className="mt-3 space-y-3">{!people.length ? <EmptyState compact title={c("no_people")} /> : people.map(grant => <CompactCard key={grant.id} title={grant.email} subtitle={`${c(grant.role)} · ${c("expires")}: ${expiryText(grant)}`} meta={c(`status_${grant.status}`)} onClick={() => openDetail({ title: grant.email, body: <GrantDetails grant={grant} workspaceId={workspaceId} refresh={refresh} /> })} />)}</div>
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
      }} title={c(phase === "accept" ? "acceptance" : "request_acceptance")}><div className="space-y-4"><p className="text-sm text-stone-500">{c("acceptance_policy")}</p><label className="block text-sm space-y-2"><span>{c("note")}</span><Textarea value={note} onChange={event => setNote(event.target.value)} disabled={busy} /></label>
          {decideProvider && <div role="radiogroup" aria-label={c("provider_decision")} className="space-y-1">
            <p className="text-sm">{c("provider_decision")}</p>
            <RadioCard selected={provider === "keep"} onSelect={() => setProvider("keep")} disabled={busy}><span className="text-sm">{c("provider_keep")}</span></RadioCard>
            <RadioCard selected={provider === "remove"} onSelect={() => setProvider("remove")} disabled={busy}><span className="text-sm">{c("provider_remove")}</span></RadioCard>
          </div>}
          {error && <p role="alert">{error}</p>}<Button loading={busy} disabled={decideProvider && !provider} onClick={() => void perform(async () => {
            await api.collaboration.phase(workspaceId, phase!, note, decideProvider ? provider! : undefined);
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
  // The opened Workspace, kept while the frame shows its ended state (refetches may revoke the grant).
  const [opened, setOpened] = useState<string | null>(null);
  // An accepted maintainer works in the customer's Workspace from here, in the
  // embedded frame; their own company never changes.
  const opensWorkspace = grant?.status === "active" && grant.role !== "tester";
  if (opened) {
    return <div className="flex h-full min-w-0 flex-col pb-4">
      <ClientTrialFrame kind="collaboration" refId={grantId} targetWorkspaceId={opened} customerName={null}
        mint={() => api.collaboration.session(grantId)} onClose={() => { setOpened(null); void query.refetch(); }} />
    </div>;
  }
  return <div className="h-full min-w-0 overflow-auto"><PageHeader title={c("portal")} subtitle={data?.title} actions={opensWorkspace ? <Button size="sm" onClick={() => setOpened(grant!.workspace_id)}>{c("open_workspace")}</Button> : undefined} /><div className="max-w-2xl space-y-5 pb-6">
    <CurrentEmailVerification onVerified={onVerified} />
    {verified && (query.isPending ? <LoadingSpinner /> : query.isError ? <EmptyState title={c("unavailable")} description={query.error.message} action={<Button onClick={() => void query.refetch()}>{c("retry")}</Button>} /> : data && grant && <>
      <p className="text-sm">{c(grant.role)} · {c(`status_${grant.status}`)} · {c("expires")}: {expiryText(grant)}</p><p className="text-sm text-stone-500">{c("portal_policy")}</p>
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
