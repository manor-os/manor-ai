import { Link } from "react-router-dom";
import { integrationSetupHref } from "../lib/integrationSetupLinks";
import IntegrationLogo from "./IntegrationLogo";
import "./InlineIntegrationLink.css";

export default function InlineIntegrationLink({ provider, label }: { provider: string; label?: string }) {
  return (
    <Link
      to={integrationSetupHref(provider)}
      className="inline-file-reference-card inline-integration-link"
      onClick={(event) => event.stopPropagation()}
    >
      <span className="inline-file-reference-card__icon" aria-hidden="true">
        <IntegrationLogo provider={provider} size={15} />
      </span>
      <span className="inline-file-reference-card__name">{label || provider}</span>
    </Link>
  );
}
