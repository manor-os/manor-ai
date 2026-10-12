/** Isolated visual fixture: never contacts a provider or modifies an account. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api, type WorkspaceConnectionStatus } from "../../src/lib/api";
import type { User } from "../../src/lib/types";
import { useAuthStore } from "../../src/stores/auth";
import WorkspaceConnectionNotice from "../../src/components/workspaces/WorkspaceConnectionNotice";
import "../../src/index.css";

const queryClient = new QueryClient();
useAuthStore.setState({ user: { id: "fixture-user", entity_id: "fixture-entity" } as User });
let mode = "missing";
api.workspaces.connectionStatus = async () => {
  if (mode === "error") throw new Error("Fixture network failure");
  const providers = mode === "many" ? Array.from({ length: 200 }, (_, i) => `Provider ${i + 1}`) : ["Gmail", "Chrome"];
  const requirements: WorkspaceConnectionStatus["requirements"] = providers.map((provider) => ({
    key: provider, provider: provider.toLowerCase(), label: provider,
    kind: "integration", ready: mode === "ready", required: mode !== "optional",
    reason: provider === "Chrome" ? "Connect the Manor Chrome extension on your paired worker." : "Reconnect your account to restore this service.",
    service_keys: ["sales"],
  }));
  return { workspace_id: "fixture", requirements, required_issue_count: requirements.filter((r) => r.required && !r.ready).length };
};

function Fixture() {
  const [narrow, setNarrow] = useState(false);
  const [, refresh] = useState(0);
  return <main style={{ padding: 16, fontFamily: "var(--font-sans, sans-serif)" }}>
    <h1>Connection notice test fixture</h1>
    <p>Test data only. No real accounts or external actions.</p>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, margin: "16px 0" }}>
      {["missing", "ready", "optional", "error", "many"].map((value) => <button className="btn-manor-outline" key={value} onClick={() => {
        mode = value;
        void queryClient.invalidateQueries({ queryKey: ["workspace-connection-status"] });
        refresh((n) => n + 1);
      }}>{value}</button>)}
      <button className="btn-manor-outline" onClick={() => setNarrow(!narrow)}>Toggle 390px</button>
    </div>
    <div style={{ width: narrow ? 358 : "100%", maxWidth: 1040 }}>
      <WorkspaceConnectionNotice workspaceId="fixture" />
      <p>Workspace content remains usable while connections need setup.</p>
    </div>
  </main>;
}

createRoot(document.getElementById("root")!).render(<QueryClientProvider client={queryClient}><MemoryRouter><Fixture /></MemoryRouter></QueryClientProvider>);
