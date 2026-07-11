/**
 * VideoCard — inline async video generation preview.
 * Shows spinner while generating, error on failure, video player on success.
 * Polls /api/v1/media/jobs/{id} and listens for WebSocket push events.
 */
import { useState, useEffect } from "react";
import { resolveDisplayMediaUrl } from "../../lib/api";
import { getAuthToken } from "../../lib/authToken";
import { t } from "../../lib/i18n";

export default function VideoCard({ resultJson }: { resultJson: string }) {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  useEffect(() => {
    try { setData(JSON.parse(resultJson)); } catch { setData(null); }
  }, [resultJson]);

  // Poll for completion if pending/processing
  useEffect(() => {
    const jobId = data?.job_id || data?.id;
    if (!jobId || (data.status !== "pending" && data.status !== "processing")) return;
    let active = true;
    const poll = async () => {
      try {
        const token = getAuthToken();
        const res = await fetch(`/api/v1/media/jobs/${jobId}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!active || !res.ok) return;
        const job = await res.json();
        if (job.status === "completed" || job.status === "failed") {
          setData(job);
        }
      } catch { /* ignore */ }
    };
    // Listen for WebSocket push (instant notification)
    const onWsEvent = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      const detailJobId = detail?.job_id || detail?.id;
      if (detailJobId === jobId) setData(detail);
    };
    window.addEventListener("manor:video-ready", onWsEvent);
    const timeout = setTimeout(poll, 10000);
    const interval = setInterval(poll, 15000);
    return () => { active = false; clearInterval(interval); clearTimeout(timeout); window.removeEventListener("manor:video-ready", onWsEvent); };
  }, [data?.job_id, data?.id, data?.status]);

  const rawUrl = data?.result_url || data?.video_url || data?.url || "";
  useEffect(() => {
    if (!rawUrl) { setDisplayUrl(null); setPreviewError(null); return; }
    let cancelled = false;
    let revoke = () => {};
    setPreviewError(null);
    resolveDisplayMediaUrl(rawUrl)
      .then((resolved) => {
        revoke = resolved.revoke;
        if (!cancelled) setDisplayUrl(resolved.url);
      })
      .catch(() => {
        if (!cancelled) setDisplayUrl(null);
        if (!cancelled) setPreviewError(t("component.video_card.preview_load_failed"));
      });
    return () => { cancelled = true; revoke(); };
  }, [rawUrl]);

  if (!data) return null;

  // Completed — show video player
  if (rawUrl) {
    return (
      <div style={{ padding: "6px 8px" }}>
        {displayUrl ? (
          <video
            src={displayUrl}
            controls
            playsInline
            preload="metadata"
            style={{ width: "100%", borderRadius: 8, maxWidth: 360, background: "#000", display: "block" }}
          />
        ) : previewError ? (
          <div style={{ width: "100%", maxWidth: 360, minHeight: 96, borderRadius: 8, background: "#fafaf9", border: "1px solid rgba(28,25,23,0.06)", display: "flex", alignItems: "center", justifyContent: "center", padding: 12 }}>
            <div style={{ fontSize: 11, color: "#78716c", textAlign: "center" }}>{previewError}</div>
          </div>
        ) : (
          <div style={{ width: "100%", maxWidth: 360, height: 202, borderRadius: 8, background: "#1c1917", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <span style={{ fontSize: 11, color: "#d6d3d1" }}>{t("component.video_card.loading_preview")}</span>
          </div>
        )}
        <div style={{ fontSize: 9, color: "#78716c", marginTop: 3 }}>
          {data.model} &middot; {data.duration || data.duration_seconds}s &middot; {data.resolution || data.params?.resolution || "720p"}
          {data.credits ? ` \u00b7 ${data.credits} ${t("component.video_card.credits")}` : ""}
        </div>
      </div>
    );
  }

  // Failed. Some tool preflight errors return an error payload before a
  // background job exists, so do not require status=failed.
  if (data.status === "failed" || data.error) {
    return (
      <div style={{ padding: "8px", background: "#f8f0ef", borderRadius: 6, margin: "4px 8px 6px" }}>
        <div style={{ fontSize: 11, color: "#c14a44", fontWeight: 500 }}>{t("component.video_card.generation_failed")}</div>
        <div style={{ fontSize: 10, color: "#7f1d1d", marginTop: 2 }}>{data.error || t("component.video_card.unknown_error")}</div>
      </div>
    );
  }

  // Pending / processing — show spinner
  return (
    <div style={{ padding: "8px", background: "#fafaf9", borderRadius: 6, margin: "4px 8px 6px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <div style={{ width: 12, height: 12, border: "2px solid #6d6fb2", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 1s linear infinite" }} />
        <span style={{ fontSize: 11, color: "#494596", fontWeight: 500 }}>{t("component.video_card.generating")}</span>
      </div>
      <div style={{ fontSize: 10, color: "#78716c", marginTop: 4 }}>
        {data.duration}s &middot; {data.resolution} &middot; {data.credits_estimate ? `~${data.credits_estimate} ${t("component.video_card.credits")}` : data.model}
      </div>
      <div style={{ fontSize: 9, color: "#a8a29e", marginTop: 2 }}>
        {t("component.video_card.usually_takes")}
      </div>
    </div>
  );
}
