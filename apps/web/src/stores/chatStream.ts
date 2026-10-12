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
  RuntimeHumanInputState,
  RuntimeQueueState,
  RuntimeStreamState,
  SetMessages,
  SetConvId,
  StreamProcessResult,
} from "../lib/chatStream";
import { runtimeHumanInputFromPayload } from "../lib/chatStream";
import { getAuthToken } from "../lib/authToken";
import { translateApiError } from "../lib/api";

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
  /** Monotonic local turn generation; retained after the stream settles. */
  streamGeneration?: number;
  messages: ChatMessage[];
  controllerKey?: string;
  runtimeRunId?: string;
  runtimeStatus?: string;
  runtimeQueue?: RuntimeQueueState;
  /** Set while the run is parked on a `request_input` question. */
  runtimeHumanInput?: RuntimeHumanInputState;
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
  transport: AbortController;
  acceptance: Promise<RuntimeStreamState | undefined>;
  accept: (state?: RuntimeStreamState) => void;
  runtimeState?: RuntimeStreamState;
  runId: number;
  sessionKey: string;
};

type CancelAttemptRecord = {
  id: number;
  ac: AbortController;
  promise: Promise<boolean>;
  resolve: (stopped: boolean) => void;
};

const _controllers = new Map<string, ControllerRecord>();
const _cancelAttempts = new Map<string, CancelAttemptRecord>();
let _streamRunSeq = 0;
let _cancelAttemptSeq = 0;
let _draftSeq = 0;
const CANCEL_REQUEST_TIMEOUT_MS = 3_000;

const _locallyStreamedConversations = new Set<string>();
const LOCAL_STOPPED_STREAM_SUPPRESSION_MS = 5 * 60 * 1000;
type LocallyStoppedStream = { messageId?: string; until: number };
const _locallyStoppedStreams = new Map<string, LocallyStoppedStream>();

export interface ChatHistoryStreamFence {
  conversationId: string;
  streamGeneration?: number;
}

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

/**
 * Fence a REST history read against a newer locally-owned stream.
 *
 * `streaming` alone is insufficient: a history request started before a fast
 * approval continuation can resolve after that continuation has already
 * finished. Retaining the generation makes the stale response recognizable
 * even after the active-stream flag returns to false.
 */
export function captureChatHistoryStreamFence(
  conversationId: string,
): ChatHistoryStreamFence {
  const state = useChatStreamStore.getState();
  const sessionKey = state.getSessionKeyForConversation(conversationId);
  return {
    conversationId,
    streamGeneration: sessionKey
      ? state.sessions[sessionKey]?.streamGeneration
      : undefined,
  };
}

