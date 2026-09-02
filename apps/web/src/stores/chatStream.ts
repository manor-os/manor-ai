/**
 * Persistent chat stream store — survives close/reopen and supports more than
 * one active chat session. Each conversation owns its own messages, streaming
 * flag, and AbortController so users can start a new session while another
 * request continues in the background.
 */
import { create } from "zustand";
import { processSSEStream, settlePendingAssistantProcess } from "../lib/chatStream";
import type {
  ChatMessage,
  RuntimeQueueState,
  RuntimeStreamState,
  SetMessages,
  SetConvId,
  StreamProcessResult,
} from "../lib/chatStream";
import { getAuthToken } from "../lib/authToken";

export type { ChatMessage };

export enum ChatStreamCompletionStatus {
  Succeeded = "succeeded",
  Failed = "failed",
  Cancelled = "cancelled",
}

export interface ChatStreamCompletionDetails {
  /** The server supplied durable evidence that it accepted this turn. */
  serverAccepted: boolean;
  /** A determinate outcome was observed; persistence-only failure stays retry-safe. */
  terminalObserved: boolean;
  messageId?: string;
  runtimeRunId?: string;
}

function mergeCompletionDetails(
  current: ChatStreamCompletionDetails,
  result: StreamProcessResult,
): ChatStreamCompletionDetails {
  const legacyMessageAcceptance = result.persisted === undefined
    && result.error?.persisted === undefined
    && Boolean(result.messageId || result.error?.messageId);
  const explicitTerminalFailure = result.error?.persisted === true
    || (Boolean(result.stopReason) && result.persisted !== false)
    || result.runtimeStatus === "failed"
    || result.runtimeStatus === "cancelled";
  const serverAccepted = current.serverAccepted
    || result.persisted === true
    || result.error?.persisted === true
    || legacyMessageAcceptance
    || Boolean(result.runtimeRunId);
  const determinateStreamEnd = result.streamEnded === true
    && (result.persisted !== false || !serverAccepted);
  return {
    serverAccepted,
    terminalObserved: current.terminalObserved
      || determinateStreamEnd
      || explicitTerminalFailure,
    messageId: result.messageId || result.error?.messageId || current.messageId,
    runtimeRunId: result.runtimeRunId || current.runtimeRunId,
  };
}

export interface ChatStreamSession {
  key: string;
  convId?: string;
  streaming: boolean;
  messages: ChatMessage[];
  controllerKey?: string;
  runtimeRunId?: string;
  runtimeStatus?: string;
  runtimeQueue?: RuntimeQueueState;
  runtimePollAfterSeconds?: number;
  runtimeLastEventId?: string;
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
    fetchFn: (signal: AbortSignal) => Promise<Response>,
    convId: string | undefined,
    messages: ChatMessage[],
    onConvId: (id: string) => void,
    sessionKey?: string,
    onCompletion?: (
      status: ChatStreamCompletionStatus,
      details: ChatStreamCompletionDetails,
    ) => void,
  ) => Promise<string>;
  /** Resolves after cancellation is durable or the runtime is already terminal. */
  stopStream: (sessionKey?: string) => Promise<boolean>;
  cancelPendingHitlRequests: (sessionKey?: string, resolution?: string) => void;
  setSessionMessages: (
    sessionKey: string,
    updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[]),
  ) => void;
  setMessages: (updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => void;
  resetSession: (sessionKey?: string) => void;
  reset: () => void;
}

type ControllerRecord = {
  ac: AbortController;
  runId: number;
  sessionKey: string;
};

type CancelAttemptRecord = {
  id: number;
  ac: AbortController;
  timeoutId: ReturnType<typeof setTimeout>;
  promise: Promise<boolean>;
  resolve: (stopped: boolean) => void;
};

