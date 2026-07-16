import {
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
  type ReactNode,
} from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../stores/auth";
import { usePageViewTracking, useWebSocket } from "../lib/websocket";
import { useConfigStore } from "../stores/config";
import {
  t,
  getLocale,
  setLocale,
  SUPPORTED_LOCALES,
  type Locale,
} from "../lib/i18n";
import { api } from "../lib/api";
import type { Workspace, Agent } from "../lib/types";
import EmbeddedChat from "../components/EmbeddedChat";
import FloatingChat from "../components/FloatingChat";
import WorkspaceChat from "../components/WorkspaceChat";
import SupportPanel, {
  useSupportUnreadCount,
} from "../components/SupportPanel";
import UserAvatar from "../components/ui/UserAvatar";
import AgentAvatar from "../components/ui/AgentAvatar";
import WorkspaceIconTile from "../components/ui/WorkspaceIcon";
import { useWorkspaceFilter } from "../stores/workspace";
import OnboardingTour, { isTourSuppressedPath } from "../components/OnboardingTour";
import { getAgentDescription } from "../lib/localizedContent";

type AppMode = "workspace" | "chat";

const EMPTY_WORKSPACES: Workspace[] = [];

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

const IconChatBubble = () => (
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
    <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
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

const IconWorkspaceGraph = () => (
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
    <circle cx="12" cy="12" r="3.5" />
    <circle cx="5" cy="6" r="2.5" />
    <circle cx="19" cy="6" r="2.5" />
    <circle cx="6" cy="19" r="2.5" />
    <circle cx="18" cy="18" r="2.5" />
    <path d="M7.1 7.7l2.7 2.1M16.9 7.7l-2.7 2.1M8.1 17.2l2.2-2.5M15.8 15l1.5 1.5" />
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

const IconAppsGrid = () => (
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

const IconReport = () => (
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
    <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
    <polyline points="14 2 14 8 20 8" />
    <line x1="16" y1="13" x2="8" y2="13" />
    <line x1="16" y1="17" x2="8" y2="17" />
    <polyline points="10 9 9 9 8 9" />
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
  },
  {
    path: "/tasks",
    label: "Tasks",
    i18nKey: "nav.tasks",
    icon: <IconChecklist />,
  },
];

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
];

const baseConfigurationItems: NavItem[] = [
  {
    path: "/agents",
    label: "Agents",
    i18nKey: "nav.agents",
    icon: <IconAgent />,
    tourKey: "nav-agents",
  },
  {
    path: "/integrations",
    label: "Integrations",
    i18nKey: "nav.integrations",
    icon: <IconConnection />,
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

type HelpCopy = {
  ariaLabel: string;
  closeLabel: string;
  kicker: string;
  title: string;
  intro: string;
  workspaceMode: string;
  workspaceModeDescription: string;
  chatMode: string;
  chatModeDescription: string;
  tip: string;
  close: string;
  startTour: string;
  sections: Array<{
    label: string;
    eyebrow: string;
    description: string;
    items: Array<{ title: string; description: string; use: string }>;
  }>;
};

const helpSectionIcons: ReactNode[][] = [
  [<IconGrid4 />, <IconChecklist />, <IconChatBubble />],
  [<IconWorkspaceGraph />, <IconLibraryStack />, <IconPeople />],
  [<IconAgent />, <IconConnection />, <IconLayers />, <IconAppsGrid />],
];

// Partial<Record<Locale, ...>> so newly added locales (e.g. `de`) don't
// force a 300-line translation drop just to satisfy the type checker —
// the consumer below falls back to `helpCopy.en` for any missing locale.
const helpCopy: Partial<Record<Locale, HelpCopy>> = {
  en: {
    ariaLabel: "Manor help",
    closeLabel: "Close help",
    kicker: "Manor Help",
    title: "Understand the workspace in one minute",
    intro:
      "Use quick chat for one-off asks. Create a workspace when the goal needs memory, files, tasks, agents, integrations, or follow-up.",
    workspaceMode: "Workspace Mode",
    workspaceModeDescription:
      "The operating layer: Manor plans the work, reads context, creates artifacts, coordinates agents, and keeps progress visible.",
    chatMode: "Manor AI Chat",
    chatModeDescription:
      "The fast lane: ask, reference people with @, attach files with #, and open artifacts when Manor generates work.",
    tip: "Tip: keep the main menu focused on daily work; put configuration deeper unless the user is setting up Manor.",
    close: "Close",
    startTour: "Start guided tour",
    sections: [
      {
        label: "Operate",
        eyebrow: "Daily work",
        description:
          "Where you check status, open tasks, and continue conversations.",
        items: [
          {
            title: "Dashboard",
            description:
              "Shows workspace progress, alerts, metrics, and items that need attention.",
            use: "Use it daily to see what Manor finished and what is blocked.",
          },
          {
            title: "Tasks",
            description:
              "Tracks tasks, owners, approvals, deadlines, and execution status.",
            use: "Use it when a goal needs clear action items and follow-through.",
          },
          {
            title: "Chat",
            description:
              "Keeps conversations with Manor AI, workspaces, and agents.",
            use: "Use it to continue context or start a fast new request.",
          },
        ],
      },
      {
        label: "Library",
        eyebrow: "Long-term memory",
        description:
          "Where Manor keeps people, files, and workspace memory it can reuse.",
        items: [
          {
            title: "Workspaces",
            description:
              "Goal-based operating rooms with files, tasks, agents, and artifacts.",
            use: "Use it for launches, fundraising, hiring, content growth, or customer operations.",
          },
          {
            title: "Knowledge",
            description:
              "Stores documents, images, sheets, decks, pages, and other reference material.",
            use: "Use it when Manor should write, analyze, or generate from existing files.",
          },
          {
            title: "Team",
            description:
              "Manages members, roles, permissions, and collaboration.",
            use: "Use it when multiple people need access or approvals.",
          },
        ],
      },
      {
        label: "Configure",
        eyebrow: "Capabilities",
        description:
          "Advanced setup that can sit one level deeper so the main menu stays clean.",
        items: [
          {
            title: "Agents",
            description:
              "Configures specialist agents for sales, research, content, design, operations, and more.",
            use: "Use it when fixed roles should participate in a workspace over time.",
          },
          {
            title: "Integrations",
            description:
              "Connects tools like Gmail, Slack, Drive, Calendar, and Twilio.",
            use: "Use it when Manor needs real context or needs to send results back into your workflow.",
          },
          {
            title: "Skills",
            description:
              "Manages reusable procedures, tools, and execution methods.",
            use: "Use it to turn repeated work into SOPs, like weekly reports or competitor scans.",
          },
          {
            title: "Apps",
            description:
              "A future home for installable or richer work surfaces.",
            use: "Use it as a second-level entry for vertical app experiences.",
          },
        ],
      },
    ],
  },
  zh: {
    ariaLabel: "Manor 帮助",
    closeLabel: "关闭帮助",
    kicker: "Manor 帮助",
    title: "一分钟理解 Workspace 怎么用",
    intro:
      "一次性的事情可以直接聊天；如果目标需要记忆、文件、任务、智能体、集成或持续跟进，就创建 workspace。",
    workspaceMode: "Workspace 模式",
    workspaceModeDescription:
      "这是操作层：Manor 会理解上下文、规划工作、生成产物、协调智能体，并让进展保持可见。",
    chatMode: "Manor AI 对话",
    chatModeDescription:
      "这是快速通道：直接提问，用 @ 引用人或 agent，用 # 附加文件，生成后打开 artifacts 查看结果。",
    tip: "建议：主菜单保持日常工作优先；配置类能力放到第二层，用户需要设置 Manor 时再进入。",
    close: "关闭",
    startTour: "开始引导",
    sections: [
      {
        label: "Operate",
        eyebrow: "日常推进",
        description: "这里是每天看状态、开任务、继续对话的地方。",
        items: [
          {
            title: "Dashboard",
            description:
              "查看 workspace 的进度、提醒、指标和需要你处理的事项。",
            use: "适合每天打开，快速知道 Manor 已经做了什么、哪里卡住了。",
          },
          {
            title: "Tasks",
            description: "管理任务、owner、审批、截止时间和执行状态。",
            use: "适合把一个目标拆成行动项，并持续跟踪推进。",
          },
          {
            title: "Chat",
            description: "查看和 Manor AI、workspace、agent 的对话历史。",
            use: "适合继续之前的上下文，或者快速发起一次新请求。",
          },
        ],
      },
      {
        label: "Library",
        eyebrow: "长期记忆",
        description: "这里保存 Manor 可以持续引用的人、文件、workspace 记忆。",
        items: [
          {
            title: "Workspaces",
            description:
              "围绕一个长期目标建立操作空间，里面有文件、任务、agent 和产出。",
            use: "适合 launch、融资、招聘、内容增长、客户运营这类需要 follow-up 的目标。",
          },
          {
            title: "Knowledge",
            description:
              "上传和管理文档、图片、表格、deck、网页等可被 AI 引用的材料。",
            use: "适合让 Manor 根据已有资料写报告、做 PPT、分析文件或生成新内容。",
          },
          {
            title: "Team",
            description: "管理成员、角色、权限和协作关系。",
            use: "适合多人协作，或者明确谁负责批准、查看、执行某些任务。",
          },
        ],
      },
      {
        label: "Configure",
        eyebrow: "能力配置",
        description:
          "这里放更高级的能力开关，默认可以隐藏到第二层，避免主菜单太重。",
        items: [
          {
            title: "Agents",
            description:
              "配置专业 agent，例如销售、研究、内容、设计、运营等角色。",
            use: "适合需要固定角色长期参与 workspace，而不是一次性聊天。",
          },
          {
            title: "Integrations",
            description:
              "连接 Gmail、Slack、Drive、Calendar、Twilio 等外部工具。",
            use: "适合让 Manor 读取真实上下文，或者把结果发送回你的工作流。",
          },
          {
            title: "Skills",
            description: "管理可复用的流程、工具和执行方法。",
            use: "适合把常做的事情沉淀为 SOP，例如周报、竞品分析、邮件外呼。",
          },
          {
            title: "Apps",
            description: "未来可安装或打开的工作应用入口。",
            use: "适合承载更完整的垂直工具体验，目前可以作为第二层入口。",
          },
        ],
      },
    ],
  },
  es: {
    ariaLabel: "Ayuda de Manor",
    closeLabel: "Cerrar ayuda",
    kicker: "Ayuda de Manor",
    title: "Entiende el workspace en un minuto",
    intro:
      "Usa el chat para preguntas puntuales. Crea un workspace cuando el objetivo necesite memoria, archivos, tareas, agentes, integraciones o seguimiento.",
    workspaceMode: "Modo Workspace",
    workspaceModeDescription:
      "La capa operativa: Manor planifica el trabajo, lee contexto, crea artefactos, coordina agentes y muestra el progreso.",
    chatMode: "Chat de Manor AI",
    chatModeDescription:
      "La vía rápida: pregunta, referencia personas con @, adjunta archivos con # y abre artefactos cuando Manor genera trabajo.",
    tip: "Consejo: mantén el menú principal centrado en el trabajo diario; deja la configuración un nivel más profundo.",
    close: "Cerrar",
    startTour: "Iniciar guía",
    sections: [
      {
        label: "Operar",
        eyebrow: "Trabajo diario",
        description:
          "Donde revisas estado, abres tareas y continúas conversaciones.",
        items: [
          {
            title: "Panel",
            description:
              "Muestra progreso, alertas, métricas y elementos que requieren atención.",
            use: "Úsalo cada día para ver qué terminó Manor y qué está bloqueado.",
          },
          {
            title: "Tareas",
            description:
              "Sigue tareas, responsables, aprobaciones, fechas y estado de ejecución.",
            use: "Úsalo cuando un objetivo necesite acciones claras.",
          },
          {
            title: "Chat",
            description:
              "Guarda conversaciones con Manor AI, workspaces y agentes.",
            use: "Úsalo para continuar contexto o iniciar una solicitud rápida.",
          },
        ],
      },
      {
        label: "Biblioteca",
        eyebrow: "Memoria",
        description:
          "Donde Manor conserva personas, archivos y memoria de workspace.",
        items: [
          {
            title: "Workspaces",
            description:
              "Espacios por objetivo con archivos, tareas, agentes y artefactos.",
            use: "Úsalo para lanzamientos, fundraising, contratación, contenido u operaciones.",
          },
          {
            title: "Conocimiento",
            description:
              "Guarda documentos, imágenes, hojas, decks, páginas y referencias.",
            use: "Úsalo cuando Manor deba analizar o generar a partir de archivos.",
          },
          {
            title: "Equipo",
            description: "Gestiona miembros, roles, permisos y colaboración.",
            use: "Úsalo cuando varias personas necesiten acceso o aprobaciones.",
          },
        ],
      },
      {
        label: "Configurar",
        eyebrow: "Capacidades",
        description:
          "Configuración avanzada que puede vivir un nivel más profundo.",
        items: [
          {
            title: "Agentes",
            description:
              "Configura agentes especialistas para ventas, investigación, contenido, diseño y operaciones.",
            use: "Úsalo cuando roles fijos deban participar en un workspace.",
          },
          {
            title: "Integraciones",
            description:
              "Conecta herramientas como Gmail, Slack, Drive, Calendar y Twilio.",
            use: "Úsalo cuando Manor necesite contexto real o enviar resultados.",
          },
          {
            title: "Skills",
            description:
              "Gestiona procedimientos, herramientas y métodos reutilizables.",
            use: "Úsalo para convertir trabajo repetido en SOPs.",
          },
          {
            title: "Apps",
            description: "Futuro hogar para experiencias y apps instalables.",
            use: "Úsalo como entrada secundaria para apps verticales.",
          },
        ],
      },
    ],
  },
};

function ManorHelpModal({
  locale,
  onClose,
  onStartTour,
}: {
  locale: Locale;
  onClose: () => void;
  onStartTour: () => void;
}) {
  // `en` is always populated; non-null assertion lets TS see through the
  // Partial<Record<Locale,...>> declared above.
  const copy = (helpCopy[locale] || helpCopy.en)!;
  return (
    <div
      className="manor-help-overlay"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10020,
        background: "var(--modal-overlay-bg)",
        backdropFilter: "blur(10px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={copy.ariaLabel}
        className="manor-dialog manor-help-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(920px, calc(100vw - 32px))",
          maxHeight: "min(780px, calc(100vh - 48px))",
          overflow: "auto",
          borderRadius: 28,
          background: "var(--modal-bg)",
          border: "1px solid var(--modal-border)",
          boxShadow: "var(--modal-shadow)",
          color: "var(--text-default)",
        }}
      >
        <div
          style={{
            padding: "26px 28px 18px",
            borderBottom: "1px solid var(--modal-border)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "flex-start",
              justifyContent: "space-between",
              gap: 16,
            }}
          >
            <div>
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 800,
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  color: "var(--accent)",
                  marginBottom: 8,
                }}
              >
                {copy.kicker}
              </div>
              <h2
                style={{
                  margin: 0,
                  fontSize: 26,
                  lineHeight: 1.15,
                  color: "var(--text-strong)",
                  letterSpacing: "-0.03em",
                }}
              >
                {copy.title}
              </h2>
              <p
                style={{
                  margin: "10px 0 0",
                  maxWidth: 680,
                  fontSize: 14,
                  lineHeight: 1.6,
                  color: "var(--text-muted)",
                }}
              >
                {copy.intro}
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label={copy.closeLabel}
              style={{
                width: 34,
                height: 34,
                borderRadius: 12,
                border: "1px solid var(--modal-border)",
                background: "var(--modal-control-bg)",
                color: "var(--text-muted)",
                cursor: "pointer",
                display: "grid",
                placeItems: "center",
                flexShrink: 0,
              }}
            >
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                strokeLinecap="round"
              >
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
              gap: 12,
              marginTop: 20,
            }}
          >
            <div
              style={{
                border: "1px solid var(--accent-soft-border)",
                background: "var(--accent-soft)",
                borderRadius: 18,
                padding: 16,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  color: "var(--accent)",
                  fontWeight: 800,
                  fontSize: 14,
                }}
              >
                <span
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: 12,
                    background: "var(--accent)",
                    color: "var(--surface-app)",
                    display: "grid",
                    placeItems: "center",
                  }}
                >
                  <IconWorkspaceGraph />
                </span>
                {copy.workspaceMode}
              </div>
              <p
                style={{
                  margin: "10px 0 0",
                  color: "var(--text-muted)",
                  fontSize: 13,
                  lineHeight: 1.55,
                }}
              >
                {copy.workspaceModeDescription}
              </p>
            </div>
            <div
              style={{
                border: "1px solid var(--modal-border)",
                background: "var(--modal-muted-bg)",
                borderRadius: 18,
                padding: 16,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  color: "var(--text-strong)",
                  fontWeight: 800,
                  fontSize: 14,
                }}
              >
                <span
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: 12,
                    background: "var(--modal-sunken-bg)",
                    color: "var(--text-muted)",
                    display: "grid",
                    placeItems: "center",
                  }}
                >
                  <IconChatBubble />
                </span>
                {copy.chatMode}
              </div>
              <p
                style={{
                  margin: "10px 0 0",
                  color: "var(--text-muted)",
                  fontSize: 13,
                  lineHeight: 1.55,
                }}
              >
                {copy.chatModeDescription}
              </p>
            </div>
          </div>
        </div>

        <div style={{ padding: 28, display: "grid", gap: 18 }}>
          {copy.sections.map((section, sectionIndex) => (
            <section
              key={section.label}
              style={{
                border: "1px solid var(--modal-border)",
                background: "var(--modal-muted-bg)",
                borderRadius: 22,
                padding: 18,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "space-between",
                  gap: 12,
                  marginBottom: 14,
                }}
              >
                <div>
                  <div
                    style={{
                      fontSize: 11,
                      fontWeight: 800,
                      color: "var(--accent)",
                      letterSpacing: "0.1em",
                      textTransform: "uppercase",
                    }}
                  >
                    {section.eyebrow}
                  </div>
                  <h3
                    style={{
                      margin: "4px 0 0",
                      fontSize: 18,
                      color: "var(--text-strong)",
                    }}
                  >
                    {section.label}
                  </h3>
                </div>
                <p
                  style={{
                    margin: 0,
                    maxWidth: 420,
                    color: "var(--text-muted)",
                    fontSize: 13,
                    lineHeight: 1.5,
                    textAlign: "right",
                  }}
                >
                  {section.description}
                </p>
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
                  gap: 10,
                }}
              >
                {section.items.map((item, itemIndex) => (
                  <article
                    key={item.title}
                    style={{
                      border: "1px solid #edf2f7",
                      borderColor: "var(--modal-border)",
                      borderRadius: 16,
                      padding: 14,
                      background: "var(--modal-sunken-bg)",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 9,
                        marginBottom: 8,
                      }}
                    >
                      <span
                        style={{
                          width: 28,
                          height: 28,
                          borderRadius: 10,
                          display: "grid",
                          placeItems: "center",
                          background: "var(--sidebar-nav-active-bg)",
                          color: "var(--accent)",
                        }}
                      >
                        {helpSectionIcons[sectionIndex]?.[itemIndex]}
                      </span>
                      <strong style={{ color: "var(--text-strong)", fontSize: 14 }}>
                        {item.title}
                      </strong>
                    </div>
                    <p
                      style={{
                        margin: "0 0 8px",
                        color: "var(--text-muted)",
                        fontSize: 12.5,
                        lineHeight: 1.5,
                      }}
                    >
                      {item.description}
                    </p>
                    <p
                      style={{
                        margin: 0,
                        color: "var(--text-faint)",
                        fontSize: 12,
                        lineHeight: 1.45,
                      }}
                    >
                      {item.use}
                    </p>
                  </article>
                ))}
              </div>
            </section>
          ))}
        </div>

        <div
          style={{
            padding: "0 28px 26px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13 }}>
            {copy.tip}
          </p>
          <div style={{ display: "flex", gap: 10 }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                height: 38,
                padding: "0 16px",
                borderRadius: 999,
                border: "1px solid var(--modal-border)",
                background: "var(--modal-control-bg)",
                color: "var(--text-default)",
                fontWeight: 700,
                cursor: "pointer",
              }}
            >
              {copy.close}
            </button>
            <button
              type="button"
              onClick={onStartTour}
              style={{
                height: 38,
                padding: "0 18px",
                borderRadius: 999,
                border: "1px solid var(--accent)",
                background: "var(--accent)",
                color: "var(--surface-app)",
                fontWeight: 800,
                cursor: "pointer",
                boxShadow: "0 12px 26px var(--accent-ring)",
              }}
            >
              {copy.startTour}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
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
  const [searchOpen, setSearchOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [configureOpen, setConfigureOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [supportOpen, setSupportOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  /* ── Chat Mode state ── */
  const [activeConvId, setActiveConvId] = useState<string>("manor-ai");
  const [activeConvType, setActiveConvType] = useState<
    "manor" | "operation" | "dm"
  >("manor");
  const [activeDmAgentId, setActiveDmAgentId] = useState<string | null>(null);
  const [chatWorkspaces, setChatWorkspaces] = useState<Workspace[]>([]);
  const activeWorkspaceId = useWorkspaceFilter((s) => s.activeWorkspaceId);
  const setActiveWorkspaceId = useWorkspaceFilter(
    (s) => s.setActiveWorkspaceId,
  );
  const [chatAgentsList, setChatAgentsList] = useState<Agent[]>([]);
  const [hiredAgents, setHiredAgents] = useState<Agent[]>([]);
  const [dmConversationByAgentId, setDmConversationByAgentId] = useState<
    Record<string, string>
  >({});
  const [convSearchQuery, setConvSearchQuery] = useState("");
  // Counts shown in chat rows: unresolved chat actions (proposal cards + HITL).
  const [actionCounts, setActionCounts] = useState<Record<string, number>>({});

  /* Load app config (deployment mode, feature flags) */
  useEffect(() => {
    useConfigStore.getState().load();
  }, []);
  const deploymentMode = useConfigStore((s) => s.deployment_mode);
  const configLoaded = useConfigStore((s) => s.loaded);
  const supportTicketsEnabled = useConfigStore(
    (s) => s.support_tickets_enabled,
  );
  const supportUnread =
    useSupportUnreadCount(supportTicketsEnabled).data?.count || 0;
  const configurationItems = useMemo(() => {
    const items = [...baseConfigurationItems];
    return items;
  }, [
  ]);
  const { data: workspaceList = EMPTY_WORKSPACES } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
    staleTime: 60_000,
  });

  /* Load workspaces for sidebar context switcher (both modes) */
  const refreshWorkspaces = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
  }, [queryClient]);
  useEffect(() => {
    setChatWorkspaces(workspaceList);
    // Seed counts from backend unresolved state. Chat rows include proposal
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
    const onRefresh = () => refreshWorkspaces();
    window.addEventListener("manor:workspace-actions-refresh", onRefresh);
    return () =>
      window.removeEventListener("manor:workspace-actions-refresh", onRefresh);
  }, [refreshWorkspaces]);
  // Also refresh when entering chat mode (picks up newly created workspaces)
  useEffect(() => {
    if (mode === "chat") refreshWorkspaces();
  }, [mode, refreshWorkspaces]);

  /* Load chat-specific data when entering chat mode */
  useEffect(() => {
    if (mode !== "chat") return;
    api.agents
      .list()
      .then(setChatAgentsList)
      .catch(() => {});
    // DM agents: only show agents user has an active conversation with
    api.chat
      .listConversations()
      .then((convs: any[]) => {
        const dmAgentIds = new Set<string>();
        const dmConversations: Record<string, string> = {};
        convs
          .filter((c: any) => c.agent_id && !c.workspace_id)
          .forEach((c: any) => {
            dmAgentIds.add(c.agent_id);
            if (!dmConversations[c.agent_id])
              dmConversations[c.agent_id] = c.id;
          });
        setDmConversationByAgentId(dmConversations);
        if (dmAgentIds.size > 0) {
          api.agents
            .list()
            .then((agents: any[]) => {
              const persistedDmAgents = agents.filter(
                (a: any) => a && a.id && a.name && dmAgentIds.has(a.id),
              );
              setHiredAgents((prev) => {
                const merged = new Map<string, Agent>();
                prev
                  .filter((a) => a && a.id && a.name)
                  .forEach((a) => merged.set(a.id, a));
                persistedDmAgents.forEach((a: Agent) => merged.set(a.id, a));
                return Array.from(merged.values());
              });
            })
            .catch(() => {});
        }
      })
      .catch(() => {});
  }, [mode]);

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
      title: agent?.name || t("page.app_layout.agent"),
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
  ]);

  /* Chat sidebar filtering:
   * Workspaces are stable operating rooms, so DM/agent search should not hide
   * them. The workspace-mode context filter is intentionally ignored here too;
   * otherwise chat mode can look empty because of a hidden selection elsewhere.
   */
  const conversationSearch = convSearchQuery.trim().toLowerCase();
  const isSearchingConversations = conversationSearch.length > 0;
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
  const openAgentDm = (agent: Agent) => {
    const convId = dmConversationByAgentId[agent.id] || `agent:${agent.id}`;
    setHiredAgents((prev) => {
      if (prev.some((item) => item.id === agent.id)) return prev;
      return [...prev, agent];
    });
    setActiveConvId(convId);
    setActiveDmAgentId(agent.id);
    setActiveConvType("dm");
    setConvSearchQuery("");
  };
  const filteredWorkspaces = chatWorkspaces;
  const filteredDmAgents = hiredAgents.filter(
    (a) => a && a.name && matchesConversationSearch(a),
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

  const videoRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  const onNotification = useCallback(
    (data: Record<string, unknown>) => {
      const title = (data.title as string) || "New notification";
      setToast(title);
      setTimeout(() => setToast(null), 4000);
      // Refresh notification list + unread count
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
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
  const onTaskUpdate = useCallback(
    (data: Record<string, any>) => {
      const taskId = data.task_id || data.id;
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      refreshWorkspaces();
    },
    [queryClient, refreshWorkspaces],
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
      // If this workspace isn't in our list yet, refetch
      if (!chatWorkspaces.find((w) => w.id === wsId)) {
        refreshWorkspaces();
      }
      if (data.action_resolved) {
        refreshWorkspaces();
      }
      if (data.has_pending_action) {
        refreshWorkspaces();
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
  const { connectedRef: _connectedRef, unreadCount: wsUnread } = useWebSocket({
    onNotification,
    onTaskUpdate,
    onJobUpdate,
    onVideoReady,
    onWorkspaceChatMessage,
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
    queryKey: ["notifications", "all"],
    queryFn: () => api.notifications.list(),
    refetchInterval: 60_000,
  });
  const unreadCount = notifData?.unread_count ?? wsUnread;
  const [locale, setLocaleState] = useState<Locale>(getLocale());

  const handleLocaleChange = (newLocale: Locale) => {
    setLocale(newLocale);
    setLocaleState(newLocale);
    window.location.reload();
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

  /* Keep the top-level switcher aligned with concrete routes. Chat owns /chat;
   * every other route renders the workspace surface. */
  useEffect(() => {
    setMode(shouldDefaultToChat(location.pathname) ? "chat" : "workspace");
  }, [location.pathname, location.search]);

  useEffect(() => {
    if (location.pathname !== "/chat") return;
    const params = new URLSearchParams(location.search);
    const workspaceId = params.get("workspace") || params.get("workspaceId");
    if (!workspaceId) return;
    setMode("chat");
    setActiveConvId(workspaceId);
    setActiveConvType("operation");
    setActiveDmAgentId(null);
    setConvSearchQuery("");
  }, [location.pathname, location.search]);

  const isSettingsRoute = location.pathname === "/settings";
  const sidebarCollapsed = collapsed && !mobileOpen;

  const switchMode = useCallback(
    (nextMode: AppMode) => {
      setMode(nextMode);
      if (nextMode === "chat") {
        if (location.pathname !== "/chat") navigate("/chat");
        return;
      }
      if (location.pathname === "/chat") navigate("/dashboard");
    },
    [location.pathname, navigate],
  );
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
    type: "manor" | "operation" | "dm";
    label: string;
    subtitle?: string;
    avatarUrl?: string | null;
    customAvatar?: ReactNode;
    icon?: ReactNode;
    initials?: string;
    color?: string;
    count?: number;
    agentId?: string;
  }) => {
    const active = activeConvId === item.id;
    return (
      <button
        key={item.id}
        type="button"
        title={item.subtitle ? `${item.label} · ${item.subtitle}` : item.label}
        aria-label={`Open ${item.label}`}
        aria-pressed={active}
        onClick={() => {
          setActiveConvId(item.id);
          setActiveConvType(item.type);
          setActiveDmAgentId(item.type === "dm" ? item.agentId || null : null);
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
          cursor: "pointer",
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
        {active && (
          <span
            style={{
              position: "absolute",
              left: -6,
              top: "50%",
              width: 3,
              height: 22,
              borderRadius: "0 4px 4px 0",
              background: "var(--sidebar-nav-active-indicator, var(--accent))",
              transform: "translateY(-50%)",
            }}
          />
        )}
        {item.customAvatar ? (
          item.customAvatar
        ) : item.avatarUrl ? (
          <img
            src={item.avatarUrl}
            alt=""
            style={{
              width: 30,
              height: 30,
              borderRadius: "50%",
              objectFit: "cover",
            }}
          />
        ) : item.icon ? (
          <span
            style={{
              width: 30,
              height: 30,
              borderRadius: item.type === "manor" ? "50%" : 10,
              background:
                item.type === "manor" ? "#1c1917" : "var(--accent-soft)",
              color: item.type === "manor" ? "#fff" : "var(--accent)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            {item.icon}
          </span>
        ) : (
          <span
            style={{
              width: 30,
              height: 30,
              borderRadius: item.type === "dm" ? "50%" : 10,
              background: item.color || "#5f928a",
              color: "#fff",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 11,
              fontWeight: 800,
              letterSpacing: "-0.02em",
            }}
          >
            {item.initials || item.label.charAt(0).toUpperCase()}
          </span>
        )}
        {!!item.count && item.count > 0 && (
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
    );
  };

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
              overflow: "hidden",
            }}
          >
            {/* ── LOGO AREA ── */}
            <div
              style={{
                position: "relative",
                padding: sidebarCollapsed ? "14px 0 12px" : "16px 16px 12px",
                display: "flex",
                alignItems: "center",
                justifyContent: sidebarCollapsed ? "center" : undefined,
                gap: 10,
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
                  background: "#1c1917",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                  boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
                }}
              >
                <ManorLogo size={16} color="white" className="manor-brand-logo" />
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
                    color: "var(--sidebar-nav-text)",
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
                    style={{ transition: "transform 0.3s" }}
                  >
                    <polyline points="15 18 9 12 15 6" />
                  </svg>
                </button>
              )}
            </div>

            {/* ── MODE SWITCHER ── */}
            <div
              style={{
                padding: sidebarCollapsed ? "0 0 12px" : "0 16px 12px",
                display: sidebarCollapsed ? "flex" : undefined,
                justifyContent: sidebarCollapsed ? "center" : undefined,
              }}
              data-tour="mode-switcher"
            >
              {sidebarCollapsed ? (
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr",
                    gap: 5,
                    width: 42,
                    padding: 3,
                    borderRadius: 14,
                    background: "var(--sidebar-control-bg)",
                    border: "1px solid var(--sidebar-control-border)",
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
                          border: "1px solid transparent",
                          borderRadius: 11,
                          background: active
                            ? "var(--sidebar-control-active-bg)"
                            : "transparent",
                          color: active
                            ? "var(--sidebar-nav-active-text)"
                            : "var(--sidebar-nav-text)",
                          boxShadow: active
                            ? "var(--sidebar-control-active-shadow)"
                            : "none",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          cursor: "pointer",
                          transition: "all 0.18s ease",
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
                    background: "var(--sidebar-control-bg)",
                    border: "1px solid var(--sidebar-control-border)",
                    borderRadius: 10,
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
                      background: "var(--sidebar-control-active-bg)",
                      borderRadius: 8,
                      boxShadow: "var(--sidebar-control-active-shadow)",
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
                          ? "var(--sidebar-nav-active-text)"
                          : "var(--sidebar-nav-text)",
                      background: "transparent",
                      border: "none",
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
                          ? "var(--sidebar-nav-active-text)"
                          : "var(--sidebar-nav-text)",
                      background: "transparent",
                      border: "none",
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

            {/* ── SCROLLABLE NAV ── */}
            <div
              style={{
                flex: 1,
                overflowY: "auto",
                padding: sidebarCollapsed ? "0 0" : "0 8px",
              }}
            >
              {/* ══════════════════════════════════════════════ */}
              {/* CHAT MODE: Conversation list                   */}
              {/* ══════════════════════════════════════════════ */}
              {mode === "chat" && !sidebarCollapsed && (
                <div
                  style={{ display: "flex", flexDirection: "column", gap: 0 }}
                >
                  {/* Search input */}
                  <div style={{ padding: "0 8px 8px" }}>
                    <div style={{ position: "relative" }}>
                      <svg
                        width="13"
                        height="13"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="#a8a29e"
                        strokeWidth={2}
                        style={{
                          position: "absolute",
                          left: 10,
                          top: "50%",
                          transform: "translateY(-50%)",
                        }}
                      >
                        <circle cx="11" cy="11" r="8" />
                        <line x1="21" y1="21" x2="16.65" y2="16.65" />
                      </svg>
                      <input
                        type="text"
                        value={convSearchQuery}
                        onChange={(e) => setConvSearchQuery(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key !== "Enter") return;
                          const firstMatch =
                            filteredDmAgents[0] || filteredSearchAgents[0];
                          if (!firstMatch) return;
                          e.preventDefault();
                          openAgentDm(firstMatch);
                        }}
                        placeholder={t("page.app_layout.search_agent_to_dm")}
                        style={{
                          width: "100%",
                          height: 34,
                          borderRadius: 10,
                          border: "1px solid var(--sidebar-control-border)",
                          background: "var(--sidebar-control-bg)",
                          paddingLeft: 30,
                          paddingRight: 10,
                          fontSize: 11.5,
                          fontWeight: 500,
                          color: "var(--text-default)",
                          outline: "none",
                        }}
                      />
                    </div>
                  </div>

                  {/* PINNED section */}
                  {!isSearchingConversations && (
                    <div className="conv-section-label">
                      {t("page.app_layout.pinned")}
                    </div>
                  )}

                  {/* Manor AI conversation (always visible) */}
                  {!isSearchingConversations && (
                    <div
                      className={`conv-row ${activeConvId === "manor-ai" ? "conv-row--active" : ""}`}
                      onClick={() => {
                        setActiveConvId("manor-ai");
                        setActiveConvType("manor");
                        setActiveDmAgentId(null);
                      }}
                    >
                      {activeConvId === "manor-ai" && (
                        <span className="conv-active-bar" />
                      )}
                      <div
                        className="conv-avatar"
                        style={{ background: "#1c1917", borderRadius: "50%" }}
                      >
                        <ManorLogo />
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="conv-name">
                          Manor AI
                          <span
                            style={{ marginLeft: 4, fontSize: 10 }}
                            title={t("page.app_layout.pinned")}
                          >
                            {"\uD83D\uDCCC"}
                          </span>
                        </div>
                        <div className="conv-preview">
                          {t("page.app_layout.your_ai_chief_of_staff")}
                        </div>
                      </div>
                      {(actionCounts["manor-ai"] || 0) > 0 && (
                        <span className="conv-badge">
                          {actionCounts["manor-ai"]}
                        </span>
                      )}
                    </div>
                  )}

                  {/* WORKSPACES section */}
                  {!isSearchingConversations &&
                    filteredWorkspaces.length > 0 && (
                      <>
                        <div className="conv-section-label">
                          {t("page.app_layout.workspaces")}
                        </div>
                        {filteredWorkspaces.map((ws) => {
                          const isActive = activeConvId === ws.id;
                          return (
                            <div
                              key={ws.id}
                              className={`conv-row ${isActive ? "conv-row--active" : ""}`}
                              onClick={() => {
                                setActiveConvId(ws.id);
                                setActiveConvType("operation");
                                setActiveDmAgentId(null);
                              }}
                            >
                              {isActive && <span className="conv-active-bar" />}
                              <WorkspaceIconTile
                                workspace={ws}
                                size={30}
                                iconSize={15}
                                style={{ borderRadius: 9, flexShrink: 0 }}
                              />
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div
                                  className="conv-name"
                                  style={
                                    (actionCounts[ws.id] || 0) > 0
                                      ? { fontWeight: 800, color: "var(--text-strong)" }
                                      : undefined
                                  }
                                >
                                  {ws.name}
                                </div>
                                <div className="conv-preview">
                                  {workspaceStatusLabel(ws.status)}
                                </div>
                              </div>
                              {(actionCounts[ws.id] || 0) > 0 && (
                                <span className="conv-badge">
                                  {actionCounts[ws.id]}
                                </span>
                              )}
                            </div>
                          );
                        })}
                      </>
                    )}

                  {/* DIRECT MESSAGES section */}
                  {filteredDmAgents.length > 0 && (
                    <>
                      <div className="conv-section-label">
                        {t("page.app_layout.direct_messages")}
                      </div>
                      {filteredDmAgents
                        .filter((a) => a && a.name)
                        .map((agent) => {
                          const convId =
                            dmConversationByAgentId[agent.id] ||
                            `agent:${agent.id}`;
                          const isActive =
                            activeConvType === "dm" &&
                            activeDmAgentId === agent.id;
                          return (
                            <div
                              key={agent.id}
                              className={`conv-row ${isActive ? "conv-row--active" : ""}`}
                              onClick={() => openAgentDm(agent)}
                            >
                              {isActive && <span className="conv-active-bar" />}
                              <AgentAvatar
                                name={agent.name}
                                avatarUrl={agent.avatar_url}
                                seed={agent.id}
                                size={30}
                                className="conv-avatar"
                              />
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div className="conv-name">{agent.name}</div>
                                <div className="conv-preview">
                                  {agentPreview(agent, "page.app_layout.agent")}
                                </div>
                              </div>
                              {(actionCounts[agent.id] || 0) > 0 && (
                                <span className="conv-badge">
                                  {actionCounts[agent.id]}
                                </span>
                              )}
                            </div>
                          );
                        })}
                    </>
                  )}

                  {/* AGENT SEARCH RESULTS section */}
                  {filteredSearchAgents.length > 0 && (
                    <>
                      <div className="conv-section-label">
                        {t("page.app_layout.start_direct_chat")}
                      </div>
                      {filteredSearchAgents.map((agent) => {
                        const convId =
                          dmConversationByAgentId[agent.id] ||
                          `agent:${agent.id}`;
                        const isActive =
                          activeConvType === "dm" &&
                          activeDmAgentId === agent.id;
                        return (
                          <div
                            key={agent.id}
                            className={`conv-row ${isActive ? "conv-row--active" : ""}`}
                            onClick={() => openAgentDm(agent)}
                          >
                            {isActive && <span className="conv-active-bar" />}
                            <AgentAvatar
                              name={agent.name}
                              avatarUrl={agent.avatar_url}
                              seed={agent.id}
                              size={30}
                              className="conv-avatar"
                            />
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div className="conv-name">{agent.name}</div>
                              <div className="conv-preview">
                                {agentPreview(
                                  agent,
                                  "page.app_layout.start_a_chat",
                                )}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </>
                  )}

                  {convSearchQuery.trim() &&
                    filteredDmAgents.length === 0 &&
                    filteredSearchAgents.length === 0 && (
                      <div
                        style={{
                          padding: "14px 12px",
                          fontSize: 12,
                          color: "var(--sidebar-section-text)",
                          textAlign: "center",
                        }}
                      >
                        {t(
                          "page.app_layout.no_matching_direct_messages_or_agents",
                        )}
                      </div>
                    )}
                </div>
              )}

              {mode === "chat" && sidebarCollapsed && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    gap: 7,
                    padding: "4px 0 10px",
                  }}
                >
                  {renderCollapsedChatButton({
                    id: "manor-ai",
                    type: "manor",
                    label: "Manor AI",
                    subtitle: t("page.app_layout.your_ai_chief_of_staff"),
                    icon: <ManorLogo size={15} color="white" />,
                    count: actionCounts["manor-ai"] || 0,
                  })}

                  {chatWorkspaces.slice(0, 8).map((ws) => {
                    return renderCollapsedChatButton({
                      id: ws.id,
                      type: "operation",
                      label: ws.name,
                      subtitle:
                        ws.status === "active"
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

                  {hiredAgents.length > 0 && (
                    <div
                      style={{
                        width: 28,
                        height: 1,
                        background: "rgba(231,229,228,0.88)",
                        margin: "2px 0",
                      }}
                    />
                  )}

                  {hiredAgents
                    .filter((agent) => agent && agent.id && agent.name)
                    .slice(0, 8)
                    .map((agent) => {
                      const colors = [
                        "#534AB7",
                        "#6d6fb2",
                        "#5a8ea6",
                        "#cf9b44",
                        "#4f9c84",
                        "#d65f59",
                      ];
                      const colorIdx =
                        agent.name
                          .split("")
                          .reduce((sum, char) => sum + char.charCodeAt(0), 0) %
                        colors.length;
                      const convId =
                        dmConversationByAgentId[agent.id] ||
                        `agent:${agent.id}`;
                      return renderCollapsedChatButton({
                        id: convId,
                        type: "dm",
                        label: agent.name,
                        subtitle: agentPreview(agent, "page.app_layout.agent"),
                        avatarUrl: agent.avatar_url,
                        initials: agent.name.charAt(0).toUpperCase(),
                        color: colors[colorIdx],
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
              {mode === "workspace" && (
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
                        OPERATE
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
                      <Link
                        to="/workspaces/new"
                        className="sidebar-workspace-create"
                        title={t("page.workspaces.create_workspace")}
                        aria-label={t("page.workspaces.create_workspace")}
                        style={{
                          width: 36,
                          height: 36,
                          marginRight: 4,
                          borderRadius: 12,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          textDecoration: "none",
                          flexShrink: 0,
                        }}
                      >
                        <IconPlus />
                      </Link>
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
                      <Link
                        to="/workspaces/new"
                        className="sidebar-workspace-create sidebar-workspace-create--collapsed"
                        title={t("page.workspaces.create_workspace")}
                        aria-label={t("page.workspaces.create_workspace")}
                        style={{
                          width: 36,
                          height: 36,
                          margin: "0 auto",
                          borderRadius: 12,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          textDecoration: "none",
                        }}
                      >
                        <IconPlus />
                      </Link>
                    )}
                  </div>
                </div>
              )}

              {/* MANAGEMENT SECTION (workspace mode only) */}
              {mode === "workspace" && (
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
                        LIBRARY
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
                            {user.role || "Member"}
                          </div>
                        </div>
                      )}
                    </button>
                  </div>
                )}
                <div style={{ position: "relative", order: 4 }}>
                  <button
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
                          : "var(--text-faint)",
                      fontSize: 11,
                      fontWeight: 500,
                      cursor: "pointer",
                      transition: "all 0.2s",
                    }}
                    onMouseEnter={(e) => {
                      if (!moreOpen && !isActive("/chat/history")) {
                        e.currentTarget.style.color = "var(--text-muted)";
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!moreOpen && !isActive("/chat/history")) {
                        e.currentTarget.style.color = "var(--text-faint)";
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
                            setHelpOpen(true);
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
                            <circle cx="12" cy="12" r="10" />
                            <path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3" />
                            <line x1="12" y1="17" x2="12.01" y2="17" />
                          </svg>
                          <span>
                            {locale === "zh"
                              ? "帮助"
                              : locale === "es"
                                ? "Ayuda"
                                : "Help"}
                          </span>
                        </button>
                        <Link
                          to="/settings"
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
                            <span>Support</span>
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
                              onClick={() => {
                                handleLocaleChange(l.code as Locale);
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
                                cursor: "pointer",
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
                                src={`https://flagcdn.com/w20/${({ en: "us", zh: "cn", es: "es", de: "de", ja: "jp" } as const)[l.code]}.png`}
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

                {/* Notifications bell */}
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
                    color: "var(--text-faint)",
                    textDecoration: "none",
                    fontSize: 11,
                    fontWeight: 500,
                    position: "relative",
                    transition: "color 0.2s",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.color = "var(--text-muted)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.color = "var(--text-faint)";
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
                        }}
                      />
                    )}
                  </span>
                </Link>
              </div>
            </div>
          </div>

          {/* Collapsed: edge expand button */}
          {sidebarCollapsed && (
            <button
              onClick={() => setCollapsed(false)}
              style={{
                position: "absolute",
                right: -4,
                top: "50%",
                transform: "translateY(-50%)",
                width: 24,
                height: 56,
                background: "var(--surface-panel)",
                border: "1px solid var(--glass-border)",
                borderRadius: "0 12px 12px 0",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                color: "var(--text-faint)",
                zIndex: 20,
                boxShadow: "2px 0 8px rgba(0,0,0,0.04)",
              }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2.5}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>
          )}
        </aside>
        )}

        <main
          className={`app-main-shell flex-1 min-w-0 overflow-auto relative z-10 p-3 ${isSettingsRoute ? "pl-3" : "pl-0"}`}
        >
          {/* Mobile hamburger */}
          {!isSettingsRoute && (
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
              marginBottom: 8,
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
          )}

          {mode === "chat" ? (
            <div
              style={{
                flex: 1,
                minWidth: 0,
                display: "flex",
                flexDirection: "column",
                background: "var(--chrome-surface)",
                backdropFilter: "blur(14px) saturate(1.04)",
                WebkitBackdropFilter: "blur(14px) saturate(1.04)",
                border: "1px solid var(--chrome-border)",
                boxShadow:
                  "var(--glass-highlight), 0 1px 2px rgba(49,75,78,0.012)",
                height: "100%",
                borderRadius: 24,
                overflow: "hidden",
              }}
            >
              {activeConvType === "operation" ? (
                (() => {
                  const activeWorkspace = chatWorkspaces.find(
                    (w) => w.id === activeConvId,
                  );
                  return (
                    <WorkspaceChat
                      key={activeConvId}
                      workspaceId={activeConvId}
                      workspace={activeWorkspace}
                      workspaceName={activeWorkspace?.name}
                    />
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
                  return (
                    <EmbeddedChat
                      conversationId={activeConvId}
                      title={info.title}
                      subtitle={info.subtitle}
                      agents={info.agents}
                      avatarUrl={dmAgent?.avatar_url}
                      agentId={dmAgent?.id}
                    />
                  );
                })()
              )}
            </div>
          ) : (
            <div className={`app-content-panel glass-panel h-full min-w-0 overflow-auto ${isSettingsRoute ? "app-content-panel--settings p-0" : "p-6"}`}>
              <Outlet />
            </div>
          )}
        </main>

        {/* Toast notification */}
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
        {mode === "workspace" &&
          !location.pathname.startsWith("/chat")
          && (
          <FloatingChat />
        )}


        {helpOpen && (
          <ManorHelpModal
            locale={locale}
            onClose={() => setHelpOpen(false)}
            onStartTour={() => {
              setHelpOpen(false);
              window.dispatchEvent(new Event("manor:start-tour"));
            }}
          />
        )}

        {/* Onboarding tour for new users */}
        {!isTourSuppressedPath(location.pathname) && <OnboardingTour />}

        {/* Support drawer — talks to the platform team */}
        {supportTicketsEnabled && (
          <SupportPanel
            open={supportOpen}
            onClose={() => setSupportOpen(false)}
          />
        )}
      </div>
    </div>
  );
}
