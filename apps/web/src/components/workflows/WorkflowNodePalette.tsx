import { useMemo, useState } from "react";
import { TYPE_META, NodeIcon } from "./WorkflowCanvas";
import Input from "../ui/Input";

/* A grouped, searchable palette of the canonical node types. Clicking a node
   adds it to the workflow. Mirrors the catalogue ComfyUI / n8n surface, backed
   by manor's native node types + tool/MCP ecosystem. */

export const NODE_GROUPS: { group: string; types: string[] }[] = [
  { group: "Triggers", types: ["trigger", "webhook"] },
  { group: "AI", types: ["llm", "agent", "rag", "classifier", "extract"] },
  { group: "Media", types: ["image", "video", "audio"] },
  { group: "Logic", types: ["condition", "switch", "loop", "parallel", "merge", "transform", "filter", "aggregate", "split", "limit", "sort", "dedupe", "datetime", "wait"] },
  { group: "I/O", types: ["http", "connector", "code", "tool", "subworkflow", "extractfromfile", "respond"] },
  { group: "Lifecycle", types: ["notify", "stop", "end"] },
  { group: "Annotate", types: ["note"] },
];

function meta(t: string) {
  return TYPE_META[t] || { color: "#9b938c", label: t.toUpperCase() };
}

export default function WorkflowNodePalette({ onPick }: { onPick: (type: string) => void }) {
  const [q, setQ] = useState("");

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return NODE_GROUPS;
    return NODE_GROUPS.map((g) => ({
      ...g,
      types: g.types.filter(
        (t) => t.includes(needle) || meta(t).label.toLowerCase().includes(needle),
      ),
    })).filter((g) => g.types.length > 0);
  }, [q]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Input placeholder="Search nodes…" value={q} onChange={(e) => setQ(e.target.value)} />

      <div style={{ maxHeight: 360, overflowY: "auto", display: "flex", flexDirection: "column", gap: 14 }}>
        {groups.map((g) => (
          <div key={g.group}>
            <div
              style={{
                fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
                color: "var(--text-faint)", marginBottom: 8,
              }}
            >
              {g.group}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 8 }}>
              {g.types.map((t) => {
                const m = meta(t);
                return (
                  <button
                    key={t}
                    onClick={() => onPick(t)}
                    style={{
                      display: "flex", alignItems: "center", gap: 8,
                      padding: "9px 11px", borderRadius: 10, cursor: "pointer",
                      background: "var(--surface-muted)", border: "none", textAlign: "left",
                      transition: "background .15s",
                    }}
                    onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "var(--surface-sunken)"; }}
                    onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "var(--surface-muted)"; }}
                  >
                    <span
                      style={{
                        width: 26, height: 26, borderRadius: 7, flexShrink: 0,
                        background: `${m.color}1a`, color: m.color,
                        display: "flex", alignItems: "center", justifyContent: "center",
                      }}
                    >
                      <NodeIcon type={t} size={15} />
                    </span>
                    <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text-strong)" }}>{m.label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
        {groups.length === 0 && (
          <div style={{ fontSize: 13, color: "var(--text-faint)", padding: "12px 0" }}>No matching nodes.</div>
        )}
      </div>
    </div>
  );
}
