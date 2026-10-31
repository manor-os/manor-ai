import { authPrincipalKey, decodeAuthTokenClaims, getAuthToken, isSameAuthIdentity, notifyAuthTokenChanged } from "./authToken";
import { EMBED } from "../embed/embedContext";
import { renewEmbeddedSession } from "../embed/embeddedSession";

/** Membership role of a provider co-working in a customer's company. */
export const COLLABORATOR_ROLE = "collaborator";
/** Dispatched on window when a refresh returned an ended collaboration to the person's own company. */
export const COLLABORATION_ENDED_EVENT = "manor:collaboration-ended";

// Kept until the router takes it: a refresh at app start (checkAuth) fires
// the event before anything listens.
let collaborationEndedPending = false;

/** Whether an ended collaboration moved the session home since the last call. */
export function takeCollaborationEnded(): boolean {
  const ended = collaborationEndedPending;
  collaborationEndedPending = false;
  return ended;
}

/** Login query reason for a collaboration that ended with no session to return home to. */
export const COLLABORATION_ENDED_REASON = "collaboration_ended";
const COLLABORATION_ENDED_SIGN_IN_KEY = "manor_collaboration_ended_sign_in";

function tabStorage(): Storage | null {
  try {
    return window.sessionStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * An ended collaboration could not move the session home (no remember-me
 * cookie): the person signs in again, and lands in their own company rather
 * than back on the customer's Workspace.
 */
export function markCollaborationEndedSignIn(): void {
  try {
    tabStorage()?.setItem(COLLABORATION_ENDED_SIGN_IN_KEY, "1");
  } catch {
    // Without storage the login page shows no notice; the sign-in still works.
  }
}

/** Whether the next sign-in follows an ended collaboration; clears the mark when asked to. */
export function collaborationEndedSignIn({ clear = false } = {}): boolean {
  try {
    const storage = tabStorage();
    const ended = storage?.getItem(COLLABORATION_ENDED_SIGN_IN_KEY) === "1";
    if (clear) storage?.removeItem(COLLABORATION_ENDED_SIGN_IN_KEY);
    return ended;
  } catch {
    return false;
  }
}

/** Where to sign in again after the session ended at ``next``. */
export function signInPath(next: string): string {
  // The customer's Workspace no longer exists for this person: never return to it.
  if (collaborationEndedSignIn()) return `/login?reason=${COLLABORATION_ENDED_REASON}`;
  return `/login?next=${encodeURIComponent(next)}`;
}

let restoring: Promise<boolean> | null = null;
export function accessTokenExpired(token: string | null): boolean {
  const claims = decodeAuthTokenClaims(token);
  return Boolean(claims?.exp && claims.exp * 1000 <= Date.now());
}

/** Restore only through the HttpOnly cookie; never save a refresh secret in JS. */
export function restorePersistentSession(): Promise<boolean> {
  // The embedded customer-trial frame renews through its parent page only.
  if (EMBED) return renewEmbeddedSession();
  if (localStorage.getItem("manor_session_signed_out") === "1") return Promise.resolve(false);
  if (restoring) return restoring;
  const before = getAuthToken();
  const claims = decodeAuthTokenClaims(before);
  if (claims?.typ === "impersonation") return Promise.resolve(false);
  const generation = localStorage.getItem("manor_auth_generation");
  restoring = (async () => {
    const response = await fetch("/api/v1/auth/session/refresh", {
      method: "POST", credentials: "include", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Manor-Session": "1" },
      body: JSON.stringify({ entity_id: claims?.entity_id }),
    });
    if (response.status === 401 || response.status === 403) return false;
    if (!response.ok) throw new Error("Persistent login temporarily unavailable");
    const data = await response.json();
    if (typeof data.access_token !== "string") return false;
    const next = decodeAuthTokenClaims(data.access_token);
    if (!next?.sub || (claims?.sub && next.sub !== claims.sub)) return false;
    // The server changes the company only for a collaborator session whose
    // customer Workspace access ended: it returns them to their own company.
    const leftCollaboration = Boolean(claims?.sub && next.entity_id !== claims.entity_id);
    if (leftCollaboration && claims?.role !== COLLABORATOR_ROLE) return false;
    if (generation !== localStorage.getItem("manor_auth_generation")) return false;
    if (getAuthToken() !== before) return isSameAuthIdentity(before, getAuthToken());
    localStorage.setItem("manor_token", data.access_token);
    notifyAuthTokenChanged();
    if (leftCollaboration) {
      collaborationEndedPending = true;
      window.dispatchEvent(new Event(COLLABORATION_ENDED_EVENT));
    }
    return true;
  })().finally(() => { restoring = null; });
  return restoring;
}

/** Renew expired access before uploads, downloads or a streamed write begins. */
export async function getReadyAuthToken(): Promise<string | null> {
  const before = getAuthToken();
  if (!accessTokenExpired(before)) return before;
  await restorePersistentSession();
  const current = getAuthToken();
  if (authPrincipalKey(before) !== authPrincipalKey(current)) {
    throw new DOMException("Session changed while refreshing", "AbortError");
  }
  return current;
}
