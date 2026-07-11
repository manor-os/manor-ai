import { forwardRef } from "react";

type DiffRowKind = "context" | "add" | "remove";

type DiffRow = {
  id: string;
  kind: DiffRowKind;
  lineNumber?: number;
  marker: " " | "+" | "-";
  text: string;
};

type EditorLiveInlineDiffProps = {
  content: string;
  diff?: string | null;
  title?: string;
  variant?: "code" | "document" | "markdown";
  onClose?: () => void;
  showHeader?: boolean;
};

function parseHunkHeader(line: string) {
  const match = line.match(/^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
  if (!match) return null;
  return {
    oldLine: Number(match[1]),
    newLine: Number(match[2]),
  };
}

function omittedCountForLine(kind: DiffRowKind, text: string) {
  if (kind !== "add" && kind !== "remove") return 0;
  const match = text.match(/^\.\.\. (\d+) (?:added|removed) lines truncated$/);
  return match ? Number(match[1]) : 0;
}

function buildInlineDiffRows(content: string, diff: string) {
  const contentLines = content.split("\n");
  const rows: DiffRow[] = [];
  let oldCursor = 1;
  let newCursor = 1;
  let contentCursor = 1;
  let inHunk = false;
  let sawHunk = false;

  const pushUnchangedContentRows = (endExclusive: number) => {
    const boundedEnd = Math.min(Math.max(endExclusive, contentCursor), contentLines.length + 1);
    for (let lineNumber = contentCursor; lineNumber < boundedEnd; lineNumber += 1) {
      rows.push({
        id: `context-${lineNumber}-${rows.length}`,
        kind: "context",
        lineNumber,
        marker: " ",
        text: contentLines[lineNumber - 1] ?? "",
      });
    }
    contentCursor = boundedEnd;
  };

  for (const rawLine of diff.split("\n")) {
    const hunk = parseHunkHeader(rawLine);
    if (hunk) {
      pushUnchangedContentRows(hunk.newLine);
      oldCursor = hunk.oldLine;
      newCursor = hunk.newLine;
      contentCursor = hunk.newLine;
      inHunk = true;
      sawHunk = true;
      continue;
    }
    if (!inHunk || rawLine.startsWith("---") || rawLine.startsWith("+++")) {
      continue;
    }
    if (rawLine.startsWith("\\ No newline")) continue;

    if (rawLine.startsWith("...")) {
      rows.push({
        id: `note-${rows.length}`,
        kind: "context",
        marker: " ",
        text: rawLine,
      });
      continue;
    }

    const prefix = rawLine[0];
    const text = rawLine.slice(1);

    if (prefix === "-") {
      const omitted = omittedCountForLine("remove", text);
      rows.push({
        id: `remove-${oldCursor}-${rows.length}`,
        kind: "remove",
        lineNumber: omitted ? undefined : oldCursor,
        marker: "-",
        text,
      });
      oldCursor += omitted || 1;
      continue;
    }

    if (prefix === "+") {
      const omitted = omittedCountForLine("add", text);
      rows.push({
        id: `add-${newCursor}-${rows.length}`,
        kind: "add",
        lineNumber: omitted ? undefined : newCursor,
        marker: "+",
        text,
      });
      newCursor += omitted || 1;
      contentCursor = Math.max(contentCursor, newCursor);
      continue;
    }

    if (prefix === " ") {
      rows.push({
        id: `context-hunk-${newCursor}-${rows.length}`,
        kind: "context",
        lineNumber: newCursor,
        marker: " ",
        text,
      });
      oldCursor += 1;
      newCursor += 1;
      contentCursor = Math.max(contentCursor, newCursor);
    }
  }

  if (sawHunk) {
    pushUnchangedContentRows(contentLines.length + 1);
    return rows;
  }

  diff.split("\n").forEach((line, index) => {
    if (line.startsWith("+") && !line.startsWith("+++")) {
      rows.push({
        id: `fallback-add-${index}`,
        kind: "add",
        marker: "+",
        text: line.slice(1),
      });
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      rows.push({
        id: `fallback-remove-${index}`,
        kind: "remove",
        marker: "-",
        text: line.slice(1),
      });
    }
  });

  return rows;
}

function rowKindClass(kind: DiffRowKind) {
  if (kind === "add") return "editor-live-line-diff-row--add";
  if (kind === "remove") return "editor-live-line-diff-row--remove";
  return "editor-live-line-diff-row--context";
}

const EditorLiveInlineDiff = forwardRef<HTMLDivElement, EditorLiveInlineDiffProps>(
  function EditorLiveInlineDiff({
    content,
    diff,
    title = "AI edit diff",
    variant = "code",
    onClose,
    showHeader = false,
  }, ref) {
    const text = diff?.trim();
    if (!text) return null;

    const rows = buildInlineDiffRows(content, text);
    if (rows.every((row) => row.kind === "context")) return null;

    return (
      <div
        ref={ref}
        className={`editor-live-line-diff editor-live-line-diff--${variant}`}
        role="region"
        aria-label={title}
      >
        {showHeader && (
          <div className="editor-live-line-diff-header">
            <span>{title}</span>
            {onClose && (
              <button
                type="button"
                className="editor-live-line-diff-close"
                aria-label="Close inline diff"
                onClick={onClose}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true">
                  <path
                    d="M6 6l12 12M18 6L6 18"
                    fill="none"
                    stroke="currentColor"
                    strokeLinecap="round"
                    strokeWidth="2"
                  />
                </svg>
              </button>
            )}
          </div>
        )}
        <div className="editor-live-line-diff-content">
          {rows.map((row) => (
            <div key={row.id} className={`editor-live-line-diff-row ${rowKindClass(row.kind)}`}>
              <span className="editor-live-line-diff-line-number">
                {row.lineNumber ?? ""}
              </span>
              <span className="editor-live-line-diff-marker">{row.marker}</span>
              <code className="editor-live-line-diff-text">{row.text || " "}</code>
            </div>
          ))}
        </div>
      </div>
    );
  },
);

export default EditorLiveInlineDiff;
