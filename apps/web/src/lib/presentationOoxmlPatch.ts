const PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

type PresentationSourceKind = "sp" | "pic" | "cxnSp" | "graphicFrame";

interface PresentationShapeSource {
  part: string;
  kind: PresentationSourceKind;
  objectId: string;
  editable: boolean;
  mediaPart?: string;
}

interface PresentationText {
  text: string;
  runs?: Array<{ text: string }>;
}

interface PresentationTableCell {
  text: string;
}

export interface PreservePresentationShape {
  id: string;
  type?: string;
  x: number;
  y: number;
  w: number;
  h: number;
  rotation?: number;
  flipH?: boolean;
  flipV?: boolean;
  imgCrop?: { l: number; t: number; r: number; b: number };
  texts: PresentationText[];
  tableRows?: PresentationTableCell[][];
  imgUrl?: string;
  hyperlink?: string;
  source?: PresentationShapeSource;
}

export interface PreservePresentationSlide {
  id: string;
  notes?: string;
  shapes: PreservePresentationShape[];
  sourcePart?: string;
  notesPart?: string;
}

export class PresentationPreservationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PresentationPreservationError";
  }
}

function escapeXml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]),
  );
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(stableValue(left)) === JSON.stringify(stableValue(right));
}

function shapeUnsupportedProperties(shape: PreservePresentationShape): Record<string, unknown> {
  const unsupported = { ...shape } as Record<string, unknown>;
  for (const key of ["id", "source", "x", "y", "w", "h", "rotation", "flipH", "flipV", "imgCrop", "texts", "tableRows", "imgUrl", "hyperlink"]) {
    delete unsupported[key];
  }
  return unsupported;
}

function textStyle(text: PresentationText): Record<string, unknown> {
  const style = { ...text } as Record<string, unknown>;
  delete style.text;
  delete style.runs;
  return style;
}

function tableCellStyle(cell: PresentationTableCell): Record<string, unknown> {
  const style = { ...cell } as Record<string, unknown>;
  delete style.text;
  return style;
}

function slideUnsupportedProperties(slide: PreservePresentationSlide): Record<string, unknown> {
  const unsupported = { ...slide } as Record<string, unknown>;
  for (const key of ["id", "notes", "shapes", "sourcePart", "notesPart"]) delete unsupported[key];
  return unsupported;
}

function setXmlAttribute(openTag: string, name: string, value: string | undefined): string {
  const attribute = new RegExp(`\\s${name}="[^"]*"`, "i");
  if (value == null) return openTag.replace(attribute, "");
  if (attribute.test(openTag)) return openTag.replace(attribute, ` ${name}="${value}"`);
  return openTag.replace(/\s*\/?\s*>$/, (ending) => ` ${name}="${value}"${ending}`);
}

function patchTransform(element: string, shape: PreservePresentationShape, slideWidth: number, slideHeight: number): string {
  const x = Math.round((shape.x / 100) * slideWidth);
  const y = Math.round((shape.y / 100) * slideHeight);
  const cx = Math.max(1, Math.round((shape.w / 100) * slideWidth));
  const cy = Math.max(1, Math.round((shape.h / 100) * slideHeight));
  const rotation = shape.rotation ? String(Math.round(shape.rotation * 60000)) : undefined;

  const patchXfrm = (xfrm: string, prefix: string) => {
    let next = xfrm.replace(new RegExp(`<${prefix}:xfrm\\b[^>]*>`, "i"), (openTag) => {
      let patched = setXmlAttribute(openTag, "rot", rotation);
      patched = setXmlAttribute(patched, "flipH", shape.flipH ? "1" : undefined);
      return setXmlAttribute(patched, "flipV", shape.flipV ? "1" : undefined);
    });
    next = next.replace(new RegExp(`<${prefix}:off\\b[^>]*/>`, "i"), `<${prefix}:off x="${x}" y="${y}"/>`);
    next = next.replace(new RegExp(`<${prefix}:ext\\b[^>]*/>`, "i"), `<${prefix}:ext cx="${cx}" cy="${cy}"/>`);
    return next;
  };

  const aXfrm = element.match(/<a:xfrm\b[^>]*>[\s\S]*?<\/a:xfrm>/i)?.[0];
  if (aXfrm) return element.replace(aXfrm, patchXfrm(aXfrm, "a"));
  const pXfrm = element.match(/<p:xfrm\b[^>]*>[\s\S]*?<\/p:xfrm>/i)?.[0];
  if (pXfrm) return element.replace(pXfrm, patchXfrm(pXfrm, "p"));

  const attributes = `${rotation ? ` rot="${rotation}"` : ""}${shape.flipH ? ' flipH="1"' : ""}${shape.flipV ? ' flipV="1"' : ""}`;
  const xfrm = `<a:xfrm${attributes}><a:off x="${x}" y="${y}"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm>`;
  if (/<p:spPr\b[^>]*\/>/i.test(element)) {
    return element.replace(/<p:spPr\b([^>]*)\/>/i, `<p:spPr$1>${xfrm}</p:spPr>`);
  }
  if (/<p:spPr\b[^>]*>/i.test(element)) {
    return element.replace(/<p:spPr\b[^>]*>/i, (openTag) => `${openTag}${xfrm}`);
  }
  throw new PresentationPreservationError("This object has no editable OOXML transform.");
}

