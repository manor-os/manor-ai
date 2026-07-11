import { useEffect } from "react";
import { t } from "./lib/i18n";
import { useToastStore } from "./stores/toast";

const VERSION_POLL_INTERVAL_MS = 30_000;
const VERSION_CHECK_TIMEOUT_MS = 10_000;
const VERSION_DISMISS_KEY = "manor-version-refresh-dismissed";
const CHUNK_DISMISS_KEY = "manor-chunk-refresh-dismissed";
const CHUNK_ERROR_TOAST_ID = "app-chunk-load-failed";
const CHUNK_AUTO_RELOAD_KEY = "manor-chunk-auto-reload-attempted";
const CHUNK_AUTO_RELOAD_TTL_MS = 30_000;
const STATEFUL_EDITING_PATHS = ["/video-editor", "/doc-editor"];

function isChunkLoadFailureMessage(message: string) {
  const normalized = message.toLowerCase();
  return (
    normalized.includes("failed to fetch dynamically imported module") ||
    normalized.includes("importing a module script failed") ||
    normalized.includes("loading chunk") ||
    normalized.includes("chunkloaderror") ||
    normalized.includes("dynamically imported module")
  );
}

function getErrorMessage(value: unknown): string {
  if (typeof value === "string") return value;
  if (value instanceof Error) return value.message;
  if (value && typeof value === "object" && "message" in value) {
    const message = (value as { message?: unknown }).message;
    return typeof message === "string" ? message : "";
  }
  return "";
}

async function fetchDeployedVersion(): Promise<string | null> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), VERSION_CHECK_TIMEOUT_MS);

  try {
    const res = await fetch(`/version.json?ts=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
      headers: {
        "cache-control": "no-cache",
      },
    });

    if (!res.ok) return null;
    const data = await res.json() as { version?: unknown };
    return typeof data.version === "string" ? data.version : null;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeout);
  }
}

function setSessionValue(key: string, value: string) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // ignore storage issues in private / restricted environments
  }
}

function getSessionValue(key: string) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function clearSessionValue(key: string) {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // ignore storage issues
  }
}

function markVersionDismissed(version: string) {
  setSessionValue(VERSION_DISMISS_KEY, version);
}

function wasVersionDismissed(version: string) {
  return getSessionValue(VERSION_DISMISS_KEY) === version;
}

function markChunkDismissed(buildVersion: string) {
  setSessionValue(CHUNK_DISMISS_KEY, buildVersion);
}

function wasChunkDismissed(buildVersion: string) {
  return getSessionValue(CHUNK_DISMISS_KEY) === buildVersion;
}

function shouldAutoReloadForChunkFailure() {
  const attemptedAtRaw = getSessionValue(CHUNK_AUTO_RELOAD_KEY);
  if (!attemptedAtRaw) return true;

  const attemptedAt = Number(attemptedAtRaw);
  if (!Number.isFinite(attemptedAt)) return true;

  return Date.now() - attemptedAt > CHUNK_AUTO_RELOAD_TTL_MS;
}

function markChunkAutoReloadAttempt() {
  setSessionValue(CHUNK_AUTO_RELOAD_KEY, String(Date.now()));
}

function clearChunkAutoReloadAttempt() {
  clearSessionValue(CHUNK_AUTO_RELOAD_KEY);
}

function isStatefulEditingRoute() {
  return STATEFUL_EDITING_PATHS.some((path) => window.location.pathname.startsWith(path));
}

function reloadToLatestVersion() {
  clearSessionValue(VERSION_DISMISS_KEY);
  clearSessionValue(CHUNK_DISMISS_KEY);
  markChunkAutoReloadAttempt();
  window.location.reload();
}

export default function VersionRefreshManager() {
  const addToast = useToastStore((s) => s.addToast);
  const removeToast = useToastStore((s) => s.removeToast);

  useEffect(() => {
    let active = true;
    let currentVersionToastId: string | null = null;

    if (!wasChunkDismissed(__APP_VERSION__)) {
      clearChunkAutoReloadAttempt();
    }

    const showUpdateToast = (nextVersion: string) => {
      if (currentVersionToastId || wasVersionDismissed(nextVersion)) return;

      const id = `app-update-${nextVersion}`;
      currentVersionToastId = id;
      addToast({
        id,
        type: "info",
        title: t("component.version_refresh.new_version_title"),
        message: t("component.version_refresh.new_version_message"),
        duration: 0,
        actionLabel: t("component.version_refresh.refresh_now"),
        onAction: reloadToLatestVersion,
        onDismiss: () => {
          markVersionDismissed(nextVersion);
          currentVersionToastId = null;
        },
      });
    };

    const showChunkFailureToast = () => {
      if (wasChunkDismissed(__APP_VERSION__)) return;
      addToast({
        id: CHUNK_ERROR_TOAST_ID,
        type: "warning",
        title: t("component.version_refresh.page_needs_refresh_title"),
        message: t("component.version_refresh.page_needs_refresh_message"),
        duration: 0,
        actionLabel: t("component.version_refresh.refresh_now"),
        onAction: reloadToLatestVersion,
        onDismiss: () => {
          markChunkDismissed(__APP_VERSION__);
        },
      });
    };

    const handleChunkFailure = () => {
      if (!isStatefulEditingRoute() && shouldAutoReloadForChunkFailure()) {
        markChunkAutoReloadAttempt();
        window.location.reload();
        return;
      }
      showChunkFailureToast();
    };

    const clearUpdateToast = () => {
      if (!currentVersionToastId) return;
      removeToast(currentVersionToastId);
      currentVersionToastId = null;
    };

    const checkVersion = async () => {
      const deployedVersion = await fetchDeployedVersion();
      if (!active || !deployedVersion || deployedVersion === __APP_VERSION__) {
        if (deployedVersion === __APP_VERSION__) clearUpdateToast();
        return;
      }
      showUpdateToast(deployedVersion);
    };

    const handleWindowError = (event: ErrorEvent) => {
      if (isChunkLoadFailureMessage(getErrorMessage(event.error) || event.message || "")) {
        handleChunkFailure();
      }
    };

    const handleUnhandledRejection = (event: PromiseRejectionEvent) => {
      if (isChunkLoadFailureMessage(getErrorMessage(event.reason))) {
        handleChunkFailure();
      }
    };

    void checkVersion();
    const interval = window.setInterval(() => {
      void checkVersion();
    }, VERSION_POLL_INTERVAL_MS);

    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void checkVersion();
      }
    };

    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("error", handleWindowError);
    window.addEventListener("unhandledrejection", handleUnhandledRejection);

    return () => {
      active = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("error", handleWindowError);
      window.removeEventListener("unhandledrejection", handleUnhandledRejection);
      clearUpdateToast();
    };
  }, [addToast, removeToast]);

  return null;
}
