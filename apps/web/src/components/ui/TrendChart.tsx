import { useEffect, useMemo, useState, useRef, useCallback } from "react";

/* ── types ─────────────────────────────────────────────── */

export interface TrendDataPoint {
  label: string;
  value: number;
  value2?: number;
}

export interface TrendChartProps {
  data: TrendDataPoint[];
  height?: number;
  color?: string;
  color2?: string;
  type?: "line" | "bar" | "area";
  showLabels?: boolean;
  showGrid?: boolean;
}

/* ── helpers ────────────────────────────────────────────── */

const PAD_LEFT = 40;
const PAD_RIGHT = 16;
const PAD_TOP = 16;
const PAD_BOTTOM = 28;

function niceMax(v: number): number {
  if (v <= 0) return 10;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const norm = v / mag;
  if (norm <= 1) return mag;
  if (norm <= 2) return 2 * mag;
  if (norm <= 5) return 5 * mag;
  return 10 * mag;
}

function smoothPath(points: [number, number][]): string {
  if (points.length === 0) return "";
  if (points.length === 1) return `M ${points[0][0]} ${points[0][1]}`;

  let d = `M ${points[0][0]} ${points[0][1]}`;
  for (let i = 0; i < points.length - 1; i++) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[i + 1];
    const cpx = (x0 + x1) / 2;
    d += ` C ${cpx} ${y0}, ${cpx} ${y1}, ${x1} ${y1}`;
  }
  return d;
}

/* ── component ──────────────────────────────────────────── */

