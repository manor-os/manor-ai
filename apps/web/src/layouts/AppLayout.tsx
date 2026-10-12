import {
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
  lazy,
  Suspense,
  type CSSProperties,
  type ReactNode,
} from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../stores/auth";
import { useDisplayPreferences } from "../stores/displayPreferences";
import { usePageViewTracking, useWebSocket, type ChatRoomChangeEvent } from "../lib/websocket";
import { useConfigStore } from "../stores/config";
import { usePreviewFeatureAccess } from "../lib/previewFeatureAccess";
import {
  t,
  getLocale,
  SUPPORTED_LOCALES,
  type Locale,
} from "../lib/i18n";
import { ApiError, api } from "../lib/api";
import { useAfterStartup } from "../lib/useAfterStartup";
import { installAutoHideScrollbars } from "../lib/autoHideScrollbars";
import { scheduleQueryInvalidation } from "../lib/coalescedInvalidation";
import { PEOPLE_HOME_PATH } from "../lib/peopleNavigation";
import { agentListQueryOptions, catalogScope as catalogScopeForToken, useCatalogScope } from "../lib/catalogQueries";
import { getAuthToken } from "../lib/authToken";
import { createTaskUpdateInvalidationBatcher, type TaskUpdateInvalidationBatcher } from "../lib/taskUpdateInvalidation";
import { invalidateCatalogQueries } from "../lib/catalogInvalidation";
import { CATALOG_REVALIDATE_EVENT } from "../components/CatalogCacheBoundary";
import type { Workspace, Agent, Conversation } from "../lib/types";
import type { ChatRoom } from "../lib/chatRoomTypes";
import FloatingChat from "../components/FloatingChat";
import TextSelectionToolbar from "../components/TextSelectionToolbar";
import SupportPanel, {
  useSupportUnreadCount,
} from "../components/SupportPanel";
import UserAvatar from "../components/ui/UserAvatar";
import GroupChatAvatar from "../components/ui/GroupChatAvatar";
import FeatureDiscoveryHint, { FeatureDiscoveryBadge } from "../components/ui/FeatureDiscoveryHint";
import useFeatureDiscovery from "../components/useFeatureDiscovery";
import AgentAvatar from "../components/ui/AgentAvatar";
import WorkspaceIconTile from "../components/ui/WorkspaceIcon";
import HoverMarqueeText from "../components/ui/HoverMarqueeText";
import AgentActivityOrb, {
  inferAgentActivity,
  type AgentActivity,
} from "../components/ui/AgentActivityOrb";
import { IconChevronRight, IconMoreHorizontal } from "../components/icons";
import Button from "../components/ui/Button";
import EmptyState from "../components/ui/EmptyState";
import WorkspaceAttentionBadge from "../components/WorkspaceAttentionBadge";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import Dropdown from "../components/ui/Dropdown";
import Input from "../components/ui/Input";
import { InlineRowsSkeleton } from "../components/ui/Skeleton";
import { PageHeaderBoundary } from "../components/ui/PageHeader";
import { useWorkspaceFilter } from "../stores/workspace";
import { useAgentEditModalStore } from "../stores/agentEditModal";
import OnboardingTour, { isTourSuppressedPath, MAIN_TOUR_STORAGE_KEY } from "../components/OnboardingTour";
import { FEATURE_DISCOVERIES } from "../lib/featureDiscovery";
import { getAgentDescription } from "../lib/localizedContent";
import { parseAppLayoutChatTarget } from "./appLayoutChatQuery";
import { relativeTime } from "../lib/format";
import { isChatRunActive } from "../lib/chatStream";
import { latestMessageByRole } from "../lib/chatHistoryScan";
import { useChatStreamStore } from "../stores/chatStream";
import {
  compactSidebarWorkspaces,
  orderSidebarWorkspaces,
  WORKSPACE_SIDEBAR_LIMIT,
} from "../lib/workspaceChatSidebar";
import { useSidebarReorder } from "../lib/useSidebarReorder";

type AppMode = "workspace" | "chat";
type ChatSidebarSection = "ai" | "workspaces" | "group" | "dm";
type DirectSidebarItem = { id: string; kind: "room"; room: ChatRoom } | { id: string; kind: "agent"; agent: Agent };
const CHAT_SIDEBAR_SECTIONS_KEY = "manor-chat-sidebar-sections";
const DEFAULT_CHAT_SIDEBAR_SECTIONS: Record<ChatSidebarSection, boolean> = {
  ai: true,
  workspaces: false,
  group: true,
  dm: true,
};

function ChatSidebarSectionHeader({
  id,
  label,
  expanded,
  onToggle,
  badge,
  action,
  searching,
  attentionCount = 0,
}: {
  id: string;
  label: string;
  expanded: boolean;
  onToggle: () => void;
  badge?: ReactNode;
  action: ReactNode;
  searching: boolean;
  attentionCount?: number;
}) {
  return (
    <div className="conv-section-label conv-section-label--with-action" data-tour={id}>
      <button
        type="button"
        className="conv-section-toggle"
        aria-expanded={expanded}
        aria-controls={expanded ? id : undefined}
        onClick={onToggle}
        disabled={searching}
      >
        <IconChevronRight size={12} className={expanded ? "conv-section-chevron conv-section-chevron--open" : "conv-section-chevron"} />
        <span>{label}</span>
        {!expanded && attentionCount > 0 && (
          <span className="conv-section-attention">{attentionCount > 99 ? "99+" : attentionCount}</span>
        )}
      </button>
      {badge}
      {action}
    </div>
  );
}

const WorkspaceDraftChat = lazy(() => import("../pages/WorkspaceDraftChat"));
const EmbeddedChat = lazy(() => import("../components/EmbeddedChat"));
const loadWorkspaceChat = () => import("../components/WorkspaceChat");
const WorkspaceChat = lazy(loadWorkspaceChat);
const AgentEditModal = lazy(() => import("../components/AgentEditModal"));
const InterfaceModeDialog = lazy(() => import("../components/UserGuide"));
const ChatRoomNewConversation = lazy(() =>
  import("../components/ChatRooms").then((module) => ({
    default: module.ChatRoomNewConversation,
  })),
);
const ChatRoomConversation = lazy(() =>
  import("../components/ChatRooms").then((module) => ({
    default: module.ChatRoomConversation,
  })),
);
const ChatInviteAcceptance = lazy(() =>
  import("../components/ChatRooms").then((module) => ({
    default: module.ChatInviteAcceptance,
  })),
);

function ChatSurfaceLoading() {
  return (
    <div className="flex flex-1 min-h-0 p-4" aria-busy="true">
      <InlineRowsSkeleton rows={3} dense />
    </div>
  );
}

const EMPTY_WORKSPACES: Workspace[] = [];
const EMPTY_CONVERSATIONS: Conversation[] = [];
const EMPTY_AGENTS: Agent[] = [];

function isPersonalAgentConversation(conversation: Conversation, userId?: string): boolean {
  return Boolean(
    userId
    && conversation.agent_id
    && conversation.user_id === userId
    && !conversation.workspace_id
    && conversation.channel.toLowerCase() === "web",
  );
}

function groupChatSummary(room: ChatRoom): string {
  const members = getLocale() === "zh" ? `${room.member_count} 位成员` : `${room.member_count} ${room.member_count === 1 ? "member" : "members"}`;
  if (room.member_count < 2 || !room.online_user_ids) return members;
  return `${members} · ${room.online_user_ids.length} ${getLocale() === "zh" ? "人在线" : "online"}`;
}

function positiveStat(value: unknown) {
  const count = Number(value || 0);
  return Number.isFinite(count) && count > 0 ? count : 0;
}

function workspaceLegacyActionCount(ws: Pick<Workspace, "stats">) {
  const stats = ws.stats || {};
  const value =
    stats.pending_actions ??
    stats.hitl_tasks ??
    stats.chat_pending_actions ??
    0;
  return positiveStat(value);
}

function workspaceChatActionCount(ws: Pick<Workspace, "stats">) {
  const stats = ws.stats || {};
  if (stats.unseen_actions !== undefined && stats.unseen_actions !== null) return positiveStat(stats.unseen_actions);
  const hasVisibleChatStats = [
    stats.chat_pending_actions,
    stats.proposal_actions,
    stats.failed_actions,
  ].some((value) => value !== undefined && value !== null);

  if (!hasVisibleChatStats) {
    return workspaceLegacyActionCount(ws);
  }

  return (
    positiveStat(stats.chat_pending_actions) +
    positiveStat(stats.proposal_actions) +
    positiveStat(stats.failed_actions)
  );
}

function formatWorkspaceCount(count: number) {
  return t(
    count === 1
      ? "page.app_layout.workspace_count_one"
      : "page.app_layout.workspace_count_other",
  ).replace("{count}", String(count));
}

function formatAgentCount(count: number) {
  return t(
    count === 1
      ? "page.app_layout.agent_count_one"
      : "page.app_layout.agent_count_other",
  ).replace("{count}", String(count));
}

function workspaceStatusLabel(status?: string | null) {
  if (status === "active") return t("component.status.active");
  if (status === "paused") return t("component.status.paused");
  return status || t("page.app_layout.workspace");
}

function agentPreview(agent: Agent, fallbackKey: string) {
  return getAgentDescription(agent) || agent.category || t(fallbackKey);
}

function shouldDefaultToChat(pathname: string) {
  return pathname === "/chat";
}

/* ─── SVG Icon components (12×12 default, inline) ─── */

const IconGrid4 = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
  >
    <rect
      x="3"
      y="3"
      width="7"
      height="7"
      rx="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <rect
      x="14"
      y="3"
      width="7"
      height="7"
      rx="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <rect
      x="3"
      y="14"
      width="7"
      height="7"
      rx="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <rect
      x="14"
      y="14"
      width="7"
      height="7"
      rx="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

const IconChecklist = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M9 11l3 3L22 4" />
    <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
  </svg>
);

const IconSchedule = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="10" />
    <path d="M12 6v6l4 2" />
  </svg>
);

const IconGoal = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="10" />
    <circle cx="12" cy="12" r="6" />
    <circle cx="12" cy="12" r="2" />
  </svg>
);

const IconPlus = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2.2}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M12 5v14M5 12h14" />
  </svg>
);

function WorkspaceAddAction({
  cloud,
  ready,
  className,
  style,
  tourId,
  onCreate,
  onMarketplace,
}: {
  cloud: boolean;
  ready: boolean;
  className: string;
  style?: CSSProperties;
  tourId?: string;
  onCreate: () => void;
  onMarketplace: () => void;
}) {
  const label = t(ready && !cloud ? "page.workspaces.create_workspace" : "page.workspaces.add_workspace");
  const trigger = (
    <button
      type="button"
      disabled={!ready}
      className={className}
      style={style}
      data-tour={tourId}
      aria-label={label}
      title={label}
      onClick={cloud ? undefined : onCreate}
    >
      <IconPlus />
    </button>
  );

  if (!cloud) return trigger;
  return (
    <Dropdown
      align="right"
      constrainTo=".app-shell-sidebar"
      menuStyle={{ border: "none" }}
      trigger={trigger}
      items={[
        { key: "marketplace", label: t("page.workspaces.choose_from_marketplace"), icon: <IconGrid4 /> },
        { key: "create", label: t("page.workspaces.create_your_own"), icon: <IconPlus /> },
      ]}
      onSelect={(key) => {
        if (key === "marketplace") onMarketplace();
        if (key === "create") onCreate();
      }}
    />
  );
}

function SidebarRowMenu({
  name,
  items,
  onSelect,
}: {
  name: string;
  items: { key: string; label: string; danger?: boolean }[];
  onSelect: (key: string) => void;
}) {
  return (
    <Dropdown
      align="right"
      constrainTo=".app-shell-sidebar"
      menuStyle={{ border: "none" }}
      trigger={
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="conv-row-more sidebar-row-more"
          style={{ padding: 0, justifyContent: "center" }}
          title={`${t("action.more")}: ${name}`}
          ariaLabel={`${t("action.more")}: ${name}`}
        >
          <IconMoreHorizontal size={18} />
        </Button>
      }
      items={items}
      onSelect={onSelect}
    />
  );
}

const IconBuilding = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M3 21h18M5 21V7l8-4v18M19 21V11l-6-4" />
    <path d="M9 9v.01M9 12v.01M9 15v.01M9 18v.01" />
  </svg>
);

const IconBrain = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M12 2a7 7 0 017 7c0 2.38-1.19 4.47-3 5.74V17a2 2 0 01-2 2h-4a2 2 0 01-2-2v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 017-7z" />
    <path d="M9 21h6M10 17v4M14 17v4" />
  </svg>
);

const IconLibraryStack = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M6 3h11a2 2 0 012 2v14" />
    <path d="M5 7h11a2 2 0 012 2v12" />
    <path d="M4 11h11a2 2 0 012 2v8H6a2 2 0 01-2-2z" />
    <path d="M8 15h5M8 18h4" />
  </svg>
);

const IconConnection = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71" />
    <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71" />
  </svg>
);

const IconPeople = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" />
  </svg>
);

const IconLayers = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <polygon points="12 2 2 7 12 12 22 7 12 2" />
    <polyline points="2 17 12 22 22 17" />
    <polyline points="2 12 12 17 22 12" />
  </svg>
);

const IconGridSmall = () => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
  >
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
  </svg>
);

const IconChatSmall = () => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
  </svg>
);

const IconGear = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" />
  </svg>
);

const IconSearch = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="11" cy="11" r="8" />
    <line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
);

const IconHelp = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="10" />
    <path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3" />
    <line x1="12" y1="17" x2="12.01" y2="17" />
  </svg>
);

const IconBell = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9" />
    <path d="M13.73 21a2 2 0 01-3.46 0" />
  </svg>
);

const IconMoreDots = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="5" cy="12" r="1" />
    <circle cx="12" cy="12" r="1" />
    <circle cx="19" cy="12" r="1" />
  </svg>
);


const IconAgent = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M12 3v3" />
    <rect x="5" y="6" width="14" height="12" rx="4" />
    <circle cx="9.5" cy="12" r="1" />
    <circle cx="14.5" cy="12" r="1" />
    <path d="M9 16h6" />
    <path d="M4 11H2M22 11h-2" />
  </svg>
);

const IconKey = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
  </svg>
);

const IconWebhook = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M18 16.98h1.5c1.65 0 3-1.34 3-3s-1.35-3-3-3h-.5" />
    <path d="M2 12h20M12 2a10 10 0 100 20 10 10 0 000-20z" />
  </svg>
);

const IconFields = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <path d="M3 9h18M9 21V9" />
  </svg>
);

const IconMemory = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M4 4h16v16H4z" />
    <path d="M9 9h6v6H9z" />
    <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" />
  </svg>
);

/* ─── Section definitions ─── */

interface NavItem {
  path: string;
  label: string;
  i18nKey: string;
  icon: React.ReactNode;
  disabled?: boolean;
  badge?: string;
  tourKey?: string;
}

const workspaceItems: NavItem[] = [
  {
    path: "/dashboard",
    label: "Dashboard",
    i18nKey: "nav.dashboard",
    icon: <IconGrid4 />,
    tourKey: "nav-dashboard",
  },
  {
    path: "/tasks",
    label: "Tasks",
    i18nKey: "nav.tasks",
    icon: <IconChecklist />,
    tourKey: "nav-tasks",
  },
  {
    path: "/jobs",
    label: "Automations",
    i18nKey: "nav.automations",
    icon: <IconSchedule />,
    tourKey: "nav-automations",
  },
];

const peopleNavItem: NavItem = {
  path: PEOPLE_HOME_PATH,
  label: "Team & Clients",
  i18nKey: "nav.people",
  icon: <IconPeople />,
  tourKey: "nav-team",
};

const managementItems: NavItem[] = [
  {
    path: "/workspaces",
    label: "Workspaces",
    i18nKey: "nav.workspaces",
    icon: <IconBuilding />,
    tourKey: "nav-workspaces",
  },
  {
    path: "/knowledge",
    label: "Knowledge",
    i18nKey: "nav.knowledge",
    icon: <IconLibraryStack />,
    tourKey: "nav-knowledge",
  },
  peopleNavItem,
];

const IconWorkflow = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <circle cx="5" cy="12" r="2.5" />
    <circle cx="19" cy="6" r="2.5" />
    <circle cx="19" cy="18" r="2.5" />
    <path d="M7.5 12h4a2 2 0 002-2l.4-1a2 2 0 012-1.4" />
    <path d="M7.5 12h4a2 2 0 012 2l.4 1a2 2 0 002 1.4" />
  </svg>
);

const baseConfigurationItems: NavItem[] = [
  {
    path: "/integrations",
    label: "Integrations",
    i18nKey: "nav.integrations",
    icon: <IconConnection />,
    tourKey: "nav-integrations",
  },
  {
    path: "/agents",
    label: "Agents",
    i18nKey: "nav.agents",
    icon: <IconAgent />,
    tourKey: "nav-agents",
  },
  // Blueprints — temporarily hidden from nav until the feature is ready
  // for users. Pages, routes, and APIs all stay so deep links keep working.
  {
    path: "/skills",
    label: "Skills",
    i18nKey: "nav.skills",
    icon: <IconLayers />,
    tourKey: "nav-skills",
  },
];

const flowsConfigurationItem = (enabled: boolean): NavItem => ({
  path: "/flows",
  label: "Workflows",
  i18nKey: "nav.flows",
  icon: <IconWorkflow />,
  disabled: !enabled,
  badge: enabled ? undefined : "Soon",
});


