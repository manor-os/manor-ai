export const DEFAULT_AWAY_WINDOW_MS: number;
export const MIN_AWAY_MS: number;

export function awayWindowStart(
  storedLastSeenAt: string | null | undefined,
  now: number,
): string;

export function shouldAdvanceAfterReturn(
  hiddenAt: number | null,
  now: number,
): boolean;
