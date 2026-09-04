const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const SOURCE_ATTRIBUTE = "data-docx-paragraph-index";
const SOURCE_EDITABLE_ATTRIBUTE = "data-docx-source-editable";
const INSERT_AFTER_ATTRIBUTE = "data-docx-insert-after";
const PLACEHOLDER_ATTRIBUTE = "data-docx-placeholder";
const ALT_CHUNK_ATTRIBUTE = "data-docx-alt-chunk-id";
const BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li,div,blockquote,pre,figure,td,th,hr";
const DIRECT_INLINE_BLOCK_SELECTOR = "a[href],img[src]";
const WORD_PARAGRAPH_PATTERN = /<w:p(?:\s[^>]*)?\/>|<w:p(?:\s[^>]*)?>[\s\S]*?<\/w:p>/g;

export interface DocumentParagraphSource {
  index: number;
  text: string;
  editable: boolean;
  containsMedia?: boolean;
}

export interface DocumentParagraphEdits {
  replacements: Map<number, string>;
  insertions: Map<number, string[]>;
  formattedReplacements?: Map<number, string>;
  formattedContentReplacements?: Map<number, string>;
  formattedInsertions?: Map<number, string[]>;
  deletions?: Set<number>;
}

export class DocumentPreservationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DocumentPreservationError";
  }
}

function decodeXmlText(value: string): string {
  return value
    .replace(/&#x([0-9a-f]+);/gi, (_, hex: string) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, decimal: string) => String.fromCodePoint(parseInt(decimal, 10)))
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function escapeXmlText(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function escapeXmlAttribute(value: string): string {
  return escapeXmlText(value).replace(/\r/g, "&#13;").replace(/\n/g, "&#10;");
}

function normalizeText(value: string): string {
  return value.replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
}

function paragraphText(paragraphXml: string): string {
  const tokens = paragraphXml.match(/<w:t(?:\s[^>]*)?>[\s\S]*?<\/w:t>|<w:tab\b[^>]*\/>|<w:br\b[^>]*\/>|<w:cr\b[^>]*\/>/g) || [];
  return tokens.map((token) => {
    if (/^<w:tab\b/.test(token)) return "\t";
    if (/^<w:(?:br|cr)\b/.test(token)) return "\n";
    const content = token.match(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>/)?.[1] || "";
    return decodeXmlText(content);
  }).join("");
}

function paragraphIsEditable(paragraphXml: string): boolean {
  return !/<w:(?:fldChar|instrText|drawing|object|pict|altChunk|ins|del|moveFrom|moveTo)\b/i.test(paragraphXml);
}

export async function extractDocumentParagraphSources(buffer: ArrayBuffer): Promise<DocumentParagraphSource[]> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(buffer);
  const documentEntry = zip.file("word/document.xml");
  if (!documentEntry) throw new DocumentPreservationError("The DOCX package has no word/document.xml part.");
  const documentXml = await documentEntry.async("text");
  let revisionBlockDepth = 0;
  let cursor = 0;
  return Array.from(documentXml.matchAll(WORD_PARAGRAPH_PATTERN)).map((match, index) => {
    const paragraphXml = match[0];
    const paragraphStart = match.index || 0;
    for (const revision of documentXml.slice(cursor, paragraphStart).matchAll(/<w:(?:ins|del|moveFrom|moveTo)\b[^>]*>|<\/w:(?:ins|del|moveFrom|moveTo)>/g)) {
      if (revision[0].startsWith("</")) revisionBlockDepth = Math.max(0, revisionBlockDepth - 1);
      else if (!/\/\s*>$/.test(revision[0])) revisionBlockDepth += 1;
    }
    cursor = paragraphStart + paragraphXml.length;
    return {
      index,
      text: paragraphText(paragraphXml),
      editable: revisionBlockDepth === 0 && paragraphIsEditable(paragraphXml),
      containsMedia: /<w:(?:drawing|object|pict|altChunk)\b/i.test(paragraphXml),
    };
  });
}

function leafBlocks(root: ParentNode): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(`${BLOCK_SELECTOR},${DIRECT_INLINE_BLOCK_SELECTOR}`)).filter((element) => {
    if (element.parentElement === root && element.matches(DIRECT_INLINE_BLOCK_SELECTOR)) return true;
    return element.matches(BLOCK_SELECTOR) && !element.querySelector(BLOCK_SELECTOR);
  });
}

export function matchDocumentParagraphSourceIndexes(
  blocks: Array<{ text: string; containsMedia?: boolean }>,
  sources: DocumentParagraphSource[],
): Array<number | null> {
  let sourceCursor = 0;
  return blocks.map((block) => {
    const blockText = normalizeText(block.text);
    let fallbackIndex = -1;
    let matchedIndex = -1;
    for (let index = sourceCursor; index < sources.length; index += 1) {
      const source = sources[index];
      if (normalizeText(source.text) !== blockText) continue;
      if (fallbackIndex < 0) fallbackIndex = index;
      if (Boolean(source.containsMedia) !== Boolean(block.containsMedia)) continue;
      matchedIndex = index;
      break;
    }
    if (matchedIndex < 0) matchedIndex = fallbackIndex;
    if (matchedIndex < 0) return null;
    const source = sources[matchedIndex];
    sourceCursor = matchedIndex + 1;
    return source.index;
  });
}

export function annotateDocumentHtml(html: string, sources: DocumentParagraphSource[]): string {
  const parsed = new DOMParser().parseFromString(`<div id="docx-annotation-root">${html}</div>`, "text/html");
  const root = parsed.getElementById("docx-annotation-root");
  if (!root) return html;
  const blocks = leafBlocks(root);
  const sourceByIndex = new Map(sources.map((source) => [source.index, source]));
  const existingIndexes = blocks.map((block) => {
    const value = Number(block.getAttribute(SOURCE_ATTRIBUTE));
    return Number.isInteger(value) && sourceByIndex.has(value) ? value : null;
  });
  if (existingIndexes.every((value): value is number => value != null)
    && new Set(existingIndexes).size === existingIndexes.length) {
    blocks.forEach((block, index) => {
      const source = sourceByIndex.get(existingIndexes[index])!;
      block.setAttribute(SOURCE_EDITABLE_ATTRIBUTE, source.editable ? "true" : "false");
      block.removeAttribute(INSERT_AFTER_ATTRIBUTE);
    });
    return root.innerHTML;
  }
  const matches = matchDocumentParagraphSourceIndexes(blocks.map((block) => ({
    text: editableBlockText(block),
    containsMedia: block.matches("img,video,object") || Boolean(block.querySelector("img,video,object")),
  })), sources);

  blocks.forEach((block, blockIndex) => {
    const sourceIndex = matches[blockIndex];
    const source = sourceIndex == null ? undefined : sourceByIndex.get(sourceIndex);
    if (!source) return;
    block.setAttribute(SOURCE_ATTRIBUTE, String(source.index));
    block.setAttribute(SOURCE_EDITABLE_ATTRIBUTE, source.editable ? "true" : "false");
    block.removeAttribute(INSERT_AFTER_ATTRIBUTE);
  });

  return root.innerHTML;
}

export function recoverDocumentParagraphSourceIndexes(
  baseline: Array<{ sourceIndex: number | null; tagName: string }>,
  edited: Array<{ sourceIndex: number | null; tagName: string }>,
): Array<number | null> {
  const recovered = edited.map((block) => block.sourceIndex);
  const baselineSources = baseline.filter((block): block is { sourceIndex: number; tagName: string } => (
    block.sourceIndex != null
  ));
  const baselinePosition = new Map(baselineSources.map((block, index) => [block.sourceIndex, index]));
  const claimed = new Set(recovered.filter((sourceIndex): sourceIndex is number => sourceIndex != null));
  const anchors = recovered.flatMap((sourceIndex, editedIndex) => {
    const position = sourceIndex == null ? undefined : baselinePosition.get(sourceIndex);
    return position == null ? [] : [{ editedIndex, baselineIndex: position }];
  });
  if (anchors.some((anchor, index) => index > 0 && anchor.baselineIndex <= anchors[index - 1].baselineIndex)) {
    return recovered;
  }

  const boundaries = [
    { editedIndex: -1, baselineIndex: -1 },
    ...anchors,
    { editedIndex: edited.length, baselineIndex: baselineSources.length },
  ];
  for (let boundaryIndex = 1; boundaryIndex < boundaries.length; boundaryIndex += 1) {
    const previous = boundaries[boundaryIndex - 1];
    const next = boundaries[boundaryIndex];
    const editedIndexes: number[] = [];
    for (let index = previous.editedIndex + 1; index < next.editedIndex; index += 1) {
      if (recovered[index] == null) editedIndexes.push(index);
    }
    const availableSources = baselineSources
      .slice(previous.baselineIndex + 1, next.baselineIndex)
      .filter((block) => !claimed.has(block.sourceIndex));
    if (
      editedIndexes.length !== availableSources.length
      || editedIndexes.some((editedIndex, index) => edited[editedIndex].tagName !== availableSources[index].tagName)
    ) continue;
    editedIndexes.forEach((editedIndex, index) => {
      const sourceIndex = availableSources[index].sourceIndex;
      recovered[editedIndex] = sourceIndex;
      claimed.add(sourceIndex);
    });
  }
  return recovered;
}

function carryDocumentSourceAnnotations(baselineHtml: string, editedHtml: string): string {
  const baseline = parseAnnotatedHtml(baselineHtml);
  const edited = parseAnnotatedHtml(editedHtml);
  const sourceIndex = (block: HTMLElement): number | null => {
    const rawValue = block.getAttribute(SOURCE_ATTRIBUTE);
    if (rawValue == null) return null;
    const value = Number(rawValue);
    return Number.isInteger(value) && value >= 0 ? value : null;
  };
  const baselineBySource = new Map(baseline.blocks.flatMap((block) => {
    const source = sourceIndex(block);
    return source == null ? [] : [[source, block] as const];
  }));
  const recovered = recoverDocumentParagraphSourceIndexes(
    baseline.blocks.map((block) => ({ sourceIndex: sourceIndex(block), tagName: block.tagName })),
    edited.blocks.map((block) => ({ sourceIndex: sourceIndex(block), tagName: block.tagName })),
  );
  edited.blocks.forEach((editedBlock, index) => {
    if (editedBlock.hasAttribute(SOURCE_ATTRIBUTE)) return;
    const recoveredSource = recovered[index];
    const baselineBlock = recoveredSource == null ? undefined : baselineBySource.get(recoveredSource);
    if (!baselineBlock) return;
    editedBlock.setAttribute(SOURCE_ATTRIBUTE, String(recoveredSource));
    const baselineEditable = baselineBlock.getAttribute(SOURCE_EDITABLE_ATTRIBUTE);
    if (baselineEditable != null) editedBlock.setAttribute(SOURCE_EDITABLE_ATTRIBUTE, baselineEditable);
    editedBlock.removeAttribute(INSERT_AFTER_ATTRIBUTE);
  });
  return edited.root.innerHTML;
}

function parseAnnotatedHtml(html: string): { blocks: HTMLElement[]; root: HTMLElement } {
  const parsed = new DOMParser().parseFromString(`<div id="docx-edit-root">${html}</div>`, "text/html");
  const root = parsed.getElementById("docx-edit-root");
  if (!root) throw new DocumentPreservationError("Unable to read the DOCX editor content.");
  return { root, blocks: leafBlocks(root) };
}

function markupSignature(element: HTMLElement): string {
  const clone = element.cloneNode(true) as HTMLElement;
  clone.removeAttribute(INSERT_AFTER_ATTRIBUTE);
  clone.querySelectorAll("br").forEach((node) => node.remove());
  const walker = clone.ownerDocument.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    node.textContent = "";
    node = walker.nextNode();
  }
  return clone.outerHTML;
}

