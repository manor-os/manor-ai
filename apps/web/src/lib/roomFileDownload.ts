import { fetchCachedFile } from "./persistentFileCache";

/** Room file IDs identify immutable shared snapshots. */
export async function downloadRoomFile(
  url: string,
  options: { signal?: AbortSignal; force?: boolean },
  fetchFile: (url: string, init: RequestInit) => Promise<Response>,
): Promise<Blob> {
  return (await fetchCachedFile(url, options, fetchFile)).blob();
}
