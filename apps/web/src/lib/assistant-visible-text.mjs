const MANOR_FINAL_RESPONSE_OPEN_TAG = "<manor-final-response>";
const MANOR_FINAL_RESPONSE_TAG_RE = /<\/?manor-final-response>\s*/gi;
const HIDDEN_EDITOR_LIVE_TAG_PREFIXES = [
  "<manor-live-patch",
  "</manor-live-patch",
  "<manor-live-edit",
  "</manor-live-edit",
  "<manor live patch",
  "</manor live patch",
  "<manor live edit",
  "</manor live edit",
];
const INTERNAL_ERROR_DETAIL_RE =
  /(?:sqlalchemy|asyncpg|psycopg|InFailedSQLTransactionError|current\s+transaction\s+is\s+aborted|Traceback\s+\(most\s+recent\s+call\s+last\)|\[SQL:|\[parameters?:)/i;
const USER_FACING_ERROR_DETAIL_RE =
  /(?:^|(?:\r?\n\s*)+)(?:Error detail:\s*|──\s*Error detail\s*──\s*)/i;
const ASSISTANT_FAILURE_PREFIX_RE =
  /^(?:Sorry,\s+the request failed(?:\.\s+Please try again\.)?|Internal (?:server )?error\b)/i;
const GENERIC_ASSISTANT_FAILURE = "Sorry, the request failed. Please try again.";

function stripTrailingEditorLiveTagFragment(text) {
  const tagStart = text.lastIndexOf("<");
  if (tagStart < 0) return text;
  const fragment = text.slice(tagStart).toLowerCase();
  if (!fragment || fragment.includes(">")) return text;
  if (!fragment.startsWith("<manor") && !fragment.startsWith("</manor")) return text;
  return HIDDEN_EDITOR_LIVE_TAG_PREFIXES.some(
    (prefix) => prefix.startsWith(fragment) || fragment.startsWith(prefix),
  )
    ? text.slice(0, tagStart)
    : text;
}

export function stripEditorLiveEditBlocks(value) {
  const text = typeof value === "string" ? value : "";
  const withoutProtocolBlocks = text
    .replace(
      /<manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>[\s\S]*?<\/manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)\s*>/gi,
      "",
    )
    .replace(
      /<manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>[\s\S]*$/i,
      "",
    )
    .replace(
      /<\/?manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>/gi,
      "",
    );
  return stripTrailingEditorLiveTagFragment(withoutProtocolBlocks).trim();
}

/** Hide internal stack/database details from previously persisted assistant failures. */
export function redactInternalAssistantErrorDetails(value) {
  const text = typeof value === "string" ? value : "";
  if (!INTERNAL_ERROR_DETAIL_RE.test(text)) return text;
  const detailMarker = USER_FACING_ERROR_DETAIL_RE.exec(text);
  if (!detailMarker) return text;
  const prefix = text.slice(0, detailMarker.index).trim();
  if (prefix && !ASSISTANT_FAILURE_PREFIX_RE.test(prefix)) return text;
  return prefix || GENERIC_ASSISTANT_FAILURE;
}

/** Return only the assistant text eligible for user-visible chat surfaces. */
export function visibleAssistantText(value) {
  const source = typeof value === "string" ? value : "";
  const withoutProtocol = stripEditorLiveEditBlocks(source);
  const markerIndex = withoutProtocol
    .toLowerCase()
    .lastIndexOf(MANOR_FINAL_RESPONSE_OPEN_TAG);
  const visible = markerIndex >= 0
    ? withoutProtocol.slice(markerIndex + MANOR_FINAL_RESPONSE_OPEN_TAG.length)
    : withoutProtocol;
  return visible.replace(MANOR_FINAL_RESPONSE_TAG_RE, "").trim();
}
