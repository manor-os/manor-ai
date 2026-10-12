/**
 * Server-rendered presentation previews (`/documents/:id/slides`), shared by
 * the file viewer and the chat artifact viewer.
 *
 * A cold render (LibreOffice + pdftoppm, possibly queued behind other Office
 * renders) can take far longer than a warm cache hit, so the slide list gets a
 * long overall budget and treats "busy" (503) and "source changed" (409) as
 * retryable. Slide images are then fetched with bounded concurrency in a
 * caller-controlled priority order; one slide failing never discards the rest.
 */
import { runPreviewQueue } from "./previewQueue";

export interface ServerSlideEntry {
  index: number;
  url: string;
  width?: number;
  height?: number;
}

export interface ServerSlideList {
  slides: ServerSlideEntry[];
  total: number;
  version?: string;
  width?: number;
  height?: number;
}

/** Overall budget for one slide list request cycle, including busy retries. */
export const SERVER_SLIDE_LIST_BUDGET_MS = 150_000;
/** Per-attempt budget for one cached slide PNG. */
export const SERVER_SLIDE_IMAGE_TIMEOUT_MS = 20_000;
export const SERVER_SLIDE_IMAGE_ATTEMPTS = 2;
export const SERVER_SLIDE_SOURCE_CHANGED_RETRIES = 3;
export const SERVER_SLIDE_VERSION_REFRESHES = 2;

export class ServerSlidePreviewError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryAfterMs?: number;

  constructor(message: string, status: number, code?: string, retryAfterMs?: number) {
    super(message);
    this.name = "ServerSlidePreviewError";
    this.status = status;
    this.code = code;
    this.retryAfterMs = retryAfterMs;
  }
}

function abortError(message = "Preview cancelled"): DOMException {
  return new DOMException(message, "AbortError");
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function linkedController(signal: AbortSignal): { controller: AbortController; release: () => void } {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal.aborted) controller.abort();
  else signal.addEventListener("abort", abort, { once: true });
  return { controller, release: () => signal.removeEventListener("abort", abort) };
}

export function waitFor(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(abortError()); return; }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, Math.max(0, ms));
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function retryAfterMs(response: Response): number | undefined {
  const header = response.headers.get("Retry-After");
  if (!header) return undefined;
  const seconds = Number(header);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const date = Date.parse(header);
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : undefined;
}

async function responseError(response: Response, fallback: string): Promise<ServerSlidePreviewError> {
  let code: string | undefined;
  let message = fallback;
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (detail && typeof detail === "object") {
      code = typeof detail.code === "string" ? detail.code : undefined;
      if (typeof detail.message === "string") message = detail.message;
    } else if (typeof detail === "string") {
      message = detail;
    }
  } catch { /* non-JSON error body */ }
  return new ServerSlidePreviewError(message, response.status, code, retryAfterMs(response));
}

export function isRetryableSlideListError(error: unknown): boolean {
  if (!(error instanceof ServerSlidePreviewError)) return false;
  return error.status === 503
    || (error.status === 409 && error.code === "document_source_changed");
}

export function isSlideVersionGoneError(error: unknown): boolean {
  return error instanceof ServerSlidePreviewError
    && error.status === 404
    && error.code === "slide_preview_version_not_found";
}

export interface FetchServerSlideListOptions {
  headers: Record<string, string>;
  /** Listing endpoint; defaults to the document's `/slides`. Share links pass their own. */
  url?: string;
  signal: AbortSignal;
  budgetMs?: number;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  now?: () => number;
}

/**
 * Request the rendered slide list, retrying while the server is busy (503 +
 * Retry-After) or the source changed mid-render (409), within one budget.
 */
export async function fetchServerSlideList(
  docId: string,
  {
    headers,
    url = `/api/v1/documents/${docId}/slides`,
    signal,
    budgetMs = SERVER_SLIDE_LIST_BUDGET_MS,
    fetchImpl = fetch,
    sleep = waitFor,
    now = Date.now,
  }: FetchServerSlideListOptions,
): Promise<ServerSlideList> {
  const deadline = now() + budgetMs;
  const { controller, release } = linkedController(signal);
  const budgetTimer = setTimeout(() => controller.abort(), Math.max(0, budgetMs));
  let sourceChangedRetries = 0;
  try {
    for (let attempt = 0; ; attempt += 1) {
      let response: Response;
      try {
        response = await fetchImpl(url, {
          headers,
          signal: controller.signal,
        });
      } catch (error) {
        if (signal.aborted) throw abortError();
        if (controller.signal.aborted) throw new ServerSlidePreviewError("Slide rendering timed out", 504);
        throw error;
      }
      if (response.ok) return await response.json() as ServerSlideList;
      const error = await responseError(response, "Slide rendering failed");
      if (!isRetryableSlideListError(error)) throw error;
      if (error.status === 409 && ++sourceChangedRetries > SERVER_SLIDE_SOURCE_CHANGED_RETRIES) throw error;
      const delay = Math.min(10_000, Math.max(500, error.retryAfterMs ?? Math.min(8_000, 1_000 * 2 ** attempt)));
      if (now() + delay >= deadline) throw new ServerSlidePreviewError("Slide rendering timed out", 504, error.code);
      try {
        await sleep(delay, controller.signal);
      } catch {
        if (signal.aborted) throw abortError();
        throw new ServerSlidePreviewError("Slide rendering timed out", 504, error.code);
      }
    }
  } finally {
    clearTimeout(budgetTimer);
    release();
  }
}

