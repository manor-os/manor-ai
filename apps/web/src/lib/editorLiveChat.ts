export const EDITOR_LIVE_CHAT_EVENT = "manor:open-editor-live-chat";
export const EDITOR_LIVE_CHAT_CLOSE_EVENT = "manor:close-editor-live-chat";

export type EditorLiveApplyMeta = {
  complete: boolean;
  source: "assistant-stream";
  mode?: "patch";
  diff?: string;
  patch?: string;
  patchCount?: number;
  sourceLabel?: string;
};

export type EditorLiveChatDetail = {
  documentId?: string | null;
  documentName?: string | null;
  fileType?: string | null;
  mimeType?: string | null;
  editorType?: string | null;
  sourcePath?: string | null;
  instruction?: string | null;
  getContent?: () => string;
  applyContent?: (content: string, meta: EditorLiveApplyMeta) => void;
  localEditContent?: (userRequest: string, currentContent: string) => string | null;
  getAttachmentFiles?: () => File[] | Promise<File[]>;
  supportsImageGeneration?: boolean;
  applyGeneratedImage?: (imageUrl: string, meta: EditorLiveApplyMeta) => void | Promise<void>;
};

export type EditorLivePatchOperation =
  | {
      op: "replace";
      find: string;
      replace: string;
      all?: boolean;
    }
  | {
      op: "delete";
      find: string;
      all?: boolean;
    }
  | {
      op: "insert_before" | "insert_after";
      find: string;
      text: string;
    }
  | {
      op: "prepend" | "append";
      text: string;
    };

export type EditorLivePatchResult = {
  content: string;
  applied: number;
  failed: Array<{ index: number; reason: string }>;
};

function documentReference(detail: EditorLiveChatDetail) {
  if (detail.documentName && detail.documentId) return `#${detail.documentName}`;
  return detail.documentName || "the current document";
}

export function buildEditorLiveEditPrompt(detail: EditorLiveChatDetail = {}) {
  const docRef = documentReference(detail);
  const firstLine =
    detail.instruction?.trim() || `Tell me what to change in ${docRef}.`;

  return firstLine;
}

export function buildEditorLiveEditRequest(
  detail: EditorLiveChatDetail,
  userRequest: string,
  currentContent: string,
) {
  void detail;
  void currentContent;
  return userRequest.trim();
}

function isHtmlLike(detail: EditorLiveChatDetail, currentContent: string) {
  const name = (detail.documentName || "").toLowerCase();
  const type = `${detail.fileType || ""} ${detail.mimeType || ""} ${detail.editorType || ""}`.toLowerCase();
  return (
    name.endsWith(".html") ||
    name.endsWith(".htm") ||
    type.includes("html") ||
    /<(!doctype\s+html|html|head|body)\b/i.test(currentContent)
  );
}

function isCssLike(detail: EditorLiveChatDetail) {
  const name = (detail.documentName || "").toLowerCase();
  const type = `${detail.fileType || ""} ${detail.mimeType || ""} ${detail.editorType || ""}`.toLowerCase();
  return name.endsWith(".css") || type.includes("css");
}

function asksForVisualCss(userRequest: string) {
  return /css|style|design|beautiful|modern|polish|visual|ui|ux|美感|美化|好看|漂亮|设计|样式|视觉|界面|页面/i.test(
    userRequest,
  );
}

const BASIC_HTML_POLISH_CSS = `
  :root {
    color-scheme: light;
    --page-bg: #f5f7fb;
    --surface: #ffffff;
    --ink: #232020;
    --muted: #607089;
    --accent: #0f8f84;
    --accent-strong: #0b6f67;
    --line: rgba(15, 30, 50, 0.12);
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }

  * {
    box-sizing: border-box;
  }

  body {
    margin: 0;
    min-height: 100vh;
    background:
      radial-gradient(circle at 20% 10%, rgba(15, 143, 132, 0.14), transparent 30%),
      linear-gradient(135deg, #f8fbff 0%, var(--page-bg) 100%);
    color: var(--ink);
    line-height: 1.6;
  }

  main, .container, .page, .content {
    width: min(1120px, calc(100% - 40px));
    margin: 0 auto;
  }

  header, section, article, .card {
    background: rgba(255, 255, 255, 0.86);
    border: 1px solid var(--line);
    border-radius: 18px;
    box-shadow: 0 18px 50px rgba(35, 32, 32, 0.08);
  }

  header {
    margin: 32px auto 24px;
    padding: clamp(28px, 5vw, 72px);
  }

  section, article, .card {
    margin: 20px 0;
    padding: clamp(20px, 3vw, 36px);
  }

  h1, h2, h3 {
    margin: 0 0 14px;
    line-height: 1.15;
    letter-spacing: 0;
  }

  h1 {
    font-size: clamp(2.3rem, 5vw, 4.8rem);
  }

  p {
    color: var(--muted);
    max-width: 68ch;
  }

  a, button, .button {
    color: #fff;
    background: var(--accent);
    border: 0;
    border-radius: 10px;
    padding: 10px 16px;
    text-decoration: none;
    font-weight: 700;
    transition: transform 160ms ease, background 160ms ease;
  }

  a:hover, button:hover, .button:hover {
    background: var(--accent-strong);
    transform: translateY(-1px);
  }
`;

