import { api } from "./api";
import { getAuthToken } from "./authToken";
import { catalogScope } from "./catalogQueries";

async function readChatCatalog<T>(scope: string | null, read: () => Promise<T>): Promise<T> {
  const assertScope = () => {
    if (!scope || catalogScope(getAuthToken()) !== scope) {
      throw new DOMException("Session changed while reading chat options", "AbortError");
    }
  };
  assertScope();
  const result = await read();
  assertScope();
  return result;
}

export function chatMentionUsersQueryOptions(scope: string | null, enabled = true) {
  return {
    queryKey: ["chat-mention-users", scope] as const,
    queryFn: () => readChatCatalog(scope, () => api.users.directory()),
    enabled: enabled && !!scope,
  };
}

export function chatFlowEntrypointsQueryOptions(scope: string | null, workspaceId?: string, enabled = true) {
  return {
    queryKey: ["global-chat-flow-entrypoints", scope, workspaceId ?? null] as const,
    queryFn: () => readChatCatalog(scope, () => api.chat.listFlowEntrypoints(workspaceId)),
    enabled: enabled && !!scope,
  };
}