function blockShellSignature(element: HTMLElement): string {
  const clone = element.cloneNode(false) as HTMLElement;
  clone.removeAttribute(SOURCE_ATTRIBUTE);
  clone.removeAttribute(SOURCE_EDITABLE_ATTRIBUTE);
  clone.removeAttribute(INSERT_AFTER_ATTRIBUTE);
  return clone.outerHTML;
}

function editableBlockText(element: HTMLElement): string {
  if (element.matches(".doc-editor-page-break,[data-docx-page-break='true']")) return "\n";
  const clone = element.cloneNode(true) as HTMLElement;
  clone.querySelectorAll("br").forEach((lineBreak) => {
    lineBreak.replaceWith(lineBreak.hasAttribute(PLACEHOLDER_ATTRIBUTE) ? "" : "\n");
  });
  return clone.textContent || "";
}

function documentStructureSignature(root: HTMLElement): string {
  const clone = root.cloneNode(true) as HTMLElement;
  leafBlocks(clone).forEach((block) => {
    if (block.hasAttribute(SOURCE_ATTRIBUTE) || block.hasAttribute(INSERT_AFTER_ATTRIBUTE)) block.remove();
  });
  const walker = clone.ownerDocument.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    node.textContent = "";
    node = walker.nextNode();
  }
  return clone.innerHTML;
}

export function collectDocumentParagraphEdits(baselineHtml: string, editedHtml: string): DocumentParagraphEdits {
  const baseline = parseAnnotatedHtml(baselineHtml);
  const edited = parseAnnotatedHtml(editedHtml);
  const baselineMapped = new Map<number, HTMLElement>();
  const editedMapped = new Map<number, HTMLElement>();
  const editedExistingBlocks = edited.blocks.filter((block) => !block.hasAttribute(INSERT_AFTER_ATTRIBUTE));

  for (const block of baseline.blocks) {
    const rawIndex = block.getAttribute(SOURCE_ATTRIBUTE);
    if (rawIndex == null) continue;
    baselineMapped.set(Number(rawIndex), block);
  }
  for (const block of edited.blocks) {
    const rawIndex = block.getAttribute(SOURCE_ATTRIBUTE);
    if (rawIndex == null) continue;
    const index = Number(rawIndex);
    if (editedMapped.has(index)) throw new DocumentPreservationError("A source paragraph was duplicated.");
    editedMapped.set(index, block);
  }

  const baselineOrder = baseline.blocks.flatMap((block) => {
    const source = block.getAttribute(SOURCE_ATTRIBUTE);
    return source == null ? [] : [Number(source)];
  });
  const editedOrder = editedExistingBlocks.flatMap((block) => {
    const source = block.getAttribute(SOURCE_ATTRIBUTE);
    return source == null ? [] : [Number(source)];
  });
  let orderCursor = -1;
  if (editedOrder.some((sourceIndex) => {
    const next = baselineOrder.indexOf(sourceIndex, orderCursor + 1);
    if (next < 0) return true;
    orderCursor = next;
    return false;
  })) {
    throw new DocumentPreservationError("Changing this Word document structure is not supported yet.");
  }
  if (documentStructureSignature(baseline.root) !== documentStructureSignature(edited.root)) {
    throw new DocumentPreservationError("Changing this Word document structure requires a document-body update.");
  }

  const replacements = new Map<number, string>();
  const formattedReplacements = new Map<number, string>();
  const formattedContentReplacements = new Map<number, string>();
  const deletions = new Set<number>();
  for (const [index, baselineBlock] of baselineMapped) {
    const editedBlock = editedMapped.get(index);
    if (!editedBlock) {
      deletions.add(index);
      continue;
    }
    if (baselineBlock.innerHTML !== editedBlock.innerHTML) {
      const baselineText = editableBlockText(baselineBlock);
      const editedText = editableBlockText(editedBlock);
      if (
        baselineText !== editedText
        && baselineBlock.getAttribute(SOURCE_EDITABLE_ATTRIBUTE) !== "false"
        && baselineBlock.tagName === editedBlock.tagName
        && markupSignature(baselineBlock) === markupSignature(editedBlock)
      ) {
        replacements.set(index, editedText);
      } else {
        const list = editedBlock.closest("ol,ul");
        const prefix = list?.tagName.toLowerCase() === "ol"
          ? `${Array.from(list.children).indexOf(editedBlock) + 1}. `
          : list ? "• " : "";
        const replacement = documentParagraphXml(editedBlock, prefix);
        if (baselineBlock.tagName === editedBlock.tagName
          && blockShellSignature(baselineBlock) === blockShellSignature(editedBlock)) {
          formattedContentReplacements.set(index, replacement);
        } else formattedReplacements.set(index, replacement);
      }
    }
  }

  for (let index = 0; index < baseline.blocks.length; index += 1) {
    const baselineBlock = baseline.blocks[index];
    if (baselineBlock.hasAttribute(SOURCE_ATTRIBUTE)) continue;
    if (baselineBlock.outerHTML !== editedExistingBlocks[index]?.outerHTML) {
      throw new DocumentPreservationError("This Word object cannot be edited safely yet.");
    }
  }

  const insertions = new Map<number, string[]>();
  const formattedInsertions = new Map<number, string[]>();
  let lastSourceIndex: number | null = null;
  for (const block of edited.blocks) {
    const rawSourceIndex = block.getAttribute(SOURCE_ATTRIBUTE);
    if (rawSourceIndex != null) {
      lastSourceIndex = Number(rawSourceIndex);
      continue;
    }
    const rawInsertAfter = block.getAttribute(INSERT_AFTER_ATTRIBUTE);
    if (rawInsertAfter == null) continue;
    const insertAfter = Number(rawInsertAfter);
    if (!baselineMapped.has(insertAfter) || insertAfter !== lastSourceIndex) {
      throw new DocumentPreservationError("A new paragraph was moved away from its source paragraph.");
    }
    const values = formattedInsertions.get(insertAfter) || [];
    const list = block.closest("ol,ul");
    const prefix = list?.tagName.toLowerCase() === "ol"
      ? `${Array.from(list.children).indexOf(block) + 1}. `
      : list ? "• " : "";
    values.push(documentParagraphXml(block, prefix));
    formattedInsertions.set(insertAfter, values);
  }

  return {
    replacements,
    insertions,
    formattedReplacements,
    formattedContentReplacements,
    formattedInsertions,
    deletions,
  };
}

function setPreservedTextTag(tag: string, value: string): string {
  let openTag = tag.match(/^<w:t(?:\s[^>]*)?>/)?.[0] || "<w:t>";
  if (/^\s|\s$/.test(value) && !/xml:space=/i.test(openTag)) {
    openTag = openTag.replace(/>$/, ' xml:space="preserve">');
  }
  return `${openTag}${escapeXmlText(value)}</w:t>`;
}

function wordTextXml(templateTag: string, value: string): string {
  return value.split(/([\n\t])/).map((token) => {
    if (token === "\n") return "<w:br/>";
    if (token === "\t") return "<w:tab/>";
    return setPreservedTextTag(templateTag, token);
  }).join("");
}

interface WordTextToken {
  end: number;
  raw: string;
  start: number;
  text: string;
  textTag: boolean;
}

function wordTextTokens(paragraphXml: string): WordTextToken[] {
  const pattern = /<w:t(?:\s[^>]*)?>[\s\S]*?<\/w:t>|<w:tab\b[^>]*\/>|<w:br\b[^>]*\/>|<w:cr\b[^>]*\/>/g;
  let textOffset = 0;
  return Array.from(paragraphXml.matchAll(pattern), (match) => {
    const raw = match[0];
    const textTag = /^<w:t(?:\s|>)/.test(raw);
    const text = textTag
      ? decodeXmlText(raw.match(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>/)?.[1] || "")
      : /^<w:tab\b/.test(raw) ? "\t" : "\n";
    const token = { raw, text, textTag, start: textOffset, end: textOffset + text.length };
    textOffset = token.end;
    return token;
  });
}

function patchParagraphText(paragraphXml: string, value: string): string {
  const tokens = wordTextTokens(paragraphXml);
  const original = tokens.map((token) => token.text).join("");
  if (original === value) return paragraphXml;
  if (!tokens.length && value) {
    const textRun = `<w:r>${wordTextXml("<w:t>", value)}</w:r>`;
    return /\/>$/.test(paragraphXml)
      ? paragraphXml.replace(/<w:p([^>]*)\/>$/, `<w:p$1>${textRun}</w:p>`)
      : paragraphXml.replace(/<\/w:p>$/, `${textRun}</w:p>`);
  }
  if (!tokens.length) return paragraphXml;

  let prefix = 0;
  while (prefix < original.length && prefix < value.length && original[prefix] === value[prefix]) prefix += 1;
  let suffix = 0;
  const maximumSuffix = Math.min(original.length - prefix, value.length - prefix);
  while (suffix < maximumSuffix
    && original[original.length - suffix - 1] === value[value.length - suffix - 1]) suffix += 1;
  const originalEnd = original.length - suffix;
  const inserted = value.slice(prefix, value.length - suffix);
  const insertionOnly = prefix === originalEnd;
  const anchor = insertionOnly
    ? Math.max(0, tokens.findIndex((token) => prefix <= token.end))
    : Math.max(0, tokens.findIndex((token) => token.end > prefix && token.start < originalEnd));
  let tokenIndex = 0;
  return paragraphXml.replace(
    /<w:t(?:\s[^>]*)?>[\s\S]*?<\/w:t>|<w:tab\b[^>]*\/>|<w:br\b[^>]*\/>|<w:cr\b[^>]*\/>/g,
    (raw) => {
      const index = tokenIndex++;
      const token = tokens[index];
      if (insertionOnly) {
        if (index !== anchor) return raw;
        const localOffset = Math.max(0, Math.min(token.text.length, prefix - token.start));
        if (token.textTag) {
          return wordTextXml(raw, `${token.text.slice(0, localOffset)}${inserted}${token.text.slice(localOffset)}`);
        }
        const insertedXml = wordTextXml("<w:t>", inserted);
        return localOffset === 0 ? `${insertedXml}${raw}` : `${raw}${insertedXml}`;
      }
      if (token.end <= prefix || token.start >= originalEnd) return raw;
      const before = token.start < prefix ? token.text.slice(0, prefix - token.start) : "";
      const after = token.end > originalEnd ? token.text.slice(originalEnd - token.start) : "";
      const nextText = `${before}${index === anchor ? inserted : ""}${after}`;
      return token.textTag ? wordTextXml(raw, nextText) : wordTextXml("<w:t>", nextText);
    },
  );
}

function insertedParagraphXml(sourceParagraphXml: string, value: string): string {
  const paragraphProperties = (sourceParagraphXml.match(/<w:pPr(?:\s[^>]*)?\/>|<w:pPr(?:\s[^>]*)?>[\s\S]*?<\/w:pPr>/)?.[0] || "")
    .replace(/<w:sectPr(?:\s[^>]*)?\/>|<w:sectPr(?:\s[^>]*)?>[\s\S]*?<\/w:sectPr>/g, "");
  const runProperties = sourceParagraphXml.match(/<w:rPr(?:\s[^>]*)?\/>|<w:rPr(?:\s[^>]*)?>[\s\S]*?<\/w:rPr>/)?.[0] || "";
  const textTemplate = sourceParagraphXml.match(/<w:t(?:\s[^>]*)?>[\s\S]*?<\/w:t>/)?.[0] || "<w:t></w:t>";
  return `<w:p>${paragraphProperties}<w:r>${runProperties}${wordTextXml(textTemplate, value)}</w:r></w:p>`;
}

