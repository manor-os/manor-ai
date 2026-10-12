interface AgentGreetingPlaybackMessage {
  id: string;
  meta?: Record<string, unknown> | null;
}

/** Messages must use the same oldest-first order as the rendered chat. */
export function hasNewerNonGreetingMessage(
  messages: readonly AgentGreetingPlaybackMessage[],
  greetingMessageIds: readonly string[],
) {
  if (greetingMessageIds.length === 0) return false;

  const greetingIds = new Set(greetingMessageIds);
  let reachedPendingGreeting = false;

  for (const message of messages) {
    if (greetingIds.has(message.id)) {
      reachedPendingGreeting = true;
      continue;
    }
    if (reachedPendingGreeting && message.meta?.agent_greeting !== true) {
      return true;
    }
  }

  return false;
}
