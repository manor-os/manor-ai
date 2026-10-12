import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import LoadingSpinner from "../ui/LoadingSpinner";
import Select from "../ui/Select";

interface SubscriptionChoice {
  service_key: string;
  agent_slug?: string;
}

export function blueprintAgentSubscriptions(payload: Record<string, unknown>): SubscriptionChoice[] {
  const recipe = payload.recipe;
  if (!recipe || typeof recipe !== "object") return [];
  const subscriptions = (recipe as Record<string, unknown>).subscriptions;
  if (!Array.isArray(subscriptions)) return [];
  return subscriptions.filter((item): item is SubscriptionChoice => (
    item !== null
    && typeof item === "object"
    && typeof item.service_key === "string"
    && item.service_key.length > 0
  ));
}

export default function BlueprintAgentSelection({
  open,
  subscriptions,
  selectedAgentIds,
  onChange,
}: {
  open: boolean;
  subscriptions: SubscriptionChoice[];
  selectedAgentIds: Record<string, string>;
  onChange: (value: Record<string, string>) => void;
}) {
  const agentsQuery = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
    enabled: open && subscriptions.length > 0,
  });
  if (!subscriptions.length) return null;

  const options = [
    { value: "", label: t("component.blueprint_agent_selection.default") },
    ...(agentsQuery.data ?? [])
      .filter((agent) => agent.status === "active")
      .map((agent) => ({ value: agent.id, label: agent.name })),
  ];

  return (
    <section aria-label={t("component.blueprint_agent_selection.title")}>
      <h4 style={{ fontSize: 13, fontWeight: 600, margin: "0 0 2px" }}>
        {t("component.blueprint_agent_selection.title")}
      </h4>
      <p style={{ fontSize: 12, color: "var(--text-muted)", margin: "0 0 10px" }}>
        {t("component.blueprint_agent_selection.description")}
      </p>
      {agentsQuery.isPending ? <LoadingSpinner /> : agentsQuery.isError ? (
        <div role="alert" style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 12, color: "var(--text-default)" }}>
            {t("component.blueprint_agent_selection.load_failed")}
          </span>
          <Button size="sm" variant="outline" onClick={() => void agentsQuery.refetch()}>
            {t("page.blueprints.refresh_status")}
          </Button>
        </div>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {subscriptions.map((subscription) => (
            <div key={subscription.service_key} style={{ display: "grid", gap: 4 }}>
              <span style={{ color: "var(--text-strong)", fontSize: 12, fontWeight: 600 }}>
                {subscription.service_key}
              </span>
              {subscription.agent_slug && (
                <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
                  {t("component.blueprint_agent_selection.source", { agent: subscription.agent_slug })}
                </span>
              )}
              <Select
                value={selectedAgentIds[subscription.service_key] || ""}
                onChange={(agentId) => {
                  const next = { ...selectedAgentIds };
                  if (agentId) next[subscription.service_key] = agentId;
                  else delete next[subscription.service_key];
                  onChange(next);
                }}
                options={options}
                filterable
                ariaLabel={t("component.blueprint_agent_selection.choose", { service: subscription.service_key })}
              />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