const _controllers = new Map<string, ControllerRecord>();
const _cancelAttempts = new Map<string, CancelAttemptRecord>();
let _streamRunSeq = 0;
let _cancelAttemptSeq = 0;
let _draftSeq = 0;
const CANCEL_REQUEST_TIMEOUT_MS = 15_000;

const _locallyStreamedConversations = new Set<string>();
const LOCAL_STOPPED_STREAM_SUPPRESSION_MS = 5 * 60 * 1000;
type LocallyStoppedStream = { messageId?: string; until: number };
const _locallyStoppedStreams = new Map<string, LocallyStoppedStream>();

export function hasLocallyStreamedConversation(convId: string | undefined): boolean {
  return Boolean(convId && _locallyStreamedConversations.has(convId));
}

function latestAssistantMessageId(messages: ChatMessage[] | undefined): string | undefined {
  const latestAssistant = [...(messages || [])]
    .reverse()
    .find((message) => message.role === "assistant");
  const id = String(latestAssistant?.id || "").trim();
  return id || undefined;
}

function pruneLocallyStoppedStreams(now = Date.now()) {
  for (const [conversationId, record] of _locallyStoppedStreams) {
    if (record.until <= now) _locallyStoppedStreams.delete(conversationId);
  }
}

function markConversationStreamLocallyStopped(
  convId: string | undefined,
  messageId: string | undefined,
) {
  if (!convId) return;
  pruneLocallyStoppedStreams();
  _locallyStoppedStreams.set(convId, {
    messageId,
    until: Date.now() + LOCAL_STOPPED_STREAM_SUPPRESSION_MS,
  });
}

export function shouldIgnoreLocallyStoppedStreamUpdate(
  convId: string | undefined,
  messageId?: unknown,
  status?: unknown,
): boolean {
  if (!convId) return false;
  pruneLocallyStoppedStreams();
  const stopped = _locallyStoppedStreams.get(convId);
  if (!stopped) return false;
  const incomingMessageId = typeof messageId === "string" ? messageId.trim() : "";
  if (stopped.messageId && incomingMessageId && stopped.messageId !== incomingMessageId) {
    return false;
  }
  const normalizedStatus = String(status || "").toLowerCase();
  if (
    normalizedStatus === "interrupted" ||
    normalizedStatus === "cancelled" ||
    normalizedStatus === "canceled" ||
    normalizedStatus === "error" ||
    normalizedStatus === "failed"
  ) {
    _locallyStoppedStreams.delete(convId);
    return false;
  }
  return true;
}

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
  if (session.convId) _locallyStreamedConversations.delete(session.convId);
}

function consumeCancelAttempt(
  sessionKey: string,
  attemptId: number,
): CancelAttemptRecord | undefined {
  const attempt = _cancelAttempts.get(sessionKey);
  if (!attempt || attempt.id !== attemptId) return undefined;
  clearTimeout(attempt.timeoutId);
  _cancelAttempts.delete(sessionKey);
  return attempt;
}

function abortCancelAttempt(sessionKey: string) {
  const attempt = _cancelAttempts.get(sessionKey);
  if (!attempt) return;
  _cancelAttempts.delete(sessionKey);
  clearTimeout(attempt.timeoutId);
  attempt.ac.abort();
  attempt.resolve(false);
}

const TERMINAL_RUNTIME_STATUSES = new Set(["completed", "failed", "cancelled"]);

class IncompleteChatStreamError extends Error {
  constructor() {
    super("Chat stream ended before completion");
    this.name = "IncompleteChatStreamError";
  }
}

