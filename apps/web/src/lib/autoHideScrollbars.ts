/** Show a scroller's thumb only while it is moving, without changing its gutter. */
export function installAutoHideScrollbars() {
  const root = document.documentElement;
  const timers = new Map<Element, number>();
  root.setAttribute("data-auto-hide-scrollbars", "");

  const onScroll = (event: Event) => {
    const target = event.target === document ? document.scrollingElement : event.target;
    if (!(target instanceof Element)) return;

    const previousTimer = timers.get(target);
    if (previousTimer !== undefined) window.clearTimeout(previousTimer);
    target.setAttribute("data-scrollbar-active", "");
    timers.set(target, window.setTimeout(() => {
      target.removeAttribute("data-scrollbar-active");
      timers.delete(target);
    }, 900));
  };

  document.addEventListener("scroll", onScroll, { capture: true, passive: true });
  return () => {
    document.removeEventListener("scroll", onScroll, true);
    for (const [target, timer] of timers) {
      window.clearTimeout(timer);
      target.removeAttribute("data-scrollbar-active");
    }
    timers.clear();
    root.removeAttribute("data-auto-hide-scrollbars");
  };
}
