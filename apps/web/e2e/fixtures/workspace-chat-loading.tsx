import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import AppLayout from "../../src/layouts/AppLayout";
import { useAuthStore } from "../../src/stores/auth";
import { useConfigStore } from "../../src/stores/config";
import { useDisplayPreferences } from "../../src/stores/displayPreferences";
import type { User } from "../../src/lib/types";
import "../../src/index.css";

useAuthStore.setState({ token: "synthetic-loading-test-token", isLoading: false,
  user: { id: "loading-user", entity_id: "loading-entity", name: "Loading Test", role: "owner" } as User });
useConfigStore.setState({ loaded: true });
useDisplayPreferences.setState({ interfaceMode: "professional", hasSelectedInterfaceMode: true });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } });
createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={["/chat?workspace=app-fixture"]}>
    <QueryClientProvider client={queries}><AppLayout /></QueryClientProvider>
  </MemoryRouter>,
);
