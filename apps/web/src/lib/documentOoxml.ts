const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const SOURCE_ATTRIBUTE = "data-docx-paragraph-index";
const BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li";

export interface DocumentParagraphSource {
  index: number;
  text: string;
  editable: boolean;
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
  return (documentXml.match(/<w:p[\s>][\s\S]*?<\/w:p>/g) || []).map((paragraph, index) => ({
    index,
    text: paragraphText(paragraph),
    editable: paragraphIsEditable(paragraph),
  }));
}

function leafBlocks(root: ParentNode): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(BLOCK_SELECTOR)).filter((element) => (
    !element.querySelector(BLOCK_SELECTOR)
  ));
}

export function annotateDocumentHtml(html: string, sources: DocumentParagraphSource[]): string {
  const parsed = new DOMParser().parseFromString(`<div id="docx-annotation-root">${html}</div>`, "text/html");
  const root = parsed.getElementById("docx-annotation-root");
  if (!root) return html;
  let sourceCursor = 0;

  for (const block of leafBlocks(root)) {
    const blockText = normalizeText(block.textContent || "");
    let matchedIndex = -1;
    for (let index = sourceCursor; index < sources.length; index += 1) {
      const source = sources[index];
      if (!source.editable) continue;
      if (normalizeText(source.text) === blockText) {
        matchedIndex = index;
        break;
      }
    }
    if (matchedIndex < 0) continue;
    const source = sources[matchedIndex];
    block.setAttribute(SOURCE_ATTRIBUTE, String(source.index));
    sourceCursor = matchedIndex + 1;
  }

  return root.innerHTML;
}

function parseAnnotatedHtml(html: string): { blocks: HTMLElement[]; root: HTMLElement } {
  const parsed = new DOMParser().parseFromString(`<div id="docx-edit-root">${html}</div>`, "text/html");
  const root = parsed.getElementById("docx-edit-root");
  if (!root) throw new DocumentPreservationError("Unable to read the DOCX editor content.");
  return { root, blocks: leafBlocks(root) };
}

function markupSignature(element: HTMLElement): string {
  const clone = element.cloneNode(true) as HTMLElement;
  const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    node.textContent = "";
    node = walker.nextNode();
  }
  return clone.outerHTML;
}

export function collectDocumentParagraphEdits(baselineHtml: string, editedHtml: string): Map<number, string> {
  const baseline = parseAnnotatedHtml(baselineHtml);
  const edited = parseAnnotatedHtml(editedHtml);
  const baselineMapped = new Map<number, HTMLElement>();
  const editedMapped = new Map<number, HTMLElement>();

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

  if (baseline.blocks.length !== edited.blocks.length || baselineMapped.size !== editedMapped.size) {
    throw new DocumentPreservationError("Adding, removing, splitting, or merging paragraphs is not yet supported in DOCX fidelity mode.");
  }

  const baselineStructure = baseline.blocks.map((block) => [block.tagName, block.getAttribute(SOURCE_ATTRIBUTE)]);
  const editedStructure = edited.blocks.map((block) => [block.tagName, block.getAttribute(SOURCE_ATTRIBUTE)]);
  if (JSON.stringify(baselineStructure) !== JSON.stringify(editedStructure)) {
    throw new DocumentPreservationError("Changing document structure is not yet supported in DOCX fidelity mode.");
  }

  const edits = new Map<number, string>();
  for (const [index, baselineBlock] of baselineMapped) {
    const editedBlock = editedMapped.get(index);
    if (!editedBlock) throw new DocumentPreservationError("A source paragraph was removed.");
    if (baselineBlock.innerHTML !== editedBlock.innerHTML) {
      const baselineText = baselineBlock.textContent || "";
      const editedText = editedBlock.textContent || "";
      if (baselineText === editedText || markupSignature(baselineBlock) !== markupSignature(editedBlock)) {
        throw new DocumentPreservationError("Formatting changes are not yet supported in DOCX fidelity mode.");
      }
      edits.set(index, editedText);
    }
  }

  for (let index = 0; index < baseline.blocks.length; index += 1) {
    const baselineBlock = baseline.blocks[index];
    if (baselineBlock.hasAttribute(SOURCE_ATTRIBUTE)) continue;
    if (baselineBlock.outerHTML !== edited.blocks[index]?.outerHTML) {
      throw new DocumentPreservationError("This Word object cannot be edited safely in DOCX fidelity mode.");
    }
  }

  return edits;
}

function setPreservedTextTag(tag: string, value: string): string {
  let openTag = tag.match(/^<w:t(?:\s[^>]*)?>/)?.[0] || "<w:t>";
  if (/^\s|\s$/.test(value) && !/xml:space=/i.test(openTag)) {
    openTag = openTag.replace(/>$/, ' xml:space="preserve">');
  }
  return `${openTag}${escapeXmlText(value)}</w:t>`;
}

function patchParagraphText(paragraphXml: string, value: string): string {
  let foundText = false;
  let patched = paragraphXml.replace(/<w:t(?:\s[^>]*)?>[\s\S]*?<\/w:t>/g, (tag) => {
    if (foundText) return tag.replace(/(<w:t(?:\s[^>]*)?>)[\s\S]*?(<\/w:t>)/, "$1$2");
    foundText = true;
    return setPreservedTextTag(tag, value);
  });
  if (!foundText && value) throw new DocumentPreservationError("This paragraph has no editable Word text run.");
  patched = patched.replace(/<w:(?:tab|br|cr)\b[^>]*\/>/g, "");
  return patched;
}

export async function preserveDocumentFile(
  original: ArrayBuffer,
  edits: Map<number, string>,
  fileName: string,
): Promise<File> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(original);
  const documentEntry = zip.file("word/document.xml");
  if (!documentEntry) throw new DocumentPreservationError("The DOCX package has no word/document.xml part.");
  const documentXml = await documentEntry.async("text");
  let paragraphIndex = 0;
  const patchedXml = documentXml.replace(/<w:p[\s>][\s\S]*?<\/w:p>/g, (paragraph) => {
    const edit = edits.get(paragraphIndex++);
    return edit == null ? paragraph : patchParagraphText(paragraph, edit);
  });
  for (const index of edits.keys()) {
    if (index < 0 || index >= paragraphIndex) throw new DocumentPreservationError(`Unable to find source paragraph ${index}.`);
  }
  zip.file("word/document.xml", patchedXml);
  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE", mimeType: DOCX_MIME });
  const safeName = fileName.toLowerCase().endsWith(".docx") ? fileName : `${fileName.replace(/\.doc$/i, "")}.docx`;
  return new File([blob], safeName, { type: DOCX_MIME, lastModified: Date.now() });
}
