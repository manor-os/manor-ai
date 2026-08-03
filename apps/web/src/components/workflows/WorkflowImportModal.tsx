import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { useToastStore } from "../../stores/toast";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Textarea from "../ui/Textarea";
import Input from "../ui/Input";

interface ImportReport {
  source_tool: string;
  node_count: number;
  mapped: number;
  unmapped_count: number;
  unmapped: { id: string; original_type: string; reason: string }[];
  warnings: string[];
  coverage: number;
}

interface PreviewResult {
  report: ImportReport;
  definition: { name: string; steps: { type: string }[] };
}

const PLATFORM_LABEL: Record<string, string> = {
  dify: "Dify",
  n8n: "n8n",
  comfyui: "ComfyUI",
};

/** Paste a ComfyUI / n8n / Dify export → preview coverage → import. */
export default function WorkflowImportModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToastStore();
  const [content, setContent] = useState("");
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<PreviewResult | null>(null);

  const reset = () => {
    setContent("");
    setName("");
    setPreview(null);
  };

  const previewMut = useMutation({
    mutationFn: () => api.workflows.importPreview(content, name || undefined),
    onSuccess: (res: PreviewResult) => setPreview(res),
    onError: (e) => toast.error(translateApiError(e, "Unrecognised workflow format")),
  });

  const importMut = useMutation({
    mutationFn: () => api.workflows.import({ content, name: name || undefined }),
    onSuccess: (res: { workflow: { name: string } }) => {
      toast.success(`Imported "${res.workflow?.name ?? "workflow"}"`);
      qc.invalidateQueries({ queryKey: ["workflows"] });
      reset();
      onClose();
    },
    onError: (e) => toast.error(translateApiError(e, "Import failed")),
  });

  const nodeDist = useMemo(() => {
    if (!preview) return [] as [string, number][];
    const counts: Record<string, number> = {};
    for (const s of preview.definition.steps) counts[s.type] = (counts[s.type] || 0) + 1;
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [preview]);

  const coveragePct = preview ? Math.round(preview.report.coverage * 100) : 0;
  const coverageTone =
    coveragePct >= 80 ? "var(--accent)" : coveragePct >= 40 ? "#b8860b" : "var(--text-faint)";

  return (
    <Modal
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Import workflow"
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={() => { reset(); onClose(); }}>
            Cancel
          </Button>
          {!preview ? (
            <Button
              onClick={() => previewMut.mutate()}
              loading={previewMut.isPending}
              disabled={!content.trim()}
            >
              Preview
            </Button>
          ) : (
            <Button onClick={() => importMut.mutate()} loading={importMut.isPending}>
              Import {PLATFORM_LABEL[preview.report.source_tool] ?? "workflow"}
            </Button>
          )}
        </div>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>
          Paste a workflow exported from <strong>ComfyUI</strong> (.json),{" "}
          <strong>n8n</strong> (.json) or <strong>Dify</strong> (.yml). The format is
          detected automatically; nodes with no Manor equivalent are kept as placeholders.
        </p>

        <Input
          placeholder="Name (optional — taken from the export if blank)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />

        <Textarea
          placeholder="Paste the exported workflow JSON / YAML here…"
          value={content}
          onChange={(e) => {
            setContent(e.target.value);
            if (preview) setPreview(null);
          }}
          rows={8}
        />

        {preview && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 12,
              background: "var(--surface-muted)",
              borderRadius: 12,
              padding: 14,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: 0.3,
                  textTransform: "uppercase",
                  color: "var(--accent)",
                  background: "var(--accent-soft)",
                  padding: "3px 8px",
                  borderRadius: 999,
                }}
              >
                {PLATFORM_LABEL[preview.report.source_tool] ?? preview.report.source_tool}
              </span>
              <span style={{ fontSize: 13, color: "var(--text-strong)", fontWeight: 600 }}>
                {preview.definition.name}
              </span>
            </div>

            {/* coverage bar */}
            <div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: 12,
                  color: "var(--text-muted)",
                  marginBottom: 4,
                }}
              >
                <span>Coverage</span>
                <span className="mono" style={{ color: coverageTone, fontWeight: 600 }}>
                  {coveragePct}% · {preview.report.mapped}/{preview.report.node_count} nodes
                </span>
              </div>
              <div
                style={{
                  height: 6,
                  borderRadius: 999,
                  background: "var(--surface-sunken)",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: `${coveragePct}%`,
                    height: "100%",
                    background: coverageTone,
                    transition: "width .3s ease",
                  }}
                />
              </div>
            </div>

            {/* node type distribution */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {nodeDist.map(([type, n]) => (
                <span
                  key={type}
                  style={{
                    fontSize: 11,
                    color: type === "unsupported" ? "var(--text-faint)" : "var(--text-muted)",
                    background: "var(--surface-app)",
                    padding: "3px 8px",
                    borderRadius: 999,
                  }}
                >
                  {type} <span className="mono">×{n}</span>
                </span>
              ))}
            </div>

            {preview.report.unmapped_count > 0 && (
              <details>
                <summary style={{ fontSize: 12, color: "var(--text-muted)", cursor: "pointer" }}>
                  {preview.report.unmapped_count} unmapped node
                  {preview.report.unmapped_count === 1 ? "" : "s"} (kept as placeholders)
                </summary>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                  {Array.from(new Set(preview.report.unmapped.map((u) => u.original_type))).map(
                    (ot) => (
                      <span
                        key={ot}
                        className="mono"
                        style={{
                          fontSize: 11,
                          color: "var(--text-faint)",
                          background: "var(--surface-app)",
                          padding: "3px 8px",
                          borderRadius: 6,
                        }}
                      >
                        {ot}
                      </span>
                    ),
                  )}
                </div>
              </details>
            )}

            {preview.report.warnings.map((w, i) => (
              <p key={i} style={{ fontSize: 12, color: "#b8860b", margin: 0 }}>
                ⚠ {w}
              </p>
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}
