import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { useAuthStore } from "../../stores/auth";
import Button from "../ui/Button";
import InlineIntegrationLink from "../InlineIntegrationLink";
import "./WorkspaceConnectionNotice.css";

/** One live setup notice for both creation paths, in details and main chat. */
export default function WorkspaceConnectionNotice({ workspaceId }: { workspaceId: string }) {
  const user = useAuthStore((state) => state.user);
  const status = useQuery({
    queryKey: ["workspace-connection-status", user?.entity_id, user?.id, workspaceId],
    queryFn: () => api.workspaces.connectionStatus(workspaceId),
    enabled: Boolean(user && workspaceId),
    refetchInterval: 30_000,
    refetchOnWindowFocus: "always",
    staleTime: 0,
    retry: 1,
  });
  const issues = status.data?.requirements.filter((item) => item.required && !item.ready) ?? [];
  if (!user || (!status.isPending && !status.isError && issues.length === 0)) return null;

  return (
    <section className="workspace-connection-notice" aria-label={t("workspace.connections.title")}>
      <div className="workspace-connection-notice-heading">
        <span role="status" aria-live="polite">
          {status.isError
            ? t("workspace.connections.check_failed")
            : status.isPending
              ? t("workspace.connections.checking")
              : t("workspace.connections.required_issues", { count: issues.length })}
        </span>
        <Button variant="ghost" size="sm" loading={status.isFetching} onClick={() => void status.refetch()}>
          {t("page.workspace_detail.check_again")}
        </Button>
      </div>
      {issues.length > 0 && (
        <details>
          <summary>{t("workspace.connections.review")}</summary>
          <ul className="workspace-connection-notice-list">
            {issues.map((item) => (
              <li key={item.key}>
                <div>
                  <strong>{item.label || item.provider}</strong>
                  <p>{item.reason}</p>
                  {item.service_keys.length > 0 && <small>{item.service_keys.join(" · ")}</small>}
                </div>
                {item.kind !== "channel" && item.setup_kind !== "mcp_binding" ? (
                  <InlineIntegrationLink provider={item.provider} label={item.label || item.provider} />
                ) : <Link to={item.kind === "channel"
                  ? `/workspaces/${workspaceId}?tab=channels`
                  : item.setup_kind === "mcp_binding"
                    ? `/workspaces/${workspaceId}?tab=capabilities`
                    : "/integrations"}>
                  {t("workspace.connections.fix")}
                </Link>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