function patchParagraphContent(sourceParagraphXml: string, replacementParagraphXml: string): string {
  const sourceOpening = sourceParagraphXml.match(/^<w:p(?:\s[^>]*)?>/)?.[0] || "<w:p>";
  const sourceProperties = sourceParagraphXml.match(/<w:pPr(?:\s[^>]*)?\/>|<w:pPr(?:\s[^>]*)?>[\s\S]*?<\/w:pPr>/)?.[0] || "";
  const replacementOpening = replacementParagraphXml.match(/^<w:p(?:\s[^>]*)?>/)?.[0] || "<w:p>";
  const replacementContent = replacementParagraphXml
    .slice(replacementOpening.length, replacementParagraphXml.endsWith("</w:p>") ? -6 : undefined)
    .replace(/<w:pPr(?:\s[^>]*)?\/>|<w:pPr(?:\s[^>]*)?>[\s\S]*?<\/w:pPr>/, "");
  return `${sourceOpening}${sourceProperties}${replacementContent}</w:p>`;
}

const PARAGRAPH_PROPERTY_ORDER = [
  "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
  "suppressLineNumbers", "pBdr", "shd", "tabs", "spacing", "ind", "contextualSpacing",
  "mirrorIndents", "suppressOverlap", "jc", "textDirection", "textAlignment", "outlineLvl",
  "rPr", "sectPr", "pPrChange",
];

function paragraphPropertyPattern(name: string): RegExp {
  return new RegExp(`<w:${name}(?:\\s[^>]*)?\\/>|<w:${name}(?:\\s[^>]*)?>[\\s\\S]*?<\\/w:${name}>`);
}

function mergeFormattedParagraphProperties(sourceXml: string, replacementXml: string): string {
  const replacementProperties = replacementXml.match(/<w:pPr(?:\s[^>]*)?\/>|<w:pPr(?:\s[^>]*)?>[\s\S]*?<\/w:pPr>/)?.[0] || "";
  let merged = sourceXml.match(/<w:pPr(?:\s[^>]*)?\/>|<w:pPr(?:\s[^>]*)?>[\s\S]*?<\/w:pPr>/)?.[0] || "<w:pPr></w:pPr>";
  if (/\/>$/.test(merged)) merged = merged.replace(/\/>$/, "></w:pPr>");
  for (const name of ["pBdr", "shd", "spacing", "ind", "jc"]) {
    const pattern = paragraphPropertyPattern(name);
    const replacement = replacementProperties.match(pattern)?.[0];
    const preserveWhenOmitted = name === "pBdr" || name === "shd";
    if (pattern.test(merged) && (replacement || !preserveWhenOmitted)) merged = merged.replace(pattern, replacement || "");
    else if (replacement) {
      const order = PARAGRAPH_PROPERTY_ORDER.indexOf(name);
      const nextProperty = PARAGRAPH_PROPERTY_ORDER.slice(order + 1)
        .map((candidate) => paragraphPropertyPattern(candidate).exec(merged))
        .find((match) => match?.index != null);
      merged = nextProperty?.index != null
        ? `${merged.slice(0, nextProperty.index)}${replacement}${merged.slice(nextProperty.index)}`
        : merged.replace(/<\/w:pPr>$/, `${replacement}</w:pPr>`);
    }
  }
  return merged === "<w:pPr></w:pPr>" ? "" : merged;
}

function patchParagraphFormatting(sourceParagraphXml: string, replacementParagraphXml: string): string {
  const sourceOpening = sourceParagraphXml.match(/^<w:p(?:\s[^>]*)?>/)?.[0] || "<w:p>";
  const replacementOpening = replacementParagraphXml.match(/^<w:p(?:\s[^>]*)?>/)?.[0] || "<w:p>";
  const replacementContent = replacementParagraphXml
    .slice(replacementOpening.length, replacementParagraphXml.endsWith("</w:p>") ? -6 : undefined)
    .replace(/<w:pPr(?:\s[^>]*)?\/>|<w:pPr(?:\s[^>]*)?>[\s\S]*?<\/w:pPr>/, "");
  return `${sourceOpening}${mergeFormattedParagraphProperties(sourceParagraphXml, replacementParagraphXml)}${replacementContent}</w:p>`;
}

function normalizedDocumentEdits(edits: Map<number, string> | DocumentParagraphEdits): DocumentParagraphEdits {
  return edits instanceof Map
    ? { replacements: edits, insertions: new Map(), formattedReplacements: new Map(), formattedContentReplacements: new Map(), formattedInsertions: new Map(), deletions: new Set() }
    : edits;
}

export async function preserveDocumentFile(
  original: ArrayBuffer,
  edits: Map<number, string> | DocumentParagraphEdits,
  fileName: string,
): Promise<File> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(original);
  const documentEntry = zip.file("word/document.xml");
  if (!documentEntry) throw new DocumentPreservationError("The DOCX package has no word/document.xml part.");
  const documentXml = await documentEntry.async("text");
  const normalizedEdits = normalizedDocumentEdits(edits);
  let paragraphIndex = 0;
  const patchedXml = documentXml.replace(WORD_PARAGRAPH_PATTERN, (paragraph) => {
    const sourceIndex = paragraphIndex++;
    if (normalizedEdits.deletions?.has(sourceIndex)) {
      const insertedParagraphs = normalizedEdits.insertions.get(sourceIndex) || [];
      const formattedInsertions = normalizedEdits.formattedInsertions?.get(sourceIndex) || [];
      return insertedParagraphs.map((value) => insertedParagraphXml(paragraph, value)).join("") + formattedInsertions.join("");
    }
    const replacement = normalizedEdits.replacements.get(sourceIndex);
    const formattedReplacement = normalizedEdits.formattedReplacements?.get(sourceIndex);
    const formattedContentReplacement = normalizedEdits.formattedContentReplacements?.get(sourceIndex);
    const patchedParagraph = formattedReplacement
      ? patchParagraphFormatting(paragraph, formattedReplacement)
      : (formattedContentReplacement
        ? patchParagraphContent(paragraph, formattedContentReplacement)
        : replacement == null ? paragraph : patchParagraphText(paragraph, replacement));
    const insertedParagraphs = normalizedEdits.insertions.get(sourceIndex) || [];
    const formattedInsertions = normalizedEdits.formattedInsertions?.get(sourceIndex) || [];
    return patchedParagraph + insertedParagraphs.map((value) => insertedParagraphXml(paragraph, value)).join("") + formattedInsertions.join("");
  });
  for (const index of [
    ...normalizedEdits.replacements.keys(),
    ...normalizedEdits.insertions.keys(),
    ...(normalizedEdits.formattedReplacements?.keys() || []),
    ...(normalizedEdits.formattedContentReplacements?.keys() || []),
    ...(normalizedEdits.formattedInsertions?.keys() || []),
    ...(normalizedEdits.deletions || []),
  ]) {
    if (index < 0 || index >= paragraphIndex) throw new DocumentPreservationError(`Unable to find source paragraph ${index}.`);
  }
  const validPatchedXml = patchedXml.replace(/<w:tc(\s[^>]*)?>([\s\S]*?)<\/w:tc>/g, (cell, attributes = "", content = "") => (
    /<w:p(?:\s|\/|>)/.test(content) ? cell : `<w:tc${attributes}>${content}<w:p/></w:tc>`
  ));
  zip.file("word/document.xml", validPatchedXml);
  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE", mimeType: DOCX_MIME });
  const safeName = fileName.toLowerCase().endsWith(".docx") ? fileName : `${fileName.replace(/\.doc$/i, "")}.docx`;
  return new File([blob], safeName, { type: DOCX_MIME, lastModified: Date.now() });
}

interface DocumentRunStyle {
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strike?: boolean;
  color?: string;
  backgroundColor?: string;
  fontFamily?: string;
  halfPoints?: number;
}

interface DocumentImageRelationship {
  id: string;
  name: string;
  widthPx: number;
  heightPx: number;
}

interface DocumentSerializationContext {
  hyperlinks: Map<string, string>;
  images: Map<string, DocumentImageRelationship>;
  drawingId: number;
}

const EMPTY_DOCUMENT_CONTEXT: DocumentSerializationContext = {
  hyperlinks: new Map(),
  images: new Map(),
  drawingId: 1,
};

function normalizedHexColor(value: string): string | undefined {
  const trimmed = value.trim();
  const short = trimmed.match(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/i);
  if (short) return `${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`.toUpperCase();
  const full = trimmed.match(/^#([0-9a-f]{6})$/i);
  if (full) return full[1].toUpperCase();
  const rgb = trimmed.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (!rgb) return undefined;
  return rgb.slice(1, 4).map((part) => Math.max(0, Math.min(255, Number(part))).toString(16).padStart(2, "0")).join("").toUpperCase();
}

function cssHalfPoints(value: string): number | undefined {
  const match = value.trim().match(/^([\d.]+)(px|pt)?$/i);
  if (!match) return undefined;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount <= 0) return undefined;
  return Math.max(2, Math.round(amount * (match[2]?.toLowerCase() === "pt" ? 2 : 1.5)));
}

