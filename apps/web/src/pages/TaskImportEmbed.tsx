import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Tasks from "./Tasks";
import { useAuthStore } from "../stores/auth";
import { readEmbeddedTaskDrafts, taskDraftOriginAllowed } from "../lib/taskDraftHandoff";
import type { SelectedTextTaskDraft } from "../lib/selectionActions";
import type { Task } from "../lib/types";
import { applyThemePreference, getStoredThemePreference } from "../lib/theme";
import "./TaskImportEmbed.css";

/** A native task composer on Manor's own origin. The companion can supply
 * text, but only the signed-in person can choose scope/assignee and submit. */
export default function TaskImportEmbed() {
  const [drafts, setDrafts] = useState<SelectedTextTaskDraft[] | null>(null);
  const [error, setError] = useState("");
  const [closed, setClosed] = useState(false);
  const completed = useRef(new Set<number>());
  const { user, isLoading, checkAuth } = useAuthStore();
  const channel = useMemo(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const nonce = params.get("nonce") || "";
    const origin = params.get("origin") || "";
    return window.parent !== window && /^[a-f0-9-]{36}$/.test(nonce)
      && taskDraftOriginAllowed(origin, window.location.origin) ? { nonce, origin, theme: params.get("theme") } : null;
  }, []);
  const applyCompanionTheme = useCallback((theme: unknown) => {
    if (!channel || (theme !== "dark" && theme !== "light")) return;
    // This frame follows its host without changing the person's Manor preference.
    document.documentElement.dataset.companionTaskTheme = theme;
    applyThemePreference(theme === "dark" ? "dark" : "white");
  }, [channel]);
  useLayoutEffect(() => {
    applyCompanionTheme(channel?.theme);
    return () => {
      delete document.documentElement.dataset.companionTaskTheme;
      applyThemePreference(getStoredThemePreference());
    };
  }, [channel, applyCompanionTheme]);
  const send = useCallback((data: Record<string, unknown>) => {
    if (channel) window.parent.postMessage({ ...data, nonce: channel.nonce }, channel.origin);
  }, [channel]);

  useEffect(() => {
    const ended = () => useAuthStore.setState({ user: null, token: null, isLoading: false });
    window.addEventListener("manor:companion-session-ended", ended);
    return () => window.removeEventListener("manor:companion-session-ended", ended);
  }, []);

  useEffect(() => {
    if (!channel) return;
    let received = false;
    const receive = (event: MessageEvent) => {
      if (event.source !== window.parent || event.origin !== channel.origin || event.data?.nonce !== channel.nonce) return;
      if (event.data?.type === "manor:task-theme:v1") {
        applyCompanionTheme(event.data.theme);
        return;
      }
      if (received) return;
      try {
        const incoming = readEmbeddedTaskDrafts(event, window.parent, channel.nonce, channel.origin, window.location.origin);
        if (!incoming) return;
        applyCompanionTheme(event.data.theme);
        received = true;
        setDrafts(incoming);
        send({ type: "manor:task-drafts:received" });
        void checkAuth();
      } catch { setError("Could not load these drafts. Close this form and try again."); }
    };
    const ready = () => { if (!received) send({ type: "manor:task-drafts:ready" }); };
    window.addEventListener("message", receive);
    ready();
    const interval = window.setInterval(ready, 500);
    const timeout = window.setTimeout(() => {
      window.clearInterval(interval);
      if (!received) setError("The connection timed out. Close this form and try again.");
    }, 30000);
    return () => { window.removeEventListener("message", receive); window.clearInterval(interval); window.clearTimeout(timeout); };
  }, [channel, checkAuth, send, applyCompanionTheme]);

  // If authentication expires between drafts, remount only the remaining
  // queue after sign-in. Already-created tasks must never be offered again.
  const queue = useMemo(() => drafts?.map((draft, index) => ({ draft, index })).filter(item => !completed.current.has(item.index)),
    [drafts, user?.id, user?.entity_id, isLoading]);
  const onCreated = useCallback((index: number, task: Task) => {
    const originalIndex = queue?.[index]?.index;
    if (originalIndex === undefined || completed.current.has(originalIndex)) return;
    completed.current.add(originalIndex);
    send({ type: "manor:task-drafts:created", index: originalIndex, task: { id: task.id, title: task.title } });
  }, [queue, send]);
  const onBusy = useCallback((busy: boolean) => send({ type: "manor:task-drafts:busy", busy }), [send]);
  const onClose = useCallback(() => { setClosed(true); send({ type: "manor:task-drafts:closed" }); }, [send]);
  const embeddedCreate = useMemo(() => queue ? { drafts: queue.map(item => item.draft), onCreated, onBusy, onClose } : undefined, [queue, onCreated, onBusy, onClose]);
  useEffect(() => { if (!user) onBusy(false); }, [user, onBusy]);

  if (channel && !error && drafts && user && !isLoading && !closed) {
    return <Tasks key={`${user.id}:${user.entity_id}`} embeddedCreate={embeddedCreate} />;
  }
  return <main className="companion-task-message">
    <p role={error || !channel ? "alert" : "status"}>{!channel ? "Open this form from a connected app."
      : error || (closed ? "Done. You can close this form." : !drafts || isLoading ? "Loading Manor…" : "Sign in to Manor to create this task.")}</p>
    {channel && drafts && !user && !isLoading && !error && <>
      <a href="/login" target="_blank" rel="noopener noreferrer">Sign in to Manor</a>
      <button onClick={() => void checkAuth()}>I’ve signed in — continue</button>
    </>}
  </main>;
}
