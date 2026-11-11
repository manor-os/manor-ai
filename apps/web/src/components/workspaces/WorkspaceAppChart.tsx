import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { WorkspaceAppChartData } from "../../lib/api";
import { getLocale, t } from "../../lib/i18n";
import {
  chartCategoryLabel, chartScale, donutArcs, funnelSteps, heatLevel, labelEvery, seriesColor, stackSeries,
} from "./workspaceAppChartScale";
import { chartValueText } from "./workspaceAppFieldFormat";
import "./WorkspaceAppChart.css";

/** A chart block, drawn to scale in SVG from what the server worked out, with its numbers one tap away as a table. */

const HEIGHT = 200;
const LEFT = 48;
const RIGHT = 12;
const TOP = 16;
const BOTTOM = 28;

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    const measure = () => setWidth(Math.max(240, element.clientWidth || 640));
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

type Chart = WorkspaceAppChartData;

function categoryNames(chart: Chart, short = false): string[] {
  const locale = getLocale();
  return chart.categories.map((category) => chartCategoryLabel(category, chart.x_bucket, locale, short));
}

/** A scatter's groups, in the order its points first name them. */
function pointGroups(chart: Chart): string[] {
  return [...new Set((chart.points || []).map((point) => point.series || ""))];
}

function Legend({ chart }: { chart: Chart }) {
  const names = chart.kind === "scatter" ? pointGroups(chart) : chart.series.map((series) => series.label);
  if (names.length < 2 || chart.kind === "heatmap") return null;
  return <ul className="workspace-app-chart__legend">{names.map((name, index) => {
    const { color, opacity } = seriesColor(index, names.length);
    return <li key={`${name}:${index}`}><span className="workspace-app-chart__swatch" style={{ background: color, opacity }} />{name}</li>;
  })}</ul>;
}

