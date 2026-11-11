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

// What may keep an ID or key: code, links and the file/route references the
// chat turns into cards. Everything else is prose a person reads.
const PROTECTED_REFERENCE_RES = [
  /```[\s\S]*?```|~~~[\s\S]*?~~~/g,
  /!?\[(?:\\.|[^\]\\])*\]\((?:\\.|[^)\\])*\)/g,
  /<(?:https?:\/\/|mailto:|manor-file:|\/(?:viewer|api\/v1\/fs)\/)[^>\n]+>/gi,
  /(?:https?:\/\/|mailto:|manor-file:)[^\s<>"'`]+/gi,
  /\/(?:viewer\/|api\/v1\/fs\/|tasks\/|workflows\/|workspaces\/)[^\s<>"'`\])}]+/gi,
  // Inline code that is code (a call, a path, a command), not a lone key or ID.
  /`(?![a-z][a-z0-9]*(?:_[a-z0-9]+)+`|[0-9A-Za-z_-]{8,}`)[^`\n]+`/g,
];
// A 26-character ULID (Manor's document, task, run… IDs) or a UUID.
const RAW_ID = String.raw`(?:(?=[0-9A-Z]*\d)[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`;
const ID_LABEL = String.raw`\b(?:[A-Za-z]+[\s_-]+)?(?:ID|Id|id)\b|文档\s*(?:ID|编号)|编号`;
const PARENTHESISED_ID_RE = new RegExp(String.raw`\s*[(（]\s*(?:${ID_LABEL})\s*[:：]\s*\x60?[A-Za-z0-9_-]{8,}\x60?\s*[)）]`, "g");
const LABELLED_ID_RE = new RegExp(String.raw`(?:[,，;；]\s*)?(?:${ID_LABEL})\s*[:：]\s*\x60?[A-Za-z0-9_-]{8,}\x60?`, "g");
const BARE_ID_RE = new RegExp(String.raw`\s*\x60?\b${RAW_ID}\b\x60?`, "g");
// mcp__email__send_email → "send email"; send_workspace_chat_message → "send workspace chat message".
const MCP_TOOL_RE = /\bmcp__[a-z0-9_-]+?__([a-z0-9_]+)\b/gi;
const SNAKE_KEY_RE = /\x60?\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b\x60?(?!\.[a-z0-9]{1,6}\b)/g;

/** Chat prose names things; it never shows IDs or raw tool keys. Code, links
 * and file references keep theirs, so files still open from the reply. */
export function stripInternalReferences(value) {
  const source = typeof value === "string" ? value : "";
  if (!source) return source;
  const kept = [];
  let text = source;
  for (const pattern of PROTECTED_REFERENCE_RES) {
    text = text.replace(pattern, (segment) => `\uE010${kept.push(segment) - 1}\uE011`);
  }
  text = text
    .replace(PARENTHESISED_ID_RE, "")
    .replace(LABELLED_ID_RE, "")
    .replace(BARE_ID_RE, "")
    .replace(MCP_TOOL_RE, (_match, tool) => tool.replace(/_/g, " "))
    .replace(SNAKE_KEY_RE, (_match, key) => key.replace(/_/g, " "))
    .replace(/[(（]\s*[)）]/g, "")
    .replace(/[ \t]+([.,;:!?。，；：！？])/g, "$1")
    .replace(/([.。!?！？])[.。]/g, "$1")
    .replace(/^[ \t]*[.。,，]\s*/gm, "")
    .replace(/(\S)[ \t]{2,}/g, "$1 ");
  if (!/^\s/.test(source)) text = text.replace(/^[ \t]+/, "");
  return text.replace(/\uE010(\d+)\uE011/g, (_match, index) => kept[Number(index)]);
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

/** Reconcile a completed legacy snapshot only when its last final confirms the body. */
export function hasAppendedCanonicalFinal(content, finalParts, streaming, hasRunningProcess) {
  return Boolean(content) && !streaming && !hasRunningProcess &&
    finalParts.length > 1 && finalParts[finalParts.length - 1].trim() === content;
}

export function hasRunningAssistantProcess(blocks) {
  return blocks.some((block) => block?.type === "process" && (
    block.status === "running" || block.status === "pending" ||
    (Array.isArray(block.steps) && block.steps.some(
      (step) => step?.status === "running" || step?.status === "pending",
    ))
  ));
}
