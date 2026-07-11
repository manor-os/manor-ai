import { createBrowserRouter, Navigate } from "react-router-dom";
import { Suspense, lazy, useEffect } from "react";
import AppLayout from "./layouts/AppLayout";
import ProtectedRoute from "./components/ProtectedRoute";
import RouteErrorBoundary from "./components/RouteErrorBoundary";
import { t } from "./lib/i18n";
import { useConfigStore } from "./stores/config";

// Lazy load all pages
const Login = lazy(() => import("./pages/Login"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Tasks = lazy(() => import("./pages/Tasks"));
const TaskDetail = lazy(() => import("./pages/TaskDetail"));
const TaskBoard = lazy(() => import("./pages/TaskBoard"));
const TaskCollections = lazy(() => import("./pages/TaskCollections"));
const TaskProcess = lazy(() => import("./pages/TaskProcess"));
const TaskEvaluate = lazy(() => import("./pages/TaskEvaluate"));
const GoalExplorer = lazy(() => import("./pages/GoalExplorer"));
const Knowledge = lazy(() => import("./pages/Knowledge"));
const DocEditor = lazy(() => import("./pages/DocEditor"));
const FileViewer = lazy(() => import("./pages/FileViewer"));
const VideoEditor = lazy(() => import("./pages/VideoEditor"));
const DiagramStudio = lazy(() => import("./pages/DiagramStudio"));
const ChatHistory = lazy(() => import("./pages/ChatHistory"));
const Agents = lazy(() => import("./pages/Agents"));
const AgentDetail = lazy(() => import("./pages/AgentDetail"));
const AgentDashboard = lazy(() => import("./pages/AgentDashboard"));
const Flows = lazy(() => import("./pages/Flows"));
const Skills = lazy(() => import("./pages/Skills"));
const Integrations = lazy(() => import("./pages/Integrations"));
const Workspaces = lazy(() => import("./pages/Workspaces"));
const WorkspaceDraftChat = lazy(() => import("./pages/WorkspaceDraftChat"));
const WorkspaceDetail = lazy(() => import("./pages/WorkspaceDetail"));
const Messages = lazy(() => import("./pages/Messages"));
const Users = lazy(() => import("./pages/Users"));
const Activity = lazy(() => import("./pages/Activity"));
const Settings = lazy(() => import("./pages/Settings"));
const Notifications = lazy(() => import("./pages/Notifications"));
const Announcements = lazy(() => import("./pages/Announcements"));
const Account = lazy(() => import("./pages/Account"));
const ScheduledJobs = lazy(() => import("./pages/ScheduledJobs"));
const BrowserSessions = lazy(() => import("./pages/BrowserSessions"));
const OAuthCallback = lazy(() => import("./pages/OAuthCallback"));
const OAuthAuthorize = lazy(() => import("./pages/OAuthAuthorize"));
const NotFound = lazy(() => import("./pages/NotFound"));
const ForgotPassword = lazy(() => import("./pages/ForgotPassword"));
const ResetPassword = lazy(() => import("./pages/ResetPassword"));
const QRCode = lazy(() => import("./pages/QRCode"));
const PublicChat = lazy(() => import("./pages/PublicChat"));
const BookingLink = lazy(() => import("./pages/BookingLink"));
const ClientPortal = lazy(() => import("./pages/ClientPortal"));
const SharedFolder = lazy(() => import("./pages/SharedFolder"));
const SharedDocument = lazy(() => import("./pages/SharedDocument"));
const SearchResults = lazy(() => import("./pages/SearchResults"));
const ApiKeys = lazy(() => import("./pages/ApiKeys"));
const AdminOAuthClients = lazy(() => import("./pages/AdminOAuthClients"));
const WebhookManager = lazy(() => import("./pages/WebhookManager"));
const CustomFields = lazy(() => import("./pages/CustomFields"));
const AgentMemories = lazy(() => import("./pages/Memories"));
const Reports = lazy(() => import("./pages/Reports"));
const JobLogs = lazy(() => import("./pages/JobLogs"));

// Loading fallback
function PageLoader() {
  return (
    <div className="flex items-center justify-center h-full min-h-[200px]">
      <div className="flex items-center gap-3 text-stone-400">
        <svg className="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
          />
        </svg>
        <span className="text-sm font-medium">{t("status.loading")}</span>
      </div>
    </div>
  );
}

// Wrap each element with Suspense
function S({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<PageLoader />}>{children}</Suspense>;
}


export const router = createBrowserRouter([
  // Public routes
  {
    path: "/login",
    element: (
      <S>
        <Login />
      </S>
    ),
  },
  {
    path: "/register",
    element: (
      <S>
        <Login />
      </S>
    ),
  },
  {
    path: "/forgot-password",
    element: (
      <S>
        <ForgotPassword />
      </S>
    ),
  },
  {
    path: "/reset-password",
    element: (
      <S>
        <ResetPassword />
      </S>
    ),
  },
  {
    path: "/oauth/callback",
    element: (
      <S>
        <OAuthCallback />
      </S>
    ),
  },
  {
    path: "/oauth/authorize",
    element: (
      <S>
        <OAuthAuthorize />
      </S>
    ),
  },
  {
    path: "/task/process",
    element: (
      <S>
        <TaskProcess />
      </S>
    ),
  },
  {
    path: "/task/evaluate",
    element: (
      <S>
        <TaskEvaluate />
      </S>
    ),
  },

  // Protected routes
  {
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppLayout />,
        // Catches lazy-chunk-load failures (post-deploy stale references)
        // and route loader errors before React Router shows its default
        // "💿 Hey developer 👋" page. Auto-reloads once on stale chunks.
        errorElement: <RouteErrorBoundary />,
        children: [
          {
            path: "/",
            element: <Navigate to="/chat" replace />,
          },
          { path: "/chat", element: <></> },
          {
            path: "/chat/history",
            element: (
              <S>
                <ChatHistory />
              </S>
            ),
          },
          {
            path: "/chat/:id",
            element: <Navigate to="/chat/history" replace />,
          },
          {
            path: "/tasks",
            element: (
              <S>
                <Tasks />
              </S>
            ),
          },
          {
            path: "/tasks/board",
            element: (
              <S>
                <TaskBoard />
              </S>
            ),
          },
          {
            path: "/tasks/collections",
            element: (
              <S>
                <TaskCollections />
              </S>
            ),
          },
          {
            path: "/tasks/:taskId",
            element: (
              <S>
                <TaskDetail />
              </S>
            ),
          },
          { path: "/goals", element: <Navigate to="/workspaces" replace /> },
          {
            path: "/goals/:goalId",
            element: <Navigate to="/workspaces" replace />,
          },
          {
            path: "/jobs",
            element: (
              <S>
                <ScheduledJobs />
              </S>
            ),
          },
          {
            path: "/jobs/:jobId/logs",
            element: (
              <S>
                <JobLogs />
              </S>
            ),
          },
          {
            path: "/knowledge",
            element: (
              <S>
                <Knowledge />
              </S>
            ),
          },
          {
            path: "/viewer/:docId",
            element: (
              <S>
                <FileViewer />
              </S>
            ),
          },
          {
            path: "/video-editor/:docId",
            element: (
              <S>
                <VideoEditor />
              </S>
            ),
          },
          {
            path: "/editor/:docId",
            element: (
              <S>
                <DocEditor />
              </S>
            ),
          },
          {
            path: "/diagram-canvas",
            element: (
              <S>
                <DiagramStudio />
              </S>
            ),
          },
          {
            path: "/dashboard",
            element: (
              <S>
                <Dashboard />
              </S>
            ),
          },
          {
            path: "/settings",
            element: (
              <S>
                <Settings />
              </S>
            ),
          },
          {
            path: "/agents",
            element: (
              <S>
                <Agents />
              </S>
            ),
          },
          {
            path: "/agents/dashboard",
            element: (
              <S>
                <AgentDashboard />
              </S>
            ),
          },
          {
            path: "/agents/:agentId",
            element: (
              <S>
                <AgentDetail />
              </S>
            ),
          },
          {
            path: "/flows",
            element: (
              <S>
                <Flows />
              </S>
            ),
          },
          {
            path: "/skills",
            element: (
              <S>
                <Skills />
              </S>
            ),
          },
          {
            path: "/integrations",
            element: (
              <S>
                <Integrations />
              </S>
            ),
          },
          {
            path: "/notifications",
            element: (
              <S>
                <Notifications />
              </S>
            ),
          },
          {
            path: "/activity",
            element: (
              <S>
                <Activity />
              </S>
            ),
          },
          {
            path: "/users",
            element: (
              <S>
                <Users />
              </S>
            ),
          },
          {
            path: "/workspaces/new",
            element: (
              <S>
                <WorkspaceDraftChat />
              </S>
            ),
          },
          {
            path: "/workspaces/:workspaceId",
            element: (
              <S>
                <WorkspaceDetail />
              </S>
            ),
          },
          {
            path: "/workspaces",
            element: (
              <S>
                <Workspaces />
              </S>
            ),
          },
          {
            path: "/operations",
            element: <Navigate to="/workspaces" replace />,
          },
          {
            path: "/messages",
            element: (
              <S>
                <Messages />
              </S>
            ),
          },
          {
            path: "/inbox",
            element: <Navigate to="/messages" replace />,
          },
          {
            path: "/announcements",
            element: (
              <S>
                <Announcements />
              </S>
            ),
          },
          {
            path: "/account",
            element: (
              <S>
                <Account />
              </S>
            ),
          },
          {
            path: "/browser/sessions/:sessionId",
            element: (
              <S>
                <BrowserSessions />
              </S>
            ),
          },
          {
            path: "/browser/sessions",
            element: (
              <S>
                <BrowserSessions />
              </S>
            ),
          },
          {
            path: "/qrcode",
            element: (
              <S>
                <QRCode />
              </S>
            ),
          },
          {
            path: "/search",
            element: (
              <S>
                <SearchResults />
              </S>
            ),
          },
          {
            path: "/api-keys",
            element: (
              <S>
                <ApiKeys />
              </S>
            ),
          },
          // Hidden admin OAuth client management. Not in nav.
          // Reachable only by typing /__admin/oauth — backend
          // also enforces admin/owner role.
          {
            path: "/__admin/oauth",
            element: (
              <S>
                <AdminOAuthClients />
              </S>
            ),
          },
          {
            path: "/webhooks",
            element: (
              <S>
                <WebhookManager />
              </S>
            ),
          },
          {
            path: "/custom-fields",
            element: (
              <S>
                <CustomFields />
              </S>
            ),
          },
          {
            path: "/memories",
            element: (
              <S>
                <AgentMemories />
              </S>
            ),
          },
          {
            path: "/reports",
            element: (
              <S>
                <Reports />
              </S>
            ),
          },
        ],
      },
    ],
  },

  // Public webchat (no auth — accessed via QR code / shared link)
  {
    path: "/chat/public/:token",
    element: (
      <S>
        <PublicChat />
      </S>
    ),
  },
  {
    path: "/book/u/:ownerId/:slug",
    element: (
      <S>
        <BookingLink />
      </S>
    ),
  },
  {
    path: "/book/:slug",
    element: (
      <S>
        <BookingLink />
      </S>
    ),
  },

  // Public folder share (no auth — accessed via opaque share token)
  {
    path: "/shared-folder/:token",
    element: (
      <S>
        <SharedFolder />
      </S>
    ),
  },

  // Public document share (no auth — accessed via opaque share token).
  // Mirrors /shared-folder; both routes are reached from the URLs minted
  // by ShareDialog → "Anyone with the link" → Copy link.
  {
    path: "/shared-doc/:token",
    element: (
      <S>
        <SharedDocument />
      </S>
    ),
  },

  // Client Portal (public — outside ProtectedRoute)
  {
    path: "/portal",
    element: (
      <S>
        <ClientPortal />
      </S>
    ),
  },
  {
    path: "/portal/login",
    element: (
      <S>
        <ClientPortal />
      </S>
    ),
  },
  {
    path: "/portal/tickets",
    element: (
      <S>
        <ClientPortal />
      </S>
    ),
  },

  // Legacy redirects from original Vue app
  { path: "/playground", element: <Navigate to="/chat" replace /> },
  {
    path: "/playground/dashboard",
    element: <Navigate to="/chat" replace />,
  },
  { path: "/playground/myTask", element: <Navigate to="/tasks" replace /> },
  { path: "/playground/chat", element: <Navigate to="/chat" replace /> },
  {
    path: "/playground/knowledge",
    element: <Navigate to="/knowledge" replace />,
  },
  { path: "/playground/market", element: <Navigate to="/agents" replace /> },
  { path: "/playground/setting", element: <Navigate to="/settings" replace /> },
  { path: "/playground/flows", element: <Navigate to="/flows" replace /> },
  { path: "/playground/skills", element: <Navigate to="/skills" replace /> },
  {
    path: "/playground/notification",
    element: <Navigate to="/notifications" replace />,
  },
  {
    path: "/playground/operations",
    element: <Navigate to="/workspaces" replace />,
  },
  { path: "/playground/message", element: <Navigate to="/messages" replace /> },
  {
    path: "/playground/agentDashboard",
    element: <Navigate to="/agents/dashboard" replace />,
  },
  { path: "/playground/qrcode", element: <Navigate to="/qrcode" replace /> },
  { path: "/playground/*", element: <Navigate to="/chat" replace /> },
  {
    path: "/commandCenter",
    element: <Navigate to="/tasks?view=priority" replace />,
  },
  { path: "/index", element: <Navigate to="/chat" replace /> },

  // 404
  {
    path: "/404",
    element: (
      <S>
        <NotFound />
      </S>
    ),
  },
  {
    path: "*",
    element: (
      <S>
        <NotFound />
      </S>
    ),
  },
]);
