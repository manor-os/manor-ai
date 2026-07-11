export const USER_TOKEN_KEY = "manor_token";

function canUseBrowserStorage(): boolean {
  return typeof window !== "undefined";
}


export function getAuthToken(): string | null {
  if (!canUseBrowserStorage()) return null;
  return (
    window.localStorage.getItem(USER_TOKEN_KEY)
  );
}

