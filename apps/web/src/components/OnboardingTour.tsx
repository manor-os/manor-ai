import { useLocation } from "react-router-dom";
import SpotlightTour, { type TourStep } from "./SpotlightTour";
import { t } from "../lib/i18n";
import { useDisplayPreferences } from "../stores/displayPreferences";

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
    title: t("page.app_layout.ai_chats"),
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
    target: "[data-tour='create-workspace']",
    title: t("page.workspaces.create_workspace"),
    description: t("page.workspace_draft_chat.tell_me_what_you_want_to_build_i_ll_draft_the_op"),
    position: "right",
    mode: "chat",
    openSidebar: true,
  },
  {
    target: "[data-tour='chat-sidebar-group']",
    title: t("component.onboarding_tour.simple_group_chats_title"),
    description: t("component.onboarding_tour.simple_group_chats"),
    position: "right",
    mode: "chat",
    openSidebar: true,
  },
  {
    target: "[data-tour='chat-sidebar-dm']",
    title: t("component.onboarding_tour.simple_direct_chats_title"),
    description: t("component.onboarding_tour.simple_direct_chats"),
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
    title: t("component.onboarding_tour.chat_and_workspace"),
    description: t("component.onboarding_tour.switch_between_chatting_with_ai_agents_and_managing_yo"),
    position: "right",
  },
  {
    // FloatingChat bubble in workspace mode; the page composer in chat mode.
    target: "[data-tour='chat-input'], .chat-composer",
    title: t("component.onboarding_tour.ai_chat"),
    description: t("component.onboarding_tour.ask_anything_your_ai_can_search_the_web_write_files_cr"),
    position: "top",
  },
  {
    target: "[data-tour='nav-dashboard']",
    title: t("nav.dashboard"),
    description: t("component.onboarding_tour.your_command_center_daily_brief_task_stats_and_anythin"),
    position: "right",
    mode: "workspace",
  },
  {
    target: "[data-tour='nav-tasks']",
    title: t("component.onboarding_tour.tasks_and_automations"),
    description: t("component.onboarding_tour.follow_every_task_your_agents_run_the_automations_tab"),
    position: "right",
    mode: "workspace",
  },
  {
    target: "[data-tour='nav-workspaces']",
    title: t("nav.workspaces"),
    description: t("component.onboarding_tour.organize_your_operations_into_workspaces_each_one_has"),
    position: "right",
    mode: "workspace",
  },
  {
    target: "[data-tour='nav-knowledge']",
    title: t("page.knowledge.knowledge_base"),
    description: t("component.onboarding_tour.upload_documents_here_your_ai_agents_can_search_and_re"),
    position: "right",
    mode: "workspace",
  },
  {
    target: "[data-tour='nav-team']",
    title: t("nav.people"),
    description: t("component.onboarding_tour.invite_teammates_manage_roles_and_decide_who_can_view"),
    position: "right",
    mode: "workspace",
  },
  {
    target: "[data-tour='configure-menu']",
    title: t("page.apps.configure"),
    description: t("component.onboarding_tour.set_up_advanced_capabilities_here_agents_integrations"),
    position: "right",
    mode: "workspace",
  },
  {
    target: "[data-tour='nav-agents']",
    title: t("nav.agents"),
    description: t("component.onboarding_tour.create_ai_agents_with_their_own_roles_models_and_tools"),
    position: "right",
    mode: "workspace",
    openConfigure: true,
  },
  {
    target: "[data-tour='nav-skills']",
    title: t("nav.skills"),
    description: t("component.onboarding_tour.skills_are_reusable_playbooks_that_teach_agents_how_to"),
    position: "right",
    mode: "workspace",
    openConfigure: true,
  },
  {
    target: "[data-tour='nav-integrations']",
    title: t("nav.integrations"),
    description: t("component.onboarding_tour.connect_external_services_email_calendars_storefronts"),
    position: "right",
    mode: "workspace",
    openConfigure: true,
  },
];

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
  if (isTourSuppressedPath(location.pathname)) return null;
  return (
    <SpotlightTour
      key={interfaceMode}
      steps={interfaceMode === "simple" ? SIMPLE_TOUR_STEPS : TOUR_STEPS}
      storageKey={MAIN_TOUR_STORAGE_KEY}
      startEvent="manor:start-tour"
      enabled={enabled}
    />
  );
}
