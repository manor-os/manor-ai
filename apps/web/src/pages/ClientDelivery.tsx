import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { useAuthStore } from "../stores/auth";
import PageHeader from "../components/ui/PageHeader";
import Button from "../components/ui/Button";
import EmptyState from "../components/ui/EmptyState";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import CurrentEmailVerification from "../components/auth/CurrentEmailVerification";

const c = (key: string) => t(`page.collaboration.${key}`);

export default function ClientDelivery() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const identity = useAuthStore((s) => `${s.user?.id}:${s.user?.entity_id}`);
  const [verified, setVerified] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const onVerified = useCallback((value: boolean) => setVerified(value), []);
  const company = useQuery({ queryKey: ["client-delivery-company", identity], queryFn: () => api.people.me() });
  const invite = useQuery({ queryKey: ["client-delivery-invite", identity, token, verified], queryFn: () => api.clients.invitation(token), enabled: verified });
  const delivered = !!invite.data?.target_workspace_id;
  const trial = invite.data?.phase === "trial" || invite.data?.phase === "awaiting_acceptance";
  return <div className="h-full overflow-auto min-w-0">
    <PageHeader title={t("page.clients.receive")} subtitle={invite.data?.title || t("page.clients.private_invite")} />
    <div className="space-y-5 max-w-xl pb-6">
      <CurrentEmailVerification key={identity} onVerified={onVerified} />
      {verified && (invite.isLoading ? <LoadingSpinner /> : invite.isError ? <EmptyState title={t("page.clients.invite_unavailable")} description={invite.error instanceof Error ? invite.error.message : undefined} action={<Button variant="outline" onClick={() => void invite.refetch()}>{t("page.clients.retry")}</Button>} /> : <>
        <p className="text-sm">{t("page.clients.receiving_company")}: <strong>{company.data?.active_entity?.name}</strong></p>
        {/* Before accepting: which company receives it, and one sentence on the trial. */}
        {!delivered && <>
          <p className="text-sm text-stone-500">{t(trial ? "client_delivery.trial_disclosure" : "page.clients.receive_policy")}</p>
          <Button variant="outline" onClick={() => navigate("/account")}>{t("page.clients.choose_company")}</Button>
        </>}
        {delivered ? <>
          <p role="status" className="text-sm">{trial ? c(`phase_${invite.data!.phase}`) : t(invite.data?.status === "needs_setup" ? "page.clients.status_needs_setup" : "page.clients.status_delivered")}</p>
          <Button variant="outline" disabled={company.data?.active_entity?.id !== invite.data?.target_entity_id} onClick={() => navigate(`/workspaces/${invite.data!.target_workspace_id}?tab=collaboration`)}>{c("manage")}</Button>
          <Button disabled={company.data?.active_entity?.id !== invite.data?.target_entity_id} onClick={() => navigate(`/chat?workspace=${encodeURIComponent(invite.data!.target_workspace_id!)}&setup=1`)}>{t("page.clients.open_workspace")}</Button>
        </> : <Button loading={busy} disabled={!invite.data || company.isLoading} onClick={() => {
          setBusy(true); setError("");
          // The accepted Workspace joins the sidebar now, not when its cached list goes stale.
          void api.clients.acceptDelivery(token).then(() => Promise.all([invite.refetch(), queryClient.invalidateQueries({ queryKey: ["workspaces"] })])).catch((cause) => setError(cause instanceof Error ? cause.message : t("status.error"))).finally(() => setBusy(false));
        }}>{t("page.clients.accept")}</Button>}
      </>)}
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
    </div>
  </div>;
}
