import { authPrincipalKey, decodeAuthTokenClaims, getAuthToken, isSameAuthIdentity, notifyAuthTokenChanged } from "./authToken";

let restoring: Promise<boolean> | null = null;
export function accessTokenExpired(token: string | null): boolean {
  const claims = decodeAuthTokenClaims(token);
  return Boolean(claims?.exp && claims.exp * 1000 <= Date.now());
}

/** Restore only through the HttpOnly cookie; never save a refresh secret in JS. */
export function restorePersistentSession(): Promise<boolean> {
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
    if (!next?.sub || (claims?.sub && (next.sub !== claims.sub || next.entity_id !== claims.entity_id))) return false;
    if (generation !== localStorage.getItem("manor_auth_generation")) return false;
    if (getAuthToken() !== before) return isSameAuthIdentity(before, getAuthToken());
    localStorage.setItem("manor_token", data.access_token);
    notifyAuthTokenChanged();
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
