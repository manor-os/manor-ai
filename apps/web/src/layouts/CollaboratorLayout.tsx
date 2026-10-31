import { lazy, Suspense, useCallback, useEffect, useRef } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { scheduleQueryInvalidation } from "../lib/coalescedInvalidation";
import { invalidateRealtimeQueries, useRealtimeResync } from "../lib/realtimeResync";
import { createTaskUpdateInvalidationBatcher, type TaskUpdateInvalidationBatcher } from "../lib/taskUpdateInvalidation";
import { useWebSocket } from "../lib/websocket";
import { invalidateChangedWorkspace } from "../lib/workspaceChangeInvalidation";
import { useAgentEditModalStore } from "../stores/agentEditModal";
import { api } from "../lib/api";
import { embeddedWorkspaceId } from "../embed/embeddedSession";
import { parseAppLayoutChatTarget } from "./appLayoutChatQuery";
import { PageHeaderBoundary } from "../components/ui/PageHeader";
import { InlineRowsSkeleton } from "../components/ui/Skeleton";
import TabSwitcher from "../components/ui/TabSwitcher";
import { t } from "../lib/i18n";

const WorkspaceChat = lazy(() => import("../components/WorkspaceChat"));
const AgentEditModal = lazy(() => import("../components/AgentEditModal"));

// The pages a collaborator session can load: the granted Workspace, its tasks
// (the list is filtered to it), and its chat. Every company page (task
// board, Workspace creation, ...) is refused by the server.
function collaboratorPath(pathname: string) {
  if (pathname === "/workspaces/new") return false;
  return pathname === "/workspaces"
    || pathname.startsWith("/workspaces/")
    || pathname === "/tasks"
    || (/^\/tasks\/[^/]+$/.test(pathname) && !["/tasks/board", "/tasks/collections"].includes(pathname))
    || pathname === "/chat";
}

function Loading() {
  return <div className="flex min-h-0 flex-1 p-4" aria-busy="true"><InlineRowsSkeleton rows={3} dense /></div>;
}

/** /chat for a collaborator: only the chat of a Workspace they were granted. */
function CollaboratorWorkspaceChat({ workspaceId }: { workspaceId: string }) {
  // The collaborator's Workspace list is exactly their live grants.
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: () => api.workspaces.list(), staleTime: 60_000 });
  if (workspaces.isPending) return <Loading />;
  const workspace = workspaces.data?.find((item) => item.id === workspaceId);
  if (!workspace) return <Navigate to="/workspaces" replace />;
  return (
    <Suspense fallback={<Loading />}>
      {/* The frame's Chat entry opens the conversation; the App view stays one toggle away. */}
      <WorkspaceChat key={workspace.id} workspaceId={workspace.id} workspace={workspace} workspaceName={workspace.name} initialAppDisplay="chat" />
    </Suspense>
  );
}

/**
 * The realtime channel, open on every collaborator page as the company layout
 * keeps it open: the server sends a collaborator only its granted Workspace's
 * events (apps/api/routers/ws.py). Chat surfaces read turn snapshots and
 * conversation messages from the window events dispatched here.
 */
function useCollaboratorWorkspaceEvents() {
  const queryClient = useQueryClient();
  const taskUpdatesRef = useRef<TaskUpdateInvalidationBatcher>();
  useEffect(() => {
    const batcher = createTaskUpdateInvalidationBatcher(queryClient);
    taskUpdatesRef.current = batcher;
    return () => {
      if (taskUpdatesRef.current === batcher) taskUpdatesRef.current = undefined;
      batcher.dispose();
    };
  }, [queryClient]);
  const onTaskUpdate = useCallback((data: Record<string, any>) => {
    taskUpdatesRef.current?.enqueue({ taskId: data.task_id || data.id });
  }, []);
  const onJobUpdate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["scheduled-jobs"] });
    void queryClient.invalidateQueries({ queryKey: ["job-runs"] });
  }, [queryClient]);
  const onConversationMessage = useCallback((data: Record<string, any>) => {
    scheduleQueryInvalidation(queryClient, ["conversations"]);
    window.dispatchEvent(new CustomEvent("manor:conversation-message", { detail: data }));
  }, [queryClient]);
  const onChatStreamSnapshot = useCallback((data: Record<string, any>) => {
    window.dispatchEvent(new CustomEvent("manor:chat-stream-snapshot", { detail: data }));
  }, []);
  const onWorkspaceChanged = useCallback((data: Record<string, any>) => {
    invalidateChangedWorkspace(queryClient, String(data.workspace_id || ""));
  }, [queryClient]);
  useWebSocket({ onTaskUpdate, onJobUpdate, onConversationMessage, onChatStreamSnapshot, onWorkspaceChanged });
  // After a delivery gap (each token rotation reconnects) the Workspace chat,
  // tasks and approvals reconcile with the API, as the company layout does.
  useRealtimeResync(() => { void invalidateRealtimeQueries(queryClient); });
}


