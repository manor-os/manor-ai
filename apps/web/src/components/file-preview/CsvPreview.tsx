import { parseDelimitedText } from "../../lib/delimitedText";
import { t } from "../../lib/i18n";

/** A delimited-text file (CSV/TSV) as a read-only table. */
export function CsvPreview({ content }: { content: string }) {
  const rows = parseDelimitedText(content).rows;
  if (!rows.length) {
    return <p style={{ color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>{t("page.shared_doc.preview_unavailable")}</p>;
  }
  const columnCount = Math.max(1, ...rows.map((row) => row.length));
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="glass-table">
        <thead><tr>{Array.from({ length: columnCount }, (_, index) => <th key={index}>{rows[0]?.[index] ?? ""}</th>)}</tr></thead>
        <tbody>{rows.slice(1).map((row, rowIndex) => <tr key={rowIndex}>{Array.from({ length: columnCount }, (_, columnIndex) => <td key={columnIndex} style={{ whiteSpace: "pre-wrap" }}>{row[columnIndex] ?? ""}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}
