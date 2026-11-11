import type { Conversation } from "./types";

/**
 * The signed-in user's own in-app conversation with an agent: the one the sidebar's direct chats
 * list and the chat pane must both open. Integration channels (SMS, voice, WeChat…) reach the same
 * agent but are not part of that list, so they never stand in for it.
 */
export function isPersonalAgentConversation(conversation: Conversation, userId?: string): boolean {
  return Boolean(
    userId
    && conversation.agent_id
    && conversation.user_id === userId
    && !conversation.workspace_id
    && conversation.channel.toLowerCase() === "web",
  );
}
