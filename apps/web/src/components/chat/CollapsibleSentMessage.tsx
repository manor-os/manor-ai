import { type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";

const COLLAPSE_OVERFLOW_PX = 1;
const COLLAPSED_TAIL_OMITTED_GAP_PX = 48;
const MIN_USEFUL_COLLAPSED_TAIL_PX = 32;

function collapsedTailBudgetPx(body: HTMLElement, content: HTMLElement, tail: HTMLElement) {
  // Read the stylesheet cap, not a previously inlined leftover from the last sync.
  tail.hidden = false;
  tail.style.maxHeight = "";
  const hidden = Math.max(0, content.scrollHeight - body.clientHeight);
  const cssMax = Number.parseFloat(getComputedStyle(tail).maxHeight);
  const budgetCap = Number.isFinite(cssMax) && cssMax > 0 ? cssMax : 120;
  // Leave an omitted middle so the tail cannot repeat lines already visible in
  // the clipped head, and so collapsed preview stays shorter than Show less.
  return Math.min(budgetCap, hidden - COLLAPSED_TAIL_OMITTED_GAP_PX);
}

function collapsedBlockVisibility(body: HTMLElement, block: Element) {
  const bodyRect = body.getBoundingClientRect();
  const blockRect = block.getBoundingClientRect();
  if (blockRect.bottom <= bodyRect.bottom + 1) return "visible";
  if (blockRect.top < bodyRect.bottom - 1) return "partial";
  return "hidden";
}

function fillCollapsedTail(body: HTMLElement, content: HTMLElement, tail: HTMLElement) {
  const host = tail.firstElementChild;
  if (!(host instanceof HTMLElement)) return;

  const hideTail = () => {
    host.replaceChildren();
    tail.hidden = true;
    tail.style.maxHeight = "";
  };

  const tailBudget = collapsedTailBudgetPx(body, content, tail);
  if (tailBudget < MIN_USEFUL_COLLAPSED_TAIL_PX) {
    hideTail();
    return;
  }

  const root =
    content.childElementCount === 1
    && content.firstElementChild instanceof HTMLElement
    && content.firstElementChild.childElementCount > 0
      ? content.firstElementChild
      : content;

  const blocks = Array.from(root.children);
  // A single clipped paragraph (remark-breaks line list) is already in the
  // head. Cloning it into the tail repeats the middle, as in medium briefs.
  if (blocks.length === 0) {
    hideTail();
    return;
  }

  const picked: Element[] = [];
  let used = 0;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (collapsedBlockVisibility(body, block) !== "hidden") break;
    const height = Math.ceil(block.getBoundingClientRect().height);
    if (picked.length > 0 && used + height > tailBudget) break;
    picked.unshift(block);
    used += height;
  }

  if (picked.length === 0) {
    hideTail();
    return;
  }

  tail.hidden = false;
  tail.style.maxHeight = `${Math.floor(tailBudget)}px`;
  const clones = picked.map((block) => block.cloneNode(true));
  if (root === content) {
    host.replaceChildren(...clones);
  } else {
    const wrapper = root.cloneNode(false);
    if (wrapper instanceof HTMLElement) {
      wrapper.append(...clones);
      host.replaceChildren(wrapper);
    } else {
      host.replaceChildren(...clones);
    }
  }

  // Keep the trailing block in the collapsed preview by pinning overflow to
  // its end when one last block is taller than the tail. This is
  // content-agnostic: a CTA or last paragraph stays visible without
  // heading-specific matching.
  tail.scrollTop = Math.max(0, tail.scrollHeight - tail.clientHeight);
}

export default function CollapsibleSentMessage({
  text,
  children,
  enabled = true,
  tone = "user",
}: {
  text: string;
  children: ReactNode;
  enabled?: boolean;
  tone?: "user" | "assistant";
}) {
  const bodyId = useId();
  const bodyRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const tailRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const collapsed = enabled && !expanded;

  useLayoutEffect(() => {
    setExpanded(false);
  }, [text, enabled]);

  useLayoutEffect(() => {
    if (!enabled || expanded) return;
    const body = bodyRef.current;
    const content = contentRef.current;
    if (!body || !content) return;
    // Measure rendered content, not Markdown source: a long URL can occupy
    // one short line, while a narrow screen can wrap a short reply many times.
    const measure = () => setOverflowing(body.scrollHeight > body.clientHeight + COLLAPSE_OVERFLOW_PX);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(body);
    observer.observe(content);
    return () => observer.disconnect();
  }, [text, enabled, expanded]);

  useLayoutEffect(() => {
    if (!collapsed || !overflowing) return;
    const body = bodyRef.current;
    const content = contentRef.current;
    const tail = tailRef.current;
    if (!body || !content || !tail) return;
    const sync = () => fillCollapsedTail(body, content, tail);
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(content);
    observer.observe(body);
    return () => observer.disconnect();
  }, [collapsed, overflowing, text]);

  if (!enabled) return <>{children}</>;

  return (
    <div className={`chat-sent-message-collapse chat-sent-message-collapse--${tone}`}>
      <div
        id={bodyId}
        ref={bodyRef}
        className={`chat-sent-message-collapse__body${expanded ? "" : " chat-sent-message-collapse__body--collapsed"}`}
      >
        <div ref={contentRef} className="chat-sent-message-collapse__content">{children}</div>
      </div>
      {overflowing && !expanded && <span className="chat-sent-message-collapse__ellipsis" aria-hidden="true">...</span>}
      {overflowing && !expanded && (
        <div ref={tailRef} className="chat-sent-message-collapse__tail" aria-hidden="true">
          <div className="chat-sent-message-collapse__content" />
        </div>
      )}
      {(overflowing || expanded) && (
        <button
          type="button"
          className="chat-sent-message-collapse__toggle"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show less" : "Show all"}
        </button>
      )}
    </div>
  );
}
