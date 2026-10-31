/**
 * The parent side of the embedded customer-trial frame (see embedProtocol).
 * It answers only its own frame's READY from this origin, mints the scoped
 * token with the parent's own API client, and hands the frame that token and
 * a MessagePort, posting only to this origin. The parent's sign-in never
 * crosses into the frame.
 */
import { EMBED_READY, EMBED_SESSION, type FramePortMessage, type ParentPortMessage } from "./embedProtocol";
import type { EmbedKind } from "./embedContext";

export interface EmbeddedFrameOptions {
  /** The frame's window (iframe.contentWindow); messages from any other source are ignored. */
  frame: () => Window | null;
  kind: EmbedKind;
  refId: string;
  /** Mint a scoped session for this frame with the parent's own sign-in. */
  mint: () => Promise<{ access_token: string }>;
  /** Whether a failed mint means access ended (rather than a transient failure). */
  isEnded: (error: unknown) => boolean;
  onConnected?: () => void;
  onFailed?: (error: unknown) => void;
  onEnded: () => void;
  onClose: () => void;
  host?: Window;
}

export function connectEmbeddedFrame(options: EmbeddedFrameOptions): () => void {
  const host = options.host ?? window;
  let port: MessagePort | null = null;
  let servedNonce: string | null = null;
  let disposed = false;

  function listen(channel: MessagePort) {
    channel.onmessage = (event: MessageEvent<FramePortMessage>) => {
      if (disposed || channel !== port) return;
      const message = event.data;
      if (message?.type === "ended") options.onEnded();
      else if (message?.type === "close") options.onClose();
      else if (message?.type === "renew" && typeof message.id === "string") {
        const id = message.id;
        void options.mint().then(
          (session) => channel.postMessage({ type: "session", id, access_token: session.access_token } satisfies ParentPortMessage),
          (error) => channel.postMessage({ type: "renew-failed", id, ended: options.isEnded(error) } satisfies ParentPortMessage),
        );
      }
    };
  }

  const onMessage = (event: MessageEvent) => {
    const frame = options.frame();
    if (disposed || !frame || event.source !== frame || event.origin !== host.location.origin) return;
    const data = event.data as { type?: string; nonce?: unknown; kind?: unknown; ref?: unknown } | null;
    if (!data || data.type !== EMBED_READY || typeof data.nonce !== "string") return;
    if (data.kind !== options.kind || data.ref !== options.refId) return;
    // The frame repeats READY until served; a new nonce is a reloaded frame document.
    if (data.nonce === servedNonce) return;
    const nonce = data.nonce;
    servedNonce = nonce;
    port?.close();
    port = null;
    void options.mint().then((session) => {
      if (disposed || servedNonce !== nonce || options.frame() !== frame) return;
      const channel = new MessageChannel();
      port = channel.port1;
      listen(port);
      frame.postMessage({ type: EMBED_SESSION, nonce, access_token: session.access_token }, host.location.origin, [channel.port2]);
      options.onConnected?.();
    }, (error) => {
      if (!disposed && servedNonce === nonce) options.onFailed?.(error);
    });
  };

  host.addEventListener("message", onMessage);
  return () => {
    disposed = true;
    host.removeEventListener("message", onMessage);
    port?.close();
    port = null;
  };
}
