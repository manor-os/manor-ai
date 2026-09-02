import type { NavigateFunction } from "react-router-dom";

export enum AuthNavigationMode {
  SpaReplace = "spa_replace",
  DocumentReplace = "document_replace",
}

const DOCUMENT_APP_BASE_PATHS: readonly string[] = [
] as const;

export function safeAuthRedirect(value: string | null, fallback = "/chat"): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return fallback;
  return value;
}

export function resolveAuthNavigationMode(target: string): AuthNavigationMode {
  const pathname = target.split(/[?#]/, 1)[0];
  const belongsToDocumentApp = DOCUMENT_APP_BASE_PATHS.some(
    (basePath) => pathname === basePath || pathname.startsWith(`${basePath}/`),
  );

  return belongsToDocumentApp
    ? AuthNavigationMode.DocumentReplace
    : AuthNavigationMode.SpaReplace;
}

export function navigateAfterAuth(
  target: string,
  navigate: NavigateFunction,
  replaceDocument: (next: string) => void = (next) => window.location.replace(next),
): void {
  switch (resolveAuthNavigationMode(target)) {
    case AuthNavigationMode.DocumentReplace:
      replaceDocument(target);
      return;
    case AuthNavigationMode.SpaReplace:
      void navigate(target, { replace: true });
  }
}
