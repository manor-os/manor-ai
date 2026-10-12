// A revoked/replaced session must not receive a late poll, stream or resident read.
export function createPublicChatRequestFence() {
  let generation = 0;
  const active = new Set<AbortController>();
  return {
    begin() {
      const current = generation;
      const controller = new AbortController();
      active.add(controller);
      return {
        signal: controller.signal,
        isCurrent: () => current === generation && !controller.signal.aborted,
        release: () => active.delete(controller),
      };
    },
    invalidate() {
      generation++;
      for (const controller of active) controller.abort();
      active.clear();
    },
  };
}
export type PublicChatRequestFence = ReturnType<typeof createPublicChatRequestFence>;

export function residentStatusAllowsHistory(value: unknown, now = Date.now()): boolean {
  if (!value || typeof value !== "object" || !("verified" in value)) return false;
  if (value.verified === false) return true; // Server confirmed this is not a private binding.
  if (value.verified !== true || !("expires_at" in value) || typeof value.expires_at !== "string") return false;
  const expires = Date.parse(value.expires_at);
  return Number.isFinite(expires) && expires > now;
}
