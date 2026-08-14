import { useLocation, useNavigate } from "react-router-dom";
import type { ChatCreatedResourceReference } from "../lib/chatResourceReferences";
import { preserveReturnToInHistory } from "../lib/chatRouteReferences";
import { t } from "../lib/i18n";
import { IconClock, IconFlow } from "./icons";
import CompactCard from "./ui/CompactCard";
import IconTile from "./ui/IconTile";

export default function CreatedResourceCard({
  resource,
  returnTo,
}: {
  resource: ChatCreatedResourceReference;
  returnTo?: string;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const currentReturnTo = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const isWorkflow = resource.kind === "workflow";

  return (
    <div className="chat-workflow-result-card chat-created-resource-card">
      <CompactCard
        className="chat-workflow-result-card__surface"
        icon={(
          <IconTile
            size={34}
            status={{
              color: "var(--success)",
              label: t("component.created_resource.created"),
            }}
          >
            {isWorkflow ? <IconFlow size={17} /> : <IconClock size={17} />}
          </IconTile>
        )}
        title={resource.name}
        subtitle={resource.subtitle || t(
          isWorkflow
            ? "component.created_resource.open_workflow"
            : "component.created_resource.open_automation",
        )}
        meta={t(
          isWorkflow
            ? "component.created_resource.workflow"
            : "component.created_resource.automation",
        )}
        metaTone="connected"
        onClick={() => {
          preserveReturnToInHistory(currentReturnTo);
          navigate(resource.href, {
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
