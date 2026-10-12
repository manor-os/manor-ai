/**
 * XML 1.0 only admits TAB, LF, CR and the non-surrogate code points above
 * U+001F (minus U+FFFE/U+FFFF). Any other control character, a lone surrogate,
 * or a noncharacter makes the whole OOXML part unreadable, so every XML
 * escaping helper must pass its input through this sanitizer first.
 */
const INVALID_XML_CONTROL_CHARACTERS = /[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g;
const SURROGATE_CODE_UNITS = /[\uD800-\uDBFF][\uDC00-\uDFFF]|[\uD800-\uDFFF]/g;

export function stripInvalidXmlCharacters(value: string): string {
  return value
    .replace(INVALID_XML_CONTROL_CHARACTERS, "")
    .replace(SURROGATE_CODE_UNITS, (match) => (match.length === 2 ? match : ""));
}

/**
 * Office object models use VERTICAL TAB for a soft line break inside a
 * paragraph. It is not a legal XML character, so text-run builders convert it
 * to their own line-break markup before escaping.
 */
export function normalizeOfficeSoftBreaks(value: string): string {
  return value.replace(/\u000B/g, "\n");
}
