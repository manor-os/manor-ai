import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api, type ClientRecord } from "../../src/lib/api";
import { setLocale, t } from "../../src/lib/i18n";
import { useAuthStore } from "../../src/stores/auth";
import ClientDeliveryDialog from "../../src/pages/clients/ClientDeliveryDialog";
import PageHeader from "../../src/components/ui/PageHeader";
import TabSwitcher from "../../src/components/ui/TabSwitcher";
import Button from "../../src/components/ui/Button";
import "../../src/index.css";

// Isolated interaction proof. All calls below stay in memory; no real invitation or delivery is created.
const params = new URLSearchParams(location.search);
const customers = [
  { id: "alpha", name: "Example Café", status: "active", email: "owner@example.test" },
  { id: "beta", name: "Example Bakery", status: "active", email: "buyer@example.test" },
] as ClientRecord[];
let calls = 0;
let created = 0;
let readFailed = false;
const trace: unknown[] = [];
useAuthStore.setState({ user: { id: "fixture-user", entity_id: "fixture-company", email: "owner@example.test", role: "owner" } as any });
api.crm.clients = async query => {
  if (params.get("fault") === "clients" && !readFailed) { readFailed = true; throw new Error("测试加载失败，可以重试。"); }
  const q = new URLSearchParams(query);
  const rows = params.has("empty") ? [] : customers.filter(row => row.name.toLowerCase().includes((q.get("search") || "").toLowerCase()));
  return { items: rows, total: rows.length };
};
api.clients.contacts = async clientId => params.has("no_contacts") ? [] : clientId === "alpha"
  ? [{ id: "alpha-owner", name: "Marketing owner", email: "owner@example.test", client_id: clientId, is_primary: true }, { id: "alpha-finance", name: "Finance contact", email: "finance@example.test", client_id: clientId, is_primary: false }]
  : [{ id: "beta-buyer", name: "Bakery buyer", email: "buyer@example.test", client_id: clientId, is_primary: true }];
api.workspaces.list = async () => [{ id: "source", name: "Marketing Workspace", can_manage: true }, { id: "second", name: "Second Workspace", can_manage: true }] as any;
let knowledgeFailed = false;
api.workspaces.blueprintExportKnowledgeDocuments = async (workspaceId, limit = 50, offset = 0) => {
  if (params.get("fault") === "knowledge" && !knowledgeFailed) { knowledgeFailed = true; throw new Error("Fixture: starter files unavailable"); }
  const count = params.has("no_starters") ? 0 : params.has("many_starters") ? 70 : 3;
  return Array.from({ length: count }, (_, index) => ({ id: `${workspaceId}-starter-${index + 1}`, name: `template-${index + 1}.csv`, path: `templates/template-${index + 1}.csv`, file_size: 20, groups: [] })).slice(offset, offset + limit);
};
api.clients.prepareDelivery = async (clientId, data) => {
  calls += 1;
  trace.push({ clientId, ...data });
  if (params.get("fault") === "prepare" && calls === 1) throw new Error("测试生成失败：保留选择后重试。");
  created += 1;
  const row = { id: "fixture-delivery", client_id: clientId, contact_id: data.contact_id, source_workspace_id: data.workspace_id, title: "Marketing Workspace", recipient_email: data.contact_id === "beta-buyer" ? "buyer@example.test" : data.contact_id === "alpha-finance" ? "finance@example.test" : "owner@example.test", phase: data.phase, status: "pending", expires_at: "2026-10-09T12:00:00Z", provider_access: "none", can_manage: true, email_delivery: { status: params.get("fault") === "email" ? "failed" : "sent" }, invite_path: "/client-delivery/isolated-fixture-token" };
  return row as any;
};
api.clients.changeInvite = async (clientId, deliveryId) => ({ id: deliveryId, client_id: clientId, title: "Marketing Workspace", recipient_email: "owner@example.test", phase: "trial", status: "pending", email_delivery: { status: "sent" }, invite_path: "/client-delivery/isolated-resent-token" } as any);
window.addEventListener("fixture-account-change", () => useAuthStore.setState({ user: { id: "other-user", entity_id: "other-company", email: "other@example.test", role: "owner" } as any }));
function Fixture() {
  const [open, setOpen] = useState(false);
  return <main className="max-w-4xl mx-auto p-4">
    <p className="text-xs text-stone-500 mb-3">测试演示 · 数据与邀请均为隔离样例</p>
    <PageHeader title="Marketing Workspace"><Button onClick={() => setOpen(true)}>{t("page.clients.deliver")}</Button></PageHeader>
    <TabSwitcher value="collaboration" onChange={() => {}} tabs={[{ key: "overview", label: "Overview" }, { key: "collaboration", label: t("page.collaboration.tab") }]} />
    <ClientDeliveryDialog open={open} onClose={() => setOpen(false)} {...(params.has("crm") ? { client: customers[0], initialWorkspace: "source" } : { workspace: { id: "source", name: "Marketing Workspace" } })} />
    {!open && <output data-testid="delivery-trace">{JSON.stringify({ calls, created, trace })}</output>}
  </main>;
}
void setLocale(params.get("locale") === "en" ? "en" : "zh").then(() => createRoot(document.getElementById("root")!).render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Routes><Route path="/" element={<Fixture />} /><Route path="*" element={<p>已打开客户记录（测试演示）</p>} /></Routes></QueryClientProvider></MemoryRouter>));
