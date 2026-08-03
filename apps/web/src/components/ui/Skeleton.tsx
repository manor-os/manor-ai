import type { CSSProperties } from "react";
import LoadingSpinner from "./LoadingSpinner";

/* ── Shimmer Skeleton Components ── */

type SkeletonSize = string | number;

function cssSize(value: SkeletonSize | undefined, fallback: string): string | number {
  if (typeof value === "number") return `${value}px`;
  return value || fallback;
}

const panelSurfaceStyle: CSSProperties = {
  background: "var(--glass-card)",
  borderRadius: "var(--radius-card)",
  boxShadow: "var(--shadow-sm)",
};

export function SkeletonLine({
  width,
  height = 14,
  radius,
  className = "",
  style,
}: {
  width?: SkeletonSize;
  height?: SkeletonSize;
  radius?: SkeletonSize;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={`skeleton ${className}`.trim()}
      style={{
        width: cssSize(width, "100%"),
        height: cssSize(height, "14px"),
        borderRadius: radius ? cssSize(radius, "6px") : undefined,
        flexShrink: 0,
        ...style,
      }}
    />
  );
}

export function SkeletonCircle({
  size = 32,
  className = "",
  style,
}: {
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={`skeleton-circle ${className}`.trim()}
      style={{ width: size, height: size, flexShrink: 0, ...style }}
    />
  );
}

export function SkeletonCard() {
  return (
    <div
      style={{
        ...panelSurfaceStyle,
        minHeight: 180,
        padding: 24,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 12,
      }}
    >
      <SkeletonCircle size={48} />
      <SkeletonLine width="60%" height={16} />
      <SkeletonLine width="80%" height={12} />
      <SkeletonLine width="40%" height={12} />
      {/* Action row */}
      <div style={{ display: "flex", gap: 8, marginTop: 8, width: "100%", justifyContent: "center" }}>
        <SkeletonLine width={80} height={32} />
        <SkeletonLine width={80} height={32} />
      </div>
    </div>
  );
}

export function PageLoading({
  label,
  minHeight = 240,
}: {
  label?: string;
  minHeight?: number;
}) {
  return (
    <div
      className="flex h-full items-center justify-center"
      style={{ minHeight }}
      aria-busy="true"
      aria-live="polite"
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12, color: "var(--text-muted)" }}>
        <LoadingSpinner size={20} />
        {label && <span style={{ fontSize: 14, fontWeight: 650 }}>{label}</span>}
      </div>
    </div>
  );
}

export function InlineRowsSkeleton({
  rows = 3,
  dense = false,
}: {
  rows?: number;
  dense?: boolean;
}) {
  return (
    <div style={{ display: "grid", gap: dense ? 6 : 8 }} aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            minHeight: dense ? 28 : 34,
          }}
        >
          <SkeletonCircle size={dense ? 18 : 22} />
          <div style={{ flex: 1, display: "grid", gap: 5 }}>
            <SkeletonLine width={i % 2 === 0 ? "78%" : "58%"} height={dense ? 8 : 10} />
            {!dense && <SkeletonLine width="42%" height={8} />}
          </div>
        </div>
      ))}
    </div>
  );
}

