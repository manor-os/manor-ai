import {
  useState,
  useRef,
  useEffect,
  useLayoutEffect,
  useCallback,
  useMemo,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import {
  ApiError,
  api,
  isLocalFsUrl,
  resolveDisplayMediaUrl,
} from "../lib/api";
import { getAuthToken } from "../lib/authToken";
import { isMasterAgent } from "../lib/constants";
import {
  type ChatMessage,
  type HITLRequest,
  type SubAgentEvent,
  type ToolCall,
  isInternalFilePermissionMessage,
  hitlActionTranscriptText,
  parseToolCalls,
  pendingHITLIds,
} from "../lib/chatStream";
import { useChatStreamStore } from "../stores/chatStream";
import { useAuthStore } from "../stores/auth";
import type { Agent, Document, UserSummary } from "../lib/types";
import ChatMarkdown from "./ChatMarkdown";
import AssistantMessageBlocks from "./AssistantMessageBlocks";
import SessionSwitcher from "./SessionSwitcher";
import ChatMessageActions, {
  type ChatMessageFeedbackRating,
  displayContentForAssistantMessage,
  isRetryableAssistantMessage,
} from "./chat/ChatMessageActions";
import ManorAvatar from "./ui/ManorAvatar";
import UserAvatar from "./ui/UserAvatar";
import ChatActionCard, { ApprovalSummary } from "./ui/ChatActionCard";
import ApprovalActionBar from "./ui/ApprovalActionBar";
import InlineTips from "./ui/InlineTips";
import { DEFAULT_APPROVAL_OPTIONS } from "../lib/approvalOptions";
import ToolCallList from "./ui/ToolCallList";
import CreditLimitNotice from "./ui/CreditLimitNotice";
import ChatInputFooter, {
  createChatMessageAttachmentSnapshot,
  manualSkillLabel,
  stripManualSkillTokens,
  type AttachedItem,
  type ManualSkillItem,
  type MentionOption,
} from "./ChatInputFooter";
import {
  chatModeFromCapability,
  type ChatBoxMode,
} from "./ChatModeSelector";
import ChatModeToolbar from "./ChatModeToolbar";
import {
  getDefaultChatModePayload,
  getChatModeInputPlaceholder,
  type ChatModePayload,
} from "./ChatModeBriefPanel";
import {
  type ChatMessageDisplayReference,
  ChatMessageMetaChips,
  ChatMessageReferenceStrip,
  parseUserMessageDisplay,
  resolveChatMessageReferenceDocument,
} from "./ChatMessageDisplay";
import {
  clearPendingChatRetry,
  savePendingChatRetry,
  type PendingChatRetry,
} from "../lib/chatRetry";

function maybeLocalCodingRunNoticeForTools(_tools: ToolCall[]): string | null {
  return null;
}
import {
  IconAgent,
  IconCalendar,
  IconDocument,
  IconDownload,
  IconFlow,
  IconGrid4,
  IconLayers,
  IconPlay,
  IconReport,
  IconSparkles,
  IconWorkspace,
  type IconProps,
} from "./icons";
import { t } from "../lib/i18n";
import { isCodeLikeFile } from "../lib/codeFiles";


interface AgentInfo {
  id: string;
  name: string;
  color?: string;
  avatar_url?: string;
}

type ChatRetryRequest = Omit<PendingChatRetry, "createdAt">;

function hasVisibleUserContent(message: ChatMessage | undefined): boolean {
  return Boolean(
    message && message.role === "user" && (toDisplayText(message.content) || "").trim(),
  );
}

interface EmbeddedChatProps {
  conversationId: string;
  title: string;
  subtitle?: string;
  agents?: AgentInfo[];
  avatarUrl?: string; // DM agent avatar — if set, replaces ManorAvatar
  agentId?: string; // DM agent ID — ensures tools/prompt are resolved correctly
}

type ExecutionStatus =
  | "planned"
  | "running"
  | "needs_approval"
  | "done"
  | "failed";
type ArtifactFileCategory =
  | "text"
  | "markdown"
  | "code"
  | "html"
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "csv"
  | "json"
  | "docx"
  | "xlsx"
  | "diagram"
  | "unsupported";
type WorkspaceCapability =
  | "workspace"
  | "slides"
  | "docs"
  | "sheets"
  | "website"
  | "image"
  | "video"
  | "research"
  | "agents"
  | "automations";

type WorkspaceSamplePreviewContent = {
  label?: string;
  title?: string;
  lines?: string[];
  chips?: string[];
  imageSrc?: string;
  imageAlt?: string;
  imageCaption?: string;
  previewImageSrc?: string;
  previewImageAlt?: string;
  detailImageSrcs?: string[];
  detailImageAlt?: string;
  sampleSrc?: string;
  sampleLabel?: string;
  videoSrc?: string;
};

const isVirtualAgentConversationId = (id?: string) =>
  !!id && id.startsWith("agent:");

type WorkspaceCapabilityConfig = {
  key: WorkspaceCapability;
  label: string;
  icon: string;
  accent: string;
  description: string;
  placeholder: string;
  templates: Array<{ label: string; prompt: string }>;
  samples: Array<{
    title: string;
    outcome: string;
    prompt: string;
    preview?: WorkspaceCapability;
    previewContent?: WorkspaceSamplePreviewContent;
  }>;
};

const BASE_WORKSPACE_CAPABILITIES: WorkspaceCapabilityConfig[] = [
  {
    key: "workspace",
    label: t("page.knowledge.workspace"),
    icon: "OS",
    accent: "#5d7f77",
    description:
      t("component.embedded_chat.run_multi_step_work_with_memory_files_agents_tasks_art"),
    placeholder:
      t("component.embedded_chat.assign_a_task_or_ask_manor_ai_to_plan_create_research"),
    templates: [
      {
        label: t("component.embedded_chat.solo_launch_os"),
        prompt:
          "Create a one-person company workspace to launch a product in 30 days. Build the goal map, weekly milestones, artifacts, agents, and follow-up cadence.",
      },
      {
        label: t("component.embedded_chat.founder_sales"),
        prompt:
          "Create a one-person company sales workspace with target accounts, outreach drafts, CRM-style pipeline, follow-ups, and weekly review.",
      },
      {
        label: t("component.embedded_chat.content_engine"),
        prompt:
          "Create a founder-led content workspace that turns product insights into weekly posts, newsletter drafts, landing page updates, and distribution tasks.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.solo_launch_os"),
        outcome:
          t("component.embedded_chat.outcome_solo_launch_os"),
        prompt:
          "Create a one-person company launch workspace for a new AI product. Build a goal map with product ship, waitlist growth, launch assets, feedback loop, weekly milestones, agents, artifacts, and next actions.",
        preview: "workspace",
      },
      {
        title: t("component.embedded_chat.founder_sales_pipeline"),
        outcome:
          t("component.embedded_chat.outcome_founder_sales_pipeline"),
        prompt:
          "Create a founder-led sales workspace for a one-person company. Build a goal map for ICP research, target accounts, outreach drafts, follow-ups, demos, objections, and weekly pipeline review.",
        preview: "workspace",
      },
      {
        title: t("component.embedded_chat.content_growth_engine"),
        outcome:
          t("component.embedded_chat.outcome_content_growth_engine"),
        prompt:
          "Create a content growth workspace for a solo founder. Build a goal map for weekly themes, post drafts, newsletter, distribution channels, landing page updates, metrics, and repurposing.",
        preview: "workspace",
      },
      {
        title: t("component.embedded_chat.investor_prep_room"),
        outcome:
          t("component.embedded_chat.outcome_investor_prep_room"),
        prompt:
          "Create an investor prep workspace for a one-person company. Build a goal map for story, metrics, pitch deck, data room, investor list, outreach sequence, and follow-up system.",
        preview: "workspace",
      },
      {
        title: t("component.embedded_chat.customer_discovery_lab"),
        outcome:
          t("component.embedded_chat.outcome_customer_discovery_lab"),
        prompt:
          "Create a customer discovery workspace for a solo founder. Build a goal map for interview targets, scripts, notes synthesis, pain patterns, positioning, objections, and product roadmap decisions.",
        preview: "workspace",
      },
      {
        title: t("component.embedded_chat.weekly_operator_review"),
        outcome:
          t("component.embedded_chat.outcome_weekly_operator_review"),
        prompt:
          "Create a weekly operating review workspace for a one-person company. Build a goal map for revenue, product, content, customer feedback, blockers, metrics, and next-week priorities.",
        preview: "workspace",
      },
    ],
  },
  {
    key: "slides",
    label: t("component.embedded_chat.slides"),
    icon: "PPT",
    accent: "#4869ac",
    description:
      t("component.embedded_chat.create_decks_from_goals_notes_files_or_research_with_s"),
    placeholder: t("component.embedded_chat.describe_the_deck_you_want_to_create"),
    templates: [
      {
        label: t("component.embedded_chat.pitch_deck"),
        prompt:
          "Create a seed round pitch deck with problem, solution, product, market, traction, team, and ask.",
      },
      {
        label: t("component.embedded_chat.launch_deck"),
        prompt:
          "Create a product launch deck with positioning, audience, channels, timeline, and success metrics.",
      },
      {
        label: t("component.embedded_chat.doc_to_slides"),
        prompt:
          "Turn this document into a concise 10-slide presentation with speaker notes.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.seed_round_pitch_deck"),
        outcome:
          t("component.embedded_chat.outcome_seed_round_pitch_deck"),
        prompt:
          "Create a seed round pitch deck for Manor AI with problem, solution, product, market, traction, business model, competition, team, and ask.",
        preview: "slides",
      },
      {
        title: t("component.embedded_chat.document_to_deck"),
        outcome: t("component.embedded_chat.outcome_document_to_deck"),
        prompt:
          "Turn this sample memo into a 10-slide investor deck with sharp titles, speaker notes, and visual direction.\n\nSample memo: Manor AI helps solo founders run a company from one workspace. Users set a goal, Manor breaks it into milestones, coordinates specialized agents, creates artifacts like decks and docs, and keeps a weekly operating rhythm. Early users want faster launch planning, clearer investor materials, and less context switching.",
        preview: "slides",
      },
      {
        title: t("component.embedded_chat.product_launch_deck"),
        outcome:
          t("component.embedded_chat.outcome_product_launch_deck"),
        prompt:
          "Create a product launch deck for a new AI workspace feature, including positioning, audience, rollout plan, and launch metrics.",
        preview: "slides",
      },
    ],
  },
  {
    key: "docs",
    label: t("page.manor_office.docs"),
    icon: "DOC",
    accent: "#4f7e87",
    description:
      t("component.embedded_chat.draft_polished_documents_memos_prds_briefs_and_reports"),
    placeholder: t("component.embedded_chat.describe_the_document_memo_or_brief_you_need"),
    templates: [
      {
        label: t("component.embedded_chat.strategy_memo"),
        prompt:
          "Write a strategy memo with context, options, recommendation, risks, and next actions.",
      },
      {
        label: t("component.embedded_chat.prd"),
        prompt:
          "Create a PRD with goals, users, requirements, open questions, and launch checklist.",
      },
      {
        label: t("component.embedded_chat.customer_brief"),
        prompt:
          "Create a customer brief with ICP, pains, triggers, objections, and messaging angles.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.strategy_memo"),
        outcome: t("component.embedded_chat.outcome_strategy_memo"),
        prompt:
          "Write a strategy memo for this real scenario: Manor AI is deciding whether the next 6 weeks should focus on sample workspaces, generated file quality, or integrations. Compare the three paths, recommend one, and include decision criteria, risks, and next actions.",
        preview: "docs",
        previewContent: {
          label: t("component.embedded_chat.preview_memo"),
          title: t("component.embedded_chat.preview_product_focus_title"),
          lines: [
            t("component.embedded_chat.preview_product_focus_line_1"),
            t("component.embedded_chat.preview_product_focus_line_2"),
            t("component.embedded_chat.preview_product_focus_line_3"),
          ],
          chips: [t("component.embedded_chat.preview_decision"), t("component.embedded_chat.preview_tradeoffs")],
        },
      },
      {
        title: t("component.embedded_chat.customer_brief"),
        outcome: t("component.embedded_chat.outcome_customer_brief"),
        prompt:
          "Create a customer brief for this ICP: solo founders building AI-enabled SaaS products, usually pre-seed to seed stage, handling product, sales, content, and fundraising alone. Include pains, buying triggers, objections, and outreach angles.",
        preview: "docs",
        previewContent: {
          label: t("component.embedded_chat.preview_brief"),
          title: t("component.embedded_chat.preview_solo_founder_icp_title"),
          lines: [
            t("component.embedded_chat.preview_solo_founder_icp_line_1"),
            t("component.embedded_chat.preview_solo_founder_icp_line_2"),
            t("component.embedded_chat.preview_solo_founder_icp_line_3"),
          ],
          chips: [t("component.embedded_chat.preview_icp"), t("component.embedded_chat.preview_messaging")],
        },
      },
      {
        title: t("component.embedded_chat.prd_from_notes"),
        outcome: t("component.embedded_chat.outcome_prd_from_notes"),
        prompt:
          "Turn these sample notes into a PRD with goals, user stories, requirements, open questions, and launch checklist.\n\nNotes: New chat should show runnable samples. Each sample needs a concrete prompt, a realistic preview, and a clear artifact outcome. Docs should include actual memo/brief content. Image samples should show a real image thumbnail. Selecting a sample fills the composer with a self-contained prompt.",
        preview: "docs",
        previewContent: {
          label: t("component.embedded_chat.prd"),
          title: t("component.embedded_chat.preview_new_chat_samples_title"),
          lines: [
            t("component.embedded_chat.preview_new_chat_samples_line_1"),
            t("component.embedded_chat.preview_new_chat_samples_line_2"),
            t("component.embedded_chat.preview_new_chat_samples_line_3"),
          ],
          chips: [t("component.embedded_chat.preview_stories"), t("component.embedded_chat.preview_checklist")],
        },
      },
    ],
  },
  {
    key: "sheets",
    label: t("component.embedded_chat.sheets"),
    icon: "XLS",
    accent: "#44895f",
    description:
      t("component.embedded_chat.build_trackers_models_kpi_dashboards_and_planning_shee"),
    placeholder: t("component.embedded_chat.describe_the_model_tracker_or_analysis_you_need"),
    templates: [
      {
        label: t("component.embedded_chat.kpi_dashboard"),
        prompt:
          "Create a weekly KPI dashboard with owners, status, trend notes, and next actions.",
      },
      {
        label: t("component.embedded_chat.budget_model"),
        prompt:
          "Create a 12-month budget model with assumptions, hiring, revenue scenarios, and burn analysis.",
      },
      {
        label: t("component.embedded_chat.lead_tracker"),
        prompt:
          "Build a lead tracker with scoring, stage, owner, next action, and expected close date.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.operating_dashboard"),
        outcome: t("component.embedded_chat.outcome_operating_dashboard"),
        prompt:
          "Create an operating dashboard spreadsheet for weekly review with KPIs, owners, status, and trend notes.",
        preview: "sheets",
      },
      {
        title: t("component.embedded_chat.budget_model"),
        outcome: t("component.embedded_chat.outcome_budget_model"),
        prompt:
          "Create a 12-month budget model with assumptions, hiring plan, revenue scenarios, and burn analysis.",
        preview: "sheets",
      },
      {
        title: t("component.embedded_chat.lead_tracker"),
        outcome: t("component.embedded_chat.outcome_lead_tracker"),
        prompt:
          "Build a lead tracker with scoring, stage, owner, next action, and expected close date.",
        preview: "sheets",
      },
    ],
  },
  {
    key: "website",
    label: t("page.team_people.website"),
    icon: "WEB",
    accent: "#cf9b44",
    description:
      t("component.embedded_chat.generate_landing_pages_campaign_sites_docs_pages_and_r"),
    placeholder: t("component.embedded_chat.describe_the_website_or_landing_page_to_build"),
    templates: [
      {
        label: t("component.embedded_chat.landing_page"),
        prompt:
          "Build a polished landing page with hero, problem, product sections, proof, FAQ, and CTA.",
      },
      {
        label: t("component.embedded_chat.microsite"),
        prompt:
          "Create a campaign microsite with messaging, benefits, examples, FAQ, and signup CTA.",
      },
      {
        label: t("component.embedded_chat.docs_page"),
        prompt:
          "Turn this outline into a clean docs-style page with navigation and examples.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.landing_page"),
        outcome: t("component.embedded_chat.outcome_landing_page"),
        prompt:
          "Build a polished landing page for Manor AI with hero, problem, product sections, social proof, and CTA.",
        preview: "website",
      },
      {
        title: t("component.embedded_chat.campaign_microsite"),
        outcome: t("component.embedded_chat.outcome_campaign_microsite"),
        prompt:
          "Create a campaign microsite for an AI agent launch, including messaging, benefits, FAQ, and signup CTA.",
        preview: "website",
      },
      {
        title: t("component.embedded_chat.docs_style_page"),
        outcome: t("component.embedded_chat.outcome_docs_style_page"),
        prompt:
          "Turn this outline into a clean docs-style web page with navigation, sections, and examples.",
        preview: "website",
      },
    ],
  },
  {
    key: "image",
    label: t("page.account.image"),
    icon: "IMG",
    accent: "#db2777",
    description:
      t("component.embedded_chat.create_visual_assets_hero_images_social_graphics_and_b"),
    placeholder: t("component.embedded_chat.describe_the_image_visual_or_brand_asset_you_want"),
    templates: [
      {
        label: t("component.embedded_chat.hero_visual"),
        prompt:
          "Create a product hero image for an AI workspace OS, premium and clean, showing agents coordinating work.",
      },
      {
        label: t("component.embedded_chat.social_graphic"),
        prompt:
          "Create a social launch graphic announcing workspace agents and generated artifacts.",
      },
      {
        label: t("component.embedded_chat.brand_illustration"),
        prompt:
          "Create a brand illustration of an AI chief of staff organizing documents, tasks, and outputs.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.product_hero_visual"),
        outcome: t("component.embedded_chat.outcome_product_hero_visual"),
        prompt:
          "Create a product hero image for an AI workspace OS. Use this concrete art direction: a bright modern operating room for solo founders, centered dashboard, file artifacts floating near agent avatars, warm natural light, premium SaaS polish, no text in the image.",
        preview: "image",
        previewContent: {
          imageSrc: "/assets/office/interior.png",
          imageAlt: t("component.embedded_chat.preview_office_image_alt"),
          imageCaption: t("component.embedded_chat.preview_workspace_hero_reference"),
        },
      },
      {
        title: t("component.embedded_chat.social_launch_graphic"),
        outcome: t("component.embedded_chat.outcome_social_launch_graphic"),
        prompt:
          "Create a square social launch graphic for Manor AI announcing workspace agents and generated artifacts. Use a real product-style composition: center workspace dashboard, surrounding document, deck, sheet, and image previews, bold but clean, no fake UI text.",
        preview: "image",
        previewContent: {
          imageSrc: "/assets/office/interior.png",
          imageAlt: t("component.embedded_chat.preview_launch_graphic_alt"),
          imageCaption: t("component.embedded_chat.preview_launch_graphic_reference"),
        },
      },
      {
        title: t("component.embedded_chat.brand_illustration"),
        outcome: t("component.embedded_chat.outcome_brand_illustration"),
        prompt:
          "Create a reusable brand illustration of an AI chief of staff organizing documents, tasks, and outputs. Make it feel like a real workspace scene with desks, screens, files, and visual artifacts, optimistic but not cartoonish, no text.",
        preview: "image",
        previewContent: {
          imageSrc: "/assets/office/interior.png",
          imageAlt: t("component.embedded_chat.preview_brand_illustration_alt"),
          imageCaption: t("component.embedded_chat.preview_brand_scene_reference"),
        },
      },
    ],
  },
  {
    key: "video",
    label: t("page.account.video"),
    icon: "VID",
    accent: "#6f4ba8",
    description:
      t("component.embedded_chat.prepare_scripts_storyboards_shot_lists_and_production"),
    placeholder:
      t("component.embedded_chat.describe_the_video_or_storyboard_you_want_manor_to_pre"),
    templates: [
      {
        label: t("component.embedded_chat.demo_script"),
        prompt:
          "Create a 60-second product demo script with scenes, voiceover, and visual direction.",
      },
      {
        label: t("component.embedded_chat.launch_teaser"),
        prompt: "Create a 30-second launch teaser optimized for social media.",
      },
      {
        label: t("component.embedded_chat.explainer"),
        prompt:
          "Create a 90-second explainer storyboard showing how Manor turns goals into files, tasks, and artifacts.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.product_demo_script"),
        outcome: t("component.embedded_chat.outcome_product_demo_script"),
        prompt:
          "Create a 60-second product demo video script for Manor AI, including scenes, voiceover, and visual direction.",
        preview: "video",
      },
      {
        title: t("component.embedded_chat.launch_teaser"),
        outcome: t("component.embedded_chat.outcome_launch_teaser"),
        prompt:
          "Create a 30-second launch teaser for an AI workspace OS, optimized for social media.",
        preview: "video",
      },
      {
        title: t("component.embedded_chat.explainer_video"),
        outcome: t("component.embedded_chat.outcome_explainer_video"),
        prompt:
          "Create a 90-second explainer video storyboard showing how Manor turns goals into files, tasks, and artifacts.",
        preview: "video",
      },
    ],
  },
  {
    key: "research",
    label: t("page.tasks.research"),
    icon: "R&D",
    accent: "#5a8ea6",
    description:
      t("component.embedded_chat.research_markets_companies_competitors_tools_and_synth"),
    placeholder: t("component.embedded_chat.ask_manor_to_research_compare_or_synthesize"),
    templates: [
      {
        label: t("component.embedded_chat.market_scan"),
        prompt:
          "Research the AI agent workspace market and summarize competitors, trends, risks, and opportunities.",
      },
      {
        label: t("component.embedded_chat.company_brief"),
        prompt:
          "Research this company and create a partnership brief with recent news, priorities, and outreach angle.",
      },
      {
        label: t("component.embedded_chat.tool_comparison"),
        prompt:
          "Compare these tools and recommend which one to use, with tradeoffs and next steps.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.market_scan"),
        outcome: t("component.embedded_chat.outcome_market_scan"),
        prompt:
          "Research the AI agent workspace market and create a concise report with competitors, trends, and opportunities.",
        preview: "research",
      },
      {
        title: t("component.embedded_chat.company_brief"),
        outcome: t("component.embedded_chat.outcome_company_brief"),
        prompt:
          "Research this company and create a partnership brief with business model, recent news, likely priorities, and outreach angle.",
        preview: "research",
      },
      {
        title: t("component.embedded_chat.tool_comparison"),
        outcome: t("component.embedded_chat.outcome_tool_comparison"),
        prompt:
          "Compare Manus, Genspark, Claude, and other AI agent platforms from a UX and product positioning perspective.",
        preview: "research",
      },
    ],
  },
  {
    key: "agents",
    label: t("nav.agents"),
    icon: "AI",
    accent: "#5a55a6",
    description:
      t("component.embedded_chat.design_assign_or_coordinate_specialized_agents_for_rep"),
    placeholder: t("component.embedded_chat.describe_the_agent_or_delegation_workflow_you_need"),
    templates: [
      {
        label: t("component.embedded_chat.research_agent"),
        prompt:
          "Design a research agent with role, tools, cadence, outputs, and escalation rules.",
      },
      {
        label: t("component.embedded_chat.delegate_project"),
        prompt:
          "Assign this project to the best agent and ask for a plan, milestones, and first deliverable.",
      },
      {
        label: t("component.embedded_chat.ops_rules"),
        prompt:
          "Create operating instructions for an agent that manages research, drafts, and updates.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.create_research_agent"),
        outcome: t("component.embedded_chat.outcome_create_research_agent"),
        prompt:
          "Design a research agent for competitor monitoring, including tools, cadence, outputs, and escalation rules.",
        preview: "agents",
      },
      {
        title: t("component.embedded_chat.delegate_work"),
        outcome: t("component.embedded_chat.outcome_delegate_work"),
        prompt:
          "Assign this project to the best agent and ask it to prepare a plan, milestones, and first deliverable.",
        preview: "agents",
      },
      {
        title: t("component.embedded_chat.agent_operating_rules"),
        outcome: t("component.embedded_chat.outcome_agent_operating_rules"),
        prompt:
          "Create operating instructions for a sales ops agent that manages lead research, outreach drafts, and CRM updates.",
        preview: "agents",
      },
    ],
  },
  {
    key: "automations",
    label: t("page.tasks.automations"),
    icon: "AUTO",
    accent: "#b66a3c",
    description:
      t("component.embedded_chat.set_up_recurring_reviews_monitors_reminders_alerts_and"),
    placeholder: t("component.embedded_chat.describe_what_manor_should_monitor_or_run_repeatedly"),
    templates: [
      {
        label: t("component.embedded_chat.weekly_review"),
        prompt:
          "Set up a weekly operating review every Friday with progress, blockers, and next priorities.",
      },
      {
        label: t("component.embedded_chat.competitor_monitor"),
        prompt:
          "Monitor competitors weekly and create a digest of launches, pricing, and messaging shifts.",
      },
      {
        label: t("component.embedded_chat.follow_up_system"),
        prompt:
          "Create a daily follow-up automation for open customer conversations and overdue tasks.",
      },
    ],
    samples: [
      {
        title: t("component.embedded_chat.weekly_review"),
        outcome: t("component.embedded_chat.outcome_weekly_review"),
        prompt:
          "Set up a weekly operating review every Friday that summarizes progress, blockers, and priorities for next week.",
        preview: "automations",
      },
      {
        title: t("component.embedded_chat.competitor_monitor"),
        outcome: t("component.embedded_chat.outcome_competitor_monitor"),
        prompt:
          "Monitor competitors weekly and create a short digest with product launches, pricing changes, and messaging shifts.",
        preview: "automations",
      },
      {
        title: t("component.embedded_chat.follow_up_system"),
        outcome: t("component.embedded_chat.outcome_follow_up_system"),
        prompt:
          "Create a follow-up automation for open customer conversations and overdue tasks, with a daily summary.",
        preview: "automations",
      },
    ],
  },
];

