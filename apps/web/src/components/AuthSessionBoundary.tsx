import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../stores/auth";
import { useChatStreamStore } from "../stores/chatStream";
import { resetKnowledgeUploadsForAuthChange } from "../stores/knowledgeUploads";
import { AUTH_TOKEN_CHANGED_EVENT, USER_TOKEN_KEY, authPrincipalKey, decodeAuthTokenClaims, getAuthToken, isSameAuthIdentity } from "../lib/authToken";
import { accessTokenExpired } from "../lib/persistentSession";
import { ApiError, api, handleSessionExpired } from "../lib/api";

const PENDING_CHAT_RETRY_KEY = "manor_pending_chat_retry";
const ACTIVE_SESSION_RENEW_INTERVAL_MS = 5 * 60 * 1000;
const FAILED_SESSION_RENEW_RETRY_MS = 60_000;
const USER_ACTIVITY_EVENTS = ["pointerdown", "keydown", "touchstart", "wheel"] as const;

export default function AuthSessionBoundary() {
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();
  const previousTokenRef = useRef<string | null | undefined>(undefined);
  const renewalInFlightRef = useRef(false);
  const lastRenewedAtRef = useRef(Date.now());
  const nextRenewalAttemptAtRef = useRef(0);

  useEffect(() => {
    const syncSession = () => {
      const state = useAuthStore.getState();
      const previous = state.token;
      const current = getAuthToken();
      if (previous === current && !accessTokenExpired(current) && (current || !state.user)) return;
      // checkAuth clears stale user/stream state synchronously when credentials
      // disappear, before a click can submit work under the old UI identity.
      void useAuthStore.getState().checkAuth();
      if ((previous || state.user) && !current) handleSessionExpired();
    };
    const onStorage = (event: StorageEvent) => {
      if (event.storageArea === window.localStorage && (event.key === USER_TOKEN_KEY || event.key === null)) syncSession();
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, syncSession);
    window.addEventListener("focus", syncSession);
    window.addEventListener("pageshow", syncSession);
    window.addEventListener("pointerdown", syncSession, true);
    window.addEventListener("keydown", syncSession, true);
    syncSession();
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, syncSession);
      window.removeEventListener("focus", syncSession);
      window.removeEventListener("pageshow", syncSession);
      window.removeEventListener("pointerdown", syncSession, true);
      window.removeEventListener("keydown", syncSession, true);
    };
  }, []);

  useEffect(() => {
    if (
      previousTokenRef.current === undefined
      || authPrincipalKey(previousTokenRef.current) !== authPrincipalKey(token)
    ) {
      // Remove any protected bytes persisted by older clients before a new
      // principal can render Knowledge or chat thumbnails.
      api.documents.clearDownloadCache();
    }
    if (
      previousTokenRef.current !== undefined
      && authPrincipalKey(previousTokenRef.current) !== authPrincipalKey(token)
    ) {
      resetKnowledgeUploadsForAuthChange();
    }
    if (
      previousTokenRef.current !== undefined &&
      previousTokenRef.current !== token &&
      !isSameAuthIdentity(previousTokenRef.current, token)
    ) {
      useChatStreamStore.getState().reset();
      localStorage.removeItem(PENDING_CHAT_RETRY_KEY);
      queryClient.clear();
    }
    if (previousTokenRef.current !== token) {
      const issuedAt = decodeAuthTokenClaims(token)?.iat;
      lastRenewedAtRef.current = issuedAt ? issuedAt * 1000 : Date.now();
      nextRenewalAttemptAtRef.current = 0;
    }
    previousTokenRef.current = token;
  }, [queryClient, token]);

  useEffect(() => {
    if (!token) return;
    const claims = decodeAuthTokenClaims(token);
    // A scoped collaborator session renews through the parent page (embeddedSession).
    if (!claims || claims.typ === "impersonation" || claims.typ === "collaborator_session") return;

    const renewForActivity = () => {
      if (token !== getAuthToken()) return;
      if (document.visibilityState === "hidden") return;
      if (renewalInFlightRef.current) return;
      if (Date.now() < nextRenewalAttemptAtRef.current) return;
      if (Date.now() - lastRenewedAtRef.current < ACTIVE_SESSION_RENEW_INTERVAL_MS) return;

      renewalInFlightRef.current = true;
      void useAuthStore.getState().renewSession()
        .then(() => {
          if (!isSameAuthIdentity(token, useAuthStore.getState().token)) return;
          lastRenewedAtRef.current = Date.now();
          nextRenewalAttemptAtRef.current = 0;
        })
        .catch((error: unknown) => {
          // A 401 is handled centrally by the API client; transient network
          // failures wait before another real interaction can retry.
          if (!isSameAuthIdentity(token, useAuthStore.getState().token)) return;
          const retryAfterMs = error instanceof ApiError
            ? error.retryAfterMs ?? 0
            : 0;
          nextRenewalAttemptAtRef.current = Date.now() + Math.max(FAILED_SESSION_RENEW_RETRY_MS, retryAfterMs);
        })
        .finally(() => {
          renewalInFlightRef.current = false;
        });
    };

    USER_ACTIVITY_EVENTS.forEach((eventName) => {
      window.addEventListener(eventName, renewForActivity, { capture: true, passive: true });
    });
    return () => {
      USER_ACTIVITY_EVENTS.forEach((eventName) => {
        window.removeEventListener(eventName, renewForActivity, { capture: true });
      });
    };
  }, [token]);

  return null;
}
