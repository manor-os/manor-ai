import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type IntegrationMCPServer, type WorkspaceConnectionRequirement } from "../../lib/api";
import { t } from "../../lib/i18n";
import { INTEGRATION_CATALOG_QUERY_KEY, integrationCatalogQueryOptions } from "../../lib/integrationCatalog";
import { integrationSetupHref } from "../../lib/integrationSetupLinks";
import { useToastStore } from "../../stores/toast";
import IntegrationLogo from "../IntegrationLogo";
import { useNangoConnect } from "../integrations/NangoConnectButton";
import Button from "../ui/Button";

type ChromeStepsComponent = (props: { onReady: () => void }) => JSX.Element | null;

function chromeSteps(): ChromeStepsComponent | null {
  return null;
}

function isChromeRequirement(requirement: WorkspaceConnectionRequirement): boolean {
  return requirement.kind === "integration"
    && (requirement.provider === "chrome" || requirement.setup_kind === "browser_extension");
}

function requirementHint(requirement: WorkspaceConnectionRequirement): string {
  if (isChromeRequirement(requirement)) return t("component.workspace_app_view.connections_hint_chrome");
  if (requirement.kind === "channel") return t("component.workspace_app_view.connections_hint_channel");
  if (requirement.kind === "browser_session") return t("component.workspace_app_view.connections_hint_session");
  return t("component.workspace_app_view.connections_hint_integration");
}

/**
 * One thing the Workspace's Blueprint needs connected, set up without leaving
 * the App: Chrome's steps open in place, Nango opens its popup, direct OAuth
 * returns here, and channels or sessions go to their setup page.
 */
export default function ConnectionRow({ workspaceId, requirement }: {
  workspaceId: string;
  requirement: WorkspaceConnectionRequirement;
}) {
  const [expanded, setExpanded] = useState(false);
  const onToggle = () => setExpanded((value) => !value);
  const catalog = useQuery(integrationCatalogQueryOptions(requirement.kind === "integration"));
  const server: IntegrationMCPServer | undefined = catalog.data?.find((item) => item.server_key === requirement.provider);
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const name = requirement.label || requirement.provider;
  const [done, setDone] = useState(false);
  const connected = () => {
    setDone(true);
    toast.success(t("component.workspace_app_view.connections_connected", { name }));
    void queryClient.invalidateQueries({ queryKey: ["workspace-connection-status"] });
    void queryClient.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
  };
  const nangoKey = requirement.kind === "integration" ? server?.nango_provider_config_key || undefined : undefined;
  const nango = useNangoConnect({ providerConfigKeys: nangoKey ? [nangoKey] : undefined, onConnected: connected });
  const [redirecting, setRedirecting] = useState(false);
  const Steps = isChromeRequirement(requirement) ? chromeSteps() : null;
  const label = t("component.workspace_app_view.connections_connect");
  const go = (path: string) => <Button size="sm" variant="outline" onClick={() => navigate(path)}>{label}</Button>;

  let action;
  if (done) {
    action = <span className="workspace-app-status workspace-app-status--done">{t("status.connected")}</span>;
  } else if (Steps) {
    action = <Button size="sm" variant={expanded ? "ghost" : "outline"} onClick={onToggle} aria-expanded={expanded}>
      {expanded ? t("action.cancel") : label}
    </Button>;
  } else if (requirement.setup_kind === "mcp_binding") {
    action = go(`/workspaces/${workspaceId}?tab=capabilities`);
  } else if (requirement.kind === "channel" && requirement.setup_kind !== "platform_access") {
    action = go(`/workspaces/${workspaceId}?tab=channels`);
  } else if (requirement.kind === "browser_session") {
    action = go("/browser/sessions");
  } else if (nangoKey) {
    action = <Button size="sm" variant="outline" onClick={nango.begin} loading={nango.isWorking}>{label}</Button>;
  } else if (server?.auth_type === "oauth2" && server.oauth_configured) {
    // The provider sends the person straight back to this Workspace.
    const start = async () => {
      setRedirecting(true);
      try {
        const { authorize_url } = await api.integrations.oauthStart(server.server_key, {
          returnTo: `${location.pathname}${location.search}`,
        });
        window.location.href = authorize_url;
      } catch (error) {
        setRedirecting(false);
        toast.error(t("component.workspace_app_view.connections_failed", { name }),
          error instanceof Error ? error.message : undefined);
      }
    };
    action = <Button size="sm" variant="outline" onClick={() => void start()} loading={redirecting}>{label}</Button>;
  } else {
    // No in-place flow on this deployment (for example OAuth not configured).
    action = go(integrationSetupHref(requirement.provider));
  }

  return <li className="workspace-app-group__row workspace-app-connections__row">
    <span className="workspace-app-group__icon" aria-hidden="true"><IntegrationLogo provider={requirement.provider} size={18} /></span>
    <span className="workspace-app-group__text">
      <strong>{name}</strong>
      <small>{requirementHint(requirement)}</small>
    </span>
    {action}
    {Steps && expanded && !done && <div className="workspace-app-connections__steps"><Steps onReady={connected} /></div>}
  </li>;
}
