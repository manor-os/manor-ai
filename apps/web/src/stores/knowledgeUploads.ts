import { create } from "zustand";
import { authPrincipalKey, getAuthToken } from "../lib/authToken";

export type KnowledgeUploadStatus = "uploading" | "processing" | "failed";

export interface KnowledgeUploadOptions {
  visibility: string;
  classification: string;
  client_visible: boolean;
}

export interface KnowledgeUploadItem {
  id: string;
  scopeId: string;
  principalKey: string;
  file: File | null;
  fileName: string;
  fileSize: number;
  folderId?: string | null;
  options?: KnowledgeUploadOptions;
  status: KnowledgeUploadStatus;
  progress: number;
  error: "stalled" | "failed" | "identity_changed" | null;
  createdAt: number;
}

type KnowledgeUploadItemsUpdater = (
  current: KnowledgeUploadItem[],
) => KnowledgeUploadItem[];

interface KnowledgeUploadState {
  items: KnowledgeUploadItem[];
  setItems: (updater: KnowledgeUploadItemsUpdater) => void;
}

// Upload sessions belong to the application, not to one Knowledge route
// mount. Keeping both visible state and controllers at module scope lets a
// transfer or receipt reconciliation survive ordinary SPA navigation.
export const knowledgeUploadControllers = new Map<string, AbortController>();

const KNOWLEDGE_UPLOAD_RECOVERY_KEY = "manor_knowledge_upload_recovery_v1";

function persistProcessingUploads(items: KnowledgeUploadItem[]) {
  if (typeof window === "undefined") return;
  const processing = items
    .filter((item) => (
      (item.status === "processing" || item.file === null)
      && !item.principalKey.startsWith("opaque:")
    ))
    .map((item) => ({
      id: item.id,
      scopeId: item.scopeId,
      principalKey: item.principalKey,
      fileName: item.fileName,
      fileSize: item.fileSize,
      folderId: item.folderId ?? null,
      options: item.options,
      createdAt: item.createdAt,
    }));
  try {
    if (processing.length === 0) {
      window.sessionStorage.removeItem(KNOWLEDGE_UPLOAD_RECOVERY_KEY);
    } else {
      window.sessionStorage.setItem(
        KNOWLEDGE_UPLOAD_RECOVERY_KEY,
        JSON.stringify({ version: 1, items: processing }),
      );
    }
  } catch {
    // Upload completion still works when browser storage is blocked.
  }
}

function loadProcessingUploads(): KnowledgeUploadItem[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.sessionStorage.getItem(KNOWLEDGE_UPLOAD_RECOVERY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { version?: unknown; items?: unknown };
    if (parsed.version !== 1 || !Array.isArray(parsed.items)) throw new Error("Invalid upload recovery");
    const currentPrincipalKey = authPrincipalKey(getAuthToken());
    const restored = parsed.items.flatMap((candidate: any): KnowledgeUploadItem[] => {
      if (
        typeof candidate?.id !== "string"
        || typeof candidate?.scopeId !== "string"
        || candidate?.principalKey !== currentPrincipalKey
        || typeof candidate?.fileName !== "string"
        || typeof candidate?.fileSize !== "number"
      ) return [];
      return [{
        id: candidate.id,
        scopeId: candidate.scopeId,
        principalKey: candidate.principalKey,
        file: null,
        fileName: candidate.fileName,
        fileSize: candidate.fileSize,
        folderId: typeof candidate.folderId === "string" ? candidate.folderId : null,
        options: candidate.options,
        status: "processing",
        progress: 100,
        error: null,
        createdAt: typeof candidate.createdAt === "number" ? candidate.createdAt : Date.now(),
      }];
    });
    persistProcessingUploads(restored);
    return restored;
  } catch {
    window.sessionStorage.removeItem(KNOWLEDGE_UPLOAD_RECOVERY_KEY);
    return [];
  }
}

export const useKnowledgeUploadStore = create<KnowledgeUploadState>((set) => ({
  items: loadProcessingUploads(),
  setItems: (updater) => set((state) => {
    const items = updater(state.items);
    persistProcessingUploads(items);
    return { items };
  }),
}));

export function resetKnowledgeUploadsForAuthChange() {
  knowledgeUploadControllers.forEach((controller) => controller.abort());
  knowledgeUploadControllers.clear();
  useKnowledgeUploadStore.getState().setItems(() => []);
}
