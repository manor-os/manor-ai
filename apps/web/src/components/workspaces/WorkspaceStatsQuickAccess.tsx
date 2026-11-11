import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";

import {
  api,
  type WorkspaceStatDefinition,
  type WorkspaceStatLibraryEntry,
  type WorkspaceStatsQuickView,
} from "../../lib/api";
import { preserveReturnToInHistory } from "../../lib/chatRouteReferences";
import { fileReferenceKind } from "../../lib/fileReferences";
import { relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import { folderDisplayName } from "../../lib/knowledgeLayout";
import { formatUserFacingLabel } from "../../lib/taskDisplay";
import type { Document, Task, Workspace } from "../../lib/types";
import {
  formatWorkspaceStatValue,
  workspaceStatAssessmentNote,
  workspaceStatAttentionPresentation,
} from "../../lib/workspaceStats";
import {
  nextSeenTaskAttentionKeys,
  parseSeenTaskAttention,
  serializeSeenTaskAttention,
  unseenTaskAttentionSeverity,
} from "../../lib/workspaceTaskAttention";
import { useAuthStore } from "../../stores/auth";
import { useIsCollaborator } from "../../lib/collaboratorSession";
import {
  IconArrowDown,
  IconArrowUp,
  IconBell,
  IconCheck,
  IconChevronLeft,
  IconChevronRight,
  IconEye,
  IconEyeOff,
  IconFolder,
  IconList,
  IconPlus,
  IconSort,
  IconSettings,
  IconTrendingUp,
  IconViewOptions,
  IconWarning,
} from "../icons";
import { getFileReferenceIcon } from "../contentTypeIcons";
import {
  conversationInitiativeItems,
  ConversationInitiativesQuickList,
  useConversationInitiativesQuery,
} from "../agents/ConversationInitiativesButton";
import AnchoredPopover from "../ui/AnchoredPopover";
import Button from "../ui/Button";
import Input from "../ui/Input";
import LoadingSpinner from "../ui/LoadingSpinner";
import StatusBadge from "../ui/StatusBadge";
import Textarea from "../ui/Textarea";

interface WorkspaceStatsQuickAccessProps {
  enabled?: boolean;
  workspaceId: string;
  workspace?: Workspace;
  workspaceName?: string;
  initiativeConversationId?: string | null;
  canManage?: boolean;
}

type QuickViewPreference = Omit<WorkspaceStatsQuickView, "configured">;
type WorkspaceQuickTab = "initiatives" | "metrics" | "files" | "tasks" | "settings";

const TASK_ATTENTION_SEEN_STORAGE_PREFIX = "manor-workspace-task-attention-seen";

function taskAttentionStorageKey(userId: string | undefined, workspaceId: string): string {
  if (!userId || !workspaceId) return "";
  return `${TASK_ATTENTION_SEEN_STORAGE_PREFIX}:${userId}:${workspaceId}`;
}

function readSeenTaskAttention(storageKey: string): string[] {
  if (!storageKey || typeof window === "undefined") return [];
  try {
    return parseSeenTaskAttention(window.localStorage.getItem(storageKey));
  } catch {
    return [];
  }
}

function writeSeenTaskAttention(storageKey: string, attentionKeys: readonly string[]): void {
  if (!storageKey || typeof window === "undefined") return;
  try {
    if (attentionKeys.length > 0) {
      window.localStorage.setItem(storageKey, serializeSeenTaskAttention(attentionKeys));
    }
    else window.localStorage.removeItem(storageKey);
  } catch {
    // Read state is best-effort when browser storage is unavailable.
  }
}

interface WorkspaceDocumentGroup {
  id: string;
  name?: string | null;
  is_workspace_file_bucket?: boolean;
  is_default_collection?: boolean;
  document_count?: number;
  folders?: WorkspaceDocumentFolder[];
  documents?: Document[];
}

interface WorkspaceDocumentFolder {
  id: string;
  name: string;
  display_name?: string | null;
  parent_id?: string | null;
}

interface WorkspaceDocumentFolderNode extends WorkspaceDocumentFolder {
  children: WorkspaceDocumentFolderNode[];
  documents: Document[];
  documentCount: number;
}

interface WorkspaceDocumentTree {
  folders: WorkspaceDocumentFolderNode[];
  documents: Document[];
}

function workspaceQuickFolderKey(groupId: string, folderId: string): string {
  return `${groupId}/${folderId}`;
}

function buildWorkspaceDocumentTree(group: WorkspaceDocumentGroup): WorkspaceDocumentTree {
  const nodeById = new Map<string, WorkspaceDocumentFolderNode>();
  for (const folder of group.folders || []) {
    nodeById.set(folder.id, {
      ...folder,
      children: [],
      documents: [],
      documentCount: 0,
    });
  }

  const parentWouldCreateCycle = (folderId: string, parentId: string): boolean => {
    const seen = new Set([folderId]);
    let currentId: string | null | undefined = parentId;
    while (currentId) {
      if (seen.has(currentId)) return true;
      seen.add(currentId);
      currentId = nodeById.get(currentId)?.parent_id;
    }
    return false;
  };

  const rootFolders: WorkspaceDocumentFolderNode[] = [];
  for (const node of nodeById.values()) {
    const parent = node.parent_id ? nodeById.get(node.parent_id) : undefined;
    if (parent && !parentWouldCreateCycle(node.id, parent.id)) parent.children.push(node);
    else rootFolders.push(node);
  }

  const rootDocuments: Document[] = [];
  for (const document of group.documents || []) {
    const folder = document.folder_id ? nodeById.get(document.folder_id) : undefined;
    if (folder) folder.documents.push(document);
    else rootDocuments.push(document);
  }

  const finalizeFolder = (folder: WorkspaceDocumentFolderNode): number => {
    folder.children.sort((left, right) => folderDisplayName(left).localeCompare(folderDisplayName(right)));
    const childCount = folder.children.reduce(
      (total, child) => total + finalizeFolder(child),
      0,
    );
    folder.documentCount = folder.documents.length + childCount;
    return folder.documentCount;
  };
  rootFolders.sort((left, right) => folderDisplayName(left).localeCompare(folderDisplayName(right)));
  rootFolders.forEach(finalizeFolder);
  return { folders: rootFolders, documents: rootDocuments };
}

function queryErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : t("page.workspace_stats.load_failed");
}

function orderedStats(
  stats: WorkspaceStatDefinition[],
  orderedStatIds: string[],
): WorkspaceStatDefinition[] {
  const statsById = new Map(stats.map((stat) => [stat.id, stat]));
  const result = orderedStatIds.flatMap((statId) => {
    const stat = statsById.get(statId);
    if (!stat) return [];
    statsById.delete(statId);
    return [stat];
  });
  return [...result, ...stats.filter((stat) => statsById.has(stat.id))];
}

interface WorkspaceStatsQuickBodyProps {
  stats: WorkspaceStatDefinition[];
  totalStats: number;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}

