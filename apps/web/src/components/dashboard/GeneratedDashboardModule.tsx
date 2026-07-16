import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { Task, Workspace } from "../../lib/types";
import LoadingSpinner from "../ui/LoadingSpinner";
import {
  IconChatBubble,
  IconChevronDown,
  IconClose,
  IconDashboard,
  IconEyeOff,
  IconSend,
  IconTrash,
} from "../icons";

export type DashboardModuleDataSource =
  | "tasks"
  | "workspaces"
  | "activity"
  | "task_trends"
  | "stats"
  | "news"
  | "stocks"
  | "http_json"
  | "tool";

export interface DashboardModuleDataRequest {
  key: string;
  source: DashboardModuleDataSource;
  params: Record<string, unknown>;
  url?: string | null;
  tool_name?: string | null;
  tool_arguments?: Record<string, unknown>;
  refresh_seconds?: number;
}

export interface DashboardModuleCode {
  version: 1;
  runtime: "sandboxed_html";
  html: string;
  css: string;
  javascript: string;
  data_requests: DashboardModuleDataRequest[];
}

export interface DashboardGeneratedModule {
  id: string;
  title: string;
  description?: string | null;
  visible: boolean;
  size: "compact" | "wide";
  conversation_id?: string | null;
  code: DashboardModuleCode;
}

export interface DashboardModuleConversationMessage {
  role: "user" | "assistant";
  content: string;
  toolCalls?: string[];
}

export interface DashboardNewsItem {
  id: string;
  title: string;
  url: string;
  source?: string | null;
  published_at?: string | null;
  language?: string | null;
}

export interface DashboardStockQuote {
  symbol: string;
  price?: number | null;
  change?: number | null;
  change_percent?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  previous_close?: number | null;
  currency?: string | null;
  updated_at?: string | null;
  status: "ok" | "unavailable";
  provider?: string | null;
}

export interface DashboardModuleData {
  tasks: Task[];
  workspaces: Workspace[];
  activity: Record<string, any>[];
  taskTrends: Record<string, any>[];
  stats: Record<string, any> | undefined;
}

const MODULE_SOURCES: DashboardModuleDataSource[] = [
  "tasks",
  "workspaces",
  "activity",
  "task_trends",
  "stats",
  "news",
  "stocks",
  "http_json",
  "tool",
];

const SAFE_IDENTIFIER = /^[a-z][a-z0-9_]{0,39}$/;
const SAFE_TOOL_NAME = /^[A-Za-z0-9_.:-]{2,180}$/;
const SAFE_CONVERSATION_ID = /^[A-Za-z0-9_-]{10,64}$/;
const BLOCKED_HTML = /<(?:script|style|link|iframe|object|embed|form|meta|base)\b|\son[a-z]+\s*=|javascript\s*:/i;
const BLOCKED_CSS = /@import\b|url\s*\(|<\/style/i;
const BLOCKED_JAVASCRIPT = /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|importScripts|localStorage|sessionStorage|indexedDB|postMessage|eval)\b|navigator\s*\.\s*sendBeacon|document\s*\.\s*cookie|window\s*\.\s*(?:parent|top|opener)|\bimport\s*\(|\bnew\s+Function\b|while\s*\(\s*true\s*\)|for\s*\(\s*;\s*;|<\/script/i;

function normalizeParamValue(value: unknown): unknown {
  if (value == null || ["string", "number", "boolean"].includes(typeof value)) {
    return typeof value === "string" ? value.slice(0, 240) : value;
  }
  if (Array.isArray(value)) {
    return value
      .slice(0, 30)
      .map(normalizeParamValue)
      .filter((item) => item == null || ["string", "number", "boolean"].includes(typeof item));
  }
  return undefined;
}

function normalizeToolValue(value: unknown, depth = 0): unknown {
  if (depth > 5) return undefined;
  if (value == null || ["number", "boolean"].includes(typeof value)) return value;
  if (typeof value === "string") return value.slice(0, 2000);
  if (Array.isArray(value)) {
    return value
      .slice(0, 50)
      .map((item) => normalizeToolValue(item, depth + 1))
      .filter((item) => item !== undefined);
  }
  if (typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value).slice(0, 40)) {
      if (!key || key.length > 100) continue;
      const normalized = normalizeToolValue(item, depth + 1);
      if (normalized !== undefined) result[key] = normalized;
    }
    return result;
  }
  return undefined;
}

function normalizeHttpUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim() || value.length > 2_000) return null;
  try {
    const parsed = new URL(value.trim());
    if (
      parsed.protocol !== "https:"
      || !parsed.hostname
      || parsed.username
      || parsed.password
      || (parsed.port && parsed.port !== "443")
    ) {
      return null;
    }
    parsed.hash = "";
    return parsed.toString();
  } catch {
    return null;
  }
}

function collectToolResultUrls(value: unknown, urls: Set<string>, depth = 0): void {
  if (depth > 5 || value == null) return;
  if (Array.isArray(value)) {
    value.slice(0, 50).forEach((item) => collectToolResultUrls(item, urls, depth + 1));
    return;
  }
  if (typeof value !== "object") return;
  for (const [key, item] of Object.entries(value).slice(0, 40)) {
    if (
      ["url", "href", "link"].includes(key.toLowerCase())
      && typeof item === "string"
      && item.startsWith("https://")
    ) {
      urls.add(item);
    } else {
      collectToolResultUrls(item, urls, depth + 1);
    }
  }
}

function normalizeDataRequests(value: unknown): DashboardModuleDataRequest[] {
  if (!Array.isArray(value)) return [];
  const requests: DashboardModuleDataRequest[] = [];
  const keys = new Set<string>();

  for (const rawValue of value.slice(0, 8)) {
    const raw = rawValue as any;
    const key = String(raw?.key || "");
    const source = String(raw?.source || "") as DashboardModuleDataSource;
    if (!SAFE_IDENTIFIER.test(key) || keys.has(key) || !MODULE_SOURCES.includes(source)) continue;

    const params: Record<string, unknown> = {};
    if (raw?.params && typeof raw.params === "object" && !Array.isArray(raw.params)) {
      for (const [paramKey, paramValue] of Object.entries(raw.params).slice(0, 16)) {
        if (!SAFE_IDENTIFIER.test(paramKey)) continue;
        const normalized = normalizeParamValue(paramValue);
        if (normalized !== undefined) params[paramKey] = normalized;
      }
    }

    const toolName = raw?.tool_name ? String(raw.tool_name) : null;
    const toolArguments = normalizeToolValue(raw?.tool_arguments) as Record<string, unknown>;
    const httpUrl = source === "http_json" ? normalizeHttpUrl(raw?.url) : null;
    if (source === "http_json" && !httpUrl) continue;
    if (source === "tool" && (!toolName || !SAFE_TOOL_NAME.test(toolName))) continue;
    keys.add(key);
    requests.push({
      key,
      source,
      params,
      ...(source === "http_json"
        ? {
            url: httpUrl,
            refresh_seconds: Math.min(
              3600,
              Math.max(30, Math.round(Number(raw?.refresh_seconds) || 300)),
            ),
          }
        : source === "tool"
        ? {
            tool_name: toolName,
            tool_arguments: toolArguments || {},
            refresh_seconds: Math.min(
              3600,
              Math.max(30, Math.round(Number(raw?.refresh_seconds) || 300)),
            ),
          }
        : {}),
    });
  }
  return requests;
}

function isSafeCode(code: {
  html: string;
  css: string;
  javascript: string;
}): boolean {
  return (
    !BLOCKED_HTML.test(code.html) &&
    !BLOCKED_CSS.test(code.css) &&
    !BLOCKED_JAVASCRIPT.test(code.javascript) &&
    code.javascript.includes("renderDashboardModule")
  );
}

