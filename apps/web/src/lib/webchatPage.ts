export type WebchatSide = "left" | "right";
type ModuleBase = { id: string; side: WebchatSide };
export type ResolvedWorkspaceContent = { name: string; body: string; image_url: string; items: string[] };
export type WebchatBrandDefaults = { name: string; logo_url: string; website: string };
export type WebchatWorkspaceResources = {
  brand: WebchatBrandDefaults;
  profile: ResolvedWorkspaceContent & { id: "workspace" };
  documents: { id: string; name: string; body: string }[];
  documents_next_cursor: number | null;
  actions: { id: string; name: string; description: string }[];
};
export type WebchatModule = ModuleBase & (
  | { type: "brand"; name: string; logo_url: string; website: string }
  | { type: "text"; title: string; body: string }
  | { type: "image"; title: string; url: string; caption: string }
  | { type: "list"; title: string; items: { title: string; description: string }[] }
  | { type: "links"; title: string; items: { label: string; url: string }[] }
  | { type: "faq"; title: string; items: { question: string; answer: string }[] }
  | { type: "form"; title: string; fields: string[] }
  | { type: "workspace_content"; title: string; source: "profile" | "document"; resource_id: string; resolved?: ResolvedWorkspaceContent | null }
  | { type: "workspace_action"; title: string; description: string; binding_id: string; fields: string[]; submit_label: string }
);
export type WebchatModuleType = WebchatModule["type"];
export interface WebchatPage { version: 1; modules: WebchatModule[] }
export const WEBCHAT_MODULE_TYPES: WebchatModuleType[] = ["brand", "text", "image", "list", "links", "faq", "form", "workspace_content", "workspace_action"];
export const emptyWebchatPage = (): WebchatPage => ({ version: 1, modules: [] });

