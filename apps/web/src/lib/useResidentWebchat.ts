import { useCallback, useEffect, useRef, useState } from "react";
import { residentStatusAllowsHistory, residentStatusSessionMissing, type PublicChatRequestFence } from "./publicChatRequestFence";

export type ResidentPhase = "checking" | "unverified" | "verified" | "expired" | "unavailable" | "redirecting" | "disconnecting";
/** "missing": the server has no conversation for this session ID. */
export type ResidentCheckResult = "allowed" | "missing" | "denied";
export type ResidentMaintenance = { id: number; title: string; status: string; priority?: string; category?: string; created_at?: string; updated_at?: string; description?: string };
export function useResidentWebchat({ token, sessionId, fence, headers, onInvalidate, onDisconnected, onSessionMissing }: {
  token?: string; sessionId: string | null; fence: PublicChatRequestFence;
  headers: () => HeadersInit; onInvalidate: () => void; onDisconnected: () => void;
  /** The active session's conversation is gone; return true when a fresh session is being started. */
  onSessionMissing?: () => boolean;
}) {
  const [phase, setPhase] = useState<ResidentPhase>("checking");
  const [historyReady, setHistoryReady] = useState(false);
  const [checkedSessionId, setCheckedSessionId] = useState<string | null>(null);
  const historyAllowed = historyReady && checkedSessionId === sessionId;
  const [expiresAt, setExpiresAt] = useState<string | null>(null);
  const [items, setItems] = useState<ResidentMaintenance[]>([]);
  const [loadingItems, setLoadingItems] = useState(false);
  const [itemsError, setItemsError] = useState(false);
  const [notice, setNotice] = useState<"cancelled" | "error" | "revoke_pending" | null>(null);
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const current = useRef({ onInvalidate, onDisconnected, onSessionMissing });
  current.current = { onInvalidate, onDisconnected, onSessionMissing };
  const endpoint = `/api/v1/public/chat/${encodeURIComponent(token || "")}/resident`;

  const invalidate = useCallback((next: ResidentPhase = "expired") => {
    fence.invalidate();
    setHistoryReady(false); setCheckedSessionId(null); setExpiresAt(null); setItems([]); setLoadingItems(false); setItemsError(false); setPhase(next);
    current.current.onInvalidate();
  }, [fence]);

  const check = useCallback(async (id = sessionId): Promise<ResidentCheckResult> => {
    if (!token || !id) return "denied";
    if (phaseRef.current === "redirecting" || phaseRef.current === "disconnecting") return "denied";
    const request = fence.begin();
    try {
      const response = await fetch(`${endpoint}?session_id=${encodeURIComponent(id)}`, { credentials: "include", cache: "no-store", headers: headers(), signal: request.signal });
      const data = await response.json().catch(() => null);
      if (!request.isCurrent()) return "denied";
      if (residentStatusSessionMissing(response.status, data)) {
        invalidate("unavailable");
        // A remembered ID that was never adopted is the caller's to replace.
        if (id === sessionId && current.current.onSessionMissing?.()) setPhase("checking");
        return "missing";
      }
      if (!response.ok || !residentStatusAllowsHistory(data)) {
        invalidate(response.status === 401 || response.status === 403 ? "expired" : "unavailable");
        return "denied";
      }
      setCheckedSessionId(id); setHistoryReady(true); setPhase(data.verified ? "verified" : "unverified"); setExpiresAt(data.verified ? data.expires_at : null);
      return "allowed";
    } catch {
      if (request.isCurrent()) invalidate("unavailable");
      return "denied";
    } finally { request.release(); }
  }, [sessionId, token, endpoint, headers, fence, invalidate]);

  useEffect(() => {
    invalidate("checking");
    if (!sessionId) return;
    void check();
    const interval = setInterval(() => { void check(); }, 15_000);
    const focus = () => { if (document.visibilityState === "visible") void check(); };
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", focus);
    return () => { clearInterval(interval); window.removeEventListener("focus", focus); document.removeEventListener("visibilitychange", focus); fence.invalidate(); };
  }, [sessionId, check, invalidate, fence]);

  useEffect(() => {
    if (!expiresAt) return;
    const timer = setTimeout(() => invalidate("expired"), Math.max(0, Date.parse(expiresAt) - Date.now()));
    return () => clearTimeout(timer);
  }, [expiresAt, invalidate]);

  useEffect(() => {
    const url = new URL(window.location.href);
    const status = url.searchParams.get("resident");
    if (!status) return;
    if (status === "cancelled" || status === "error") setNotice(status);
    url.searchParams.delete("resident");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  }, []);

  const loadItems = useCallback(async () => {
    if (!sessionId || phase !== "verified" || !historyAllowed) return;
    const request = fence.begin();
    setLoadingItems(true); setItemsError(false);
    try {
      const response = await fetch(`${endpoint}/maintenance?session_id=${encodeURIComponent(sessionId)}&skip=0&limit=20`, { credentials: "include", cache: "no-store", headers: headers(), signal: request.signal });
      const data = await response.json().catch(() => null);
      if (!request.isCurrent()) return;
      if (response.status === 401 || response.status === 403) { invalidate(); return; }
      if (response.status === 503 && data?.detail?.code === "resident_unavailable") { invalidate("unavailable"); return; }
      if (!response.ok || !Array.isArray(data?.items)) { setItems([]); setItemsError(true); return; }
      setItems(data.items);
    } catch { if (request.isCurrent()) { setItems([]); setItemsError(true); } }
    finally { if (request.isCurrent()) setLoadingItems(false); request.release(); }
  }, [sessionId, phase, historyAllowed, fence, endpoint, headers, invalidate]);

  useEffect(() => { if (phase === "verified") void loadItems(); }, [phase, loadItems]);

  const start = async () => {
    if (!sessionId || !token) return;
    invalidate("redirecting"); setNotice(null);
    phaseRef.current = "redirecting";
    const request = fence.begin();
    try {
      const response = await fetch(`${endpoint}/start`, { method: "POST", credentials: "include", headers: headers(), signal: request.signal, body: JSON.stringify({ session_id: sessionId }) });
      const data = await response.json().catch(() => null);
      if (!request.isCurrent()) return;
      if (!response.ok || typeof data?.authorize_url !== "string") { invalidate("expired"); setNotice("error"); return; }
      const target = new URL(data.authorize_url);
      if (!['https:', 'http:'].includes(target.protocol) || target.username || target.password) throw new Error("Invalid authorization destination");
      window.location.assign(target.href);
    } catch { if (request.isCurrent()) { invalidate("expired"); setNotice("error"); } }
    finally { request.release(); }
  };

  const disconnect = async () => {
    if (!sessionId) return;
    invalidate("disconnecting"); setNotice(null);
    phaseRef.current = "disconnecting";
    const request = fence.begin();
    try {
      const response = await fetch(endpoint, { method: "DELETE", credentials: "include", headers: headers(), signal: request.signal, body: JSON.stringify({ session_id: sessionId }) });
      if (!request.isCurrent()) return;
      const data = response.status === 204 ? null : await response.json().catch(() => null);
      if (response.status !== 204 && data?.detail?.code !== "resident_revoke_pending") { setPhase("expired"); setNotice("error"); return; }
      if (data?.detail?.code === "resident_revoke_pending") setNotice("revoke_pending");
      current.current.onDisconnected();
    } catch { if (request.isCurrent()) { setPhase("expired"); setNotice("error"); } }
    finally { request.release(); }
  };
  return { phase, historyAllowed, expiresAt, items, loadingItems, itemsError, notice, check, invalidate, start, disconnect, loadItems };
}
