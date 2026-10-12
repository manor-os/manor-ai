import { useCallback, useEffect, useRef, useState } from "react";
import { getLocale, tForLocale } from "../../lib/i18n";
import { chatVoiceScopeKey, claimChatAudio, fetchChatSpeech, speechChunks, type ChatVoiceScope } from "../../lib/chatVoice";
import type { ChatVoiceInput } from "../../lib/useChatVoiceInput";
import { IconClose, IconMicrophone, IconStop } from "../icons";
import ChatActionButton from "./ChatActionButton";
import "./ChatVoiceControls.css";

export function VoiceInputButton({ voice, disabled = false }: { voice: ChatVoiceInput; disabled?: boolean }) {
  const say = (key: string) => tForLocale(`chat.voice.${key}`, voice.locale);
  const label = say(voice.phase === "recording" ? "stop_recording" : voice.busy ? voice.phase : "record");
  return (
    <span className="chat-voice-input-actions">
      <button type="button" className={`chat-composer-icon-btn ${voice.phase === "recording" ? "chat-composer-icon-btn--recording" : ""}`}
        title={label} aria-label={label} aria-pressed={voice.phase === "recording"}
        disabled={disabled || (voice.busy && voice.phase !== "recording")}
        onClick={() => voice.phase === "recording" ? voice.stop() : void voice.start()}>
        {voice.phase === "recording" ? <IconStop size={16} /> : <IconMicrophone size={17} />}
      </button>
      {voice.busy && <button type="button" className="chat-composer-icon-btn" title={say("cancel")} aria-label={say("cancel")} onClick={voice.cancel}><IconClose size={15} /></button>}
    </span>
  );
}

export function VoiceInputStatus({ voice }: { voice: ChatVoiceInput }) {
  if (!voice.busy && !voice.error) return null;
  return <div className={`chat-voice-status ${voice.error ? "chat-voice-status--error" : ""}`} role={voice.error ? "alert" : "status"}>
    {voice.error || tForLocale(`chat.voice.${voice.phase}`, voice.locale)}
  </div>;
}

export function ReadAloudButton({ text, scope = {}, messageId, disabled = false, locale = getLocale() }: {
  text: string; scope?: ChatVoiceScope; messageId?: string; disabled?: boolean; locale?: string;
}) {
  const [phase, setPhase] = useState<"idle" | "loading" | "playing" | "ready">("idle");
  const [error, setError] = useState("");
  const audioContext = useRef<AudioContext | null>(null);
  const audioSource = useRef<AudioBufferSourceNode | null>(null);
  const playReadyAudio = useRef<(() => Promise<void>) | null>(null);
  const controller = useRef<AbortController | null>(null);
  const release = useRef<(() => void)>();
  const generation = useRef(0);
  const scopeKey = chatVoiceScopeKey(scope);
  const say = (key: string) => tForLocale(`chat.voice.${key}`, locale);

  const stop = useCallback(() => {
    generation.current++;
    controller.current?.abort();
    controller.current = null;
    playReadyAudio.current = null;
    if (audioSource.current) {
      audioSource.current.onended = null;
      try { audioSource.current.stop(); } catch { /* already stopped */ }
      audioSource.current.disconnect();
      audioSource.current = null;
    }
    if (audioContext.current) {
      void audioContext.current.close().catch(() => undefined);
      audioContext.current = null;
    }
    release.current?.();
    release.current = undefined;
    setPhase("idle");
  }, []);

  useEffect(() => { stop(); setError(""); return stop; }, [scopeKey, messageId, text, disabled, locale, stop]);

  const start = async () => {
    if (disabled) return;
    // Resume the already decoded clip with a fresh user gesture without
    // generating or charging for the speech a second time.
    if (phase === "ready" && playReadyAudio.current) {
      try { await playReadyAudio.current(); setError(""); }
      catch { setError(say("playback_failed")); }
      return;
    }
    if (controller.current) { stop(); return; }
    release.current = claimChatAudio(stop);
    const run = ++generation.current;
    const request = new AbortController();
    controller.current = request;
    let player: AudioContext;
    try {
      player = new AudioContext();
    } catch {
      stop();
      setError(say("playback_failed"));
      return;
    }
    audioContext.current = player;
    // Start the browser's audio session during the click. Waiting for the TTS
    // response first loses transient user activation in Safari and Chromium,
    // which made a single click appear to do nothing.
    const initialResume = player.resume().catch(() => undefined);
    const ensureRunning = async () => {
      await initialResume;
      if (player.state !== "running") await player.resume().catch(() => undefined);
      return player.state === "running";
    };
    const chunks = speechChunks(text);
    let index = 0;
    setError("");
    const startBuffer = (buffer: AudioBuffer) => {
      if (run !== generation.current) return;
      const source = player.createBufferSource();
      source.buffer = buffer;
      source.connect(player.destination);
      source.onended = () => {
        if (audioSource.current === source) audioSource.current = null;
        source.disconnect();
        void playNext();
      };
      audioSource.current = source;
      playReadyAudio.current = null;
      source.start();
      setPhase("playing");
    };
    const playNext = async () => {
      if (run !== generation.current) return;
      if (index >= chunks.length) { stop(); return; }
      setPhase("loading");
      try {
        const blob = await fetchChatSpeech(chunks[index++], scope, request.signal, messageId, locale);
        if (run !== generation.current) return;
        let buffer: AudioBuffer;
        try {
          buffer = await player.decodeAudioData(await blob.arrayBuffer());
        } catch {
          throw new Error(say("playback_failed"));
        }
        if (run !== generation.current) return;
        if (await ensureRunning()) {
          startBuffer(buffer);
        } else {
          playReadyAudio.current = async () => {
            if (!(await ensureRunning())) throw new Error("Audio playback is blocked");
            startBuffer(buffer);
          };
          setPhase("ready");
          setError(say("tap_to_play"));
        }
      } catch (err) {
        if (run === generation.current) { stop(); setError(err instanceof Error ? err.message : say("playback_failed")); }
      }
    };
    void playNext();
  };

  const active = phase === "loading" || phase === "playing";
  const label = say(active ? "stop_playback" : phase === "ready" ? "play" : "read_aloud");
  return <span className="chat-voice-playback">
    <ChatActionButton disabled={disabled || !text.trim()} active={active} title={label} aria-label={label} aria-pressed={active} onClick={() => void start()}>
      {active ? <IconStop size={13} /> : <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14" /></svg>}
    </ChatActionButton>
    {phase === "loading" && <span className="sr-only" role="status">{say("loading")}</span>}
    {error && <span className="chat-voice-playback-error" role="alert">{error}</span>}
  </span>;
}
