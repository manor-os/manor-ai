export const PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE = "The target website denied access (HTTP 403).";

/** Only the web-fetch contract distinguishes a website refusal from a tool failure. */
export function isWebsiteAccessDenied(activity: {
  name?: string;
  status?: string;
  reason?: unknown;
  result?: unknown;
  result_preview?: unknown;
}): boolean {
  if (activity.name !== "web_fetch" || !["error", "failed"].includes(activity.status || "")) return false;
  if (activity.reason === "website_access_denied") return true;
  const result = activity.result_preview ?? activity.result;
  if (typeof result === "string" && result.trim() === PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE) return true;
  let payload: unknown = result;
  if (typeof payload === "string") {
    try {
      payload = JSON.parse(payload);
    } catch {
      return false;
    }
  }
  return Boolean(payload && typeof payload === "object" && !Array.isArray(payload)
    && (payload as Record<string, unknown>).reason === "website_access_denied");
}

/** A host from this call's arguments identifies the source; it never proves denial. */
export function websiteAccessDeniedSource(activity: {
  args?: unknown;
  arguments?: unknown;
  arguments_preview?: unknown;
  display_params?: Record<string, unknown>;
}): string {
  let args = activity.arguments_preview ?? activity.arguments ?? activity.args;
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      args = undefined;
    }
  }
  const url = args && typeof args === "object" && !Array.isArray(args)
    ? (args as Record<string, unknown>).url : undefined;
  const target = url ?? activity.display_params?.target;
  if (typeof target !== "string") return "";
  try {
    const parsed = new URL(target);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.host : "";
  } catch {
    return "";
  }
}
