import type {
  AssistantResponseSurfaceBlock,
  ResponseSurfaceSubmission,
  ResponseSurfaceSubmissionReceipt,
} from "./chatStream";
import {
  isWorkspaceLedgerOverview,
  isWorkspaceLedgerQueryVisualization,
  workspaceLedgerOverviewHtml,
  workspaceLedgerQueryHtml,
} from "./workspaceLedgerVisualization";
import type { ResolvedDocumentTheme } from "../components/ui/ThemeAwareImage";
import {
  RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT,
  RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT,
  responseSurfaceSubmissionFailureMessageIds,
  responseSurfaceSubmissionOutcomes,
} from "./responseSurfaceState.mjs";

const SAFE_ID = /^[a-z][a-z0-9_.-]{0,63}$/;
const SAFE_HASH = /^[0-9a-f]{64}$/;
const SAFE_EVENT_ID = /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}$/;
const BLOCKED_HTML_ELEMENTS = new Set([
  "a", "area", "base", "embed", "iframe", "link", "meta", "object", "script", "style",
]);
const BLOCKED_PRESENTATION_ELEMENTS = new Set([
  "animate", "animatecolor", "animatemotion", "animatetransform", "discard", "filter", "set",
]);
const BLOCKED_HTML_ATTRIBUTES = new Set([
  "action", "download", "formaction", "href", "srcdoc", "target",
]);
const BLOCKED_EMBEDDED_RESOURCE_ATTRIBUTES = new Set([
  "background", "poster", "src", "srcset",
]);
const BLOCKED_PRESENTATION_ATTRIBUTES = new Set([
  "bgcolor", "bordercolor", "color", "fill", "filter", "flood-color", "lighting-color", "stop-color", "stroke", "style",
]);
const BLOCKED_HOST_ELEMENT_IDS = new Set([
  "surface-activity", "surface-error", "surface-root",
]);
const VOID_HTML_ELEMENTS = new Set([
  "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
  "source", "track", "wbr",
]);
const BLOCKED_TAG_MARKUP = /<(?:a|area|script|style|link|iframe|object|embed|meta|base)\b/i;
const BLOCKED_ATTRIBUTE_MARKUP = /[\s/](?:on[a-z]+|action|formaction|href|target|download|srcdoc)\s*=/i;
const BLOCKED_PRESENTATION_TAG_MARKUP = /<(?:animate|animatecolor|animatemotion|animatetransform|discard|filter|set)\b/i;
const BLOCKED_EMBEDDED_RESOURCE_ATTRIBUTE_MARKUP = /[\s/](?:background|poster|src|srcset)\s*=/i;
const BLOCKED_PRESENTATION_ATTRIBUTE_MARKUP = /[\s/](?:bgcolor|bordercolor|color|fill|filter|flood-color|lighting-color|stop-color|stroke|style)\s*=/i;
const BLOCKED_HOST_ID_MARKUP = /[\s/]id\s*=\s*(?:"\s*surface-(?:activity|error|root)\s*"|'\s*surface-(?:activity|error|root)\s*'|surface-(?:activity|error|root)(?=[\s/>]))/i;
const BLOCKED_COLOR_INPUT_MARKUP = /<input\b(?:(?:"[^"]*"|'[^']*'|[^'">])*)\btype\s*=\s*(?:"\s*color\s*"|'\s*color\s*'|color(?=[\s/>]))/i;
const JAVASCRIPT_PROTOCOL = /javascript\s*:/i;
const BLOCKED_CSS = /@import\b|url\s*\(|<\/style/i;
const BLOCKED_GENERATED_CSS = /!\s*important\b|--module-[a-z0-9_-]+\s*:|(?:^|[,{])\s*(?::root\b|html\b|body\b)/im;
const BLOCKED_V2_GENERATED_CSS = /#surface-(?:activity|error|root)\b|:scope\b|\bposition\s*:\s*(?:fixed|sticky)\b|\bz-index\s*:/i;
const BLOCKED_V2_AT_RULE = /@(?!media\b)/im;
const RAW_CSS_COLOR = /#[0-9a-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark|device-cmyk)\s*\(/i;
const CSS_ATTRIBUTE_VALUE = /\battr\s*\(/i;
const CSS_NAMED_COLORS = new Set(`
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown
burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred
darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray
green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen
lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink
lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen
linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy
oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip
peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle
tomato turquoise violet wheat white whitesmoke yellow yellowgreen
`.trim().split(/\s+/));
const CSS_SYSTEM_COLORS = new Set([
  "accentcolor", "accentcolortext", "activetext", "buttonborder", "buttonface", "buttontext", "canvas",
  "canvastext", "field", "fieldtext", "graytext", "highlight", "highlighttext", "linktext", "mark",
  "marktext", "selecteditem", "selecteditemtext", "visitedtext", "-webkit-activelink",
  "-webkit-focus-ring-color", "-webkit-link",
]);
const MODULE_COLOR_TOKEN = /var\(\s*--module-[a-z0-9_-]+\s*\)/gi;
const COLOR_AFFECTING_PROPERTIES = new Set([
  "accent-color", "backdrop-filter", "background", "background-color", "background-image", "border",
  "box-reflect", "box-shadow", "caret-color", "column-rule",
  "content", "fill", "filter", "flood-color", "list-style", "list-style-image", "lighting-color",
  "mask", "mask-border", "mask-image", "outline", "shape-outside", "stop-color", "stroke",
  "text-decoration", "text-emphasis", "text-shadow", "text-stroke",
]);
const ALLOWED_COLOR_VALUE_WORDS = new Set([
  "auto", "bottom", "calc", "center", "circle", "clamp", "closest-corner", "closest-side",
  "conic-gradient", "contain", "cover", "currentcolor", "dashed", "double", "dotted", "ellipse",
  "farthest-corner", "farthest-side", "fixed", "from", "groove", "inherit", "initial", "inset",
  "left", "linear-gradient", "max", "min", "no-repeat", "none", "outset", "padding-box",
  "radial-gradient", "repeat", "repeating-conic-gradient", "repeating-linear-gradient",
  "repeating-radial-gradient", "revert", "revert-layer", "ridge", "right", "round", "scroll",
  "solid", "space", "to", "top", "transparent", "unset",
]);

const TEMPLATE_IDS = new Set([
  "learning.code_lab",
  "response.choice",
  "workspace.ledger.overview",
  "workspace.ledger.query",
]);
const REGISTERED_TEMPLATE_ACTIONS: Record<
  string,
  { id: string; label: string; intent: "submit" }
> = {
  "learning.code_lab": { id: "run", label: "Run code", intent: "submit" },
  "response.choice": { id: "answer", label: "Submit answer", intent: "submit" },
};

function objectValue(value: unknown): Record<string, any> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : null;
}

function boundedString(value: unknown, maximum: number): string {
  return typeof value === "string" ? value.slice(0, maximum) : "";
}

function boundedPayload(
  value: unknown,
  maximum = RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT,
): Record<string, unknown> | null {
  const payload = objectValue(value);
  if (!payload) return null;
  try {
    const serialized = JSON.stringify(payload);
    if (serialized.length > maximum) return null;
    return JSON.parse(serialized) as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function createResponseSurfaceSubmissionReceipt(
  submission: ResponseSurfaceSubmission,
): ResponseSurfaceSubmissionReceipt {
  const eventId = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return {
    ...submission,
    version: 1,
    eventId,
    recordedAt: new Date().toISOString(),
  };
}

export function responseSurfaceSubmissionMeta(
  receipt: ResponseSurfaceSubmissionReceipt,
): Record<string, unknown> {
  return {
    response_surface_submission: receipt,
    response_surface_submission_pending: true,
  };
}

type ResponseSurfaceSubmissionMessageLike = {
  role?: string | null;
  content?: string | null;
  meta?: Record<string, unknown> | null;
  stream_error?: boolean | null;
  stop_reason?: string | null;
};

export function rollbackResponseSurfaceSubmissionMessages<
  T extends ResponseSurfaceSubmissionMessageLike,
>(messages: T[], eventId: string): T[] {
  const receiptIndex = messages.findIndex((message) => (
    objectValue(message.meta?.response_surface_submission)?.eventId === eventId
    && message.meta?.response_surface_submission_pending === true
  ));
  if (receiptIndex < 0) return messages;
  return messages.filter((message, index) => {
    if (index === receiptIndex) return false;
    if (index !== receiptIndex + 1 || message.role !== "assistant") return true;
    const content = message.content || "";
    const streamStatus = String(message.meta?.stream_status || "").toLowerCase();
    const failed = message.stream_error === true
      || message.stop_reason === "error"
      || streamStatus === "error"
      || streamStatus === "interrupted";
    return Boolean(content && !failed && !content.startsWith("Error: "));
  });
}

export function settleResponseSurfaceSubmissionFailure<
  T extends ResponseSurfaceSubmissionMessageLike,
>(messages: T[], eventId: string, outcome: "failed" | "interrupted"): T[] {
  const receiptIndex = messages.findIndex((message) => (
    objectValue(message.meta?.response_surface_submission)?.eventId === eventId
  ));
  if (receiptIndex < 0) return messages;
  return messages.flatMap((message, index) => {
    if (index === receiptIndex) {
      const receipt = objectValue(message.meta?.response_surface_submission);
      return [{
        ...message,
        meta: {
          ...(message.meta || {}),
          response_surface_submission: { ...receipt, outcome },
          response_surface_submission_pending: false,
        },
      } as T];
    }
    if (index !== receiptIndex + 1 || message.role !== "assistant") return [message];
    const content = message.content || "";
    const streamStatus = String(message.meta?.stream_status || "").toLowerCase();
    const failed = message.stream_error === true
      || message.stop_reason === "error"
      || streamStatus === "error"
      || streamStatus === "interrupted"
      || content.startsWith("Error: ");
    return failed ? [] : [message];
  });
}

export function normalizeResponseSurfaceSubmissionReceipt(
  value: unknown,
): ResponseSurfaceSubmissionReceipt | null {
  const raw = objectValue(value);
  const eventId = boundedString(raw?.eventId, 96);
  const recordedAt = boundedString(raw?.recordedAt, 64);
  const sourceMessageId = boundedString(raw?.sourceMessageId, 128);
  const surfaceId = boundedString(raw?.surfaceId, 96);
  const title = boundedString(raw?.title, 120).trim();
  const action = boundedString(raw?.action, 64);
  const actionLabel = boundedString(raw?.actionLabel, 80).trim();
  const outcome = raw?.outcome === "failed" || raw?.outcome === "interrupted"
    ? raw.outcome
    : undefined;
  const context = objectValue(raw?.context);
  const templateId = boundedString(context?.templateId, 80);
  const payload = boundedPayload(
    raw?.payload,
    templateId === "learning.code_lab"
      ? RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT
      : RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT,
  );
  if (
    raw?.version !== 1 || !SAFE_EVENT_ID.test(eventId) || !recordedAt ||
    !sourceMessageId || !surfaceId || !title || !SAFE_ID.test(action) ||
    !actionLabel || !payload
  ) return null;
  const instructions = boundedString(context?.instructions, 4_000);
  const checks = Array.isArray(context?.checks)
    ? context.checks
      .slice(0, 20)
      .map((item) => boundedString(item, 2_000))
      .filter(Boolean)
    : [];
  return {
    version: 1,
    eventId,
    recordedAt,
    sourceMessageId,
    surfaceId,
    title,
    action,
    actionLabel,
    outcome,
    payload,
    context: templateId || instructions || checks.length
      ? {
          templateId: TEMPLATE_IDS.has(templateId)
            ? templateId as NonNullable<ResponseSurfaceSubmissionReceipt["context"]>["templateId"]
            : undefined,
          instructions: instructions || undefined,
          checks: checks.length ? checks : undefined,
        }
      : undefined,
  };
}

type ResponseSurfaceMessageLike = {
  id?: string | null;
  role?: string | null;
  author_kind?: string | null;
  meta?: Record<string, unknown> | null;
  content?: string | null;
  body?: string | null;
  created_at?: string | null;
  timestamp?: string | null;
  assistant_blocks?: unknown[] | null;
  stream_error?: boolean | null;
  stop_reason?: string | null;
};

function cssBlocksBalanced(value: string): boolean {
  let depth = 0;
  let quote = "";
  let escaped = false;
  let inComment = false;
  for (let index = 0; index < value.length; index += 1) {
    const current = value[index];
    const following = value[index + 1] || "";
    if (inComment) {
      if (current === "*" && following === "/") {
        inComment = false;
        index += 1;
      }
      continue;
    }
    if (quote) {
      if (escaped) escaped = false;
      else if (current === "\\") escaped = true;
      else if (current === quote) quote = "";
      continue;
    }
    if (current === "/" && following === "*") {
      inComment = true;
      index += 1;
    } else if (current === '"' || current === "'") quote = current;
    else if (current === "{") depth += 1;
    else if (current === "}") {
      depth -= 1;
      if (depth < 0) return false;
    }
  }
  return depth === 0 && !quote && !inComment;
}

function decodeCssEscapes(value: string): string {
  let decoded = "";
  let index = 0;
  while (index < value.length) {
    const current = value[index];
    if (current !== "\\" || index + 1 >= value.length) {
      decoded += current;
      index += 1;
      continue;
    }
    index += 1;
    if (/[\r\n\f]/.test(value[index])) {
      if (value[index] === "\r" && value[index + 1] === "\n") index += 1;
      index += 1;
      continue;
    }
    const hex = value.slice(index).match(/^[0-9a-f]{1,6}/i)?.[0];
    if (hex) {
      const codePoint = Number.parseInt(hex, 16);
      decoded += codePoint && codePoint <= 0x10ffff && !(codePoint >= 0xd800 && codePoint <= 0xdfff)
        ? String.fromCodePoint(codePoint)
        : "\ufffd";
      index += hex.length;
      if (/[ \t\r\n\f]/.test(value[index] || "")) {
        if (value[index] === "\r" && value[index + 1] === "\n") index += 1;
        index += 1;
      }
      continue;
    }
    decoded += value[index];
    index += 1;
  }
  return decoded;
}

function maskCssStrings(value: string): string {
  let masked = "";
  let quote = "";
  let escaped = false;
  for (const current of value) {
    if (quote) {
      masked += /[\r\n\f]/.test(current) ? current : " ";
      if (escaped) escaped = false;
      else if (current === "\\") escaped = true;
      else if (current === quote) quote = "";
      continue;
    }
    if (current === '"' || current === "'") {
      quote = current;
      masked += " ";
    } else masked += current;
  }
  return masked;
}

function colorProperty(propertyName: string): boolean {
  propertyName = propertyName.replace(/^-(?:moz|ms|o|webkit)-/, "");
  return COLOR_AFFECTING_PROPERTIES.has(propertyName)
    || propertyName.endsWith("color")
    || propertyName.startsWith("background-")
    || propertyName.startsWith("border-")
    || propertyName.startsWith("mask-")
    || propertyName.startsWith("outline-");
}

function generatedColorPolicyAllowed(value: string): boolean {
  const declaration = /(?:^|[;{])\s*(--[-a-z0-9_]+|[-a-z][a-z0-9-]*)\s*:\s*([^;{}]*)/gim;
  for (const match of value.matchAll(declaration)) {
    const propertyName = match[1].toLowerCase();
    if (propertyName.startsWith("--") || propertyName === "color-scheme") return false;
    const declarationValue = match[2];
    if (RAW_CSS_COLOR.test(declarationValue)) return false;
    if (CSS_ATTRIBUTE_VALUE.test(declarationValue)) return false;
    if (!colorProperty(propertyName)) continue;
    const declarationWords = declarationValue.match(/[a-z_][a-z0-9_-]*/gi) || [];
    if (declarationWords.some((word) => {
      const normalizedWord = word.toLowerCase();
      return CSS_NAMED_COLORS.has(normalizedWord) || CSS_SYSTEM_COLORS.has(normalizedWord);
    })) return false;
    const remainder = declarationValue
      .replace(MODULE_COLOR_TOKEN, " ")
      .replace(/(?:^|(?<=[\s,(]))[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:%|[a-z]+)?/gi, " ");
    if (/\bvar\s*\(/i.test(remainder)) return false;
    const words = remainder.match(/[a-z_][a-z0-9_-]*/gi) || [];
    if (words.some((word) => !ALLOWED_COLOR_VALUE_WORDS.has(word.toLowerCase()))) return false;
  }
  return true;
}

function generatedCssAllowed(value: string, strictColorPolicy: boolean): boolean {
  const withoutStrings = maskCssStrings(value);
  const withoutComments = withoutStrings.replace(/\/\*[\s\S]*?\*\//g, "");
  const normalized = decodeCssEscapes(withoutComments);
  return !BLOCKED_CSS.test(normalized)
    && !BLOCKED_GENERATED_CSS.test(normalized)
    && (!strictColorPolicy || (
      !BLOCKED_V2_GENERATED_CSS.test(normalized)
      && !BLOCKED_V2_AT_RULE.test(normalized)
      && generatedColorPolicyAllowed(normalized)
    ))
    && cssBlocksBalanced(value);
}

function htmlStartTagMarkup(value: string): string {
  const tags: string[] = [];
  let index = 0;
  while (index < value.length) {
    const start = value.indexOf("<", index);
    if (start < 0) break;
    if (value.startsWith("<!--", start)) {
      const commentEnd = value.indexOf("-->", start + 4);
      index = commentEnd < 0 ? value.length : commentEnd + 3;
      continue;
    }
    const first = value[start + 1] || "";
    if (!/[A-Za-z]/.test(first)) {
      index = start + 1;
      continue;
    }
    let quote = "";
    let cursor = start + 2;
    for (; cursor < value.length; cursor += 1) {
      const current = value[cursor];
      if (quote) {
        if (current === quote) quote = "";
        continue;
      }
      if (current === '"' || current === "'") quote = current;
      else if (current === ">") {
        tags.push(value.slice(start, cursor + 1));
        cursor += 1;
        break;
      } else if (current === "<") break;
    }
    index = Math.max(start + 1, cursor);
  }
  return tags.join("\n");
}

function htmlHasUnmatchedClosingTag(value: string): boolean {
  const openElements: string[] = [];
  let index = 0;
  while (index < value.length) {
    const start = value.indexOf("<", index);
    if (start < 0) break;
    if (value.startsWith("<!--", start)) {
      const commentEnd = value.indexOf("-->", start + 4);
      index = commentEnd < 0 ? value.length : commentEnd + 3;
      continue;
    }
    let quote = "";
    let cursor = start + 1;
    for (; cursor < value.length; cursor += 1) {
      const current = value[cursor];
      if (quote) {
        if (current === quote) quote = "";
        continue;
      }
      if (current === '"' || current === "'") quote = current;
      else if (current === ">") break;
    }
    if (cursor >= value.length) break;
    const markup = value.slice(start, cursor + 1);
    const closing = markup.match(/^<\s*\/\s*([a-z][a-z0-9:_-]*)/i)?.[1];
    if (closing) {
      const tagName = closing.toLowerCase().split(":").pop() || "";
      const matchIndex = openElements.lastIndexOf(tagName);
      if (matchIndex < 0) return true;
      openElements.splice(matchIndex);
    } else {
      const opening = markup.match(/^<\s*([a-z][a-z0-9:_-]*)/i)?.[1];
      if (opening && !/\/\s*>$/.test(markup)) {
        const tagName = opening.toLowerCase().split(":").pop() || "";
        if (!VOID_HTML_ELEMENTS.has(tagName)) openElements.push(tagName);
      }
    }
    index = cursor + 1;
  }
  return false;
}

function generatedHtmlAllowedWithoutDom(value: string, strictPresentation: boolean): boolean {
  const markup = htmlStartTagMarkup(value);
  return !BLOCKED_TAG_MARKUP.test(markup)
    && !BLOCKED_ATTRIBUTE_MARKUP.test(markup)
    && !JAVASCRIPT_PROTOCOL.test(markup)
    && !htmlHasUnmatchedClosingTag(value)
    && (!strictPresentation || (
      !BLOCKED_HOST_ID_MARKUP.test(markup)
      && !BLOCKED_PRESENTATION_TAG_MARKUP.test(markup)
      && !BLOCKED_EMBEDDED_RESOURCE_ATTRIBUTE_MARKUP.test(markup)
      && !BLOCKED_PRESENTATION_ATTRIBUTE_MARKUP.test(markup)
      && !BLOCKED_COLOR_INPUT_MARKUP.test(markup)
    ));
}

function generatedHtmlAllowed(value: string, strictPresentation: boolean): boolean {
  if (typeof DOMParser !== "function") {
    return generatedHtmlAllowedWithoutDom(value, strictPresentation);
  }
  const parsed = new DOMParser().parseFromString(
    `<!doctype html><body><div data-manor-surface-root>${value}</div></body>`,
    "text/html",
  );
  const surfaceRoot = parsed.querySelector("[data-manor-surface-root]");
  if (
    !surfaceRoot
    || Array.from(parsed.body.children).some((element) => element !== surfaceRoot)
  ) return false;
  for (const element of Array.from(surfaceRoot.querySelectorAll("*"))) {
    const elementName = element.localName.toLowerCase();
    if (
      BLOCKED_HTML_ELEMENTS.has(elementName)
      || (strictPresentation && BLOCKED_PRESENTATION_ELEMENTS.has(elementName))
    ) return false;
    if (
      strictPresentation
      && elementName === "input"
      && element.getAttribute("type")?.trim().toLowerCase() === "color"
    ) return false;
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase().split(":").pop() || "";
      if (
        name.startsWith("on")
        || BLOCKED_HTML_ATTRIBUTES.has(name)
        || (strictPresentation && name === "id" && BLOCKED_HOST_ELEMENT_IDS.has(attribute.value.trim().toLowerCase()))
        || (strictPresentation && (
          BLOCKED_EMBEDDED_RESOURCE_ATTRIBUTES.has(name)
          || BLOCKED_PRESENTATION_ATTRIBUTES.has(name)
        ))
        || JAVASCRIPT_PROTOCOL.test(attribute.value)
      ) return false;
    }
  }
  return true;
}

function responseSurfaceMessageText(message: ResponseSurfaceMessageLike): string {
  return typeof message.content === "string"
    ? message.content
    : typeof message.body === "string"
      ? message.body
      : "";
}

function isUserAuthoredResponseSurfaceMessage(
  message: ResponseSurfaceMessageLike,
): boolean {
  return [message.role, message.author_kind].some(
    (kind) => typeof kind === "string" && kind.trim().toLowerCase() === "user",
  );
}

function legacySubmissionPayload(
  text: string,
  signature: string,
): Record<string, unknown> | null {
  if (!text.startsWith(signature)) return null;
  const normalized = text
    .replace(/\\r\\n/g, "\n")
    .replace(/\\[rn]/g, "\n")
    .replace(/\r\n?/g, "\n");
  const codeFence = normalized.match(/```([A-Za-z0-9_+.-]*)\n([\s\S]*?)```/);
  if (codeFence) {
    return {
      code: codeFence[2].slice(0, 30_000),
      language: (codeFence[1] || "text").slice(0, 32),
    };
  }
  const start = normalized.indexOf("{", signature.length);
  const end = normalized.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return boundedPayload(JSON.parse(normalized.slice(start, end + 1)));
    } catch {
      const choice = normalized.slice(start, end + 1).match(
        /["']choice["']\s*:\s*["']([^"']+)["']/,
      )?.[1];
      if (choice) return { choice: choice.slice(0, 2_000) };
    }
  }
  return null;
}

function legacySubmissionContext(
  surface: AssistantResponseSurfaceBlock,
): ResponseSurfaceSubmissionReceipt["context"] {
  if (surface.render.kind !== "template") return undefined;
  const instructions = typeof surface.render.props.instructions === "string"
    ? surface.render.props.instructions.slice(0, 4_000)
    : undefined;
  const checks = Array.isArray(surface.render.props.tests)
    ? surface.render.props.tests
      .filter((item): item is string => typeof item === "string")
      .slice(0, 20)
      .map((item) => item.slice(0, 2_000))
    : undefined;
  return {
    templateId: surface.render.template_id,
    instructions,
    checks,
  };
}

export function responseSurfaceSubmissionReceiptFromMessage(
  message: ResponseSurfaceMessageLike,
): ResponseSurfaceSubmissionReceipt | null {
  const receipt = normalizeResponseSurfaceSubmissionReceipt(
    message.meta?.response_surface_submission,
  );
  return receipt ? {
    ...receipt,
    durable: message.meta?.response_surface_submission_pending !== true,
  } : null;
}

export function isResponseSurfaceSubmissionMessage(
  message: ResponseSurfaceMessageLike,
  receipts: ResponseSurfaceSubmissionReceipt[] = [],
): boolean {
  if (responseSurfaceSubmissionReceiptFromMessage(message) !== null) return true;
  const messageId = boundedString(message.id, 128);
  return Boolean(
    messageId
    && receipts.some((receipt) => receipt.eventId === `legacy:${messageId}`),
  );
}

export function collectResponseSurfaceSubmissionReceipts(
  messages: ResponseSurfaceMessageLike[],
): ResponseSurfaceSubmissionReceipt[] {
  const receipts = new Map<string, ResponseSurfaceSubmissionReceipt>();
  const receiptEventIdsByMessageId = new Map<string, string>();
  type SurfaceActionBinding = {
    sourceMessageId: string;
    surface: AssistantResponseSurfaceBlock;
    action: AssistantResponseSurfaceBlock["actions"][number];
  };
  const surfacesBySignature = new Map<string, SurfaceActionBinding>();
  const surfacesByIdentity = new Map<string, Map<string, SurfaceActionBinding>>();
  for (const message of messages) {
    const sourceMessageId = boundedString(message.id, 128);
    if (sourceMessageId && Array.isArray(message.assistant_blocks)) {
      for (const rawBlock of message.assistant_blocks) {
        const surface = normalizeResponseSurfaceBlock(rawBlock);
        if (!surface) continue;
        const identity = JSON.stringify([sourceMessageId, surface.id]);
        const actionsByAlias = new Map<string, SurfaceActionBinding>();
        for (const action of surface.actions) {
          const binding = {
            sourceMessageId,
            surface,
            action,
          };
          actionsByAlias.set(action.id, binding);
          surfacesBySignature.set(`${action.label}: ${surface.title}`, binding);
        }
        const registeredAction = surface.render.kind === "template"
          ? REGISTERED_TEMPLATE_ACTIONS[surface.render.template_id]
          : undefined;
        const canonicalBinding = registeredAction
          ? actionsByAlias.get(registeredAction.id)
          : undefined;
        if (canonicalBinding) {
          for (const historicalAction of normalizeActions(objectValue(rawBlock)?.actions)) {
            actionsByAlias.set(historicalAction.id, canonicalBinding);
            surfacesBySignature.set(
              `${historicalAction.label}: ${surface.title}`,
              canonicalBinding,
            );
          }
        }
        surfacesByIdentity.set(identity, actionsByAlias);
      }
    }
    let receipt = responseSurfaceSubmissionReceiptFromMessage(message);
    if (receipt) {
      const binding = surfacesByIdentity
        .get(JSON.stringify([receipt.sourceMessageId, receipt.surfaceId]))
        ?.get(receipt.action);
      if (binding) {
        receipt = {
          ...receipt,
          action: binding.action.id,
          actionLabel: binding.action.label,
          context: receipt.context || legacySubmissionContext(binding.surface),
        };
      }
      const existingReceipt = receipts.get(receipt.eventId);
      if (!existingReceipt?.durable || receipt.durable) {
        receipts.set(receipt.eventId, receipt);
      }
      if (sourceMessageId) receiptEventIdsByMessageId.set(sourceMessageId, receipt.eventId);
      continue;
    }
    if (!isUserAuthoredResponseSurfaceMessage(message)) continue;
    const messageId = boundedString(message.id, 128);
    const text = responseSurfaceMessageText(message);
    if (!messageId || !text) continue;
    for (const [signature, binding] of surfacesBySignature) {
      const payload = legacySubmissionPayload(text, signature);
      if (!payload) continue;
      const eventId = `legacy:${messageId}`;
      if (!SAFE_EVENT_ID.test(eventId)) break;
      receipts.set(eventId, {
        version: 1,
        eventId,
        recordedAt: boundedString(
          message.created_at || message.timestamp || "1970-01-01T00:00:00.000Z",
          64,
        ),
        sourceMessageId: binding.sourceMessageId,
        surfaceId: binding.surface.id,
        title: binding.surface.title,
        action: binding.action.id,
        actionLabel: binding.action.label,
        durable: true,
        payload,
        context: legacySubmissionContext(binding.surface),
      });
      receiptEventIdsByMessageId.set(messageId, eventId);
      break;
    }
  }
  const { outcomesByReceiptMessageId, statusesByReceiptMessageId } = responseSurfaceSubmissionOutcomes(
    messages,
    new Set(receiptEventIdsByMessageId.keys()),
  );
  for (const [receiptMessageId, status] of statusesByReceiptMessageId) {
    const eventId = receiptEventIdsByMessageId.get(receiptMessageId);
    const receipt = eventId ? receipts.get(eventId) : undefined;
    const outcome = outcomesByReceiptMessageId.get(receiptMessageId);
    if (receipt) {
      receipts.set(eventId!, { ...receipt, status, outcome });
    }
  }
  return Array.from(receipts.values());
}

export function collectResponseSurfaceSubmissionFailureMessageIds(
  messages: ResponseSurfaceMessageLike[],
  receipts: ResponseSurfaceSubmissionReceipt[],
): Set<string> {
  const receiptMessageIds = new Set(
    messages.flatMap((message) => {
      const messageId = boundedString(message.id, 128);
      return messageId && isResponseSurfaceSubmissionMessage(message, receipts)
        ? [messageId]
        : [];
    }),
  );
  return responseSurfaceSubmissionFailureMessageIds(messages, receiptMessageIds);
}

function normalizeActions(value: unknown) {
  if (!Array.isArray(value) || value.length > 8) return [];
  const seen = new Set<string>();
  return value.flatMap((raw) => {
    const action = objectValue(raw);
    const id = boundedString(action?.id, 64);
    const label = boundedString(action?.label, 80).trim();
    if (!SAFE_ID.test(id) || !label || seen.has(id) || action?.intent !== "submit") return [];
    seen.add(id);
    return [{ id, label, intent: "submit" as const }];
  });
}

export function normalizeResponseSurfaceBlock(
  value: unknown,
): AssistantResponseSurfaceBlock | null {
  const raw = objectValue(value);
  const render = objectValue(raw?.render);
  const display = objectValue(raw?.display);
  const id = boundedString(raw?.id, 96);
  const title = boundedString(raw?.title, 120).trim();
  const fallback = boundedString(raw?.fallback_markdown, 4_000).trim();
  if (
    raw?.type !== "surface" || raw?.version !== 1 || !id || !title || !fallback ||
    !render || !display
  ) return null;

  let normalizedRender: AssistantResponseSurfaceBlock["render"];
  if (render.kind === "template") {
    const templateId = boundedString(render.template_id, 80);
    const props = objectValue(render.props);
    if (!TEMPLATE_IDS.has(templateId) || render.template_version !== 1 || !props) return null;
    if (templateId === "workspace.ledger.overview" && !isWorkspaceLedgerOverview(props)) return null;
    if (templateId === "workspace.ledger.query" && !isWorkspaceLedgerQueryVisualization(props)) return null;
    normalizedRender = {
      kind: "template",
      template_id: templateId as
        | "learning.code_lab"
        | "response.choice"
        | "workspace.ledger.overview"
        | "workspace.ledger.query",
      template_version: 1,
      props: props as Record<string, unknown>,
    };
  } else if (render.kind === "sandboxed_html") {
    const code = objectValue(render.code);
    const data = objectValue(render.data);
    const validation = objectValue(render.validation);
    const html = boundedString(code?.html, 20_000);
    const css = boundedString(code?.css, 30_000);
    const javascript = boundedString(code?.javascript, 50_000);
    const policy = validation?.policy;
    const codeHash = typeof validation?.code_hash === "string"
      ? validation.code_hash
      : "";
    if (
      code?.version !== 1 || code?.runtime !== "sandboxed_html" || !html.trim() ||
      javascript.trim() || !data ||
      (policy !== "response_surface.v1" && policy !== "response_surface.v2") ||
      !SAFE_HASH.test(codeHash) ||
      !generatedHtmlAllowed(html, policy === "response_surface.v2") ||
      !generatedCssAllowed(css, policy === "response_surface.v2")
    ) return null;
    normalizedRender = {
      kind: "sandboxed_html",
      code: { version: 1, runtime: "sandboxed_html", html, css, javascript },
      data,
      validation: {
        policy,
        code_hash: codeHash,
      },
    };
  } else return null;

  const preferred = display.preferred === "focus" ? "focus" : "inline";
  const height = Number(display.inline_height);
  let actions = normalizeActions(raw.actions);
  if (normalizedRender.kind === "template") {
    const defaultAction = REGISTERED_TEMPLATE_ACTIONS[normalizedRender.template_id];
    if (defaultAction) {
      actions = [actions.find((action) => action.id === defaultAction.id) || defaultAction];
    }
  }
  return {
    id,
    type: "surface",
    version: 1,
    title,
    description: boundedString(raw.description, 300).trim() || undefined,
    render: normalizedRender,
    display: {
      preferred,
      inline_height: Number.isFinite(height) ? Math.min(720, Math.max(180, height)) : 360,
      focusable: display.focusable !== false,
    },
    actions,
    fallback_markdown: fallback,
  };
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeClosingTag(value: string, tag: "script" | "style"): string {
  return value.replace(new RegExp(`</${tag}`, "gi"), `<\\/${tag}`);
}

function safeJson(value: unknown): string {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

function consumeCssIdentifier(value: string, start: number): { decoded: string; end: number } {
  let decoded = "";
  let index = start;
  while (index < value.length) {
    const current = value[index];
    if (/[a-z0-9_-]/i.test(current) || current.charCodeAt(0) >= 0x80) {
      decoded += current;
      index += 1;
      continue;
    }
    if (current !== "\\" || index + 1 >= value.length) break;
    index += 1;
    if (/[\r\n\f]/.test(value[index])) {
      if (value[index] === "\r" && value[index + 1] === "\n") index += 1;
      index += 1;
      continue;
    }
    const hex = value.slice(index).match(/^[0-9a-f]{1,6}/i)?.[0];
    if (hex) {
      const codePoint = Number.parseInt(hex, 16);
      decoded += codePoint && codePoint <= 0x10ffff && !(codePoint >= 0xd800 && codePoint <= 0xdfff)
        ? String.fromCodePoint(codePoint)
        : "\ufffd";
      index += hex.length;
      if (/[ \t\r\n\f]/.test(value[index] || "")) {
        if (value[index] === "\r" && value[index + 1] === "\n") index += 1;
        index += 1;
      }
      continue;
    }
    decoded += value[index];
    index += 1;
  }
  return { decoded, end: index };
}

function consumeLegacySurfaceRootAttributeSelector(
  value: string,
  start: number,
): number | null {
  let index = start + 1;
  let quote = "";
  while (index < value.length) {
    const current = value[index];
    if (current === "\\") {
      index += index + 1 < value.length ? 2 : 1;
      continue;
    }
    if (quote) {
      if (current === quote) quote = "";
      index += 1;
      continue;
    }
    if (current === '"' || current === "'") {
      quote = current;
      index += 1;
      continue;
    }
    if (current !== "]") {
      index += 1;
      continue;
    }
    const selector = decodeCssEscapes(value.slice(start + 1, index)).trim();
    const attribute = selector.match(
      /^([a-z][a-z0-9_-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"']+))(?:\s+([is]))?\s*$/i,
    );
    if (!attribute || attribute[1].toLowerCase() !== "id") return null;
    const expected = attribute[2] ?? attribute[3] ?? attribute[4] ?? "";
    const matchesRoot = attribute[5]?.toLowerCase() === "i"
      ? expected.toLowerCase() === "surface-root"
      : expected === "surface-root";
    return matchesRoot ? index + 1 : null;
  }
  return null;
}

function rewriteLegacySurfaceRootSelectors(value: string): string {
  let rewritten = "";
  let index = 0;
  let quote = "";
  let inComment = false;
  while (index < value.length) {
    const current = value[index];
    const following = value[index + 1] || "";
    if (inComment) {
      rewritten += current;
      if (current === "*" && following === "/") {
        rewritten += following;
        index += 2;
        inComment = false;
      } else index += 1;
      continue;
    }
    if (quote) {
      rewritten += current;
      if (current === "\\") {
        rewritten += following;
        index += following ? 2 : 1;
      } else {
        if (current === quote) quote = "";
        index += 1;
      }
      continue;
    }
    if (current === "/" && following === "*") {
      rewritten += "/*";
      index += 2;
      inComment = true;
      continue;
    }
    if (current === '"' || current === "'") {
      rewritten += current;
      quote = current;
      index += 1;
      continue;
    }
    if (current === "\\") {
      rewritten += current + following;
      index += following ? 2 : 1;
      continue;
    }
    const rootAttributeEnd = current === "["
      ? consumeLegacySurfaceRootAttributeSelector(value, index)
      : null;
    if (rootAttributeEnd !== null) {
      rewritten += ":scope";
      index = rootAttributeEnd;
      continue;
    }
    if (current === "#") {
      const identifier = consumeCssIdentifier(value, index + 1);
      if (identifier.decoded === "surface-root") {
        rewritten += ":scope";
        index = identifier.end;
        continue;
      }
    }
    rewritten += current;
    index += 1;
  }
  return rewritten;
}

function normalizeDocumentLanguage(value: string): string {
  const language = value.trim().toLowerCase().replace(/_/g, "-").slice(0, 32);
  return /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/.test(language) ? language : "en";
}

const SURFACE_BASE_CSS = `
:root {
  color-scheme: light;
  --module-font: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --module-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace;
  --module-text: #292524;
  --module-strong: #1c1917;
  --module-muted: #78716c;
  --module-faint: #a8a29e;
  --module-surface: #ffffff;
  --module-row: #f7f6f3;
  --module-sunken: #f2f1ee;
  --module-border: rgba(28,25,23,.08);
  --module-accent: #436b65;
  --module-on-accent: #ffffff;
  --module-ring: rgba(67,107,101,.22);
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --module-text: #e7e5e4;
  --module-strong: #fafaf9;
  --module-muted: #a8a29e;
  --module-faint: #78716c;
  --module-surface: #1c1917;
  --module-row: #292524;
  --module-sunken: #0c0a09;
  --module-border: rgba(255,255,255,.1);
  --module-accent: #9cc8be;
  --module-on-accent: #1c1917;
  --module-ring: rgba(156,200,190,.24);
}
* { box-sizing: border-box; }
html, body { min-width: 0; margin: 0; background: transparent; color: var(--module-text); }
body { padding: 1px; font: 13px/1.55 var(--module-font); }
button, input, select, textarea { font: inherit; }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {
  outline: 2px solid var(--module-ring); outline-offset: 2px;
}
:disabled, [aria-disabled="true"] { cursor: not-allowed !important; opacity: .62; }
#surface-root { min-width: 0; }
`.trim();

const SURFACE_GENERATED_POLICY_CSS = `
body.response-surface-generated #surface-root {
  position:relative; min-height:100%; padding:14px; overflow:auto; contain:paint; isolation:isolate;
  background:var(--module-row); color:var(--module-text);
  font-family:var(--module-font);
}
body.response-surface-generated #surface-root :where(h1,h2,h3,h4,p) { margin-top:0; }
body.response-surface-generated #surface-root :where(h1,h2,h3,h4,strong,legend,label) { color:var(--module-strong); }
body.response-surface-generated #surface-root :where(p,small,figcaption) { color:var(--module-muted); }
body.response-surface-generated #surface-root :where(form,fieldset) { min-width:0; margin:0; padding:0; border:0; }
body.response-surface-generated #surface-root :where(input,select,textarea) {
  min-height:38px; max-width:100%; border:0; border-radius:8px; background:var(--module-surface);
  color:var(--module-text); box-shadow:inset 0 0 0 1px var(--module-border);
}
body.response-surface-generated #surface-root :where(input,select) { padding:0 11px; }
body.response-surface-generated #surface-root textarea { min-height:104px; padding:10px 11px; resize:vertical; }
body.response-surface-generated #surface-root :where(input,select,textarea):focus-visible {
  outline:2px solid var(--module-ring); outline-offset:1px;
}
body.response-surface-generated #surface-root :where(button,[role="button"])[data-manor-action],
body.response-surface-generated #surface-root form[data-manor-action] :where(button[type="submit"],input[type="submit"]) {
  display:inline-flex; min-height:38px; align-items:center; justify-content:center; padding:0 15px;
  border:0; border-radius:8px; background:var(--module-accent); color:var(--module-on-accent); font-weight:700;
  box-shadow:0 1px 2px rgba(28,25,23,.08); cursor:pointer;
}
body.response-surface-generated #surface-root :where(button,[role="button"])[data-manor-action]:hover:not(:disabled),
body.response-surface-generated #surface-root form[data-manor-action] :where(button[type="submit"],input[type="submit"]):hover:not(:disabled) {
  filter:brightness(.96);
}
body.response-surface-generated #surface-root :where(code,pre,kbd,samp) { font-family:var(--module-mono); }
@media(max-width:560px) {
  body.response-surface-generated #surface-root { padding:10px; }
  body.response-surface-generated #surface-root :where(button,[role="button"])[data-manor-action],
  body.response-surface-generated #surface-root form[data-manor-action] :where(button[type="submit"],input[type="submit"]) { width:100%; }
}
`.trim();

const SURFACE_ACTIVITY_CSS = `
#surface-activity { position:relative; z-index:1; margin:10px 1px 1px; padding:10px 12px; border-radius:8px; background:var(--module-surface); box-shadow:inset 0 0 0 1px var(--module-border); }
#surface-activity .activity-heading { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:7px; }
#surface-activity .activity-heading strong { color:var(--module-muted); font-size:11px; }
#surface-activity .activity-heading span { color:var(--module-faint); font:10px/1 var(--module-mono); }
#surface-activity ol { display:grid; gap:7px; margin:0; padding:0; list-style:none; }
#surface-activity li { display:flex; min-width:0; align-items:baseline; justify-content:space-between; gap:12px; color:var(--module-text); }
#surface-activity li strong { overflow:hidden; color:var(--module-strong); font-size:12px; text-overflow:ellipsis; white-space:nowrap; }
#surface-activity li span { overflow:hidden; color:var(--module-muted); font:11px/1.45 var(--module-mono); text-align:right; text-overflow:ellipsis; white-space:nowrap; }
#surface-activity li span b { color:var(--module-strong); font:700 10px/1.45 var(--module-font); }
#surface-activity details { margin-top:7px; }
#surface-activity summary { width:max-content; color:var(--module-accent); font:700 10px/1.5 var(--module-mono); cursor:pointer; list-style:none; }
#surface-activity summary::-webkit-details-marker { display:none; }
#surface-activity details ol { max-height:150px; margin-top:7px; padding-top:7px; overflow:auto; border-top:1px solid var(--module-border); }
#surface-error { position:relative; z-index:1; }
`.trim();

export interface ResponseSurfaceActivityLabels {
  history: string;
  submitted: string;
  lines: string;
}

export interface ResponseSurfaceLabels extends ResponseSurfaceActivityLabels {
  codeExercise: string;
  codeEditor: string;
  language: string;
  line: string;
  column: string;
  spaces: string;
  checks: string;
  runCode: string;
  submitAnswer: string;
  failed: string;
  interrupted: string;
}

const DEFAULT_RESPONSE_SURFACE_LABELS: ResponseSurfaceLabels = {
  history: "Activity",
  submitted: "Submitted",
  lines: "lines",
  codeExercise: "Code exercise",
  codeEditor: "Code editor",
  language: "Language",
  line: "Ln",
  column: "Col",
  spaces: "Spaces",
  checks: "Checks",
  runCode: "Run code",
  submitAnswer: "Submit answer",
  failed: "Failed",
  interrupted: "Interrupted",
};

function receiptSummary(
  surface: AssistantResponseSurfaceBlock,
  receipt: ResponseSurfaceSubmissionReceipt,
  labels: ResponseSurfaceActivityLabels,
): string {
  if (surface.render.kind === "template") {
    if (surface.render.template_id === "response.choice") {
      const selected = String(receipt.payload.choice || "");
      const options = Array.isArray(surface.render.props.options)
        ? surface.render.props.options
        : [];
      const option = options
        .map(objectValue)
        .find((item) => String(item?.id || "") === selected);
      return boundedString(option?.label, 120) || selected || labels.submitted;
    }
    if (surface.render.template_id === "learning.code_lab") {
      const language = boundedString(receipt.payload.language, 32)
        || boundedString(surface.render.props.language, 32)
        || "code";
      const code = boundedString(receipt.payload.code, 30_000);
      const lineCount = code ? code.split(/\r?\n/).length : 0;
      return lineCount ? `${language} · ${lineCount} ${labels.lines}` : language;
    }
  }
  const details = Object.entries(receipt.payload)
    .filter(([key]) => key !== "code")
    .slice(0, 3)
    .map(([key, value]) => {
      const text = Array.isArray(value) ? value.join(", ") : String(value ?? "");
      return `${key}: ${text.slice(0, 80)}`;
    });
  return details.join(" · ") || labels.submitted;
}

function activityMarkup(
  surface: AssistantResponseSurfaceBlock,
  receipts: ResponseSurfaceSubmissionReceipt[],
  labels: ResponseSurfaceLabels,
): string {
  if (!receipts.length) return "";
  const rows = receipts.slice(-20).reverse();
  const rowMarkup = (receipt: ResponseSurfaceSubmissionReceipt) => {
    const actionLabel = surface.render.kind === "template"
      && surface.render.template_id === "learning.code_lab"
      && receipt.action === "run"
      ? labels.runCode
      : surface.render.kind === "template"
        && surface.render.template_id === "response.choice"
        && receipt.action === "answer"
        ? labels.submitAnswer
        : receipt.actionLabel;
    const outcome = receipt.outcome === "interrupted"
      ? labels.interrupted
      : receipt.outcome === "failed"
        ? labels.failed
        : "";
    return `<li><strong>${escapeHtml(actionLabel)}</strong><span>${outcome ? `<b>${escapeHtml(outcome)}</b> · ` : ""}${escapeHtml(receiptSummary(surface, receipt, labels))}</span></li>`;
  };
  const latest = rows[0]!;
  const previous = rows.slice(1);
  const activityClass = surface.render.kind === "template" &&
    surface.render.template_id === "learning.code_lab" ? "code-activity" : "surface-activity";
  return `<section id="surface-activity" class="${activityClass}" aria-label="${escapeHtml(labels.history)}">
    <div class="activity-heading"><strong>${escapeHtml(labels.history)}</strong><span>${receipts.length}</span></div>
    <ol>${rowMarkup(latest)}</ol>
    ${previous.length ? `<details><summary aria-label="${escapeHtml(labels.history)} +${previous.length}">+${previous.length}</summary><ol>${previous.map(rowMarkup).join("")}</ol></details>` : ""}
  </section>`;
}

type CodeLabLanguage = {
  id: string;
  label: string;
  filename: string;
  starterCode: string;
};

const DEFAULT_CODE_LAB_LANGUAGES: CodeLabLanguage[] = [
  { id: "python", label: "Python", filename: "main.py", starterCode: "# Write your solution here\n" },
  { id: "javascript", label: "JavaScript", filename: "main.js", starterCode: "// Write your solution here\n" },
  { id: "typescript", label: "TypeScript", filename: "main.ts", starterCode: "// Write your solution here\n" },
  { id: "java", label: "Java", filename: "Main.java", starterCode: "// Write your solution here\n" },
  { id: "cpp", label: "C++", filename: "main.cpp", starterCode: "// Write your solution here\n" },
  { id: "go", label: "Go", filename: "main.go", starterCode: "// Write your solution here\n" },
  { id: "rust", label: "Rust", filename: "main.rs", starterCode: "// Write your solution here\n" },
];

function codeLabLanguages(props: Record<string, any>, initialLanguage: string): CodeLabLanguage[] {
  const configured = Array.isArray(props.languages)
    ? props.languages.slice(0, 8).map((raw): CodeLabLanguage | null => {
      const item = objectValue(raw);
      const id = boundedString(item?.id, 32).trim().toLowerCase();
      if (!SAFE_ID.test(id)) return null;
      return {
        id,
        label: boundedString(item?.label, 40).trim() || id,
        filename: boundedString(item?.filename, 80).trim() || id,
        starterCode: boundedString(item?.starter_code, 10_000),
      };
    }).filter((item): item is CodeLabLanguage => item !== null)
    : [];
  const languages = configured.length
    ? configured
    : DEFAULT_CODE_LAB_LANGUAGES.map((item) => ({ ...item }));
  if (!languages.some((item) => item.id === initialLanguage)) {
    languages.unshift({
      id: initialLanguage,
      label: initialLanguage,
      filename: initialLanguage,
      starterCode: "",
    });
  }
  return languages.slice(0, 8);
}

function codeLabBundle(props: Record<string, any>, labels: ResponseSurfaceLabels) {
  const languageCandidate = boundedString(props.language, 32).trim().toLowerCase();
  const language = SAFE_ID.test(languageCandidate) ? languageCandidate : "text";
  const instructions = boundedString(props.instructions, 4_000);
  const starterCode = boundedString(props.starter_code, 30_000);
  const tests = Array.isArray(props.tests)
    ? props.tests.slice(0, 20).map((test) => boundedString(test, 2_000)).filter(Boolean)
    : [];
  const languages = codeLabLanguages(props, language).map((item) => (
    item.id === language ? { ...item, starterCode: starterCode || item.starterCode } : item
  ));
  const initial = languages.find((item) => item.id === language) || languages[0];
  return {
    html: `<main class="code-lab">
      <header class="lab-brief"><span>${escapeHtml(labels.codeExercise)}</span><h2>${escapeHtml(instructions)}</h2></header>
      <form data-manor-action="run">
        <section class="ide-shell" aria-label="${escapeHtml(labels.codeEditor)}">
          <div class="ide-toolbar">
            <div class="file-tab"><span aria-hidden="true">&lt;/&gt;</span><strong data-code-filename>${escapeHtml(initial.filename)}</strong></div>
            <label class="language-control" for="surface-language">
              <span>${escapeHtml(labels.language)}</span>
              <select id="surface-language" name="language" data-code-language>
                ${languages.map((item) => `<option value="${escapeHtml(item.id)}"${item.id === language ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
              </select>
            </label>
          </div>
          <div class="editor-workbench">
            <pre class="line-numbers" data-code-lines aria-hidden="true">1</pre>
            <div class="code-stack">
              <pre class="code-highlight" aria-hidden="true"><code data-code-highlight></code></pre>
              <textarea id="surface-code" name="code" data-code-editor aria-label="${escapeHtml(labels.codeEditor)}" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="30000" required>${escapeHtml(initial.starterCode)}</textarea>
            </div>
          </div>
          <div class="ide-status" aria-live="polite">
            <span data-code-language-status>${escapeHtml(initial.label)}</span>
            <span data-code-position>${escapeHtml(labels.line)} 1, ${escapeHtml(labels.column)} 1</span>
            <span>${escapeHtml(labels.spaces)}: 2</span>
          </div>
        </section>
        <div class="lab-footer">
          ${tests.length ? `<section class="checks"><strong>${escapeHtml(labels.checks)}</strong><ul>${tests.map((test) => `<li>${escapeHtml(test)}</li>`).join("")}</ul></section>` : "<span></span>"}
          <button type="submit" data-manor-action="run"><span aria-hidden="true">▶</span> ${escapeHtml(labels.runCode)}</button>
        </div>
      </form>
    </main>`,
    css: `
      .code-lab {
        --ide-editor: #fbfaf8;
        --ide-line: #c7c1b8;
        --ide-code: #292524;
        --syntax-keyword: #3f7067;
        --syntax-string: #9a603d;
        --syntax-number: #746291;
        --syntax-comment: #8f887f;
        --syntax-function: #466c91;
        --syntax-type: #8a5979;
        --syntax-builtin: #6f6642;
        --syntax-operator: #6b625b;
        display:flex; min-height:100%; flex-direction:column; gap:12px; padding:14px; background:var(--module-row);
      }
      :root[data-theme="dark"] .code-lab {
        --ide-editor: #211f1d;
        --ide-line: #77716b;
        --ide-code: #f5f5f4;
        --syntax-keyword: #9cc8be;
        --syntax-string: #d4a27f;
        --syntax-number: #b5a4d2;
        --syntax-comment: #96908a;
        --syntax-function: #9fb9d1;
        --syntax-type: #c8a0b8;
        --syntax-builtin: #c6b985;
        --syntax-operator: #c7c1bb;
      }
      .lab-brief { padding:0 2px; }
      .lab-brief > span { color:var(--module-muted); font:700 10px/1.3 var(--module-mono); letter-spacing:.08em; text-transform:uppercase; }
      h2 { margin:5px 0 0; color:var(--module-strong); font-size:14px; line-height:1.45; }
      form { display:flex; min-height:0; flex:1; flex-direction:column; gap:10px; }
      .ide-shell { min-width:0; overflow:hidden; border:1px solid var(--module-border); border-radius:10px; background:var(--module-surface); box-shadow:0 10px 24px rgba(28,25,23,.07); }
      .ide-toolbar { display:flex; min-height:42px; align-items:center; justify-content:space-between; gap:12px; border-bottom:1px solid var(--module-border); background:var(--module-sunken); }
      .file-tab { align-self:stretch; display:flex; min-width:0; align-items:center; gap:8px; padding:0 13px; border-right:1px solid var(--module-border); background:var(--module-surface); color:var(--module-strong); }
      .file-tab > span { color:var(--module-accent); font:700 11px/1 var(--module-mono); }
      .file-tab strong { overflow:hidden; font:600 11px/1.3 var(--module-mono); text-overflow:ellipsis; white-space:nowrap; }
      .language-control { display:flex; align-items:center; gap:8px; padding-right:8px; color:var(--module-muted); font-size:10px; font-weight:700; text-transform:uppercase; }
      select { min-height:30px; padding:0 26px 0 9px; border:1px solid var(--module-border); border-radius:7px; background:var(--module-surface); color:var(--module-strong); font:600 11px/1 var(--module-font); cursor:pointer; }
      .editor-workbench { display:grid; min-height:300px; grid-template-columns:48px minmax(0,1fr); background:var(--ide-editor); }
      .line-numbers { min-height:300px; margin:0; padding:15px 11px 18px 0; overflow:hidden; border-right:1px solid var(--module-border); color:var(--ide-line); font:12px/1.65 var(--module-mono); text-align:right; user-select:none; }
      .code-stack { position:relative; min-width:0; min-height:300px; overflow:hidden; }
      .code-highlight, textarea { position:absolute; inset:0; width:100%; height:100%; margin:0; padding:15px 16px 18px; border:0; font:13px/1.65 var(--module-mono); tab-size:2; white-space:pre; }
      .code-highlight { z-index:0; overflow:hidden; pointer-events:none; background:transparent; color:var(--ide-code); }
      .code-highlight code { font:inherit; }
      textarea { z-index:1; resize:none; outline:0; background:transparent; color:transparent; caret-color:var(--module-accent); -webkit-text-fill-color:transparent; }
      textarea::selection { background:var(--module-ring); }
      textarea:focus-visible { outline:0; }
      .ide-shell:focus-within { border-color:var(--module-accent); box-shadow:0 0 0 3px var(--module-ring),0 10px 24px rgba(28,25,23,.07); }
      .ide-status { display:flex; min-height:28px; align-items:center; justify-content:flex-end; gap:14px; padding:0 11px; border-top:1px solid var(--module-border); background:var(--module-sunken); color:var(--module-muted); font:10px/1 var(--module-mono); }
      .ide-status [data-code-language-status] { margin-right:auto; color:var(--module-accent); }
      .syntax-keyword { color:var(--syntax-keyword); font-weight:650; }
      .syntax-string { color:var(--syntax-string); }
      .syntax-number { color:var(--syntax-number); }
      .syntax-comment { color:var(--syntax-comment); font-style:italic; }
      .syntax-function { color:var(--syntax-function); }
      .syntax-type { color:var(--syntax-type); }
      .syntax-builtin { color:var(--syntax-builtin); }
      .syntax-operator { color:var(--syntax-operator); }
      .lab-footer { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; }
      .checks { min-width:0; flex:1; padding:10px 12px; border-radius:8px; background:var(--module-surface); box-shadow:inset 0 0 0 1px var(--module-border); }
      .checks strong { color:var(--module-muted); font-size:11px; font-weight:700; }
      ul { margin:5px 0 0; padding-left:18px; color:var(--module-muted); }
      button { display:inline-flex; min-height:38px; flex:0 0 auto; align-items:center; gap:7px; padding:0 15px; border:0; border-radius:8px; background:var(--module-accent); color:var(--module-on-accent); font-weight:700; cursor:pointer; }
      button:hover:not(:disabled) { filter:brightness(.96); }
      #surface-activity { border:0; background:var(--module-surface); box-shadow:inset 0 0 0 1px var(--module-border); }
      #surface-activity details ol { max-height:140px; }
      @media(max-width:560px){
        .code-lab{padding:10px}.language-control>span{display:none}.editor-workbench{min-height:280px;grid-template-columns:40px minmax(0,1fr)}.line-numbers,.code-stack{min-height:280px}.lab-footer{align-items:stretch;flex-direction:column}.lab-footer button{justify-content:center;width:100%}
      }
    `,
    javascript: `
      window.renderResponseSurface = function(data, host) {
        const selector = document.querySelector("[data-code-language]");
        const editor = document.querySelector("[data-code-editor]");
        const highlight = document.querySelector("[data-code-highlight]");
        const lineNumbers = document.querySelector("[data-code-lines]");
        const filename = document.querySelector("[data-code-filename]");
        const languageStatus = document.querySelector("[data-code-language-status]");
        const position = document.querySelector("[data-code-position]");
        if (!selector || !editor || !highlight || !lineNumbers || !filename || !languageStatus || !position) return;
        const keywordWords = {
          python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield".split(" "),
          javascript: "as async await break case catch class const continue debugger default delete do else export extends false finally for from function get if import in instanceof let new null of return set static super switch this throw true try typeof undefined var void while with yield".split(" "),
          typescript: "abstract any as asserts async await bigint boolean break case catch class const constructor continue declare default delete do else enum export extends false finally for from function get if implements import in infer instanceof interface is keyof let module namespace never new null number object of override private protected public readonly require return satisfies set static string super switch symbol this throw true try type typeof undefined unique unknown var void while with yield".split(" "),
          java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends false final finally float for goto if implements import instanceof int interface long native new null package private protected public return short static strictfp super switch synchronized this throw throws transient true try void volatile while".split(" "),
          cpp: "alignas alignof and asm auto bool break case catch char class const constexpr continue default delete do double else enum explicit export extern false float for friend if inline int long mutable namespace new noexcept nullptr operator private protected public register reinterpret_cast return short signed sizeof static static_assert struct switch template this thread_local throw true try typedef typeid typename union unsigned using virtual void volatile while".split(" "),
          go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var".split(" "),
          rust: "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while".split(" "),
        };
        const builtinWords = {
          python: "abs all any bool dict enumerate filter float int len list map max min open print range reversed round set sorted str sum tuple zip".split(" "),
          javascript: "Array BigInt Boolean Date Error JSON Map Math Number Object Promise RegExp Set String console document globalThis window".split(" "),
          typescript: "Array BigInt Boolean Date Error JSON Map Math Number Object Promise RegExp Set String console document globalThis window".split(" "),
          java: "Integer Long Double Float Boolean Character Math String System".split(" "),
          cpp: "cin cout endl size_t std string vector".split(" "),
          go: "append cap close copy delete len make new panic print println recover".split(" "),
          rust: "Box Err None Ok Option Result Self Some String Vec println".split(" "),
        };
        const escapeToken = function(value) {
          return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        };
        const syntaxToken = function(kind, value) {
          return '<span class="syntax-' + kind + '">' + escapeToken(value) + "</span>";
        };
        const highlightCode = function(code, language) {
          const keywords = new Set(keywordWords[language] || []);
          const builtins = new Set(builtinWords[language] || []);
          let html = "";
          let index = 0;
          while (index < code.length) {
            const character = code[index];
            const next = code[index + 1] || "";
            if (character === "/" && next === "*") {
              const close = code.indexOf("*/", index + 2);
              const end = close < 0 ? code.length : close + 2;
              html += syntaxToken("comment", code.slice(index, end));
              index = end;
              continue;
            }
            if ((language === "python" && character === "#") || (language !== "python" && character === "/" && next === "/")) {
              const newline = code.indexOf("\\n", index);
              const end = newline < 0 ? code.length : newline;
              html += syntaxToken("comment", code.slice(index, end));
              index = end;
              continue;
            }
            if (character === '"' || character === "'" || character.charCodeAt(0) === 96) {
              const triple = language === "python" && code.slice(index, index + 3) === character + character + character;
              const delimiterLength = triple ? 3 : 1;
              let end = index + delimiterLength;
              let escaped = false;
              while (end < code.length) {
                if (!triple && character.charCodeAt(0) !== 96 && code[end] === "\\n") break;
                if (!escaped && code.slice(end, end + delimiterLength) === character.repeat(delimiterLength)) {
                  end += delimiterLength;
                  break;
                }
                escaped = !escaped && code[end] === "\\\\";
                if (code[end] !== "\\\\") escaped = false;
                end += 1;
              }
              html += syntaxToken("string", code.slice(index, end));
              index = end;
              continue;
            }
            if (/[0-9]/.test(character)) {
              let end = index + 1;
              while (end < code.length && /[0-9A-Fa-f_xXoObB.]/.test(code[end])) end += 1;
              html += syntaxToken("number", code.slice(index, end));
              index = end;
              continue;
            }
            if (/[A-Za-z_$]/.test(character)) {
              let end = index + 1;
              while (end < code.length && /[A-Za-z0-9_$]/.test(code[end])) end += 1;
              const word = code.slice(index, end);
              let kind = "";
              if (keywords.has(word)) kind = "keyword";
              else if (builtins.has(word)) kind = "builtin";
              else {
                let lookahead = end;
                while (lookahead < code.length && (code[lookahead] === " " || code[lookahead] === "\\t")) lookahead += 1;
                if (code[lookahead] === "(") kind = "function";
                else if (word[0] === word[0].toUpperCase() && word[0] !== word[0].toLowerCase()) kind = "type";
              }
              html += kind ? syntaxToken(kind, word) : escapeToken(word);
              index = end;
              continue;
            }
            if (/[+*/%=!<>&|^~?:-]/.test(character)) {
              let end = index + 1;
              while (end < code.length && /[+*/%=!<>&|^~?:-]/.test(code[end])) end += 1;
              html += syntaxToken("operator", code.slice(index, end));
              index = end;
              continue;
            }
            html += escapeToken(character);
            index += 1;
          }
          return html;
        };
        const options = Array.isArray(data.languages) ? data.languages : [];
        const byId = Object.create(null);
        const drafts = Object.create(null);
        const restoredDrafts = host && host.draft && host.draft.draftsByLanguage
          && typeof host.draft.draftsByLanguage === "object"
          && !Array.isArray(host.draft.draftsByLanguage)
          ? host.draft.draftsByLanguage
          : null;
        options.forEach(function(item) {
          if (!item || typeof item.id !== "string") return;
          byId[item.id] = item;
          drafts[item.id] = restoredDrafts && typeof restoredDrafts[item.id] === "string"
            ? restoredDrafts[item.id].slice(0, 30000)
            : typeof item.starterCode === "string" ? item.starterCode : "";
        });
        const restoredLanguage = host && host.draft && typeof host.draft.language === "string"
          ? host.draft.language
          : data.initialLanguage;
        const canRestoreLanguage = Boolean(byId[restoredLanguage]);
        if (canRestoreLanguage) selector.value = restoredLanguage;
        let currentLanguage = selector.value;
        const restoredCode = canRestoreLanguage && host && host.draft && typeof host.draft.code === "string"
          ? host.draft.code
          : drafts[currentLanguage] || "";
        drafts[currentLanguage] = restoredCode;
        editor.value = restoredCode;
        const syncChrome = function() {
          const item = byId[currentLanguage] || { label: currentLanguage, filename: currentLanguage };
          filename.textContent = item.filename || currentLanguage;
          languageStatus.textContent = item.label || currentLanguage;
          highlight.innerHTML = highlightCode(editor.value, currentLanguage);
          const lines = editor.value.split(/\\r?\\n/).length;
          lineNumbers.textContent = Array.from({ length: Math.max(1, lines) }, function(_, index) { return String(index + 1); }).join("\\n");
          lineNumbers.scrollTop = editor.scrollTop;
          highlight.scrollTop = editor.scrollTop;
          highlight.scrollLeft = editor.scrollLeft;
          const beforeCursor = editor.value.slice(0, editor.selectionStart || 0).split(/\\r?\\n/);
          position.textContent = ${safeJson(`${labels.line} `)} + beforeCursor.length + ${safeJson(`, ${labels.column} `)} + (beforeCursor[beforeCursor.length - 1].length + 1);
        };
        selector.addEventListener("change", function() {
          drafts[currentLanguage] = editor.value;
          currentLanguage = selector.value;
          editor.value = Object.prototype.hasOwnProperty.call(drafts, currentLanguage) ? drafts[currentLanguage] : "";
          syncChrome();
          editor.dispatchEvent(new Event("input", { bubbles: true }));
          editor.focus();
        });
        editor.addEventListener("input", syncChrome);
        editor.addEventListener("click", syncChrome);
        editor.addEventListener("keyup", syncChrome);
        editor.addEventListener("select", syncChrome);
        editor.addEventListener("scroll", function() {
          lineNumbers.scrollTop = editor.scrollTop;
          highlight.scrollTop = editor.scrollTop;
          highlight.scrollLeft = editor.scrollLeft;
        });
        editor.addEventListener("keydown", function(event) {
          if (event.key !== "Tab") return;
          event.preventDefault();
          const start = editor.selectionStart;
          const end = editor.selectionEnd;
          editor.setRangeText("  ", start, end, "end");
          drafts[currentLanguage] = editor.value;
          editor.dispatchEvent(new Event("input", { bubbles: true }));
        });
        window.collectResponseSurfaceState = function() {
          drafts[currentLanguage] = editor.value;
          const draftsByLanguage = Object.create(null);
          options.forEach(function(item) {
            if (item && typeof item.id === "string" && typeof drafts[item.id] === "string") {
              draftsByLanguage[item.id] = drafts[item.id].slice(0, 30000);
            }
          });
          return {
            language: currentLanguage,
            code: editor.value,
            draftsByLanguage,
          };
        };
        syncChrome();
      };
    `,
    data: { initialLanguage: language, languages },
  };
}

function choiceBundle(props: Record<string, any>, labels: ResponseSurfaceLabels) {
  const prompt = boundedString(props.prompt, 1_000);
  const options = Array.isArray(props.options) ? props.options.slice(0, 12) : [];
  return {
    html: `<form class="choice" data-manor-action="answer">
      <fieldset><legend>${escapeHtml(prompt)}</legend>
      <div class="options">${options.map((raw) => {
        const option = objectValue(raw) || {};
        return `<label><input type="radio" name="choice" value="${escapeHtml(option.id)}" required><span><strong>${escapeHtml(option.label)}</strong>${option.description ? `<small>${escapeHtml(option.description)}</small>` : ""}</span></label>`;
      }).join("")}</div></fieldset>
      <button type="submit" data-manor-action="answer">${escapeHtml(labels.submitAnswer)}</button>
    </form>`,
    css: `
      .choice { display:flex; min-height:100%; flex-direction:column; gap:12px; padding:14px; background:var(--module-row); }
      fieldset { min-width:0; margin:0; padding:0; border:0; }
      legend { margin-bottom:12px; color:var(--module-strong); font-size:15px; font-weight:750; }
      .options { display:grid; gap:8px; }
      .options label { display:flex; align-items:flex-start; gap:10px; padding:11px 12px; border-radius:8px; background:var(--module-surface); cursor:pointer; box-shadow:inset 0 0 0 1px var(--module-border); }
      input { margin-top:3px; accent-color:var(--module-accent); }
      strong, small { display:block; } small { margin-top:2px; color:var(--module-muted); }
      button { align-self:flex-end; min-height:36px; padding:0 15px; border:0; border-radius:8px; background:var(--module-accent); color:var(--module-on-accent); font-weight:700; cursor:pointer; }
      @media(max-width:560px){.choice{padding:10px}button{width:100%}}
    `,
    javascript: "window.renderResponseSurface = function() {};",
    data: {},
  };
}

function interactiveDocument(
  surface: AssistantResponseSurfaceBlock,
  bundle: { html: string; css: string; javascript: string; data: Record<string, unknown> },
  theme: ResolvedDocumentTheme,
  draft: Record<string, unknown>,
  receipts: ResponseSurfaceSubmissionReceipt[],
  activityLabels: ResponseSurfaceLabels,
  executeBundleJavascript: boolean,
  documentLanguage: string,
): string {
  const allowedActions = surface.actions.map((action) => action.id);
  const generatedSurface = !executeBundleJavascript;
  const legacyGeneratedSurface = generatedSurface
    && surface.render.kind === "sandboxed_html"
    && surface.render.validation.policy === "response_surface.v1";
  const imageSourcePolicy = legacyGeneratedSurface ? "data:" : "'none'";
  const generatedPolicyCss = generatedSurface ? SURFACE_GENERATED_POLICY_CSS : "";
  const scopedBundleCss = legacyGeneratedSurface
    ? rewriteLegacySurfaceRootSelectors(bundle.css)
    : bundle.css;
  const bundleCss = generatedSurface
    ? `@scope (#surface-root) {\n${scopedBundleCss}\n}`
    : bundle.css;
  return `<!doctype html><html lang="${escapeHtml(normalizeDocumentLanguage(documentLanguage))}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src ${imageSourcePolicy}; font-src data:; connect-src 'none'; object-src 'none'; media-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; navigate-to 'none'">
<style>${SURFACE_BASE_CSS}\n${escapeClosingTag(bundleCss, "style")}\n${generatedPolicyCss}\n${SURFACE_ACTIVITY_CSS}</style></head>
<body class="${generatedSurface ? "response-surface-generated" : "response-surface-template"}" data-manor-ui-policy="response-surface.v1"><div id="surface-root"></div>${activityMarkup(surface, receipts, activityLabels)}<div id="surface-error" role="alert" hidden></div>
<script>(() => {
  const surfaceRoot = document.getElementById("surface-root");
  if (!surfaceRoot) return;
  surfaceRoot.innerHTML = ${safeJson(bundle.html)};
  if (${safeJson(generatedSurface)}) {
    const reservedHostIds = new Set(["surface-root", "surface-activity", "surface-error"]);
    surfaceRoot.querySelectorAll("[id]").forEach((node) => {
      if (reservedHostIds.has(node.id.trim().toLowerCase())) node.removeAttribute("id");
    });
  }
  const surfaceId = ${safeJson(surface.id)};
  const allowedActions = new Set(${safeJson(allowedActions)});
  const bridgeNonce = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint32Array(4)), (value) => value.toString(16)).join("");
  const parentWindow = window.parent;
  const parentPostMessage = parentWindow.postMessage.bind(parentWindow);
  const queryAll = Element.prototype.querySelectorAll;
  const closest = Element.prototype.closest;
  const objectEntries = Object.entries;
  const objectHasOwn = Object.prototype.hasOwnProperty;
  const objectCreate = Object.create;
  const nodeContains = Node.prototype.contains;
  const promiseResolve = Promise.resolve.bind(Promise);
  const requestFrame = window.requestAnimationFrame.bind(window);
  const escapeName = CSS.escape.bind(CSS);
  const Input = HTMLInputElement;
  const Button = HTMLButtonElement;
  const Form = HTMLFormElement;
  const Textarea = HTMLTextAreaElement;
  const Select = HTMLSelectElement;
  const ElementType = Element;
  const HTMLElementType = HTMLElement;
  const checkFormValidity = Form.prototype.checkValidity;
  const reportFormValidity = Form.prototype.reportValidity;
  const send = (type, detail = {}) => parentPostMessage({ type, surfaceId, bridgeNonce, ...detail }, "*");
  send("manor:response-surface:bridge-ready");
  const fields = () => {
    const payload = objectCreate(null);
    queryAll.call(surfaceRoot, "[name]").forEach((field) => {
      if (!(field instanceof Input || field instanceof Textarea || field instanceof Select)) return;
      if ((field.type === "radio" || field.type === "checkbox") && !field.checked) return;
      const values = field instanceof Select && field.multiple
        ? Array.from(field.selectedOptions, (option) => option.value)
        : [field.value];
      values.forEach((value) => {
        if (objectHasOwn.call(payload, field.name)) {
          payload[field.name] = Array.isArray(payload[field.name]) ? [...payload[field.name], value] : [payload[field.name], value];
        } else payload[field.name] = value;
      });
    });
    return payload;
  };
  const statePayload = () => {
    const collect = window.collectResponseSurfaceState;
    if (typeof collect === "function") {
      try {
        const payload = collect();
        if (payload && typeof payload === "object" && !Array.isArray(payload)) return payload;
      } catch (error) { showError(error); }
    }
    return fields();
  };
  const reportHeight = () => send("manor:response-surface:resize", { height: Math.ceil(Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) + 2) });
  const showError = (error) => { const node = document.getElementById("surface-error"); if (node) { node.hidden = false; node.textContent = error instanceof Error ? error.message : String(error); } reportHeight(); };
  document.documentElement.dataset.theme = ${safeJson(theme)} === "dark" ? "dark" : "light";
  const draft = ${safeJson(draft)};
  const restoreDraft = () => objectEntries(draft).forEach(([name, value]) => {
    const selected = new Set((Array.isArray(value) ? value : [value]).map((item) => String(item)));
    queryAll.call(surfaceRoot, '[name="' + escapeName(name) + '"]').forEach((field) => {
      if (!(field instanceof Input || field instanceof Textarea || field instanceof Select)) return;
      if (field instanceof Input && (field.type === "radio" || field.type === "checkbox")) {
        field.checked = selected.has(field.value);
        return;
      }
      if (field instanceof Select && field.multiple && Array.isArray(value)) {
        Array.from(field.options).forEach((option) => { option.selected = selected.has(option.value); });
        return;
      }
      if (typeof value === "string") field.value = value;
    });
  });
  document.addEventListener("input", (event) => {
    send("manor:response-surface:state", { payload: statePayload() });
  });
  document.addEventListener("change", (event) => {
    send("manor:response-surface:state", { payload: statePayload() });
  });
  document.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!event.isTrusted) return;
    const submitter = event.submitter instanceof HTMLElementType ? event.submitter : null;
    const form = event.target instanceof Form ? event.target : null;
    if (!form || !nodeContains.call(surfaceRoot, form)) return;
    if (!checkFormValidity.call(form)) {
      reportFormValidity.call(form);
      return;
    }
    const action = submitter?.dataset.manorAction || form?.dataset.manorAction || "";
    if (!allowedActions.has(action)) return;
    send("manor:response-surface:submit", { action, payload: fields() });
  });
  document.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    if (!(event.target instanceof ElementType)) return;
    if (!nodeContains.call(surfaceRoot, event.target)) return;
    const form = closest.call(event.target, "form");
    if (form) {
      if (!(form instanceof Form)) return;
      const submitter = closest.call(event.target, 'button, input[type="submit"], input[type="image"]');
      const isSubmitControl = submitter instanceof Button && submitter.type === "submit"
        || submitter instanceof Input && (submitter.type === "submit" || submitter.type === "image");
      if (!submitter || !isSubmitControl || !nodeContains.call(form, submitter)) return;
      event.preventDefault();
      if (!checkFormValidity.call(form)) {
        reportFormValidity.call(form);
        return;
      }
      const action = submitter.getAttribute("data-manor-action") || form.getAttribute("data-manor-action") || "";
      if (allowedActions.has(action)) send("manor:response-surface:submit", { action, payload: fields() });
      return;
    }
    const target = closest.call(event.target, "[data-manor-action]");
    if (!target) return;
    const action = target.getAttribute("data-manor-action") || "";
    if (allowedActions.has(action)) send("manor:response-surface:submit", { action, payload: fields() });
  });
  window.addEventListener("message", (event) => {
    if (!event.isTrusted || event.source !== parentWindow) return;
    if (event.data?.type === "manor-html-preview:host-ready") {
      send("manor:response-surface:bridge-ready");
      return;
    }
    if (event.data?.bridgeNonce !== bridgeNonce) return;
    if (event.data?.type === "manor:response-surface:request-state") {
      const requestId = typeof event.data.requestId === "string" ? event.data.requestId.slice(0, 128) : "";
      if (requestId) send("manor:response-surface:state", { payload: statePayload(), requestId });
      return;
    }
    if (event.data?.type !== "manor:response-surface:set-disabled") return;
    const disabled = event.data.disabled === true;
    queryAll.call(surfaceRoot, "input, select, textarea, button, [data-manor-action]").forEach((node) => {
      if ("disabled" in node) node.disabled = disabled;
      node.setAttribute("aria-disabled", disabled ? "true" : "false");
    });
    document.documentElement.setAttribute("aria-busy", event.data.busy === true ? "true" : "false");
  });
  const userScriptSource = ${safeJson(executeBundleJavascript ? escapeClosingTag(bundle.javascript, "script") : "")};
  if (userScriptSource) {
    try {
      const userScript = document.createElement("script");
      userScript.textContent = userScriptSource;
      document.body.appendChild(userScript);
    } catch (error) { showError(error); }
  }
  const render = window.renderResponseSurface;
  promiseResolve(typeof render === "function" ? render(${safeJson(bundle.data)}, { theme: ${safeJson(theme)}, draft }) : undefined)
    .then(() => { restoreDraft(); requestFrame(reportHeight); }).catch(showError);
  if (typeof ResizeObserver === "function") new ResizeObserver(reportHeight).observe(surfaceRoot);
  reportHeight();
})();</script></body></html>`;
}

export function responseSurfaceDocument(
  surface: AssistantResponseSurfaceBlock,
  theme: ResolvedDocumentTheme,
  draft: Record<string, unknown> = {},
  receipts: ResponseSurfaceSubmissionReceipt[] = [],
  surfaceLabels: Partial<ResponseSurfaceLabels> = {},
  documentLanguage = "en",
): string {
  const labels: ResponseSurfaceLabels = {
    ...DEFAULT_RESPONSE_SURFACE_LABELS,
    ...surfaceLabels,
  };
  const render = surface.render;
  if (render.kind === "template") {
    if (render.template_id === "workspace.ledger.overview") {
      return workspaceLedgerOverviewHtml(render.props as any, theme);
    }
    if (render.template_id === "workspace.ledger.query") {
      return workspaceLedgerQueryHtml(render.props as any, theme);
    }
    const bundle = render.template_id === "learning.code_lab"
      ? codeLabBundle(render.props, labels)
      : choiceBundle(render.props, labels);
    return interactiveDocument(
      surface,
      bundle,
      theme,
      draft,
      receipts,
      labels,
      true,
      documentLanguage,
    );
  }
  return interactiveDocument(surface, {
    html: render.code.html,
    css: render.code.css,
    javascript: render.code.javascript,
    data: render.data,
  }, theme, draft, receipts, labels, false, documentLanguage);
}

export function responseSurfaceSubmissionMessage(submission: ResponseSurfaceSubmission): string {
  const code = typeof submission.payload.code === "string" ? submission.payload.code : "";
  const language = typeof submission.payload.language === "string"
    ? submission.payload.language.replace(/[^A-Za-z0-9_+-]/g, "").slice(0, 32)
    : "text";
  if (code) {
    const isCodeRun = submission.context?.templateId === "learning.code_lab" &&
      submission.action === "run";
    const instructions = submission.context?.instructions?.trim();
    const checks = submission.context?.checks?.filter(Boolean) || [];
    const executionRequest = isCodeRun
      ? [
        "Execute this submission with the sandbox-backed bash tool using a single non-multiline command. Call search_tools with query select:bash now before execution, even if an earlier turn reported that bash was unavailable. Do not write files to the Workspace. Do not claim any test passed unless the tool output proves it. Evaluate it against the exercise requirements and report concise pass/fail feedback.",
        instructions ? `Exercise requirements:\n${instructions}` : "",
        checks.length ? `Checks:\n${checks.map((check) => `- ${check}`).join("\n")}` : "",
      ].filter(Boolean).join("\n\n")
      : "";
    return `${submission.actionLabel}: ${submission.title}${executionRequest ? `\n\n${executionRequest}` : ""}\n\n\`\`\`${language}\n${code.slice(0, 30_000)}\n\`\`\``;
  }
  const payload = JSON.stringify(submission.payload, null, 2).slice(0, 8_000);
  return `${submission.actionLabel}: ${submission.title}${payload && payload !== "{}" ? `\n\n${payload}` : ""}`;
}
