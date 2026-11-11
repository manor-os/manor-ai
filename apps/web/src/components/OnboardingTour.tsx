import { useLocation } from "react-router-dom";
import SpotlightTour, { type TourStep } from "./SpotlightTour";
import { t } from "../lib/i18n";
import { useDisplayPreferences } from "../stores/displayPreferences";
import { useConfigStore } from "../stores/config";

/**
 * Sidebar onboarding tour for new users.
 * Simple mode covers everyday chat and collaboration; professional mode covers the shell.
 */

const SIMPLE_TOUR_STEPS: TourStep[] = [
  {
    target: "[data-tour='chat-input'], .chat-composer",
    title: t("nav.chat"),
    description: t("component.onboarding_tour.simple_chat"),
    position: "top",
    mode: "chat",
    openSidebar: false,
  },
  {
    target: "[data-tour='chat-sidebar-ai-chats']",
    title: t("page.app_layout.conversations"),
    description: t("component.onboarding_tour.simple_ai_chats"),
    position: "right",
    mode: "chat",
    openSidebar: true,
  },
  {
    target: "[data-tour='chat-sidebar-workspaces']",
    title: t("page.app_layout.workspaces"),
    description: t("component.onboarding_tour.simple_workspaces"),
    position: "right",
    mode: "chat",
    openSidebar: true,
  },
  {
    // One "+" starts a chat, a Workspace, a group or a direct chat.
    target: "[data-tour='create-workspace']",
    title: t("page.workspaces.create_workspace"),
    description: t("page.workspace_draft_chat.tell_me_what_you_want_to_build_i_ll_draft_the_op"),
    position: "right",
    mode: "chat",
    openSidebar: true,
  },
  {
    target: "[data-tour='sidebar-more']",
    title: t("nav.more"),
    description: t("component.onboarding_tour.simple_more"),
    position: "right",
    mode: "chat",
    openSidebar: true,
  },
];

const TOUR_STEPS: TourStep[] = [
  {
    target: "[data-tour='mode-switcher']",
    title: t("component.onboarding_tour.places_title"),
    description: t("component.onboarding_tour.places_description"),
    position: "right",
  },
  {
    target: "[data-tour='chat-input'], .chat-composer",
    title: t("component.onboarding_tour.ai_chat"),
    description: t("component.onboarding_tour.ask_anything_your_ai_can_search_the_web_write_files_cr"),
    position: "top",
  },
  {
    target: "[data-tour='nav-tasks']",
    title: t("nav.tasks"),
    description: t("component.onboarding_tour.follow_every_task_your_agents_run_the_automations_tab"),
    position: "right",
  },
  {
    target: "[data-tour='nav-workspaces']",
    title: t("nav.workspaces"),
    description: t("component.onboarding_tour.organize_your_operations_into_workspaces_each_one_has"),
    position: "right",
  },
  {
    target: "[data-tour='nav-knowledge']",
    title: t("page.knowledge.knowledge_base"),
    description: t("component.onboarding_tour.upload_documents_here_your_ai_agents_can_search_and_re"),
    position: "right",
  },
  {
    target: "[data-tour='sidebar-more']",
    title: t("nav.more"),
    description: t("component.onboarding_tour.simple_more"),
    position: "right",
    openSidebar: true,
  },
];

// The marketplace is a cloud place; the sidebar shows it only there.
const MARKETPLACE_STEP: TourStep = {
  target: "[data-tour='nav-marketplace']",
  title: t("nav.marketplace"),
  description: t("component.onboarding_tour.marketplace_description"),
  position: "right",
};
const MARKETPLACE_STEP_AFTER = "[data-tour='nav-workspaces']";

export const MAIN_TOUR_STORAGE_KEY = "manor_tour_completed";

export function isTourSuppressedPath(pathname: string) {
  return (
    pathname.startsWith("/editor/") ||
    pathname.startsWith("/viewer/") ||
    pathname.startsWith("/diagram-canvas")
  );
}

export default function OnboardingTour({ enabled = true }: { enabled?: boolean }) {
  const location = useLocation();
  const interfaceMode = useDisplayPreferences((state) => state.interfaceMode);
  let showMarketplace = false;
  if (isTourSuppressedPath(location.pathname)) return null;
  const steps = !showMarketplace ? TOUR_STEPS : TOUR_STEPS.flatMap((step) =>
    step.target === MARKETPLACE_STEP_AFTER ? [step, MARKETPLACE_STEP] : [step]);
  return (
    <SpotlightTour
      key={`${interfaceMode}:${showMarketplace}`}
      steps={interfaceMode === "simple" ? SIMPLE_TOUR_STEPS : steps}
      storageKey={MAIN_TOUR_STORAGE_KEY}
      startEvent="manor:start-tour"
      enabled={enabled}
    />
  );
}