/** Lines, areas and columns over time or categories, with a hover readout and an optional target line. */
function PlotChart({ chart, summary }: { chart: Chart; summary: string }) {
  const { ref, width } = useWidth();
  const [hovered, setHovered] = useState<number | null>(null);
  const locale = getLocale();
  const stacked = chart.kind === "stacked_bar" || chart.kind === "stacked_area";
  const columns = chart.kind === "bar" || chart.kind === "stacked_bar";
  const filled = chart.kind === "area" || chart.kind === "stacked_area";
  const layers = stacked ? stackSeries(chart.series) : null;
  const values = layers ? layers.flatMap((layer) => layer.map(([, top]) => top))
    : chart.series.flatMap((series) => series.values.filter((value): value is number => value !== null));
  const scale = chartScale(chart.target ? [...values, chart.target.value] : values, columns || filled);
  const count = chart.categories.length;
  const plot = Math.max(120, width - LEFT - RIGHT);
  const slot = plot / Math.max(1, columns ? count : count - 1);
  const xAt = (index: number) => LEFT + (columns ? slot * (index + 0.5) : count === 1 ? plot / 2 : slot * index);
  const yAt = (value: number) => TOP + (HEIGHT - TOP - BOTTOM) * (1 - (value - scale.min) / (scale.max - scale.min));
  const baseline = yAt(Math.max(scale.min, Math.min(0, scale.max)));
  const names = categoryNames(chart, true);
  const every = labelEvery(count, plot);
  const multi = chart.series.length > 1;
  const groupWidth = Math.min(36, slot * 0.7);
  const barWidth = stacked || !multi ? groupWidth : groupWidth / chart.series.length;
  const fullNames = categoryNames(chart);
  const tooltipLeft = hovered === null ? 0 : Math.min(Math.max(xAt(hovered) - 70, 0), Math.max(0, width - 160));
  return <div ref={ref} className="workspace-app-chart__plot" onMouseLeave={() => setHovered(null)}>
    <svg width={width} height={HEIGHT} role="img" aria-label={summary}>
      {scale.ticks.map((tick) => <g key={tick}>
        <line x1={LEFT} x2={width - RIGHT} y1={yAt(tick)} y2={yAt(tick)} className="workspace-app-chart__grid" />
        <text x={LEFT - 8} y={yAt(tick) + 4} textAnchor="end" className="workspace-app-chart__axis">{chartValueText(tick, chart, locale, true)}</text>
      </g>)}
      {names.map((name, index) => index % every === 0 || index === count - 1
        ? <text key={chart.categories[index].key} x={xAt(index)} y={HEIGHT - 8} textAnchor="middle" className="workspace-app-chart__axis">{name}</text> : null)}
      {columns ? chart.series.map((series, seriesIndex) => {
        const { color, opacity } = seriesColor(seriesIndex, chart.series.length);
        return <g key={series.key} fill={color} opacity={opacity}>{series.values.map((value, index) => {
          const [bottom, top] = layers ? layers[seriesIndex][index] : [0, value ?? 0];
          if (value === null || top === bottom) return null;
          const x = xAt(index) - groupWidth / 2 + (stacked || !multi ? 0 : barWidth * seriesIndex);
          const y = Math.min(yAt(top), yAt(bottom));
          return <rect key={index} x={x} y={y} width={Math.max(1, barWidth - (multi && !stacked ? 1 : 0))} height={Math.max(1, Math.abs(yAt(bottom) - yAt(top)))} rx={stacked ? 0 : 2} />;
        })}</g>;
      }) : chart.series.map((series, seriesIndex) => {
        const { color, opacity } = seriesColor(seriesIndex, chart.series.length);
        const points = series.values.map((value, index) => {
          const top = layers ? layers[seriesIndex][index][1] : value;
          return top === null ? null : [xAt(index), yAt(top)] as const;
        });
        // A gap (no reading) breaks the line rather than drawing through it.
        const runs: Array<Array<readonly [number, number]>> = [];
        points.forEach((point) => { if (!point) runs.push([]); else (runs[runs.length - 1] ?? runs[runs.push([]) - 1]).push(point); });
        const lower = (index: number) => layers ? yAt(layers[seriesIndex][index][0]) : baseline;
        return <g key={series.key} opacity={opacity}>
          {filled && runs.filter((run) => run.length > 1).map((run, runIndex) => {
            const first = points.indexOf(run[0]);
            const back = run.map((_, offset) => `${run[run.length - 1 - offset][0]},${lower(first + run.length - 1 - offset)}`).join(" ");
            return <polygon key={runIndex} points={`${run.map(([x, y]) => `${x},${y}`).join(" ")} ${back}`} fill={color} opacity={stacked ? 0.75 : 0.14} />;
          })}
          {runs.filter((run) => run.length).map((run, runIndex) => run.length === 1
            ? <circle key={runIndex} cx={run[0][0]} cy={run[0][1]} r="3" fill={color} />
            : <polyline key={runIndex} points={run.map(([x, y]) => `${x},${y}`).join(" ")} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />)}
        </g>;
      })}
      {chart.target && <g>
        <line x1={LEFT} x2={width - RIGHT} y1={yAt(chart.target.value)} y2={yAt(chart.target.value)} className="workspace-app-chart__target" />
        <text x={LEFT + 6} y={yAt(chart.target.value) - 6} className="workspace-app-chart__axis workspace-app-chart__target-label">
          {chart.target.label || t("component.workspace_app_view.chart_target")} {chartValueText(chart.target.value, chart, locale, true)}</text>
      </g>}
      {hovered !== null && !columns && <line x1={xAt(hovered)} x2={xAt(hovered)} y1={TOP} y2={HEIGHT - BOTTOM} className="workspace-app-chart__crosshair" />}
      {chart.categories.map((category, index) => <rect key={category.key} x={xAt(index) - slot / 2} y={0} width={Math.max(slot, 16)} height={HEIGHT}
        fill="transparent" onMouseEnter={() => setHovered(index)} onTouchStart={() => setHovered(index)} />)}
    </svg>
    {hovered !== null && <div className="workspace-app-chart__tooltip" style={{ left: tooltipLeft }} role="status">
      <div>{fullNames[hovered]}</div>
      {chart.series.map((series, index) => <strong key={series.key}>
        {multi && <span className="workspace-app-chart__swatch" style={{ background: seriesColor(index, chart.series.length).color, opacity: seriesColor(index, chart.series.length).opacity }} />}
        {multi ? `${series.label} ` : ""}{chartValueText(series.values[hovered], chart, locale)}</strong>)}
    </div>}
  </div>;
}