export default function TrendChart({
  data,
  height = 200,
  color = "#4f7d75",
  color2 = "#5f84bd",
  type = "area",
  showLabels = true,
  showGrid = true,
}: TrendChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [containerWidth, setContainerWidth] = useState(600);
  const [tooltip, setTooltip] = useState<{
    x: number;
    y: number;
    label: string;
    value: number;
    value2?: number;
  } | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => {
      const width = el.getBoundingClientRect().width;
      if (width > 0) setContainerWidth(Math.max(240, Math.round(width)));
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const chartW = containerWidth;
  const chartH = height;
  const plotW = chartW - PAD_LEFT - PAD_RIGHT;
  const plotH = chartH - PAD_TOP - PAD_BOTTOM;

  const { maxVal, gridLines, points1, points2, bars1 } = useMemo(() => {
    if (data.length === 0) {
      return { maxVal: 10, gridLines: [] as number[], points1: [] as [number, number][], points2: [] as [number, number][], bars1: [] as { x: number; w: number; h: number; h2: number; y: number; y2: number }[] };
    }

    const allVals = data.flatMap((d) => [d.value, d.value2 ?? 0]);
    const rawMax = Math.max(...allVals, 1);
    const max = niceMax(rawMax);

    // Grid lines (4 horizontal)
    const lines: number[] = [];
    for (let i = 0; i <= 4; i++) {
      lines.push(Math.round((max / 4) * i));
    }

    const n = data.length;
    const step = n > 1 ? plotW / (n - 1) : plotW;
    const barGap = 4;
    const barW = n > 0 ? Math.min(Math.max((plotW / n) - barGap, 4), 40) : 20;

    const pts1: [number, number][] = [];
    const pts2: [number, number][] = [];
    const brs: { x: number; w: number; h: number; h2: number; y: number; y2: number }[] = [];

    data.forEach((d, i) => {
      const x = n > 1 ? PAD_LEFT + i * step : PAD_LEFT + plotW / 2;
      const y1 = PAD_TOP + plotH - (d.value / max) * plotH;
      const y2 = PAD_TOP + plotH - ((d.value2 ?? 0) / max) * plotH;
      pts1.push([x, y1]);
      if (d.value2 !== undefined) pts2.push([x, y2]);

      const bx = PAD_LEFT + (plotW / n) * i + barGap / 2;
      const h1 = (d.value / max) * plotH;
      const h2val = ((d.value2 ?? 0) / max) * plotH;
      brs.push({
        x: bx,
        w: barW,
        h: h1,
        h2: h2val,
        y: PAD_TOP + plotH - h1,
        y2: PAD_TOP + plotH - h2val,
      });
    });

    return { maxVal: max, gridLines: lines, points1: pts1, points2: pts2, bars1: brs };
  }, [data, plotW, plotH]);

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (!svgRef.current || data.length === 0) return;
      const rect = svgRef.current.getBoundingClientRect();
      const scaleX = chartW / rect.width;
      const mx = (e.clientX - rect.left) * scaleX;

      // Find closest data point
      const n = data.length;
      const step = n > 1 ? plotW / (n - 1) : plotW;
      let closest = 0;
      let minDist = Infinity;
      for (let i = 0; i < n; i++) {
        const x = n > 1 ? PAD_LEFT + i * step : PAD_LEFT + plotW / 2;
        const dist = Math.abs(mx - x);
        if (dist < minDist) {
          minDist = dist;
          closest = i;
        }
      }

      if (minDist < step * 0.6 || n === 1) {
        const d = data[closest];
        const x = n > 1 ? PAD_LEFT + closest * step : PAD_LEFT + plotW / 2;
        const y = PAD_TOP + plotH - (d.value / maxVal) * plotH;
        setTooltip({ x, y, label: d.label, value: d.value, value2: d.value2 });
      } else {
        setTooltip(null);
      }
    },
    [data, plotW, plotH, maxVal, chartW],
  );

  const gradientId = `trend-grad-${color.replace("#", "")}`;
  const gradientId2 = `trend-grad-${color2.replace("#", "")}`;

  return (
    <div ref={containerRef} className="trend-chart" style={{ position: "relative", width: "100%" }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${chartW} ${chartH}`}
        style={{ width: "100%", height, display: "block" }}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setTooltip(null)}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.3} />
            <stop offset="100%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
          <linearGradient id={gradientId2} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color2} stopOpacity={0.3} />
            <stop offset="100%" stopColor={color2} stopOpacity={0.02} />
          </linearGradient>
        </defs>

        {/* Grid lines */}
        {showGrid &&
          gridLines.map((val, i) => {
            const y = PAD_TOP + plotH - (val / maxVal) * plotH;
            return (
              <g key={`grid-${i}`}>
                <line
                  x1={PAD_LEFT}
                  y1={y}
                  x2={chartW - PAD_RIGHT}
                  y2={y}
                  stroke="var(--trend-chart-grid, #e7e5e4)"
                  strokeWidth={1}
                  strokeDasharray="4 4"
                />
                <text
                  x={PAD_LEFT - 8}
                  y={y}
                  fontSize={10}
                  fill="var(--trend-chart-label, #a8a29e)"
                  textAnchor="end"
                  dominantBaseline="middle"
                >
                  {val}
                </text>
              </g>
            );
          })}

        {/* Bar chart */}
        {type === "bar" &&
          bars1.map((b, i) => (
            <g key={`bar-${i}`}>
              {/* Primary bar */}
              <rect
                x={data[i].value2 !== undefined ? b.x : b.x}
                y={b.y}
                width={data[i].value2 !== undefined ? b.w * 0.45 : b.w}
                height={Math.max(b.h, 0)}
                rx={4}
                ry={4}
                fill={color}
                opacity={0.85}
              />
              {/* Secondary bar */}
              {data[i].value2 !== undefined && (
                <rect
                  x={b.x + b.w * 0.55}
                  y={b.y2}
                  width={b.w * 0.45}
                  height={Math.max(b.h2, 0)}
                  rx={4}
                  ry={4}
                  fill={color2}
                  opacity={0.85}
                />
              )}
            </g>
          ))}

        {/* Line / Area chart */}
        {(type === "line" || type === "area") && points1.length > 0 && (
          <>
            {/* Area fill */}
            {type === "area" && (
              <path
                d={`${smoothPath(points1)} L ${points1[points1.length - 1][0]} ${PAD_TOP + plotH} L ${points1[0][0]} ${PAD_TOP + plotH} Z`}
                fill={`url(#${gradientId})`}
              />
            )}
            {/* Line */}
            <path
              d={smoothPath(points1)}
              fill="none"
              stroke={color}
              strokeWidth={2.5}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            {/* Dots */}
            {points1.map(([x, y], i) => (
              <circle key={`dot1-${i}`} cx={x} cy={y} r={3} fill={color} stroke="var(--trend-chart-dot-stroke, #ffffff)" strokeWidth={1.5} />
            ))}
          </>
        )}

        {/* Second series */}
        {(type === "line" || type === "area") && points2.length > 0 && (
          <>
            {type === "area" && (
              <path
                d={`${smoothPath(points2)} L ${points2[points2.length - 1][0]} ${PAD_TOP + plotH} L ${points2[0][0]} ${PAD_TOP + plotH} Z`}
                fill={`url(#${gradientId2})`}
              />
            )}
            <path
              d={smoothPath(points2)}
              fill="none"
              stroke={color2}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeDasharray="6 3"
            />
            {points2.map(([x, y], i) => (
              <circle key={`dot2-${i}`} cx={x} cy={y} r={2.5} fill={color2} stroke="var(--trend-chart-dot-stroke, #ffffff)" strokeWidth={1.5} />
            ))}
          </>
        )}

        {/* X-axis labels */}
        {showLabels &&
          data.map((d, i) => {
            const n = data.length;
            const step = n > 1 ? plotW / (n - 1) : plotW;
            const x = type === "bar"
              ? bars1[i]?.x + (bars1[i]?.w ?? 0) / 2
              : n > 1
                ? PAD_LEFT + i * step
                : PAD_LEFT + plotW / 2;
            // Show every label if <= 14 items, otherwise skip some
            if (n > 14 && i % Math.ceil(n / 14) !== 0 && i !== n - 1) return null;
            return (
              <text
                key={`label-${i}`}
                x={x}
                y={chartH - 4}
                fontSize={10}
                fill="var(--trend-chart-label, #a8a29e)"
                textAnchor="middle"
              >
                {d.label}
              </text>
            );
          })}

        {/* Tooltip crosshair */}
        {tooltip && (
          <>
            <line
              x1={tooltip.x}
              y1={PAD_TOP}
              x2={tooltip.x}
              y2={PAD_TOP + plotH}
              stroke="var(--trend-chart-crosshair, #d6d3d1)"
              strokeWidth={1}
              strokeDasharray="3 3"
            />
            <circle cx={tooltip.x} cy={tooltip.y} r={5} fill={color} stroke="var(--trend-chart-dot-stroke, #ffffff)" strokeWidth={2} />
          </>
        )}
      </svg>

      {/* Tooltip popup */}
      {tooltip && (
        <div
          style={{
            position: "absolute",
            left: `${(tooltip.x / chartW) * 100}%`,
            top: `${(tooltip.y / chartH) * 100 - 14}%`,
            transform: "translate(-50%, -100%)",
            background: "var(--trend-chart-tooltip-bg, rgba(255,255,255,0.95))",
            backdropFilter: "blur(12px)",
            WebkitBackdropFilter: "blur(12px)",
            borderRadius: 10,
            border: "1px solid var(--trend-chart-tooltip-border, rgba(28,25,23,0.06))",
            boxShadow: "var(--trend-chart-tooltip-shadow, 0 4px 12px rgba(0,0,0,0.08))",
            padding: "6px 12px",
            pointerEvents: "none",
            whiteSpace: "nowrap",
            zIndex: 10,
          }}
        >
          <div style={{ fontSize: 11, fontWeight: 600, color: "var(--trend-chart-tooltip-label, #78716c)", marginBottom: 2 }}>
            {tooltip.label}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 700, color }}>
              {tooltip.value}
            </span>
            {tooltip.value2 !== undefined && (
              <span style={{ fontSize: 14, fontWeight: 700, color: color2 }}>
                {tooltip.value2}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
