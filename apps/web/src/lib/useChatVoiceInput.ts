import { useCallback, useEffect, useRef, useState } from "react";
import { getLocale, tForLocale } from "./i18n";
import { chatVoiceScopeKey, claimChatAudio, transcribeChatVoice, type ChatVoiceScope } from "./chatVoice";

export function useChatVoiceInput({
  scope = {}, disabled = false, onTranscript, locale = getLocale(),
}: { scope?: ChatVoiceScope; disabled?: boolean; onTranscript: (text: string) => void; locale?: string }) {
  const [phase, setPhase] = useState<"idle" | "requesting" | "recording" | "transcribing">("idle");
  const [error, setError] = useState("");
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const release = useRef<(() => void)>();
  const busy = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const latest = useRef({ scope, onTranscript, locale });
  latest.current = { scope, onTranscript, locale };
  const scopeKey = chatVoiceScopeKey(scope);

  const cancel = useCallback(() => {
    generation.current++;
    clearTimeout(timer.current);
    request.current?.abort();
    request.current = null;
    const rec = recorder.current;
    recorder.current = null;
    if (rec) {
      rec.ondataavailable = rec.onstop = rec.onerror = null;
      if (rec.state !== "inactive") rec.stop();
    }
    stream.current?.getTracks().forEach((track) => { track.onended = null; track.stop(); });
    stream.current = null;
    release.current?.();
    release.current = undefined;
    busy.current = false;
    setPhase("idle");
  }, []);

  useEffect(() => {
    cancel();
    setError("");
    return cancel;
  }, [scopeKey, disabled, cancel]);

  const stop = useCallback(() => {
    clearTimeout(timer.current);
    if (recorder.current?.state === "recording") {
      setPhase("transcribing");
      recorder.current.stop();
      stream.current?.getTracks().forEach((track) => { track.onended = null; track.stop(); });
      stream.current = null;
    }
  }, []);

  const start = useCallback(async () => {
    if (disabled || busy.current) return;
    const say = (key: string) => tForLocale(`chat.voice.${key}`, latest.current.locale);
    setError("");
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError(say("unsupported"));
      return;
    }
    release.current = claimChatAudio(cancel);
    busy.current = true;
    const run = ++generation.current;
    setPhase("requesting");
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (run !== generation.current) { media.getTracks().forEach((track) => track.stop()); return; }
      stream.current = media;
      const mimeType = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find((type) => MediaRecorder.isTypeSupported(type));
      const rec = new MediaRecorder(media, mimeType ? { mimeType } : undefined);
      recorder.current = rec;
      const chunks: Blob[] = [];
      let size = 0;
      rec.ondataavailable = (event) => {
        size += event.data.size;
        if (size > 10 * 1024 * 1024) { cancel(); setError(say("too_large")); return; }
        if (event.data.size) chunks.push(event.data);
      };
      rec.onerror = () => { cancel(); setError(say("recording_failed")); };
      rec.onstop = async () => {
        if (run !== generation.current) return;
        recorder.current = null;
        clearTimeout(timer.current);
        media.getTracks().forEach((track) => { track.onended = null; track.stop(); });
        stream.current = null;
        setPhase("transcribing");
        const controller = new AbortController();
        request.current = controller;
        try {
          const blob = new Blob(chunks, { type: rec.mimeType || mimeType || "audio/webm" });
          if (!blob.size) throw new Error(say("no_speech"));
          const text = await transcribeChatVoice(blob, latest.current.scope, controller.signal);
          if (run !== generation.current) return;
          if (text) latest.current.onTranscript(text);
          else setError(say("no_speech"));
        } catch (err) {
          if (run === generation.current) setError(err instanceof Error ? err.message : say("transcription_failed"));
        } finally {
          if (run === generation.current) { release.current?.(); release.current = undefined; busy.current = false; setPhase("idle"); request.current = null; }
        }
      };
      media.getTracks().forEach((track) => { track.onended = stop; });
      rec.start(1000);
      setPhase("recording");
      timer.current = setTimeout(stop, 120_000);
    } catch (err) {
      if (run !== generation.current) return;
      cancel();
      setError(say(err instanceof DOMException && err.name === "NotAllowedError" ? "permission_denied" : "recording_failed"));
    }
  }, [disabled, cancel, stop]);

  return { phase, error, busy: phase !== "idle", start, stop, cancel, locale };
}

export type ChatVoiceInput = ReturnType<typeof useChatVoiceInput>;
