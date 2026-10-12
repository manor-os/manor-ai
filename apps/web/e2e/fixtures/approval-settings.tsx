import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ApprovalSettingsPanel, { openApprovalSettings } from "../../src/components/approvals/ApprovalSettingsPanel";
import ApprovalModeTrigger from "../../src/components/approvals/ApprovalModeTrigger";
import ApprovalSaveRuleAction from "../../src/components/approvals/ApprovalSaveRuleAction";
import DetailDrawer from "../../src/components/ui/DetailDrawer";
import { useAuthStore } from "../../src/stores/auth";
import type { User } from "../../src/lib/types";
import "../../src/index.css";
useAuthStore.setState({ token: "synthetic-approval-token", isLoading: false, user: { id: "approval-user", entity_id: "approval-account", role: "owner" } as User });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
function CommandFixture() {
  const [done, setDone] = useState(false);
  return done ? <p role="status">Approval and rule saved</p> : <ApprovalSaveRuleAction requestId="approval-request" scopeLabel="Publishing Studio" onCompleted={() => setDone(true)} />;
}
createRoot(document.getElementById("root")!).render(<MemoryRouter><QueryClientProvider client={queries}>
  <main style={{ maxWidth: 1024, margin: "24px auto", padding: 20 }}>
    <h1>Execution & approvals</h1>
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 24 }}>
      <button onClick={() => openApprovalSettings({ kind: "personal" })}>Personal chat settings</button>
      <button onClick={() => openApprovalSettings({ kind: "workspace", id: "approval-workspace" }, "Publishing Studio")}>Workspace settings</button>
      <button onClick={() => queries.invalidateQueries({ queryKey: ["approvals"] })}>Refresh settings</button>
      <ApprovalModeTrigger scope={{ kind: "workspace", id: "approval-workspace" }} />
      <button onClick={() => useAuthStore.setState({ user: { id: "other-user", entity_id: "other-account", role: "member" } as User })}>Switch account</button>
    </div>
    <ApprovalSettingsPanel scope={{ kind: "account", id: "approval-account" }} showOverrides />
    <CommandFixture />
  </main><DetailDrawer />
</QueryClientProvider></MemoryRouter>);
