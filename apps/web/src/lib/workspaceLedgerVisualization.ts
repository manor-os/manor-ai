import type {
  WorkspaceLedgerOverview,
  WorkspaceLedgerOverviewItem,
  WorkspaceLedgerPresentationSection,
  WorkspaceLedgerQueryVisualization,
  WorkspaceLedgerVisualizationScalar,
} from "./api";
import { getLocale, t } from "./i18n";
import { formatUserFacingLabel } from "./taskDisplay";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function isWorkspaceLedgerOverview(
  value: unknown,
): value is WorkspaceLedgerOverview {
  const isCount = (count: unknown) => (
    typeof count === "number" && Number.isFinite(count) && count >= 0
  );
  const isBreakdown = (item: unknown) => (
    isRecord(item)
    && typeof item.key === "string"
    && item.key.length <= 160
    && isCount(item.count)
  );
  const isTotal = (item: unknown) => (
    isRecord(item)
    && typeof item.currency === "string"
    && item.currency.length <= 16
    && isCount(item.inflow_minor)
    && isCount(item.outflow_minor)
  );
  if (
    !isRecord(value)
    || typeof value.workspace_id !== "string"
    || !Array.isArray(value.ledgers)
    || value.ledgers.length > 16
  ) return false;
  return [value.ledger_count, value.record_count, value.event_count].every(
    isCount,
  ) && value.ledgers.every((ledger) => (
    isRecord(ledger)
    && typeof ledger.contract_id === "string"
    && ledger.contract_id.length <= 160
    && typeof ledger.kind === "string"
    && ledger.kind.length <= 80
    && typeof ledger.title === "string"
    && ledger.title.length <= 160
    && typeof ledger.projection_kind === "string"
    && isCount(ledger.record_count)
    && Array.isArray(ledger.stage_counts)
    && ledger.stage_counts.length <= 100
    && ledger.stage_counts.every(isBreakdown)
    && Array.isArray(ledger.status_counts)
    && ledger.status_counts.length <= 100
    && ledger.status_counts.every(isBreakdown)
    && Array.isArray(ledger.totals)
    && ledger.totals.length <= 20
    && ledger.totals.every(isTotal)
  ));
}

function isVisualizationScalar(
  value: unknown,
): value is WorkspaceLedgerVisualizationScalar {
  return value === null
    || typeof value === "string"
    || typeof value === "boolean"
    || (typeof value === "number" && Number.isFinite(value));
}

const WORKSPACE_LEDGER_COUNT_METRICS = new Set(["count"]);
const WORKSPACE_LEDGER_FINANCE_METRICS = new Set([
  "count", "sum_amount_minor", "sum_tax_minor", "sum_fee_minor",
  "sum_inflow_minor", "sum_outflow_minor",
]);

function workspaceLedgerMetricTypes(contractId: unknown): Set<string> {
  return contractId === "manor.finance_ledger/v1"
    ? WORKSPACE_LEDGER_FINANCE_METRICS
    : WORKSPACE_LEDGER_COUNT_METRICS;
}

export function isWorkspaceLedgerQueryVisualization(
  value: unknown,
): value is WorkspaceLedgerQueryVisualization {
  const isBoundedText = (item: unknown, maxLength = 160) => (
    typeof item === "string" && item.length <= maxLength
  );
  const allowedMetrics = workspaceLedgerMetricTypes(
    isRecord(value) ? value.contract_id : null,
  );
  const isMetricMap = (item: unknown) => (
    isRecord(item)
    && Object.keys(item).length <= 6
    && Object.entries(item).every(([key, count]) => (
      isBoundedText(key)
      && allowedMetrics.has(key)
      && typeof count === "number"
      && Number.isFinite(count)
    ))
  );
  const isPresentation = (
    item: unknown,
    groupBy: unknown,
    dateGranularity: unknown,
  ) => {
    if (!isRecord(item) || item.version !== 1 || !Array.isArray(item.sections)) {
      return false;
    }
    if (item.sections.length < 1 || item.sections.length > 4) return false;
    if (!(item.title === undefined || isBoundedText(item.title, 160))) return false;
    if (!(item.subtitle === undefined || isBoundedText(item.subtitle, 240))) return false;
    const sectionTypes = new Set(["metrics", "chart", "records", "profile", "projection"]);
    const chartTypes = new Set(["bar", "line", "area", "pie", "donut"]);
    return item.sections.every((section) => (
      isRecord(section)
      && typeof section.type === "string"
      && sectionTypes.has(section.type)
      && (
        section.type !== "projection"
        || (
          Array.isArray(groupBy)
          && groupBy.length === 1
          && typeof groupBy[0] === "string"
          && ["recorded_at", "occurred_at"].includes(groupBy[0])
          && typeof dateGranularity === "string"
          && ["day", "week", "month"].includes(dateGranularity)
        )
      )
      && (section.title === undefined || isBoundedText(section.title, 120))
      && (
        section.chart === undefined
        || (typeof section.chart === "string" && chartTypes.has(section.chart))
      )
      && (
        section.metric === undefined
        || (typeof section.metric === "string" && allowedMetrics.has(section.metric))
      )
      && (
        section.metrics === undefined
        || (
          Array.isArray(section.metrics)
          && section.metrics.length <= 4
          && section.metrics.every((metric) => (
            typeof metric === "string" && allowedMetrics.has(metric)
          ))
        )
      )
      && (
        section.columns === undefined
        || (
          Array.isArray(section.columns)
          && section.columns.length <= 10
          && section.columns.every((column) => isBoundedText(column))
        )
      )
      && (
        section.forecast_periods === undefined
        || (
          Number.isInteger(section.forecast_periods)
          && Number(section.forecast_periods) >= 1
          && Number(section.forecast_periods) <= 12
        )
      )
    ));
  };
  if (
    !isRecord(value)
    || !isBoundedText(value.contract_id)
    || !isBoundedText(value.view, 24)
    || typeof value.layout !== "string"
    || !["metrics", "bar", "timeline", "table", "empty"].includes(value.layout)
    || typeof value.matched_count !== "number"
    || !Number.isFinite(value.matched_count)
    || value.matched_count < 0
    || !isVisualizationScalar(value.as_of)
    || typeof value.has_more !== "boolean"
    || !(value.currency === null || isBoundedText(value.currency, 16))
    || !(
      value.presentation === undefined
      || value.presentation === null
      || isPresentation(value.presentation, value.group_by, value.date_granularity)
    )
    || !(
      value.query_fingerprint === undefined
      || (
        typeof value.query_fingerprint === "string"
        && /^[0-9a-f]{64}$/.test(value.query_fingerprint)
      )
    )
    || !(
      value.coalesce_previous_visualization === undefined
      || typeof value.coalesce_previous_visualization === "boolean"
    )
    || !Array.isArray(value.group_by)
    || value.group_by.length > 3
    || !value.group_by.every((field) => isBoundedText(field))
    || !(
      value.date_granularity === undefined
      || value.date_granularity === null
      || (
        typeof value.date_granularity === "string"
        && ["day", "week", "month"].includes(value.date_granularity)
      )
    )
    || !Array.isArray(value.metrics)
    || value.metrics.length > 6
    || !value.metrics.every((metric) => (
      isBoundedText(metric) && allowedMetrics.has(metric)
    ))
    || !isMetricMap(value.aggregates)
    || !Array.isArray(value.groups)
    || value.groups.length > 24
    || !(
      value.group_count === undefined
      || (
        typeof value.group_count === "number"
        && Number.isFinite(value.group_count)
        && value.group_count >= value.groups.length
      )
    )
    || !(
      value.groups_truncated === undefined
      || typeof value.groups_truncated === "boolean"
    )
    || !Array.isArray(value.columns)
    || value.columns.length > 10
    || !value.columns.every((column) => isBoundedText(column))
    || !Array.isArray(value.rows)
    || value.rows.length > 20
  ) return false;
  const columns = new Set(value.columns);
  return value.groups.every((group) => (
    isRecord(group)
    && isRecord(group.key)
    && Object.keys(group.key).length <= 3
    && Object.entries(group.key).every(([key, item]) => (
      isBoundedText(key) && isVisualizationScalar(item)
    ))
    && isMetricMap(group.aggregates)
  )) && value.rows.every((row) => (
    isRecord(row)
    && Object.entries(row).every(([key, item]) => (
      columns.has(key) && isVisualizationScalar(item)
    ))
  ));
}

function workspaceLedgerTableRelation(
  existing: WorkspaceLedgerQueryVisualization,
  incoming: WorkspaceLedgerQueryVisualization,
): "incoming_subset" | "incoming_superset" | null {
  if (
    existing.layout !== "table"
    || incoming.layout !== "table"
    || existing.rows.length === 0
    || incoming.rows.length === 0
  ) return null;
  const incomingColumns = new Set(incoming.columns);
  const commonColumns = [...new Set(existing.columns)]
    .filter((column) => incomingColumns.has(column))
    .sort();
  if (commonColumns.length === 0) return null;

  const rowCounts = (
    rows: WorkspaceLedgerQueryVisualization["rows"],
  ): Map<string, number> => {
    const counts = new Map<string, number>();
    for (const row of rows) {
      const token = JSON.stringify(commonColumns.map((column) => row[column] ?? null));
      counts.set(token, (counts.get(token) || 0) + 1);
    }
    return counts;
  };
  const existingCounts = rowCounts(existing.rows);
  const incomingCounts = rowCounts(incoming.rows);
  const contains = (superset: Map<string, number>, subset: Map<string, number>) => (
    [...subset].every(([token, count]) => count <= (superset.get(token) || 0))
  );
  if (contains(existingCounts, incomingCounts)) return "incoming_subset";
  if (contains(incomingCounts, existingCounts)) return "incoming_superset";
  return null;
}

