import { useState } from "react";
import { contentFold } from "../../lib/contentFold";
import { parseDelimitedText } from "../../lib/delimitedText";
import { t } from "../../lib/i18n";

/**
 * A delimited-text file (CSV/TSV) as a read-only table.
 *
 * ``compact`` is the chat form: cells stay on one line and the table scrolls
 * sideways, and a long table shows its first rows until expanded. The file
 * preview page uses the full-page table.
 */
export function CsvPreview({ content, compact = false }: { content: string; compact?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const rows = parseDelimitedText(content).rows;
  if (!rows.length) {
    return <p style={{ color: "var(--text-muted)", textAlign: "center", padding: "24px 0" }}>{t("page.shared_doc.preview_unavailable")}</p>;
  }
  const columnCount = Math.max(1, ...rows.map((row) => row.length));
  const header = <thead><tr>{Array.from({ length: columnCount }, (_, index) => <th key={index}>{rows[0]?.[index] ?? ""}</th>)}</tr></thead>;
  if (!compact) {
    return (
      <div style={{ overflowX: "auto" }}>
        <table className="glass-table">
          {header}
          <tbody>{rows.slice(1).map((row, rowIndex) => <tr key={rowIndex}>{Array.from({ length: columnCount }, (_, columnIndex) => <td key={columnIndex} style={{ whiteSpace: "pre-wrap" }}>{row[columnIndex] ?? ""}</td>)}</tr>)}</tbody>
        </table>
      </div>
    );
  }
  const body = rows.slice(1);
  const fold = contentFold("table", body.length);
  const shown = fold && !expanded ? body.slice(0, fold.visible) : body;
  return (
    <div className="chat-table-preview">
      <div className={`chat-table-preview__scroll${fold && !expanded ? " is-folded" : ""}`}>
        <table>
          {header}
          <tbody>{shown.map((row, rowIndex) => <tr key={rowIndex}>{Array.from({ length: columnCount }, (_, columnIndex) => <td key={columnIndex} title={row[columnIndex] || undefined}>{row[columnIndex] ?? ""}</td>)}</tr>)}</tbody>
        </table>
      </div>
      {fold && (
        <button type="button" className={`content-fold-toggle${expanded ? " content-fold-toggle--pinned" : ""}`}
          aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded
            ? t("component.content_fold.show_less")
            : t("component.content_fold.show_more_rows", { count: fold.hidden })}
        </button>
      )}
    </div>
  );
}