/** Categories as horizontal bars, a row each, so long names read at any width: bars, stacks, a leaderboard. */
function BarRows({ chart, ranked }: { chart: Chart; ranked: boolean }) {
  const locale = getLocale();
  const stacked = chart.kind === "stacked_bar";
  const totals = chart.categories.map((_, index) => chart.series.reduce((sum, series) => sum + Math.max(0, series.values[index] ?? 0), 0));
  const largest = stacked ? Math.max(0, ...totals) : Math.max(0, ...chart.series.flatMap((series) => series.values.map((value) => Math.abs(value ?? 0))));
  const names = categoryNames(chart);
  return <ol className={`workspace-app-chart__rows${ranked ? " is-ranked" : ""}`}>{chart.categories.map((category, index) => <li key={category.key}>
    {ranked && <span className="workspace-app-chart__rank mono">{index + 1}</span>}
    <span className="workspace-app-chart__row-name" title={names[index]}>{names[index]}</span>
    <span className="workspace-app-chart__row-bars">
      {stacked ? <span className="workspace-app-chart__track"><span className="workspace-app-chart__stack" style={{ width: `${largest ? totals[index] / largest * 100 : 0}%` }}>
        {chart.series.map((series, seriesIndex) => {
          const { color, opacity } = seriesColor(seriesIndex, chart.series.length);
          return <span key={series.key} style={{ flexGrow: Math.max(0, series.values[index] ?? 0), background: color, opacity }}
            title={`${series.label} ${chartValueText(series.values[index], chart, locale)}`} />;
        })}</span></span>
        : chart.series.map((series, seriesIndex) => {
          const { color, opacity } = seriesColor(seriesIndex, chart.series.length);
          return <span key={series.key} className="workspace-app-chart__track"><span style={{ width: `${largest ? Math.abs(series.values[index] ?? 0) / largest * 100 : 0}%`, background: color, opacity }} /></span>;
        })}
    </span>
    <span className="workspace-app-chart__row-value mono">{stacked || chart.series.length === 1
      ? chartValueText(stacked ? totals[index] : chart.series[0].values[index], chart, locale)
      : chart.series.map((series) => chartValueText(series.values[index], chart, locale, true)).join(" · ")}</span>
  </li>)}</ol>;
}

function DonutChart({ chart, summary }: { chart: Chart; summary: string }) {
  const locale = getLocale();
  const values = (chart.series[0]?.values || []).map((value) => Math.max(0, value ?? 0));
  const total = values.reduce((sum, value) => sum + value, 0);
  const arcs = donutArcs(values, 70, 70, 66, 44);
  const names = categoryNames(chart);
  return <div className="workspace-app-chart__donut">
    <svg width="140" height="140" viewBox="0 0 140 140" role="img" aria-label={summary}>
      {arcs.map((path, index) => path ? <path key={chart.categories[index].key} d={path} fillRule="evenodd"
        fill={seriesColor(index, Math.max(2, values.length)).color} opacity={seriesColor(index, Math.max(2, values.length)).opacity} /> : null)}
      <text x="70" y="68" textAnchor="middle" className="workspace-app-chart__donut-total">{chartValueText(total, chart, locale, true)}</text>
      <text x="70" y="86" textAnchor="middle" className="workspace-app-chart__axis">{t("component.workspace_app_view.chart_total")}</text>
    </svg>
    <ul className="workspace-app-chart__shares">{chart.categories.map((category, index) => {
      const { color, opacity } = seriesColor(index, Math.max(2, values.length));
      return <li key={category.key}>
        <span><span className="workspace-app-chart__swatch" style={{ background: color, opacity }} />{names[index]}</span>
        <span className="mono">{chartValueText(values[index], chart, locale)} · {total ? Math.round(values[index] / total * 100) : 0}%</span>
      </li>;
    })}</ul>
  </div>;
}

function FunnelChart({ chart }: { chart: Chart }) {
  const locale = getLocale();
  const steps = funnelSteps(chart.series[0]?.values || []);
  const names = categoryNames(chart);
  const percent = (share: number | null) => share === null ? "—" : new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 }).format(share);
  return <ol className="workspace-app-chart__funnel">{steps.map((step, index) => <li key={chart.categories[index].key}>
    <span className="workspace-app-chart__row-name">{names[index]}</span>
    <span className="workspace-app-chart__funnel-bar"><span style={{ width: `${(step.ofFirst ?? (index === 0 ? 1 : 0)) * 100}%` }} /></span>
    <span className="workspace-app-chart__row-value mono">{chartValueText(step.value, chart, locale)}</span>
    {step.ofPrevious !== null && <small className="workspace-app-chart__conversion">{t("component.workspace_app_view.chart_conversion", { share: percent(step.ofPrevious) })}</small>}
  </li>)}
    {steps.length > 1 && <li className="workspace-app-chart__funnel-overall"><small>
      {t("component.workspace_app_view.chart_overall", { share: percent(steps[steps.length - 1].ofFirst) })}</small></li>}
  </ol>;
}

