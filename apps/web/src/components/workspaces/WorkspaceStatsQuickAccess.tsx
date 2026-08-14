import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";

import {
  api,
  type WorkspaceStatDefinition,
  type WorkspaceStatLibraryEntry,
  type WorkspaceStatsQuickView,
} from "../../lib/api";
import { preserveReturnToInHistory } from "../../lib/chatRouteReferences";
import { relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel } from "../../lib/taskDisplay";
import type { Document, Task } from "../../lib/types";
import {
  formatWorkspaceStatValue,
  workspaceStatAttentionPresentation,
} from "../../lib/workspaceStats";
import {
  IconArrowDown,
  IconArrowUp,
  IconCheck,
  IconChevronRight,
  IconDocument,
  IconEye,
  IconEyeOff,
  IconFolder,
  IconList,
  IconPlus,
  IconSort,
  IconTrendingUp,
  IconViewOptions,
  IconWarning,
} from "../icons";
import AnchoredPopover from "../ui/AnchoredPopover";
import Button from "../ui/Button";
import LoadingSpinner from "../ui/LoadingSpinner";
import StatusBadge from "../ui/StatusBadge";
import TabSwitcher from "../ui/TabSwitcher";

interface WorkspaceStatsQuickAccessProps {
  workspaceId: string;
  workspaceName?: string;
}

type QuickViewPreference = Omit<WorkspaceStatsQuickView, "configured">;
type WorkspaceQuickTab = "metrics" | "files" | "tasks";

interface WorkspaceDocumentGroup {
  id: string;
  name?: string | null;
  is_workspace_file_bucket?: boolean;
  is_default_collection?: boolean;
  document_count?: number;
  documents?: Document[];
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
        <span>{t("component.workspace_chat.loading")}</span>
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
          const timing = stat.current_value_updated_at
            ? `${t("page.workspace_stats.updated")} ${relativeTime(stat.current_value_updated_at)}`
            : t("page.workspace_stats.waiting_for_value");
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
        <span>{t("component.workspace_chat.loading")}</span>
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
  const folderSignature = sortedGroups.map((group) => group.id).join(":");

