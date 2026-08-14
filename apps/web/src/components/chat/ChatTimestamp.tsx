import { useMemo, useState } from "react";
import { getLocale, t } from "../../lib/i18n";

const LOCALES = {
  de: "de-DE",
  en: "en-US",
  es: "es-ES",
  zh: "zh-CN",
} as const;

function parsedTimestamp(timestamp?: string | null): Date | null {
  if (!timestamp) return null;
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatChatTime(timestamp?: string | null): string {
  const date = parsedTimestamp(timestamp);
  if (!date) return "";
  return date.toLocaleTimeString(LOCALES[getLocale()] || undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatChatDateTime(timestamp?: string | null): string {
  const date = parsedTimestamp(timestamp);
  if (!date) return "";
  return date.toLocaleString(LOCALES[getLocale()] || undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** A compact timestamp that reveals the recorded calendar date on demand. */
export default function ChatTimestamp({
  timestamp,
  className = "chat-timestamp",
}: {
  timestamp?: string | null;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const locale = getLocale();
  const { shortLabel, fullLabel } = useMemo(
    () => ({
      shortLabel: formatChatTime(timestamp),
      fullLabel: formatChatDateTime(timestamp),
    }),
    [locale, timestamp],
  );

  if (!shortLabel || !fullLabel) return null;

  return (
    <button
      type="button"
      className={`chat-timestamp-button ${className}`.trim()}
      aria-expanded={expanded}
      aria-label={t(
        expanded
          ? "component.chat_timestamp.show_time_only"
          : "component.chat_timestamp.show_full_date",
        { timestamp: fullLabel },
      )}
      title={fullLabel}
      onClick={() => setExpanded((current) => !current)}
    >
      {expanded ? fullLabel : shortLabel}
    </button>
  );
}
