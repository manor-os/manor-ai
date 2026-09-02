export const USER_TOKEN_KEY = "manor_token";
export const AUTH_TOKEN_CHANGED_EVENT = "manor:auth-token-changed";

export interface AuthTokenClaims {
  sub?: string;
  entity_id?: string;
  role?: string;
  token_version?: number;
  typ?: string;
  amr?: string[];
  iat?: number;
  exp?: number;
}

const LOCAL_AUTH_KEYS = [
  USER_TOKEN_KEY,
  "manor_user",
  "manor_portal_token",
  "manor_pending_chat_retry",
] as const;
const SESSION_AUTH_KEYS = [
  "manor_impersonation_token",
  "oauth_state",
  "oauth_next",
  "oauth_remember_me",
  "oauth_team_invite",
  "oauth_invitation_code",
  "oauth_public_chat_token",
] as const;
const LOCAL_AUTH_PREFIXES = ["manor_public_chat_session:"] as const;
const SESSION_AUTH_PREFIXES = ["manor_public_chat_autostart:"] as const;

function canUseBrowserStorage(): boolean {
  return typeof window !== "undefined";
}

function notifyAuthTokenChanged() {
  if (canUseBrowserStorage()) window.dispatchEvent(new Event(AUTH_TOKEN_CHANGED_EVENT));
}

function removeKeysWithPrefixes(storage: Storage, prefixes: readonly string[]) {
  const keys: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key && prefixes.some((prefix) => key.startsWith(prefix))) keys.push(key);
  }
  keys.forEach((key) => storage.removeItem(key));
}

export function clearAuthBrowserState() {
  if (!canUseBrowserStorage()) return;
  try {
    LOCAL_AUTH_KEYS.forEach((key) => window.localStorage.removeItem(key));
    removeKeysWithPrefixes(window.localStorage, LOCAL_AUTH_PREFIXES);
  } catch {
    // Storage may be blocked; continue clearing the other storage scope.
  }
  try {
    SESSION_AUTH_KEYS.forEach((key) => window.sessionStorage.removeItem(key));
    removeKeysWithPrefixes(window.sessionStorage, SESSION_AUTH_PREFIXES);
  } catch {
    // Storage may be blocked by the browser.
  }
  notifyAuthTokenChanged();
}


export function getAuthToken(): string | null {
  if (!canUseBrowserStorage()) return null;
  return (
    window.localStorage.getItem(USER_TOKEN_KEY)
  );
}

export function decodeAuthTokenClaims(token: string | null | undefined): AuthTokenClaims | null {
  if (!token) return null;
  const [, payload] = token.split(".");
  if (!payload) return null;
  try {
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
    return JSON.parse(atob(padded)) as AuthTokenClaims;
  } catch {
    return null;
  }
}

/** Stable filesystem ownership scope. Authorization context changes within
 * one entity must still serialize writes to the same entity path. */
export function authEntityKey(token: string | null | undefined): string {
  const claims = decodeAuthTokenClaims(token);
  if (!claims) return token ? `opaque:${token}` : "anonymous";
  return claims.entity_id ? `entity:${claims.entity_id}` : `opaque:${token}`;
}

/** Stable user-within-entity scope used to distinguish token rotation from
 * switching to another effective account in the same entity. */
export function authPrincipalKey(token: string | null | undefined): string {
  const claims = decodeAuthTokenClaims(token);
  if (!claims?.sub || !claims.entity_id) return token ? `opaque:${token}` : "anonymous";
  return JSON.stringify([claims.sub, claims.entity_id]);
}

/** Token rotation for the same authenticated principal must not look like an
 * account switch to the rest of the UI. */
export function isSameAuthIdentity(
  previousToken: string | null | undefined,
  nextToken: string | null | undefined,
): boolean {
  const previous = decodeAuthTokenClaims(previousToken);
  const next = decodeAuthTokenClaims(nextToken);
  if (!previous || !next) return previousToken === nextToken;
  return (
    previous.sub === next.sub &&
    previous.entity_id === next.entity_id &&
    previous.role === next.role &&
    previous.token_version === next.token_version &&
    previous.typ === next.typ &&
    JSON.stringify(previous.amr || []) === JSON.stringify(next.amr || [])
  );
}

