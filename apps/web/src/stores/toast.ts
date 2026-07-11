import { create } from "zustand";

export interface ToastItem {
  id: string;
  type: "success" | "error" | "warning" | "info";
  title: string;
  message?: string;
  duration?: number;
  actionLabel?: string;
  onAction?: () => void;
  onDismiss?: () => void;
}

interface ToastState {
  toasts: ToastItem[];
  addToast: (toast: Omit<ToastItem, "id"> & { id?: string }) => void;
  removeToast: (id: string) => void;
  success: (title: string, message?: string) => void;
  error: (title: string, message?: string) => void;
  warning: (title: string, message?: string) => void;
  info: (title: string, message?: string) => void;
}

export const useToastStore = create<ToastState>((set, get) => ({
  toasts: [],
  addToast: (toast) => {
    const id = toast.id || Math.random().toString(36).slice(2);
    const duration = toast.duration ?? 4000;
    set((state) => ({
      toasts: [...state.toasts.filter((t) => t.id !== id), { ...toast, id }],
    }));
    if (duration > 0) {
      setTimeout(() => {
        const existing = get().toasts.find((t) => t.id === id);
        existing?.onDismiss?.();
        set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }));
      }, duration);
    }
  },
  removeToast: (id) =>
    set((state) => {
      const existing = state.toasts.find((t) => t.id === id);
      existing?.onDismiss?.();
      return { toasts: state.toasts.filter((t) => t.id !== id) };
    }),
  success: (title, message) => get().addToast({ type: "success", title, message }),
  error: (title, message) => get().addToast({ type: "error", title, message }),
  warning: (title, message) => get().addToast({ type: "warning", title, message }),
  info: (title, message) => get().addToast({ type: "info", title, message }),
}));