function styleBlock() {
  return `<style id="manor-ai-polish">${BASIC_HTML_POLISH_CSS}</style>`;
}

function appendCssFallback(currentContent: string) {
  const css = `${BASIC_HTML_POLISH_CSS}\n`;
  return currentContent.trimEnd() + `\n\n/* Manor AI visual polish */\n${css}`;
}

function quotedSegments(value: string) {
  const segments: string[] = [];
  const re = /"([^"]{1,200})"|'([^']{1,200})'|`([^`]{1,200})`|“([^”]{1,200})”|‘([^’]{1,200})’/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(value))) {
    const segment = (match[1] || match[2] || match[3] || match[4] || match[5] || "").trim();
    if (segment) segments.push(segment);
  }
  return segments;
}

function isJsonLikeContent(value: string) {
  try {
    const parsed = JSON.parse(value);
    return parsed !== null && typeof parsed === "object";
  } catch {
    return false;
  }
}

function applyQuotedTextFallback(userRequest: string, currentContent: string) {
  const lower = userRequest.toLowerCase();
  const quoted = quotedSegments(userRequest);
  const replaceIntent = /(replace|change|rename|update|改成|替换|改为|换成)/.test(lower);
  const deleteIntent = /(delete|remove|drop|删掉|删除|移除|去掉)/.test(lower);
  const appendIntent = /(append|add to end|bottom|末尾|最后|追加)/.test(lower);
  const prependIntent = /(prepend|add to top|top|beginning|开头|顶部|最前)/.test(lower);
  const addIntent = /(add|insert|write|添加|加入|插入|写入)/.test(lower);
  const all = /(all|every|全部|所有|每个)/.test(lower);
  const jsonLike = isJsonLikeContent(currentContent);

  if (replaceIntent && quoted.length >= 2 && currentContent.includes(quoted[0]!)) {
    return all
      ? currentContent.split(quoted[0]!).join(quoted[1]!)
      : currentContent.replace(quoted[0]!, quoted[1]!);
  }

  if (deleteIntent && quoted.length >= 1 && currentContent.includes(quoted[0]!)) {
    return all
      ? currentContent.split(quoted[0]!).join("")
      : currentContent.replace(quoted[0]!, "");
  }

  if (!jsonLike && (appendIntent || (addIntent && /end|bottom|末尾|最后/.test(lower))) && quoted.length >= 1) {
    const separator = currentContent.endsWith("\n") || !currentContent ? "" : "\n";
    return `${currentContent}${separator}${quoted[0]}\n`;
  }

  if (!jsonLike && (prependIntent || (addIntent && /top|beginning|开头|顶部|最前/.test(lower))) && quoted.length >= 1) {
    const separator = quoted[0]!.endsWith("\n") ? "" : "\n";
    return `${quoted[0]}${separator}${currentContent}`;
  }

  return null;
}

