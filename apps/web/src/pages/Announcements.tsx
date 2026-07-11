import { useState, useEffect, useCallback } from "react";
import PageHeader from "../components/ui/PageHeader";
import StatusBadge from "../components/ui/StatusBadge";
import EmptyState from "../components/ui/EmptyState";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import type { Notification } from "../lib/types";
import { formatDateLong as formatDate } from "../lib/format";

function formatAnnouncementType(type?: string | null): string {
  const normalized = String(type || "").trim();
  if (!normalized) return t("page.announcements.announcement");
  const translated = t(`page.announcements.type.${normalized}`);
  if (translated !== `page.announcements.type.${normalized}`) return translated;
  return normalized
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

/* ------------------------------------------------------------------ */
/*  Announcement Item                                                  */
/* ------------------------------------------------------------------ */
function AnnouncementItem({
  item,
  onMarkRead,
}: {
  item: Notification;
  onMarkRead: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const content = item.content || "";
  const isLong = content.length > 180;
  const preview = isLong ? content.slice(0, 180) + "..." : content;
  const isUnread = !item.read_at;

  return (
    <div
      style={{
        background: isUnread ? "rgba(242,246,245,0.8)" : "rgba(255,255,255,0.65)",
        border: isUnread
          ? "1px solid rgba(79,125,117,0.2)"
          : "1px solid rgba(231,229,228,0.6)",
        borderRadius: 20,
        padding: "20px 24px",
        borderLeft: isUnread ? "4px solid #5f928a" : undefined,
        transition: "all 0.25s",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "start",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <div style={{ minWidth: 0, flex: 1 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 6,
            }}
          >
            <h3
              style={{
                fontSize: 16,
                fontWeight: 700,
                color: "#1c1917",
                margin: 0,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {item.title || t("page.announcements.announcement")}
            </h3>
            {isUnread && <StatusBadge type="teal">{t("page.announcements.new")}</StatusBadge>}
          </div>
          <div
            style={{
              fontSize: 13,
              color: "#57534e",
              whiteSpace: "pre-line",
              lineHeight: 1.6,
              overflow: "hidden",
              maxHeight: expanded ? "none" : isLong ? "4.8em" : "none",
              transition: "max-height 0.3s ease",
            }}
          >
            {expanded ? content : preview}
          </div>
          {isLong && (
            <button
              onClick={() => setExpanded(!expanded)}
              style={{
                marginTop: 8,
                fontSize: 12,
                fontWeight: 600,
                color: "#436b65",
                background: "transparent",
                border: "none",
                cursor: "pointer",
                padding: 0,
                transition: "color 0.2s",
              }}
            >
              {expanded ? t("page.announcements.show_less") : t("page.announcements.read_more")}
            </button>
          )}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              marginTop: 12,
            }}
          >
            <span style={{ fontSize: 12, fontWeight: 600, color: "#78716c" }}>
              {formatAnnouncementType(item.type)}
            </span>
            <span style={{ fontSize: 12, color: "#a8a29e" }}>
              {item.created_at ? formatDate(item.created_at) : ""}
            </span>
            {isUnread && (
              <button
                onClick={() => onMarkRead(item.id)}
                style={{
                  marginLeft: "auto",
                  fontSize: 12,
                  fontWeight: 600,
                  color: "#436b65",
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  padding: 0,
                }}
              >
                {t("page.announcements.mark_as_read")}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Page                                                          */
/* ------------------------------------------------------------------ */
export default function Announcements() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchNotifications = useCallback(async () => {
    try {
      setError(null);
      const data = await api.notifications.list();
      setNotifications(data.items);
    } catch (err: any) {
      setError(err.message || t("page.announcements.failed_load"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchNotifications();
  }, [fetchNotifications]);

  const handleMarkRead = async (id: string) => {
    try {
      await api.notifications.markRead(id);
      setNotifications((prev) =>
        prev.map((n) =>
          n.id === id ? { ...n, read_at: new Date().toISOString() } : n,
        ),
      );
    } catch {
      // toast already shown by api layer
    }
  };

  const handleMarkAllRead = async () => {
    try {
      await api.notifications.markAllRead();
      setNotifications((prev) =>
        prev.map((n) => ({ ...n, read_at: n.read_at || new Date().toISOString() })),
      );
    } catch {
      // toast already shown by api layer
    }
  };

  const unreadCount = notifications.filter((n) => !n.read_at).length;

  // Sort: unread first, then by date descending
  const sorted = [...notifications].sort((a, b) => {
    const aUnread = !a.read_at ? 1 : 0;
    const bUnread = !b.read_at ? 1 : 0;
    if (aUnread !== bUnread) return bUnread - aUnread;
    return (
      new Date(b.created_at || 0).getTime() -
      new Date(a.created_at || 0).getTime()
    );
  });

  return (
    <>
      <PageHeader
        title={t("nav.announcements")}
        subtitle={t("page.announcements.subtitle")}
        actions={
          unreadCount > 0 ? (
            <button onClick={handleMarkAllRead} className="btn-manor-outline">
              {t("page.announcements.mark_all_as_read")} ({unreadCount})
            </button>
          ) : undefined
        }
      />

      {loading ? (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            padding: "48px 0",
          }}
        >
          <div
            style={{
              width: 32,
              height: 32,
              border: "3px solid rgba(28,25,23,0.06)",
              borderTopColor: "#5f928a",
              borderRadius: "50%",
              animation: "spin 0.8s linear infinite",
            }}
          />
        </div>
      ) : error ? (
        <div
          style={{
            padding: "16px 20px",
            borderRadius: 12,
            background: "#f8f0ef",
            border: "1px solid rgba(214,95,89,0.2)",
            color: "#883a35",
            fontSize: 13,
          }}
        >
          {error}
          <button
            onClick={() => {
              setLoading(true);
              fetchNotifications();
            }}
            style={{
              marginLeft: 12,
              fontSize: 12,
              fontWeight: 600,
              color: "#436b65",
              background: "transparent",
              border: "none",
              cursor: "pointer",
              textDecoration: "underline",
            }}
          >
            {t("page.announcements.retry")}
          </button>
        </div>
      ) : sorted.length === 0 ? (
        <EmptyState
          icon={
            <svg
              style={{ width: 32, height: 32, color: "#a8a29e" }}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.5}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M10.34 15.84c-.688-.06-1.386-.09-2.09-.09H7.5a4.5 4.5 0 110-9h.75c.704 0 1.402-.03 2.09-.09m0 9.18c.253.962.584 1.892.985 2.783.247.55.06 1.21-.463 1.511l-.657.38a1.154 1.154 0 01-1.614-.49A15.478 15.478 0 016 12c0-2.706.693-5.252 1.912-7.47.407-.862 1.407-1.14 2.164-.694l.657.38c.523.3.71.96.463 1.511a13.497 13.497 0 00-.985 2.783m0 9.18V15.84m0-6.68V6.16m0 3.016a12.03 12.03 0 010 5.647m0 0c.688.06 1.386.09 2.09.09h.75a4.5 4.5 0 100-9h-.75c-.704 0-1.402.03-2.09.09"
              />
            </svg>
          }
          title={t("page.announcements.empty_title")}
          description={t("page.announcements.empty_desc")}
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {sorted.map((item) => (
            <AnnouncementItem
              key={item.id}
              item={item}
              onMarkRead={handleMarkRead}
            />
          ))}
        </div>
      )}
    </>
  );
}
