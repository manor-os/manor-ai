import { useCallback, useEffect, useRef, useState } from "react";
import { getAuthToken } from "./authToken";
import { chatVoiceScopeKey, claimChatAudio, type ChatVoiceScope } from "./chatVoice";
import { LiveChatAudio } from "./liveChatAudio";

export type CallPhase = "idle" | "connecting" | "listening" | "transcribing" | "thinking" | "synthesizing" | "speaking" | "ended" | "error";
export type CallVoice = "warm" | "clear" | "bright" | "deep";
export type CallErrorCode = "timeout" | "unsupported" | "slowConnection" | "microphoneDisconnected"
  | "playback" | "connection" | "disconnected" | "permissionDenied" | "busy" | "unavailable" | "failed";

const CALL_VOICES: CallVoice[] = ["warm", "clear", "bright", "deep"];
const VOICE_STORAGE_KEY = "manor.chat.call.voice";

function savedCallVoice(): CallVoice {
  if (typeof window === "undefined") return "warm";
  try {
    const value = window.localStorage.getItem(VOICE_STORAGE_KEY);
    return CALL_VOICES.includes(value as CallVoice) ? value as CallVoice : "warm";
  } catch {
    return "warm";
  }
}

function persistCallVoice(voice: CallVoice) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(VOICE_STORAGE_KEY, voice);
  } catch {
    // Voice preference persistence is best-effort. Restricted storage must not
    // prevent Webchat from rendering or disconnect an active voice call.
  }
}

