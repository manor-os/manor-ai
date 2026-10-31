import { useEffect } from "react";
import { Outlet, useNavigate } from "react-router-dom";
import DetailDrawer from "./ui/DetailDrawer";
import { COLLABORATION_ENDED_EVENT, takeCollaborationEnded } from "../lib/persistentSession";
import { t } from "../lib/i18n";
import { useToastStore } from "../stores/toast";

/**
 * Pathless root route element. Global overlays mount here, inside the
 * router, because their bodies are built by pages and may use router links
 * and hooks; rendered beside <RouterProvider> those throw on first render.
 */
export default function RouterRoot() {
  const navigate = useNavigate();
  useEffect(() => {
    // The session already moved to the person's own company; leave the
    // customer Workspace page, which no longer exists for them.
    const ended = () => {
      if (!takeCollaborationEnded()) return;
      useToastStore.getState().info(t("collaboration.ended"));
      navigate("/", { replace: true });
    };
    // It may already have happened during startup, before this listener.
    ended();
    window.addEventListener(COLLABORATION_ENDED_EVENT, ended);
    return () => window.removeEventListener(COLLABORATION_ENDED_EVENT, ended);
  }, [navigate]);
  return (
    <>
      <Outlet />
      <DetailDrawer />
    </>
  );
}
