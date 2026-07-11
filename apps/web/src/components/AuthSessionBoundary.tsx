import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../stores/auth";
import { useChatStreamStore } from "../stores/chatStream";

const PENDING_CHAT_RETRY_KEY = "manor_pending_chat_retry";

export default function AuthSessionBoundary() {
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();
  const previousTokenRef = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    if (previousTokenRef.current !== undefined && previousTokenRef.current !== token) {
      useChatStreamStore.getState().reset();
      localStorage.removeItem(PENDING_CHAT_RETRY_KEY);
      queryClient.clear();
    }
    previousTokenRef.current = token;
  }, [queryClient, token]);

  return null;
}
