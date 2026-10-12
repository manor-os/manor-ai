import { useCallback, useEffect, useRef, useState } from "react";
import { createPublicChatRequestFence, type PublicChatRequestFence } from "./publicChatRequestFence";
import { isResidentDraft, isResidentDraftCollection, residentCreateIsActive, type ResidentDraft, type ResidentDraftCollection, type ResidentDraftFields } from "./residentMaintenanceDrafts";

type Notice = "cancelled" | "error" | "conflict" | "save_failed" | "outcome_unknown" | null;
type Phase = "checking" | "unverified" | "verified" | "unavailable" | "redirecting";
export function useResidentMaintenanceDrafts({ token, sessionId, readReady, fence, headers, onReadInvalid, refreshKey }: {
  token?: string; sessionId: string | null; readReady: boolean; fence: PublicChatRequestFence;
  headers: () => HeadersInit; onReadInvalid: (unavailable?: boolean) => void; refreshKey: string;
}) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [snapshot, setSnapshot] = useState<{ key: string; data: ResidentDraftCollection } | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyDraftId, setBusyDraftId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [uncertain, setUncertain] = useState<Set<string>>(new Set());
  const uncertainDrafts = useRef(new Map<string, ResidentDraft>());
  const localFence = useRef(createPublicChatRequestFence()).current;
  const busy = useRef(false);
  const loadSequence = useRef(0);
  const page = useRef(0);
  const callbacks = useRef({ onReadInvalid }); callbacks.current = { onReadInvalid };
  const key = readReady && token && sessionId ? `${token}:${sessionId}` : "";
  const endpoint = `/api/v1/public/chat/${encodeURIComponent(token || "")}/resident`;
  const collection = key && snapshot?.key === key ? snapshot.data : null;
  const active = !!collection && residentCreateIsActive(collection.create_access);

  const begin = useCallback(() => {
    const parent = fence.begin(), local = localFence.begin();
    return { signal: AbortSignal.any([parent.signal, local.signal]), isCurrent: () => parent.isCurrent() && local.isCurrent(), release: () => { parent.release(); local.release(); } };
  }, [fence, localFence]);
  const clear = useCallback((next: Phase) => {
    localFence.invalidate(); loadSequence.current++;
    busy.current = false;
    uncertainDrafts.current.clear();
    setSnapshot(null); setUncertain(new Set()); setBusyDraftId(null); setLoading(false); setPhase(next);
  }, [localFence]);
  const accessError = useCallback((status: number, code?: string) => {
    if (code === "resident_auth_required" || code === "resident_unavailable" || ((status === 401 || status === 403) && code !== "resident_create_auth_required")) {
      clear("unverified"); callbacks.current.onReadInvalid(code === "resident_unavailable"); return true;
    }
    if (code === "resident_create_auth_required") { clear("unverified"); return true; }
    if (code === "resident_create_unavailable") { clear("unavailable"); return true; }
    return false;
  }, [clear]);

  const load = useCallback(async (skip = page.current) => {
    if (!key || busy.current) return;
    const request = begin(), sequence = ++loadSequence.current;
    setLoading(true);
    try {
      const response = await fetch(`${endpoint}/maintenance/drafts?session_id=${encodeURIComponent(sessionId!)}&skip=${skip}&limit=20`, { credentials: "include", cache: "no-store", headers: headers(), signal: request.signal });
      const data = await response.json().catch(() => null);
      if (!request.isCurrent() || sequence !== loadSequence.current) return;
      if (!response.ok || !isResidentDraftCollection(data)) {
        if (!accessError(response.status, data?.detail?.code)) clear("unavailable");
        return;
      }
      if (!residentCreateIsActive(data.create_access)) { clear("unverified"); return; }
      page.current = skip;
      for (const draft of data.items) {
        if (draft.state !== "draft") uncertainDrafts.current.delete(draft.draft_id);
      }
      // An empty page is not proof that a timed-out confirmation did not submit.
      const missingUncertain = [...uncertainDrafts.current.values()].filter(draft => !data.items.some(item => item.draft_id === draft.draft_id));
      setSnapshot({ key, data: { ...data, items: [...data.items, ...missingUncertain] } }); setPhase("verified");
      setUncertain(new Set(uncertainDrafts.current.keys()));
    } catch { if (request.isCurrent() && sequence === loadSequence.current) clear("unavailable"); }
    finally { if (request.isCurrent() && sequence === loadSequence.current) setLoading(false); request.release(); }
  }, [key, begin, endpoint, sessionId, headers, accessError, clear]);

  useEffect(() => {
    clear("checking"); busy.current = false; page.current = 0;
    if (!key) return;
    void load(0);
    const timer = setInterval(() => void load(), 10_000);
    const focus = () => { if (document.visibilityState === "visible") void load(); };
    window.addEventListener("focus", focus);
    return () => { clearInterval(timer); window.removeEventListener("focus", focus); localFence.invalidate(); };
  }, [key, load, clear, localFence]);
  useEffect(() => { if (key) void load(); }, [refreshKey, key, load]);
  useEffect(() => {
    if (!collection?.create_access.expires_at) return;
    const timer = setTimeout(() => clear("unverified"), Math.max(0, Date.parse(collection.create_access.expires_at) - Date.now()));
    return () => clearTimeout(timer);
  }, [collection?.create_access.expires_at, clear]);
  useEffect(() => {
    const url = new URL(window.location.href), outcome = url.searchParams.get("resident_create");
    if (!outcome) return;
    if (outcome === "cancelled" || outcome === "error") setNotice(outcome);
    url.searchParams.delete("resident_create");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  }, []);

  const start = async () => {
    if (!key || busy.current) return;
    clear("redirecting"); setNotice(null); busy.current = true;
    const request = begin();
    try {
      const response = await fetch(`${endpoint}/create/start`, { method: "POST", credentials: "include", headers: headers(), signal: request.signal, body: JSON.stringify({ session_id: sessionId }) });
      const data = await response.json().catch(() => null);
      if (!request.isCurrent()) return;
      if (!response.ok || typeof data?.authorize_url !== "string") { if (!accessError(response.status, data?.detail?.code)) clear("unavailable"); setNotice("error"); return; }
      const target = new URL(data.authorize_url);
      if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) throw new Error("Invalid authorization destination");
      window.location.assign(target.href);
    } catch { if (request.isCurrent()) { clear("unavailable"); setNotice("error"); } }
    finally { if (request.isCurrent()) busy.current = false; request.release(); }
  };

  const mutate = async (draft: ResidentDraft, operation: "edit" | "cancel" | "confirm" | "recover", fields?: ResidentDraftFields) => {
    if (!key || !active || busy.current) return;
    busy.current = true; localFence.invalidate(); loadSequence.current++; setBusyDraftId(draft.draft_id); setNotice(null);
    const request = begin();
    const uncertainOperation = operation === "confirm" || operation === "recover";
    const path = `${endpoint}/maintenance/drafts/${encodeURIComponent(draft.draft_id)}${uncertainOperation ? `/${operation}` : ""}`;
    const body = operation === "confirm" ? { session_id: sessionId, revision: draft.revision, confirmation_nonce: draft.confirmation_nonce }
      : operation === "recover" ? { session_id: sessionId }
      : { session_id: sessionId, action: operation, revision: draft.revision, ...(fields ? { fields } : {}) };
    try {
      const response = await fetch(path, { method: uncertainOperation ? "POST" : "PATCH", credentials: "include", headers: headers(), signal: AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]), body: JSON.stringify(body) });
      const data = await response.json().catch(() => null);
      if (!request.isCurrent()) return;
      if (!response.ok) {
        if (accessError(response.status, data?.detail?.code)) return;
        if (response.status === 409) { setNotice("conflict"); return; }
        if (uncertainOperation && response.status >= 500) throw new Error("Uncertain submission");
        setNotice("save_failed"); return;
      }
      if (!isResidentDraft(data) || data.draft_id !== draft.draft_id) throw new Error("Invalid draft response");
      setSnapshot(previous => previous?.key === key ? { key, data: { ...previous.data, items: previous.data.items.map(item => item.draft_id === draft.draft_id ? data : item) } } : previous);
      uncertainDrafts.current.delete(draft.draft_id);
      setUncertain(new Set(uncertainDrafts.current.keys()));
    } catch {
      if (request.isCurrent()) {
        setNotice(uncertainOperation ? "outcome_unknown" : "save_failed");
        if (uncertainOperation) {
          uncertainDrafts.current.set(draft.draft_id, draft);
          setUncertain(new Set(uncertainDrafts.current.keys()));
        }
      }
    } finally {
      if (request.isCurrent()) { busy.current = false; setBusyDraftId(null); void load(); }
      request.release();
    }
  };
  return { phase, collection, active, loading, busyDraftId, notice, uncertain, start, load, mutate };
}
