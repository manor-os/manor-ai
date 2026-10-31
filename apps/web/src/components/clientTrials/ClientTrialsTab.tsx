import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ClientTrial } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import CompactCard from "../ui/CompactCard";
import EmptyState from "../ui/EmptyState";
import LoadingSpinner from "../ui/LoadingSpinner";
import ClientTrialFrame, { ClientTrialPhasePill } from "./ClientTrialFrame";
import { openTrialFor, trialListError } from "./clientTrialView";

export const clientTrialsKey = (identity: string, workspaceId: string) => ["client-trials", identity, workspaceId];

/** Deliveries of this source Workspace the provider team can open now. */
export function useClientTrials(workspaceId: string, identity: string, enabled: boolean) {
  return useQuery({
    queryKey: clientTrialsKey(identity, workspaceId),
    queryFn: ({ signal }) => api.clientTrials.list(workspaceId, signal),
    enabled,
    staleTime: 60_000,
    refetchOnWindowFocus: true,
    // A refusal (not a manager here) is an answer, not a failure to retry.
    retry: (count, error) => trialListError(error) === "load_failed" && count < 2,
  });
}

const customer = (item: ClientTrial) => item.customer_name || item.client_name || item.title || item.delivery_id;

/** The provider's "Customer trials" tab: a compact list; `trialId` opens one in the frame. */
export default function ClientTrialsTab({
  workspaceId,
  identity,
  trialId,
  onOpen,
  onClose,
}: {
  workspaceId: string;
  identity: string;
  trialId: string | null;
  onOpen: (deliveryId: string) => void;
  onClose: () => void;
}) {
  const cache = useQueryClient();
  const trials = useClientTrials(workspaceId, identity, true);
  const [opened, setOpened] = useState<ClientTrial | null>(null);
  const open = openTrialFor(trialId, trials.data, opened);
  useEffect(() => {
    if (open?.delivery_id !== opened?.delivery_id) setOpened(open);
  }, [open, opened]);

  if (open) {
    return (
      <ClientTrialFrame
        key={open.delivery_id}
        kind="client-trial"
        refId={open.delivery_id}
        targetWorkspaceId={open.target_workspace_id}
        customerName={customer(open)}
        phase={trials.data?.find((item) => item.delivery_id === open.delivery_id)?.phase ?? open.phase}
        mint={() => api.clientTrials.session(workspaceId, open.delivery_id)}
        onClose={() => {
          // The list catches up only once the frame (and its ended state) is closed.
          setOpened(null);
          onClose();
          void cache.invalidateQueries({ queryKey: clientTrialsKey(identity, workspaceId) });
        }}
      />
    );
  }
  if (trials.isPending) return <LoadingSpinner ariaLabel={t("status.loading")} />;
  if (trials.isError) {
    return trialListError(trials.error) === "no_access"
      ? <p role="status" className="text-sm text-stone-500">{t("page.client_trials.no_access")}</p>
      : <EmptyState title={t("page.client_trials.load_failed")} action={<Button onClick={() => void trials.refetch()}>{t("page.collaboration.retry")}</Button>} />;
  }
  if (trialId) {
    return <EmptyState title={t("page.client_trials.unavailable")} action={<Button variant="outline" onClick={onClose}>{t("page.client_trials.close")}</Button>} />;
  }
  if (!trials.data.length) return <EmptyState title={t("page.client_trials.empty")} />;
  return (
    <div className="space-y-2">
      {trials.data.map((item) => (
        <CompactCard
          key={item.delivery_id}
          title={customer(item)}
          meta={<ClientTrialPhasePill phase={item.phase} />}
          action={<Button size="sm" onClick={() => onOpen(item.delivery_id)}>{t("page.client_trials.open")}</Button>}
        />
      ))}
    </div>
  );
}
