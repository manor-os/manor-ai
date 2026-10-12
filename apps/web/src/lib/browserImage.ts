const convertedImages = new WeakMap<Blob, Promise<Blob>>();
const heicBrands = new Set(["heic", "heix", "hevc", "hevx", "heim", "heis", "hevm", "hevs"]);

/** Check the container, since iPhone photos can arrive with a .png filename. */
export function hasHeicHeader(bytes: Uint8Array): boolean {
  const brand = (offset: number) => String.fromCharCode(...bytes.subarray(offset, offset + 4));
  if (bytes.length < 16 || brand(4) !== "ftyp") return false;
  const boxSize = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0);
  if (boxSize < 16) return false;
  for (let offset = 8; offset + 4 <= Math.min(boxSize, bytes.length); offset += 4) {
    if (offset !== 12 && heicBrands.has(brand(offset))) return true;
  }
  return false;
}

/** Decode locally; the server still validates and scans the resulting upload. */
export function browserImageBlob(blob: Blob): Promise<Blob> {
  const cached = convertedImages.get(blob);
  if (cached) return cached;
  const conversion = (async () => {
    const header = new Uint8Array(await blob.slice(0, 256).arrayBuffer());
    if (!hasHeicHeader(header)) return blob;
    const { heicTo } = await import("heic-to/csp");
    return heicTo({ blob, type: "image/png" });
  })();
  convertedImages.set(blob, conversion);
  conversion.catch(() => convertedImages.delete(blob));
  return conversion;
}

export async function prepareChatUploadFile(file: File): Promise<File> {
  const blob = await browserImageBlob(file);
  if (blob === file) return file;
  const name = `${file.name.replace(/\.[^.]+$/, "")}.png`;
  return new File([blob], name, { type: "image/png", lastModified: file.lastModified });
}
