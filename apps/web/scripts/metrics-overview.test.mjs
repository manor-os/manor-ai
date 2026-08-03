#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const overviewSource = await readFile(
  new URL("../src/admin/pages/MetricsOverview.tsx", import.meta.url),
  "utf8",
);
const detailSource = await readFile(
  new URL("../src/admin/pages/MetricsDetail.tsx", import.meta.url),
  "utf8",
);
const formatSource = await readFile(
  new URL("../src/admin/lib/format.ts", import.meta.url),
  "utf8",
);
const appSource = await readFile(
  new URL("../src/admin/App.tsx", import.meta.url),
  "utf8",
);
const systemDetailSource = await readFile(
  new URL("../src/admin/pages/MetricsSystemDetail.tsx", import.meta.url),
  "utf8",
);
const trafficDetailSource = await readFile(
  new URL("../src/admin/pages/MetricsTrafficDetail.tsx", import.meta.url),
  "utf8",
);

test("MetricsOverview wires all four cards to their own query, plus the trend sparkline", () => {
  assert.match(overviewSource, /title: "Efficiency"/);
  assert.match(overviewSource, /title: "Discovery & Routing"/);
  assert.match(overviewSource, /title: "Reliability & Outcomes"/);
  assert.match(overviewSource, /title: "Economics"/);
  // Each card must fetch its own report — a shared query would mean one
  // area's failure silently blanks out the others.
  assert.match(overviewSource, /adminApi\.metricsEfficiency\(/);
  assert.match(overviewSource, /adminApi\.metricsDiscovery\(/);
  assert.match(overviewSource, /adminApi\.metricsReliability\(/);
  assert.match(overviewSource, /adminApi\.metricsEconomics\(/);
  assert.match(overviewSource, /adminApi\.metricsTrend\(/);
});

test("MetricsOverview degrades gracefully when a query fails or returns no data", () => {
  assert.match(overviewSource, /Data unavailable/);
  assert.match(overviewSource, /"no data"/);
});

test("both pages use the shared human-readable formatters", () => {
  // Raw "379.80s" / "$0.0000" read as broken (dogfooding feedback) — both
  // pages must go through the adaptive duration/cost formatters.
  assert.match(overviewSource, /from "\.\.\/lib\/format"/);
  assert.match(detailSource, /from "\.\.\/lib\/format"/);
  assert.match(overviewSource, /fmtDurationMs/);
  assert.match(detailSource, /fmtDurationMs\(b\.p99_duration_ms\)/);
  assert.match(detailSource, /fmtUsd\(b\.total_cost_usd\)/);
});

test("cost formatting distinguishes 'no cost data' (BYOK) from a real $0", () => {
  // BYOK calls never record cost_usd; the backend reports null and the
  // frontend must say "no cost data" — never render an all-zeros "$0.0000".
  // (The overview card's headline is now tokens; its cost sub-stat says
  // "cost not tracked (BYOK)" when null, and the fmtUsd fallback keeps the
  // "no cost data" wording for the detail table.)
  assert.match(formatSource, /"no cost data"/);
  assert.match(overviewSource, /cost not tracked \(BYOK\)/);
  assert.doesNotMatch(overviewSource, /BYOK calls don't record cost/);
});

test("Economics leads with token usage (BYOK included); cost is secondary", () => {
  // Product decision: token consumption is real for EVERY call — BYOK just
  // means the price isn't counted. The card's headline is total tokens...
  assert.match(overviewSource, /fmtTokens\(report\.total_tokens\)/);
  assert.match(overviewSource, /label: "tokens · 7d"/);
  assert.match(overviewSource, /tokens\/req/);
  // ...and its sparkline charts the trend's `tokens` (present for every
  // day with traffic), not the mostly-null BYOK cost `value`.
  assert.match(overviewSource, /trendValue: \(p\) => p\.tokens \?\? null/);
  // The formatter is adaptive ("842", "9.5k", "101k", "4.4M", "1.2B").
  assert.match(formatSource, /export function fmtTokens/);
});

test("MetricsDetail's economics table puts token columns before cost columns", () => {
  assert.match(detailSource, /fmtTokens\(report\.total_tokens\)/);
  assert.match(detailSource, /fmtTokens\(b\.total_tokens\)/);
  assert.match(detailSource, /fmtTokens\(b\.tokens_per_request\)/);
  const tokensIdx = detailSource.indexOf(">Total tokens</th>");
  const costIdx = detailSource.indexOf(">Total cost</th>");
  assert.ok(tokensIdx > -1 && costIdx > -1 && tokensIdx < costIdx);
  // The economics trend chart maps `tokens`, with an area-specific title.
  assert.match(detailSource, /p\.tokens as number/);
  assert.match(detailSource, /Tokens per day \(UTC\)/);
});

test("trend charts skip null points instead of plotting fake zero dips", () => {
  // A null trend day means "no data" (e.g. no recorded cost), not zero —
  // coercing with `p.value ?? 0` would draw a dishonest dip to $0.
  assert.match(overviewSource, /p\.value !== null/);
  assert.match(detailSource, /p\.value !== null/);
  assert.doesNotMatch(overviewSource, /p\.value \?\? 0/);
  assert.doesNotMatch(detailSource, /p\.value \?\? 0/);
  // With <2 real points, both pages show a muted empty-state line instead
  // of a dead flat sparkline.
  assert.match(overviewSource, /no trend data yet/);
  assert.match(detailSource, /no trend data yet/);
});

test("MetricsDetail exposes all four areas and the 7/30/90-day range picker", () => {
  assert.match(detailSource, /efficiency: "Efficiency"/);
  assert.match(detailSource, /discovery: "Discovery & Routing"/);
  assert.match(detailSource, /reliability: "Reliability & Outcomes"/);
  assert.match(detailSource, /economics: "Economics"/);
  assert.match(detailSource, /RANGE_OPTIONS = \[7, 30, 90\]/);
});

test("MetricsDetail's reliability source-filter chips read from tool_error_buckets, not buckets", () => {
  // Reliability's per-source rows live under `tool_error_buckets` (its
  // top-level `buckets`-shaped field doesn't exist) — a regression back to
  // plain `.buckets` would silently empty the source-filter chips for the
  // reliability area only.
  assert.match(
    detailSource,
    /\(sourceOptionsQuery\.data as \{ tool_error_buckets\?: \{ source: string \}\[\] \} \| undefined\)\?\.tool_error_buckets/,
  );
  assert.match(detailSource, /report\.tool_error_buckets\.map\(/);
});

test("MetricsDetail resets the source filter whenever the area changes", () => {
  assert.match(
    detailSource,
    /useEffect\(\(\) => \{\s*setSelectedSources\(\[\]\);\s*\}, \[validArea\]\);/,
  );
});

test("MetricsOverview adds the System and Traffic cards with their own queries", () => {
  assert.match(overviewSource, /title="System"/);
  assert.match(overviewSource, /title="Traffic"/);
  // Same isolation rule as the four config cards: each fetches its own
  // report so one endpoint's failure can't blank out the others.
  assert.match(overviewSource, /adminApi\.metricsSystem\(/);
  assert.match(overviewSource, /adminApi\.metricsTraffic\(/);
  // Their drill-downs are dedicated routes, NOT MetricsArea values.
  assert.match(overviewSource, /\/metrics\/system-resources/);
  assert.match(overviewSource, /navigate\("\/metrics\/traffic"\)/);
});

test("App.tsx registers the two new static metrics routes ahead of /metrics/:area", () => {
  assert.match(appSource, /path="\/metrics\/system-resources"/);
  assert.match(appSource, /path="\/metrics\/traffic"/);
  const systemIdx = appSource.indexOf('path="/metrics/system-resources"');
  const trafficIdx = appSource.indexOf('path="/metrics/traffic"');
  const dynamicIdx = appSource.indexOf('path="/metrics/:area"');
  assert.ok(dynamicIdx > -1);
  // Static-before-dynamic documents intent even though v7 ranks by
  // specificity regardless of declaration order.
  assert.ok(systemIdx < dynamicIdx && trafficIdx < dynamicIdx);
});

test("the new detail pages exist and call their respective endpoints", () => {
  assert.match(systemDetailSource, /adminApi\.metricsSystem\(/);
  assert.match(trafficDetailSource, /adminApi\.metricsTraffic\(/);
  // Both offer an hours-based range picker (not the 4-area days picker).
  assert.match(systemDetailSource, /hours: 168/);
  assert.match(trafficDetailSource, /hours: 720/);
});

test("traffic pages keep the null-vs-zero 5xx distinction", () => {
  // error_5xx_rate === null means ZERO traffic; 0.0 means traffic with no
  // 5xx — collapsing them would fabricate either errors or silence.
  assert.match(trafficDetailSource, /error_5xx_rate !== null/);
  assert.match(trafficDetailSource, /rate !== null \? `\$\{\(rate \* 100\)\.toFixed\(1\)\}%` : "—"/);
  assert.match(overviewSource, /error_5xx_rate !== null/);
  assert.match(overviewSource, /no traffic data/);
});

test("traffic charts zero-fill absent hours; system series keep filtering nulls", () => {
  // Opposite semantics: a missing traffic bucket is a GENUINE 0 (backend
  // emits no row for empty hours) so both traffic charts must zero-fill;
  // a system null is a collector gap ("no data") and must stay filtered —
  // zero-filling it would fabricate a dip.
  assert.match(overviewSource, /zeroFillTrafficHours\(/);
  assert.match(trafficDetailSource, /zeroFillTrafficHours\(/);
  assert.match(systemDetailSource, /\.filter\(\(p\) => p\[s\.key\] !== null\)/);
  assert.doesNotMatch(systemDetailSource, /zeroFillTrafficHours/);
});

test("Efficiency card surfaces the chat bucket's cache_hit_rate honestly", () => {
  // Per-source rates can't be averaged into a platform-wide number (no raw
  // numerators to weight by) — the card shows chat's own rate, labeled.
  assert.match(overviewSource, /cache_hit_rate/);
  assert.match(overviewSource, /chat cache hit/);
  assert.match(overviewSource, /chat\.cache_hit_rate !== null/);
});
