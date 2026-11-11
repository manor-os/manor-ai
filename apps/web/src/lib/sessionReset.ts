/**
 * What must be forgotten when the signed-in user changes. A store registers
 * itself when it loads, so the auth store (on every page, including login)
 * does not have to load the chat code just to clear it.
 */
const resets = new Set<() => void>();

export function onSessionReset(reset: () => void): void {
  resets.add(reset);
}

export function runSessionResets(): void {
  for (const reset of resets) reset();
}
