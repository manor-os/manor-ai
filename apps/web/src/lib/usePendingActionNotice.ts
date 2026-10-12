import { useEffect, useRef, useState } from "react";

/** Presentation state only: hiding a notice never resolves its pending actions. */
export function usePendingActionNotice(scope: string, messageIds: readonly string[]) {
  const [dismissed, setDismissed] = useState<{ scope: string; ids: Set<string> } | null>(null);
  const [highlighted, setHighlighted] = useState<{ scope: string; id: string } | null>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setHighlighted(null);
    return () => {
      if (highlightTimer.current !== null) clearTimeout(highlightTimer.current);
    };
  }, [scope]);

  return {
    hidden: dismissed?.scope === scope && messageIds.every((id) => dismissed.ids.has(id)),
    dismiss: () => setDismissed({ scope, ids: new Set(messageIds) }),
    highlightedMessageId: highlighted?.scope === scope && messageIds.includes(highlighted.id) ? highlighted.id : null,
    highlight: (id: string) => {
      if (highlightTimer.current !== null) clearTimeout(highlightTimer.current);
      setHighlighted({ scope, id });
      highlightTimer.current = setTimeout(() => setHighlighted(null), 2600);
    },
  };
}