/**
 * The embedded customer-trial frame's shell (src/embed/embeddedRouter.tsx).
 * The parent page owns the header and the way out; this renders no company
 * navigation, no company switcher and no sign-out, and starts none of the main
 * layout's company requests (notifications, chats, billing, announcements):
 * only the pinned Workspace's pages and chat and the Workspace-filtered websocket.
 */
/** Workspace, its chat and its tasks: the frame's only navigation. */
function FrameNavigation({ workspaceId }: { workspaceId: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const id = encodeURIComponent(workspaceId);
  const current = location.pathname === "/chat" ? "chat" : location.pathname.startsWith("/tasks") ? "tasks" : "workspace";
  const to: Record<string, string> = { workspace: `/workspaces/${id}`, chat: `/chat?workspace=${id}`, tasks: `/tasks?workspaceId=${id}` };
  return (
    <TabSwitcher
      tabs={[
        { key: "workspace", label: t("page.client_trials.nav_workspace") },
        { key: "chat", label: t("nav.chat") },
        { key: "tasks", label: t("nav.tasks") },
      ]}
      value={current}
      onChange={(key) => navigate(to[key])}
      size="sm"
      ariaLabel={t("page.client_trials.nav_workspace")}
      className="shrink-0 self-start"
    />
  );
}

export default function CollaboratorLayout() {
  const location = useLocation();
  const agentEditModalOpen = useAgentEditModalStore((state) => state.agentId !== undefined);
  useCollaboratorWorkspaceEvents();
  const pinned = embeddedWorkspaceId();
  const home = pinned ? `/workspaces/${encodeURIComponent(pinned)}` : "/workspaces";
  if (!collaboratorPath(location.pathname)) return <Navigate to={home} replace />;
  const chatTarget = location.pathname === "/chat" ? parseAppLayoutChatTarget(location.search) : null;
  if (location.pathname === "/chat" && chatTarget?.type !== "workspace") return <Navigate to={home} replace />;

  return (
    <div className="flex flex-col h-screen relative">
      <div className="flex flex-1 relative" style={{ minHeight: 0 }}>
        <div className="aurora-bg">
          <div className="aurora-blob aurora-blob-1" />
          <div className="aurora-blob aurora-blob-2" />
          <div className="aurora-blob aurora-blob-3" />
        </div>
        <main className="app-main-shell relative z-10 flex min-w-0 flex-1 flex-col gap-3 overflow-hidden p-3">
          {pinned && <FrameNavigation workspaceId={pinned} />}
          {chatTarget?.type === "workspace" ? (
            <div className="app-chat-shell flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden" style={{ borderRadius: 24 }}>
              <CollaboratorWorkspaceChat workspaceId={chatTarget.workspaceId} />
            </div>
          ) : (
            <div className="app-content-panel glass-panel flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden p-0">
              <PageHeaderBoundary>
                <div className="app-route-content min-h-0 min-w-0 flex-1 overflow-auto px-6 pb-6 pt-2">
                  <Outlet />
                </div>
              </PageHeaderBoundary>
            </div>
          )}
        </main>
      </div>
      {/* Opened by the Workspace page and chat via openAgentEditModal(). */}
      {agentEditModalOpen && (
        <Suspense fallback={null}>
          <AgentEditModal />
        </Suspense>
      )}
    </div>
  );
}