export function useLiveChatCall({ scope, disabled, onConversation }: {
  scope: ChatVoiceScope; disabled?: boolean; onConversation?: (id: string) => void;
}) {
  const [phase, setPhase] = useState<CallPhase>("idle");
  const [open, setOpen] = useState(false);
  const [muted, setMuted] = useState(false);
  const [caption, setCaption] = useState("");
  const [transcript, setTranscript] = useState<{ id: number; turn: number; role: "user" | "assistant"; text: string }[]>([]);
  const [error, setError] = useState<CallErrorCode | "">("");
  const [inputLevel, setInputLevel] = useState(0);
  const [duplexMode, setDuplexMode] = useState<"full" | "push_to_interrupt">("full");
  const [transportMode, setTransportMode] = useState<"native_realtime" | "turn_based">();
  const [backgroundWorking, setBackgroundWorking] = useState(false);
  const [voice, setVoice] = useState<CallVoice>(savedCallVoice);
  const [voiceLocked, setVoiceLocked] = useState(false);
  const [startedAt, setStartedAt] = useState<number>();
  const voiceRef = useRef(voice);
  voiceRef.current = voice;
  const current = useRef({ scope, disabled, onConversation });
  current.current = { scope, disabled, onConversation };
  const call = useRef<{
    key: string; socket?: WebSocket; audio?: LiveChatAudio; release?: () => void;
    timer?: ReturnType<typeof setTimeout>; conversationId?: string; ready?: boolean;
    captionItem?: string;
    generation?: number;
    muted?: boolean;
    awaitingReply?: boolean;
    inputPaused?: boolean;
    resumePhase?: CallPhase;
    transcriptId?: number;
    transcriptTurn?: number;
  }>();

  const stop = useCallback(() => {
    const active = call.current;
    call.current = undefined;
    setVoiceLocked(false);
    setBackgroundWorking(false);
    if (!active) return;
    clearTimeout(active.timer);
    active.audio?.close();
    setInputLevel(0);
    active.release?.();
    if (active.socket) {
      active.socket.onclose = null;
      active.socket.onmessage = null;
      if (active.socket.readyState === WebSocket.OPEN) active.socket.send(JSON.stringify({ type: "end" }));
      active.socket.close();
    }
    setPhase("ended");
  }, []);

  const start = useCallback(async () => {
    if (current.current.disabled) return;
    stop();
    setOpen(true); setPhase("connecting"); setMuted(false); setError("");
    setCaption(""); setTranscript([]); setStartedAt(undefined);
    setInputLevel(0); setDuplexMode("full"); setTransportMode(undefined); setVoiceLocked(false);
    setBackgroundWorking(false);
    const snapshot = current.current.scope;
    const active: NonNullable<typeof call.current> = { key: chatVoiceScopeKey(snapshot) };
    call.current = active;
    const fail = (code: CallErrorCode) => {
      if (call.current !== active) return;
      stop(); setError(code); setPhase("error");
    };
    const appendTranscript = (role: "user" | "assistant", text: string, generation?: number) => {
      const id = active.transcriptId = (active.transcriptId || 0) + 1;
      if (role === "user") active.transcriptTurn = (active.transcriptTurn || 0) + 1;
      const turn = Number.isSafeInteger(generation) ? generation! : active.transcriptTurn || 0;
      setTranscript(items => [...items, { id, turn, role, text }]
        .sort((a, b) => a.turn - b.turn || (a.role === b.role ? a.id - b.id : a.role === "user" ? -1 : 1)).slice(-80));
    };
    active.release = claimChatAudio(() => { stop(); setOpen(false); });
    active.timer = setTimeout(() => fail("timeout"), 30000);
    try {
      if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) {
        fail("unsupported");
        return;
      }
      const audio = new LiveChatAudio();
      active.audio = audio;
      await audio.start(payload => {
        if (call.current !== active || !active.ready || active.socket?.readyState !== WebSocket.OPEN) return;
        if (active.socket.bufferedAmount > 256000) { fail("slowConnection"); return; }
        active.socket.send(JSON.stringify({ type: "audio", audio: payload }));
      }, itemId => {
        if (call.current !== active) return;
        if (!active.inputPaused) setPhase(active.awaitingReply ? "synthesizing" : "listening");
        if (itemId && active.socket?.readyState === WebSocket.OPEN) {
          active.socket.send(JSON.stringify({ type: "clip_done", item_id: itemId }));
        }
      }, () => fail("microphoneDisconnected"), level => {
        if (call.current === active) setInputLevel(level);
      });
      if (call.current !== active) return;
      const url = new URL("/api/v1/audio/live", window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(url.toString());
      active.socket = socket;
      socket.onopen = () => {
        if (call.current !== active) { socket.close(); return; }
        // Server setup is bounded to 45s, including Realtime fallback. Start
        // its browser deadline after microphone permission and WS opening.
        clearTimeout(active.timer);
        active.timer = setTimeout(() => fail("timeout"), 60000);
        socket.send(JSON.stringify({ type: "start", token: getAuthToken() || undefined,
          ...(snapshot.publicToken ? { public_token: snapshot.publicToken, session_id: snapshot.sessionId } : {
            conversation_id: snapshot.conversationId || undefined, workspace_id: snapshot.workspaceId || undefined,
            agent_id: snapshot.agentId || undefined, thread_ref_kind: snapshot.threadRefKind, thread_ref_id: snapshot.threadRefId,
          }),
          voice: voiceRef.current,
        }));
      };
      socket.onmessage = ({ data }) => {
        if (call.current !== active) return;
        try {
          const event = JSON.parse(data);
          switch (event.type) {
            case "ready":
              clearTimeout(active.timer); active.ready = true;
              active.conversationId = event.conversation_id;
              active.generation = Number.isSafeInteger(event.generation) ? event.generation : undefined;
              if (event.duplex_mode === "push_to_interrupt") {
                audio.setDuplexMode("push_to_interrupt");
                setDuplexMode("push_to_interrupt");
              }
              setTransportMode(event.transport_mode === "turn_based" || event.duplex_mode === "push_to_interrupt"
                ? "turn_based" : "native_realtime");
              if (CALL_VOICES.includes(event.voice)) {
                setVoice(event.voice); voiceRef.current = event.voice;
              }
              setVoiceLocked(Boolean(event.voice_locked));
              if (!snapshot.publicToken) active.key = chatVoiceScopeKey({ ...snapshot, conversationId: event.conversation_id });
              setPhase("listening"); setStartedAt(Date.now());
              current.current.onConversation?.(event.conversation_id);
              break;
            case "ping": socket.send(JSON.stringify({ type: "pong" })); break;
            case "input_started":
              if (active.generation !== undefined && event.generation !== active.generation) break;
              if (!active.inputPaused) {
                active.inputPaused = true; audio.pause();
                setPhase(phase => { active.resumePhase = phase; return "listening"; });
              }
              break;
            case "input_empty":
              if (active.generation !== undefined && event.generation !== active.generation) break;
              active.inputPaused = false;
              setPhase(audio.resume() ? "speaking" : active.awaitingReply ? "synthesizing" : active.resumePhase || "listening");
              active.resumePhase = undefined;
              break;
            case "transcribing":
            case "thinking":
              if (active.generation === undefined || event.generation === active.generation) setPhase(event.type);
              break;
            case "synthesizing":
              if (active.generation === undefined || event.generation === active.generation) {
                active.awaitingReply = true;
                if (active.inputPaused) active.resumePhase = "synthesizing";
                else setPhase(phase => phase === "speaking" ? phase : "synthesizing");
              }
              break;
            case "listening":
              if (active.generation === undefined || event.generation === active.generation) {
                active.awaitingReply = false;
                if (active.inputPaused) active.resumePhase = "listening";
                else setPhase("listening");
              }
              break;
            case "work":
              setBackgroundWorking(event.status === "running" || event.status === "queued");
              break;
            case "interrupt": {
              if (Number.isSafeInteger(event.generation)) {
                if (active.generation !== undefined && event.generation < active.generation) break;
                active.generation = event.generation;
              }
              const played = audio.interrupt();
              active.awaitingReply = false;
              active.inputPaused = false; active.resumePhase = undefined;
              if (event.item_id) socket.send(JSON.stringify({ type: "played", ...played, item_id: event.item_id }));
              active.captionItem = undefined; setCaption(""); setPhase("listening");
              break;
            }
            case "audio":
              setVoiceLocked(true); audio.play(event.audio, event.item_id); setPhase("speaking"); break;
            case "audio_clip":
              if (active.generation !== undefined && event.generation !== active.generation) break;
              void audio.playClip(event.audio, event.item_id).then(played => {
                if (played && call.current === active) setPhase("speaking");
              }).catch(() => fail("playback"));
              break;
            case "audio_done":
              active.awaitingReply = false;
              audio.finish(event.item_id);
              break;
            case "caption":
              if (active.generation !== undefined && event.generation !== active.generation) break;
              if (active.captionItem !== event.item_id) { active.captionItem = event.item_id; setCaption(event.delta); }
              else setCaption(text => text + event.delta);
              break;
            case "transcript": {
              appendTranscript("user", event.text, event.generation);
              setCaption("");
              break;
            }
            case "turn":
              if (
                typeof event.conversation_id === "string"
                && event.conversation_id === active.conversationId
              ) {
                current.current.onConversation?.(event.conversation_id);
              }
              if (event.text) {
                // A completed Chat reply remains readable even if speaking
                // again cancelled its audio. It belongs to this call's log.
                appendTranscript("assistant", event.text, event.generation);
              }
              if (event.generation === undefined || active.generation === undefined || event.generation === active.generation) {
                active.captionItem = undefined; setCaption("");
                if (!event.text) { active.awaitingReply = false; setPhase("listening"); }
              }
              break;
            case "voice":
              if (CALL_VOICES.includes(event.voice)) {
                setVoice(event.voice); voiceRef.current = event.voice;
                persistCallVoice(event.voice);
              }
              setVoiceLocked(Boolean(event.locked));
              break;
            case "error":
              fail(event.status === 504 ? "timeout" : event.status === 429 ? "busy"
                : event.status === 503 ? "unavailable" : "failed");
              break;
          }
        } catch { fail("connection"); }
      };
      socket.onerror = () => fail("connection");
      socket.onclose = () => fail("disconnected");
    } catch (cause) {
      fail(cause instanceof DOMException && cause.name === "NotAllowedError" ? "permissionDenied" : "failed");
    }
  }, [stop]);

  const key = chatVoiceScopeKey(scope);
  useEffect(() => {
    if (call.current && (disabled || call.current.key !== key)) { stop(); setOpen(false); }
  }, [key, disabled, stop]);
  useEffect(() => () => stop(), [stop]);
  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    if (next) setInputLevel(0);
    if (call.current) call.current.muted = next;
    call.current?.audio?.setMuted(next);
    if (call.current?.socket?.readyState === WebSocket.OPEN) {
      call.current.socket.send(JSON.stringify({ type: "mute", muted: next }));
    }
  };
  const interruptForSpeech = () => {
    const active = call.current;
    if (!active?.audio || active.socket?.readyState !== WebSocket.OPEN) return;
    const played = active.audio.interrupt();
    active.awaitingReply = false;
    active.inputPaused = false;
    active.resumePhase = undefined;
    active.captionItem = undefined;
    setCaption(""); setPhase("listening");
    active.socket.send(JSON.stringify({ type: "stop_reply", item_id: played.item_id }));
  };
  const selectVoice = (next: string) => {
    if (!CALL_VOICES.includes(next as CallVoice) || voiceLocked) return;
    const selected = next as CallVoice;
    setVoice(selected); voiceRef.current = selected;
    persistCallVoice(selected);
    if (call.current?.socket?.readyState === WebSocket.OPEN) {
      call.current.socket.send(JSON.stringify({ type: "voice", voice: selected }));
    }
  };
  return { phase, open, muted, caption, transcript, error, startedAt, start, stop, toggleMute,
    inputLevel, duplexMode, transportMode, backgroundWorking, interruptForSpeech, voice, voiceLocked, selectVoice,
    close: () => { stop(); setOpen(false); } };
}
