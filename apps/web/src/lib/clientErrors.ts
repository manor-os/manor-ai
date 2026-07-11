import { getAuthToken } from "./authToken";
import {
  getErrorText,
  isRecoverableUiRuntimeError,
  isStaleChunkError,
} from "../utils/recoverableErrors";

type ClientApp = "web" | "admin";
type ClientErrorLevel = "error" | "warning" | "info";

interface CaptureOptions {
  level?: ClientErrorLevel;
  handled?: boolean;
  mechanism?: string;
  boundary?: string;
  componentStack?: string;
  fingerprint?: string;
  tags?: Record<string, string | number | boolean | null | undefined>;
  extra?: Record<string, unknown>;
}

interface NormalizedError {
  name?: string;
  message: string;
  stack?: string;
}

const CAPTURE_ENDPOINT = "/api/v1/client-errors";
const DEDUPE_WINDOW_MS = 30_000;

let clientApp: ClientApp = "web";
let globalHandlersInstalled = false;
const recentFingerprints = new Map<string, number>();

export function initializeClientErrorCapture(app: ClientApp) {
  clientApp = app;
  if (!isCaptureEnabled() || globalHandlersInstalled || typeof window === "undefined") {
    return;
  }
  globalHandlersInstalled = true;

  window.addEventListener("error", (event) => {
    captureClientError(event.error || event.message, {
      handled: false,
      mechanism: "window.error",
      extra: {
        filename: event.filename,
        lineno: event.lineno,
        colno: event.colno,
      },
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    captureClientError(event.reason, {
      handled: false,
      mechanism: "window.unhandledrejection",
    });
  });
}

export function captureClientError(error: unknown, options: CaptureOptions = {}) {
  if (!isCaptureEnabled() || typeof window === "undefined") return;

  const normalized = normalizeError(error);
  if (shouldSkipClientErrorCapture(normalized, options)) return;

  const recoverable = isRecoverableUiRuntimeError(error);
  const staleChunk = isStaleChunkError(error);
  const level = options.level || (recoverable ? "warning" : "error");
  const tags = sanitizeTags({
    ...options.tags,
    mechanism: options.mechanism,
    boundary: options.boundary,
    recoverable,
    stale_chunk: staleChunk,
  });
  const payload = {
    source: clientApp,
    level,
    handled: options.handled ?? true,
    name: clip(scrub(normalized.name), 120),
    message: clip(scrub(normalized.message), 4000) || "Unknown client error",
    stack: clip(scrub(normalized.stack), 12000),
    component_stack: clip(scrub(options.componentStack), 8000),
    fingerprint: options.fingerprint || fingerprintFor(normalized, options),
    route: currentRoute(),
    url: safeCurrentUrl(),
    release: typeof __APP_VERSION__ !== "undefined" ? __APP_VERSION__ : undefined,
    environment: import.meta.env.MODE,
    tags,
    extra: sanitizeValue(options.extra || {}),
    context: {
      viewport: `${window.innerWidth}x${window.innerHeight}`,
      language: navigator.language,
      online: navigator.onLine,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    },
  };

  if (isDuplicate(payload.fingerprint)) return;
  void send(payload);
}

export function captureClientMessage(
  message: string,
  options: CaptureOptions = {},
) {
  captureClientError(new Error(message), {
    ...options,
    level: options.level || "info",
    handled: options.handled ?? true,
  });
}

function isCaptureEnabled(): boolean {
  const flag = import.meta.env.VITE_CLIENT_ERROR_CAPTURE;
  if (flag === "1" || flag === "true") return true;
  if (flag === "0" || flag === "false") return false;
  return !import.meta.env.DEV;
}

function normalizeError(error: unknown): NormalizedError {
  if (error instanceof Error) {
    return {
      name: error.name,
      message: error.message || "Error",
      stack: error.stack,
    };
  }
  if (typeof error === "string") {
    return { name: "Error", message: error };
  }
  const text = getErrorText(error);
  if (text) return { name: "Error", message: text };
  return { name: "Error", message: "Unknown client error" };
}

function shouldSkipClientErrorCapture(error: NormalizedError, options: CaptureOptions): boolean {
  if (!isHandledApiNetworkMechanism(options.mechanism)) return false;
  return isTransientFetchNetworkError(error);
}

function isHandledApiNetworkMechanism(mechanism: string | undefined): boolean {
  return mechanism === "api.network" ||
    mechanism === "admin_api.network" ||
    mechanism === "commerce_api.network";
}

function isTransientFetchNetworkError(error: NormalizedError): boolean {
  const name = (error.name || "").toLowerCase();
  const message = error.message.toLowerCase();
  return (
    name === "typeerror" &&
    (
      message === "failed to fetch" ||
      message.includes("networkerror when attempting to fetch resource") ||
      message.includes("load failed")
    )
  );
}

async function send(payload: Record<string, unknown>) {
  const token = getAuthToken();
  try {
    await fetch(CAPTURE_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(payload),
      cache: "no-store",
      keepalive: true,
    });
  } catch {
    if (!navigator.sendBeacon) return;
    try {
      const blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
      navigator.sendBeacon(CAPTURE_ENDPOINT, blob);
    } catch {
      // best-effort only
    }
  }
}

function currentRoute(): string {
  const params = new URLSearchParams(window.location.search);
  for (const key of Array.from(params.keys())) {
    params.set(key, "<redacted>");
  }
  const query = params.toString();
  return `${window.location.pathname}${query ? `?${query}` : ""}`;
}

function safeCurrentUrl(): string {
  try {
    const url = new URL(window.location.href);
    for (const key of Array.from(url.searchParams.keys())) {
      url.searchParams.set(key, "<redacted>");
    }
    url.hash = "";
    return url.toString();
  } catch {
    return window.location.pathname;
  }
}

function sanitizeTags(tags: CaptureOptions["tags"]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(tags || {})) {
    if (value === null || value === undefined) continue;
    out[clip(scrub(key), 80) || "key"] = clip(scrub(String(value)), 240) || "";
  }
  return out;
}

