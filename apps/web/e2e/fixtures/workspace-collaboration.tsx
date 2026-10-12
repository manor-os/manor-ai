import React from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type CollaborationGrant, type CollaborationManagement } from "../../src/lib/api";
import { setLocale } from "../../src/lib/i18n";
import { useAuthStore } from "../../src/stores/auth";
import WorkspaceCollaboration, { WorkspaceCollaborationManagement } from "../../src/pages/WorkspaceCollaboration";
import DetailDrawer from "../../src/components/ui/DetailDrawer";
import "../../src/index.css";

// Synthetic UI only. None of these requests, grants or tests reach a real company.
const params = new URLSearchParams(location.search);
const guest = params.get("view") === "guest";
const fault = params.get("fault");
const expires = "2026-10-16T12:00:00Z";
const grants: CollaborationGrant[] = [{
  id: "maintainer",
  workspace_id: "workspace",
  email: "maintainer@example.test",
  role: "maintainer",
  status: "active",
  expires_at: expires,
  share_configuration: true,
  path: "/workspace-collaboration/maintainer"
}, {
  id: "request",
  workspace_id: "workspace",
  email: "provider@example.test",
  role: "maintainer",
  status: "requested",
  expires_at: expires,
  share_configuration: false,
  path: "/workspace-collaboration/request"
}];
const groups = [{id: "marketing-group", title: "OLOSO · 产品营销协作", path: "/chat?room=marketing-group"}];
let communication = {linked: !params.has("unlinked"), group: params.has("unlinked") || params.has("nonmember") ? null : groups[0]};
let phase = "trial";
let acceptance: Record<string, unknown> = {};
let failedSave = false;
useAuthStore.setState({
  user: {
    id: guest ? "guest-user" : "owner-user",
    entity_id: guest ? "provider-company" : "customer-company",
    email: guest ? "maintainer@example.test" : "owner@example.test",
    role: "owner"
  } as any
});
api.collaboration.management = async () => {
  if (fault === "load") throw new Error("测试演示：加载失败，可以重试。");
  return {
    workspace_id: "workspace",
    title: "Marketing Workspace · 测试演示",
    status: "paused",
    phase,
    acceptance,
    grants: grants.map(row => structuredClone(row)),
    communication,
    group_options: groups
  } as CollaborationManagement;
};
api.collaboration.invite = async (_workspace, data) => {
  const grant = {
    ...data,
    id: "new-invite",
    workspace_id: "workspace",
    status: "invited",
    expires_at: expires,
    path: "/workspace-collaboration/new-invite"
  };
  grants.push(grant);
  return grant;
};
api.collaboration.change = async (_workspace, id, action, data) => {
  const grant = grants.find(row => row.id === id)!;
  if (action === "revoke") grant.status = "revoked";else {
    Object.assign(grant, data);
    if (action === "authorize") grant.status = "invited";
  }
  return {
    ...grant
  };
};
api.collaboration.phase = async (_workspace, action, note) => {
  phase = action === "accept" ? "delivered" : "awaiting_acceptance";
  acceptance = {
    note,
    confirmed_at: new Date().toISOString()
  };
  return {};
};
api.collaboration.portal = async id => ({
  title: "Marketing Workspace · 测试演示",
  grant: {
    ...grants.find(row => row.id === id)!
  },
  configuration: {
    revision: 0,
    services: [{
      service_key: "marketing",
      title: "产品内容服务",
      description: "按需求制作产品营销内容"
    }],
    processes: []
  },
  communication
});
api.collaboration.accept = async id => {
  const grant = grants.find(row => row.id === id)!;
  grant.status = "active";
  return {
    ...grant
  };
};
api.collaboration.linkGroup = async (_workspace, groupId) => {
  if (fault === "save" && !failedSave) {
    failedSave = true;
    throw new Error("测试演示：保存失败，请重试。已选择的 Group 保持不变。");
  }
  communication = {linked: !!groupId, group: groupId ? groups[0] : null};
  return communication;
};
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => String(input).includes("/api/v1/auth/me/email-verification") ? new Response(JSON.stringify({
  email: guest ? "maintainer@example.test" : "owner@example.test",
  email_verified: true
}), {
  headers: {
    "Content-Type": "application/json"
  }
}) : originalFetch(input, init);
void setLocale(params.get("locale") === "en" ? "en" : "zh").then(() => createRoot(document.getElementById("root")!).render(<React.StrictMode><MemoryRouter initialEntries={[guest ? "/workspace-collaboration/maintainer" : "/workspaces/workspace/collaboration"]}><QueryClientProvider client={new QueryClient({
      defaultOptions: {
        queries: {
          retry: false
        }
      }
    })}><main className="max-w-4xl mx-auto p-4"><p className="text-xs text-stone-500 mb-3">测试演示 · 所有数据与操作均为隔离样例</p><Routes><Route path="/workspaces/:workspaceId/collaboration" element={<WorkspaceCollaborationManagement workspaceId="workspace" identity="owner-user:customer-company" />} /><Route path="/workspace-collaboration/:grantId" element={<WorkspaceCollaboration />} /><Route path="*" element={<p>已打开现有 Group / Workspace（测试演示）</p>} /></Routes><DetailDrawer /></main></QueryClientProvider></MemoryRouter></React.StrictMode>));
