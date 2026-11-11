import { useEffect, useState } from "react";

/** Observe controls only while their row/module is actually on screen. */
export function useElementVisible<T extends Element>() {
  const [element, ref] = useState<T | null>(null);
  const [visible, setVisible] = useState(typeof IntersectionObserver === "undefined");
  useEffect(() => {
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => setVisible(Boolean(entry?.isIntersecting)), { rootMargin: "100px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return { ref, visible };
}
