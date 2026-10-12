import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import BlueprintDetail from "../../src/pages/BlueprintDetail";
import { PageHeaderBoundary } from "../../src/components/ui/PageHeader";
import "../../src/index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter initialEntries={["/blueprints/builtin:video-studio"]}>
      <main data-testid="content-shell" style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
        <PageHeaderBoundary>
        <div className="app-route-content px-3 pb-6 pt-2 sm:px-4 md:px-6" style={{ minHeight: 0, flex: 1 }}>
        <Routes>
          <Route path="/blueprints/:blueprintId" element={<BlueprintDetail />} />
          <Route path="*" element={<p>Workspace installed</p>} />
        </Routes>
        </div>
        </PageHeaderBoundary>
      </main>
    </MemoryRouter>
  </QueryClientProvider>,
);