function patchImageCrop(element: string, crop: PreservePresentationShape["imgCrop"]): string {
  const existing = /<a:srcRect\b[^>]*\/?\s*>/i;
  if (!crop || !(crop.l || crop.t || crop.r || crop.b)) {
    return element.replace(existing, "");
  }
  const attributes = (["l", "t", "r", "b"] as const)
    .map((key) => `${key}="${Math.round(Math.max(0, Math.min(95, crop[key])) * 1000)}"`)
    .join(" ");
  const sourceRect = `<a:srcRect ${attributes}/>`;
  if (existing.test(element)) return element.replace(existing, sourceRect);
  const blip = element.match(/<a:blip\b[^>]*\/>|<a:blip\b[^>]*>[\s\S]*?<\/a:blip>/i)?.[0];
  if (!blip) throw new PresentationPreservationError("This image has no editable OOXML blip.");
  return element.replace(blip, `${blip}${sourceRect}`);
}

function patchTextParagraphs(element: string, texts: PresentationText[]): string {
  const paragraphs = element.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [];
  if (paragraphs.length !== texts.length) {
    throw new PresentationPreservationError("Adding or removing paragraphs is not yet supported in fidelity mode.");
  }
  let paragraphIndex = 0;
  return element.replace(/<a:p[\s>][\s\S]*?<\/a:p>/g, (paragraph) => {
    const value = escapeXml(texts[paragraphIndex++].text);
    let found = false;
    const patched = paragraph.replace(/<a:t(\s[^>]*)?>([\s\S]*?)<\/a:t>/g, (tag, attributes = "") => {
      if (found) return `<a:t${attributes}></a:t>`;
      found = true;
      return `<a:t${attributes}>${value}</a:t>`;
    });
    if (!found && value) throw new PresentationPreservationError("This text object has no editable OOXML text run.");
    return patched;
  });
}

function patchTableCells(element: string, rows: PresentationTableCell[][]): string {
  const cells = rows.flat();
  const xmlCells = element.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/g) || [];
  if (xmlCells.length !== cells.length) {
    throw new PresentationPreservationError("Changing the table structure is not yet supported in fidelity mode.");
  }
  let cellIndex = 0;
  return element.replace(/<a:tc[\s>][\s\S]*?<\/a:tc>/g, (cellXml) => {
    const value = escapeXml(cells[cellIndex++].text);
    let found = false;
    const patched = cellXml.replace(/<a:t(\s[^>]*)?>([\s\S]*?)<\/a:t>/g, (tag, attributes = "") => {
      if (found) return `<a:t${attributes}></a:t>`;
      found = true;
      return `<a:t${attributes}>${value}</a:t>`;
    });
    if (!found && value) throw new PresentationPreservationError("This table cell has no editable OOXML text run.");
    return patched;
  });
}

function replaceSourceObject(
  slideXml: string,
  source: PresentationShapeSource,
  update: (element: string) => string,
): string {
  const pattern = new RegExp(`<p:${source.kind}\\b[\\s\\S]*?<\\/p:${source.kind}>`, "g");
  let found = false;
  const patched = slideXml.replace(pattern, (element) => {
    const cNvPr = element.match(/<p:cNvPr\b[^>]*\/?\s*>/)?.[0];
    const objectId = cNvPr?.match(/\bid="([^"]+)"/i)?.[1];
    if (objectId !== source.objectId) return element;
    found = true;
    return update(element);
  });
  if (!found) throw new PresentationPreservationError(`Unable to find source object ${source.objectId}.`);
  return patched;
}

