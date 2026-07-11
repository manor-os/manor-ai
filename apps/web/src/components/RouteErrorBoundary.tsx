/**
 * Route-level error boundary for React Router v6 data routers.
 *
 * The default UI (the "💿 Hey developer 👋" page) is not what we want
 * users to see — and importantly it intercepts errors *before*
 * `<ErrorBoundary>` in main.tsx ever fires, so the auto-reload-on-
 * recoverable-error logic over there is bypassed.
 *
 * Wired in router.tsx as `errorElement` on the root and any leaf route
 * that loads a lazy chunk. Detects recoverable runtime errors, auto-reloads
 * once per deployed version, and otherwise renders a friendly retry page.
 */
import { useRouteError } from "react-router-dom";
import { useEffect } from "react";
import {
  clearRecoverableErrorAutoReloadAttempt,
  getErrorText,
  isRecoverableUiRuntimeError,
  isStaleChunkError,
  markRecoverableErrorAutoReloadAttempt,
  shouldAutoReloadForRecoverableError,
} from "../utils/recoverableErrors";
import { t } from "../lib/i18n";
import { captureClientError } from "../lib/clientErrors";


export default function RouteErrorBoundary() {
  const error = useRouteError();
  const stale = isStaleChunkError(error);
  const recoverable = isRecoverableUiRuntimeError(error);
  const autoReloadAvailable = recoverable && shouldAutoReloadForRecoverableError();

  useEffect(() => {
    captureClientError(error, {
      handled: false,
      mechanism: "react_router.error_boundary",
      boundary: "RouteErrorBoundary",
      tags: { recoverable },
    });
  }, [error, recoverable]);

  useEffect(() => {
    if (!autoReloadAvailable) return;
    markRecoverableErrorAutoReloadAttempt();
    // eslint-disable-next-line no-console
    console.warn("[manor] Recoverable route error detected; reloading once:", error);
    window.location.reload();
  }, [autoReloadAvailable, error]);

  if (autoReloadAvailable) {
    // While the reload is in flight, show a quiet placeholder rather
    // than the alarming default error UI.
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="text-center max-w-md">
          <h1 className="text-xl font-semibold text-stone-900 mb-2">
            {stale ? t("component.route_error_boundary.updating_to_the_latest_version") : t("component.route_error_boundary.refreshing_the_app")}
          </h1>
          <p className="text-stone-500 text-sm">
            {stale
              ? t("component.route_error_boundary.one_moment_fetching_the_freshly_deployed_assets")
              : t("component.route_error_boundary.one_moment_recovering_from_a_browser_dom_sync_issue")}
          </p>
        </div>
      </div>
    );
  }

  const message = getErrorText(error) || "Unknown error";

  return (
    <div className="flex items-center justify-center h-screen">
      <div className="text-center max-w-md">
        <h1 className="text-2xl font-bold text-stone-900 mb-2">
          {t("status.error")}</h1>
        <p className="text-stone-600 mb-4 break-words">{message}</p>
        <div className="flex justify-center gap-2">
          <button
            onClick={() => {
              clearRecoverableErrorAutoReloadAttempt();
              window.location.reload();
            }}
            className="px-4 py-2 btn-manor"
          >
            {t("component.error_boundary.reload")}</button>
          <button
            onClick={() => (window.location.href = "/")}
            className="px-4 py-2 btn-manor-outline"
          >
            {t("component.error_boundary.go_home")}</button>
        </div>
      </div>
    </div>
  );
}
