import { useEffect, useRef, useState } from "react";

/** Highlight sidebar navigation targets without resolving the underlying action. */
export function usePendingActionHighlight(scope: string, messageIds: readonly string[]) {
  const [highlighted, setHighlighted] = useState<{ scope: string; id: string } | null>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setHighlighted(null);
    return () => {
      if (highlightTimer.current !== null) clearTimeout(highlightTimer.current);
    };
  }, [scope]);

  return {
    highlightedMessageId: highlighted?.scope === scope && messageIds.includes(highlighted.id) ? highlighted.id : null,
    highlight: (id: string) => {
      if (highlightTimer.current !== null) clearTimeout(highlightTimer.current);
      setHighlighted({ scope, id });
      highlightTimer.current = setTimeout(() => setHighlighted(null), 2600);
    },
  };
}
