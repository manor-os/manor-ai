/** Real upgrade dialog; only the backend responses are fixture data. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import BlueprintUpgradeDialog from "../../src/components/blueprints/BlueprintUpgradeDialog";
import "../../src/index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

function Fixture() {
  const [open, setOpen] = useState(true);
  return <main className="p-6">
    <h1 className="text-lg font-semibold">Faceless Stickman Video Studio</h1>
    <button className="btn-manor-outline mt-4" onClick={() => setOpen(true)}>Check Blueprint updates</button>
    <BlueprintUpgradeDialog open={open} onClose={() => setOpen(false)}
      workspaceId="blueprint-upgrade-fixture" workspaceName="Faceless Stickman Video Studio" />
  </main>;
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}><Fixture /></QueryClientProvider>,
);
