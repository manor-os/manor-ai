import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type IntegrationMCPServer, type WorkspaceConnectionRequirement } from "../../lib/api";
import { t } from "../../lib/i18n";
import { INTEGRATION_CATALOG_QUERY_KEY, integrationCatalogQueryOptions } from "../../lib/integrationCatalog";
import { INTEGRATION_CONNECTED_EVENT, openIntegrationConnect, openSetupInPopup } from "../../lib/integrationConnect";
import { useToastStore } from "../../stores/toast";
import IntegrationLogo from "../IntegrationLogo";
import { useNangoConnect } from "./NangoConnectButton";
import Button from "../ui/Button";
import "./IntegrationConnectRow.css";

/** What one connection row needs to know: what to connect and how it is set up. */
export type IntegrationConnectRequirement = Pick<WorkspaceConnectionRequirement, "kind" | "provider">
  & Partial<Pick<WorkspaceConnectionRequirement, "key" | "label" | "setup_kind">>;

type ChromeStepsComponent = (props: { onReady: () => void }) => JSX.Element | null;

function chromeSteps(): ChromeStepsComponent | null {
  return null;
}

function isChromeRequirement(requirement: IntegrationConnectRequirement): boolean {
  return requirement.kind === "integration"
    && (requirement.provider === "chrome" || requirement.setup_kind === "browser_extension");
}

function requirementHint(requirement: IntegrationConnectRequirement): string {
  if (isChromeRequirement(requirement)) return t("component.workspace_app_view.connections_hint_chrome");
  if (requirement.kind === "channel") return t("component.workspace_app_view.connections_hint_channel");
  if (requirement.kind === "browser_session") return t("component.workspace_app_view.connections_hint_session");
  return t("component.workspace_app_view.connections_hint_integration");
}

/**
 * The one way Manor asks for a connection, wherever it is asked (the App, a
 * Flow's start, chat, a stopped run, a Blueprint install): the integration's
 * logo, name and what it is for, and Connect, set up without leaving the page.
 * Chrome's steps open in place, Nango opens its popup, any other integration
 * opens its own setup over this page (IntegrationConnectHost), and channels or
 * sessions go to their setup page.
 */
export default function IntegrationConnectRow({ requirement, workspaceId, hint, onConnected, as: Tag = "li" }: {
  requirement: IntegrationConnectRequirement;
  /** The Workspace asking, when there is one: its channels and capabilities are set up there. */
  workspaceId?: string;
  /** What waits on it, in place of the general line for its kind. */
  hint?: string;
  /** Told once it is connected, so what waited on it (a Flow's start, a run) can go on. */
  onConnected?: () => void;
  /** An item of a list of connections (default), or a row standing alone. */
  as?: "li" | "div";
}) {
  const [expanded, setExpanded] = useState(false);
  const onToggle = () => setExpanded((value) => !value);
  const catalog = useQuery(integrationCatalogQueryOptions(requirement.kind === "integration"));
  const server: IntegrationMCPServer | undefined = catalog.data?.find((item) => item.server_key === requirement.provider);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const name = requirement.label || server?.name
    || `${requirement.provider.slice(0, 1).toUpperCase()}${requirement.provider.slice(1)}`;
  const [done, setDone] = useState(false);
  const connected = () => {
    setDone(true);
    toast.success(t("component.workspace_app_view.connections_connected", { name }));
    void queryClient.invalidateQueries({ queryKey: ["workspace-connection-status"] });
    void queryClient.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
    if (workspaceId) void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
    onConnected?.();
  };
  const nangoKey = requirement.kind === "integration" ? server?.nango_provider_config_key || undefined : undefined;
  const nango = useNangoConnect({ providerConfigKeys: nangoKey ? [nangoKey] : undefined, onConnected: connected });
  useEffect(() => {
    // Connected from anywhere on this page (a chat link, a card, this row).
    const onEvent = (event: Event) => {
      if ((event as CustomEvent<{ provider?: string }>).detail?.provider !== requirement.provider) return;
      setDone(true);
      onConnected?.();
    };
    window.addEventListener(INTEGRATION_CONNECTED_EVENT, onEvent);
    return () => window.removeEventListener(INTEGRATION_CONNECTED_EVENT, onEvent);
  }, [requirement.provider, onConnected]);
  const Steps = isChromeRequirement(requirement) ? chromeSteps() : null;
  const label = t("component.workspace_app_view.connections_connect");
  // Setup that lives on a Manor page opens in a popup over this one, like OAuth; closing it looks again.
  const recheck = () => {
    for (const queryKey of [["workspace-connection-status"], ["workspace-setup-status"], INTEGRATION_CATALOG_QUERY_KEY]) {
      void queryClient.invalidateQueries({ queryKey: [...queryKey] });
    }
    if (workspaceId) void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
  };
  const go = (path: string) => <Button size="sm" variant="outline"
    onClick={() => { if (!openSetupInPopup(path, recheck)) navigate(path); }}>{label}</Button>;

  let action;
  if (done) {
    action = <span className="integration-connect-row__done">{t("status.connected")}</span>;
  } else if (Steps) {
    action = <Button size="sm" variant={expanded ? "ghost" : "outline"} onClick={onToggle} aria-expanded={expanded}>
      {expanded ? t("action.cancel") : label}
    </Button>;
  } else if (requirement.setup_kind === "mcp_binding" && workspaceId) {
    action = go(`/workspaces/${workspaceId}?tab=capabilities`);
  } else if (requirement.kind === "channel" && requirement.setup_kind !== "platform_access" && workspaceId) {
    action = go(`/workspaces/${workspaceId}?tab=channels`);
  } else if (requirement.kind === "browser_session") {
    action = go("/browser/sessions");
  } else if (nangoKey) {
    action = <Button size="sm" variant="outline" onClick={nango.begin} loading={nango.isWorking}>{label}</Button>;
  } else if (requirement.kind === "integration") {
    // OAuth, credential forms and the rest open over this page and return here.
    action = <Button size="sm" variant="outline" onClick={() => openIntegrationConnect(requirement.provider)}>{label}</Button>;
  } else {
    // A platform account a channel needs is an integration too: its setup opens over this page.
    action = <Button size="sm" variant="outline" onClick={() => openIntegrationConnect(requirement.provider)}>{label}</Button>;
  }

  return <Tag className="integration-connect-row">
    <span className="integration-connect-row__icon" aria-hidden="true"><IntegrationLogo provider={requirement.provider} size={18} /></span>
    <span className="integration-connect-row__text">
      <strong>{name}</strong>
      <small>{hint || requirementHint(requirement)}</small>
    </span>
    {action}
    {Steps && expanded && !done && <div className="integration-connect-row__steps"><Steps onReady={connected} /></div>}
  </Tag>;
}
