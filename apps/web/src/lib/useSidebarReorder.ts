import { useEffect, useMemo, useRef, useState, type ButtonHTMLAttributes } from "react";
import {
  moveSidebarWorkspace as moveSidebarItem,
  readWorkspaceSidebarPreferences as readSidebarPreferences,
  type WorkspaceSidebarPreferences as SidebarPreferences,
} from "./workspaceChatSidebar";

export function useSidebarReorder<T extends { id: string }>(
  storageKey: string | null,
  items: T[],
  disabled: boolean,
) {
  const restored = useMemo(() => readSidebarPreferences(storageKey), [storageKey]);
  const [saved, setSaved] = useState({ key: storageKey, value: restored });
  const preferences = saved.key === storageKey ? saved.value : restored;
  const orderedItems = useMemo(() => {
    if (preferences.mode !== "manual") return items;
    const rank = new Map(preferences.order.map((id, index) => [id, index]));
    return [...items].sort((a, b) => (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity) || 0);
  }, [items, preferences]);
  const drag = useRef<{ id: string; pointerId: number; x: number; y: number; moved: boolean; target?: { id: string; after: boolean } } | null>(null);
  const dragClick = useRef<string | null>(null);
  const [dropTarget, setDropTarget] = useState<{ id: string; after: boolean } | null>(null);
  const resetDrag = () => {
    drag.current = null;
    dragClick.current = null;
    setDropTarget(null);
  };
  useEffect(() => {
    resetDrag();
    const onStorage = (event: StorageEvent) => {
      if (storageKey && (event.key === storageKey || event.key === null)) {
        setSaved({ key: storageKey, value: readSidebarPreferences(storageKey) });
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [storageKey, disabled]);

  const move = (id: string, target: string, after = false) => {
    const order = orderedItems.map((item) => item.id);
    const next = moveSidebarItem(order, id, target, after);
    if (next === order) return;
    const value: SidebarPreferences = { mode: "manual", order: next };
    setSaved({ key: storageKey, value });
    if (storageKey) {
      try { window.localStorage.setItem(storageKey, JSON.stringify(value)); } catch { /* Keep the order for this session. */ }
    }
  };

  const rowAttributes = (id: string) => ({
    "data-sidebar-id": id,
    "data-sidebar-drop": dropTarget?.id === id ? dropTarget.after ? "after" : "before" : undefined,
  });
  const buttonAttributes = (id: string, onOpen: () => void): ButtonHTMLAttributes<HTMLButtonElement> => ({
    onClick: (event) => {
      const dragged = dragClick.current === id;
      dragClick.current = null;
      if (dragged && event.detail > 0) { event.preventDefault(); return; }
      onOpen();
    },
    onDragStart: (event) => event.preventDefault(),
    onPointerDown: (event) => {
      dragClick.current = null;
      if (event.button !== 0 || disabled || drag.current) return;
      drag.current = { id, pointerId: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    onPointerMove: (event) => {
      const current = drag.current;
      if (!current || current.id !== id || current.pointerId !== event.pointerId || disabled
        || (!current.moved && Math.hypot(event.clientX - current.x, event.clientY - current.y) < 5)) return;
      current.moved = true;
      const list = event.currentTarget.closest<HTMLElement>("[data-sidebar-reorder-list]");
      const scroll = list?.scrollHeight && list.scrollHeight > list.clientHeight
        ? list : list?.closest<HTMLElement>("[data-sidebar-scroll-area]");
      const bounds = scroll?.getBoundingClientRect();
      if (scroll && bounds) {
        if (event.clientY < bounds.top + 20) scroll.scrollTop -= 12;
        if (event.clientY > bounds.bottom - 20) scroll.scrollTop += 12;
      }
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>("[data-sidebar-id]");
      const targetId = target?.dataset.sidebarId;
      if (!target || !targetId || targetId === id || target.closest("[data-sidebar-reorder-list]") !== list) {
        current.target = undefined;
        setDropTarget(null);
        return;
      }
      const targetBounds = target.getBoundingClientRect();
      const after = event.clientY > targetBounds.top + targetBounds.height / 2;
      current.target = { id: targetId, after };
      setDropTarget((previous) => previous?.id === targetId && previous.after === after ? previous : { id: targetId, after });
    },
    onPointerUp: (event) => {
      const current = drag.current;
      if (!current || current.id !== id || current.pointerId !== event.pointerId) return;
      dragClick.current = current.moved ? id : null;
      if (!disabled && current.moved && current.target) move(id, current.target.id, current.target.after);
      drag.current = null;
      setDropTarget(null);
    },
    onPointerCancel: resetDrag,
    onLostPointerCapture: () => { drag.current = null; setDropTarget(null); },
    onKeyDown: (event) => {
      if (disabled) return;
      const index = orderedItems.findIndex((item) => item.id === id);
      if (event.key === "ArrowUp" && index > 0) { event.preventDefault(); move(id, orderedItems[index - 1].id); }
      if (event.key === "ArrowDown" && index >= 0 && index < orderedItems.length - 1) { event.preventDefault(); move(id, orderedItems[index + 1].id, true); }
    },
  });
  return { items: orderedItems, preferences, rowAttributes, buttonAttributes };
}
