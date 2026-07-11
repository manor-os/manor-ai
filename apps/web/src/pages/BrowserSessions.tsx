import { useState, useCallback } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { formatDate as formatTime, statusBadgeType } from "../lib/format";
import PageHeader from "../components/ui/PageHeader";
import GlassCard from "../components/ui/GlassCard";
import StatusBadge from "../components/ui/StatusBadge";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import { IconPlus, IconEye, IconInfo, IconClose } from "../components/icons";

/* ------------------------------------------------------------------ */
/*  Session List View (no sessionId in URL)                            */
/* ------------------------------------------------------------------ */

function SessionList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: sessions, isLoading, error } = useQuery({
    queryKey: ["browser-sessions"],
    queryFn: () => api.browser.listSessions(),
  });

  const createMutation = useMutation({
    mutationFn: () => api.browser.createSession(),
    onSuccess: (session) => {
      queryClient.invalidateQueries({ queryKey: ["browser-sessions"] });
      navigate(`/browser/sessions/${session.session_id}`);
    },
  });

  const closeMutation = useMutation({
    mutationFn: (id: string) => api.browser.close(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["browser-sessions"] });
    },
  });

  return (
    <div className="animate-fade-in" style={{ padding: "1rem" }}>
      <PageHeader
        title={t("page.browser_sessions.title")}
        subtitle={t("page.browser_sessions.subtitle")}
        actions={
          <Button
            variant="primary"
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending}
          >
            <IconPlus size={14} />
            {createMutation.isPending ? t("page.browser_sessions.creating") : t("page.browser_sessions.new_session")}
          </Button>
        }
      />

      {isLoading ? (
        <div style={{ display: "flex", justifyContent: "center", padding: "80px 0" }}>
          <LoadingSpinner />
        </div>
      ) : error ? (
        <div style={{ padding: "80px 0" }}>
          <EmptyState
            icon={
              <IconInfo size={48} className="text-stone-300" />
            }
            title={t("page.browser_sessions.failed_load")}
            description={error instanceof Error ? error.message : t("page.browser_sessions.unexpected_error")}
          />
        </div>
      ) : !sessions || sessions.length === 0 ? (
        <div style={{ padding: "80px 0" }}>
          <EmptyState
            icon={
              <svg style={{ width: 48, height: 48, color: "#d6d3d1" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 17.25v1.007a3 3 0 01-.879 2.122L7.5 21h9l-.621-.621A3 3 0 0115 18.257V17.25m6-12V15a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 15V5.25m18 0A2.25 2.25 0 0018.75 3H5.25A2.25 2.25 0 003 5.25m18 0V12a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 12V5.25" />
              </svg>
            }
            title={t("page.browser_sessions.no_sessions")}
            description={t("page.browser_sessions.no_sessions_desc")}
            action={
              <Button
                variant="primary"
                onClick={() => createMutation.mutate()}
                disabled={createMutation.isPending}
              >
                {createMutation.isPending ? t("page.browser_sessions.creating") : t("page.browser_sessions.new_session")}
              </Button>
            }
          />
        </div>
      ) : (
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 320px), 1fr))",
          gap: 16,
          marginTop: 16,
        }}>
          {sessions.map((session) => (
            <GlassCard key={session.session_id}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <p style={{ fontSize: 14, fontWeight: 700, color: "#292524", marginBottom: 4, fontFamily: "monospace" }}>
                    {session.session_id.slice(0, 12)}...
                  </p>
                  {session.current_url && (
                    <p style={{
                      fontSize: 12,
                      color: "#78716c",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}>
                      {session.current_url}
                    </p>
                  )}
                </div>
                <StatusBadge type={statusBadgeType(session.status)} dot>
                  {session.status}
                </StatusBadge>
              </div>

              {session.created_at && (
                <p style={{ fontSize: 11, color: "#a8a29e", marginBottom: 12 }}>
                  {t("page.browser_sessions.created")} {formatTime(session.created_at)}
                </p>
              )}

              <div style={{ display: "flex", gap: 8 }}>
                <Link
                  to={`/browser/sessions/${session.session_id}`}
                  className="btn-manor"
                  style={{
                    flex: 1,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: 6,
                    fontSize: 12,
                    fontWeight: 700,
                    textDecoration: "none",
                  }}
                >
                  <IconEye size={12} />
                  {t("page.browser_sessions.view")}
                </Link>
                <Button
                  variant="outline"
                  onClick={() => closeMutation.mutate(session.session_id)}
                  disabled={closeMutation.isPending}
                >
                  {t("page.browser_sessions.close")}
                </Button>
              </div>
            </GlassCard>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Session Detail View                                                */
/* ------------------------------------------------------------------ */

function SessionDetail({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [navUrl, setNavUrl] = useState("");
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null);

  const { data: session, isLoading, error } = useQuery({
    queryKey: ["browser-session", sessionId],
    queryFn: () => api.browser.getSession(sessionId),
    refetchInterval: 5000,
  });

  const navigateMutation = useMutation({
    mutationFn: (url: string) => api.browser.navigate(sessionId, url),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["browser-session", sessionId] });
      setNavUrl("");
    },
  });

  const screenshotMutation = useMutation({
    mutationFn: () => api.browser.screenshot(sessionId),
    onSuccess: (blob) => {
      // Revoke previous URL
      if (screenshotUrl) URL.revokeObjectURL(screenshotUrl);
      setScreenshotUrl(URL.createObjectURL(blob));
    },
  });

  const actionMutation = useMutation({
    mutationFn: ({ actionType, params }: { actionType: string; params: Record<string, any> }) =>
      api.browser.action(sessionId, actionType, params),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["browser-session", sessionId] });
    },
  });

  const closeMutation = useMutation({
    mutationFn: () => api.browser.close(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["browser-sessions"] });
      navigate("/browser/sessions");
    },
  });

  const handleNavigate = useCallback((e: React.FormEvent) => {
    e.preventDefault();
    if (!navUrl.trim()) return;
    const url = navUrl.startsWith("http") ? navUrl : `https://${navUrl}`;
    navigateMutation.mutate(url);
  }, [navUrl, navigateMutation]);

  if (isLoading) {
    return (
      <div className="animate-fade-in" style={{ padding: "1rem" }}>
        <PageHeader title={t("page.browser_sessions.session_title")} subtitle={t("status.loading")} />
        <div style={{ display: "flex", justifyContent: "center", padding: "80px 0" }}>
          <LoadingSpinner />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="animate-fade-in" style={{ padding: "1rem" }}>
        <PageHeader title={t("page.browser_sessions.session_title")} subtitle={t("status.error")} />
        <div style={{ padding: "80px 0" }}>
          <EmptyState
            icon={
              <IconInfo size={48} className="text-stone-300" />
            }
            title={t("page.browser_sessions.session_not_found")}
            description={error instanceof Error ? error.message : t("page.browser_sessions.could_not_load")}
            action={
              <Link to="/browser/sessions" className="btn-manor" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 14, textDecoration: "none" }}>
                {t("page.browser_sessions.back_to_sessions")}
              </Link>
            }
          />
        </div>
      </div>
    );
  }

  const statusType = session ? statusBadgeType(session.status) : "inactive";

  return (
    <div className="animate-fade-in" style={{ padding: "1rem" }}>
      <PageHeader
        title={t("page.browser_sessions.session_title")}
        subtitle={`${t("page.browser_sessions.session")} ${sessionId.slice(0, 12)}...`}
        actions={
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <StatusBadge type={statusType} dot pulse={statusType === "active"}>
              {session?.status || t("page.browser_sessions.unknown")}
            </StatusBadge>
            <Link to="/browser/sessions" style={{ fontSize: 12, color: "#436b65", textDecoration: "none", fontWeight: 600 }}>
              {t("page.browser_sessions.all_sessions")}
            </Link>
          </div>
        }
      />

      {/* URL Navigation Bar */}
      <div style={{ marginBottom: 16 }}>
        <form onSubmit={handleNavigate} style={{ display: "flex", gap: 8 }}>
          <div className="glass-panel" style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            padding: "4px 4px 4px 14px",
            gap: 8,
          }}>
            <svg style={{ width: 14, height: 14, color: "#a8a29e", flexShrink: 0 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418" />
            </svg>
            <input
              type="text"
              placeholder={session?.current_url || t("page.browser_sessions.enter_url")}
              value={navUrl}
              onChange={(e) => setNavUrl(e.target.value)}
              style={{
                flex: 1,
                background: "transparent",
                padding: "8px 0",
                fontSize: 13,
                outline: "none",
                border: "none",
                color: "#44403c",
              }}
            />
            <Button
              type="submit"
              variant="primary"
              disabled={navigateMutation.isPending || !navUrl.trim()}
            >
              {navigateMutation.isPending ? t("page.browser_sessions.going") : t("page.browser_sessions.go")}
            </Button>
          </div>
        </form>
      </div>

      {/* Browser Preview / Screenshot */}
      <div style={{ marginBottom: 16 }}>
        <div className="glass-panel" style={{ padding: 20 }}>
          <div style={{
            borderRadius: 16,
            background: screenshotUrl ? "transparent" : "rgba(250,250,249,0.6)",
            border: screenshotUrl ? "none" : "2px dashed rgba(231,229,228,0.8)",
            minHeight: 320,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            overflow: "hidden",
          }}>
            {screenshotUrl ? (
              <img
                src={screenshotUrl}
                alt={t("page.browser_sessions.screenshot_alt")}
                style={{ width: "100%", height: "auto", borderRadius: 12 }}
              />
            ) : (
              <div style={{ textAlign: "center" }}>
                <svg style={{ width: 48, height: 48, color: "#d6d3d1", margin: "0 auto 12px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 17.25v1.007a3 3 0 01-.879 2.122L7.5 21h9l-.621-.621A3 3 0 0115 18.257V17.25m6-12V15a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 15V5.25m18 0A2.25 2.25 0 0018.75 3H5.25A2.25 2.25 0 003 5.25m18 0V12a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 12V5.25" />
                </svg>
                <p style={{ fontSize: 14, fontWeight: 600, color: "#78716c", marginBottom: 4 }}>{t("page.browser_sessions.browser_preview")}</p>
                <p style={{ fontSize: 12, color: "#a8a29e", marginBottom: 12 }}>{t("page.browser_sessions.take_screenshot_desc")}</p>
                <Button
                  variant="primary"
                  onClick={() => screenshotMutation.mutate()}
                  disabled={screenshotMutation.isPending}
                >
                  {screenshotMutation.isPending ? t("page.browser_sessions.capturing") : t("page.browser_sessions.take_screenshot")}
                </Button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Session Metadata + Controls */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 16, marginBottom: 16 }}>
        <div className="lg:grid lg:grid-cols-3 lg:gap-4">
          <div className="lg:col-span-2 mb-4 lg:mb-0">
            <GlassCard hoverable={false}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", marginBottom: 12 }}>{t("page.browser_sessions.session_details")}</h3>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12 }}>
                <div>
                  <span style={{ fontSize: 11, color: "#a8a29e", display: "block", marginBottom: 2 }}>{t("page.browser_sessions.session_id")}</span>
                  <p style={{ fontSize: 14, fontWeight: 600, color: "#57534e", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "monospace" }}>
                    {session?.session_id || sessionId}
                  </p>
                </div>
                <div>
                  <span style={{ fontSize: 11, color: "#a8a29e", display: "block", marginBottom: 2 }}>{t("page.browser_sessions.status")}</span>
                  <p style={{ fontSize: 14, fontWeight: 600, color: "#57534e" }}>{session?.status || t("page.browser_sessions.unknown")}</p>
                </div>
                <div>
                  <span style={{ fontSize: 11, color: "#a8a29e", display: "block", marginBottom: 2 }}>{t("page.browser_sessions.current_url")}</span>
                  <p style={{ fontSize: 14, fontWeight: 600, color: "#57534e", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {session?.current_url || t("page.browser_sessions.no_url")}
                  </p>
                </div>
                <div>
                  <span style={{ fontSize: 11, color: "#a8a29e", display: "block", marginBottom: 2 }}>{t("page.browser_sessions.created_at")}</span>
                  <p style={{ fontSize: 14, fontWeight: 600, color: "#57534e" }}>
                    {session?.created_at ? formatTime(session.created_at) : t("page.browser_sessions.na")}
                  </p>
                </div>
              </div>
            </GlassCard>
          </div>

          <GlassCard hoverable={false}>
            <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", marginBottom: 12 }}>{t("page.browser_sessions.controls")}</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <Button
                variant="primary"
                onClick={() => screenshotMutation.mutate()}
                disabled={screenshotMutation.isPending}
                className="w-full justify-center"
              >
                <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6.827 6.175A2.31 2.31 0 015.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 00-1.134-.175 2.31 2.31 0 01-1.64-1.055l-.822-1.316a2.192 2.192 0 00-1.736-1.039 48.774 48.774 0 00-5.232 0 2.192 2.192 0 00-1.736 1.039l-.821 1.316z" />
                </svg>
                {screenshotMutation.isPending ? t("page.browser_sessions.capturing") : t("page.browser_sessions.screenshot")}
              </Button>
              <Button
                variant="primary"
                onClick={() => actionMutation.mutate({ actionType: "click", params: {} })}
                disabled={actionMutation.isPending}
                className="w-full justify-center"
              >
                <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15.042 21.672L13.684 16.6m0 0l-2.51 2.225.569-9.47 5.227 7.917-3.286-.672z" />
                </svg>
                {t("page.browser_sessions.click_action")}
              </Button>
              <Button
                variant="primary"
                onClick={() => closeMutation.mutate()}
                disabled={closeMutation.isPending}
                className="w-full justify-center"
              >
                <IconClose size={16} />
                {closeMutation.isPending ? t("page.browser_sessions.closing") : t("page.browser_sessions.close_session")}
              </Button>
            </div>
          </GlassCard>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Page                                                          */
/* ------------------------------------------------------------------ */

export default function BrowserSessions() {
  const { sessionId } = useParams<{ sessionId: string }>();

  if (!sessionId) return <SessionList />;

  return <SessionDetail sessionId={sessionId} />;
}