function HeatmapChart({ chart, summary }: { chart: Chart; summary: string }) {
  const locale = getLocale();
  const max = Math.max(0, ...chart.series.flatMap((series) => series.values.map((value) => value ?? 0)));
  const names = categoryNames(chart, true);
  const every = Math.max(1, Math.ceil(chart.categories.length / 8));
  return <div className="workspace-app-chart__heatmap" role="img" aria-label={summary}
    style={{ gridTemplateColumns: `auto repeat(${chart.categories.length}, minmax(0, 1fr))` }}>
    {chart.series.map((series) => <div key={series.key} className="workspace-app-chart__heat-row" style={{ display: "contents" }}>
      <span className="workspace-app-chart__heat-name">{series.label}</span>
      {series.values.map((value, index) => <span key={chart.categories[index].key} className="workspace-app-chart__heat-cell"
        style={{ opacity: value ? 0.12 + heatLevel(value, max) * 0.88 : undefined }} data-empty={!value || undefined}
        title={`${series.label} · ${names[index]}: ${chartValueText(value ?? 0, chart, locale)}`} />)}
    </div>)}
    <span />
    {names.map((name, index) => <span key={chart.categories[index].key} className="workspace-app-chart__heat-axis">{index % every === 0 ? name : ""}</span>)}
  </div>;
}

function ScatterChart({ chart, summary }: { chart: Chart; summary: string }) {
  const { ref, width } = useWidth();
  const [hovered, setHovered] = useState<number | null>(null);
  const locale = getLocale();
  const points = chart.points || [];
  const xs = chartScale(points.map((point) => point.x), false);
  const ys = chartScale(points.map((point) => point.y), false);
  const groups = pointGroups(chart);
  const plot = Math.max(120, width - LEFT - RIGHT);
  const xAt = (value: number) => LEFT + plot * (value - xs.min) / (xs.max - xs.min);
  const yAt = (value: number) => TOP + (HEIGHT - TOP - BOTTOM) * (1 - (value - ys.min) / (ys.max - ys.min));
  const point = hovered === null ? null : points[hovered];
  return <div ref={ref} className="workspace-app-chart__plot" onMouseLeave={() => setHovered(null)}>
    <svg width={width} height={HEIGHT} role="img" aria-label={summary}>
      {ys.ticks.map((tick) => <g key={`y${tick}`}>
        <line x1={LEFT} x2={width - RIGHT} y1={yAt(tick)} y2={yAt(tick)} className="workspace-app-chart__grid" />
        <text x={LEFT - 8} y={yAt(tick) + 4} textAnchor="end" className="workspace-app-chart__axis">{chartValueText(tick, chart, locale, true)}</text>
      </g>)}
      {xs.ticks.map((tick) => <text key={`x${tick}`} x={xAt(tick)} y={HEIGHT - 8} textAnchor="middle" className="workspace-app-chart__axis">{chartValueText(tick, { format: "number" }, locale, true)}</text>)}
      {points.map((entry, index) => {
        const { color, opacity } = seriesColor(groups.indexOf(entry.series || ""), groups.length);
        return <circle key={index} cx={xAt(entry.x)} cy={yAt(entry.y)} r={hovered === index ? 6 : 4} fill={color} fillOpacity={opacity * 0.8}
          onMouseEnter={() => setHovered(index)} onTouchStart={() => setHovered(index)} />;
      })}
    </svg>
    {point && <div className="workspace-app-chart__tooltip" style={{ left: Math.min(Math.max(xAt(point.x) - 70, 0), Math.max(0, width - 160)) }} role="status">
      <div>{point.label}{point.series ? ` · ${point.series}` : ""}</div>
      <strong>{chartValueText(point.x, { format: "number" }, locale)}, {chartValueText(point.y, chart, locale)}</strong>
    </div>}
  </div>;
}

