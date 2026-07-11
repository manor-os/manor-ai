/**
 * Persistent chat stream store — survives close/reopen and supports more than
 * one active chat session. Each conversation owns its own messages, streaming
 * flag, and AbortController so users can start a new session while another
 * request continues in the background.
 */
import { create } from "zustand";
import { processSSEStream } from "../lib/chatStream";
import type { ChatMessage, SetMessages, SetConvId } from "../lib/chatStream";

export type { ChatMessage };

export interface ChatStreamSession {
  key: string;
  convId?: string;
  streaming: boolean;
  messages: ChatMessage[];
  controllerKey?: string;
}

interface ChatStreamState {
  /** Back-compat snapshot for callers that have not moved to per-session selectors. */
  streaming: boolean;
  streamingConvId: string | undefined;
  messages: ChatMessage[];

  sessions: Record<string, ChatStreamSession>;
  sessionAliases: Record<string, string>;
  latestSessionKey: string | undefined;

  createDraftSession: () => string;
  getSessionKeyForConversation: (convId: string | undefined) => string | undefined;
  startStream: (
    fetchFn: () => Promise<Response>,
    convId: string | undefined,
    messages: ChatMessage[],
    onConvId: (id: string) => void,
    sessionKey?: string,
  ) => Promise<string>;
  stopStream: (sessionKey?: string) => void;
  cancelPendingHitlRequests: (sessionKey?: string, resolution?: string) => void;
  setSessionMessages: (
    sessionKey: string,
    updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[]),
  ) => void;
  setMessages: (updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => void;
  resetSession: (sessionKey?: string) => void;
  reset: () => void;
}

type ControllerRecord = { ac: AbortController; runId: number; sessionKey: string };

const _controllers = new Map<string, ControllerRecord>();
let _streamRunSeq = 0;
let _draftSeq = 0;

function makeDraftKey() {
  _draftSeq += 1;
  return `draft:${Date.now()}:${_draftSeq}`;
}

function closePendingHitlRequests(messages: ChatMessage[], resolution = "cancelled"): ChatMessage[] {
  return messages.map((msg) => {
    if (!msg.hitl_requests?.some((hitl) => !hitl.resolved)) return msg;
    return {
      ...msg,
      hitl_requests: msg.hitl_requests.map((hitl) =>
        hitl.resolved ? hitl : { ...hitl, resolved: true, resolution },
      ),
    };
  });
}

function activeSnapshot(state: ChatStreamState, key?: string) {
  const latestKey = resolveSessionKey(state, key || state.latestSessionKey);
  const latest = latestKey ? state.sessions[latestKey] : undefined;
  return {
    latestSessionKey: latestKey,
    streaming: Boolean(latest?.streaming),
    streamingConvId: latest?.convId,
    messages: latest?.messages || [],
  };
}

function resolveSessionKey(state: Pick<ChatStreamState, "sessions" | "sessionAliases">, key?: string) {
  if (!key) return undefined;
  let resolved = key;
  const seen = new Set<string>();
  while (!state.sessions[resolved] && state.sessionAliases[resolved] && !seen.has(resolved)) {
    seen.add(resolved);
    resolved = state.sessionAliases[resolved];
  }
  return resolved;
}

function abortSessionController(session?: ChatStreamSession) {
  if (!session) return;
  const controllerKey = session.controllerKey || session.key;
  const controller = _controllers.get(controllerKey);
  controller?.ac.abort();
  _controllers.delete(controllerKey);
}

