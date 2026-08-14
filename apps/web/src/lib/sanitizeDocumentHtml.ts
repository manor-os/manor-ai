import DOMPurify from "dompurify";

/** Sanitize HTML derived from user-controlled Office files before rendering. */
export function sanitizeDocumentHtml(value: string): string {
  return DOMPurify.sanitize(value || "", {
    USE_PROFILES: { html: true },
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: [
      "base",
      "embed",
      "form",
      "iframe",
      "link",
      "meta",
      "object",
      "script",
      "style",
    ],
    FORBID_ATTR: ["style", "srcset"],
  });
}
