/**
 * Messages between the provider's page (parent) and the embedded customer
 * trial frame. Both are this origin; every window message is posted with
 * targetOrigin = location.origin and checked for origin and source.
 *
 * frame → parent (window): READY { nonce, kind, ref }
 * parent → frame (window, with a MessagePort): SESSION { nonce, access_token }
 * frame → parent (port): { type: "renew", id } | { type: "ended" } | { type: "close" }
 * parent → frame (port): { type: "session", id, access_token } | { type: "renew-failed", id, ended }
 *
 * The parent's own sign-in never crosses: it mints the scoped token with its
 * own API client and sends only that token.
 */
export const EMBED_READY = "manor:embed-ready";
export const EMBED_SESSION = "manor:embed-session";

export type FramePortMessage = { type: "renew"; id: string } | { type: "ended" } | { type: "close" };
export type ParentPortMessage =
  | { type: "session"; id: string; access_token: string }
  | { type: "renew-failed"; id: string; ended: boolean };

export function randomId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