export function normalizeGeneratedModules(value: unknown): DashboardGeneratedModule[] {
  if (!Array.isArray(value)) return [];
  const modules: DashboardGeneratedModule[] = [];
  const ids = new Set<string>();

  for (const rawValue of value.slice(0, 12)) {
    const raw = rawValue as any;
    const id = String(raw?.id || "");
    const title = String(raw?.title || "").trim();
    const html = String(raw?.code?.html || "").slice(0, 20_000);
    const css = String(raw?.code?.css || "").slice(0, 30_000);
    const javascript = String(raw?.code?.javascript || "").slice(0, 50_000);
    if (
      !id.startsWith("module_") ||
      ids.has(id) ||
      !title ||
      raw?.code?.runtime !== "sandboxed_html" ||
      Number(raw?.code?.version) !== 1 ||
      !isSafeCode({ html, css, javascript })
    ) {
      continue;
    }

    ids.add(id);
    modules.push({
      id,
      title: title.slice(0, 80),
      description: raw?.description ? String(raw.description).slice(0, 180) : null,
      visible: raw?.visible !== false,
      size: raw?.size === "wide" ? "wide" : "compact",
      conversation_id:
        typeof raw?.conversation_id === "string" &&
        SAFE_CONVERSATION_ID.test(raw.conversation_id)
          ? raw.conversation_id
          : null,
      code: {
        version: 1,
        runtime: "sandboxed_html",
        html,
        css,
        javascript,
        data_requests: normalizeDataRequests(raw?.code?.data_requests),
      },
    });
  }
  return modules;
}

function numberParam(
  params: Record<string, unknown>,
  key: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const value = Number(params[key]);
  return Number.isFinite(value) ? Math.min(max, Math.max(min, Math.round(value))) : fallback;
}