function mergedRunStyle(parent: DocumentRunStyle, element: HTMLElement): DocumentRunStyle {
  const tag = element.tagName.toLowerCase();
  const decoration = element.style.textDecoration.toLowerCase();
  const fontColor = tag === "font" ? normalizedHexColor(element.getAttribute("color") || "") : undefined;
  const fontFace = tag === "font" ? element.getAttribute("face")?.replace(/["']/g, "").split(",")[0]?.trim() : undefined;
  const legacyFontSize = tag === "font"
    ? ({ "1": 16, "2": 20, "3": 24, "4": 28, "5": 36, "6": 48, "7": 72 } as Record<string, number>)[element.getAttribute("size") || ""]
    : undefined;
  return {
    ...parent,
    bold: parent.bold || tag === "b" || tag === "strong" || Number(element.style.fontWeight) >= 600,
    italic: parent.italic || tag === "i" || tag === "em" || element.style.fontStyle === "italic",
    underline: parent.underline || tag === "u" || decoration.includes("underline"),
    strike: parent.strike || tag === "s" || tag === "strike" || tag === "del" || decoration.includes("line-through"),
    color: normalizedHexColor(element.style.color) || fontColor || parent.color,
    backgroundColor: normalizedHexColor(element.style.backgroundColor) || (tag === "mark" ? "FFFF00" : undefined) || parent.backgroundColor,
    fontFamily: element.style.fontFamily.replace(/["']/g, "").split(",")[0]?.trim() || fontFace || parent.fontFamily,
    halfPoints: cssHalfPoints(element.style.fontSize) || legacyFontSize || parent.halfPoints,
  };
}

function documentRunXml(text: string, style: DocumentRunStyle = {}): string {
  if (!text) return "";
  const properties = [
    style.bold ? "<w:b/>" : "",
    style.italic ? "<w:i/>" : "",
    style.underline ? '<w:u w:val="single"/>' : "",
    style.strike ? "<w:strike/>" : "",
    style.color ? `<w:color w:val="${style.color}"/>` : "",
    style.backgroundColor ? `<w:shd w:val="clear" w:color="auto" w:fill="${style.backgroundColor}"/>` : "",
    style.fontFamily ? `<w:rFonts w:ascii="${escapeXmlAttribute(style.fontFamily)}" w:hAnsi="${escapeXmlAttribute(style.fontFamily)}"/>` : "",
    style.halfPoints ? `<w:sz w:val="${style.halfPoints}"/><w:szCs w:val="${style.halfPoints}"/>` : "",
  ].join("");
  const runProperties = properties ? `<w:rPr>${properties}</w:rPr>` : "";
  return text.split(/([\n\t])/).map((part) => {
    if (part === "\n") return `<w:r>${runProperties}<w:br/></w:r>`;
    if (part === "\t") return `<w:r>${runProperties}<w:tab/></w:r>`;
    if (!part) return "";
    const space = /^\s|\s$/.test(part) ? ' xml:space="preserve"' : "";
    return `<w:r>${runProperties}<w:t${space}>${escapeXmlText(part)}</w:t></w:r>`;
  }).join("");
}

function documentDrawingXml(image: DocumentImageRelationship, context: DocumentSerializationContext): string {
  const width = Math.max(1, Math.round(image.widthPx * 9525));
  const height = Math.max(1, Math.round(image.heightPx * 9525));
  const drawingId = context.drawingId++;
  const name = escapeXmlAttribute(image.name);
  return `<w:r><w:drawing xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="${width}" cy="${height}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:docPr id="${drawingId}" name="${name}"/><wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="${drawingId}" name="${name}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="${image.id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="${width}" cy="${height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>`;
}

function documentInlineXml(
  node: Node,
  style: DocumentRunStyle = {},
  context: DocumentSerializationContext = EMPTY_DOCUMENT_CONTEXT,
): string {
  if (node.nodeType === Node.TEXT_NODE) return documentRunXml(node.textContent || "", style);
  if (!(node instanceof HTMLElement)) return "";
  const tag = node.tagName.toLowerCase();
  if (tag === "br") return "<w:r><w:br/></w:r>";
  if (tag === "img") {
    const source = node.getAttribute("src") || "";
    const relationship = context.images.get(source);
    if (relationship) return documentDrawingXml(relationship, context);
    const label = node.getAttribute("alt") || "Image";
    return documentRunXml(`[${label}]`, { ...style, italic: true });
  }
  if (tag === "a") {
    const href = node.getAttribute("href") || "";
    const relationshipId = context.hyperlinks.get(href);
    const content = Array.from(node.childNodes).map((child) => documentInlineXml(child, {
      ...style,
      color: style.color || "0563C1",
      underline: true,
    }, context)).join("");
    return relationshipId ? `<w:hyperlink xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="${relationshipId}">${content}</w:hyperlink>` : content;
  }
  const nextStyle = mergedRunStyle(style, node);
  return Array.from(node.childNodes).map((child) => documentInlineXml(child, nextStyle, context)).join("");
}

function cssPixels(value: string): number | undefined {
  const match = value.trim().match(/^([\d.]+)(px|pt)?$/i);
  if (!match) return undefined;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount < 0) return undefined;
  return amount * (match[2]?.toLowerCase() === "pt" ? 4 / 3 : 1);
}

function documentParagraphXml(
  element: HTMLElement,
  prefix = "",
  context: DocumentSerializationContext = EMPTY_DOCUMENT_CONTEXT,
): string {
  const tag = element.tagName.toLowerCase();
  const headingLevel = /^h([1-6])$/.exec(tag)?.[1];
  const defaultHeadingSizes: Record<string, number> = { "1": 36, "2": 30, "3": 26, "4": 24, "5": 22, "6": 20 };
  const baseStyle: DocumentRunStyle = headingLevel
    ? { bold: true, halfPoints: defaultHeadingSizes[headingLevel] }
    : { bold: tag === "th" };
  const alignment = ["left", "center", "right", "justify"].includes(element.style.textAlign)
    ? `<w:jc w:val="${element.style.textAlign}"/>`
    : "";
  const marginBefore = cssPixels(element.style.marginTop);
  const marginAfter = cssPixels(element.style.marginBottom);
  const lineHeight = Number.parseFloat(element.style.lineHeight);
  const before = marginBefore == null ? (headingLevel ? 240 : 0) : Math.round(marginBefore * 15);
  const after = marginAfter == null ? (headingLevel ? 120 : 160) : Math.round(marginAfter * 15);
  const line = Number.isFinite(lineHeight) && lineHeight > 0 ? ` w:line="${Math.round(lineHeight * 240)}" w:lineRule="auto"` : "";
  const spacing = `<w:spacing w:before="${before}" w:after="${after}"${line}/>`;
  const indentPixels = cssPixels(element.style.marginLeft) || cssPixels(element.style.paddingLeft) || (tag === "blockquote" ? 36 : 0);
  const indent = indentPixels ? `<w:ind w:left="${Math.round(indentPixels * 15)}"/>` : "";
  const callout = element.classList.contains("doc-editor-callout") || tag === "blockquote"
    ? '<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="5F8F88"/></w:pBdr><w:shd w:val="clear" w:color="auto" w:fill="F0FDFA"/>'
    : "";
  const pPr = `<w:pPr>${spacing}${alignment}${indent}${callout}</w:pPr>`;
  const runs = (prefix ? documentRunXml(prefix, baseStyle) : "")
    + Array.from(element.childNodes).map((child) => documentInlineXml(child, baseStyle, context)).join("");
  return `<w:p>${pPr}${runs || "<w:r><w:t></w:t></w:r>"}</w:p>`;
}

function genericDocumentTableXml(table: HTMLTableElement, context: DocumentSerializationContext): string {
  const rows = Array.from(table.rows).map((row) => {
    const cells = Array.from(row.cells).map((cell) => {
      const blocks = Array.from(cell.children).filter((child): child is HTMLElement => child instanceof HTMLElement);
      const content = blocks.some((block) => /^(p|h[1-6]|div|blockquote)$/i.test(block.tagName))
        ? blocks.filter((block) => /^(p|h[1-6]|div|blockquote)$/i.test(block.tagName)).map((block) => documentParagraphXml(block, "", context)).join("")
        : documentParagraphXml(cell, "", context);
      const span = cell.colSpan > 1 ? `<w:gridSpan w:val="${cell.colSpan}"/>` : "";
      const fill = normalizedHexColor(cell.style.backgroundColor);
      const shading = fill ? `<w:shd w:val="clear" w:color="auto" w:fill="${fill}"/>` : "";
      return `<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/>${span}${shading}</w:tcPr>${content}</w:tc>`;
    }).join("");
    return `<w:tr>${cells}</w:tr>`;
  }).join("");
  return `<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders><w:top w:val="single" w:sz="4" w:color="D6D3D1"/><w:left w:val="single" w:sz="4" w:color="D6D3D1"/><w:bottom w:val="single" w:sz="4" w:color="D6D3D1"/><w:right w:val="single" w:sz="4" w:color="D6D3D1"/><w:insideH w:val="single" w:sz="4" w:color="D6D3D1"/><w:insideV w:val="single" w:sz="4" w:color="D6D3D1"/></w:tblBorders></w:tblPr>${rows}</w:tbl>`;
}

interface DirectWordXmlParts {
  opening: string;
  closing: string;
  children: string[];
}

function directWordElementName(xml: string): string | undefined {
  return xml.match(/^<(?![!?/])(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)\b/)?.[1];
}

function splitDirectWordChildren(elementXml: string): DirectWordXmlParts {
  const openingEnd = xmlTagEnd(elementXml, 0);
  if (openingEnd < 0) return { opening: elementXml, closing: "", children: [] };
  const opening = elementXml.slice(0, openingEnd + 1);
  if (/\/\s*>$/.test(opening)) return { opening, closing: "", children: [] };
  const closingMatch = elementXml.match(/<\/(?:[A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*>\s*$/);
  if (closingMatch?.index == null) return { opening, closing: "", children: [] };
  const closing = closingMatch[0];
  const inner = elementXml.slice(opening.length, closingMatch.index);
  const children: string[] = [];
  let cursor = 0;
  while (cursor < inner.length) {
    while (cursor < inner.length && /\s/.test(inner[cursor])) cursor += 1;
    if (cursor >= inner.length) break;
    if (inner.startsWith("<!--", cursor)) {
      const end = inner.indexOf("-->", cursor + 4);
      if (end < 0) break;
      children.push(inner.slice(cursor, end + 3));
      cursor = end + 3;
      continue;
    }
    if (inner[cursor] !== "<") {
      const next = inner.indexOf("<", cursor);
      cursor = next < 0 ? inner.length : next;
      continue;
    }
    const start = cursor;
    let depth = 0;
    while (cursor < inner.length) {
      if (inner.startsWith("<!--", cursor)) {
        const end = inner.indexOf("-->", cursor + 4);
        if (end < 0) return { opening, closing, children };
        cursor = end + 3;
        continue;
      }
      if (inner.startsWith("<![CDATA[", cursor)) {
        const end = inner.indexOf("]]>", cursor + 9);
        if (end < 0) return { opening, closing, children };
        cursor = end + 3;
        continue;
      }
      const end = xmlTagEnd(inner, cursor);
      if (end < 0) return { opening, closing, children };
      const tag = inner.slice(cursor, end + 1);
      if (/^<\//.test(tag)) depth -= 1;
      else if (!/^<\?|^<!/.test(tag) && !/\/\s*>$/.test(tag)) depth += 1;
      cursor = end + 1;
      if (depth <= 0) break;
      const next = inner.indexOf("<", cursor);
      if (next < 0) return { opening, closing, children };
      cursor = next;
    }
    children.push(inner.slice(start, cursor));
  }
  return { opening, closing, children };
}

interface NestedWordElementSource {
  xml: string;
  wrap(replacement: string): string;
}

function nestedWordElementSource(elementXml: string, name: string): NestedWordElementSource | undefined {
  if (directWordElementName(elementXml) === name) {
    return { xml: elementXml, wrap: (replacement) => replacement };
  }
  const parts = splitDirectWordChildren(elementXml);
  for (let index = 0; index < parts.children.length; index += 1) {
    const nested = nestedWordElementSource(parts.children[index], name);
    if (!nested) continue;
    return {
      xml: nested.xml,
      wrap: (replacement) => {
        const children = [...parts.children];
        children[index] = nested.wrap(replacement);
        return `${parts.opening}${children.join("")}${parts.closing}`;
      },
    };
  }
  return undefined;
}

function directWordChild(elementXml: string, name: string): string | undefined {
  return splitDirectWordChildren(elementXml).children.find((child) => directWordElementName(child) === name);
}

function wordXmlAttribute(elementXml: string | undefined, name: string): string | undefined {
  return elementXml?.match(new RegExp(`\\b(?:[A-Za-z_][\\w.-]*:)?${name}="([^"]*)"`, "i"))?.[1];
}

function setWordProperty(
  propertiesXml: string,
  containerName: string,
  propertyName: string,
  replacement: string | undefined,
  laterProperties: string[],
): string {
  let output = propertiesXml || `<w:${containerName}/>`;
  const propertyPattern = new RegExp(
    `<w:${propertyName}(?:\\s[^>]*)?\\/>|<w:${propertyName}(?:\\s[^>]*)?>[\\s\\S]*?<\\/w:${propertyName}>`,
    "i",
  );
  if (propertyPattern.test(output)) return output.replace(propertyPattern, replacement || "");
  if (!replacement) return output;
  if (/\/\s*>$/.test(output)) {
    return output.replace(/\/\s*>$/, `>${replacement}</w:${containerName}>`);
  }
  for (const later of laterProperties) {
    const match = new RegExp(`<w:${later}(?:\\s|\/|>)`, "i").exec(output);
    if (match?.index != null) return `${output.slice(0, match.index)}${replacement}${output.slice(match.index)}`;
  }
  return output.replace(new RegExp(`<\\/w:${containerName}>$`, "i"), `${replacement}</w:${containerName}>`);
}

interface SourceDocumentTableCell {
  xml: string;
  row: number;
  column: number;
  colspan: number;
  vMerge?: "restart" | "continue";
  paragraphIndexes: number[];
}

interface SourceDocumentTableRow {
  xml: string;
  cells: SourceDocumentTableCell[];
}

interface SourceDocumentTable {
  parts: DirectWordXmlParts;
  rows: SourceDocumentTableRow[];
  cellByParagraph: Map<number, SourceDocumentTableCell>;
  paragraphXml: Map<number, string>;
  gridXml?: string;
}

function sourceDocumentTable(sourceXml: string, paragraphIndexes: number[]): SourceDocumentTable {
  const parts = splitDirectWordChildren(sourceXml);
  const rows: SourceDocumentTableRow[] = [];
  const cellByParagraph = new Map<number, SourceDocumentTableCell>();
  const paragraphXml = new Map<number, string>();
  let paragraphCursor = paragraphIndexes[0] || 0;
  for (const rowXml of parts.children.filter((child) => directWordElementName(child) === "tr")) {
    const rowParts = splitDirectWordChildren(rowXml);
    const cells: SourceDocumentTableCell[] = [];
    let column = 0;
    for (const cellXml of rowParts.children.filter((child) => directWordElementName(child) === "tc")) {
      const properties = directWordChild(cellXml, "tcPr");
      const colspan = Math.max(1, Number(wordXmlAttribute(directWordChild(properties || "", "gridSpan"), "val") || 1));
      const vMergeXml = directWordChild(properties || "", "vMerge");
      const vMergeValue = wordXmlAttribute(vMergeXml, "val");
      const paragraphs = cellXml.match(WORD_PARAGRAPH_PATTERN) || [];
      const indexes = paragraphs.map((paragraph) => {
        const index = paragraphCursor++;
        paragraphXml.set(index, paragraph);
        return index;
      });
      const cell: SourceDocumentTableCell = {
        xml: cellXml,
        row: rows.length,
        column,
        colspan,
        vMerge: vMergeXml ? (vMergeValue === "restart" ? "restart" : "continue") : undefined,
        paragraphIndexes: indexes,
      };
      indexes.forEach((index) => cellByParagraph.set(index, cell));
      cells.push(cell);
      column += colspan;
    }
    rows.push({
      xml: rowXml,
      cells,
    });
  }
  return {
    parts,
    rows,
    cellByParagraph,
    paragraphXml,
    gridXml: parts.children.find((child) => directWordElementName(child) === "tblGrid"),
  };
}

function tableElementSourceIndexes(element: Element): number[] {
  const blocks = [
    ...(element instanceof HTMLElement && element.hasAttribute(SOURCE_ATTRIBUTE) ? [element] : []),
    ...Array.from(element.querySelectorAll<HTMLElement>(`[${SOURCE_ATTRIBUTE}]`)),
  ];
  return blocks.flatMap((block) => {
    const index = Number(block.getAttribute(SOURCE_ATTRIBUTE));
    return Number.isInteger(index) && index >= 0 ? [index] : [];
  });
}

function tableCellBlocks(cell: HTMLTableCellElement): HTMLElement[] {
  const blocks = Array.from(cell.children).filter((child): child is HTMLElement => (
    child instanceof HTMLElement && /^(p|h[1-6]|div|blockquote|table|del)$/i.test(child.tagName)
  ));
  return blocks.length ? blocks : [cell];
}

interface SourceDocumentTableCellChild {
  key: string;
  xml: string;
  name?: string;
  paragraphIndexes: number[];
}

function sourceDocumentTableCellChildren(cell: SourceDocumentTableCell): SourceDocumentTableCellChild[] {
  let paragraphOffset = 0;
  return splitDirectWordChildren(cell.xml).children.map((xml, index) => {
    const paragraphCount = (xml.match(WORD_PARAGRAPH_PATTERN) || []).length;
    const paragraphIndexes = cell.paragraphIndexes.slice(paragraphOffset, paragraphOffset + paragraphCount);
    paragraphOffset += paragraphCount;
    return {
      key: `${cell.row}:${cell.column}:${index}`,
      xml,
      name: directWordElementName(xml),
      paragraphIndexes,
    };
  });
}

function sourceDocumentTableCellChild(
  element: Element,
  source: SourceDocumentTable,
): SourceDocumentTableCellChild | undefined {
  for (const sourceIndex of tableElementSourceIndexes(element)) {
    const sourceCell = source.cellByParagraph.get(sourceIndex);
    const sourceChild = sourceCell && sourceDocumentTableCellChildren(sourceCell)
      .find((child) => child.paragraphIndexes.includes(sourceIndex));
    if (sourceChild) return sourceChild;
  }
  return undefined;
}

function tableCellContentXml(
  cell: HTMLTableCellElement,
  source: SourceDocumentTable,
  context: DocumentSerializationContext,
  baselineCell?: HTMLTableCellElement,
): string {
  const emittedSourceChildren = new Set<string>();
  const baselineTables = Array.from(baselineCell?.children || []).filter((child): child is HTMLTableElement => (
    child instanceof HTMLTableElement
  ));
  return tableCellBlocks(cell).flatMap((block) => {
    const altChunkId = block.getAttribute(ALT_CHUNK_ATTRIBUTE);
    if (altChunkId != null) {
      const altChunk = source.rows.flatMap((row) => row.cells).flatMap(sourceDocumentTableCellChildren)
        .find((child) => child.name === "altChunk" && wordXmlAttribute(child.xml, "id") === altChunkId);
      return altChunk ? [altChunk.xml] : [];
    }
    const sourceChild = sourceDocumentTableCellChild(block, source);
    if (sourceChild && emittedSourceChildren.has(sourceChild.key)) return [];
    if (block instanceof HTMLTableElement) {
      const tableSource = sourceChild && nestedWordElementSource(sourceChild.xml, "tbl");
      const baselineTable = sourceChild && baselineTables.find((table) => (
        tableElementSourceIndexes(table).some((index) => sourceChild.paragraphIndexes.includes(index))
      ));
      if (sourceChild) emittedSourceChildren.add(sourceChild.key);
      const tableXml = documentTableXml(
        block,
        context,
        tableSource?.xml,
        sourceChild?.paragraphIndexes,
        baselineTable,
      );
      return [tableSource ? tableSource.wrap(tableXml) : tableXml];
    }
    if (sourceChild && sourceChild.name !== "p") {
      emittedSourceChildren.add(sourceChild.key);
      return [sourceChild.xml];
    }
    const sourceValue = block.getAttribute(SOURCE_ATTRIBUTE);
    const sourceIndex = sourceValue == null ? undefined : Number(sourceValue);
    const sourceParagraph = sourceIndex != null && Number.isInteger(sourceIndex)
      ? source.paragraphXml.get(sourceIndex)
      : undefined;
    return [sourceParagraph || documentParagraphXml(block, "", context)];
  }).join("") || "<w:p/>";
}

function sourceCellForElement(cell: HTMLTableCellElement, source: SourceDocumentTable): SourceDocumentTableCell | undefined {
  return tableElementSourceIndexes(cell).flatMap((index) => {
    const sourceCell = source.cellByParagraph.get(index);
    return sourceCell ? [sourceCell] : [];
  })[0];
}

function targetTableGridColumns(
  gridXml: string | undefined,
  columnCount: number,
  sourceColumnByTarget: Map<number, number>,
): string[] {
  const safeColumnCount = Math.max(1, columnCount);
  if (!gridXml) return Array.from({ length: safeColumnCount }, () => '<w:gridCol w:w="2400"/>');
  const columns = splitDirectWordChildren(gridXml).children.filter((child) => directWordElementName(child) === "gridCol");
  const fallback = columns.at(-1) || '<w:gridCol w:w="2400"/>';
  return Array.from({ length: safeColumnCount }, (_, index) => {
    const sourceColumn = sourceColumnByTarget.get(index);
    return (sourceColumn == null ? undefined : columns[sourceColumn]) || columns[index] || fallback;
  });
}

function patchTableGrid(gridXml: string | undefined, columns: string[]): string {
  if (!gridXml) return `<w:tblGrid>${columns.join("")}</w:tblGrid>`;
  const parts = splitDirectWordChildren(gridXml);
  const existingColumns = parts.children.filter((child) => directWordElementName(child) === "gridCol");
  if (existingColumns.length === columns.length
    && existingColumns.every((column, index) => column === columns[index])) return gridXml;
  const otherChildren = parts.children.filter((child) => directWordElementName(child) !== "gridCol");
  return `${parts.opening}${columns.join("")}${otherChildren.join("")}${parts.closing}`;
}

interface TargetDocumentTableCell {
  cell?: HTMLTableCellElement;
  column: number;
  colspan: number;
  continuation?: boolean;
  origin?: TargetDocumentTableCell;
}

function targetDocumentTableRows(table: HTMLTableElement): TargetDocumentTableCell[][] {
  let active: Array<{ column: number; colspan: number; remaining: number; origin: TargetDocumentTableCell }> = [];
  return Array.from(table.rows).map((row) => {
    const placements: TargetDocumentTableCell[] = active.map((entry) => ({
      column: entry.column,
      colspan: entry.colspan,
      continuation: true,
      origin: entry.origin,
    }));
    const occupied = new Set(placements.flatMap((entry) => (
      Array.from({ length: entry.colspan }, (_, offset) => entry.column + offset)
    )));
    const nextActive = active.flatMap((entry) => entry.remaining > 1
      ? [{ ...entry, remaining: entry.remaining - 1 }]
      : []);
    let column = 0;
    for (const cell of Array.from(row.cells)) {
      while (occupied.has(column)) column += 1;
      const colspan = Math.max(1, cell.colSpan || 1);
      const placement: TargetDocumentTableCell = { cell, column, colspan };
      placements.push(placement);
      for (let offset = 0; offset < colspan; offset += 1) occupied.add(column + offset);
      if (cell.rowSpan > 1) {
        nextActive.push({ column, colspan, remaining: cell.rowSpan - 1, origin: placement });
      }
      column += colspan;
    }
    active = nextActive;
    return placements.sort((left, right) => left.column - right.column);
  });
}

function documentTableXml(
  table: HTMLTableElement,
  context: DocumentSerializationContext,
  sourceXml?: string,
  sourceParagraphIndexes: number[] = [],
  baselineTable?: HTMLTableElement,
): string {
  if (!sourceXml) return genericDocumentTableXml(table, context);
  const source = sourceDocumentTable(sourceXml, sourceParagraphIndexes);
  const baselineCellBySource = new Map<number, HTMLTableCellElement>();
  const baselineRowBySource = new Map<number, HTMLTableRowElement>();
  if (baselineTable) {
    for (const row of Array.from(baselineTable.rows)) {
      for (const index of tableElementSourceIndexes(row)) baselineRowBySource.set(index, row);
      for (const cell of Array.from(row.cells)) {
        for (const index of tableElementSourceIndexes(cell)) baselineCellBySource.set(index, cell);
      }
    }
  }
  const targetRows = targetDocumentTableRows(table);
  const columnCount = Math.max(1, ...targetRows.flatMap((row) => row.map((cell) => cell.column + cell.colspan)));
  const sourceColumnByTarget = new Map<number, number>();
  for (const placements of targetRows) {
    for (const placement of placements) {
      const mappedSourceCell = placement.cell
        ? sourceCellForElement(placement.cell, source)
        : placement.origin?.cell ? sourceCellForElement(placement.origin.cell, source) : undefined;
      if (!mappedSourceCell) continue;
      for (let offset = 0; offset < Math.min(placement.colspan, mappedSourceCell.colspan); offset += 1) {
        sourceColumnByTarget.set(placement.column + offset, mappedSourceCell.column + offset);
      }
    }
  }
  const targetGridColumns = targetTableGridColumns(source.gridXml, columnCount, sourceColumnByTarget);
  const targetGridWidths = targetGridColumns.map((column) => Number(wordXmlAttribute(column, "w")));
  const rowXml = targetRows.map((placements, rowIndex) => {
    const editedRow = table.rows[rowIndex];
    const rowSourceIndexes = tableElementSourceIndexes(editedRow);
    const sourceRow = rowSourceIndexes.flatMap((index) => {
      const sourceCell = source.cellByParagraph.get(index);
      return sourceCell ? [source.rows[sourceCell.row]] : [];
    })[0] || source.rows[Math.min(rowIndex, Math.max(0, source.rows.length - 1))];
    const baselineRow = rowSourceIndexes.flatMap((index) => {
      const row = baselineRowBySource.get(index);
      return row ? [row] : [];
    })[0];
    if (sourceRow && baselineRow?.outerHTML === editedRow.outerHTML) return sourceRow.xml;
    const sourceRowParts = sourceRow ? splitDirectWordChildren(sourceRow.xml) : undefined;
    let rowProperties = sourceRow ? directWordChild(sourceRow.xml, "trPr") || "" : "";
    if (baselineRow && Array.from(baselineRow.cells).every((cell) => cell.tagName === "TH")
      !== Array.from(editedRow.cells).every((cell) => cell.tagName === "TH")) {
      rowProperties = setWordProperty(
        rowProperties,
        "trPr",
        "tblHeader",
        Array.from(editedRow.cells).every((cell) => cell.tagName === "TH") ? "<w:tblHeader/>" : undefined,
        ["tblCellSpacing", "jc", "hidden", "ins", "del", "trPrChange"],
      );
    }
    const cells = placements.map((placement) => {
      const editedCell = placement.cell;
      const mappedSourceCell = editedCell ? sourceCellForElement(editedCell, source) : undefined;
      const sourceCell = placement.continuation
        ? sourceRow?.cells.find((cell) => cell.column === placement.column && cell.vMerge === "continue")
          || (placement.origin?.cell ? sourceCellForElement(placement.origin.cell, source) : undefined)
        : mappedSourceCell
          || sourceRow?.cells.find((cell) => cell.column === placement.column)
          || sourceRow?.cells.at(-1);
      if (placement.continuation && sourceCell?.vMerge === "continue" && sourceCell.colspan === placement.colspan) {
        return sourceCell.xml;
      }
      if (editedCell && sourceCell) {
        const baselineCell = tableElementSourceIndexes(editedCell).flatMap((index) => {
          const cell = baselineCellBySource.get(index);
          return cell ? [cell] : [];
        })[0];
        if (baselineCell?.outerHTML === editedCell.outerHTML
          && sourceCell.colspan === placement.colspan
          && (editedCell.rowSpan > 1) === (sourceCell.vMerge === "restart")) {
          return sourceCell.xml;
        }
      }
      const cellParts = sourceCell ? splitDirectWordChildren(sourceCell.xml) : undefined;
      let properties = sourceCell ? directWordChild(sourceCell.xml, "tcPr") || "" : "";
      properties = setWordProperty(
        properties,
        "tcPr",
        "gridSpan",
        placement.colspan > 1 ? `<w:gridSpan w:val="${placement.colspan}"/>` : undefined,
        ["hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge", "tcPrChange"],
      );
      const spannedWidths = targetGridWidths.slice(placement.column, placement.column + placement.colspan);
      const spannedWidth = spannedWidths.reduce((sum, width) => sum + width, 0);
      if ((!mappedSourceCell || mappedSourceCell.colspan !== placement.colspan)
        && spannedWidths.length === placement.colspan
        && spannedWidths.every((width) => Number.isFinite(width) && width >= 0)
        && spannedWidth > 0) {
        properties = setWordProperty(
          properties,
          "tcPr",
          "tcW",
          `<w:tcW w:w="${spannedWidth}" w:type="dxa"/>`,
          ["gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge", "tcPrChange"],
        );
      }
      const verticalMerge = placement.continuation ? "<w:vMerge/>" : editedCell && editedCell.rowSpan > 1 ? '<w:vMerge w:val="restart"/>' : undefined;
      properties = setWordProperty(
        properties,
        "tcPr",
        "vMerge",
        verticalMerge,
        ["tcBorders", "shd", "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge", "tcPrChange"],
      );
      if (editedCell) {
        const baselineCell = tableElementSourceIndexes(editedCell).flatMap((index) => {
          const cell = baselineCellBySource.get(index);
          return cell ? [cell] : [];
        })[0];
        const fillChanged = !baselineCell || baselineCell.style.backgroundColor !== editedCell.style.backgroundColor;
        if (fillChanged) {
          const fill = normalizedHexColor(editedCell.style.backgroundColor);
          properties = setWordProperty(
            properties,
            "tcPr",
            "shd",
            fill ? `<w:shd w:val="clear" w:color="auto" w:fill="${fill}"/>` : undefined,
            ["noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge", "tcPrChange"],
          );
        }
      }
      const baselineCell = editedCell && tableElementSourceIndexes(editedCell).flatMap((index) => {
        const cell = baselineCellBySource.get(index);
        return cell ? [cell] : [];
      })[0];
      const content = placement.continuation
        ? "<w:p/>"
        : editedCell ? tableCellContentXml(editedCell, source, context, baselineCell) : "<w:p/>";
      const preservedChildren = (cellParts?.children || []).filter((child) => {
        const name = directWordElementName(child);
        return name !== "tcPr"
          && !((child.match(WORD_PARAGRAPH_PATTERN) || []).length > 0)
          && name !== "tbl"
          && name !== "altChunk";
      });
      const opening = cellParts?.opening || "<w:tc>";
      const closing = cellParts?.closing || "</w:tc>";
      return `${opening}${properties}${content}${preservedChildren.join("")}${closing}`;
    }).join("");
    const preservedRowChildren = (sourceRowParts?.children || []).filter((child) => {
      const name = directWordElementName(child);
      return name !== "trPr" && name !== "tc";
    });
    const opening = sourceRowParts?.opening || "<w:tr>";
    const closing = sourceRowParts?.closing || "</w:tr>";
    return `${opening}${rowProperties}${cells}${preservedRowChildren.join("")}${closing}`;
  }).join("");
  const gridXml = patchTableGrid(source.gridXml, targetGridColumns);
  let rowsInserted = false;
  const children = source.parts.children.flatMap((child) => {
    const name = directWordElementName(child);
    if (name === "tblGrid") return [gridXml];
    if (name !== "tr") return [child];
    if (rowsInserted) return [];
    rowsInserted = true;
    return [rowXml];
  });
  if (!source.gridXml) {
    const propertiesIndex = children.findIndex((child) => directWordElementName(child) === "tblPr");
    children.splice(propertiesIndex + 1, 0, gridXml);
  }
  if (!rowsInserted) children.push(rowXml);
  return `${source.parts.opening}${children.join("")}${source.parts.closing}`;
}

interface DocumentElementSource {
  xml: string;
  paragraphIndexes: number[];
  baselineElement?: HTMLElement;
}

function documentElementXml(
  element: HTMLElement,
  context: DocumentSerializationContext,
  source?: DocumentElementSource,
): string {
  const tag = element.tagName.toLowerCase();
  if (tag === "table") {
    const tableSource = source && nestedWordElementSource(source.xml, "tbl");
    const tableXml = documentTableXml(
      element as HTMLTableElement,
      context,
      tableSource?.xml,
      source?.paragraphIndexes,
      source?.baselineElement instanceof HTMLTableElement ? source.baselineElement : undefined,
    );
    return tableSource ? tableSource.wrap(tableXml) : tableXml;
  }
  if (element.matches(DIRECT_INLINE_BLOCK_SELECTOR)) {
    const paragraph = element.ownerDocument.createElement("p");
    paragraph.appendChild(element.cloneNode(true));
    return documentParagraphXml(paragraph, "", context);
  }
  if (tag === "ul" || tag === "ol") {
    return Array.from(element.children).flatMap((child, index) => {
      if (!(child instanceof HTMLElement) || child.tagName.toLowerCase() !== "li") return [];
      const checklist = element.classList.contains("doc-editor-checklist");
      return [documentParagraphXml(child, checklist ? "☐ " : tag === "ol" ? `${index + 1}. ` : "• ", context)];
    }).join("");
  }
  if (element.matches(".doc-editor-page-break,[data-docx-page-break='true']")) {
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>';
  }
  if (tag === "hr") {
    return '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="B8B4AE"/></w:pBdr><w:spacing w:before="120" w:after="120"/></w:pPr></w:p>';
  }
  return documentParagraphXml(element, "", context);
}

function documentBodyXml(html: string, context: DocumentSerializationContext = EMPTY_DOCUMENT_CONTEXT): string {
  const parsed = new DOMParser().parseFromString(`<div id="docx-build-root">${html}</div>`, "text/html");
  const root = parsed.getElementById("docx-build-root");
  if (!root) throw new DocumentPreservationError("Unable to build the edited Word document.");
  const parts = Array.from(root.children).flatMap((child) => (
    child instanceof HTMLElement ? [documentElementXml(child, context)] : []
  ));
  if (parts.length === 0) parts.push("<w:p/>");
  return parts.join("");
}

const EMPTY_DOCUMENT_RELATIONSHIPS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>';
const BASIC_DOCUMENT_CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>';

interface PreparedDocumentResources {
  context: DocumentSerializationContext;
  relationshipsXml: string;
  contentTypesXml: string;
}

function appendXmlChild(xml: string, rootName: "Relationships" | "Types", child: string): string {
  const closing = `</${rootName}>`;
  if (xml.includes(closing)) return xml.replace(closing, `${child}${closing}`);
  return xml.replace(new RegExp(`<${rootName}([^>]*)\\/>`, "i"), `<${rootName}$1>${child}</${rootName}>`);
}

function nextDocumentRelationshipId(usedIds: Set<string>, prefix: string): string {
  let index = 1;
  while (usedIds.has(`${prefix}${index}`)) index += 1;
  const id = `${prefix}${index}`;
  usedIds.add(id);
  return id;
}

function imageExtension(mimeType: string): string | undefined {
  const normalized = mimeType.toLowerCase().split(";")[0].trim();
  if (normalized === "image/png") return "png";
  if (normalized === "image/jpeg" || normalized === "image/jpg") return "jpeg";
  if (normalized === "image/gif") return "gif";
  if (normalized === "image/svg+xml") return "svg";
  if (normalized === "image/webp") return "webp";
  if (normalized === "image/bmp") return "bmp";
  if (normalized === "image/tiff") return "tiff";
  return undefined;
}

function imageContentType(extension: string): string {
  if (extension === "jpeg") return "image/jpeg";
  if (extension === "svg") return "image/svg+xml";
  return `image/${extension}`;
}

function numericImageSize(image: HTMLImageElement, property: "width" | "height"): number | undefined {
  const attribute = Number(image.getAttribute(property));
  if (Number.isFinite(attribute) && attribute > 0) return attribute;
  const styled = cssPixels(image.style[property]);
  return styled && styled > 0 ? styled : undefined;
}

async function intrinsicImageSize(blob: Blob): Promise<{ width: number; height: number } | undefined> {
  if (typeof createImageBitmap !== "function") return undefined;
  try {
    const bitmap = await createImageBitmap(blob);
    const size = { width: bitmap.width, height: bitmap.height };
    bitmap.close();
    return size;
  } catch {
    return undefined;
  }
}

async function prepareDocumentResources(
  zip: { file(path: string, data?: Uint8Array | string): any; remove(path: string): unknown; files: Record<string, unknown> },
  html: string,
  relationshipsXml: string,
  contentTypesXml: string,
): Promise<PreparedDocumentResources> {
  const parsed = new DOMParser().parseFromString(`<div id="docx-resource-root">${html}</div>`, "text/html");
  const root = parsed.getElementById("docx-resource-root");
  if (!root) throw new DocumentPreservationError("Unable to prepare Word document resources.");

  const context: DocumentSerializationContext = { hyperlinks: new Map(), images: new Map(), drawingId: 1 };
  let nextRelationships = relationshipsXml;
  let nextContentTypes = contentTypesXml;
  const usedRelationshipIds = new Set(Array.from(nextRelationships.matchAll(/\bId="([^"]+)"/g), (match) => match[1]));

  for (const link of Array.from(root.querySelectorAll<HTMLAnchorElement>("a[href]"))) {
    const href = link.getAttribute("href")?.trim() || "";
    if (!href || context.hyperlinks.has(href) || /^(?:javascript|data):/i.test(href)) continue;
    const id = nextDocumentRelationshipId(usedRelationshipIds, "rIdManorLink");
    context.hyperlinks.set(href, id);
    nextRelationships = appendXmlChild(nextRelationships, "Relationships", `<Relationship Id="${id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="${escapeXmlAttribute(href)}" TargetMode="External"/>`);
  }

  let mediaIndex = 1;
  for (const image of Array.from(root.querySelectorAll<HTMLImageElement>("img[src]"))) {
    const source = image.getAttribute("src") || "";
    if (!source || context.images.has(source)) continue;
    try {
      const response = await fetch(source);
      if (!response.ok) continue;
      const blob = await response.blob();
      const extension = imageExtension(blob.type || source.match(/^data:([^;,]+)/i)?.[1] || "");
      if (!extension) continue;
      let mediaName = `manor-image-${mediaIndex++}.${extension}`;
      while (zip.files[`word/media/${mediaName}`]) mediaName = `manor-image-${mediaIndex++}.${extension}`;
      zip.file(`word/media/${mediaName}`, new Uint8Array(await blob.arrayBuffer()));
      const id = nextDocumentRelationshipId(usedRelationshipIds, "rIdManorImage");
      const intrinsic = await intrinsicImageSize(blob);
      const requestedWidth = numericImageSize(image, "width");
      const requestedHeight = numericImageSize(image, "height");
      const naturalWidth = intrinsic?.width || 640;
      const naturalHeight = intrinsic?.height || 360;
      const widthPx = Math.min(624, requestedWidth || (requestedHeight ? requestedHeight * naturalWidth / naturalHeight : naturalWidth));
      const heightPx = Math.min(780, requestedHeight || widthPx * naturalHeight / naturalWidth);
      context.images.set(source, { id, name: image.getAttribute("alt") || mediaName, widthPx, heightPx });
      nextRelationships = appendXmlChild(nextRelationships, "Relationships", `<Relationship Id="${id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/${escapeXmlAttribute(mediaName)}"/>`);
      if (!new RegExp(`<Default\\s+[^>]*Extension="${extension}"`, "i").test(nextContentTypes)) {
        nextContentTypes = appendXmlChild(nextContentTypes, "Types", `<Default Extension="${extension}" ContentType="${imageContentType(extension)}"/>`);
      }
    } catch {
      // Keep the image's accessible label in the document when its source is no longer readable.
    }
  }

  return { context, relationshipsXml: nextRelationships, contentTypesXml: nextContentTypes };
}

interface DocumentBodyUnit {
  xml: string;
  paragraphIndexes: number[];
}

function xmlTagEnd(xml: string, start: number): number {
  let quote = "";
  for (let index = start + 1; index < xml.length; index += 1) {
    const character = xml[index];
    if (quote) {
      if (character === quote) quote = "";
      continue;
    }
    if (character === '"' || character === "'") quote = character;
    else if (character === ">") return index;
  }
  return -1;
}

function splitDocumentBodyUnits(documentXml: string): {
  body: string;
  opening: string;
  closing: string;
  units: DocumentBodyUnit[];
} {
  const body = documentXml.match(/<w:body(?:\s[^>]*)?>[\s\S]*?<\/w:body>/i)?.[0];
  if (!body) throw new DocumentPreservationError("The DOCX package has no editable document body.");
  const parts = splitDirectWordChildren(body);

  let paragraphIndex = 0;
  const units = parts.children.map((xml) => {
    const paragraphCount = (xml.match(WORD_PARAGRAPH_PATTERN) || []).length;
    const paragraphIndexes = Array.from({ length: paragraphCount }, () => paragraphIndex++);
    return { xml, paragraphIndexes };
  });
  return { body, opening: parts.opening, closing: parts.closing, units };
}

function documentElementSourceIndexes(element: HTMLElement): number[] {
  const elements = [
    ...(element.hasAttribute(SOURCE_ATTRIBUTE) ? [element] : []),
    ...Array.from(element.querySelectorAll<HTMLElement>(`[${SOURCE_ATTRIBUTE}]`)),
  ];
  return Array.from(new Set(elements.flatMap((item) => {
    const value = Number(item.getAttribute(SOURCE_ATTRIBUTE));
    return Number.isInteger(value) && value >= 0 ? [value] : [];
  })));
}

function parseDocumentTopLevelElements(html: string): HTMLElement[] {
  const parsed = new DOMParser().parseFromString(`<div id="docx-body-merge-root">${html}</div>`, "text/html");
  const root = parsed.getElementById("docx-body-merge-root");
  if (!root) throw new DocumentPreservationError("Unable to read the edited Word document structure.");
  return Array.from(root.children).filter((child): child is HTMLElement => child instanceof HTMLElement);
}

function patchMappedParagraphsBeforeStructuralMerge(
  documentXml: string,
  baselineHtml: string,
  editedHtml: string,
  context: DocumentSerializationContext,
): string {
  const baseline = parseAnnotatedHtml(baselineHtml);
  const edited = parseAnnotatedHtml(editedHtml);
  const baselineBySource = new Map(baseline.blocks.flatMap((block) => {
    const rawSource = block.getAttribute(SOURCE_ATTRIBUTE);
    const source = rawSource == null ? undefined : Number(rawSource);
    return source != null && Number.isInteger(source) ? [[source, block] as const] : [];
  }));
  const editedBySource = new Map(edited.blocks.flatMap((block) => {
    const rawSource = block.getAttribute(SOURCE_ATTRIBUTE);
    const source = rawSource == null ? undefined : Number(rawSource);
    return source != null && Number.isInteger(source) ? [[source, block] as const] : [];
  }));
  let paragraphIndex = 0;
  return documentXml.replace(WORD_PARAGRAPH_PATTERN, (paragraphXml) => {
    const sourceIndex = paragraphIndex++;
    const baselineBlock = baselineBySource.get(sourceIndex);
    const editedBlock = editedBySource.get(sourceIndex);
    if (!baselineBlock || !editedBlock || baselineBlock.outerHTML === editedBlock.outerHTML) return paragraphXml;
    const baselineText = editableBlockText(baselineBlock);
    const editedText = editableBlockText(editedBlock);
    if (
      baselineText !== editedText
      && baselineBlock.getAttribute(SOURCE_EDITABLE_ATTRIBUTE) !== "false"
      && baselineBlock.tagName === editedBlock.tagName
      && markupSignature(baselineBlock) === markupSignature(editedBlock)
    ) {
      return patchParagraphText(paragraphXml, editedText);
    }
    const list = editedBlock.closest("ol,ul");
    const prefix = list?.tagName.toLowerCase() === "ol"
      ? `${Array.from(list.children).indexOf(editedBlock) + 1}. `
      : list ? "• " : "";
    const replacement = documentParagraphXml(editedBlock, prefix, context);
    return baselineBlock.tagName === editedBlock.tagName
      && blockShellSignature(baselineBlock) === blockShellSignature(editedBlock)
      ? patchParagraphContent(paragraphXml, replacement)
      : patchParagraphFormatting(paragraphXml, replacement);
  });
}

export function reorderDocumentBodyUnitXml(
  units: string[],
  sourceSlots: number[][],
  editedXml: string[],
): string[] {
  const slots = sourceSlots
    .filter((indexes) => indexes.length > 0)
    .map((indexes) => [...indexes].sort((left, right) => left - right))
    .sort((left, right) => left[0] - right[0]);
  const consumed = new Set(slots.flat());
  const replacements = new Map<number, string>();
  slots.forEach((slot, index) => {
    if (editedXml[index] != null) replacements.set(slot[0], editedXml[index]);
  });
  const overflow = editedXml.slice(slots.length);
  const overflowAfter = slots.at(-1)?.at(-1);
  const sectionIndex = units.findIndex((unit) => /^<w:sectPr\b/i.test(unit));
  const overflowBefore = overflowAfter == null ? (sectionIndex >= 0 ? sectionIndex : units.length) : undefined;
  const output: string[] = [];
  for (let unitIndex = 0; unitIndex <= units.length; unitIndex += 1) {
    if (unitIndex === overflowBefore) output.push(...overflow);
    if (unitIndex === units.length) break;
    if (replacements.has(unitIndex)) output.push(replacements.get(unitIndex)!);
    else if (!consumed.has(unitIndex)) output.push(units[unitIndex]);
    if (unitIndex === overflowAfter) output.push(...overflow);
  }
  return output;
}

function mergeDocumentBodyXml(
  documentXml: string,
  baselineHtml: string,
  editedHtml: string,
  context: DocumentSerializationContext,
): string {
  const body = splitDocumentBodyUnits(documentXml);
  const paragraphUnit = new Map<number, number>();
  body.units.forEach((unit, unitIndex) => unit.paragraphIndexes.forEach((index) => paragraphUnit.set(index, unitIndex)));
  const describe = (element: HTMLElement) => {
    const sourceIndexes = documentElementSourceIndexes(element);
    const unitIndexes = Array.from(new Set(sourceIndexes.flatMap((index) => {
      const unitIndex = paragraphUnit.get(index);
      return unitIndex == null ? [] : [unitIndex];
    }))).sort((left, right) => left - right);
    return { element, sourceIndexes, unitIndexes };
  };
  const baseline = parseDocumentTopLevelElements(baselineHtml).map(describe);
  const edited = parseDocumentTopLevelElements(editedHtml).map(describe);
  const keyFor = (indexes: number[]) => indexes.join(":");
  const baselineByUnits = new Map(baseline.flatMap((item) => (
    item.unitIndexes.length ? [[keyFor(item.unitIndexes), item] as const] : []
  )));
  const serializeItem = (
    item: (typeof edited)[number],
    baselineItem?: (typeof baseline)[number],
  ) => {
    const unitIndex = item.unitIndexes.length === 1 ? item.unitIndexes[0] : undefined;
    return documentElementXml(
      item.element,
      context,
      unitIndex == null ? undefined : {
        xml: body.units[unitIndex].xml,
        paragraphIndexes: body.units[unitIndex].paragraphIndexes,
        baselineElement: baselineItem?.element,
      },
    );
  };
  const editedUnitOrder = edited.flatMap((item) => item.unitIndexes);
  if (new Set(editedUnitOrder).size !== editedUnitOrder.length) {
    const duplicateUnit = editedUnitOrder.find((unitIndex, index) => editedUnitOrder.indexOf(unitIndex) !== index);
    throw new DocumentPreservationError(`A Word body unit was referenced more than once${duplicateUnit == null ? "" : ` (unit ${duplicateUnit})`}.`);
  }
  const sourceUnitsReordered = editedUnitOrder.some((unitIndex, index) => (
    index > 0 && unitIndex < editedUnitOrder[index - 1]
  ));
  if (sourceUnitsReordered) {
    const unchangedUnanchored = new Map<string, number>();
    for (const item of baseline.filter((candidate) => candidate.unitIndexes.length === 0)) {
      unchangedUnanchored.set(item.element.outerHTML, (unchangedUnanchored.get(item.element.outerHTML) || 0) + 1);
    }
    const orderedEditedXml = edited.flatMap((item) => {
      if (item.unitIndexes.length > 0) {
        const baselineItem = baselineByUnits.get(keyFor(item.unitIndexes));
        return [baselineItem?.element.outerHTML === item.element.outerHTML
          ? item.unitIndexes.map((unitIndex) => body.units[unitIndex].xml).join("")
          : serializeItem(item, baselineItem)];
      }
      const unchangedCount = unchangedUnanchored.get(item.element.outerHTML) || 0;
      if (unchangedCount > 0) {
        unchangedUnanchored.set(item.element.outerHTML, unchangedCount - 1);
        return [];
      }
      return [documentElementXml(item.element, context)];
    });
    const orderedBody = reorderDocumentBodyUnitXml(
      body.units.map((unit) => unit.xml),
      baseline.filter((item) => item.unitIndexes.length > 0).map((item) => item.unitIndexes),
      orderedEditedXml,
    );
    return documentXml.replace(body.body, `${body.opening}${orderedBody.join("")}${body.closing}`);
  }
  const editedReferencedUnits = new Set(edited.flatMap((item) => item.unitIndexes));
  const replacements = new Map<number, string>();
  const consumed = new Set<number>();

  for (const item of edited) {
    if (item.unitIndexes.length === 0) continue;
    const baselineItem = baselineByUnits.get(keyFor(item.unitIndexes));
    if (baselineItem?.element.outerHTML === item.element.outerHTML) continue;
    const firstUnit = item.unitIndexes[0];
    replacements.set(firstUnit, serializeItem(item, baselineItem));
    item.unitIndexes.forEach((unitIndex) => consumed.add(unitIndex));
  }
  for (const item of baseline) {
    if (item.unitIndexes.length === 0 || item.unitIndexes.some((unitIndex) => editedReferencedUnits.has(unitIndex))) continue;
    item.unitIndexes.forEach((unitIndex) => consumed.add(unitIndex));
  }

  const unchangedUnanchored = new Map<string, number>();
  for (const item of baseline.filter((candidate) => candidate.unitIndexes.length === 0)) {
    unchangedUnanchored.set(item.element.outerHTML, (unchangedUnanchored.get(item.element.outerHTML) || 0) + 1);
  }
  const insertBefore = new Map<number, string[]>();
  const insertAfter = new Map<number, string[]>();
  const appendInsertion = (target: Map<number, string[]>, index: number, xml: string) => {
    target.set(index, [...(target.get(index) || []), xml]);
  };
  for (let editedIndex = 0; editedIndex < edited.length; editedIndex += 1) {
    const item = edited[editedIndex];
    if (item.unitIndexes.length > 0) continue;
    const unchangedCount = unchangedUnanchored.get(item.element.outerHTML) || 0;
    if (unchangedCount > 0) {
      unchangedUnanchored.set(item.element.outerHTML, unchangedCount - 1);
      continue;
    }
    const xml = documentElementXml(item.element, context);
    const explicitSource = Number(item.element.getAttribute(INSERT_AFTER_ATTRIBUTE)
      || item.element.querySelector<HTMLElement>(`[${INSERT_AFTER_ATTRIBUTE}]`)?.getAttribute(INSERT_AFTER_ATTRIBUTE));
    const explicitUnit = Number.isInteger(explicitSource) ? paragraphUnit.get(explicitSource) : undefined;
    if (explicitUnit != null) {
      appendInsertion(insertAfter, explicitUnit, xml);
      continue;
    }
    let previousUnit: number | undefined;
    for (let index = editedIndex - 1; index >= 0; index -= 1) {
      if (edited[index].unitIndexes.length === 0) continue;
      previousUnit = edited[index].unitIndexes.at(-1);
      break;
    }
    if (previousUnit != null) {
      appendInsertion(insertAfter, previousUnit, xml);
      continue;
    }
    let nextUnit: number | undefined;
    for (let index = editedIndex + 1; index < edited.length; index += 1) {
      if (edited[index].unitIndexes.length === 0) continue;
      nextUnit = edited[index].unitIndexes[0];
      break;
    }
    if (nextUnit != null) appendInsertion(insertBefore, nextUnit, xml);
    else {
      const sectionIndex = body.units.findIndex((unit) => /^<w:sectPr\b/i.test(unit.xml));
      appendInsertion(insertBefore, sectionIndex >= 0 ? sectionIndex : body.units.length, xml);
    }
  }

  const output: string[] = [];
  for (let unitIndex = 0; unitIndex <= body.units.length; unitIndex += 1) {
    output.push(...(insertBefore.get(unitIndex) || []));
    if (unitIndex === body.units.length) break;
    if (replacements.has(unitIndex)) output.push(replacements.get(unitIndex)!);
    else if (!consumed.has(unitIndex)) output.push(body.units[unitIndex].xml);
    output.push(...(insertAfter.get(unitIndex) || []));
  }
  return documentXml.replace(body.body, `${body.opening}${output.join("")}${body.closing}`);
}

function pruneUnusedGeneratedDocumentResources(
  zip: { remove(path: string): unknown },
  documentXml: string,
  relationshipsXml: string,
): string {
  const unusedMedia: string[] = [];
  const nextRelationships = relationshipsXml.replace(
    /<Relationship\b(?=[^>]*\bId="rIdManor(?:Image|Link)\d+")[^>]*\/>/gi,
    (relationship) => {
      const id = relationship.match(/\bId="([^"]+)"/i)?.[1];
      if (id && new RegExp(`\\br:(?:id|embed)="${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`, "i").test(documentXml)) {
        return relationship;
      }
      const target = relationship.match(/\bTarget="([^"]+)"/i)?.[1];
      if (target && !/\bTargetMode="External"/i.test(relationship)) unusedMedia.push(`word/${target.replace(/^\.\//, "")}`);
      return "";
    },
  );
  const remainingTargets = new Set(Array.from(nextRelationships.matchAll(/\bTarget="([^"]+)"/gi), (match) => `word/${match[1].replace(/^\.\//, "")}`));
  unusedMedia.forEach((path) => {
    if (!remainingTargets.has(path)) zip.remove(path);
  });
  return nextRelationships;
}

async function rebuildDocumentBodyInPackage(
  original: ArrayBuffer,
  baselineHtml: string,
  html: string,
  fileName: string,
): Promise<File> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(original);
  const documentEntry = zip.file("word/document.xml");
  if (!documentEntry) throw new DocumentPreservationError("The DOCX package has no word/document.xml part.");
  const documentXml = await documentEntry.async("text");
  const relationshipsXml = await zip.file("word/_rels/document.xml.rels")?.async("text") || EMPTY_DOCUMENT_RELATIONSHIPS;
  const contentTypesXml = await zip.file("[Content_Types].xml")?.async("text") || BASIC_DOCUMENT_CONTENT_TYPES;
  const resources = await prepareDocumentResources(zip, html, relationshipsXml, contentTypesXml);
  const patchedDocumentXml = patchMappedParagraphsBeforeStructuralMerge(
    documentXml,
    baselineHtml,
    html,
    resources.context,
  );
  const nextDocumentXml = mergeDocumentBodyXml(patchedDocumentXml, baselineHtml, html, resources.context);
  const nextRelationshipsXml = pruneUnusedGeneratedDocumentResources(
    zip,
    nextDocumentXml,
    resources.relationshipsXml,
  );
  zip.file("word/document.xml", nextDocumentXml);
  zip.file("word/_rels/document.xml.rels", nextRelationshipsXml);
  zip.file("[Content_Types].xml", resources.contentTypesXml);
  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE", mimeType: DOCX_MIME });
  const safeName = fileName.toLowerCase().endsWith(".docx") ? fileName : `${fileName.replace(/\.(?:doc|wps)$/i, "")}.docx`;
  return new File([blob], safeName, { type: DOCX_MIME, lastModified: Date.now() });
}

/**
 * Save Word editor HTML through one package-preserving engine. Text-only edits
 * patch the original paragraph runs; structural or formatting edits rewrite
 * only word/document.xml while retaining every other original package part.
 */
export async function editDocumentFile(
  original: ArrayBuffer,
  baselineHtml: string,
  editedHtml: string,
  fileName: string,
): Promise<File> {
  const currentSources = await extractDocumentParagraphSources(original);
  const currentBaselineHtml = annotateDocumentHtml(baselineHtml, currentSources);
  const currentEditedHtml = carryDocumentSourceAnnotations(
    currentBaselineHtml,
    annotateDocumentHtml(editedHtml, currentSources),
  );
  try {
    const baseline = parseAnnotatedHtml(currentBaselineHtml);
    const edited = parseAnnotatedHtml(currentEditedHtml);
    const resourceSignature = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>("a[href],img[src]"))
      .map((element) => `${element.tagName}:${element.getAttribute(element.tagName.toLowerCase() === "a" ? "href" : "src") || ""}`)
      .join("\n");
    const baselineBlocks = new Map(baseline.blocks.flatMap((block) => {
      const index = block.getAttribute(SOURCE_ATTRIBUTE);
      return index == null ? [] : [[Number(index), block] as const];
    }));
    const changedResourceBlock = edited.blocks.some((block) => {
      if (!block.matches(DIRECT_INLINE_BLOCK_SELECTOR) && !block.querySelector(DIRECT_INLINE_BLOCK_SELECTOR)) return false;
      const index = block.getAttribute(SOURCE_ATTRIBUTE);
      if (index == null) return true;
      const baselineBlock = baselineBlocks.get(Number(index));
      if (!baselineBlock) return true;
      return block.matches(DIRECT_INLINE_BLOCK_SELECTOR)
        ? baselineBlock.outerHTML !== block.outerHTML
        : baselineBlock.innerHTML !== block.innerHTML;
    });
    if (resourceSignature(baseline.root) !== resourceSignature(edited.root) || changedResourceBlock) {
      return rebuildDocumentBodyInPackage(original, currentBaselineHtml, currentEditedHtml, fileName);
    }
    const edits = collectDocumentParagraphEdits(currentBaselineHtml, currentEditedHtml);
    return await preserveDocumentFile(original, edits, fileName);
  } catch (error) {
    if (!(error instanceof DocumentPreservationError)) throw error;
    return rebuildDocumentBodyInPackage(original, currentBaselineHtml, currentEditedHtml, fileName);
  }
}

export async function editDocumentFileWithSnapshot(
  original: ArrayBuffer,
  baselineHtml: string,
  editedHtml: string,
  fileName: string,
): Promise<{ file: File; savedBuffer: ArrayBuffer; savedHtml: string }> {
  const file = await editDocumentFile(original, baselineHtml, editedHtml, fileName);
  const savedBuffer = await file.arrayBuffer();
  const sources = await extractDocumentParagraphSources(savedBuffer);
  return {
    file,
    savedBuffer,
    savedHtml: annotateDocumentHtml(editedHtml, sources),
  };
}

export async function buildDocumentFile(html: string, fileName: string): Promise<File> {
  const JSZip = (await import("jszip")).default;
  const zip = new JSZip();
  const resources = await prepareDocumentResources(zip, html, EMPTY_DOCUMENT_RELATIONSHIPS, BASIC_DOCUMENT_CONTENT_TYPES);
  const body = documentBodyXml(html, resources.context);
  zip.file("[Content_Types].xml", resources.contentTypesXml);
  zip.file("_rels/.rels", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`);
  zip.file("word/document.xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>${body}<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>`);
  zip.file("word/_rels/document.xml.rels", resources.relationshipsXml);
  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE", mimeType: DOCX_MIME });
  const safeName = fileName.replace(/\.(?:doc|wps)$/i, ".docx");
  return new File([blob], safeName.toLowerCase().endsWith(".docx") ? safeName : `${safeName}.docx`, {
    type: DOCX_MIME,
    lastModified: Date.now(),
  });
}
