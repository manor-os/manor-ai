import { useQuery } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { formatDateLong } from "../../lib/format";
import { t } from "../../lib/i18n";
import { decisionLabel, initiativeRunItems } from "../../lib/agentInitiativeFormat";
import type { AgentInitiativeRun } from "../../lib/agentInitiativeTypes";
import Button from "../ui/Button";
import EmptyState from "../ui/EmptyState";
import LoadingSpinner from "../ui/LoadingSpinner";
import StatusPill from "../ui/StatusPill";
import { IconClock, IconRefresh } from "../icons";

function runStatus(run: AgentInitiativeRun): string {
  if (run.status === "admitted") return "pending";
  if (run.status === "running") return "in_progress";
  if (run.status === "succeeded" || run.status === "filtered") return "completed";
  return run.status;
}

function runStatusLabel(status: string): string {
  const key = `page.agent_initiative.run_status_${status}`;
  const value = t(key);
  return value === key ? status.replaceAll("_", " ") : value;
}

function deliveryLabel(kind: string): string {
  const key = `page.agent_initiative.delivery_${kind}`;
  const value = t(key);
  return value === key ? kind.replaceAll("_", " ") : value;
}

export default function AgentInitiativeRunHistory({ initiativeId }: { initiativeId: string }) {
  const query = useQuery({
    queryKey: ["agent-initiative-runs", initiativeId],
    queryFn: () => api.agentInitiatives.runs(initiativeId, { limit: 20 }),
    enabled: !!initiativeId,
  });
  const runs = initiativeRunItems(query.data);

  return (
    <section className="space-y-3" aria-labelledby={`initiative-runs-${initiativeId}`}>
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 id={`initiative-runs-${initiativeId}`} className="m-0 text-sm font-bold text-stone-800">
            {t("page.agent_initiative.run_history")}
          </h3>
          <p className="mt-1 text-xs leading-relaxed text-stone-500">
            {t("page.agent_initiative.run_history_description")}
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => query.refetch()}
          disabled={query.isFetching}
          ariaLabel={t("page.agent_initiative.refresh_runs")}
          title={t("page.agent_initiative.refresh_runs")}
        >
          <IconRefresh size={14} />
        </Button>
      </div>

      {query.isLoading ? (
        <div className="flex min-h-24 items-center justify-center" aria-live="polite">
          <LoadingSpinner size={20} />
        </div>
      ) : query.error ? (
        <div className="rounded-xl bg-red-50/70 p-3" role="alert">
          <p className="m-0 text-xs text-red-700">
            {translateApiError(query.error, t("page.agent_initiative.runs_load_failed"))}
          </p>
          <Button variant="ghost" size="sm" className="mt-2" onClick={() => query.refetch()}>
            {t("page.agent_initiative.retry")}
          </Button>
        </div>
      ) : runs.length === 0 ? (
        <EmptyState
          icon={<IconClock size={20} />}
          title={t("page.agent_initiative.no_runs")}
          description={t("page.agent_initiative.no_runs_description")}
        />
      ) : (
        <div className="divide-y divide-stone-200/60">
          {runs.map((run) => (
            <article key={run.id} className="py-3 first:pt-0 last:pb-0">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill status={runStatus(run)} label={runStatusLabel(run.status)} size="sm" />
                  {(run.final_decision || run.decision || run.candidate_decision?.decision) ? (
                    <span className="rounded-full bg-stone-100 px-2 py-0.5 text-[10px] font-semibold text-stone-600">
                      {decisionLabel(run.final_decision || run.decision || run.candidate_decision?.decision)}
                    </span>
                  ) : null}
                </div>
                <time className="font-mono text-[10px] tabular-nums text-stone-400" dateTime={run.completed_at || run.started_at || run.created_at || undefined}>
                  {formatDateLong(run.completed_at || run.started_at || run.created_at || undefined)}
                </time>
              </div>

              {run.reason || run.candidate_decision?.reason || run.reason_code ? (
                <p className="mt-2 text-xs leading-relaxed text-stone-600">
                  {run.reason || run.candidate_decision?.reason || run.reason_code}
                </p>
              ) : null}
              {(run.message || run.candidate_decision?.message) ? (
                <p className="mt-2 whitespace-pre-wrap rounded-lg bg-stone-50 px-3 py-2 text-xs leading-relaxed text-stone-700">
                  {run.message || run.candidate_decision?.message}
                </p>
              ) : null}
              {run.error ? (
                <p className="mt-2 text-xs leading-relaxed text-red-700" role="alert">
                  {run.error}
                </p>
              ) : null}

              {run.deliveries?.length ? (
                <div className="mt-2 flex flex-wrap gap-1.5" aria-label={t("page.agent_initiative.deliveries")}>
                  {run.deliveries.map((delivery) => (
                    <span key={delivery.id} className="rounded-full bg-stone-100 px-2 py-1 text-[10px] text-stone-600">
                      {deliveryLabel(delivery.kind)} · {delivery.status.replaceAll("_", " ")}
                    </span>
                  ))}
                </div>
              ) : null}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
