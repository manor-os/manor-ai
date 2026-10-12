// Object URLs whose bytes a save may still need after the view that showed
// them is gone. Presentation media and graphic previews are held as blob:
// URLs; leaving the editor revokes them, but the unmount flush (and any save
// already queued) reads those same images afterwards. Revoking only drops the
// URL — the Blob stays readable here for a grace period, and every reader goes
// through `readObjectUrlBlob` instead of `fetch(url)`.

export const RETAINED_OBJECT_URL_GRACE_MS = 10 * 60_000;

const retainedBlobs = new Map<string, Blob>();

export function createRetainedObjectUrl(blob: Blob): string {
  const url = URL.createObjectURL(blob);
  retainedBlobs.set(url, blob);
  return url;
}

/** Revoke now; keep the bytes readable for saves that are still on their way. */
export function releaseRetainedObjectUrls(
  urls: Iterable<string>,
  graceMs = RETAINED_OBJECT_URL_GRACE_MS,
): void {
  const released = [...urls];
  if (released.length === 0) return;
  released.forEach((url) => URL.revokeObjectURL(url));
  setTimeout(() => {
    released.forEach((url) => retainedBlobs.delete(url));
  }, graceMs);
}

export function retainedObjectUrlBlob(url: string): Blob | undefined {
  return retainedBlobs.get(url);
}

/** Bytes behind an image URL, including retained object URLs already revoked. */
export async function readObjectUrlBlob(url: string): Promise<Blob> {
  const retained = retainedBlobs.get(url);
  if (retained) return retained;
  const response = await fetch(url);
  if (!response.ok) throw new ObjectUrlReadError(response.status);
  return response.blob();
}

export class ObjectUrlReadError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Unable to read image (${status})`);
    this.name = "ObjectUrlReadError";
    this.status = status;
  }
}
