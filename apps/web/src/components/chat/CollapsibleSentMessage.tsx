import { type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";

export default function CollapsibleSentMessage({
  text,
  children,
  enabled = true,
  tone = "user",
}: {
  text: string;
  children: ReactNode;
  enabled?: boolean;
  tone?: "user" | "assistant";
}) {
  const bodyId = useId();
  const bodyRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    setExpanded(false);
  }, [text, enabled]);

  useLayoutEffect(() => {
    if (!enabled || expanded) return;
    const body = bodyRef.current;
    const content = contentRef.current;
    if (!body || !content) return;
    // Measure rendered content, not Markdown source: a long URL can occupy
    // one short line, while a narrow screen can wrap a short reply many times.
    const measure = () => setOverflowing(body.scrollHeight > body.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(body);
    observer.observe(content);
    return () => observer.disconnect();
  }, [text, enabled, expanded]);

  if (!enabled) return <>{children}</>;

  return (
    <div className={`chat-sent-message-collapse chat-sent-message-collapse--${tone}`}>
      <div
        id={bodyId}
        ref={bodyRef}
        className={`chat-sent-message-collapse__body${expanded ? "" : " chat-sent-message-collapse__body--collapsed"}`}
      >
        <div ref={contentRef} className="chat-sent-message-collapse__content">{children}</div>
      </div>
      {overflowing && !expanded && <span className="chat-sent-message-collapse__ellipsis" aria-hidden="true">...</span>}
      {(overflowing || expanded) && (
        <button
          type="button"
          className="chat-sent-message-collapse__toggle"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show less" : "Show all"}
        </button>
      )}
    </div>
  );
}
