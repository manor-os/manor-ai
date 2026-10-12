import { create } from "zustand";
import { api, ApiError } from "./api";
import { t } from "./i18n";
import { saveFileResponse } from "./fileDownload";
import { useToastStore } from "../stores/toast";

type DownloadDocument = { id: string; name: string };
type DocumentDownloadState = {
  activeIds: string[];
  downloadDocument: (doc: DownloadDocument) => Promise<void>;
};

// Shared across entry surfaces so navigation or a second menu cannot enqueue
// another request for a document whose save operation is still running.
export const useDocumentDownload = create<DocumentDownloadState>((set, get) => ({
  activeIds: [],
  downloadDocument: async (doc) => {
    if (get().activeIds.includes(doc.id)) return;
    set((state) => ({ activeIds: [...state.activeIds, doc.id] }));
    const toast = useToastStore.getState();
    const toastId = `document-download:${doc.id}`;
    const controller = new AbortController();
    toast.addToast({
      id: toastId,
      type: "info",
      title: t("download.in_progress"),
      message: doc.name,
      duration: 0,
      actionLabel: t("common.cancel"),
      onAction: () => controller.abort(),
    });
    try {
      const result = await saveFileResponse(
        doc.name,
        // Re-authorize every download. Never reuse a preview URL or its bytes.
        () => api.documents.downloadResponse(doc.id, { signal: controller.signal }),
        controller.signal,
      );
      if (result === "saved") toast.success(t("download.saved"), doc.name);
      if (result === "requested") {
        toast.info(t("download.requested"), t("download.check_browser"));
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        const message = error instanceof ApiError
          ? `${error.message} (HTTP ${error.status})`
          : error instanceof Error ? error.message : undefined;
        toast.error(t("download.failed"), message);
      }
    } finally {
      toast.removeToast(toastId);
      set((state) => ({ activeIds: state.activeIds.filter((id) => id !== doc.id) }));
    }
  },
}));
