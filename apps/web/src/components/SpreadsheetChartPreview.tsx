import type { SpreadsheetChartModel } from "../lib/spreadsheetOoxml";

const CHART_COLORS = ["#2563eb", "#f97316", "#15803d", "#0ea5e9", "#7c3aed", "#dc2626"];

function piePath(cx: number, cy: number, radius: number, start: number, end: number): string {
  const point = (angle: number) => ({ x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius });
  const from = point(start);
  const to = point(end);
  return `M ${cx} ${cy} L ${from.x} ${from.y} A ${radius} ${radius} 0 ${end - start > Math.PI ? 1 : 0} 1 ${to.x} ${to.y} Z`;
}

function smoothLinePath(points: Array<{ x: number; y: number }>): string {
  if (points.length < 2) return points.length === 1 ? `M ${points[0].x} ${points[0].y}` : "";
  return points.reduce((path, point, index) => {
    if (index === 0) return `M ${point.x} ${point.y}`;
    const previous = points[index - 1];
    const before = points[Math.max(0, index - 2)];
    const after = points[Math.min(points.length - 1, index + 1)];
    const control1 = {
      x: previous.x + (point.x - before.x) / 6,
      y: previous.y + (point.y - before.y) / 6,
    };
    const control2 = {
      x: point.x - (after.x - previous.x) / 6,
      y: point.y - (after.y - previous.y) / 6,
    };
    return `${path} C ${control1.x} ${control1.y}, ${control2.x} ${control2.y}, ${point.x} ${point.y}`;
  }, "");
}

