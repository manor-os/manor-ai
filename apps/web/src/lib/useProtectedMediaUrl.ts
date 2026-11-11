import { useEffect, useState } from "react";
import { fetchProtectedFsResponse, isLocalFsUrl } from "./api";

export interface ProtectedMedia {
  /** What an img/video/audio element can load: the address itself, or a local object URL. */
  src: string | null;
  failed: boolean;
}

/**
 * A configured picture, loop or sample the browser can load.
 *
 * Platform pictures (`/blueprints/...`) and https links load as they are. A file in
 * the entity's own storage (`/api/v1/fs/...`: a Workspace upload, or a file a
 * Blueprint package carried in) needs the signed-in user's token, which an
 * img/video element cannot send, so it is fetched and shown from an object URL.
 */
export function useProtectedMediaUrl(url: string | null | undefined): ProtectedMedia {
  const protectedUrl = url && isLocalFsUrl(url) ? url : null;
  const [loaded, setLoaded] = useState<{ url: string; src: string | null; failed: boolean } | null>(null);

  useEffect(() => {
    if (!protectedUrl) return;
    const controller = new AbortController();
    let objectUrl = "";
    void fetchProtectedFsResponse(protectedUrl, { signal: controller.signal })
      .then((response) => response.blob())
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        setLoaded({ url: protectedUrl, src: objectUrl, failed: false });
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoaded({ url: protectedUrl, src: null, failed: true });
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [protectedUrl]);

  if (!url) return { src: null, failed: false };
  if (!protectedUrl) return { src: url, failed: false };
  return loaded?.url === protectedUrl ? { src: loaded.src, failed: loaded.failed } : { src: null, failed: false };
}
