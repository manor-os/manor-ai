import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import {
  DEFAULT_AWAY_WINDOW_MS,
  MIN_AWAY_MS,
  awayWindowStart,
  shouldAdvanceAfterReturn,
} from "../src/pages/dashboard-away-window.mjs";

const NOW = Date.parse("2026-07-28T12:00:00.000Z");

/* ── The window ──────────────────────────────────────────────── */

test("a stored timestamp is the window start", () => {
  const seen = "2026-07-27T09:30:00.000Z";
  assert.equal(awayWindowStart(seen, NOW), seen);
});

test("a first-ever visit gets a default window, not nothing", () => {
  // The old code disabled the query when storage was empty, so a fresh
  // browser profile showed "All quiet" no matter what had happened.
  const start = awayWindowStart(null, NOW);
  assert.equal(Date.parse(start), NOW - DEFAULT_AWAY_WINDOW_MS);
});

test("unusable stored values fall back rather than hiding everything", () => {
  for (const stored of ["", "not-a-date", undefined]) {
    assert.equal(Date.parse(awayWindowStart(stored, NOW)), NOW - DEFAULT_AWAY_WINDOW_MS);
  }
});

test("a future timestamp does not blank the panel", () => {
  // A clock change or edited storage would otherwise exclude all activity.
  const future = new Date(NOW + 60 * 60 * 1000).toISOString();
  assert.equal(Date.parse(awayWindowStart(future, NOW)), NOW - DEFAULT_AWAY_WINDOW_MS);
});

/* ── Returning ───────────────────────────────────────────────── */

test("a real absence advances the window to when you left", () => {
  assert.equal(shouldAdvanceAfterReturn(NOW - MIN_AWAY_MS, NOW), true);
  assert.equal(shouldAdvanceAfterReturn(NOW - 60 * 60 * 1000, NOW), true);
});

test("glancing at another tab is not an absence", () => {
  // This is what collapsed the digest mid-read: any return re-read the
  // just-written timestamp and refetched with since≈now.
  assert.equal(shouldAdvanceAfterReturn(NOW - 3000, NOW), false);
});

test("never having left cannot advance anything", () => {
  assert.equal(shouldAdvanceAfterReturn(null, NOW), false);
});

/* ── The page wiring ─────────────────────────────────────────── */

const dashboardSource = await readFile(
  new URL("../src/pages/Dashboard.tsx", import.meta.url),
  "utf8",
);

test("arriving does not mark the dashboard seen", () => {
  // A 1.5s timer used to overwrite the timestamp right after mount, so the
  // window was consumed before the user could read it.
  assert.ok(
    !/setTimeout\(\s*markDashboardSeen/.test(dashboardSource),
    "marking seen on a timer destroys the window being displayed",
  );
});

test("window focus does not re-read the timestamp", () => {
  // Switching apps fires focus with no absence behind it.
  assert.ok(
    !/addEventListener\(\s*"focus"/.test(dashboardSource),
    "a focus handler re-applies the stored timestamp and empties the panel",
  );
});

test("the activity query is never disabled for want of a timestamp", () => {
  assert.ok(
    !/enabled:\s*Boolean\(dashboardLastSeenAt\)/.test(dashboardSource),
    "no stored timestamp must mean a default window, not a skipped query",
  );
});

test("leaving still marks the dashboard seen", () => {
  for (const marker of ['"visibilitychange"', '"pagehide"']) {
    assert.ok(
      dashboardSource.includes(marker),
      `${marker} must still record a departure`,
    );
  }
});
