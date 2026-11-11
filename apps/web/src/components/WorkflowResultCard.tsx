import { useLocation, useNavigate } from "react-router-dom";
import type { WorkflowResultReference } from "../lib/chatStream";
import { preserveReturnToInHistory } from "../lib/chatRouteReferences";
import { t } from "../lib/i18n";
import { usePreviewFeatureAccess } from "../lib/previewFeatureAccess";
import { workflowReferencePath } from "../lib/workflowRoutes";
import { IconFlow } from "./icons";

function workflowResultHref(result: WorkflowResultReference): string {
  return workflowReferencePath(result);
}

/**
 * The run behind a workflow result, as one quiet line under the message. The
 * message already says the result and shows its files, so this line only
 * names the Flow and opens its run; it never repeats the summary.
 */
export default function WorkflowResultCard({
  result,
  returnTo,
}: {
  result: WorkflowResultReference;
  returnTo?: string;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  // The run page is part of Flows. Without it the message above already
  // carries the result and its files, so no line points at a closed page.
  const flows = usePreviewFeatureAccess("flows");
  if (!flows.enabled) return null;
  const href = workflowResultHref(result);
  const currentReturnTo = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const verifiedPublications = Number(result.publication_summary?.verified || 0);
  const name = result.workflow_name || t("component.workflow_result.workflow");

  return (
    <button
      type="button"
      className="chat-workflow-result-link"
      aria-label={`${name} · ${t("component.workflow_result.open_details")}`}
      onClick={() => {
        preserveReturnToInHistory(currentReturnTo);
        navigate(href, {
          state: {
            returnTo: currentReturnTo,
            chatReturnTo: currentReturnTo,
          },
        });
      }}
    >
      <IconFlow size={13} />
      <span className="chat-workflow-result-link__name">{name}</span>
      {result.publication_summary?.count ? (
        <span>{t("component.workflow_result.verified_publications", { count: verifiedPublications })}</span>
      ) : null}
      <span className="chat-workflow-result-link__open">{t("component.workflow_result.view_run")}</span>
    </button>
  );
}