function ChartTable({ chart }: { chart: Chart }) {
  const locale = getLocale();
  if (chart.kind === "scatter") {
    return <div className="workspace-app-chart__table"><table><tbody>{(chart.points || []).map((point, index) => <tr key={index}>
      <th scope="row">{point.label}</th>{point.series !== undefined && <td>{point.series}</td>}
      <td className="mono">{chartValueText(point.x, { format: "number" }, locale)}</td><td className="mono">{chartValueText(point.y, chart, locale)}</td>
    </tr>)}</tbody></table></div>;
  }
  const names = categoryNames(chart);
  const single = chart.series.length === 1;
  return <div className="workspace-app-chart__table"><table>
    {!single && <thead><tr><th scope="col" />{chart.series.map((series) => <th scope="col" key={series.key}>{series.label}</th>)}</tr></thead>}
    <tbody>{chart.categories.map((category, index) => <tr key={category.key}>
      <th scope="row">{names[index]}</th>
      {chart.series.map((series) => <td key={series.key} className="mono">{chartValueText(series.values[index], chart, locale)}</td>)}
    </tr>)}</tbody>
  </table></div>;
}

function chartSummary(chart: Chart, title: string): string {
  const locale = getLocale();
  if (chart.points) return `${title}: ${chart.points.length}`;
  const names = categoryNames(chart);
  return `${title}: ${chart.series.map((series) => `${series.label ? `${series.label} ` : ""}${series.values
    .map((value, index) => `${names[index]} ${chartValueText(value, chart, locale)}`).join(", ")}`).join("; ")}`;
}

/** One chart block: its picture (or its numbers as a table), legend and what it leaves out. */
export default function WorkspaceAppChart({ chart, title, actions }: { chart: Chart; title: string; actions?: ReactNode }) {
  const [asTable, setAsTable] = useState(false);
  const summary = useMemo(() => chartSummary(chart, title), [chart, title]);
  const timed = Boolean(chart.x_bucket && ["day", "week", "month"].includes(chart.x_bucket));
  const empty = chart.kind === "scatter" ? !chart.points?.length : !chart.categories.length
    || chart.series.every((series) => series.values.every((value) => !value));
  let picture: ReactNode;
  if (chart.kind === "donut") picture = <DonutChart chart={chart} summary={summary} />;
  else if (chart.kind === "funnel") picture = <FunnelChart chart={chart} />;
  else if (chart.kind === "heatmap") picture = <HeatmapChart chart={chart} summary={summary} />;
  else if (chart.kind === "scatter") picture = <ScatterChart chart={chart} summary={summary} />;
  else if (chart.kind === "leaderboard") picture = <BarRows chart={chart} ranked />;
  // Time runs along the bottom; named categories read best as rows.
  else if ((chart.kind === "bar" || chart.kind === "stacked_bar") && !timed) picture = <BarRows chart={chart} ranked={false} />;
  else picture = <PlotChart chart={chart} summary={summary} />;
  return <div className={`workspace-app-chart workspace-app-chart--${chart.kind}`}>
    <div className="workspace-app-chart__head">
      <Legend chart={chart} />
      {actions}
      {!empty && <button type="button" className="workspace-app-chart__toggle" aria-pressed={asTable} onClick={() => setAsTable((value) => !value)}>
        {t(asTable ? "component.workspace_app_view.chart_view_chart" : "component.workspace_app_view.chart_view_table")}
      </button>}
    </div>
    {empty ? <p className="workspace-app-chart__empty">{t("component.workspace_app_view.chart_empty")}</p> : asTable ? <ChartTable chart={chart} /> : picture}
    {(chart.truncated || Boolean(chart.hidden_categories)) && <p className="workspace-app-chart__note">
      {chart.truncated && t("component.workspace_app_view.chart_truncated", { count: chart.records })}
      {chart.truncated && chart.hidden_categories ? " · " : ""}
      {Boolean(chart.hidden_categories) && t("component.workspace_app_view.chart_hidden", { count: chart.hidden_categories! })}
    </p>}
  </div>;
}

/** A small trend line in a card or a cell: the numbers' shape, with the latest one marked. */
export function Sparkline({ points, label, width = 96, height = 24 }: { points: number[]; label: string; width?: number; height?: number }) {
  if (points.length < 2) return null;
  const low = Math.min(...points);
  const high = Math.max(...points);
  const span = high - low || 1;
  const coords = points.map((value, index) => [index / (points.length - 1) * (width - 4) + 2, height - 3 - (value - low) / span * (height - 6)]);
  const last = coords[coords.length - 1];
  return <svg className="workspace-app-chart__sparkline" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
    <polyline points={coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    <circle cx={last[0]} cy={last[1]} r="2" fill="currentColor" />
  </svg>;
}