type SideHustleSampleDefinition = {
  id: string;
  preview: WorkspaceCapability;
  imageSrc?: string;
  sampleSrc?: string;
  videoSrc?: string;
};

const sideHustleText = (key: string) =>
  t(`component.embedded_chat.side_hustle.${key}`);

const SIDE_HUSTLE_SAMPLE_DEFINITIONS: Record<
  WorkspaceCapability,
  SideHustleSampleDefinition[]
> = {
  workspace: [
    {
      id: "coffee_popup",
      preview: "workspace",
      sampleSrc: "/assets/samples/artifacts/workspace/coffee-popup-workspace.html",
    },
    {
      id: "designer_studio",
      preview: "workspace",
      sampleSrc: "/assets/samples/artifacts/workspace/designer-studio-workspace.html",
    },
    {
      id: "manga_serial",
      preview: "workspace",
      sampleSrc: "/assets/samples/artifacts/workspace/manga-serial-workspace.html",
    },
  ],
  slides: [
    {
      id: "wedding_photo_quote",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/wedding-photography-proposal.pptx",
    },
    {
      id: "candle_channel_pitch",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/candle-retail-pitch.pptx",
    },
    {
      id: "course_launch_plan",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/course-launch-plan.pptx",
    },
  ],
  docs: [
    {
      id: "personal_company_copy",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/personal-company-copy.docx",
    },
    {
      id: "side_hustle_product_manual",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/side-hustle-product-manual.docx",
    },
    {
      id: "travel_ebook_plan",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/travel-ebook-plan.docx",
    },
  ],
  sheets: [
    {
      id: "side_income_dashboard",
      preview: "sheets",
      sampleSrc: "/assets/samples/artifacts/sheets/side-income-dashboard.xlsx",
    },
    {
      id: "content_calendar",
      preview: "sheets",
      sampleSrc: "/assets/samples/artifacts/sheets/content-calendar.xlsx",
    },
    {
      id: "craft_inventory_costs",
      preview: "sheets",
      sampleSrc: "/assets/samples/artifacts/sheets/craft-inventory-costs.xlsx",
    },
  ],
  website: [
    {
      id: "personal_consulting_site",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/personal-ai-consulting-company.html",
    },
    {
      id: "anime_short_series_site",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/anime-short-series-site.html",
    },
    {
      id: "coffee_popup_booking_site",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/coffee-popup-booking-site.html",
    },
  ],
  image: [
    {
      id: "digital_product_cover",
      preview: "image",
      imageSrc: "/assets/samples/digital-product-cover.jpg",
      sampleSrc: "/assets/samples/digital-product-cover.jpg",
    },
    {
      id: "coffee_brand_visual",
      preview: "image",
      imageSrc: "/assets/samples/coffee-brand-visual.jpg",
      sampleSrc: "/assets/samples/coffee-brand-visual.jpg",
    },
    {
      id: "manga_character_poster",
      preview: "image",
      imageSrc: "/assets/samples/ai-manga-poster.jpg",
      sampleSrc: "/assets/samples/ai-manga-poster.jpg",
    },
  ],
  video: [
    {
      id: "ugc_resume_ad",
      preview: "video",
      sampleSrc: "/viewer/01KQVY1GEGJAGFCCEGCNAKYPQE",
      videoSrc: "/assets/samples/artifacts/video/work-cat-interview-day.mp4",
    },
    {
      id: "course_teaser_script",
      preview: "video",
      sampleSrc: "/viewer/01KRFCF974DR2ZH9HWQTTG6898",
      videoSrc: "/assets/samples/artifacts/video/peach-garden-wide-shot.mp4",
    },
    {
      id: "personal_brand_intro",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/personal-brand-intro.mp4",
    },
  ],
  research: [
    {
      id: "xiaohongshu_side_hustle_scan",
      preview: "research",
      sampleSrc: "/assets/samples/artifacts/research/xiaohongshu-side-hustle-scan.html",
    },
    {
      id: "digital_product_competitors",
      preview: "research",
      sampleSrc: "/assets/samples/artifacts/research/digital-product-competitors.html",
    },
    {
      id: "local_service_pricing",
      preview: "research",
      sampleSrc: "/assets/samples/artifacts/research/local-service-pricing.html",
    },
  ],
  agents: [
    {
      id: "personal_brand_content_agent",
      preview: "agents",
      sampleSrc: "/assets/samples/artifacts/agents/personal-brand-content-agent.html",
    },
    {
      id: "side_hustle_finance_agent",
      preview: "agents",
      sampleSrc: "/assets/samples/artifacts/agents/side-hustle-finance-agent.html",
    },
    {
      id: "client_followup_agent",
      preview: "agents",
      sampleSrc: "/assets/samples/artifacts/agents/client-followup-agent.html",
    },
  ],
  automations: [
    {
      id: "weekly_side_hustle_review",
      preview: "automations",
      sampleSrc: "/assets/samples/artifacts/automations/weekly-side-hustle-review.html",
    },
    {
      id: "content_publish_reminder",
      preview: "automations",
      sampleSrc: "/assets/samples/artifacts/automations/content-publish-reminder.html",
    },
    {
      id: "client_delivery_reminder",
      preview: "automations",
      sampleSrc: "/assets/samples/artifacts/automations/client-delivery-reminder.html",
    },
  ],
};

function createSideHustleSample(
  capability: WorkspaceCapability,
  {
    id,
    preview,
    imageSrc,
    sampleSrc,
    videoSrc: explicitVideoSrc,
  }: SideHustleSampleDefinition,
): WorkspaceCapabilityConfig["samples"][number] {
  const key = `${capability}.${id}`;
  const videoSrc =
    explicitVideoSrc ||
    (capability === "video" && sampleSrc?.match(/\.(mp4|webm|mov|m4v)$/i)
      ? sampleSrc
      : undefined);
  const detailImageSrcs = [1, 2, 3].map(
    (page) =>
      `/assets/samples/details/${capability}/${id}-${String(page).padStart(2, "0")}.png`,
  );
  const previewContent: WorkspaceSamplePreviewContent = imageSrc
    ? {
        imageSrc,
        imageAlt: sideHustleText(`${key}.image_alt`),
        imageCaption: sideHustleText(`${key}.image_caption`),
        detailImageSrcs,
        detailImageAlt: sideHustleText(`${key}.image_caption`),
        sampleSrc,
        sampleLabel: sideHustleText(`${capability}.sample_asset_label`),
        videoSrc,
      }
    : {
        label: sideHustleText(`${key}.label`),
        title: sideHustleText(`${key}.preview_title`),
        previewImageSrc: `/assets/samples/previews/${capability}/${id}.png`,
        previewImageAlt: sideHustleText(`${key}.preview_title`),
        detailImageSrcs,
        detailImageAlt: sideHustleText(`${key}.preview_title`),
        lines: [
          sideHustleText(`${key}.line_1`),
          sideHustleText(`${key}.line_2`),
        ],
        chips: [
          sideHustleText(`${key}.chip_1`),
          sideHustleText(`${key}.chip_2`),
        ],
        sampleSrc,
        sampleLabel: sideHustleText(`${capability}.sample_asset_label`),
        videoSrc,
      };

  return {
    title: sideHustleText(`${key}.title`),
    outcome: sideHustleText(`${key}.outcome`),
    prompt: sideHustleText(`${key}.prompt`),
    preview,
    previewContent,
  };
}

const SIDE_HUSTLE_SAMPLES: Record<
  WorkspaceCapability,
  WorkspaceCapabilityConfig["samples"]
> = {
  workspace: SIDE_HUSTLE_SAMPLE_DEFINITIONS.workspace.map((definition) =>
    createSideHustleSample("workspace", definition),
  ),
  slides: SIDE_HUSTLE_SAMPLE_DEFINITIONS.slides.map((definition) =>
    createSideHustleSample("slides", definition),
  ),
  docs: SIDE_HUSTLE_SAMPLE_DEFINITIONS.docs.map((definition) =>
    createSideHustleSample("docs", definition),
  ),
  sheets: SIDE_HUSTLE_SAMPLE_DEFINITIONS.sheets.map((definition) =>
    createSideHustleSample("sheets", definition),
  ),
  website: SIDE_HUSTLE_SAMPLE_DEFINITIONS.website.map((definition) =>
    createSideHustleSample("website", definition),
  ),
  image: SIDE_HUSTLE_SAMPLE_DEFINITIONS.image.map((definition) =>
    createSideHustleSample("image", definition),
  ),
  video: SIDE_HUSTLE_SAMPLE_DEFINITIONS.video.map((definition) =>
    createSideHustleSample("video", definition),
  ),
  research: SIDE_HUSTLE_SAMPLE_DEFINITIONS.research.map((definition) =>
    createSideHustleSample("research", definition),
  ),
  agents: SIDE_HUSTLE_SAMPLE_DEFINITIONS.agents.map((definition) =>
    createSideHustleSample("agents", definition),
  ),
  automations: SIDE_HUSTLE_SAMPLE_DEFINITIONS.automations.map((definition) =>
    createSideHustleSample("automations", definition),
  ),
};

const SIDE_HUSTLE_COPY: Record<
  WorkspaceCapability,
  Pick<WorkspaceCapabilityConfig, "description" | "placeholder">
> = {
  workspace: {
    description: sideHustleText("workspace.description"),
    placeholder: sideHustleText("workspace.placeholder"),
  },
  slides: {
    description: sideHustleText("slides.description"),
    placeholder: sideHustleText("slides.placeholder"),
  },
  docs: {
    description: sideHustleText("docs.description"),
    placeholder: sideHustleText("docs.placeholder"),
  },
  sheets: {
    description: sideHustleText("sheets.description"),
    placeholder: sideHustleText("sheets.placeholder"),
  },
  website: {
    description: sideHustleText("website.description"),
    placeholder: sideHustleText("website.placeholder"),
  },
  image: {
    description: sideHustleText("image.description"),
    placeholder: sideHustleText("image.placeholder"),
  },
  video: {
    description: sideHustleText("video.description"),
    placeholder: sideHustleText("video.placeholder"),
  },
  research: {
    description: sideHustleText("research.description"),
    placeholder: sideHustleText("research.placeholder"),
  },
  agents: {
    description: sideHustleText("agents.description"),
    placeholder: sideHustleText("agents.placeholder"),
  },
  automations: {
    description: sideHustleText("automations.description"),
    placeholder: sideHustleText("automations.placeholder"),
  },
};

const WORKSPACE_CAPABILITIES: WorkspaceCapabilityConfig[] =
  BASE_WORKSPACE_CAPABILITIES.map((capability) => {
    const samples = SIDE_HUSTLE_SAMPLES[capability.key];
    const copy = SIDE_HUSTLE_COPY[capability.key];
    return {
      ...capability,
      ...copy,
      samples,
      templates: samples.map(({ title, prompt, previewContent }) => ({
        label: previewContent?.label || title,
        prompt,
      })),
    };
  });

const CAPABILITY_ICON: Record<
  WorkspaceCapability,
  (props: IconProps) => JSX.Element
> = {
  workspace: IconWorkspace,
  slides: IconLayers,
  docs: IconDocument,
  sheets: IconGrid4,
  website: IconFlow,
  image: IconSparkles,
  video: IconPlay,
  research: IconReport,
  agents: IconAgent,
  automations: IconCalendar,
};

interface ProgressItem {
  id: string;
  title: string;
  detail?: string;
  status: ExecutionStatus;
}

interface OutputArtifact {
  id: string;
  kind:
    | "presentation"
    | "document"
    | "pdf"
    | "spreadsheet"
    | "diagram"
    | "code"
    | "file"
    | "image"
    | "video"
    | "audio"
    | "page"
    | "workspace"
    | "task"
    | "approval";
  title: string;
  status: ExecutionStatus;
  body?: string;
  href?: string;
  meta?: string;
  language?: string;
  data?: Record<string, any>;
}

type MessageInlinePart =
  | { kind: "text"; text: string; key: string }
  | {
      kind: "mention";
      token: string;
      mention: NonNullable<ChatMessage["mentions"]>[number];
      key: string;
    }
  | {
      kind: "attachment";
      token: string;
      attachment: NonNullable<ChatMessage["attachments"]>[number];
      key: string;
    };

function referenceArtifactKind(
  refItem: ChatMessageDisplayReference,
  doc?: Document | null,
): OutputArtifact["kind"] {
  if (doc) {
    return artifactKindFromRecord(
      {
        file_type: doc.file_type,
        mime_type: doc.mime_type,
      },
      doc.name,
    );
  }
  if (refItem.kind !== "file") return refItem.kind;
  return inferArtifactKindFromPath(refItem.url || refItem.name);
}

function artifactFromMessageReference(
  refItem: ChatMessageDisplayReference,
  doc?: Document | null,
): OutputArtifact {
  const documentId = doc?.id || refItem.id || undefined;
  const title = doc?.name || refItem.name;
  const directUrl = refItem.url;
  return {
    id: `chat-reference-${documentId || refItem.key}`,
    kind: referenceArtifactKind(refItem, doc),
    title,
    status: "done",
    href: directUrl,
    body: directUrl,
    meta: doc?.mime_type || refItem.mimeType || doc?.file_type || refItem.fileType,
    data: {
      source: "chat_reference",
      document_id: documentId,
      file_type: doc?.file_type || refItem.fileType,
      mime_type: doc?.mime_type || refItem.mimeType,
      fs_path: doc?.fs_path,
    },
  };
}

function stripAttachedLine(content: unknown) {
  return (toDisplayText(content) || "").replace(
    /\n{1,2}\[Attached: [\s\S]*?\]\s*$/u,
    "",
  );
}

function buildMessageInlineParts(
  msg: ChatMessage,
  contentOverride?: string,
): MessageInlinePart[] {
  const content =
    typeof contentOverride === "string"
      ? contentOverride
      : stripAttachedLine(msg.content);
  const matches: Array<{
    start: number;
    end: number;
    part: MessageInlinePart;
  }> = [];

  (msg.mentions || []).forEach((mention, index) => {
    const token = `@${mention.name}`;
    let start = content.indexOf(token);
    let count = 0;
    while (start >= 0) {
      matches.push({
        start,
        end: start + token.length,
        part: {
          kind: "mention",
          token,
          mention,
          key: `mention-${mention.type}-${mention.id}-${index}-${count}`,
        },
      });
      start = content.indexOf(token, start + token.length);
      count += 1;
    }
  });

  (msg.attachments || []).forEach((attachment, index) => {
    const token = `#${attachment.name}`;
    let start = content.indexOf(token);
    let count = 0;
    while (start >= 0) {
      matches.push({
        start,
        end: start + token.length,
        part: {
          kind: "attachment",
          token,
          attachment,
          key: `attachment-${attachment.id || attachment.name}-${index}-${count}`,
        },
      });
      start = content.indexOf(token, start + token.length);
      count += 1;
    }
  });

  const parts: MessageInlinePart[] = [];
  let cursor = 0;
  matches
    .sort((a, b) => a.start - b.start || b.end - a.end)
    .forEach((match, index) => {
      if (match.start < cursor) return;
      if (match.start > cursor) {
        parts.push({
          kind: "text",
          text: content.slice(cursor, match.start),
          key: `text-${index}-${cursor}`,
        });
      }
      parts.push(match.part);
      cursor = match.end;
    });
  if (cursor < content.length)
    parts.push({
      kind: "text",
      text: content.slice(cursor),
      key: `text-tail-${cursor}`,
    });
  if (parts.length === 0 && content)
    parts.push({ kind: "text", text: content, key: "text-only" });
  return parts;
}

function UserMessageContent({
  msg,
  content,
  onOpenReference,
}: {
  msg: ChatMessage;
  content?: string;
  onOpenReference?: (refItem: ChatMessageDisplayReference) => void;
}) {
  const parsed = parseUserMessageDisplay(
    typeof content === "string" ? { ...msg, content } : msg,
  );
  const contentText = parsed.cleanContent;
  const parts = buildMessageInlineParts(msg, contentText);
  const chips = parsed.chips;
  const references = parsed.references;
  return (
    <>
      {parts.length > 0 && (
        <div className="chat-user-rich-text">
          {parts.map((part) => {
            if (part.kind === "text")
              return <span key={part.key}>{part.text}</span>;
            if (part.kind === "mention") {
              return (
                <span
                  key={part.key}
                  className={`chat-message-inline-token chat-message-inline-token--${part.mention.type}`}
                >
                  <span className="chat-message-inline-avatar">
                    {part.mention.name.charAt(0).toUpperCase()}
                  </span>
                  <span className="chat-message-inline-main">
                    <strong>@{part.mention.name}</strong>
                    <small>{part.mention.type}</small>
                  </span>
                </span>
              );
            }
            const label = (
              part.attachment.fileType ||
              part.attachment.mimeType ||
              part.attachment.name.split(".").pop() ||
              "file"
            )
              .toUpperCase()
              .slice(0, 5);
          return (
            <span
              key={part.key}
              className="chat-message-inline-token chat-message-inline-token--attachment"
            >
              <span className="chat-message-inline-file">{label}</span>
              <span className="chat-message-inline-main">
                <strong>#{part.attachment.name}</strong>
                <small>
                  {part.attachment.mimeType ||
                    part.attachment.fileType ||
                    part.attachment.type ||
                    t("page.knowledge.file")}
                </small>
              </span>
            </span>
          );
          })}
        </div>
      )}
      <ChatMessageReferenceStrip
        references={references}
        align="right"
        onOpenReference={onOpenReference}
      />
      <ChatMessageMetaChips chips={chips} align="right" />
    </>
  );
}

const DEFAULT_DOC_PREVIEW_CONTENT: WorkspaceSamplePreviewContent = {
  label: t("component.embedded_chat.preview_sample_doc"),
  title: t("component.embedded_chat.preview_operating_memo"),
  lines: [
    t("component.embedded_chat.preview_default_doc_line_1"),
    t("component.embedded_chat.preview_default_doc_line_2"),
    t("component.embedded_chat.preview_default_doc_line_3"),
  ],
  chips: [t("component.embedded_chat.preview_memo"), t("component.embedded_chat.preview_actions")],
};

const DEFAULT_IMAGE_PREVIEW_CONTENT: WorkspaceSamplePreviewContent = {
  imageSrc: "/assets/office/interior.png",
  imageAlt: "Real office image sample",
  imageCaption: "Real image sample",
};

function WorkspaceSampleAssetBadge({
  content,
}: {
  content: WorkspaceSamplePreviewContent;
}) {
  if (!content.sampleSrc || !content.sampleLabel) return null;
  return (
    <a
      className="preview-real-sample-asset"
      href={content.sampleSrc}
      target="_blank"
      rel="noreferrer"
      onClick={(event) => event.stopPropagation()}
    >
      {content.sampleLabel}
    </a>
  );
}