function imageMimeForPart(part: string): string {
  const extension = part.split(".").pop()?.toLowerCase();
  if (extension === "jpg" || extension === "jpeg") return "image/jpeg";
  if (extension === "svg") return "image/svg+xml";
  if (extension === "webp") return "image/webp";
  if (extension === "gif") return "image/gif";
  return "image/png";
}

async function imageBytes(url: string, expectedMime: string): Promise<Uint8Array> {
  let blob: Blob;
  if (url.startsWith("data:")) {
    const match = url.match(/^data:([^;,]+)(;base64)?,(.*)$/s);
    if (!match) throw new PresentationPreservationError("The replacement image data is invalid.");
    const mime = match[1].toLowerCase();
    if (mime !== expectedMime) {
      throw new PresentationPreservationError(`Use a ${expectedMime.replace("image/", "").toUpperCase()} image to preserve this PPTX package.`);
    }
    const binary = match[2] ? atob(match[3]) : decodeURIComponent(match[3]);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes;
  }
  const response = await fetch(url);
  if (!response.ok) throw new PresentationPreservationError(`Unable to read the replacement image (${response.status}).`);
  blob = await response.blob();
  if (blob.type && blob.type.toLowerCase() !== expectedMime) {
    throw new PresentationPreservationError(`Use a ${expectedMime.replace("image/", "").toUpperCase()} image to preserve this PPTX package.`);
  }
  return new Uint8Array(await blob.arrayBuffer());
}

function imageExtensionForMime(mime: string): string {
  if (mime === "image/jpeg") return "jpg";
  if (mime === "image/svg+xml") return "svg";
  if (mime === "image/webp") return "webp";
  if (mime === "image/gif") return "gif";
  return "png";
}

async function newImagePayload(url: string): Promise<{ bytes: Uint8Array; mime: string; extension: string }> {
  let blob: Blob;
  if (url.startsWith("data:")) {
    const match = url.match(/^data:([^;,]+)(;base64)?,(.*)$/s);
    if (!match) throw new PresentationPreservationError("The inserted image data is invalid.");
    const mime = match[1].toLowerCase();
    if (!mime.startsWith("image/")) throw new PresentationPreservationError("The inserted object is not an image.");
    const binary = match[2] ? atob(match[3]) : decodeURIComponent(match[3]);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return { bytes, mime, extension: imageExtensionForMime(mime) };
  }
  const response = await fetch(url);
  if (!response.ok) throw new PresentationPreservationError(`Unable to read the inserted image (${response.status}).`);
  blob = await response.blob();
  const mime = blob.type.toLowerCase();
  if (!mime.startsWith("image/")) throw new PresentationPreservationError("The inserted object is not an image.");
  return {
    bytes: new Uint8Array(await blob.arrayBuffer()),
    mime,
    extension: imageExtensionForMime(mime),
  };
}

function relationshipPartForSlide(slidePart: string): string {
  const separator = slidePart.lastIndexOf("/");
  const directory = separator >= 0 ? slidePart.slice(0, separator) : "";
  const fileName = separator >= 0 ? slidePart.slice(separator + 1) : slidePart;
  return `${directory}/_rels/${fileName}.rels`;
}

function nextRelationshipId(relationshipsXml: string): string {
  const ids = Array.from(relationshipsXml.matchAll(/\bId="rId(\d+)"/g), (match) => Number(match[1]));
  return `rId${Math.max(0, ...ids) + 1}`;
}

function nextMediaPart(zip: { files: Record<string, unknown> }, extension: string): string {
  const indexes = Object.keys(zip.files)
    .map((path) => path.match(/^ppt\/media\/image(\d+)\.[^.]+$/i)?.[1])
    .filter(Boolean)
    .map(Number);
  return `ppt/media/image${Math.max(0, ...indexes) + 1}.${extension}`;
}

function ensureImageContentType(contentTypesXml: string, extension: string, mime: string): string {
  const existing = new RegExp(`<Default\\b[^>]*\\bExtension="${extension}"`, "i");
  if (existing.test(contentTypesXml)) return contentTypesXml;
  return contentTypesXml.replace(
    /<\/Types>\s*$/i,
    `<Default Extension="${escapeXml(extension)}" ContentType="${escapeXml(mime)}"/></Types>`,
  );
}

