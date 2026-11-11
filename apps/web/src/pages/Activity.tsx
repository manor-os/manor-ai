import { displayReadInterval } from "../lib/readRefreshPolicy";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import PageHeader from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import StatusBadge from "../components/ui/StatusBadge";
import { localizeEventText } from "../lib/eventCopy";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import { t } from "../lib/i18n";

const EVENT_TABS = [
  { key: "", label: t("page.workspaces.filter_all") }, // TODO: localize tab labels in a later pass
  { key: "task", label: t("nav.tasks") },
  { key: "document", label: t("page.workspace_detail.documents") },
  { key: "user", label: t("nav.users") },
  { key: "goal", label: t("nav.goals") },
];

export default function Activity() {
  const [eventType, setEventType] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["activity-feed", eventType],
    queryFn: async (): Promise<any[]> => {
      if (eventType) {
        const res = await api.activity.events({ event_type: eventType, limit: 50 });
        return res.items;
      }
      return api.activity.feed(50);
    },
    refetchInterval: (query) => displayReadInterval(query.state, 120_000),
  });

  const items: any[] = data ?? [];

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: 0 }}>
      <PageHeader
        title={t("nav.activity")}
        subtitle={t("page.activity.subtitle")}
        actions={
          <StatusBadge type="teal" dot pulse>{t("page.activity.auto_refresh")}</StatusBadge>
        }
      />

      <div style={{ marginBottom: 20, maxWidth: "100%", overflowX: "auto", WebkitOverflowScrolling: "touch" }}>
        <TabSwitcher
          tabs={EVENT_TABS}
          value={eventType}
          onChange={setEventType}
          className="w-full sm:w-auto"
        />
      </div>

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}

      {/* Empty */}
      {!isLoading && items.length === 0 && (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d1d1d6" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
          title={t("page.activity.no_activity_yet")}
          description={t("page.activity.empty")}
        />
      )}

      {/* Timeline */}
      {!isLoading && items.length > 0 && (
        <div className="inset-list">
          {items.map((item: any, idx: number) => {
            const desc = localizeEventText(item.description || item.message || item.title || t("page.activity.event_fallback"));
            const at = item.created_at || item.timestamp;
            const content = (
              <div className="inset-row-copy">
                <div className="inset-row-title"><span>{desc}</span></div>
                {item.user_name && <div className="inset-row-desc">{t("page.activity.by")} {item.user_name}</div>}
              </div>
            );
            return item.link ? (
              <a key={item.id || idx} href={item.link} className="inset-row">
                {content}
                <div className="inset-row-meta"><span>{at ? relativeTime(at) : ""}</span></div>
              </a>
            ) : (
              <div key={item.id || idx} className="inset-row inset-row--static">
                {content}
                <div className="inset-row-meta"><span>{at ? relativeTime(at) : ""}</span></div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