export function ChatMessagesSkeleton({
  rows = 4,
  compact = false,
  maxWidth = "var(--chat-thread-max-width, 920px)",
  className = "",
  style,
}: {
  rows?: number;
  compact?: boolean;
  maxWidth?: SkeletonSize;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={className}
      style={{
        width: "100%",
        display: "grid",
        gap: compact ? 10 : 14,
        padding: compact ? "4px 0" : "6px 0",
        ...style,
      }}
      aria-hidden="true"
    >
      {Array.from({ length: rows }).map((_, i) => {
        const isUser = i % 3 === 1;
        const lineCount = compact ? 2 : i % 2 === 0 ? 3 : 2;
        return (
          <div
            key={i}
            style={{
              width: `min(100%, ${cssSize(maxWidth, "920px")})`,
              margin: "0 auto",
              display: "flex",
              justifyContent: isUser ? "flex-end" : "flex-start",
              alignItems: "flex-start",
              gap: compact ? 8 : 10,
            }}
          >
            {!isUser && <SkeletonCircle size={compact ? 24 : 32} />}
            <div
              style={{
                width: isUser ? (compact ? "58%" : "52%") : compact ? "72%" : "68%",
                maxWidth: isUser ? 520 : 680,
                minWidth: 0,
                padding: compact ? "9px 10px" : "12px 14px",
                borderRadius: isUser
                  ? "14px 14px 4px 14px"
                  : "14px 14px 14px 4px",
                background: "var(--surface-muted)",
                display: "grid",
                gap: compact ? 6 : 8,
              }}
            >
              {Array.from({ length: lineCount }).map((_, lineIndex) => (
                <SkeletonLine
                  key={lineIndex}
                  width={
                    lineIndex === lineCount - 1
                      ? isUser
                        ? "54%"
                        : "46%"
                      : lineIndex % 2 === 0
                        ? "92%"
                        : "76%"
                  }
                  height={compact ? 9 : 11}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function ListRowsSkeleton({
  rows = 5,
  avatar = true,
  action = false,
  rowHeight = 48,
  surface = true,
}: {
  rows?: number;
  avatar?: boolean;
  action?: boolean;
  rowHeight?: number;
  surface?: boolean;
}) {
  return (
    <div style={{ display: "grid", gap: 8 }} aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          style={{
            ...(surface
              ? panelSurfaceStyle
              : { background: "var(--surface-muted)", borderRadius: "var(--radius-control)" }),
            minHeight: rowHeight,
            padding: "10px 12px",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          {avatar && <SkeletonCircle size={32} />}
          <div style={{ flex: 1, minWidth: 0, display: "grid", gap: 7 }}>
            <SkeletonLine width={i % 3 === 0 ? "68%" : "48%"} height={12} />
            <SkeletonLine width={i % 2 === 0 ? "86%" : "62%"} height={9} />
          </div>
          {action && <SkeletonLine width={56} height={24} radius={999} />}
        </div>
      ))}
    </div>
  );
}

export function TableRowsSkeleton({
  rows = 5,
  cols = 4,
  showHeader = true,
}: {
  rows?: number;
  cols?: number;
  showHeader?: boolean;
}) {
  return (
    <div
      className="glass-table"
      style={{
        width: "100%",
        borderCollapse: "collapse",
        overflow: "hidden",
        borderRadius: "var(--radius-card)",
        background: "var(--glass-card)",
        boxShadow: "var(--shadow-sm)",
      }}
      aria-hidden="true"
    >
      {showHeader && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
            gap: 12,
            padding: "14px 20px",
            background: "var(--surface-muted)",
            borderBottom: "1px solid var(--glass-hairline)",
          }}
        >
          {Array.from({ length: cols }).map((_, i) => (
            <SkeletonLine key={`h-${i}`} width="70%" height={12} />
          ))}
        </div>
      )}
      {Array.from({ length: rows }).map((_, r) => (
        <div
          key={`r-${r}`}
          style={{
            display: "grid",
            gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
            gap: 12,
            padding: "14px 20px",
            borderBottom: r < rows - 1 ? "1px solid var(--glass-hairline)" : undefined,
          }}
        >
          {Array.from({ length: cols }).map((_, c) => (
            <SkeletonLine key={`r${r}-c${c}`} width={c === 0 ? "90%" : "60%"} height={14} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function SkeletonTable({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return <TableRowsSkeleton rows={rows} cols={cols} />;
}

export function CardGridSkeleton({
  count = 6,
  minWidth = 260,
}: {
  count?: number;
  minWidth?: number;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${minWidth}px), 1fr))`,
        gap: 14,
      }}
      aria-hidden="true"
    >
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  );
}

export function MetricGridSkeleton({
  count = 4,
  minWidth = 170,
}: {
  count?: number;
  minWidth?: number;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(auto-fit, minmax(min(100%, ${minWidth}px), 1fr))`,
        gap: 12,
      }}
      aria-hidden="true"
    >
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          style={{
            ...panelSurfaceStyle,
            padding: 16,
            minHeight: 104,
            display: "grid",
            gap: 14,
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
            <SkeletonLine width={i % 2 === 0 ? 92 : 68} height={12} />
            <SkeletonCircle size={20} />
          </div>
          <SkeletonLine width={64} height={30} />
        </div>
      ))}
    </div>
  );
}

export function PanelLoading({
  label,
  rows = 3,
  minHeight = 156,
}: {
  label?: string;
  rows?: number;
  minHeight?: number;
}) {
  return (
    <div
      style={{
        ...panelSurfaceStyle,
        minHeight,
        padding: 16,
        display: "grid",
        gap: 14,
      }}
      aria-busy="true"
      aria-live="polite"
    >
      {label && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--text-muted)", fontSize: 13, fontWeight: 650 }}>
          <LoadingSpinner size={16} />
          <span>{label}</span>
        </div>
      )}
      <ListRowsSkeleton rows={rows} surface={false} />
    </div>
  );
}

export function SkeletonDashboard() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {/* Greeting */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <SkeletonLine width={200} height={24} />
        <SkeletonLine width={320} height={14} />
      </div>

      {/* Brief panel */}
      <div
        style={{
          ...panelSurfaceStyle,
          padding: 24,
        }}
      >
        <SkeletonLine width={140} height={16} />
        <div style={{ marginTop: 12 }}>
          <SkeletonLine width="90%" height={14} />
        </div>
        <div style={{ marginTop: 8 }}>
          <SkeletonLine width="70%" height={14} />
        </div>
      </div>

      {/* 3 metric cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 200px), 1fr))", gap: 16 }}>
        {[1, 2, 3].map((i) => (
          <div
            key={i}
            style={{
              ...panelSurfaceStyle,
              padding: 20,
              display: "flex",
              flexDirection: "column",
              gap: 10,
            }}
          >
            <SkeletonLine width={80} height={12} />
            <SkeletonLine width={60} height={28} />
            <SkeletonLine width="50%" height={10} />
          </div>
        ))}
      </div>

      {/* 2-column bottom */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))", gap: 16 }}>
        <div
          style={{
            ...panelSurfaceStyle,
            padding: 20,
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <SkeletonLine width={120} height={16} />
          {[1, 2, 3, 4].map((i) => (
            <SkeletonLine key={i} height={40} />
          ))}
        </div>
        <div
          style={{
            ...panelSurfaceStyle,
            padding: 20,
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <SkeletonLine width={120} height={16} />
          {[1, 2, 3].map((i) => (
            <SkeletonLine key={i} height={48} />
          ))}
        </div>
      </div>
    </div>
  );
}

export function SkeletonList({ count = 5 }: { count?: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  );
}
