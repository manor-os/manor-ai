/** Real Integrations UI; disposable OAuth responses test presentation, not a live grant. */
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import Integrations from "../../src/pages/Integrations";
import DetailDrawer from "../../src/components/ui/DetailDrawer";
import ToastContainer from "../../src/components/ToastContainer";
import { api, type IntegrationMCPServer } from "../../src/lib/api";
import { useAuthStore } from "../../src/stores/auth";
import { useConfigStore } from "../../src/stores/config";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

await setLocale("en");
useAuthStore.setState({ token: "fixture-only", isLoading: false, user: { id: "fixture-owner", role: "owner" } as any });
useConfigStore.setState({ loaded: true, deployment_mode: "oss" });
const query = new URLSearchParams(window.location.search);
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
let disconnected = false;
let locallyDisabled = false;
let disconnectAttempts = 0;
api.skills.list = async () => [];
api.integrations.channelBindings = async () => [];
api.integrations.mcpServers = async () => [
  ["manor_pms", "Manor PMS"], ["manor_mcp_minutes", "Manor Minutes"],
].map(([key, name]) => {
  const connected = query.has("connected") && key === "manor_pms" && !disconnected;
  const callable = connected && !locallyDisabled && !query.has("expired");
  const legacy = query.has("legacy") && key === "manor_pms";
  return {
    server_key: key, server_kind: "managed", name, category: "Productivity",
    description: "Your Manor application account", tagline: "Your Manor application",
    app_url: query.has("no-app-url") ? null : `https://${key === "manor_pms" ? "portfolio.manorai.xyz/management" : "minutes.manorai.xyz/"}`,
    auth_type: "oauth2", supports_multi_account: true, oauth_configured: !query.has("unconfigured"),
    entity_accounts: legacy ? [{
      id: "fixture-legacy-pms", name: "Legacy PMS account", display_name: "Legacy PMS account",
      is_default: true, kind: "integration", ownership: "mine", owner_user_id: "fixture-owner",
      can_manage: true, can_share: false, runtime_callable: true, availability: "callable",
    }] : [], connections: connected ? [{
      id: "fixture-oauth-pms", kind: "oauth_account", display_name: "Test PMS account",
      provider_user_id: "pms:test-org:test-owner", is_default: true,
      ownership: "mine", owner_user_id: "fixture-owner", can_manage: true, can_share: false,
      runtime_callable: callable, availability: callable ? "callable" : "reconnect_required",
      expires_at: query.has("expired") ? "2020-01-01T00:00:00Z" : null,
      health: locallyDisabled ? { ok: false, detail: "Disconnected locally; remote revocation is pending." } : null,
    }] : [],
    entity_connected: legacy, user_connected: connected, agent_can_use: callable || legacy,
    user_has_required_permission: true, tools: [], coming_soon: false,
  };
}) as unknown as IntegrationMCPServer[];
api.integrations.oauthStart = async (key, options) => {
  if (query.has("start-error")) throw Object.assign(new Error("OAuth client is not configured for this deployment."), { status: 501 });
  const args = new URLSearchParams({ provider: key, connection: options?.connectionId || "" });
  return { authorize_url: `${window.location.origin}/test-oauth/authorize?${args}`, state: "fixture-state", server_key: key, source: "db" };
};
api.integrations.disconnectAccount = async () => {
  disconnectAttempts += 1;
  if (query.has("revoke-error") && disconnectAttempts === 1) {
    locallyDisabled = true;
    throw new Error("Disconnected locally; remote revocation is pending. Retry disconnecting.");
  }
  disconnected = true;
};

createRoot(document.getElementById("root")!).render(<QueryClientProvider client={queryClient}>
  <MemoryRouter initialEntries={[query.has("cancelled") ? "/integrations?oauth_cancelled=manor_pms" : "/integrations"]}>
    <main style={{ maxWidth: 1040, padding: 20, margin: "24px auto", height: "90vh" }}>
      <Integrations /><DetailDrawer /><ToastContainer />
    </main>
  </MemoryRouter>
</QueryClientProvider>);
