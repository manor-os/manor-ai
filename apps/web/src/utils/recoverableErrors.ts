export const ERROR_BOUNDARY_AUTO_RELOAD_KEY = "manor:eb-auto-reload";

const AUTO_RELOAD_DELAYS_MS = [0, 1_500, 4_000] as const;
const AUTO_RELOAD_RESET_AFTER_MS = 60_000;
const STATEFUL_EDITING_PATH_PREFIXES = ["/video-editor", "/editor"] as const;
let autoReloadScheduled = false;

/** The customer-trial frame (marker left by public/embed-isolate.js; no imports here). */
function inEmbeddedFrame(): boolean {
  return typeof window !== "undefined" && Boolean(window.__manorEmbed);
}

export enum ApplicationReloadMode {
  Refresh = "refresh",
  RecoverAssets = "recover_assets",
}

export function reloadApplication(mode = ApplicationReloadMode.Refresh): void {
  // The customer-trial frame cannot load the (unframeable) recovery page; its
  // entry is served uncached, so a plain reload picks up the new release.
  if (mode === ApplicationReloadMode.RecoverAssets && !inEmbeddedFrame()) {
    const next = `${window.location.pathname}${window.location.search || ""}${window.location.hash || ""}`;
    window.location.replace(`/app-recovery.html?next=${encodeURIComponent(next)}`);
    return;
  }
  window.location.reload();
}

interface RecoverableErrorAutoReloadState {
  version: string;
  attempts: number;
  lastAttemptAt: number;
}

function readRecoverableErrorAutoReloadState(now: number): RecoverableErrorAutoReloadState | null {
  const raw = sessionStorage.getItem(ERROR_BOUNDARY_AUTO_RELOAD_KEY);
  if (!raw) return null;

  try {
    const value = JSON.parse(raw) as Partial<RecoverableErrorAutoReloadState>;
    if (
      value &&
      typeof value === "object" &&
      typeof value.version === "string" &&
      Number.isInteger(value.attempts) &&
      Number(value.attempts) >= 0 &&
      Number.isFinite(value.lastAttemptAt)
    ) {
      return {
        version: value.version,
        attempts: Number(value.attempts),
        lastAttemptAt: Number(value.lastAttemptAt),
      };
    }
  } catch {
    // Values written by older builds contain only the version string.
  }

  return { version: raw, attempts: 1, lastAttemptAt: now };
}

function getRecoverableErrorAutoReloadAttemptCount(now: number): number {
  const state = readRecoverableErrorAutoReloadState(now);
  if (
    !state ||
    state.version !== __APP_VERSION__ ||
    now - state.lastAttemptAt >= AUTO_RELOAD_RESET_AFTER_MS
  ) {
    return 0;
  }

  return Math.min(state.attempts, AUTO_RELOAD_DELAYS_MS.length);
}

export function getErrorText(err: unknown): string {
  if (!err) return "";
  if (typeof err === "string") return err;
  if (err instanceof Error) return `${err.name || ""} ${err.message || ""}`;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

export function isStaleChunkError(err: unknown): boolean {
  const msg = getErrorText(err).toLowerCase();
  return (
    msg.includes("dynamically imported module") ||
    msg.includes("loading chunk") ||
    msg.includes("chunkloaderror") ||
    msg.includes("module script failed") ||
    msg.includes("importing a module")
  );
}

export function isExternalDomMutationError(err: unknown): boolean {
  const msg = getErrorText(err).toLowerCase();
  const nodeApiMismatch =
    msg.includes("failed to execute") &&
    msg.includes("node") &&
    (
      msg.includes("removechild") ||
      msg.includes("remoyechild") ||
      msg.includes("insertbefore")
    );
  const childMismatch =
    msg.includes("not a child of this node") ||
    msg.includes("node to be removed is not a child") ||
    msg.includes("node to be remoyed is not a child") ||
    msg.includes("child of this node");
  const notFoundDomException =
    typeof DOMException !== "undefined" &&
    err instanceof DOMException &&
    err.name === "NotFoundError";
  const genericDomNotFound =
    msg.includes("notfounderror") &&
    (
      msg.includes("object can not be found") ||
      msg.includes("object cannot be found") ||
      msg.includes("object could not be found")
    );

  return (nodeApiMismatch && childMismatch) || (notFoundDomException && childMismatch) || genericDomNotFound;
}

export function isRecoverableUiRuntimeError(err: unknown): boolean {
  return isStaleChunkError(err) || isExternalDomMutationError(err);
}

export function isStatefulEditingRoute(pathname?: string): boolean {
  const currentPath = pathname ?? (
    typeof window !== "undefined" ? window.location.pathname : ""
  );
  return STATEFUL_EDITING_PATH_PREFIXES.some(
    (prefix) => currentPath === prefix || currentPath.startsWith(`${prefix}/`),
  );
}

export function shouldAutoReloadForRecoverableError(): boolean {
  // The frame's sessionStorage is memory: the attempt bound would reset on
  // every reload, so it never reloads by itself.
  if (inEmbeddedFrame()) return false;
  try {
    return (
      !isStatefulEditingRoute() &&
      getRecoverableErrorAutoReloadAttemptCount(Date.now()) < AUTO_RELOAD_DELAYS_MS.length
    );
  } catch {
    return false;
  }
}

export function getRecoverableErrorAutoReloadDelayMs(): number {
  try {
    const attempts = getRecoverableErrorAutoReloadAttemptCount(Date.now());
    return AUTO_RELOAD_DELAYS_MS[Math.min(attempts, AUTO_RELOAD_DELAYS_MS.length - 1)];
  } catch {
    return AUTO_RELOAD_DELAYS_MS[0];
  }
}

export function markRecoverableErrorAutoReloadAttempt(): boolean {
  try {
    const now = Date.now();
    const attempts = getRecoverableErrorAutoReloadAttemptCount(now);
    const state: RecoverableErrorAutoReloadState = {
      version: __APP_VERSION__,
      attempts: Math.min(attempts + 1, AUTO_RELOAD_DELAYS_MS.length),
      lastAttemptAt: now,
    };
    sessionStorage.setItem(ERROR_BOUNDARY_AUTO_RELOAD_KEY, JSON.stringify(state));
    return true;
  } catch {
    // ignore storage issues in private / restricted environments
    return false;
  }
}

export function scheduleRecoverableErrorAutoReload(error?: unknown): number | null {
  if (autoReloadScheduled || !shouldAutoReloadForRecoverableError()) return null;

  const reloadDelayMs = getRecoverableErrorAutoReloadDelayMs();
  if (!markRecoverableErrorAutoReloadAttempt()) return null;
  autoReloadScheduled = true;
  window.setTimeout(() => reloadApplication(
    isStaleChunkError(error) ? ApplicationReloadMode.RecoverAssets : ApplicationReloadMode.Refresh,
  ), reloadDelayMs);
  return reloadDelayMs;
}

export function isRecoverableErrorAutoReloadScheduled(): boolean {
  return autoReloadScheduled;
}

export function clearRecoverableErrorAutoReloadAttempt(): void {
  try {
    sessionStorage.removeItem(ERROR_BOUNDARY_AUTO_RELOAD_KEY);
  } catch {
    // ignore storage issues in private / restricted environments
  }
}