function appendImageRelationship(relationshipsXml: string, relationshipId: string, mediaPart: string): string {
  const target = `../media/${mediaPart.split("/").pop()}`;
  const relationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="${escapeXml(target)}"/>`;
  return relationshipsXml.replace(/<\/Relationships>\s*$/i, `${relationship}</Relationships>`);
}

function appendHyperlinkRelationship(relationshipsXml: string, relationshipId: string, url: string): string {
  const relationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="${escapeXml(url)}" TargetMode="External"/>`;
  return relationshipsXml.replace(/<\/Relationships>\s*$/i, `${relationship}</Relationships>`);
}

function appendPictureToSlide(
  slideXml: string,
  shape: PreservePresentationShape,
  relationshipId: string,
  hyperlinkRelationshipId: string | undefined,
  slideWidth: number,
  slideHeight: number,
): string {
  const objectIds = Array.from(slideXml.matchAll(/<p:cNvPr\b[^>]*\bid="(\d+)"/g), (match) => Number(match[1]));
  const objectId = Math.max(1, ...objectIds) + 1;
  const x = Math.round((shape.x / 100) * slideWidth);
  const y = Math.round((shape.y / 100) * slideHeight);
  const cx = Math.max(1, Math.round((shape.w / 100) * slideWidth));
  const cy = Math.max(1, Math.round((shape.h / 100) * slideHeight));
  const rotation = shape.rotation ? ` rot="${Math.round(shape.rotation * 60000)}"` : "";
  const flips = `${shape.flipH ? ' flipH="1"' : ""}${shape.flipV ? ' flipV="1"' : ""}`;
  const crop = shape.imgCrop && (shape.imgCrop.l || shape.imgCrop.t || shape.imgCrop.r || shape.imgCrop.b)
    ? `<a:srcRect ${(["l", "t", "r", "b"] as const).map((key) => `${key}="${Math.round(Math.max(0, Math.min(95, shape.imgCrop![key])) * 1000)}"`).join(" ")}/>`
    : "";
  const picture = [
    "<p:pic>",
    `<p:nvPicPr><p:cNvPr id="${objectId}" name="Inserted image ${objectId}">${hyperlinkRelationshipId ? `<a:hlinkClick r:id="${hyperlinkRelationshipId}"/>` : ""}</p:cNvPr><p:cNvPicPr/><p:nvPr/></p:nvPicPr>`,
    `<p:blipFill><a:blip r:embed="${relationshipId}"/>${crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill>`,
    `<p:spPr><a:xfrm${rotation}${flips}><a:off x="${x}" y="${y}"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>`,
    "</p:pic>",
  ].join("");
  if (!/<\/p:spTree>/i.test(slideXml)) {
    throw new PresentationPreservationError("This slide has no editable shape tree.");
  }
  return slideXml.replace(/<\/p:spTree>/i, `${picture}</p:spTree>`);
}

function parseSlideSize(presentationXml: string): { width: number; height: number } {
  const match = presentationXml.match(/<p:sldSz\b[^>]*\bcx="(\d+)"[^>]*\bcy="(\d+)"/i);
  return match
    ? { width: Number(match[1]), height: Number(match[2]) }
    : { width: 12192000, height: 6858000 };
}

function patchSpeakerNotes(notesXml: string, notes: string): string {
  const bodyShape = (notesXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [])
    .find((shape) => /<p:ph\b[^>]*type="body"/i.test(shape));
  if (!bodyShape) throw new PresentationPreservationError("This slide has no editable speaker-notes body.");
  const textBody = bodyShape.match(/<p:txBody[\s>][\s\S]*?<\/p:txBody>/i)?.[0];
  if (!textBody) throw new PresentationPreservationError("This slide has no editable speaker-notes text body.");
  const bodyPr = textBody.match(/<a:bodyPr\b[^>]*\/?\s*>/i)?.[0] || "<a:bodyPr/>";
  const listStyle = textBody.match(/<a:lstStyle\b[^>]*>[\s\S]*?<\/a:lstStyle>|<a:lstStyle\b[^>]*\/>/i)?.[0] || "<a:lstStyle/>";
  const paragraphs = (notes.split(/\r?\n/) || [""])
    .map((line) => `<a:p><a:r><a:rPr lang="en-US"/><a:t>${escapeXml(line)}</a:t></a:r><a:endParaRPr lang="en-US"/></a:p>`)
    .join("");
  const nextTextBody = `<p:txBody>${bodyPr}${listStyle}${paragraphs}</p:txBody>`;
  return notesXml.replace(bodyShape, bodyShape.replace(textBody, nextTextBody));
}

