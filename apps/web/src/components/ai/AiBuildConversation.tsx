import { useEffect, useRef, useState, type ReactNode } from "react";
import MessageRow from "../chat/MessageRow";
import MessageBubble from "../chat/MessageBubble";
import PanelComposer from "../chat/PanelComposer";
import ManorAvatar from "../ui/ManorAvatar";
import { t } from "../../lib/i18n";

type Phase = "describe" | "clarify" | "generating" | "review";
type Msg = { id: number; role: "ai" | "user"; text: string };

export interface AiBuildReview {
  title?: string;
  content: ReactNode;
  confirmLabel?: string;
  reviseLabel?: string;
  onConfirm: () => Promise<void>;
}

export interface AiBuildConversationProps {
  intro: string;
  describePlaceholder: string;
  answersPlaceholder: string;
  buildingHint: string;
  draftQuestions: (prompt: string) => Promise<{ questions: string[]; ready: boolean }>;
  generate: (prompt: string, onStep: (label: string) => void) => Promise<void | AiBuildReview>;
  onManual?: () => void;
  manualLabel?: string;
}

let _mid = 0;
const nextId = () => ++_mid;

/** Cycling "…" so a long-running step looks alive, not frozen. */
function AnimatedDots() {
  const [n, setN] = useState(1);
  useEffect(() => {
    const id = setInterval(() => setN((x) => (x % 3) + 1), 400);
    return () => clearInterval(id);
  }, []);
  return <span>{".".repeat(n)}</span>;
}

/** Reveal text one chunk at a time so AI messages "stream" in like the real
 *  Manor AI chat. Types once per message (stable key), then calls onTick so the
 *  transcript keeps scrolling. */
function TypedText({ text, onTick }: { text: string; onTick?: () => void }) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    setShown(0);
    if (!text) return;
    let i = 0;
    const id = setInterval(() => {
      i = Math.min(text.length, i + 2);
      setShown(i);
      onTick?.();
      if (i >= text.length) clearInterval(id);
    }, 16);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  return (
    <span style={{ whiteSpace: "pre-wrap" }}>
      {text.slice(0, shown)}
      {shown < text.length && <span style={{ opacity: 0.5 }}>▌</span>}
    </span>
  );
}

/**
 * Chat-style "Build with AI" conversation that reuses the FloatingChat UI:
 * ManorAvatar + MessageRow/MessageBubble for a streaming transcript, and
 * PanelComposer for the standard input box. Describe → the AI asks 1-3
 * numbered clarifying questions → answer → generate.
 */
