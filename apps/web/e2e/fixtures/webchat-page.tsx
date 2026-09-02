import { useState } from "react";
import { createRoot } from "react-dom/client";
import WebchatPageEditor from "../../src/components/webchat/WebchatPageEditor";
import { api } from "../../src/lib/api";
import { withWorkspaceResourcePreviews, type WebchatPage, type WebchatWorkspaceResources } from "../../src/lib/webchatPage";
import "../../src/index.css";

const resources: WebchatWorkspaceResources = {
  brand: { name: "Acme Workspace", logo_url: "https://example.com/logo.png", website: "" },
  profile: { id: "workspace", name: "Acme Workspace", body: "A customer-focused workspace.", image_url: "", items: ["Property services", "San Francisco"] },
  documents: [{ id: "guide", name: "Visitor guide", body: "Public arrival and access information." }],
  documents_next_cursor: null,
  actions: [{ id: "tour-flow", name: "Request a tour", description: "Send a tour request to the leasing team." }],
};

function Fixture() {
  const params = new URLSearchParams(location.search);
  const [open, setOpen] = useState(true);
  const [page, setPage] = useState<WebchatPage>({ version: 1, modules: [
    { id: "brand", type: "brand", side: "left", name: "Acme", website: "https://example.com", logo_url: "" },
    { id: "intro", type: "text", side: "left", title: "Welcome", body: "Public information" },
    { id: "faq", type: "faq", side: "right", title: "Questions", items: [{ question: "Can I book a tour?", answer: "Tell us your preferred time." }] },
    ...(params.has("stale") ? [{ id: "stale", type: "workspace_action" as const, side: "right" as const, title: "Unavailable action", description: "This action is no longer active.", binding_id: "missing-flow", fields: [], submit_label: "Run" }] : []),
  ] });
  return <div style={{ padding: 24 }}>
    <button type="button" className="btn-manor" onClick={() => setOpen(true)}>Open page editor</button>
    <output data-testid="saved-page" hidden>{JSON.stringify(page)}</output>
    {open && <WebchatPageEditor initialPage={params.has("invalid-initial") ? { version: 1, modules: [{ type: "html", html: "invalid" }] } : page} workspaceName="Acme Workspace" agentName="Leasing Consultant" resources={resources} canEdit={!params.has("readonly")} onClose={() => setOpen(false)} onReview={async draft => {
      if (params.has("review-fails")) throw new Error("Review unavailable");
      return withWorkspaceResourcePreviews({
        ...draft,
        modules: draft.modules.filter(module => module.type !== "workspace_action" || resources.actions.some(action => action.id === module.binding_id)),
      }, resources);
    }} onSave={async next => {
      if (params.has("api")) await api.workspaces.updateChannel("fixture", "binding", { config: { public_page: next } });
      setPage(next);
    }} />}
  </div>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
