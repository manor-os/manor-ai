import type { SpreadsheetChartModel } from "../lib/spreadsheetOoxml";

const CHART_COLORS = ["#2563eb", "#f97316", "#15803d", "#0ea5e9", "#7c3aed", "#dc2626"];

function piePath(cx: number, cy: number, radius: number, start: number, end: number): string {
  const point = (angle: number) => ({ x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius });
  const from = point(start);
  const to = point(end);
  return `M ${cx} ${cy} L ${from.x} ${from.y} A ${radius} ${radius} 0 ${end - start > Math.PI ? 1 : 0} 1 ${to.x} ${to.y} Z`;
}

function SpreadsheetChartSvg({ chart }: { chart: SpreadsheetChartModel }) {
  const width = 720;
  const height = 320;
  const pad = { top: 22, right: 20, bottom: 62, left: 48 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const categories = chart.series[0]?.categories || [];

  if (chart.type === "pie") {
    const series = chart.series[0];
    const values = series?.values.map((value) => Math.max(0, value)) || [];
    const total = values.reduce((sum, value) => sum + value, 0);
    let angle = -Math.PI / 2;
    return (
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title} style={{ display: "block", width: "100%", height: "auto" }}>
        {total > 0 ? values.map((value, index) => {
          const nextAngle = angle + (value / total) * Math.PI * 2;
          const path = piePath(240, 155, 112, angle, nextAngle);
          angle = nextAngle;
          return <path key={index} d={path} fill={CHART_COLORS[index % CHART_COLORS.length]} stroke="#ffffff" strokeWidth="2" />;
        }) : <circle cx="240" cy="155" r="112" fill="#f5f5f4" stroke="#d6d3d1" />}
        {(series?.categories || []).slice(0, 8).map((label, index) => (
          <g key={`${label}:${index}`} transform={`translate(430 ${72 + index * 25})`}>
            <rect width="12" height="12" rx="2" fill={CHART_COLORS[index % CHART_COLORS.length]} />
            <text x="20" y="11" fontSize="12" fill="#57534e">{label.slice(0, 24)}</text>
          </g>
        ))}
      </svg>
    );
  }

  const allValues = chart.series.flatMap((series) => series.values).filter(Number.isFinite);
  const minimum = Math.min(0, ...allValues);
  const maximum = Math.max(1, ...allValues);
  const span = Math.max(1, maximum - minimum);
  const yFor = (value: number) => pad.top + ((maximum - value) / span) * plotHeight;
  const zeroY = yFor(0);
  const categoryCount = Math.max(1, categories.length, ...chart.series.map((series) => series.values.length));
  const categoryWidth = plotWidth / categoryCount;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title} style={{ display: "block", width: "100%", height: "auto" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
        const y = pad.top + plotHeight * ratio;
        const value = maximum - span * ratio;
        return (
          <g key={ratio}>
            <line x1={pad.left} y1={y} x2={pad.left + plotWidth} y2={y} stroke="#e7e5e4" strokeDasharray="4 4" />
            <text x={pad.left - 8} y={y + 4} textAnchor="end" fontSize="10" fill="#a8a29e">{Math.round(value * 100) / 100}</text>
          </g>
        );
      })}
      <line x1={pad.left} y1={zeroY} x2={pad.left + plotWidth} y2={zeroY} stroke="#a8a29e" />
      {chart.type === "line" ? chart.series.map((series, seriesIndex) => {
        const points = series.values.map((value, index) => ({
          x: pad.left + categoryWidth * index + categoryWidth / 2,
          y: yFor(value),
        }));
        return (
          <g key={`${series.name}:${seriesIndex}`}>
            <path d={points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ")} fill="none" stroke={CHART_COLORS[seriesIndex % CHART_COLORS.length]} strokeWidth="3" />
            {points.map((point, index) => <circle key={index} cx={point.x} cy={point.y} r="3.5" fill={CHART_COLORS[seriesIndex % CHART_COLORS.length]} />)}
          </g>
        );
      }) : chart.series.flatMap((series, seriesIndex) => {
        const groupWidth = Math.max(8, categoryWidth * 0.72);
        const barWidth = groupWidth / Math.max(1, chart.series.length);
        return series.values.map((value, index) => {
          const x = pad.left + categoryWidth * index + (categoryWidth - groupWidth) / 2 + barWidth * seriesIndex;
          const y = Math.min(zeroY, yFor(value));
          return (
            <rect
              key={`${seriesIndex}:${index}`}
              x={x}
              y={y}
              width={Math.max(2, barWidth - 2)}
              height={Math.max(1, Math.abs(zeroY - yFor(value)))}
              rx="2"
              fill={CHART_COLORS[seriesIndex % CHART_COLORS.length]}
            />
          );
        });
      })}
      {categories.map((label, index) => index % Math.max(1, Math.ceil(categories.length / 8)) === 0 && (
        <text key={`${label}:${index}`} x={pad.left + categoryWidth * index + categoryWidth / 2} y={height - 38} textAnchor="middle" fontSize="10" fill="#78716c">
          {label.slice(0, 12)}
        </text>
      ))}
      {chart.series.map((series, index) => (
        <g key={`${series.name}:legend`} transform={`translate(${pad.left + index * 130} ${height - 15})`}>
          <rect width="11" height="11" rx="2" fill={CHART_COLORS[index % CHART_COLORS.length]} />
          <text x="17" y="10" fontSize="10" fill="#57534e">{series.name.slice(0, 17)}</text>
        </g>
      ))}
    </svg>
  );
}

export default function SpreadsheetChartPreview({ charts }: { charts: SpreadsheetChartModel[] }) {
  if (charts.length === 0) return null;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 520px), 1fr))", gap: 12 }}>
      {charts.map((chart) => (
        <section key={chart.id} style={{ minWidth: 0, border: "1px solid var(--border-subtle, #e7e5e4)", borderRadius: 10, background: "var(--surface-panel, #ffffff)", padding: 12 }}>
          <h3 style={{ margin: "0 0 8px", fontSize: 14, fontWeight: 800, color: "var(--text-strong, #1c1917)" }}>{chart.title}</h3>
          <SpreadsheetChartSvg chart={chart} />
        </section>
      ))}
    </div>
  );
}