export function safeWebchatUrl(value: unknown): string {
  if (typeof value !== "string" || !/^https?:\/\/[^/]/i.test(value) || value.length > 2048 || /[\s\\\u0000-\u001f]/.test(value)) return "";
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

const text = (value: unknown, limit: number) => typeof value === "string" && value.length <= limit;
const url = (value: unknown) => value === "" || Boolean(safeWebchatUrl(value));
const PUBLIC_ACTION_RESERVED_FIELDS = new Set(["fields", "trigger", "webchat_channel_config_id", "webchat_module_id", "webchat_session_id", "webchat_submission_id"]);
const fields = (value: unknown, rejectActionReserved = false) => {
  if (!Array.isArray(value) || value.length > 6) return false;
  const normalized = value.map(field => typeof field === "string" ? field.toLowerCase().replace(/[^a-z0-9]/g, "") : "");
  return new Set(value.map(field => typeof field === "string" ? field.toLowerCase() : field)).size === value.length && value.every((field, index) => (
    text(field, 80) && field.length > 0 && field.trim() === field && !/[\u0000-\u001f]/.test(field)
    && !["password", "passwd", "secret", "token", "apikey", "creditcard", "cvv", "cvc"].some(part => normalized[index].includes(part))
    && (!rejectActionReserved || !PUBLIC_ACTION_RESERVED_FIELDS.has(field.toLowerCase()))
  ));
};
const resolved = (value: unknown) => value == null || Boolean(value && typeof value === "object"
  && text((value as ResolvedWorkspaceContent).name, 160)
  && text((value as ResolvedWorkspaceContent).body, 4000)
  && url((value as ResolvedWorkspaceContent).image_url)
  && Array.isArray((value as ResolvedWorkspaceContent).items)
  && (value as ResolvedWorkspaceContent).items.length <= 8
  && (value as ResolvedWorkspaceContent).items.every(item => text(item, 500)));

/** Shared by the editor and public renderer; invalid stored data fails closed. */
export function validWebchatPage(value: unknown): value is WebchatPage {
  if (!value || typeof value !== "object") return false;
  const page = value as WebchatPage;
  if (page.version !== 1 || !Array.isArray(page.modules) || page.modules.length > 12) return false;
  const ids = new Set<string>();
  return page.modules.every(module => {
    if (!module || !text(module.id, 80) || !/^[A-Za-z0-9_-]+$/.test(module.id) || ids.has(module.id) || !["left", "right"].includes(module.side)) return false;
    ids.add(module.id);
    if (module.type !== "brand" && !text("title" in module ? module.title : undefined, 160)) return false;
    switch (module.type) {
      case "brand": return text(module.name, 160) && url(module.logo_url) && url(module.website);
      case "text": return text(module.body, 4000);
      case "image": return url(module.url) && text(module.caption, 500);
      case "list": return Array.isArray(module.items) && module.items.length <= 12 && module.items.every(item => item && text(item.title, 160) && text(item.description, 1000));
      case "links": return Array.isArray(module.items) && module.items.length <= 12 && module.items.every(item => item && text(item.label, 160) && url(item.url));
      case "faq": return Array.isArray(module.items) && module.items.length <= 12 && module.items.every(item => item && text(item.question, 160) && text(item.answer, 2000));
      case "form": return fields(module.fields);
      case "workspace_content": return text(module.title, 160) && ["profile", "document"].includes(module.source) && text(module.resource_id, 80) && /^[A-Za-z0-9_-]*$/.test(module.resource_id) && resolved(module.resolved);
      case "workspace_action": return text(module.title, 160) && text(module.description, 1000) && text(module.binding_id, 80) && /^[A-Za-z0-9_-]+$/.test(module.binding_id) && fields(module.fields, true) && text(module.submit_label, 80);
      default: return false;
    }
  });
}

export function webchatModuleVisible(module: WebchatModule): boolean {
  switch (module.type) {
    case "brand": return Boolean(module.name.trim() || safeWebchatUrl(module.logo_url) || safeWebchatUrl(module.website));
    case "text": return Boolean(module.title.trim() || module.body.trim());
    case "image": return Boolean(safeWebchatUrl(module.url));
    case "list": return module.items.some(item => item.title.trim() || item.description.trim());
    case "links": return module.items.some(item => item.label.trim() && safeWebchatUrl(item.url));
    case "faq": return module.items.some(item => item.question.trim() && item.answer.trim());
    case "form": return module.fields.some(field => field.trim());
    case "workspace_content": return Boolean(module.resolved?.name.trim() || module.resolved?.body.trim() || module.resolved?.items.some(item => item.trim()) || safeWebchatUrl(module.resolved?.image_url));
    case "workspace_action": return Boolean(module.binding_id);
  }
}

export function webchatWorkspaceDisplayName(workspace: { name: string; identity_label?: string | null }): string {
  return workspace.identity_label?.trim() || workspace.name;
}

export function createWebchatModule(type: WebchatModuleType, side: WebchatSide, brand: WebchatBrandDefaults): WebchatModule {
  const base = { id: globalThis.crypto?.randomUUID?.() || `module_${Date.now()}_${Math.random().toString(36).slice(2)}`, side };
  switch (type) {
    case "brand": return { ...base, type, ...brand };
    case "text": return { ...base, type, title: "", body: "" };
    case "image": return { ...base, type, title: "", url: "", caption: "" };
    case "list": return { ...base, type, title: "", items: [{ title: "", description: "" }] };
    case "links": return { ...base, type, title: "", items: [{ label: "", url: "" }] };
    case "faq": return { ...base, type, title: "", items: [{ question: "", answer: "" }] };
    case "form": return { ...base, type, title: "", fields: [] };
    case "workspace_content": return { ...base, type, title: "", source: "profile", resource_id: "workspace", resolved: null };
    case "workspace_action": return { ...base, type, title: "", description: "", binding_id: "", fields: [], submit_label: "" };
  }
}

/** Browser previews are derived from the current authenticated resource list. */
export function withWorkspaceResourcePreviews(page: WebchatPage, resources?: WebchatWorkspaceResources): WebchatPage {
  if (!resources) return page;
  return {
    ...page,
    modules: page.modules.map(module => {
      if (module.type !== "workspace_content") return module;
      if (module.source === "profile") return { ...module, resolved: resources.profile };
      const document = resources.documents.find(item => item.id === module.resource_id);
      return {
        ...module,
        resolved: document ? { name: document.name, body: document.body, image_url: "", items: [] } : null,
      };
    }),
  };
}

/** Persist references only; the server resolves fresh public content on every read. */
export function webchatPageForSave(page: WebchatPage): WebchatPage {
  return {
    ...page,
    modules: page.modules.map(module => {
      if (module.type !== "workspace_content") return module;
      const { resolved: _resolved, ...stored } = module;
      return stored;
    }),
  };
}

function canonicalJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalJsonValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalJsonValue(item)]),
  );
}

/** Compare page semantics without relying on JSON/JSONB object key order. */
export function webchatPageFingerprint(page: WebchatPage): string {
  return JSON.stringify(canonicalJsonValue(webchatPageForSave(page)));
}

/** beforeId is a destination module, or null to append to the destination side. */
export function moveWebchatModule(page: WebchatPage, id: string, side: WebchatSide, beforeId: string | null): WebchatPage {
  const source = page.modules.find(module => module.id === id);
  if (!source || id === beforeId || (beforeId && !page.modules.some(module => module.id === beforeId && module.side === side))) return page;
  const modules = page.modules.filter(module => module.id !== id);
  const index = beforeId ? modules.findIndex(module => module.id === beforeId) : modules.length;
  modules.splice(index, 0, { ...source, side });
  return { ...page, modules };
}