export interface FetchServerSlideImageOptions {
  headers: Record<string, string>;
  signal: AbortSignal;
  timeoutMs?: number;
  attempts?: number;
  fetchImpl?: typeof fetch;
}

/** Fetch one slide PNG with its own timeout and a retry; never aborts siblings. */
export async function fetchServerSlideImage(
  slide: ServerSlideEntry,
  {
    headers,
    signal,
    timeoutMs = SERVER_SLIDE_IMAGE_TIMEOUT_MS,
    attempts = SERVER_SLIDE_IMAGE_ATTEMPTS,
    fetchImpl = fetch,
  }: FetchServerSlideImageOptions,
): Promise<Blob> {
  let lastError: unknown;
  for (let attempt = 0; attempt < Math.max(1, attempts); attempt += 1) {
    if (signal.aborted) throw abortError();
    const { controller, release } = linkedController(signal);
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetchImpl(`/api/v1${slide.url}`, { headers, signal: controller.signal });
      if (response.ok) return await response.blob();
      const error = await responseError(response, "Slide fetch failed");
      // A superseded version will not come back; let the caller refresh the list.
      if (isSlideVersionGoneError(error)) throw error;
      lastError = error;
    } catch (error) {
      if (signal.aborted) throw abortError();
      if (isSlideVersionGoneError(error)) throw error;
      lastError = error;
    } finally {
      clearTimeout(timer);
      release();
    }
  }
  throw lastError instanceof Error ? lastError : new Error("Slide fetch failed");
}

export interface LoadServerSlidePreviewOptions {
  docId: string;
  headers: Record<string, string>;
  signal: AbortSignal;
  /** A (re)issued slide list; slot state for the previous list must be reset. */
  onSlideList: (list: ServerSlideList) => void;
  /** One slide image arrived; the caller owns any object URL it creates. */
  onSlideImage: (index: number, blob: Blob) => void;
  /** The listed slide could not be fetched after retries. */
  onSlideFailed?: (index: number, error: unknown) => void;
  /** Priority order for the not-yet-started slides (e.g. active slide first). */
  pickNext?: (pending: ReadonlySet<number>) => number | undefined;
  listBudgetMs?: number;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export interface ServerSlidePreviewResult {
  list: ServerSlideList;
  failed: number[];
}

/**
 * Load the slide list and stream its images. If the rendered version is
 * invalidated mid-load (the file was edited), the list is re-requested a
 * bounded number of times instead of giving up on server rendering.
 */
export async function loadServerSlidePreview({
  docId,
  headers,
  signal,
  onSlideList,
  onSlideImage,
  onSlideFailed,
  pickNext,
  listBudgetMs,
  fetchImpl,
  sleep,
}: LoadServerSlidePreviewOptions): Promise<ServerSlidePreviewResult> {
  for (let refresh = 0; ; refresh += 1) {
    const list = await fetchServerSlideList(docId, { headers, signal, budgetMs: listBudgetMs, fetchImpl, sleep });
    if (signal.aborted) throw abortError();
    const slides = Array.isArray(list.slides) ? list.slides : [];
    onSlideList({ ...list, slides });
    if (slides.length === 0) return { list: { ...list, slides }, failed: [] };

    const { controller, release } = linkedController(signal);
    let versionGone = false;
    try {
      const result = await runPreviewQueue(slides.length, async (index, queueSignal) => {
        try {
          const blob = await fetchServerSlideImage(slides[index], { headers, signal: queueSignal, fetchImpl });
          if (queueSignal.aborted) return;
          onSlideImage(index, blob);
        } catch (error) {
          if (isSlideVersionGoneError(error)) {
            versionGone = true;
            controller.abort();
          }
          throw error;
        }
      }, controller.signal, { isolateFailures: true, pickNext });
      const failed = [...result.failed.keys()].sort((left, right) => left - right);
      failed.forEach((index) => onSlideFailed?.(index, result.failed.get(index)));
      return { list: { ...list, slides }, failed };
    } catch (error) {
      if (signal.aborted) throw abortError();
      if (!versionGone) throw error;
      if (refresh >= SERVER_SLIDE_VERSION_REFRESHES) {
        throw new ServerSlidePreviewError("Presentation kept changing while loading", 409, "document_source_changed");
      }
    } finally {
      release();
    }
  }
}

/**
 * Priority for slide fetches: the active slide, then its neighbours, then
 * visible thumbnails, then everything else in slide order.
 */
export function slidePriorityPicker(
  getActive: () => number,
  getVisible: () => ReadonlySet<number> = () => new Set<number>(),
): (pending: ReadonlySet<number>) => number | undefined {
  return (pending) => {
    const active = getActive();
    const visible = getVisible();
    let best: number | undefined;
    let bestScore = Number.POSITIVE_INFINITY;
    pending.forEach((index) => {
      const distance = Math.abs(index - active);
      let score: number;
      if (distance <= 1) score = distance;
      else if (visible.has(index)) score = 10 + distance;
      else score = 100_000 + index;
      if (score < bestScore || (score === bestScore && best !== undefined && index < best)) {
        best = index;
        bestScore = score;
      }
    });
    return best;
  };
}

/** Pixel aspect of a rendered slide (per-slide size first, then the list default). */
export function serverSlideAspect(list: Pick<ServerSlideList, "slides" | "width" | "height"> | null, index: number): number | null {
  if (!list) return null;
  const slide = list.slides?.[index];
  const width = slide?.width ?? list.width;
  const height = slide?.height ?? list.height;
  return width && height && width > 0 && height > 0 ? width / height : null;
}
