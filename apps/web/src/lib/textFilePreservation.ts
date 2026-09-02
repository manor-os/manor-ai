export type PreservedTextEncoding = "utf-8" | "utf-16le" | "utf-16be";

export interface PreservedTextFormat {
  encoding: PreservedTextEncoding;
  bom: boolean;
  safeToSave: boolean;
  dominantNewline: "\n" | "\r\n" | "\r";
  originalNewlines: Array<"\n" | "\r\n" | "\r">;
}

export interface DecodedTextFile {
  text: string;
  format: PreservedTextFormat;
}

export enum TextFileSaveStrategy {
  PreserveSource = "preserve-source",
  NormalizeUtf8 = "normalize-utf8",
}

export function textFileSaveStrategy(format: PreservedTextFormat): TextFileSaveStrategy {
  return format.safeToSave
    ? TextFileSaveStrategy.PreserveSource
    : TextFileSaveStrategy.NormalizeUtf8;
}

export function textFileFormatForSave(format: PreservedTextFormat): PreservedTextFormat {
  if (textFileSaveStrategy(format) === TextFileSaveStrategy.PreserveSource) return format;
  return {
    ...format,
    encoding: "utf-8",
    bom: false,
    safeToSave: true,
  };
}

function newlineMetadata(text: string) {
  const originalNewlines = (text.match(/\r\n|\r|\n/g) || []) as PreservedTextFormat["originalNewlines"];
  const counts = new Map<PreservedTextFormat["dominantNewline"], number>([["\n", 0], ["\r\n", 0], ["\r", 0]]);
  originalNewlines.forEach((newline) => counts.set(newline, (counts.get(newline) || 0) + 1));
  const dominantNewline = (["\r\n", "\n", "\r"] as const)
    .reduce((best, candidate) => (counts.get(candidate) || 0) > (counts.get(best) || 0) ? candidate : best, "\n");
  return { originalNewlines, dominantNewline };
}

function looksLikeUtf16(bytes: Uint8Array): "utf-16le" | "utf-16be" | null {
  const sampleLength = Math.min(bytes.length - (bytes.length % 2), 256);
  if (sampleLength < 8) return null;
  let evenZeros = 0;
  let oddZeros = 0;
  for (let index = 0; index < sampleLength; index += 2) {
    if (bytes[index] === 0) evenZeros += 1;
    if (bytes[index + 1] === 0) oddZeros += 1;
  }
  const pairs = sampleLength / 2;
  if (oddZeros / pairs > 0.35 && evenZeros / pairs < 0.1) return "utf-16le";
  if (evenZeros / pairs > 0.35 && oddZeros / pairs < 0.1) return "utf-16be";
  return null;
}

export function decodeTextFile(input: ArrayBuffer | Uint8Array): DecodedTextFile {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  let encoding: PreservedTextEncoding = "utf-8";
  let bom = false;
  let offset = 0;

  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    bom = true;
    offset = 3;
  } else if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
    encoding = "utf-16le";
    bom = true;
    offset = 2;
  } else if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
    encoding = "utf-16be";
    bom = true;
    offset = 2;
  } else {
    encoding = looksLikeUtf16(bytes) || "utf-8";
  }

  let safeToSave = true;
  let text = "";
  try {
    text = new TextDecoder(encoding, { fatal: true }).decode(bytes.subarray(offset));
  } catch {
    safeToSave = false;
    text = new TextDecoder(encoding).decode(bytes.subarray(offset));
  }
  const newline = newlineMetadata(text);
  return {
    text,
    format: {
      encoding,
      bom,
      safeToSave,
      dominantNewline: newline.dominantNewline,
      originalNewlines: newline.originalNewlines,
    },
  };
}

function restoreNewlines(editedText: string, baselineText: string, format: PreservedTextFormat): string {
  const normalized = editedText.replace(/\r\n|\r/g, "\n");
  const parts = normalized.split("\n");
  const baselineCount = (baselineText.match(/\r\n|\r|\n/g) || []).length;
  const editedCount = parts.length - 1;
  const perLine = baselineCount === editedCount && format.originalNewlines.length === editedCount
    ? format.originalNewlines
    : Array.from({ length: editedCount }, () => format.dominantNewline);
  return parts.map((part, index) => index < perLine.length ? `${part}${perLine[index]}` : part).join("");
}

function encodeUtf16(text: string, littleEndian: boolean): Uint8Array {
  const output = new Uint8Array(text.length * 2);
  for (let index = 0; index < text.length; index += 1) {
    const value = text.charCodeAt(index);
    output[index * 2] = littleEndian ? value & 0xff : value >>> 8;
    output[index * 2 + 1] = littleEndian ? value >>> 8 : value & 0xff;
  }
  return output;
}

export function encodeTextFile(
  editedText: string,
  baselineText: string,
  format: PreservedTextFormat,
): Uint8Array {
  const outputFormat = textFileFormatForSave(format);
  const restored = restoreNewlines(editedText, baselineText, outputFormat);
  const body = outputFormat.encoding === "utf-8"
    ? new TextEncoder().encode(restored)
    : encodeUtf16(restored, outputFormat.encoding === "utf-16le");
  const prefix = !outputFormat.bom
    ? new Uint8Array()
    : outputFormat.encoding === "utf-8"
      ? Uint8Array.of(0xef, 0xbb, 0xbf)
      : outputFormat.encoding === "utf-16le"
        ? Uint8Array.of(0xff, 0xfe)
        : Uint8Array.of(0xfe, 0xff);
  const output = new Uint8Array(prefix.length + body.length);
  output.set(prefix);
  output.set(body, prefix.length);
  return output;
}

export function textEncodingLabel(format: PreservedTextFormat | null): string {
  if (!format) return "UTF-8";
  const label = format.encoding === "utf-8" ? "UTF-8" : format.encoding === "utf-16le" ? "UTF-16 LE" : "UTF-16 BE";
  return format.bom ? `${label} BOM` : label;
}