function WorkspaceStatsQuickBody({
  stats,
  totalStats,
  loading,
  error,
  onRetry,
}: WorkspaceStatsQuickBodyProps) {
  if (loading) {
    return (
      <div className="workspace-chat-stats-quick-state" aria-live="polite">
        <LoadingSpinner size={18} />
        <span className="sr-only">{t("component.workspace_chat.loading")}</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--error" role="alert">
        <IconWarning className="text-red-500" size={18} />
        <strong>{t("page.workspace_stats.load_failed")}</strong>
        <span>{queryErrorMessage(error)}</span>
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("page.workspace_stats.try_again")}
        </Button>
      </div>
    );
  }

  if (stats.length === 0) {
    const hasAvailableStats = totalStats > 0;
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--empty">
        <strong>
          {hasAvailableStats
            ? t("component.workspace_chat.no_stats_selected")
            : t("page.workspace_stats.empty")}
        </strong>
      </div>
    );
  }

  return (
    <div className="workspace-chat-stats-quick-body">
      <div className="workspace-chat-stats-quick-list" role="list">
        {stats.map((stat) => {
          const attention = workspaceStatAttentionPresentation(stat.freshness_status);
          const timing = workspaceStatAssessmentNote(stat) || (stat.current_value_updated_at
            ? `${t("page.workspace_stats.updated")} ${relativeTime(stat.current_value_updated_at)}`
            : t("page.workspace_stats.waiting_for_value"));
          return (
            <div className="workspace-chat-stat-quick-row" role="listitem" key={stat.id}>
              <div className="workspace-chat-stat-quick-copy">
                <strong title={stat.name}>{stat.name}</strong>
                <span>{timing}</span>
              </div>
              <div className="workspace-chat-stat-quick-value">
                <strong>{formatWorkspaceStatValue(stat)}</strong>
                {attention && (
                  <StatusBadge type={attention.type} dot>{attention.label}</StatusBadge>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface WorkspaceStatsQuickAdderProps {
  entries: WorkspaceStatLibraryEntry[];
  loading: boolean;
  error: unknown;
  addingKey?: string;
  addError: unknown;
  onRetry: () => void;
  onAdd: (entry: WorkspaceStatLibraryEntry) => void;
}

function WorkspaceStatsQuickAdder({
  entries,
  loading,
  error,
  addingKey,
  addError,
  onRetry,
  onAdd,
}: WorkspaceStatsQuickAdderProps) {
  if (loading) {
    return (
      <div className="workspace-chat-stats-quick-state" aria-live="polite">
        <LoadingSpinner size={18} />
        <span className="sr-only">{t("component.workspace_chat.loading")}</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--error" role="alert">
        <strong>{t("page.workspace_stats.load_failed")}</strong>
        <span>{queryErrorMessage(error)}</span>
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("page.workspace_stats.try_again")}
        </Button>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="workspace-chat-stats-quick-state">
        <span>{t("page.workspace_stats.no_matching_stats")}</span>
      </div>
    );
  }

  return (
    <div className="workspace-chat-stats-library" data-testid="workspace-chat-stats-library">
      <div className="workspace-chat-stats-library-list" role="list">
        {entries.map((entry) => {
          const pending = addingKey === entry.key;
          return (
            <button
              type="button"
              className="workspace-chat-stats-library-row"
              role="listitem"
              key={entry.key}
              disabled={Boolean(addingKey)}
              onClick={() => onAdd(entry)}
            >
              <span className="workspace-chat-stats-library-copy">
                <strong>{entry.name}</strong>
              </span>
              <span className="workspace-chat-stats-library-add" aria-hidden="true">
                {pending ? <LoadingSpinner size={14} /> : <IconPlus size={15} />}
              </span>
            </button>
          );
        })}
      </div>
      {Boolean(addError) && (
        <div className="workspace-chat-stats-editor-error" role="alert">
          {queryErrorMessage(addError)}
        </div>
      )}
    </div>
  );
}

interface WorkspaceStatsQuickEditorProps {
  stats: WorkspaceStatDefinition[];
  hiddenStatIds: Set<string>;
  loading: boolean;
  error: unknown;
  saving: boolean;
  saveError: boolean;
  onRetry: () => void;
  onToggle: (statId: string) => void;
  onMove: (statId: string, direction: -1 | 1) => void;
}

function WorkspaceStatsQuickEditor({
  stats,
  hiddenStatIds,
  loading,
  error,
  saving,
  saveError,
  onRetry,
  onToggle,
  onMove,
}: WorkspaceStatsQuickEditorProps) {
  if (loading) {
    return (
      <div className="workspace-chat-stats-quick-state" aria-live="polite">
        <LoadingSpinner size={18} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--error" role="alert">
        <strong>{t("page.workspace_stats.load_failed")}</strong>
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("page.workspace_stats.try_again")}
        </Button>
      </div>
    );
  }

  if (stats.length === 0) {
    return (
      <div className="workspace-chat-stats-quick-state">
        <strong>{t("page.workspace_stats.empty")}</strong>
        <span>{t("page.workspace_stats.empty_description")}</span>
      </div>
    );
  }

  return (
    <div className="workspace-chat-stats-editor">
      <div className="workspace-chat-stats-editor-list" role="list">
        {stats.map((stat, index) => {
          const visible = !hiddenStatIds.has(stat.id);
          return (
            <div className="workspace-chat-stats-editor-row" role="listitem" key={stat.id}>
              <div className="workspace-chat-stats-editor-order">
                <button
                  type="button"
                  disabled={index === 0 || saving}
                  aria-label={t("component.workspace_chat.move_stat_up", { name: stat.name })}
                  onClick={() => onMove(stat.id, -1)}
                >
                  <IconArrowUp size={13} />
                </button>
                <button
                  type="button"
                  disabled={index === stats.length - 1 || saving}
                  aria-label={t("component.workspace_chat.move_stat_down", { name: stat.name })}
                  onClick={() => onMove(stat.id, 1)}
                >
                  <IconArrowDown size={13} />
                </button>
              </div>
              <div className="workspace-chat-stats-editor-copy">
                <strong title={stat.name}>{stat.name}</strong>
                <span>{formatWorkspaceStatValue(stat)}</span>
              </div>
              <button
                type="button"
                className="workspace-chat-stats-editor-visibility"
                disabled={saving}
                aria-pressed={visible}
                aria-label={t(
                  visible
                    ? "component.workspace_chat.hide_stat"
                    : "component.workspace_chat.show_stat",
                  { name: stat.name },
                )}
                onClick={() => onToggle(stat.id)}
              >
                {visible ? <IconEye size={16} /> : <IconEyeOff size={16} />}
              </button>
            </div>
          );
        })}
      </div>
      {saveError && (
        <div className="workspace-chat-stats-editor-error" role="alert">
          {t("component.workspace_chat.stats_view_save_failed")}
        </div>
      )}
    </div>
  );
}

interface WorkspaceFilesQuickBodyProps {
  groups: WorkspaceDocumentGroup[];
  returnTo: string;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}

interface WorkspaceQuickFileRowProps {
  document: Document;
  returnTo: string;
}

function WorkspaceQuickFileRow({ document, returnTo }: WorkspaceQuickFileRowProps) {
  const documentKind = fileReferenceKind(
    document.name,
    document.mime_type,
    document.file_type,
  );
  const DocumentTypeIcon = getFileReferenceIcon(documentKind);
  return (
    <Link
      className="workspace-chat-quick-file-row"
      to={`/viewer/${document.id}`}
      state={{ returnTo, chatReturnTo: returnTo }}
      role="listitem"
      onClick={() => preserveReturnToInHistory(returnTo)}
    >
      <span
        aria-hidden="true"
        className="workspace-chat-quick-file-type-icon"
        data-file-kind={documentKind}
      >
        <DocumentTypeIcon size={15} />
      </span>
      <span className="workspace-chat-quick-file-copy">
        <strong title={document.name}>{document.name}</strong>
        <small>{document.file_type || t("page.workspace_detail.file")}</small>
      </span>
      <IconChevronRight size={13} />
    </Link>
  );
}

interface WorkspaceQuickFolderNodeProps {
  groupId: string;
  folder: WorkspaceDocumentFolderNode;
  returnTo: string;
  expandedFolderIds: Set<string>;
  onToggle: (folderId: string) => void;
}

function WorkspaceQuickFolderNode({
  groupId,
  folder,
  returnTo,
  expandedFolderIds,
  onToggle,
}: WorkspaceQuickFolderNodeProps) {
  const folderKey = workspaceQuickFolderKey(groupId, folder.id);
  const expanded = expandedFolderIds.has(folderKey);
  const folderName = folderDisplayName(folder) || t("component.workspace_chat.untitled_folder");
  const contentId = `workspace-quick-folder-${groupId}-${folder.id}`;
  return (
    <div className="workspace-chat-quick-nested-folder" role="listitem">
      <button
        type="button"
        className="workspace-chat-quick-folder-toggle workspace-chat-quick-folder-toggle--nested"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => onToggle(folderKey)}
      >
        <IconChevronRight className="workspace-chat-quick-folder-chevron" size={14} />
        <IconFolder size={16} />
        <span title={folderName}>{folderName}</span>
        <small>{folder.documentCount}</small>
      </button>
      {expanded && (
        <div id={contentId} className="workspace-chat-quick-folder-children" role="list">
          {folder.children.map((child) => (
            <WorkspaceQuickFolderNode
              groupId={groupId}
              folder={child}
              returnTo={returnTo}
              expandedFolderIds={expandedFolderIds}
              onToggle={onToggle}
              key={child.id}
            />
          ))}
          {folder.documents.map((document) => (
            <WorkspaceQuickFileRow
              document={document}
              returnTo={returnTo}
              key={document.id}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function WorkspaceFilesQuickBody({
  groups,
  returnTo,
  loading,
  error,
  onRetry,
}: WorkspaceFilesQuickBodyProps) {
  const [expandedFolderIds, setExpandedFolderIds] = useState<Set<string>>(new Set());
  const sortedGroups = useMemo(
    () => [...groups].sort((left, right) => (
      Number(Boolean(right.is_workspace_file_bucket))
      - Number(Boolean(left.is_workspace_file_bucket))
    )),
    [groups],
  );
  const folderSignature = sortedGroups.map((group) => (
    `${group.id}:${(group.folders || []).map((folder) => folder.id).join(",")}`
  )).join("|");

  useEffect(() => {
    setExpandedFolderIds((current) => {
      if (sortedGroups.length === 0) return new Set();
      const availableFolderIds = new Set(sortedGroups.flatMap((group) => [
        group.id,
        ...(group.folders || []).map((folder) => (
          workspaceQuickFolderKey(group.id, folder.id)
        )),
      ]));
      const next = new Set([...current].filter((folderId) => (
        availableFolderIds.has(folderId)
      )));
      if (sortedGroups.some((group) => next.has(group.id))) return next;
      const firstFolder = sortedGroups.find((group) => group.is_workspace_file_bucket)
        || sortedGroups[0];
      next.add(firstFolder.id);
      return next;
    });
  }, [folderSignature]); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return (
      <div className="workspace-chat-stats-quick-state" aria-live="polite">
        <LoadingSpinner size={18} />
        <span className="sr-only">{t("component.workspace_chat.loading")}</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--error" role="alert">
        <IconWarning className="text-red-500" size={18} />
        <strong>{t("component.workspace_chat.workspace_files_load_failed")}</strong>
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("page.workspace_stats.try_again")}
        </Button>
      </div>
    );
  }

  if (sortedGroups.length === 0) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--empty">
        <IconFolder size={20} />
        <strong>{t("component.workspace_chat.no_workspace_files")}</strong>
        <span>{t("component.workspace_chat.no_workspace_files_description")}</span>
      </div>
    );
  }

  const toggleFolder = (folderId: string) => {
    setExpandedFolderIds((current) => {
      const next = new Set(current);
      if (next.has(folderId)) next.delete(folderId);
      else next.add(folderId);
      return next;
    });
  };

  return (
    <div className="workspace-chat-quick-folder-list" role="list">
      {sortedGroups.map((group) => {
        const expanded = expandedFolderIds.has(group.id);
        const tree = buildWorkspaceDocumentTree(group);
        const documentCount = group.document_count ?? (group.documents || []).length;
        const folderName = group.name || (
          group.is_workspace_file_bucket
            ? t("page.workspace_detail.workspace_files")
            : t("component.workspace_chat.untitled_folder")
        );
        return (
          <div className="workspace-chat-quick-folder" role="listitem" key={group.id}>
            <button
              type="button"
              className="workspace-chat-quick-folder-toggle"
              aria-expanded={expanded}
              aria-controls={`workspace-quick-folder-${group.id}`}
              onClick={() => toggleFolder(group.id)}
            >
              <IconChevronRight className="workspace-chat-quick-folder-chevron" size={14} />
              <IconFolder size={16} />
              <span title={folderName}>{folderName}</span>
              <small>{documentCount}</small>
            </button>
            {expanded && (
              <div
                id={`workspace-quick-folder-${group.id}`}
                className="workspace-chat-quick-file-list"
                role="list"
              >
                {tree.folders.map((folder) => (
                  <WorkspaceQuickFolderNode
                    groupId={group.id}
                    folder={folder}
                    returnTo={returnTo}
                    expandedFolderIds={expandedFolderIds}
                    onToggle={toggleFolder}
                    key={folder.id}
                  />
                ))}
                {tree.documents.map((document) => (
                  <WorkspaceQuickFileRow
                    document={document}
                    returnTo={returnTo}
                    key={document.id}
                  />
                ))}
                {tree.folders.length === 0 && tree.documents.length === 0 && (
                  <div className="workspace-chat-quick-folder-empty">
                    {t("component.workspace_chat.folder_is_empty")}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function taskStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    created: t("page.dashboard.created"),
    proposed: t("page.tasks.proposed"),
    pending: t("status.pending"),
    scheduled: t("status.scheduled"),
    in_progress: t("status.in_progress"),
    waiting_on_customer: t("page.tasks.waiting_on_customer"),
    on_hold: t("page.tasks.on_hold"),
    blocked: t("page.tasks.blocked"),
    completed: t("status.completed"),
    cancelled: t("status.cancelled"),
    failed: t("page.dashboard.failed"),
  };
  return labels[status] || formatUserFacingLabel(status);
}

function taskStatusBadgeType(status: string): string {
  if (status === "failed" || status === "blocked") return "danger";
  if (status === "waiting_on_customer" || status === "on_hold") return "warning";
  if (status === "in_progress") return "teal";
  if (status === "completed") return "success";
  if (status === "cancelled") return "gray";
  return "info";
}

function taskStatusNeedsAttention(status: string): boolean {
  return ["waiting_on_customer", "on_hold", "blocked", "failed"].includes(status);
}

function taskAttentionOccurrenceKey(task: Task): string {
  return `${task.id}:${task.status}:${task.status_changed_at || task.created_at || "unknown"}`;
}

interface WorkspaceQuickAccessMenuItemProps {
  section: WorkspaceQuickTab;
  icon: ReactNode;
  label: string;
  description: string;
  status?: "warning" | "danger";
  statusLabel?: string;
  buttonRef: (node: HTMLButtonElement | null) => void;
  onSelect: (section: WorkspaceQuickTab) => void;
}

function WorkspaceQuickAccessMenuItem({
  section,
  icon,
  label,
  description,
  status,
  statusLabel,
  buttonRef,
  onSelect,
}: WorkspaceQuickAccessMenuItemProps) {
  return (
    <button
      type="button"
      ref={buttonRef}
      className="chat-quick-access-row workspace-chat-quick-menu-row"
      data-testid={`workspace-chat-quick-access-${section}`}
      aria-label={statusLabel ? `${label}: ${description}. ${statusLabel}` : `${label}: ${description}`}
      onClick={() => onSelect(section)}
    >
      <span className="chat-quick-access-row-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="chat-quick-access-row-copy">
        <strong>{label}</strong>
        <span>{description}</span>
      </span>
      <span className="workspace-chat-quick-menu-trailing" aria-hidden="true">
        {status && (
          <span
            className={`workspace-chat-quick-menu-status ${status === "danger" ? "bg-red-500" : "bg-amber-500"}`}
          />
        )}
        <IconChevronRight className="chat-quick-access-row-chevron" size={15} />
      </span>
    </button>
  );
}

interface WorkspaceQuickDetailHeaderProps {
  title: string;
  actions?: ReactNode;
  backButtonRef: RefObject<HTMLButtonElement>;
  onBack: () => void;
}

function WorkspaceQuickDetailHeader({
  title,
  actions,
  backButtonRef,
  onBack,
}: WorkspaceQuickDetailHeaderProps) {
  return (
    <div className="workspace-chat-quick-detail-header">
      <button
        type="button"
        ref={backButtonRef}
        className="workspace-chat-quick-detail-back"
        aria-label={`${t("page.agent_initiative.back")}: ${t("component.workspace_chat.workspace_quick_access")}`}
        onClick={onBack}
      >
        <IconChevronLeft size={16} aria-hidden="true" />
        <strong>{title}</strong>
      </button>
      {actions && (
        <div className="workspace-chat-quick-detail-actions">
          {actions}
        </div>
      )}
    </div>
  );
}

/** A new Task of this Workspace; the Workspace's own Agents pick it up from there. */
function WorkspaceTaskQuickCreate({ workspaceId, onDone }: { workspaceId: string; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const create = useMutation({
    mutationFn: () => api.tasks.create({ title: title.trim(), description: description.trim(), workspace_id: workspaceId }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      onDone();
    },
  });
  return (
    <form
      className="space-y-2 px-1 pb-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (title.trim() && !create.isPending) create.mutate();
      }}
    >
      <Input
        ariaLabel={t("page.tasks.task_title")}
        placeholder={t("page.tasks.task_title")}
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        disabled={create.isPending}
        autoFocus
      />
      <Textarea
        ariaLabel={t("component.workspace_chat.task_details_optional")}
        placeholder={t("component.workspace_chat.task_details_optional")}
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        disabled={create.isPending}
        rows={3}
      />
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" size="sm" onClick={onDone} disabled={create.isPending}>
          {t("action.cancel")}
        </Button>
        <Button type="submit" size="sm" loading={create.isPending} disabled={!title.trim()}>
          {t("page.tasks.create_task")}
        </Button>
      </div>
    </form>
  );
}

interface WorkspaceTasksQuickBodyProps {
  tasks: Task[];
  returnTo: string;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}

function WorkspaceTasksQuickBody({
  tasks,
  returnTo,
  loading,
  error,
  onRetry,
}: WorkspaceTasksQuickBodyProps) {
  if (loading) {
    return (
      <div className="workspace-chat-stats-quick-state" aria-live="polite">
        <LoadingSpinner size={18} />
        <span className="sr-only">{t("component.workspace_chat.loading")}</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--error" role="alert">
        <IconWarning className="text-red-500" size={18} />
        <strong>{t("component.workspace_chat.task_briefs_load_failed")}</strong>
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("page.workspace_stats.try_again")}
        </Button>
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--empty">
        <IconList size={20} />
        <strong>{t("component.workspace_chat.no_task_briefs")}</strong>
        <span>{t("component.workspace_chat.no_task_briefs_description")}</span>
      </div>
    );
  }

  return (
    <div className="workspace-chat-quick-task-list" role="list">
      {tasks.map((task) => (
        <Link
          className="workspace-chat-quick-task-row"
          to={`/tasks/${task.id}`}
          state={{ returnTo, chatReturnTo: returnTo }}
          role="listitem"
          key={task.id}
          onClick={() => preserveReturnToInHistory(returnTo)}
        >
          <span className="workspace-chat-quick-task-copy">
            <strong title={task.title}>{task.title}</strong>
            <span>{task.description || t("component.workspace_chat.no_task_brief_description")}</span>
          </span>
          <span className="workspace-chat-quick-task-meta">
            <StatusBadge type={taskStatusBadgeType(task.status)} dot>
              {taskStatusLabel(task.status)}
            </StatusBadge>
            <IconChevronRight size={13} />
          </span>
        </Link>
      ))}
    </div>
  );
}

interface WorkspaceSettingsQuickBodyProps {
  workspace?: Workspace;
  canManage: boolean;
  loading: boolean;
  loadError: unknown;
  saving: boolean;
  saveError: unknown;
  saved: boolean;
  onRetry: () => void;
  onEdit: () => void;
  onSave: (data: Pick<Workspace, "name" | "description">) => void;
}

function WorkspaceSettingsQuickBody({
  workspace,
  canManage,
  loading,
  loadError,
  saving,
  saveError,
  saved,
  onRetry,
  onEdit,
  onSave,
}: WorkspaceSettingsQuickBodyProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    setName(workspace?.name || "");
    setDescription(workspace?.description || "");
  }, [workspace?.description, workspace?.id, workspace?.name]);

  if (loading && !workspace) {
    return (
      <div className="workspace-chat-stats-quick-state" aria-live="polite">
        <LoadingSpinner size={18} />
        <span className="sr-only">{t("component.workspace_chat.loading")}</span>
      </div>
    );
  }

  if (!workspace) {
    return (
      <div className="workspace-chat-stats-quick-state workspace-chat-stats-quick-state--error" role="alert">
        <IconWarning className="text-red-500" size={18} />
        <strong>{t("component.workspace_chat.settings_load_failed")}</strong>
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("page.workspace_stats.try_again")}
        </Button>
      </div>
    );
  }

  const normalizedName = name.trim();
  const normalizedDescription = description.trim();
  const dirty = normalizedName !== workspace.name
    || normalizedDescription !== (workspace.description || "");

  return (
    <form
      className="workspace-chat-quick-settings-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!canManage || !normalizedName || !dirty) return;
        onSave({ name: normalizedName, description: normalizedDescription });
      }}
    >
      <Input
        label={t("page.task_collections.name")}
        value={name}
        required
        error={canManage && !normalizedName
          ? t("component.workspace_chat.settings_name_required")
          : undefined}
        disabled={!canManage || saving}
        onChange={(event) => {
          setName(event.target.value);
          onEdit();
        }}
      />
      <Textarea
        label={t("page.task_collections.description")}
        value={description}
        rows={3}
        disabled={!canManage || saving}
        onChange={(event) => {
          setDescription(event.target.value);
          onEdit();
        }}
      />
      {!canManage && (
        <p className="workspace-chat-quick-settings-note">
          {t("component.workspace_chat.settings_read_only")}
        </p>
      )}
      {Boolean(loadError) && (
        <p className="workspace-chat-quick-settings-error" role="alert">
          {t("component.workspace_chat.settings_refresh_failed")}
        </p>
      )}
      {Boolean(saveError) && (
        <p className="workspace-chat-quick-settings-error" role="alert">
          {t("page.workspace_detail.could_not_update_workspace")}
        </p>
      )}
      {saved && !dirty && (
        <p className="workspace-chat-quick-settings-success" role="status">
          {t("page.workspace_detail.workspace_updated")}
        </p>
      )}
      {canManage && (
        <div className="workspace-chat-quick-settings-actions">
          <Button
            type="submit"
            size="sm"
            loading={saving}
            disabled={!normalizedName || !dirty}
          >
            {t("action.save")}
          </Button>
        </div>
      )}
    </form>
  );
}

export default function WorkspaceStatsQuickAccess({
  enabled = true,
  workspaceId,
  workspace,
  workspaceName,
  initiativeConversationId,
  canManage = false,
}: WorkspaceStatsQuickAccessProps) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const detailBackRef = useRef<HTMLButtonElement>(null);
  const menuItemRefs = useRef<Partial<Record<WorkspaceQuickTab, HTMLButtonElement | null>>>({});
  const overviewReturnFocusRef = useRef<WorkspaceQuickTab | null>(null);
  const queryClient = useQueryClient();
  const location = useLocation();
  const currentUserId = useAuthStore((state) => state.user?.id);
  const taskAttentionSeenStorageKey = taskAttentionStorageKey(currentUserId, workspaceId);
  const currentReturnTo = useMemo(() => {
    const returnSearch = new URLSearchParams(location.search);
    if (location.pathname === "/chat") returnSearch.set("workspace", workspaceId);
    const serializedSearch = returnSearch.toString();
    return `${location.pathname}${serializedSearch ? `?${serializedSearch}` : ""}${location.hash}`;
  }, [location.hash, location.pathname, location.search, workspaceId]);
  const [activeTab, setActiveTab] = useState<WorkspaceQuickTab | null>(null);
  const [quickAccessOpen, setQuickAccessOpen] = useState(false);
  const [pageIsActive, setPageIsActive] = useState(false);
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [creatingTask, setCreatingTask] = useState(false);
  const collaborator = useIsCollaborator();
  const [preference, setPreference] = useState<QuickViewPreference | null>(null);
  const [taskAttentionSeenState, setTaskAttentionSeenState] = useState(() => ({
    storageKey: taskAttentionSeenStorageKey,
    attentionKeys: readSeenTaskAttention(taskAttentionSeenStorageKey),
  }));
  const initiativesQuery = useConversationInitiativesQuery(initiativeConversationId);
  const conversationInitiatives = conversationInitiativeItems(initiativesQuery.data);
  const hasInitiatives = conversationInitiatives.length > 0;
  const resolvedActiveTab: WorkspaceQuickTab | null = (
    activeTab === "initiatives" && !hasInitiatives
  ) ? null : activeTab;
  const statsQuery = useQuery({
    queryKey: ["workspace-stats", workspaceId],
    queryFn: () => api.workspaces.stats.list(workspaceId),
    enabled: Boolean(workspaceId && enabled && quickAccessOpen),
    staleTime: 30_000,
  });
  const quickViewQuery = useQuery({
    queryKey: ["workspace-stats-quick-view", workspaceId],
    queryFn: () => api.workspaces.stats.quickView(workspaceId),
    enabled: Boolean(workspaceId && enabled && quickAccessOpen),
    staleTime: 30_000,
  });
  const libraryQuery = useQuery({
    queryKey: ["workspace-stats-library", workspaceId],
    queryFn: () => api.workspaces.stats.library(),
    enabled: adding,
    staleTime: 30_000,
    refetchOnMount: "always",
  });
  const filesQuery = useQuery({
    // Share the full file page's cache and mutation invalidations. Start while
    // the overview is open so selecting Files can use the in-flight result.
    queryKey: ["workspace-documents", workspaceId],
    queryFn: () => api.workspaces.documents(workspaceId),
    enabled: Boolean(workspaceId) && (quickAccessOpen || activeTab === "files"),
    staleTime: 30_000,
  });
  const taskBriefsQuery = useQuery({
    queryKey: ["tasks", "workspace-quick-briefs", workspaceId],
    queryFn: () => api.tasks.list({ workspace_id: workspaceId, limit: 20 }),
    enabled: Boolean(workspaceId && enabled && quickAccessOpen),
    staleTime: 30_000,
  });
  const taskAttentionQuery = useQuery({
    queryKey: ["tasks", "workspace-attention", workspaceId],
    queryFn: () => api.tasks.listAttention(workspaceId),
    enabled: Boolean(workspaceId && enabled),
    staleTime: 30_000,
  });
  const workspaceQuery = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => api.workspaces.get(workspaceId),
    enabled: Boolean(workspaceId) && activeTab === "settings",
    initialData: workspace,
    initialDataUpdatedAt: 0,
    staleTime: 30_000,
  });
  const saveViewMutation = useMutation({
    mutationFn: (next: QuickViewPreference) => (
      api.workspaces.stats.updateQuickView(workspaceId, next)
    ),
  });
  const updateWorkspaceMutation = useMutation({
    mutationFn: (data: Pick<Workspace, "name" | "description">) => (
      api.workspaces.update(workspaceId, data)
    ),
    onSuccess: (updatedWorkspace) => {
      queryClient.setQueryData(["workspace", workspaceId], updatedWorkspace);
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });

  useEffect(() => {
    overviewReturnFocusRef.current = null;
    setActiveTab(null);
    setEditing(false);
    setAdding(false);
    setPreference(null);
  }, [workspaceId]);

  useEffect(() => {
    if (activeTab !== "initiatives" || hasInitiatives) return;
    overviewReturnFocusRef.current = "metrics";
    setActiveTab(null);
  }, [activeTab, hasInitiatives]);

  useEffect(() => {
    const focusFrame = window.requestAnimationFrame(() => {
      if (resolvedActiveTab) {
        detailBackRef.current?.focus();
        return;
      }
      const returnSection = overviewReturnFocusRef.current;
      if (!returnSection) return;
      const returnTarget = menuItemRefs.current[returnSection];
      const connectedTarget = returnTarget?.isConnected
        ? returnTarget
        : menuItemRefs.current.metrics;
      connectedTarget?.focus();
      overviewReturnFocusRef.current = null;
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [resolvedActiveTab]);

  useEffect(() => {
    setTaskAttentionSeenState({
      storageKey: taskAttentionSeenStorageKey,
      attentionKeys: readSeenTaskAttention(taskAttentionSeenStorageKey),
    });
  }, [taskAttentionSeenStorageKey]);

  useEffect(() => {
    if (typeof document === "undefined" || typeof window === "undefined") return undefined;
    const updatePageIsActive = () => {
      setPageIsActive(document.visibilityState === "visible" && document.hasFocus());
    };
    updatePageIsActive();
    document.addEventListener("visibilitychange", updatePageIsActive);
    window.addEventListener("focus", updatePageIsActive);
    window.addEventListener("blur", updatePageIsActive);
    return () => {
      document.removeEventListener("visibilitychange", updatePageIsActive);
      window.removeEventListener("focus", updatePageIsActive);
      window.removeEventListener("blur", updatePageIsActive);
    };
  }, []);

  useEffect(() => {
    if (!taskAttentionSeenStorageKey || typeof window === "undefined") return undefined;
    const handleStorage = (event: StorageEvent) => {
      if (event.key !== taskAttentionSeenStorageKey) return;
      setTaskAttentionSeenState({
        storageKey: taskAttentionSeenStorageKey,
        attentionKeys: parseSeenTaskAttention(event.newValue),
      });
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, [taskAttentionSeenStorageKey]);

  useEffect(() => {
    if (!quickViewQuery.data) return;
    setPreference({
      ordered_stat_ids: quickViewQuery.data.ordered_stat_ids,
      hidden_stat_ids: quickViewQuery.data.hidden_stat_ids,
    });
  }, [quickViewQuery.data]);

  const stats = statsQuery.data?.items || [];
  const documentGroups = (Array.isArray(filesQuery.data) ? filesQuery.data : []) as WorkspaceDocumentGroup[];
  const taskBriefs = taskBriefsQuery.data?.items || [];
  const attentionTasks = taskAttentionQuery.data || [];
  const installedKeys = useMemo(
    () => new Set(stats.flatMap((stat) => [stat.key, stat.library_key || ""])),
    [stats],
  );
  const availableLibraryEntries = useMemo(
    () => (libraryQuery.data?.items || []).filter((entry) => (
      !installedKeys.has(entry.key)
      && (entry.collector_type !== "integration" || entry.available_connections.length > 0)
    )),
    [installedKeys, libraryQuery.data?.items],
  );
  const currentPreference = preference || {
    ordered_stat_ids: [],
    hidden_stat_ids: [],
  };
  const sortedStats = useMemo(
    () => orderedStats(stats, currentPreference.ordered_stat_ids),
    [currentPreference.ordered_stat_ids, stats],
  );
  const hiddenStatIds = useMemo(
    () => new Set(currentPreference.hidden_stat_ids),
    [currentPreference.hidden_stat_ids],
  );
  const visibleStats = sortedStats.filter((stat) => !hiddenStatIds.has(stat.id));
  const attention = stats.map((stat) => (
    workspaceStatAttentionPresentation(stat.freshness_status)
  )).filter(Boolean);
  const hasCollectionError = stats.some((stat) => stat.freshness_status === "collection_error");
  const tasksNeedingAttention = attentionTasks.filter((task) => taskStatusNeedsAttention(task.status));
  const taskAttentionKeys = tasksNeedingAttention
    .map(taskAttentionOccurrenceKey)
    .sort();
  const seenTaskAttentionKeys = taskAttentionSeenState.storageKey === taskAttentionSeenStorageKey
    ? taskAttentionSeenState.attentionKeys
    : [];
  const taskAttentionIsVisible = quickAccessOpen && resolvedActiveTab === "tasks" && pageIsActive;
  const unseenAttentionSeverity = unseenTaskAttentionSeverity(
    tasksNeedingAttention.map((task) => ({
      key: taskAttentionOccurrenceKey(task),
      status: task.status,
    })),
    seenTaskAttentionKeys,
  );
  const hasUnseenTaskAttention = Boolean(
    !taskAttentionIsVisible
    && unseenAttentionSeverity,
  );
  const metricsSectionStatus: "warning" | "danger" | undefined = statsQuery.isError
    ? "danger"
    : hasCollectionError
      ? "danger"
      : attention.length > 0
        ? "warning"
        : undefined;
  const tasksSectionStatus: "warning" | "danger" | undefined = (
    taskBriefsQuery.isError || taskAttentionQuery.isError
  )
    ? "danger"
    : !hasUnseenTaskAttention
      ? undefined
      : unseenAttentionSeverity;

  useEffect(() => {
    if (!taskAttentionQuery.isSuccess) return;
    if (taskAttentionSeenState.storageKey !== taskAttentionSeenStorageKey) return;
    const nextAttentionKeys = nextSeenTaskAttentionKeys(
      taskAttentionKeys,
      seenTaskAttentionKeys,
      taskAttentionIsVisible,
    );
    if (nextAttentionKeys.join("|") === seenTaskAttentionKeys.join("|")) return;
    setTaskAttentionSeenState({
      storageKey: taskAttentionSeenStorageKey,
      attentionKeys: nextAttentionKeys,
    });
    writeSeenTaskAttention(taskAttentionSeenStorageKey, nextAttentionKeys);
  }, [
    seenTaskAttentionKeys,
    taskAttentionIsVisible,
    taskAttentionKeys,
    taskAttentionSeenStorageKey,
    taskAttentionSeenState.storageKey,
    taskAttentionQuery.isSuccess,
  ]);
  const addStatMutation = useMutation({
    mutationFn: async (entry: WorkspaceStatLibraryEntry) => {
      const connection = entry.available_connections.find((candidate) => candidate.is_default)
        || entry.available_connections[0];
      let stat = await api.workspaces.stats.create(workspaceId, {
        library_key: entry.key,
        collector_config: connection ? { connection_id: connection.id } : undefined,
      });
      if (stat.collector_type !== "manual") {
        try {
          stat = (await api.workspaces.stats.collect(workspaceId, stat.id)).stat;
        } catch {
          // The stat remains installed and will surface its collection state in the list.
        }
      }
      return stat;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["workspace-stats", workspaceId] });
      await statsQuery.refetch();
      setAdding(false);
    },
  });

  const persistPreference = (next: QuickViewPreference) => {
    const previous = currentPreference;
    setPreference(next);
    saveViewMutation.mutate(next, {
      onError: () => setPreference(previous),
    });
  };

  const toggleStat = (statId: string) => {
    const hidden = new Set(currentPreference.hidden_stat_ids);
    if (hidden.has(statId)) hidden.delete(statId);
    else hidden.add(statId);
    persistPreference({
      ordered_stat_ids: sortedStats.map((stat) => stat.id),
      hidden_stat_ids: [...hidden],
    });
  };

  const moveStat = (statId: string, direction: -1 | 1) => {
    const order = sortedStats.map((stat) => stat.id);
    const currentIndex = order.indexOf(statId);
    const nextIndex = currentIndex + direction;
    if (currentIndex < 0 || nextIndex < 0 || nextIndex >= order.length) return;
    [order[currentIndex], order[nextIndex]] = [order[nextIndex], order[currentIndex]];
    persistPreference({
      ordered_stat_ids: order,
      hidden_stat_ids: currentPreference.hidden_stat_ids,
    });
  };

  const selectedCountLabel = t("component.workspace_chat.stats_selected_count", {
    selected: visibleStats.length,
    total: stats.length,
  });
  const ariaLabel = [
    t("component.workspace_chat.workspace_quick_access"),
    workspaceName,
    selectedCountLabel,
  ]
    .filter(Boolean)
    .join(" · ");
  const metricsStatusLabel = statsQuery.isError
    ? t("page.workspace_stats.load_failed")
    : attention[0]?.label;
  const tasksStatusLabel = taskBriefsQuery.isError || taskAttentionQuery.isError
    ? t("component.workspace_chat.task_briefs_load_failed")
    : hasUnseenTaskAttention
      ? t("page.tasks.needs_attention")
      : undefined;
  const metricsMenuDescription = statsQuery.isLoading || quickViewQuery.isLoading
    ? t("component.workspace_chat.loading")
    : statsQuery.isError || quickViewQuery.isError
      ? t("page.workspace_stats.load_failed")
      : selectedCountLabel;
  const tasksMenuDescription = taskBriefsQuery.isLoading
    ? t("component.workspace_chat.loading")
    : taskBriefsQuery.isError
      ? t("component.workspace_chat.task_briefs_load_failed")
      : hasUnseenTaskAttention
        ? t("page.tasks.needs_attention")
        : taskBriefs.length === 0
          ? t("component.workspace_chat.no_task_briefs")
          : t("component.workspace_chat.no_task_briefs_description");

  const openSection = (section: WorkspaceQuickTab) => {
    overviewReturnFocusRef.current = null;
    updateWorkspaceMutation.reset();
    setActiveTab(section);
    setEditing(false);
    setAdding(false);
    addStatMutation.reset();
  };

  const returnToOverview = () => {
    overviewReturnFocusRef.current = resolvedActiveTab;
    updateWorkspaceMutation.reset();
    setActiveTab(null);
    setEditing(false);
    setAdding(false);
    addStatMutation.reset();
  };

  const handleOpenChange = (open: boolean) => {
    setQuickAccessOpen(open);
    if (open) return;
    overviewReturnFocusRef.current = null;
    updateWorkspaceMutation.reset();
    setActiveTab(null);
    setEditing(false);
    setAdding(false);
    addStatMutation.reset();
  };

  return (
    <AnchoredPopover
      align="right"
      width={360}
      persistentOnWide
      persistentBreakpoint={1280}
      persistentPlacement="below-anchor"
      persistentInlineInset={24}
      onOpenChange={handleOpenChange}
      ariaLabel={ariaLabel}
      panelClassName="workspace-chat-stats-popover"
      trigger={(
        <button
          type="button"
          ref={triggerRef}
          className="workspace-chat-stats-trigger"
          aria-label={ariaLabel}
          title={t("component.workspace_chat.workspace_quick_access")}
          data-testid="workspace-chat-stats-trigger"
        >
          <IconViewOptions size={18} />
        </button>
      )}
    >
      {({ close }) => (
        <div className="workspace-chat-stats-popover-content">
          {resolvedActiveTab === null && (
            <div
              className="chat-quick-access-panel workspace-chat-quick-menu"
              data-testid="workspace-chat-quick-access-menu"
            >
              {hasInitiatives && (
                <WorkspaceQuickAccessMenuItem
                  key="initiatives"
                  section="initiatives"
                  icon={<IconBell size={16} />}
                  label={t("page.agent_initiative.title")}
                  description={t("page.agent_initiative.quick_access_description")}
                  buttonRef={(node) => { menuItemRefs.current.initiatives = node; }}
                  onSelect={openSection}
                />
              )}
              <WorkspaceQuickAccessMenuItem
                key="metrics"
                section="metrics"
                icon={<IconTrendingUp size={16} />}
                label={t("page.workspace_stats.title")}
                description={metricsMenuDescription}
                status={metricsSectionStatus}
                statusLabel={metricsStatusLabel}
                buttonRef={(node) => { menuItemRefs.current.metrics = node; }}
                onSelect={openSection}
              />
              <WorkspaceQuickAccessMenuItem
                key="files"
                section="files"
                icon={<IconFolder size={16} />}
                label={t("component.workspace_chat.files")}
                description={t("component.workspace_chat.workspace_folders")}
                buttonRef={(node) => { menuItemRefs.current.files = node; }}
                onSelect={openSection}
              />
              <WorkspaceQuickAccessMenuItem
                key="tasks"
                section="tasks"
                icon={<IconList size={16} />}
                label={t("component.workspace_chat.tasks")}
                description={tasksMenuDescription}
                status={tasksSectionStatus}
                statusLabel={tasksStatusLabel}
                buttonRef={(node) => { menuItemRefs.current.tasks = node; }}
                onSelect={openSection}
              />
              <WorkspaceQuickAccessMenuItem
                key="settings"
                section="settings"
                icon={<IconSettings size={16} />}
                label={t("component.workspace_chat.settings")}
                description={t("component.workspace_chat.settings_quick_description")}
                buttonRef={(node) => { menuItemRefs.current.settings = node; }}
                onSelect={openSection}
              />
            </div>
          )}

          {resolvedActiveTab === "initiatives" && initiativesQuery.data && (
            <div className="workspace-chat-quick-section">
              <WorkspaceQuickDetailHeader
                title={t("page.agent_initiative.title")}
                backButtonRef={detailBackRef}
                onBack={returnToOverview}
              />
              <ConversationInitiativesQuickList
                response={initiativesQuery.data}
                fallbackTitle={workspaceName}
                onSelect={close}
                returnFocusTo={triggerRef.current}
              />
            </div>
          )}

          {resolvedActiveTab === "metrics" && (
            <div className="workspace-chat-quick-section">
              <WorkspaceQuickDetailHeader
                title={adding
                  ? t("page.workspace_stats.add")
                  : editing
                    ? t("component.workspace_chat.customize_stats")
                    : t("page.workspace_stats.title")}
                backButtonRef={detailBackRef}
                onBack={returnToOverview}
                actions={editing || adding ? (
                  <button
                    key="done"
                    type="button"
                    className="workspace-chat-stats-popover-header-done"
                    aria-label={t("action.done")}
                    title={t("action.done")}
                    data-testid="workspace-chat-stats-done"
                    onClick={() => {
                      setEditing(false);
                      setAdding(false);
                      addStatMutation.reset();
                    }}
                  >
                    <IconCheck size={16} />
                  </button>
                ) : (
                  <div className="workspace-chat-stats-popover-header-actions">
                    <button
                      type="button"
                      className="workspace-chat-stats-popover-header-arrange"
                      aria-label={t("component.workspace_chat.customize_stats")}
                      title={t("component.workspace_chat.customize_stats")}
                      data-testid="workspace-chat-stats-customize"
                      onClick={() => {
                        setAdding(false);
                        setEditing(true);
                      }}
                    >
                      <IconSort size={16} />
                    </button>
                    <button
                      key="add"
                      type="button"
                      className="workspace-chat-stats-popover-header-add"
                      aria-label={t("page.workspace_stats.add")}
                      title={t("page.workspace_stats.add")}
                      data-testid="workspace-chat-stats-add"
                      onClick={() => {
                        setEditing(false);
                        setAdding(true);
                        addStatMutation.reset();
                      }}
                    >
                      <IconPlus size={17} />
                    </button>
                  </div>
                )}
              />
              {adding ? (
                <WorkspaceStatsQuickAdder
                  entries={availableLibraryEntries}
                  loading={libraryQuery.isLoading}
                  error={libraryQuery.error}
                  addingKey={addStatMutation.isPending ? addStatMutation.variables?.key : undefined}
                  addError={addStatMutation.error}
                  onRetry={() => void libraryQuery.refetch()}
                  onAdd={(entry) => addStatMutation.mutate(entry)}
                />
              ) : editing ? (
                <WorkspaceStatsQuickEditor
                  stats={sortedStats}
                  hiddenStatIds={hiddenStatIds}
                  loading={statsQuery.isLoading || quickViewQuery.isLoading}
                  error={statsQuery.error || quickViewQuery.error}
                  saving={saveViewMutation.isPending}
                  saveError={saveViewMutation.isError}
                  onRetry={() => {
                    void statsQuery.refetch();
                    void quickViewQuery.refetch();
                  }}
                  onToggle={toggleStat}
                  onMove={moveStat}
                />
              ) : (
                <WorkspaceStatsQuickBody
                  stats={visibleStats}
                  totalStats={stats.length}
                  loading={statsQuery.isLoading || quickViewQuery.isLoading}
                  error={statsQuery.error || quickViewQuery.error}
                  onRetry={() => {
                    void statsQuery.refetch();
                    void quickViewQuery.refetch();
                  }}
                />
              )}
            </div>
          )}

          {resolvedActiveTab === "files" && (
            <div className="workspace-chat-quick-section">
              <WorkspaceQuickDetailHeader
                title={t("component.workspace_chat.files")}
                backButtonRef={detailBackRef}
                onBack={returnToOverview}
                actions={(
                  <Link
                    to={`/knowledge?workspace_id=${encodeURIComponent(workspaceId)}`}
                    state={{ returnTo: currentReturnTo, chatReturnTo: currentReturnTo }}
                    onClick={() => preserveReturnToInHistory(currentReturnTo)}
                  >
                    {t("component.workspace_chat.view_all")}
                    <IconChevronRight size={13} />
                  </Link>
                )}
              />
              <WorkspaceFilesQuickBody
                groups={documentGroups}
                returnTo={currentReturnTo}
                loading={filesQuery.isLoading}
                error={filesQuery.error}
                onRetry={() => void filesQuery.refetch()}
              />
            </div>
          )}

          {resolvedActiveTab === "tasks" && (
            <div className="workspace-chat-quick-section">
              <WorkspaceQuickDetailHeader
                title={t("component.workspace_chat.tasks")}
                backButtonRef={detailBackRef}
                onBack={returnToOverview}
                actions={(
                  <>
                    {canManage && !creatingTask && (
                      <button
                        type="button"
                        className="workspace-chat-stats-popover-header-add"
                        aria-label={t("page.tasks.new_task")}
                        title={t("page.tasks.new_task")}
                        onClick={() => setCreatingTask(true)}
                      >
                        <IconPlus size={17} />
                      </button>
                    )}
                    {/* The company task board is not part of a collaborator's session. */}
                    {!collaborator && (
                      <Link
                        to={`/tasks?workspaceId=${encodeURIComponent(workspaceId)}`}
                        state={{ returnTo: currentReturnTo, chatReturnTo: currentReturnTo }}
                        onClick={() => preserveReturnToInHistory(currentReturnTo)}
                      >
                        {t("component.workspace_chat.view_all")}
                        <IconChevronRight size={13} />
                      </Link>
                    )}
                  </>
                )}
              />
              {creatingTask && (
                <WorkspaceTaskQuickCreate workspaceId={workspaceId} onDone={() => setCreatingTask(false)} />
              )}
              <WorkspaceTasksQuickBody
                tasks={taskBriefs}
                returnTo={currentReturnTo}
                loading={taskBriefsQuery.isLoading}
                error={taskBriefsQuery.error}
                onRetry={() => void taskBriefsQuery.refetch()}
              />
            </div>
          )}

          {resolvedActiveTab === "settings" && (
            <div className="workspace-chat-quick-section">
              <WorkspaceQuickDetailHeader
                title={t("component.workspace_chat.settings")}
                backButtonRef={detailBackRef}
                onBack={returnToOverview}
                actions={(
                  <Link
                    to={`/workspaces/${workspaceId}?tab=settings`}
                    state={{ returnTo: currentReturnTo, chatReturnTo: currentReturnTo }}
                    onClick={() => preserveReturnToInHistory(currentReturnTo)}
                  >
                    {t("component.workspace_chat.view_all")}
                    <IconChevronRight size={13} />
                  </Link>
                )}
              />
              <WorkspaceSettingsQuickBody
                workspace={workspaceQuery.data}
                canManage={canManage}
                loading={workspaceQuery.isLoading}
                loadError={workspaceQuery.error}
                saving={updateWorkspaceMutation.isPending}
                saveError={updateWorkspaceMutation.error}
                saved={updateWorkspaceMutation.isSuccess}
                onRetry={() => void workspaceQuery.refetch()}
                onEdit={() => updateWorkspaceMutation.reset()}
                onSave={(data) => updateWorkspaceMutation.mutate(data)}
              />
            </div>
          )}
        </div>
      )}
    </AnchoredPopover>
  );
}