/* ─── Context Switcher (workspace/operation selector) ─── */
function ContextSwitcher({
  workspaces,
  onSelect,
  value,
}: {
  workspaces: Workspace[];
  onSelect: (id: string) => void;
  value?: string;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string>(value || "all");
  const inputRef = useRef<HTMLInputElement>(null);

  // Sync with external value (URL navigation)
  useEffect(() => {
    if (value !== undefined && value !== selected) setSelected(value);
  }, [value]);

  const allWorkspacesLabel = t("page.app_layout.all_workspaces");
  const filtered = query
    ? workspaces.filter((w) =>
        w.name.toLowerCase().includes(query.toLowerCase()),
      )
    : workspaces;

  const selectedName =
    selected === "all"
      ? allWorkspacesLabel
      : workspaces.find((w) => w.id === selected)?.name || allWorkspacesLabel;

  return (
    <div className="workspace-context-switcher">
      {/* Input */}
      <div className="workspace-context-input-wrap">
        {selected === "all" && !open && (
          <span className="workspace-context-scope-icon" aria-hidden="true">
            <IconGrid4 />
          </span>
        )}
        <input
          ref={inputRef}
          value={open ? query : selectedName}
          placeholder={t("page.app_layout.search_or_select_operation")}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => {
            setOpen(true);
            setQuery("");
          }}
          onBlur={() => setTimeout(() => setOpen(false), 200)}
          className={`workspace-context-input ${selected === "all" && !open ? "workspace-context-input--all" : ""} ${open ? "workspace-context-input--open" : ""}`}
        />
        {/* Chevron */}
        <svg
          className="workspace-context-chevron"
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="#a8a29e"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M19.5 8.25l-7.5 7.5-7.5-7.5"
          />
        </svg>
      </div>

      {/* Dropdown */}
      {open && (
        <div className="workspace-context-menu">
          {/* All workspaces */}
          <div
            onClick={() => {
              setSelected("all");
              setOpen(false);
              setQuery("");
              onSelect("all");
            }}
            className={`workspace-context-option workspace-context-option--all-scope ${selected === "all" ? "workspace-context-option--active" : ""}`}
          >
            <span className="workspace-context-option-icon workspace-context-option-icon--all">
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z"
                />
              </svg>
            </span>
            <div className="workspace-context-option-text">
              <span className="workspace-context-option-title">
                {allWorkspacesLabel}
              </span>
              <span className="workspace-context-option-meta">
                {formatWorkspaceCount(workspaces.length)}
              </span>
            </div>
          </div>

          {/* Individual workspaces */}
          {filtered.map((ws) => (
            <div
              key={ws.id}
              onClick={() => {
                setSelected(ws.id);
                setOpen(false);
                setQuery("");
                onSelect(ws.id);
              }}
              className={`workspace-context-option ${selected === ws.id ? "workspace-context-option--active" : ""}`}
            >
              <span className="workspace-context-option-icon">
                <WorkspaceIconTile
                  workspace={ws}
                  size={22}
                  iconSize={12}
                  style={{ borderRadius: 7 }}
                />
              </span>
              <div className="workspace-context-option-text">
                <span className="workspace-context-option-title">
                  {ws.name}
                </span>
                <span className="workspace-context-option-meta">
                  {workspaceStatusLabel(ws.status)}
                </span>
              </div>
            </div>
          ))}

          {filtered.length === 0 && query && (
            <div className="workspace-context-empty">
              {t("page.app_layout.no_workspaces_found")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Manor Logo SVG ─── */
const ManorLogo = ({
  size = 16,
  color = "white",
  className,
}: {
  size?: number;
  color?: string;
  className?: string;
}) => (
  <svg
    viewBox="0 0 1024 1024"
    width={size}
    height={size}
    fill={color}
    className={className}
    aria-hidden="true"
    focusable="false"
  >
    <path
      className="manor-brand-logo-cap"
      d="M295.152941 0l224.376471 224.376471L743.905882 0H1024v63.247059L519.529412 567.717647 0 49.694118V0h295.152941z"
    />
    <path
      className="manor-brand-logo-leg manor-brand-logo-leg--left"
      d="M0 256l243.952941 243.952941V1024H0V256z"
    />
    <path
      className="manor-brand-logo-leg manor-brand-logo-leg--right"
      d="M1024 271.058824v752.941176H780.047059V515.011765L1024 271.058824z"
    />
  </svg>
);

export default function AppLayout() {
  useEffect(installAutoHideScrollbars, []);
  const location = useLocation();
  const navigate = useNavigate();
  const agentEditModalOpen = useAgentEditModalStore(
    (state) => state.agentId !== undefined,
  );
  const simpleMode = useDisplayPreferences((s) => s.interfaceMode === "simple");
  const hasSelectedInterfaceMode = useDisplayPreferences((s) => s.hasSelectedInterfaceMode);
  const queryClient = useQueryClient();
  const startupSettled = useAfterStartup();
  const catalogScope = useCatalogScope();
  const storeUser = useAuthStore((s) => s.user);
  const token = useAuthStore((s) => s.token);
  // Use react-query for live user data (syncs with Account page updates)
  const { data: queryUser } = useQuery({
    queryKey: ["auth-me"],
    queryFn: async () => {
      const user = await api.auth.me();
      useAuthStore.setState({ user });
      return user;
    },
    enabled: !!token,
    initialData: storeUser ?? undefined,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  });
  const user = queryUser || storeUser;
  const userRoleLabel = user?.role === "owner"
    ? t("page.users.role_owner")
    : user?.role === "admin"
      ? t("page.users.role_admin")
      : user?.role === "member" || !user?.role
        ? t("page.users.role_member")
        : user.role;
  const [toast, setToast] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(() => {
    if (typeof window === "undefined") return false;
    const saved = window.localStorage.getItem("manor-sidebar-collapsed");
    if (saved != null) return saved === "1";
    return window.innerWidth < 1280;
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const [mode, setMode] = useState<AppMode>(() =>
    shouldDefaultToChat(location.pathname) ? "chat" : "workspace",
  );
  const sidebarMode = simpleMode ? "chat" : mode;
  const [searchOpen, setSearchOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [configureOpen, setConfigureOpen] = useState(false);
  const [tourRequested, setTourRequested] = useState(false);
  const interfaceModeDialogOpen = Boolean(user && !hasSelectedInterfaceMode && !isTourSuppressedPath(location.pathname));

  useEffect(() => {
    if (!tourRequested || !user || interfaceModeDialogOpen || !hasSelectedInterfaceMode || isTourSuppressedPath(location.pathname)) return;
    window.dispatchEvent(new Event("manor:start-tour"));
    setTourRequested(false);
  }, [tourRequested, user, interfaceModeDialogOpen, hasSelectedInterfaceMode, simpleMode, location.pathname]);
  const [supportOpen, setSupportOpen] = useState(false);
  const supportRestoreFocusRef = useRef<HTMLButtonElement>(null);
  const [searchQuery, setSearchQuery] = useState("");

  /* ── Chat Mode state ── */
  const initialWorkspaceId = location.pathname === "/chat"
    ? new URLSearchParams(location.search).get("workspace") : null;
  const [activeConvId, setActiveConvId] = useState<string>(initialWorkspaceId || "manor-ai");
  const [activeConvType, setActiveConvType] = useState<
    "manor" | "operation" | "dm" | "room" | "compose" | "workspace-draft"
  >(initialWorkspaceId ? "operation" : "manor");
  const { data: humanChatRooms = [], isSuccess: humanChatRoomsLoaded } = useQuery({
    queryKey: ["chat-rooms", user?.id],
    queryFn: api.chatRooms.list,
    enabled: sidebarMode === "chat" && !!user,
    // WebSocket events provide freshness; this is a safety resync.
    refetchInterval: 60_000,
  });
  const { data: groupUsage, isSuccess: groupUsageLoaded } = useQuery({
    queryKey: ["group-chat-usage", user?.id],
    queryFn: api.chatRooms.groupUsage,
    enabled: sidebarMode === "chat" && !!user,
    staleTime: 5 * 60_000,
  });
  const [unavailableWorkspaceId, setUnavailableWorkspaceId] = useState<string | null>(null);
  const [activeDmAgentId, setActiveDmAgentId] = useState<string | null>(null);
  const [chatWorkspaces, setChatWorkspaces] = useState<Workspace[]>([]);
  const activeWorkspaceId = useWorkspaceFilter((s) => s.activeWorkspaceId);
  const setActiveWorkspaceId = useWorkspaceFilter(
    (s) => s.setActiveWorkspaceId,
  );
  const { data: chatAgentsList = EMPTY_AGENTS, isLoading: chatAgentsLoading } = useQuery({
    ...agentListQueryOptions(catalogScope),
    enabled: !!catalogScope && mode === "chat",
  });
  const [hiredAgents, setHiredAgents] = useState<Agent[]>([]);
  const [dmConversationByAgentId, setDmConversationByAgentId] = useState<
    Record<string, string>
  >({});
  const [convSearchQuery, setConvSearchQuery] = useState("");
  const workspaceOrderKey = user ? `manor-workspace-sidebar:${user.entity_id}:${user.id}` : null;
  const [allSidebarWorkspaces, setAllSidebarWorkspaces] = useState(false);
  useEffect(() => {
    setAllSidebarWorkspaces(false);
  }, [workspaceOrderKey]);
  const [chatSidebarSections, setChatSidebarSections] = useState(() => {
    if (typeof window === "undefined") return DEFAULT_CHAT_SIDEBAR_SECTIONS;
    try {
      const saved = JSON.parse(window.localStorage.getItem(CHAT_SIDEBAR_SECTIONS_KEY) || "null");
      if (!saved || typeof saved !== "object") return DEFAULT_CHAT_SIDEBAR_SECTIONS;
      return Object.fromEntries(
        (Object.keys(DEFAULT_CHAT_SIDEBAR_SECTIONS) as ChatSidebarSection[]).map((section) => [
          section,
          typeof saved[section] === "boolean" ? saved[section] : DEFAULT_CHAT_SIDEBAR_SECTIONS[section],
        ]),
      ) as Record<ChatSidebarSection, boolean>;
    } catch {
      return DEFAULT_CHAT_SIDEBAR_SECTIONS;
    }
  });
  const toggleChatSidebarSection = (section: ChatSidebarSection) => {
    setChatSidebarSections((current) => ({ ...current, [section]: !current[section] }));
  };
  const [manorSessionsOpen, setManorSessionsOpen] = useState(false);
  const [renamingConversationId, setRenamingConversationId] = useState<
    string | null
  >(null);
  const [renamingAgentDraftId, setRenamingAgentDraftId] = useState<string | null>(null);
  const [conversationTitleDraft, setConversationTitleDraft] = useState("");
  const [conversationRenameError, setConversationRenameError] = useState("");
  const [deleteConversationTarget, setDeleteConversationTarget] =
    useState<Conversation | null>(null);
  const [deleteConversationError, setDeleteConversationError] = useState("");
  const [roomSidebarAction, setRoomSidebarAction] = useState<{
    roomId: string;
    id: number;
    action: "details" | "rename" | "close";
  } | null>(null);
  const [renamingWorkspace, setRenamingWorkspace] = useState<Workspace | null>(null);
  const [workspaceNameDraft, setWorkspaceNameDraft] = useState("");
  const [workspaceRenameError, setWorkspaceRenameError] = useState("");
  const [deleteWorkspaceTarget, setDeleteWorkspaceTarget] = useState<Workspace | null>(null);
  const [deleteWorkspaceError, setDeleteWorkspaceError] = useState("");
  const runningManorSessionActivitiesKey = useChatStreamStore((state) =>
    JSON.stringify(
      Object.values(state.sessions)
        .flatMap((session) => {
          if (!session.streaming || !session.convId) return [];
          const latestAssistantMessage = latestMessageByRole(session.messages, "assistant");
          return [
            [
              session.convId,
              inferAgentActivity(latestAssistantMessage),
            ] satisfies [string, AgentActivity],
          ];
        })
        .sort(([leftId], [rightId]) => leftId.localeCompare(rightId)),
    ),
  );
  const runningManorSessionActivities = useMemo(
    () =>
      new Map<string, AgentActivity>(
        JSON.parse(runningManorSessionActivitiesKey) as Array<
          [string, AgentActivity]
        >,
      ),
    [runningManorSessionActivitiesKey],
  );
  const activeManorSessionActivity = useChatStreamStore((state) => {
    if (!isChatRunActive(state.messages, state.streaming)) return null;
    const latestAssistantMessage = latestMessageByRole(state.messages, "assistant");
    return inferAgentActivity(latestAssistantMessage);
  });
  // Counts shown in chat rows: unresolved chat actions (proposal cards + HITL).
  const [actionCounts, setActionCounts] = useState<Record<string, number>>({});

  /* Load app config (deployment mode, feature flags) */
  useEffect(() => {
    useConfigStore.getState().load();
  }, []);
  const deploymentMode = useConfigStore((s) => s.deployment_mode);
  const configLoaded = useConfigStore((s) => s.loaded);
  const configLoadError = useConfigStore((s) => s.load_error);
  const workspaceAddReady = configLoaded || configLoadError;
  const { enabled: flowsAvailable } = usePreviewFeatureAccess("flows");
  const supportTicketsEnabled = useConfigStore(
    (s) => s.support_tickets_enabled,
  );
  const supportUnread =
    useSupportUnreadCount(supportTicketsEnabled && startupSettled).data?.count || 0;
  const configurationItems = useMemo(() => {
    const items = [...baseConfigurationItems];
    // Only unavailable accounts see the disabled "Soon" state for Workflows.
    items.push(flowsConfigurationItem(flowsAvailable));
    return items;
  }, [flowsAvailable]);
  const {
    data: workspaceList = EMPTY_WORKSPACES,
    isSuccess: workspaceListReady,
    dataUpdatedAt: workspaceListUpdatedAt,
  } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
    staleTime: 60_000,
  });
  // Deep links load the selected Workspace independently of the sidebar's
  // full list and aggregated stats. Sidebar clicks reuse that list's cache.
  const activeChatWorkspaceQuery = useQuery({
    queryKey: ["workspace", activeConvId],
    queryFn: ({ signal }) => api.workspaces.get(activeConvId, signal),
    enabled: mode === "chat" && activeConvType === "operation",
    initialData: () => queryClient.getQueryData<Workspace[]>(["workspaces"])
      ?.find((workspace) => workspace.id === activeConvId),
    initialDataUpdatedAt: () => queryClient.getQueryState(["workspaces"])?.dataUpdatedAt,
    staleTime: 60_000,
  });
  const activeChatWorkspaceUnavailable = (
    activeChatWorkspaceQuery.error instanceof ApiError
    && (activeChatWorkspaceQuery.error.status === 404 || activeChatWorkspaceQuery.error.status === 403)
  ) || (
    workspaceListReady && !activeChatWorkspaceQuery.isPending
    && workspaceListUpdatedAt > activeChatWorkspaceQuery.dataUpdatedAt
    && !workspaceList.some((workspace) => workspace.id === activeConvId)
  );
  useEffect(() => {
    if (mode === "chat" && activeConvType === "operation") void loadWorkspaceChat().catch(() => {});
  }, [mode, activeConvType]);
  const {
    data: chatConversations = EMPTY_CONVERSATIONS,
    isLoading: chatConversationsLoading,
    isError: chatConversationsError,
  } = useQuery({
    queryKey: ["conversations"],
    queryFn: () => api.chat.listConversations(),
    enabled: sidebarMode === "chat",
    staleTime: 30_000,
  });
  const manorChatSessions = useMemo(
    () =>
      chatConversations.filter(
        (conversation) =>
          !conversation.agent_id && !conversation.workspace_id,
      ),
    [chatConversations],
  );
  const dmConversationRecordByAgentId = useMemo(() => {
    const byAgent = new Map<string, Conversation>();
    for (const conversation of chatConversations) {
      if (isPersonalAgentConversation(conversation, user?.id) && conversation.agent_id && !byAgent.has(conversation.agent_id)) {
        byAgent.set(conversation.agent_id, conversation);
      }
    }
    return byAgent;
  }, [chatConversations, user?.id]);
  const conversationSearch = convSearchQuery.trim().toLocaleLowerCase();
  const isSearchingConversations = conversationSearch.length > 0;
  const sidebarOrderKey = (section: "ai" | "group" | "dm") => user ? `manor-chat-sidebar-${section}:${user.entity_id}:${user.id}` : null;
  const aiSidebar = useSidebarReorder(sidebarOrderKey("ai"), manorChatSessions, isSearchingConversations);
  const isChatSidebarSectionOpen = (section: ChatSidebarSection) =>
    isSearchingConversations || chatSidebarSections[section];
  const groupDiscovery = useFeatureDiscovery({
    feature: FEATURE_DISCOVERIES.groupChat,
    userId: user?.id,
    enabled: sidebarMode === "chat" && humanChatRoomsLoaded && groupUsageLoaded,
    active: sidebarMode === "chat" && location.pathname.startsWith("/chat") && !collapsed && !isSearchingConversations &&
      (window.innerWidth >= 1024 || mobileOpen),
    alreadyUsed: groupUsage?.used === true || humanChatRooms.some((room) => room.kind === "group"),
    autoShow: sidebarMode === "chat" && location.pathname.startsWith("/chat") &&
      humanChatRoomsLoaded && groupUsageLoaded && !collapsed &&
      chatSidebarSections.group && !isSearchingConversations && hasSelectedInterfaceMode && !interfaceModeDialogOpen &&
      Boolean(window.localStorage.getItem(MAIN_TOUR_STORAGE_KEY)) &&
      (window.innerWidth >= 1024 || mobileOpen),
  });
  const filteredManorChatSessions = useMemo(() => {
    if (!conversationSearch) return aiSidebar.items;
    return aiSidebar.items.filter((conversation) =>
      (conversation.title || t("page.chat_history.untitled"))
        .toLocaleLowerCase()
        .includes(conversationSearch),
    );
  }, [conversationSearch, aiSidebar.items]);
  const manorIdentityMatchesSearch = !conversationSearch || [
    "manor ai",
    t("page.app_layout.your_ai_chief_of_staff").toLocaleLowerCase(),
  ].some((label) => label.includes(conversationSearch));
  const manorSearchHasResults =
    manorIdentityMatchesSearch || filteredManorChatSessions.length > 0;

  useEffect(() => {
    if (conversationSearch) setManorSessionsOpen(true);
  }, [conversationSearch]);
  const renameChatConversation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.chat.renameConversation(id, title),
    onSuccess: () => {
      setRenamingConversationId(null);
      setConversationTitleDraft("");
      setConversationRenameError("");
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    onError: () => {
      setConversationRenameError(
        t("page.app_layout.rename_manor_session_failed"),
      );
    },
  });
  const startAgentConversation = useMutation({
    mutationFn: ({ agentId, title }: { agentId: string; title: string }) =>
      api.chat.startAgentConversation(agentId, title),
    onSuccess: (conversation) => {
      setRenamingAgentDraftId(null);
      setConversationTitleDraft("");
      setConversationRenameError("");
      queryClient.setQueryData<Conversation[]>(["conversations"], (previous = []) => [
        conversation,
        ...previous.filter((item) => item.id !== conversation.id),
      ]);
      if (conversation.agent_id) {
        setDmConversationByAgentId((previous) => ({ ...previous, [conversation.agent_id!]: conversation.id }));
        if (activeConvType === "dm" && activeDmAgentId === conversation.agent_id) {
          setActiveConvId(conversation.id);
        }
      }
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    onError: () => setConversationRenameError(t("page.app_layout.rename_manor_session_failed")),
  });
  const deleteChatConversation = useMutation({
    mutationFn: (id: string) => api.chat.deleteConversation(id),
    onSuccess: (_data, deletedId) => {
      setDeleteConversationTarget(null);
      setDeleteConversationError("");
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      const agentId = deleteConversationTarget?.id === deletedId ? deleteConversationTarget.agent_id : null;
      const nextConversation = agentId ? chatConversations.find((conversation) =>
        conversation.id !== deletedId && conversation.agent_id === agentId && isPersonalAgentConversation(conversation, user?.id),
      ) : undefined;
      if (agentId) {
        setDmConversationByAgentId((previous) => {
          const next = { ...previous };
          if (nextConversation) next[agentId] = nextConversation.id;
          else delete next[agentId];
          return next;
        });
        if (!nextConversation) setHiredAgents((previous) => previous.filter((agent) => agent.id !== agentId));
      }
      if (activeConvType === "manor" && activeConvId === deletedId) {
        const draftId = `manor-new:${Date.now()}`;
        setActiveConvId(draftId);
        setActiveDmAgentId(null);
        navigate(`/chat?new=chat&draft=${encodeURIComponent(draftId)}`, { replace: true });
      } else if (activeConvType === "dm" && activeConvId === deletedId) {
        if (nextConversation) {
          setActiveConvId(nextConversation.id);
        } else {
          setActiveConvId("manor-ai");
          setActiveConvType("manor");
          setActiveDmAgentId(null);
          navigate("/chat", { replace: true });
        }
      }
    },
    onError: () => {
      setDeleteConversationError(
        t("page.app_layout.delete_manor_session_failed"),
      );
    },
  });
  const renameWorkspaceFromSidebar = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      api.workspaces.update(id, { name }),
    onSuccess: (_data, { id }) => {
      setRenamingWorkspace(null);
      setWorkspaceRenameError("");
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      void queryClient.invalidateQueries({ queryKey: ["workspace", id] });
    },
    onError: (error: Error) => setWorkspaceRenameError(error.message),
  });
  const deleteWorkspaceFromSidebar = useMutation({
    mutationFn: (id: string) => api.workspaces.delete(id),
    onSuccess: (_data, deletedId) => {
      setDeleteWorkspaceTarget(null);
      setDeleteWorkspaceError("");
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      invalidateCatalogQueries(queryClient, "agents", { queryKey: ["agents", "list"] });
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["agent-deployments"] });
      if (activeWorkspaceId === deletedId) setActiveWorkspaceId("all");
      if (location.pathname === `/workspaces/${deletedId}`) {
        navigate("/workspaces", { replace: true });
      } else if (location.pathname === "/chat" && activeConvType === "operation" && activeConvId === deletedId) {
        setActiveConvType("manor");
        setActiveConvId("manor-ai");
        navigate("/chat", { replace: true });
      }
      setToast(t("page.workspace_detail.workspace_deleted"));
    },
    onError: (error: Error) => setDeleteWorkspaceError(error.message),
  });

  /* Load workspaces for sidebar context switcher (both modes) */
  // Coalesced: install, chat-mode entry, task updates and every realtime
  // workspace chat event all ask for this list within a few seconds.
  const refreshWorkspaces = useCallback(() => {
    scheduleQueryInvalidation(queryClient, ["workspaces"]);
  }, [queryClient]);
  useEffect(() => {
    setChatWorkspaces(workspaceList);
    // Seed counts from personal unread reminder state. Chat rows include proposal
    // cards because these actions are handled in workspace chat.
    const chatCounts: Record<string, number> = {};
    const workspaceIds = new Set<string>();
    for (const ws of workspaceList) {
      workspaceIds.add(ws.id);
      const chatCount = workspaceChatActionCount(ws);
      if (chatCount > 0) chatCounts[ws.id] = chatCount;
    }
    setActionCounts((prev) => {
      const next = { ...prev };
      workspaceIds.forEach((id) => {
        delete next[id];
      });
      return { ...next, ...chatCounts };
    });
  }, [workspaceList]);
  useEffect(() => {
    if (
      mode !== "chat"
      || activeConvType !== "operation"
    ) {
      return;
    }
    if (!activeChatWorkspaceUnavailable) {
      setUnavailableWorkspaceId((current) =>
        current === activeConvId ? null : current,
      );
      return;
    }
    setUnavailableWorkspaceId(activeConvId);
    if (location.pathname === "/chat" && location.search) {
      navigate("/chat", { replace: true });
    }
  }, [
    activeConvId,
    activeConvType,
    location.pathname,
    location.search,
    mode,
    navigate,
    activeChatWorkspaceUnavailable,
  ]);
  useEffect(() => {
    const onRefresh = () => refreshWorkspaces();
    window.addEventListener("manor:workspace-actions-refresh", onRefresh);
    return () =>
      window.removeEventListener("manor:workspace-actions-refresh", onRefresh);
  }, [refreshWorkspaces]);
  // Also refresh when entering chat mode (picks up newly created workspaces)
  useEffect(() => {
    if (mode === "chat") refreshWorkspaces();
  }, [mode, refreshWorkspaces]);

  // DM agents are the agents that already own at least one personal
  // conversation. The same canonical conversation query also powers the
  // expandable Manor AI session list below.
  useEffect(() => {
    if (mode !== "chat") return;
    const dmAgentIds = new Set<string>();
    const dmConversations: Record<string, string> = {};
    chatConversations
      .filter((conversation) => isPersonalAgentConversation(conversation, user?.id))
      .forEach((conversation) => {
        const agentId = conversation.agent_id;
        if (!agentId) return;
        dmAgentIds.add(agentId);
        if (!dmConversations[agentId]) {
          dmConversations[agentId] = conversation.id;
        }
      });
    setDmConversationByAgentId(dmConversations);
    if (dmAgentIds.size === 0 || chatAgentsList.length === 0) return;
    const persistedDmAgents = chatAgentsList.filter(
      (agent) => agent?.id && agent?.name && dmAgentIds.has(agent.id),
    );
    setHiredAgents((prev) => {
      const merged = new Map<string, Agent>();
      prev
        .filter((agent) => agent?.id && agent?.name)
        .forEach((agent) => merged.set(agent.id, agent));
      persistedDmAgents.forEach((agent) => merged.set(agent.id, agent));
      return Array.from(merged.values());
    });
  }, [mode, chatConversations, chatAgentsList, user?.id]);

  // During development/HMR or from older state, activeConvId may still be an
  // agent id. Normalize it to the new DM shape so EmbeddedChat never requests
  // /chat/conversations/{agentId}/messages.
  useEffect(() => {
    if (activeConvType !== "dm" || activeDmAgentId || !activeConvId) return;
    const agent = [...chatAgentsList, ...hiredAgents].find(
      (a) => a.id === activeConvId,
    );
    if (!agent) return;
    setActiveDmAgentId(agent.id);
    setActiveConvId(dmConversationByAgentId[agent.id] || `agent:${agent.id}`);
  }, [
    activeConvType,
    activeDmAgentId,
    activeConvId,
    chatAgentsList,
    hiredAgents,
    dmConversationByAgentId,
  ]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          agentId?: string;
          conversationId?: string | null;
        }>
      ).detail;
      if (!detail?.agentId) return;
      const agentId = detail.agentId;
      setDmConversationByAgentId((prev) => {
        const next = { ...prev };
        if (detail.conversationId) next[agentId] = detail.conversationId;
        else delete next[agentId];
        return next;
      });
      if (activeConvType === "dm" && activeDmAgentId === agentId) {
        setActiveConvId(detail.conversationId || `agent:${agentId}`);
      }
    };
    window.addEventListener("manor:dm-conversation-resolved", handler);
    return () =>
      window.removeEventListener("manor:dm-conversation-resolved", handler);
  }, [activeConvType, activeDmAgentId]);

  /* Derive chat title/subtitle from active conversation */
  const getChatInfo = useCallback(() => {
    if (activeConvType === "manor") {
      return {
        title: "Manor AI",
        subtitle: t("page.app_layout.your_ai_chief_of_staff"),
        agents: [] as Agent[],
      };
    }
    if (activeConvType === "operation") {
      const ws = chatWorkspaces.find((w) => w.id === activeConvId);
      return {
        title: ws?.name || t("page.app_layout.operation"),
        subtitle: formatAgentCount(chatAgentsList.length),
        agents: chatAgentsList.slice(0, 10),
      };
    }
    // dm
    const agent = [...chatAgentsList, ...hiredAgents].find(
      (a) => a.id === activeDmAgentId,
    );
    return {
      title: (activeDmAgentId ? dmConversationRecordByAgentId.get(activeDmAgentId)?.title : null) || agent?.name || t("page.app_layout.agent"),
      subtitle: agent
        ? agentPreview(agent, "page.app_layout.direct_message")
        : t("page.app_layout.direct_message"),
      agents: [] as Agent[],
    };
  }, [
    activeConvType,
    activeConvId,
    activeDmAgentId,
    chatWorkspaces,
    chatAgentsList,
    hiredAgents,
    dmConversationRecordByAgentId,
  ]);

  /* Chat sidebar filtering:
   * Workspaces are stable operating rooms, so DM/agent search should not hide
   * them. The workspace-mode context filter is intentionally ignored here too;
   * otherwise chat mode can look empty because of a hidden selection elsewhere.
   */
  const matchesConversationSearch = (agent: {
    name?: string;
    category?: string;
    description?: string;
  }) => {
    const name = String(agent?.name || "").toLowerCase();
    const category = String(agent?.category || "").toLowerCase();
    if (!conversationSearch) return true;
    if (conversationSearch.length === 1)
      return name.startsWith(conversationSearch);
    return (
      name.includes(conversationSearch) || category.includes(conversationSearch)
    );
  };
  const openManorChat = () => {
    setActiveConvId("manor-ai");
    setActiveConvType("manor");
    setActiveDmAgentId(null);
    setConvSearchQuery("");
    setMobileOpen(false);
    if (location.pathname !== "/chat" || location.search) {
      navigate("/chat");
    }
  };
  const openManorSession = (conversationId: string) => {
    setActiveConvId(conversationId);
    setActiveConvType("manor");
    setActiveDmAgentId(null);
    setConvSearchQuery("");
    setMobileOpen(false);
    navigate(`/chat?conversation=${encodeURIComponent(conversationId)}`);
  };
  const startNewManorSession = () => {
    const draftId = `manor-new:${Date.now()}`;
    setActiveConvId(draftId);
    setActiveConvType("manor");
    setActiveDmAgentId(null);
    setConvSearchQuery("");
    setManorSessionsOpen(true);
    setMobileOpen(false);
    navigate(`/chat?new=chat&draft=${encodeURIComponent(draftId)}`);
  };
  const handleManorConversationResolved = (conversationId: string) => {
    setActiveConvId(conversationId);
    void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    navigate(`/chat?conversation=${encodeURIComponent(conversationId)}`, {
      replace: true,
    });
  };
  const beginRenameConversation = (conversation: Conversation) => {
    setRenamingConversationId(conversation.id);
    setRenamingAgentDraftId(null);
    setConversationTitleDraft(
      conversation.title || t("page.chat_history.untitled"),
    );
    setConversationRenameError("");
  };
  const beginRenameAgentDraft = (agent: Agent) => {
    setRenamingConversationId(null);
    setRenamingAgentDraftId(agent.id);
    setConversationTitleDraft(agent.name);
    setConversationRenameError("");
  };
  const cancelRenameConversation = () => {
    setRenamingConversationId(null);
    setRenamingAgentDraftId(null);
    setConversationTitleDraft("");
    setConversationRenameError("");
  };
  const submitRenameConversation = () => {
    const title = conversationTitleDraft.trim();
    if (!title) return;
    if (renamingAgentDraftId) {
      startAgentConversation.mutate({ agentId: renamingAgentDraftId, title });
      return;
    }
    if (!renamingConversationId) return;
    renameChatConversation.mutate({ id: renamingConversationId, title });
  };
  const openAgentDm = (agent: Agent, replace = false) => {
    const convId = dmConversationByAgentId[agent.id] || `agent:${agent.id}`;
    setHiredAgents((prev) => {
      if (prev.some((item) => item.id === agent.id)) return prev;
      return [...prev, agent];
    });
    setActiveConvId(convId);
    setActiveDmAgentId(agent.id);
    setActiveConvType("dm");
    setConvSearchQuery("");
    setMobileOpen(false);
    navigate(`/chat?agent=${encodeURIComponent(agent.id)}`, { replace });
  };
  const openAgentDmFromCompose = (agent: Agent) => {
    openAgentDm(agent, true);
  };
  const openWorkspaceChat = (workspaceId: string) => {
    setActiveConvId(workspaceId);
    setActiveConvType("operation");
    setActiveDmAgentId(null);
    setConvSearchQuery("");
    setMobileOpen(false);
    navigate(`/chat?workspace=${encodeURIComponent(workspaceId)}`);
  };
  const openHumanRoom = (roomId: string) => {
    setActiveConvId(roomId);
    setActiveConvType("room");
    setActiveDmAgentId(null);
    setConvSearchQuery("");
    setMobileOpen(false);
    navigate(`/chat?room=${encodeURIComponent(roomId)}`);
  };
  const openHumanRoomAction = (
    roomId: string,
    action: "details" | "rename" | "close",
  ) => {
    setRoomSidebarAction((current) => ({
      roomId,
      id: (current?.id || 0) + 1,
      action,
    }));
    openHumanRoom(roomId);
  };
  const openNewConversation = () => {
    setActiveConvType("compose");
    setActiveConvId("new-conversation");
    setMobileOpen(false);
    navigate("/chat?new=message");
  };
  const openNewGroup = () => {
    groupDiscovery.dismiss();
    setActiveConvType("compose");
    setActiveConvId("new-conversation");
    setMobileOpen(false);
    navigate("/chat?new=message&kind=group");
  };
  const openNewWorkspaceDraft = () => {
    setActiveConvType("workspace-draft");
    setActiveConvId("new-workspace");
    setMobileOpen(false);
    navigate("/chat?new=workspace");
  };
  const openWorkspaceMarketplace = () => {
    setMobileOpen(false);
    navigate("/workspaces?view=marketplace");
  };
  const openStandaloneWorkspaceDraft = () => {
    setMobileOpen(false);
    navigate("/workspaces/new");
  };
  const recentWorkspaces = useMemo(() => orderSidebarWorkspaces(chatWorkspaces, { mode: "recent", order: [] }), [chatWorkspaces]);
  const workspaceSidebar = useSidebarReorder(workspaceOrderKey, recentWorkspaces, isSearchingConversations);
  const orderedWorkspaces = workspaceSidebar.items;
  const matchingWorkspaces = isSearchingConversations
    ? orderedWorkspaces.filter((ws) => {
        const name = String(ws?.name || "").toLowerCase();
        if (conversationSearch.length === 1) return name.startsWith(conversationSearch);
        return name.includes(conversationSearch);
      })
    : orderedWorkspaces;
  const filteredWorkspaces = isSearchingConversations || allSidebarWorkspaces
    ? matchingWorkspaces
    : compactSidebarWorkspaces(matchingWorkspaces, activeConvType === "operation" ? activeConvId : null);
  const filteredDmAgents = hiredAgents.filter((agent) =>
    agent && agent.name && (
      matchesConversationSearch(agent)
      || dmConversationRecordByAgentId.get(agent.id)?.title?.toLocaleLowerCase().includes(conversationSearch)
    ),
  );
  const filteredSearchAgents = isSearchingConversations
    ? chatAgentsList
        .filter((agent) => {
          if (!agent?.id || !agent?.name) return false;
          if (hiredAgents.some((hired) => hired.id === agent.id)) return false;
          return matchesConversationSearch(agent);
        })
        .slice(0, 10)
    : [];
  const visibleSearchAgents = simpleMode ? [] : filteredSearchAgents;
  const filteredHumanChatRooms = isSearchingConversations
    ? humanChatRooms.filter((room) => room.title.toLocaleLowerCase().includes(conversationSearch))
    : humanChatRooms;
  const groupSidebar = useSidebarReorder(sidebarOrderKey("group"), humanChatRooms.filter((room) => room.kind === "group"), isSearchingConversations);
  const dmSidebar = useSidebarReorder<DirectSidebarItem>(sidebarOrderKey("dm"), [
    ...humanChatRooms.filter((room) => room.kind === "dm").map((room) => ({ id: `room:${room.id}`, kind: "room" as const, room })),
    ...hiredAgents.map((agent) => ({ id: `agent:${agent.id}`, kind: "agent" as const, agent })),
  ], isSearchingConversations);
  const collapsedDmItems = useMemo(() => {
    const count = { room: 0, agent: 0 };
    return dmSidebar.items.filter((item) => ++count[item.kind] <= 8);
  }, [dmSidebar.items]);

  const videoRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  const onNotification = useCallback(
    (data: Record<string, unknown>) => {
      const title = (data.title as string) || "New notification";
      setToast(title);
      setTimeout(() => setToast(null), 4000);
      // Refresh notification list + unread count (coalesced across a burst)
      scheduleQueryInvalidation(queryClient, ["notifications"]);
      // If it's a proposal, also refresh workspace data + tasks
      if (data.type === "proposal") {
        queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
        queryClient.invalidateQueries({ queryKey: ["tasks"] });
        queryClient.invalidateQueries({ queryKey: ["dashboard-stats"] });
      }
      if (data.type === "booking_confirmed") {
        queryClient.invalidateQueries({ queryKey: ["calendar-settings"] });
        queryClient.invalidateQueries({ queryKey: ["calendar-agenda"] });
      }
    },
    [queryClient],
  );
  const taskUpdateInvalidationRef = useRef<TaskUpdateInvalidationBatcher>();
  useEffect(() => {
    const batcher = createTaskUpdateInvalidationBatcher(queryClient, {
      isCurrentScope: () => Boolean(catalogScope && catalogScopeForToken(getAuthToken()) === catalogScope),
    });
    taskUpdateInvalidationRef.current = batcher;
    return () => {
      if (taskUpdateInvalidationRef.current === batcher) taskUpdateInvalidationRef.current = undefined;
      batcher.dispose();
    };
  }, [queryClient, catalogScope]);
  const onTaskUpdate = useCallback(
    (data: Record<string, any>) => {
      const taskId = data.task_id || data.id;
      const planId = data.plan_id;
      taskUpdateInvalidationRef.current?.enqueue({ taskId, planId });
    },
    [],
  );
  const onJobUpdate = useCallback(
    (_data: Record<string, any>) => {
      // Scheduled job created / updated / deleted — refresh the
      // Automations page + any run history drawer that's open.
      queryClient.invalidateQueries({ queryKey: ["scheduled-jobs"] });
      queryClient.invalidateQueries({ queryKey: ["job-runs"] });
    },
    [queryClient],
  );
  const onVideoReady = useCallback(
    (data: Record<string, any>) => {
      if (!videoRefreshTimerRef.current) {
        videoRefreshTimerRef.current = setTimeout(() => {
          videoRefreshTimerRef.current = null;
          queryClient.invalidateQueries({ queryKey: ["media-jobs"] });
          queryClient.invalidateQueries({ queryKey: ["conversations"] });
        }, 3000);
      }
      const status = data.status;
      const prompt = data.prompt ? data.prompt.slice(0, 50) : "Video";
      if (status === "completed") {
        setToast(`Video ready: ${prompt}...`);
        setTimeout(() => setToast(null), 6000);
      } else if (status === "failed") {
        setToast(
          t("page.app_layout.video_failed").replace(
            "{error}",
            data.error?.slice(0, 60) || t("page.app_layout.unknown_error"),
          ),
        );
        setTimeout(() => setToast(null), 6000);
      }
      window.dispatchEvent(
        new CustomEvent("manor:video-ready", { detail: data }),
      );
    },
    [queryClient],
  );
  const onWorkspaceChatMessage = useCallback(
    (data: Record<string, any>) => {
      const wsId = data.workspace_id as string;
      if (!wsId) return;
      if (data.history_reset === true) {
        refreshWorkspaces();
        return;
      }
      if (
        data.author_kind === "user"
        // If this workspace isn't in our list yet, refetch
        || !chatWorkspaces.find((w) => w.id === wsId)
        || data.action_resolved
        || data.has_pending_action
      ) {
        refreshWorkspaces();
      }
      if (data.has_pending_action) {
        const wsName =
          chatWorkspaces.find((w) => w.id === wsId)?.name ||
          t("page.app_layout.workspace");
        if (data.action_kind === "human_input") {
          setToast(
            t("page.app_layout.input_needed_review_required").replace(
              "{name}",
              wsName,
            ),
          );
          setTimeout(() => setToast(null), 5000);
        } else if (data.action_kind === "approve_proposals") {
          setToast(
            t("page.app_layout.proposal_ready_review_in_chat").replace(
              "{name}",
              wsName,
            ),
          );
          setTimeout(() => setToast(null), 5000);
        } else if (data.action_kind === "retry_strategist_review") {
          setToast(
            t("page.app_layout.strategist_failed_retry_in_chat").replace(
              "{name}",
              wsName,
            ),
          );
          setTimeout(() => setToast(null), 5000);
        }
      }
    },
    [chatWorkspaces, refreshWorkspaces],
  );
  const onConversationMessage = useCallback(
    (data: Record<string, any>) => {
      scheduleQueryInvalidation(queryClient, ["conversations"]);
      window.dispatchEvent(
        new CustomEvent("manor:conversation-message", { detail: data }),
      );
    },
    [queryClient],
  );
  const onChatStreamSnapshot = useCallback((data: Record<string, any>) => {
    // Deliberately no query invalidation: these arrive about once a second for
    // the length of a turn, and invalidateQueries refetches active observers
    // immediately regardless of staleTime. The payload already carries the
    // reply, so the chat surface renders from it directly.
    window.dispatchEvent(
      new CustomEvent("manor:chat-stream-snapshot", { detail: data }),
    );
  }, []);
  const hasConnectedRef = useRef(false);
  const onConnect = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["chat-rooms"] });
    if (hasConnectedRef.current) {
      window.dispatchEvent(new Event(CATALOG_REVALIDATE_EVENT));
    }
    hasConnectedRef.current = true;
  }, [queryClient]);
  const roomListRefreshTimerRef = useRef<number | null>(null);
  const seenRoomMessageIdsRef = useRef(new Set<string>());
  const onChatRoomChanged = useCallback(({ room_id, kind, message }: ChatRoomChangeEvent) => {
    if (kind === "message" && message?.room_id === room_id) {
      if (seenRoomMessageIdsRef.current.has(message.id)) return;
      seenRoomMessageIdsRef.current.add(message.id);
      if (seenRoomMessageIdsRef.current.size > 1000) {
        seenRoomMessageIdsRef.current.delete(seenRoomMessageIdsRef.current.values().next().value!);
      }
      const readingRoom = activeConvType === "room" && activeConvId === room_id
        && document.visibilityState === "visible" && document.hasFocus();
      queryClient.setQueriesData<ChatRoom[]>({ queryKey: ["chat-rooms"] }, (rooms) => {
        if (!rooms?.some((room) => room.id === room_id)) return rooms;
        return rooms.map((room) => room.id === room_id ? {
          ...room,
          last_message_at: Date.parse(message.created_at) > Date.parse(room.last_message_at || room.created_at)
            ? message.created_at : room.last_message_at,
          unread_count: (room.unread_count || 0) + (message.sender_user_id !== user?.id && !readingRoom ? 1 : 0),
        } : room).sort((left, right) =>
          Date.parse(right.last_message_at || right.created_at) - Date.parse(left.last_message_at || left.created_at));
      });
      return;
    }
    if (kind === "reaction") return;
    if (roomListRefreshTimerRef.current !== null) return;
    roomListRefreshTimerRef.current = window.setTimeout(() => {
      roomListRefreshTimerRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ["chat-rooms"] });
    }, 250);
  }, [activeConvId, activeConvType, queryClient, user?.id]);
  useEffect(() => () => {
    if (roomListRefreshTimerRef.current !== null) window.clearTimeout(roomListRefreshTimerRef.current);
  }, []);
  const { connectedRef: _connectedRef, unreadCount: wsUnread } = useWebSocket({
    onConnect,
    onNotification,
    onTaskUpdate,
    onJobUpdate,
    onVideoReady,
    onWorkspaceChatMessage,
    onConversationMessage,
    onChatRoomChanged,
    onChatStreamSnapshot,
  });
  usePageViewTracking();
  useEffect(
    () => () => {
      if (videoRefreshTimerRef.current) {
        clearTimeout(videoRefreshTimerRef.current);
        videoRefreshTimerRef.current = null;
      }
    },
    [],
  );
  // Also poll unread count so it updates after mark-read on Notifications page
  const { data: notifData } = useQuery({
    queryKey: ["notifications", "badge"],
    queryFn: () => api.notifications.list({ limit: 1 }),
    enabled: startupSettled,
    refetchInterval: 60_000,
  });
  let unreadCount = notifData?.unread_count ?? wsUnread;
  const [locale, setLocaleState] = useState<Locale>(getLocale());
  const [localeSaving, setLocaleSaving] = useState(false);

  const handleLocaleChange = async (newLocale: Locale) => {
    if (localeSaving || (newLocale === locale && user?.locale === newLocale)) return;
    setLocaleSaving(true);
    try {
      const updatedUser = await useAuthStore.getState().updateLocale(newLocale);
      queryClient.setQueryData(["auth-me"], updatedUser);
      setLocaleState(getLocale());
      window.location.reload();
    } catch {
      setToast(t("page.account.save_failed"));
    } finally {
      setLocaleSaving(false);
    }
  };

  const isActive = (path: string) => {
    if (path === "/chat/history") {
      return location.pathname.startsWith("/chat/history");
    }
    if (path === "/tasks") {
      return (
        location.pathname === "/tasks" ||
        (location.pathname.startsWith("/tasks/") &&
          !location.pathname.startsWith("/tasks/collections"))
      );
    }
    if (path === "/tasks/collections") {
      return location.pathname.startsWith("/tasks/collections");
    }
    return location.pathname.startsWith(path);
  };
  /* Close mobile sidebar on route change */
  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    window.localStorage.setItem(
      "manor-sidebar-collapsed",
      collapsed ? "1" : "0",
    );
  }, [collapsed]);

  useEffect(() => {
    window.localStorage.setItem(CHAT_SIDEBAR_SECTIONS_KEY, JSON.stringify(chatSidebarSections));
  }, [chatSidebarSections]);

  /* Keep the top-level switcher aligned with concrete routes. Chat owns /chat;
   * every other route renders the workspace surface. */
  useEffect(() => {
    setMode(shouldDefaultToChat(location.pathname) ? "chat" : "workspace");
  }, [location.pathname, location.search]);

  useEffect(() => {
    if (location.pathname !== "/chat") return;
    const params = new URLSearchParams(location.search);
    const newTarget = params.get("new");
    if (newTarget === "chat") {
      const draftId = params.get("draft");
      setMode("chat");
      setActiveConvId(draftId?.startsWith("manor-new:") ? draftId : "manor-new:chat");
      setActiveConvType("manor");
      setActiveDmAgentId(null);
      setManorSessionsOpen(true);
      return;
    }
    if (newTarget === "message" || newTarget === "workspace") {
      setMode("chat");
      setActiveConvId(newTarget === "message" ? "new-conversation" : "new-workspace");
      setActiveConvType(newTarget === "message" ? "compose" : "workspace-draft");
      setActiveDmAgentId(null);
      return;
    }
    const humanRoomId = params.get("room");
    if (humanRoomId) {
      setMode("chat");
      setActiveConvId(humanRoomId);
      setActiveConvType("room");
      setActiveDmAgentId(null);
      return;
    }
    const target = parseAppLayoutChatTarget(location.search);
    setMode("chat");
    if (!target) {
      setActiveConvId("manor-ai");
      setActiveConvType("manor");
      setActiveDmAgentId(null);
      return;
    }
    if (target.type === "conversation") {
      setActiveConvId(target.conversationId);
      setActiveConvType("manor");
      setManorSessionsOpen(true);
    } else if (target.type === "workspace") {
      setActiveConvId(target.workspaceId);
      setActiveConvType("operation");
    } else {
      setActiveConvId(dmConversationByAgentId[target.agentId] || `agent:${target.agentId}`);
      setActiveConvType("dm");
      setActiveDmAgentId(target.agentId);
      const agent = chatAgentsList.find((item) => item.id === target.agentId);
      if (agent) {
        setHiredAgents((prev) => prev.some((item) => item.id === agent.id) ? prev : [...prev, agent]);
      }
      return;
    }
    setActiveDmAgentId(null);
    setConvSearchQuery("");
  }, [location.pathname, location.search, dmConversationByAgentId, chatAgentsList]);

  const isSettingsRoute = location.pathname === "/settings";
  // A display preference only changes presentation. Account, setup, approval
  // and artifact links retain their existing authorization and runtime paths.
  const simpleSetupTab = new URLSearchParams(location.search).get("tab");
  const isSimpleSetupPage = /^\/workspaces\/[^/]+$/.test(location.pathname)
    && new URLSearchParams(location.search).get("setup") === "1"
    && (simpleSetupTab === "channels" || simpleSetupTab === "capabilities");
  const isSimplePage = location.pathname === "/"
    || location.pathname === "/chat"
    || location.pathname.startsWith("/chat/")
    || isSettingsRoute
    || location.pathname === "/account"
    || location.pathname === "/company/clients"
    || location.pathname === "/team/clients"
    || location.pathname.startsWith("/client-delivery/")
    || location.pathname === "/notifications"
    || location.pathname === "/workspaces"
    || isSimpleSetupPage
    || location.pathname.startsWith("/viewer/")
    || location.pathname.startsWith("/editor/")
    || location.pathname.startsWith("/video-editor/")
    || location.pathname.startsWith("/integrations")
    ;
  const simpleWorkspaceId = /^\/workspaces\/([^/]+)$/.exec(location.pathname)?.[1];
  const simpleChatPath = simpleWorkspaceId && simpleWorkspaceId !== "new"
    ? `/chat?workspace=${simpleWorkspaceId}`
    : "/chat";
  const showProfessionalPageNotice = simpleMode && !isSimplePage;
  const isTasksRoute =
    location.pathname === "/tasks" ||
    (location.pathname.startsWith("/tasks/") &&
      !location.pathname.startsWith("/tasks/collections"));
  const isEditorShellRoute =
    location.pathname.startsWith("/viewer/") ||
    location.pathname.startsWith("/editor/") ||
    location.pathname === "/diagram-canvas";
  const sidebarCollapsed = collapsed && !mobileOpen;

  const switchMode = useCallback(
    (nextMode: AppMode) => {
      if (simpleMode && nextMode === "workspace") return;
      setMode(nextMode);
      if (nextMode === "chat") {
        if (location.pathname !== "/chat") {
          // Match the destination before Chat mounts and starts scoped reads.
          setActiveConvId("manor-ai");
          setActiveConvType("manor");
          setActiveDmAgentId(null);
          navigate("/chat");
        }
        return;
      }
      if (location.pathname === "/chat") navigate("/dashboard");
    },
    [location.pathname, navigate, simpleMode],
  );

  useEffect(() => {
    if (hasSelectedInterfaceMode && simpleMode && location.pathname === "/dashboard") navigate("/chat", { replace: true });
  }, [hasSelectedInterfaceMode, simpleMode, location.pathname, navigate]);

  // The onboarding tour highlights targets that only exist in one sidebar
  // mode; let it switch the app there (see OnboardingTour's "manor:tour-mode").
  useEffect(() => {
    const handleTourMode = (event: Event) => {
      const detail = (event as CustomEvent<{ mode?: AppMode; openSidebar?: boolean }>).detail;
      const nextMode = detail?.mode;
      if (nextMode === "chat" || nextMode === "workspace") switchMode(nextMode);
      if (typeof detail?.openSidebar === "boolean") setMobileOpen(detail.openSidebar);
    };
    // Agents/Skills/Integrations only mount while the Configure menu is open
    // in the expanded sidebar; the tour asks for it before highlighting them.
    const handleTourConfigure = () => setConfigureOpen(true);
    const handleTourFinish = () => setMobileOpen(false);
    window.addEventListener("manor:tour-mode", handleTourMode);
    window.addEventListener("manor:tour-configure", handleTourConfigure);
    window.addEventListener("manor:tour-finish", handleTourFinish);
    return () => {
      window.removeEventListener("manor:tour-mode", handleTourMode);
      window.removeEventListener("manor:tour-configure", handleTourConfigure);
      window.removeEventListener("manor:tour-finish", handleTourFinish);
    };
  }, [switchMode]);

  const sidebarWidth = sidebarCollapsed ? 92 : 260;
  const sectionLabelStyle = {
    display: "flex",
    alignItems: "center",
    gap: 6,
    padding: "0 12px 8px",
  } as const;

  const renderNavItem = (item: NavItem, opts: { nested?: boolean } = {}) => {
    const active = !item.disabled && isActive(item.path);
    const El = item.disabled ? "div" : Link;
    const linkProps = item.disabled ? {} : { to: item.path };
    return (
      <El
        key={item.path}
        {...(linkProps as any)}
        data-tour={item.tourKey || undefined}
        aria-label={t(item.i18nKey)}
        aria-current={active ? "page" : undefined}
        title={sidebarCollapsed ? t(item.i18nKey) : undefined}
        style={{
          position: "relative",
          display: "flex",
          alignItems: "center",
          gap: opts.nested ? 9 : 10,
          width: sidebarCollapsed ? 36 : "100%",
          height: sidebarCollapsed ? 36 : undefined,
          margin: sidebarCollapsed ? "0 auto" : undefined,
          padding: sidebarCollapsed
            ? 0
            : opts.nested
              ? "8px 10px 8px 14px"
              : "9px 10px",
          borderRadius: opts.nested ? 10 : 12,
          fontSize: opts.nested ? 12 : 12.5,
          fontWeight: active ? 760 : 560,
          letterSpacing: active ? "-0.01em" : "0",
          color: item.disabled
            ? "var(--sidebar-nav-disabled)"
            : active
              ? "var(--sidebar-nav-active-text)"
              : "var(--sidebar-nav-text)",
          background: active ? "var(--sidebar-nav-active-bg)" : "transparent",
          textDecoration: "none",
          transition: "all 0.2s ease",
          justifyContent: sidebarCollapsed ? "center" : undefined,
          cursor: item.disabled ? "default" : "pointer",
          opacity: item.disabled ? 0.6 : 1,
        }}
        onMouseEnter={(e: any) => {
          if (!active && !item.disabled) {
            e.currentTarget.style.background = "var(--sidebar-nav-hover-bg)";
            e.currentTarget.style.color = "var(--sidebar-nav-hover-text)";
          }
        }}
        onMouseLeave={(e: any) => {
          if (!active && !item.disabled) {
            e.currentTarget.style.background = "transparent";
            e.currentTarget.style.color = "var(--sidebar-nav-text)";
          }
        }}
      >
        {active && (
          <span
            style={{
              position: "absolute",
              left: sidebarCollapsed ? -6 : 0,
              top: sidebarCollapsed ? "50%" : "24%",
              height: sidebarCollapsed ? 22 : "52%",
              width: 3,
              background: "var(--sidebar-nav-active-indicator, var(--accent))",
              borderRadius: "0 4px 4px 0",
              transform: sidebarCollapsed ? "translateY(-50%)" : undefined,
            }}
          />
        )}
        <span
          style={{
            width: sidebarCollapsed ? 28 : opts.nested ? 24 : 28,
            height: sidebarCollapsed ? 28 : opts.nested ? 24 : 28,
            borderRadius: opts.nested ? 9 : 10,
            color: item.disabled
              ? "var(--sidebar-nav-disabled)"
              : active
                ? "var(--sidebar-nav-active-icon)"
                : "var(--sidebar-nav-icon)",
            background: active ? "var(--sidebar-icon-active-bg)" : "transparent",
            flexShrink: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {item.icon}
        </span>
        {!sidebarCollapsed && (
          <>
            <span>{t(item.i18nKey)}</span>
            {item.badge && (
              <span
                style={{
                  marginLeft: "auto",
                  fontSize: 9,
                  fontWeight: 700,
                  color: "var(--text-faint)",
                  background: "var(--surface-muted)",
                  padding: "2px 6px",
                  borderRadius: 6,
                  letterSpacing: "0.03em",
                }}
              >
                {item.badge}
              </span>
            )}
          </>
        )}
      </El>
    );
  };

  const renderCollapsedChatButton = (item: {
    id: string;
    type: "manor" | "operation" | "dm" | "room";
    label: string;
    subtitle?: string;
    customAvatar: ReactNode;
    count?: number;
    agentId?: string;
    sidebarId?: string;
  }) => {
    const active = item.type === "manor"
      ? activeConvType === "manor"
      : activeConvType === item.type && activeConvId === item.id;
    return (
      <span key={item.sidebarId || item.id} className="workspace-collapsed-chat-item">
      <button
        type="button"
        title={item.subtitle ? `${item.label} · ${item.subtitle}` : item.label}
        aria-label={`Open ${item.label}`}
        aria-pressed={active}
        onClick={() => {
          if (item.type === "manor") openManorChat();
          else if (item.type === "operation") openWorkspaceChat(item.id);
          else if (item.type === "room") openHumanRoom(item.id);
          else {
            const agent = hiredAgents.find((entry) => entry.id === item.agentId);
            if (agent) openAgentDm(agent);
          }
        }}
        style={{
          position: "relative",
          width: 36,
          height: 36,
          padding: 0,
          border: "1px solid transparent",
          borderRadius: 12,
          background: active ? "var(--sidebar-nav-active-bg)" : "transparent",
          boxShadow: "none",
          color: active
            ? "var(--sidebar-nav-active-text)"
            : "var(--sidebar-nav-text)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "default",
          transition: "all 0.18s ease",
        }}
        onMouseEnter={(e) => {
          if (!active) {
            e.currentTarget.style.background = "var(--sidebar-nav-hover-bg)";
          }
        }}
        onMouseLeave={(e) => {
          if (!active) {
            e.currentTarget.style.background = "transparent";
          }
        }}
      >
        {item.customAvatar}
        {item.type !== "operation" && !!item.count && item.count > 0 && (
          <span
            style={{
              position: "absolute",
              top: 2,
              right: 2,
              minWidth: 15,
              height: 15,
              padding: "0 4px",
              borderRadius: 999,
              background: "#d65f59",
              color: "#fff",
              border: "2px solid #fff",
              fontSize: 8.5,
              fontWeight: 800,
              lineHeight: "11px",
            }}
          >
            {item.count > 9 ? "9+" : item.count}
          </span>
        )}
      </button>
      {item.type === "operation" && !!item.count && item.count > 0 && (
        <WorkspaceAttentionBadge workspaceId={item.id} name={item.label} count={item.count} compact />
      )}
      </span>
    );
  };

  const newConversationKind = new URLSearchParams(location.search).get("kind") === "group" ? "group" : "dm";
  const manorMainActive = activeConvType === "manor" &&
    (activeConvId === "manor-ai" || activeConvId.startsWith("manor-new:") || !manorSessionsOpen);
  const chatWorkspaceSection = (!isSearchingConversations || filteredWorkspaces.length > 0) && (
    <section aria-label={t("page.app_layout.workspaces")}>
      <ChatSidebarSectionHeader
        id="chat-sidebar-workspaces"
        label={t("page.app_layout.workspaces")}
        expanded={isChatSidebarSectionOpen("workspaces")}
        onToggle={() => toggleChatSidebarSection("workspaces")}
        searching={isSearchingConversations}
        attentionCount={matchingWorkspaces.reduce((count, ws) => count + positiveStat(actionCounts[ws.id]), 0)}
        action={<WorkspaceAddAction
          cloud={deploymentMode === "cloud"}
          ready={workspaceAddReady}
          className="conv-section-add"
          tourId="create-workspace"
          onCreate={openNewWorkspaceDraft}
          onMarketplace={openWorkspaceMarketplace}
        />}
      />
      {isChatSidebarSectionOpen("workspaces") && <div id="chat-sidebar-workspaces">
      <div data-sidebar-reorder-list="workspaces" className={`workspace-sidebar-list${allSidebarWorkspaces && !isSearchingConversations ? " workspace-sidebar-list--expanded" : ""}`}>
      {filteredWorkspaces.map((ws) => {
        const isActive = activeConvType === "operation" && activeConvId === ws.id;
        if (renamingWorkspace?.id === ws.id) {
          return <form key={ws.id} className="conv-row conv-row-rename" onSubmit={(event) => {
            event.preventDefault();
            const name = workspaceNameDraft.trim();
            if (name && name !== ws.name) renameWorkspaceFromSidebar.mutate({ id: ws.id, name });
            else if (name === ws.name) setRenamingWorkspace(null);
          }}>
            <Input
              value={workspaceNameDraft}
              onChange={(event) => setWorkspaceNameDraft(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Escape") setRenamingWorkspace(null); }}
              maxLength={255}
              ariaLabel={getLocale() === "zh" ? `重命名 ${ws.name}` : `Rename ${ws.name}`}
              className="conv-row-rename-field"
              error={workspaceRenameError || undefined}
              autoFocus
              disabled={renameWorkspaceFromSidebar.isPending}
            />
            <Button type="submit" size="sm" variant="ghost" disabled={!workspaceNameDraft.trim() || renameWorkspaceFromSidebar.isPending} ariaLabel={t("action.save")}>{t("action.save")}</Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setRenamingWorkspace(null)} disabled={renameWorkspaceFromSidebar.isPending} ariaLabel={t("action.cancel")}>×</Button>
          </form>;
        }
        return <div key={ws.id} data-workspace-id={ws.id} {...workspaceSidebar.rowAttributes(ws.id)}
          className={`conv-row conv-row--with-menu workspace-sidebar-row ${isActive ? "conv-row--active" : ""}`}
        >
          <button type="button" className="conv-row-main sidebar-reorder-button"
          aria-current={isActive ? "page" : undefined}
          aria-description={isSearchingConversations ? undefined : t("page.app_layout.workspace_drag_name", { name: ws.name })}
          title={isSearchingConversations ? undefined : t("page.app_layout.workspace_drag_hint")}
          {...workspaceSidebar.buttonAttributes(ws.id, () => openWorkspaceChat(ws.id))}
          >
            <WorkspaceIconTile workspace={ws} size={30} iconSize={15} style={{ borderRadius: 9, flexShrink: 0 }} />
            <span className="min-w-0 flex-1">
              <HoverMarqueeText className="conv-name" text={ws.name} style={(actionCounts[ws.id] || 0) > 0 ? { fontWeight: 800, color: "var(--text-strong)" } : undefined} />
              {(!simpleMode || (ws.status && ws.status !== "active")) && <span className="conv-preview block">{workspaceStatusLabel(ws.status)}</span>}
            </span>
          </button>
          {(actionCounts[ws.id] || 0) > 0 && <WorkspaceAttentionBadge workspaceId={ws.id} name={ws.name} count={actionCounts[ws.id]} />}
          <SidebarRowMenu
            name={ws.name}
            items={[
              { key: "details", label: getLocale() === "zh" ? "Workspace 设置" : "Workspace settings" },
              ...(ws.can_manage ? [{ key: "rename", label: getLocale() === "zh" ? "重命名 Workspace" : "Rename Workspace" }] : []),
              ...(ws.can_delete ? [{ key: "delete", label: t("page.workspaces.delete_workspace"), danger: true }] : []),
            ]}
            onSelect={(key) => {
              if (key === "details") navigate(`/workspaces/${encodeURIComponent(ws.id)}?tab=settings`);
              if (key === "rename") {
                setRenamingWorkspace(ws);
                setWorkspaceNameDraft(ws.name);
                setWorkspaceRenameError("");
              }
              if (key === "delete" && ws.can_delete) {
                setDeleteWorkspaceTarget(ws);
                setDeleteWorkspaceError("");
              }
            }}
          />
        </div>;
      })}
      </div>
      {!isSearchingConversations && matchingWorkspaces.length > WORKSPACE_SIDEBAR_LIMIT && <button
        type="button"
        className="workspace-sidebar-expand"
        aria-expanded={allSidebarWorkspaces}
        onClick={() => setAllSidebarWorkspaces((value) => !value)}
      >{allSidebarWorkspaces ? t("page.app_layout.workspace_show_less") : t("page.app_layout.workspace_show_all", { count: String(matchingWorkspaces.length) })}</button>}
      </div>}
    </section>
  );

  return (
    <div className="flex flex-col h-screen relative">

      <div className="flex flex-1 relative" style={{ minHeight: 0 }}>
        {/* Aurora background */}
        <div className="aurora-bg">
          <div className="aurora-blob aurora-blob-1" />
          <div className="aurora-blob aurora-blob-2" />
          <div className="aurora-blob aurora-blob-3" />
        </div>

        {/* Mobile overlay backdrop */}
        {mobileOpen && (
          <div
            className="fixed inset-0 z-40 lg:hidden"
            style={{ background: "rgba(0,0,0,0.2)" }}
            onClick={() => setMobileOpen(false)}
          />
        )}

        {/* Sidebar */}
        {!isSettingsRoute && (
        <aside
          className={`sidebar-mobile ${mobileOpen ? "open" : ""}`}
          style={{
            width: sidebarWidth,
            flexShrink: 0,
            transition: "width 0.3s ease, margin-left 0.3s ease",
            left: mobileOpen ? 0 : undefined,
            marginLeft: mobileOpen ? 0 : undefined,
            position: "relative",
            zIndex: 50,
            padding: 12,
          }}
        >
          <div
            className="glass-panel app-shell-sidebar"
            style={{
              height: "100%",
              display: "flex",
              flexDirection: "column",
              overflow: sidebarCollapsed ? "visible" : "hidden",
              position: "relative",
            }}
          >
            {/* ── LOGO AREA ── */}
            <div
              style={{
                position: "relative",
                padding: sidebarCollapsed ? "12px 0 10px" : "16px 16px 12px",
                display: "flex",
                flexDirection: sidebarCollapsed ? "column" : undefined,
                alignItems: "center",
                justifyContent: sidebarCollapsed ? "center" : undefined,
                gap: sidebarCollapsed ? 8 : 10,
              }}
            >
              {/* Dark logo box */}
              <div
                className="manor-brand-mark"
                style={{
                  width: 32,
                  height: 32,
                  minWidth: 32,
                  borderRadius: 8,
                  background: "#000000",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                  boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
                }}
              >
                <ManorLogo size={24} color="white" className="manor-brand-logo" />
              </div>
              {!sidebarCollapsed && (
                <span
                  style={{
                    fontSize: 18,
                    fontWeight: 800,
                    color: "var(--text-strong)",
                    letterSpacing: 0,
                    lineHeight: 1,
                  }}
                >
                  Manor AI
                </span>
              )}
              {!sidebarCollapsed && (
                <button
                  aria-label={t("page.app_layout.collapse_sidebar")}
                  title={t("page.app_layout.collapse_sidebar")}
                  onClick={() => {
                    if (mobileOpen) {
                      setMobileOpen(false);
                    } else {
                      setCollapsed(!collapsed);
                    }
                  }}
                  style={{
                    marginLeft: "auto",
                    width: 36,
                    height: 36,
                    borderRadius: "50%",
                    background: "var(--sidebar-control-bg)",
                    border: "1px solid var(--sidebar-control-border)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    cursor: "pointer",
                    flexShrink: 0,
                    transition: "all 0.2s",
                    color: "var(--text-muted)",
                  }}
                >
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    style={{
                      transition: "transform 0.3s",
                      transform: "rotate(0deg)",
                    }}
                  >
                    <polyline points="15 18 9 12 15 6" />
                  </svg>
                </button>
              )}
            </div>

            {sidebarCollapsed && (
              <button
                className="app-sidebar-edge-toggle"
                aria-label={t("page.app_layout.expand_sidebar")}
                title={t("page.app_layout.expand_sidebar")}
                onClick={() => {
                  if (mobileOpen) {
                    setMobileOpen(false);
                  } else {
                    setCollapsed(!collapsed);
                  }
                }}
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <polyline points="15 18 9 12 15 6" />
                </svg>
              </button>
            )}

            {/* ── MODE SWITCHER ── */}
            {(!simpleMode || sidebarCollapsed) && (
            <div
              style={{
                padding: sidebarCollapsed ? "0 0 12px" : "0 16px 12px",
                display: sidebarCollapsed ? "flex" : undefined,
                justifyContent: sidebarCollapsed ? "center" : undefined,
              }}
              data-tour={simpleMode ? "create-workspace" : "mode-switcher"}
            >
              {simpleMode ? (
                <WorkspaceAddAction
                  cloud={deploymentMode === "cloud"}
                  ready={workspaceAddReady}
                  className="conv-section-add conv-section-add--collapsed"
                  onCreate={openNewWorkspaceDraft}
                  onMarketplace={openWorkspaceMarketplace}
                />
              ) : sidebarCollapsed ? (
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr",
                    gap: 5,
	                    width: 42,
	                    padding: 3,
	                    borderRadius: 14,
	                    background: "var(--sidebar-mode-bg)",
	                    border: "1px solid var(--sidebar-mode-border)",
	                  }}
	                >
                  {[
                    {
                      key: "chat" as const,
                      label: t("nav.chat"),
                      icon: <IconChatSmall />,
                    },
                    {
                      key: "workspace" as const,
                      label: t("page.app_layout.workspace_mode"),
                      icon: <IconGridSmall />,
                    },
                  ].map((item) => {
                    const active = mode === item.key;
                    return (
                      <button
                        key={item.key}
                        type="button"
                        title={item.label}
                        aria-label={t("page.app_layout.switch_to").replace(
                          "{label}",
                          item.label,
                        )}
                        aria-pressed={active}
                        onClick={() => switchMode(item.key)}
                        style={{
                          width: 34,
                          height: 32,
                          border: active
                            ? "1px solid var(--sidebar-mode-active-border)"
                            : "1px solid transparent",
                          borderRadius: 11,
                          background: active
                            ? "var(--sidebar-mode-active-bg)"
                            : "transparent",
                          color: active
                            ? "var(--sidebar-mode-active-text)"
                            : "var(--sidebar-mode-text)",
                          boxShadow: active
                            ? "var(--sidebar-mode-active-shadow)"
                            : "none",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          cursor: "pointer",
                          transition: "all 0.18s ease",
                          outline: "none",
                        }}
                      >
                        {item.icon}
                      </button>
                    );
                  })}
                </div>
              ) : (
                <div
                  style={{
                    position: "relative",
                    background: "var(--sidebar-mode-bg)",
                    border: "1px solid var(--sidebar-mode-border)",
                    borderRadius: 12,
                    padding: 3,
                    display: "flex",
                  }}
                >
                  {/* Animated slider */}
                  <div
                    style={{
                      position: "absolute",
                      top: 3,
                      left: 3,
                      width: "calc(50% - 3px)",
                      height: "calc(100% - 6px)",
                      background: "var(--sidebar-mode-active-bg)",
                      border: "1px solid var(--sidebar-mode-active-border)",
                      borderRadius: 10,
                      boxShadow: "var(--sidebar-mode-active-shadow)",
                      transition: "transform 0.25s cubic-bezier(0.4,0,0.2,1)",
                      transform:
                        mode === "workspace"
                          ? "translateX(100%)"
                          : "translateX(0)",
                      zIndex: 0,
                    }}
                  />
                  <button
                    onClick={() => switchMode("chat")}
                    style={{
                      flex: 1,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 5,
                      padding: "6px 0",
                      fontSize: 12,
                      fontWeight: mode === "chat" ? 600 : 500,
                      color:
                        mode === "chat"
                          ? "var(--sidebar-mode-active-text)"
                          : "var(--sidebar-mode-text)",
                      background: "transparent",
                      border: "none",
                      outline: "none",
                      cursor: "pointer",
                      position: "relative",
                      zIndex: 1,
                      transition: "color 0.2s",
                      borderRadius: 8,
                    }}
                  >
                    <IconChatSmall />
                    {t("nav.chat")}
                  </button>
                  <button
                    onClick={() => switchMode("workspace")}
                    style={{
                      flex: 1,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 5,
                      padding: "6px 0",
                      fontSize: 12,
                      fontWeight: mode === "workspace" ? 600 : 500,
                      color:
                        mode === "workspace"
                          ? "var(--sidebar-mode-active-text)"
                          : "var(--sidebar-mode-text)",
                      background: "transparent",
                      border: "none",
                      outline: "none",
                      cursor: "pointer",
                      position: "relative",
                      zIndex: 1,
                      transition: "color 0.2s",
                      borderRadius: 8,
                    }}
                  >
                    <IconGridSmall />
                    {t("page.app_layout.workspace_mode")}
                  </button>
                </div>
              )}
            </div>
            )}

            {/* ── SCROLLABLE NAV ── */}
            <div
              data-sidebar-scroll-area
              style={{
                flex: 1,
                overflowY: "auto",
                padding: sidebarCollapsed ? "0 0" : "0 8px",
              }}
            >
              {/* ══════════════════════════════════════════════ */}
              {/* CHAT MODE: Conversation list                   */}
              {/* ══════════════════════════════════════════════ */}
              {sidebarMode === "chat" && !sidebarCollapsed && (
                <div
                  style={{ display: "flex", flexDirection: "column", gap: 0 }}
                >
                  {/* Search input */}
                  <div className="chat-sidebar-global-search-wrap">
                    <div className="chat-sidebar-global-search-field">
                      <svg
                        width="13"
                        height="13"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="#a8a29e"
                        strokeWidth={2}
                        className="chat-sidebar-global-search-icon"
                      >
                        <circle cx="11" cy="11" r="8" />
                        <line x1="21" y1="21" x2="16.65" y2="16.65" />
                      </svg>
                      <input
                        type="text"
                        className="chat-sidebar-global-search"
                        value={convSearchQuery}
                        onChange={(e) => setConvSearchQuery(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key !== "Enter") return;
                          if (!conversationSearch) return;
                          const firstSession = filteredManorChatSessions[0];
                          if (firstSession) {
                            e.preventDefault();
                            openManorSession(firstSession.id);
                            return;
                          }
                          if (manorIdentityMatchesSearch) {
                            e.preventDefault();
                            openManorChat();
                            return;
                          }
                          const firstWorkspace = filteredWorkspaces[0];
                          if (firstWorkspace) {
                            e.preventDefault();
                            openWorkspaceChat(firstWorkspace.id);
                            return;
                          }
                          const firstGroup = filteredHumanChatRooms.find((room) => room.kind === "group");
                          if (firstGroup) {
                            e.preventDefault();
                            openHumanRoom(firstGroup.id);
                            return;
                          }
                          const firstDirect = filteredHumanChatRooms.find((room) => room.kind === "dm");
                          if (firstDirect) {
                            e.preventDefault();
                            openHumanRoom(firstDirect.id);
                            return;
                          }
                          const firstMatch =
                            filteredDmAgents[0] || visibleSearchAgents[0];
                          if (!firstMatch) return;
                          e.preventDefault();
                          openAgentDm(firstMatch);
                        }}
                        placeholder={t(simpleMode ? "page.app_layout.search_conversations" : "page.app_layout.search_chats_and_agents")}
                        aria-label={t(simpleMode ? "page.app_layout.search_conversations" : "page.app_layout.search_chats_and_agents")}
                      />
                    </div>
                  </div>


                  {/* Manor AI and its sessions lead the chat list. */}
                  {(!isSearchingConversations || manorSearchHasResults) && (
                    <section className="manor-session-group" aria-label={t("page.app_layout.ai_chats")}>
                      <ChatSidebarSectionHeader
                        id="chat-sidebar-ai-chats"
                        label={t("page.app_layout.ai_chats")}
                        expanded={isChatSidebarSectionOpen("ai")}
                        onToggle={() => toggleChatSidebarSection("ai")}
                        searching={isSearchingConversations}
                        attentionCount={positiveStat(actionCounts["manor-ai"])}
                        action={<button type="button" className="conv-section-add" aria-label={t("component.session_switcher.new_chat_2")} title={t("component.session_switcher.new_chat_2")} onClick={startNewManorSession}>
                          <IconPlus />
                        </button>}
                      />
                      {isChatSidebarSectionOpen("ai") && <div id="chat-sidebar-ai-chats">
                      <div
                        className={`conv-row conv-row--manor ${manorMainActive ? "conv-row--active" : ""}`}
                      >
                        <button
                          type="button"
                          className="conv-row-main"
                          aria-current={manorMainActive ? "page" : undefined}
                          onClick={openManorChat}
                        >
                          <UserAvatar type="manor" size={30} />
                          <span className="conv-row-copy">
                            <span className="conv-name">Manor AI</span>
                            <span className="conv-preview">
                              {t("page.app_layout.your_ai_chief_of_staff")}
                            </span>
                          </span>
                        </button>
                        {(actionCounts["manor-ai"] || 0) > 0 && (
                          <span className="conv-badge">
                            {actionCounts["manor-ai"]}
                          </span>
                        )}
                        <button
                          type="button"
                          className={`manor-session-toggle ${manorSessionsOpen ? "manor-session-toggle--open" : ""}`}
                          aria-expanded={manorSessionsOpen}
                          aria-controls="manor-chat-sessions"
                          aria-label={t(
                            manorSessionsOpen
                              ? "page.app_layout.collapse_manor_sessions"
                              : "page.app_layout.expand_manor_sessions",
                          )}
                          onClick={() => setManorSessionsOpen((open) => !open)}
                        >
                          <svg
                            width="14"
                            height="14"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth={2}
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            aria-hidden="true"
                          >
                            <path d="m9 18 6-6-6-6" />
                          </svg>
                        </button>
                      </div>

                      {manorSessionsOpen && (
                        <div
                          id="manor-chat-sessions"
                          className="manor-session-panel"
                        >
                          {chatConversationsLoading && (
                            <div className="manor-session-loading">
                              <InlineRowsSkeleton rows={3} dense />
                            </div>
                          )}
                          {!chatConversationsLoading &&
                            !chatConversationsError &&
                            filteredManorChatSessions.length > 0 && (
                              <div className="manor-session-list" data-sidebar-reorder-list="ai">
                                {filteredManorChatSessions.map((conversation) => {
                                  const isActive =
                                    activeConvType === "manor" &&
                                    activeConvId === conversation.id;
                                  const isRenaming =
                                    renamingConversationId === conversation.id;
                                  const runningActivity =
                                    runningManorSessionActivities.get(
                                      conversation.id,
                                    ) ||
                                    (isActive
                                      ? activeManorSessionActivity
                                      : undefined);
                                  const isRunning = Boolean(runningActivity);
                                  if (isRenaming) {
                                    return (
                                      <form
                                        key={conversation.id}
                                        className="manor-session-rename"
                                        onSubmit={(event) => {
                                          event.preventDefault();
                                          submitRenameConversation();
                                        }}
                                      >
                                        <Input
                                          value={conversationTitleDraft}
                                          onChange={(event) =>
                                            setConversationTitleDraft(
                                              event.target.value,
                                            )
                                          }
                                          ariaLabel={t(
                                            "component.session_switcher.rename_chat_label",
                                            {
                                              title:
                                                conversation.title ||
                                                t("page.chat_history.untitled"),
                                            },
                                          )}
                                          className="manor-session-rename-input"
                                          error={conversationRenameError || undefined}
                                          autoFocus
                                          disabled={renameChatConversation.isPending}
                                        />
                                        <div className="manor-session-rename-actions">
                                          <Button
                                            type="submit"
                                            variant="ghost"
                                            size="sm"
                                            className="manor-session-icon-action"
                                            title={t("action.save")}
                                            ariaLabel={t("action.save")}
                                            loading={renameChatConversation.isPending}
                                            disabled={!conversationTitleDraft.trim()}
                                          >
                                            <svg
                                              width="14"
                                              height="14"
                                              viewBox="0 0 24 24"
                                              fill="none"
                                              stroke="currentColor"
                                              strokeWidth={2}
                                              strokeLinecap="round"
                                              strokeLinejoin="round"
                                              aria-hidden="true"
                                            >
                                              <path d="m5 12 4 4L19 6" />
                                            </svg>
                                          </Button>
                                          <Button
                                            variant="ghost"
                                            size="sm"
                                            className="manor-session-icon-action"
                                            onClick={cancelRenameConversation}
                                            title={t("action.cancel")}
                                            ariaLabel={t("action.cancel")}
                                            disabled={renameChatConversation.isPending}
                                          >
                                            <svg
                                              width="14"
                                              height="14"
                                              viewBox="0 0 24 24"
                                              fill="none"
                                              stroke="currentColor"
                                              strokeWidth={2}
                                              strokeLinecap="round"
                                              strokeLinejoin="round"
                                              aria-hidden="true"
                                            >
                                              <path d="m18 6-12 12" />
                                              <path d="m6 6 12 12" />
                                            </svg>
                                          </Button>
                                        </div>
                                      </form>
                                    );
                                  }
                                  return (
                                    <div
                                      key={conversation.id}
                                      {...aiSidebar.rowAttributes(conversation.id)}
                                      className={`conv-row manor-session-row ${isActive ? "conv-row--active manor-session-row--active" : ""}`}
                                    >
                                      <button
                                        type="button"
                                        className="manor-session-row-main sidebar-reorder-button"
                                        aria-current={isActive ? "page" : undefined}
                                        aria-description={isSearchingConversations ? undefined : t("page.app_layout.workspace_drag_name", { name: conversation.title || t("page.chat_history.untitled") })}
                                        {...aiSidebar.buttonAttributes(conversation.id, () => openManorSession(conversation.id))}
                                      >
                                        <span className="manor-session-avatar" aria-hidden="true">
                                          <IconChatSmall />
                                        </span>
                                        <span className="manor-session-copy">
                                          <HoverMarqueeText
                                            className="manor-session-title"
                                            text={
                                              conversation.title ||
                                              t("page.chat_history.untitled")
                                            }
                                          />
                                          <span className="manor-session-time">
                                            {relativeTime(
                                              conversation.updated_at ||
                                                conversation.created_at,
                                            )}
                                          </span>
                                        </span>
                                      </button>
                                      <Dropdown
                                        align="right"
                                        constrainTo=".app-shell-sidebar"
                                        menuStyle={{ border: "none" }}
                                        trigger={
                                          <Button
                                            variant="ghost"
                                            size="sm"
                                            className={`manor-session-more sidebar-row-more ${isRunning ? "manor-session-more--running" : ""}`}
                                            title={t(
                                              isRunning
                                                ? "page.app_layout.running_manor_session_menu"
                                                : "action.more",
                                            )}
                                            ariaLabel={t(
                                              isRunning
                                                ? "page.app_layout.running_manor_session_menu"
                                                : "action.more",
                                            )}
                                          >
                                            <span
                                              className="manor-session-more__orb"
                                              aria-hidden="true"
                                            >
                                              <AgentActivityOrb
                                                activity={
                                                  runningActivity || "working"
                                                }
                                                size={20}
                                                displaySize={16}
                                                iconOnly
                                              />
                                            </span>
                                            <span
                                              className="manor-session-more__dots"
                                              aria-hidden="true"
                                            >
                                              <IconMoreHorizontal size={18} />
                                            </span>
                                          </Button>
                                        }
                                        items={[
                                          {
                                            key: "rename",
                                            label: t(
                                              "component.session_switcher.rename_chat",
                                            ),
                                          },
                                          {
                                            key: "delete",
                                            label: t(
                                              "component.session_switcher.delete_chat",
                                            ),
                                            danger: true,
                                          },
                                        ]}
                                        onSelect={(key) => {
                                          if (key === "rename") {
                                            beginRenameConversation(conversation);
                                            return;
                                          }
                                          setDeleteConversationError("");
                                          setDeleteConversationTarget(conversation);
                                        }}
                                      />
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          {!chatConversationsLoading &&
                            !chatConversationsError &&
                            manorChatSessions.length === 0 && (
                              <div className="manor-session-empty">
                                {t("page.app_layout.no_manor_sessions")}
                              </div>
                            )}
                          {!chatConversationsLoading &&
                            !chatConversationsError &&
                            manorChatSessions.length > 0 &&
                            filteredManorChatSessions.length === 0 &&
                            !manorIdentityMatchesSearch && (
                              <div className="manor-session-empty">
                                {t("page.app_layout.no_matching_manor_sessions")}
                              </div>
                            )}
                          {chatConversationsError && (
                            <div className="manor-session-empty">
                              {t("page.app_layout.manor_sessions_unavailable")}
                            </div>
                          )}
                        </div>
                      )}
                      </div>}
                    </section>
                  )}

                  {chatWorkspaceSection}

                  {(["group", "dm"] as const).map((kind) => {
                    const sidebar = kind === "group" ? groupSidebar : dmSidebar;
                    const rank = new Map(sidebar.items.map((item, index) => [item.id, index]));
                    const matchingRooms = filteredHumanChatRooms.filter((room) => room.kind === kind);
                    if (isSearchingConversations && !matchingRooms.length && (kind === "group" || (filteredDmAgents.length === 0 && visibleSearchAgents.length === 0))) return null;
                    const sectionLabel = kind === "group" ? (getLocale() === "zh" ? "群聊" : "Group chats") : (getLocale() === "zh" ? "私聊" : "Direct chats");
                    return <section key={kind} aria-label={sectionLabel}>
                      <ChatSidebarSectionHeader
                        id={`chat-sidebar-${kind}`}
                        label={sectionLabel}
                        expanded={isChatSidebarSectionOpen(kind)}
                        onToggle={() => toggleChatSidebarSection(kind)}
                        searching={isSearchingConversations}
                        attentionCount={kind === "group" ? matchingRooms.reduce((count, room) => count + positiveStat(room.unread_count), 0) : filteredDmAgents.reduce((count, agent) => count + positiveStat(actionCounts[agent.id]), 0)}
                        badge={kind === "group" && groupUsageLoaded && humanChatRoomsLoaded && groupDiscovery.badgeVisible && !isSearchingConversations ? <FeatureDiscoveryBadge label={t("component.feature_discovery.new")} onClick={groupDiscovery.reveal} expanded={groupDiscovery.open} controls="group-chat-feature-discovery" /> : undefined}
                        action={<button type="button" className="conv-section-add" aria-label={kind === "group" ? (getLocale() === "zh" ? "新建群聊" : "New group chat") : (getLocale() === "zh" ? "新建私聊" : "New direct chat")} title={kind === "group" ? (getLocale() === "zh" ? "新建群聊" : "New group chat") : (getLocale() === "zh" ? "新建私聊" : "New direct chat")} onClick={kind === "group" ? openNewGroup : openNewConversation}><IconPlus /></button>}
                      />
                      {kind === "group" && groupDiscovery.open && !isSearchingConversations && <FeatureDiscoveryHint id="group-chat-feature-discovery" title={t("component.feature_discovery.group_chat_title")} description={t("component.feature_discovery.group_chat_description")} actionLabel={t("component.feature_discovery.create_group")} laterLabel={t("component.feature_discovery.later")} onAction={openNewGroup} onDismiss={groupDiscovery.dismiss} />}
                      {isChatSidebarSectionOpen(kind) && <div id={`chat-sidebar-${kind}`} data-sidebar-reorder-list={kind}>
                      {!matchingRooms.length && kind === "group" && <div className="px-4 py-1 text-xs text-[var(--text-faint)]">{getLocale() === "zh" ? "还没有群聊" : "No group chats yet"}</div>}
                      {[...matchingRooms.map((room) => {
                        const sidebarId = kind === "dm" ? `room:${room.id}` : room.id;
                        const isActive = activeConvType === "room" && activeConvId === room.id;
                        const memberRole = room.members.find((member) => member.user_id === user?.id)?.role;
                        const otherMember = room.kind === "dm" ? room.members.find((member) => member.user_id !== user?.id) : undefined;
                        return <div key={sidebarId} {...sidebar.rowAttributes(sidebarId)} className={`conv-row conv-row--with-menu ${isActive ? "conv-row--active" : ""}`}>
                          <button type="button" className="conv-row-main sidebar-reorder-button" aria-current={isActive ? "page" : undefined} aria-description={isSearchingConversations ? undefined : t("page.app_layout.workspace_drag_name", { name: room.title })} {...sidebar.buttonAttributes(sidebarId, () => openHumanRoom(room.id))}>
                            {room.kind === "group" ? <GroupChatAvatar title={room.title} members={room.members} currentUserId={user?.id} /> : <UserAvatar name={room.title} avatarUrl={otherMember?.avatar_url} size={30} />}
                            <span className="min-w-0 flex-1"><HoverMarqueeText className="conv-name" text={room.title} style={room.kind === "group" && (room.unread_count || 0) > 0 ? { fontWeight: 800, color: "var(--text-strong)" } : undefined} /><span className="conv-preview block">{room.kind === "group" ? groupChatSummary(room) : room.member_count < 2 ? (getLocale() === "zh" ? "私聊 · 等待加入" : "Direct message · Awaiting acceptance") : room.online_user_ids ? <span className="inline-flex items-center gap-1.5"><span aria-hidden="true" className={room.online_user_ids.some((id) => id !== user?.id) ? "chat-status-dot chat-status-dot--online" : "chat-status-dot bg-[var(--text-faint)]"} />{getLocale() === "zh" ? "私聊 · " : "Direct message · "}{room.online_user_ids.some((id) => id !== user?.id) ? (getLocale() === "zh" ? "在线" : "Online") : (getLocale() === "zh" ? "离线" : "Offline")}</span> : (getLocale() === "zh" ? "私聊" : "Direct message")}</span></span>
                          </button>
                          {room.kind === "group" && (room.unread_count || 0) > 0 && <span className="conv-badge" aria-label={getLocale() === "zh" ? `${room.unread_count} 条未读消息` : `${room.unread_count} unread messages`}>{(room.unread_count || 0) > 99 ? "99+" : room.unread_count}</span>}
                          <SidebarRowMenu
                            name={room.title}
                            items={[
                              { key: "details", label: room.kind === "group" ? (getLocale() === "zh" ? "群聊详情" : "Group details") : (getLocale() === "zh" ? "私聊详情" : "Direct message details") },
                              ...(room.kind === "group" && (memberRole === "owner" || memberRole === "admin") ? [{ key: "rename", label: getLocale() === "zh" ? "重命名群聊" : "Rename group" }] : []),
                              ...(room.kind === "group" && memberRole === "owner" ? [{ key: "close", label: getLocale() === "zh" ? "解散群聊" : "Close group", danger: true }] : []),
                            ]}
                            onSelect={(key) => {
                              if (key === "details" || key === "rename" || key === "close") openHumanRoomAction(room.id, key);
                            }}
                          />
                        </div>;
                      }), ...(kind === "dm" ? filteredDmAgents
                    .filter((agent) => agent && agent.name)
                    .map((agent) => {
                      const sidebarId = `agent:${agent.id}`;
                      const dmConversation = dmConversationRecordByAgentId.get(agent.id);
                      const isDraft = !dmConversation && !chatConversationsLoading && !chatConversationsError;
                      const isActive = activeConvType === "dm" && activeDmAgentId === agent.id;
                      if ((dmConversation && renamingConversationId === dmConversation.id) || (isDraft && renamingAgentDraftId === agent.id)) {
                        return <form key={sidebarId} className="conv-row conv-row-rename" onSubmit={(event) => {
                          event.preventDefault();
                          submitRenameConversation();
                        }}>
                          <Input
                            value={conversationTitleDraft}
                            onChange={(event) => setConversationTitleDraft(event.target.value)}
                            onKeyDown={(event) => { if (event.key === "Escape") cancelRenameConversation(); }}
                            maxLength={500}
                            ariaLabel={t("component.session_switcher.rename_chat_label", { title: dmConversation?.title || agent.name })}
                            className="conv-row-rename-field"
                            error={conversationRenameError || undefined}
                            autoFocus
                            disabled={renameChatConversation.isPending || startAgentConversation.isPending}
                          />
                          <Button type="submit" size="sm" variant="ghost" loading={renameChatConversation.isPending || startAgentConversation.isPending} disabled={!conversationTitleDraft.trim()} ariaLabel={t("action.save")}>{t("action.save")}</Button>
                          <Button type="button" size="sm" variant="ghost" onClick={cancelRenameConversation} disabled={renameChatConversation.isPending || startAgentConversation.isPending} ariaLabel={t("action.cancel")}>×</Button>
                        </form>;
                      }
                      return (
                        <div
                          key={sidebarId}
                          {...dmSidebar.rowAttributes(sidebarId)}
                          className={`conv-row conv-row--with-menu ${isActive ? "conv-row--active" : ""}`}
                        >
                          <button type="button" className="conv-row-main sidebar-reorder-button" aria-current={isActive ? "page" : undefined} aria-description={isSearchingConversations ? undefined : t("page.app_layout.workspace_drag_name", { name: dmConversation?.title || agent.name })} {...dmSidebar.buttonAttributes(sidebarId, () => openAgentDm(agent))}>
                            <AgentAvatar name={agent.name} avatarUrl={agent.avatar_url} seed={agent.id} size={30} className="conv-avatar" />
                            <span className="min-w-0 flex-1">
                              <HoverMarqueeText className="conv-name" text={dmConversation?.title || agent.name} />
                              <span className="conv-preview block">{dmConversation?.title ? agent.name : agentPreview(agent, "page.app_layout.agent")}</span>
                            </span>
                          </button>
                          {(actionCounts[agent.id] || 0) > 0 && <span className="conv-badge">{actionCounts[agent.id]}</span>}
                          <SidebarRowMenu
                            name={agent.name}
                            items={[
                              { key: "profile", label: getLocale() === "zh" ? "查看 Agent" : "View Agent" },
                              ...(dmConversation || isDraft ? [
                                { key: "rename", label: t("component.session_switcher.rename_chat") },
                                { key: "delete", label: t("component.session_switcher.delete_chat"), danger: true },
                              ] : []),
                            ]}
                            onSelect={(key) => {
                              if (key === "profile") navigate(`/agents/${encodeURIComponent(agent.id)}`);
                              if (key === "rename") {
                                if (dmConversation) beginRenameConversation(dmConversation);
                                else if (isDraft) beginRenameAgentDraft(agent);
                              }
                              if (key === "delete" && dmConversation) {
                                setDeleteConversationError("");
                                setDeleteConversationTarget(dmConversation);
                              } else if (key === "delete" && isDraft) {
                                setHiredAgents((previous) => previous.filter((item) => item.id !== agent.id));
                                if (isActive) openManorChat();
                              }
                            }}
                          />
                        </div>
                      );
                    }) : [])].sort((a, b) => (rank.get(String(a.key)) ?? Infinity) - (rank.get(String(b.key)) ?? Infinity))}

                      {/* Search can offer new AI direct chats below existing ones. */}
                      {kind === "dm" && visibleSearchAgents.length > 0 && (
                    <>
                      {visibleSearchAgents.map((agent) => {
                        const isActive =
                          activeConvType === "dm" &&
                          activeDmAgentId === agent.id;
                        return (
                          <div
                            key={agent.id}
                            className={`conv-row conv-row--with-menu ${isActive ? "conv-row--active" : ""}`}
                          >
                            <button type="button" className="conv-row-main" aria-current={isActive ? "page" : undefined} onClick={() => openAgentDm(agent)}>
                              <AgentAvatar
                                name={agent.name}
                                avatarUrl={agent.avatar_url}
                                seed={agent.id}
                                size={30}
                                className="conv-avatar"
                              />
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <HoverMarqueeText className="conv-name" text={agent.name} />
                                <div className="conv-preview">
                                  {agentPreview(
                                    agent,
                                    "page.app_layout.start_a_chat",
                                  )}
                                </div>
                              </div>
                            </button>
                            <SidebarRowMenu name={agent.name} items={[{ key: "profile", label: getLocale() === "zh" ? "查看 Agent" : "View Agent" }]} onSelect={() => navigate(`/agents/${encodeURIComponent(agent.id)}`)} />
                          </div>
                        );
                      })}
                    </>
                  )}
                      </div>}
                    </section>;
                  })}

                  {convSearchQuery.trim() &&
                    !manorSearchHasResults &&
                    filteredWorkspaces.length === 0 &&
                    filteredHumanChatRooms.length === 0 &&
                    filteredDmAgents.length === 0 &&
                    visibleSearchAgents.length === 0 && (
                      <div
                        style={{
                          padding: "14px 12px",
                          fontSize: 12,
                          color: "var(--sidebar-section-text)",
                          textAlign: "center",
                        }}
                      >
                        {t(simpleMode ? "page.app_layout.no_matching_conversations" : "page.app_layout.no_matching_chats_or_agents")}
                      </div>
                    )}
                </div>
              )}

              {sidebarMode === "chat" && sidebarCollapsed && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    gap: 7,
                    padding: "4px 0 10px",
                  }}
                >
                  <button type="button" title={getLocale() === "zh" ? "新建对话" : "New conversation"} aria-label={getLocale() === "zh" ? "新建对话" : "New conversation"} onClick={openNewConversation} className="flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--surface-muted)] text-[var(--text-strong)]"><IconPlus /></button>
                  {renderCollapsedChatButton({
                    id: "manor-ai",
                    type: "manor",
                    label: "Manor AI",
                    subtitle: t("page.app_layout.your_ai_chief_of_staff"),
                    customAvatar: <UserAvatar type="manor" size={30} />,
                    count: actionCounts["manor-ai"] || 0,
                  })}

                  {orderedWorkspaces.slice(0, 8).map((ws) => {
                    return renderCollapsedChatButton({
                      id: ws.id,
                      type: "operation",
                      label: ws.name,
                      subtitle:
                        simpleMode && (!ws.status || ws.status === "active")
                          ? undefined
                          : ws.status === "active"
                          ? t("page.app_layout.active_workspace")
                          : workspaceStatusLabel(ws.status),
                      customAvatar: (
                        <WorkspaceIconTile
                          workspace={ws}
                          size={30}
                          iconSize={15}
                          style={{ borderRadius: 10 }}
                        />
                      ),
                      count: actionCounts[ws.id] || 0,
                    });
                  })}

                  {groupSidebar.items.slice(0, 8).map((room) => renderCollapsedChatButton({ id: room.id, type: "room", label: room.title, subtitle: groupChatSummary(room), customAvatar: <GroupChatAvatar title={room.title} members={room.members} currentUserId={user?.id} />, count: room.unread_count || 0 }))}
                  {collapsedDmItems.map((item) => {
                    if (item.kind === "room") {
                      const room = item.room;
                      return renderCollapsedChatButton({ id: room.id, sidebarId: item.id, type: "room", label: room.title, subtitle: getLocale() === "zh" ? "私聊" : "Direct chat", customAvatar: <UserAvatar name={room.title} avatarUrl={room.members.find((member) => member.user_id !== user?.id)?.avatar_url} size={30} /> });
                    }
                    const agent = item.agent;
                    const convId = dmConversationByAgentId[agent.id] || item.id;
                    return renderCollapsedChatButton({
                      id: convId,
                      sidebarId: item.id,
                      type: "dm",
                      label: agent.name,
                      subtitle: agentPreview(agent, "page.app_layout.agent"),
                      customAvatar: <AgentAvatar name={agent.name} avatarUrl={agent.avatar_url} seed={agent.id} size={30} />,
                      count: actionCounts[agent.id] || 0,
                      agentId: agent.id,
                    });
                  })}
                </div>
              )}

              {/* ══════════════════════════════════════════════ */}
              {/* WORKSPACE MODE: Original nav menus             */}
              {/* ══════════════════════════════════════════════ */}

              {/* WORKSPACE SECTION */}
              {sidebarMode === "workspace" && (
                <div
                  style={{
                    background: "var(--sidebar-group-bg)",
                    border: "1px solid var(--sidebar-group-border)",
                    borderRadius: sidebarCollapsed ? 18 : 16,
                    padding: sidebarCollapsed ? "10px 4px" : "12px 9px",
                    marginBottom: 10,
                  }}
                >
                  {/* Section header */}
                  {!sidebarCollapsed && (
                    <div style={sectionLabelStyle}>
                      <span
                        style={{
                          fontSize: 10,
                          fontWeight: 800,
                          color: "var(--sidebar-section-text)",
                          textTransform: "uppercase",
                          letterSpacing: "0.16em",
                        }}
                      >
                        {t("page.app_layout.operate")}
                      </span>
                      <span
                        className="sidebar-pulse-dot"
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: "50%",
                          background: "#5f928a",
                          flexShrink: 0,
                        }}
                      />
                    </div>
                  )}

                  {/* Context switcher — autocomplete style matching original */}
                  {!sidebarCollapsed && (
                    <div
                      style={{
                        display: "flex",
                        alignItems: "flex-start",
                        gap: 6,
                      }}
                    >
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <ContextSwitcher
                          workspaces={chatWorkspaces}
                          value={activeWorkspaceId}
                          onSelect={(id) => {
                            setActiveWorkspaceId(id);
                          }}
                        />
                      </div>
                      <WorkspaceAddAction
                        cloud={deploymentMode === "cloud"}
                        ready={workspaceAddReady}
                        className="sidebar-workspace-create"
                        style={{
                          width: 36,
                          height: 36,
                          marginRight: 4,
                          borderRadius: 12,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          flexShrink: 0,
                        }}
                        onCreate={openStandaloneWorkspaceDraft}
                        onMarketplace={openWorkspaceMarketplace}
                      />
                    </div>
                  )}

                  {/* Workspace nav items */}
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: sidebarCollapsed ? "center" : "stretch",
                      gap: sidebarCollapsed ? 6 : 2,
                    }}
                  >
                    {workspaceItems.map((item) => renderNavItem(item))}
                    {sidebarCollapsed && (
                      <WorkspaceAddAction
                        cloud={deploymentMode === "cloud"}
                        ready={workspaceAddReady}
                        className="sidebar-workspace-create sidebar-workspace-create--collapsed"
                        style={{
                          width: 36,
                          height: 36,
                          margin: "0 auto",
                          borderRadius: 12,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                        }}
                        onCreate={openStandaloneWorkspaceDraft}
                        onMarketplace={openWorkspaceMarketplace}
                      />
                    )}
                  </div>
                </div>
              )}

              {/* MANAGEMENT SECTION (workspace mode only) */}
              {sidebarMode === "workspace" && (
                <div
                  style={{
                    paddingTop: sidebarCollapsed ? 2 : 8,
                  }}
                >
                  {/* Section header */}
                  {!sidebarCollapsed && (
                    <div style={sectionLabelStyle}>
                      <span
                        style={{
                          fontSize: 10,
                          fontWeight: 800,
                          color: "var(--sidebar-section-text)",
                          textTransform: "uppercase",
                          letterSpacing: "0.16em",
                        }}
                      >
                        {t("page.app_layout.library")}
                      </span>
                    </div>
                  )}

                  {/* Management nav items */}
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: sidebarCollapsed ? "center" : "stretch",
                      gap: sidebarCollapsed ? 6 : 2,
                    }}
                  >
                    {managementItems.map((item) => renderNavItem(item))}
                  </div>

                  {/* Configuration stays tucked away so first-time users see the operating surface first. */}
                  <div
                    data-tour="configure-menu"
                    style={{
                      marginTop: sidebarCollapsed ? 8 : 10,
                      paddingTop: sidebarCollapsed ? 8 : 10,
                      borderTop: "1px solid var(--sidebar-divider)",
                    }}
                  >
                    {!sidebarCollapsed ? (
                      <>
                        <button
                          type="button"
                          onClick={() => setConfigureOpen((open) => !open)}
                          style={{
                            width: "100%",
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            padding: "8px 10px",
                            border: "none",
                            borderRadius: 12,
                            background: configureOpen
                              ? "var(--sidebar-nav-active-bg)"
                              : "transparent",
                            color: "var(--sidebar-nav-text)",
                            cursor: "pointer",
                            fontFamily: "inherit",
                            fontSize: 12,
                            fontWeight: 700,
                            letterSpacing: "0.01em",
                          }}
                        >
                          <span
                            style={{
                              width: 28,
                              height: 28,
                              borderRadius: 10,
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              color: configureOpen
                                ? "var(--sidebar-nav-active-icon)"
                                : "var(--sidebar-nav-icon)",
                              background: configureOpen
                                ? "var(--sidebar-icon-active-bg)"
                                : "transparent",
                            }}
                          >
                            <IconGear />
                          </span>
                          <span style={{ flex: 1, textAlign: "left" }}>
                            {t("page.apps.configure")}
                          </span>
                          <span
                            style={{
                              display: "flex",
                              color: "var(--sidebar-nav-icon)",
                              transition: "transform 0.18s ease",
                              transform: configureOpen
                                ? "rotate(90deg)"
                                : "rotate(0deg)",
                            }}
                          >
                            <svg
                              width="13"
                              height="13"
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth={2.3}
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            >
                              <polyline points="9 18 15 12 9 6" />
                            </svg>
                          </span>
                        </button>
                        {configureOpen && (
                          <div
                            style={{
                              display: "flex",
                              flexDirection: "column",
                              gap: 1,
                              marginTop: 4,
                              paddingLeft: 8,
                            }}
                          >
                            {configurationItems.map((item) =>
                              renderNavItem(item, { nested: true }),
                            )}
                          </div>
                        )}
                      </>
                    ) : (
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          alignItems: "center",
                          gap: 6,
                        }}
                      >
                        {configurationItems.map((item) => renderNavItem(item))}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* ── FOOTER ── */}
            <div
              style={{
                borderTop: "1px solid var(--sidebar-divider)",
                padding: sidebarCollapsed ? "8px 12px" : "8px 10px",
                display: "flex",
                flexDirection: "column",
                alignItems: "stretch",
                gap: 6,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: sidebarCollapsed ? "center" : "space-between",
                  gap: 6,
                }}
              >
                {user && (
                  <div
                    style={{
                      position: "relative",
                      flex: sidebarCollapsed ? undefined : 1,
                      minWidth: 0,
                    }}
                  >
                    <button
                      type="button"
                      onClick={() => {
                        setMode("workspace");
                        navigate("/account");
                        setMoreOpen(false);
                      }}
                      title={user.display_name || user.email || "Profile"}
                      aria-label={user.display_name || user.email || "Profile"}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        width: sidebarCollapsed ? 34 : "100%",
                        minWidth: 0,
                        padding: sidebarCollapsed ? "2px 0" : "4px 4px",
                        justifyContent: sidebarCollapsed ? "center" : undefined,
                        textDecoration: "none",
                        border: "none",
                        background: isActive("/account")
                          ? "var(--sidebar-control-bg)"
                          : "transparent",
                        borderRadius: 10,
                        transition: "background 0.2s",
                        cursor: "pointer",
                        fontFamily: "inherit",
                      }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.background =
                          "var(--sidebar-control-bg)";
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.background = isActive("/account")
                          ? "var(--sidebar-control-bg)"
                          : "transparent";
                      }}
                    >
                      <UserAvatar
                        name={user.display_name || user.email || "U"}
                        avatarUrl={user.avatar_url}
                        size={30}
                        style={{ color: "white" }}
                      />
                      {!sidebarCollapsed && (
                        <div style={{ flex: 1, minWidth: 0, textAlign: "left" }}>
                          <div
                            style={{
                              fontSize: 13,
                              fontWeight: 700,
                              color: "var(--text-strong)",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {user.display_name || user.email}
                          </div>
                          <div
                            style={{
                              fontSize: 12,
                              color: "var(--text-muted)",
                              marginTop: 0,
                            }}
                          >
                            {userRoleLabel}
                          </div>
                        </div>
                      )}
                    </button>
                  </div>
                )}
                {!sidebarCollapsed && (
                  <div style={{ position: "relative", order: 4 }}>
                  <button
                    ref={supportRestoreFocusRef}
                    data-tour="sidebar-more"
                    type="button"
                    onClick={() => {
                      setMoreOpen(!moreOpen);
                    }}
                    title={t("nav.more")}
                    aria-label={t("nav.more")}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 4,
                      padding: 0,
                      width: 24,
                      height: 24,
                      justifyContent: "center",
                      borderRadius: 7,
                      border: "none",
                      background:
                        moreOpen || isActive("/chat/history")
                          ? "var(--accent-soft)"
                          : "transparent",
                      color:
                        moreOpen || isActive("/chat/history")
                          ? "var(--accent)"
                          : "var(--text-muted)",
                      fontSize: 11,
                      fontWeight: 500,
                      cursor: "pointer",
                      transition: "all 0.2s",
                    }}
                    onMouseEnter={(e) => {
                      if (!moreOpen && !isActive("/chat/history")) {
                        e.currentTarget.style.color = "var(--text-strong)";
                        e.currentTarget.style.background = "var(--sidebar-control-bg)";
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!moreOpen && !isActive("/chat/history")) {
                        e.currentTarget.style.color = "var(--text-muted)";
                        e.currentTarget.style.background = "transparent";
                      }
                    }}
                  >
                    <IconMoreDots />
                  </button>
                  {moreOpen && (
                    <>
                      <div
                        style={{ position: "fixed", inset: 0, zIndex: 199 }}
                        onClick={() => setMoreOpen(false)}
                      />
                      <div
                        style={{
                          position: "fixed",
                          bottom: sidebarCollapsed ? 76 : 52,
                          left: sidebarCollapsed ? 56 : 12,
                          background: "var(--glass-panel)",
                          backdropFilter: "blur(24px)",
                          WebkitBackdropFilter: "blur(24px)",
                          borderRadius: 14,
                          border: "1px solid var(--glass-border)",
                          boxShadow:
                            "var(--shadow-lg), 0 0 0 1px var(--glass-hairline)",
                          padding: 6,
                          width: 224,
                          zIndex: 200,
                          animation: "dialog-in 0.15s ease-out",
                        }}
                      >
                        <div
                          style={{
                            padding: "6px 10px 8px",
                            fontSize: 10,
                            fontWeight: 700,
                            color: "var(--text-faint)",
                            textTransform: "uppercase",
                            letterSpacing: "0.05em",
                          }}
                        >
                          {t("nav.more")}
                        </div>
                        <button
                          type="button"
                          onClick={() => {
                            setMoreOpen(false);
                            setTourRequested(true);
                          }}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") event.stopPropagation();
                          }}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            width: "100%",
                            padding: "9px 10px",
                            borderRadius: 9,
                            border: "none",
                            background: "transparent",
                            color: "var(--text-default)",
                            fontSize: 13,
                            fontWeight: 500,
                            cursor: "pointer",
                            textAlign: "left",
                          }}
                          onMouseEnter={(e) => {
                            e.currentTarget.style.background = "var(--surface-muted)";
                          }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.background = "transparent";
                          }}
                        >
                          <IconHelp />
                          <span>{t("component.user_guide.title")}</span>
                        </button>
                        <Link
                          to="/settings"
                          state={{ returnTo: `${location.pathname}${location.search}` }}
                          onClick={() => {
                            setMode("workspace");
                            setMoreOpen(false);
                          }}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            padding: "9px 10px",
                            borderRadius: 9,
                            color: isActive("/settings")
                              ? "var(--accent)"
                              : "var(--text-default)",
                            background: isActive("/settings")
                              ? "rgba(13,148,136,0.08)"
                              : "transparent",
                            textDecoration: "none",
                            fontSize: 13,
                            fontWeight: isActive("/settings") ? 650 : 500,
                          }}
                          onMouseEnter={(e) => {
                            if (!isActive("/settings")) {
                              e.currentTarget.style.background = "var(--surface-muted)";
                            }
                          }}
                          onMouseLeave={(e) => {
                            if (!isActive("/settings")) {
                              e.currentTarget.style.background = "transparent";
                            }
                          }}
                        >
                          <IconGear />
                          <span>{t("nav.settings")}</span>
                        </Link>
                        <Link
                          to="/chat/history"
                          onClick={() => {
                            setMode("workspace");
                            setMoreOpen(false);
                          }}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            padding: "9px 10px",
                            borderRadius: 9,
                            color: isActive("/chat/history")
                              ? "var(--accent)"
                              : "var(--text-default)",
                            background: isActive("/chat/history")
                              ? "rgba(13,148,136,0.08)"
                              : "transparent",
                            textDecoration: "none",
                            fontSize: 13,
                            fontWeight: isActive("/chat/history") ? 650 : 500,
                          }}
                          onMouseEnter={(e) => {
                            if (!isActive("/chat/history")) {
                              e.currentTarget.style.background = "var(--surface-muted)";
                            }
                          }}
                          onMouseLeave={(e) => {
                            if (!isActive("/chat/history")) {
                              e.currentTarget.style.background = "transparent";
                            }
                          }}
                        >
                          <IconChatSmall />
                          <span>{t("nav.chatHistory")}</span>
                        </Link>
                        {supportTicketsEnabled && (
                          <button
                            type="button"
                            onClick={() => {
                              setMoreOpen(false);
                              setSupportOpen(true);
                            }}
                            aria-haspopup="dialog"
                            aria-controls="floating-support-panel"
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 10,
                              width: "100%",
                              padding: "9px 10px",
                              borderRadius: 9,
                              border: "none",
                              background: "transparent",
                              color: "var(--text-default)",
                              fontSize: 13,
                              fontWeight: 500,
                              cursor: "pointer",
                              textAlign: "left",
                            }}
                            onMouseEnter={(e) => {
                              e.currentTarget.style.background = "var(--surface-muted)";
                            }}
                            onMouseLeave={(e) => {
                              e.currentTarget.style.background = "transparent";
                            }}
                          >
                            <span
                              style={{
                                position: "relative",
                                display: "inline-flex",
                                alignItems: "center",
                              }}
                            >
                              <svg
                                width="14"
                                height="14"
                                viewBox="0 0 24 24"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="2"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              >
                                <path d="M4 13a8 8 0 0116 0" />
                                <path d="M18 13v3a2 2 0 01-2 2h-1" />
                                <path d="M6 13v3a2 2 0 002 2" />
                                <path d="M18 13h1a2 2 0 012 2v1a2 2 0 01-2 2h-1v-5z" />
                                <path d="M6 13H5a2 2 0 00-2 2v1a2 2 0 002 2h1v-5z" />
                              </svg>
                              {supportUnread > 0 && (
                                <span
                                  style={{
                                    position: "absolute",
                                    top: -4,
                                    right: -5,
                                    minWidth: 12,
                                    height: 12,
                                    padding: "0 3px",
                                    borderRadius: 6,
                                    background: "#5d7f77",
                                    color: "#fff",
                                    fontSize: 8,
                                    fontWeight: 700,
                                    display: "inline-flex",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    lineHeight: 1,
                                  }}
                                >
                                  {supportUnread > 9 ? "9+" : supportUnread}
                                </span>
                              )}
                            </span>
                            <span>{t("nav.support")}</span>
                          </button>
                        )}
                        <div
                          style={{
                            height: 1,
                            background: "var(--glass-border)",
                            margin: "6px 4px",
                          }}
                        />
                        <div
                          style={{
                            padding: "4px 10px 6px",
                            fontSize: 10,
                            fontWeight: 700,
                            color: "var(--text-faint)",
                            textTransform: "uppercase",
                            letterSpacing: "0.05em",
                          }}
                        >
                          {t("settings.language")}
                        </div>
                        {SUPPORTED_LOCALES.map((l) => {
                          const active = locale === l.code;
                          return (
                            <button
                              key={l.code}
                              disabled={localeSaving}
                              aria-pressed={active}
                              aria-busy={localeSaving}
                              onClick={() => {
                                void handleLocaleChange(l.code);
                                setMoreOpen(false);
                              }}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 10,
                                width: "100%",
                                padding: "8px 10px",
                                borderRadius: 8,
                                border: "none",
                                background: active
                                  ? "var(--accent-soft)"
                                  : "transparent",
                                cursor: localeSaving ? "wait" : "pointer",
                                opacity: localeSaving ? 0.6 : 1,
                                fontSize: 13,
                                transition: "background 0.1s",
                                fontWeight: active ? 600 : 400,
                                color: active ? "var(--accent)" : "var(--text-default)",
                              }}
                              onMouseEnter={(e) => {
                                if (!active)
                                  e.currentTarget.style.background = "var(--surface-muted)";
                              }}
                              onMouseLeave={(e) => {
                                e.currentTarget.style.background = active
                                  ? "var(--accent-soft)"
                                  : "transparent";
                              }}
                            >
                              <img
                                src={`https://flagcdn.com/w20/${({ en: "us", zh: "cn", es: "es", fr: "fr", de: "de" } as const)[l.code]}.png`}
                                width="18"
                                height="13"
                                alt=""
                                style={{ borderRadius: 2, objectFit: "cover" }}
                              />
                              <span style={{ flex: 1, textAlign: "left" }}>
                                {l.name}
                              </span>
                              {active && (
                                <svg
                                  width="14"
                                  height="14"
                                  viewBox="0 0 24 24"
                                  fill="currentColor"
                                >
                                  <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                                </svg>
                              )}
                            </button>
                          );
                      })}
                    </div>
                  </>
                )}
                  </div>
                )}

                {/* Notifications bell */}
                {!sidebarCollapsed && (
                  <Link
                    to="/notifications"
                    onClick={() => setMode("workspace")}
                    title={t("nav.notifications")}
                    aria-label={t("nav.notifications")}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      order: 1,
                      padding: 0,
                      width: 24,
                      height: 24,
                      borderRadius: 7,
                      color: "var(--text-muted)",
                      textDecoration: "none",
                      fontSize: 11,
                      fontWeight: 500,
                      position: "relative",
                      transition: "color 0.2s",
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.color = "var(--text-strong)";
                      e.currentTarget.style.background = "var(--sidebar-control-bg)";
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.color = "var(--text-muted)";
                      e.currentTarget.style.background = "transparent";
                    }}
                  >
                    <span
                      style={{
                        position: "relative",
                        display: "flex",
                        alignItems: "center",
                      }}
                    >
                      <IconBell />
                      {unreadCount > 0 && (
                        <span
                          style={{
                            position: "absolute",
                            top: 0,
                            right: 0,
                            width: 6,
                            height: 6,
                            borderRadius: "50%",
                            background: "#ef4444",
                            boxShadow: "0 0 0 2px var(--chrome-surface)",
                          }}
                        />
                      )}
                    </span>
                  </Link>
                )}
              </div>
            </div>
          </div>

        </aside>
        )}

        <main
          className={`app-main-shell flex-1 min-w-0 overflow-auto relative z-10 p-3 ${isSettingsRoute ? "pl-3" : "pl-0"} ${isTasksRoute ? "app-main-shell--flush" : ""}`}
        >
          {/* Mobile hamburger */}
          {!isSettingsRoute && (
          <div className="mobile-chat-actions">
          <button
            className="mobile-hamburger"
            onClick={() => setMobileOpen(true)}
            style={{
              display: "none",
              width: 40,
              height: 40,
              borderRadius: 12,
              background: "var(--surface-panel)",
              border: "1px solid var(--glass-border)",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--text-muted)",
              cursor: "pointer",
              flexShrink: 0,
            }}
          >
            <svg
              width="24"
              height="24"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
          {sidebarMode === "chat" && (
            <button
              type="button"
              className="mobile-chat-new"
              onClick={openNewConversation}
              aria-label={getLocale() === "zh" ? "新建对话" : "New conversation"}
              title={getLocale() === "zh" ? "新建对话" : "New conversation"}
            >
              <IconPlus />
            </button>
          )}
          </div>
          )}

          {showProfessionalPageNotice ? (
            <div className="app-content-panel glass-panel flex h-full min-w-0 items-center justify-center p-6">
              <EmptyState
                title={t("page.app_layout.professional_page")}
                description={t("page.app_layout.professional_page_description")}
                action={(
                  <div className="flex flex-wrap justify-center gap-2">
                    <Button onClick={() => navigate(simpleChatPath)}>{t("nav.chat")}</Button>
                    <Button variant="outline" onClick={() => navigate("/settings?tab=appearance", {
                      state: { returnTo: `${location.pathname}${location.search}` },
                    })}>
                      {t("page.app_layout.open_display_settings")}
                    </Button>
                  </div>
                )}
              />
            </div>
          ) : mode === "chat" ? (
            <div
              className="app-chat-shell"
              style={{
                flex: 1,
                minWidth: 0,
                display: "flex",
                flexDirection: "column",
                height: "100%",
                borderRadius: 24,
                overflow: "hidden",
              }}
            >
              {activeConvType === "compose" ? (
                <Suspense fallback={<ChatSurfaceLoading />}>
                  <ChatRoomNewConversation key={newConversationKind} initialKind={newConversationKind} rooms={humanChatRooms} agents={chatAgentsList} agentsLoading={chatAgentsLoading} currentUserId={user?.id || ""} onClose={openManorChat} onCreated={(room) => openHumanRoom(room.id)} onSelectAgent={openAgentDmFromCompose} />
                </Suspense>
              ) : activeConvType === "workspace-draft" ? (
                <Suspense fallback={<div className="flex-1 min-h-0 p-4" aria-busy="true"><InlineRowsSkeleton rows={3} dense /></div>}>
                  <WorkspaceDraftChat embeddedInChat />
                </Suspense>
              ) : activeConvType === "room" ? (
                <Suspense fallback={<ChatSurfaceLoading />}>
                  <ChatRoomConversation
                    key={activeConvId}
                    roomId={activeConvId}
                    currentUserId={user?.id || ""}
                    requestedAction={roomSidebarAction?.roomId === activeConvId ? roomSidebarAction : undefined}
                  />
                </Suspense>
              ) : activeConvType === "operation" ? (
                (() => {
                  const activeWorkspace = activeChatWorkspaceQuery.data || workspaceList.find(
                    (w) => w.id === activeConvId,
                  );
                  if (activeChatWorkspaceUnavailable || unavailableWorkspaceId === activeConvId) {
                    return (
                      <div className="flex flex-1 items-center justify-center p-6" role="alert">
                        <div className="text-center">
                          <h2 className="m-0 text-lg font-semibold text-stone-800">
                            {t("page.workspace_detail.not_found")}
                          </h2>
                          <p className="mb-4 mt-2 text-sm text-stone-500">
                            {t("page.workspace_detail.not_found_desc")}
                          </p>
                          <Button
                            variant="outline"
                            onClick={() => {
                              setUnavailableWorkspaceId(null);
                              navigate("/workspaces");
                            }}
                          >
                            {t("page.workspace_detail.back_to_workspaces")}
                          </Button>
                        </div>
                      </div>
                    );
                  }
                  if (!activeWorkspace) {
                    if (activeChatWorkspaceQuery.isError) return (
                      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6" role="alert">
                        <p>{t("component.workspace_app_view.error")}</p>
                        <Button variant="outline" onClick={() => void activeChatWorkspaceQuery.refetch()}>
                          {t("component.workspace_app_view.retry")}
                        </Button>
                      </div>
                    );
                    return (
                      <div className="flex-1 min-h-0 p-4" aria-busy="true">
                        <InlineRowsSkeleton rows={3} dense />
                      </div>
                    );
                  }
                  return (
                    <Suspense fallback={<ChatSurfaceLoading />}>
                      <WorkspaceChat
                        key={activeConvId}
                        workspaceId={activeConvId}
                        workspace={activeWorkspace}
                        workspaceName={activeWorkspace?.name}
                      />
                    </Suspense>
                  );
                })()
              ) : (
                (() => {
                  const info = getChatInfo();
                  const dmAgent =
                    activeConvType === "dm"
                      ? [...chatAgentsList, ...hiredAgents].find(
                          (a) => a.id === activeDmAgentId,
                        )
                      : null;
                  if (activeConvType === "dm" && !dmAgent) {
                    return <div className="flex min-h-0 flex-1 items-center justify-center p-6">
                      {!catalogScope || chatAgentsLoading ? <InlineRowsSkeleton rows={3} dense /> : <EmptyState
                        title={t("page.app_layout.agent_unavailable")}
                        description={t("page.app_layout.agent_unavailable_desc")}
                        action={<Button variant="outline" onClick={openManorChat}>{t("nav.chat")}</Button>}
                      />}
                    </div>;
                  }
                  return (
                    <Suspense fallback={<ChatSurfaceLoading />}>
                      <EmbeddedChat
                        conversationId={activeConvId}
                        title={info.title}
                        subtitle={info.subtitle}
                        agents={info.agents}
                        avatarUrl={dmAgent?.avatar_url}
                        agentId={dmAgent?.id}
                        onConversationResolved={
                          activeConvType === "manor"
                            ? handleManorConversationResolved
                            : undefined
                        }
                        onNewConversation={
                          activeConvType === "manor"
                            ? startNewManorSession
                            : undefined
                        }
                        showWorkspaceIntro={activeConvType === "manor"}
                      />
                    </Suspense>
                  );
                })()
              )}
            </div>
          ) : (
            <div className={`app-content-panel glass-panel flex h-full min-w-0 flex-col overflow-hidden p-0 ${isSettingsRoute ? "app-content-panel--settings" : ""} ${isTasksRoute ? "app-content-panel--flush" : ""}`}>
              <PageHeaderBoundary>
                <div className={`app-route-content min-h-0 min-w-0 flex-1 ${
                  isSettingsRoute
                    ? "app-route-content--settings overflow-auto p-0"
                    : isEditorShellRoute
                      ? "app-route-content--editor overflow-hidden p-0"
                      : "overflow-auto px-6 pb-6 pt-2"
                }`}>
                  <Outlet />
                </div>
              </PageHeaderBoundary>
            </div>
          )}
        </main>

        {/* Toast notification */}
        {location.pathname === "/chat" && new URLSearchParams(location.search).get("invite") && (
          <Suspense fallback={null}>
            <ChatInviteAcceptance
              token={new URLSearchParams(location.search).get("invite") || ""}
              onClose={() => navigate("/chat", { replace: true })}
              onAccepted={(room) => openHumanRoom(room.id)}
            />
          </Suspense>
        )}
        {toast && (
          <div className="fixed left-3 right-3 top-4 z-50 glass-panel px-4 py-3 text-sm text-stone-700 animate-fade-in sm:left-auto sm:right-4 sm:max-w-sm">
            <div className="flex items-center gap-2">
              <span className="inline-block h-2 w-2 rounded-full bg-emerald-500 shrink-0" />
              {toast}
              <button
                onClick={() => setToast(null)}
                className="ml-auto text-stone-400 hover:text-stone-600"
              >
                &times;
              </button>
            </div>
          </div>
        )}

        {/* Floating chat — visible in workspace mode, hidden on /chat */}
        {!simpleMode && mode === "workspace" &&
          !location.pathname.startsWith("/chat")
          && (
          <FloatingChat />
        )}

        <TextSelectionToolbar />

        {/* Global agent create/edit modal — opened via openAgentEditModal()
            from any page (Agents.tsx, WorkspaceDetail.tsx, WorkspaceChat).
            Mounted here (inside the Router tree, under AppLayout) rather
            than in main.tsx because it calls useNavigate(). */}
        {agentEditModalOpen && (
          <Suspense fallback={null}>
            <AgentEditModal />
          </Suspense>
        )}

        <ConfirmDialog
          open={Boolean(deleteWorkspaceTarget)}
          onClose={() => {
            if (deleteWorkspaceFromSidebar.isPending) return;
            setDeleteWorkspaceTarget(null);
            setDeleteWorkspaceError("");
          }}
          onConfirm={() => {
            if (deleteWorkspaceTarget?.can_delete) {
              deleteWorkspaceFromSidebar.mutate(deleteWorkspaceTarget.id);
            }
          }}
          title={t("page.workspaces.delete_workspace")}
          message={t("page.workspaces.delete_workspace_msg", { days: "30" })}
          confirmLabel={t("page.workspaces.move_to_trash")}
          cancelLabel={t("action.cancel")}
          danger
          loading={deleteWorkspaceFromSidebar.isPending}
          closeOnConfirm={false}
          error={deleteWorkspaceError || undefined}
        />

        <ConfirmDialog
          open={Boolean(deleteConversationTarget)}
          onClose={() => {
            if (deleteChatConversation.isPending) return;
            setDeleteConversationTarget(null);
            setDeleteConversationError("");
          }}
          onConfirm={() => {
            if (deleteConversationTarget) {
              deleteChatConversation.mutate(deleteConversationTarget.id);
            }
          }}
          title={t("page.chat_history.delete_title")}
          message={t("page.app_layout.delete_manor_session_message", {
            title:
              deleteConversationTarget?.title ||
              t("page.chat_history.untitled"),
          })}
          confirmLabel={t("action.delete")}
          cancelLabel={t("action.cancel")}
          danger
          loading={deleteChatConversation.isPending}
          closeOnConfirm={false}
          error={deleteConversationError || undefined}
        />


        {interfaceModeDialogOpen && (
          <Suspense fallback={null}>
            <InterfaceModeDialog />
          </Suspense>
        )}
        {user && !isTourSuppressedPath(location.pathname) && (
          <OnboardingTour enabled={false} />
        )}

        {/* Support drawer — talks to the platform team */}
        {supportTicketsEnabled && (
          <SupportPanel
            open={supportOpen}
            onClose={() => setSupportOpen(false)}
            restoreFocusRef={supportRestoreFocusRef}
          />
        )}
      </div>
    </div>
  );
}
