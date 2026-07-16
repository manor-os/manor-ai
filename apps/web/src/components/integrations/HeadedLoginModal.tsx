 /**
 * HeadedLoginModal — interactive sign-in for browser-session providers.
 *
 * Connects to the browser-runner sidecar's VNC stream (relayed by api)
 * via @novnc/novnc's RFB client. The container runs a real headed
 * Chromium on Xvfb; the user gets a remote-desktop-quality view with
 * native mouse capture, full keyboard with IME support, and lossless
 * region updates.
 *
 * Replaces the previous CDP-screencast canvas. That flow:
 *   - dropped frames whenever the page didn't repaint (e.g. typing
 *     into a static input)
 *   - silently swallowed keystrokes unless the canvas had focus,
 *     which broke the moment the user dragged or selected text
 *
 * Flow:
 *   1. POST /integrations/headed-login/start → spawns a Chromium in
 *      browser-runner against an Xvfb display, returns a session id
 *      + the WS path to its VNC stream.
 *   2. RFB().attach() over /api/v1/integrations/headed-login/:sid/stream
 *      — the api proxies to the runner's websockify which speaks RFB
 *      to x11vnc.
 *   3. POST /finish → server calls Playwright.context.storage_state(),
 *      encrypts via CredentialService, persists as the integration's
 *      credentials. Modal closes; cards refresh.
 */
import { useEffect, useRef, useState } from "react";
// noVNC 1.7.0 dropped the legacy `lib/rfb` subpath and exposes the RFB
// class as the package's only export (package.json: "exports":
// "./core/rfb.js"). Importing the old path errors at vite build time
// with: Package subpath 'undefined' is not defined by "exports".
import RFB from "@novnc/novnc";
import { getAuthToken } from "../../lib/authToken";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import { useToastStore } from "../../stores/toast";
import { t } from "../../lib/i18n";


interface HeadedLoginModalProps {
  open: boolean;
  provider: string | null;
  providerName?: string;
  onClose: () => void;
  onSuccess?: (integrationId: string) => void;
}

interface StartResponse {
  session_id: string;
  viewport: { width: number; height: number };
  ws_path: string;
  provider: string;
}

const API_BASE = "/api/v1";

async function startSession(provider: string): Promise<StartResponse> {
  const token = getAuthToken();
  const r = await fetch(`${API_BASE}/integrations/headed-login/start`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ provider }),
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`start failed: ${r.status} ${detail}`);
  }
  return r.json();
}

async function finishSession(sid: string): Promise<{ integration_id: string; final_url: string }> {
  const token = getAuthToken();
  const r = await fetch(`${API_BASE}/integrations/headed-login/${sid}/finish`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({}),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(body.detail || r.statusText);
  }
  return r.json();
}

async function cancelSession(sid: string): Promise<void> {
  const token = getAuthToken();
  await fetch(`${API_BASE}/integrations/headed-login/${sid}/cancel`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).catch(() => undefined);
}

