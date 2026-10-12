import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api, type WorkspaceAppView } from "./lib/api";
import { setLocale } from "./lib/i18n";
import WorkspaceAppViewSettings from "./components/workspaces/WorkspaceAppViewSettings";
import "./index.css";

const actions: WorkspaceAppView["actions"] = [
  { key: "workflow:one", kind: "workflow", title: "把一个想法做成内容", source_title: "把一个想法做成内容", description: "从素材到文案，再到可审阅的成品", binding_id: "one" },
  { key: "workflow:two", kind: "workflow", title: "改写已有内容", source_title: "改写已有内容", description: "将一段长内容变成适合不同平台的版本", binding_id: "two" },
  { key: "service:strategy", kind: "chat", title: "聊聊内容方向", source_title: "聊聊内容方向", description: "一起确定角度、受众与表达方式", prompt: "聊聊内容方向" },
];
const view: WorkspaceAppView = {
  workspace_id: "preview", eyebrow: null, title: "内容工作室", description: "从灵感到发布，让每一步都轻松一点。", status: "active", can_start: true,
  cover_image_url: null, hero: "workflow:one", actions, available_actions: actions,
  workflow_options: actions.filter((action) => action.kind === "workflow").map((action) => ({ binding_id: action.binding_id!, workflow_id: action.binding_id!, title: action.title, description: action.description, placeholder: "", inputs: [] })),
  automation_options: [], agent_options: [],
  config: { version: 1, eyebrow: null, title: null, description: null, hero: "workflow:one", order: actions.map((action) => action.key), hidden: [], labels: {}, descriptions: {}, forms: {}, targets: {}, visual: "motion", layout: "feature", motion_cover_url: null, motion_cover_source_url: null },
};
api.workspaces.appView = async () => view;
api.workspaces.saveAppView = async (_id, config) => { view.config = config; return {}; };

void setLocale("zh").then(() => {
  document.body.style.cssText = "margin:0;background:#f7f6f4;";
  createRoot(document.getElementById("root")!).render(<React.StrictMode><MemoryRouter><QueryClientProvider client={new QueryClient()}><div style={{ padding: "48px clamp(16px, 4vw, 64px)" }}><div style={{ maxWidth: 1320, margin: "0 auto 12px", fontSize: 12, color: "#78716c" }}>Workspace App View · 示意数据</div><WorkspaceAppViewSettings workspaceId="preview" canManage /></div></QueryClientProvider></MemoryRouter></React.StrictMode>);
});
