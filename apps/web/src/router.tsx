import { createBrowserRouter, Navigate, useLocation, type RouteObject } from "react-router-dom";
import { Suspense, lazy, useEffect } from "react";
import ProtectedRoute from "./components/ProtectedRoute";
import RouteErrorBoundary from "./components/RouteErrorBoundary";
import RouterRoot from "./components/RouterRoot";
import WorkflowLegacyRedirect from "./components/workflows/WorkflowLegacyRedirect";
import { PageLoading } from "./components/ui/Skeleton";
import Button from "./components/ui/Button";
import EmptyState from "./components/ui/EmptyState";
import { t } from "./lib/i18n";
import { useConfigStore } from "./stores/config";
import {
  type PreviewFeature,
  usePreviewFeatureAccess,
} from "./lib/previewFeatureAccess";

// Lazy load all pages
const AppLayout = lazy(() => import("./layouts/AppLayout"));
const Login = lazy(() => import("./pages/Login"));
const Tasks = lazy(() => import("./pages/Tasks"));
const TaskImport = lazy(() => import("./pages/TaskImport"));
const TaskImportEmbed = lazy(() => import("./pages/TaskImportEmbed"));
const CalendarPickerEmbed = lazy(() => import("./pages/CalendarPickerEmbed"));
const TaskDetail = lazy(() => import("./pages/TaskDetail"));
const TaskBoard = lazy(() => import("./pages/TaskBoard"));
const TaskCollections = lazy(() => import("./pages/TaskCollections"));
const TaskProcess = lazy(() => import("./pages/TaskProcess"));
const TaskEvaluate = lazy(() => import("./pages/TaskEvaluate"));
const Knowledge = lazy(() => import("./pages/Knowledge"));
const DocEditor = lazy(() => import("./pages/DocEditor"));
const FileViewer = lazy(() => import("./pages/FileViewer"));
const VideoEditor = lazy(() => import("./pages/VideoEditor"));
const DiagramStudio = lazy(() => import("./pages/DiagramStudio"));
const AgentDetail = lazy(() => import("./pages/AgentDetail"));
const AgentDashboard = lazy(() => import("./pages/AgentDashboard"));
const Flows = lazy(() => import("./pages/Flows"));
const WorkflowRun = lazy(() => import("./pages/WorkflowRun"));
const Integrations = lazy(() => import("./pages/Integrations"));
const Workspaces = lazy(() => import("./pages/Workspaces"));
const WorkspaceDraftChat = lazy(() => import("./pages/WorkspaceDraftChat"));
const WorkspaceDetail = lazy(() => import("./pages/WorkspaceDetail"));
const TeamLayout = lazy(() => import("./pages/team/TeamLayout"));
const TeamPeople = lazy(() => import("./pages/team/TeamPeople"));
const TeamRoles = lazy(() => import("./pages/team/TeamRoles"));

const Messages = lazy(() => import("./pages/Messages"));
const Users = lazy(() => import("./pages/Users"));
const Activity = lazy(() => import("./pages/Activity"));
const Settings = lazy(() => import("./pages/Settings"));
// Team, clients and integrations open inside the Settings frame.
const SettingsFrame = lazy(() => import("./pages/Settings").then((m) => ({ default: m.SettingsFrame })));
const Notifications = lazy(() => import("./pages/Notifications"));
const ApprovalRequest = lazy(() => import("./pages/ApprovalRequest"));
const Clients = lazy(() => import("./pages/Clients"));
const ClientDelivery = lazy(() => import("./pages/ClientDelivery"));
const WorkspaceCollaboration = lazy(() => import("./pages/WorkspaceCollaboration"));
const Account = lazy(() => import("./pages/Account"));
const BrowserSessions = lazy(() => import("./pages/BrowserSessions"));
const OAuthCallback = lazy(() => import("./pages/OAuthCallback"));
const IntegrationOAuthDone = lazy(() => import("./pages/IntegrationOAuthDone"));
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
const SignDocument = lazy(() => import("./pages/SignDocument"));
const ApiKeys = lazy(() => import("./pages/ApiKeys"));
const AdminOAuthClients = lazy(() => import("./pages/AdminOAuthClients"));
const WebhookManager = lazy(() => import("./pages/WebhookManager"));
const CustomFields = lazy(() => import("./pages/CustomFields"));
const Reports = lazy(() => import("./pages/Reports"));
const JobLogs = lazy(() => import("./pages/JobLogs"));

// Loading fallback
function PageLoader() {
  return <PageLoading label={t("status.loading")} minHeight={200} />;
}

// Wrap each element with Suspense
function S({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<PageLoader />}>{children}</Suspense>;
}

function ChatHistoryRedirect() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  params.set("tab", "history");
  return <Navigate to={`/settings?${params}`} replace />;
}