export function chatHistoryStreamFenceIsCurrent(
  fence: ChatHistoryStreamFence,
): boolean {
  const state = useChatStreamStore.getState();
  const sessionKey = state.getSessionKeyForConversation(fence.conversationId);
  const session = sessionKey ? state.sessions[sessionKey] : undefined;
  return !session?.streaming
    && session?.streamGeneration === fence.streamGeneration;
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

function abortSessionController(session?: ChatStreamSession, preserveAcceptance = false) {
  if (!session) return;
  const controllerKey = session.controllerKey || session.key;
  const controller = _controllers.get(controllerKey);
  controller?.ac.abort();
  // A durable run can be admitted before fetch exposes its response headers.
  // Keep that receipt alive on Stop so it can still be cancelled by its ID.
  if (controller && (!preserveAcceptance || controller.runtimeState)) controller.transport.abort();
  _controllers.delete(controllerKey);
  if (session.convId) _locallyStreamedConversations.delete(session.convId);
}

function consumeCancelAttempt(
  sessionKey: string,
  attemptId: number,
): CancelAttemptRecord | undefined {
  const attempt = _cancelAttempts.get(sessionKey);
  if (!attempt || attempt.id !== attemptId) return undefined;
  _cancelAttempts.delete(sessionKey);
  return attempt;
}

function abortCancelAttempt(sessionKey: string) {
  const attempt = _cancelAttempts.get(sessionKey);
  if (!attempt) return;
  _cancelAttempts.delete(sessionKey);
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

const _runtimeRestorations = new Map<string, Promise<void>>();

/** Reattach to an owned turn after reload. Never replay the original POST. */
export function recoverRuntimeChatSession(
  convId: string,
  messages: ChatMessage[],
  sessionKey = convId,
): Promise<void> {
  const pending = _runtimeRestorations.get(sessionKey);
  if (pending) return pending;
  const session = useChatStreamStore.getState().sessions[sessionKey];
  if (session?.streaming || (session?.runtimeRunId && !TERMINAL_RUNTIME_STATUSES.has(session.runtimeStatus || ""))
    || shouldIgnoreLocallyStoppedStreamUpdate(convId)) {
    return Promise.resolve();
  }
  const token = getAuthToken();
  const fence = captureChatHistoryStreamFence(convId);
  const generation = session?.streamGeneration;
  const restoration = (async () => {
    const response = await fetch(`/api/v1/chat/conversations/${encodeURIComponent(convId)}/active-run`, {
      headers: runtimeHeaders(),
    });
    if (!response.ok) throw new Error(`Chat recovery failed (${response.status})`);
    const run = await response.json();
    const current = useChatStreamStore.getState().sessions[sessionKey];
    if (
      !run?.id || !run.assistant_message_id || run.conversation_id !== convId
      || TERMINAL_RUNTIME_STATUSES.has(run.status)
      || getAuthToken() !== token || !chatHistoryStreamFenceIsCurrent(fence)
      || current?.streamGeneration !== generation || current?.streaming
      || shouldIgnoreLocallyStoppedStreamUpdate(convId)
    ) return;
    // Replay rebuilds only the active assistant message. History stays intact.
    const restored: ChatMessage[] = [
      ...messages.filter((message) => message.id !== run.assistant_message_id),
      { id: run.assistant_message_id, role: "assistant", content: "" },
    ];
    void useChatStreamStore.getState().startStream(
      (signal) => fetch(`/api/v1/chat/runs/${encodeURIComponent(run.id)}/events`, {
        headers: runtimeHeaders(), signal,
      }),
      convId, restored, () => {}, sessionKey,
    );
  })().finally(() => { _runtimeRestorations.delete(sessionKey); });
  _runtimeRestorations.set(sessionKey, restoration);
  return restoration;
}

function runtimeStateFromStatus(payload: Record<string, any>): RuntimeStreamState {
  const rawQueue = payload.queue && typeof payload.queue === "object" ? payload.queue : undefined;
  const pollAfterSeconds = Math.max(
    1,
    Number(rawQueue?.poll_after_seconds || payload.poll_after_seconds || 5),
  );
  const status = String(payload.status || "queued");
  return {
    runId: String(payload.id || payload.run_id || ""),
    status,
    pollAfterSeconds,
    humanInput:
      status === "waiting_resource"
        ? runtimeHumanInputFromPayload(payload.human_input)
        : undefined,
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

/**
 * Answer the `request_input` question a durable run is parked on. The run
 * resumes server-side; the session's poll loop picks the new status up and
 * replays the rest of the turn. Resolves to the projected run status.
 */
export async function answerRuntimeHumanInput(
  sessionKey: string,
  answer: string,
  fields?: Record<string, unknown>,
  choice?: string,
): Promise<RuntimeStreamState> {
  const session = useChatStreamStore.getState().sessions[sessionKey];
  const runId = session?.runtimeRunId;
  if (!runId || !session?.runtimeHumanInput) {
    throw new Error("This chat is not waiting for an answer.");
  }
  const input = session.runtimeHumanInput;
  const response = await fetch(`/api/v1/chat/runs/${encodeURIComponent(runId)}/input`, {
    method: "POST",
    headers: { ...runtimeHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      request_id: input.requestId,
      answer,
      ...(fields && Object.keys(fields).length > 0 ? { fields } : {}),
      ...(choice ? { choice } : {}),
    }),
  });
  if (!response.ok) {
    let detail = `Answer failed (${response.status})`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* keep the status text */
    }
    throw new Error(detail);
  }
  const state = runtimeStateFromStatus(await response.json());
  const answerText =
    answer.trim()
    || (fields ? Object.entries(fields).map(([key, value]) => `${key}: ${String(value)}`).join("; ") : "")
    || (choice || "");
  useChatStreamStore.setState((current) => {
    const existing = current.sessions[sessionKey];
    if (!existing || existing.runtimeRunId !== runId
      || (existing.runtimeHumanInput && existing.runtimeHumanInput.requestId !== input.requestId)) return {};
    // The server persisted the answer as a user message; mirror it locally so
    // the transcript shows what was said without waiting for a reload.
    const stamp = Date.now();
    const questionText = runtimeHumanInputText(input);
    const inputMeta = { runtime_run_id: runId, runtime_input_request_id: input.requestId };
    const hasInputMessage = (role: "question" | "answer") => existing.messages.some((message) =>
      message.meta?.runtime_run_id === runId
      && message.meta?.runtime_input_request_id === input.requestId
      && message.meta?.runtime_input_role === role,
    );
    const questionMessage: ChatMessage = {
      id: `runtime-input-question-${input.requestId}`,
      role: "assistant",
      content: questionText,
      meta: { ...inputMeta, runtime_input_role: "question" },
      created_at: new Date(stamp - 1).toISOString(),
    } as ChatMessage;
    const answerMessage: ChatMessage = {
      id: `runtime-input-answer-${input.requestId}`,
      role: "user",
      content: answerText,
      meta: { ...inputMeta, runtime_input_role: "answer" },
      created_at: new Date(stamp).toISOString(),
    } as ChatMessage;
    // The reply the run produces after resuming answers this message, so the
    // streaming assistant bubble stays LAST: question, answer, then reply —
    // the same order the server persists.
    let messages = existing.messages;
    if (answerText && !hasInputMessage("answer")) {
      const streamingIndex = [...existing.messages]
        .map((message, index) => ({ message, index }))
        .reverse()
        .find(({ message }) => message.role === "assistant")?.index;
      const inserted = questionText && !hasInputMessage("question")
        ? [questionMessage, answerMessage] : [answerMessage];
      messages =
        streamingIndex == null
          ? [...existing.messages, ...inserted]
          : [
              ...existing.messages.slice(0, streamingIndex),
              ...inserted,
              ...existing.messages.slice(streamingIndex),
            ];
    }
    return {
      sessions: {
        ...current.sessions,
        [sessionKey]: {
          ...existing,
          messages,
          runtimeStatus: state.status,
          // The question is answered: drop the card even though the run is
          // still `waiting_resource` until the resume worker claims it.
          runtimeHumanInput: undefined,
          runtimePollAfterSeconds: Math.min(existing.runtimePollAfterSeconds || 5, 2),
        },
      },
    };
  });
  return state;
}

function runtimeHumanInputText(input: RuntimeHumanInputState): string {
  const lines = [input.question];
  if (input.options && input.options.length > 0) lines.push(`Options: ${input.options.join(" / ")}`);
  if (input.fields && input.fields.length > 0) {
    lines.push(`Please provide: ${input.fields.map((field) => field.label || field.name).join(", ")}`);
  }
  if (input.context) lines.push(input.context);
  return lines.filter(Boolean).join("\n");
}

async function fetchRuntimeStatus(runId: string, signal: AbortSignal): Promise<RuntimeStreamState> {
  const response = await fetch(`/api/v1/chat/runs/${encodeURIComponent(runId)}`, {
    headers: runtimeHeaders(),
    signal,
  });
  if (!response.ok) throw new Error(`Runtime status failed (${response.status})`);
  return runtimeStateFromStatus(await response.json());
}

async function fetchCancellationState(runId: string, requestStop: boolean, signal: AbortSignal): Promise<RuntimeStreamState> {
  const request = new AbortController();
  const abort = () => request.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();
  const timeout = setTimeout(abort, CANCEL_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/v1/chat/runs/${encodeURIComponent(runId)}${requestStop ? "/cancel" : ""}`, {
      method: requestStop ? "POST" : "GET", headers: runtimeHeaders(), signal: request.signal,
    });
    if (!response.ok) throw new Error(`Runtime cancellation request failed (${response.status})`);
    const state = runtimeStateFromStatus(await response.json());
    if (state.runId !== runId) throw new Error("Runtime cancellation returned the wrong run");
    return state;
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", abort);
  }
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
    }, Math.max(0.1, seconds) * 1000);
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
    const requestedKey = providedSessionKey || convId || makeDraftKey();
    const initialKey = resolveSessionKey(get(), requestedKey) || requestedKey;
    const controllerKey = initialKey;
    const previousCancellation = _cancelAttempts.get(initialKey)?.promise;
    abortSessionController(get().sessions[initialKey]);

    const ac = new AbortController();
    const transport = new AbortController();
    let accept!: ControllerRecord["accept"];
    const acceptance = new Promise<RuntimeStreamState | undefined>((resolve) => { accept = resolve; });
    const runId = ++_streamRunSeq;
    const controller: ControllerRecord = { ac, transport, acceptance, accept, runId, sessionKey: initialKey };
    _controllers.set(controllerKey, controller);

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
          streamGeneration: runId,
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
      controller.runtimeState = runtimeState;
      accept(runtimeState);
      if (ac.signal.aborted) transport.abort();
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
            runtimeHumanInput:
              runtimeStatus === "waiting_resource" ? runtimeState.humanInput : undefined,
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
        ac.signal.aborted ? transport.signal : ac.signal,
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
          humanInput: result.runtimeHumanInput,
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
      if (previousCancellation && !await previousCancellation) {
        throw new DOMException("Aborted", "AbortError");
      }
      if (ac.signal.aborted || !isCurrentRun()) throw new DOMException("Aborted", "AbortError");
      const response = await fetchFn(transport.signal);
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
        if (ac.signal.aborted) {
          await response.body?.cancel().catch(() => {});
          throw new DOMException("Aborted", "AbortError");
        }
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
        !streamEndSeen &&
        (
          !TERMINAL_RUNTIME_STATUSES.has(runtimeStatus)
          || runtimeStatus === "completed"
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
        !streamEndSeen &&
        !completionDetails.terminalObserved &&
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
        const message = translateApiError(terminalError);
        set((state) => {
          const existing = state.sessions[liveKey];
          if (!existing) return {};
          const messages = [...existing.messages];
          const last = messages[messages.length - 1];
          const hasVisibleAssistantBody = Boolean(String(last?.content || "").trim())
            || (Array.isArray(last?.assistant_blocks) && last.assistant_blocks.length > 0);
          if (last?.role === "assistant" && !hasVisibleAssistantBody) {
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
      accept(controller.runtimeState);
      transport.abort();
      const currentRun = isCurrentRun();
      const completedSession = get().sessions[liveKey];
      const terminalRuntimeStatus = completedSession?.streamGeneration === runId
        ? completedSession.runtimeStatus : controller.runtimeState?.status;
      if (
        terminalRuntimeStatus === "cancelled"
        || terminalRuntimeStatus === "failed"
      ) {
        completionDetails = {
          ...completionDetails,
          serverAccepted: true,
          terminalObserved: true,
          runtimeRunId:
            controller.runtimeState?.runId || completionDetails.runtimeRunId,
        };
      }
      if (terminalRuntimeStatus === "cancelled") {
        completionStatus = ChatStreamCompletionStatus.Cancelled;
      } else if (terminalRuntimeStatus === "failed") {
        completionStatus = ChatStreamCompletionStatus.Failed;
      } else if (terminalRuntimeStatus === "completed" && !streamEndSeen) {
        completionStatus = ChatStreamCompletionStatus.Failed;
      } else if (!streamEndSeen && (ac.signal.aborted || !currentRun)) {
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
    const pending = _cancelAttempts.get(key);
    const controller = _controllers.get(session.controllerKey || key);
    const runtimeRunId = session.runtimeRunId;
    const generation = session.streamGeneration;
    const ownsSession = () => {
      const current = get().sessions[key];
      return current?.runtimeRunId === runtimeRunId && current?.streamGeneration === generation;
    };

    // Stop presentation immediately. Durable cancellation is tracked separately
    // so a held response or disconnected SSE cannot lock the composer.
    markConversationStreamLocallyStopped(session.convId, latestAssistantMessageId(session.messages));
    set((state) => {
      const existing = state.sessions[key];
      if (!existing) return {};
      const sessions = {
        ...state.sessions,
        [key]: {
          ...existing,
          streaming: false,
          controllerKey: undefined,
          runtimeStatus: runtimeRunId && !TERMINAL_RUNTIME_STATUSES.has(existing.runtimeStatus || "")
            ? "cancel_requested" : existing.runtimeStatus,
          runtimeQueue: undefined,
          runtimeHumanInput: undefined,
          messages: closePendingHitlRequests(settlePendingAssistantProcess(existing.messages)),
        },
      };
      return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
    });
    abortSessionController(session, true);
    if (pending) return pending.promise;
    if ((!runtimeRunId && !controller) || TERMINAL_RUNTIME_STATUSES.has(session.runtimeStatus || "")) {
      return Promise.resolve(true);
    }

    const cancelController = new AbortController();
    const cancelAttemptId = ++_cancelAttemptSeq;
    let resolveCancel!: (stopped: boolean) => void;
    const cancelPromise = new Promise<boolean>((resolve) => { resolveCancel = resolve; });
    const finish = (status?: string) => {
      const attempt = consumeCancelAttempt(key, cancelAttemptId);
      if (!attempt) return;
      cancelController.abort();
      if (ownsSession()) {
        set((state) => {
          const existing = state.sessions[key];
          const sessions = {
            ...state.sessions,
            [key]: {
              ...existing,
              runtimeStatus: status || existing.runtimeStatus,
            },
          };
          return { sessions, ...activeSnapshot({ ...state, sessions }, key) };
        });
      }
      attempt.resolve(true);
    };
    _cancelAttempts.set(key, {
      id: cancelAttemptId, ac: cancelController,
      promise: cancelPromise, resolve: resolveCancel,
    });
    cancelController.signal.addEventListener("abort", () => controller?.transport.abort(), { once: true });

    void (async () => {
      const accepted = runtimeRunId ? undefined : await controller?.acceptance;
      const targetRunId = runtimeRunId || accepted?.runId;
      if (!targetRunId) {
        finish();
        return;
      }
      let acknowledged = false;
      let failures = 0;
      while (_cancelAttempts.get(key)?.id === cancelAttemptId) {
        try {
          const state = await fetchCancellationState(targetRunId, !acknowledged, cancelController.signal);
          if (TERMINAL_RUNTIME_STATUSES.has(state.status)) {
            finish(state.status);
            return;
          }
          acknowledged = state.status === "cancel_requested" || state.status === "cancelling";
          failures = 0;
        } catch {
          if (cancelController.signal.aborted) return;
          failures += 1;
        }
        // Transport failure does not undo the user's stop. Keep delivering the
        // idempotent request automatically, independently of composer state.
        await waitForRuntimePoll(Math.min(5, 0.25 * 2 ** Math.min(failures, 5)), cancelController.signal);
      }
    })().catch(() => {
      // Only reset/logout aborts this cancellation owner; it resolves waiters.
    });
    return cancelPromise;
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
      controller.transport.abort();
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
