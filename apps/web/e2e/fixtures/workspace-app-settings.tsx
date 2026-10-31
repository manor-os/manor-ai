import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import WorkspaceAppViewSettings from "../../src/components/workspaces/WorkspaceAppViewSettings";
import { useAuthStore } from "../../src/stores/auth";
import "../../src/index.css";

useAuthStore.setState({ token: "synthetic-app-test-token", isLoading: false });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queries}>
      <MemoryRouter>
        <WorkspaceAppViewSettings workspaceId="app-fixture" canManage />
      </MemoryRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