function LegacyClientsRedirect() {
  const location = useLocation();
  return <Navigate to={`/team/clients${location.search}${location.hash}`} replace />;
}


/** Old library routes land on their Settings › AI tab with the same query. */
function SettingsTabRedirect({ tab }: { tab: "agents" | "skills" | "memories" | "flows" | "automations" }) {
  const location = useLocation();
  const query = new URLSearchParams(location.search);
  query.delete("tab");
  const rest = query.toString();
  return <Navigate to={`/settings?tab=${tab}${rest ? `&${rest}` : ""}${location.hash}`} state={location.state} replace />;
}

function PreviewFeatureRoute({
  feature,
  children,
}: {
  feature: PreviewFeature;
  children: React.ReactNode;
}) {
  const access = usePreviewFeatureAccess(feature);
  if (!access.loaded) return <PageLoader />;
  if (!access.enabled) return <Navigate to="/settings?tab=skills" replace />;
  return <>{children}</>;
}

const routes: RouteObject[] = [
  {
    path: "/calendar-picker/embed",
    element: <Suspense fallback={<PageLoading />}><CalendarPickerEmbed /></Suspense>,
  },
  // Public routes
  {
    path: "/task-import/embed",
    element: <Suspense fallback={<PageLoading />}><TaskImportEmbed /></Suspense>,
  },
  {
    path: "/task-import",
    element: <Suspense fallback={<PageLoading />}><TaskImport /></Suspense>,
  },
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
    // An integration's OAuth popup lands here and reports to the chat that opened it.
    path: "/integrations/connected",
    element: (
      <S>
        <IntegrationOAuthDone />
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
        element: (
          <Suspense fallback={<PageLoader />}>
            <AppLayout />
          </Suspense>
        ),
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
            element: <ChatHistoryRedirect />,
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
          { path: "/jobs", element: <SettingsTabRedirect tab="automations" /> },
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
            element: <Navigate to="/chat" replace />,
          },
          {
            path: "/settings",
            element: (
              <S>
                <Settings />
              </S>
            ),
          },
          // The AI libraries live in Settings › AI; old links keep their query.
          { path: "/agents", element: <SettingsTabRedirect tab="agents" /> },
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
              <PreviewFeatureRoute feature="flows">
                <S>
                  <WorkflowLegacyRedirect><SettingsTabRedirect tab="flows" /></WorkflowLegacyRedirect>
                </S>
              </PreviewFeatureRoute>
            ),
          },
          {
            path: "/flows/:workflowId",
            element: <PreviewFeatureRoute feature="flows"><S><Flows /></S></PreviewFeatureRoute>,
          },
          {
            path: "/flows/:workflowId/runs/:runId",
            element: <PreviewFeatureRoute feature="flows"><S><WorkflowRun /></S></PreviewFeatureRoute>,
          },
          {
            path: "/workspaces/:workspaceId/workflow-runs/:runId",
            element: <PreviewFeatureRoute feature="flows"><S><WorkflowRun /></S></PreviewFeatureRoute>,
          },
          { path: "/skills", element: <SettingsTabRedirect tab="skills" /> },
          {
            path: "/integrations",
            element: (
              <S>
                <SettingsFrame activeKey="integrations"><Integrations /></SettingsFrame>
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
            path: "/approvals/:requestId",
            element: (
              <S>
                <ApprovalRequest />
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
            path: "/team",
            element: (
              <S>
                <SettingsFrame activeKey="team"><TeamLayout /></SettingsFrame>
              </S>
            ),
            children: [
              {
                index: true,
                element: (
                  <S>
                    <TeamPeople />
                  </S>
                ),
              },
              {
                path: "roles",
                element: (
                  <S>
                    <TeamRoles />
                  </S>
                ),
              },
            ],
          },
          { path: "/team/clients", element: <S><SettingsFrame activeKey="clients"><Clients /></SettingsFrame></S> },
          { path: "/company/clients", element: <LegacyClientsRedirect /> },
          { path: "/client-delivery/:token", element: <S><ClientDelivery /></S> },
          { path: "/workspaces/:workspaceId/collaboration", element: <S><WorkspaceCollaboration /></S> },
          { path: "/workspace-collaboration/:grantId", element: <S><WorkspaceCollaboration /></S> },
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
          // Search lives in the sidebar (⌘K); old links land on the home page.
          { path: "/search", element: <Navigate to="/chat" replace /> },
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
          { path: "/memories", element: <SettingsTabRedirect tab="memories" /> },
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

  // Public PDF signature ceremony (opaque token + recipient email OTP).
  {
    path: "/sign/:token",
    element: (
      <S>
        <SignDocument />
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
];

export const router = createBrowserRouter([
  { element: <RouterRoot />, errorElement: <RouteErrorBoundary />, children: routes },
]);
