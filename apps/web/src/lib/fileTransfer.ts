/** A slow download may keep running while bytes arrive; only inactivity expires it. */
export async function fetchFileBlob(
  url: string,
  init: RequestInit = {},
  fetchFile: (url: string, init: RequestInit) => Promise<Response> = fetch,
  readBody: (response: Response) => Promise<Blob> = (response) => response.blob(),
): Promise<{ blob: Blob; headers: Headers }> {
  init.signal?.throwIfAborted();
  const controller = new AbortController();
  const abort = () => controller.abort(init.signal?.reason);
  init.signal?.addEventListener("abort", abort, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const refreshDeadline = () => {
    clearTimeout(timer);
    timer = setTimeout(() => controller.abort(new Error("File download stalled for 60 seconds. Please retry.")), 60_000);
  };
  let rejectAbort: () => void = () => {};
  const aborted = new Promise<never>((_, reject) => {
    rejectAbort = () => reject(controller.signal.reason);
    controller.signal.addEventListener("abort", rejectAbort, { once: true });
  });
  try {
    refreshDeadline();
    const response = await Promise.race([fetchFile(url, { ...init, signal: controller.signal }), aborted]);
    controller.signal.throwIfAborted();
    if (!response.ok) throw new Error(`File loading failed (HTTP ${response.status}). Please retry.`);
    refreshDeadline();
    reader = response.body?.getReader();
    const source = reader;
    const body = source ? new ReadableStream<Uint8Array>({
      async pull(stream) {
        try {
          const { done, value } = await source.read();
          controller.signal.throwIfAborted();
          if (done) stream.close();
          else {
            if (value.byteLength) refreshDeadline();
            stream.enqueue(value);
          }
        } catch (cause) { stream.error(cause); }
      },
      cancel: (reason) => source.cancel(reason),
    }) : null;
    const blob = await Promise.race([readBody(new Response(body, { headers: response.headers })), aborted]);
    controller.signal.throwIfAborted();
    return { blob, headers: response.headers };
  } finally {
    clearTimeout(timer);
    init.signal?.removeEventListener("abort", abort);
    controller.signal.removeEventListener("abort", rejectAbort);
    // Cancel reads even if a transport/body reader ignores AbortSignal.
    if (reader) void reader.cancel(controller.signal.reason).catch(() => {}).finally(() => reader?.releaseLock());
  }
}
