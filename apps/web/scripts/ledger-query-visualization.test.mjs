import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { build } from "esbuild";

const visualizationSource = await readFile(
  new URL("../src/lib/workspaceLedgerVisualization.ts", import.meta.url),
  "utf8",
);
const assistantBlocksSource = await readFile(
  new URL("../src/components/AssistantMessageBlocks.tsx", import.meta.url),
  "utf8",
);
const chatStreamSource = await readFile(
  new URL("../src/lib/chatStream.ts", import.meta.url),
  "utf8",
);
const responseSurfaceSource = await readFile(
  new URL("../src/lib/responseSurface.ts", import.meta.url),
  "utf8",
);
const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const previewFrameSource = await readFile(
  new URL("../src/components/ui/IsolatedHtmlPreviewFrame.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);

const visualizationBundle = await build({
  stdin: {
    contents: `
      export {
        coalesceWorkspaceLedgerQueryBlocks,
        isWorkspaceLedgerQueryVisualization,
        workspaceLedgerQueryFrameHeights,
        workspaceLedgerQueryHtml,
        workspaceLedgerTimelineTimestamp,
      }
        from "../src/lib/workspaceLedgerVisualization.ts";
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const visualizationModuleUrl = `data:text/javascript;base64,${Buffer.from(
  visualizationBundle.outputFiles[0].text,
).toString("base64")}`;
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
};
const {
  coalesceWorkspaceLedgerQueryBlocks,
  isWorkspaceLedgerQueryVisualization,
  workspaceLedgerQueryFrameHeights,
  workspaceLedgerQueryHtml,
  workspaceLedgerTimelineTimestamp,
} = await import(visualizationModuleUrl);

function queryData(overrides = {}) {
  return {
    contract_id: "manor.relationship_ledger/v1",
    view: "current",
    layout: "bar",
    matched_count: 5,
    as_of: "2026-08-23T19:36:00Z",
    group_by: ["stage"],
    metrics: ["count"],
    aggregates: { count: 5 },
    groups: [
      { key: { stage: "contacted" }, aggregates: { count: 3 } },
      { key: { stage: "discovery" }, aggregates: { count: 2 } },
    ],
    group_count: 2,
    groups_truncated: false,
    columns: [],
    rows: [],
    has_more: false,
    currency: null,
    ...overrides,
  };
}

test("query_ledger results render as bounded host-owned HTML", () => {
  assert.match(chatStreamSource, /kind: "ledger_query_result"/);
  assert.match(assistantBlocksSource, /isWorkspaceLedgerQueryVisualization/);
  assert.match(assistantBlocksSource, /workspace\.ledger\.query/);
  assert.match(responseSurfaceSource, /workspaceLedgerQueryHtml\(render\.props as any, theme\)/);
  assert.match(visualizationSource, /data-visualization="workspace-ledger-query"/);
  assert.match(visualizationSource, /"metrics", "bar", "timeline", "table", "empty"/);
  assert.match(visualizationSource, /value\.groups\.length > 24/);
  assert.match(visualizationSource, /value\.rows\.length > 20/);
  assert.match(visualizationSource, /value\.columns\.length > 10/);
  assert.match(visualizationSource, /query_fingerprint/);
  assert.match(visualizationSource, /groups_truncated/);
  assert.match(visualizationSource, /escapeHtml/);
  assert.doesNotMatch(assistantBlocksSource, /dangerouslySetInnerHTML/);
  assert.doesNotMatch(visualizationSource, /<script/);
});

test("truncated Ledger groups are disclosed separately from row pagination", () => {
  const html = workspaceLedgerQueryHtml(queryData({
    group_count: 30,
    groups_truncated: true,
    has_more: false,
  }));
  assert.match(html, /Showing 2 of 30 groups/);
  assert.doesNotMatch(html, /More matching records are available/);
});

test("Workspace Chat renders streamed Ledger blocks before history refresh", () => {
  assert.match(workspaceChatSource, /const localAssistantBlocks =/);
  assert.match(
    workspaceChatSource,
    /localAssistantBlocks \? \([\s\S]*?<AssistantMessageBlocks[\s\S]*?streaming=\{isStreamingAssistant\}/,
  );
  assert.match(
    workspaceChatSource,
    /!localAssistantBlocks && localTools\.length > 0/,
  );
});

test("historical Ledger table subsets coalesce without hiding distinct queries", () => {
  const rows = Array.from({ length: 6 }, (_, index) => ({
    occurred_at: `2026-08-${index + 1}T10:00:00Z`,
    entry_type: index < 2 ? "income" : "expense",
    status: "posted",
    direction: index < 2 ? "inflow" : "outflow",
    currency: "CNY",
    amount_minor: (index + 1) * 100,
  }));
  const block = (id, selectedRows, queryOverrides = {}) => ({
    id,
    type: "visualization",
    kind: "ledger_query_result",
    data: queryData({
      contract_id: "manor.finance_ledger/v1",
      view: "events",
      layout: "table",
      matched_count: selectedRows.length,
      group_by: [],
      metrics: [],
      aggregates: {},
      groups: [],
      columns: [
        "occurred_at", "entry_type", "status", "direction", "currency", "amount_minor",
      ],
      rows: selectedRows,
      ...queryOverrides,
    }),
  });
  const opening = { id: "text-1", type: "text", phase: "opening", text: "Checking." };
  const fullThenSubset = coalesceWorkspaceLedgerQueryBlocks([
    opening,
    block("visual-1", rows),
    block("visual-2", rows.slice(0, 3)),
  ]);
  assert.equal(fullThenSubset.length, 2);
  assert.equal(fullThenSubset[1].id, "visual-1");
  assert.equal(fullThenSubset[1].data.rows.length, 6);

  const subsetThenFull = coalesceWorkspaceLedgerQueryBlocks([
    block("visual-1", rows.slice(0, 3)),
    block("visual-2", rows),
  ]);
  assert.equal(subsetThenFull.length, 1);
  assert.equal(subsetThenFull[0].id, "visual-1");
  assert.equal(subsetThenFull[0].data.rows.length, 6);

  const distinct = coalesceWorkspaceLedgerQueryBlocks([
    block("visual-1", rows.slice(0, 2)),
    block("visual-2", rows.slice(2, 4)),
  ]);
  assert.equal(distinct.length, 2);

  const overlappingButDistinct = coalesceWorkspaceLedgerQueryBlocks([
    block("visual-1", rows, { query_fingerprint: "a".repeat(64) }),
    block("visual-2", rows.slice(0, 2), { query_fingerprint: "b".repeat(64) }),
  ]);
  assert.equal(overlappingButDistinct.length, 2);

  const explicitlyCoalesced = coalesceWorkspaceLedgerQueryBlocks([
    block("visual-1", rows, { query_fingerprint: "a".repeat(64) }),
    block("visual-2", rows.slice(0, 2), {
      query_fingerprint: "b".repeat(64),
      coalesce_previous_visualization: true,
    }),
  ]);
  assert.equal(explicitlyCoalesced.length, 1);
  assert.equal(explicitlyCoalesced[0].data.rows.length, 6);
  assert.match(assistantBlocksSource, /coalesceWorkspaceLedgerQueryBlocks\(sanitized\)/);
});

test("explicit Ledger coalescing targets only the latest compatible query", () => {
  const rows = Array.from({ length: 5 }, (_, index) => ({
    occurred_at: `2026-08-${index + 1}T10:00:00Z`,
    direction: "outflow",
    currency: "CNY",
    amount_minor: (index + 1) * 100,
  }));
  const block = (
    id,
    selectedRows,
    fingerprint,
    coalescePrevious = false,
    queryOverrides = {},
  ) => ({
    id,
    type: "visualization",
    kind: "ledger_query_result",
    data: queryData({
      contract_id: "manor.finance_ledger/v1",
      view: "events",
      layout: "table",
      matched_count: selectedRows.length,
      group_by: [],
      metrics: [],
      aggregates: {},
      groups: [],
      columns: ["occurred_at", "direction", "currency", "amount_minor"],
      rows: selectedRows,
      query_fingerprint: fingerprint,
      coalesce_previous_visualization: coalescePrevious,
      ...queryOverrides,
    }),
  });

  const coalesced = coalesceWorkspaceLedgerQueryBlocks([
    block("visual-1", rows, "a".repeat(64)),
    block("visual-2", rows.slice(0, 1), "b".repeat(64)),
    block("visual-3", rows.slice(0, 2), "c".repeat(64), true),
  ]);

  assert.deepEqual(coalesced.map((item) => item.data.rows.length), [5, 2]);
  assert.deepEqual(coalesced.map((item) => item.id), ["visual-1", "visual-2"]);

  const disjoint = coalesceWorkspaceLedgerQueryBlocks([
    block("visual-1", rows.slice(0, 1), "d".repeat(64)),
    block("visual-2", rows.slice(3, 4), "e".repeat(64), true),
  ]);
  assert.equal(disjoint.length, 1);
  assert.equal(disjoint[0].id, "visual-1");
  assert.deepEqual(disjoint[0].data.rows, rows.slice(3, 4));

  const acrossLayouts = coalesceWorkspaceLedgerQueryBlocks([
    block("visual-1", [], "f".repeat(64), false, { layout: "empty" }),
    block("visual-2", rows.slice(0, 1), "0".repeat(64), true),
  ]);
  assert.equal(acrossLayouts.length, 1);
  assert.equal(acrossLayouts[0].id, "visual-1");
  assert.equal(acrossLayouts[0].data.layout, "table");
});

test("ledger timeline ordering uses raw timestamps instead of localized labels", () => {
  const january = workspaceLedgerTimelineTimestamp({
    recorded_at: "2026-01-20T12:00:00Z",
  });
  const february = workspaceLedgerTimelineTimestamp({
    recorded_at: "2026-02-01T12:00:00Z",
  });

  assert.equal(typeof january, "number");
  assert.equal(typeof february, "number");
  assert.ok(january < february);
});

test("Ledger query HTML selects business-specific host presentations", () => {
  const financeSummary = workspaceLedgerQueryHtml(queryData({
    contract_id: "manor.finance_ledger/v1",
    layout: "metrics",
    group_by: [],
    matched_count: 6,
    metrics: ["sum_inflow_minor", "sum_outflow_minor"],
    aggregates: { sum_inflow_minor: 128_500_00, sum_outflow_minor: 42_500_00 },
    groups: [],
    currency: "CNY",
  }));
  assert.match(financeSummary, /data-kind="finance"/);
  assert.match(financeSummary, /data-presentation="finance-summary"/);
  assert.match(financeSummary, /Net Cash Flow/);
  assert.deepEqual(workspaceLedgerQueryFrameHeights(queryData({
    contract_id: "manor.finance_ledger/v1",
    layout: "metrics",
    group_by: [],
    metrics: ["sum_inflow_minor", "sum_outflow_minor"],
    aggregates: { sum_inflow_minor: 128_500_00, sum_outflow_minor: 42_500_00 },
    groups: [],
    currency: "CNY",
  })), { desktop: 340, mobile: 470 });

  const financeComparison = workspaceLedgerQueryHtml(queryData({
    contract_id: "manor.finance_ledger/v1",
    group_by: ["direction"],
    groups: [
      { key: { direction: "inflow" }, aggregates: { count: 2 } },
      { key: { direction: "outflow" }, aggregates: { count: 4 } },
    ],
  }));
  assert.match(financeComparison, /data-presentation="finance-comparison"/);
  assert.match(financeComparison, /data-direction="inflow"/);
  assert.match(financeComparison, /data-direction="outflow"/);

  const recruitingPipeline = workspaceLedgerQueryHtml(queryData({
    contract_id: "manor.recruiting_ledger/v1",
    groups: [
      { key: { stage: "offer" }, aggregates: { count: 1 } },
      { key: { stage: "screening" }, aggregates: { count: 4 } },
    ],
  }));
  assert.match(recruitingPipeline, /data-presentation="stage-pipeline"/);
  assert.ok(recruitingPipeline.indexOf("Screening") < recruitingPipeline.indexOf("Offer"));

  const relationshipPipeline = workspaceLedgerQueryHtml(queryData());
  assert.match(relationshipPipeline, /data-kind="relationship"/);
  assert.match(relationshipPipeline, /data-presentation="stage-pipeline"/);

  const contentStatus = workspaceLedgerQueryHtml(queryData({
    contract_id: "manor.content_ledger/v1",
    group_by: ["status"],
    groups: [
      { key: { status: "draft" }, aggregates: { count: 3 } },
      { key: { status: "published" }, aggregates: { count: 2 } },
    ],
  }));
  assert.match(contentStatus, /data-presentation="status-board"/);

  const financeTransactions = workspaceLedgerQueryHtml(queryData({
    contract_id: "manor.finance_ledger/v1",
    layout: "table",
    matched_count: 2,
    group_by: [],
    metrics: [],
    aggregates: {},
    groups: [],
    columns: [
      "entry_id", "occurred_at", "entry_type", "status", "direction",
      "currency", "amount_minor", "account_ref", "counterparty_ref", "memo",
    ],
    rows: [
      {
        entry_id: "finance-1",
        occurred_at: "2026-08-12T12:00:00Z",
        entry_type: "expense",
        status: "posted",
        direction: "outflow",
        currency: "CNY",
        amount_minor: 2800000,
        account_ref: "marketing",
        counterparty_ref: "Ad vendor",
        memo: "August launch campaign",
      },
      {
        entry_id: "finance-2",
        occurred_at: "2026-08-15T12:00:00Z",
        entry_type: "income",
        status: "paid",
        direction: "inflow",
        currency: "CNY",
        amount_minor: 6800000,
        account_ref: "sales",
        counterparty_ref: "Client A",
        memo: "<unsafe>",
      },
    ],
    currency: "CNY",
  }));
  assert.match(financeTransactions, /data-presentation="finance-transactions"/);
  assert.match(financeTransactions, /August launch campaign/);
  assert.match(financeTransactions, /marketing → Ad vendor/);
  assert.match(financeTransactions, /finance-1/);
  assert.doesNotMatch(financeTransactions, /<table>/);
  assert.doesNotMatch(financeTransactions, /<unsafe>/);
  assert.deepEqual(workspaceLedgerQueryFrameHeights(queryData({
    contract_id: "manor.finance_ledger/v1",
    layout: "table",
    group_by: [],
    metrics: [],
    rows: [{ entry_id: "finance-1" }, { entry_id: "finance-2" }],
  })), { desktop: 320, mobile: 340 });
});

test("Ledger query HTML composes line, donut, projection, and profile reports", () => {
  const trendData = queryData({
    contract_id: "manor.recruiting_ledger/v1",
    layout: "timeline",
    group_by: ["occurred_at"],
    date_granularity: "month",
    groups: [
      { key: { occurred_at: "2026-05-01T00:00:00Z" }, aggregates: { count: 2 } },
      { key: { occurred_at: "2026-06-01T00:00:00Z" }, aggregates: { count: 4 } },
      { key: { occurred_at: "2026-07-01T00:00:00Z" }, aggregates: { count: 7 } },
    ],
    group_count: 3,
    presentation: {
      version: 1,
      title: "Hiring <forecast>",
      subtitle: "Observed pipeline and expected development",
      sections: [
        { type: "chart", chart: "line", metric: "count", title: "Trend" },
        { type: "chart", chart: "donut", metric: "count", title: "Share" },
        { type: "projection", metric: "count", forecast_periods: 3, title: "Expected" },
      ],
    },
  });
  const html = workspaceLedgerQueryHtml(trendData);

  assert.equal(isWorkspaceLedgerQueryVisualization(trendData), true);
  assert.match(html, /data-composed="true"/);
  assert.match(html, /data-presentation="line-chart"/);
  assert.match(html, /data-presentation="donut-chart"/);
  assert.match(html, /data-presentation="projection-report"/);
  assert.match(html, /Linear trend estimate/);
  assert.match(html, /Hiring &lt;forecast&gt;/);
  assert.doesNotMatch(html, /Hiring <forecast>/);
  assert.ok(workspaceLedgerQueryFrameHeights(trendData).desktop > 600);

  const profileData = queryData({
    contract_id: "manor.recruiting_ledger/v1",
    layout: "table",
    matched_count: 2,
    group_by: [],
    groups: [],
    columns: [
      "display_name", "role_title", "department", "stage", "status",
      "event", "occurred_at", "channel",
    ],
    rows: [
      {
        display_name: "Alex Chen",
        role_title: "Product Designer",
        department: "Product",
        stage: "interview",
        status: "active",
        event: "panel_interview",
        occurred_at: "2026-08-23T10:00:00Z",
        channel: "referral",
      },
      {
        display_name: "Alex Chen",
        role_title: "Product Designer",
        department: "Product",
        stage: "screening",
        status: "active",
        event: "screen_completed",
        occurred_at: "2026-08-20T10:00:00Z",
        channel: "referral",
      },
    ],
    presentation: {
      version: 1,
      title: "Alex Chen candidate report",
      sections: [{ type: "profile", title: "Candidate" }],
    },
  });
  const profileHtml = workspaceLedgerQueryHtml(profileData);
  assert.match(profileHtml, /data-presentation="personal-report"/);
  assert.match(profileHtml, /Alex Chen/);
  assert.match(profileHtml, /Panel Interview/);
});

test("Ledger composed presentation validation stays bounded", () => {
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    presentation: {
      version: 1,
      sections: Array.from({ length: 5 }, () => ({ type: "metrics" })),
    },
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    presentation: {
      version: 1,
      sections: [{ type: "chart", chart: "javascript" }],
    },
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    contract_id: "manor.recruiting_ledger/v1",
    metrics: ["sum_amount_minor"],
    aggregates: { sum_amount_minor: 0 },
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    contract_id: "manor.recruiting_ledger/v1",
    group_by: ["occurred_at", "stage"],
    date_granularity: "day",
    presentation: {
      version: 1,
      sections: [{ type: "projection", metric: "count" }],
    },
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    layout: ["bar"],
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    group_by: ["occurred_at"],
    date_granularity: ["day"],
    presentation: {
      version: 1,
      sections: [{ type: "projection", metric: "count" }],
    },
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    presentation: {
      version: 1,
      sections: [{ type: ["chart"], chart: "line", metric: "count" }],
    },
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    presentation: {
      version: 1,
      sections: [{ type: "chart", chart: "line", metric: ["count"] }],
    },
  })), false);
  assert.equal(isWorkspaceLedgerQueryVisualization(queryData({
    presentation: {
      version: 1,
      sections: [{ type: "metrics", metrics: [["count"]] }],
    },
  })), false);
});

test("count projections use real time spacing and never display negative values", () => {
  const projection = (values, dates) => workspaceLedgerQueryHtml(queryData({
    contract_id: "manor.recruiting_ledger/v1",
    layout: "timeline",
    group_by: ["occurred_at"],
    date_granularity: "day",
    matched_count: values.reduce((sum, value) => sum + value, 0),
    groups: values.map((value, index) => ({
      key: { occurred_at: dates[index] },
      aggregates: { count: value },
    })),
    group_count: values.length,
    presentation: {
      version: 1,
      sections: [{ type: "projection", metric: "count", forecast_periods: 1 }],
    },
  }));

  const irregular = projection(
    [0, 10, 20],
    ["2026-01-01", "2026-01-02", "2026-01-31"],
  );
  assert.match(irregular, /<strong>21<\/strong><span>Feb 1<\/span>/);

  const declining = projection(
    [10, 5, 0],
    ["2026-01-01", "2026-01-02", "2026-01-03"],
  );
  assert.match(declining, /<strong>0<\/strong><span>Jan 4<\/span>/);
  assert.doesNotMatch(declining, /<strong>-\d/);
});

test("legacy timestamp projections keep browser-local date labels", () => {
  const previousTimezone = process.env.TZ;
  process.env.TZ = "America/Los_Angeles";
  try {
    const html = workspaceLedgerQueryHtml(queryData({
      contract_id: "manor.recruiting_ledger/v1",
      layout: "timeline",
      group_by: ["occurred_at"],
      matched_count: 3,
      groups: [
        { key: { occurred_at: "2026-07-31T00:00:00Z" }, aggregates: { count: 1 } },
        { key: { occurred_at: "2026-08-01T00:00:00Z" }, aggregates: { count: 2 } },
      ],
      group_count: 2,
      presentation: {
        version: 1,
        sections: [{ type: "projection", metric: "count", forecast_periods: 1 }],
      },
    }));

    assert.match(html, /<strong>3<\/strong><span>Aug 1<\/span>/);
  } finally {
    if (previousTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = previousTimezone;
  }
});

test("isolated Ledger HTML keeps its loading skeleton until render acknowledgement", () => {
  assert.match(previewFrameSource, /preview\.isPreparingPreview && \(/);
  assert.doesNotMatch(previewFrameSource, /isPreparingPreview && !preview\.previewUrl/);
  assert.match(previewFrameSource, /aria-live="polite"/);
  assert.doesNotMatch(assistantBlocksSource, /ledger_rendering/);
  assert.match(previewFrameSource, /loadingLabel = t\("status\.loading"\)/);
  assert.match(stylesSource, /assistant-ledger-visualization[\s\S]*assistant-ledger-skeleton-pulse/);
  assert.match(stylesSource, /prefers-reduced-motion: reduce/);
});
