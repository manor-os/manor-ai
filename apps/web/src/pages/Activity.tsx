import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import PageHeader from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import StatusBadge from "../components/ui/StatusBadge";
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

const EVENT_DOT_COLORS: Record<string, string> = {
  task: "#436b65",
  document: "#6f4ba8",
  user: "#4869ac",
  goal: "#b27c34",
  agent: "#437f6b",
  chat: "#4a7d96",
  system: "#a8a29e",
};

const EVENT_BADGE_TYPE: Record<string, "teal" | "purple" | "info" | "warning" | "inactive"> = {
  task: "teal",
  document: "purple",
  user: "info",
  goal: "warning",
  agent: "teal",
  chat: "info",
  system: "inactive",
};

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
    refetchInterval: 120_000,
  });

  const items: any[] = data ?? [];

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: "clamp(16px, 5vw, 24px)", maxWidth: 720, margin: "0 auto" }}>
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
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
          title={t("page.activity.no_activity_yet")}
          description={t("page.activity.empty")}
        />
      )}

      {/* Timeline */}
      {!isLoading && items.length > 0 && (
        <div style={{ position: "relative", paddingLeft: 24 }}>
          {/* Vertical line */}
          <div
            style={{
              position: "absolute",
              left: 7,
              top: 8,
              bottom: 8,
              width: 2,
              background: "rgba(231,229,228,0.7)",
              borderRadius: 2,
            }}
          />

          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {items.map((item: any, idx: number) => {
              const eventKey = item.event_type || item.type || "";
              const dotColor = EVENT_DOT_COLORS[eventKey] || "#a8a29e";
              const badgeType = EVENT_BADGE_TYPE[eventKey] || "inactive";

              // Extract bold resource name from description
              const desc = item.description || item.message || item.title || t("page.activity.event_fallback");

              return (
                <div key={item.id || idx} style={{ position: "relative", display: "flex", alignItems: "flex-start", gap: 14 }}>
                  {/* Timeline dot */}
                  <div
                    style={{
                      position: "absolute",
                      left: -24,
                      top: 18,
                      width: 8,
                      height: 8,
                      borderRadius: "50%",
                      background: dotColor,
                      border: "2px solid #fff",
                      boxShadow: `0 0 0 1px ${dotColor}33`,
                      zIndex: 1,
                    }}
                  />

                  {/* Content card */}
                  <div className="glass-card-sm" style={{ flex: 1, minWidth: 0 }}>
                    <p style={{ fontSize: 13, fontWeight: 600, color: "#292524", margin: 0 }}>
                      {desc}
                    </p>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}>
                      <span style={{ fontSize: 12, color: "#a8a29e" }}>
                        {item.created_at || item.timestamp
                          ? relativeTime(item.created_at || item.timestamp)
                          : ""}
                      </span>
                      {item.user_name && (
                        <span style={{ fontSize: 12, color: "#78716c" }}>{t("page.activity.by")} {item.user_name}</span>
                      )}
                      {eventKey && (
                        <StatusBadge type={badgeType}>
                          {eventKey}
                        </StatusBadge>
                      )}
                    </div>
                  </div>

                  {/* Link arrow */}
                  {item.link && (
                    <a
                      href={item.link}
                      style={{
                        color: "#a8a29e",
                        flexShrink: 0,
                        marginTop: 14,
                        transition: "color 0.2s",
                      }}
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                      </svg>
                    </a>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
