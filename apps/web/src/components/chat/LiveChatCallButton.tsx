import { useEffect, useRef, useState } from "react";
import type { ChatVoiceScope } from "../../lib/chatVoice";
import { getLocale, tForLocale } from "../../lib/i18n";
import { useLiveChatCall } from "../../lib/useLiveChatCall";
import { IconAudioWave, IconMicrophone, IconPhone } from "../icons";
import AgentActivityOrb from "../ui/AgentActivityOrb";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import Select from "../ui/Select";
import "./LiveChatCallButton.css";

export default function LiveChatCallButton({ scope, disabled, onConversation, locale = getLocale() }: {
  scope: ChatVoiceScope; disabled?: boolean; onConversation?: (id: string) => void; locale?: string;
}) {
  const call = useLiveChatCall({ scope, disabled, onConversation });
  const say = (key: string) => tForLocale(`chat.call.${key}`, locale);
  const [seconds, setSeconds] = useState(0);
  const [reducedMotion, setReducedMotion] = useState(false);
  const captions = useRef<HTMLDivElement>(null);
  const active = !["idle", "ended", "error"].includes(call.phase);
  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(preference.matches);
    update(); preference.addEventListener("change", update);
    return () => preference.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!call.startedAt || !active) { setSeconds(0); return; }
    const update = () => setSeconds(Math.floor((Date.now() - call.startedAt!) / 1000));
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [call.startedAt, active]);
  useEffect(() => {
    if (captions.current) captions.current.scrollTop = captions.current.scrollHeight;
  }, [call.caption, call.transcript]);
  const label = say(call.muted && active
    ? "muted"
    : call.phase === "listening" && call.backgroundWorking
      ? "working"
      : call.phase);
  const activity = call.phase === "connecting" ? "connecting"
    : call.backgroundWorking || ["transcribing", "thinking", "synthesizing"].includes(call.phase) ? "solving"
    : call.phase === "speaking" ? "composing" : "listening";
  const voiceOptions = ["warm", "clear", "bright", "deep"].map(value => ({
    value,
    label: say(`voice.${value}`),
  }));
  return <>
    <button type="button" className="chat-composer-icon-btn" disabled={disabled}
      aria-label={say("start")} title={say("start")} onClick={() => void call.start()}>
      <IconAudioWave size={18} />
    </button>
    <Modal open={call.open} onClose={call.close} title={say("title")} maxWidth="400px" bodyClassName="live-chat-call">
      <div className="live-chat-call__meta">
        <span className="live-chat-call__timer" aria-label={say("duration")}>
          {String(Math.floor(seconds / 60)).padStart(2, "0")}:{String(seconds % 60).padStart(2, "0")}
        </span>
        {call.transportMode && <span className={`live-chat-call__mode live-chat-call__mode--${call.transportMode}`}>
          {say(call.transportMode === "native_realtime" ? "modeRealtime" : "modeTurnBased")}
        </span>}
      </div>
      <div className="live-chat-call__orb" aria-hidden="true">
        <AgentActivityOrb activity={activity} displaySize={112} size={64} iconOnly paused={!active || call.muted || reducedMotion} />
      </div>
      <p className="live-chat-call__state" role="status">{label}</p>
      <div className="live-chat-call__settings">
        <Select value={call.voice} onChange={call.selectVoice} options={voiceOptions}
          disabled={active && call.voiceLocked} ariaLabel={say("voice")}
          style={{ width: 132 }} dropdownMinWidth={132}
          buttonStyle={{ height: 36, minHeight: 36, padding: "0 12px", border: "none", background: "var(--surface-muted)" }} />
        {active && <div className="live-chat-call__input" aria-label={say("inputLevel")}>
          <IconMicrophone size={14} />
          <meter min={0} max={1} value={call.inputLevel} aria-label={say("inputLevel")} />
        </div>}
      </div>
      <div className="live-chat-call__captions" ref={captions}
        role="log" aria-label={say("captions")} aria-live="polite">
        {call.transcript.map(item => <p key={item.id} className={item.role === "user" ? "live-chat-call__user" : undefined}>
          <span>{item.role === "user" ? say("you") : "AI"}</span>{item.text}
        </p>)}
        {call.caption && call.transcript.at(-1)?.role !== "assistant" && <p><span>AI</span>{call.caption}</p>}
      </div>
      {call.error && <p className="live-chat-call__error" role="alert">{say(`error.${call.error}`)}</p>}
      <div className="live-chat-call__actions">
        {active ? <>
          <Button variant="outline" onClick={call.toggleMute} disabled={call.phase === "connecting"}
            ariaPressed={call.muted}>
            <IconMicrophone size={18} />
            {say(call.muted ? "unmute" : "mute")}
          </Button>
          {call.duplexMode === "push_to_interrupt" && call.phase === "speaking" &&
            <Button variant="outline" onClick={call.interruptForSpeech}>
              <IconAudioWave size={18} />{say("interrupt")}
            </Button>}
          <Button variant="danger" onClick={call.close}><IconPhone size={18} />{say("end")}</Button>
        </> : <Button onClick={() => void call.start()}>{say("retry")}</Button>}
      </div>
      <p className="live-chat-call__disclosure">{say("disclosureShort")}</p>
    </Modal>
  </>;
}