function ensureCompatibleShapeChange(baseline: PreservePresentationShape, edited: PreservePresentationShape) {
  if (!sameValue(shapeUnsupportedProperties(baseline), shapeUnsupportedProperties(edited))) {
    throw new PresentationPreservationError("This formatting change is not yet supported in fidelity mode.");
  }
  if (baseline.texts.length !== edited.texts.length) {
    throw new PresentationPreservationError("Adding or removing text paragraphs is not yet supported in fidelity mode.");
  }
  for (let index = 0; index < baseline.texts.length; index += 1) {
    if (!sameValue(textStyle(baseline.texts[index]), textStyle(edited.texts[index]))) {
      throw new PresentationPreservationError("Text formatting changes are not yet supported in fidelity mode.");
    }
    if (baseline.texts[index].text === edited.texts[index].text) {
      if (!sameValue(baseline.texts[index].runs, edited.texts[index].runs)) {
        throw new PresentationPreservationError("Rich-text run changes are not yet supported in fidelity mode.");
      }
    } else if (edited.texts[index].runs?.length) {
      throw new PresentationPreservationError("Finish the current rich-text edit before saving in fidelity mode.");
    }
  }
  const baselineCells = baseline.tableRows?.flat() || [];
  const editedCells = edited.tableRows?.flat() || [];
  if (baselineCells.length !== editedCells.length) {
    throw new PresentationPreservationError("Changing the table structure is not yet supported in fidelity mode.");
  }
  for (let index = 0; index < baselineCells.length; index += 1) {
    if (!sameValue(tableCellStyle(baselineCells[index]), tableCellStyle(editedCells[index]))) {
      throw new PresentationPreservationError("Table formatting changes are not yet supported in fidelity mode.");
    }
  }
}