function SpreadsheetChartSvg({ chart }: { chart: SpreadsheetChartModel }) {
  const width = 720;
  const height = 320;
  const pad = { top: 22, right: 20, bottom: 62, left: chart.type === "bar" ? 96 : 48 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const categories = chart.series[0]?.categories || [];
  const colorForSeries = (index: number) => chart.series[index]?.color || CHART_COLORS[index % CHART_COLORS.length];
  const colorForPoint = (index: number) => chart.series[0]?.pointColors?.[index] || CHART_COLORS[index % CHART_COLORS.length];

  if (chart.type === "pie" || chart.type === "doughnut") {
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
          return <path key={index} d={path} fill={colorForPoint(index)} stroke="#ffffff" strokeWidth="2" />;
        }) : <circle cx="240" cy="155" r="112" fill="#f5f5f4" stroke="#d6d3d1" />}
        {chart.type === "doughnut" && <circle cx="240" cy="155" r="58" fill="var(--surface-panel, #ffffff)" />}
        {(series?.categories || []).slice(0, 8).map((label, index) => (
          <g key={`${label}:${index}`} transform={`translate(430 ${72 + index * 25})`}>
            <rect width="12" height="12" rx="2" fill={colorForPoint(index)} />
            <text x="20" y="11" fontSize="12" fill="#57534e">{String(label).slice(0, 24)}</text>
          </g>
        ))}
      </svg>
    );
  }

  if (chart.type === "scatter") {
    const allPoints = chart.series.flatMap((series) => series.values.flatMap((value, index) => {
      const x = Number(series.categories[index]);
      return Number.isFinite(x) && Number.isFinite(value) ? [{ x, y: value }] : [];
    }));
    const boundsPoints = allPoints.length > 0 ? allPoints : [{ x: 0, y: 0 }];
    const rawMinX = Math.min(...boundsPoints.map((point) => point.x));
    const rawMaxX = Math.max(...boundsPoints.map((point) => point.x));
    const rawMinY = Math.min(...boundsPoints.map((point) => point.y));
    const rawMaxY = Math.max(...boundsPoints.map((point) => point.y));
    const xSpan = Math.max(1e-9, rawMaxX - rawMinX);
    const ySpan = Math.max(1e-9, rawMaxY - rawMinY);
    const minX = rawMinX - xSpan * 0.06;
    const maxX = rawMaxX + xSpan * 0.06;
    const minY = rawMinY - ySpan * 0.08;
    const maxY = rawMaxY + ySpan * 0.08;
    const xForScatter = (value: number) => pad.left + ((value - minX) / (maxX - minX)) * plotWidth;
    const yForScatter = (value: number) => pad.top + ((maxY - value) / (maxY - minY)) * plotHeight;
    const showLines = chart.scatterStyle !== "marker";
    const showMarkers = chart.scatterStyle === "marker"
      || chart.scatterStyle === "lineMarker"
      || chart.scatterStyle === "smoothMarker";
    const smooth = chart.scatterStyle === "smooth" || chart.scatterStyle === "smoothMarker";
    return (
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title} style={{ display: "block", width: "100%", height: "auto" }}>
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const x = pad.left + plotWidth * ratio;
          const y = pad.top + plotHeight * ratio;
          return (
            <g key={ratio}>
              <line x1={x} y1={pad.top} x2={x} y2={pad.top + plotHeight} stroke="#e7e5e4" strokeDasharray="4 4" />
              <line x1={pad.left} y1={y} x2={pad.left + plotWidth} y2={y} stroke="#e7e5e4" strokeDasharray="4 4" />
              <text x={x} y={height - 38} textAnchor="middle" fontSize="10" fill="#78716c">
                {Math.round((minX + (maxX - minX) * ratio) * 100) / 100}
              </text>
              <text x={pad.left - 8} y={y + 4} textAnchor="end" fontSize="10" fill="#a8a29e">
                {Math.round((maxY - (maxY - minY) * ratio) * 100) / 100}
              </text>
            </g>
          );
        })}
        {chart.series.map((series, seriesIndex) => {
          const points = series.values.flatMap((value, index) => {
            const x = Number(series.categories[index]);
            return Number.isFinite(x) && Number.isFinite(value)
              ? [{ x: xForScatter(x), y: yForScatter(value) }]
              : [];
          });
          const line = smooth ? smoothLinePath(points) : points.map(
            (point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`,
          ).join(" ");
          return (
            <g key={`${series.name}:${seriesIndex}`}>
              {showLines && <path d={line} fill="none" stroke={colorForSeries(seriesIndex)} strokeWidth="3" />}
              {(showMarkers || series.showMarkers) && points.map((point, index) => (
                <circle key={index} cx={point.x} cy={point.y} r="4" fill={colorForSeries(seriesIndex)} />
              ))}
            </g>
          );
        })}
        {chart.series.map((series, index) => (
          <g key={`${series.name}:legend`} transform={`translate(${pad.left + index * 130} ${height - 15})`}>
            <rect width="11" height="11" rx="2" fill={colorForSeries(index)} />
            <text x="17" y="10" fontSize="10" fill="#57534e">{series.name.slice(0, 17)}</text>
          </g>
        ))}
      </svg>
    );
  }

  if (["stock_hlc", "stock_ohlc", "stock_vhlc", "stock_vohlc"].includes(chart.type)) {
    const hasVolume = chart.type === "stock_vhlc" || chart.type === "stock_vohlc";
    const hasOpen = chart.type === "stock_ohlc" || chart.type === "stock_vohlc";
    const stockOffset = hasVolume ? 1 : 0;
    const volume = hasVolume ? chart.series[0] : undefined;
    const open = hasOpen ? chart.series[stockOffset] : undefined;
    const high = chart.series[stockOffset + (hasOpen ? 1 : 0)];
    const low = chart.series[stockOffset + (hasOpen ? 2 : 1)];
    const close = chart.series[stockOffset + (hasOpen ? 3 : 2)];
    const stockValues = [high, low, close, open]
      .flatMap((series) => series?.values || [])
      .filter(Number.isFinite);
    const rawMinimum = Math.min(...(stockValues.length ? stockValues : [0]));
    const rawMaximum = Math.max(...(stockValues.length ? stockValues : [1]));
    const stockSpan = Math.max(1, rawMaximum - rawMinimum);
    const minimum = rawMinimum - stockSpan * 0.08;
    const maximum = rawMaximum + stockSpan * 0.08;
    const yForStock = (value: number) => pad.top + ((maximum - value) / (maximum - minimum)) * plotHeight;
    const categoryCount = Math.max(1, categories.length, high?.values.length || 0);
    const categoryWidth = plotWidth / categoryCount;
    const volumeMaximum = Math.max(1, ...(volume?.values || []).filter(Number.isFinite));
    return (
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title} style={{ display: "block", width: "100%", height: "auto" }}>
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const y = pad.top + plotHeight * ratio;
          const value = maximum - (maximum - minimum) * ratio;
          return (
            <g key={ratio}>
              <line x1={pad.left} y1={y} x2={pad.left + plotWidth} y2={y} stroke="#e7e5e4" strokeDasharray="4 4" />
              <text x={pad.left - 8} y={y + 4} textAnchor="end" fontSize="10" fill="#a8a29e">
                {Math.round(value * 100) / 100}
              </text>
            </g>
          );
        })}
        {volume?.values.map((value, index) => {
          if (!Number.isFinite(value)) return null;
          const barWidth = Math.max(3, categoryWidth * 0.64);
          const barHeight = Math.max(1, Number(value) / volumeMaximum * plotHeight);
          return (
            <rect
              key={`volume:${index}`}
              data-chart-plot-type="volume"
              x={pad.left + categoryWidth * index + (categoryWidth - barWidth) / 2}
              y={pad.top + plotHeight - barHeight}
              width={barWidth}
              height={barHeight}
              rx="2"
              fill={colorForSeries(0)}
              opacity="0.24"
            />
          );
        })}
        {Array.from({ length: categoryCount }, (_unused, index) => {
          const highValue = high?.values[index];
          const lowValue = low?.values[index];
          const closeValue = close?.values[index];
          const openValue = open?.values[index];
          if (!Number.isFinite(highValue) || !Number.isFinite(lowValue) || !Number.isFinite(closeValue)) return null;
          const highNumber = Number(highValue);
          const lowNumber = Number(lowValue);
          const closeNumber = Number(closeValue);
          const x = pad.left + categoryWidth * index + categoryWidth / 2;
          const rising = !Number.isFinite(openValue) || closeNumber >= Number(openValue);
          const color = rising ? "#15803d" : "#dc2626";
          return (
            <g key={index} data-chart-plot-type="stock">
              <line x1={x} y1={yForStock(highNumber)} x2={x} y2={yForStock(lowNumber)} stroke={color} strokeWidth="2" />
              {Number.isFinite(openValue) && (
                <line x1={x - Math.min(12, categoryWidth * 0.22)} y1={yForStock(Number(openValue))} x2={x} y2={yForStock(Number(openValue))} stroke={color} strokeWidth="3" />
              )}
              <line x1={x} y1={yForStock(closeNumber)} x2={x + Math.min(12, categoryWidth * 0.22)} y2={yForStock(closeNumber)} stroke={color} strokeWidth="3" />
              {!hasOpen && <circle cx={x} cy={yForStock(closeNumber)} r="2.5" fill={color} />}
            </g>
          );
        })}
        {categories.map((label, index) => index % Math.max(1, Math.ceil(categories.length / 8)) === 0 && (
          <text key={`${label}:${index}`} x={pad.left + categoryWidth * index + categoryWidth / 2} y={height - 38} textAnchor="middle" fontSize="10" fill="#78716c">
            {String(label).slice(0, 12)}
          </text>
        ))}
        {chart.series.map((series, index) => (
          <g key={`${series.name}:legend`} transform={`translate(${pad.left + index * 130} ${height - 15})`}>
            <rect width="11" height="11" rx="2" fill={colorForSeries(index)} />
            <text x="17" y="10" fontSize="10" fill="#57534e">{series.name.slice(0, 17)}</text>
          </g>
        ))}
      </svg>
    );
  }

  const categoryCount = Math.max(1, categories.length, ...chart.series.map((series) => series.values.length));
  const isStacked = chart.grouping === "stacked" || chart.grouping === "percentStacked";
  const normalizedValue = (seriesIndex: number, categoryIndex: number) => {
    const value = chart.series[seriesIndex]?.values[categoryIndex] || 0;
    if (chart.grouping !== "percentStacked") return value;
    const total = chart.series.reduce((sum, series) => sum + Math.abs(series.values[categoryIndex] || 0), 0);
    return total ? value / total * 100 : 0;
  };
  const stackBounds = (seriesIndex: number, categoryIndex: number) => {
    const value = normalizedValue(seriesIndex, categoryIndex);
    if (!isStacked) return [0, value] as const;
    let start = 0;
    for (let index = 0; index < seriesIndex; index += 1) {
      const previous = normalizedValue(index, categoryIndex);
      if ((value >= 0 && previous >= 0) || (value < 0 && previous < 0)) start += previous;
    }
    return [start, start + value] as const;
  };
  const scaleValues = chart.series.flatMap((_series, seriesIndex) => (
    Array.from({ length: categoryCount }, (_unused, categoryIndex) => stackBounds(seriesIndex, categoryIndex)).flat()
  )).filter(Number.isFinite);
  const minimum = Math.min(0, ...scaleValues);
  const maximum = Math.max(1, ...scaleValues);
  const span = Math.max(1, maximum - minimum);
  const yFor = (value: number) => pad.top + ((maximum - value) / span) * plotHeight;
  const xFor = (value: number) => pad.left + ((value - minimum) / span) * plotWidth;
  const zeroY = yFor(0);
  const categoryWidth = plotWidth / categoryCount;
  const categoryHeight = plotHeight / categoryCount;
  const indexedSeries = chart.series.map((series, seriesIndex) => ({ series, seriesIndex }));
  const lineSeries = chart.type === "combo_column_line"
    ? indexedSeries.filter(({ series }) => series.plotType === "line")
    : chart.type === "line" || chart.type === "area" ? indexedSeries : [];
  const columnSeries = chart.type === "combo_column_line"
    ? indexedSeries.filter(({ series }) => series.plotType !== "line")
    : chart.type === "line" || chart.type === "area" ? [] : indexedSeries;

  if (chart.type === "bar") {
    return (
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title} style={{ display: "block", width: "100%", height: "auto" }}>
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const x = pad.left + plotWidth * ratio;
          return <line key={ratio} x1={x} y1={pad.top} x2={x} y2={pad.top + plotHeight} stroke="#e7e5e4" strokeDasharray="4 4" />;
        })}
        {Array.from({ length: categoryCount }, (_unused, categoryIndex) => (
          <g key={categoryIndex}>
            <text x={pad.left - 8} y={pad.top + categoryHeight * (categoryIndex + 0.5) + 4} textAnchor="end" fontSize="10" fill="#78716c">
              {String(categories[categoryIndex] || String(categoryIndex + 1)).slice(0, 10)}
            </text>
            {chart.series.map((_series, seriesIndex) => {
              const [start, end] = stackBounds(seriesIndex, categoryIndex);
              const groupHeight = Math.max(8, categoryHeight * 0.72);
              const barHeight = isStacked ? groupHeight : groupHeight / Math.max(1, chart.series.length);
              const y = pad.top + categoryHeight * categoryIndex + (categoryHeight - groupHeight) / 2
                + (isStacked ? 0 : barHeight * seriesIndex);
              return <rect key={seriesIndex} x={Math.min(xFor(start), xFor(end))} y={y} width={Math.max(1, Math.abs(xFor(end) - xFor(start)))} height={Math.max(2, barHeight - 2)} rx="2" fill={colorForSeries(seriesIndex)} />;
            })}
          </g>
        ))}
        <line x1={xFor(0)} y1={pad.top} x2={xFor(0)} y2={pad.top + plotHeight} stroke="#a8a29e" />
        {chart.series.map((series, index) => (
          <g key={`${series.name}:legend`} transform={`translate(${pad.left + index * 130} ${height - 15})`}>
            <rect width="11" height="11" rx="2" fill={colorForSeries(index)} />
            <text x="17" y="10" fontSize="10" fill="#57534e">{series.name.slice(0, 17)}</text>
          </g>
        ))}
      </svg>
    );
  }

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
      {lineSeries.map(({ series, seriesIndex }) => {
        const points = series.values.map((_value, index) => ({
          x: pad.left + categoryWidth * index + categoryWidth / 2,
          y: yFor(stackBounds(seriesIndex, index)[1]),
        }));
        const baseline = series.values.map((_value, index) => ({
          x: pad.left + categoryWidth * index + categoryWidth / 2,
          y: yFor(stackBounds(seriesIndex, index)[0]),
        })).reverse();
        return (
          <g key={`${series.name}:${seriesIndex}`} data-chart-plot-type="line">
            {chart.type === "area" && <path d={[...points, ...baseline].map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ") + " Z"} fill={colorForSeries(seriesIndex)} opacity="0.28" />}
            <path d={points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ")} fill="none" stroke={colorForSeries(seriesIndex)} strokeWidth="3" />
            {series.showMarkers && points.map((point, index) => <circle key={index} cx={point.x} cy={point.y} r="3.5" fill={colorForSeries(seriesIndex)} />)}
          </g>
        );
      })}
      {columnSeries.flatMap(({ series, seriesIndex }, columnSeriesIndex) => {
        const groupWidth = Math.max(8, categoryWidth * 0.72);
        const barWidth = isStacked ? groupWidth : groupWidth / Math.max(1, columnSeries.length);
        return series.values.map((_value, index) => {
          const [start, end] = stackBounds(seriesIndex, index);
          const x = pad.left + categoryWidth * index + (categoryWidth - groupWidth) / 2
            + (isStacked ? 0 : barWidth * columnSeriesIndex);
          const y = Math.min(yFor(start), yFor(end));
          return (
            <rect
              key={`${seriesIndex}:${index}`}
              data-chart-plot-type="column"
              x={x}
              y={y}
              width={Math.max(2, barWidth - 2)}
              height={Math.max(1, Math.abs(yFor(start) - yFor(end)))}
              rx="2"
              fill={colorForSeries(seriesIndex)}
            />
          );
        });
      })}
      {categories.map((label, index) => index % Math.max(1, Math.ceil(categories.length / 8)) === 0 && (
        <text key={`${label}:${index}`} x={pad.left + categoryWidth * index + categoryWidth / 2} y={height - 38} textAnchor="middle" fontSize="10" fill="#78716c">
          {String(label).slice(0, 12)}
        </text>
      ))}
      {chart.series.map((series, index) => (
        <g key={`${series.name}:legend`} transform={`translate(${pad.left + index * 130} ${height - 15})`}>
          <rect width="11" height="11" rx="2" fill={colorForSeries(index)} />
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
