import { useMemo, useState } from "react";
import { getPreferredTimeZone } from "../../lib/format";
import { getLocale, t } from "../../lib/i18n";

const LOCALES = {
  de: "de-DE",
  en: "en-US",
  es: "es-ES",
  fr: "fr-FR",
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
    timeZone: getPreferredTimeZone(),
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
    second: "2-digit",
    timeZone: getPreferredTimeZone(),
  });
}

/** The calendar day of a timestamp in the viewer's time zone, as YYYY-MM-DD. */
export function chatDayKey(timestamp?: string | null): string {
  const date = parsedTimestamp(timestamp);
  if (!date) return "";
  return date.toLocaleDateString("en-CA", { timeZone: getPreferredTimeZone() });
}

/** Date divider copy: "Today · Oct 26", "Yesterday · Oct 25", "Oct 24". */
export function formatChatDayLabel(
  timestamp?: string | null,
  now: Date = new Date(),
): string {
  const date = parsedTimestamp(timestamp);
  if (!date) return "";
  const timeZone = getPreferredTimeZone();
  const day = chatDayKey(timestamp);
  const today = now.toLocaleDateString("en-CA", { timeZone });
  // Move one calendar day in the viewer's zone; DST days need not last 24 hours.
  const previousDay = new Date(`${today}T12:00:00Z`);
  previousDay.setUTCDate(previousDay.getUTCDate() - 1);
  const yesterday = previousDay.toISOString().slice(0, 10);
  const sameYear = day.slice(0, 4) === today.slice(0, 4);
  const calendar = date.toLocaleDateString(LOCALES[getLocale()] || undefined, {
    ...(sameYear ? {} : { year: "numeric" }),
    month: "short",
    day: "numeric",
    timeZone,
  });
  if (day === today) return `${t("component.chat_timestamp.today")} · ${calendar}`;
  if (day === yesterday) return `${t("component.chat_timestamp.yesterday")} · ${calendar}`;
  return calendar;
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
  const timeZone = getPreferredTimeZone();
  const { shortLabel, fullLabel } = useMemo(
    () => ({
      shortLabel: formatChatTime(timestamp),
      fullLabel: formatChatDateTime(timestamp),
    }),
    [locale, timeZone, timestamp],
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
