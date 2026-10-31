/**
 * The embedded frame's session: a scoped customer-trial token held in memory
 * (the public/embed-isolate.js storage), received from the parent page and renewed
 * through it. The frame never calls /auth/session/refresh, /auth/renew or
 * /auth/logout; the server refuses them for a scoped token anyway.
 */
import { USER_TOKEN_KEY, decodeAuthTokenClaims, notifyAuthTokenChanged, type AuthTokenClaims } from "../lib/authToken";
import { EMBED } from "./embedContext";
import { EMBED_READY, EMBED_SESSION, randomId, type FramePortMessage, type ParentPortMessage } from "./embedProtocol";

export type EmbeddedStatus = "connecting" | "ready" | "ended" | "unavailable";

const CONNECT_TIMEOUT_MS = 10_000;
const READY_RETRY_MS = 1_000;
const RENEW_TIMEOUT_MS = 15_000;
const RENEW_BEFORE_EXPIRY_MS = 120_000;
const RENEW_RETRY_MS = 30_000;
const MIN_RENEW_DELAY_MS = 5_000;

const nonce = randomId();
let status: EmbeddedStatus = "connecting";
const listeners = new Set<() => void>();
let started = false;
let port: MessagePort | null = null;
let workspaceId: string | null = null;
let subject: string | null = null;
let initial: Promise<boolean> | null = null;
let resolveInitial: (connected: boolean) => void = () => {};
let readyTimer: ReturnType<typeof setInterval> | undefined;
let connectTimer: ReturnType<typeof setTimeout> | undefined;
let renewTimer: ReturnType<typeof setTimeout> | undefined;
let renewing: Promise<boolean> | null = null;
const pending = new Map<string, (message: ParentPortMessage | null) => void>();

function setStatus(next: EmbeddedStatus) {
  if (status === next) return;
  status = next;
  listeners.forEach((listener) => listener());
}

export function embeddedStatus(): EmbeddedStatus {
  return status;
}

export function subscribeEmbeddedStatus(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The one customer Workspace this frame's session is pinned to. */
export function embeddedWorkspaceId(): string | null {
  return workspaceId;
}

/** A scoped session for exactly this frame: its kind, its delivery or grant, the same person and Workspace. */
function sessionClaims(token: unknown): AuthTokenClaims | null {
  if (!EMBED || typeof token !== "string") return null;
  const claims = decodeAuthTokenClaims(token);
  const cs = claims?.cs;
  if (!claims?.sub || claims.typ !== "collaborator_session" || !cs) return null;
  if (cs.kind !== EMBED.sessionKind || cs.ref !== EMBED.refId || typeof cs.workspace_id !== "string") return null;
  if (workspaceId && cs.workspace_id !== workspaceId) return null;
  if (subject && claims.sub !== subject) return null;
  if (claims.exp && claims.exp * 1000 <= Date.now()) return null;
  return claims;
}

function clearTimers() {
  clearInterval(readyTimer);
  clearTimeout(connectTimer);
  clearTimeout(renewTimer);
}

function scheduleRenew(claims: AuthTokenClaims) {
  clearTimeout(renewTimer);
  if (!claims.exp) return;
  const left = claims.exp * 1000 - Date.now();
  const delay = Math.max(left - RENEW_BEFORE_EXPIRY_MS, left / 2, MIN_RENEW_DELAY_MS);
  renewTimer = setTimeout(() => {
    void renewEmbeddedSession().then((renewed) => {
      if (renewed || status === "ended") return;
      // A transient failure retries while the current token still works.
      if (claims.exp! * 1000 - Date.now() > RENEW_RETRY_MS) {
        renewTimer = setTimeout(() => scheduleRenew(claims), RENEW_RETRY_MS);
      }
    });
  }, delay);
}

function adopt(token: string, claims: AuthTokenClaims) {
  workspaceId = claims.cs!.workspace_id!;
  subject = claims.sub!;
  // The memory shim: this never reaches the browser's real storage.
  localStorage.setItem(USER_TOKEN_KEY, token);
  notifyAuthTokenChanged();
  scheduleRenew(claims);
}

function onPortMessage(event: MessageEvent<ParentPortMessage>) {
  const message = event.data;
  if (!message || typeof message.id !== "string") return;
  pending.get(message.id)?.(message);
}

function onWindowMessage(event: MessageEvent) {
  if (event.origin !== window.location.origin || event.source !== window.parent) return;
  const data = event.data as { type?: string; nonce?: string; access_token?: unknown } | null;
  if (!data || data.type !== EMBED_SESSION || data.nonce !== nonce) return;
  const channel = event.ports?.[0];
  if (!channel || port || status !== "connecting") return;
  const claims = sessionClaims(data.access_token);
  if (!claims) return;
  port = channel;
  port.onmessage = onPortMessage;
  clearInterval(readyTimer);
  clearTimeout(connectTimer);
  adopt(data.access_token as string, claims);
  setStatus("ready");
  resolveInitial(true);
}

function post(message: FramePortMessage) {
  port?.postMessage(message);
}

/** Announce the frame to the parent and wait for its session (idempotent). */
export function startEmbeddedSession(): Promise<boolean> {
  if (!EMBED) return Promise.resolve(false);
  if (started) return initial!;
  started = true;
  initial = new Promise<boolean>((resolve) => { resolveInitial = resolve; });
  const context = EMBED;
  window.addEventListener("message", onWindowMessage);
  const ready = () => window.parent.postMessage(
    { type: EMBED_READY, nonce, kind: context.kind, ref: context.refId },
    window.location.origin,
  );
  ready();
  readyTimer = setInterval(ready, READY_RETRY_MS);
  connectTimer = setTimeout(() => {
    if (port) return;
    clearInterval(readyTimer);
    setStatus("unavailable");
    resolveInitial(false);
  }, CONNECT_TIMEOUT_MS);
  return initial;
}

/** A fresh scoped token from the parent; false when it could not provide one. */
export function renewEmbeddedSession(): Promise<boolean> {
  if (!EMBED || status === "ended" || status === "unavailable") return Promise.resolve(false);
  if (!port) return startEmbeddedSession();
  if (renewing) return renewing;
  const channel = port;
  renewing = new Promise<boolean>((resolve) => {
    const id = randomId();
    const timeout = setTimeout(() => settle(null), RENEW_TIMEOUT_MS);
    function settle(message: ParentPortMessage | null) {
      clearTimeout(timeout);
      pending.delete(id);
      if (message?.type === "session") {
        const claims = sessionClaims(message.access_token);
        if (claims) {
          adopt(message.access_token, claims);
          resolve(true);
          return;
        }
      }
      if (message?.type === "renew-failed" && message.ended) endEmbeddedSession();
      resolve(false);
    }
    pending.set(id, settle);
    channel.postMessage({ type: "renew", id } satisfies FramePortMessage);
  }).finally(() => { renewing = null; });
  return renewing;
}

/** Access ended: tell the parent, drop the token and stay on the ended screen (never /login). */
export function endEmbeddedSession(): void {
  if (!EMBED || status === "ended") return;
  setStatus("ended");
  clearTimers();
  post({ type: "ended" });
  if (localStorage.getItem(USER_TOKEN_KEY) !== null) {
    localStorage.removeItem(USER_TOKEN_KEY);
    notifyAuthTokenChanged();
  }
}

/** The frame's "sign out": the parent closes the frame. */
export function closeEmbeddedSession(): void {
  post({ type: "close" });
}
