import { type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";
import { contentFold, type ContentFold, type ContentFoldKind } from "../../lib/contentFold";
import { t } from "../../lib/i18n";

type Box = { top: number; bottom: number };
type RenderedLines = { count: number; bottoms: number[] };

/** Screen lines a very long paragraph may wrap to before each extra three count as a line. */
const SCREEN_LINES_PER_WRITTEN_LINE = 3;
const INLINE_DISPLAY = /^(inline|inline-block|inline-flex|contents|table-cell|ruby)/;

function mergeIntoLines(boxes: Box[]): Box[] {
  const lines: Box[] = [];
  for (const box of [...boxes].sort((a, b) => a.top - b.top)) {
    const line = lines[lines.length - 1];
    const overlap = line ? Math.min(line.bottom, box.bottom) - Math.max(line.top, box.top) : 0;
    // Boxes that share most of their height sit on one line (inline code, links, bold runs).
    if (line && overlap > Math.min(line.bottom - line.top, box.bottom - box.top) / 2) {
      line.bottom = Math.max(line.bottom, box.bottom);
    } else {
      lines.push({ ...box });
    }
  }
  return lines;
}

/**
 * The written lines of ``content`` and where each one ends, measured from its
 * top. A line is what the reader counts, the same on every screen: a paragraph
 * line, a list item, a table row, a line of code. Wrapping on a narrow screen
 * does not add lines, except that a very long paragraph counts one line per
 * three screen lines, so a wall of text still folds.
 */
function renderedLines(content: HTMLElement): RenderedLines {
  const origin = content.getBoundingClientRect().top;
  const written = new Map<string, Box[]>();
  const blocks = new Map<Element, string>();
  const blockOf = (node: Node) => {
    let element = node.parentElement;
    while (element && element !== content && INLINE_DISPLAY.test(getComputedStyle(element).display)) element = element.parentElement;
    const block = element || content;
    if (!blocks.has(block)) blocks.set(block, String(blocks.size));
    return blocks.get(block)!;
  };
  const range = document.createRange();
  let breaks = 0;
  const add = (key: string, node: Text, start: number, end: number) => {
    range.setStart(node, start);
    range.setEnd(node, end);
    for (const rect of Array.from(range.getClientRects())) {
      if (rect.width <= 0 || rect.height <= 0) continue;
      const boxes = written.get(key) || [];
      boxes.push({ top: rect.top - origin, bottom: rect.bottom - origin });
      written.set(key, boxes);
    }
  };
  // A file shown in the message folds on its own terms; its rows are not the reply's lines.
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
    acceptNode: (node) => {
      const file = (node instanceof Element ? node : node.parentElement)?.closest(".generated-file");
      return file && content.contains(file) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node instanceof HTMLBRElement) { breaks += 1; continue; }
    if (!(node instanceof Text) || !node.data.trim()) continue;
    const block = blockOf(node);
    const preformatted = /^pre/.test(getComputedStyle(node.parentElement || content).whiteSpace);
    if (!preformatted) { add(`${block}:${breaks}`, node, 0, node.data.length); continue; }
    let start = 0;
    for (const segment of node.data.split("\n")) {
      if (segment.trim()) add(`${block}:${breaks}`, node, start, start + segment.length);
      start += segment.length + 1;
      breaks += 1;
    }
  }
  const bottoms: number[] = [];
  const lines = [...written.values()]
    .map((boxes) => mergeIntoLines(boxes))
    .sort((a, b) => a[0].top - b[0].top);
  for (const screenLines of lines) {
    // A long wrapped paragraph: one written line per three screen lines.
    for (let index = SCREEN_LINES_PER_WRITTEN_LINE - 1; index < screenLines.length - 1; index += SCREEN_LINES_PER_WRITTEN_LINE) {
      bottoms.push(screenLines[index].bottom);
    }
    bottoms.push(screenLines[screenLines.length - 1].bottom);
  }
  if (bottoms.length) return { count: bottoms.length, bottoms };
  // No text (only media): fall back to the height in line-height units.
  const style = getComputedStyle(content);
  const lineHeight = Number.parseFloat(style.lineHeight) || (Number.parseFloat(style.fontSize) || 14) * 1.5;
  const count = Math.round(content.getBoundingClientRect().height / lineHeight);
  return { count, bottoms: Array.from({ length: count }, (_, index) => (index + 1) * lineHeight) };
}

/**
 * Long content shows its first lines with "Show N more lines"; expanded, a
 * "Show less" stays pinned at the bottom of the view. Thresholds per kind live
 * in ``contentFold``; short or barely-long content is never folded.
 */
export default function CollapsibleSentMessage({
  text,
  children,
  enabled = true,
  tone = "user",
  kind,
}: {
  text: string;
  children: ReactNode;
  enabled?: boolean;
  tone?: "user" | "assistant";
  /** Fold thresholds; defaults to the author's (user or assistant). */
  kind?: ContentFoldKind;
}) {
  const bodyId = useId();
  const contentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [fold, setFold] = useState<(ContentFold & { height: number }) | null>(null);
  const foldKind = kind || tone;

  useLayoutEffect(() => {
    setExpanded(false);
  }, [text, enabled]);

  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!enabled || !content) {
      setFold(null);
      return;
    }
    // The content element is never clamped itself, so it measures in full
    // whether the body around it is folded or not.
    const measure = () => {
      const lines = renderedLines(content);
      const next = contentFold(foldKind, lines.count);
      // Clamp just below the last line shown, so no line is cut through.
      const height = next ? Math.ceil(lines.bottoms[next.visible - 1] + 2) : 0;
      setFold((current) => {
        if (!next) return null;
        if (current && current.visible === next.visible && current.hidden === next.hidden
          && current.height === height) return current;
        return { ...next, height };
      });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, [text, enabled, foldKind]);

  if (!enabled) return <>{children}</>;

  const folded = Boolean(fold) && !expanded;
  return (
    <div className={`chat-sent-message-collapse chat-sent-message-collapse--${tone}`}>
      <div
        id={bodyId}
        className={`chat-sent-message-collapse__body${folded ? " chat-sent-message-collapse__body--collapsed" : ""}`}
        style={folded && fold ? { maxHeight: `${fold.height}px` } : undefined}
      >
        <div ref={contentRef} className="chat-sent-message-collapse__content">{children}</div>
      </div>
      {fold && (
        <button
          type="button"
          className={`chat-sent-message-collapse__toggle${expanded ? " chat-sent-message-collapse__toggle--pinned" : ""}`}
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded
            ? t("component.content_fold.show_less")
            : t("component.content_fold.show_more_lines", { count: fold.hidden })}
        </button>
      )}
    </div>
  );
}
