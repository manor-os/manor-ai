import { useSyncExternalStore } from "react";

export function isPageVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

function subscribe(listener: () => void): () => void {
  document.addEventListener("visibilitychange", listener);
  return () => document.removeEventListener("visibilitychange", listener);
}

/** Disabling a display query also prevents event invalidations from fetching it. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(subscribe, isPageVisible, () => true);
}