function WorkspaceSampleContentPreview({
  kind,
  content,
}: {
  kind: WorkspaceCapability;
  content: WorkspaceSamplePreviewContent;
}) {
  const lines = (content.lines || []).slice(0, 2);
  const chips = (content.chips || []).slice(0, 2);
  const title =
    content.title || t("component.embedded_chat.preview_sample_document");

  if (kind === "workspace") {
    return (
      <article className="preview-real-sample preview-real-workspace">
        <span className="preview-real-sample-kicker">{content.label}</span>
        <h4>{title}</h4>
        <div className="preview-real-workspace-map">
          <span>{chips[0] || content.label}</span>
          <i />
          <span>{chips[1] || t("component.embedded_chat.ops")}</span>
        </div>
        <ul>
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  if (kind === "slides") {
    return (
      <article className="preview-real-sample preview-real-deck">
        <header>
          <span>{content.label}</span>
          <em>01 / 08</em>
        </header>
        <h4>{title}</h4>
        <ul>
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <div className="preview-real-deck-strip">
          {chips.map((chip) => (
            <span key={chip}>{chip}</span>
          ))}
        </div>
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  if (kind === "sheets") {
    return (
      <article className="preview-real-sample preview-real-sheet">
        <header>
          <span>{content.label}</span>
          <strong>{title}</strong>
        </header>
        <table>
          <thead>
            <tr>
              <th>{chips[0] || content.label}</th>
              <th>{chips[1] || t("component.embedded_chat.ops")}</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line) => {
              const [name, value = ""] = line.split(/:(.*)/s);
              return (
                <tr key={line}>
                  <td>{name.trim()}</td>
                  <td>{value.trim() || line}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  if (kind === "website") {
    return (
      <article className="preview-real-sample preview-real-site">
        <header>
          <i />
          <span>{chips[0]}</span>
          <span>{chips[1]}</span>
        </header>
        <h4>{title}</h4>
        {lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  if (kind === "video") {
    return (
      <article className="preview-real-sample preview-real-video">
        <header>
          <span>{content.label}</span>
          <strong>{title}</strong>
        </header>
        <div className="preview-real-video-frames">
          {lines.map((line, index) => (
            <section key={line}>
              <em>{index + 1}</em>
              <p>{line}</p>
            </section>
          ))}
        </div>
        <div className="preview-real-video-tags">
          {chips.map((chip) => (
            <span key={chip}>{chip}</span>
          ))}
        </div>
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  if (kind === "research") {
    return (
      <article className="preview-real-sample preview-real-report">
        <span className="preview-real-sample-kicker">{content.label}</span>
        <h4>{title}</h4>
        {lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
        <div>
          {chips.map((chip) => (
            <span key={chip}>{chip}</span>
          ))}
        </div>
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  if (kind === "agents") {
    return (
      <article className="preview-real-sample preview-real-agent">
        <h4>{title}</h4>
        <div className="preview-real-agent-flow">
          <span>{chips[0] || content.label}</span>
          <i />
          <span>{chips[1] || t("component.embedded_chat.ops")}</span>
        </div>
        {lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  if (kind === "automations") {
    return (
      <article className="preview-real-sample preview-real-automation">
        <span className="preview-real-sample-kicker">{content.label}</span>
        <h4>{title}</h4>
        <ol>
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ol>
        <div>
          {chips.map((chip) => (
            <span key={chip}>{chip}</span>
          ))}
        </div>
        <WorkspaceSampleAssetBadge content={content} />
      </article>
    );
  }

  return (
    <article className="preview-real-sample preview-real-doc">
      {content.label && (
        <span className="preview-real-sample-kicker">{content.label}</span>
      )}
      <h4>{title}</h4>
      <ul>
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      {chips.length > 0 && (
        <div className="preview-real-sample-tags">
          {chips.map((chip) => (
            <span key={chip}>{chip}</span>
          ))}
        </div>
      )}
      <WorkspaceSampleAssetBadge content={content} />
    </article>
  );
}

function WorkspaceSamplePreview({
  kind,
  content,
}: {
  kind: WorkspaceCapability;
  content?: WorkspaceSamplePreviewContent;
}) {
  const [isBrowsing, setIsBrowsing] = useState(false);
  const [isDetailPinned, setIsDetailPinned] = useState(false);
  const [activeDetailIndex, setActiveDetailIndex] = useState(0);
  const previewContent =
    content ||
    (kind === "docs"
      ? DEFAULT_DOC_PREVIEW_CONTENT
      : kind === "image"
        ? DEFAULT_IMAGE_PREVIEW_CONTENT
        : undefined);
  const detailImages = (previewContent?.detailImageSrcs || []).slice(0, 3);
  const hasBrowseDetails = detailImages.length > 0;
  const activeBrowseIndex = Math.min(
    activeDetailIndex,
    Math.max(detailImages.length - 1, 0),
  );
  const activeDetailSrc =
    hasBrowseDetails && (isBrowsing || isDetailPinned)
      ? detailImages[activeBrowseIndex]
      : undefined;
  const videoPreviewSrc =
    kind === "video" && previewContent?.videoSrc
      ? previewContent.videoSrc
      : undefined;
  const showDetailAt = (index: number) => {
    if (!hasBrowseDetails) return;
    setIsBrowsing(true);
    setIsDetailPinned(true);
    setActiveDetailIndex(
      Math.min(Math.max(index, 0), detailImages.length - 1),
    );
  };
  const showRelativeDetail = (step: number) => {
    if (!hasBrowseDetails) return;
    showDetailAt(
      (activeBrowseIndex + step + detailImages.length) % detailImages.length,
    );
  };
  const handleBrowseMove = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (detailImages.length < 2) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = Math.min(
      0.999,
      Math.max(0, (event.clientX - rect.left) / rect.width),
    );
    setIsDetailPinned(false);
    setActiveDetailIndex(Math.floor(ratio * detailImages.length));
  };
  const handleBrowseClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (detailImages.length < 2) return;
    if ((event.target as HTMLElement).closest("button")) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = Math.min(
      0.999,
      Math.max(0, (event.clientX - rect.left) / rect.width),
    );
    showDetailAt(Math.floor(ratio * detailImages.length));
  };
  const browseHandlers = hasBrowseDetails
    ? {
        onMouseEnter: () => setIsBrowsing(true),
        onMouseLeave: () => {
          setIsBrowsing(false);
          if (!isDetailPinned) {
            setActiveDetailIndex(0);
          }
        },
        onMouseMove: handleBrowseMove,
        onClick: handleBrowseClick,
      }
    : {};
  const renderBrowseControls = () =>
    hasBrowseDetails ? (
      <>
        {detailImages.length > 1 && (
          <>
            <button
              type="button"
              className="preview-sample-roller-button preview-sample-roller-button--prev"
              aria-label="Previous preview page"
              onClick={(event) => {
                event.stopPropagation();
                showRelativeDetail(-1);
              }}
            >
              {"<"}
            </button>
            <button
              type="button"
              className="preview-sample-roller-button preview-sample-roller-button--next"
              aria-label="Next preview page"
              onClick={(event) => {
                event.stopPropagation();
                showRelativeDetail(1);
              }}
            >
              {">"}
            </button>
          </>
        )}
        <div className="preview-sample-browse-indicator">
          {detailImages.map((src, index) => (
            <button
              key={src}
              type="button"
              className={index === activeBrowseIndex ? "is-active" : ""}
              aria-label={`Preview page ${index + 1}`}
              onClick={(event) => {
                event.stopPropagation();
                showDetailAt(index);
              }}
            >
              {String(index + 1).padStart(2, "0")}
            </button>
          ))}
        </div>
      </>
    ) : null;

  if (previewContent?.previewImageSrc) {
    return (
      <div
        className={`workspace-sample-preview workspace-sample-preview--${kind} ${hasBrowseDetails ? "workspace-sample-preview--browseable" : ""}`}
        {...browseHandlers}
      >
        <figure className="preview-image-art preview-sample-artifact">
          <img
            src={activeDetailSrc || previewContent.previewImageSrc}
            alt={previewContent.previewImageAlt || ""}
            loading="lazy"
          />
          {videoPreviewSrc && isBrowsing && (
            <video
              className="preview-sample-video"
              src={videoPreviewSrc}
              poster={previewContent.previewImageSrc}
              autoPlay
              muted
              loop
              playsInline
              preload="metadata"
              aria-label={previewContent.previewImageAlt || ""}
            />
          )}
          {renderBrowseControls()}
          <WorkspaceSampleAssetBadge content={previewContent} />
        </figure>
      </div>
    );
  }

  if (previewContent && kind !== "image") {
    return (
      <div
        className={`workspace-sample-preview workspace-sample-preview--${kind}`}
        aria-hidden="true"
      >
        <WorkspaceSampleContentPreview kind={kind} content={previewContent} />
      </div>
    );
  }

  return (
    <div
      className={`workspace-sample-preview workspace-sample-preview--${kind} ${hasBrowseDetails ? "workspace-sample-preview--browseable" : ""}`}
      {...browseHandlers}
    >
      {kind === "workspace" && (
        <>
          <div className="preview-workspace-node preview-workspace-node--main">
            {t("component.embedded_chat.goal")}</div>
          <div className="preview-workspace-node preview-workspace-node--a">
            {t("component.embedded_chat.ship")}</div>
          <div className="preview-workspace-node preview-workspace-node--b">
            {t("component.embedded_chat.sell")}</div>
          <div className="preview-workspace-node preview-workspace-node--c">
            {t("component.embedded_chat.learn")}</div>
          <div className="preview-workspace-node preview-workspace-node--d">
            {t("component.embedded_chat.ops")}</div>
          <span className="preview-workspace-line preview-workspace-line--a" />
          <span className="preview-workspace-line preview-workspace-line--b" />
          <span className="preview-workspace-line preview-workspace-line--c" />
          <span className="preview-workspace-line preview-workspace-line--d" />
        </>
      )}
      {kind === "slides" && (
        <>
          <div className="preview-slide-main">
            <span />
            <strong>{t("component.embedded_chat.pitch_deck_2")}</strong>
            <em />
          </div>
          <div className="preview-slide-strip">
            <i />
            <i />
            <i />
          </div>
        </>
      )}
      {kind === "docs" && (
        <article className="preview-doc-page preview-doc-page--real">
          {previewContent?.label && (
            <span className="preview-doc-kicker">{previewContent.label}</span>
          )}
          <h4>{previewContent?.title || t("component.embedded_chat.preview_sample_document")}</h4>
          <ul>
            {(previewContent?.lines || []).slice(0, 2).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {previewContent?.chips && previewContent.chips.length > 0 && (
            <div className="preview-doc-tags">
              {previewContent.chips.slice(0, 2).map((chip) => (
                <span key={chip}>{chip}</span>
              ))}
            </div>
          )}
        </article>
      )}
      {kind === "sheets" && (
        <div className="preview-sheet-grid">
          {Array.from({ length: 20 }).map((_, index) => (
            <span key={index} />
          ))}
        </div>
      )}
      {kind === "website" && (
        <div className="preview-web-page">
          <nav>
            <span />
            <span />
            <span />
          </nav>
          <strong />
          <p />
          <span className="preview-web-cta" />
        </div>
      )}
      {kind === "image" && (
        <figure className="preview-image-art">
          <img
            src={
              activeDetailSrc ||
              previewContent?.imageSrc ||
              DEFAULT_IMAGE_PREVIEW_CONTENT.imageSrc
            }
            alt={previewContent?.imageAlt || ""}
            loading="lazy"
          />
          {previewContent?.imageCaption && (
            <figcaption>{previewContent.imageCaption}</figcaption>
          )}
          {renderBrowseControls()}
        </figure>
      )}
      {kind === "video" && (
        <div className="preview-video-board">
          <div>
            <span />
          </div>
          <div>
            <span />
          </div>
          <div>
            <span />
          </div>
        </div>
      )}
      {kind === "research" && (
        <div className="preview-research-report">
          <strong />
          <span />
          <span />
          <span />
          <i />
        </div>
      )}
      {kind === "agents" && (
        <div className="preview-agent-flow">
          <span>A</span>
          <i />
          <span>B</span>
          <i />
          <span>C</span>
        </div>
      )}
      {kind === "automations" && (
        <div className="preview-automation-timeline">
          <span />
          <span />
          <span />
        </div>
      )}
    </div>
  );
}

function WorkspaceWelcome({
  activeCapability,
  onCapabilityChange,
  onSampleSelect,
}: {
  activeCapability: WorkspaceCapability;
  onCapabilityChange: (capability: WorkspaceCapability) => void;
  onSampleSelect: (prompt: string) => void;
}) {
  const selected =
    WORKSPACE_CAPABILITIES.find((item) => item.key === activeCapability) ||
    WORKSPACE_CAPABILITIES[0];
  const activeIndex = WORKSPACE_CAPABILITIES.findIndex(
    (item) => item.key === selected.key,
  );
  const modeRailRef = useRef<HTMLDivElement | null>(null);
  const activePillRef = useRef<HTMLButtonElement | null>(null);
  const wheelSwitchLockedRef = useRef(false);
  const dockDragStartXRef = useRef<number | null>(null);
  const dockDragLastXRef = useRef<number | null>(null);
  const dockClickCapabilityRef = useRef<WorkspaceCapability | null>(null);

  useEffect(() => {
    activePillRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
      inline: "center",
    });
  }, [selected.key]);

  const switchCapabilityByStep = useCallback(
    (step: number) => {
      if (step === 0) return;
      const nextIndex = Math.max(
        0,
        Math.min(WORKSPACE_CAPABILITIES.length - 1, activeIndex + step),
      );
      if (nextIndex !== activeIndex)
        onCapabilityChange(WORKSPACE_CAPABILITIES[nextIndex].key);
    },
    [activeIndex, onCapabilityChange],
  );

  useEffect(() => {
    const rail = modeRailRef.current;
    if (!rail) return;
    const handleWheel = (event: WheelEvent) => {
      const delta =
        Math.abs(event.deltaX) > Math.abs(event.deltaY)
          ? event.deltaX
          : event.deltaY;
      if (!delta) return;
      event.preventDefault();
      if (wheelSwitchLockedRef.current) return;
      wheelSwitchLockedRef.current = true;
      switchCapabilityByStep(delta > 0 ? 1 : -1);
      window.setTimeout(() => {
        wheelSwitchLockedRef.current = false;
      }, 180);
    };
    rail.addEventListener("wheel", handleWheel, { passive: false });
    return () => rail.removeEventListener("wheel", handleWheel);
  }, [switchCapabilityByStep]);

  return (
    <div
      className="workspace-welcome"
      style={{ "--capability-accent": selected.accent } as CSSProperties}
    >
      <div className="workspace-welcome-kicker">{t("component.embedded_chat.manor_ai_workspace")}</div>
      <h1>{t("component.embedded_chat.start_with_sample_or_prompt")}</h1>

      <div
        ref={modeRailRef}
        className="workspace-mode-rail"
        role="tablist"
        aria-label={t("component.embedded_chat.workspace_capability_selector")}
        onPointerDown={(event) => {
          const targetPill = (event.target as HTMLElement).closest<HTMLElement>(
            ".workspace-mode-pill",
          );
          const targetCapability = targetPill?.dataset
            .capability as WorkspaceCapability | undefined;
          dockClickCapabilityRef.current =
            targetCapability &&
            WORKSPACE_CAPABILITIES.some(
              (capability) => capability.key === targetCapability,
            )
              ? targetCapability
              : null;
          dockDragStartXRef.current = event.clientX;
          dockDragLastXRef.current = event.clientX;
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (dockDragStartXRef.current == null) return;
          dockDragLastXRef.current = event.clientX;
        }}
        onPointerUp={(event) => {
          const startX = dockDragStartXRef.current;
          const lastX = dockDragLastXRef.current;
          dockDragStartXRef.current = null;
          dockDragLastXRef.current = null;
          event.currentTarget.releasePointerCapture?.(event.pointerId);
          if (startX == null || lastX == null) return;
          const diff = lastX - startX;
          if (Math.abs(diff) < 34) {
            const clickedCapability = dockClickCapabilityRef.current;
            dockClickCapabilityRef.current = null;
            if (clickedCapability) onCapabilityChange(clickedCapability);
            return;
          }
          dockClickCapabilityRef.current = null;
          switchCapabilityByStep(diff < 0 ? 1 : -1);
        }}
        onPointerCancel={() => {
          dockDragStartXRef.current = null;
          dockDragLastXRef.current = null;
          dockClickCapabilityRef.current = null;
        }}
      >
        {WORKSPACE_CAPABILITIES.map((capability, index) => {
          const distance = Math.abs(index - activeIndex);
          const direction =
            index === activeIndex ? 0 : index < activeIndex ? -1 : 1;
          const CapabilityIcon = CAPABILITY_ICON[capability.key];
          const isActive = capability.key === selected.key;
          return (
            <button
              key={capability.key}
              ref={isActive ? activePillRef : undefined}
              type="button"
              role="tab"
              aria-selected={isActive}
              className={`workspace-mode-pill ${isActive ? "workspace-mode-pill--active" : ""}`}
              data-distance={Math.min(distance, 3)}
              data-direction={direction}
              data-capability={capability.key}
              style={
                { "--capability-accent": capability.accent } as CSSProperties
              }
              onClick={() => onCapabilityChange(capability.key)}
            >
              <span>
                <CapabilityIcon size={18} />
              </span>
              <strong>{capability.label}</strong>
            </button>
          );
        })}
      </div>

      <p className="workspace-mode-summary">
        <span>
          {selected.label} {t("component.embedded_chat.mode")}
        </span>
        {selected.description}
      </p>

      <div className="workspace-sample-grid">
        {selected.samples.map((sample, index) => (
          <article
            key={`${selected.key}-sample-${index}-${sample.title}`}
            className="workspace-sample-card"
          >
            <strong>{sample.title}</strong>
            <WorkspaceSamplePreview
              kind={sample.preview || selected.key}
              content={sample.previewContent}
            />
            <button
              type="button"
              className="workspace-sample-apply"
              onClick={() => onSampleSelect(sample.prompt)}
            >
              {t("component.embedded_chat.use_sample")}
            </button>
          </article>
        ))}
      </div>
    </div>
  );
}

function inferArtifactKindFromPath(path: string): OutputArtifact["kind"] {
  const lowerPath = path.toLowerCase();
  if (lowerPath.match(/\.(ppt|pptx)$/)) return "presentation";
  if (lowerPath.match(/\.(pdf)$/)) return "pdf";
  if (lowerPath.match(/\.(xlsx|xls|csv)$/)) return "spreadsheet";
  if (lowerPath.match(/\.(mmd|mermaid|drawio|diagram)$/)) return "diagram";
  if (lowerPath.match(/\.(png|jpg|jpeg|webp|gif|svg)$/)) return "image";
  if (lowerPath.match(/\.(mp4|mov|webm|m4v)$/)) return "video";
  if (lowerPath.match(/\.(mp3|wav|m4a|aac|ogg|flac)$/)) return "audio";
  if (lowerPath.match(/\.(html|htm|css)$/)) return "page";
  if (
    lowerPath.match(
      /\.(js|jsx|ts|tsx|py|sql|json|yaml|yml|sh|go|rs|java|rb|php)$/,
    )
  )
    return "code";
  if (lowerPath.match(/\.(docx|doc|md|txt|rtf)$/)) return "document";
  return "file";
}

function fileNameFromPath(path?: string) {
  if (!path) return "";
  const clean = String(path).split(/[?#]/)[0].trim();
  return clean.split(/[\\/]/).pop() || clean;
}

function fileExtensionFromName(name?: string) {
  if (!name) return "";
  const match = name.match(/\.([a-z0-9]{2,8})$/i);
  return match ? match[1].toLowerCase() : "";
}

function isGeneratedAssetName(name?: string) {
  if (!name) return false;
  const base = fileNameFromPath(name).replace(/\.[a-z0-9]{2,8}$/i, "");
  return (
    /^gen[_-][a-z0-9]+(?:[_-]\d+)?$/i.test(base) ||
    /^[a-f0-9]{24,}$/i.test(base) ||
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(base)
  );
}

function slugifyArtifactTitle(
  value?: string,
  fallback = "generated-file",
  maxWords = 6,
) {
  const words = String(value || "")
    .toLowerCase()
    .replace(/['"]/g, "")
    .replace(/[^a-z0-9\u4e00-\u9fff]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, maxWords);
  return words.join("-") || fallback;
}

function friendlyGeneratedAssetTitle(
  path?: string,
  prompt?: string,
  fallback = "Generated file",
) {
  const name = fileNameFromPath(path);
  const ext = fileExtensionFromName(name);
  if (name && !isGeneratedAssetName(name)) return name;

  const base = prompt
    ? slugifyArtifactTitle(prompt, fallback.toLowerCase().replace(/\s+/g, "-"))
    : fallback.toLowerCase().replace(/\s+/g, "-");
  return ext ? `${base}.${ext}` : fallback;
}

function hasFriendlyArtifactTitle(artifact: OutputArtifact) {
  return Boolean(
    artifact.title &&
    !isGeneratedAssetName(artifact.title) &&
    !/^generated (file|image|video)$/i.test(artifact.title.trim()),
  );
}

function looksLikeFileReference(value?: string) {
  if (!value) return false;
  return Boolean(fileNameFromPath(value).match(/\.[a-z0-9]{2,8}$/i));
}

const CODE_FENCE_LANGUAGES = new Set([
  "bash",
  "c",
  "cc",
  "cpp",
  "cs",
  "csharp",
  "css",
  "dockerfile",
  "go",
  "graphql",
  "hcl",
  "html",
  "ini",
  "java",
  "js",
  "json",
  "jsx",
  "kotlin",
  "lua",
  "makefile",
  "perl",
  "php",
  "prisma",
  "py",
  "python",
  "r",
  "rb",
  "rs",
  "ruby",
  "rust",
  "sass",
  "scss",
  "sh",
  "shell",
  "sql",
  "swift",
  "terraform",
  "toml",
  "ts",
  "tsx",
  "typescript",
  "xml",
  "yaml",
  "yml",
  "zsh",
]);

const NON_CODE_FENCE_LANGUAGES = new Set([
  "diagram",
  "markdown",
  "md",
  "mermaid",
  "mmd",
  "plain",
  "text",
  "txt",
]);

function looksLikeSourceSnippet(value: string) {
  const lines = value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length === 0) return false;

  const codeLikeLines = lines.filter((line) =>
    /^(import|export|from|const|let|var|function|class|interface|type|def|async|await|return|if|else|for|while|switch|try|catch|package|func|pub|fn|impl|use|SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH)\b/i.test(line) ||
    /^#include\b/.test(line) ||
    /^<\/?[a-z][\w-]*(\s|>|\/>)/i.test(line) ||
    /[{};]$/.test(line) ||
    /^[\w.$]+\([^)]*\)\s*(?:[{;]|=>)?$/.test(line),
  ).length;

  return codeLikeLines >= 2 || (codeLikeLines === 1 && lines.length <= 4);
}

function looksLikeCodeDraftContent(content: string) {
  const fenceMatches = Array.from(
    content.matchAll(/```([^\n`]*)\n([\s\S]*?)```/g),
  );
  return fenceMatches.some((match) => {
    const language = String(match[1] || "")
      .trim()
      .toLowerCase()
      .split(/\s+/)[0];
    const body = String(match[2] || "").trim();
    if (!body) return false;
    if (CODE_FENCE_LANGUAGES.has(language)) return true;
    if (language && NON_CODE_FENCE_LANGUAGES.has(language)) return false;
    if (language) return false;
    return looksLikeSourceSnippet(body);
  });
}

function detectArtifactFileCategory(
  doc: Pick<Document, "name" | "mime_type" | "file_type">,
): ArtifactFileCategory {
  const ext = (doc.name || "").split(".").pop()?.toLowerCase() || "";
  const mime = doc.mime_type || doc.file_type || "";

  if (["md", "markdown"].includes(ext)) return "markdown";
  if (["html", "htm"].includes(ext) || mime === "text/html") return "html";
  if (["json"].includes(ext) || mime === "application/json") return "json";
  if (["csv"].includes(ext) || mime === "text/csv") return "csv";
  if (["mmd", "mermaid", "drawio", "diagram"].includes(ext)) return "diagram";
  if (
    ["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"].includes(ext) ||
    mime.startsWith("image/")
  )
    return "image";
  if (
    ["mp4", "webm", "mov", "avi", "mkv", "m4v"].includes(ext) ||
    mime.startsWith("video/")
  )
    return "video";
  if (
    ["mp3", "wav", "ogg", "aac", "flac", "m4a"].includes(ext) ||
    mime.startsWith("audio/")
  )
    return "audio";
  if (ext === "pdf" || mime === "application/pdf") return "pdf";
  if (
    ["docx", "doc", "wps"].includes(ext) ||
    mime ===
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
  )
    return "docx";
  if (
    ["xlsx", "xls", "et"].includes(ext) ||
    mime === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
  )
    return "xlsx";
  if (isCodeLikeFile(doc))
    return "code";
  if (
    ["txt", "log", "env", "gitignore", "dockerignore", "editorconfig"].includes(
      ext,
    ) ||
    mime.startsWith("text/")
  )
    return "text";

  return "unsupported";
}

const EDITABLE_ARTIFACT_CATEGORIES = new Set<ArtifactFileCategory>([
  "text",
  "markdown",
  "code",
  "html",
  "json",
  "csv",
  "docx",
  "xlsx",
  "diagram",
]);

function canEditArtifactDocument(doc: Document): boolean {
  const category = detectArtifactFileCategory(doc);
  return category === "video" || EDITABLE_ARTIFACT_CATEGORIES.has(category);
}

function artifactEditorPath(doc: Document): string {
  return detectArtifactFileCategory(doc) === "video"
    ? `/video-editor/${doc.id}`
    : `/editor/${doc.id}`;
}

function safeAnchorSegment(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, "-");
}

function chatMessageAnchorId(message: Pick<ChatMessage, "id">, index: number) {
  const raw = message.id || `index-${index}`;
  return `chat-message-${safeAnchorSegment(String(raw))}`;
}

function isLocalHtmlPreviewAssetUrl(url: string): boolean {
  const trimmed = url.trim();
  if (!trimmed || trimmed.startsWith("#")) return false;
  return !/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(trimmed);
}

function stripHtmlPreviewUrlSuffix(url: string): string {
  return url.split(/[?#]/, 1)[0] || "";
}

function normalizeHtmlPreviewPath(path: string): string {
  const parts: string[] = [];
  for (const rawPart of path.replace(/\\/g, "/").split("/")) {
    const part = rawPart.trim();
    if (!part || part === ".") continue;
    if (part === "..") {
      parts.pop();
      continue;
    }
    parts.push(part);
  }
  return parts.join("/");
}

function dirname(path: string): string {
  const normalized = normalizeHtmlPreviewPath(path);
  const idx = normalized.lastIndexOf("/");
  return idx >= 0 ? normalized.slice(0, idx) : "";
}

function resolveHtmlPreviewAssetPath(currentFsPath: string | undefined | null, rawUrl: string): string | null {
  if (!currentFsPath || !isLocalHtmlPreviewAssetUrl(rawUrl)) return null;
  const cleanUrl = stripHtmlPreviewUrlSuffix(rawUrl).replace(/^\/+/, "");
  if (!cleanUrl) return null;
  return normalizeHtmlPreviewPath(`${dirname(currentFsPath)}/${cleanUrl}`);
}

function escapeHtmlPreviewAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function escapeHtmlRawElementText(value: string, tagName: "script" | "style"): string {
  const closingTag = new RegExp(`</${tagName}`, "gi");
  return value.replace(closingTag, `<\\/${tagName}`);
}

function extractHtmlStylesheetAndScriptRefs(html: string): string[] {
  const refs = new Set<string>();
  html.replace(/<link\b[^>]*?\bhref\s*=\s*(["'])(.*?)\1[^>]*>/gi, (match, _quote, url) => {
    if (/\brel\s*=\s*(["'])?[^"'>\s]*stylesheet/i.test(match) && isLocalHtmlPreviewAssetUrl(url)) {
      refs.add(String(url).trim());
    }
    return match;
  });
  html.replace(/<script\b[^>]*?\bsrc\s*=\s*(["'])(.*?)\1[^>]*>/gi, (match, _quote, url) => {
    if (isLocalHtmlPreviewAssetUrl(url)) refs.add(String(url).trim());
    return match;
  });
  return [...refs];
}

async function inlineHtmlPreviewStylesAndScripts(html: string, fsPath?: string | null): Promise<string> {
  if (!fsPath) return html;
  const refs = extractHtmlStylesheetAndScriptRefs(html);
  if (!refs.length) return html;

  const assets: Record<string, { kind: "style" | "script"; content: string }> = {};
  await Promise.all(
    refs.map(async (ref) => {
      const path = resolveHtmlPreviewAssetPath(fsPath, ref);
      if (!path) return;
      try {
        const result = await api.fs.read(path);
        if (result.encoding !== "utf-8") return;
        const cleanRef = stripHtmlPreviewUrlSuffix(ref).toLowerCase();
        const mime = (result.mime_type || "").toLowerCase();
        if (cleanRef.endsWith(".css") || mime === "text/css") {
          assets[ref] = { kind: "style", content: result.content };
          return;
        }
        if (
          cleanRef.endsWith(".js") ||
          cleanRef.endsWith(".mjs") ||
          cleanRef.endsWith(".cjs") ||
          mime.includes("javascript") ||
          mime === "text/ecmascript"
        ) {
          assets[ref] = { kind: "script", content: result.content };
        }
      } catch {
        // Leave the original tag in place when a sibling asset cannot be read.
      }
    }),
  );

  if (!Object.keys(assets).length) return html;
  return html
    .replace(/<link\b([^>]*?)\bhref\s*=\s*(["'])(.*?)\2([^>]*)>/gi, (match, before, _quote, url, after) => {
      const asset = assets[String(url).trim()];
      const attrs = `${before || ""} ${after || ""}`;
      if (asset?.kind === "style" && /\brel\s*=\s*(["'])?[^"'>\s]*stylesheet/i.test(attrs)) {
        return `<style data-manor-preview-src="${escapeHtmlPreviewAttr(String(url).trim())}">\n${escapeHtmlRawElementText(asset.content, "style")}\n</style>`;
      }
      return match;
    })
    .replace(/<script\b([^>]*?)\bsrc\s*=\s*(["'])(.*?)\2([^>]*)>([\s\S]*?)<\/script>/gi, (match, before, _quote, url, after) => {
      const asset = assets[String(url).trim()];
      if (asset?.kind !== "script") return match;
      const attrs = `${before || ""}${after || ""}`.replace(
        /\s+\b(?:async|defer|crossorigin|integrity|referrerpolicy)\b(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?/gi,
        "",
      );
      return `<script${attrs} data-manor-preview-src="${escapeHtmlPreviewAttr(String(url).trim())}">\n${escapeHtmlRawElementText(asset.content, "script")}\n</script>`;
    });
}

function parseArtifactCSV(text: string): string[][] {
  return text
    .split("\n")
    .filter((line) => line.trim())
    .map((line) =>
      line.split(",").map((cell) => cell.trim().replace(/^"|"$/g, "")),
    );
}

function extractFileArtifactsFromText(
  content: string,
  idPrefix: string,
): OutputArtifact[] {
  const matches = Array.from(
    content.matchAll(
      /([A-Za-z0-9_\-./\s\u4e00-\u9fff]+?\.(?:pptx?|pdf|docx?|xlsx?|csv|md|txt|rtf|html?|css|tsx?|jsx|py|sql|json|ya?ml|png|jpe?g|webp|gif|svg|mmd|mermaid|drawio|diagram|mp4|mov|webm|m4v|mp3|wav|m4a|aac|ogg|flac))/gi,
    ),
  );
  const seen = new Set<string>();
  return matches.flatMap((match, index) => {
    const raw = match[1].trim().replace(/^[`'"]|[`'"，。!！?？]+$/g, "");
    const name = fileNameFromPath(raw);
    if (!isPlatformFilesystemUrl(raw)) return [];
    if (!name || seen.has(name)) return [];
    seen.add(name);
    return [
      {
        id: `${idPrefix}-file-${index}`,
        kind: inferArtifactKindFromPath(name),
        title: friendlyGeneratedAssetTitle(
          name,
          undefined,
          inferArtifactKindFromPath(name) === "image"
            ? "Generated image"
            : "Generated file",
        ),
        status: "done" as const,
        body: raw,
        meta: raw,
      },
    ];
  });
}

interface OutputState {
  artifacts: OutputArtifact[];
  progress: ProgressItem[];
}

function normalizeArtifactPath(value?: string) {
  if (!value) return "";
  return String(value).trim().replace(/\\/g, "/").toLowerCase();
}

function artifactDedupKey(artifact: OutputArtifact) {
  const href = normalizeArtifactPath(artifact.href);
  const body = normalizeArtifactPath(artifact.body);
  const meta = normalizeArtifactPath(artifact.meta);
  const title = (artifact.title || "").trim().toLowerCase();
  const fileLike =
    fileNameFromPath(href) ||
    fileNameFromPath(body) ||
    fileNameFromPath(meta) ||
    fileNameFromPath(title);
  if (fileLike) return `${artifact.kind}|file:${fileLike.toLowerCase()}`;
  return `${artifact.kind}|title:${title}|body:${body}`;
}

function dedupeArtifacts(artifacts: OutputArtifact[]): OutputArtifact[] {
  const seen = new Set<string>();
  const result: OutputArtifact[] = [];
  for (const artifact of artifacts) {
    const key = artifactDedupKey(artifact);
    if (seen.has(key)) {
      const existingIndex = result.findIndex(
        (item) => artifactDedupKey(item) === key,
      );
      if (
        existingIndex >= 0 &&
        !hasFriendlyArtifactTitle(result[existingIndex]) &&
        hasFriendlyArtifactTitle(artifact)
      ) {
        result[existingIndex] = {
          ...result[existingIndex],
          ...artifact,
          id: result[existingIndex].id,
        };
      }
      continue;
    }
    seen.add(key);
    result.push(artifact);
  }
  return result;
}

function looksLikeLocalMachinePath(value?: unknown) {
  if (value == null) return false;
  const text = String(value).trim();
  if (!text) return false;
  return /^(~\/|\/Users\/|\/Volumes\/|\/private\/|[A-Za-z]:[\\/])/i.test(text);
}

const FILE_BACKED_ARTIFACT_KINDS = new Set<OutputArtifact["kind"]>([
  "audio",
  "code",
  "diagram",
  "document",
  "file",
  "image",
  "page",
  "pdf",
  "presentation",
  "spreadsheet",
  "video",
]);

function isPlatformFilesystemUrl(value?: unknown) {
  const text = toDisplayText(value)?.trim();
  return Boolean(text && /(^|\/)api\/v1\/fs\//i.test(text));
}

function looksLikeStoredFilesystemPath(value?: unknown) {
  const text = toDisplayText(value)?.trim();
  if (!text) return false;
  if (/^(https?:|data:)/i.test(text)) return isPlatformFilesystemUrl(text);
  if (text.startsWith("/viewer/") || text.startsWith("/api/")) {
    return isPlatformFilesystemUrl(text);
  }
  if (looksLikeLocalMachinePath(text)) return false;
  return true;
}

function hasFilesystemArtifactProof(artifact: OutputArtifact) {
  const data = artifact.data || {};
  const urlCandidates = [
    artifact.href,
    artifact.body,
    artifact.meta,
    data.result_url,
    data.file_url,
    data.download_url,
    data.document_url,
    data.image_url,
    data.video_url,
    data.audio_url,
    data.media_url,
    data.output_url,
    data.url,
  ];
  if (urlCandidates.some(isPlatformFilesystemUrl)) return true;

  const pathCandidates = [
    data.fs_path,
    data.file_path,
    data.path,
    data.output_path,
    data.saved_to,
    data.document?.fs_path,
  ];
  return pathCandidates.some(looksLikeStoredFilesystemPath);
}

function isLocalMachineArtifact(artifact: OutputArtifact) {
  const data = artifact.data || {};
  return [
    artifact.href,
    artifact.body,
    artifact.meta,
    data.fs_path,
    data.file_path,
    data.path,
    data.output_path,
    data.saved_to,
  ].some(looksLikeLocalMachinePath);
}

function primaryArtifacts(artifacts: OutputArtifact[]): OutputArtifact[] {
  const deduped = dedupeArtifacts(artifacts);
  return deduped.filter((artifact) => {
    if (artifact.status !== "done") return false;
    if (isLocalMachineArtifact(artifact)) return false;
    const data = artifact.data || {};
    const role = String(
      data.artifact_role ||
        data.artifact?.role ||
        data.role ||
        "",
    ).trim().toLowerCase();
    if (role && role !== "final") return false;
    if (
      FILE_BACKED_ARTIFACT_KINDS.has(artifact.kind) &&
      !hasFilesystemArtifactProof(artifact)
    ) {
      return false;
    }
    return true;
  });
}

function toDisplayText(value: unknown): string | undefined {
  if (value == null) return undefined;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  try {
    const serialized = JSON.stringify(value);
    return serialized === undefined ? String(value) : serialized;
  } catch {
    return String(value);
  }
}

function trimText(value?: unknown, max = 360) {
  const text = toDisplayText(value);
  if (!text) return undefined;
  const compact = text.trim();
  return compact.length > max ? `${compact.slice(0, max)}...` : compact;
}

function statusLabel(status: ExecutionStatus) {
  if (status === "running") return t("component.embedded_chat.status_running");
  if (status === "needs_approval") return t("component.embedded_chat.status_needs_approval");
  if (status === "failed") return t("component.embedded_chat.status_failed");
  if (status === "planned") return t("component.embedded_chat.status_planned");
  return t("component.embedded_chat.status_done");
}

function getToolProgressTitle(tool: ToolCall) {
  const name = tool.name || "";
  if (name.includes("image")) return t("component.embedded_chat.generating_visual");
  if (name.includes("video")) return t("component.embedded_chat.generating_video");
  if (name.includes("task")) return t("component.embedded_chat.creating_task");
  if (name.includes("document") || name.includes("file"))
    return t("component.embedded_chat.preparing_file");
  if (
    name.includes("search") ||
    name.includes("rag") ||
    name.includes("knowledge")
  )
    return t("component.embedded_chat.gathering_context");
  if (name.includes("email") || name.includes("calendar"))
    return t("component.embedded_chat.preparing_external_action");
  return t("component.embedded_chat.preparing_output");
}

function isImageGenerationTool(tool: ToolCall) {
  const name = (tool.name || "").toLowerCase();
  const input = JSON.stringify(tool.args || {}).toLowerCase();
  return (
    name.includes("image") ||
    name.includes("visual") ||
    name.includes("picture") ||
    name.includes("generate_art") ||
    input.includes("image") ||
    input.includes("illustration")
  );
}

function hasPendingImageGeneration(msg: ChatMessage) {
  return Boolean(
    msg.tool_calls?.some(
      (tool) => tool.status === "pending" && isImageGenerationTool(tool),
    ),
  );
}

function visibleToolCallsForMessage(msg: ChatMessage) {
  return (msg.tool_calls || []).filter(
    (tool) => !(tool.status === "pending" && isImageGenerationTool(tool)),
  );
}

function hasApprovalRequest(msg: ChatMessage) {
  return Boolean(msg.hitl_requests?.some((hitl) => hitl.type === "approval"));
}

function approvalPromptSignals(content: unknown) {
  const lower = (toDisplayText(content) || "").toLowerCase();
  const mentionsApproval = /审批|批准|确认|approval|approve|permission/.test(
    lower,
  );
  const mentionsAction =
    /删除|写入|修改|移动|覆盖|创建|生成|保存|delete|write|modify|move|overwrite|create|generate|save/.test(
      lower,
    );
  return mentionsApproval && mentionsAction;
}

function isInlineApprovalPrompt(msg: ChatMessage) {
  if (msg.role !== "assistant" || hasApprovalRequest(msg)) return false;
  const content = (toDisplayText(msg.content) || "").trim();
  return Boolean(
    content && content.length <= 700 && approvalPromptSignals(content),
  );
}

function messageHasApprovalPrompt(msg: ChatMessage) {
  return hasApprovalRequest(msg);
}

function toolStatus(tool: ToolCall) {
  return tool.status || (tool.result ? "success" : "pending");
}

function visibleToolCallsForApprovalMessage(msg: ChatMessage) {
  const tools = visibleToolCallsForMessage(msg);
  if (!messageHasApprovalPrompt(msg)) return tools;
  return tools.filter((tool) => toolStatus(tool) !== "success");
}

function isApprovalBoilerplateContent(msg: ChatMessage) {
  if (!messageHasApprovalPrompt(msg)) return false;
  const content = (toDisplayText(msg.content) || "").trim();
  if (!content || content.length > 700) return false;
  return approvalPromptSignals(content);
}

function inferApprovalAction(content: unknown) {
  const lower = (toDisplayText(content) || "").toLowerCase();
  if (/删除|移除|delete|remove|trash/.test(lower)) return "delete";
  if (/修改|编辑|更新|edit|modify|update/.test(lower)) return "edit";
  if (/创建|生成|create|generate/.test(lower)) return "create";
  if (/移动|move/.test(lower)) return "move";
  if (/保存|写入|save|write/.test(lower)) return "write";
  return "change";
}

function extractApprovalPaths(content: unknown) {
  const extensions =
    "md|txt|csv|json|html|docx|xlsx|pptx|pdf|png|jpg|jpeg|webp|mp4|mov";
  const paths = new Set<string>();
  const text = toDisplayText(content) || "";
  const backtickRe = new RegExp("`([^`]+\\.(" + extensions + "))`", "gi");
  let match: RegExpExecArray | null;
  while ((match = backtickRe.exec(text)) && paths.size < 3) {
    paths.add(match[1].trim());
  }
  if (paths.size === 0) {
    const bareRe = new RegExp(
      "(?:^|[\\s（(])([^\\s,，。；;:：\"'`]+\\.(" +
        extensions +
        "))(?=$|[\\s,，。；;:：\"'`?？!！）)])",
      "gi",
    );
    while ((match = bareRe.exec(text)) && paths.size < 3) {
      paths.add(match[1].trim());
    }
  }
  return Array.from(paths);
}

function isPrimaryArtifactTool(tool: ToolCall) {
  return [
    "generate_file",
    "generate_document_file",
    "generate_image",
    "generate_video",
  ].includes((tool.name || "").toLowerCase());
}

function parseToolResultJson(rawResult: unknown): any {
  if (typeof rawResult === "string") {
    try {
      return JSON.parse(rawResult);
    } catch {
      return null;
    }
  }
  return rawResult && typeof rawResult === "object" ? rawResult : null;
}

function isSandboxSavedFileTool(tool: ToolCall) {
  return ["sandbox_save_result", "save_sandbox_file"].includes(
    (tool.name || "").toLowerCase(),
  );
}

function coerceArtifactFlag(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    return ["1", "true", "yes", "y", "on"].includes(
      value.trim().toLowerCase(),
    );
  }
  return false;
}

function recordRequestsChatArtifact(record: any): boolean {
  if (!record || typeof record !== "object") return false;
  const artifact =
    record.artifact && typeof record.artifact === "object"
      ? record.artifact
      : {};
  const display =
    record.display_as_artifact ??
    record.show_as_artifact ??
    record.show_in_chat ??
    record.chat_artifact ??
    artifact.display_as_artifact ??
    artifact.show_as_artifact ??
    artifact.show_in_chat ??
    artifact.chat_artifact;
  if (coerceArtifactFlag(display)) return true;
  const role = String(
    record.artifact_role || artifact.role || record.role || "",
  )
    .trim()
    .toLowerCase();
  return role === "final";
}

function toolResultRequestsChatArtifact(tool: ToolCall): boolean {
  const parsed = parseToolResultJson(tool.result);
  return recordRequestsChatArtifact(parsed);
}

function isLocalCodingToolCall(tool: ToolCall) {
  return false;
}

function messageHasLocalCodingToolCall(msg?: ChatMessage | null) {
  return Boolean(msg?.tool_calls?.some(isLocalCodingToolCall));
}

function looksLikeLocalCodingAnswer(content: unknown) {
  return false;
}

function isWorkspaceDraftToolResult(tool: ToolCall) {
  const normalizedName = (tool.name || "").toLowerCase();
  if (normalizedName !== "start_workspace_draft" && normalizedName !== "manor")
    return false;
  const parsed = parseToolResultJson(tool.result);
  return Boolean(
    parsed?.draft_id &&
    typeof parsed?.deep_link === "string" &&
    parsed.deep_link.includes("/workspaces/new?draft="),
  );
}

function shouldParseToolResultAsArtifact(tool: ToolCall) {
  if (isWorkspaceDraftToolResult(tool)) return true;
  if (isSandboxSavedFileTool(tool)) return toolResultRequestsChatArtifact(tool);
  return isPrimaryArtifactTool(tool);
}

function normalizeArtifactKind(kind?: unknown): OutputArtifact["kind"] | null {
  const value = String(kind || "").trim().toLowerCase();
  if (!value) return null;
  if (["ppt", "pptx", "presentation", "slides"].includes(value))
    return "presentation";
  if (["doc", "docx", "document", "markdown", "md", "txt"].includes(value))
    return "document";
  if (["xls", "xlsx", "csv", "spreadsheet", "sheet"].includes(value))
    return "spreadsheet";
  if (["pdf"].includes(value)) return "pdf";
  if (["diagram", "mermaid", "mmd", "drawio"].includes(value))
    return "diagram";
  if (["code", "source"].includes(value)) return "code";
  if (["html", "page", "website", "url"].includes(value)) return "page";
  if (["image", "img", "photo", "picture"].includes(value)) return "image";
  if (["video", "movie"].includes(value)) return "video";
  if (["audio", "voice", "music", "sfx", "sound"].includes(value))
    return "audio";
  if (["workspace"].includes(value)) return "workspace";
  if (["task"].includes(value)) return "task";
  if (["approval"].includes(value)) return "approval";
  if (["file", "artifact", "output"].includes(value)) return "file";
  return null;
}

function artifactKindFromRecord(
  record: Record<string, any>,
  reference?: string,
): OutputArtifact["kind"] {
  if (record.html || record.component || record.preview_url || record.route)
    return "page";
  const explicit = normalizeArtifactKind(
    record.kind || record.type || record.category || record.file_type,
  );
  if (explicit) return explicit;
  const mime = String(record.mime_type || record.mime || "").toLowerCase();
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  if (mime === "application/pdf") return "pdf";
  if (mime.includes("presentation")) return "presentation";
  if (mime.includes("spreadsheet") || mime.includes("excel"))
    return "spreadsheet";
  if (mime.includes("wordprocessing") || mime.includes("document"))
    return "document";
  if (reference) return inferArtifactKindFromPath(reference);
  return "file";
}

function isTerminalArtifactRecord(record: Record<string, any>) {
  const status = String(record.status || record.state || "").toLowerCase();
  if (["pending", "running", "queued", "processing", "started"].includes(status))
    return false;
  if (["error", "failed", "timeout", "cancelled", "canceled"].includes(status))
    return false;
  const role = String(
    record.artifact_role || record.artifact?.role || record.role || "",
  )
    .trim()
    .toLowerCase();
  return !role || role === "final";
}

function artifactFromRecord(
  record: unknown,
  id: string,
): OutputArtifact | null {
  if (typeof record === "string") {
    const value = record.trim();
    if (!value || value.startsWith("data:")) return null;
    return {
      id,
      kind: inferArtifactKindFromPath(value),
      title: friendlyGeneratedAssetTitle(value),
      status: "done",
      href: value.match(/^https?:\/\//i) || value.startsWith("/api/")
        ? value
        : undefined,
      body: value,
      meta: value,
    };
  }
  if (!record || typeof record !== "object") return null;

  const item = record as Record<string, any>;
  const document =
    item.document && typeof item.document === "object"
      ? (item.document as Record<string, any>)
      : {};
  const merged = { ...document, ...item };
  if (!isTerminalArtifactRecord(merged)) return null;

  const href =
    merged.download_url ||
    merged.file_url ||
    merged.document_url ||
    merged.image_url ||
    merged.video_url ||
    merged.audio_url ||
    merged.result_url ||
    merged.preview_url ||
    merged.route ||
    merged.url ||
    undefined;
  const path =
    merged.fs_path ||
    merged.file_path ||
    merged.path ||
    merged.output_path ||
    merged.saved_to ||
    merged.primary ||
    undefined;
  const reference = String(path || href || merged.name || "").trim();
  const documentId =
    merged.document_id ||
    (normalizeArtifactKind(merged.kind || merged.type) === "document"
      ? merged.id
      : undefined);

  if (!reference && !documentId) return null;

  const kind = artifactKindFromRecord(merged, reference);
  const title =
    merged.title ||
    merged.name ||
    merged.filename ||
    fileNameFromPath(reference) ||
    (kind === "image"
      ? "Generated image"
      : kind === "video"
        ? "Generated video"
        : kind === "audio"
          ? "Generated audio"
          : "Generated file");

  return {
    id,
    kind,
    title: friendlyGeneratedAssetTitle(reference || title, merged.prompt, title),
    status: "done",
    href,
    body: path || href || merged.name || documentId,
    meta: path || href || merged.name || documentId,
    data: merged,
  };
}

function structuredArtifactsFromResult(
  parsed: any,
  idPrefix: string,
): OutputArtifact[] {
  if (!parsed || typeof parsed !== "object") return [];
  const artifacts: OutputArtifact[] = [];
  const add = (value: unknown, suffix: string) => {
    const artifact = artifactFromRecord(value, `${idPrefix}-${suffix}`);
    if (artifact) artifacts.push(artifact);
  };

  add(parsed, "result");

  for (const key of ["files", "artifacts", "outputs", "documents", "images"]) {
    const value = parsed[key];
    if (Array.isArray(value)) {
      value.forEach((item, index) =>
        add(
          key === "images" && typeof item === "string"
            ? { kind: "image", url: item }
            : item,
          `${key}-${index}`,
        ),
      );
    } else if (value && typeof value === "object") {
      add(value, key);
    }
  }

  for (const key of ["image_urls", "video_urls", "audio_urls"]) {
    const value = parsed[key];
    const kind = key.startsWith("image")
      ? "image"
      : key.startsWith("video")
        ? "video"
        : "audio";
    if (Array.isArray(value)) {
      value.forEach((item, index) =>
        add({ kind, url: item }, `${key}-${index}`),
      );
    }
  }

  return primaryArtifacts(artifacts);
}

function ImageGenerationStatusCard() {
  return (
    <div className="chat-image-generation-card" aria-live="polite">
      <div className="chat-image-generation-title">{t("component.embedded_chat.creating_image")}</div>
      <div className="chat-image-generation-stage" aria-hidden="true">
        <span className="chat-image-generation-orb chat-image-generation-orb--a" />
        <span className="chat-image-generation-orb chat-image-generation-orb--b" />
        <span className="chat-image-generation-orb chat-image-generation-orb--c" />
        <span className="chat-image-generation-scan" />
        <span className="chat-image-generation-frame" />
      </div>
    </div>
  );
}

function parseToolResult(tool: ToolCall, id: string): OutputArtifact | null {
  const rawResult: unknown = tool.result;
  const textResult = toDisplayText(rawResult) || "";
  if (!textResult || tool.status === "pending") return null;
  if (tool.status === "error") return null;

  const parsed = parseToolResultJson(rawResult);

  const normalizedName = (tool.name || "").toLowerCase();

  if (
    parsed?.draft_id &&
    typeof parsed?.deep_link === "string" &&
    parsed.deep_link.includes("/workspaces/new?draft=")
  ) {
    return {
      id,
      kind: "workspace",
      title: parsed.title || "Workspace draft started",
      status: "done",
      href: parsed.deep_link,
      body:
        parsed.assistant_reply ||
        "Continue setting up this workspace in the guided draft flow.",
      data: parsed,
    };
  }

  if (
    parsed?.html ||
    parsed?.component ||
    parsed?.preview_url ||
    parsed?.route
  ) {
    return {
      id,
      kind: "page",
      title: parsed.title || parsed.name || "Page preview",
      status: "done",
      href: parsed.preview_url || parsed.route,
      body: parsed.html || parsed.component || parsed.description,
      language: parsed.component ? "tsx" : "html",
      data: parsed,
    };
  }
  if (parsed?.code || parsed?.language || normalizedName.includes("code")) {
    return {
      id,
      kind: "code",
      title: parsed.title || parsed.filename || "Generated code",
      status: "done",
      body: trimText(parsed.code || textResult, 900),
      language: parsed.language,
      data: parsed,
    };
  }
  if (
    parsed?.markdown ||
    parsed?.document ||
    parsed?.report ||
    normalizedName.includes("docgen")
  ) {
    return {
      id,
      kind: "document",
      title: parsed.title || parsed.name || "Generated document",
      status: "done",
      body: trimText(
        parsed.markdown || parsed.document || parsed.report || textResult,
        900,
      ),
    };
  }
  const parsedImageUrl =
    parsed?.image_url ||
    (Array.isArray(parsed?.images) ? parsed.images[0] : undefined) ||
    (Array.isArray(parsed?.outputs) ? parsed.outputs[0] : undefined) ||
    (parsed?.intent === "image" ? parsed?.primary : undefined);
  if (parsedImageUrl) {
    return {
      id,
      kind: "image",
      title:
        parsed.title ||
        parsed.name ||
        parsed.filename ||
        friendlyGeneratedAssetTitle(
          parsedImageUrl,
          parsed.prompt,
          "Generated image",
        ),
      status: "done",
      href: parsedImageUrl,
      body: parsed.prompt,
    };
  }
  const parsedVideoUrl =
    parsed?.video_url ||
    (parsed?.intent === "video" ? parsed?.primary : undefined) ||
    (typeof parsed?.url === "string" && parsed.url.match(/\.(mp4|mov|webm)$/i)
      ? parsed.url
      : undefined);
  if (parsedVideoUrl) {
    return {
      id,
      kind: "video",
      title:
        parsed.title ||
        parsed.name ||
        parsed.filename ||
        friendlyGeneratedAssetTitle(
          parsedVideoUrl,
          parsed.prompt || parsed.title,
          "Generated video",
        ),
      status: "done",
      href: parsedVideoUrl,
      body: parsed.prompt || parsed.title,
    };
  }
  const parsedAudioUrl =
    parsed?.audio_url ||
    (parsed?.kind === "audio" ? parsed?.result_url : undefined) ||
    (typeof parsed?.url === "string" && parsed.url.match(/\.(mp3|wav|flac|ogg|opus|aac|m4a)$/i)
      ? parsed.url
      : undefined);
  if (parsedAudioUrl) {
    return {
      id,
      kind: "audio",
      title:
        parsed.title ||
        parsed.name ||
        parsed.filename ||
        friendlyGeneratedAssetTitle(
          parsedAudioUrl,
          parsed.prompt || parsed.title,
          "Generated audio",
        ),
      status: "done",
      href: parsedAudioUrl,
      body: parsed.prompt || parsed.title,
      data: parsed,
    };
  }
  if (parsed?.task_id || parsed?.task?.id) {
    return {
      id,
      kind: "task",
      title: parsed.title || parsed.task?.title || "Task created",
      status: "done",
      body: trimText(parsed.description || parsed.task?.description),
      data: parsed.task || parsed,
    };
  }
  if (
    isPrimaryArtifactTool(tool) &&
    (parsed?.file_path || parsed?.path || parsed?.download_url || parsed?.url)
  ) {
    const path =
      parsed.file_path || parsed.path || parsed.download_url || parsed.url;
    const kind = inferArtifactKindFromPath(String(path));
    return {
      id,
      kind,
      title:
        parsed.title ||
        parsed.name ||
        parsed.filename ||
        friendlyGeneratedAssetTitle(
          String(path),
          parsed.prompt || parsed.description,
          kind === "image" ? "Generated image" : "Generated file",
        ),
      status: "done",
      href: parsed.download_url || parsed.url,
      body: path,
      meta: path,
      data: parsed,
    };
  }

  return null;
}

function parseToolResultArtifacts(
  tool: ToolCall,
  idPrefix: string,
): OutputArtifact[] {
  if (tool.status === "pending" || tool.status === "error") return [];

  const parsed = parseToolResultJson(tool.result);
  if (isSandboxSavedFileTool(tool) && !recordRequestsChatArtifact(parsed)) {
    return [];
  }
  // An async media job (e.g. generate_file kind="video") reports
  // status:"pending" in its result even though the tool call itself succeeded.
  // Don't render its placeholder file as a finished, openable artifact — show a
  // generating placeholder card instead. The real artifact (or failure)
  // replaces it once the job completes (the agent waits via wait_media_jobs).
  if (parsed && typeof parsed === "object" && parsed.status === "pending") {
    if (String(parsed.kind || "") === "video" && parsed.job_id) {
      return [
        {
          id: `${idPrefix}-generating`,
          kind: "video",
          title: String(
            parsed.name ||
              parsed.prompt ||
              t("component.embedded_chat.generating_video"),
          ),
          status: "running",
          data: { job_id: String(parsed.job_id), generating: true },
        },
      ];
    }
    return [];
  }
  const structured = structuredArtifactsFromResult(parsed, idPrefix);
  if (structured.length > 0) return structured;

  const artifact = parseToolResult(tool, idPrefix);
  return artifact ? primaryArtifacts([artifact]) : [];
}

function subAgentToProgressItem(ev: SubAgentEvent, id: string): ProgressItem {
  return {
    id,
    title: t("component.embedded_chat.specialist_contribution_ready"),
    detail: trimText(ev.content || ev.event_type, 140),
    status: "done",
  };
}

function deriveOutputState(
  messages: ChatMessage[],
  streaming: boolean,
): OutputState {
  const artifacts: OutputArtifact[] = [];
  const progress: ProgressItem[] = [];
  const latestAssistant = [...messages]
    .reverse()
    .find((msg) => msg.role === "assistant");
  const latestUser = [...messages].reverse().find((msg) => msg.role === "user");

  if (latestUser) {
    progress.push({
      id: "request-received",
      title: t("component.embedded_chat.request_received"),
      detail: trimText(latestUser.content, 110),
      status: "done",
    });
  }

  const latestAssistantContent = toDisplayText(latestAssistant?.content) || "";
  const latestAssistantHasLocalCodingTool =
    messageHasLocalCodingToolCall(latestAssistant);
  const latestAssistantLooksLocalCoding =
    looksLikeLocalCodingAnswer(latestAssistantContent);
  messages.forEach((msg, messageIndex) => {
    msg.tool_calls?.forEach((tool, toolIndex) => {
      const id = `tool-${messageIndex}-${toolIndex}`;
      if (tool.status === "pending") {
        progress.push({
          id,
          title: getToolProgressTitle(tool),
          detail: tool.activeChild
            ? "Working through the next step."
            : undefined,
          status: "running",
        });
      } else {
        const toolArtifacts = shouldParseToolResultAsArtifact(tool)
          ? parseToolResultArtifacts(tool, id)
          : [];
        artifacts.push(...toolArtifacts);
        const firstArtifact = toolArtifacts[0];
        progress.push({
          id: `progress-${id}`,
          title:
            toolArtifacts.length > 1
              ? `${toolArtifacts.length} artifacts ready`
              : firstArtifact?.title || getToolProgressTitle(tool),
          detail: firstArtifact?.body,
          status: tool.status === "error" ? "failed" : "done",
        });
      }
    });
    msg.sub_agent_events?.forEach((event, eventIndex) => {
      progress.push(
        subAgentToProgressItem(event, `agent-${messageIndex}-${eventIndex}`),
      );
    });
    msg.hitl_requests?.forEach((hitl, hitlIndex) => {
      const id = `hitl-${messageIndex}-${hitlIndex}`;
      progress.push({
        id: `progress-${id}`,
        title: hitl.resolved
          ? "Approval completed"
          : "Waiting for your approval",
        detail: hitl.prompt,
        status: hitl.resolved ? "done" : "needs_approval",
      });
    });
  });

  if (
    artifacts.length === 0 &&
    !streaming &&
    latestAssistant &&
    latestAssistantContent &&
    !latestAssistantHasLocalCodingTool &&
    !latestAssistantLooksLocalCoding &&
    !isApprovalBoilerplateContent(latestAssistant)
  ) {
    const content = latestAssistantContent.trim();
    const fileArtifacts = extractFileArtifactsFromText(content, "working");
    artifacts.push(...fileArtifacts);
    const looksLikeCode = looksLikeCodeDraftContent(content);
    if (fileArtifacts.length === 0 && looksLikeCode) {
      artifacts.push({
        id: "working-draft",
        kind: "code",
        title: t("component.embedded_chat.code_draft"),
        status: "done",
        body: trimText(content, 900),
      });
    }
  }

  if (
    streaming &&
    !progress.some(
      (item) => item.status === "running" || item.status === "needs_approval",
    )
  ) {
    progress.push({
      id: "assistant-streaming",
      title: t("component.embedded_chat.writing_output"),
      detail: "Updating the visible answer as Manor works.",
      status: "running",
    });
  }

  return { artifacts: primaryArtifacts(artifacts), progress };
}

function deriveMessageArtifacts(
  msg: ChatMessage,
  streaming: boolean,
): OutputArtifact[] {
  const artifacts: OutputArtifact[] = [];
  msg.tool_calls?.forEach((tool, toolIndex) => {
    if (!shouldParseToolResultAsArtifact(tool)) return;
    artifacts.push(
      ...parseToolResultArtifacts(tool, `message-tool-${toolIndex}`),
    );
  });
  const structuredArtifacts = primaryArtifacts(artifacts);
  if (structuredArtifacts.length > 0) return structuredArtifacts;
  if (
    messageHasLocalCodingToolCall(msg) ||
    looksLikeLocalCodingAnswer(msg.content)
  ) {
    return [];
  }

  const suppressBoilerplate = isApprovalBoilerplateContent(msg);
  const messageContent = toDisplayText(msg.content) || "";
  if (
    msg.role === "assistant" &&
    messageContent &&
    !streaming &&
    !suppressBoilerplate
  ) {
    const content = messageContent.trim();
    const fileArtifacts = extractFileArtifactsFromText(content, "message");
    artifacts.push(...fileArtifacts);
    if (fileArtifacts.length > 0) return primaryArtifacts(artifacts);
    const looksLikeCode = looksLikeCodeDraftContent(content);
    if (fileArtifacts.length === 0 && looksLikeCode) {
      artifacts.push({
        id: "message-draft",
        kind: "code",
        title: t("component.embedded_chat.code_draft"),
        status: streaming ? "running" : "done",
        body: trimText(content, 900),
      });
      return primaryArtifacts(artifacts);
    }
  }
  return primaryArtifacts(artifacts);
}

function ExecutionStatusDot({ status }: { status: ExecutionStatus }) {
  return (
    <span className={`chat-execution-dot chat-execution-dot--${status}`} />
  );
}

function ArtifactIcon({ kind }: { kind: OutputArtifact["kind"] }) {
  const label =
    kind === "presentation"
      ? "PPT"
      : kind === "pdf"
        ? "PDF"
        : kind === "spreadsheet"
          ? "XLS"
          : kind === "document"
            ? "DOC"
            : kind === "diagram"
              ? "DIA"
              : kind === "code"
                ? "</>"
                : kind === "page"
                  ? "HTML"
                  : kind === "image"
                    ? "IMG"
                    : kind === "video"
                      ? "VID"
                      : kind === "audio"
                        ? "AUD"
                        : kind === "workspace"
                          ? "WS"
                          : kind === "approval"
                            ? "!"
                            : kind.charAt(0).toUpperCase();
  return (
    <span
      className={`chat-output-artifact-icon chat-output-artifact-icon--${kind}`}
    >
      {label}
    </span>
  );
}

/** Best image URL to use as an artifact thumbnail, or null when none applies. */
function artifactThumbSrc(artifact: OutputArtifact): string | null {
  const data = artifact.data || {};
  const candidates =
    artifact.kind === "image"
      ? [
          artifact.href,
          data.url,
          data.image_url,
          data.file_url,
          data.download_url,
          data.preview_url,
          data.thumbnail,
          data.thumbnail_url,
          data.src,
        ]
      : [
          data.poster,
          data.thumbnail,
          data.thumbnail_url,
          data.preview_url,
          data.first_frame_url,
        ];
  for (const candidate of candidates) {
    const value = String(candidate || "").trim();
    if (value && (/^https?:\/\//i.test(value) || value.startsWith("/"))) {
      return value;
    }
  }
  return null;
}

/** Shows the file's actual thumbnail (images, or any poster/preview), falling
 *  back to the type-label badge when there's no usable image. */
const DOC_THUMB_KINDS = new Set<OutputArtifact["kind"]>([
  "image",
  "video",
  "presentation",
  "pdf",
  "document",
  "spreadsheet",
  "file",
]);

function documentThumbnailCacheVersionForArtifact(doc: Document): string {
  const updatedAt = (doc as Document & { updated_at?: string | null })
    .updated_at;
  return [
    updatedAt || doc.created_at || "",
    doc.file_size ?? "",
    doc.vector_status || "",
    (doc as Document & { status?: string | null }).status || "",
  ].join(":");
}

function artifactThumbnailKind(
  artifact: OutputArtifact,
  doc?: Document | null,
): OutputArtifact["kind"] {
  if (artifact.kind !== "file") return artifact.kind;
  const reference =
    doc?.name ||
    doc?.fs_path ||
    doc?.file_type ||
    doc?.mime_type ||
    artifact.body ||
    artifact.meta ||
    artifact.href ||
    artifact.title;
  const inferred = inferArtifactKindFromPath(String(reference || ""));
  if (inferred !== "file") return inferred;
  const mime = String(doc?.mime_type || doc?.file_type || "").toLowerCase();
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  return artifact.kind;
}

async function loadArtifactDocumentThumbnail(
  artifact: OutputArtifact,
  doc: Document,
): Promise<string> {
  const version = documentThumbnailCacheVersionForArtifact(doc);
  const kind = artifactThumbnailKind(artifact, doc);
  if (kind === "image")
    return api.documents.imageThumbnail(doc.id, { cache: true, version });
  if (kind === "video")
    return api.documents.videoThumbnail(doc.id, { cache: true, version });
  if (kind === "presentation")
    return api.documents.presentationThumbnail(doc.id, {
      cache: true,
      version,
    });
  return api.documents.thumbnail(doc.id, { cache: true, version });
}

function ArtifactThumb({ artifact }: { artifact: OutputArtifact }) {
  const directSrc = artifactThumbSrc(artifact);
  const [docThumb, setDocThumb] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [failedDirect, setFailedDirect] = useState(false);

  const wantsDocThumb = DOC_THUMB_KINDS.has(artifact.kind);

  useEffect(() => {
    if (!wantsDocThumb) return;
    let cancelled = false;
    (async () => {
      try {
        const doc = await findDocumentForArtifact(artifact);
        if (!doc || cancelled) return;
        const url = await loadArtifactDocumentThumbnail(artifact, doc);
        if (!cancelled && url) setDocThumb(url);
      } catch {
        // No thumbnail (unsupported type / render failed) - keep the badge.
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifact.id, wantsDocThumb]);

  const src = directSrc && !failedDirect ? directSrc : docThumb;
  if (!src || failed) return <ArtifactIcon kind={artifact.kind} />;
  return (
    <img
      className="chat-artifact-thumb"
      src={src}
      alt=""
      loading="lazy"
      onError={() => {
        if (directSrc && src === directSrc) setFailedDirect(true);
        else setFailed(true);
      }}
    />
  );
}

function artifactDocumentId(artifact: OutputArtifact): string {
  const data = artifact.data || {};
  const nestedDocument =
    data.document && typeof data.document === "object" ? data.document : null;
  const dataLooksDocumentBacked = Boolean(
    data.fs_path ||
      data.file_path ||
      data.file_type ||
      data.mime_type ||
      data.source,
  );
  const candidate =
    data.document_id ||
    nestedDocument?.id ||
    (data.id && dataLooksDocumentBacked ? data.id : "");
  return String(candidate || "").trim();
}

function artifactDownloadName(artifact: OutputArtifact, doc?: Document | null) {
  return (
    doc?.name ||
    fileNameFromPath(artifact.body) ||
    fileNameFromPath(artifact.meta) ||
    fileNameFromPath(artifact.href) ||
    artifact.title ||
    "artifact"
  );
}

function imageArtifactLooksLikeSlide(artifact: OutputArtifact) {
  const data = artifact.data || {};
  const haystack = [
    artifact.title,
    artifact.body,
    artifact.href,
    artifact.meta,
    data.name,
    data.filename,
    data.title,
    data.fs_path,
    data.file_path,
    data.path,
    data.output_path,
    data.saved_to,
    data.download_url,
    data.file_url,
    data.image_url,
    data.preview_url,
  ]
    .map((value) => String(value || "").toLowerCase())
    .join(" ");

  return (
    /\b(ppt|pptx|presentation|deck|slides?)\b/.test(haystack) ||
    /(?:^|[\s/_-])slide[\s_-]?\d+/.test(haystack)
  );
}

function canDownloadArtifact(artifact: OutputArtifact) {
  if (["approval", "task", "workspace"].includes(artifact.kind)) return false;
  const data = artifact.data || {};
  return Boolean(
    artifactDocumentId(artifact) ||
      artifact.href ||
      looksLikeFileReference(artifact.body) ||
      looksLikeFileReference(artifact.meta) ||
      data.fs_path ||
      data.file_path ||
      data.path ||
      data.saved_to,
  );
}

function triggerBrowserDownload(
  url: string,
  filename: string,
  revoke?: () => void,
) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "artifact";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => revoke?.(), 1000);
}

const ARTIFACT_DOCUMENT_CACHE_TTL_MS = 30 * 60 * 1000;
const artifactDocumentCache = new Map<
  string,
  { expiresAt: number; document: Document | null }
>();
const artifactDocumentInflight = new Map<string, Promise<Document | null>>();

function artifactDocumentCacheKey(artifact: OutputArtifact) {
  const documentId = artifactDocumentId(artifact);
  if (documentId) return `id:${documentId}`;
  return [
    "lookup",
    artifact.kind,
    artifact.title,
    artifact.body,
    artifact.meta,
    artifact.href,
  ]
    .filter(Boolean)
    .join(":")
    .toLowerCase();
}

async function findDocumentForArtifact(
  artifact: OutputArtifact,
): Promise<Document | null> {
  const cacheKey = artifactDocumentCacheKey(artifact);
  const now = Date.now();
  const cached = artifactDocumentCache.get(cacheKey);
  if (cached && cached.expiresAt > now) return cached.document;
  if (cached) artifactDocumentCache.delete(cacheKey);

  const inflight = artifactDocumentInflight.get(cacheKey);
  if (inflight) return inflight;

  const lookup = (async () => {
    const explicitDocumentId = artifactDocumentId(artifact);
    if (explicitDocumentId) {
      try {
        return await api.documents.get(explicitDocumentId);
      } catch {
        // Fall back to name/path search for older tool results.
      }
    }

    const data = artifact.data || {};
    const nestedDocument =
      data.document && typeof data.document === "object"
        ? (data.document as Record<string, any>)
        : {};
    const searchTerms = [
      artifact.title,
      fileNameFromPath(artifact.body),
      fileNameFromPath(artifact.meta),
      fileNameFromPath(artifact.href),
      data.name,
      data.filename,
      data.title,
      data.fs_path,
      data.file_path,
      data.path,
      data.output_path,
      data.saved_to,
      data.primary,
      data.download_url,
      data.file_url,
      data.image_url,
      data.video_url,
      data.audio_url,
      data.preview_url,
      nestedDocument.name,
      nestedDocument.fs_path,
      nestedDocument.file_path,
      artifact.body,
      artifact.meta,
      artifact.href,
    ]
      .map((term) => String(term || "").trim())
      .filter(Boolean);

    const seen = new Set<string>();
    for (const term of searchTerms) {
      const normalizedTerm =
        fileNameFromPath(term).toLowerCase() || term.toLowerCase();
      if (!normalizedTerm || seen.has(normalizedTerm)) continue;
      seen.add(normalizedTerm);

      const docs = await api.documents.list({
        search: normalizedTerm,
        limit: 10,
      });
      const exact = docs.items.find(
        (doc) => doc.name.toLowerCase() === normalizedTerm,
      );
      const contains = docs.items.find((doc) => {
        const docName = doc.name.toLowerCase();
        return (
          normalizedTerm.includes(docName) || docName.includes(normalizedTerm)
        );
      });
      if (exact || contains || docs.items[0])
        return exact || contains || docs.items[0];
    }

    return null;
  })();

  artifactDocumentInflight.set(cacheKey, lookup);
  try {
    const document = await lookup;
    const entry = {
      document,
      expiresAt: Date.now() + ARTIFACT_DOCUMENT_CACHE_TTL_MS,
    };
    artifactDocumentCache.set(cacheKey, entry);
    if (document) artifactDocumentCache.set(`id:${document.id}`, entry);
    if (artifactDocumentCache.size > 200) {
      const expiredAt = Date.now();
      for (const [key, value] of artifactDocumentCache) {
        if (value.expiresAt <= expiredAt || artifactDocumentCache.size > 160) {
          artifactDocumentCache.delete(key);
        }
      }
    }
    return document;
  } finally {
    artifactDocumentInflight.delete(cacheKey);
  }
}

async function downloadArtifact(artifact: OutputArtifact) {
  const doc = await findDocumentForArtifact(artifact);
  if (doc) {
    const url = await api.documents.download(doc.id);
    triggerBrowserDownload(url, artifactDownloadName(artifact, doc), () =>
      URL.revokeObjectURL(url),
    );
    return;
  }

  const directUrl =
    artifact.href ||
    (isLocalFsUrl(artifact.body) ? artifact.body : "") ||
    (isLocalFsUrl(artifact.meta) ? artifact.meta : "");
  if (!directUrl) return;

  if (isLocalFsUrl(directUrl)) {
    const resolved = await resolveDisplayMediaUrl(directUrl);
    triggerBrowserDownload(
      resolved.url,
      artifactDownloadName(artifact),
      resolved.revoke,
    );
    return;
  }

  triggerBrowserDownload(directUrl, artifactDownloadName(artifact));
}

function PresentationArtifactViewer({
  artifact,
}: {
  artifact: OutputArtifact;
}) {
  const [docId, setDocId] = useState<string | null>(null);
  const [slideUrls, setSlideUrls] = useState<string[]>([]);
  const [activeSlide, setActiveSlide] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const objectUrls: string[] = [];

    async function loadPresentation() {
      setLoading(true);
      setSlideUrls([]);
      setActiveSlide(0);
      try {
        const doc = await findDocumentForArtifact(artifact);
        if (!doc || cancelled) return;
        setDocId(doc.id);

        try {
          const slideData = await api.documents.getSlides(doc.id);
          const token = getAuthToken();
          const headers: Record<string, string> = {};
          if (token) headers.Authorization = `Bearer ${token}`;
          const urls = await Promise.all(
            (slideData.slides || []).map(async (slide) => {
              const res = await fetch(`/api/v1${slide.url}`, { headers });
              if (!res.ok) throw new Error("Slide fetch failed");
              const blob = await res.blob();
              const url = URL.createObjectURL(blob);
              objectUrls.push(url);
              return url;
            }),
          );
          if (!cancelled) setSlideUrls(urls);
        } catch {
          // FileViewer fallback below still shows the real document.
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    loadPresentation();
    return () => {
      cancelled = true;
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [artifact.title, artifact.body]);

  if (loading) {
    return (
      <div className="chat-output-file-loading">
        <span className="chat-tool-spinner" />
        <p>{t("component.embedded_chat.loading_presentation_preview")}</p>
      </div>
    );
  }

  if (slideUrls.length > 0) {
    return (
      <div className="chat-output-ppt-viewer">
        <div className="chat-output-ppt-stage">
          <img
            src={slideUrls[activeSlide]}
            alt={`${artifact.title} slide ${activeSlide + 1}`}
          />
        </div>
        <div className="chat-output-ppt-strip">
          {slideUrls.map((url, index) => (
            <button
              key={url}
              className={index === activeSlide ? "active" : ""}
              onClick={() => setActiveSlide(index)}
              type="button"
            >
              <img src={url} alt={`Slide ${index + 1}`} />
              <span>{index + 1}</span>
            </button>
          ))}
        </div>
      </div>
    );
  }

  if (docId) {
    return (
      <div className="chat-output-file-frame">
        <iframe title={artifact.title} src={`/viewer/${docId}`} />
      </div>
    );
  }

  return (
    <div className="chat-output-file-missing">
      <p>{t("component.embedded_chat.presentation_content_is_not_available_yet")}</p>
      <span>{artifact.body || artifact.title}</span>
    </div>
  );
}

function FileArtifactViewer({ artifact }: { artifact: OutputArtifact }) {
  const [category, setCategory] = useState<ArtifactFileCategory | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [docxHtml, setDocxHtml] = useState("");
  const [sheets, setSheets] = useState<{ name: string; data: any[][] }[]>([]);
  const [activeSheet, setActiveSheet] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const directUrl = artifact.href || artifact.body || "";

  useEffect(() => {
    let cancelled = false;
    let createdObjectUrl: string | null = null;

    async function loadFile() {
      setLoading(true);
      setCategory(null);
      setObjectUrl(null);
      setContent("");
      setDocxHtml("");
      setSheets([]);
      setActiveSheet(0);
      setError("");
      try {
        const doc = await findDocumentForArtifact(artifact);
        if (!doc) {
          const fallbackCategory = inferArtifactKindFromPath(
            directUrl || artifact.title,
          );
          if (isLocalFsUrl(directUrl)) {
            const resolved = await resolveDisplayMediaUrl(directUrl);
            createdObjectUrl = resolved.url;
            if (!cancelled) setObjectUrl(resolved.url);
          }
          if (!cancelled)
            setCategory(
              fallbackCategory === "presentation"
                ? "unsupported"
                : (fallbackCategory as ArtifactFileCategory),
            );
          return;
        }

        const nextCategory = detectArtifactFileCategory(doc);
        if (!cancelled) setCategory(nextCategory);

        if (
          [
            "text",
            "markdown",
            "code",
            "html",
            "csv",
            "json",
            "diagram",
          ].includes(nextCategory)
        ) {
          const res = await api.documents.getContent(doc.id);
          const rawContent = typeof res === "string" ? res : res.content;
          const previewContent = nextCategory === "html"
            ? await inlineHtmlPreviewStylesAndScripts(rawContent, doc.fs_path)
            : rawContent;
          if (!cancelled)
            setContent(previewContent);
          return;
        }

        if (
          ["image", "video", "audio", "pdf", "docx", "xlsx"].includes(
            nextCategory,
          )
        ) {
          createdObjectUrl = await api.documents.download(doc.id);
          if (!cancelled) setObjectUrl(createdObjectUrl);

          if (nextCategory === "docx") {
            const res = await fetch(createdObjectUrl);
            const buf = await res.arrayBuffer();
            const mammoth = await import("mammoth");
            const result = await mammoth.convertToHtml({ arrayBuffer: buf });
            if (!cancelled) setDocxHtml(result.value);
          }

          if (nextCategory === "xlsx") {
            const res = await fetch(createdObjectUrl);
            const buf = await res.arrayBuffer();
            const XLSX = await import("xlsx");
            const wb = XLSX.read(buf, { type: "array" });
            const parsedSheets = wb.SheetNames.map((name) => ({
              name,
              data: XLSX.utils.sheet_to_json<any[]>(wb.Sheets[name], {
                header: 1,
              }) as any[][],
            }));
            if (!cancelled) setSheets(parsedSheets);
          }
        }
      } catch (err: any) {
        if (!cancelled) setError(err?.message || "File preview failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    loadFile();
    return () => {
      cancelled = true;
      if (createdObjectUrl) URL.revokeObjectURL(createdObjectUrl);
    };
  }, [artifact.title, artifact.body, artifact.meta, artifact.href]);

  if (loading) {
    return (
      <div className="chat-output-file-loading">
        <span className="chat-tool-spinner" />
        <p>{t("component.embedded_chat.loading_file_preview")}</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="chat-output-file-missing">
        <p>{t("component.embedded_chat.file_preview_failed")}</p>
        <span>{error}</span>
      </div>
    );
  }

  if (
    (category === "pdf" || artifact.kind === "pdf") &&
    (objectUrl || artifact.href)
  ) {
    return (
      <div className="chat-output-file-frame chat-output-file-frame--raw">
        <iframe title={artifact.title} src={objectUrl || artifact.href} />
      </div>
    );
  }

  if (
    (category === "image" || artifact.kind === "image") &&
    (objectUrl || artifact.href)
  ) {
    const isSlideImage = imageArtifactLooksLikeSlide(artifact);
    return (
      <div
        className={`chat-output-plain-media${isSlideImage ? " chat-output-plain-media--slide" : ""}`}
      >
        <div className="chat-output-plain-media-frame">
          <img src={objectUrl || artifact.href} alt={artifact.title} />
        </div>
      </div>
    );
  }

  if (
    (category === "video" || artifact.kind === "video") &&
    (objectUrl || artifact.href)
  ) {
    return (
      <video
        className="chat-output-video"
        src={objectUrl || artifact.href}
        controls
      />
    );
  }

  if (
    (category === "audio" || artifact.kind === "audio") &&
    (objectUrl || artifact.href)
  ) {
    return (
      <audio
        className="chat-output-audio"
        src={objectUrl || artifact.href}
        controls
      />
    );
  }

  if (category === "html" && content) {
    return (
      <div className="chat-output-render-frame chat-output-render-frame--raw">
        <iframe
          title={artifact.title}
          srcDoc={content}
          sandbox="allow-scripts allow-same-origin"
        />
      </div>
    );
  }

  if (category === "markdown" && content) {
    return (
      <div className="chat-output-artifact-body chat-output-artifact-body--document">
        <ChatMarkdown
          content={content}
          isUser={false}
          streaming={artifact.status === "running"}
        />
      </div>
    );
  }

  if (
    (category === "text" ||
      category === "code" ||
      category === "json" ||
      category === "diagram") &&
    content
  ) {
    return (
      <pre className="chat-output-code-block chat-output-code-block--light">
        <code>{content}</code>
      </pre>
    );
  }

  if (category === "csv" && content) {
    const rows = parseArtifactCSV(content);
    return (
      <div className="chat-output-table-frame">
        <table>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) =>
                  rowIndex === 0 ? (
                    <th key={cellIndex}>{cell}</th>
                  ) : (
                    <td key={cellIndex}>{cell}</td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (category === "docx" && docxHtml) {
    return (
      <div
        className="chat-output-docx"
        dangerouslySetInnerHTML={{ __html: docxHtml }}
      />
    );
  }

  if (category === "xlsx" && sheets.length > 0) {
    const sheet = sheets[activeSheet] || sheets[0];
    return (
      <div className="chat-output-spreadsheet">
        {sheets.length > 1 && (
          <div className="chat-output-sheet-tabs">
            {sheets.map((sheetItem, index) => (
              <button
                key={sheetItem.name}
                className={index === activeSheet ? "active" : ""}
                onClick={() => setActiveSheet(index)}
                type="button"
              >
                {sheetItem.name}
              </button>
            ))}
          </div>
        )}
        <div className="chat-output-table-frame">
          <table>
            <tbody>
              {sheet.data.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) =>
                    rowIndex === 0 ? (
                      <th key={cellIndex}>{String(cell ?? "")}</th>
                    ) : (
                      <td key={cellIndex}>{String(cell ?? "")}</td>
                    ),
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  if (
    artifact.kind === "document" &&
    artifact.body &&
    !looksLikeFileReference(artifact.body)
  ) {
    return (
      <div className="chat-output-artifact-body chat-output-artifact-body--document">
        <ChatMarkdown
          content={artifact.body}
          isUser={false}
          streaming={artifact.status === "running"}
        />
      </div>
    );
  }

  return (
    <div className="chat-output-file-missing">
      <p>{t("component.embedded_chat.file_preview_is_not_available_yet")}</p>
      <span>{fileNameFromPath(directUrl) || artifact.title}</span>
    </div>
  );
}

function ArtifactEditAction({
  artifact,
  returnTo,
}: {
  artifact: OutputArtifact;
  returnTo: string;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [doc, setDoc] = useState<Document | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDoc(null);
    (async () => {
      const nextDoc = await findDocumentForArtifact(artifact);
      if (!cancelled) setDoc(nextDoc);
    })();
    return () => {
      cancelled = true;
    };
  }, [
    artifact.id,
    artifact.kind,
    artifact.title,
    artifact.body,
    artifact.meta,
    artifact.href,
    artifact.data,
  ]);

  if (!doc || !canEditArtifactDocument(doc)) return null;

  const handleEdit = () => {
    const priorState =
      location.state && typeof location.state === "object"
        ? (location.state as Record<string, unknown>)
        : {};
    navigate(artifactEditorPath(doc), {
      state: {
        ...priorState,
        chatReturnTo: returnTo,
        returnTo,
      },
    });
  };

  return (
    <button className="chat-output-edit" type="button" onClick={handleEdit}>
      {t("action.edit")}
    </button>
  );
}

function ArtifactViewer({ artifact }: { artifact: OutputArtifact }) {
  const [viewMode, setViewMode] = useState<"preview" | "code">("preview");
  const task = artifact.kind === "task" ? artifact.data || {} : null;
  const htmlSource = artifact.kind === "page" ? artifact.body || "" : "";
  const isFileBackedArtifact =
    [
      "document",
      "pdf",
      "spreadsheet",
      "diagram",
      "file",
      "image",
      "video",
      "audio",
    ].includes(artifact.kind) ||
    (artifact.kind === "page" &&
      (looksLikeFileReference(artifact.body) ||
        looksLikeFileReference(artifact.meta))) ||
    (artifact.kind === "code" &&
      (looksLikeFileReference(artifact.body) ||
        looksLikeFileReference(artifact.meta)));
  const canToggleCode =
    artifact.kind === "page" && Boolean(htmlSource) && !isFileBackedArtifact;

  return (
    <div
      className={`chat-output-artifact chat-output-artifact--focused chat-output-artifact--${artifact.status}`}
    >
      <div className="chat-output-artifact-head">
        <ArtifactThumb artifact={artifact} />
        <div className="chat-output-artifact-title-wrap">
          <span className="chat-output-artifact-title">{artifact.title}</span>
          <span className="chat-output-artifact-status">
            {artifact.kind} · {statusLabel(artifact.status)}
          </span>
        </div>
        {canToggleCode && (
          <div className="chat-output-view-toggle">
            <button
              className={viewMode === "preview" ? "active" : ""}
              onClick={() => setViewMode("preview")}
              type="button"
            >
              {t("page.doc_editor.preview")}</button>
            <button
              className={viewMode === "code" ? "active" : ""}
              onClick={() => setViewMode("code")}
              type="button"
            >
              {t("component.embedded_chat.code")}</button>
          </div>
        )}
      </div>

      {isFileBackedArtifact &&
        artifact.kind !== "presentation" &&
        artifact.kind !== "task" &&
        artifact.kind !== "approval" && (
          <FileArtifactViewer artifact={artifact} />
        )}

      {!isFileBackedArtifact &&
        artifact.kind === "page" &&
        viewMode === "preview" && (
          <div className="chat-output-render-frame">
            {artifact.href ? (
              <iframe title={artifact.title} src={artifact.href} />
            ) : (
              <iframe
                title={artifact.title}
                srcDoc={htmlSource}
                sandbox="allow-scripts allow-same-origin"
              />
            )}
          </div>
        )}

      {!isFileBackedArtifact &&
        artifact.kind === "page" &&
        viewMode === "code" && (
          <pre className="chat-output-code-block">
            <code>{htmlSource}</code>
          </pre>
        )}

      {!isFileBackedArtifact && artifact.kind === "code" && (
        <pre className="chat-output-code-block">
          <code>{artifact.body}</code>
        </pre>
      )}

      {artifact.kind === "task" && task && (
        <div className="chat-output-task-panel">
          <div>
            <span>{t("page.agent_dashboard.status")}</span>
            <strong>{String(task.status || "created")}</strong>
          </div>
          <div>
            <span>{t("page.task_detail.priority")}</span>
            <strong>{String(task.priority || "normal")}</strong>
          </div>
          {(task.assignee_name ||
            task.assignee ||
            task.default_assignee_id) && (
            <div>
              <span>{t("component.embedded_chat.assignee")}</span>
              <strong>
                {String(
                  task.assignee_name ||
                    task.assignee ||
                    task.default_assignee_id,
                )}
              </strong>
            </div>
          )}
          {(task.due_date || task.deadline) && (
            <div>
              <span>{t("page.task_process.due")}</span>
              <strong>{String(task.due_date || task.deadline)}</strong>
            </div>
          )}
          {artifact.body && <p>{artifact.body}</p>}
        </div>
      )}

      {artifact.kind === "presentation" && (
        <PresentationArtifactViewer artifact={artifact} />
      )}

      {artifact.kind === "approval" && artifact.body && (
        <div className="chat-output-artifact-body">
          <p>{artifact.body}</p>
        </div>
      )}

      {artifact.href &&
        !isFileBackedArtifact &&
        artifact.kind !== "image" &&
        artifact.kind !== "video" && (
          <a
            className="chat-output-link"
            href={artifact.href}
            target={artifact.href.startsWith("/") ? undefined : "_blank"}
            rel={
              artifact.href.startsWith("/") ? undefined : "noopener noreferrer"
            }
          >
            {artifact.kind === "workspace" ? t("component.embedded_chat.continue_setup") : t("component.embedded_chat.open_artifact")}
          </a>
        )}
    </div>
  );
}

function OutputPanel({
  artifact,
  onStop,
  returnTo,
}: {
  artifact?: OutputArtifact | null;
  onStop: () => void;
  returnTo: string;
}) {
  return (
    <aside
      className="chat-execution-panel chat-output-panel"
      aria-label={t("component.embedded_chat.output_panel")}
    >
      <div className="chat-execution-header">
        <div>
          <h3 className="chat-execution-title">
            {artifact?.title || t("component.embedded_chat.generated_work")}
          </h3>
        </div>
        <div className="chat-output-header-actions">
          {artifact && <ArtifactEditAction artifact={artifact} returnTo={returnTo} />}
          <button
            className="chat-output-close"
            onClick={onStop}
            type="button"
            aria-label={t("component.embedded_chat.close_artifact")}
          >
            ×
          </button>
        </div>
      </div>

      {!artifact ? (
        <div className="chat-execution-empty">
          <p>{t("component.embedded_chat.no_artifact_selected")}</p>
          <span>
            {t("component.embedded_chat.open_an_artifact_card_from_the_conversation_to_preview")}</span>
        </div>
      ) : (
        <ArtifactViewer artifact={artifact} />
      )}
    </aside>
  );
}

function ArtifactSummaryCards({
  artifacts,
  onOpen,
}: {
  artifacts: OutputArtifact[];
  onOpen: (artifact: OutputArtifact) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  if (artifacts.length === 0) return null;
  const visible = expanded ? artifacts : artifacts.slice(0, 2);
  const remaining = artifacts.length - visible.length;

  return (
    <div className="chat-artifact-summaries">
      {visible.map((artifact) => {
        const isGenerating = artifact.status === "running";
        const downloadable = !isGenerating && canDownloadArtifact(artifact);
        const isDownloading = downloadingId === artifact.id;
        const downloadButton = downloadable ? (
          <button
            className="chat-artifact-summary-download"
            disabled={isDownloading}
            onClick={async () => {
              setDownloadingId(artifact.id);
              try {
                await downloadArtifact(artifact);
              } finally {
                setDownloadingId((current) =>
                  current === artifact.id ? null : current,
                );
              }
            }}
            title={t("page.knowledge.download")}
            aria-label={`${t("page.knowledge.download")} ${artifact.title}`}
            type="button"
          >
            {isDownloading ? (
              <span className="chat-tool-spinner" />
            ) : (
              <IconDownload size={14} />
            )}
          </button>
        ) : null;

        return (
          <div
            key={artifact.id}
            className={`chat-artifact-summary chat-artifact-summary--${artifact.status} chat-artifact-summary-kind--${artifact.kind}`}
          >
            <button
              className="chat-artifact-summary-open"
              onClick={() => onOpen(artifact)}
              type="button"
            >
              <ArtifactThumb artifact={artifact} />
              <span className="chat-artifact-summary-text">
                <strong>{artifact.title}</strong>
                <small>
                  {isGenerating ||
                  artifact.kind === "approval" ||
                  artifact.kind === "task"
                    ? statusLabel(artifact.status)
                    : t("component.embedded_chat.open_artifact")}
                </small>
              </span>
            </button>
            {isGenerating && (
              <span
                className="chat-artifact-summary-download"
                aria-label={statusLabel(artifact.status)}
              >
                <span className="chat-tool-spinner" />
              </span>
            )}
            {downloadButton}
          </div>
        );
      })}
      {remaining > 0 && (
        <button
          className="chat-artifact-summary chat-artifact-summary--more"
          onClick={() => setExpanded(true)}
          type="button"
        >
          +{remaining} {t("component.embedded_chat.more")}
        </button>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function EmbeddedChat({
  conversationId,
  title,
  subtitle,
  agents,
  avatarUrl,
  agentId,
}: EmbeddedChatProps) {
  const queryClient = useQueryClient();
  const location = useLocation();
  const currentUser = useAuthStore((s) => s.user);
  const currentUserName =
    currentUser?.display_name ||
    currentUser?.first_name ||
    currentUser?.email ||
    "You";
  const currentUserAvatar = currentUser?.avatar_url;
  const isAgentConversation = Boolean(agentId && !isMasterAgent(agentId));

  const [input, setInput] = useState("");
  const [selectedAgent, setSelectedAgent] = useState<AgentInfo | null>(null);
  const [mentionedAgent, setMentionedAgent] = useState<AgentInfo | null>(null);
  const [selectedMentions, setSelectedMentions] = useState<MentionOption[]>([]);
  const [outputOpen, setOutputOpen] = useState(false);
  const [selectedArtifact, setSelectedArtifact] =
    useState<OutputArtifact | null>(null);
  const [selectedArtifactAnchor, setSelectedArtifactAnchor] =
    useState<string | null>(null);
  const handleOpenMessageReference = useCallback(
    async (refItem: ChatMessageDisplayReference, sourceAnchor?: string) => {
      setSelectedArtifact(artifactFromMessageReference(refItem));
      setSelectedArtifactAnchor(sourceAnchor || null);
      setOutputOpen(true);
      const doc = await resolveChatMessageReferenceDocument(refItem);
      if (doc) {
        setSelectedArtifact(artifactFromMessageReference(refItem, doc));
      }
    },
    [],
  );
  const [activeCapability, setActiveCapability] =
    useState<WorkspaceCapability>("workspace");
  const [chatMode, setChatMode] = useState<ChatBoxMode>("auto");
  const [chatModePayload, setChatModePayload] = useState<ChatModePayload>(() =>
    getDefaultChatModePayload("auto"),
  );
  const { data: workspaceUsers = [] } = useQuery({
    queryKey: ["chat-mention-users"],
    queryFn: () => api.users.directory(),
  });
  const { data: allAgents = [] } = useQuery({
    queryKey: ["chat-mention-agents"],
    queryFn: () => api.agents.list(),
  });

  // Persistent per-session stream store — survives route transitions.
  const initialPropConvId =
    conversationId === "manor-ai" ||
    isVirtualAgentConversationId(conversationId)
      ? undefined
      : conversationId;
  const initialStreamState = useMemo(() => useChatStreamStore.getState(), []);
  const initialSession = initialPropConvId
    ? initialStreamState.sessions[initialPropConvId]
    : initialStreamState.latestSessionKey
      ? initialStreamState.sessions[initialStreamState.latestSessionKey]
      : undefined;
  const [currentConvId, setCurrentConvId] = useState<string | undefined>(
    initialPropConvId || initialSession?.convId,
  );
  const [draftSessionKey, setDraftSessionKey] = useState<string | undefined>(
    initialSession && !initialSession.convId ? initialSession.key : undefined,
  );
  const currentSessionKey = currentConvId || draftSessionKey;
  const currentSession = useChatStreamStore((s) =>
    currentSessionKey
      ? s.sessions[currentSessionKey] ||
        s.sessions[s.sessionAliases[currentSessionKey]]
      : undefined,
  );
  const streaming = Boolean(currentSession?.streaming);
  const messages = currentSession?.messages || [];
  const streamingConvId = currentSession?.convId;
  const [messageFeedback, setMessageFeedback] = useState<
    Record<string, ChatMessageFeedbackRating>
  >({});
  const setSessionMessages = useChatStreamStore((s) => s.setSessionMessages);
  const createDraftSession = useChatStreamStore((s) => s.createDraftSession);
  const startStream = useChatStreamStore((s) => s.startStream);
  const stopStream = useChatStreamStore((s) => s.stopStream);
  const streamingRef = useRef(false);
  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);
  const currentSessionKeyRef = useRef<string | undefined>(currentSessionKey);
  useEffect(() => {
    currentSessionKeyRef.current = currentSessionKey;
  }, [currentSessionKey]);
  const didInitialScrollRef = useRef(false);
  const streamScrollFrameRef = useRef<number | null>(null);
  const lastStreamScrollAtRef = useRef(0);
  useEffect(() => {
    didInitialScrollRef.current = false;
    lastStreamScrollAtRef.current = 0;
    if (streamScrollFrameRef.current != null) {
      window.cancelAnimationFrame(streamScrollFrameRef.current);
      streamScrollFrameRef.current = null;
    }
  }, [currentSessionKey]);
  const setMessages = useCallback(
    (updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => {
      const key = currentSessionKeyRef.current;
      if (!key) return;
      setSessionMessages(key, updater);
    },
    [setSessionMessages],
  );
  const outputState = useMemo(
    () => deriveOutputState(messages, streaming),
    [messages, streaming],
  );
  const messageArtifacts = useMemo(
    () =>
      messages.map((message, index) =>
        deriveMessageArtifacts(
          message,
          streaming && index === messages.length - 1,
        ),
      ),
    [messages, streaming],
  );

  const mentionOptions = useMemo<MentionOption[]>(() => {
    const agentMap = new Map<string, AgentInfo>();
    (allAgents as Agent[]).forEach((agent) => {
      agentMap.set(agent.id, {
        id: agent.id,
        name: agent.name,
        avatar_url: agent.avatar_url,
      });
    });
    (agents || []).forEach((agent) => agentMap.set(agent.id, agent));
    const agentOptions = Array.from(agentMap.values()).map((agent) => ({
      id: agent.id,
      type: "agent" as const,
      name: agent.name,
      subtitle: t("component.embedded_chat.assign_this_message_to_an_agent"),
      avatarUrl: agent.avatar_url,
    }));
    const userOptions = (workspaceUsers as UserSummary[]).map((user) => {
      const name =
        user.display_name ||
        user.email;
      return {
        id: user.id,
        type: "user" as const,
        name,
        subtitle: user.email,
        avatarUrl: user.avatar_url,
      };
    });
    return [...agentOptions, ...userOptions];
  }, [agents, allAgents, workspaceUsers]);

  const handleMentionSelect = useCallback(
    (mention: MentionOption) => {
      setSelectedMentions((prev) => {
        if (
          prev.some(
            (item) => item.id === mention.id && item.type === mention.type,
          )
        )
          return prev;
        const next =
          mention.type === "agent"
            ? prev.filter((item) => item.type !== "agent")
            : prev;
        return [...next, mention];
      });
      if (mention.type === "agent") {
        const agent =
          (agents || []).find((item) => item.id === mention.id) ||
          (allAgents as Agent[]).find((item) => item.id === mention.id);
        if (agent) {
          const normalizedAgent = {
            id: agent.id,
            name: agent.name,
            avatar_url: agent.avatar_url,
          };
          setMentionedAgent(normalizedAgent);
        }
      }
    },
    [agents, allAgents],
  );

  const handleComposerChange = useCallback((nextValue: string) => {
    setInput(nextValue);
    setSelectedMentions((prev) =>
      prev.filter((mention) => nextValue.includes(`@${mention.name}`)),
    );
    setMentionedAgent((prev) =>
      prev && !nextValue.includes(`@${prev.name}`) ? null : prev,
    );
  }, []);

  const handleMentionRemove = useCallback((mention: MentionOption) => {
    setSelectedMentions((prev) =>
      prev.filter(
        (item) => !(item.id === mention.id && item.type === mention.type),
      ),
    );
    if (mention.type === "agent") {
      setMentionedAgent(null);
    }
  }, []);

  const activeCapabilityConfig = useMemo(
    () =>
      WORKSPACE_CAPABILITIES.find((item) => item.key === activeCapability) ||
      WORKSPACE_CAPABILITIES[0],
    [activeCapability],
  );
  const requestChatMode = chatMode === "auto" ? undefined : chatMode;

  const handleChatModeChange = useCallback((mode: ChatBoxMode) => {
    setChatMode(mode);
    setChatModePayload(getDefaultChatModePayload(mode));
    if (mode === "auto") {
      setActiveCapability("workspace");
    } else if (mode === "document") {
      setActiveCapability("docs");
    } else if (mode === "sheet") {
      setActiveCapability("sheets");
    } else if (
      mode === "slides" ||
      mode === "website" ||
      mode === "image" ||
      mode === "video" ||
      mode === "research"
    ) {
      setActiveCapability(mode);
    }
  }, []);

  const resetChatModeAfterTurn = useCallback(() => {
    setChatMode("auto");
    setChatModePayload(getDefaultChatModePayload("auto"));
    setActiveCapability("workspace");
  }, []);

  const handleSampleSelect = useCallback((prompt: string) => {
    setInput(prompt);
  }, []);

  // Resolved agent ID for stream calls (DM prop takes priority over @mention selection)
  const resolvedAgentId = agentId || mentionedAgent?.id || selectedAgent?.id;

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const resumedRef = useRef(false);
  const selectedArtifactReturnTo = useMemo(() => {
    const base = `${location.pathname}${location.search}`;
    const hash = selectedArtifactAnchor
      ? `#${selectedArtifactAnchor}`
      : location.hash || "";
    return `${base}${hash}`;
  }, [
    location.hash,
    location.pathname,
    location.search,
    selectedArtifactAnchor,
  ]);

  useEffect(() => {
    if (!location.hash.startsWith("#chat-message-")) return undefined;
    const anchor = decodeURIComponent(location.hash.slice(1));
    const timer = window.setTimeout(() => {
      document.getElementById(anchor)?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [location.hash, messages.length]);

  const closeOutputPanel = useCallback(() => {
    setOutputOpen(false);
    setSelectedArtifactAnchor(null);
  }, []);

  const mapMessages = useCallback(
    (msgs: any[]) =>
      msgs
        .filter(
          (m: any) =>
            !(m.role === "user" && isInternalFilePermissionMessage(m.content)),
        )
        .map((m: any) => ({
          id: m.id,
          conversation_id: m.conversation_id,
          role: m.role as "user" | "assistant",
          content: toDisplayText(m.content) || "",
          tool_calls: parseToolCalls(m.tool_calls),
          assistant_blocks: Array.isArray(m.assistant_blocks) ? m.assistant_blocks : undefined,
          hitl_requests: Array.isArray(m.hitl_requests) ? m.hitl_requests : undefined,
          attachments: Array.isArray(m.attachments) ? m.attachments : undefined,
          stop_reason: m.stop_reason,
          limit_detail: m.limit_detail,
          timestamp: m.created_at,
        })),
    [],
  );

  const resolveLatestAgentConversationId = useCallback(async () => {
    if (!agentId) return undefined;
    const convs = await api.chat.listConversations();
    const latest = (convs || []).find(
      (conv: any) => conv.agent_id === agentId && !conv.workspace_id,
    );
    return latest?.id;
  }, [agentId]);

  const loadConversationMessages = useCallback(
    async (convId: string, options: { fallbackToAgent?: boolean } = {}) => {
      try {
        const msgs = await api.chat.getMessages(convId, { silent: true });
        if (streamingRef.current) return;
        setSessionMessages(convId, mapMessages(msgs));
        setCurrentConvId(convId);
        setDraftSessionKey(undefined);
      } catch (err) {
        const isMissing = err instanceof ApiError && err.status === 404;
        if (isMissing && options.fallbackToAgent && agentId) {
          const fallbackConvId = await resolveLatestAgentConversationId().catch(
            () => undefined,
          );
          if (fallbackConvId && fallbackConvId !== convId) {
            try {
              const msgs = await api.chat.getMessages(fallbackConvId, {
                silent: true,
              });
              if (streamingRef.current) return;
              setSessionMessages(fallbackConvId, mapMessages(msgs));
              setCurrentConvId(fallbackConvId);
              setDraftSessionKey(undefined);
              window.dispatchEvent(
                new CustomEvent("manor:dm-conversation-resolved", {
                  detail: { agentId, conversationId: fallbackConvId },
                }),
              );
              return;
            } catch {
              // Fall through to the clean empty DM state below.
            }
          }
        }
        if (streamingRef.current) return;
        setCurrentConvId(undefined);
        setDraftSessionKey(undefined);
        if (isMissing && agentId) {
          window.dispatchEvent(
            new CustomEvent("manor:dm-conversation-resolved", {
              detail: { agentId, conversationId: null },
            }),
          );
        }
      }
    },
    [
      agentId,
      mapMessages,
      resolveLatestAgentConversationId,
      setSessionMessages,
    ],
  );

  /* Reset when conversation changes */
  useEffect(() => {
    if (streamingRef.current) return; // don't overwrite during active stream
    const cid =
      conversationId === "manor-ai" ||
      isVirtualAgentConversationId(conversationId)
        ? undefined
        : conversationId;
    setCurrentConvId(cid);
    setDraftSessionKey(undefined);
    if (cid) setSessionMessages(cid, []);
    setSelectedAgent(null);
    setMentionedAgent(null);
    setSelectedMentions([]);
    resumedRef.current = false; // allow auto-resume for new "manor-ai" switch
    if (cid) {
      loadConversationMessages(cid, { fallbackToAgent: Boolean(agentId) });
      return;
    }
    if (isVirtualAgentConversationId(conversationId) && agentId) {
      resolveLatestAgentConversationId()
        .then((latestId) => {
          if (latestId) {
            loadConversationMessages(latestId, { fallbackToAgent: false });
          }
        })
        .catch(() => {});
    }
  }, [
    agentId,
    conversationId,
    loadConversationMessages,
    resolveLatestAgentConversationId,
    setSessionMessages,
  ]);

  /* Auto-resume most recent conversation when conversationId is "manor-ai" */
  useEffect(() => {
    if (conversationId !== "manor-ai") return;
    if (resumedRef.current || currentConvId) return;
    if (streamingRef.current) return;
    resumedRef.current = true;
    api.chat.listConversations().then((convs) => {
      const latest = (convs || []).find(
        (conv: any) => !conv.agent_id && !conv.workspace_id,
      );
      if (latest) {
        setCurrentConvId(latest.id);
        loadConversationMessages(latest.id, { fallbackToAgent: false });
      }
    });
  }, [conversationId, currentConvId, loadConversationMessages]);

  /* Auto-scroll: land at the latest message immediately, then avoid smooth-scroll churn while streaming. */
  useLayoutEffect(() => {
    if (messages.length === 0) return;
    const scrollToBottom = () => {
      messagesEndRef.current?.scrollIntoView({
        behavior: "auto",
        block: "end",
      });
    };

    if (!didInitialScrollRef.current) {
      didInitialScrollRef.current = true;
      scrollToBottom();
      return;
    }

    if (!streaming) {
      scrollToBottom();
      return;
    }

    const endRect = messagesEndRef.current?.getBoundingClientRect();
    const userIsNearBottom = !endRect || endRect.top <= window.innerHeight + 240;
    if (!userIsNearBottom) return;

    const now = Date.now();
    if (now - lastStreamScrollAtRef.current < 240) return;
    lastStreamScrollAtRef.current = now;
    if (streamScrollFrameRef.current != null) return;
    streamScrollFrameRef.current = window.requestAnimationFrame(() => {
      streamScrollFrameRef.current = null;
      scrollToBottom();
    });
  }, [messages, streaming]);

  const handleSwitchSession = (convId: string) => {
    if (convId === currentConvId) return;
    setCurrentConvId(convId);
    setDraftSessionKey(undefined);
    setSessionMessages(convId, []);
    setMentionedAgent(null);
    setSelectedMentions([]);
    loadConversationMessages(convId, { fallbackToAgent: Boolean(agentId) });
  };

  /* ---- Send message (SSE streaming) ---- */
  const handleSend = useCallback(
    async (
      rawText: string,
      attachments: AttachedItem[],
      manualSkills: ManualSkillItem[] = [],
    ) => {
      const text = stripManualSkillTokens(rawText, manualSkills);
      if (!text && attachments.length === 0 && manualSkills.length === 0)
        return;
      let sessionKey = currentSessionKeyRef.current;
      if (!sessionKey) {
        sessionKey = createDraftSession();
        setDraftSessionKey(sessionKey);
      }
      if (useChatStreamStore.getState().sessions[sessionKey]?.streaming) return;

      const now = new Date().toISOString();
      const mentionsSnapshot = selectedMentions.filter((mention) =>
        rawText.includes(`@${mention.name}`),
      );
      const peopleMentions = mentionsSnapshot.filter(
        (mention) => mention.type === "user",
      );
      const mentionMeta = mentionsSnapshot.map((mention) => ({
        id: mention.id,
        type: mention.type,
        name: mention.name,
        subtitle: mention.subtitle,
      }));
      const mentionContext =
        peopleMentions.length > 0
          ? `\n\n[Referenced people: ${peopleMentions.map((mention) => `${mention.name} <id:${mention.id}>${mention.subtitle ? ` ${mention.subtitle}` : ""}`).join(", ")}]`
          : "";
      const streamText =
        `${text}${mentionContext}`.trim() ||
        "Use the manually selected skill with the current conversation context.";
      setInput("");
      setSelectedMentions([]);
      setMentionedAgent(null);

      const displayContent = text;

      // Separate local files and KB document IDs for the API call
      const localFiles = attachments
        .filter((a) => a.type === "file" && a.file)
        .map((a) => a.file!);
      const documentIds = attachments
        .filter((a) => a.type === "knowledge" && a.id)
        .map((a) => a.id!);
      const retryRequest: ChatRetryRequest = {
        message: streamText || text,
        conversationId: currentConvId,
        documentIds: documentIds.length > 0 ? documentIds : undefined,
        agentId: resolvedAgentId,
        chatMode: requestChatMode,
        chatModePayload: requestChatMode ? chatModePayload : undefined,
        manualSkillIds:
          manualSkills.length > 0
            ? manualSkills.map((skill) => skill.id)
            : undefined,
      };
      savePendingChatRetry(retryRequest);

      const attachmentSnapshots = await Promise.all(
        attachments.map(createChatMessageAttachmentSnapshot),
      );

      const msgsBeforeSend = [
        ...messages,
        {
          role: "user" as const,
          content: displayContent,
          timestamp: now,
          attachments:
            attachmentSnapshots.length > 0
              ? attachmentSnapshots
              : undefined,
          mentions: mentionMeta.length > 0 ? mentionMeta : undefined,
          manualSkills:
            manualSkills.length > 0
              ? manualSkills.map((skill) => ({
                  id: skill.id,
                  name: manualSkillLabel(skill),
                  slug: skill.slug || undefined,
                }))
              : undefined,
          chatMode: requestChatMode,
          chatModePayload: requestChatMode ? chatModePayload : undefined,
        },
        {
          role: "assistant" as const,
          content: "",
          timestamp: now,
          retryRequest,
        },
      ];

      await startStream(
        () =>
          api.chat.stream(streamText, currentConvId, {
            files: localFiles.length > 0 ? localFiles : undefined,
            documentIds: documentIds.length > 0 ? documentIds : undefined,
            agentId: resolvedAgentId,
            chatMode: requestChatMode,
            chatModePayload: requestChatMode ? chatModePayload : undefined,
            manualSkillIds:
              manualSkills.length > 0
                ? manualSkills.map((skill) => skill.id)
                : undefined,
          }),
        currentConvId,
        msgsBeforeSend,
        (newConvId) => {
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          }
        },
        sessionKey,
      );
      clearPendingChatRetry();
      if (requestChatMode) resetChatModeAfterTurn();
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    [
      selectedMentions,
      currentConvId,
      queryClient,
      resolvedAgentId,
      messages,
      startStream,
      createDraftSession,
      requestChatMode,
      chatModePayload,
      resetChatModeAfterTurn,
    ],
  );

  const handleStopRequest = useCallback(() => {
    const convId = currentConvId || streamingConvId;
    const hitlIds = pendingHITLIds(messages);
    stopStream(currentSessionKeyRef.current);
    if (convId) {
      api.chat
        .cancelPendingFileApprovals(convId, hitlIds)
        .then(() =>
          queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        )
        .catch(() => {});
    }
  }, [currentConvId, streamingConvId, messages, stopStream, queryClient]);

  /* ---- HITL actions ---- */
  const handleHITLAction = useCallback(
    async (hitlId: string, action: string) => {
      const markResolved = (items: ChatMessage[]) =>
        items.map((msg) => ({
          ...msg,
          hitl_requests: msg.hitl_requests?.map((h) =>
            h.id === hitlId ? { ...h, resolved: true, resolution: action } : h,
          ),
        }));
      const updatedMessages = markResolved(messages);
      setMessages(updatedMessages);

      const hitlMessage = JSON.stringify({ hitl_id: hitlId, action });
      const now = new Date().toISOString();
      const msgsForHitl = [
        ...updatedMessages,
        {
          role: "user" as const,
          content: hitlActionTranscriptText(action),
          timestamp: now,
        },
        { role: "assistant" as const, content: "", timestamp: now },
      ];
      const sessionKey =
        currentSessionKeyRef.current || currentConvId || createDraftSession();
      if (!currentSessionKeyRef.current && !currentConvId) {
        setDraftSessionKey(sessionKey);
      }

      await startStream(
        () =>
          api.chat.stream(hitlMessage, currentConvId, {
            agentId: resolvedAgentId,
          }),
        currentConvId,
        msgsForHitl,
        (newConvId) => {
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          }
        },
        sessionKey,
      );
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    [
      currentConvId,
      queryClient,
      messages,
      startStream,
      resolvedAgentId,
      createDraftSession,
      setMessages,
    ],
  );

  const handleRetryMessage = useCallback(
    async (message: ChatMessage, index: number) => {
      if (streamingRef.current) return;
      const fallbackUserMessage = messages
        .slice(0, index)
        .reverse()
        .find(hasVisibleUserContent);
      const fallbackUserContent = (toDisplayText(fallbackUserMessage?.content) || "").trim();
      const retryRequest: ChatRetryRequest | undefined =
        message.retryRequest ||
        (fallbackUserContent
          ? {
              message: fallbackUserContent,
              conversationId: currentConvId,
              agentId: resolvedAgentId,
            }
          : undefined);
      if (!retryRequest?.message?.trim()) return;

      const now = new Date().toISOString();
      const sessionKey =
        currentSessionKeyRef.current ||
        retryRequest.conversationId ||
        currentConvId ||
        createDraftSession();
      if (!currentSessionKeyRef.current && !currentConvId) {
        setDraftSessionKey(sessionKey);
      }

      const retryUserContent =
        fallbackUserContent || retryRequest.message;
      const msgsBeforeSend: ChatMessage[] = [
        ...messages,
        {
          role: "user",
          content: retryUserContent,
          timestamp: now,
          attachments: fallbackUserMessage?.attachments,
          mentions: fallbackUserMessage?.mentions,
          manualSkills: fallbackUserMessage?.manualSkills,
          chatMode: fallbackUserMessage?.chatMode,
          chatModePayload: fallbackUserMessage?.chatModePayload,
        },
        {
          role: "assistant",
          content: "",
          timestamp: now,
          retryRequest,
        },
      ];

      await startStream(
        () =>
          api.chat.stream(
            retryRequest.message,
            retryRequest.conversationId || currentConvId,
            {
              documentIds: retryRequest.documentIds,
              agentId: retryRequest.agentId || resolvedAgentId,
              workspaceId: retryRequest.workspaceId,
              chatMode: retryRequest.chatMode,
              chatModePayload: retryRequest.chatModePayload,
              manualSkillIds: retryRequest.manualSkillIds,
            },
          ),
        retryRequest.conversationId || currentConvId,
        msgsBeforeSend,
        (newConvId) => {
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
          }
        },
        sessionKey,
      );
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    [
      currentConvId,
      createDraftSession,
      messages,
      queryClient,
      resolvedAgentId,
      startStream,
    ],
  );

  const feedbackKeyForMessage = useCallback(
    (message: ChatMessage, index: number) =>
      message.id || `${currentSessionKey || "draft"}:${index}`,
    [currentSessionKey],
  );

  const handleMessageFeedback = useCallback(
    async (
      message: ChatMessage,
      index: number,
      rating: ChatMessageFeedbackRating,
      contentPreview: string,
    ) => {
      const conversationId =
        message.retryRequest?.conversationId ||
        message.conversation_id ||
        currentConvId ||
        streamingConvId;
      if (message.role !== "assistant" || !message.id || !conversationId) return;

      const key = feedbackKeyForMessage(message, index);
      const previous = messageFeedback[key] || null;
      setMessageFeedback((prev) => ({ ...prev, [key]: rating }));

      const fallbackRequestPreview = messages
        .slice(0, index)
        .reverse()
        .find(hasVisibleUserContent);

      try {
        await api.chat.feedback(conversationId, message.id, {
          rating,
          content_preview: contentPreview,
          request_preview: (toDisplayText(fallbackRequestPreview?.content) || "").slice(0, 1000),
        });
      } catch {
        setMessageFeedback((prev) => {
          const next = { ...prev };
          if (previous) next[key] = previous;
          else delete next[key];
          return next;
        });
      }
    },
    [
      currentConvId,
      feedbackKeyForMessage,
      messageFeedback,
      messages,
      streamingConvId,
    ],
  );

  /* ---- Helpers ---- */
  const formatTime = (ts?: string) => {
    if (!ts) return "";
    try {
      return new Date(ts).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return "";
    }
  };

  /* ---- New conversation ---- */
  const handleNewChat = () => {
    const key = createDraftSession();
    setCurrentConvId(undefined);
    setDraftSessionKey(key);
    setSelectedAgent(null);
    setMentionedAgent(null);
    setSelectedMentions([]);
  };

  /* ================================================================ */
  /*  Render                                                           */
  /* ================================================================ */

  return (
    <div className="embedded-chat-root">
      {/* ---- Header ---- */}
      <div className="embedded-chat-header">
        <div
          style={{ display: "flex", alignItems: "center", gap: 12, flex: 1 }}
        >
          {isAgentConversation ? (
            <UserAvatar
              name={title}
              avatarUrl={avatarUrl}
              type="agent"
              seed={agentId}
              size={40}
            />
          ) : (
            <ManorAvatar size={40} />
          )}
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <h2
                style={{
                  fontSize: 16,
                  fontWeight: 600,
                  color: "#292524",
                  lineHeight: 1.3,
                }}
              >
                {title}
              </h2>
              <span className="chat-model-badge">AI</span>
            </div>
            {streaming ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 2,
                }}
              >
                <span className="chat-typing-dots">
                  <span />
                  <span />
                  <span />
                </span>
                <span style={{ fontSize: 11, color: "#78716c" }}>
                  {t("component.embedded_chat.replying")}</span>
              </div>
            ) : (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 2,
                }}
              >
                <span className="chat-status-dot chat-status-dot--online" />
                <span style={{ fontSize: 11, color: "#78716c" }}>
                  {subtitle || t("component.floating_chat.online")}
                </span>
              </div>
            )}
          </div>
        </div>
        <SessionSwitcher
          currentConvId={currentConvId}
          onNewChat={handleNewChat}
          onSwitchSession={handleSwitchSession}
        />
      </div>

      {/* ---- Agent Chip Selector (for operations) ---- */}
      {agents && agents.length > 0 && (
        <div className="embedded-agents-row">
          <span
            className={`agent-chip ${!selectedAgent ? "agent-chip--selected" : ""}`}
            onClick={() => {
              setSelectedAgent(null);
              setMentionedAgent(null);
              setSelectedMentions((prev) =>
                prev.filter((mention) => mention.type !== "agent"),
              );
            }}
          >
            <span className="agent-dot" style={{ background: "#5d7f77" }}>
              <svg width="8" height="8" viewBox="0 0 12 12" fill="white">
                <rect x="1" y="1" width="4" height="4" rx="0.5" />
                <rect x="7" y="1" width="4" height="4" rx="0.5" />
                <rect x="1" y="7" width="4" height="4" rx="0.5" />
                <rect x="7" y="7" width="4" height="4" rx="0.5" />
              </svg>
            </span>
            {t("component.embedded_chat.everyone")}</span>
          {agents.slice(0, 5).map((agent) => (
            <span
              key={agent.id}
              className={`agent-chip ${selectedAgent?.id === agent.id ? "agent-chip--selected" : ""}`}
              onClick={() => {
                setSelectedAgent(agent);
                setMentionedAgent(null);
                setSelectedMentions((prev) =>
                  prev.filter((mention) => mention.type !== "agent"),
                );
              }}
            >
              <span
                className="agent-dot"
                style={{ background: agent.color || "#534AB7" }}
              >
                {agent.name.charAt(0).toUpperCase()}
              </span>
              {agent.name}
            </span>
          ))}
          {agents.length > 5 && (
            <span className="agent-chip agent-chip--more">
              +{agents.length - 5}
            </span>
          )}
        </div>
      )}

      <div
        className={`embedded-chat-workbench ${outputOpen ? "embedded-chat-workbench--output-open" : ""}`}
      >
        <div className="embedded-chat-column">
          {/* ---- Chat Body ---- */}
          <div
            className={`embedded-chat-body ${
              messages.length === 0 ? "embedded-chat-body--empty" : ""
            }`}
          >
            {messages.length === 0 && (
              <WorkspaceWelcome
                activeCapability={activeCapability}
                onCapabilityChange={(capability) => {
                  setActiveCapability(capability);
                  const nextMode = chatModeFromCapability(capability);
                  setChatMode(nextMode);
                  setChatModePayload(getDefaultChatModePayload(nextMode));
                }}
                onSampleSelect={handleSampleSelect}
              />
            )}

            {messages.map((msg, i) => {
              const content = toDisplayText(msg.content) || "";
              const visibleTools = visibleToolCallsForApprovalMessage(msg);
              const hasAssistantBlocks =
                msg.role === "assistant" &&
                Array.isArray(msg.assistant_blocks) &&
                msg.assistant_blocks.length > 0;
              const localCodingNotice =
                msg.role === "assistant"
                  ? maybeLocalCodingRunNoticeForTools(visibleTools)
                  : null;
              const rawBubbleContent = localCodingNotice || content;
              const canRetryFromContent = isRetryableAssistantMessage(msg, rawBubbleContent);
              const bubbleContent =
                msg.role === "assistant"
                  ? displayContentForAssistantMessage(msg, rawBubbleContent)
                  : rawBubbleContent;
              const isLatestStreaming = streaming && i === messages.length - 1;
              const suppressApprovalBubble =
                isApprovalBoilerplateContent(msg) && !isLatestStreaming;
              const showCreditLimitNotice =
                msg.role === "assistant" &&
                msg.stop_reason === "credit_exhausted";
              const actionCopyText = msg.role === "user" ? content : rawBubbleContent;
              const showMessageActions = Boolean(
                !suppressApprovalBubble &&
                  !showCreditLimitNotice &&
                  actionCopyText.trim(),
              );
              const hasRetryTarget =
                Boolean(msg.retryRequest) ||
                messages
                  .slice(0, i)
                  .some(hasVisibleUserContent);
              const canRetryMessage =
                canRetryFromContent && hasRetryTarget;
              const messageAnchorId = chatMessageAnchorId(msg, i);
              // Only backend HITL cards have an id that can be safely resolved.
              const showInlineApproval = false;
              return (
	                <div
	                  key={i}
                    id={messageAnchorId}
	                  className={`chat-message-row chat-message-shell ${msg.role === "user" ? "chat-message-row--user" : ""}`}
	                >
                  {/* Avatar */}
                  {msg.role === "assistant" ? (
                    isAgentConversation ? (
                      <UserAvatar
                        name={title}
                        avatarUrl={avatarUrl}
                        type="agent"
                        seed={agentId}
                        size={32}
                      />
                    ) : (
                      <ManorAvatar size={32} />
                    )
                  ) : (
                    <UserAvatar
                      name={currentUserName}
                      avatarUrl={currentUserAvatar}
                      type="user"
                      size={32}
                    />
                  )}

                  {/* Content column */}
                  <div
                    className={`chat-message-col ${msg.role === "user" ? "chat-message-col--user" : ""}`}
                  >
                    <span
                      className={`chat-sender-name ${msg.role === "user" ? "chat-sender-name--user" : ""}`}
                    >
                      {msg.role === "user" ? t("page.chat_history.you") : title}
                    </span>

                    {/* Tool Calls */}
                    {!hasAssistantBlocks && visibleTools.length > 0 && (
                      <ToolCallList tools={visibleTools} keyPrefix={i} />
                    )}

                    {msg.role === "assistant" &&
                      hasPendingImageGeneration(msg) && (
                        <ImageGenerationStatusCard />
                      )}

                    {showCreditLimitNotice && (
                      <CreditLimitNotice detail={msg.limit_detail} />
                    )}

                    {/* Sub-Agent Cards */}
                    {msg.sub_agent_events &&
                      msg.sub_agent_events.length > 0 && (
                        <div className="chat-sub-agents">
                          {msg.sub_agent_events.map((ev, j) => (
                            <div key={j} className="chat-sub-agent-card">
                              <div className="flex items-center gap-3 mb-3">
                                <div className="chat-sub-agent-avatar">
                                  <svg
                                    width="20"
                                    height="20"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    stroke="white"
                                    strokeWidth={1.5}
                                  >
                                    <path
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                      d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"
                                    />
                                  </svg>
                                </div>
                                <div>
                                  <div
                                    style={{
                                      fontSize: 16,
                                      fontWeight: 600,
                                      color: "#fff",
                                    }}
                                  >
                                    {ev.agent_name}
                                  </div>
                                  <div
                                    style={{
                                      fontSize: 12,
                                      opacity: 0.9,
                                      color: "#fff",
                                    }}
                                  >
                                    {t("component.embedded_chat.delegated_agent")}</div>
                                </div>
                              </div>
                              <div className="chat-sub-agent-content">
                                <p
                                  style={{
                                    fontSize: 13,
                                    lineHeight: 1.6,
                                    color: "#fff",
                                  }}
                                >
                                  {ev.content}
                                </p>
                              </div>
                              <div
                                style={{
                                  display: "flex",
                                  alignItems: "center",
                                  justifyContent: "space-between",
                                  marginTop: 12,
                                }}
                              >
                                <span
                                  style={{
                                    fontSize: 11,
                                    opacity: 0.8,
                                    color: "#fff",
                                  }}
                                >
                                  {formatTime(ev.timestamp)}
                                </span>
                                {ev.event_type && (
                                  <span className="chat-sub-agent-badge">
                                    {ev.event_type}
                                  </span>
                                )}
                              </div>
                            </div>
                          ))}
                        </div>
                      )}

                    {/* HITL Cards
                     *
                     * Approval-type HITLs render a description here for
                     * scrollback context, but the Approve/Reject buttons live
                     * in the sticky <ApprovalActionBar> below — one place,
                     * no scroll-hunting when LinkedIn forces a re-approval
                     * round. Other HITL kinds (human_input, etc.) keep their
                     * inline ChatActionCard since their resolution is more
                     * than a binary yes/no. Resolved approvals also keep
                     * their inline card so the resolution badge is visible
                     * in scrollback. */}
                    {msg.hitl_requests && msg.hitl_requests.length > 0 && (
                      <div className="chat-hitl-cards">
                        {msg.hitl_requests.map((hitl) => {
                          const isUnresolvedApproval =
                            hitl.type === "approval" && !hitl.resolved;
                          return (
                            <div
                              key={hitl.id}
                              className={`chat-hitl-card ${hitl.type === "approval" ? "chat-hitl-card--approval" : ""}`}
                            >
                              {hitl.type === "approval" ? (
                                <ApprovalSummary
                                  prompt={hitl.prompt}
                                  action={hitl.action}
                                  tool={hitl.tool}
                                  hasWorkspace={Boolean(hitl.workspace?.id || hitl.workspace?.name)}
                                  paths={hitl.paths}
                                  content={hitl.content}
                                  argsPreview={hitl.args_preview}
                                  operation={hitl.operation}
                                />
                              ) : (
                                <p className="chat-hitl-prompt">
                                  {hitl.prompt}
                                </p>
                              )}
                              {!isUnresolvedApproval && (
                                <ChatActionCard
                                  action={{
                                    kind:
                                      hitl.type === "approval"
                                        ? "approve"
                                        : "human_input",
                                    options: hitl.options || [
                                      "approve",
                                      "reject",
                                    ],
                                  }}
                                  resolved={hitl.resolved}
                                  resolution={
                                    hitl.resolved
                                      ? {
                                          choice: hitl.resolution || "approved",
                                        }
                                      : null
                                  }
                                  disabled={streaming || hitl.resolved}
                                  onResolve={(choice) =>
                                    handleHITLAction(hitl.id, choice)
                                  }
                                />
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}

                    {showInlineApproval && (
                      <div className="chat-hitl-cards">
                        <div className="chat-hitl-card chat-hitl-card--approval">
                          <ApprovalSummary
                            action={inferApprovalAction(content)}
                            paths={extractApprovalPaths(content)}
                            content={content}
                          />
                          <ChatActionCard
                            action={{
                              kind: "approve",
                              options: DEFAULT_APPROVAL_OPTIONS,
                            }}
                            disabled={streaming}
                            onResolve={(choice) =>
                              handleSend(
                                choice.includes("reject")
                                  ? t("chat.approval.reject")
                                  : t("chat.approval.approve"),
                                [],
                              )
                            }
                          />
                        </div>
                      </div>
                    )}

                    {/* Message Bubble */}
                    {hasAssistantBlocks && !canRetryFromContent && !showCreditLimitNotice && (
                      <div className="chat-bubble chat-bubble--bot">
                        <AssistantMessageBlocks
                          blocks={msg.assistant_blocks}
                          content={bubbleContent}
                          keyPrefix={i}
                          streaming={streaming && i === messages.length - 1}
                          returnTo={`${location.pathname}${location.search}#${messageAnchorId}`}
                        />
                      </div>
                    )}

                    {(!hasAssistantBlocks || canRetryFromContent) &&
                      bubbleContent &&
                      !suppressApprovalBubble &&
                      !showCreditLimitNotice && (
                        <div
                          className={`chat-bubble ${msg.role === "user" ? "chat-bubble--user" : "chat-bubble--bot"}`}
                        >
                          {msg.role === "user" ? (
                            <UserMessageContent
                              msg={msg}
                              onOpenReference={(refItem) =>
                                handleOpenMessageReference(refItem, messageAnchorId)
                              }
                            />
                          ) : (
                            <>
                              <ChatMarkdown
                                content={bubbleContent}
                                isUser={false}
                                streaming={
                                  streaming &&
                                  i === messages.length - 1 &&
                                  msg.role === "assistant"
                                }
                                returnTo={`${location.pathname}${location.search}#${messageAnchorId}`}
                              />
                              <ChatMessageReferenceStrip
                                references={parseUserMessageDisplay(msg).references}
                                onOpenReference={(refItem) =>
                                  handleOpenMessageReference(refItem, messageAnchorId)
                                }
                              />
                            </>
                          )}
                          {streaming &&
                            i === messages.length - 1 &&
                            msg.role === "assistant" && (
                              <span className="chat-streaming-cursor" />
                            )}
                        </div>
                      )}

                    {/* Streaming cursor when content empty */}
                    {!content &&
                      streaming &&
                      i === messages.length - 1 &&
                      msg.role === "assistant" && (
                        <div className="chat-bubble chat-bubble--bot">
                          <span className="chat-streaming-cursor" />
                        </div>
                      )}

                    {msg.role === "assistant" && (
                      <ArtifactSummaryCards
                        artifacts={messageArtifacts[i] || []}
                        onOpen={(artifact) => {
                          setSelectedArtifact(artifact);
                          setSelectedArtifactAnchor(messageAnchorId);
                          setOutputOpen(true);
                        }}
                      />
                    )}

                    <div
                      className={`chat-message-meta-row ${
                        msg.role === "user" ? "chat-message-meta-row--user" : ""
                      } ${showMessageActions ? "chat-message-meta-row--actions" : ""}`}
                    >
                      <span className="chat-timestamp">
                        {formatTime(msg.timestamp)}
                      </span>
                      {showMessageActions && (
                        <span className="chat-message-meta-actions">
                          <ChatMessageActions
                            align="right"
                            copyText={actionCopyText}
                            copyLabel={t(
                              msg.role === "user"
                                ? "component.chat_message_actions.copy_request"
                                : "component.chat_message_actions.copy_response",
                            )}
                            canRetry={canRetryMessage}
                            feedbackValue={
                              msg.role === "assistant"
                                ? messageFeedback[feedbackKeyForMessage(msg, i)] || null
                                : null
                            }
                            disabled={streaming}
                            onRetry={() => handleRetryMessage(msg, i)}
                            onFeedback={
                              msg.role === "assistant" &&
                              Boolean(
                                msg.id &&
                                  (msg.conversation_id || currentConvId || streamingConvId),
                              )
                                ? (rating) =>
                                    handleMessageFeedback(
                                      msg,
                                      i,
                                      rating,
                                      actionCopyText,
                                    )
                                : undefined
                            }
                          />
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}

            <div ref={messagesEndRef} />
          </div>

          {/* Sticky approval bar — surfaces the oldest unresolved
            approval-style HITL with its human description and
            Approve/Reject. Hidden when nothing is pending. The
            `variant` keeps width / padding in lockstep with the
            composer below — when OutputPanel opens, the composer
            narrows from 920→820px and the bar follows. */}
          <ApprovalActionBar
            messages={messages}
            disabled={streaming}
            onResolve={handleHITLAction}
            variant={outputOpen ? "embedded-output-open" : "embedded"}
          />

          <div className="chat-tip-bar">
            <InlineTips surface="general_chat" placement="composer" />
          </div>

          <ChatInputFooter
            value={input}
            onChange={handleComposerChange}
            streaming={streaming}
            onSend={handleSend}
            onStop={handleStopRequest}
            placeholder={
              requestChatMode
                ? getChatModeInputPlaceholder(chatMode, chatModePayload)
                : messages.length === 0
                ? activeCapabilityConfig.placeholder
                : `Message ${title}... @ mention, # attach, / skill`
            }
            modeSlot={
              <ChatModeToolbar
                mode={chatMode}
                payload={chatModePayload}
                onModeChange={handleChatModeChange}
                onPayloadChange={setChatModePayload}
                disabled={streaming}
              />
            }
            replaceActionButtons={
              chatMode !== "auto"
            }
            mentions={mentionOptions}
            selectedMentions={selectedMentions}
            onMentionSelect={handleMentionSelect}
            onMentionRemove={handleMentionRemove}
            className={`embedded-chat-footer ${outputOpen ? "embedded-chat-footer--output-open" : ""}`}
          />
        </div>

        {outputOpen && (
          <OutputPanel
            artifact={selectedArtifact || outputState.artifacts[0]}
            onStop={closeOutputPanel}
            returnTo={selectedArtifactReturnTo}
          />
        )}
      </div>
    </div>
  );
}