export default function HeadedLoginModal({
  open, provider, providerName, onClose, onSuccess,
}: HeadedLoginModalProps) {
  const screenRef = useRef<HTMLDivElement | null>(null);
  const rfbRef = useRef<RFB | null>(null);
  const sidRef = useRef<string | null>(null);
  const [status, setStatus] = useState<"idle" | "starting" | "connecting" | "live" | "saving" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState<string>("");
  const [viewport, setViewport] = useState<{ width: number; height: number }>({ width: 1440, height: 900 });

  // ── Lifecycle ─────────────────────────────────────────────────────────

  useEffect(() => {
    if (!open || !provider) return;
    let cancelled = false;
    setStatus("starting");
    setErrorMsg("");

    (async () => {
      try {
        const start = await startSession(provider);
        if (cancelled) {
          await cancelSession(start.session_id);
          return;
        }
        sidRef.current = start.session_id;
        setViewport(start.viewport);

        const target = screenRef.current;
        if (!target) {
          setStatus("error");
          setErrorMsg("internal: VNC mount point missing");
          return;
        }

        const token = getAuthToken() || "";
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${proto}//${window.location.host}${start.ws_path}?token=${encodeURIComponent(token)}`;

        setStatus("connecting");
        // RFB attaches a <canvas> into `target`, manages mouse/keyboard
        // natively (real focus, IME-aware), and handles all the RFB
        // protocol negotiation including encoding fallback.
        const rfb = new RFB(target, wsUrl, {
          credentials: { username: "", password: "", target: "" },
          // We negotiate `binary` subprotocol on the runner side; let
          // the browser pick its preferred one too.
          wsProtocols: ["binary"],
        });
        rfb.viewOnly = false;
        rfb.scaleViewport = true;        // fit the canvas to its container
        rfb.resizeSession = false;       // don't ask the server to resize
        rfb.background = "#1c1917";
        rfb.qualityLevel = 8;            // 0-9, higher = sharper/larger
        rfb.compressionLevel = 2;        // 0-9, lower = less CPU/server
        rfb.showDotCursor = true;        // local dot if remote cursor missing

        rfb.addEventListener("connect", () => {
          if (!cancelled) setStatus("live");
        });
        rfb.addEventListener("disconnect", (e: any) => {
          if (cancelled || status === "saving") return;
          const reason = (e?.detail?.reason || "").toString();
          // VNC server vanished on capture → expected when Done is clicked.
          if (reason.includes("clean")) return;
          setStatus("error");
          setErrorMsg(reason || "VNC connection closed");
        });
        rfb.addEventListener("securityfailure", (e: any) => {
          setStatus("error");
          setErrorMsg(`auth failed: ${e?.detail?.reason || "unknown"}`);
        });

        rfbRef.current = rfb;
      } catch (exc: any) {
        if (!cancelled) {
          setStatus("error");
          setErrorMsg(String(exc?.message || exc));
        }
      }
    })();

    return () => {
      cancelled = true;
      const rfb = rfbRef.current;
      if (rfb) {
        try { rfb.disconnect(); } catch { /* noop */ }
      }
      rfbRef.current = null;
      if (sidRef.current) {
        void cancelSession(sidRef.current);
        sidRef.current = null;
      }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, provider]);

  // ── Actions ──────────────────────────────────────────────────────────

  async function handleDone() {
    if (!sidRef.current) return;
    setStatus("saving");
    try {
      const res = await finishSession(sidRef.current);
      useToastStore.getState().success(
        t("component.headed_login_modal.signed_in").replace("{provider}", providerName || provider || t("nav.integrations")),
        t("component.headed_login_modal.session_captured"),
      );
      onSuccess?.(res.integration_id);
      sidRef.current = null;
      onClose();
    } catch (exc: any) {
      setStatus("error");
      setErrorMsg(String(exc?.message || exc));
    }
  }

  // Make the panel fit the viewport while preserving aspect ratio. The
  // RFB canvas inside `screenRef` is `width: 100%; height: 100%` so the
  // outer aspect-ratio div drives sizing.
  const aspect = `${viewport.width} / ${viewport.height}`;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Sign in to ${providerName || provider || ""}`}
      maxWidth="min(1280px, 92vw)"
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose}>{t("action.cancel")}</Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleDone}
            disabled={status !== "live"}
          >
            {status === "saving" ? t("page.agents.saving") : t("component.headed_login_modal.i_m_signed_in_save")}
          </Button>
        </>
      }
    >
      <div style={{ fontSize: 13, color: "#57534e", marginBottom: 12 }}>
        {t("component.headed_login_modal.complete_sign_in_inside_the_window_below_when_the_dash")}<strong>{t("component.headed_login_modal.i_m_signed_in_save")}</strong> {t("component.headed_login_modal.and_manor_will_capture_the_session_cookies_nothing_lea")}</div>

      <div
        style={{
          position: "relative",
          width: "min(100%, calc((85vh - 200px) * " + viewport.width + " / " + viewport.height + "))",
          aspectRatio: aspect,
          margin: "0 auto",
          background: "#1c1917",
          borderRadius: 8,
          overflow: "hidden",
          border: status === "live" ? "2px solid #54a176" : "2px solid #292524",
          transition: "border-color 120ms",
        }}
      >
        {/*
          RFB attaches a <canvas> as a child of this div on connect.
          We don't render anything inside ourselves — noVNC owns the
          DOM tree under here. Pointer events and keyboard are handled
          by RFB's internal listeners; no manual focus management.
        */}
        <div
          ref={screenRef}
          style={{ width: "100%", height: "100%" }}
        />

        {status !== "live" && (
          <div
            style={{
              position: "absolute", inset: 0,
              display: "flex", alignItems: "center", justifyContent: "center",
              flexDirection: "column", gap: 8,
              color: "#d6d3d1", fontSize: 13,
              background: "rgba(28, 25, 23, 0.85)",
              pointerEvents: "none",  // let underlying RFB stay interactive when transitioning
            }}
          >
            {status === "starting"   && <div>{t("component.headed_login_modal.spinning_up_chromium")}</div>}
            {status === "connecting" && <div>{t("component.headed_login_modal.negotiating_remote_desktop_session")}</div>}
            {status === "saving"     && <div>{t("component.headed_login_modal.saving_session")}</div>}
            {status === "error"      && (
              <div style={{ color: "#ddafac" }}>
                {errorMsg || t("component.headed_login_modal.connection_lost")}
              </div>
            )}
            {status === "idle"       && <div>{t("component.headed_login_modal.idle")}</div>}
          </div>
        )}
      </div>

      <div style={{ marginTop: 8, fontSize: 11, color: "#a8a29e" }}>
        {t("page.workspace_detail.status")}{status}
        {status === "live" && (
          <> · <span style={{ color: "#54a176" }}>{t("component.headed_login_modal.connected_click_anywhere_to_interact")}</span></>
        )}
        {sidRef.current ? ` · Session ${sidRef.current.slice(0, 8)}` : ""}
      </div>
    </Modal>
  );
}