function applyHtmlStyleFallback(currentContent: string) {
  const block = styleBlock();
  if (/<style\b[^>]*id=["']manor-ai-polish["'][^>]*>/i.test(currentContent)) {
    return currentContent.replace(
      /<style\b[^>]*id=["']manor-ai-polish["'][^>]*>[\s\S]*?<\/style>/i,
      block,
    );
  }
  if (/<\/head>/i.test(currentContent)) {
    return currentContent.replace(/<\/head>/i, `${block}\n</head>`);
  }
  if (/<head[^>]*>/i.test(currentContent)) {
    return currentContent.replace(/<head[^>]*>/i, (match) => `${match}\n${block}`);
  }
  return `${block}\n${currentContent}`;
}

export function buildEditorLiveEditFallbackContent(
  detail: EditorLiveChatDetail,
  userRequest: string,
  currentContent: string,
) {
  const custom = detail.localEditContent?.(userRequest, currentContent);
  if (typeof custom === "string" && custom !== currentContent) return custom;

  const quotedFallback = applyQuotedTextFallback(userRequest, currentContent);
  if (typeof quotedFallback === "string" && quotedFallback !== currentContent) {
    return quotedFallback;
  }

  if (!asksForVisualCss(userRequest)) return null;
  if (isHtmlLike(detail, currentContent)) return applyHtmlStyleFallback(currentContent);
  if (isCssLike(detail)) return appendCssFallback(currentContent);
  return null;
}

function parsePatchOperations(patchJson: string): EditorLivePatchOperation[] {
  const parsed = JSON.parse(patchJson);
  const operations = Array.isArray(parsed) ? parsed : [parsed];
  return operations.filter(
    (operation): operation is EditorLivePatchOperation =>
      operation && typeof operation === "object" && typeof operation.op === "string",
  );
}

function firstFailure(index: number, reason: string) {
  return [{ index, reason }];
}

export function applyEditorLivePatch(
  currentContent: string,
  patchJson: string,
): EditorLivePatchResult {
  let operations: EditorLivePatchOperation[];
  try {
    operations = parsePatchOperations(patchJson);
  } catch (err) {
    return {
      content: currentContent,
      applied: 0,
      failed: firstFailure(0, `Invalid patch JSON: ${(err as Error).message}`),
    };
  }

  if (operations.length === 0) {
    return {
      content: currentContent,
      applied: 0,
      failed: firstFailure(0, "Patch did not include any operations."),
    };
  }

  let draft = currentContent;
  for (let index = 0; index < operations.length; index += 1) {
    const operation = operations[index]!;
    if (operation.op === "append") {
      if (typeof operation.text !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Append operation is missing text."),
        };
      }
      draft += operation.text;
      continue;
    }

    if (operation.op === "prepend") {
      if (typeof operation.text !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Prepend operation is missing text."),
        };
      }
      draft = operation.text + draft;
      continue;
    }

    if (!("find" in operation) || typeof operation.find !== "string" || !operation.find) {
      return {
        content: currentContent,
        applied: index,
        failed: firstFailure(index, "Patch operation is missing exact find text."),
      };
    }

    const at = draft.indexOf(operation.find);
    if (at < 0) {
      return {
        content: currentContent,
        applied: index,
        failed: firstFailure(index, "Find text was not present in the current document."),
      };
    }

    if (operation.op === "delete") {
      draft = operation.all
        ? draft.split(operation.find).join("")
        : draft.slice(0, at) + draft.slice(at + operation.find.length);
      continue;
    }

    if (operation.op === "replace") {
      if (typeof operation.replace !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Replace operation is missing replacement text."),
        };
      }
      draft = operation.all
        ? draft.split(operation.find).join(operation.replace)
        : draft.slice(0, at) + operation.replace + draft.slice(at + operation.find.length);
      continue;
    }

    if (operation.op === "insert_before" || operation.op === "insert_after") {
      if (typeof operation.text !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Insert operation is missing text."),
        };
      }
      const insertAt =
        operation.op === "insert_before" ? at : at + operation.find.length;
      draft = draft.slice(0, insertAt) + operation.text + draft.slice(insertAt);
      continue;
    }

    return {
      content: currentContent,
      applied: index,
      failed: firstFailure(index, `Unsupported patch operation: ${(operation as any).op}`),
    };
  }

  return {
    content: draft,
    applied: operations.length,
    failed: [],
  };
}

function trimPatchPayload(value: string) {
  let next = value;
  if (next.startsWith("\r\n")) next = next.slice(2);
  else if (next.startsWith("\n")) next = next.slice(1);
  if (next.endsWith("\r\n")) next = next.slice(0, -2);
  else if (next.endsWith("\n")) next = next.slice(0, -1);
  return next;
}

export function extractEditorLivePatchPayload(text: string) {
  return extractEditorLivePatchPayloads(text)[0] || null;
}

export function extractEditorLivePatchPayloads(text: string) {
  const payloads: string[] = [];
  const re = /<manor-live-patch(?:\s[^>]*)?>([\s\S]*?)<\/manor-live-patch>/gi;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    payloads.push(trimPatchPayload(match[1] || ""));
  }
  return payloads;
}

export function stripEditorLiveEditBlocks(text: string) {
  return text
    .replace(/<manor-live-patch(?:\s[^>]*)?>[\s\S]*?<\/manor-live-patch>/gi, "")
    .replace(/<manor-live-patch(?:\s[^>]*)?>[\s\S]*$/i, "")
    .replace(/<manor-live-edit(?:\s[^>]*)?>[\s\S]*?<\/manor-live-edit>/gi, "")
    .replace(/<manor-live-edit(?:\s[^>]*)?>[\s\S]*$/i, "")
    .trim();
}

export function openEditorLiveChat(detail: EditorLiveChatDetail) {
  if (typeof window === "undefined") return;
  const liveEditDetail: EditorLiveChatDetail = {
    ...detail,
    sourcePath: detail.sourcePath || window.location.pathname,
  };
  window.dispatchEvent(
    new CustomEvent<EditorLiveChatDetail>(EDITOR_LIVE_CHAT_EVENT, {
      detail: liveEditDetail,
    }),
  );
}

export function closeEditorLiveChat() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(EDITOR_LIVE_CHAT_CLOSE_EVENT));
}
