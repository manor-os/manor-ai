import { queryOptions, type QueryClient, type QueryKey } from "@tanstack/react-query";
import type { ClientRecord, ClientDeliveryReceipt } from "./api";

export function crmQueryOptions<T>(queryKey: QueryKey, read: (signal: AbortSignal) => Promise<T>) {
  return queryOptions({
    queryKey,
    queryFn: ({ signal }) => read(signal),
    staleTime: 60_000,
    refetchOnWindowFocus: true,
  });
}

export type CrmChange = "profile" | "archive" | "contacts" | "activities" | "opportunities" | "followups";

export async function invalidateCrmQueries(cache: QueryClient, identity: string, clientId: string, change: CrmChange) {
  const company = (key: string) => [key, identity];
  const client = (key: string) => [key, identity, clientId];
  let keys: QueryKey[];
  switch (change) {
    case "profile":
    case "archive":
      keys = [company("crm-clients"), company("crm-overview"), company("crm-pipeline"), company("crm-queue"), client("crm-opportunities"), client("crm-followups")];
      if (change === "archive") keys.push(client("crm-activities"), client("client-contacts"), client("client-deliveries"));
      break;
    case "contacts":
      keys = [client("client-contacts"), client("client-deliveries")];
      break;
    case "activities":
      keys = [client("crm-activities")];
      break;
    case "opportunities":
      keys = [client("crm-opportunities"), client("crm-activities"), company("crm-pipeline"), company("crm-overview")];
      break;
    case "followups":
      keys = [client("crm-followups"), company("crm-queue"), company("crm-overview")];
      break;
  }
  // Profile writes return the authoritative detail. Other views refresh on entry,
  // without holding the saved profile open for unrelated directory requests.
  await Promise.all(keys.map(async queryKey => {
    if (change === "profile" || change === "archive") await cache.cancelQueries({ queryKey });
    await cache.invalidateQueries({
      queryKey,
      refetchType: change === "profile" || change === "archive" ? "none" : "active",
    });
  }));
}

export async function cacheCrmProfile(cache: QueryClient, identity: string, row: ClientRecord) {
  const queryKey = ["crm-client", identity, row.id];
  await cache.cancelQueries({ queryKey, exact: true });
  cache.setQueryData(queryKey, row);
}

export function seedCrmClient(cache: QueryClient, identity: string, row: ClientRecord, updatedAt: number) {
  const queryKey = ["crm-client", identity, row.id];
  if ((cache.getQueryState(queryKey)?.dataUpdatedAt ?? 0) >= updatedAt) return;
  // Preserve the directory's age; an old list must still revalidate the detail.
  cache.setQueryData(queryKey, row, { updatedAt });
}

export function clientDeliveryPollInterval(items: ClientDeliveryReceipt[] | undefined) {
  return items?.some(item => item.status === "pending") ? 15_000 : 60_000;
}
