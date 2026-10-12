import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { integrationSetupHref } from "../../lib/integrationSetupLinks";
import { workspaceConnectionRefetchInterval } from "../../lib/workspaceLiveRefresh";
import { useAuthStore } from "../../stores/auth";
import { useDisplayPreferences } from "../../stores/displayPreferences";
import { IconChevronRight, IconRefresh, IconWarning } from "../icons";
import Button from "../ui/Button";
import AnchoredPopover from "../ui/AnchoredPopover";
import InlineIntegrationLink from "../InlineIntegrationLink";
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
  const simpleMode = useDisplayPreferences((state) => state.interfaceMode === "simple");
  const setupSuffix = simpleMode ? "&setup=1" : "";
  const status = useQuery({
    queryKey: ["workspace-connection-status", user?.entity_id, user?.id, workspaceId],
    queryFn: ({ signal }) => api.workspaces.connectionStatus(workspaceId, signal),
    enabled: Boolean(user && workspaceId),
    refetchInterval: (query) => {
      const current = query.state.data;
      return workspaceConnectionRefetchInterval({
        hasData: Boolean(current),
        isError: query.state.status === "error",
        hasRequiredIssue: Boolean(current?.requirements.some(
          (item) => item.required && !item.ready,
        )),
      });
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: "always",
    staleTime: 0,
    retry: 1,
  });
  const issues = status.data?.requirements.filter((item) => item.required && !item.ready) ?? [];
  if (!user || (!status.isPending && !status.isError && issues.length === 0)) return null;
  const noticeState = status.isError ? "error" : status.isPending ? "checking" : "warning";
  const noticeTitle = status.isError
    ? t("workspace.connections.check_failed")
    : status.isPending
      ? t("workspace.connections.checking")
      : t("workspace.connections.required_issues", { count: issues.length });
  const statusCopy = (
    <span className="workspace-connection-notice-status">
      <span className="workspace-connection-notice-indicator" data-state={noticeState} aria-hidden="true" />
      <strong role="status" aria-live="polite">{noticeTitle}</strong>
    </span>
  );
  const issueList = issues.length > 0 ? (
    <ul className="workspace-connection-notice-list">
      {issues.map((item) => (
        <li key={item.key}>
          <div>
            <strong>{item.label || item.provider}</strong>
            <p>{item.reason}</p>
            {item.service_keys.length > 0 && <small>{item.service_keys.join(" · ")}</small>}
          </div>
          {(item.kind !== "channel" || item.setup_kind === "platform_access") && item.setup_kind !== "mcp_binding" ? (
            <InlineIntegrationLink provider={item.provider} label={t("workspace.connections.fix")} />
          ) : <Link to={item.kind === "channel"
            ? `/workspaces/${workspaceId}?tab=channels${setupSuffix}`
            : item.setup_kind === "mcp_binding"
              ? `/workspaces/${workspaceId}?tab=capabilities${setupSuffix}`
              : "/integrations"}>
              {t("workspace.connections.fix")}
            </Link>}
        </li>
      ))}
    </ul>
  ) : null;

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
            {!status.isPending && !status.isError && (
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
          <ul className="workspace-connection-popover-list">
            {issues.map((item) => (
              <li key={item.key}>
                <div className="workspace-connection-popover-copy">
                  <strong>{item.label || item.provider}</strong>
                  <span>{t("page.workspace_detail.needs_setup_2")}</span>
                  <span className="sr-only">{item.reason}</span>
                </div>
                <Link
                  className="workspace-connection-popover-setup"
                  to={item.kind === "channel" && item.setup_kind !== "platform_access"
                    ? `/workspaces/${workspaceId}?tab=channels${setupSuffix}`
                    : item.setup_kind === "mcp_binding"
                      ? `/workspaces/${workspaceId}?tab=capabilities${setupSuffix}`
                      : integrationSetupHref(item.provider)}
                >
                  {t("workspace.connections.fix")}
                  <IconChevronRight size={13} aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ul>
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
