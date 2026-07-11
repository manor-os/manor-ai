import {
  useEffect,
  useRef,
  useCallback,
  useState,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";
import { useLocation } from "react-router-dom";
import { useAuthStore } from "../stores/auth";
import { captureClientError } from "./clientErrors";

interface UseWebSocketOptions {
  onNotification?: (data: Record<string, any>) => void;
  onTaskUpdate?: (data: Record<string, any>) => void;
  onJobUpdate?: (data: Record<string, any>) => void;
  onGoalProgress?: (data: Record<string, any>) => void;
  onWorkspaceChatMessage?: (data: Record<string, any>) => void;
  onConnect?: (data: Record<string, any>) => void;
  onTyping?: (data: Record<string, any>) => void;
  onPresence?: (data: Record<string, any>) => void;
  onVideoReady?: (data: Record<string, any>) => void;
}

type Subscriber = {
  connectedRef: MutableRefObject<boolean>;
  optionsRef: MutableRefObject<UseWebSocketOptions>;
  setUnreadCount: Dispatch<SetStateAction<number>>;
};

let sharedWs: WebSocket | null = null;
let sharedToken: string | null = null;
let sharedConnected = false;
let sharedHeartbeatInterval: number | undefined;
let sharedReconnectTimeout: number | undefined;
let sharedCloseTimeout: number | undefined;
let sharedReconnectAttempts = 0;
let latestUnreadCount = 0;
let nextSubscriberId = 1;
const subscribers = new Map<number, Subscriber>();

function isUsableJwt(token: string | null | undefined): token is string {
  if (!token) return false;
  const [, payload] = token.split(".");
  if (!payload) return true;
  try {
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const decoded = JSON.parse(atob(normalized));
    const exp = typeof decoded.exp === "number" ? decoded.exp : null;
    return exp === null || exp > Date.now() / 1000 + 30;
  } catch {
    // If this is an opaque token, let the server validate it.
    return true;
  }
}

function updateConnectedRefs(value: boolean) {
  sharedConnected = value;
  for (const sub of subscribers.values()) {
    sub.connectedRef.current = value;
  }
}

function clearSharedReconnect() {
  if (sharedReconnectTimeout) {
    clearTimeout(sharedReconnectTimeout);
    sharedReconnectTimeout = undefined;
  }
}

function clearSharedClose() {
  if (sharedCloseTimeout) {
    clearTimeout(sharedCloseTimeout);
    sharedCloseTimeout = undefined;
  }
}

function closeSharedSocket(manual = true) {
  clearSharedReconnect();
  if (sharedHeartbeatInterval) {
    clearInterval(sharedHeartbeatInterval);
    sharedHeartbeatInterval = undefined;
  }
  if (sharedWs) {
    (sharedWs as any)._manualClose = manual;
    sharedWs.close();
  }
  sharedWs = null;
  sharedToken = null;
  updateConnectedRefs(false);
}

function sendShared(payload: Record<string, any>) {
  if (sharedWs?.readyState === WebSocket.OPEN) {
    sharedWs.send(JSON.stringify(payload));
  }
}

function dispatchMessage(event: MessageEvent) {
  try {
    const msg = JSON.parse(event.data as string) as {
      event: string;
      data: Record<string, any>;
    };

    switch (msg.event) {
      case "connected":
        latestUnreadCount =
          (msg.data.unread_notifications as number | undefined) || 0;
        for (const sub of subscribers.values()) {
          sub.setUnreadCount(latestUnreadCount);
          sub.optionsRef.current.onConnect?.(msg.data);
        }
        break;
      case "notification":
        latestUnreadCount += 1;
        for (const sub of subscribers.values()) {
          sub.setUnreadCount((c) => c + 1);
          sub.optionsRef.current.onNotification?.(msg.data);
        }
        break;
      case "task_update":
        for (const sub of subscribers.values()) {
          sub.optionsRef.current.onTaskUpdate?.(msg.data);
        }
        break;
      case "job_update":
        for (const sub of subscribers.values()) {
          sub.optionsRef.current.onJobUpdate?.(msg.data);
        }
        break;
      case "goal_progress":
        for (const sub of subscribers.values()) {
          sub.optionsRef.current.onGoalProgress?.(msg.data);
        }
        break;
      case "workspace_chat_message":
        for (const sub of subscribers.values()) {
          sub.optionsRef.current.onWorkspaceChatMessage?.(msg.data);
        }
        break;
      case "typing":
        for (const sub of subscribers.values()) {
          sub.optionsRef.current.onTyping?.(msg.data);
        }
        break;
      case "presence_update":
        for (const sub of subscribers.values()) {
          sub.optionsRef.current.onPresence?.(msg.data);
        }
        break;
      case "video_ready":
        for (const sub of subscribers.values()) {
          sub.optionsRef.current.onVideoReady?.(msg.data);
        }
        break;
      case "ping":
        sendShared({ type: "ping" });
        break;
    }
  } catch (e) {
    captureClientError(e, {
      handled: true,
      mechanism: "websocket.parse",
      tags: { event: "message" },
      extra: {
        payload_preview: typeof event.data === "string"
          ? event.data.slice(0, 500)
          : "[non-string]",
      },
    });
    console.error("WS parse error:", e);
  }
}

function connectShared(token: string) {
  clearSharedClose();

  if (!isUsableJwt(token)) {
    closeSharedSocket();
    return;
  }
  if (
    sharedToken === token &&
    (sharedWs?.readyState === WebSocket.OPEN ||
      sharedWs?.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  closeSharedSocket();
  sharedToken = token;

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(
    `${protocol}//${window.location.host}/ws?token=${encodeURIComponent(token)}`,
  );

  ws.onopen = () => {
    sharedReconnectAttempts = 0;
    updateConnectedRefs(true);
    sharedHeartbeatInterval = window.setInterval(() => {
      sendShared({ type: "presence", status: "online" });
    }, 30000);
  };
  ws.onclose = () => {
    if (sharedHeartbeatInterval) {
      clearInterval(sharedHeartbeatInterval);
      sharedHeartbeatInterval = undefined;
    }
    if (sharedWs === ws) {
      sharedWs = null;
    }
    updateConnectedRefs(false);
    if ((ws as any)._manualClose) return;
    if (subscribers.size === 0) return;

    const latestToken = useAuthStore.getState().token;
    if (!isUsableJwt(latestToken) || latestToken !== token) return;

    const delay = Math.min(
      30_000,
      1000 * 2 ** Math.min(sharedReconnectAttempts, 5),
    );
    sharedReconnectAttempts += 1;
    sharedReconnectTimeout = window.setTimeout(() => connectShared(token), delay);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = dispatchMessage;

  sharedWs = ws;
}

function subscribeToSharedWebSocket(token: string, subscriber: Subscriber): number {
  clearSharedClose();
  const id = nextSubscriberId++;
  subscriber.connectedRef.current = sharedConnected;
  subscriber.setUnreadCount(latestUnreadCount);
  subscribers.set(id, subscriber);
  connectShared(token);
  return id;
}

function unsubscribeFromSharedWebSocket(id: number) {
  subscribers.delete(id);
  if (subscribers.size > 0) return;

  // React strict mode unmounts and remounts effects immediately in dev. A short
  // grace period keeps that check from opening duplicate socket handshakes.
  clearSharedClose();
  sharedCloseTimeout = window.setTimeout(() => {
    if (subscribers.size === 0) {
      closeSharedSocket();
    }
  }, 750);
}

export function useWebSocket(options: UseWebSocketOptions = {}) {
  const token = useAuthStore((s) => s.token);
  const connectedRef = useRef(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const subscriberIdRef = useRef<number | null>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const disconnect = useCallback(() => {
    if (subscriberIdRef.current !== null) {
      unsubscribeFromSharedWebSocket(subscriberIdRef.current);
      subscriberIdRef.current = null;
    }
    connectedRef.current = false;
  }, []);

  const markRead = useCallback((notificationId: string) => {
    sendShared({ type: "mark_read", notification_id: notificationId });
    latestUnreadCount = Math.max(0, latestUnreadCount - 1);
    setUnreadCount((c) => Math.max(0, c - 1));
  }, []);

  useEffect(() => {
    if (!isUsableJwt(token)) {
      disconnect();
      return disconnect;
    }
    subscriberIdRef.current = subscribeToSharedWebSocket(token, {
      connectedRef,
      optionsRef,
      setUnreadCount,
    });
    return disconnect;
  }, [token, disconnect]);

  const sendTyping = useCallback((conversationId: string) => {
    sendShared({ type: "typing", conversation_id: conversationId });
  }, []);

  const sendViewing = useCallback((resource: string) => {
    sendShared({ type: "presence", status: "online", viewing: resource });
  }, []);

  return {
    /** Non-reactive ref — reading `.current` gives the latest value without causing re-renders */
    connectedRef,
    unreadCount,
    markRead,
    setUnreadCount,
    sendTyping,
    sendViewing,
  };
}

/**
 * Streams `location.pathname` to the server over the shared WS as
 * presence frames. The server uses these to attribute per-page dwell
 * time in `user_page_view_logs` — admin analytics only, never used
 * for routing decisions, so a missed beat (offline, reconnect) just
 * means a small gap in the accumulated time, not a broken UI.
 *
 * Mount this once near the top of the app inside the Router context.
 */
export function usePageViewTracking() {
  const { sendViewing } = useWebSocket();
  const { pathname } = useLocation();
  useEffect(() => {
    if (!pathname) return;
    sendViewing(pathname);
  }, [pathname, sendViewing]);
}
