/** Real WorkspaceChat; API responses and chat generation are test data only. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import WorkspaceChat from "../../src/components/WorkspaceChat";
import ToastContainer from "../../src/components/ToastContainer";
import { useAuthStore } from "../../src/stores/auth";
import { useChatStreamStore } from "../../src/stores/chatStream";
import type { User, Workspace } from "../../src/lib/types";
import "../../src/index.css";

const workspaceId = "proposal-approval-fixture";
const streamKey = `workspace-chat:${workspaceId}`;
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});
useAuthStore.setState({
  token: null,
  user: { id: "fixture-user", entity_id: "fixture-entity", display_name: "Reviewer", role: "owner" } as User,
});

function Fixture() {
  const [paused, setPaused] = useState(false);
  const streaming = useChatStreamStore((s) => Boolean(s.sessions[streamKey]?.streaming));
  const workspace = {
    id: workspaceId, name: "Proposal approval fixture", entity_id: "fixture-entity",
    status: paused ? "paused" : "active", heartbeat_enabled: !paused, settings: {},
  } as Workspace;
  return <main style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
    <header style={{ padding: 12, display: "flex", flexWrap: "wrap", gap: 8 }}>
      <button className="btn-manor-outline" onClick={() => useChatStreamStore.setState((s) => ({
        sessions: { ...s.sessions, [streamKey]: { key: streamKey, streaming: !streaming, messages: [] } },
      }))}>{streaming ? "Finish simulated reply" : "Start simulated reply"}</button>
      <button className="btn-manor-outline" onClick={() => setPaused(!paused)}>
        {paused ? "Resume fixture Workspace" : "Pause fixture Workspace"}
      </button>
      <span role="status" aria-label="Fixture reply">{streaming ? "Reply streaming" : "Reply idle"}</span>
    </header>
    <div style={{ flex: 1, minHeight: 0 }}>
      <WorkspaceChat workspaceId={workspaceId} workspace={workspace} workspaceName={workspace.name} />
    </div>
    <ToastContainer />
  </main>;
}

createRoot(document.getElementById("root")!).render(
  <MemoryRouter><QueryClientProvider client={queryClient}><Fixture /></QueryClientProvider></MemoryRouter>,
);
