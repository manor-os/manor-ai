import { getAuthToken } from "./authToken";

export interface ChatVoiceScope {
  conversationId?: string | null;
  workspaceId?: string | null;
  agentId?: string | null;
  threadRefKind?: string;
  threadRefId?: string;
  publicToken?: string;
  sessionId?: string | null;
}

export function chatVoiceScopeKey(scope: ChatVoiceScope) {
  return JSON.stringify([scope.conversationId, scope.workspaceId, scope.agentId, scope.threadRefKind, scope.threadRefId, scope.publicToken, scope.sessionId]);
}

function headers() {
  const token = getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

async function checked(response: Response) {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message || `Voice request failed (${response.status})`);
  }
  return response;
}

export async function transcribeChatVoice(blob: Blob, scope: ChatVoiceScope, signal: AbortSignal) {
  const form = new FormData();
  const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  form.append("file", blob, `voice.${ext}`);
  // Let the provider detect the spoken language; it need not match UI locale.
  let url = "/api/v1/audio/transcribe";
  if (scope.publicToken) {
    if (!scope.sessionId) throw new Error("Chat session not ready");
    form.append("session_id", scope.sessionId);
    url = `/api/v1/public/chat/${encodeURIComponent(scope.publicToken)}/audio/transcribe`;
  } else {
    if (scope.workspaceId) form.append("workspace_id", scope.workspaceId);
    if (scope.conversationId) form.append("conversation_id", scope.conversationId);
  }
  const response = await checked(await fetch(url, { method: "POST", headers: headers(), body: form, signal }));
  return String((await response.json()).text || "").trim();
}

// Use Unicode code-point offsets: public speech slices the saved Python string.
export function speechChunks(text: string) {
  const chars = Array.from(text);
  const chunks: { text: string; offset: number; length: number }[] = [];
  for (let offset = 0; offset < chars.length;) {
    let end = Math.min(offset + 3000, chars.length);
    if (end < chars.length) {
      for (let i = end; i > offset + 1500; i--) {
        if (/[\s.!?。！？]/.test(chars[i - 1])) { end = i; break; }
      }
    }
    chunks.push({ text: chars.slice(offset, end).join(""), offset, length: end - offset });
    offset = end;
  }
  return chunks;
}

export async function fetchChatSpeech(
  chunk: ReturnType<typeof speechChunks>[number], scope: ChatVoiceScope,
  signal: AbortSignal, messageId?: string,
) {
  const isPublic = Boolean(scope.publicToken);
  if (isPublic && (!scope.sessionId || !messageId)) throw new Error("Reply not ready");
  const url = isPublic
    ? `/api/v1/public/chat/${encodeURIComponent(scope.publicToken!)}/audio/speech`
    : "/api/v1/chat/tts";
  const response = await checked(await fetch(url, {
    method: "POST", signal,
    headers: { ...headers(), "Content-Type": "application/json" },
    body: JSON.stringify(isPublic ? {
      session_id: scope.sessionId, message_id: messageId, offset: chunk.offset, length: chunk.length,
    } : { text: chunk.text, conversation_id: scope.conversationId, workspace_id: scope.workspaceId }),
  }));
  return response.blob();
}

let activeAudio: (() => void) | undefined;
export function stopChatAudio() { activeAudio?.(); }
export function claimChatAudio(stop: () => void) {
  stopChatAudio();
  activeAudio = stop;
  window.addEventListener("pagehide", stopChatAudio, { once: true });
  return () => { if (activeAudio === stop) activeAudio = undefined; };
}
