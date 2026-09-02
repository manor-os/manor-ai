/** Real UI components, isolated catalog data. No real account authorization. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import ChatInputFooter, { type AttachedItem } from "../../src/components/ChatInputFooter";
import ChatMarkdown from "../../src/components/ChatMarkdown";
import Integrations from "../../src/pages/Integrations";
import DetailDrawer from "../../src/components/ui/DetailDrawer";
import ToastContainer from "../../src/components/ToastContainer";
import { closeDetail } from "../../src/stores/detail";
import { useAuthStore } from "../../src/stores/auth";
import { useConfigStore } from "../../src/stores/config";
import { api, type IntegrationMCPServer } from "../../src/lib/api";
import { INTEGRATION_CATALOG_QUERY_KEY } from "../../src/lib/integrationCatalog";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

setLocale("en");
useAuthStore.setState({ token: "fixture-only", isLoading: false, user: null });
useConfigStore.setState({ loaded: true, deployment_mode: "oss", flows_available: false, flows_released: false });
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
let catalogRequests = 0;
let authorizationRequests = 0;
let syncRequests = 0;
let authorizationResponses = 0;
let syncResponses = 0;
const delayedPhase = new URLSearchParams(window.location.search).get("delay");
const startedConnections = new Set<string>();
let authorizationOutcome = "success";
let failCatalog = false;
let ready = false;
let hasConnection = false;
const subscribers = new Set<() => void>();
const notify = () => subscribers.forEach(callback => callback());
api.integrations.mcpServers = async () => {
  catalogRequests += 1;
  notify();
  if (failCatalog) throw new Error("Fixture catalog unavailable");
  return [{
    server_key: "gmail", name: "Gmail", tagline: "Read and send email", category: "productivity",
    description: "Connect Gmail to read and send email.", auth_type: "oauth2", oauth_configured: true,
    nango_provider_config_key: "google-mail", agent_can_use: ready,
    user_connected: hasConnection, entity_connected: false, connections: [], entity_accounts: [],
    oauth_scopes: [], tools: [], coming_soon: false,
  }] as unknown as IntegrationMCPServer[];
};
api.integrations.channelBindings = async () => [];
api.skills.list = async () => [];
api.integrations.oauthStart = async () => {
  authorizationRequests += 1; notify(); throw new Error("Real OAuth disabled in fixture");
};
api.integrations.nango.startConnect = async () => {
  authorizationRequests += 1;
  const connectionId = `fixture-connection-${authorizationRequests}`;
  const outcome = authorizationOutcome;
  startedConnections.add(connectionId);
  notify();
  // E2E controls only this local response; no real authorization is performed.
  if (delayedPhase === "start") await fetch("/e2e/fixtures/integration-start");
  authorizationResponses += 1;
  notify();
  if (outcome === "start-error") throw new Error("Fixture authorization failed");
  return {
    session_token: "fixture-only",
    nango_connect_url: "/e2e/fixtures/integration-oauth.html",
    connection_id: connectionId, provider_config_key: "google-mail",
  };
};
api.integrations.nango.sync = async (data) => {
  syncRequests += 1;
  const outcome = authorizationOutcome;
  if (!startedConnections.has(data.expected_connection_id || "") || data.expected_provider_config_key !== "google-mail") {
    throw new Error("Wrong fixture connection identity");
  }
  notify();
  if (delayedPhase === "sync") await fetch("/e2e/fixtures/integration-sync");
  syncResponses += 1;
  notify();
  if (outcome === "sync-error") throw new Error("Fixture sync failed");
  ready = outcome === "success";
  hasConnection = ready;
  return {
    upserted: ready ? 1 : 0, providers: ready ? ["gmail"] : [],
    setup_required: false, whatsapp_resources: [], pending_connection_id: null,
  };
};

// Match production ownership: leaving /chat destroys both text and attachments.
function ChatPage() {
  const [draft, setDraft] = useState("");
  const [sent, setSent] = useState<{ text: string; attachments: AttachedItem[] } | null>(null);
  return <section style={{ paddingTop: 120 }}>
    <ChatMarkdown content="Configure [Gmail mailbox](/integrations?provider=gmail) to send email." />
    <ChatInputFooter value={draft} onChange={setDraft} streaming={false}
      onSend={(text, attachments) => setSent({ text, attachments })} onStop={() => {}} />
    <output aria-label="Chat draft">{draft}</output>
    <output aria-label="Sent draft">{sent && JSON.stringify({ text: sent.text, files: sent.attachments.map(item => item.file?.name || item.name) })}</output>
  </section>;
}

function Fixture() {
  const [, render] = useState(0);
  const navigate = useNavigate();
  const location = useLocation();
  React.useEffect(() => {
    const update = () => render(value => value + 1);
    subscribers.add(update);
    return () => { subscribers.delete(update); };
  }, []);
  return <main style={{ maxWidth: 960, padding: 16, margin: "24px auto" }}>
    <p>Real components · isolated test data · no real account authorization</p>
    <nav style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 16 }}>
      <button onClick={() => { closeDetail(); navigate("/chat"); }}>Back to Chat</button>
      <button onClick={() => navigate("/integrations")}>Integrations</button>
      <button onClick={() => {
        ready = !ready;
        hasConnection = true;
        void queryClient.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
        notify();
      }}>Simulate connection change</button>
      <button onClick={() => {
        failCatalog = !failCatalog;
        void queryClient.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
        notify();
      }}>Toggle catalog error</button>
    </nav>
    <label>Authorization outcome <select aria-label="Authorization outcome" value={authorizationOutcome}
      onChange={event => { authorizationOutcome = event.target.value; render(value => value + 1); }}>
      <option value="success">Success</option><option value="cancel">Cancel</option>
      <option value="start-error">Start error</option><option value="sync-error">Sync error</option>
    </select></label>
    <p aria-label="Request counts">Catalog requests: {catalogRequests} · Authorization requests: {authorizationRequests} · Sync requests: {syncRequests}</p>
    <p aria-label="Response counts">Authorization responses: {authorizationResponses} · Sync responses: {syncResponses}</p>
    <output aria-label="Current route">{location.pathname}{location.search}</output>
    <Routes>
      <Route path="/integrations" element={<div style={{ height: "75vh" }}><Integrations /></div>} />
      <Route path="/chat" element={<ChatPage />} />
    </Routes>
    <DetailDrawer />
    <ToastContainer />
  </main>;
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}><MemoryRouter initialEntries={["/chat"]}><Fixture /></MemoryRouter></QueryClientProvider>,
);
