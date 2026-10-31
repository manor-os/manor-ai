import { useEffect, useRef } from "react";
import type { QueryClient } from "@tanstack/react-query";

/*
 * Realtime events are hints, not a log. The shared socket has no replay and no
 * cursor: anything published while it is down (backend restart, laptop sleep,
 * a frozen background tab, the API's Redis relay resubscribing) is gone. Every
 * realtime-driven view therefore needs one moment where it stops trusting the
 * hints and asks the API for the source of truth again. This module owns that
 * moment, so views subscribe to one signal instead of each guessing when a gap
 * happened.
 */

export const REALTIME_RESYNC_EVENT = "manor:realtime-resync";
/** Reconnect, server-signalled relay recovery and a long hidden gap often coincide. */
export const REALTIME_RESYNC_DEBOUNCE_MS = 400;
/** A shorter hidden stretch keeps its socket; only a long one risks a frozen tab. */
export const REALTIME_RESYNC_HIDDEN_GAP_MS = 2 * 60_000;
/** A token rotation swaps sockets in a handshake; a longer gap may have lost events. */
export const REALTIME_PLANNED_GAP_GRACE_MS = 5_000;

export type RealtimeResyncReason = "reconnect" | "server" | "resume";
export interface RealtimeResyncDetail {
  reasons: RealtimeResyncReason[];
}

let pendingReasons = new Set<RealtimeResyncReason>();
let pendingTimer: ReturnType<typeof setTimeout> | undefined;

/** Ask every realtime-driven view to reconcile with the API, coalesced across a burst. */
export function requestRealtimeResync(reason: RealtimeResyncReason): void {
  if (typeof window === "undefined") return;
  pendingReasons.add(reason);
  if (pendingTimer !== undefined) return;
  pendingTimer = setTimeout(() => {
    pendingTimer = undefined;
    const reasons = [...pendingReasons];
    pendingReasons = new Set();
    window.dispatchEvent(
      new CustomEvent<RealtimeResyncDetail>(REALTIME_RESYNC_EVENT, { detail: { reasons } }),
    );
  }, REALTIME_RESYNC_DEBOUNCE_MS);
}

export function onRealtimeResync(handler: (detail: RealtimeResyncDetail) => void): () => void {
  const listener = (event: Event) => {
    handler((event as CustomEvent<RealtimeResyncDetail>).detail || { reasons: [] });
  };
  window.addEventListener(REALTIME_RESYNC_EVENT, listener);
  return () => window.removeEventListener(REALTIME_RESYNC_EVENT, listener);
}

/** Run `handler` whenever realtime delivery may have had a gap. Always the latest closure. */
export function useRealtimeResync(handler: (detail: RealtimeResyncDetail) => void): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => onRealtimeResync((detail) => handlerRef.current(detail)), []);
}

/**
 * Decides whether a freshly established socket follows a delivery gap.
 *
 * `lost` records the moment an open socket stopped being open: unplanned for a
 * drop, planned when this client closed it on purpose (token rotation, last
 * subscriber leaving). The first connection of a page has nothing to catch up
 * on — its views load from the API as they mount.
 */
export function createConnectionGapTracker(now: () => number = Date.now) {
  let everConnected = false;
  let gapStartedAt: number | null = null;
  let gapUnplanned = false;
  return {
    lost(planned: boolean) {
      if (!everConnected) return;
      if (gapStartedAt === null) gapStartedAt = now();
      if (!planned) gapUnplanned = true;
    },
    /** Returns true when views must reconcile after this connection. */
    connected(): boolean {
      const resync = everConnected && gapStartedAt !== null
        && (gapUnplanned || now() - gapStartedAt > REALTIME_PLANNED_GAP_GRACE_MS);
      everConnected = true;
      gapStartedAt = null;
      gapUnplanned = false;
      return resync;
    },
  };
}

/**
 * Query families whose freshness depends on realtime pushes. Each one is
 * invalidated by a socket handler somewhere (AppLayout, WorkspaceChat,
 * ChatRooms, taskUpdateInvalidation), so after a gap it may be missing the
 * changes those pushes would have announced. Only active observers refetch.
 */
export const REALTIME_RESYNC_QUERY_ROOTS: ReadonlySet<string> = new Set([
  "conversations",
  "notifications",
  "task",
  "task-logs",
  "taskBoard",
  "tasks",
  "workspaces",
  "workspace-chat",
  "workspace-pending-attention",
  "workspace-attention-message",
  "workflow-run",
  "workflow-runs",
  "workspace-workflow-runs",
  "workspace-chat-workflow-run-family",
  "scheduled-jobs",
  "job-runs",
  "media-jobs",
  "chat-rooms",
  "chat-room",
  "chat-room-messages",
  "chat-room-tool-approvals",
  "chat-room-reply-approvals",
  "approvals",
  "approval-command",
  "dashboard-stats",
]);

export function invalidateRealtimeQueries(
  queryClient: Pick<QueryClient, "invalidateQueries">,
): Promise<void> {
  return queryClient.invalidateQueries(
    { predicate: (query) => REALTIME_RESYNC_QUERY_ROOTS.has(String(query.queryKey[0])) },
    // A refetch already in flight started after the gap; reuse it.
    { cancelRefetch: false },
  );
}
