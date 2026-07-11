/**
 * useChatStream — reusable hook for SSE chat streaming with abort support.
 *
 * Encapsulates: streaming state, AbortController, stop handler, and the
 * common send-then-stream pattern used by all chat components.
 */
import { useState, useRef, useCallback } from "react";
import { processSSEStream, type SetMessages, type SetConvId } from "./chatStream";

export interface StreamOptions {
  setMessages: SetMessages;
  setCurrentConvId: SetConvId;
  currentConvId: string | undefined;
}

export interface RunStreamResult {
  aborted: boolean;
}

export function useChatStream() {
  const [streaming, setStreaming] = useState(false);
  const streamingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /**
   * Run a streaming request. Handles:
   * - Setting streaming state on/off
   * - Creating AbortController
   * - Processing SSE stream
   * - Catching abort vs real errors
   *
   * @param fetchFn - async function that returns the fetch Response
   * @param opts - setMessages, setCurrentConvId, currentConvId
   * @param onError - optional error handler (called for non-abort errors)
   * @returns { aborted: boolean }
   */
  const run = useCallback(async (
    fetchFn: () => Promise<Response>,
    opts: StreamOptions,
    onError?: () => void,
  ): Promise<RunStreamResult> => {
    setStreaming(true);
    streamingRef.current = true;
    const ac = new AbortController();
    abortRef.current = ac;

    try {
      const response = await fetchFn();
      await processSSEStream(
        response,
        { setMessages: opts.setMessages, setCurrentConvId: opts.setCurrentConvId },
        opts.currentConvId,
        ac.signal,
      );
      return { aborted: false };
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        return { aborted: true };
      }
      onError?.();
      return { aborted: false };
    } finally {
      abortRef.current = null;
      setStreaming(false);
      streamingRef.current = false;
    }
  }, []);

  return { streaming, streamingRef, stop, run };
}
