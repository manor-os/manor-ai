import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import WorkspaceChat from "../../src/components/WorkspaceChat";
import EmbeddedChat from "../../src/components/EmbeddedChat";
import FloatingChat from "../../src/components/FloatingChat";
import DetailDrawer from "../../src/components/ui/DetailDrawer";
import { useAuthStore } from "../../src/stores/auth";
import { useDisplayPreferences } from "../../src/stores/displayPreferences";
import "../../src/index.css";

useAuthStore.setState({ token: "synthetic-app-test-token", isLoading: false });
useDisplayPreferences.setState({ interfaceMode: "professional", hasSelectedInterfaceMode: true });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function Fixture() {
  const [workspaceId, setWorkspaceId] = useState("app-fixture");
  const surface = new URLSearchParams(location.search).get("surface");
  if (surface === "floating") return <FloatingChat />;
  if (surface === "embedded") return <main style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
    <button onClick={() => setWorkspaceId("second-fixture")}>Switch conversation</button>
    <EmbeddedChat conversationId={workspaceId === "app-fixture" ? "history-conversation" : "other-conversation"} title="Manor AI" />
  </main>;
  return <main style={{ padding: 16, height: "100vh", display: "flex", flexDirection: "column" }}>
    <button onClick={() => setWorkspaceId((id) => id === "app-fixture" ? "second-fixture" : "app-fixture")}>Switch Workspace</button>
    <button onClick={() => void queries.invalidateQueries({ queryKey: ["workspace-app-view"] })}>Refresh App</button>
    <WorkspaceChat workspaceId={workspaceId} workspaceName="Studio" />
  </main>;
}

createRoot(document.getElementById("root")!).render(
  <MemoryRouter><QueryClientProvider client={queries}>
    <Fixture />
    <DetailDrawer />
  </QueryClientProvider></MemoryRouter>,
);
