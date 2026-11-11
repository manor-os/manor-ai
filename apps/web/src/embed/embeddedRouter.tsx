import { Suspense, lazy, useEffect, useLayoutEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { createBrowserRouter, Navigate, Outlet, useLocation } from "react-router-dom";
import RouteErrorBoundary from "../components/RouteErrorBoundary";
import DetailDrawer from "../components/ui/DetailDrawer";
import IntegrationConnectHost from "../components/integrations/IntegrationConnectHost";
import EmptyState from "../components/ui/EmptyState";
import { PageLoading } from "../components/ui/Skeleton";
import { t } from "../lib/i18n";
import { useAuthStore } from "../stores/auth";
import { useWorkspaceFilter } from "../stores/workspace";
import { EMBED } from "./embedContext";
import { embeddedStatus, embeddedWorkspaceId, subscribeEmbeddedStatus } from "./embeddedSession";

// The same pages the app uses; the frame offers only the pinned Workspace's.
const CollaboratorLayout = lazy(() => import("../layouts/CollaboratorLayout"));
const Tasks = lazy(() => import("../pages/Tasks"));
const WorkspaceDetail = lazy(() => import("../pages/WorkspaceDetail"));
const WorkflowRun = lazy(() => import("../pages/WorkflowRun"));
const TaskDetail = lazy(() => import("../pages/TaskDetail"));

function S({ children }: { children: ReactNode }) {
  return <Suspense fallback={<PageLoading label={t("status.loading")} minHeight={200} />}>{children}</Suspense>;
}

function FrameState({ title }: { title: string }) {
  return <div className="flex h-screen items-center justify-center p-6"><EmptyState title={title} /></div>;
}

function EmbeddedRoot() {
  return <><Outlet /><DetailDrawer /><IntegrationConnectHost /></>;
}

/** Waits for the parent's session; an ended or failed one stays on this screen (no sign-in page). */
function EmbeddedProtectedRoute() {
  const status = useSyncExternalStore(subscribeEmbeddedStatus, embeddedStatus);
  const token = useAuthStore((s) => s.token);
  const isLoading = useAuthStore((s) => s.isLoading);
  const checkAuth = useAuthStore((s) => s.checkAuth);
  useEffect(() => { void checkAuth(); }, [checkAuth]);
  if (status === "ended") return <FrameState title={t("page.client_trials.ended")} />;
  if (status === "unavailable" || (!token && !isLoading && status === "ready")) {
    return <FrameState title={t("page.client_trials.unavailable")} />;
  }
  if (isLoading || !token) return <PageLoading label={t("page.client_trials.connecting")} minHeight={320} />;
  return <Outlet />;
}

/** The frame has one Workspace: any list or unknown path opens it. */
function PinnedWorkspace() {
  const id = embeddedWorkspaceId();
  return id ? <Navigate to={`/workspaces/${encodeURIComponent(id)}`} replace /> : <PageLoading label={t("status.loading")} minHeight={200} />;
}

/** The task list, always filtered to the pinned Workspace. */
function PinnedTasks() {
  const location = useLocation();
  const id = embeddedWorkspaceId();
  // The page reads its Workspace filter from the store: pin it before the
  // page's first query, which would otherwise ask for every Workspace's tasks.
  const [pinned, setPinned] = useState(false);
  useLayoutEffect(() => {
    if (!id) return;
    useWorkspaceFilter.getState().setActiveWorkspaceId(id);
    setPinned(true);
  }, [id]);
  if (!id || !pinned) return <PageLoading label={t("status.loading")} minHeight={200} />;
  const params = new URLSearchParams(location.search);
  if (params.get("workspaceId") !== id) {
    params.set("workspaceId", id);
    params.delete("workspace_id");
    return <Navigate to={`/tasks?${params}`} replace />;
  }
  return <S><Tasks /></S>;
}

export const router = createBrowserRouter([
  {
    element: <EmbeddedRoot />,
    errorElement: <RouteErrorBoundary />,
    children: [{
      element: <EmbeddedProtectedRoute />,
      children: [{
        element: <S><CollaboratorLayout /></S>,
        errorElement: <RouteErrorBoundary />,
        children: [
          { path: "/workspaces", element: <PinnedWorkspace /> },
          { path: "/workspaces/:workspaceId", element: <S><WorkspaceDetail /></S> },
          { path: "/workspaces/:workspaceId/workflow-runs/:runId", element: <S><WorkflowRun /></S> },
          { path: "/tasks", element: <PinnedTasks /> },
          { path: "/tasks/:taskId", element: <S><TaskDetail /></S> },
          { path: "/chat", element: <></> },
          { path: "*", element: <PinnedWorkspace /> },
        ],
      }],
    }],
  },
], { basename: EMBED?.basename });
