/** Real configuration components with synthetic API responses in the test. */
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import WorkspaceDetail from "../../src/pages/WorkspaceDetail";
import AgentEditModal from "../../src/components/AgentEditModal";
import ToastContainer from "../../src/components/ToastContainer";
import { useAuthStore } from "../../src/stores/auth";
import type { User } from "../../src/lib/types";
import "../../src/index.css";

// The catalog reads its scope from the token's claims, so the synthetic token carries them.
const fixtureToken = `e30.${btoa(JSON.stringify({ sub: "fixture-user", entity_id: "fixture-entity", role: "owner", exp: 4102444800 }))}.sig`;
localStorage.setItem("manor_token", fixtureToken);
useAuthStore.setState({
  token: fixtureToken, isLoading: false,
  user: { id: "fixture-user", entity_id: "fixture-entity", display_name: "Operator", role: "owner" } as User,
});
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });

createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={["/workspaces/agent-fixture?tab=staff"]}>
    <QueryClientProvider client={queryClient}>
      <main style={{ padding: 16 }}><Routes><Route path="/workspaces/:workspaceId" element={<WorkspaceDetail />} /></Routes></main>
      <AgentEditModal />
      <ToastContainer />
    </QueryClientProvider>
  </MemoryRouter>,
);
