import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../stores/auth";
import { useChatStreamStore } from "../stores/chatStream";
import { resetKnowledgeUploadsForAuthChange } from "../stores/knowledgeUploads";
import { authPrincipalKey, decodeAuthTokenClaims, isSameAuthIdentity } from "../lib/authToken";

const PENDING_CHAT_RETRY_KEY = "manor_pending_chat_retry";
const ACTIVE_SESSION_RENEW_INTERVAL_MS = 5 * 60 * 1000;
const USER_ACTIVITY_EVENTS = ["pointerdown", "keydown", "touchstart", "wheel"] as const;

export default function AuthSessionBoundary() {
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();
  const previousTokenRef = useRef<string | null | undefined>(undefined);
  const renewalInFlightRef = useRef(false);
  const lastRenewedAtRef = useRef(Date.now());

  useEffect(() => {
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
    }
    previousTokenRef.current = token;
  }, [queryClient, token]);

  useEffect(() => {
    if (!token) return;
    const claims = decodeAuthTokenClaims(token);
    if (!claims || claims.typ === "impersonation") return;

    const renewForActivity = () => {
      if (document.visibilityState === "hidden") return;
      if (renewalInFlightRef.current) return;
      if (Date.now() - lastRenewedAtRef.current < ACTIVE_SESSION_RENEW_INTERVAL_MS) return;

      renewalInFlightRef.current = true;
      void useAuthStore.getState().renewSession()
        .then(() => {
          lastRenewedAtRef.current = Date.now();
        })
        .catch(() => {
          // A 401 is handled centrally by the API client; transient network
          // failures retry on the next real user interaction.
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