function workspaceLedgerQueryRelation(
  existing: WorkspaceLedgerQueryVisualization,
  incoming: WorkspaceLedgerQueryVisualization,
): "incoming_subset" | "incoming_superset" | "incoming_replacement" | null {
  if (
    existing.contract_id !== incoming.contract_id
    || existing.view !== incoming.view
  ) return null;
  if (existing.query_fingerprint && incoming.query_fingerprint) {
    if (existing.query_fingerprint === incoming.query_fingerprint) {
      return "incoming_replacement";
    }
    if (!incoming.coalesce_previous_visualization) return null;
    if (existing.layout !== incoming.layout) return "incoming_replacement";
    if (existing.layout !== "table") return "incoming_replacement";
    return workspaceLedgerTableRelation(existing, incoming) || "incoming_replacement";
  }
  if (existing.query_fingerprint || incoming.query_fingerprint) return null;

  // Historical persisted blocks have no query identity. Preserve their
  // table-only compatibility behavior without applying it to new results.
  return workspaceLedgerTableRelation(existing, incoming);
}

type WorkspaceLedgerQueryBlockLike = {
  id?: string;
  type?: string;
  kind?: string;
  data?: unknown;
};

export function coalesceWorkspaceLedgerQueryBlocks<
  Block extends WorkspaceLedgerQueryBlockLike,
>(blocks: Block[]): Block[] {
  const result: Block[] = [];
  for (const block of blocks) {
    if (
      block.type !== "visualization"
      || block.kind !== "ledger_query_result"
      || !isWorkspaceLedgerQueryVisualization(block.data)
    ) {
      result.push(block);
      continue;
    }
    if (block.data.coalesce_previous_visualization) {
      let latestIndex = -1;
      let latestData: WorkspaceLedgerQueryVisualization | null = null;
      for (let index = result.length - 1; index >= 0; index -= 1) {
        const candidate = result[index];
        if (
          candidate.type !== "visualization"
          || candidate.kind !== "ledger_query_result"
          || !isWorkspaceLedgerQueryVisualization(candidate.data)
          || candidate.data.contract_id !== block.data.contract_id
          || candidate.data.view !== block.data.view
        ) continue;
        latestIndex = index;
        latestData = candidate.data;
        break;
      }
      if (latestIndex < 0 || !latestData) {
        result.push(block);
        continue;
      }
      const relation = workspaceLedgerQueryRelation(latestData, block.data);
      if (relation === "incoming_subset") continue;
      if (
        relation === "incoming_superset"
        || relation === "incoming_replacement"
      ) {
        result[latestIndex] = {
          ...block,
          id: result[latestIndex].id || block.id,
        };
        continue;
      }
      result.push(block);
      continue;
    }
    const supersededIndices: number[] = [];
    let covered = false;
    for (const [index, existing] of result.entries()) {
      if (
        existing.type !== "visualization"
        || existing.kind !== "ledger_query_result"
        || !isWorkspaceLedgerQueryVisualization(existing.data)
      ) continue;
      const relation = workspaceLedgerQueryRelation(existing.data, block.data);
      if (relation === "incoming_subset") {
        covered = true;
        break;
      }
      if (
        relation === "incoming_superset"
        || relation === "incoming_replacement"
      ) supersededIndices.push(index);
    }
    if (covered) continue;
    if (supersededIndices.length === 0) {
      result.push(block);
      continue;
    }
    const targetIndex = supersededIndices[0];
    const replacement = {
      ...block,
      id: result[targetIndex].id || block.id,
    };
    const superseded = new Set(supersededIndices);
    for (let index = result.length - 1; index >= 0; index -= 1) {
      if (index !== targetIndex && superseded.has(index)) result.splice(index, 1);
    }
    result[targetIndex] = replacement;
  }
  return result;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  })[character] || character);
}

function finiteCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.round(value))
    : 0;
}

function ledgerTitle(ledger: WorkspaceLedgerOverviewItem): string {
  const labelKeys: Record<string, string> = {
    content: "component.workspace_chat.content_ledger",
    finance: "component.workspace_chat.finance_ledger",
    recruiting: "component.workspace_chat.recruiting_hr_ledger",
    relationship: "component.workspace_chat.relationship_ledger",
  };
  const labelKey = labelKeys[ledger.kind];
  return labelKey ? t(labelKey) : ledger.title;
}

