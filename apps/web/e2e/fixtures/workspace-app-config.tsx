import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import WorkspaceAppViewSettings from "../../src/components/workspaces/WorkspaceAppViewSettings";
import ToastContainer from "../../src/components/ToastContainer";
import { useAuthStore } from "../../src/stores/auth";
import "../../src/index.css";

useAuthStore.setState({ token: "synthetic-app-test-token", isLoading: false });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById("root")!).render(
  <MemoryRouter><QueryClientProvider client={queries}>
    <main style={{ padding: 16 }}><WorkspaceAppViewSettings workspaceId="app-fixture" canManage /></main>
    <ToastContainer />
  </QueryClientProvider></MemoryRouter>,
);
