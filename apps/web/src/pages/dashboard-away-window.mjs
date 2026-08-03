/**
 * "While You Were Away" — deciding which window of activity to show.
 *
 * The panel asks the API for everything that happened since you last saw the
 * dashboard. That only works if "last saw" advances when you actually leave.
 * It used to advance 1.5 seconds after the page mounted, and the first window
 * `focus` event copied that new value into the query — so the digest you had
 * come to read blanked out within seconds of arriving, and a fresh browser
 * profile never saw anything at all (no stored timestamp meant the query was
 * disabled outright).
 *
 * The rules here:
 *   - a visit gets one window, fixed at mount, so reading it cannot erase it;
 *   - the stored timestamp advances only on a real departure — the tab going
 *     hidden, the page unloading, navigating off the dashboard;
 *   - coming back after a real absence advances the window to when you left;
 *   - with no stored timestamp we show a default window rather than nothing.
 */

/** How far back a first-ever visit looks. */
export const DEFAULT_AWAY_WINDOW_MS = 24 * 60 * 60 * 1000;

/**
 * How long the tab must stay hidden before returning counts as having been
 * away. Glancing at another tab is not an absence, and treating it as one is
 * what made the panel collapse mid-read.
 */
export const MIN_AWAY_MS = 5 * 60 * 1000;

/**
 * The `since` to query for this visit.
 *
 * @param {string | null | undefined} storedLastSeenAt ISO timestamp from storage
 * @param {number} now epoch ms
 * @returns {string} ISO timestamp
 */
export function awayWindowStart(storedLastSeenAt, now) {
  const parsed = storedLastSeenAt ? Date.parse(storedLastSeenAt) : NaN;
  if (!Number.isFinite(parsed)) {
    return new Date(now - DEFAULT_AWAY_WINDOW_MS).toISOString();
  }
  // A timestamp from the future (clock change, edited storage) would hide
  // everything; fall back rather than show an empty panel.
  if (parsed > now) {
    return new Date(now - DEFAULT_AWAY_WINDOW_MS).toISOString();
  }
  return new Date(parsed).toISOString();
}

/**
 * Whether returning to a still-mounted dashboard should move the window
 * forward to when the tab went hidden.
 *
 * @param {number | null} hiddenAt epoch ms the tab went hidden, null if it never did
 * @param {number} now epoch ms
 */
export function shouldAdvanceAfterReturn(hiddenAt, now) {
  if (hiddenAt === null || !Number.isFinite(hiddenAt)) return false;
  return now - hiddenAt >= MIN_AWAY_MS;
}