export function AiBuildConversation({
  intro,
  describePlaceholder,
  answersPlaceholder,
  buildingHint,
  draftQuestions,
  generate,
  onManual,
  manualLabel,
}: AiBuildConversationProps) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [phase, setPhase] = useState<Phase>("describe");
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [description, setDescription] = useState("");
  const [questions, setQuestions] = useState<string[]>([]);
  const [step, setStep] = useState("");
  const [review, setReview] = useState<AiBuildReview | null>(null);
  const [confirming, setConfirming] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const scrollDown = () => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  const pushAi = (text: string) => setMessages((m) => [...m, { id: nextId(), role: "ai", text }]);
  const pushUser = (text: string) => setMessages((m) => [...m, { id: nextId(), role: "user", text }]);

  // Greet once on mount.
  useEffect(() => {
    setMessages([{ id: nextId(), role: "ai", text: intro }]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(scrollDown, [messages.length, phase, thinking, step]);

  const runGenerate = async (prompt: string) => {
    setPhase("generating");
    setStep("");
    setReview(null);
    try {
      const result = await generate(prompt, setStep);
      if (result) {
        setReview(result);
        pushAi(result.title || t("page.agent_form.ai_review_title"));
        setPhase("review");
      }
    } catch {
      // The caller toasts the error; revert so the user can retry.
      setPhase(questions.length ? "clarify" : "describe");
    } finally {
      setStep("");
    }
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || thinking || phase === "generating" || phase === "review") return;
    pushUser(text);
    setInput("");

    if (phase === "describe") {
      setDescription(text);
      setThinking(true);
      try {
        const res = await draftQuestions(text);
        if (res.ready || !res.questions?.length) {
          setQuestions([]);
          await runGenerate(text);
        } else {
          setQuestions(res.questions);
          pushAi(
            `${t("page.skill_form.ai_questions_title")}\n` +
              res.questions.map((q, i) => `${i + 1}. ${q}`).join("\n"),
          );
          setPhase("clarify");
        }
      } catch {
        await runGenerate(text);
      } finally {
        setThinking(false);
      }
    } else if (phase === "clarify") {
      const prompt =
        description +
        "\n\nClarifications:\n" +
        questions.map((q, i) => `Q${i + 1}: ${q}`).join("\n") +
        `\n\nAnswers:\n${text}`;
      await runGenerate(prompt);
    }
  };

  const handleConfirmReview = async () => {
    if (!review || confirming) return;
    setConfirming(true);
    try {
      await review.onConfirm();
    } catch {
      // The caller owns the toast/error copy; keep the review visible.
    } finally {
      setConfirming(false);
    }
  };

  const handleReviseReview = () => {
    setReview(null);
    setQuestions([]);
    setDescription("");
    setInput("");
    setPhase("describe");
    pushAi(t("page.agent_form.ai_revision_prompt"));
  };

  return (
    <div className="ai-build-conversation" style={{ display: "flex", flexDirection: "column", height: 440, maxHeight: "62vh" }}>
      <div
        ref={scrollRef}
        style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 12, padding: "4px 2px" }}
      >
        {messages.map((m) => (
          <MessageRow
            key={m.id}
            role={m.role === "user" ? "user" : "other"}
            avatar={m.role === "ai" ? <ManorAvatar size={26} /> : undefined}
          >
            <MessageBubble role={m.role === "user" ? "user" : "other"}>
              {m.role === "ai" ? <TypedText text={m.text} onTick={scrollDown} /> : m.text}
            </MessageBubble>
          </MessageRow>
        ))}
        {(thinking || phase === "generating") && (
          <MessageRow role="other" avatar={<ManorAvatar size={26} />}>
            <MessageBubble role="other">
              <span style={{ opacity: 0.7 }}>
                {phase === "generating" ? (step || buildingHint) : t("page.skill_form.ai_thinking")}
                <AnimatedDots />
              </span>
            </MessageBubble>
          </MessageRow>
        )}
        {review && phase === "review" && (
          <MessageRow role="other" avatar={<ManorAvatar size={26} />}>
            <MessageBubble
              role="other"
              style={{
                width: "min(100%, 520px)",
                background: "var(--surface-muted)",
                borderColor: "var(--border-subtle)",
                padding: 0,
                overflow: "hidden",
              }}
            >
              <div style={{ padding: 14 }}>{review.content}</div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "flex-end",
                  gap: 8,
                  padding: "10px 12px",
                  borderTop: "1px solid var(--border-subtle)",
                  background: "var(--surface-sunken)",
                }}
              >
                <button
                  type="button"
                  onClick={handleReviseReview}
                  disabled={confirming}
                  style={{
                    border: "1px solid var(--border-subtle)",
                    borderRadius: 8,
                    background: "var(--surface-panel)",
                    color: "var(--text-muted)",
                    fontSize: 12,
                    fontWeight: 700,
                    padding: "7px 11px",
                    cursor: confirming ? "not-allowed" : "pointer",
                  }}
                >
                  {review.reviseLabel || t("page.agent_form.ai_revise")}
                </button>
                <button
                  type="button"
                  onClick={handleConfirmReview}
                  disabled={confirming}
                  style={{
                    border: "1px solid var(--accent)",
                    borderRadius: 8,
                    background: "var(--accent)",
                    color: "#fff",
                    fontSize: 12,
                    fontWeight: 800,
                    padding: "7px 12px",
                    cursor: confirming ? "not-allowed" : "pointer",
                    opacity: confirming ? 0.72 : 1,
                  }}
                >
                  {confirming
                    ? t("page.agents.saving")
                    : review.confirmLabel || t("page.agent_form.ai_confirm_create")}
                </button>
              </div>
            </MessageBubble>
          </MessageRow>
        )}
      </div>

      {phase !== "generating" && phase !== "review" && (
        <PanelComposer
          value={input}
          onChange={setInput}
          onSend={handleSend}
          sending={thinking}
          placeholder={phase === "describe" ? describePlaceholder : answersPlaceholder}
          autoFocus
          toolbarSlot={
            onManual ? (
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button
                  type="button"
                  onClick={onManual}
                  style={{ background: "none", border: "none", color: "var(--text-muted)", fontSize: 12, cursor: "pointer", padding: 0 }}
                >
                  {manualLabel}
                </button>
              </div>
            ) : undefined
          }
        />
      )}
    </div>
  );
}
