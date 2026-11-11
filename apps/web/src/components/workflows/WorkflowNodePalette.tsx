import { useMemo, useState } from "react";
import { TYPE_META, NodeIcon } from "./WorkflowCanvas";
import Input from "../ui/Input";
import { t } from "../../lib/i18n";

/* A grouped, searchable palette of the canonical node types. Clicking a node
   adds it to the workflow. Mirrors the catalogue ComfyUI / n8n surface, backed
   by manor's native node types + tool/MCP ecosystem. */

export const NODE_GROUPS: { group: string; types: string[] }[] = [
  { group: "Triggers", types: ["trigger", "webhook"] },
  // "rag" is hidden while document indexing is paused; existing rag steps
  // still render and run.
  { group: "AI", types: ["llm", "agent", "classifier", "extract"] },
  { group: "Media", types: ["image", "video", "audio"] },
  { group: "Logic", types: ["condition", "switch", "loop", "parallel", "merge", "transform", "filter", "aggregate", "split", "limit", "sort", "dedupe", "datetime", "wait"] },
  { group: "I/O", types: ["http", "connector", "code", "tool", "subworkflow", "extractfromfile", "respond"] },
  { group: "Lifecycle", types: ["workflow_effect_deadline", "publication_receipt", "notify", "stop", "end"] },
  { group: "Annotate", types: ["note"] },
];

function meta(type: string) {
  return TYPE_META[type] || { color: "#9b938c", label: type };
}

// The steps people actually mean; every other node type waits under "More".
const COMMON_STEPS: { type: string; label: string }[] = [
  { type: "agent", label: t("component.workflow_palette.agent") },
  { type: "wait", label: t("component.workflow_palette.wait") },
  { type: "condition", label: t("component.workflow_palette.condition") },
  { type: "loop", label: t("component.workflow_palette.loop") },
  { type: "connector", label: t("component.workflow_palette.connector") },
  { type: "notify", label: t("component.workflow_palette.notify") },
  { type: "subworkflow", label: t("component.workflow_palette.subworkflow") },
];

export default function WorkflowNodePalette({ onPick }: { onPick: (type: string, label?: string) => void }) {
  const [q, setQ] = useState("");
  const [showAll, setShowAll] = useState(false);

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return NODE_GROUPS;
    return NODE_GROUPS.map((g) => ({
      ...g,
      types: g.types.filter(
        (type) => type.includes(needle) || meta(type).label.toLowerCase().includes(needle),
      ),
    })).filter((g) => g.types.length > 0);
  }, [q]);

  if (!showAll) {
    return (
      <div className="workflow-palette-common">
        {COMMON_STEPS.map((step) => (
          <button key={step.type} type="button" className="workflow-palette-row" onClick={() => onPick(step.type, step.label)}>
            <NodeIcon type={step.type} size={16} />
            <span>{step.label}</span>
          </button>
        ))}
        <button type="button" className="workflow-palette-more" onClick={() => setShowAll(true)}>
          {t("component.workflow_palette.more")}
        </button>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Input placeholder={t("component.workflow_palette.search")} value={q} onChange={(e) => setQ(e.target.value)} />

      <div style={{ maxHeight: 360, overflowY: "auto", display: "flex", flexDirection: "column", gap: 14 }}>
        {groups.map((g) => (
          <div key={g.group}>
            <div
              style={{
                fontSize: 11, fontWeight: 700,
                color: "var(--text-faint)", marginBottom: 8,
              }}
            >
              {g.group}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 8 }}>
              {g.types.map((type) => {
                const m = meta(type);
                return (
                  <button
                    key={type}
                    onClick={() => onPick(type)}
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
                      <NodeIcon type={type} size={15} />
                    </span>
                    <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text-strong)" }}>{m.label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
        {groups.length === 0 && (
          <div style={{ fontSize: 13, color: "var(--text-faint)", padding: "12px 0" }}>{t("component.workflow_palette.no_match")}</div>
        )}
      </div>
    </div>
  );
}
