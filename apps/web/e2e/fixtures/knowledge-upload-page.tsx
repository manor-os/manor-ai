/** Real Knowledge page mounted with isolated auth/query/router providers. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import Knowledge from "../../src/pages/Knowledge";
import ToastContainer from "../../src/components/ToastContainer";
import { useAuthStore } from "../../src/stores/auth";
import "../../src/index.css";

const fixtureUserId = "01FIXTUREKNOWLEDGEUSER001";
useAuthStore.setState({
  token: "fixture-token",
  isLoading: false,
  user: {
    id: fixtureUserId,
    entity_id: "01FIXTUREKNOWLEDGEENTITY1",
    username: "fixture",
    email: "fixture@example.test",
    display_name: "Fixture User",
    role: "owner",
    permissions: ["docs.upload"],
  } as any,
});

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, staleTime: Number.POSITIVE_INFINITY },
    mutations: { retry: false },
  },
});

function FixtureHarness() {
  const [showKnowledge, setShowKnowledge] = useState(true);
  return (
    <>
      <div className="fixed right-2 top-2 z-[100] flex gap-2">
        <button type="button" onClick={() => setShowKnowledge(false)}>Hide Knowledge</button>
        <button type="button" onClick={() => setShowKnowledge(true)}>Show Knowledge</button>
      </div>
      {showKnowledge ? <Knowledge /> : <div>Knowledge route unmounted</div>}
      <ToastContainer />
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter initialEntries={["/knowledge?view=grid"]}>
      <FixtureHarness />
    </MemoryRouter>
  </QueryClientProvider>,
);
