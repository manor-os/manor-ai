import { Component, ReactNode } from "react";
import {
  clearRecoverableErrorAutoReloadAttempt,
  isExternalDomMutationError,
  isRecoverableErrorAutoReloadScheduled,
  isRecoverableUiRuntimeError,
  isStaleChunkError,
  scheduleRecoverableErrorAutoReload,
  shouldAutoReloadForRecoverableError,
} from "../utils/recoverableErrors";
import { t } from "../lib/i18n";
import { captureClientError } from "../lib/clientErrors";


interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  autoReloadUnavailable: boolean;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null, autoReloadUnavailable: false };

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error, autoReloadUnavailable: false };
  }

  componentDidCatch(error: Error, errorInfo: { componentStack: string }) {
    captureClientError(error, {
      handled: false,
      mechanism: "react.error_boundary",
      boundary: "ErrorBoundary",
      componentStack: errorInfo.componentStack,
    });

    // Keep detailed diagnostics in the console for debugging / monitoring,
    // but avoid surfacing raw runtime messages to end users in production.
    // eslint-disable-next-line no-console
    console.error("Unhandled UI error:", error, errorInfo);

    if (isRecoverableUiRuntimeError(error)) {
      const reloadDelayMs = scheduleRecoverableErrorAutoReload();
      if (reloadDelayMs === null) {
        if (!isRecoverableErrorAutoReloadScheduled()) {
          this.setState({ autoReloadUnavailable: true });
        }
        return;
      }
      // eslint-disable-next-line no-console
      console.warn(
        `[manor] Recoverable UI runtime error detected; reloading in ${reloadDelayMs}ms:`,
        error,
      );
    }
  }

  render() {
    if (this.state.hasError) {
      const isDev = import.meta.env.DEV;
      const stale = isStaleChunkError(this.state.error);
      const domMutation = isExternalDomMutationError(this.state.error);
      const recoverable = stale || domMutation;
      const autoReloadAvailable =
        recoverable &&
        !this.state.autoReloadUnavailable &&
        (isRecoverableErrorAutoReloadScheduled() || shouldAutoReloadForRecoverableError());
      return (
        <div className="flex items-center justify-center h-screen">
          <div className="text-center max-w-md px-6">
            <h1 className="text-2xl font-bold text-stone-900 mb-2">
              {stale
                ? t("component.error_boundary.app_was_updated")
                : autoReloadAvailable
                  ? t("component.error_boundary.refreshing_the_app")
                  : recoverable
                    ? t("component.error_boundary.this_page_needs_a_refresh")
                    : t("status.error")}
            </h1>
            <p className="text-stone-600 mb-4">
              {isDev
                ? (this.state.error?.message || t("component.error_boundary.unexpected_application_error"))
                : autoReloadAvailable
                  ? stale
                    ? t("component.route_error_boundary.one_moment_fetching_the_freshly_deployed_assets")
                    : t("component.error_boundary.this_page_hit_a_browser_dom_sync_issue_and_is_recoveri")
                  : stale
                    ? t("component.error_boundary.this_page_needs_a_refresh")
                  : recoverable
                    ? t("component.error_boundary.this_page_hit_a_browser_dom_sync_issue_and_needs_a_ref")
                    : t("component.error_boundary.this_page_hit_an_unexpected_error_please_refresh_and_t")}
            </p>
            <div className="flex justify-center gap-2">
              <button
                onClick={() => {
                  clearRecoverableErrorAutoReloadAttempt();
                  window.location.reload();
                }}
                className="px-4 py-2 btn-manor"
              >
                {t("component.error_boundary.reload")}</button>
              {!stale && (
                <button
                  onClick={() => {
                    this.setState({ hasError: false, error: null, autoReloadUnavailable: false });
                    window.location.href = "/";
                  }}
                  className="px-4 py-2 btn-manor-outline"
                >
                  {t("component.error_boundary.go_home")}</button>
              )}
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
