import { t } from "./i18n";
import { formatUserFacingLabel, formatUserFacingText } from "./taskDisplay";

export function parseTags(tags: string | string[] | undefined): string[] {
  if (!tags) return [];
  if (Array.isArray(tags)) return tags;
  return String(tags)
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

export function displayAgentCategory(category?: string | null): string {
  const raw = String(category || "").trim();
  if (!raw) return t("page.workspace_detail.agent");
  if (raw.toLowerCase() === "mcp") return "Connector";
  if (raw.toLowerCase() === "builtin") return "Built-in";
  if (raw.toLowerCase() === "builtinsystem") return "Built-in";
  return formatUserFacingLabel(raw) || t("page.workspace_detail.agent");
}

export function displayAgentTag(tag?: string | null): string {
  const raw = String(tag || "").trim();
  if (!raw) return "";
  if (raw === "auto_created") return "Workspace generated";
  return formatUserFacingLabel(raw);
}

export function displayToolName(tool: any): string {
  return formatUserFacingText(formatUserFacingLabel(tool?.display_name || tool?.name || ""))
    .replace(/^MCP\s+/i, "")
    .replace(/\bMCP\b/g, "Connector")
    .trim();
}

export function displayToolDescription(tool: any): string {
  const raw = String(tool?.description || "").trim();
  if (!raw) return "";
  const normalized = formatUserFacingText(raw)
    .replace(/\[MCP:[^\]]+\]\s*/gi, "")
    .replace(/\bSystem tool:\s*/gi, "Built-in tool: ")
    .replace(/\bmcp__([a-z0-9_]+)__([a-z0-9_]+)/gi, (_match, server, action) => `${formatUserFacingLabel(server)} ${formatUserFacingLabel(action)}`)
    .replace(/\s+/g, " ")
    .trim();
  if (/^Built-in tool:\s*$/i.test(normalized)) return "Built-in tool available to this agent.";
  return normalized;
}

export function parseMcpToolName(name?: string | null): { serverKey: string; actionKey: string } | null {
  const raw = String(name || "").trim();
  if (!raw.startsWith("mcp__")) return null;
  const parts = raw.split("__");
  if (parts.length < 3) return null;
  const serverKey = parts[1]?.trim();
  const actionKey = parts.slice(2).join("__").trim();
  if (!serverKey || !actionKey) return null;
  return { serverKey, actionKey };
}

export function mcpToolActionLabel(tool: any): string {
  const parsed = parseMcpToolName(tool?.name);
  const raw = parsed?.actionKey || tool?.display_name || tool?.name || "";
  return formatUserFacingLabel(raw);
}

export function mcpProviderLabel(server: any): string {
  return formatUserFacingLabel(server?.name || server?.server_key || "MCP");
}
