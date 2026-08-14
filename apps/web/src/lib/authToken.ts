export const USER_TOKEN_KEY = "manor_token";

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
}


export function getAuthToken(): string | null {
  if (!canUseBrowserStorage()) return null;
  return (
    window.localStorage.getItem(USER_TOKEN_KEY)
  );
}

