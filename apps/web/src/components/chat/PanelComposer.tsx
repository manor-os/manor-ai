import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * Lean message composer for chat-style panels. It is the simple shared core of
 * the Manor AI chat input: an auto-growing textarea plus a Send button,
 * Enter-to-send (Shift+Enter for a newline), with an optional toolbar slot.
 *
 * It deliberately reuses FloatChat's composer *design* — the same
 * `.chat-composer*` styles (rounded pill shell, focus ring, teal send icon) —
 * without pulling in the heavy ChatInputFooter, whose mentions, #docs, voice,
 * attachments and chat-mode machinery don't apply outside a conversation.
 */
export default function PanelComposer({
  value,
  onChange,
  onSend,
  placeholder,
  sending = false,
  disabled = false,
  autoFocus = false,
  toolbarSlot,
  maxHeight = 160,
}: {
  value: string;
  onChange: (next: string) => void;
  onSend: () => void;
  placeholder?: string;
  /** In-flight send — shows the button spinner and blocks re-send. */
  sending?: boolean;
  /** Hard-disable the whole composer regardless of contents. */
  disabled?: boolean;
  autoFocus?: boolean;
  /** Optional controls rendered above the input row. */
  toolbarSlot?: ReactNode;
  maxHeight?: number;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [focused, setFocused] = useState(false);

  // Auto-grow with the content, capped so it never eats the whole panel.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [value, maxHeight]);

  const canSend = value.trim().length > 0 && !sending && !disabled;

  return (
    <div style={{ padding: "10px 2px 2px" }}>
      {toolbarSlot && <div style={{ padding: "0 2px 8px" }}>{toolbarSlot}</div>}
      <div className={`chat-composer ${focused ? "chat-composer--focused" : ""}`}>
        <div className="chat-composer-input-wrap">
          <textarea
            ref={ref}
            className="chat-composer-textarea"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter is a newline. Never send while an IME
              // composition is active (Enter then only confirms the candidate —
              // e.g. Chinese/Japanese input), matching the Manor AI chat.
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing &&
                (e.nativeEvent as any).keyCode !== 229
              ) {
                e.preventDefault();
                if (canSend) onSend();
              }
            }}
            placeholder={placeholder}
            rows={2}
            autoFocus={autoFocus}
            disabled={disabled}
            style={{ maxHeight, resize: "none" }}
          />
        </div>
        <div className="chat-composer-row">
          <div style={{ flex: 1 }} />
          <button
            type="button"
            onClick={onSend}
            disabled={!canSend}
            className="chat-composer-send"
            aria-label="Send"
          >
            {sending ? (
              <span
                aria-hidden
                style={{
                  width: 15,
                  height: 15,
                  border: "2px solid currentColor",
                  borderTopColor: "transparent",
                  borderRadius: "50%",
                  animation: "spin 0.75s linear infinite",
                }}
              />
            ) : (
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M22 2L11 13" />
                <path d="M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
