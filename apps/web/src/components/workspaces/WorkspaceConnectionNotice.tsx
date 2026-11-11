import { useQuery } from "@tanstack/react-query";
import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { useAfterStartup } from "../../lib/useAfterStartup";
import { usePageVisible } from "../../lib/usePageVisible";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { workspaceConnectionRefetchInterval } from "../../lib/workspaceLiveRefresh";
import { useAuthStore } from "../../stores/auth";
import { IconChevronRight, IconRefresh, IconWarning } from "../icons";
import Button from "../ui/Button";
import IntegrationConnectRow from "../integrations/IntegrationConnectRow";
import AnchoredPopover from "../ui/AnchoredPopover";
import "./WorkspaceConnectionNotice.css";

/** One live setup notice for both creation paths, in details and main chat. */
export default function WorkspaceConnectionNotice({
  workspaceId,
  variant = "banner",
}: {
  workspaceId: string;
  variant?: "banner" | "icon";
}) {
  const user = useAuthStore((state) => state.user);
  const visible = usePageVisible();
  const startupSettled = useAfterStartup();
  const status = useQuery({
    queryKey: ["workspace-connection-status", user?.entity_id, user?.id, workspaceId],
    queryFn: ({ signal }) => api.workspaces.connectionStatus(workspaceId, signal),
    enabled: visible && startupSettled && Boolean(user && workspaceId),
    refetchInterval: (query) => {
      const current = query.state.data;
      return displayReadInterval(query.state, workspaceConnectionRefetchInterval({
        hasData: Boolean(current),
        isError: query.state.status === "error",
        hasRequiredIssue: Boolean(current?.requirements.some(
          (item) => item.required && !item.ready,
        )),
      }));
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: "always",
    staleTime: 0,
    retry: false,
  });
  const issues = status.data?.requirements.filter((item) => item.required && !item.ready) ?? [];
  if (!user || status.isPending || (!status.isError && issues.length === 0)) return null;
  const noticeState = status.isError ? "error" : "warning";
  const noticeTitle = status.isError
    ? t("workspace.connections.check_failed")
    : t("workspace.connections.required_issues", { count: issues.length });
  const statusCopy = (
    <span className="workspace-connection-notice-status">
      <span className="workspace-connection-notice-indicator" data-state={noticeState} aria-hidden="true" />
      <strong role="status" aria-live="polite">{noticeTitle}</strong>
    </span>
  );
  // The same connection row as everywhere: connect in place, then this notice checks again.
  const issueRows = issues.map((item) => <IntegrationConnectRow key={item.key} requirement={item} workspaceId={workspaceId}
    hint={item.reason || undefined} onConnected={() => void status.refetch()} />);
  const issueList = issues.length > 0 ? <ul className="integration-connect-list workspace-connection-notice-list">{issueRows}</ul> : null;

  if (variant === "icon") {
    return (
      <AnchoredPopover
        align="right"
        width={300}
        ariaLabel={t("workspace.connections.title")}
        panelClassName={`workspace-connection-popover workspace-connection-popover--${noticeState}`}
        trigger={(
          <button
            type="button"
            className="btn-manor-ghost workspace-connection-icon-trigger"
            data-state={noticeState}
            aria-label={noticeTitle}
            title={noticeTitle}
          >
            <IconWarning size={17} aria-hidden="true" />
          </button>
        )}
      >
        <div className="workspace-connection-popover-header">
          <div className="workspace-connection-popover-heading">
            <strong>{t("workspace.connections.title")}</strong>
            {!status.isError && (
              <span className="workspace-connection-popover-count" aria-hidden="true">
                {issues.length}
              </span>
            )}
          </div>
          <button
            type="button"
            className="btn-manor-ghost workspace-connection-popover-refresh"
            disabled={status.isFetching}
            aria-label={t("page.workspace_detail.check_again")}
            title={t("page.workspace_detail.check_again")}
            aria-busy={status.isFetching}
            onClick={() => void status.refetch()}
          >
            <IconRefresh size={15} aria-hidden="true" />
          </button>
        </div>
        <span className="sr-only" role="status" aria-live="polite">{noticeTitle}</span>
        {issues.length > 0 ? (
          <ul className="integration-connect-list workspace-connection-popover-list">{issueRows}</ul>
        ) : (
          <div className="workspace-connection-popover-state" data-state={noticeState}>
            <span aria-hidden="true" />
            {noticeTitle}
          </div>
        )}
      </AnchoredPopover>
    );
  }

  return (
    <section className="workspace-connection-notice" aria-label={t("workspace.connections.title")}>
      <div className="workspace-connection-notice-layout">
        {issues.length > 0 ? (
          <details className="workspace-connection-notice-details">
            <summary>
              {statusCopy}
              <span className="workspace-connection-notice-review">
                {t("workspace.connections.review")}
                <IconChevronRight size={14} aria-hidden="true" />
              </span>
            </summary>
            {issueList}
          </details>
        ) : (
          <div className="workspace-connection-notice-static">{statusCopy}</div>
        )}
        <Button
          className="workspace-connection-notice-refresh"
          variant="ghost"
          size="sm"
          loading={status.isFetching}
          onClick={() => void status.refetch()}
        >
          {t("page.workspace_detail.check_again")}
        </Button>
      </div>
    </section>
  );
}