  useEffect(() => {
    setExpandedFolderIds((current) => {
      if (current.size > 0 || sortedGroups.length === 0) return current;
      const firstFolder = sortedGroups.find((group) => group.is_workspace_file_bucket)
        || sortedGroups[0];
      return new Set([firstFolder.id]);
    });
  }, [folderSignature]); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return (
      <div className="workspace-chat-stats-quick-state" aria-live="polite">
        <LoadingSpinner size={18} />
        <span>{t("component.workspace_chat.loading")}</span>
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
        const documents = group.documents || [];
        const documentCount = group.document_count ?? documents.length;
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
                {documents.length > 0 ? documents.map((document) => (
                  <Link
                    className="workspace-chat-quick-file-row"
                    to={`/viewer/${document.id}`}
                    state={{ returnTo, chatReturnTo: returnTo }}
                    role="listitem"
                    key={document.id}
                    onClick={() => preserveReturnToInHistory(returnTo)}
                  >
                    <IconDocument size={15} />
                    <span>
                      <strong title={document.name}>{document.name}</strong>
                      <small>{document.file_type || t("page.workspace_detail.file")}</small>
                    </span>
                    <IconChevronRight size={13} />
                  </Link>
                )) : (
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
        <span>{t("component.workspace_chat.loading")}</span>
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

export default function WorkspaceStatsQuickAccess({
  workspaceId,
  workspaceName,
}: WorkspaceStatsQuickAccessProps) {
  const queryClient = useQueryClient();
  const location = useLocation();
  const currentReturnTo = useMemo(() => {
    const returnSearch = new URLSearchParams(location.search);
    if (location.pathname === "/chat") returnSearch.set("workspace", workspaceId);
    const serializedSearch = returnSearch.toString();
    return `${location.pathname}${serializedSearch ? `?${serializedSearch}` : ""}${location.hash}`;
  }, [location.hash, location.pathname, location.search, workspaceId]);
  const [activeTab, setActiveTab] = useState<WorkspaceQuickTab>("metrics");
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [preference, setPreference] = useState<QuickViewPreference | null>(null);
  const statsQuery = useQuery({
    queryKey: ["workspace-stats", workspaceId],
    queryFn: () => api.workspaces.stats.list(workspaceId),
    enabled: Boolean(workspaceId),
    staleTime: 30_000,
  });
  const quickViewQuery = useQuery({
    queryKey: ["workspace-stats-quick-view", workspaceId],
    queryFn: () => api.workspaces.stats.quickView(workspaceId),
    enabled: Boolean(workspaceId),
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
    queryKey: ["workspace-quick-files", workspaceId],
    queryFn: () => api.workspaces.documents(workspaceId),
    enabled: Boolean(workspaceId) && activeTab === "files",
    staleTime: 30_000,
  });
  const taskBriefsQuery = useQuery({
    queryKey: ["workspace-quick-task-briefs", workspaceId],
    queryFn: () => api.tasks.list({ workspace_id: workspaceId, limit: 20 }),
    enabled: Boolean(workspaceId),
    staleTime: 30_000,
  });
  const saveViewMutation = useMutation({
    mutationFn: (next: QuickViewPreference) => (
      api.workspaces.stats.updateQuickView(workspaceId, next)
    ),
  });

  useEffect(() => {
    setActiveTab("metrics");
    setEditing(false);
    setAdding(false);
    setPreference(null);
  }, [workspaceId]);

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
  const tasksNeedingAttention = taskBriefs.filter((task) => taskStatusNeedsAttention(task.status));
  const hasTaskError = tasksNeedingAttention.some((task) => (
    task.status === "blocked" || task.status === "failed"
  ));
  const metricsTabStatus: "warning" | "danger" | undefined = statsQuery.isError
    ? "danger"
    : hasCollectionError
      ? "danger"
      : attention.length > 0
        ? "warning"
        : undefined;
  const tasksTabStatus: "warning" | "danger" | undefined = taskBriefsQuery.isError
    ? "danger"
    : hasTaskError
      ? "danger"
      : tasksNeedingAttention.length > 0
        ? "warning"
        : undefined;
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
  const quickTabs = [
    {
      key: "metrics",
      label: t("page.workspace_stats.title"),
      icon: <IconTrendingUp size={14} />,
      status: metricsTabStatus,
      statusLabel: statsQuery.isError
        ? t("page.workspace_stats.load_failed")
        : attention[0]?.label,
    },
    {
      key: "files",
      label: t("component.workspace_chat.files"),
      icon: <IconFolder size={14} />,
    },
    {
      key: "tasks",
      label: t("component.workspace_chat.tasks"),
      icon: <IconList size={14} />,
      status: tasksTabStatus,
      statusLabel: taskBriefsQuery.isError
        ? t("component.workspace_chat.task_briefs_load_failed")
        : tasksNeedingAttention.length > 0
          ? t("page.tasks.needs_attention")
          : undefined,
    },
  ];

  return (
    <AnchoredPopover
      align="right"
      width={360}
      persistentOnWide
      persistentBreakpoint={1280}
      persistentPlacement="below-anchor"
      persistentInlineInset={24}
      ariaLabel={ariaLabel}
      panelClassName="workspace-chat-stats-popover"
      trigger={(
        <button
          type="button"
          className="workspace-chat-stats-trigger"
          aria-label={ariaLabel}
          title={t("component.workspace_chat.workspace_quick_access")}
          data-testid="workspace-chat-stats-trigger"
        >
          <IconViewOptions size={18} />
        </button>
      )}
    >
      {() => (
        <div className="workspace-chat-stats-popover-content">
          <TabSwitcher
            tabs={quickTabs}
            value={activeTab}
            size="sm"
            className="workspace-chat-quick-tabs"
            ariaLabel={t("component.workspace_chat.workspace_quick_access_sections")}
            onChange={(key) => {
              setActiveTab(key as WorkspaceQuickTab);
              setEditing(false);
              setAdding(false);
              addStatMutation.reset();
            }}
          />

          {activeTab === "metrics" && (
            <div className="workspace-chat-quick-section" role="tabpanel">
              <div className="workspace-chat-stats-popover-header">
                <strong>
                  {adding
                    ? t("page.workspace_stats.add")
                    : editing
                      ? t("component.workspace_chat.customize_stats")
                      : t("page.workspace_stats.title")}
                </strong>
                {editing || adding ? (
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
              </div>
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

          {activeTab === "files" && (
            <div className="workspace-chat-quick-section" role="tabpanel">
              <div className="workspace-chat-quick-section-header">
                <strong>{t("component.workspace_chat.workspace_folders")}</strong>
                <Link
                  to={`/workspaces/${workspaceId}?tab=documents`}
                  state={{ returnTo: currentReturnTo, chatReturnTo: currentReturnTo }}
                  onClick={() => preserveReturnToInHistory(currentReturnTo)}
                >
                  {t("component.workspace_chat.view_all")}
                  <IconChevronRight size={13} />
                </Link>
              </div>
              <WorkspaceFilesQuickBody
                groups={documentGroups}
                returnTo={currentReturnTo}
                loading={filesQuery.isLoading}
                error={filesQuery.error}
                onRetry={() => void filesQuery.refetch()}
              />
            </div>
          )}

          {activeTab === "tasks" && (
            <div className="workspace-chat-quick-section" role="tabpanel">
              <div className="workspace-chat-quick-section-header">
                <strong>{t("component.workspace_chat.tasks")}</strong>
                <Link
                  to={`/tasks?workspaceId=${encodeURIComponent(workspaceId)}`}
                  state={{ returnTo: currentReturnTo, chatReturnTo: currentReturnTo }}
                  onClick={() => preserveReturnToInHistory(currentReturnTo)}
                >
                  {t("component.workspace_chat.view_all")}
                  <IconChevronRight size={13} />
                </Link>
              </div>
              <WorkspaceTasksQuickBody
                tasks={taskBriefs}
                returnTo={currentReturnTo}
                loading={taskBriefsQuery.isLoading}
                error={taskBriefsQuery.error}
                onRetry={() => void taskBriefsQuery.refetch()}
              />
            </div>
          )}
        </div>
      )}
    </AnchoredPopover>
  );
}
