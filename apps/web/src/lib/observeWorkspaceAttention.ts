/** Only acknowledge reminders that remain visible in the active browser tab. */
export function observeWorkspaceAttention(
  root: HTMLElement,
  onSeen: (keys: string[]) => Promise<void>,
) {
  const visible = new Set<Element>();
  const timers = new Map<Element, ReturnType<typeof setTimeout>>();
  const acknowledged = new Set<string>();
  let stopped = false;
  let sending = false;

  const keyFor = (node: Element) => node.getAttribute("data-workspace-attention-key") || "";
  const cancel = (node: Element) => {
    clearTimeout(timers.get(node));
    timers.delete(node);
  };
  const send = async () => {
    if (stopped || sending || document.visibilityState !== "visible") return;
    const keys = [...new Set([...visible].filter((node) => !timers.has(node))
      .map(keyFor).filter((key) => key && !acknowledged.has(key)))].slice(0, 100);
    if (!keys.length) return;
    sending = true;
    try {
      await onSeen(keys);
      keys.forEach((key) => acknowledged.add(key));
    } catch {
      // Keep the reminder unread if saving fails; retry only while visible.
      visible.forEach((node) => schedule(node, 5000));
    } finally {
      sending = false;
      if (!stopped) void send();
    }
  };
  const schedule = (node: Element, delay = 1000) => {
    cancel(node);
    if (stopped || document.visibilityState !== "visible" || acknowledged.has(keyFor(node))) return;
    timers.set(node, setTimeout(() => {
      timers.delete(node);
      if (visible.has(node)) void send();
    }, delay));
  };
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const readable = entry.isIntersecting && entry.intersectionRect.height >= Math.min(120, entry.boundingClientRect.height / 2);
      if (readable) {
        visible.add(entry.target);
        schedule(entry.target);
      } else {
        visible.delete(entry.target);
        cancel(entry.target);
      }
    }
  }, { threshold: [0, 0.05, 0.1, 0.25, 0.5, 0.75, 1] });
  const observed = new Set<Element>();
  const observeCards = () => {
    const cards = new Set(root.querySelectorAll("[data-workspace-attention-key]"));
    observed.forEach((node) => {
      if (!cards.has(node)) {
        observer.unobserve(node);
        observed.delete(node);
        visible.delete(node);
        cancel(node);
      }
    });
    cards.forEach((node) => {
      if (!observed.has(node)) {
        observed.add(node);
        observer.observe(node);
      }
    });
  };
  const mutations = new MutationObserver(observeCards);
  mutations.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-workspace-attention-key"] });
  observeCards();
  const onVisibility = () => visible.forEach((node) => {
    cancel(node);
    if (document.visibilityState === "visible") schedule(node);
  });
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    stopped = true;
    timers.forEach(clearTimeout);
    observer.disconnect();
    mutations.disconnect();
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