function stringParam(params: Record<string, unknown>, key: string): string | undefined {
  const value = params[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function stringListParam(params: Record<string, unknown>, key: string): string[] {
  if (typeof params[key] === "string") {
    return (params[key] as string).split(",").map((value) => value.trim()).filter(Boolean).slice(0, 30);
  }
  return Array.isArray(params[key])
    ? (params[key] as unknown[]).map(String).filter(Boolean).slice(0, 30)
    : [];
}

function numberListParam(params: Record<string, unknown>, key: string): number[] {
  return Array.isArray(params[key])
    ? (params[key] as unknown[]).map(Number).filter(Number.isFinite).slice(0, 30)
    : [];
}

function isWithinDays(value: string | undefined, days: number): boolean {
  if (!value) return true;
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) || timestamp >= Date.now() - days * 86_400_000;
}

function resolveLocalData(
  request: DashboardModuleDataRequest,
  data: DashboardModuleData,
): unknown {
  const { params } = request;
  const limit = numberParam(params, "limit", 20, 1, 200);
  const days = numberParam(params, "days", 30, 1, 365);
  const query = stringParam(params, "query")?.toLocaleLowerCase();

  if (request.source === "stats") return data.stats ?? {};
  if (request.source === "task_trends") {
    return data.taskTrends.slice(-numberParam(params, "days", 14, 1, 365));
  }
  if (request.source === "tasks") {
    const statuses = stringListParam(params, "statuses");
    const priorities = numberListParam(params, "priorities");
    return data.tasks
      .filter((task) => {
        if (statuses.length > 0 && !statuses.includes(task.status)) return false;
        if (priorities.length > 0 && !priorities.includes(task.priority)) return false;
        if (
          query &&
          !`${task.title} ${task.description || ""} ${task.workspace_name || ""}`
            .toLocaleLowerCase()
            .includes(query)
        ) {
          return false;
        }
        return isWithinDays(task.created_at || task.completed_at, days);
      })
      .slice(0, limit);
  }
  if (request.source === "workspaces") {
    const statuses = stringListParam(params, "statuses");
    return (data.workspaces as any[])
      .filter((workspace) => {
        if (statuses.length > 0 && !statuses.includes(String(workspace.status || ""))) return false;
        return !query || String(workspace.name || "").toLocaleLowerCase().includes(query);
      })
      .slice(0, limit);
  }
  if (request.source === "activity") {
    const actions = stringListParam(params, "actions");
    return data.activity
      .filter((item) => {
        if (actions.length > 0 && !actions.includes(String(item.action || ""))) return false;
        if (
          query &&
          !`${item.name || ""} ${item.description || ""}`.toLocaleLowerCase().includes(query)
        ) {
          return false;
        }
        return isWithinDays(item.timestamp, days);
      })
      .slice(0, limit);
  }
  return [];
}

function escapeClosingTag(value: string, tag: "style" | "script"): string {
  return value.replace(new RegExp(`</${tag}`, "gi"), `<\\/${tag}`);
}

const MANOR_MODULE_BASE_CSS = `
:root {
  color-scheme: light;
  --module-text: #292524;
  --module-muted: #8f8780;
  --module-faint: #a8a29e;
  --module-border: rgba(28, 25, 23, 0.10);
  --module-border-strong: rgba(28, 25, 23, 0.18);
  --module-surface: #ffffff;
  --module-row: #fafaf9;
  --module-row-hover: #f5f5f4;
  --module-accent: #4f7169;
  --module-accent-soft: #eef5f3;
  --module-danger: #bd4a43;
  --module-warning: #9a6a2f;
  --module-info: #4d6fa8;
  --module-focus: rgba(79, 113, 105, 0.24);
  --module-font: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --module-radius-sm: 6px;
  --module-radius-md: 8px;
  --module-control-height: 30px;
  --module-space-1: 4px;
  --module-space-2: 6px;
  --module-space-3: 8px;
  --module-space-4: 12px;
  --module-space-5: 16px;
  --module-type-xs: 10px;
  --module-type-sm: 11px;
  --module-type-md: 12px;
  --module-type-lg: 13px;
  --module-type-title: 16px;
  --module-type-metric: 24px;
  font-family: var(--module-font);
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --module-text: #f5f5f4;
  --module-muted: rgba(255, 255, 255, 0.72);
  --module-faint: rgba(255, 255, 255, 0.56);
  --module-border: rgba(255, 255, 255, 0.12);
  --module-border-strong: rgba(255, 255, 255, 0.22);
  --module-surface: #18181b;
  --module-row: rgba(255, 255, 255, 0.04);
  --module-row-hover: rgba(255, 255, 255, 0.08);
  --module-accent: #9cc8be;
  --module-accent-soft: rgba(156, 200, 190, 0.14);
  --module-danger: #ff9b9b;
  --module-warning: #f5d08a;
  --module-info: #abc7ff;
  --module-focus: rgba(156, 200, 190, 0.28);
}
* { box-sizing: border-box; letter-spacing: 0; }
[hidden] { display: none !important; }
html, body { margin: 0; min-width: 0; background: transparent; color: var(--module-text); }
body {
  padding: 1px;
  font-family: var(--module-font);
  font-size: var(--module-type-md);
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
:where(h1, h2, h3, h4, p, ul, ol, figure) { margin: 0; }
:where(h1, h2, h3, h4) { color: var(--module-text); font-size: inherit; line-height: 1.3; }
:where(table) { width: 100%; border-collapse: collapse; border-spacing: 0; }
:where(button, a) { font: inherit; }
:where(button) { color: inherit; }
:where(button:not([class])) {
  min-height: var(--module-control-height);
  padding: 0 var(--module-space-4);
  border: 1px solid var(--module-border);
  border-radius: var(--module-radius-sm);
  background: var(--module-surface);
  cursor: pointer;
}
:where(button:not([class]):hover) { border-color: var(--module-border-strong); background: var(--module-row-hover); }
:where(button:disabled) { opacity: 0.42; cursor: default; }
:where(a) { color: var(--module-accent); }
:where(button:focus-visible, a:focus-visible) { outline: 2px solid var(--module-focus); outline-offset: 2px; }
::selection { background: var(--module-accent-soft); }
#module-root { min-width: 0; container-type: inline-size; }
#module-runtime-error {
  min-height: 72px;
  padding: var(--module-space-4);
  border: 1px solid var(--module-border);
  border-radius: var(--module-radius-md);
  color: var(--module-muted);
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}
`.trim();

function buildModuleDocument(module: DashboardGeneratedModule): string {
  const moduleId = JSON.stringify(module.id);
  const html = module.code.html;
  const css = escapeClosingTag(module.code.css, "style");
  const javascript = escapeClosingTag(module.code.javascript, "script");

  return `<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; object-src 'none'; media-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'">
<style>
${MANOR_MODULE_BASE_CSS}
${css}
</style></head><body>
<div id="module-root">${html}</div><div id="module-runtime-error" hidden></div>
<script>
(() => {
  const moduleId = ${moduleId};
  const send = (type, detail = {}) => window.parent.postMessage({ type, moduleId, ...detail }, "*");
  const showError = (error) => {
    const node = document.getElementById("module-runtime-error");
    const root = document.getElementById("module-root");
    if (root) root.hidden = true;
    if (node) { node.hidden = false; node.textContent = error instanceof Error ? error.message : String(error); }
    send("manor:dashboard:error", { message: error instanceof Error ? error.message : String(error) });
  };
  const measuredHeight = (node) => {
    if (!node || node.hidden) return 0;
    return Math.max(node.scrollHeight, node.getBoundingClientRect().height);
  };
  const reportHeight = () => {
    const root = document.getElementById("module-root");
    const errorNode = document.getElementById("module-runtime-error");
    send("manor:dashboard:resize", {
      height: Math.ceil(Math.max(measuredHeight(root), measuredHeight(errorNode)) + 2),
    });
  };
  try {
${javascript}
  } catch (error) {
    showError(error);
  }
  window.addEventListener("message", (event) => {
    const message = event.data;
    if (!message || message.type !== "manor:dashboard:data" || message.moduleId !== moduleId) return;
    document.documentElement.dataset.theme = message.context && message.context.theme === "dark" ? "dark" : "light";
    const renderer = window.renderDashboardModule;
    if (typeof renderer !== "function") { showError("Module render function is missing"); return; }
    const root = document.getElementById("module-root");
    const errorNode = document.getElementById("module-runtime-error");
    if (root) root.hidden = false;
    if (errorNode) errorNode.hidden = true;
    Promise.resolve(renderer(message.payload || {}, message.context || {}))
      .then(() => requestAnimationFrame(reportHeight))
      .catch(showError);
  });
  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-manor-url]") : null;
    if (!target) return;
    event.preventDefault();
    send("manor:dashboard:open-url", { url: target.getAttribute("data-manor-url") || "" });
  });
  window.addEventListener("error", (event) => showError(event.error || event.message));
  window.addEventListener("unhandledrejection", (event) => showError(event.reason));
  if (typeof ResizeObserver === "function") {
    const resizeObserver = new ResizeObserver(reportHeight);
    resizeObserver.observe(document.getElementById("module-root"));
    resizeObserver.observe(document.getElementById("module-runtime-error"));
  }
  send("manor:dashboard:ready");
  reportHeight();
})();
</script></body></html>`;
}

export default function GeneratedDashboardModule({
  module,
  data,
  editing,
  conversationOpen,
  conversationMessages,
  conversationPrompt,
  conversationUpdating,
  conversationLoading,
  previewPending,
  confirming,
  canMoveUp,
  canMoveDown,
  onMove,
  onHide,
  onDelete,
  onOpenConversation,
  onCloseConversation,
  onConversationPromptChange,
  onSubmitConversation,
  onConfirm,
  onDiscard,
}: {
  module: DashboardGeneratedModule;
  data: DashboardModuleData;
  editing: boolean;
  conversationOpen: boolean;
  conversationMessages: DashboardModuleConversationMessage[];
  conversationPrompt: string;
  conversationUpdating: boolean;
  conversationLoading: boolean;
  previewPending: boolean;
  confirming: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMove: (offset: -1 | 1) => void;
  onHide: () => void;
  onDelete: () => void;
  onOpenConversation: () => void;
  onCloseConversation: () => void;
  onConversationPromptChange: (value: string) => void;
  onSubmitConversation: () => void;
  onConfirm: () => void;
  onDiscard: () => void;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [frameReady, setFrameReady] = useState(false);
  const [frameHeight, setFrameHeight] = useState(150);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const newsRequests = useMemo(
    () => module.code.data_requests.filter((request) => request.source === "news"),
    [module.code.data_requests],
  );
  const newsQueries = useQueries({
    queries: newsRequests.map((request) => ({
      queryKey: ["dashboard-module-data", module.id, request.key, request.params],
      staleTime: 10 * 60_000,
      retry: 1,
      queryFn: () =>
        api.dashboard.news(
          stringParam(request.params, "query"),
          numberParam(request.params, "days", 1, 1, 365),
          numberParam(request.params, "limit", 8, 1, 20),
        ),
    })),
  });
  const stockRequests = useMemo(
    () => module.code.data_requests.filter((request) => request.source === "stocks"),
    [module.code.data_requests],
  );
  const stockQueries = useQueries({
    queries: stockRequests.map((request) => ({
      queryKey: ["dashboard-module-data", module.id, request.key, request.params],
      staleTime: 10_000,
      refetchInterval: numberParam(request.params, "refresh_seconds", 15, 10, 300) * 1000,
      refetchIntervalInBackground: false,
      retry: 1,
      queryFn: () => api.dashboard.stocks(stringListParam(request.params, "symbols")),
    })),
  });
  const httpRequests = useMemo(
    () => module.code.data_requests.filter((request) => request.source === "http_json"),
    [module.code.data_requests],
  );
  const httpQueries = useQueries({
    queries: httpRequests.map((request) => ({
      queryKey: [
        "dashboard-module-http-data",
        module.id,
        request.key,
        request.url,
      ],
      staleTime: (request.refresh_seconds ?? 300) * 1000,
      refetchInterval: (request.refresh_seconds ?? 300) * 1000,
      refetchIntervalInBackground: false,
      retry: 1,
      queryFn: () => api.dashboard.httpData(
        request.url || "",
        request.refresh_seconds ?? 300,
      ),
    })),
  });
  const toolRequests = useMemo(
    () => module.code.data_requests.filter((request) => request.source === "tool"),
    [module.code.data_requests],
  );
  const toolQueries = useQueries({
    queries: toolRequests.map((request) => ({
      queryKey: [
        "dashboard-module-tool-data",
        module.id,
        request.key,
        request.tool_name,
        request.tool_arguments,
      ],
      staleTime: (request.refresh_seconds ?? 300) * 1000,
      refetchInterval: (request.refresh_seconds ?? 300) * 1000,
      refetchIntervalInBackground: false,
      retry: 1,
      queryFn: () =>
        api.dashboard.toolData(
          request.tool_name || "",
          request.tool_arguments ?? {},
          module.conversation_id,
          request.refresh_seconds ?? 300,
        ),
    })),
  });
  const payload = useMemo(() => {
    const result: Record<string, unknown> = {};
    const newsByKey = new Map(
      newsRequests.map((request, index) => [request.key, newsQueries[index]?.data ?? []]),
    );
    const stocksByKey = new Map(
      stockRequests.map((request, index) => [request.key, stockQueries[index]?.data ?? []]),
    );
    const httpByKey = new Map(
      httpRequests.map((request, index) => [
        request.key,
        httpQueries[index]?.data?.result ?? null,
      ]),
    );
    const toolsByKey = new Map(
      toolRequests.map((request, index) => [
        request.key,
        toolQueries[index]?.data?.result ?? null,
      ]),
    );
    for (const request of module.code.data_requests) {
      if (request.source === "news") {
        result[request.key] = newsByKey.get(request.key) ?? [];
      } else if (request.source === "stocks") {
        result[request.key] = stocksByKey.get(request.key) ?? [];
      } else if (request.source === "http_json") {
        result[request.key] = httpByKey.get(request.key) ?? null;
      } else if (request.source === "tool") {
        result[request.key] = toolsByKey.get(request.key) ?? null;
      } else {
        result[request.key] = resolveLocalData(request, data);
      }
    }
    return result;
  }, [
    data,
    httpQueries,
    httpRequests,
    module.code.data_requests,
    newsQueries,
    newsRequests,
    stockQueries,
    stockRequests,
    toolQueries,
    toolRequests,
  ]);
  const payloadSignature = JSON.stringify(payload);
  const allowedUrls = useMemo(() => {
    const urls = new Set(
      newsQueries.flatMap((query) =>
        ((query.data ?? []) as DashboardNewsItem[])
          .map((item) => item.url)
          .filter((url) => url.startsWith("https://")),
      ),
    );
    httpQueries.forEach((query) => collectToolResultUrls(query.data?.result, urls));
    toolQueries.forEach((query) => collectToolResultUrls(query.data?.result, urls));
    return urls;
  }, [httpQueries, newsQueries, toolQueries]);
  const allowedUrlSignature = Array.from(allowedUrls).sort().join("\n");
  const sourceDocument = useMemo(() => buildModuleDocument(module), [module]);
  const dataLoading = [...newsQueries, ...stockQueries, ...httpQueries, ...toolQueries].some(
    (query) => query.isLoading,
  );
  const dataFailed = [...newsQueries, ...stockQueries, ...httpQueries, ...toolQueries].some(
    (query) => query.isError,
  );
  const previewFailed = dataFailed || Boolean(runtimeError);
  const previewReady = frameReady && !dataLoading && !previewFailed;

  useLayoutEffect(() => {
    setFrameReady(false);
    setRuntimeError(null);
    setFrameHeight(150);
  }, [sourceDocument]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.source !== iframeRef.current?.contentWindow) return;
      const message = event.data as Record<string, any> | null;
      if (!message || message.moduleId !== module.id) return;
      if (message.type === "manor:dashboard:ready") {
        setFrameReady(true);
      } else if (message.type === "manor:dashboard:resize") {
        const height = Number(message.height);
        if (Number.isFinite(height)) setFrameHeight(Math.min(720, Math.max(110, height)));
      } else if (message.type === "manor:dashboard:error") {
        setRuntimeError(String(message.message || t("page.dashboard.module_runtime_failed")));
      } else if (
        message.type === "manor:dashboard:open-url" &&
        typeof message.url === "string" &&
        allowedUrls.has(message.url)
      ) {
        window.open(message.url, "_blank", "noopener,noreferrer");
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [allowedUrlSignature, allowedUrls, module.id]);

  useEffect(() => {
    if (!frameReady || !iframeRef.current?.contentWindow) return;
    iframeRef.current.contentWindow.postMessage(
      {
        type: "manor:dashboard:data",
        moduleId: module.id,
        payload,
        context: {
          locale: document.documentElement.lang || navigator.language || "en",
          theme: document.documentElement.dataset.theme === "dark" ? "dark" : "light",
        },
      },
      "*",
    );
  }, [frameReady, module.id, payloadSignature]);

  return (
    <article
      className={`dashboard-generated-module${module.size === "wide" ? " dashboard-generated-module--wide" : ""}${editing ? " is-editing" : ""}`}
      data-module-id={module.id}
    >
      <header className="dashboard-generated-module-header">
        <div className="dashboard-generated-module-heading">
          <span className="dashboard-generated-module-icon" aria-hidden="true">
            <IconDashboard size={15} />
          </span>
          <div>
            <h2>{module.title}</h2>
            {module.description && <p>{module.description}</p>}
          </div>
        </div>
        {editing && (
          <div className="dashboard-generated-module-actions">
            <button
              type="button"
              className={conversationOpen ? "is-active" : undefined}
              aria-pressed={conversationOpen}
              title={t("page.dashboard.edit_module_with_ai")}
              aria-label={`${t("page.dashboard.edit_module_with_ai")} ${module.title}`}
              onClick={onOpenConversation}
            >
              <IconChatBubble size={13} />
            </button>
            <button
              type="button"
              disabled={!canMoveUp}
              title={t("page.dashboard.move_up")}
              aria-label={`${t("page.dashboard.move_up")} ${module.title}`}
              onClick={() => onMove(-1)}
            >
              <IconChevronDown size={13} style={{ transform: "rotate(180deg)" }} />
            </button>
            <button
              type="button"
              disabled={!canMoveDown}
              title={t("page.dashboard.move_down")}
              aria-label={`${t("page.dashboard.move_down")} ${module.title}`}
              onClick={() => onMove(1)}
            >
              <IconChevronDown size={13} />
            </button>
            <button
              type="button"
              title={t("page.dashboard.hide_widget")}
              aria-label={`${t("page.dashboard.hide_widget")} ${module.title}`}
              onClick={onHide}
            >
              <IconEyeOff size={13} />
            </button>
            <button
              type="button"
              title={t("action.delete")}
              aria-label={`${t("action.delete")} ${module.title}`}
              onClick={onDelete}
            >
              <IconTrash size={13} />
            </button>
          </div>
        )}
      </header>

      <div className="dashboard-generated-runtime" aria-busy={dataLoading}>
        <iframe
          ref={iframeRef}
          title={module.title}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          srcDoc={sourceDocument}
          style={{ height: frameHeight }}
          onLoad={() => setFrameReady(true)}
        />
        {(dataLoading || !frameReady) && (
          <div className="dashboard-generated-runtime-status">
            {t("page.dashboard.module_loading")}
          </div>
        )}
        {dataFailed && !dataLoading && (
          <div className="dashboard-generated-runtime-status is-error">
            {t("page.dashboard.module_data_failed")}
          </div>
        )}
        {runtimeError && (
          <div className="dashboard-generated-runtime-status is-error">{runtimeError}</div>
        )}
      </div>
      {editing && conversationOpen && (
        <section
          className="dashboard-module-conversation"
          aria-label={`${t("page.dashboard.module_conversation_title")} ${module.title}`}
        >
          <header className="dashboard-module-conversation-header">
            <div>
              <IconChatBubble size={14} />
              <strong>{t("page.dashboard.module_conversation_title")}</strong>
            </div>
            <button
              type="button"
              disabled={conversationUpdating}
              title={t("page.dashboard.close_module_conversation")}
              aria-label={t("page.dashboard.close_module_conversation")}
              onClick={onCloseConversation}
            >
              <IconClose size={13} />
            </button>
          </header>
          {(conversationMessages.length > 0 || conversationUpdating || conversationLoading) && (
            <div className="dashboard-module-conversation-messages" aria-live="polite">
              {conversationMessages.slice(-8).map((message, index) => (
                <div
                  key={`${message.role}-${index}-${message.content.slice(0, 24)}`}
                  className={`dashboard-module-conversation-message is-${message.role}`}
                >
                  <span>{message.content}</span>
                  {message.toolCalls && message.toolCalls.length > 0 && (
                    <div className="dashboard-module-conversation-tools">
                      {message.toolCalls.map((toolName) => (
                        <code key={toolName}>{toolName}</code>
                      ))}
                    </div>
                  )}
                </div>
              ))}
              {conversationUpdating && (
                <div className="dashboard-module-conversation-message is-assistant is-updating">
                  <LoadingSpinner size={12} />
                  {t("page.dashboard.updating_module_code")}
                </div>
              )}
              {conversationLoading && !conversationUpdating && (
                <div className="dashboard-module-conversation-message is-assistant is-updating">
                  <LoadingSpinner size={12} />
                  {t("page.dashboard.loading_module_conversation")}
                </div>
              )}
            </div>
          )}
          <div className="dashboard-module-conversation-composer">
            <textarea
              rows={2}
              value={conversationPrompt}
              disabled={conversationUpdating || conversationLoading}
              placeholder={t("page.dashboard.module_conversation_placeholder")}
              aria-label={t("page.dashboard.module_conversation_placeholder")}
              onChange={(event) => onConversationPromptChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  if (
                    conversationPrompt.trim() &&
                    !conversationUpdating &&
                    !conversationLoading
                  ) {
                    onSubmitConversation();
                  }
                }
              }}
            />
            <button
              type="button"
              disabled={
                !conversationPrompt.trim() || conversationUpdating || conversationLoading
              }
              title={t("page.dashboard.send_module_edit")}
              aria-label={t("page.dashboard.send_module_edit")}
              onClick={onSubmitConversation}
            >
              {conversationUpdating ? <LoadingSpinner size={13} /> : <IconSend size={14} />}
            </button>
          </div>
        </section>
      )}
      {previewPending && (
        <footer className="dashboard-generated-confirmation" aria-live="polite">
          <div className="dashboard-generated-confirmation-copy">
            <strong>
              {conversationUpdating
                ? t("page.dashboard.updating_module_code")
                : previewFailed
                ? t("page.dashboard.preview_failed")
                : previewReady
                  ? t("page.dashboard.preview_ready")
                  : t("page.dashboard.preparing_preview")}
            </strong>
            <span>
              {conversationUpdating
                ? t("page.dashboard.preparing_preview_detail")
                : previewFailed
                ? t("page.dashboard.preview_failed_detail")
                : previewReady
                  ? t("page.dashboard.preview_ready_detail")
                  : t("page.dashboard.preparing_preview_detail")}
            </span>
          </div>
          <div className="dashboard-generated-confirmation-actions">
            <button
              type="button"
              disabled={confirming || conversationUpdating}
              onClick={onDiscard}
            >
              {t("page.dashboard.discard_preview")}
            </button>
            <button
              type="button"
              className="is-primary"
              disabled={!previewReady || confirming || conversationUpdating}
              onClick={onConfirm}
            >
              {confirming && <LoadingSpinner size={13} />}
              {t("page.dashboard.confirm_and_save")}
            </button>
          </div>
        </footer>
      )}
    </article>
  );
}