function sanitizeValue(value: unknown, depth = 0): unknown {
  if (depth > 4) return "[max-depth]";
  if (value === null || value === undefined || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") return clip(scrub(value), 1000);
  if (Array.isArray(value)) return value.slice(0, 50).map((item) => sanitizeValue(item, depth + 1));
  if (typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(value).slice(0, 80)) {
      const cleanKey = clip(scrub(key), 120) || "key";
      if (/token|secret|password|api[_-]?key|authorization/i.test(cleanKey)) {
        out[cleanKey] = "<redacted>";
      } else {
        out[cleanKey] = sanitizeValue(raw, depth + 1);
      }
    }
    return out;
  }
  return clip(scrub(String(value)), 1000);
}

function scrub(value: string | undefined): string | undefined {
  if (!value) return value;
  return value
    .replace(/\b(authorization|bearer|token|access_token|refresh_token|api[_-]?key|password|secret|client_secret)\b\s*[:=]\s*([^\s&"']+)/gi, "$1=<redacted>")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "<email>")
    .replace(/\x00/g, "");
}

function clip(value: string | undefined, max: number): string | undefined {
  if (!value || value.length <= max) return value;
  return `${value.slice(0, max - 14)}...[truncated]`;
}

function fingerprintFor(error: NormalizedError, options: CaptureOptions): string {
  const firstFrames = (error.stack || "").split("\n").slice(0, 5).join("\n");
  return hash([
    clientApp,
    options.mechanism || "",
    error.name || "",
    error.message.slice(0, 500),
    firstFrames,
    currentRoute(),
  ].join("|"));
}

function hash(value: string): string {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < value.length; i += 1) {
    const ch = value.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return `${(h2 >>> 0).toString(16).padStart(8, "0")}${(h1 >>> 0).toString(16).padStart(8, "0")}`;
}

function isDuplicate(fingerprint: string): boolean {
  const now = Date.now();
  const last = recentFingerprints.get(fingerprint);
  recentFingerprints.set(fingerprint, now);
  for (const [key, seenAt] of recentFingerprints) {
    if (now - seenAt > DEDUPE_WINDOW_MS * 4) recentFingerprints.delete(key);
  }
  return typeof last === "number" && now - last < DEDUPE_WINDOW_MS;
}
