import { lazy, Suspense, useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { INTEGRATION_CATALOG_QUERY_KEY, integrationCatalogQueryOptions, useIntegrationCatalog } from "../../lib/integrationCatalog";
import { INTEGRATION_CONNECTED_EVENT, useIntegrationConnectStore } from "../../lib/integrationConnect";
import { t } from "../../lib/i18n";
import { useAuthStore } from "../../stores/auth";
import { useToastStore } from "../../stores/toast";

// The Integrations page's own setup panel (drawer + credential forms), loaded on first use.
const IntegrationSetupPanel = lazy(() =>
  import("../../pages/Integrations").then((module) => ({ default: module.IntegrationSetupPanel })),
);

/** The OAuth return this page already reported (React may run an effect twice). */
let handledReturn = "";

/** Everything that shows whether a connection is there, refreshed together. */
function refreshConnectionViews(queryClient: ReturnType<typeof useQueryClient>) {
  for (const queryKey of [
    INTEGRATION_CATALOG_QUERY_KEY,
    ["workspace-connection-status"],
    ["workspace-app-section"],
    ["email-accounts"],
    ["channel-bindings"],
  ]) {
    void queryClient.invalidateQueries({ queryKey: [...queryKey] });
  }
}

/**
 * Connects an integration on the current page. Mounted once beside the global
 * DetailDrawer: a chat link, a connect card or the App's Connect calls
 * `openIntegrationConnect(provider)`, and the provider's setup opens here, over
 * the page the person is on. When the provider becomes usable, every view of
 * it is refreshed and `INTEGRATION_CONNECTED_EVENT` fires.
 */
export default function IntegrationConnectHost() {
  const { provider, session, panelOpen, popupPending, closePanel, end } = useIntegrationConnectStore();
  const queryClient = useQueryClient();
  const location = useLocation();
  const navigate = useNavigate();
  const signedIn = Boolean(useAuthStore((state) => state.token));
  const catalog = useIntegrationCatalog(signedIn && Boolean(provider));
  const row = provider ? catalog.data?.find((server: { server_key: string }) => server.server_key === provider) : undefined;
  const ready = Boolean((row as { agent_can_use?: boolean } | undefined)?.agent_can_use);
  // Readiness when this session first saw the row: only a change counts as connecting.
  const baseline = useRef<{ session: number; ready: boolean } | null>(null);

  const announce = (key: string, name: string) => {
    refreshConnectionViews(queryClient);
    window.dispatchEvent(new CustomEvent(INTEGRATION_CONNECTED_EVENT, { detail: { provider: key } }));
    useToastStore.getState().success(t("component.integration_connect.connected", { name }));
  };

  useEffect(() => {
    if (!provider || !row) return;
    if (baseline.current?.session !== session) {
      baseline.current = { session, ready };
      return;
    }
    if (ready && !baseline.current.ready) {
      baseline.current.ready = true;
      announce(provider, (row as { name?: string }).name || provider);
      end();
    }
  }, [provider, session, row, ready]); // eslint-disable-line react-hooks/exhaustive-deps

  // The panel closed and no popup is out: look once more, then end the session.
  useEffect(() => {
    if (!provider || panelOpen || popupPending) return;
    let cancelled = false;
    void queryClient.refetchQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY }).finally(() => {
      if (!cancelled) window.setTimeout(() => useIntegrationConnectStore.getState().session === session && end(), 0);
    });
    return () => { cancelled = true; };
  }, [provider, session, panelOpen, popupPending]); // eslint-disable-line react-hooks/exhaustive-deps

  // A blocked popup fell back to a full redirect that came back here.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const connected = params.get("connected");
    const cancelled = params.get("oauth_cancelled");
    if (!connected && !cancelled) return;
    if (location.pathname === "/integrations") return; // the Integrations page reads these itself
    const returned = `${location.pathname}?${location.search}`;
    if (handledReturn === returned) return; // one return, one message
    handledReturn = returned;
    params.delete("connected");
    params.delete("oauth_cancelled");
    const search = params.toString();
    navigate({ pathname: location.pathname, search: search ? `?${search}` : "", hash: location.hash }, { replace: true });
    const key = connected || cancelled || "";
    // The provider said yes; only a usable account counts as connected.
    void queryClient.fetchQuery({ ...integrationCatalogQueryOptions(true), staleTime: 0 }).catch(() => undefined).then((servers) => {
      const server = (servers as Array<{ server_key: string; name?: string; agent_can_use?: boolean }> | undefined)
        ?.find((item) => item.server_key === key);
      const name = server?.name || key;
      if (connected && server?.agent_can_use) announce(key, name);
      else if (connected) useToastStore.getState().error(t("component.integration_connect.failed", { name }));
      else useToastStore.getState().info(t("component.integration_connect.cancelled", { name }));
    });
  }, [location.search]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!provider || !panelOpen) return null;
  return (
    <Suspense fallback={null}>
      <IntegrationSetupPanel key={session} provider={provider} onClose={closePanel} />
    </Suspense>
  );
}
