/** Real Integrations page with isolated OAuth and legacy Lark responses. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import Integrations from "../../src/pages/Integrations";
import DetailDrawer from "../../src/components/ui/DetailDrawer";
import ToastContainer from "../../src/components/ToastContainer";
import { useAuthStore } from "../../src/stores/auth";
import { useConfigStore } from "../../src/stores/config";
import { api, type IntegrationMCPServer } from "../../src/lib/api";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

await setLocale("en");
useAuthStore.setState({ token: "fixture-only", isLoading: false, user: { id: "fixture-owner", role: "owner" } as any });
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const parameters = new URLSearchParams(window.location.search);
useConfigStore.setState({ loaded: true, deployment_mode: parameters.has("cloud") ? "cloud" : "oss" });
let oauthConfigured = !parameters.has("unconfigured");
let lastOAuthConfig: unknown = null;
let saved: any = parameters.has("existing") ? {
  provider: "lark", credentials: { app_id: "cli_fixture", app_secret: "fixture-app-secret", platform: "feishu" }, config: {},
} : null;
let lastUpdate: any = null;
const subscribers = new Set<() => void>();
const notify = () => subscribers.forEach(fn => fn());
api.features.catalog = async () => [];
api.features.list = async () => ({ features: {} }) as any;
api.skills.list = async () => [];
api.integrations.channelBindings = async () => [];
api.chrome.connections = async () => ({ connections: [] });
api.workers.list = async () => [];
api.integrations.mcpServers = async () => [{
  server_key: "lark", server_kind: "managed", name: "Lark", category: "Messaging",
  description: "Read chats and messages, send messages, create and read documents, and manage Base records.", tagline: "Team collaboration",
  auth_type: "oauth2", supports_multi_account: true, oauth_configured: oauthConfigured,
  connections: [], entity_accounts: saved ? [{
    id: "fixture-lark", kind: "integration", name: "Team app", status: "active",
    owner_user_id: "fixture-owner", owned_by_current_user: true, is_default: true,
    can_manage: true, can_use: true, can_share: true, runtime_callable: true, availability: "callable",
  }] : [],
  entity_connected: !!saved, user_connected: false, user_has_required_permission: true,
  agent_can_use: !!saved, tools: [], coming_soon: false,
}] as unknown as IntegrationMCPServer[];
api.integrations.create = async () => { throw new Error("New Lark connections must use OAuth"); };
api.integrations.setOAuthConfig = async (key, data) => {
  if (key !== "lark") throw new Error("Wrong OAuth client");
  lastOAuthConfig = data;
  oauthConfigured = true;
  notify();
  return {} as any;
};
api.integrations.oauthStart = async (key, options) => ({
  authorize_url: `${window.location.origin}/test-lark-oauth/authorize?${new URLSearchParams({ provider: key, connection: options?.connectionId || "" })}`,
  state: "fixture-state", server_key: key, source: "db",
});
api.integrations.list = async () => saved ? [{
  id: "fixture-lark", provider: "lark", config: saved.config,
  credential_preview: parameters.has("missing-preview") ? {} : {
    app_id: saved.credentials.app_id, app_secret: "__unchanged__", platform: saved.credentials.platform,
  },
}] as any : [];
api.integrations.update = async (id, data) => {
  if (id !== "fixture-lark") throw new Error("Wrong account");
  lastUpdate = data;
  const replacements = Object.fromEntries(Object.entries(data.credentials || {}).filter(([, value]) => value !== "__unchanged__"));
  saved = { ...saved, config: data.config, credentials: { ...saved.credentials, ...replacements } };
  notify();
  return { id } as any;
};

function Fixture() {
  const [, render] = useState(0);
  React.useEffect(() => {
    const update = () => render(n => n + 1);
    subscribers.add(update);
    return () => { subscribers.delete(update); };
  }, []);
  return <main style={{ maxWidth: 1040, padding: 20, margin: "24px auto", height: "90vh" }}>
    <output aria-label="Saved connection">{saved ? JSON.stringify(saved) : "none"}</output>
    <output aria-label="Updated connection">{lastUpdate ? JSON.stringify(lastUpdate) : "none"}</output>
    <output aria-label="Saved OAuth client">{lastOAuthConfig ? JSON.stringify(lastOAuthConfig) : "none"}</output>
    <Integrations />
    <DetailDrawer /><ToastContainer />
  </main>;
}
createRoot(document.getElementById("root")!).render(<QueryClientProvider client={queryClient}>
  <MemoryRouter initialEntries={["/integrations?provider=lark"]}><Fixture /></MemoryRouter>
</QueryClientProvider>);
