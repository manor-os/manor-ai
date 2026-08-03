import { type ReactNode, useEffect, useMemo, useState } from "react";

const LONG_SENT_MESSAGE_CHARS = 520;
const LONG_SENT_MESSAGE_LINES = 8;

function shouldCollapseSentMessage(text: string) {
  const normalized = text.trim();
  if (!normalized) return false;
  return (
    normalized.length > LONG_SENT_MESSAGE_CHARS ||
    normalized.split(/\r?\n/).length > LONG_SENT_MESSAGE_LINES
  );
}

export default function CollapsibleSentMessage({
  text,
  children,
}: {
  text: string;
  children: ReactNode;
}) {
  const collapsible = useMemo(() => shouldCollapseSentMessage(text), [text]);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    setExpanded(false);
  }, [text]);

  if (!collapsible) return <>{children}</>;

  return (
    <div className="chat-sent-message-collapse">
      <div
        className={`chat-sent-message-collapse__body${expanded ? "" : " chat-sent-message-collapse__body--collapsed"}`}
      >
        {children}
      </div>
      {!expanded && <span className="chat-sent-message-collapse__ellipsis">...</span>}
      <button
        type="button"
        className="chat-sent-message-collapse__toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? "Show less" : "Show all"}
      </button>
    </div>
  );
}
