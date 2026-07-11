/* ── Shimmer Skeleton Components ── */

export function SkeletonLine({ width, height = 14 }: { width?: string | number; height?: number }) {
  return (
    <div
      className="skeleton"
      style={{
        width: typeof width === "number" ? `${width}px` : width || "100%",
        height,
        flexShrink: 0,
      }}
    />
  );
}

export function SkeletonCircle({ size = 32 }: { size?: number }) {
  return (
    <div
      className="skeleton-circle"
      style={{ width: size, height: size, flexShrink: 0 }}
    />
  );
}

export function SkeletonCard() {
  return (
    <div
      style={{
        background: "rgba(255,255,255,0.65)",
        border: "1px solid rgba(28,25,23,0.06)",
        borderRadius: 20,
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

export function SkeletonTable({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div
      className="glass-table"
      style={{
        width: "100%",
        borderCollapse: "collapse",
        overflow: "hidden",
        borderRadius: 16,
        border: "1px solid rgba(28,25,23,0.06)",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${cols}, 1fr)`,
          gap: 12,
          padding: "14px 20px",
          background: "rgba(250,250,249,0.8)",
          borderBottom: "1px solid rgba(28,25,23,0.06)",
        }}
      >
        {Array.from({ length: cols }).map((_, i) => (
          <SkeletonLine key={`h-${i}`} width="70%" height={12} />
        ))}
      </div>
      {/* Rows */}
      {Array.from({ length: rows }).map((_, r) => (
        <div
          key={`r-${r}`}
          style={{
            display: "grid",
            gridTemplateColumns: `repeat(${cols}, 1fr)`,
            gap: 12,
            padding: "14px 20px",
            borderBottom: r < rows - 1 ? "1px solid rgba(231,229,228,0.4)" : undefined,
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
          background: "rgba(255,255,255,0.65)",
          border: "1px solid rgba(28,25,23,0.06)",
          borderRadius: 20,
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
              background: "rgba(255,255,255,0.65)",
              border: "1px solid rgba(28,25,23,0.06)",
              borderRadius: 20,
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
            background: "rgba(255,255,255,0.65)",
            border: "1px solid rgba(28,25,23,0.06)",
            borderRadius: 20,
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
            background: "rgba(255,255,255,0.65)",
            border: "1px solid rgba(28,25,23,0.06)",
            borderRadius: 20,
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
