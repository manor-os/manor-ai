import { create } from "zustand";
import type { ReactNode } from "react";

/**
 * Global detail pop-up (Codex-plugin style). A single DetailDrawer is mounted
 * once at the app root; any card/row opens it by calling `openDetail(...)`
 * with its own header + body + actions. Keeps the pop-up a global element
 * instead of per-page drawer instances.
 */
/** A single footer action. The drawer lays these out for you (primary →
 *  full-width, secondary → equal grid, danger → separated), so callers don't
 *  hand-roll button rows that wrap raggedly when there are many actions. */
export interface DetailAction {
  label: ReactNode;
  onClick: () => void;
  icon?: ReactNode;
  disabled?: boolean;
}

export interface DetailPayload {
  /** Leading visual — an Avatar / IconTile / monogram node. */
  icon?: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  /** Inline chips under the title. */
  badges?: ReactNode;
  /** Main scrollable detail content. */
  body: ReactNode;
  /** Structured footer actions — preferred. The drawer arranges them:
   *  primary (prominent, full-width), secondary (equal ghost grid), danger
   *  (separated). */
  primaryAction?: DetailAction;
  secondaryActions?: DetailAction[];
  dangerAction?: DetailAction;
  /** Escape hatch: render a custom footer node instead of the structured
   *  actions above. */
  actions?: ReactNode;
  /** Drawer width in px (default 400). */
  width?: number;
  /** Optional key — opening with the same key replaces content without re-animating. */
  key?: string;
}

interface DetailState {
  payload: DetailPayload | null;
  openDetail: (payload: DetailPayload) => void;
  closeDetail: () => void;
}

export const useDetailStore = create<DetailState>((set) => ({
  payload: null,
  openDetail: (payload) => set({ payload }),
  closeDetail: () => set({ payload: null }),
}));

/** Convenience helpers for non-hook call sites. */
export const openDetail = (p: DetailPayload) => useDetailStore.getState().openDetail(p);
export const closeDetail = () => useDetailStore.getState().closeDetail();
