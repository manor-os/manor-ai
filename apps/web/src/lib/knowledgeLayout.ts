import type { Document } from "./types";

export type KnowledgeLibrarySection = "all" | "recent" | "favorites" | "trash";
export type KnowledgeFileTypeFilter =
  | "all"
  | "pdf"
  | "docx"
  | "xlsx"
  | "txt"
  | "csv"
  | "md"
  | "json"
  | "html"
  | "png"
  | "jpg"
  | "mp4"
  | "mp3"
  | "py"
  | "js"
  | "sql"
  | "yaml";
export type KnowledgeSortKey = "name" | "date" | "size";

type KnowledgeListParamsInput = {
  currentFolderId: string | null;
  librarySection?: KnowledgeLibrarySection;
  searchTerm: string;
  selectedWorkspaceId: string | null;
};

export function getKnowledgeBrowseParams({
  currentFolderId,
  librarySection = "all",
  searchTerm,
  selectedWorkspaceId,
}: KnowledgeListParamsInput): {
  search?: string;
  folder_id?: string;
  scope?: "all";
  workspace_id?: string;
} {
  const search = searchTerm.trim();
  const useGlobalScope = librarySection === "recent" || librarySection === "favorites";
  return {
    ...(search ? { search } : {}),
    ...(useGlobalScope ? { scope: "all" as const } : {}),
    ...(!search && !selectedWorkspaceId && !useGlobalScope ? { folder_id: currentFolderId || "root" } : {}),
    ...(selectedWorkspaceId ? { workspace_id: selectedWorkspaceId } : {}),
  };
}

type KnowledgeDocumentsForViewInput = {
  currentFolderId: string | null;
  documents: Document[];
  favoriteDocIds: Set<string>;
  fileTypeFilter: KnowledgeFileTypeFilter;
  isSearching: boolean;
  librarySection: KnowledgeLibrarySection;
  selectedWorkspaceId: string | null;
  sortKey: KnowledgeSortKey;
};

export function getKnowledgeDocumentsForView({
  currentFolderId,
  documents,
  favoriteDocIds,
  fileTypeFilter,
  isSearching,
  librarySection,
  selectedWorkspaceId,
  sortKey,
}: KnowledgeDocumentsForViewInput): Document[] {
  return documents
    .filter((doc) => {
      if (librarySection === "favorites" && !favoriteDocIds.has(doc.id)) return false;
      if (!isSearching && librarySection !== "recent" && librarySection !== "favorites" && !selectedWorkspaceId) {
        const folderId = doc.folder_id || null;
        if (folderId !== currentFolderId) return false;
      }
      if (!matchesFileTypeFilter(doc, fileTypeFilter)) return false;
      return true;
    })
    .sort((a, b) => {
      if (librarySection === "recent") return compareByCreatedAtDesc(a, b);
      if (sortKey === "name") return (a.name || "").localeCompare(b.name || "");
      if (sortKey === "size") return (b.file_size || 0) - (a.file_size || 0);
      return compareByCreatedAtDesc(a, b);
    })
    .slice(0, librarySection === "recent" ? 20 : undefined);
}

function compareByCreatedAtDesc(a: Document, b: Document): number {
  return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
}

function matchesFileTypeFilter(doc: Document, fileTypeFilter: KnowledgeFileTypeFilter): boolean {
  if (fileTypeFilter === "all") return true;

  const ext = (doc.file_type || doc.name?.split(".").pop() || "").toLowerCase();
  if (fileTypeFilter === "png") {
    return ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"].includes(ext);
  }
  if (fileTypeFilter === "mp4") {
    return ["mp4", "webm", "ogg", "mov", "avi", "mkv"].includes(ext);
  }
  if (fileTypeFilter === "mp3") {
    return ["mp3", "wav", "ogg", "aac", "flac", "m4a", "wma"].includes(ext);
  }
  return ext === fileTypeFilter;
}
