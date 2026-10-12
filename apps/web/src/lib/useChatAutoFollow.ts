import { useCallback, useLayoutEffect, useRef } from "react";

/**
 * Owns the viewport's scroll position. Content/viewport resizes follow the
 * bottom until the reader scrolls up or explicitly navigates to a message.
 * Refs avoid rendering the conversation again for every scroll event.
 */
export function useChatAutoFollow(scopeKey: string | undefined, active = true) {
  const chatBodyRef = useRef<HTMLDivElement>(null);
  const chatContentRef = useRef<HTMLDivElement>(null);
  const autoFollowRef = useRef(true);
  const lastTopRef = useRef(0);
  const navigatingRef = useRef(false);
  const prependRef = useRef<{
    top: number;
    height: number;
    selector?: string;
    offset: number;
  } | null>(null);

  const writeTop = useCallback((top: number) => {
    const el = chatBodyRef.current;
    if (!el) return;
    // `auto` can inherit CSS smooth scrolling. Automatic layout corrections
    // must be instant, and their scroll events must not change reader intent.
    el.scrollTo({ top, behavior: "instant" });
    lastTopRef.current = el.scrollTop;
  }, []);

  const followBottom = useCallback(() => {
    const el = chatBodyRef.current;
    if (!el || !autoFollowRef.current) return;
    const top = Math.max(0, el.scrollHeight - el.clientHeight);
    if (Math.abs(el.scrollTop - top) > 1) writeTop(top);
  }, [writeTop]);

  const pauseAutoFollow = useCallback(() => {
    autoFollowRef.current = false;
    navigatingRef.current = true;
  }, []);

  const resumeAutoFollow = useCallback(() => {
    autoFollowRef.current = true;
    navigatingRef.current = false;
    const el = chatBodyRef.current;
    // Also cancel a navigation animation that has not moved off bottom yet.
    if (el) writeTop(el.scrollHeight - el.clientHeight);
  }, [writeTop]);

  const scrollToPosition = useCallback((top: number, behavior: ScrollBehavior = "smooth") => {
    pauseAutoFollow();
    const el = chatBodyRef.current;
    if (!el) return;
    if (behavior !== "smooth" || window.matchMedia("(prefers-reduced-motion: reduce)").matches) writeTop(top);
    else el.scrollTo({ top, behavior });
  }, [pauseAutoFollow, writeTop]);

  const scrollToMessage = useCallback((target: HTMLElement, behavior: ScrollBehavior = "smooth") => {
    const el = chatBodyRef.current;
    if (!el || !el.contains(target)) return;
    const rect = target.getBoundingClientRect();
    scrollToPosition(el.scrollTop + rect.top - el.getBoundingClientRect().top - (el.clientHeight - rect.height) / 2, behavior);
  }, [scrollToPosition]);

  // Call immediately before prepending the fetched page. Restore before paint,
  // using the reader's current position, not the position at request start.
  const preserveHistoryAnchor = useCallback(() => {
    const el = chatBodyRef.current;
    // The reader may have returned to the bottom or sent a message while the
    // page was in flight. In that case keep following instead of restoring.
    if (!el || autoFollowRef.current) return;
    pauseAutoFollow();
    const viewportTop = el.getBoundingClientRect().top;
    const element = Array.from(el.querySelectorAll<HTMLElement>(".chat-message-row, .ws-activity-line-row"))
      .find((row) => row.getBoundingClientRect().bottom > viewportTop);
    prependRef.current = {
      top: el.scrollTop,
      height: el.scrollHeight,
      // Some timelines reuse DOM rows by index. Resolve the message again
      // after prepending rather than retaining that now-repurposed DOM node.
      selector: element?.id
        ? `#${CSS.escape(element.id)}`
        : element?.dataset.chatScrollMarkerId
          ? `[data-chat-scroll-marker-id="${CSS.escape(element.dataset.chatScrollMarkerId)}"]`
          : undefined,
      offset: element ? element.getBoundingClientRect().top - viewportTop : 0,
    };
  }, [pauseAutoFollow]);

  useLayoutEffect(() => {
    prependRef.current = null;
    if (active) resumeAutoFollow();
  }, [scopeKey, active, resumeAutoFollow]);

  useLayoutEffect(() => {
    if (!active) return;
    const el = chatBodyRef.current;
    const anchor = prependRef.current;
    prependRef.current = null;
    if (el && anchor) {
      // A visible row also handles concurrent reply growth below the reader;
      // total scrollHeight alone would incorrectly count that as prepending.
      const target = anchor.selector ? el.querySelector(anchor.selector) : null;
      writeTop(target
        ? el.scrollTop + target.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset
        : anchor.top + el.scrollHeight - anchor.height);
    } else followBottom();
  });

  useLayoutEffect(() => {
    const el = chatBodyRef.current;
    const content = chatContentRef.current;
    if (!active || !el || !content) return;
    let touchY = 0;
    const userScroll = (up: boolean) => {
      navigatingRef.current = false;
      if (up) autoFollowRef.current = false;
      else if (el.scrollHeight - el.clientHeight - el.scrollTop <= 2) autoFollowRef.current = true;
    };
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY) userScroll(event.deltaY < 0);
    };
    const onTouchStart = (event: TouchEvent) => { touchY = event.touches[0]?.clientY ?? 0; };
    const onTouchMove = (event: TouchEvent) => {
      const y = event.touches[0]?.clientY ?? touchY;
      if (y !== touchY) userScroll(y > touchY);
      touchY = y;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest("input, textarea, select, [contenteditable='true']")) return;
      if (event.key === " " && target.closest("button")) return;
      if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) userScroll(true);
      else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) userScroll(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      // Do not interpret clicks in viewport padding as scrolling. Native and
      // overlay scrollbar drags can start before the first scroll event.
      const rect = el.getBoundingClientRect();
      const gutter = Math.max(12, el.offsetWidth - el.clientWidth);
      const inScrollbar = getComputedStyle(el).direction === "rtl"
        ? event.clientX <= rect.left + gutter
        : event.clientX >= rect.right - gutter;
      if (event.target === el && inScrollbar) userScroll(true);
    };
    const onScroll = () => {
      const top = el.scrollTop;
      if (!navigatingRef.current) {
        const gap = el.scrollHeight - el.clientHeight - top;
        // Layout/clamping also emits scroll events. Only explicit user input
        // can pause following; reaching the bottom can resume it.
        if (top > lastTopRef.current && gap <= 2) autoFollowRef.current = true;
      }
      lastTopRef.current = top;
    };
    const observer = new ResizeObserver(followBottom);
    observer.observe(content);
    observer.observe(el);
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onWheel, { passive: true });
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: true });
    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("keydown", onKeyDown);
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("keydown", onKeyDown);
    };
  }, [scopeKey, active, followBottom]);

  return { chatBodyRef, chatContentRef, followBottom, pauseAutoFollow, resumeAutoFollow, scrollToPosition, scrollToMessage, preserveHistoryAnchor };
}
