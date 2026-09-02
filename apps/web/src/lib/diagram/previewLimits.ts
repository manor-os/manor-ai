export const MAX_DIAGRAM_PREVIEW_BYTES = 5 * 1024 * 1024;
export const DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE = "Diagram source is too large to preview";

export function assertDiagramPreviewFileSize(fileSize?: number | null): void {
  if (
    typeof fileSize === "number"
    && Number.isFinite(fileSize)
    && fileSize > MAX_DIAGRAM_PREVIEW_BYTES
  ) {
    throw new Error(DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE);
  }
}

export function diagramPreviewSourceIsTooLarge(content: string): boolean {
  return content.length > MAX_DIAGRAM_PREVIEW_BYTES
    || new TextEncoder().encode(content).byteLength > MAX_DIAGRAM_PREVIEW_BYTES;
}

export function diagramPreviewTextFromFsRead(result: {
  content: string;
  encoding: string;
  size?: number | null;
}): string {
  assertDiagramPreviewFileSize(result.size);
  let content = result.content;
  if (result.encoding === "base64") {
    if (content.replace(/\s+/g, "").length > Math.ceil(MAX_DIAGRAM_PREVIEW_BYTES * 4 / 3) + 4) {
      throw new Error(DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE);
    }
    const binary = atob(content);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    assertDiagramPreviewFileSize(bytes.byteLength);
    content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } else if (result.encoding !== "utf-8") {
    throw new Error("Diagram source uses an unsupported text encoding");
  }
  if (diagramPreviewSourceIsTooLarge(content)) {
    throw new Error(DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE);
  }
  return content;
}

export async function readDiagramPreviewText(response: Response): Promise<string> {
  const contentLength = Number(response.headers.get("content-length") || 0);
  if (Number.isFinite(contentLength) && contentLength > MAX_DIAGRAM_PREVIEW_BYTES) {
    await response.body?.cancel();
    throw new Error(DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE);
  }

  if (!response.body) {
    const text = await response.text();
    if (diagramPreviewSourceIsTooLarge(text)) {
      throw new Error(DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE);
    }
    return text;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let bytesRead = 0;
  let text = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    bytesRead += value.byteLength;
    if (bytesRead > MAX_DIAGRAM_PREVIEW_BYTES) {
      await reader.cancel();
      throw new Error(DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE);
    }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}
