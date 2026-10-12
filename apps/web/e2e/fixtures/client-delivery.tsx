import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api } from "../../src/lib/api";
import { setLocale } from "../../src/lib/i18n";
import { useAuthStore } from "../../src/stores/auth";
import Clients from "../../src/pages/Clients";
import ClientDelivery from "../../src/pages/ClientDelivery";
import "../../src/index.css";

// Isolated UI fixture. No request, invitation or install reaches a real tenant.
const clientRows = [{ id: "customer", name: "Example Café", email: "marketing@example.test", status: "active" }];
const contactRows = [{ id: "contact", name: "Marketing owner", email: "marketing@example.test" }];
let deliveryRows: any[] = [];
let accepted: any;
const receipt = () => ({ id: "delivery", client_id: "customer", contact_id: "contact", source_workspace_id: "source", title: "Marketing Workspace", recipient_email: "marketing@example.test", status: "pending", expires_at: "2026-10-08T12:00:00Z", provider_access: "none" as const, can_manage: true });
useAuthStore.setState({ user: { id: "fixture-user", entity_id: "customer-company", email: "marketing@example.test", role: "owner" } as any });
api.clients.list = async () => ({ items: clientRows, total: clientRows.length });
api.clients.create = async (data) => { const row = { id: "new-customer", ...data, status: "active" }; clientRows.push(row as any); return row; };
api.clients.contacts = async () => contactRows;
api.clients.addContact = async (_id, data) => { const row = { id: "new-contact", ...data }; contactRows.push(row); return row; };
api.workspaces.list = async () => [{ id: "source", name: "Marketing Workspace", can_manage: true }] as any;
api.clients.deliveries = async () => [...deliveryRows];
api.workspaces.blueprintExportKnowledgeDocuments = async () => [];
api.clients.prepareDelivery = async () => { const row = receipt(); deliveryRows.push(row); return { ...row, invite_path: "/client-delivery/private-test-token" }; };
api.clients.changeInvite = async (_client, _delivery, action) => { const row = deliveryRows[0]; if (action === "revoke") row.status = "revoked"; return { ...row, ...(action === "rotate" ? { invite_path: "/client-delivery/rotated-test-token" } : {}) }; };
api.clients.invitation = async () => accepted || receipt();
api.clients.acceptDelivery = async () => { accepted = { ...receipt(), status: "delivered", target_workspace_id: "customer-workspace", target_entity_id: "customer-company" }; return accepted; };
api.people.me = async () => ({ active_entity: { id: "customer-company", name: "Customer company" } }) as any;
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  if (String(input).includes("/api/v1/auth/me/email-verification")) return new Response(JSON.stringify({ email: "marketing@example.test", email_verified: true }), { headers: { "Content-Type": "application/json" } });
  return originalFetch(input, init);
};
void setLocale(new URLSearchParams(location.search).get("locale") === "en" ? "en" : "zh").then(() => {
  const receiving = new URLSearchParams(location.search).get("receive") === "1";
  createRoot(document.getElementById("root")!).render(<React.StrictMode><MemoryRouter initialEntries={[receiving ? "/client-delivery/private-test-token" : "/team/clients"]}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><main style={{ maxWidth: 960, margin: "0 auto", padding: 16 }}><p>Isolated client delivery test</p><Routes><Route path="/team/clients" element={<Clients />} /><Route path="/client-delivery/:token" element={<ClientDelivery />} /><Route path="*" element={<p>Workspace opened</p>} /></Routes></main></QueryClientProvider></MemoryRouter></React.StrictMode>);
});