function runtimeHeaders(): HeadersInit {
  const token = getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function runtimeStateFromStatus(payload: Record<string, any>): RuntimeStreamState {
  const rawQueue = payload.queue && typeof payload.queue === "object" ? payload.queue : undefined;
  const pollAfterSeconds = Math.max(
    1,
    Number(rawQueue?.poll_after_seconds || payload.poll_after_seconds || 5),
  );
  return {
    runId: String(payload.id || payload.run_id || ""),
    status: String(payload.status || "queued"),
    pollAfterSeconds,
    queue: rawQueue?.ticket
      ? {
          ticket: String(rawQueue.ticket),
          position: rawQueue.position == null ? undefined : Number(rawQueue.position),
          etaSeconds: rawQueue.eta_seconds == null ? undefined : Number(rawQueue.eta_seconds),
          pollAfterSeconds,
          deadlineAt: rawQueue.deadline_at == null ? undefined : String(rawQueue.deadline_at),
        }
      : undefined,
  };
}

async function fetchRuntimeStatus(runId: string, signal: AbortSignal): Promise<RuntimeStreamState> {
  const response = await fetch(`/api/v1/chat/runs/${encodeURIComponent(runId)}`, {
    headers: runtimeHeaders(),
    signal,
  });
  if (!response.ok) throw new Error(`Runtime status failed (${response.status})`);
  return runtimeStateFromStatus(await response.json());
}

function waitForRuntimePoll(seconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, Math.max(1, seconds) * 1000);
    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
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

  startStream: async (
    fetchFn,
    convId,
    initialMessages,
    onConvId,
    providedSessionKey,
    onCompletion,
  ) => {
    const initialKey = providedSessionKey || convId || makeDraftKey();
    const controllerKey = initialKey;
    abortCancelAttempt(initialKey);
    abortSessionController(get().sessions[initialKey]);

    const ac = new AbortController();
    const runId = ++_streamRunSeq;
    _controllers.set(controllerKey, { ac, runId, sessionKey: initialKey });

    let liveKey = initialKey;
    let completionStatus = ChatStreamCompletionStatus.Succeeded;
    let completionDetails: ChatStreamCompletionDetails = {
      serverAccepted: false,
      terminalObserved: false,
    };
    let streamEndSeen = false;
    if (convId) _locallyStreamedConversations.add(convId);
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
      if (get().sessions[liveKey]?.convId === newId) return;
      _locallyStreamedConversations.add(newId);

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

    const applyRuntimeState = (runtimeState: RuntimeStreamState) => {
      if (!isCurrentRun()) return;
      set((state) => {
        const existing = state.sessions[liveKey];
        if (!existing) return {};
        const runtimeStatus = (
          existing.runtimeStatus === "cancel_requested"
          && !TERMINAL_RUNTIME_STATUSES.has(runtimeState.status)
        ) ? "cancel_requested" : runtimeState.status;
        const sessions = {
          ...state.sessions,
          [liveKey]: {
            ...existing,
            runtimeRunId: runtimeState.runId,
            runtimeStatus,
            runtimeQueue:
              runtimeStatus === "waiting_resource" ? runtimeState.queue : undefined,
            runtimePollAfterSeconds: runtimeState.pollAfterSeconds,
            runtimeLastEventId: runtimeState.lastEventId || existing.runtimeLastEventId,
          },
        };
        return { sessions, ...activeSnapshot({ ...state, sessions }, liveKey) };
      });
    };

    const processResponse = async (response: Response) => {
      const result = await processSSEStream(
        response,
        {
          setMessages,
          setCurrentConvId: setConvId,
          onRuntimeState: applyRuntimeState,
        },
        get().sessions[liveKey]?.convId || convId,
        ac.signal,
      );
      completionDetails = mergeCompletionDetails(completionDetails, result);
      streamEndSeen = streamEndSeen || result.streamEnded === true;
      if (result.streamEnded && result.persisted === false) {
        completionStatus = ChatStreamCompletionStatus.Failed;
      }
      if (result.runtimeRunId) {
        applyRuntimeState({
          runId: result.runtimeRunId,
          status: result.runtimeStatus || "running",
          queue: result.runtimeQueue,
          pollAfterSeconds: result.pollAfterSeconds || 5,
          lastEventId: result.lastEventId,
        });
      }
      if (result.error) {
        completionStatus = ChatStreamCompletionStatus.Failed;
        throw new Error(result.error.message);
      }
      if (result.stopReason) {
        completionStatus = ChatStreamCompletionStatus.Failed;
        throw new Error(result.stopReason);
      }
      if (!streamEndSeen && !completionDetails.runtimeRunId) {
        throw new IncompleteChatStreamError();
      }
      return result;
    };

    const recoverDurableStream = async () => {
      while (isCurrentRun() && !ac.signal.aborted) {
        const session = get().sessions[liveKey];
        const durableRunId = session?.runtimeRunId;
        const currentStatus = session?.runtimeStatus || "";
        if (!durableRunId) return;
        if (TERMINAL_RUNTIME_STATUSES.has(currentStatus)) {
          if (streamEndSeen || currentStatus !== "completed") return;
        } else {
          await waitForRuntimePoll(session.runtimePollAfterSeconds || 5, ac.signal);
          let status: RuntimeStreamState;
          try {
            status = await fetchRuntimeStatus(durableRunId, ac.signal);
          } catch (error) {
            if ((error as Error)?.name === "AbortError") throw error;
            continue;
          }
          applyRuntimeState({ ...status, lastEventId: session.runtimeLastEventId });
          if (status.status === "waiting_resource" || status.status === "queued") continue;
        }

        const cursor = get().sessions[liveKey]?.runtimeLastEventId;
        const query = cursor ? `?after_id=${encodeURIComponent(cursor)}` : "";
        const response = await fetch(
          `/api/v1/chat/runs/${encodeURIComponent(durableRunId)}/events${query}`,
          { headers: runtimeHeaders(), signal: ac.signal },
        );
        if (!response.ok) throw new Error(`Runtime event replay failed (${response.status})`);
        await processResponse(response);
        if (streamEndSeen) return;
        const recoveredStatus = get().sessions[liveKey]?.runtimeStatus || currentStatus;
        if (TERMINAL_RUNTIME_STATUSES.has(recoveredStatus)) {
          if (recoveredStatus === "completed") throw new IncompleteChatStreamError();
          return;
        }
      }
    };

    try {
      const response = await fetchFn(ac.signal);
      const responseConversationId = response.headers.get("X-Conversation-ID")?.trim();
      if (responseConversationId) setConvId(responseConversationId);
      const responseRuntimeRunId = response.headers.get("X-Runtime-Run-ID")?.trim();
      if (responseRuntimeRunId) {
        completionDetails = {
          ...completionDetails,
          serverAccepted: true,
          runtimeRunId: responseRuntimeRunId,
        };
        applyRuntimeState({
          runId: responseRuntimeRunId,
          status: "queued",
          pollAfterSeconds: 5,
        });
      }
      const responseSurfaceEventId = response.headers
        .get("X-Response-Surface-Event-ID")
        ?.trim();
      if (responseSurfaceEventId) {
        completionDetails = { ...completionDetails, serverAccepted: true };
      }
      await processResponse(response);
      const session = get().sessions[liveKey];
      const runtimeStatus = session?.runtimeStatus || "";
      if (
        session?.runtimeRunId &&
        (
          !TERMINAL_RUNTIME_STATUSES.has(runtimeStatus)
          || (!streamEndSeen && runtimeStatus === "completed")
        ) &&
        !ac.signal.aborted
      ) {
        await recoverDurableStream();
      }
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        completionStatus = ChatStreamCompletionStatus.Cancelled;
      }
      const session = get().sessions[liveKey];
      if (
        isCurrentRun() &&
        (err as Error)?.name !== "AbortError" &&
        session?.runtimeRunId &&
        (err as Error)?.name !== "IncompleteChatStreamError" &&
        (
          !TERMINAL_RUNTIME_STATUSES.has(session.runtimeStatus || "")
          || (!streamEndSeen && session.runtimeStatus === "completed")
        )
      ) {
        try {
          await recoverDurableStream();
          return liveKey;
        } catch (recoveryError) {
          if ((recoveryError as Error)?.name === "AbortError") return liveKey;
        }
      }
      if (isCurrentRun() && (err as Error)?.name !== "AbortError") {
        completionStatus = ChatStreamCompletionStatus.Failed;
        const terminalError = err instanceof Error
          ? err
          : new Error("Failed to get response. Please try again.");
        const message = terminalError.message;
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
      const currentRun = isCurrentRun();
      const terminalRuntimeStatus = get().sessions[liveKey]?.runtimeStatus;
      if (
        terminalRuntimeStatus === "cancelled"
        || terminalRuntimeStatus === "failed"
      ) {
        completionDetails = {
          ...completionDetails,
          serverAccepted: true,
          terminalObserved: true,
          runtimeRunId:
            get().sessions[liveKey]?.runtimeRunId || completionDetails.runtimeRunId,
        };
      }
      if (terminalRuntimeStatus === "cancelled") {
        completionStatus = ChatStreamCompletionStatus.Cancelled;
      } else if (terminalRuntimeStatus === "failed") {
        completionStatus = ChatStreamCompletionStatus.Failed;
      } else if (terminalRuntimeStatus === "completed" && !streamEndSeen) {
        completionStatus = ChatStreamCompletionStatus.Failed;
      } else if (ac.signal.aborted || !currentRun) {
        completionStatus = ChatStreamCompletionStatus.Cancelled;
      }
      try {
        onCompletion?.(completionStatus, completionDetails);
      } catch {
        // Completion observers must never alter stream cleanup or session state.
      }
      if (currentRun) {
        _controllers.delete(controllerKey);
        const endedConvId = get().sessions[liveKey]?.convId;
        if (endedConvId) _locallyStreamedConversations.delete(endedConvId);
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
    if (!key) return Promise.resolve(true);
    const session = get().sessions[key];
    if (!session) return Promise.resolve(true);
    if (session.runtimeRunId && session.runtimeStatus === "cancel_requested") {
      // With an active attempt, callers join the same request. Without one,
      // cancel_requested came from a confirmed response or runtime event and
      // is already durable enough for a later close to continue.
      return _cancelAttempts.get(key)?.promise || Promise.resolve(true);
    }
    const finishObservedTerminal = (
      runtimeStatus: "completed" | "failed" | "cancelled",
    ) => {
      const current = get().sessions[key];
      if (!current) return;
      if (runtimeStatus === "cancelled") {
        markConversationStreamLocallyStopped(
          current.convId,
          latestAssistantMessageId(current.messages),
        );
      }
      set((state) => {
        const existing = state.sessions[key];
        if (!existing) return {};
        const sessions = {
          ...state.sessions,
          [key]: {
            ...existing,
            streaming: false,
            controllerKey: undefined,
            runtimeStatus: existing.runtimeRunId
              ? runtimeStatus
              : existing.runtimeStatus,
            runtimeQueue: undefined,
            messages: runtimeStatus === "cancelled"
              ? closePendingHitlRequests(settlePendingAssistantProcess(existing.messages))
              : existing.messages,
          },
        };
        return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
      });
      abortSessionController(current);
    };
    const finishStop = () => finishObservedTerminal("cancelled");
    if (session?.runtimeRunId) {
      const runtimeRunId = session.runtimeRunId;
      const previousRuntimeStatus = session.runtimeStatus;
      abortCancelAttempt(key);
      const cancelController = new AbortController();
      const cancelAttemptId = ++_cancelAttemptSeq;
      let resolveCancelAttempt!: (stopped: boolean) => void;
      const cancelPromise = new Promise<boolean>((resolve) => {
        resolveCancelAttempt = resolve;
      });
      const restorePreviousStatus = () => {
        set((state) => {
          const existing = state.sessions[key];
          if (
            !existing
            || existing.runtimeRunId !== runtimeRunId
            || existing.runtimeStatus !== "cancel_requested"
          ) {
            return {};
          }
          const sessions = {
            ...state.sessions,
            [key]: { ...existing, runtimeStatus: previousRuntimeStatus },
          };
          return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
        });
      };
      const cancellationNoLongerNeeded = () => {
        const current = get().sessions[key];
        return !current
          || !current.streaming
          || TERMINAL_RUNTIME_STATUSES.has(current.runtimeStatus || "");
      };
      const timeoutId = setTimeout(() => {
        const attempt = consumeCancelAttempt(key, cancelAttemptId);
        if (!attempt) return;
        cancelController.abort();
        const settled = cancellationNoLongerNeeded();
        if (!settled) restorePreviousStatus();
        attempt.resolve(settled);
      }, CANCEL_REQUEST_TIMEOUT_MS);
      _cancelAttempts.set(key, {
        id: cancelAttemptId,
        ac: cancelController,
        timeoutId,
        promise: cancelPromise,
        resolve: resolveCancelAttempt,
      });
      set((state) => {
        const existing = state.sessions[key];
        if (!existing || existing.runtimeRunId !== runtimeRunId) return {};
        const sessions = {
          ...state.sessions,
          [key]: { ...existing, runtimeStatus: "cancel_requested" },
        };
        return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
      });
      void fetch(`/api/v1/chat/runs/${encodeURIComponent(runtimeRunId)}/cancel`, {
        method: "POST",
        headers: runtimeHeaders(),
        signal: cancelController.signal,
      }).then(async (response) => {
        if (!response.ok) throw new Error(`Chat run cancellation failed (${response.status})`);
        const cancelledRun = runtimeStateFromStatus(await response.json());
        if (cancelledRun.runId !== runtimeRunId) {
          throw new Error("Chat run cancellation returned the wrong run");
        }
        const attempt = consumeCancelAttempt(key, cancelAttemptId);
        if (!attempt) return;
        if (cancelledRun.status !== "cancelled") {
          const cancellationAccepted = cancelledRun.status === "cancel_requested";
          const alreadyTerminal = TERMINAL_RUNTIME_STATUSES.has(cancelledRun.status);
          if (!cancellationAccepted && !alreadyTerminal) {
            restorePreviousStatus();
            attempt.resolve(false);
            return;
          }
          if (alreadyTerminal) {
            finishObservedTerminal(
              cancelledRun.status as "completed" | "failed" | "cancelled",
            );
            attempt.resolve(true);
            return;
          }
          set((state) => {
            const existing = state.sessions[key];
            if (!existing || existing.runtimeRunId !== runtimeRunId) return {};
            const sessions = {
              ...state.sessions,
              [key]: {
                ...existing,
                runtimeStatus: cancelledRun.status,
                runtimeQueue: undefined,
                runtimePollAfterSeconds: cancelledRun.pollAfterSeconds,
              },
            };
            return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
          });
          attempt.resolve(true);
          return;
        }
        const current = get().sessions[key];
        if (
          cancelledRun.status === "cancelled"
          && current?.streaming
          && current.runtimeRunId === runtimeRunId
        ) {
          finishStop();
        }
        attempt.resolve(true);
      }).catch(() => {
        const attempt = consumeCancelAttempt(key, cancelAttemptId);
        if (!attempt) return;
        const settled = cancellationNoLongerNeeded();
        if (!settled) restorePreviousStatus();
        attempt.resolve(settled);
      });
      return cancelPromise;
    } else {
      finishStop();
      return Promise.resolve(true);
    }
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
    const session = get().sessions[key];
    abortCancelAttempt(key);
    abortSessionController(session);
    if (session?.convId) _locallyStoppedStreams.delete(session.convId);
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
    for (const key of Array.from(_cancelAttempts.keys())) {
      abortCancelAttempt(key);
    }
    _controllers.clear();
    _locallyStreamedConversations.clear();
    _locallyStoppedStreams.clear();
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
