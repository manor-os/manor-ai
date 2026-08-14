import type { ReactNode } from "react";

export default function MarkdownTable({ children }: { children: ReactNode }) {
  return (
    <div
      className="md-table-scroll"
      role="region"
      aria-label="Scrollable table"
      tabIndex={0}
    >
      <table className="md-table">{children}</table>
    </div>
  );
}
