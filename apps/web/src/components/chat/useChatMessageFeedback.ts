import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import { chatFeedbackCoordinator } from "../../lib/chat-feedback-queue.mjs";
import type { ChatMessageFeedbackRating } from "./ChatMessageActions";

// The page chat and the floating chat can open the same conversation at once.
const feedbackLoads = new Map<string, ReturnType<typeof api.chat.listFeedback>>();

function loadFeedback(conversationId: string) {
  const inFlight = feedbackLoads.get(conversationId);
  if (inFlight) return inFlight;
  const load = api.chat.listFeedback(conversationId).finally(() => {
    feedbackLoads.delete(conversationId);
  });
  feedbackLoads.set(conversationId, load);
  return load;
}

export default function useChatMessageFeedback(userId?: string | null) {
  const scope = userId || null;
  const [state, setState] = useState<{
    scope: string | null;
    values: Record<string, ChatMessageFeedbackRating>;
  }>(() => ({
    scope,
    values: scope ? chatFeedbackCoordinator.snapshot(scope) : {},
  }));
  const values = state.scope === scope ? state.values : {};

  useEffect(() => {
    if (!scope) {
      setState({ scope: null, values: {} });
      return undefined;
    }
    return chatFeedbackCoordinator.subscribe(scope, (next) => {
      setState({ scope, values: next });
    });
  }, [scope]);

  const submit = useCallback(
    (
      key: string,
      rating: ChatMessageFeedbackRating,
      persist: (
        rating: ChatMessageFeedbackRating,
      ) => Promise<unknown>,
    ) => {
      if (!scope) return Promise.reject(new Error("Feedback requires a signed-in user"));
      return chatFeedbackCoordinator.submit(scope, key, rating, persist);
    },
    [scope],
  );

  const hydrateConversation = useCallback(
    async (conversationId: string) => {
      if (!scope || !conversationId) return;
      const records = await loadFeedback(conversationId);
      chatFeedbackCoordinator.hydrate(scope, records);
    },
    [scope],
  );

  return { hydrateConversation, submit, values };
}