export async function preservePresentationFile(
  original: ArrayBuffer,
  baselineSlides: PreservePresentationSlide[],
  editedSlides: PreservePresentationSlide[],
  fileName: string,
): Promise<File> {
  if (baselineSlides.length !== editedSlides.length) {
    throw new PresentationPreservationError("Adding or removing slides is not yet supported in fidelity mode.");
  }

  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(original);
  const presentationEntry = zip.file("ppt/presentation.xml");
  if (!presentationEntry) throw new PresentationPreservationError("The PPTX package has no presentation.xml part.");
  const size = parseSlideSize(await presentationEntry.async("text"));
  const contentTypesEntry = zip.file("[Content_Types].xml");
  if (!contentTypesEntry) throw new PresentationPreservationError("The PPTX package has no content-types part.");
  let contentTypesXml = await contentTypesEntry.async("text");

  for (let slideIndex = 0; slideIndex < baselineSlides.length; slideIndex += 1) {
    const baseline = baselineSlides[slideIndex];
    const edited = editedSlides[slideIndex];
    if (!baseline.sourcePart || baseline.sourcePart !== edited.sourcePart) {
      throw new PresentationPreservationError("Reordering slides is not yet supported in fidelity mode.");
    }
    if (!sameValue(slideUnsupportedProperties(baseline), slideUnsupportedProperties(edited))) {
      throw new PresentationPreservationError("Changing slide backgrounds or layout is not yet supported in fidelity mode.");
    }

    const baselineShapes = baseline.shapes.filter((shape) => shape.source?.editable);
    const editedShapes = edited.shapes.filter((shape) => shape.source?.editable);
    const addedShapes = edited.shapes.filter((shape) => !shape.source);
    const baselineLockedShapes = baseline.shapes.filter((shape) => shape.source && !shape.source.editable);
    const editedLockedShapes = edited.shapes.filter((shape) => shape.source && !shape.source.editable);
    if (!sameValue(baselineLockedShapes, editedLockedShapes)) {
      throw new PresentationPreservationError("Objects inherited from a group, layout, or master cannot be edited in fidelity mode.");
    }
    const baselineOrder = baselineShapes.map((shape) => `${shape.source!.kind}:${shape.source!.objectId}`);
    const editedOrder = editedShapes.map((shape) => `${shape.source!.kind}:${shape.source!.objectId}`);
    if (!sameValue(baselineOrder, editedOrder)) {
      throw new PresentationPreservationError("Adding, removing, grouping, or reordering objects is not yet supported in fidelity mode.");
    }
    if (addedShapes.some((shape) => shape.type !== "image" || !shape.imgUrl)) {
      throw new PresentationPreservationError("Only new images can currently be added in fidelity mode.");
    }

    const slideEntry = zip.file(baseline.sourcePart);
    if (!slideEntry) throw new PresentationPreservationError(`Missing slide part ${baseline.sourcePart}.`);
    let slideXml = await slideEntry.async("text");

    for (let shapeIndex = 0; shapeIndex < baselineShapes.length; shapeIndex += 1) {
      const originalShape = baselineShapes[shapeIndex];
      const editedShape = editedShapes[shapeIndex];
      const source = originalShape.source!;
      ensureCompatibleShapeChange(originalShape, editedShape);

      const transformChanged = !sameValue(
        [originalShape.x, originalShape.y, originalShape.w, originalShape.h, originalShape.rotation, originalShape.flipH, originalShape.flipV],
        [editedShape.x, editedShape.y, editedShape.w, editedShape.h, editedShape.rotation, editedShape.flipH, editedShape.flipV],
      );
      const cropChanged = !sameValue(originalShape.imgCrop, editedShape.imgCrop);
      const textChanged = !sameValue(originalShape.texts.map((text) => text.text), editedShape.texts.map((text) => text.text));
      const tableChanged = !sameValue(
        originalShape.tableRows?.map((row) => row.map((cell) => cell.text)),
        editedShape.tableRows?.map((row) => row.map((cell) => cell.text)),
      );

      if (transformChanged || cropChanged || textChanged || tableChanged) {
        slideXml = replaceSourceObject(slideXml, source, (element) => {
          let patched = transformChanged ? patchTransform(element, editedShape, size.width, size.height) : element;
          if (cropChanged) patched = patchImageCrop(patched, editedShape.imgCrop);
          if (textChanged) patched = patchTextParagraphs(patched, editedShape.texts);
          if (tableChanged && editedShape.tableRows) patched = patchTableCells(patched, editedShape.tableRows);
          return patched;
        });
      }

      if (originalShape.imgUrl !== editedShape.imgUrl) {
        if (!source.mediaPart || !editedShape.imgUrl) {
          throw new PresentationPreservationError("Adding or removing image relationships is not yet supported in fidelity mode.");
        }
        zip.file(source.mediaPart, await imageBytes(editedShape.imgUrl, imageMimeForPart(source.mediaPart)));
      }
    }

    if (addedShapes.length > 0) {
      const relationshipsPart = relationshipPartForSlide(baseline.sourcePart);
      const relationshipsEntry = zip.file(relationshipsPart);
      if (!relationshipsEntry) {
        throw new PresentationPreservationError(`Missing slide relationships part ${relationshipsPart}.`);
      }
      let relationshipsXml = await relationshipsEntry.async("text");
      for (const addedShape of addedShapes) {
        const image = await newImagePayload(addedShape.imgUrl!);
        const mediaPart = nextMediaPart(zip, image.extension);
        const relationshipId = nextRelationshipId(relationshipsXml);
        zip.file(mediaPart, image.bytes);
        contentTypesXml = ensureImageContentType(contentTypesXml, image.extension, image.mime);
        relationshipsXml = appendImageRelationship(relationshipsXml, relationshipId, mediaPart);
        const hyperlinkRelationshipId = addedShape.hyperlink ? nextRelationshipId(relationshipsXml) : undefined;
        if (hyperlinkRelationshipId && addedShape.hyperlink) {
          relationshipsXml = appendHyperlinkRelationship(relationshipsXml, hyperlinkRelationshipId, addedShape.hyperlink);
        }
        slideXml = appendPictureToSlide(slideXml, addedShape, relationshipId, hyperlinkRelationshipId, size.width, size.height);
      }
      zip.file(relationshipsPart, relationshipsXml);
    }

    zip.file(baseline.sourcePart, slideXml);

    if ((baseline.notes || "") !== (edited.notes || "")) {
      if (!baseline.notesPart || baseline.notesPart !== edited.notesPart) {
        throw new PresentationPreservationError("Adding speaker notes to this slide is not yet supported in fidelity mode.");
      }
      const notesEntry = zip.file(baseline.notesPart);
      if (!notesEntry) throw new PresentationPreservationError(`Missing notes part ${baseline.notesPart}.`);
      zip.file(baseline.notesPart, patchSpeakerNotes(await notesEntry.async("text"), edited.notes || ""));
    }
  }

  zip.file("[Content_Types].xml", contentTypesXml);

  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE", mimeType: PPTX_MIME });
  const safeName = fileName.toLowerCase().endsWith(".pptx") ? fileName : `${fileName.replace(/\.ppt$/i, "")}.pptx`;
  return new File([blob], safeName, { type: PPTX_MIME, lastModified: Date.now() });
}
