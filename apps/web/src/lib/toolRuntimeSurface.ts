export type RuntimeToolSurface =
  | "manor_tool"
  | "external_mcp"
  | "external_tool";

export const TOOL_SURFACE_BADGES: Record<
  RuntimeToolSurface,
  { label: string; compactLabel: string; bg: string; color: string; border: string }
> = {
  manor_tool: {
    label: "Manor tool",
    compactLabel: "Manor",
    bg: "#f5f7f5",
    color: "#4f7d75",
    border: "#d6e3df",
  },
  external_mcp: {
    label: "MCP",
    compactLabel: "MCP",
    bg: "#f5f5f4",
    color: "#57534e",
    border: "#e7e5e4",
  },
  external_tool: {
    label: "Tool",
    compactLabel: "Tool",
    bg: "#fafaf9",
    color: "#78716c",
    border: "#e7e5e4",
  },
};

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

function isManorInternalToolName(name?: string) {
  const normalized = String(name || "").trim().toLowerCase();
  if (!normalized) return false;
  return normalized
    .split("__")
    .some((part) => part === "manor" || part.startsWith("manor_"));
}

export function runtimeToolSurface(name?: string): RuntimeToolSurface {
  if (isManorInternalToolName(name)) {
    return "manor_tool";
  }
  const parsedMcp = parseMcpToolName(name);
  if (parsedMcp) {
    return "external_mcp";
  }
  return "external_tool";
}

export function runtimeToolBadge(name?: string) {
  return TOOL_SURFACE_BADGES[runtimeToolSurface(name)];
}

export function processSurfaceSummary(names: Array<string | undefined>) {
  let manor = 0;
  let other = 0;
  for (const name of names) {
    const surface = runtimeToolSurface(name);
    if (surface === "manor_tool") manor += 1;
    else other += 1;
  }
  if (manor > 0 && other > 0) return `${manor} Manor · ${other} other`;
  if (manor > 0) return "Manor only";
  if (other > 0) return "Tool/MCP only";
  return "";
}
