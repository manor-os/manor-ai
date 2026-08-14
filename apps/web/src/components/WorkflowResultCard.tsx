import { useLocation, useNavigate } from "react-router-dom";
import type { WorkflowResultReference } from "../lib/chatStream";
import { preserveReturnToInHistory } from "../lib/chatRouteReferences";
import { t } from "../lib/i18n";
import { IconFlow } from "./icons";
import CompactCard from "./ui/CompactCard";
import IconTile from "./ui/IconTile";

function workflowResultHref(result: WorkflowResultReference): string {
  const declared = String(result.url || "").trim();
  if (/^\/flows(?:[?#]|$)/.test(declared)) return declared;
  const query = new URLSearchParams();
  query.set("workflow", result.workflow_id);
  if (result.run_id) query.set("run", result.run_id);
  return `/flows?${query.toString()}`;
}

export default function WorkflowResultCard({
  result,
  returnTo,
}: {
  result: WorkflowResultReference;
  returnTo?: string;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const href = workflowResultHref(result);
  const currentReturnTo = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const completed = String(result.status || "completed").toLowerCase() === "completed";
  const subtitle = result.title || result.summary || t("component.workflow_result.open_details");
  const verifiedPublications = Number(result.publication_summary?.verified || 0);
  const receiptMeta = result.publication_summary?.count
    ? t("component.workflow_result.verified_publications", { count: verifiedPublications })
    : t("component.workflow_result.result");

  return (
    <div className="chat-workflow-result-card">
      <CompactCard
        className="chat-workflow-result-card__surface"
        icon={(
          <IconTile
            size={34}
            status={completed ? {
              color: "var(--success)",
              label: t("component.workflow_result.completed"),
            } : undefined}
          >
            <IconFlow size={17} />
          </IconTile>
        )}
        title={result.workflow_name || t("component.workflow_result.workflow")}
        subtitle={subtitle}
        meta={receiptMeta}
        metaTone={completed ? "connected" : "muted"}
        onClick={() => {
          preserveReturnToInHistory(currentReturnTo);
          navigate(href, {
            state: {
              returnTo: currentReturnTo,
              chatReturnTo: currentReturnTo,
            },
          });
        }}
      />
    </div>
  );
}