export const useChatStreamStore = create<ChatStreamState>((set, get) => ({
  streaming: false,
  streamingConvId: undefined,
  messages: [],
  sessions: {},
  sessionAliases: {},
  latestSessionKey: undefined,

  createDraftSession: () => {
    const key = makeDraftKey();
    set((state) => {
      const sessions = {
        ...state.sessions,
        [key]: { key, streaming: false, messages: [] },
      };
      return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
    });
    return key;
  },

  getSessionKeyForConversation: (convId) => {
    if (!convId) return undefined;
    const state = get();
    if (state.sessions[convId]) return convId;
    const alias = state.sessionAliases[convId];
    if (alias && state.sessions[alias]) return alias;
    return Object.values(state.sessions).find((s) => s.convId === convId)?.key;
  },

  setSessionMessages: (sessionKey, updater) => {
    set((state) => {
      const resolvedKey = resolveSessionKey(state, sessionKey) || sessionKey;
      const existing = state.sessions[resolvedKey] || {
        key: resolvedKey,
        convId: resolvedKey.startsWith("draft:") ? undefined : resolvedKey,
        streaming: false,
        messages: [],
      };
      const messages =
        typeof updater === "function" ? updater(existing.messages) : updater;
      const sessions = {
        ...state.sessions,
        [resolvedKey]: { ...existing, messages },
      };
      return { sessions, ...activeSnapshot({ ...state, sessions }, resolvedKey) };
    });
  },

  // Back-compatible writer: updates the latest visible session.
  setMessages: (updater) => {
    const key = get().latestSessionKey || get().createDraftSession();
    get().setSessionMessages(key, updater);
  },

  startStream: async (fetchFn, convId, initialMessages, onConvId, providedSessionKey) => {
    const initialKey = providedSessionKey || convId || makeDraftKey();
    const controllerKey = initialKey;
    abortSessionController(get().sessions[initialKey]);

    const ac = new AbortController();
    const runId = ++_streamRunSeq;
    _controllers.set(controllerKey, { ac, runId, sessionKey: initialKey });

    let liveKey = initialKey;
    set((state) => {
      const sessions = {
        ...state.sessions,
        [initialKey]: {
          key: initialKey,
          convId,
          streaming: true,
          messages: initialMessages,
          controllerKey,
        },
      };
      return { sessions, ...activeSnapshot({ ...state, sessions }, initialKey) };
    });

    const isCurrentRun = () => _controllers.get(controllerKey)?.runId === runId;

    const setMessages: SetMessages = (updater) => {
      if (!isCurrentRun()) return;
      set((state) => {
        const existing = state.sessions[liveKey];
        if (!existing) return {};
        const messages =
          typeof updater === "function" ? updater(existing.messages) : updater;
        const sessions = {
          ...state.sessions,
          [liveKey]: { ...existing, messages },
        };
        return { sessions, ...activeSnapshot({ ...state, sessions }, liveKey) };
      });
    };

    const setConvId: SetConvId = (id) => {
      if (!isCurrentRun()) return;
      const newId = typeof id === "function" ? id(get().sessions[liveKey]?.convId) : id;
      if (!newId) return;

      set((state) => {
        const current = state.sessions[liveKey];
        if (!current) return {};
        const oldKey = liveKey;
        const nextKey = current.key.startsWith("draft:") ? newId : current.key;
        liveKey = nextKey;

        const sessions = { ...state.sessions };
        const sessionAliases = { ...state.sessionAliases };
        delete sessions[oldKey];
        if (oldKey !== nextKey) sessionAliases[oldKey] = nextKey;
        sessions[nextKey] = {
          ...current,
          key: nextKey,
          convId: newId,
          controllerKey,
        };
        const record = _controllers.get(controllerKey);
        if (record) record.sessionKey = nextKey;
        return { sessionAliases, sessions, ...activeSnapshot({ ...state, sessionAliases, sessions }, nextKey) };
      });
      onConvId(newId);
    };

    try {
      const response = await fetchFn();
      await processSSEStream(
        response,
        { setMessages, setCurrentConvId: setConvId },
        convId,
        ac.signal,
      );
    } catch (err) {
      if (isCurrentRun() && (err as Error)?.name !== "AbortError") {
        const message = (err as Error)?.message || "Failed to get response. Please try again.";
        set((state) => {
          const existing = state.sessions[liveKey];
          if (!existing) return {};
          const messages = [...existing.messages];
          const last = messages[messages.length - 1];
          if (last?.role === "assistant" && !last.content) {
            messages[messages.length - 1] = { ...last, content: `Error: ${message}` };
          }
          const sessions = {
            ...state.sessions,
            [liveKey]: { ...existing, messages },
          };
          return { sessions, ...activeSnapshot({ ...state, sessions }, liveKey) };
        });
      }
    } finally {
      if (isCurrentRun()) {
        _controllers.delete(controllerKey);
        set((state) => {
          const existing = state.sessions[liveKey];
          if (!existing) return {};
          const sessions = {
            ...state.sessions,
            [liveKey]: { ...existing, streaming: false, controllerKey: undefined },
          };
          return { sessions, ...activeSnapshot({ ...state, sessions }, liveKey) };
        });
      }
    }

    return liveKey;
  },

  stopStream: (sessionKey) => {
    const key = resolveSessionKey(get(), sessionKey || get().latestSessionKey);
    if (!key) return;
    const session = get().sessions[key];
    abortSessionController(session);
    set((state) => {
      const existing = state.sessions[key];
      if (!existing) return {};
      const sessions = {
        ...state.sessions,
        [key]: {
          ...existing,
          streaming: false,
          controllerKey: undefined,
          messages: closePendingHitlRequests(existing.messages),
        },
      };
      return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
    });
  },

  cancelPendingHitlRequests: (sessionKey, resolution = "cancelled") => {
    const key = resolveSessionKey(get(), sessionKey || get().latestSessionKey);
    if (!key) return;
    set((state) => {
      const existing = state.sessions[key];
      if (!existing) return {};
      const sessions = {
        ...state.sessions,
        [key]: {
          ...existing,
          messages: closePendingHitlRequests(existing.messages, resolution),
        },
      };
      return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
    });
  },

  resetSession: (sessionKey) => {
    const key = resolveSessionKey(get(), sessionKey || get().latestSessionKey);
    if (!key) return;
    abortSessionController(get().sessions[key]);
    set((state) => {
      const sessions = { ...state.sessions };
      delete sessions[key];
      const sessionAliases = Object.fromEntries(
        Object.entries(state.sessionAliases).filter(([from, to]) => from !== key && to !== key),
      );
      const fallbackKey = state.latestSessionKey === key ? undefined : state.latestSessionKey;
      return { sessionAliases, sessions, ...activeSnapshot({ ...state, sessionAliases, sessions }, fallbackKey) };
    });
  },

  reset: () => {
    for (const controller of _controllers.values()) {
      controller.ac.abort();
    }
    _controllers.clear();
    set({
      streaming: false,
      streamingConvId: undefined,
      messages: [],
      sessions: {},
      sessionAliases: {},
      latestSessionKey: undefined,
    });
  },
}));
