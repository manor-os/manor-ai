import { useCallback, useEffect, useRef, useState } from "react";
import { createPublicChatReadCoalescer, residentStatusAllowsHistory, residentStatusSessionMissing, type PublicChatRequestFence } from "./publicChatRequestFence";

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
  const checkFlight = useRef<{ id: string; pending: boolean; promise: Promise<ResidentCheckResult> } | null>(null);
  const lastBackgroundCheckAt = useRef(0);
  const acceptedBinding = useRef<{ token: string; id: string; expires: number } | null>(null);
  const itemReads = useRef(createPublicChatReadCoalescer()).current;
  const current = useRef({ onInvalidate, onDisconnected, onSessionMissing });
  current.current = { onInvalidate, onDisconnected, onSessionMissing };
  const endpoint = `/api/v1/public/chat/${encodeURIComponent(token || "")}/resident`;

  const invalidate = useCallback((next: ResidentPhase = "expired") => {
    acceptedBinding.current = null;
    fence.invalidate(); checkFlight.current = null; lastBackgroundCheckAt.current = 0; itemReads.invalidate();
    setHistoryReady(false); setCheckedSessionId(null); setExpiresAt(null); setItems([]); setLoadingItems(false); setItemsError(false); setPhase(next);
    current.current.onInvalidate();
  }, [fence, itemReads]);

  const check = useCallback((id = sessionId, followUp = false): Promise<ResidentCheckResult> => {
    if (!token || !id || phaseRef.current === "redirecting" || phaseRef.current === "disconnecting") return Promise.resolve("denied");
    if (checkFlight.current?.id === id) {
      checkFlight.current.pending ||= followUp;
      return checkFlight.current.promise;
    }
    const promise = (async (): Promise<ResidentCheckResult> => {
      const request = fence.begin();
      const timeout = AbortSignal.timeout(15_000);
      try {
        const response = await fetch(`${endpoint}?session_id=${encodeURIComponent(id)}`, { credentials: "include", cache: "no-store", headers: headers(), signal: AbortSignal.any([request.signal, timeout]) });
        if (!request.isCurrent()) return "denied";
        if (response.status === 401 || response.status === 403) {
          invalidate("expired");
          return "denied";
        }
        const data = await response.json().catch(() => null);
        if (!request.isCurrent()) return "denied";
        timeout.throwIfAborted();
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
        acceptedBinding.current = data.verified ? { token, id, expires: Date.parse(data.expires_at) } : null;
        setCheckedSessionId(id); setHistoryReady(true); setPhase(data.verified ? "verified" : "unverified"); setExpiresAt(data.verified ? data.expires_at : null);
        return "allowed";
      } catch {
        if (request.isCurrent()) {
          const binding = acceptedBinding.current;
          // A read deadline is not revocation. Keep this already verified view
          // only until its original expiry; the failed check authorizes nothing.
          const retainUntilExpiry = timeout.aborted && binding?.token === token
            && binding.id === id && id === sessionId && binding.expires > Date.now();
          if (!retainUntilExpiry) invalidate("unavailable");
        }
        return "denied";
      } finally { request.release(); }
    })();
    checkFlight.current = { id, pending: false, promise };
    void promise.finally(() => {
      const flight = checkFlight.current;
      if (flight?.promise !== promise) return;
      checkFlight.current = null;
      if (flight.pending && id === sessionId) void check(id);
    });
    return promise;
  }, [sessionId, token, endpoint, headers, fence, invalidate]);
  const checkBackground = useCallback((visibleOnly = false) => {
    if (visibleOnly && document.visibilityState !== "visible") return;
    const now = Date.now();
    if (now - lastBackgroundCheckAt.current < 250) return;
    lastBackgroundCheckAt.current = now;
    void check(undefined, true);
  }, [check]);

  useEffect(() => {
    invalidate("checking");
    if (!sessionId) return;
    checkBackground();
    const interval = setInterval(checkBackground, 15_000);
    const focus = () => checkBackground(true);
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", focus);
    return () => { clearInterval(interval); window.removeEventListener("focus", focus); document.removeEventListener("visibilitychange", focus); checkFlight.current = null; itemReads.invalidate(); fence.invalidate(); };
  }, [sessionId, checkBackground, invalidate, fence, itemReads]);

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

  const loadItems = useCallback(() => {
    if (!sessionId || phase !== "verified" || !historyAllowed) return;
    itemReads.request(async () => {
      const request = fence.begin();
      setLoadingItems(true); setItemsError(false);
      try {
        const response = await fetch(`${endpoint}/maintenance?session_id=${encodeURIComponent(sessionId)}&skip=0&limit=20`, { credentials: "include", cache: "no-store", headers: headers(), signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]) });
        const data = await response.json().catch(() => null);
        if (!request.isCurrent()) return;
        if (response.status === 401 || response.status === 403) { invalidate(); return; }
        if (response.status === 503 && data?.detail?.code === "resident_unavailable") { invalidate("unavailable"); return; }
        if (!response.ok || !Array.isArray(data?.items)) { setItems([]); setItemsError(true); return; }
        setItems(data.items);
      } catch { if (request.isCurrent()) { setItems([]); setItemsError(true); } }
      finally { if (request.isCurrent()) setLoadingItems(false); request.release(); }
    });
  }, [sessionId, phase, historyAllowed, fence, endpoint, headers, itemReads, invalidate]);

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
