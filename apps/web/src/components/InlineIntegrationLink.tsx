import { Link } from "react-router-dom";
import { integrationSetupHref } from "../lib/integrationSetupLinks";
import { openIntegrationConnect } from "../lib/integrationConnect";
import IntegrationLogo from "./IntegrationLogo";
import "./InlineIntegrationLink.css";

export default function InlineIntegrationLink({ provider, label }: { provider: string; label?: string }) {
  return (
    <Link
      to={integrationSetupHref(provider)}
      className="inline-file-reference-card inline-integration-link"
      onClick={(event) => {
        event.stopPropagation();
        // Connect right here; a modified click still opens Integrations in a new tab.
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        openIntegrationConnect(provider);
      }}
    >
      <span className="inline-file-reference-card__icon" aria-hidden="true">
        <IntegrationLogo provider={provider} size={15} />
      </span>
      <span className="inline-file-reference-card__name">{label || provider}</span>
    </Link>
  );
}