function updatedAtLabel(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(getLocale(), {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function money(value: number, currency: string): string {
  const normalizedCurrency = /^[A-Z]{3}$/.test(currency) ? currency : "";
  if (normalizedCurrency) {
    try {
      return new Intl.NumberFormat(getLocale(), {
        style: "currency",
        currency: normalizedCurrency,
        maximumFractionDigits: 2,
      }).format(value / 100);
    } catch {
      // Fall through to the bounded plain-number representation.
    }
  }
  return `${normalizedCurrency || currency} ${(value / 100).toLocaleString(getLocale())}`.trim();
}

function ledgerSection(ledger: WorkspaceLedgerOverviewItem): string {
  const breakdown = (ledger.stage_counts.length > 0
    ? ledger.stage_counts
    : ledger.status_counts).slice(0, 6);
  const maxCount = Math.max(1, ...breakdown.map((item) => finiteCount(item.count)));
  const bars = breakdown.map((item) => {
    const count = finiteCount(item.count);
    const width = Math.max(4, Math.round((count / maxCount) * 100));
    return `<div class="bar-row">
      <span title="${escapeHtml(formatUserFacingLabel(item.key))}">${escapeHtml(formatUserFacingLabel(item.key))}</span>
      <i aria-hidden="true"><b style="width:${width}%"></b></i>
      <strong>${count.toLocaleString(getLocale())}</strong>
    </div>`;
  }).join("");
  const totals = ledger.totals.slice(0, 4).map((total) => {
    const currency = String(total.currency || "").toUpperCase();
    return `<div class="finance-row">
      <span>${escapeHtml(currency)}</span>
      <strong class="inflow">+${escapeHtml(money(finiteCount(total.inflow_minor), currency))}</strong>
      <strong class="outflow">−${escapeHtml(money(finiteCount(total.outflow_minor), currency))}</strong>
    </div>`;
  }).join("");
  const projection = ledger.projection_kind === "current"
    ? t("component.workspace_chat.current_projection")
    : t("component.workspace_chat.event_stream");
  const updated = updatedAtLabel(ledger.updated_at);

  return `<section class="ledger-card">
    <header>
      <div>
        <h2>${escapeHtml(ledgerTitle(ledger))}</h2>
        <p>${escapeHtml(projection)}${updated ? ` · ${escapeHtml(updated)}` : ""}</p>
      </div>
      <strong class="record-count">${finiteCount(ledger.record_count).toLocaleString(getLocale())}</strong>
    </header>
    ${totals ? `<div class="finance-list">${totals}</div>` : ""}
    ${bars ? `<div class="bar-list">${bars}</div>` : ""}
  </section>`;
}

export function workspaceLedgerOverviewHtml(
  overview: WorkspaceLedgerOverview,
  theme: "white" | "dark" = "white",
): string {
  const locale = getLocale();
  const ledgers = overview.ledgers.slice(0, 8).map(ledgerSection).join("");
  const colorScheme = theme === "dark" ? "dark" : "light";
  const palette = theme === "dark"
    ? "--bg:#121211;--panel:#1c1c1b;--muted:#282725;--ink:#f5f5f4;--subtle:#c4c0bb;--faint:#8f8a84;--bar:#908a83;--positive:#74a995;--negative:#df8179;--shadow:0 7px 20px rgba(0,0,0,.22)"
    : "--bg:#f7f6f3;--panel:rgba(255,255,255,.82);--muted:#efede8;--ink:#292524;--subtle:#78716c;--faint:#a8a29e;--bar:#78716c;--positive:#437f6b;--negative:#c14a44;--shadow:0 7px 20px rgba(28,25,23,.07)";
  return `<!doctype html>
<html lang="${escapeHtml(locale)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${escapeHtml(t("component.workspace_chat.business_ledger_overview"))}</title>
  <style>
    :root{color-scheme:${colorScheme};${palette}}
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--ink);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}body{padding:16px}
    .overview{max-width:820px;margin:0 auto}.overview-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}.eyebrow{margin:0 0 3px;color:var(--subtle);font-size:11px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.overview h1{margin:0;font-size:19px;line-height:1.25}.summary{display:grid;grid-template-columns:repeat(3,minmax(84px,1fr));gap:8px;margin-bottom:12px}.summary div{padding:10px 11px;border-radius:10px;background:var(--panel);box-shadow:var(--shadow)}.summary strong{display:block;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:18px;line-height:1.15;font-variant-numeric:tabular-nums}.summary span{display:block;margin-top:3px;color:var(--subtle);font-size:10px}
    .ledger-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.ledger-card{min-width:0;padding:12px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.ledger-card header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.ledger-card h2{margin:0;font-size:13px;line-height:1.3}.ledger-card p{margin:3px 0 0;color:var(--faint);font-size:9px}.record-count{font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:20px;line-height:1;font-variant-numeric:tabular-nums}.bar-list,.finance-list{display:flex;flex-direction:column;gap:6px;margin-top:11px}.bar-row{display:grid;grid-template-columns:minmax(64px,.8fr) minmax(70px,1.4fr) 28px;align-items:center;gap:8px}.bar-row>span{overflow:hidden;color:var(--subtle);font-size:9.5px;text-overflow:ellipsis;white-space:nowrap}.bar-row i{height:6px;overflow:hidden;border-radius:999px;background:var(--muted)}.bar-row b{display:block;height:100%;border-radius:inherit;background:var(--bar)}.bar-row strong{font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:10px;text-align:right;font-variant-numeric:tabular-nums}.finance-row{display:grid;grid-template-columns:36px minmax(0,1fr) minmax(0,1fr);gap:6px;color:var(--faint);font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:9px;font-variant-numeric:tabular-nums}.finance-row strong{overflow:hidden;text-align:right;text-overflow:ellipsis;white-space:nowrap}.inflow{color:var(--positive)}.outflow{color:var(--negative)}.empty{padding:22px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow);text-align:center}.empty strong{display:block;font-size:13px}.empty span{display:block;margin-top:5px;color:var(--subtle);font-size:10px;line-height:1.5}
    @media(max-width:560px){body{padding:12px}.ledger-grid{grid-template-columns:1fr}.overview h1{font-size:17px}.summary{gap:6px}.summary div{padding:9px}.summary strong{font-size:16px}}
  </style>
</head>
<body>
  <main class="overview" data-visualization="workspace-ledger-overview">
    <div class="overview-head"><div><p class="eyebrow">Ledger</p><h1>${escapeHtml(t("component.workspace_chat.business_ledger_overview"))}</h1></div></div>
    <div class="summary" aria-label="${escapeHtml(t("component.workspace_chat.ledger_summary"))}">
      <div><strong>${finiteCount(overview.ledger_count).toLocaleString(locale)}</strong><span>${escapeHtml(t("component.workspace_chat.ledgers"))}</span></div>
      <div><strong>${finiteCount(overview.record_count).toLocaleString(locale)}</strong><span>${escapeHtml(t("component.workspace_chat.records"))}</span></div>
      <div><strong>${finiteCount(overview.event_count).toLocaleString(locale)}</strong><span>${escapeHtml(t("component.workspace_chat.events"))}</span></div>
    </div>
    ${ledgers
      ? `<div class="ledger-grid">${ledgers}</div>`
      : `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.no_ledgers"))}</strong><span>${escapeHtml(t("component.workspace_chat.no_ledgers_description"))}</span></div>`}
  </main>
</body>
</html>`;
}

export function workspaceLedgerOverviewFrameHeights(
  overview: WorkspaceLedgerOverview,
): { desktop: number; mobile: number } {
  const cardHeights = overview.ledgers.slice(0, 8).map((ledger) => {
    const breakdownRows = Math.min(
      6,
      Math.max(ledger.stage_counts.length, ledger.status_counts.length),
    );
    const financeRows = Math.min(4, ledger.totals.length);
    return 70 + (breakdownRows + financeRows) * 15;
  });
  const desktopRows: number[] = [];
  for (let index = 0; index < cardHeights.length; index += 2) {
    desktopRows.push(Math.max(...cardHeights.slice(index, index + 2)));
  }
  return {
    desktop: Math.min(560, Math.max(260, 170 + desktopRows.reduce((sum, height) => sum + height, 0))),
    mobile: Math.min(560, Math.max(280, 170 + cardHeights.reduce((sum, height) => sum + height, 0))),
  };
}

function queryLedgerKind(contractId: string): string {
  if (contractId.includes("content_ledger")) return "content";
  if (contractId.includes("finance_ledger")) return "finance";
  if (contractId.includes("recruiting_ledger")) return "recruiting";
  if (contractId.includes("relationship_ledger")) return "relationship";
  return "ledger";
}

function queryLedgerTitle(contractId: string): string {
  const labelKeys: Record<string, string> = {
    content: "component.workspace_chat.content_ledger",
    finance: "component.workspace_chat.finance_ledger",
    recruiting: "component.workspace_chat.recruiting_hr_ledger",
    relationship: "component.workspace_chat.relationship_ledger",
  };
  const kind = queryLedgerKind(contractId);
  return labelKeys[kind] ? t(labelKeys[kind]) : formatUserFacingLabel(kind);
}

function metricLabel(metric: string): string {
  return formatUserFacingLabel(
    metric.replace(/^sum_/, "").replace(/_minor$/, ""),
  );
}

function queryValue(
  value: WorkspaceLedgerVisualizationScalar,
  field: string,
  currency: string | null,
): string {
  if (value === null || value === "") return "—";
  if (typeof value === "boolean") return String(value);
  if (typeof value === "number") {
    if (field.endsWith("_minor")) return money(value, currency || "");
    return value.toLocaleString(getLocale());
  }
  if (field.endsWith("_at")) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      return new Intl.DateTimeFormat(getLocale(), {
        year: "numeric",
        month: "short",
        day: "numeric",
        timeZone: "UTC",
      }).format(new Date(`${value}T00:00:00Z`));
    }
    const formatted = updatedAtLabel(value);
    if (formatted) return formatted;
  }
  if (field === "currency") return value.toUpperCase();
  if (
    field.endsWith("_key")
    || field.endsWith("_ref")
    || field === "display_name"
  ) return value;
  return formatUserFacingLabel(value);
}

function groupLabel(
  key: Record<string, WorkspaceLedgerVisualizationScalar>,
): string {
  const values = Object.entries(key).map(([field, value]) => (
    queryValue(value, field, null)
  ));
  return values.length > 0
    ? values.join(" · ")
    : "—";
}

function queryGroupMetric(
  data: WorkspaceLedgerQueryVisualization,
  preferredMetric?: string,
): string {
  if (
    preferredMetric
    && data.groups.some((group) => preferredMetric in group.aggregates)
  ) return preferredMetric;
  return data.metrics.find((candidate) => (
    data.groups.some((group) => candidate in group.aggregates)
  )) || Object.keys(data.groups[0]?.aggregates || {})[0] || "count";
}

function queryGroupValue(
  group: WorkspaceLedgerQueryVisualization["groups"][number],
  metric: string,
): number {
  const value = group.aggregates[metric];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function metricCard(
  metric: string,
  value: number,
  currency: string | null,
  modifier = "",
): string {
  return `<div class="metric-card${modifier ? ` ${modifier}` : ""}">
    <strong>${escapeHtml(queryValue(value, metric, currency))}</strong>
    <span>${escapeHtml(metricLabel(metric))}</span>
  </div>`;
}

export function workspaceLedgerTimelineTimestamp(
  key: Record<string, WorkspaceLedgerVisualizationScalar>,
): number | null {
  for (const field of ["recorded_at", "occurred_at"]) {
    const value = key[field];
    if (typeof value !== "string") continue;
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return null;
}

function queryMetricsSection(data: WorkspaceLedgerQueryVisualization): string {
  const metrics = Object.entries(data.aggregates).slice(0, 4);
  const cards = (metrics.length > 0
    ? metrics
    : [["count", data.matched_count] as [string, number]])
    .map(([metric, value]) => metricCard(metric, value, data.currency)).join("");
  return `<div class="metric-grid">${cards}</div>`;
}

function queryFinanceMetricsSection(data: WorkspaceLedgerQueryVisualization): string {
  const inflow = data.aggregates.sum_inflow_minor;
  const outflow = data.aggregates.sum_outflow_minor;
  const cards: string[] = [];
  if (typeof inflow === "number") {
    cards.push(metricCard("sum_inflow_minor", inflow, data.currency, "metric-card--positive"));
  }
  if (typeof outflow === "number") {
    cards.push(metricCard("sum_outflow_minor", outflow, data.currency, "metric-card--negative"));
  }
  if (typeof inflow === "number" && typeof outflow === "number") {
    cards.push(metricCard("net_cash_flow_minor", inflow - outflow, data.currency, "metric-card--net"));
  }
  for (const [metric, value] of Object.entries(data.aggregates)) {
    if (cards.length >= 4 || metric === "sum_inflow_minor" || metric === "sum_outflow_minor") continue;
    cards.push(metricCard(metric, value, data.currency));
  }
  if (cards.length === 0) {
    cards.push(metricCard("count", data.matched_count, data.currency));
  }
  return `<div class="finance-metrics" data-presentation="finance-summary">
    <div class="metric-grid">${cards.join("")}</div>
  </div>`;
}

function queryBarSection(
  data: WorkspaceLedgerQueryVisualization,
  preferredMetric?: string,
): string {
  const metric = queryGroupMetric(data, preferredMetric);
  const values = data.groups.map((group) => queryGroupValue(group, metric));
  const maxValue = Math.max(1, ...values.map((value) => Math.max(0, value)));
  const rows = data.groups.map((group, index) => {
    const value = values[index];
    const width = Math.max(3, Math.round((Math.max(0, value) / maxValue) * 100));
    const label = groupLabel(group.key);
    return `<div class="query-bar-row">
      <span title="${escapeHtml(label)}">${escapeHtml(label)}</span>
      <i aria-hidden="true"><b style="width:${width}%"></b></i>
      <strong>${escapeHtml(queryValue(value, metric, data.currency))}</strong>
    </div>`;
  }).join("");
  return `<div class="section-head"><span>${escapeHtml(metricLabel(metric))}</span></div>
    <div class="query-bar-list">${rows}</div>`;
}

function queryFinanceBarSection(
  data: WorkspaceLedgerQueryVisualization,
  preferredMetric?: string,
): string {
  const metric = queryGroupMetric(data, preferredMetric);
  const values = data.groups.map((group) => queryGroupValue(group, metric));
  const maxValue = Math.max(1, ...values.map((value) => Math.max(0, value)));
  const cards = data.groups.map((group, index) => {
    const label = groupLabel(group.key);
    const value = values[index];
    const width = Math.max(3, Math.round((Math.max(0, value) / maxValue) * 100));
    const direction = Object.values(group.key).some((item) => String(item).toLowerCase() === "inflow")
      ? "inflow"
      : Object.values(group.key).some((item) => String(item).toLowerCase() === "outflow")
        ? "outflow"
        : "neutral";
    return `<div class="finance-flow-card" data-direction="${direction}">
      <span title="${escapeHtml(label)}">${escapeHtml(label)}</span>
      <strong>${escapeHtml(queryValue(value, metric, data.currency))}</strong>
      <i aria-hidden="true"><b style="width:${width}%"></b></i>
    </div>`;
  }).join("");
  return `<div class="section-head"><span>${escapeHtml(metricLabel(metric))}</span></div>
    <div class="finance-flow-grid" data-presentation="finance-comparison">${cards}</div>`;
}

const RECRUITING_STAGE_ORDER = [
  "sourced", "applied", "screening", "interview", "assessment",
  "reference_check", "offer", "hired", "onboarding", "active", "leave",
  "offboarding", "departed", "rejected", "withdrawn", "archived",
];

function queryPipelineSection(
  data: WorkspaceLedgerQueryVisualization,
  preferredMetric?: string,
): string {
  const metric = queryGroupMetric(data, preferredMetric);
  const groups = queryLedgerKind(data.contract_id) === "recruiting"
    ? [...data.groups].sort((left, right) => {
      const leftStage = String(left.key.stage || "").toLowerCase();
      const rightStage = String(right.key.stage || "").toLowerCase();
      const leftIndex = RECRUITING_STAGE_ORDER.indexOf(leftStage);
      const rightIndex = RECRUITING_STAGE_ORDER.indexOf(rightStage);
      return (leftIndex < 0 ? RECRUITING_STAGE_ORDER.length : leftIndex)
        - (rightIndex < 0 ? RECRUITING_STAGE_ORDER.length : rightIndex);
    })
    : data.groups;
  const values = groups.map((group) => queryGroupValue(group, metric));
  const maxValue = Math.max(1, ...values.map((value) => Math.max(0, value)));
  const steps = groups.map((group, index) => {
    const label = groupLabel(group.key);
    const value = values[index];
    const width = Math.max(3, Math.round((Math.max(0, value) / maxValue) * 100));
    return `<div class="pipeline-step" role="listitem">
      <span class="pipeline-index" aria-hidden="true">${index + 1}</span>
      <div class="pipeline-copy">
        <div><span title="${escapeHtml(label)}">${escapeHtml(label)}</span><strong>${escapeHtml(queryValue(value, metric, data.currency))}</strong></div>
        <i aria-hidden="true"><b style="width:${width}%"></b></i>
      </div>
    </div>`;
  }).join("");
  return `<div class="section-head"><span>${escapeHtml(metricLabel(metric))}</span></div>
    <div class="pipeline" data-presentation="stage-pipeline" role="list">${steps}</div>`;
}

function queryStatusSection(
  data: WorkspaceLedgerQueryVisualization,
  preferredMetric?: string,
): string {
  const metric = queryGroupMetric(data, preferredMetric);
  const values = data.groups.map((group) => queryGroupValue(group, metric));
  const total = Math.max(1, values.reduce((sum, value) => sum + Math.max(0, value), 0));
  const cards = data.groups.map((group, index) => {
    const label = groupLabel(group.key);
    const value = values[index];
    const share = Math.round((Math.max(0, value) / total) * 100);
    return `<div class="status-card">
      <div><span title="${escapeHtml(label)}">${escapeHtml(label)}</span><strong>${escapeHtml(queryValue(value, metric, data.currency))}</strong></div>
      <i aria-hidden="true"><b style="width:${share}%"></b></i>
      <small>${share.toLocaleString(getLocale())}%</small>
    </div>`;
  }).join("");
  return `<div class="section-head"><span>${escapeHtml(metricLabel(metric))}</span></div>
    <div class="status-grid" data-presentation="status-board">${cards}</div>`;
}

function queryGroupedSection(
  data: WorkspaceLedgerQueryVisualization,
  preferredMetric?: string,
): string {
  const kind = queryLedgerKind(data.contract_id);
  if (kind === "finance") return queryFinanceBarSection(data, preferredMetric);
  if ((kind === "recruiting" || kind === "relationship") && data.group_by.includes("stage")) {
    return queryPipelineSection(data, preferredMetric);
  }
  if (kind === "content" || data.group_by.includes("status")) {
    return queryStatusSection(data, preferredMetric);
  }
  return queryBarSection(data, preferredMetric);
}

function queryTimelineSection(data: WorkspaceLedgerQueryVisualization): string {
  const metric = queryGroupMetric(data);
  const points = [...data.groups].sort((left, right) => {
    const leftTimestamp = workspaceLedgerTimelineTimestamp(left.key);
    const rightTimestamp = workspaceLedgerTimelineTimestamp(right.key);
    if (leftTimestamp !== null && rightTimestamp !== null) {
      return leftTimestamp - rightTimestamp;
    }
    if (leftTimestamp !== null) return -1;
    if (rightTimestamp !== null) return 1;
    return groupLabel(left.key).localeCompare(groupLabel(right.key));
  });
  const values = points.map((point) => Number(point.aggregates[metric] || 0));
  const minValue = Math.min(0, ...values);
  const maxValue = Math.max(1, ...values);
  const range = Math.max(1, maxValue - minValue);
  const coordinates = values.map((value, index) => {
    const x = points.length <= 1 ? 320 : 36 + (index / (points.length - 1)) * 568;
    const y = 188 - ((value - minValue) / range) * 148;
    return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 };
  });
  const polyline = coordinates.map(({ x, y }) => `${x},${y}`).join(" ");
  const circles = coordinates.map(({ x, y }, index) => (
    `<circle cx="${x}" cy="${y}" r="4"><title>${escapeHtml(
      `${groupLabel(points[index].key)}: ${queryValue(values[index], metric, data.currency)}`,
    )}</title></circle>`
  )).join("");
  const firstLabel = points[0] ? groupLabel(points[0].key) : "";
  const lastPoint = points[points.length - 1];
  const lastLabel = lastPoint ? groupLabel(lastPoint.key) : "";
  return `<div class="section-head"><span>${escapeHtml(metricLabel(metric))}</span></div>
    <div class="timeline-chart">
      <svg viewBox="0 0 640 220" role="img" aria-label="${escapeHtml(metricLabel(metric))}">
        <line x1="36" y1="188" x2="604" y2="188"></line>
        ${coordinates.length > 1 ? `<polyline points="${polyline}"></polyline>` : ""}
        ${circles}
      </svg>
      <div class="timeline-labels"><span>${escapeHtml(firstLabel)}</span><span>${escapeHtml(lastLabel)}</span></div>
    </div>`;
}

function queryTableSection(
  data: WorkspaceLedgerQueryVisualization,
  requestedColumns?: string[],
): string {
  const allowedColumns = new Set(data.columns);
  const columns = (requestedColumns || data.columns).filter((column) => (
    allowedColumns.has(column)
  ));
  if (columns.length === 0 || data.rows.length === 0) {
    return `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.ledger_query_no_results"))}</strong></div>`;
  }
  const headings = columns.map((column) => (
    `<th scope="col">${escapeHtml(formatUserFacingLabel(column))}</th>`
  )).join("");
  const rows = data.rows.map((row) => `<tr>${columns.map((column) => (
    `<td>${escapeHtml(queryValue(row[column] ?? null, column, data.currency))}</td>`
  )).join("")}</tr>`).join("");
  return `<div class="table-wrap"><table><thead><tr>${headings}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function queryFinanceTransactionsSection(
  data: WorkspaceLedgerQueryVisualization,
): string {
  if (data.rows.length === 0) {
    return `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.ledger_query_no_results"))}</strong></div>`;
  }
  const rows = data.rows.map((row, index) => {
    const direction = String(row.direction || "").toLowerCase();
    const directionKind = direction === "inflow" || direction === "outflow"
      ? direction
      : "neutral";
    const currency = typeof row.currency === "string"
      ? row.currency.toUpperCase()
      : data.currency || "";
    const amount = typeof row.amount_minor === "number"
      ? money(row.amount_minor, currency)
      : "—";
    const amountSign = direction === "inflow" ? "+" : direction === "outflow" ? "−" : "";
    const memo = typeof row.memo === "string" && row.memo.trim()
      ? row.memo.trim()
      : "";
    const counterparty = typeof row.counterparty_ref === "string" && row.counterparty_ref.trim()
      ? row.counterparty_ref.trim()
      : "";
    const account = typeof row.account_ref === "string" && row.account_ref.trim()
      ? row.account_ref.trim()
      : "—";
    const entryType = queryValue(row.entry_type ?? null, "entry_type", currency);
    const status = queryValue(row.status ?? null, "status", currency);
    const occurredAt = queryValue(row.occurred_at ?? null, "occurred_at", currency);
    const entryId = typeof row.entry_id === "string" && row.entry_id.trim()
      ? row.entry_id.trim()
      : `#${index + 1}`;
    const title = memo || counterparty || entryType || entryId;
    const typeAndStatus = [entryType, status].filter((value) => value !== "—").join(" · ");
    const accountPath = counterparty ? `${account} → ${counterparty}` : account;
    return `<article class="transaction-row" data-direction="${directionKind}" role="listitem">
      <div class="transaction-main">
        <div class="transaction-copy">
          <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
          <span>${escapeHtml(typeAndStatus || entryId)}</span>
        </div>
        <strong class="transaction-amount">${escapeHtml(amountSign + amount)}</strong>
      </div>
      <div class="transaction-meta">
        <span class="transaction-direction">${escapeHtml(queryValue(row.direction ?? null, "direction", currency))}</span>
        <span title="${escapeHtml(accountPath)}">${escapeHtml(accountPath)}</span>
        <time>${escapeHtml(occurredAt)}</time>
        <code title="${escapeHtml(entryId)}">${escapeHtml(entryId)}</code>
      </div>
    </article>`;
  }).join("");
  return `<div class="transaction-list" data-presentation="finance-transactions" role="list">${rows}</div>`;
}

function queryPresentationMetric(
  data: WorkspaceLedgerQueryVisualization,
  section: WorkspaceLedgerPresentationSection,
): string {
  const preferredMetric = section.metric;
  if (
    preferredMetric
    && data.groups.some((group) => preferredMetric in group.aggregates)
  ) return preferredMetric;
  return queryGroupMetric(data);
}

function queryPresentationMetricsSection(
  data: WorkspaceLedgerQueryVisualization,
  section: WorkspaceLedgerPresentationSection,
): string {
  const requested = new Set(section.metrics || (section.metric ? [section.metric] : []));
  const selected = requested.size > 0
    ? Object.fromEntries(Object.entries(data.aggregates).filter(([metric]) => requested.has(metric)))
    : data.aggregates;
  const scoped = { ...data, aggregates: selected };
  return queryLedgerKind(data.contract_id) === "finance"
    ? queryFinanceMetricsSection(scoped)
    : queryMetricsSection(scoped);
}

function querySeriesChartSection(
  data: WorkspaceLedgerQueryVisualization,
  section: WorkspaceLedgerPresentationSection,
): string {
  const metric = queryPresentationMetric(data, section);
  const variant = section.chart === "area" ? "area" : "line";
  const points = [...data.groups].sort((left, right) => {
    const leftTimestamp = workspaceLedgerTimelineTimestamp(left.key);
    const rightTimestamp = workspaceLedgerTimelineTimestamp(right.key);
    if (leftTimestamp !== null && rightTimestamp !== null) return leftTimestamp - rightTimestamp;
    if (leftTimestamp !== null) return -1;
    if (rightTimestamp !== null) return 1;
    return groupLabel(left.key).localeCompare(groupLabel(right.key));
  });
  if (points.length === 0) {
    return `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.ledger_query_no_results"))}</strong></div>`;
  }
  const values = points.map((point) => queryGroupValue(point, metric));
  const minValue = Math.min(0, ...values);
  const maxValue = Math.max(1, ...values);
  const range = Math.max(1, maxValue - minValue);
  const coordinates = values.map((value, index) => ({
    x: points.length <= 1 ? 320 : 36 + (index / (points.length - 1)) * 568,
    y: 188 - ((value - minValue) / range) * 148,
  })).map(({ x, y }) => ({ x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 }));
  const polyline = coordinates.map(({ x, y }) => `${x},${y}`).join(" ");
  const area = coordinates.length > 1
    ? `<polygon class="series-area" points="36,188 ${polyline} 604,188"></polygon>`
    : "";
  const circles = coordinates.map(({ x, y }, index) => (
    `<circle cx="${x}" cy="${y}" r="4"><title>${escapeHtml(
      `${groupLabel(points[index].key)}: ${queryValue(values[index], metric, data.currency)}`,
    )}</title></circle>`
  )).join("");
  return `<div class="section-head"><span>${escapeHtml(metricLabel(metric))}</span></div>
    <div class="series-chart" data-presentation="${variant}-chart">
      <svg viewBox="0 0 640 220" role="img" aria-label="${escapeHtml(metricLabel(metric))}">
        <line x1="36" y1="188" x2="604" y2="188"></line>
        ${variant === "area" ? area : ""}
        ${coordinates.length > 1 ? `<polyline class="series-line" points="${polyline}"></polyline>` : ""}
        ${circles}
      </svg>
      <div class="timeline-labels"><span>${escapeHtml(groupLabel(points[0].key))}</span><span>${escapeHtml(groupLabel(points[points.length - 1].key))}</span></div>
    </div>`;
}

const QUERY_SERIES_COLORS = [
  "var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)",
  "var(--series-5)", "var(--series-6)", "var(--series-7)", "var(--series-8)",
];

function queryPieSection(
  data: WorkspaceLedgerQueryVisualization,
  section: WorkspaceLedgerPresentationSection,
): string {
  const metric = queryPresentationMetric(data, section);
  const slices = data.groups.map((group) => ({
    label: groupLabel(group.key),
    value: Math.max(0, queryGroupValue(group, metric)),
  })).filter((slice) => slice.value > 0);
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  if (total <= 0) {
    return `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.ledger_query_no_results"))}</strong></div>`;
  }
  let cursor = 0;
  const stops = slices.map((slice, index) => {
    const start = cursor;
    cursor += (slice.value / total) * 360;
    return `${QUERY_SERIES_COLORS[index % QUERY_SERIES_COLORS.length]} ${start.toFixed(2)}deg ${cursor.toFixed(2)}deg`;
  }).join(",");
  const legend = slices.map((slice, index) => {
    const share = Math.round((slice.value / total) * 100);
    return `<li><i style="background:${QUERY_SERIES_COLORS[index % QUERY_SERIES_COLORS.length]}"></i><span title="${escapeHtml(slice.label)}">${escapeHtml(slice.label)}</span><strong>${escapeHtml(queryValue(slice.value, metric, data.currency))}</strong><small>${share}%</small></li>`;
  }).join("");
  const variant = section.chart === "pie" ? "pie" : "donut";
  return `<div class="section-head"><span>${escapeHtml(metricLabel(metric))}</span></div>
    <div class="pie-layout" data-presentation="${variant}-chart">
      <div class="pie-plot pie-plot--${variant}" style="background:conic-gradient(${stops})" role="img" aria-label="${escapeHtml(metricLabel(metric))}">
        ${variant === "donut" ? `<div><strong>${escapeHtml(queryValue(total, metric, data.currency))}</strong><span>${escapeHtml(metricLabel(metric))}</span></div>` : ""}
      </div>
      <ul class="pie-legend">${legend}</ul>
    </div>`;
}

function queryProfileSection(
  data: WorkspaceLedgerQueryVisualization,
  section: WorkspaceLedgerPresentationSection,
): string {
  if (data.rows.length === 0) {
    return `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.ledger_query_no_results"))}</strong></div>`;
  }
  const identityField = ["display_name", "identity_key", "record_key", "counterparty_ref", "entry_id"]
    .find((field) => data.columns.includes(field));
  const buckets = new Map<string, WorkspaceLedgerQueryVisualization["rows"]>();
  for (const row of data.rows) {
    const identity = identityField ? queryValue(row[identityField] ?? null, identityField, data.currency) : "—";
    const bucket = buckets.get(identity) || [];
    bucket.push(row);
    buckets.set(identity, bucket);
  }
  const requested = new Set(section.columns || []);
  const factColumns = data.columns.filter((column) => (
    (!identityField || column !== identityField)
    && !column.endsWith("_at")
    && !column.endsWith("_id")
    && column !== "event"
    && (requested.size === 0 || requested.has(column))
  ));
  const profiles = [...buckets.entries()].slice(0, 6).map(([identity, rows]) => {
    const current = rows[0];
    const facts = factColumns.slice(0, 8).map((column) => (
      `<div><span>${escapeHtml(formatUserFacingLabel(column))}</span><strong>${escapeHtml(queryValue(current[column] ?? null, column, data.currency))}</strong></div>`
    )).join("");
    const activity = rows.slice(0, 8).map((row) => {
      const event = queryValue(row.event ?? row.entry_type ?? row.status ?? null, "event", data.currency);
      const occurred = queryValue(row.occurred_at ?? row.recorded_at ?? null, "occurred_at", data.currency);
      const context = [row.stage, row.status, row.channel]
        .filter((value) => value !== undefined && value !== null && value !== "")
        .map((value) => queryValue(value ?? null, "status", data.currency))
        .join(" · ");
      return `<li><i aria-hidden="true"></i><div><strong>${escapeHtml(event)}</strong>${context ? `<span>${escapeHtml(context)}</span>` : ""}</div><time>${escapeHtml(occurred)}</time></li>`;
    }).join("");
    return `<article class="profile-card">
      <header><div class="profile-avatar" aria-hidden="true">${escapeHtml(identity.slice(0, 1).toUpperCase() || "·")}</div><div><h3>${escapeHtml(identity)}</h3><p>${escapeHtml(queryLedgerTitle(data.contract_id))}</p></div></header>
      ${facts ? `<div class="profile-facts">${facts}</div>` : ""}
      ${activity ? `<div class="profile-activity"><h4>${escapeHtml(t("component.workspace_chat.ledger_activity"))}</h4><ol>${activity}</ol></div>` : ""}
    </article>`;
  }).join("");
  return `<div class="profile-report" data-presentation="personal-report">${profiles}</div>`;
}

function linearForecast(
  values: number[],
  observedTimestamps: number[],
  projectedTimestamps: number[],
): number[] {
  if (values.length === 0 || projectedTimestamps.length === 0) return [];
  if (values.length === 1) {
    return Array.from({ length: projectedTimestamps.length }, () => values[0]);
  }
  const origin = observedTimestamps[0] ?? 0;
  const dayMs = 24 * 60 * 60 * 1000;
  const observedX = observedTimestamps.map((timestamp) => (
    (timestamp - origin) / dayMs
  ));
  const projectedX = projectedTimestamps.map((timestamp) => (
    (timestamp - origin) / dayMs
  ));
  const count = values.length;
  const meanX = observedX.reduce((sum, value) => sum + value, 0) / count;
  const meanY = values.reduce((sum, value) => sum + value, 0) / count;
  const denominator = observedX.reduce((sum, value) => sum + (value - meanX) ** 2, 0);
  const slope = denominator === 0 ? 0 : values.reduce((sum, value, index) => (
    sum + (observedX[index] - meanX) * (value - meanY)
  ), 0) / denominator;
  const intercept = meanY - slope * meanX;
  return projectedX.map((value) => intercept + slope * value);
}

function projectedPeriods(
  points: WorkspaceLedgerQueryVisualization["groups"],
  periods: number,
  granularity: WorkspaceLedgerQueryVisualization["date_granularity"],
): Array<{ label: string; timestamp: number | null }> {
  const timestamps = points.map((point) => workspaceLedgerTimelineTimestamp(point.key));
  const last = timestamps[timestamps.length - 1];
  const previous = timestamps[timestamps.length - 2];
  const formatterOptions: Intl.DateTimeFormatOptions = {
    month: "short",
    day: granularity === "month" ? undefined : "numeric",
    year: granularity === "month" ? "numeric" : undefined,
  };
  if (granularity) formatterOptions.timeZone = "UTC";
  const formatter = new Intl.DateTimeFormat(getLocale(), formatterOptions);
  return Array.from({ length: periods }, (_value, index) => {
    let timestamp: number | null = null;
    if (last !== null && granularity) {
      const projected = new Date(last);
      if (granularity === "month") {
        projected.setUTCMonth(projected.getUTCMonth() + index + 1);
      } else {
        projected.setUTCDate(
          projected.getUTCDate() + (granularity === "week" ? 7 : 1) * (index + 1),
        );
      }
      timestamp = projected.getTime();
    } else if (last !== null && previous !== null && last > previous) {
      timestamp = last + (last - previous) * (index + 1);
    }
    return {
      label: timestamp === null ? `P+${index + 1}` : formatter.format(new Date(timestamp)),
      timestamp,
    };
  });
}

function queryProjectionSection(
  data: WorkspaceLedgerQueryVisualization,
  section: WorkspaceLedgerPresentationSection,
): string {
  const metric = queryPresentationMetric(data, section);
  const points = [...data.groups].sort((left, right) => (
    (workspaceLedgerTimelineTimestamp(left.key) ?? 0)
    - (workspaceLedgerTimelineTimestamp(right.key) ?? 0)
  ));
  if (points.length === 0) {
    return `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.ledger_query_no_results"))}</strong></div>`;
  }
  const observed = points.map((point) => queryGroupValue(point, metric));
  const periods = Math.max(1, Math.min(12, section.forecast_periods || 4));
  const futurePeriods = projectedPeriods(points, periods, data.date_granularity);
  const observedTimeline = points.map((point) => workspaceLedgerTimelineTimestamp(point.key));
  const hasCompleteTimeline = observedTimeline.every((timestamp) => timestamp !== null)
    && futurePeriods.every((period) => period.timestamp !== null);
  const observedTimestamps = hasCompleteTimeline
    ? observedTimeline as number[]
    : observed.map((_value, index) => index);
  const projectedTimestamps = hasCompleteTimeline
    ? futurePeriods.map((period) => period.timestamp as number)
    : futurePeriods.map((_period, index) => observed.length + index);
  const rawForecast = linearForecast(
    observed,
    observedTimestamps,
    projectedTimestamps,
  );
  const forecast = metric === "count"
    ? rawForecast.map((value) => Math.max(0, Math.round(value)))
    : rawForecast;
  const values = [...observed, ...forecast];
  const minValue = Math.min(0, ...values);
  const maxValue = Math.max(1, ...values);
  const range = Math.max(1, maxValue - minValue);
  const coordinateTimeline = [...observedTimestamps, ...projectedTimestamps];
  const minTimestamp = Math.min(...coordinateTimeline);
  const maxTimestamp = Math.max(...coordinateTimeline);
  const coordinateRange = Math.max(1, maxTimestamp - minTimestamp);
  const coordinates = values.map((value, index) => ({
    x: values.length <= 1
      ? 320
      : 36 + ((coordinateTimeline[index] - minTimestamp) / coordinateRange) * 568,
    y: 188 - ((value - minValue) / range) * 148,
  })).map(({ x, y }) => ({ x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 }));
  const observedCoordinates = coordinates.slice(0, observed.length);
  const projectedCoordinates = coordinates.slice(Math.max(0, observed.length - 1));
  const observedLine = observedCoordinates.map(({ x, y }) => `${x},${y}`).join(" ");
  const projectedLine = projectedCoordinates.map(({ x, y }) => `${x},${y}`).join(" ");
  const futureLabels = futurePeriods.map((period) => period.label);
  const lastForecast = forecast[forecast.length - 1];
  return `<div class="projection-card" data-presentation="projection-report">
    <div class="projection-head"><div><strong>${escapeHtml(metricLabel(metric))}</strong><span>${escapeHtml(t("component.workspace_chat.ledger_projection_method"))}</span></div><div><strong>${escapeHtml(queryValue(lastForecast, metric, data.currency))}</strong><span>${escapeHtml(futureLabels[futureLabels.length - 1])}</span></div></div>
    <svg viewBox="0 0 640 220" role="img" aria-label="${escapeHtml(t("component.workspace_chat.ledger_projection"))}">
      <line x1="36" y1="188" x2="604" y2="188"></line>
      ${observedCoordinates.length > 1 ? `<polyline class="observed-line" points="${observedLine}"></polyline>` : ""}
      ${projectedCoordinates.length > 1 ? `<polyline class="projected-line" points="${projectedLine}"></polyline>` : ""}
      ${coordinates.map(({ x, y }, index) => `<circle class="${index < observed.length ? "observed-point" : "projected-point"}" cx="${x}" cy="${y}" r="4"></circle>`).join("")}
    </svg>
    <div class="projection-legend"><span><i class="observed-key"></i>${escapeHtml(t("component.workspace_chat.ledger_observed"))}</span><span><i class="projected-key"></i>${escapeHtml(t("component.workspace_chat.ledger_projected"))}</span></div>
  </div>`;
}

function queryComposedSection(data: WorkspaceLedgerQueryVisualization): string {
  const presentation = data.presentation;
  if (!presentation) return "";
  const sections = presentation.sections.map((section) => {
    let body: string;
    if (section.type === "metrics") {
      body = queryPresentationMetricsSection(data, section);
    } else if (section.type === "chart") {
      if (section.chart === "pie" || section.chart === "donut") {
        body = queryPieSection(data, section);
      } else if (section.chart === "line" || section.chart === "area") {
        body = querySeriesChartSection(data, section);
      } else {
        body = queryGroupedSection(data, section.metric);
      }
    } else if (section.type === "profile") {
      body = queryProfileSection(data, section);
    } else if (section.type === "projection") {
      body = queryProjectionSection(data, section);
    } else if (
      queryLedgerKind(data.contract_id) === "finance"
      && !(section.columns && section.columns.length > 0)
    ) {
      body = queryFinanceTransactionsSection(data);
    } else {
      body = queryTableSection(data, section.columns);
    }
    return `<section class="report-section" data-section="${escapeHtml(section.type)}">
      ${section.title ? `<h2>${escapeHtml(section.title)}</h2>` : ""}
      ${body}
    </section>`;
  }).join("");
  return `<div class="report-sections" data-presentation="composed-report">${sections}</div>`;
}

export function workspaceLedgerQueryHtml(
  data: WorkspaceLedgerQueryVisualization,
  theme: "white" | "dark" = "white",
): string {
  const locale = getLocale();
  const kind = queryLedgerKind(data.contract_id);
  const colorScheme = theme === "dark" ? "dark" : "light";
  const palette = theme === "dark"
    ? "--bg:#121211;--panel:#1c1c1b;--muted:#282725;--ink:#f5f5f4;--subtle:#c4c0bb;--faint:#8f8a84;--bar:#a8a29e;--accent:#74a995;--positive:#74a995;--negative:#df8179;--line:#3a3835;--series-1:#74a995;--series-2:#b6a37e;--series-3:#8d98ad;--series-4:#b48d93;--series-5:#929d7d;--series-6:#9d8eaa;--series-7:#b19b8c;--series-8:#7f9fa1;--shadow:0 7px 20px rgba(0,0,0,.22)"
    : "--bg:#f7f6f3;--panel:rgba(255,255,255,.86);--muted:#efede8;--ink:#292524;--subtle:#78716c;--faint:#a8a29e;--bar:#78716c;--accent:#437f6b;--positive:#437f6b;--negative:#c14a44;--line:#e7e3dd;--series-1:#527d73;--series-2:#aa8c5e;--series-3:#71809a;--series-4:#9d6f77;--series-5:#788663;--series-6:#806f90;--series-7:#967a68;--series-8:#608487;--shadow:0 7px 20px rgba(28,25,23,.07)";
  const projection = data.view === "current"
    ? t("component.workspace_chat.current_projection")
    : t("component.workspace_chat.event_stream");
  const asOf = typeof data.as_of === "string" ? updatedAtLabel(data.as_of) : "";
  const groupCount = typeof data.group_count === "number"
    ? Math.max(data.groups.length, Math.round(data.group_count))
    : data.groups.length;
  const body = data.layout === "empty"
    ? `<div class="empty"><strong>${escapeHtml(t("component.workspace_chat.ledger_query_no_results"))}</strong></div>`
    : data.presentation
      ? queryComposedSection(data)
      : data.layout === "metrics"
      ? kind === "finance" ? queryFinanceMetricsSection(data) : queryMetricsSection(data)
      : data.layout === "bar"
        ? queryGroupedSection(data)
        : data.layout === "timeline"
          ? queryTimelineSection(data)
          : kind === "finance"
            ? queryFinanceTransactionsSection(data)
            : queryTableSection(data);
  const heading = data.presentation?.title || queryLedgerTitle(data.contract_id);
  const subheading = data.presentation?.subtitle
    || `${projection}${asOf ? ` · ${asOf}` : ""}`;
  return `<!doctype html>
<html lang="${escapeHtml(locale)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${escapeHtml(t("component.workspace_chat.ledger_query_result"))}</title>
  <style>
    :root{color-scheme:${colorScheme};${palette}}
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--ink);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}body{padding:16px}.query{max-width:820px;margin:0 auto}.query-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px}.eyebrow{margin:0 0 3px;color:var(--subtle);font-size:11px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.query h1{margin:0;font-size:19px;line-height:1.25}.query-head p:last-child{margin:4px 0 0;color:var(--faint);font-size:10px}.matched{flex:none;padding:7px 9px;border-radius:9px;background:var(--panel);box-shadow:var(--shadow);text-align:right}.matched strong{display:block;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:18px;line-height:1;font-variant-numeric:tabular-nums}.matched span{display:block;margin-top:3px;color:var(--subtle);font-size:9px}.metric-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.metric-card{min-width:0;padding:15px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.metric-card strong{display:block;overflow:hidden;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:22px;font-variant-numeric:tabular-nums;text-overflow:ellipsis;white-space:nowrap}.metric-card span{display:block;margin-top:5px;color:var(--subtle);font-size:10px}.metric-card--positive strong{color:var(--positive,#437f6b)}.metric-card--negative strong{color:var(--negative,#c14a44)}.metric-card--net{background:var(--ink);color:var(--bg)}.metric-card--net span{color:var(--faint)}.section-head{display:flex;justify-content:flex-end;margin:0 0 7px;color:var(--faint);font-size:9px}.query-bar-list{display:flex;max-height:390px;flex-direction:column;gap:7px;overflow:auto;padding:12px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.query-bar-row{display:grid;grid-template-columns:minmax(80px,.8fr) minmax(100px,1.5fr) minmax(44px,.4fr);align-items:center;gap:9px}.query-bar-row>span{overflow:hidden;color:var(--subtle);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.query-bar-row i{height:7px;overflow:hidden;border-radius:999px;background:var(--muted)}.query-bar-row b{display:block;height:100%;border-radius:inherit;background:var(--bar)}.query-bar-row strong{overflow:hidden;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:10px;text-align:right;text-overflow:ellipsis;white-space:nowrap;font-variant-numeric:tabular-nums}.finance-flow-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.finance-flow-card{min-width:0;padding:14px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.finance-flow-card>span{display:block;overflow:hidden;color:var(--subtle);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.finance-flow-card>strong{display:block;margin:9px 0 11px;overflow:hidden;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:21px;font-variant-numeric:tabular-nums;text-overflow:ellipsis;white-space:nowrap}.finance-flow-card>i,.status-card>i,.pipeline-copy>i{display:block;height:6px;overflow:hidden;border-radius:999px;background:var(--muted)}.finance-flow-card b,.status-card b,.pipeline-copy b{display:block;height:100%;border-radius:inherit;background:var(--bar)}.finance-flow-card[data-direction="inflow"] b{background:var(--positive,#437f6b)}.finance-flow-card[data-direction="outflow"] b{background:var(--negative,#c14a44)}.pipeline{display:flex;max-height:390px;flex-direction:column;gap:0;overflow:auto;padding:12px 14px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.pipeline-step{position:relative;display:grid;grid-template-columns:22px minmax(0,1fr);gap:10px;padding-bottom:12px}.pipeline-step:last-child{padding-bottom:0}.pipeline-step:not(:last-child)::after{position:absolute;top:21px;bottom:0;left:10px;width:1px;background:var(--line);content:""}.pipeline-index{position:relative;z-index:1;display:grid;width:22px;height:22px;place-items:center;border-radius:999px;background:var(--ink);color:var(--bg);font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:9px}.pipeline-copy{min-width:0;padding-top:2px}.pipeline-copy>div{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:7px}.pipeline-copy span{overflow:hidden;color:var(--subtle);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.pipeline-copy strong{font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:10px;font-variant-numeric:tabular-nums}.status-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.status-card{position:relative;min-width:0;padding:13px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.status-card>div{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:11px}.status-card span{overflow:hidden;color:var(--subtle);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.status-card strong{font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:16px;font-variant-numeric:tabular-nums}.status-card small{display:block;margin-top:6px;color:var(--faint);font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:9px;text-align:right}.timeline-chart{padding:10px 13px 12px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.timeline-chart svg{display:block;width:100%;height:auto}.timeline-chart line{stroke:var(--line);stroke-width:1}.timeline-chart polyline{fill:none;stroke:var(--accent);stroke-linecap:round;stroke-linejoin:round;stroke-width:3}.timeline-chart circle{fill:var(--panel);stroke:var(--accent);stroke-width:3}.timeline-labels{display:flex;justify-content:space-between;gap:10px;color:var(--faint);font-size:9px}.timeline-labels span{max-width:45%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.transaction-list{display:flex;max-height:390px;flex-direction:column;overflow:auto;padding:0 14px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.transaction-row{min-width:0;padding:13px 0}.transaction-row+.transaction-row{border-top:1px solid var(--line)}.transaction-main{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:14px}.transaction-copy{min-width:0}.transaction-copy strong{display:block;overflow:hidden;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.transaction-copy span{display:block;margin-top:3px;color:var(--subtle);font-size:9px}.transaction-amount{font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}.transaction-row[data-direction="inflow"] .transaction-amount{color:var(--positive)}.transaction-row[data-direction="outflow"] .transaction-amount{color:var(--negative)}.transaction-meta{display:flex;align-items:center;gap:6px;margin-top:9px;overflow:hidden;color:var(--faint);font-size:9px}.transaction-meta>span:not(.transaction-direction){min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.transaction-meta time,.transaction-meta code{flex:none;white-space:nowrap}.transaction-meta code{overflow:hidden;max-width:110px;color:inherit;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;text-overflow:ellipsis}.transaction-direction{display:inline-flex;flex:none;align-items:center;gap:5px;color:var(--subtle)}.transaction-direction::before{width:5px;height:5px;border-radius:999px;background:var(--bar);content:""}.transaction-row[data-direction="inflow"] .transaction-direction::before{background:var(--positive)}.transaction-row[data-direction="outflow"] .transaction-direction::before{background:var(--negative)}.table-wrap{max-height:390px;overflow:auto;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}table{width:100%;border-collapse:collapse;font-size:10px;text-align:left}th{position:sticky;top:0;z-index:1;padding:9px 10px;background:var(--muted);color:var(--subtle);font-size:9px;font-weight:700;white-space:nowrap}td{max-width:210px;padding:9px 10px;border-top:1px solid var(--line);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.empty{padding:34px 18px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow);text-align:center}.empty strong{font-size:13px}.more{margin:9px 2px 0;color:var(--faint);font-size:9px;text-align:right}
    @media(max-width:560px){body{padding:12px}.query h1{font-size:17px}.metric-grid,.finance-flow-grid,.status-grid{grid-template-columns:1fr}.query-bar-row{grid-template-columns:minmax(70px,.8fr) minmax(64px,1.1fr) minmax(38px,.45fr)}.transaction-meta time{display:none}.transaction-meta code{max-width:82px}.table-wrap{max-height:400px}}
  </style>
  <style>
    .report-sections{display:flex;flex-direction:column;gap:12px}.report-section{min-width:0}.report-section>h2{margin:0 0 8px;font-size:12px;line-height:1.35}.series-chart,.projection-card{padding:10px 13px 12px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.series-chart svg,.projection-card svg{display:block;width:100%;height:auto}.series-chart line,.projection-card>svg>line{stroke:var(--line);stroke-width:1}.series-line,.observed-line{fill:none;stroke:var(--accent);stroke-linecap:round;stroke-linejoin:round;stroke-width:3}.series-area{fill:var(--accent);opacity:.12}.series-chart circle,.observed-point{fill:var(--panel);stroke:var(--accent);stroke-width:3}.pie-layout{display:grid;grid-template-columns:minmax(150px,.75fr) minmax(200px,1.25fr);align-items:center;gap:18px;padding:16px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.pie-plot{position:relative;width:min(100%,210px);aspect-ratio:1;margin:auto;border-radius:999px}.pie-plot--donut::after{position:absolute;inset:25%;border-radius:inherit;background:var(--panel);content:""}.pie-plot>div{position:absolute;inset:25%;z-index:1;display:flex;align-items:center;justify-content:center;flex-direction:column;padding:8px;text-align:center}.pie-plot>div strong{max-width:100%;overflow:hidden;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:14px;text-overflow:ellipsis;white-space:nowrap}.pie-plot>div span{margin-top:3px;color:var(--faint);font-size:8px}.pie-legend{display:flex;max-height:220px;flex-direction:column;gap:8px;margin:0;padding:0;overflow:auto;list-style:none}.pie-legend li{display:grid;grid-template-columns:8px minmax(70px,1fr) auto 30px;align-items:center;gap:7px;min-width:0}.pie-legend i{width:7px;height:7px;border-radius:999px}.pie-legend span{overflow:hidden;color:var(--subtle);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.pie-legend strong{font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:10px;font-variant-numeric:tabular-nums}.pie-legend small{color:var(--faint);font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:9px;text-align:right}.profile-report{display:flex;flex-direction:column;gap:10px}.profile-card{padding:16px;border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.profile-card>header{display:flex;align-items:center;gap:10px}.profile-avatar{display:grid;width:34px;height:34px;flex:0 0 34px;place-items:center;border-radius:999px;background:var(--ink);color:var(--bg);font-size:13px;font-weight:750}.profile-card h3{margin:0;font-size:14px}.profile-card header p{margin:2px 0 0;color:var(--faint);font-size:9px}.profile-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:13px}.profile-facts>div{min-width:0;padding:9px 10px;border-radius:9px;background:var(--muted)}.profile-facts span{display:block;color:var(--faint);font-size:8px}.profile-facts strong{display:block;margin-top:3px;overflow:hidden;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.profile-activity{margin-top:14px}.profile-activity h4{margin:0 0 8px;color:var(--subtle);font-size:9px;text-transform:uppercase}.profile-activity ol{display:flex;flex-direction:column;gap:0;margin:0;padding:0;list-style:none}.profile-activity li{display:grid;grid-template-columns:8px minmax(0,1fr) auto;align-items:start;gap:8px;padding:7px 0}.profile-activity li+li{border-top:1px solid var(--line)}.profile-activity li>i{width:6px;height:6px;margin-top:4px;border-radius:999px;background:var(--bar)}.profile-activity li strong,.profile-activity li span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.profile-activity li strong{font-size:9px}.profile-activity li span,.profile-activity time{margin-top:2px;color:var(--faint);font-size:8px}.profile-activity time{white-space:nowrap}.projection-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:3px 3px 0}.projection-head>div:last-child{text-align:right}.projection-head strong,.projection-head span{display:block}.projection-head strong{font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;font-size:12px;font-variant-numeric:tabular-nums}.projection-head span{margin-top:3px;color:var(--faint);font-size:8px}.projected-line{fill:none;stroke:var(--series-2);stroke-dasharray:7 7;stroke-linecap:round;stroke-linejoin:round;stroke-width:3}.projected-point{fill:var(--panel);stroke:var(--series-2);stroke-width:3}.projection-legend{display:flex;justify-content:flex-end;gap:12px;color:var(--faint);font-size:8px}.projection-legend span{display:flex;align-items:center;gap:5px}.projection-legend i{display:block;width:14px;height:3px;border-radius:999px;background:var(--accent)}.projection-legend .projected-key{background:repeating-linear-gradient(90deg,var(--series-2) 0 5px,transparent 5px 8px)}
    @media(max-width:560px){.pie-layout{grid-template-columns:1fr}.pie-plot{width:min(72%,190px)}.profile-facts{grid-template-columns:1fr}.projection-head{align-items:flex-end}}
  </style>
</head>
<body>
  <main class="query" data-visualization="workspace-ledger-query" data-kind="${escapeHtml(kind)}" data-layout="${escapeHtml(data.layout)}"${data.presentation ? " data-composed=\"true\"" : ""}>
    <div class="query-head">
      <div><p class="eyebrow">${escapeHtml(t("component.workspace_chat.ledger_query_result"))}</p><h1>${escapeHtml(heading)}</h1><p>${escapeHtml(subheading)}</p></div>
      <div class="matched"><strong>${finiteCount(data.matched_count).toLocaleString(locale)}</strong><span>${escapeHtml(t("component.workspace_chat.records"))}</span></div>
    </div>
    ${body}
    ${data.groups_truncated ? `<p class="more">${escapeHtml(t("component.workspace_chat.ledger_groups_truncated", { visible: data.groups.length, total: groupCount }))}</p>` : ""}
    ${data.has_more ? `<p class="more">${escapeHtml(t("component.workspace_chat.more_ledger_results"))}</p>` : ""}
  </main>
</body>
</html>`;
}

export function workspaceLedgerQueryFrameHeights(
  data: WorkspaceLedgerQueryVisualization,
): { desktop: number; mobile: number } {
  if (data.layout === "empty") return { desktop: 250, mobile: 260 };
  if (data.presentation) {
    const sectionHeight = (section: WorkspaceLedgerPresentationSection, mobile: boolean) => {
      if (section.type === "metrics") {
        const count = Math.max(1, Math.min(4, section.metrics?.length || Object.keys(data.aggregates).length));
        return mobile ? count * 88 : Math.ceil(count / 2) * 82;
      }
      if (section.type === "records") return Math.min(390, 70 + Math.min(data.rows.length, 5) * 55);
      if (section.type === "profile") return Math.min(470, 150 + Math.min(data.rows.length, 6) * 42);
      return section.chart === "pie" || section.chart === "donut"
        ? mobile ? 440 : 310
        : 300;
    };
    const desktop = data.presentation.sections.reduce((sum, section) => (
      sum + sectionHeight(section, false) + (section.title ? 28 : 12)
    ), 145);
    const mobile = data.presentation.sections.reduce((sum, section) => (
      sum + sectionHeight(section, true) + (section.title ? 28 : 12)
    ), 155);
    return {
      desktop: Math.min(860, Math.max(320, desktop)),
      mobile: Math.min(900, Math.max(340, mobile)),
    };
  }
  if (data.layout === "metrics") {
    const aggregateCount = Object.keys(data.aggregates).length;
    const derivedFinanceMetric = queryLedgerKind(data.contract_id) === "finance"
      && typeof data.aggregates.sum_inflow_minor === "number"
      && typeof data.aggregates.sum_outflow_minor === "number"
      ? 1
      : 0;
    const cardCount = Math.max(1, Math.min(4, aggregateCount + derivedFinanceMetric));
    const desktopRows = Math.ceil(cardCount / 2);
    return {
      desktop: 180 + desktopRows * 80,
      mobile: Math.min(560, 170 + cardCount * 100),
    };
  }
  if (data.layout === "timeline") return { desktop: 390, mobile: 350 };
  if (data.layout === "table" && queryLedgerKind(data.contract_id) === "finance") {
    const visibleRows = Math.min(data.rows.length, 5);
    return {
      desktop: Math.min(560, Math.max(320, 175 + visibleRows * 72)),
      mobile: Math.min(560, Math.max(340, 185 + visibleRows * 72)),
    };
  }
  const rows = data.layout === "bar" ? data.groups.length : data.rows.length;
  return {
    desktop: Math.min(560, Math.max(280, 175 + Math.min(rows, 12) * 31)),
    mobile: Math.min(560, Math.max(300, 185 + Math.min(rows, 12) * 32)),
  };
}
