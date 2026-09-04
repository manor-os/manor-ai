import {
  lazy,
  Suspense,
  useState,
  useRef,
  useEffect,
  useLayoutEffect,
  useCallback,
  useMemo,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import {
  ApiError,
  api,
  type BlueprintDetail,
  type BlueprintCoverTemplate,
  type GlobalChatFlowEntrypoint,
  fetchProtectedFsResponse,
  isLocalFsUrl,
  resolveDisplayMediaUrl,
} from "../lib/api";
import { getAuthToken } from "../lib/authToken";
import { isMasterAgent } from "../lib/constants";
import {
  type ChatMessage,
  type ChatStreamSnapshot,
  type HITLRequest,
  type ResponseSurfaceSubmissionReceipt,
  type ResponseSurfaceSubmissionResult,
  type SubAgentEvent,
  type ToolCall,
  formatRuntimeQueueStatus,
  isInternalFilePermissionMessage,
  isRedundantApprovalResolutionReceipt,
  isTerminalStreamSnapshot,
  hitlActionTranscriptText,
  hasActivePersistedChatStream,
  mergeChatStreamSnapshot,
  mergeResolvedWorkflowMessage,
  normalizeWorkspaceRecommendation,
  parseToolCalls,
  pendingHITLIds,
  resolveGlobalWorkflowMessageAction,
  streamSnapshotNeedsHistory,
} from "../lib/chatStream";
import {
  collectResponseSurfaceSubmissionFailureMessageIds,
  collectResponseSurfaceSubmissionReceipts,
  isResponseSurfaceSubmissionMessage,
  rollbackResponseSurfaceSubmissionMessages,
  settleResponseSurfaceSubmissionFailure,
  responseSurfaceSubmissionMessage,
  responseSurfaceSubmissionMeta,
} from "../lib/responseSurface";
import { chatMessageAnchorId } from "../lib/chatMessageAnchor";
import { createdChatResourceReferences } from "../lib/chatResourceReferences";
import { invalidateKnowledgeQueries } from "../lib/knowledgeInvalidation";
import { sanitizeDocumentHtml } from "../lib/sanitizeDocumentHtml";
import {
  paginateManorDocument,
  renderManorDocument,
  type ManorDocumentRender,
} from "../lib/manorDocumentEngine";
import { useIsolatedHtmlPreview } from "../lib/useIsolatedHtmlPreview";
import { usePreviewFeatureAccess } from "../lib/previewFeatureAccess";
import {
  ChatStreamCompletionStatus,
  hasLocallyStreamedConversation,
  shouldIgnoreLocallyStoppedStreamUpdate,
  useChatStreamStore,
} from "../stores/chatStream";
import { useAuthStore } from "../stores/auth";
import { useToastStore } from "../stores/toast";
import type { Agent, Document, UserSummary, Workspace } from "../lib/types";
import ChatMarkdown from "./ChatMarkdown";
import WorkflowResultCard from "./WorkflowResultCard";
import CreatedResourceCard from "./CreatedResourceCard";
import AssistantMessageBlocks from "./AssistantMessageBlocks";
import ChatMessageActions, {
  chatMessageActionText,
  type ChatMessageFeedbackRating,
  displayContentForAssistantMessage,
  isRetryableAssistantMessage,
} from "./chat/ChatMessageActions";
import useChatMessageFeedback from "./chat/useChatMessageFeedback";
import ChatTimestamp from "./chat/ChatTimestamp";
import CollapsibleSentMessage from "./chat/CollapsibleSentMessage";
import ChatScrollRail, {
  type ChatScrollRailMarker,
} from "./chat/ChatScrollRail";
import {
  buildChatScrollRailTurnMarkers,
  type ChatScrollRailTurnSource,
} from "./chat/chatScrollRailTurns";
import ManorAvatar from "./ui/ManorAvatar";
import AgentActivityOrb, { inferAgentActivity } from "./ui/AgentActivityOrb";
import ThemeAwareImage from "./ui/ThemeAwareImage";
import WorkspaceIntroDialog from "./workspaces/WorkspaceIntroDialog";
import UserAvatar from "./ui/UserAvatar";
import ChatActionCard, { ApprovalSummary } from "./ui/ChatActionCard";
import ApprovalActionBar from "./ui/ApprovalActionBar";
import InlineTips from "./ui/InlineTips";
import WorkspaceRailVisual from "./ui/WorkspaceRailVisual";
import { DEFAULT_APPROVAL_OPTIONS } from "../lib/approvalOptions";
import ToolCallList from "./ui/ToolCallList";
import { ChatMessagesSkeleton } from "./ui/Skeleton";
import CreditLimitNotice from "./ui/CreditLimitNotice";
import Button from "./ui/Button";
import EmptyState from "./ui/EmptyState";
import LoadingSpinner from "./ui/LoadingSpinner";
import IsolatedHtmlPreviewFrame from "./ui/IsolatedHtmlPreviewFrame";
import Modal from "./ui/Modal";
import ResizablePaneGroup from "./ui/ResizablePaneGroup";
import WorkspaceDraftConfigurationPanel from "./WorkspaceDraftConfigurationPanel";
import WorkspaceRecommendationCard from "./WorkspaceRecommendationCard";
import useWorkspaceRecommendationActions from "./useWorkspaceRecommendationActions";
import {
  buildTemplateRemixPrompt,
  uploadTemplateRemixSource,
} from "./templateRemix";
import { WEBSITE_TEMPLATE_PROMPTS } from "./websiteTemplatePrompts";
import ChatInputFooter, {
  createChatMessageAttachmentSnapshot,
  manualSkillLabel,
  stripManualSkillTokens,
  stripWorkflowInvokeToken,
  workflowInvokeMessage,
  type AttachedItem,
  type ChatComposerSendContext,
  type ManualSkillItem,
  type MentionOption,
  type WorkflowInvokeItem,
} from "./ChatInputFooter";
import WorkflowRunHost, {
  buildWorkspaceWorkflowRunGroups,
  workflowHostOwnedMessageIds,
} from "./workflows/WorkflowRunHost";
import FlowTemplateGallery from "./workflows/FlowTemplateSamples";
import {
  chatModeFromCapability,
  type ChatBoxMode,
} from "./ChatModeSelector";
import ChatModeToolbar from "./ChatModeToolbar";
import ChatModeTemplateGallery, {
  type ChatModeTemplateSample,
} from "./ChatModeTemplateGallery";
import {
  getDefaultChatModePayload,
  getChatModeInputPlaceholder,
  type ChatModePayload,
} from "./ChatModeBriefPanel";
import {
  CHAT_MESSAGE_REFERENCE_CARD_LIMIT,
  type ChatMessageDisplayProjectionOptions,
  type ChatMessageDisplayReference,
  ChatMessageMetaChips,
  ChatMessageReferenceStrip,
  parseUserMessageDisplay,
  renderedChatMessageMarkdownForFileDedupe,
  resolveChatMessageReferenceDocument,
} from "./ChatMessageDisplay";
import {
  clearPendingChatRetry,
  savePendingChatRetry,
  type PendingChatRetry,
} from "../lib/chatRetry";
import {
  manualSkillReferences,
  resolveManualSkillReferenceIds,
} from "../lib/manualSkillRefs";
import {
  extractPlatformFileReferences,
  explicitDiagramIdentityOverridesGenericJson,
  filterGeneratedFileRecordsAlreadyLinkedInMarkdown,
  filterGeneratedFileRecordsAlreadyRepresented,
  fileReferenceKind,
  generatedFileFsPath,
  generatedFileOpenReference,
} from "../lib/fileReferences";
import {
  assertDiagramPreviewFileSize,
  diagramPreviewTextFromFsRead,
  readDiagramPreviewText,
} from "../lib/diagram/previewLimits";
import {
  INSERT_CHAT_COMPOSER_EVENT,
  type InsertChatComposerDetail,
} from "../lib/selectionActions";

const LazyDiagramArtifactViewer = lazy(() => import("./diagram/DiagramArtifactViewer"));

function maybeLocalCodingRunNoticeForTools(_tools: ToolCall[]): string | null {
  return null;
}
import {
  IconCheckCircle,
  IconChevronLeft,
  IconChevronRight,
  IconDownload,
  IconFlow,
  IconPlus,
  IconReport,
  IconSparkles,
} from "./icons";
import type { WorkflowTemplate } from "./workflows/WorkflowTemplates";
import { t } from "../lib/i18n";
import { useChatAutoFollow } from "../lib/useChatAutoFollow";
import { isCodeLikeFile } from "../lib/codeFiles";
import { parseDelimitedText } from "../lib/delimitedText";
import {
  spreadsheetChartsFromFile,
  spreadsheetMergeAt,
  spreadsheetSheetsFromWorkbook,
  type SpreadsheetSheetModel,
} from "../lib/spreadsheetOoxml";
import SpreadsheetChartPreview from "./SpreadsheetChartPreview";
import {
  pickRandomSoloBusinessIdeas,
  soloBusinessIdeaExecutionKey,
  soloBusinessIdeaKey,
  type SoloBusinessIdeaDefinition,
} from "../lib/soloBusinessIdeas";
import {
  blueprintCoverDataUrl,
  blueprintOilPaintingCoverUrl,
} from "../services/blueprintCoverTemplate";


interface AgentInfo {
  id: string;
  name: string;
  color?: string;
  avatar_url?: string;
}

type ChatRetryRequest = Omit<PendingChatRetry, "createdAt">;

/*
 * How long a followed run may stay silent before the reader asks the API what
 * happened. Snapshots are the fast path, not a guaranteed one: nothing replays
 * what a socket reconnect dropped, so this is also the floor at which a page
 * whose WebSocket never recovers still makes progress.
 */
const FOLLOWED_RUN_SILENCE_MS = 45_000;
/* ≈15 minutes of a row claiming "streaming" before the reader stops asking. */
const FOLLOWED_RUN_MAX_POLLS = 20;

const CHAT_MESSAGE_PAGE_SIZE = 75;
const GLOBAL_WORKFLOW_INVALIDATION_QUERY_KEYS = [["conversations"]] as const;

function hasVisibleUserContent(message: ChatMessage | undefined): boolean {
  return Boolean(
    message && message.role === "user" && (toDisplayText(message.content) || "").trim(),
  );
}

const isNewManorConversationId = (id?: string) =>
  !!id && id.startsWith("manor-new:");

interface EmbeddedChatProps {
  conversationId: string;
  title: string;
  subtitle?: string;
  agents?: AgentInfo[];
  avatarUrl?: string; // DM agent avatar — if set, replaces ManorAvatar
  agentId?: string; // DM agent ID — ensures tools/prompt are resolved correctly
  onConversationResolved?: (conversationId: string) => void;
  onNewConversation?: () => void;
  showWorkspaceIntro?: boolean;
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
type ArtifactDocumentPage = {
  index: number;
  url: string;
  width: number | null;
  height: number | null;
};
type WorkspaceCapability =
  | "workspace"
  | "slides"
  | "docs"
  | "pdf"
  | "sheets"
  | "website"
  | "image"
  | "video"
  | "research"
  | "agents"
  | "automations"
  | "flows";

type WorkspaceSamplePreviewContent = {
  label?: string;
  title?: string;
  lines?: string[];
  chips?: string[];
  imageSrc?: string;
  imageAlt?: string;
  imageCaption?: string;
  previewImageSrc?: string;
  previewImageDarkSrc?: string;
  previewImageAlt?: string;
  detailImageSrcs?: string[];
  detailImageAlt?: string;
  sampleSrc?: string;
  sampleLabel?: string;
  videoSrc?: string;
  marketplaceCover?: boolean;
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
    chatModePayloadPatch?: ChatModePayload;
  }>;
};

type WorkspaceSample = WorkspaceCapabilityConfig["samples"][number];

type IdeaQuickAction = {
  id: "new-idea" | "validate-idea";
  title: string;
  description: string;
  accent: string;
};

type IdeaQuickActionRequest = {
  message: string;
  skill: ManualSkillItem;
};

/*
 * The empty chat pre-focuses the "new idea" rail card purely for looks. That
 * focus must never be mistaken for the user asking for the idea skill: the
 * server treats any client-supplied manual Skill reference as an explicit selection
 * and force-invokes it in round 1, before the model gets to reason. So the
 * composer records WHO chose the mode, and only a deliberate gesture ("user")
 * is allowed to attach a built-in skill to the send.
 */
type IdeaComposerSelection = {
  mode: IdeaQuickAction["id"];
  origin: "user" | "default";
};

const IDEA_BUILT_IN_SKILLS: Record<IdeaQuickAction["id"], ManualSkillItem> = {
  "new-idea": {
    id: "solo-business-idea-finder",
    name: "solo-business-idea-finder",
    slug: "solo-business-idea-finder",
    reference: {
      kind: "slug",
      value: "solo-business-idea-finder",
      source: "builtin",
    },
  },
  "validate-idea": {
    id: "solo-business-idea-review",
    name: "solo-business-idea-review",
    slug: "solo-business-idea-review",
    reference: {
      kind: "slug",
      value: "solo-business-idea-review",
      source: "builtin",
    },
  },
};

function ideaField(
  idea: SoloBusinessIdeaDefinition,
  field: Parameters<typeof soloBusinessIdeaKey>[1],
) {
  return t(soloBusinessIdeaKey(idea, field));
}

function ideaCandidateRequest(
  action: IdeaQuickAction,
  idea: SoloBusinessIdeaDefinition,
): IdeaQuickActionRequest {
  const context = t("component.embedded_chat.idea_library.selected_context", {
    title: ideaField(idea, "title"),
    buyer: ideaField(idea, "buyer"),
    promise: ideaField(idea, "promise"),
    revenue: ideaField(idea, "revenue"),
    signal: ideaField(idea, "signal"),
    test: ideaField(idea, "test"),
    execution: t(soloBusinessIdeaExecutionKey(idea.manorExecution)),
    manorPath: ideaField(idea, "manorPath"),
  });
  const instruction = t(
    action.id === "new-idea"
      ? "component.embedded_chat.idea_library.explore_instruction"
      : "component.embedded_chat.idea_library.validate_instruction",
  );
  return {
    message: `${context}\n\n${instruction}`,
    skill: IDEA_BUILT_IN_SKILLS[action.id],
  };
}

function freshIdeaRequest(): IdeaQuickActionRequest {
  return {
    message: t("component.embedded_chat.new_idea_today_prompt"),
    skill: IDEA_BUILT_IN_SKILLS["new-idea"],
  };
}

type WorkspaceRailKey = WorkspaceCapability | IdeaQuickAction["id"];

const WORKSPACE_RAIL_ORDER: WorkspaceRailKey[] = [
  "research",
  "agents",
  "automations",
  "flows",
  "new-idea",
  "validate-idea",
  "workspace",
  "slides",
  "docs",
  "pdf",
  "sheets",
  "website",
  "image",
  "video",
];

function ideaQuickActions(): IdeaQuickAction[] {
  return [
    {
      id: "new-idea",
      title: t("component.embedded_chat.new_idea_today"),
      description: t("component.embedded_chat.new_idea_today_desc"),
      accent: "var(--accent)",
    },
    {
      id: "validate-idea",
      title: t("component.embedded_chat.validate_my_idea"),
      description: t("component.embedded_chat.validate_my_idea_desc"),
      accent: "var(--text-muted)",
    },
  ];
}

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
    label: t("page.workspace_detail.documents"),
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
    key: "pdf",
    label: "PDF",
    icon: "PDF",
    accent: "#7b5b58",
    description: t("component.embedded_chat.side_hustle.pdf.description"),
    placeholder: t("component.embedded_chat.side_hustle.pdf.placeholder"),
    templates: [],
    samples: [],
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
          imageSrc: "/assets/blueprints/oil-paintings/productized-service.webp",
          imageAlt: t("component.embedded_chat.preview_workspace_scene_alt"),
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
          imageSrc: "/assets/blueprints/oil-paintings/automation-launch.webp",
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
          imageSrc: "/assets/blueprints/oil-paintings/digital-store.webp",
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
  {
    key: "flows",
    label: t("component.chat_mode.flows"),
    icon: "FLOW",
    accent: "var(--text-muted)",
    description: t("component.chat_mode.flows_helper"),
    placeholder: t("component.chat_mode.flows_placeholder"),
    templates: [],
    samples: [],
  },
];

type SideHustleSampleDefinition = {
  id: string;
  preview: WorkspaceCapability;
  coverTemplate?: BlueprintCoverTemplate;
  imageSrc?: string;
  sampleSrc?: string;
  videoSrc?: string;
  detailPageCount?: number;
  chatModePayloadPatch?: ChatModePayload;
  copy?: {
    title: string;
    outcome: string;
    prompt: string;
    label: string;
    preview_title: string;
    line_1: string;
    line_2: string;
    chip_1: string;
    chip_2: string;
  };
};

const sideHustleText = (key: string) =>
  t(`component.embedded_chat.side_hustle.${key}`);

const SIDE_HUSTLE_SAMPLE_DEFINITIONS: Record<
  Exclude<WorkspaceCapability, "flows">,
  SideHustleSampleDefinition[]
> = {
  workspace: [
    {
      id: "productized_service_os",
      preview: "workspace",
      coverTemplate: {
        motif: "service",
      palette: "stone",
      variant: 1,
      seed: 2061640672,
      },
    },
    {
      id: "digital_product_store_os",
      preview: "workspace",
      coverTemplate: {
        motif: "commerce",
      palette: "blue",
      variant: 0,
      seed: 2495511228,
      },
    },
  ],
  slides: [
    {
      id: "aurelia_far_north",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/aurelia-far-north.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Aurelia — The Far North",
        outcome: "Create an image-led luxury travel narrative with cinematic pacing, quiet typography, and full-bleed editorial photography.",
        prompt: "Remix Aurelia — The Far North into a new 10-slide premium travel story for a different destination and fictional travel brand. Preserve the cinematic image-led pacing, quiet luxury typography, disciplined split layouts, itinerary rhythm, and editorial restraint while replacing all content and imagery.",
        label: "Luxury travel",
        preview_title: "Aurelia — The Far North",
        line_1: "Cinematic travel narrative with full-bleed photography",
        line_2: "Quiet luxury pacing, itinerary, details, and closing image",
        chip_1: "Travel",
        chip_2: "Image-led",
      },
    },
    {
      id: "null_signal_incident_manual",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/null-signal-incident-manual.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Null Signal Incident Manual",
        outcome: "Build a black terminal-style cybersecurity field manual with operational diagrams, triage logic, and high-contrast technical hierarchy.",
        prompt: "Remix the Null Signal Incident Manual into a new 10-slide operational playbook for a different technical topic and fictional organization. Preserve the black terminal aesthetic, cyan and amber signal colors, grid system, procedural diagrams, severity logic, and field-manual typography while replacing all content.",
        label: "Cybersecurity",
        preview_title: "Null Signal Incident Manual",
        line_1: "Technical field manual with triage and response logic",
        line_2: "Dark grid, signal colors, diagrams, and procedures",
        chip_1: "Security",
        chip_2: "Playbook",
      },
    },
    {
      id: "moss_moon_story",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/moss-and-moon-story.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Moss & Moon Storybook",
        outcome: "Tell a warm illustrated children's story with paper texture, gentle color, hand-drawn framing, and expressive page turns.",
        prompt: "Remix Moss & Moon into a new 10-slide illustrated story for a different fictional tale and audience. Preserve the warm paper texture, hand-drawn framing, playful typography, gentle color system, character-led pacing, and storybook page rhythm while replacing all story content and illustrations.",
        label: "Illustrated story",
        preview_title: "Moss & Moon Storybook",
        line_1: "Warm illustrated narrative with tactile paper texture",
        line_2: "Character moments, expressive type, and gentle pacing",
        chip_1: "Story",
        chip_2: "Illustrated",
      },
    },
    {
      id: "kinetic_autumn_27",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/kinetic-autumn-27.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Kinetic / Autumn 27",
        outcome: "Present a fashion collection through bold art direction, editorial cropping, saturated color, and runway-scale typography.",
        prompt: "Remix Kinetic / Autumn 27 into a new 10-slide fashion or culture lookbook for a different fictional label. Preserve the editorial image crops, oversized typography, saturated color blocking, collection sequencing, credits, and high-fashion pacing while replacing all copy and imagery.",
        label: "Fashion lookbook",
        preview_title: "Kinetic / Autumn 27",
        line_1: "Editorial lookbook with bold crops and saturated color",
        line_2: "Collection story, material details, lineup, and credits",
        chip_1: "Fashion",
        chip_2: "Editorial",
      },
    },
    {
      id: "blue_commons_impact_report",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/blue-commons-impact-report.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Blue Commons Impact Report",
        outcome: "Combine field photography, human stories, KPI evidence, and data graphics in an accessible nonprofit impact report.",
        prompt: "Remix the Blue Commons Impact Report into a new 10-slide impact story for a different fictional nonprofit or public-interest initiative. Preserve the field-photography system, human story pages, evidence charts, KPI hierarchy, warm coastal palette, and report structure while replacing all claims, figures, and imagery.",
        label: "Impact report",
        preview_title: "Blue Commons Impact Report",
        line_1: "Human stories, field photography, KPIs, and evidence",
        line_2: "Accessible nonprofit reporting with clear data hierarchy",
        chip_1: "Impact",
        chip_2: "Data story",
      },
    },
    {
      id: "monolith_house",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/monolith-house.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Monolith House",
        outcome: "Showcase architecture through restrained Swiss typography, material studies, plans, photography, and generous negative space.",
        prompt: "Remix Monolith House into a new 10-slide architecture or design monograph for a different fictional project. Preserve the restrained grid, neutral material palette, technical captions, photography-to-plan rhythm, credits system, and generous negative space while replacing all project content and images.",
        label: "Architecture",
        preview_title: "Monolith House",
        line_1: "Architecture monograph with plans and material studies",
        line_2: "Restrained grid, technical captions, and calm whitespace",
        chip_1: "Architecture",
        chip_2: "Monograph",
      },
    },
    {
      id: "orbital_bakery_brand_launch",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/orbital-bakery-brand-launch.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Orbital Bakery Brand Launch",
        outcome: "Launch a playful consumer brand with product photography, packaging moments, campaign language, and a distinctive graphic system.",
        prompt: "Remix the Orbital Bakery Brand Launch into a new 10-slide consumer brand launch for a different fictional product. Preserve the playful art direction, packaging moments, product photography, campaign language hierarchy, graphic motifs, and rollout structure while replacing the brand, copy, and imagery.",
        label: "Brand launch",
        preview_title: "Orbital Bakery Brand Launch",
        line_1: "Consumer brand story, packaging, campaign, and rollout",
        line_2: "Playful graphic system with product-led photography",
        chip_1: "Brand",
        chip_2: "Launch",
      },
    },
    {
      id: "helio_sx_evidence_review",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/helio-sx-evidence-review.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Helio-SX Evidence Review",
        outcome: "Communicate clinical or scientific evidence with precise tables, workflow diagrams, safety signals, and conference-grade restraint.",
        prompt: "Remix the Helio-SX Evidence Review into a new 10-slide scientific or clinical conference deck for a different fictional study. Preserve the evidence-first hierarchy, precise tables, workflow diagrams, restrained blue palette, safety-signal pages, and conference footer system while replacing all study content and data.",
        label: "Scientific evidence",
        preview_title: "Helio-SX Evidence Review",
        line_1: "Scientific conference deck with evidence and workflow",
        line_2: "Precise tables, safety signals, and restrained hierarchy",
        chip_1: "Science",
        chip_2: "Evidence",
      },
    },
    {
      id: "line_47_wayfinding",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/line-47-wayfinding.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Line 47 Wayfinding System",
        outcome: "Explain a public-space design system through bold numbered stages, signage rules, maps, and rigorous black-red-blue typography.",
        prompt: "Remix the Line 47 Wayfinding System into a new 10-slide service, transit, or public-design proposal for a different fictional system. Preserve the bold numbered hierarchy, red-blue-black palette, signage logic, rollout timeline, diagram language, and rigorous modernist grid while replacing all content.",
        label: "Wayfinding system",
        preview_title: "Line 47 Wayfinding System",
        line_1: "Modernist service system with signage and rollout logic",
        line_2: "Bold numbers, maps, diagrams, and disciplined grid",
        chip_1: "Wayfinding",
        chip_2: "System",
      },
    },
    {
      id: "cinder_road_film_pitch",
      preview: "slides",
      sampleSrc: "/assets/samples/artifacts/slides/cinder-road-film-pitch.pptx",
      detailPageCount: 3,
      chatModePayloadPatch: { render: "editable" },
      copy: {
        title: "Cinder Road Film Pitch",
        outcome: "Pitch a film through cinematic stills, character beats, tonal references, production notes, and emotionally paced typography.",
        prompt: "Remix the Cinder Road Film Pitch into a new 10-slide film, series, or documentary pitch for a different fictional story. Preserve the cinematic stills, tonal pacing, character introductions, story beats, production framing, and emotionally restrained typography while replacing all narrative content and imagery.",
        label: "Film pitch",
        preview_title: "Cinder Road Film Pitch",
        line_1: "Cinematic narrative with characters, tone, and story beats",
        line_2: "Image-led pitch pacing with production framing",
        chip_1: "Film",
        chip_2: "Pitch",
      },
    },
  ],
  docs: [
    {
      id: "shelter_photo_report",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/shelter-photo-report.docx",
      detailPageCount: 2,
    },
    {
      id: "adaptive_daylight_experiment",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/adaptive-daylight-experiment-report.docx",
      detailPageCount: 2,
    },
    {
      id: "circular_timber_investment_memo",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/circular-timber-investment-memo.docx",
      detailPageCount: 1,
    },
    {
      id: "partnership_pilot_letter",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/partnership-pilot-letterhead.docx",
      detailPageCount: 1,
    },
    {
      id: "personal_company_copy",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/personal-company-copy.docx",
      detailPageCount: 3,
      copy: {
        title: "Personal Company Profile",
        outcome: "Turn a solo operator's positioning, services, proof, and working style into a polished company profile.",
        prompt: "Remix the Personal Company Profile for a different independent business. Preserve the concise editorial structure, service positioning, proof points, process, and contact close while replacing all sample copy with a coherent English company story.",
        label: "Company profile",
        preview_title: "Personal Company Profile",
        line_1: "Positioning, services, proof, and working process",
        line_2: "A concise profile for clients, partners, and proposals",
        chip_1: "Profile",
        chip_2: "Business",
      },
    },
    {
      id: "side_hustle_product_manual",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/side-hustle-product-manual.docx",
      detailPageCount: 3,
      copy: {
        title: "Side Hustle Product Manual",
        outcome: "Package a small product or service into a practical manual covering the offer, workflow, delivery, and review loop.",
        prompt: "Remix the Side Hustle Product Manual for a different productized offer. Preserve the operating-manual structure, offer definition, customer journey, delivery checklist, quality controls, and review cadence while replacing the sample business and instructions.",
        label: "Operating manual",
        preview_title: "Side Hustle Product Manual",
        line_1: "Offer, customer journey, delivery, and quality controls",
        line_2: "A repeatable operating system for a small business",
        chip_1: "Manual",
        chip_2: "Operations",
      },
    },
    {
      id: "travel_ebook_plan",
      preview: "docs",
      sampleSrc: "/assets/samples/artifacts/docs/travel-ebook-plan.docx",
      detailPageCount: 3,
      copy: {
        title: "Travel Ebook Plan",
        outcome: "Shape a destination idea into an editorial ebook plan with audience, chapter arc, visual direction, and production steps.",
        prompt: "Remix the Travel Ebook Plan for a different destination and audience. Preserve the editorial concept, reader promise, chapter plan, image direction, research checklist, production timeline, and launch notes while replacing all sample travel content.",
        label: "Editorial plan",
        preview_title: "Travel Ebook Plan",
        line_1: "Audience, reader promise, chapters, and visual direction",
        line_2: "Research, production, review, and launch plan",
        chip_1: "Travel",
        chip_2: "Ebook",
      },
    },
  ],
  pdf: [
    {
      id: "solis_editorial_report",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/solis-editorial-market-report.pdf",
      detailPageCount: 1,
    },
    {
      id: "wildwood_creative_proposal",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/wildwood-bold-creative-proposal.pdf",
      detailPageCount: 2,
    },
    {
      id: "gridline_swiss_invoice",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/gridline-swiss-studio-invoice.pdf",
      detailPageCount: 1,
    },
    {
      id: "civic_ecologies_paper",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/civic-ecologies-academic-paper.pdf",
      detailPageCount: 3,
    },
    {
      id: "northstar_quarterly_review",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/northstar-quarterly-business-review.pdf",
      detailPageCount: 2,
      copy: {
        title: "Northstar Quarterly Business Review",
        outcome: "Turn quarterly performance into a concise operating review with metrics, decisions, risks, and next-quarter priorities.",
        prompt: "Remix this Quarterly Business Review for my company. Preserve the executive summary, KPI hierarchy, decision framing, risk section, and signal chart while replacing all sample business data.",
        label: "Business review",
        preview_title: "Northstar Quarterly Business Review",
        line_1: "Growth, retention, risks, and operating decisions",
        line_2: "Two-page executive review with a clear signal chart",
        chip_1: "QBR",
        chip_2: "Strategy",
      },
    },
    {
      id: "open_harbor_impact_report",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/open-harbor-community-impact-report.pdf",
      detailPageCount: 2,
      copy: {
        title: "Open Harbor Community Impact Report",
        outcome: "Communicate program reach, measurable outcomes, and next-year commitments through a warm nonprofit report.",
        prompt: "Remix this Community Impact Report for my organization. Keep the human-centered narrative, outcome metrics, program sections, and next commitment while replacing all sample impact data.",
        label: "Impact report",
        preview_title: "Open Harbor Community Impact Report",
        line_1: "Program reach, outcomes, and community commitments",
        line_2: "Warm editorial layout for donors and partners",
        chip_1: "Nonprofit",
        chip_2: "Impact",
      },
    },
    {
      id: "lumen_festival_sponsorship",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/lumen-festival-sponsorship-proposal.pdf",
      detailPageCount: 2,
      copy: {
        title: "Lumen Festival Sponsorship Proposal",
        outcome: "Pitch an event partnership with audience scale, program value, and clear sponsor tiers.",
        prompt: "Remix this Festival Sponsorship Proposal for my event. Preserve the cinematic cover, audience metrics, partnership tiers, and demographic signal view while replacing the sample event and offer.",
        label: "Sponsorship proposal",
        preview_title: "Lumen Festival Sponsorship Proposal",
        line_1: "Audience reach, sponsor value, and partnership tiers",
        line_2: "High-energy dark campaign layout",
        chip_1: "Event",
        chip_2: "Sponsors",
      },
    },
    {
      id: "relay_product_launch_brief",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/relay-product-launch-brief.pdf",
      detailPageCount: 2,
      copy: {
        title: "Relay Product Launch Brief",
        outcome: "Align a product launch around audience, promise, campaign stories, timing, and activation goals.",
        prompt: "Remix this Product Launch Brief for my product. Keep the audience, promise, launch motion, KPI summary, and conversion signal view while replacing the sample product details.",
        label: "Launch brief",
        preview_title: "Relay Product Launch Brief",
        line_1: "Audience, positioning, campaign motion, and goals",
        line_2: "Modern product launch one-pager plus detail page",
        chip_1: "Product",
        chip_2: "Launch",
      },
    },
    {
      id: "casa_lumen_investment_brief",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/casa-lumen-real-estate-investment-brief.pdf",
      detailPageCount: 2,
      copy: {
        title: "Casa Lumen Real Estate Investment Brief",
        outcome: "Summarize a property opportunity with project economics, investment thesis, value creation, and milestones.",
        prompt: "Remix this Real Estate Investment Brief for my property. Preserve the project summary, cost and return metrics, thesis, value-creation plan, and milestone page while replacing all sample assumptions.",
        label: "Investment brief",
        preview_title: "Casa Lumen Real Estate Investment Brief",
        line_1: "Project economics, thesis, value creation, and milestones",
        line_2: "Warm hospitality investment presentation",
        chip_1: "Real estate",
        chip_2: "Investment",
      },
    },
    {
      id: "morrow_brand_foundations",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/morrow-brand-foundations-guide.pdf",
      detailPageCount: 2,
      copy: {
        title: "Morrow Brand Foundations Guide",
        outcome: "Define a brand through positioning, voice, visual behavior, and a focused personality profile.",
        prompt: "Remix this Brand Foundations Guide for my brand. Preserve the positioning, voice, visual behavior, personality metrics, and compact editorial format while replacing all sample identity content.",
        label: "Brand guide",
        preview_title: "Morrow Brand Foundations Guide",
        line_1: "Positioning, voice, visual behavior, and personality",
        line_2: "Compact two-page brand system",
        chip_1: "Brand",
        chip_2: "Guidelines",
      },
    },
    {
      id: "ember_table_tasting_menu",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/ember-table-seasonal-tasting-menu.pdf",
      detailPageCount: 2,
      copy: {
        title: "Ember Table Seasonal Tasting Menu",
        outcome: "Present a seasonal restaurant experience through elegant course descriptions, pricing, and flavor progression.",
        prompt: "Remix this Seasonal Tasting Menu for my restaurant. Keep the refined split layout, course progression, pricing hierarchy, and flavor signal view while replacing the sample menu and pairings.",
        label: "Tasting menu",
        preview_title: "Ember Table Seasonal Tasting Menu",
        line_1: "Courses, pairings, pricing, and flavor progression",
        line_2: "Warm editorial menu for a fine dining experience",
        chip_1: "Restaurant",
        chip_2: "Menu",
      },
    },
    {
      id: "fieldwork_conference_program",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/fieldwork-design-conference-program.pdf",
      detailPageCount: 2,
      copy: {
        title: "Fieldwork Design Conference Program",
        outcome: "Organize a conference into a clear program with event positioning, session times, formats, and participation mix.",
        prompt: "Remix this Conference Program for my event. Preserve the bold numbered cover, event summary, timed sessions, format mix, and practical program structure while replacing all sample content.",
        label: "Conference program",
        preview_title: "Fieldwork Design Conference Program",
        line_1: "Event positioning, sessions, times, and format mix",
        line_2: "Bold numbered program built for quick scanning",
        chip_1: "Conference",
        chip_2: "Program",
      },
    },
    {
      id: "good_company_handbook",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/good-company-employee-handbook.pdf",
      detailPageCount: 2,
      copy: {
        title: "Good Company Employee Handbook",
        outcome: "Turn team values into clear operating agreements for decisions, communication, focus time, and escalation.",
        prompt: "Remix this Employee Handbook for my team. Keep the practical operating principles, communication defaults, focus-time rules, escalation guidance, and calm editorial structure while replacing the sample policies.",
        label: "Employee handbook",
        preview_title: "Good Company Employee Handbook",
        line_1: "Decisions, communication, focus, and team care",
        line_2: "Practical operating agreements for distributed teams",
        chip_1: "People",
        chip_2: "Handbook",
      },
    },
    {
      id: "verdant_sustainability_scorecard",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/verdant-sustainability-scorecard.pdf",
      detailPageCount: 2,
      copy: {
        title: "Verdant Sustainability Scorecard",
        outcome: "Report environmental performance across energy, water, materials, logistics, and next-step priorities.",
        prompt: "Remix this Sustainability Scorecard for my organization. Preserve the headline reductions, category analysis, operational priorities, and performance chart while replacing all sample environmental data.",
        label: "Sustainability scorecard",
        preview_title: "Verdant Sustainability Scorecard",
        line_1: "Energy, water, materials, freight, and priorities",
        line_2: "Evidence-led environmental performance summary",
        chip_1: "ESG",
        chip_2: "Scorecard",
      },
    },
    {
      id: "orbit_project_status",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/orbit-project-status-report.pdf",
      detailPageCount: 2,
      copy: {
        title: "Orbit Project Status Report",
        outcome: "Give stakeholders a fast, decision-ready view of delivery health, progress, dependencies, and escalations.",
        prompt: "Remix this Project Status Report for my initiative. Keep the overall health, completion metric, escalation count, completed and in-progress work, decision request, and workstream chart while replacing the sample project.",
        label: "Status report",
        preview_title: "Orbit Project Status Report",
        line_1: "Health, progress, dependencies, and decisions",
        line_2: "Dark executive status format with workstream signals",
        chip_1: "Project",
        chip_2: "Status",
      },
    },
    {
      id: "signal_ux_research_summary",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/signal-ux-research-summary.pdf",
      detailPageCount: 2,
      copy: {
        title: "Signal UX Research Summary",
        outcome: "Translate interviews and observed workflows into clear themes, evidence, and actionable design direction.",
        prompt: "Remix this UX Research Summary for my study. Preserve the participant summary, core themes, evidence-led findings, design direction, and signal chart while replacing the sample research content.",
        label: "Research summary",
        preview_title: "Signal UX Research Summary",
        line_1: "Participants, themes, evidence, and design direction",
        line_2: "Concise research readout for product decisions",
        chip_1: "UX",
        chip_2: "Research",
      },
    },
    {
      id: "bright_steps_grant_proposal",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/bright-steps-youth-grant-proposal.pdf",
      detailPageCount: 2,
      copy: {
        title: "Bright Steps Youth Grant Proposal",
        outcome: "Frame a funding request around community need, program design, budget, reach, and evaluation.",
        prompt: "Remix this Youth Grant Proposal for my program. Keep the need statement, program model, funding request, reach metrics, evaluation plan, and resource chart while replacing all sample details.",
        label: "Grant proposal",
        preview_title: "Bright Steps Youth Grant Proposal",
        line_1: "Need, program, funding request, and evaluation",
        line_2: "Optimistic proposal for foundations and public funders",
        chip_1: "Grant",
        chip_2: "Youth",
      },
    },
    {
      id: "clear_path_patient_guide",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/clear-path-patient-preparation-guide.pdf",
      detailPageCount: 2,
      copy: {
        title: "Clear Path Patient Preparation Guide",
        outcome: "Explain a clinical visit in calm, accessible language covering preparation, arrival, the procedure, and aftercare.",
        prompt: "Remix this Patient Preparation Guide for my clinic or procedure. Preserve the reassuring tone, before-during-after structure, timing metrics, safety guidance, and journey view while replacing the sample instructions.",
        label: "Patient guide",
        preview_title: "Clear Path Patient Preparation Guide",
        line_1: "Preparation, arrival, procedure, and aftercare",
        line_2: "Calm accessible healthcare communication",
        chip_1: "Healthcare",
        chip_2: "Guide",
      },
    },
    {
      id: "monolith_architecture_portfolio",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/monolith-architecture-portfolio.pdf",
      detailPageCount: 2,
      copy: {
        title: "Monolith Architecture Portfolio",
        outcome: "Introduce an architecture practice through a strong studio point of view, selected projects, and design priorities.",
        prompt: "Remix this Architecture Portfolio for my studio. Keep the cinematic dark cover, practice statement, project summaries, portfolio metrics, and design-priority chart while replacing the sample work.",
        label: "Architecture portfolio",
        preview_title: "Monolith Architecture Portfolio",
        line_1: "Practice statement, selected projects, and design priorities",
        line_2: "Cinematic portfolio for clients and collaborators",
        chip_1: "Architecture",
        chip_2: "Portfolio",
      },
    },
    {
      id: "stillwater_photography_guide",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/stillwater-photography-pricing-guide.pdf",
      detailPageCount: 2,
      copy: {
        title: "Stillwater Photography Pricing Guide",
        outcome: "Present photography services, deliverables, pricing, usage, and workflow in a refined client-ready guide.",
        prompt: "Remix this Photography Pricing Guide for my studio. Preserve the editorial positioning, service packages, pricing hierarchy, deliverables, and project workflow while replacing the sample offer.",
        label: "Pricing guide",
        preview_title: "Stillwater Photography Pricing Guide",
        line_1: "Services, deliverables, pricing, and workflow",
        line_2: "Refined client guide for a creative studio",
        chip_1: "Photography",
        chip_2: "Pricing",
      },
    },
    {
      id: "decisive_workshop_book",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/decisive-team-workshop-workbook.pdf",
      detailPageCount: 2,
      copy: {
        title: "Decisive Team Workshop Workbook",
        outcome: "Guide a team from an ambiguous question to evidence, options, ownership, and an explicit review date.",
        prompt: "Remix this Team Workshop Workbook for my session. Keep the timing, exercise sequence, decision framing, evidence mapping, ownership step, and facilitation chart while replacing the sample workshop content.",
        label: "Workshop workbook",
        preview_title: "Decisive Team Workshop Workbook",
        line_1: "Decision framing, evidence, options, and ownership",
        line_2: "A practical 90-minute facilitator workbook",
        chip_1: "Workshop",
        chip_2: "Decisions",
      },
    },
    {
      id: "street_safety_policy_brief",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/street-safety-policy-brief.pdf",
      detailPageCount: 2,
      copy: {
        title: "Street Safety Policy Brief",
        outcome: "Turn public evidence into a focused policy recommendation with cost, target outcomes, and accountability measures.",
        prompt: "Remix this Policy Brief for my policy issue. Preserve the problem statement, recommendation package, target metrics, implementation cost, accountability plan, and evidence chart while replacing the sample topic.",
        label: "Policy brief",
        preview_title: "Street Safety Policy Brief",
        line_1: "Problem, policy package, cost, and accountability",
        line_2: "Decision-ready brief for public leaders",
        chip_1: "Policy",
        chip_2: "Civic",
      },
    },
    {
      id: "harbor_financial_summary",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/harbor-annual-financial-summary.pdf",
      detailPageCount: 2,
      copy: {
        title: "Harbor Annual Financial Summary",
        outcome: "Explain annual revenue, margin, investment, reserves, and outlook in a clear member-friendly format.",
        prompt: "Remix this Annual Financial Summary for my organization. Keep the headline financial metrics, revenue narrative, investment summary, outlook, and multi-year chart while replacing all sample figures.",
        label: "Financial summary",
        preview_title: "Harbor Annual Financial Summary",
        line_1: "Revenue, margin, investment, reserves, and outlook",
        line_2: "Clear annual summary for members and stakeholders",
        chip_1: "Finance",
        chip_2: "Annual report",
      },
    },
    {
      id: "slow_coast_itinerary",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/slow-coast-seven-day-itinerary.pdf",
      detailPageCount: 2,
      copy: {
        title: "Slow Coast Seven-Day Itinerary",
        outcome: "Shape a relaxed multi-stop trip with daily rhythm, transport choices, signature experiences, and open time.",
        prompt: "Remix this Seven-Day Itinerary for my destination. Preserve the slow-travel positioning, route summary, day groups, transport metrics, activity balance, and planning chart while replacing all sample locations.",
        label: "Travel itinerary",
        preview_title: "Slow Coast Seven-Day Itinerary",
        line_1: "Route, daily rhythm, transport, food, and open time",
        line_2: "Warm editorial guide for a seven-day journey",
        chip_1: "Travel",
        chip_2: "Itinerary",
      },
    },
    {
      id: "atelier_north_company_profile",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/atelier-north-company-profile.pdf",
      detailPageCount: 3,
      copy: {
        title: "Atelier North Company Profile",
        outcome: "Introduce a company through an image-led founder story, point of view, proof metrics, services, and working process.",
        prompt: "Remix the attached Atelier North Company Profile into a new three-page company profile for my business. Preserve the founder-led editorial photography, split cover, serif hierarchy, point-of-view page, proof metrics, service narrative, and process page while replacing the sample company, copy, metrics, and imagery.",
        label: "Company profile",
        preview_title: "Atelier North Company Profile",
        line_1: "Founder story, positioning, proof, services, and process",
        line_2: "Warm editorial photography with a premium studio voice",
        chip_1: "Company",
        chip_2: "Profile",
      },
    },
    {
      id: "cascade_growth_business_plan",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/cascade-growth-business-plan.pdf",
      detailPageCount: 3,
      copy: {
        title: "Cascade Growth Business Plan",
        outcome: "Frame a business plan around the market wedge, repeatable growth engine, operating model, and financial targets.",
        prompt: "Remix the attached Cascade Growth Business Plan into a new three-page business plan for my company. Preserve the image-led analytics cover, operating thesis, market wedge, growth engine, capital plan, financial progression, KPI cards, and restrained blue visual system while replacing all sample assumptions and imagery.",
        label: "Business plan",
        preview_title: "Cascade Growth Business Plan",
        line_1: "Market wedge, growth engine, capital plan, and targets",
        line_2: "Modern analytics imagery with executive planning structure",
        chip_1: "Business",
        chip_2: "Plan",
      },
    },
    {
      id: "arcline_enterprise_sales_proposal",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/arcline-enterprise-sales-proposal.pdf",
      detailPageCount: 3,
      copy: {
        title: "Arcline Enterprise Sales Proposal",
        outcome: "Turn a complex service into a clear enterprise proposal covering client friction, solution, value, rollout, and governance.",
        prompt: "Remix the attached Arcline Enterprise Sales Proposal into a new three-page proposal for my prospect. Preserve the photographic product cover, white proposal panel, friction-to-control story, three-part value system, controlled rollout, governance gates, and premium enterprise tone while replacing all sample client details and imagery.",
        label: "Sales proposal",
        preview_title: "Arcline Enterprise Sales Proposal",
        line_1: "Client friction, proposed system, proof, and rollout",
        line_2: "Image-led enterprise proposal with a controlled delivery story",
        chip_1: "Sales",
        chip_2: "Proposal",
      },
    },
    {
      id: "luma_skincare_product_catalog",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/luma-skincare-product-catalog.pdf",
      detailPageCount: 3,
      copy: {
        title: "Luma Skincare Product Catalog",
        outcome: "Present a product collection through campaign photography, product descriptions, pricing, merchandising, and brand consistency.",
        prompt: "Remix the attached Luma Skincare Product Catalog into a new three-page catalog for my products. Preserve the square editorial format, full-width product photography, restrained serif hierarchy, collection metrics, product cards, pricing details, and merchandising page while replacing every sample product, claim, price, and image.",
        label: "Product catalog",
        preview_title: "Luma Skincare Product Catalog",
        line_1: "Collection story, products, pricing, and merchandising",
        line_2: "Premium product photography in a square retail lookbook",
        chip_1: "Product",
        chip_2: "Catalog",
      },
    },
    {
      id: "studio_kind_services_pricing",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/studio-kind-services-pricing-guide.pdf",
      detailPageCount: 3,
      copy: {
        title: "Studio Kind Services and Pricing",
        outcome: "Package professional services into clear engagement levels with positioning, scope, pricing, and next steps.",
        prompt: "Remix the attached Studio Kind Services and Pricing guide into a new three-page services guide for my business. Preserve the architectural hero photography, warm editorial palette, engagement ladder, scope descriptions, price hierarchy, timeline metrics, and next-step structure while replacing the sample offer and imagery.",
        label: "Services and pricing",
        preview_title: "Studio Kind Services and Pricing",
        line_1: "Positioning, engagement levels, scope, pricing, and timing",
        line_2: "Client-ready service guide with warm architectural imagery",
        chip_1: "Services",
        chip_2: "Pricing",
      },
    },
    {
      id: "relay_customer_case_study",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/relay-customer-case-study.pdf",
      detailPageCount: 3,
      copy: {
        title: "Relay Customer Case Study",
        outcome: "Show customer transformation through a concise before-change-result narrative, measurable outcomes, and implementation story.",
        prompt: "Remix the attached Relay Customer Case Study into a new three-page customer story. Preserve the dark technology cover, product imagery, before-change-result narrative, outcome metrics, customer quote, six-week implementation timeline, and credible enterprise tone while replacing the sample customer, evidence, and imagery.",
        label: "Customer case study",
        preview_title: "Relay Customer Case Study",
        line_1: "Customer challenge, implementation, proof, and results",
        line_2: "Dark technology case study with product-led evidence",
        chip_1: "Customer",
        chip_2: "Case study",
      },
    },
    {
      id: "earthline_annual_report",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/earthline-annual-report.pdf",
      detailPageCount: 3,
      copy: {
        title: "Earthline Annual Report",
        outcome: "Combine commercial performance, measurable impact, annual highlights, and future priorities in an accessible stakeholder report.",
        prompt: "Remix the attached Earthline Annual Report into a new three-page annual report for my organization. Preserve the full-bleed impact imagery, translucent report panel, commercial and impact metrics, year-in-review cards, outlook priorities, and evidence-led editorial voice while replacing all sample figures, claims, and imagery.",
        label: "Annual report",
        preview_title: "Earthline Annual Report",
        line_1: "Performance, impact, year highlights, and outlook",
        line_2: "Image-led stakeholder report connecting growth and outcomes",
        chip_1: "Annual",
        chip_2: "Report",
      },
    },
    {
      id: "sol_house_property_brochure",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/sol-house-property-brochure.pdf",
      detailPageCount: 3,
      copy: {
        title: "Sol House Property Brochure",
        outcome: "Market a property through cinematic photography, a concise residence story, key facts, materials, location, and lifestyle moments.",
        prompt: "Remix the attached Sol House Property Brochure into a new three-page property brochure. Preserve the cinematic full-bleed architecture cover, editorial listing card, residence facts, split image-and-copy page, materials and location story, lifestyle spread, and luxury restraint while replacing all sample property details and imagery.",
        label: "Property brochure",
        preview_title: "Sol House Property Brochure",
        line_1: "Residence story, facts, materials, location, and lifestyle",
        line_2: "Cinematic real-estate photography with premium editorial restraint",
        chip_1: "Property",
        chip_2: "Brochure",
      },
    },
    {
      id: "nightshift_festival_sponsorship",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/nightshift-festival-sponsorship-deck.pdf",
      detailPageCount: 3,
      copy: {
        title: "Nightshift Festival Sponsorship",
        outcome: "Sell an event partnership through high-energy photography, audience scale, sponsor tiers, activations, and measurable reach.",
        prompt: "Remix the attached Nightshift Festival Sponsorship deck into a new three-page sponsorship PDF for my event. Preserve the full-bleed night photography, neon identity, audience metrics, three partnership levels, activation plan, sponsor value, and campaign pacing while replacing every sample event, offer, figure, and image.",
        label: "Sponsorship deck",
        preview_title: "Nightshift Festival Sponsorship",
        line_1: "Audience reach, partnership levels, and activations",
        line_2: "High-energy event photography with a neon campaign system",
        chip_1: "Event",
        chip_2: "Sponsors",
      },
    },
    {
      id: "table_olive_menu_brand_book",
      preview: "pdf",
      sampleSrc: "/assets/samples/artifacts/pdf/table-olive-menu-brand-book.pdf",
      detailPageCount: 3,
      copy: {
        title: "Table Olive Menu and Brand Book",
        outcome: "Bring a restaurant concept together through food photography, menu structure, pricing, brand voice, colors, and materials.",
        prompt: "Remix the attached Table Olive Menu and Brand Book into a new three-page restaurant PDF. Preserve the food-led cover, narrow editorial format, seasonal course progression, pricing hierarchy, packaging photography, brand voice, color, material, and hospitality principles while replacing the sample restaurant, menu, prices, and imagery.",
        label: "Menu and brand book",
        preview_title: "Table Olive Menu and Brand Book",
        line_1: "Food story, seasonal menu, pricing, and brand ingredients",
        line_2: "Warm restaurant photography with a tactile brand system",
        chip_1: "Restaurant",
        chip_2: "Brand book",
      },
    },
  ],
  sheets: [
    {
      id: "atlas_product_roadmap",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/atlas-product-development.xlsx",
      detailPageCount: 2,
      copy: {
        title: "Atlas Product Development Roadmap",
        outcome: "Prioritize product initiatives from discovery through release with owners, effort, progress, and decision gates.",
        prompt: "Remix the Atlas Product Development Roadmap for a different product team. Preserve the formula-driven KPI cards, initiative list, stage chart, owners, effort, progress, due dates, editable Data sheet, validations, and conditional formatting; replace all sample inputs with a coherent English product roadmap.",
        label: "Product development",
        preview_title: "Atlas Product Development Roadmap",
        line_1: "Roadmap: discovery, design, build, validation, and release",
        line_2: "Control: owners, effort, progress, blockers, and due dates",
        chip_1: "Roadmap",
        chip_2: "Releases",
      },
    },
    {
      id: "orbit_people_planner",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/orbit-people-planner.xlsx",
      detailPageCount: 2,
      copy: {
        title: "Orbit People & Staffing Planner",
        outcome: "Balance team coverage, weekly capacity, leave, allocation, and hiring requirements.",
        prompt: "Remix the Orbit People & Staffing Planner for a different organization. Preserve the staffing KPI cards, allocation dashboard, team and status chart, capacity flags, editable Data sheet, dropdowns, and formula-driven weighted capacity; replace the fictional roles and people with a coherent English workforce plan.",
        label: "People planning",
        preview_title: "Orbit People & Staffing Planner",
        line_1: "People: roles, teams, weekly hours, leave, and hiring",
        line_2: "Capacity: allocation, coverage, and overload warnings",
        chip_1: "Staffing",
        chip_2: "Capacity",
      },
    },
    {
      id: "harbor_crm_pipeline",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/harbor-crm-pipeline.xlsx",
      detailPageCount: 2,
      copy: {
        title: "Harbor CRM & Sales Pipeline",
        outcome: "Track accounts, deal stages, next actions, weighted pipeline, and close confidence.",
        prompt: "Remix the Harbor CRM & Sales Pipeline for a different sales team. Preserve the pipeline KPI cards, stage chart, account list, owners, deal value, win probability, editable Data sheet, dropdowns, status warnings, and weighted-value formulas; replace all fictional accounts with a coherent English CRM pipeline.",
        label: "CRM",
        preview_title: "Harbor CRM & Sales Pipeline",
        line_1: "Pipeline: leads, qualification, proposals, negotiation, and wins",
        line_2: "Forecast: deal value, win probability, owners, and stalled deals",
        chip_1: "CRM",
        chip_2: "Pipeline",
      },
    },
    {
      id: "relay_project_portfolio",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/relay-project-portfolio.xlsx",
      detailPageCount: 2,
      copy: {
        title: "Relay Project Portfolio Command",
        outcome: "Review active projects, owners, budgets, milestones, completion, and portfolio risk.",
        prompt: "Remix the Relay Project Portfolio Command for a different leadership team. Preserve the executive KPI cards, health chart, project register, budgets, owners, completion bars, risk alerts, editable Data sheet, validations, and weighted-budget formulas; replace all project examples with a coherent English portfolio.",
        label: "Project management",
        preview_title: "Relay Project Portfolio Command",
        line_1: "Portfolio: projects, owners, budgets, milestones, and status",
        line_2: "Leadership: completion, risk signals, and priority decisions",
        chip_1: "Projects",
        chip_2: "Risks",
      },
    },
    {
      id: "northstar_saas_command_center",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/northstar-saas-command-center.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Northstar SaaS Revenue Command Center",
        outcome: "Analyze MRR, retention, expansion, churn, cohort decay, and revenue bridges in a dense finance cockpit.",
        prompt: "Remix the Northstar SaaS Revenue Command Center for another subscription business. Preserve the dark finance cockpit, formula-driven MRR and retention metrics, cohort heatmap, trend chart, and expansion/churn bridge; replace the sample inputs with coherent English SaaS data.",
        label: "SaaS analytics",
        preview_title: "Northstar SaaS Revenue Command Center",
        line_1: "Finance cockpit: MRR, ARR, NRR, churn, and expansion",
        line_2: "Cohorts: retention heatmap and recurring-revenue bridge",
        chip_1: "SaaS",
        chip_2: "Cohorts",
      },
    },
    {
      id: "casa_sol_hotel_planner",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/casa-sol-hotel-planner.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Casa Sol Hotel Front Desk Planner",
        outcome: "Run a month-long room allocation calendar with reservations, occupancy, ADR, guest names, and room types.",
        prompt: "Remix the Casa Sol Hotel Front Desk Planner for a different property and month. Preserve the room-by-day reservation grid, status colors, occupancy and ADR formulas, and compact front-desk layout; replace the sample rooms and guests with coherent English hotel data.",
        label: "Hospitality",
        preview_title: "Casa Sol Hotel Front Desk Planner",
        line_1: "Room calendar: arrivals, stays, status, and room type",
        line_2: "Front desk: occupancy, ADR, and collision-free allocation",
        chip_1: "Rooms",
        chip_2: "Occupancy",
      },
    },
    {
      id: "night_shift_film_control",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/night-shift-film-control.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Night Shift Film Production Control Book",
        outcome: "Schedule scenes, cast, locations, page counts, day/night requirements, and shoot dates on a production stripboard.",
        prompt: "Remix the Night Shift Film Production Control Book for a different production. Preserve the black-and-yellow stripboard, scene rows, shoot-date blocks, cast, locations, pages, and readiness status; replace the sample schedule with a coherent English production plan.",
        label: "Film production",
        preview_title: "Night Shift Film Production Control Book",
        line_1: "Stripboard: scenes, cast, locations, pages, and dates",
        line_2: "Production: prep, holds, ready state, and shoot sequence",
        chip_1: "Film",
        chip_2: "Stripboard",
      },
    },
    {
      id: "verdant_lab_notebook",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/verdant-lab-notebook.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Verdant Laboratory Experiment Notebook",
        outcome: "Document a hypothesis, independent variables, measured outputs, milestones, quality gates, and scientific sign-off.",
        prompt: "Remix the Verdant Laboratory Experiment Notebook for a different study. Preserve the portrait scientific-notebook layout, hypothesis panel, variable and output blocks, milestones, QC, and sign-off areas; replace the sample experiment with coherent English research content.",
        label: "Scientific research",
        preview_title: "Verdant Laboratory Experiment Notebook",
        line_1: "Study design: hypothesis, variables, outputs, and QC",
        line_2: "Governance: protocol milestones and dual sign-off",
        chip_1: "Lab",
        chip_2: "Protocol",
      },
    },
    {
      id: "ember_menu_engineering",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/ember-menu-engineering.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Ember Table Menu Engineering Studio",
        outcome: "Classify menu items by popularity and contribution margin using a four-quadrant engineering matrix.",
        prompt: "Remix the Ember Table Menu Engineering Studio for a different restaurant. Preserve the editorial menu layout, popularity-versus-margin scatter chart, and Stars, Plowhorses, Puzzles, and Dogs quadrants; replace the menu items with coherent English restaurant data.",
        label: "Restaurant operations",
        preview_title: "Ember Table Menu Engineering Studio",
        line_1: "Menu economics: popularity and contribution margin",
        line_2: "Matrix: Stars, Plowhorses, Puzzles, and Dogs",
        chip_1: "Menu",
        chip_2: "Margin",
      },
    },
    {
      id: "pulse_athlete_profile",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/pulse-athlete-profile.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Pulse Athlete Progress Profile",
        outcome: "Track an athlete's goal, personal records, compliance, pace, strength, recovery, and micro-trends.",
        prompt: "Remix the Pulse Athlete Progress Profile for a different athlete. Preserve the single-profile dark performance card, lime progress bars, recent PRs, goal, compliance, and micro-trends; replace the sample metrics with coherent English training data.",
        label: "Sports performance",
        preview_title: "Pulse Athlete Progress Profile",
        line_1: "Profile: goal, compliance, performance, and recovery",
        line_2: "Trends: pace, load, sleep, PRs, and progress bars",
        chip_1: "Athlete",
        chip_2: "Progress",
      },
    },
    {
      id: "forge_inventory_control",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/forge-inventory-control.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Forge Inventory Exception Control",
        outcome: "Prioritize SKUs by exposure, days of cover, aging band, owner, status, and replenishment action.",
        prompt: "Remix the Forge Inventory Exception Control workbook for a different warehouse. Preserve the exception-led inventory register, days-cover indicators, Pareto chart, exposure ranking, owners, and action rules; replace the sample SKUs with coherent English inventory data.",
        label: "Inventory control",
        preview_title: "Forge Inventory Exception Control",
        line_1: "Exceptions: exposure, cover, aging, owner, and action",
        line_2: "Pareto: rank stock exposure and focus replenishment",
        chip_1: "Inventory",
        chip_2: "Pareto",
      },
    },
    {
      id: "open_hands_grant_pipeline",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/open-hands-grant-pipeline.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Open Hands Grant Pipeline & Impact Story",
        outcome: "Manage grant prospects through drafting, submission, award, reporting, expected value, and weighted impact.",
        prompt: "Remix the Open Hands Grant Pipeline for a different nonprofit. Preserve the stage-based grant cards, expected and weighted amounts, reporting flow, and total weighted pipeline; replace the sample funders with coherent English grant opportunities.",
        label: "Nonprofit funding",
        preview_title: "Open Hands Grant Pipeline & Impact Story",
        line_1: "Funding: prospect, drafting, submitted, awarded, reporting",
        line_2: "Impact: expected value and weighted grant pipeline",
        chip_1: "Grants",
        chip_2: "Impact",
      },
    },
    {
      id: "ironclad_cost_risk",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/ironclad-cost-risk.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Ironclad Construction Cost & Risk Register",
        outcome: "Control planned cost, actual cost, forecast at completion, change orders, and cost-to-complete risk.",
        prompt: "Remix the Ironclad Construction Cost & Risk Register for a different project. Preserve the dark cost-control board, S-curve forecast, change-order chart, monthly plan-versus-actual table, and forecast-at-completion metric; replace the sample data with coherent English construction costs.",
        label: "Construction finance",
        preview_title: "Ironclad Construction Cost & Risk Register",
        line_1: "Cost control: plan, actual, forecast, and completion",
        line_2: "Risk: S-curve, change orders, and budget exposure",
        chip_1: "Construction",
        chip_2: "Cost risk",
      },
    },
    {
      id: "willow_rose_run_of_show",
      preview: "sheets",
      sampleSrc: "/assets/reviews/sheets-final/willow-rose-run-of-show.xlsx",
      detailPageCount: 1,
      copy: {
        title: "Willow & Rose Wedding Run-of-Show",
        outcome: "Coordinate the wedding-day timeline, moments, owners, locations, dependencies, and vendor handoffs.",
        prompt: "Remix the Willow & Rose Wedding Run-of-Show for a different event. Preserve the editorial timeline layout, large time hierarchy, moment, owner, location, and dependency columns; replace the sample schedule with a coherent English event run-of-show.",
        label: "Event planning",
        preview_title: "Willow & Rose Wedding Run-of-Show",
        line_1: "Timeline: moments, owners, locations, and dependencies",
        line_2: "Event control: handoffs from load-in through send-off",
        chip_1: "Event",
        chip_2: "Run-of-show",
      },
    },
  ],
  website: [
    {
      id: "heavyweight_brutalist_store",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/heavyweight-brutalist-store.html",
      detailPageCount: 1,
      copy: {
        title: "HEAVYWEIGHT Brutalist Store",
        outcome: "Create an industrial product storefront with oversized typography, asymmetric editorial layouts, and tactile interactions.",
        prompt: WEBSITE_TEMPLATE_PROMPTS.heavyweightBrutalistStore,
        label: "Website template",
        preview_title: "HEAVYWEIGHT Brutalist Store",
        line_1: "Brutalist commerce with asymmetric product storytelling",
        line_2: "Concrete, ink, grain, and grayscale-to-colour interactions",
        chip_1: "Commerce",
        chip_2: "Brutalist",
      },
    },
    {
      id: "luxury_ai_dark_interface",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/luxury-ai-dark-interface.html",
      detailPageCount: 1,
      copy: {
        title: "Aurelia Luxury AI Interface",
        outcome: "Present a premium AI product through cinematic dark surfaces, neural-network motion, and editorial gold accents.",
        prompt: WEBSITE_TEMPLATE_PROMPTS.luxuryAiDarkInterface,
        label: "Website template",
        preview_title: "Aurelia Luxury AI Interface",
        line_1: "Luxury dark-mode product narrative",
        line_2: "Neural visuals, glass layers, and editorial typography",
        chip_1: "AI",
        chip_2: "Luxury",
      },
    },
    {
      id: "neo_brutalist_saas",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/neo-brutalist-saas-landing.html",
      detailPageCount: 1,
      copy: {
        title: "BoltDesk Neo-Brutalist SaaS",
        outcome: "Launch a SaaS product with sharp hierarchy, hard shadows, expressive yellow, and physical button interactions.",
        prompt: WEBSITE_TEMPLATE_PROMPTS.neoBrutalistSaas,
        label: "Website template",
        preview_title: "BoltDesk Neo-Brutalist SaaS",
        line_1: "High-energy SaaS landing page",
        line_2: "Hard borders, offset shadows, and bold conversion sections",
        chip_1: "SaaS",
        chip_2: "Neo-brutalist",
      },
    },
    {
      id: "obsidian_lime_tubes",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/obsidian-lime-tubes.html",
      detailPageCount: 1,
      copy: {
        title: "Obsidian & Lime Interactive Tubes",
        outcome: "Showcase a future-facing creative product with interactive tubes, neon lime accents, and a floating glass shell.",
        prompt: WEBSITE_TEMPLATE_PROMPTS.obsidianLimeTubes,
        label: "Website template",
        preview_title: "Obsidian & Lime Interactive Tubes",
        line_1: "Interactive dark-mode creative showcase",
        line_2: "3D tubes, bento features, glass surfaces, and neon motion",
        chip_1: "Interactive",
        chip_2: "Futuristic",
      },
    },
    {
      id: "linux_bash_staff_test",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/linux-bash-staff-test.html",
      detailPageCount: 1,
      copy: {
        title: "Linux / Bash Staff-Level Test",
        outcome: "Turn advanced Linux and Bash concepts into a structured, interactive staff-level assessment experience.",
        prompt: WEBSITE_TEMPLATE_PROMPTS.linuxBashStaffTest,
        label: "Website template",
        preview_title: "Linux / Bash Staff-Level Test",
        line_1: "Technical interview and assessment interface",
        line_2: "Commands, permissions, scenarios, and scored learning states",
        chip_1: "Education",
        chip_2: "Developer",
      },
    },
    {
      id: "shoot_to_approval_motion",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/shoot-to-approval-motion-demo.html",
      detailPageCount: 1,
      copy: {
        title: "Shoot-to-Approval Motion Demo",
        outcome: "Pitch a workflow template product through a timed SaaS-style motion story from scattered feedback to final sign-off.",
        prompt: WEBSITE_TEMPLATE_PROMPTS.shootToApprovalMotion,
        label: "Website template",
        preview_title: "Shoot-to-Approval Motion Demo",
        line_1: "Animated product demo for solo videographers",
        line_2: "Revision tracking, approvals, usage rights, and preorder CTA",
        chip_1: "Motion",
        chip_2: "Product demo",
      },
    },
    {
      id: "solo_ai_ops_consultant",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/solo-ai-ops-consultant.html",
      detailPageCount: 1,
      copy: {
        title: "Solo AI Operations Consultant",
        outcome: "Sell a focused AI operations service with clear offers, proof metrics, process steps, FAQs, and a booking CTA.",
        prompt: WEBSITE_TEMPLATE_PROMPTS.soloAiOpsConsultant,
        label: "Website template",
        preview_title: "Solo AI Operations Consultant",
        line_1: "Dark editorial consulting landing page",
        line_2: "Services, proof, process, FAQ, and conversion CTA",
        chip_1: "Consulting",
        chip_2: "AI operations",
      },
    },
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
    {
      id: "digital_product_launch_site",
      preview: "website",
      sampleSrc: "/assets/samples/artifacts/website/digital-product-launch-site.html",
      detailPageCount: 1,
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
    {
      id: "product_campaign_visuals",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/arcline-product-campaign.png",
      sampleSrc: "/assets/samples/artifacts/image/arcline-product-campaign.png",
    },
    {
      id: "luxury_skincare_campaign",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/luxury-skincare-campaign.png",
      sampleSrc: "/assets/samples/artifacts/image/luxury-skincare-campaign.png",
      copy: {
        title: "Mineral Light Skincare Campaign",
        outcome: "Build a premium skincare launch visual with refined product styling.",
        prompt: "Remix this skincare campaign for my product. Keep the luminous mineral styling and premium studio lighting, then adapt the product form, materials, and palette to my brand.",
        label: "Image template",
        preview_title: "Mineral Light Skincare Campaign",
        line_1: "Luxury product still life",
        line_2: "Soft mineral light and water textures",
        chip_1: "Skincare",
        chip_2: "Campaign",
      },
    },
    {
      id: "seasonal_restaurant_campaign",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/seasonal-restaurant-campaign.png",
      sampleSrc: "/assets/samples/artifacts/image/seasonal-restaurant-campaign.png",
      copy: {
        title: "Hearth Seasonal Menu Campaign",
        outcome: "Create an appetizing editorial visual for a seasonal restaurant menu.",
        prompt: "Remix this food campaign for my restaurant. Preserve the warm bistro atmosphere and editorial plating, then replace the dishes, ceramics, and color accents for my menu.",
        label: "Image template",
        preview_title: "Hearth Seasonal Menu Campaign",
        line_1: "Editorial food photography",
        line_2: "Warm, ingredient-led restaurant story",
        chip_1: "Food",
        chip_2: "Restaurant",
      },
    },
    {
      id: "brutalist_fashion_editorial",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/brutalist-fashion-editorial.png",
      sampleSrc: "/assets/samples/artifacts/image/brutalist-fashion-editorial.png",
      copy: {
        title: "Concrete & Cobalt Fashion Editorial",
        outcome: "Produce a bold editorial fashion campaign with architectural contrast.",
        prompt: "Remix this fashion editorial for my collection. Retain the brutalist setting and cobalt color tension, then update the styling, silhouette, model direction, and mood.",
        label: "Image template",
        preview_title: "Concrete & Cobalt Fashion Editorial",
        line_1: "Architectural fashion portrait",
        line_2: "Graphic color and concrete geometry",
        chip_1: "Fashion",
        chip_2: "Editorial",
      },
    },
    {
      id: "modern_home_campaign",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/modern-home-campaign.png",
      sampleSrc: "/assets/samples/artifacts/image/modern-home-campaign.png",
      copy: {
        title: "Warm Modern Home Campaign",
        outcome: "Showcase residential architecture with warm cinematic realism.",
        prompt: "Remix this real estate visual for my property. Keep the blue-hour atmosphere and warm interior glow, then adapt the architecture, landscape, and camera framing.",
        label: "Image template",
        preview_title: "Warm Modern Home Campaign",
        line_1: "Cinematic property exterior",
        line_2: "Blue hour with welcoming interior light",
        chip_1: "Real estate",
        chip_2: "Architecture",
      },
    },
    {
      id: "fitness_wellness_campaign",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/fitness-wellness-campaign.png",
      sampleSrc: "/assets/samples/artifacts/image/fitness-wellness-campaign.png",
      copy: {
        title: "Everyday Strength Wellness Campaign",
        outcome: "Create an energetic, approachable visual for a fitness program.",
        prompt: "Remix this fitness campaign for my program. Preserve the natural movement and airy studio light, then update the athlete, exercise, equipment, and brand colors.",
        label: "Image template",
        preview_title: "Everyday Strength Wellness Campaign",
        line_1: "Authentic movement photography",
        line_2: "Bright studio wellness aesthetic",
        chip_1: "Fitness",
        chip_2: "Wellness",
      },
    },
    {
      id: "night_music_festival",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/night-music-festival.png",
      sampleSrc: "/assets/samples/artifacts/image/night-music-festival.png",
      copy: {
        title: "After Dark Music Festival",
        outcome: "Launch a high-energy music event with cinematic stage visuals.",
        prompt: "Remix this festival campaign for my event. Keep the monumental light architecture and crowd energy, then adapt the stage geometry, genre, palette, and atmosphere.",
        label: "Image template",
        preview_title: "After Dark Music Festival",
        line_1: "Immersive nighttime stage",
        line_2: "Magenta, cobalt, and amber energy",
        chip_1: "Music",
        chip_2: "Event",
      },
    },
    {
      id: "independent_podcast_cover",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/independent-podcast-cover.png",
      sampleSrc: "/assets/samples/artifacts/image/independent-podcast-cover.png",
      copy: {
        title: "Independent Podcast Studio",
        outcome: "Build a sophisticated visual identity for an independent podcast.",
        prompt: "Remix this podcast visual for my show. Preserve the intimate studio mood and tactile desk styling, then update the equipment, lighting, props, and color palette.",
        label: "Image template",
        preview_title: "Independent Podcast Studio",
        line_1: "Moody editorial studio still life",
        line_2: "Oxblood, charcoal, and warm brass",
        chip_1: "Podcast",
        chip_2: "Cover",
      },
    },
    {
      id: "coastal_travel_editorial",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/coastal-travel-editorial.png",
      sampleSrc: "/assets/samples/artifacts/image/coastal-travel-editorial.png",
      copy: {
        title: "Coastal Escape Travel Editorial",
        outcome: "Create an aspirational destination campaign with a strong sense of place.",
        prompt: "Remix this travel editorial for my destination. Keep the cinematic golden-hour depth and human scale, then replace the location, architecture, season, and traveler styling.",
        label: "Image template",
        preview_title: "Coastal Escape Travel Editorial",
        line_1: "Golden-hour destination photography",
        line_2: "Spacious coastal storytelling",
        chip_1: "Travel",
        chip_2: "Editorial",
      },
    },
    {
      id: "mobile_app_launch",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/mobile-app-launch.png",
      sampleSrc: "/assets/samples/artifacts/image/mobile-app-launch.png",
      copy: {
        title: "Mobile Product Launch",
        outcome: "Present a mobile product in a clean, premium launch visual.",
        prompt: "Remix this app launch for my product. Preserve the polished device presentation and floating interface system, then adapt the screens, feature hierarchy, palette, and device mix.",
        label: "Image template",
        preview_title: "Mobile Product Launch",
        line_1: "Premium device visualization",
        line_2: "Modular app interface highlights",
        chip_1: "App",
        chip_2: "Launch",
      },
    },
    {
      id: "saas_data_hero",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/saas-data-hero.png",
      sampleSrc: "/assets/samples/artifacts/image/saas-data-hero.png",
      copy: {
        title: "Modular Data SaaS Hero",
        outcome: "Explain a connected data platform through an elegant abstract system.",
        prompt: "Remix this SaaS hero for my platform. Keep the translucent modular data language, then update the visual modules, data relationships, palette, and emphasis for my product.",
        label: "Image template",
        preview_title: "Modular Data SaaS Hero",
        line_1: "Connected 3D data system",
        line_2: "Glass, cobalt, and coral modules",
        chip_1: "SaaS",
        chip_2: "Hero",
      },
    },
    {
      id: "botanical_book_cover",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/botanical-book-cover.png",
      sampleSrc: "/assets/samples/artifacts/image/botanical-book-cover.png",
      copy: {
        title: "Pressed Botanical Book Cover",
        outcome: "Design tactile literary artwork with a calm botanical character.",
        prompt: "Remix this book artwork for my story. Preserve the handmade paper and pressed botanical composition, then adapt the plants, palette, season, and emotional tone.",
        label: "Image template",
        preview_title: "Pressed Botanical Book Cover",
        line_1: "Tactile botanical collage",
        line_2: "Handmade paper and window shadows",
        chip_1: "Book",
        chip_2: "Cover",
      },
    },
    {
      id: "modern_wedding_stationery",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/modern-wedding-stationery.png",
      sampleSrc: "/assets/samples/artifacts/image/modern-wedding-stationery.png",
      copy: {
        title: "Modern Wedding Stationery",
        outcome: "Create a refined stationery scene ready for a custom invitation suite.",
        prompt: "Remix this wedding stationery for my event. Keep the premium flat lay and tactile materials, then update the paper, ribbon, flowers, seal, and color story.",
        label: "Image template",
        preview_title: "Modern Wedding Stationery",
        line_1: "Blank invitation suite mockup",
        line_2: "Ivory paper, silk, flowers, and brass",
        chip_1: "Wedding",
        chip_2: "Stationery",
      },
    },
    {
      id: "electric_vehicle_campaign",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/electric-vehicle-campaign.png",
      sampleSrc: "/assets/samples/artifacts/image/electric-vehicle-campaign.png",
      copy: {
        title: "Electric Horizon Vehicle Campaign",
        outcome: "Build a cinematic premium campaign for a modern vehicle.",
        prompt: "Remix this automotive campaign for my vehicle. Preserve the coastal blue-hour motion and architectural setting, then adapt the car form, location, color, and lighting.",
        label: "Image template",
        preview_title: "Electric Horizon Vehicle Campaign",
        line_1: "Cinematic automotive motion",
        line_2: "Coastal architecture at blue hour",
        chip_1: "Automotive",
        chip_2: "Campaign",
      },
    },
    {
      id: "pet_care_lifestyle",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/pet-care-lifestyle.png",
      sampleSrc: "/assets/samples/artifacts/image/pet-care-lifestyle.png",
      copy: {
        title: "Sunlit Pet Care Lifestyle",
        outcome: "Create a warm, trustworthy lifestyle campaign for a pet brand.",
        prompt: "Remix this pet care visual for my brand. Keep the natural home light and friendly interaction, then update the animals, interior, props, and brand palette.",
        label: "Image template",
        preview_title: "Sunlit Pet Care Lifestyle",
        line_1: "Authentic pet lifestyle photography",
        line_2: "Warm oak, linen, and sage interior",
        chip_1: "Pet care",
        chip_2: "Lifestyle",
      },
    },
    {
      id: "woodland_storybook",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/woodland-storybook.png",
      sampleSrc: "/assets/samples/artifacts/image/woodland-storybook.png",
      copy: {
        title: "Woodland Storybook Illustration",
        outcome: "Illustrate a warm, imaginative scene for a children's story.",
        prompt: "Remix this storybook scene for my story. Preserve the hand-painted wonder and gentle friendship, then adapt the characters, setting, season, and magical details.",
        label: "Image template",
        preview_title: "Woodland Storybook Illustration",
        line_1: "Watercolor woodland adventure",
        line_2: "Original child and animal characters",
        chip_1: "Storybook",
        chip_2: "Illustration",
      },
    },
    {
      id: "fantasy_game_key_art",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/fantasy-game-key-art.png",
      sampleSrc: "/assets/samples/artifacts/image/fantasy-game-key-art.png",
      copy: {
        title: "Frontier Fantasy Game Key Art",
        outcome: "Establish an expansive original world for a game or story campaign.",
        prompt: "Remix this fantasy key art for my world. Keep the dramatic scale and lone-explorer composition, then redesign the landscape, ruins, sky, character, and color atmosphere.",
        label: "Image template",
        preview_title: "Frontier Fantasy Game Key Art",
        line_1: "Cinematic world-building concept",
        line_2: "Floating islands, ruins, and distant moons",
        chip_1: "Game",
        chip_2: "Key art",
      },
    },
    {
      id: "artisan_packaging_system",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/artisan-packaging-system.png",
      sampleSrc: "/assets/samples/artifacts/image/artisan-packaging-system.png",
      copy: {
        title: "Artisan Packaging System",
        outcome: "Visualize a cohesive natural packaging family for a small-batch brand.",
        prompt: "Remix this packaging system for my products. Preserve the tactile natural materials and blank-label mockup format, then adapt the product mix, containers, colors, and ingredients.",
        label: "Image template",
        preview_title: "Artisan Packaging System",
        line_1: "Blank small-batch packaging mockups",
        line_2: "Paper, linen, ceramic, and botanicals",
        chip_1: "Packaging",
        chip_2: "Brand",
      },
    },
    {
      id: "founder_editorial_portrait",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/founder-editorial-portrait.png",
      sampleSrc: "/assets/samples/artifacts/image/founder-editorial-portrait.png",
      copy: {
        title: "Creative Founder Editorial Portrait",
        outcome: "Create an authentic editorial portrait for a founder or creative leader.",
        prompt: "Remix this founder portrait for my profile. Keep the natural confidence, side light, and working-studio context, then adapt the person, profession, materials, and palette.",
        label: "Image template",
        preview_title: "Creative Founder Editorial Portrait",
        line_1: "Authentic magazine-style portrait",
        line_2: "Warm design studio environment",
        chip_1: "Portrait",
        chip_2: "Founder",
      },
    },
    {
      id: "winter_gift_campaign",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/winter-gift-campaign.png",
      sampleSrc: "/assets/samples/artifacts/image/winter-gift-campaign.png",
      copy: {
        title: "Winter Gift Campaign",
        outcome: "Create a rich seasonal product visual for holiday ecommerce.",
        prompt: "Remix this winter gift campaign for my products. Preserve the premium still life and atmospheric candlelight, then adapt the packaging, ribbon, foliage, palette, and product arrangement.",
        label: "Image template",
        preview_title: "Winter Gift Campaign",
        line_1: "Premium seasonal gift still life",
        line_2: "Pine, burgundy, ivory, and brass",
        chip_1: "Holiday",
        chip_2: "Ecommerce",
      },
    },
    {
      id: "climate_science_visual",
      preview: "image",
      imageSrc: "/assets/samples/artifacts/image/climate-science-visual.png",
      sampleSrc: "/assets/samples/artifacts/image/climate-science-visual.png",
      copy: {
        title: "Living Climate Science Visual",
        outcome: "Explain a complex environmental system with an accessible 3D visual.",
        prompt: "Remix this science visual for my topic. Keep the clear isometric system view and connected flows, then adapt the ecosystem, processes, energy sources, species, and color coding.",
        label: "Image template",
        preview_title: "Living Climate Science Visual",
        line_1: "Isometric environmental system model",
        line_2: "Watershed, coast, climate, and energy flows",
        chip_1: "Science",
        chip_2: "Education",
      },
    },
  ],
  video: [
    {
      id: "aurora-app-launch",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/aurora-app-launch.mp4",
      copy: {
        title: "Aurora Mobile App Launch",
        outcome: "Launch a mobile product with a polished vertical interface film.",
        prompt: "Remix this vertical mobile app launch template for my product. Preserve the glassy interface reveal, focus-score animation, neon depth, and concise product-story pacing while replacing every brand, message, metric, and UI detail.",
        label: "Code-generated video template",
        preview_title: "Aurora Mobile App Launch",
        line_1: "Vertical glass UI product reveal",
        line_2: "App launch · interface motion · 9:16",
        chip_1: "Product",
        chip_2: "Mobile",
      },
    },
    {
      id: "morrow-fragrance-film",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/morrow-fragrance-film.mp4",
      copy: {
        title: "Morrow Fragrance Product Film",
        outcome: "Introduce a premium product through a restrained editorial film.",
        prompt: "Remix this luxury product film for my product. Keep the editorial serif hierarchy, hero-object choreography, soft material light, ingredient notes, and slow premium cadence while replacing the sample brand, product, claims, palette, and object design.",
        label: "Code-generated video template",
        preview_title: "Morrow Fragrance Product Film",
        line_1: "Luxury editorial product choreography",
        line_2: "Beauty · fragrance · premium goods",
        chip_1: "Luxury",
        chip_2: "Product",
      },
    },
    {
      id: "gridline-conference",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/gridline-conference.mp4",
      copy: {
        title: "Gridline Conference Poster",
        outcome: "Promote an event with a bold square motion poster.",
        prompt: "Remix this Swiss-inspired motion poster for my event. Preserve the strict grid, oversized date typography, red-blue geometry, decisive kinetic entrances, and square social format while replacing the event identity, dates, location, theme, and visual accents.",
        label: "Code-generated video template",
        preview_title: "Gridline Conference Poster",
        line_1: "Swiss grid motion poster",
        line_2: "Conference · culture · square social",
        chip_1: "Event",
        chip_2: "Poster",
      },
    },
    {
      id: "fieldnote-data-dispatch",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/fieldnote-data-dispatch.mp4",
      copy: {
        title: "Fieldnote Data Dispatch",
        outcome: "Turn a research finding into a sharp editorial data story.",
        prompt: "Remix this editorial data dispatch for my findings. Keep the newspaper masthead, decision signal, animated metrics, compact bar chart, warm paper system, and evidence-first cadence while replacing all research claims, values, labels, and attribution.",
        label: "Code-generated video template",
        preview_title: "Fieldnote Data Dispatch",
        line_1: "Editorial research and data briefing",
        line_2: "Metrics · chart · decision signal",
        chip_1: "Data",
        chip_2: "Research",
      },
    },
    {
      id: "casa-sombra-property",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/casa-sombra-property.mp4",
      copy: {
        title: "Casa Sombra Property Film",
        outcome: "Present a property through an architectural plan-led narrative.",
        prompt: "Remix this architectural property film for my space. Preserve the quiet material palette, editorial serif title, animated floor plan, moving daylight study, and measured facts while replacing the property, rooms, measurements, studio, location, and architectural story.",
        label: "Code-generated video template",
        preview_title: "Casa Sombra Property Film",
        line_1: "Architectural plan and daylight story",
        line_2: "Property · interiors · real estate",
        chip_1: "Property",
        chip_2: "Design",
      },
    },
    {
      id: "pulse-fm-visualizer",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/pulse-fm-visualizer.mp4",
      copy: {
        title: "Pulse FM Music Visualizer",
        outcome: "Package a music release as a vivid square visualizer.",
        prompt: "Remix this neon radio visualizer for my track. Preserve the circular broadcast system, kinetic waveform, club-light palette, live badge, track metadata, and continuous rhythmic motion while replacing the station, artist, track, timing, colors, and release identity.",
        label: "Code-generated video template",
        preview_title: "Pulse FM Music Visualizer",
        line_1: "Neon radio waveform system",
        line_2: "Music · release · square social",
        chip_1: "Music",
        chip_2: "Visualizer",
      },
    },
    {
      id: "orbit-science-explainer",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/orbit-science-explainer.mp4",
      copy: {
        title: "Orbit Science Explainer",
        outcome: "Explain a science concept with clear playful motion graphics.",
        prompt: "Remix this visual science explainer for my concept. Keep the approachable diagram system, numbered learning steps, labeled moving model, annotation card, and classroom-friendly pacing while replacing the subject, mechanism, terms, colors, and teaching sequence.",
        label: "Code-generated video template",
        preview_title: "Orbit Science Explainer",
        line_1: "Animated learning model and steps",
        line_2: "Science · education · explainer",
        chip_1: "Science",
        chip_2: "Learning",
      },
    },
    {
      id: "unscripted-podcast-quote",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/unscripted-podcast-quote.mp4",
      copy: {
        title: "Unscripted Podcast Quote",
        outcome: "Promote an episode with a vertical speaker quote card.",
        prompt: "Remix this vertical podcast quote template for my episode. Preserve the speaker portrait stage, high-contrast quote reveal, lime identity strip, animated waveform, episode metadata, and social-first pacing while replacing the show, guest, quote, colors, and supporting copy.",
        label: "Code-generated video template",
        preview_title: "Unscripted Podcast Quote",
        line_1: "Vertical speaker quote and waveform",
        line_2: "Podcast · interview · 9:16",
        chip_1: "Podcast",
        chip_2: "Social",
      },
    },
    {
      id: "northline-coffee-story",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/northline-coffee-story.mp4",
      copy: {
        title: "Northline Coffee Origin Story",
        outcome: "Tell a tactile product-origin story through animated collage.",
        prompt: "Remix this tactile editorial collage for my product story. Preserve the paper texture, layered photo-card grammar, handcrafted stamp, hero object reveal, serif narrative voice, and warm slow pacing while replacing the product, origin, materials, claims, and identity.",
        label: "Code-generated video template",
        preview_title: "Northline Coffee Origin Story",
        line_1: "Tactile editorial product collage",
        line_2: "Food · craft · origin story",
        chip_1: "Story",
        chip_2: "Craft",
      },
    },
    {
      id: "common-ground-impact",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/common-ground-impact.mp4",
      copy: {
        title: "Common Ground Impact Story",
        outcome: "Show community impact through a warm network narrative.",
        prompt: "Remix this nonprofit impact story for my organization. Keep the split editorial canvas, animated partner network, outcome counter, human quote, and clear action prompt while replacing the cause, organization, participants, outcome data, message, and palette.",
        label: "Code-generated video template",
        preview_title: "Common Ground Impact Story",
        line_1: "Community network and outcome story",
        line_2: "Nonprofit · impact · campaign",
        chip_1: "Impact",
        chip_2: "Community",
      },
    },
    {
      id: "classic-cinema-parody",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/classic-cinema-parody.mp4",
      copy: {
        title: "Classic Cinema Parody",
        outcome: "Recast an original dialogue scene as a dramatic black-and-white period film.",
        prompt: "Remix this classic-cinema dialogue template for my story. Preserve the high-contrast black-and-white photography, period wardrobe and setting, expressive close-ups, selective color accent, cinematic shot-reverse-shot rhythm, and synchronized dialogue while replacing the characters, script, location, props, and narrative.",
        label: "Video template",
        preview_title: "Classic Cinema Parody",
        line_1: "Black-and-white dramatic dialogue",
        line_2: "Period cinema · close-ups · 16:9",
        chip_1: "Cinema",
        chip_2: "Parody",
      },
    },
    {
      id: "liubang-marketplace-drama",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/liubang-marketplace-drama.mp4",
      copy: {
        title: "Historical Marketplace Drama",
        outcome: "Stage a cinematic historical marketplace exchange with synchronized character dialogue.",
        prompt: "Remix this historical marketplace drama for my scene. Preserve the cinematic ancient-market setting, strong hero framing, expressive character performance, period costumes, background crowd, grounded props, and synchronized spoken dialogue while replacing the characters, script, location, objects, and dramatic conflict.",
        label: "Video template",
        preview_title: "Historical Marketplace Drama",
        line_1: "Cinematic period character exchange",
        line_2: "Marketplace · dialogue · 16:9",
        chip_1: "Historical",
        chip_2: "Dialogue",
      },
    },
    {
      id: "stickman-reset-retry",
      preview: "video",
      sampleSrc: "/assets/samples/artifacts/video/templates/stickman-reset-retry.mp4",
      copy: {
        title: "Stickman Reset & Retry Explainer",
        outcome: "Explain a reset-and-retry workflow with a simple hand-drawn character sequence.",
        prompt: "Remix this hand-drawn workflow explainer for my process. Preserve the clean monochrome line art, simple stick-character acting, labeled blocks, visible reset prop, spacious white background, and easy-to-follow motion while replacing the labels, process steps, objects, character actions, and teaching message.",
        label: "Video template",
        preview_title: "Stickman Reset & Retry Explainer",
        line_1: "Hand-drawn workflow animation",
        line_2: "Reset · retry · square explainer",
        chip_1: "Explainer",
        chip_2: "Workflow",
      },
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
    {
      id: "market_opportunity_map",
      preview: "research",
      sampleSrc: "/assets/samples/artifacts/research/market-opportunity-map.html",
      detailPageCount: 1,
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
    {
      id: "customer_success_team",
      preview: "agents",
      sampleSrc: "/assets/samples/artifacts/agents/customer-success-agent-team.html",
      detailPageCount: 1,
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
    {
      id: "lead_nurture_system",
      preview: "automations",
      sampleSrc: "/assets/samples/artifacts/automations/lead-nurture-system.html",
      detailPageCount: 1,
    },
  ],
};

function createSideHustleSample(
  capability: WorkspaceCapability,
  {
    id,
    preview,
    coverTemplate,
    imageSrc,
    sampleSrc,
    videoSrc: explicitVideoSrc,
    detailPageCount = 3,
    chatModePayloadPatch,
    copy,
  }: SideHustleSampleDefinition,
): WorkspaceCapabilityConfig["samples"][number] {
  const key = `${capability}.${id}`;
  const sampleText = (field: keyof NonNullable<SideHustleSampleDefinition["copy"]>) =>
    copy?.[field] ?? sideHustleText(`${key}.${field}`);
  const videoSrc =
    explicitVideoSrc ||
    (capability === "video" && sampleSrc?.match(/\.(mp4|webm|mov|m4v)$/i)
      ? sampleSrc
      : undefined);
  const detailImageSrcs = Array.from({ length: detailPageCount }, (_, index) => index + 1).map(
    (page) =>
      `/assets/samples/details/${capability}/${id}-${String(page).padStart(2, "0")}.png`,
  );
  const previewContent: WorkspaceSamplePreviewContent = coverTemplate
    ? {
        previewImageSrc: blueprintOilPaintingCoverUrl(coverTemplate)
          ?? blueprintCoverDataUrl(coverTemplate, "white"),
        previewImageDarkSrc: blueprintOilPaintingCoverUrl(coverTemplate)
          ?? blueprintCoverDataUrl(coverTemplate, "dark"),
        previewImageAlt: sampleText("preview_title"),
        marketplaceCover: true,
      }
    : imageSrc
    ? {
        imageSrc,
        imageAlt: sideHustleText(`${key}.image_alt`),
        imageCaption: sideHustleText(`${key}.image_caption`),
        sampleSrc,
        sampleLabel: sideHustleText(`${capability}.sample_asset_label`),
        videoSrc,
      }
    : {
        label: sampleText("label"),
        title: sampleText("preview_title"),
        previewImageSrc:
          capability === "docs"
            ? detailImageSrcs[0]
            : `/assets/samples/previews/${capability}/${id}.png`,
        previewImageAlt: sampleText("preview_title"),
        detailImageSrcs,
        detailImageAlt: sampleText("preview_title"),
        lines: [
          sampleText("line_1"),
          sampleText("line_2"),
        ],
        chips: [
          sampleText("chip_1"),
          sampleText("chip_2"),
        ],
        sampleSrc,
        sampleLabel: sideHustleText(`${capability}.sample_asset_label`),
        videoSrc,
      };

  return {
    title: sampleText("title"),
    outcome: sampleText("outcome"),
    prompt: sampleText("prompt"),
    preview,
    previewContent,
    chatModePayloadPatch,
  };
}

const SIDE_HUSTLE_SAMPLES: Record<
  Exclude<WorkspaceCapability, "flows">,
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
  pdf: SIDE_HUSTLE_SAMPLE_DEFINITIONS.pdf.map((definition) =>
    createSideHustleSample("pdf", definition),
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
  Exclude<WorkspaceCapability, "flows">,
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
  pdf: {
    description: sideHustleText("pdf.description"),
    placeholder: sideHustleText("pdf.placeholder"),
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
    if (capability.key === "flows") {
      return {
        ...capability,
        templates: capability.samples.map(({ title, prompt }) => ({
          label: title,
          prompt,
        })),
      };
    }
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

const CHAT_MODE_TEMPLATE_CAPABILITY: Partial<
  Record<ChatBoxMode, Exclude<WorkspaceCapability, "flows">>
> = {
  document: "docs",
  pdf: "pdf",
  slides: "slides",
  sheet: "sheets",
  website: "website",
  image: "image",
  video: "video",
  research: "research",
};

export function chatModeTemplateSamples(
  mode: ChatBoxMode,
): ChatModeTemplateSample[] {
  const capability = CHAT_MODE_TEMPLATE_CAPABILITY[mode];
  return capability ? SIDE_HUSTLE_SAMPLES[capability] : [];
}

interface ProgressItem {
  id: string;
  title: string;
  detail?: string;
  status: ExecutionStatus;
}

export interface OutputArtifact {
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
  return inferArtifactKindFromPath(refItem.openUrl || refItem.fsPath || refItem.url || refItem.name);
}

function artifactFromMessageReference(
  refItem: ChatMessageDisplayReference,
  doc?: Document | null,
): OutputArtifact {
  const documentId = doc?.id || refItem.document_id || undefined;
  const title = doc?.name || refItem.name;
  const directUrl = generatedFileOpenReference({
    document_id: documentId,
    open_url: refItem.openUrl,
    result_url: refItem.url || refItem.previewUrl,
    fs_path: refItem.fsPath,
  }) || undefined;
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
          key: `attachment-${attachment.document_id || attachment.name}-${index}-${count}`,
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
        <CollapsibleSentMessage text={contentText}>
          <div className="chat-user-rich-text">
            {parts.map((part) => {
              if (part.kind === "text")
                return <span key={part.key}>{part.text}</span>;
              if (part.kind === "mention") {
                return (
                  <span
                    key={part.key}
                    className={`chat-message-inline-token chat-message-inline-token--mention chat-message-inline-token--${part.mention.type}`}
                  >
                    <span className="chat-message-inline-mention-prefix">@</span>
                    <span className="chat-message-inline-avatar">
                      <UserAvatar
                        name={part.mention.name}
                        avatarUrl={part.mention.avatarUrl}
                        type={part.mention.type}
                        seed={part.mention.avatarSeed || part.mention.id}
                        size={18}
                      />
                    </span>
                    <span className="chat-message-inline-main">
                      <strong>{part.mention.name}</strong>
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
        </CollapsibleSentMessage>
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
  imageSrc: "/assets/samples/coffee-brand-visual.jpg",
  imageAlt: "Real image sample",
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
        className={`workspace-sample-preview workspace-sample-preview--${kind} ${previewContent.marketplaceCover ? "workspace-sample-preview--marketplace-cover" : ""} ${hasBrowseDetails ? "workspace-sample-preview--browseable" : ""}`}
        {...browseHandlers}
      >
        <figure className="preview-image-art preview-sample-artifact">
          <ThemeAwareImage
            src={activeDetailSrc || previewContent.previewImageSrc}
            darkSrc={activeDetailSrc ? undefined : previewContent.previewImageDarkSrc}
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
      {kind === "flows" && (
        <div className="preview-flow-route">
          <i className="preview-flow-route-line preview-flow-route-line--a" />
          <i className="preview-flow-route-line preview-flow-route-line--b" />
          <i className="preview-flow-route-line preview-flow-route-line--c" />
          <i className="preview-flow-route-line preview-flow-route-line--d" />
          <span className="preview-flow-route-node preview-flow-route-node--trigger" />
          <span className="preview-flow-route-node preview-flow-route-node--branch-a" />
          <span className="preview-flow-route-node preview-flow-route-node--branch-b" />
          <span className="preview-flow-route-node preview-flow-route-node--output" />
        </div>
      )}
    </div>
  );
}

function WorkspaceSampleQuickPreview({
  sample,
  onClose,
  onRemix,
  remixing,
  remixDisabled,
  blueprint,
  blueprintStatus,
  installedWorkspace,
  installationStatus,
  onInstall,
  onOpenInstalled,
}: {
  sample: WorkspaceSample | null;
  onClose: () => void;
  onRemix: (sample: WorkspaceSample) => Promise<boolean>;
  remixing: boolean;
  remixDisabled: boolean;
  blueprint: BlueprintDetail | null;
  blueprintStatus: "idle" | "loading" | "error";
  installedWorkspace: Workspace | null;
  installationStatus: "idle" | "loading" | "error";
  onInstall: (blueprint: BlueprintDetail) => void;
  onOpenInstalled: (workspace: Workspace) => void;
}) {
  const [activePage, setActivePage] = useState(0);
  const content = sample?.previewContent;
  const pages = content?.detailImageSrcs || [];
  const activeImage =
    pages[activePage] || content?.previewImageSrc || content?.imageSrc;
  const activeDarkImage = pages[activePage]
    ? undefined
    : content?.previewImageDarkSrc;
  const openSampleLabel = content?.sampleSrc?.match(/\.(ppt|pptx)(?:$|[?#])/i)
    ? t("component.embedded_chat.open_presentation")
    : content?.sampleSrc?.match(/\.docx?(?:$|[?#])/i)
      ? t("component.embedded_chat.open_document")
      : content?.sampleSrc?.match(/\.pdf(?:$|[?#])/i)
        ? t("component.embedded_chat.open_pdf")
        : t("component.embedded_chat.open_artifact");
  const setupPreview = blueprint?.setup_preview;
  const setupItems = setupPreview
    ? [
        ...setupPreview.required_variables,
        ...setupPreview.optional_variables,
        ...setupPreview.required_channels,
        ...setupPreview.optional_channels,
        ...setupPreview.required_sessions,
        ...setupPreview.optional_sessions,
      ]
    : [];
  const previewHighlights = setupPreview
    ? [
        ...setupPreview.first_week_outputs,
        ...setupPreview.services.map((service) => service.purpose || service.label),
      ]
        .filter((item, index, items) => item && items.indexOf(item) === index)
        .slice(0, 4)
    : [];
  const blueprintDescription = blueprint?.description?.trim();
  const blueprintSummary = blueprint?.summary?.trim() || sample?.outcome;
  const isSpreadsheetPreview = sample?.preview === "sheets";
  const isInstallableWorkspace = Boolean(
  );
  const renderPageControls = () =>
    pages.length > 1 ? (
      <div
        className="workspace-sample-quick-preview-pages"
        aria-label={t("component.embedded_chat.preview_pages")}
      >
        <button
          type="button"
          disabled={activePage === 0}
          aria-label={t("component.embedded_chat.previous_preview_page")}
          onClick={() => setActivePage((page) => Math.max(0, page - 1))}
        >
          {"<"}
        </button>
        <span>
          {activePage + 1} / {pages.length}
        </span>
        <button
          type="button"
          disabled={activePage === pages.length - 1}
          aria-label={t("component.embedded_chat.next_preview_page")}
          onClick={() =>
            setActivePage((page) => Math.min(pages.length - 1, page + 1))
          }
        >
          {">"}
        </button>
      </div>
    ) : null;

  useEffect(() => {
    setActivePage(0);
  }, [sample]);

  return (
    <Modal
      open={Boolean(sample)}
      onClose={onClose}
      title={sample ? `${t("component.embedded_chat.quick_preview")} · ${sample.title}` : t("component.embedded_chat.quick_preview")}
      className={`workspace-sample-quick-preview-modal${isSpreadsheetPreview ? " workspace-sheet-quick-preview-modal" : ""}`}
      bodyClassName="workspace-sample-quick-preview-body"
      maxWidth="980px"
      footer={
        sample ? (
          <div className="workspace-sample-quick-preview-footer">
            {content?.sampleSrc && (
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  window.open(content.sampleSrc, "_blank", "noopener,noreferrer")
                }
              >
                <IconDownload size={14} />
                {openSampleLabel}
              </Button>
            )}
            {!isInstallableWorkspace && (
              <Button
                size="sm"
                variant="outline"
                loading={remixing}
                disabled={remixDisabled}
                onClick={() => {
                  void onRemix(sample).then((didRemix) => {
                    if (didRemix) onClose();
                  });
                }}
              >
                <IconSparkles size={14} />
                {t("component.embedded_chat.remix")}
              </Button>
            )}
            {isInstallableWorkspace && installedWorkspace ? (
              <Button
                size="sm"
                variant="primary"
                onClick={() => onOpenInstalled(installedWorkspace)}
              >
                <IconCheckCircle size={14} />
                {t("component.embedded_chat.open_installed_workspace")}
              </Button>
            ) : isInstallableWorkspace && blueprint ? (
              <Button
                size="sm"
                variant="primary"
                loading={installationStatus === "loading"}
                disabled={installationStatus === "loading"}
                onClick={() => onInstall(blueprint)}
              >
                {t("component.embedded_chat.install_workspace")}
              </Button>
            ) : isInstallableWorkspace ? (
              <Button
                size="sm"
                variant="primary"
                loading={blueprintStatus === "loading"}
                disabled
              >
                {t("component.embedded_chat.install_workspace")}
              </Button>
            ) : null}
          </div>
        ) : null
      }
    >
      {sample && activeImage && (
        <div
          className={`workspace-sample-quick-preview workspace-blueprint-quick-preview${isSpreadsheetPreview ? " workspace-sheet-quick-preview" : ""}`}
        >
          <figure>
            {content?.videoSrc ? (
              <video
                src={content.videoSrc}
                poster={activeImage}
                controls
                playsInline
                preload="metadata"
                aria-label={content?.previewImageAlt || sample.title}
              />
            ) : (
              <ThemeAwareImage
                src={activeImage}
                darkSrc={activeDarkImage}
                alt={content?.detailImageAlt || content?.previewImageAlt || sample.title}
              />
            )}
          </figure>
          {isSpreadsheetPreview ? (
            <div className="workspace-sheet-quick-preview-navigation">
              {renderPageControls()}
            </div>
          ) : (
          <aside className="workspace-blueprint-preview-details">
            <span className="workspace-blueprint-preview-eyebrow">
              {t("component.embedded_chat.marketplace_workspace")}
            </span>

            <section className="workspace-blueprint-preview-section">
              <h3>{t("page.blueprint_detail.what_this_workspace_does")}</h3>
              <p className="workspace-blueprint-preview-summary">
                {blueprintSummary}
              </p>
              {blueprintDescription && blueprintDescription !== blueprintSummary && (
                <p className="workspace-blueprint-preview-description">
                  {blueprintDescription}
                </p>
              )}
            </section>

            {blueprintStatus === "loading" && (
              <div className="workspace-blueprint-preview-loading" role="status">
                <LoadingSpinner size={14} />
                {t("component.embedded_chat.loading_workspace_details")}
              </div>
            )}

            {blueprintStatus === "error" && (
              <div
                className="workspace-blueprint-preview-loading workspace-blueprint-preview-loading--error"
                role="status"
              >
                {t("component.embedded_chat.workspace_details_unavailable")}
              </div>
            )}

            {blueprint && (
              <>
                <div
                  className="workspace-blueprint-preview-facts"
                  aria-label={t("component.embedded_chat.workspace_contents")}
                >
                  <span>
                    <strong>{setupPreview?.services.length || 0}</strong>
                    {t("page.blueprint_detail.service_loop_count")}
                  </span>
                  <span>
                    <strong>{setupPreview?.first_week_outputs.length || 0}</strong>
                    {t("page.blueprint_detail.first_week_outputs")}
                  </span>
                  <span>
                    <strong>{setupItems.length}</strong>
                    {t("component.embedded_chat.setup_items")}
                  </span>
                </div>

                {previewHighlights.length > 0 && (
                  <section className="workspace-blueprint-preview-section workspace-blueprint-preview-includes">
                    <h3>{t("component.embedded_chat.workspace_contents")}</h3>
                    <ul>
                      {previewHighlights.map((highlight) => (
                        <li key={highlight}>
                          <IconCheckCircle size={14} />
                          <span>{highlight}</span>
                        </li>
                      ))}
                    </ul>
                  </section>
                )}
              </>
            )}

            <div className="workspace-sample-quick-preview-meta">
              <div className="workspace-sample-quick-preview-copy">
                {isInstallableWorkspace && installationStatus === "loading" && (
                  <span className="workspace-sample-installation-status" role="status">
                    <LoadingSpinner size={12} />
                    {t("component.embedded_chat.checking_installation")}
                  </span>
                )}
                {isInstallableWorkspace && installationStatus === "error" && (
                  <span className="workspace-sample-installation-status workspace-sample-installation-status--error">
                    {t("component.embedded_chat.installation_status_unavailable")}
                  </span>
                )}
                {installedWorkspace && (
                  <span className="workspace-sample-installation-status workspace-sample-installation-status--installed">
                    <IconCheckCircle size={13} />
                    {t("component.embedded_chat.installed_as").replace(
                      "{name}",
                      installedWorkspace.name,
                    )}
                  </span>
                )}
              </div>
              {renderPageControls()}
            </div>
          </aside>
          )}
        </div>
      )}
    </Modal>
  );
}

const FEATURED_FLOW_TEMPLATE_KEYS = [
  "opc-generate-topic-from-knowledge-v1",
  "opc-write-article-from-topic-v1",
  "opc-create-image-from-topic-v1",
  "opc-create-video-from-topic-v1",
] as const;

type FlowTemplatePreviewNodeKind =
  | "knowledge"
  | "tool"
  | "agent"
  | "approval"
  | "condition"
  | "image"
  | "video"
  | "end";

type FlowTemplatePreviewNode = {
  id: string;
  label: string;
  kind: FlowTemplatePreviewNodeKind;
  x: number;
  y: number;
};

type FlowTemplatePreviewEdge = {
  from: string;
  to: string;
  branch?: "approved" | "changes";
};

type FlowTemplatePreviewGraph = {
  nodes: FlowTemplatePreviewNode[];
  edges: FlowTemplatePreviewEdge[];
};

const FLOW_TEMPLATE_PREVIEW_NODE_WIDTH = 126;
const FLOW_TEMPLATE_PREVIEW_NODE_HEIGHT = 54;

const FLOW_TEMPLATE_PREVIEW_GRAPHS: Record<string, FlowTemplatePreviewGraph> = {
  "opc-generate-topic-from-knowledge-v1": {
    nodes: [
      { id: "knowledge", label: "Workspace knowledge", kind: "knowledge", x: 28, y: 118 },
      { id: "signals", label: "Public signals", kind: "tool", x: 178, y: 118 },
      { id: "draft", label: "Draft topic brief", kind: "agent", x: 328, y: 118 },
      { id: "review", label: "Approve topic", kind: "approval", x: 478, y: 118 },
      { id: "gate", label: "Topic approved?", kind: "condition", x: 628, y: 118 },
      { id: "approved", label: "Approved topic", kind: "end", x: 818, y: 54 },
      { id: "changes", label: "Revise or cancel", kind: "end", x: 818, y: 184 },
    ],
    edges: [
      { from: "knowledge", to: "signals" },
      { from: "signals", to: "draft" },
      { from: "draft", to: "review" },
      { from: "review", to: "gate" },
      { from: "gate", to: "approved", branch: "approved" },
      { from: "gate", to: "changes", branch: "changes" },
    ],
  },
  "opc-write-article-from-topic-v1": {
    nodes: [
      { id: "knowledge", label: "Article evidence", kind: "knowledge", x: 82, y: 118 },
      { id: "draft", label: "Draft article", kind: "agent", x: 262, y: 118 },
      { id: "review", label: "Editorial review", kind: "approval", x: 442, y: 118 },
      { id: "gate", label: "Article approved?", kind: "condition", x: 622, y: 118 },
      { id: "approved", label: "Approved article", kind: "end", x: 818, y: 54 },
      { id: "changes", label: "Revise or cancel", kind: "end", x: 818, y: 184 },
    ],
    edges: [
      { from: "knowledge", to: "draft" },
      { from: "draft", to: "review" },
      { from: "review", to: "gate" },
      { from: "gate", to: "approved", branch: "approved" },
      { from: "gate", to: "changes", branch: "changes" },
    ],
  },
  "opc-create-image-from-topic-v1": {
    nodes: [
      { id: "knowledge", label: "Visual knowledge", kind: "knowledge", x: 10, y: 54 },
      { id: "draft", label: "Draft image brief", kind: "agent", x: 150, y: 54 },
      { id: "brief_review", label: "Review brief", kind: "approval", x: 290, y: 54 },
      { id: "brief_gate", label: "Brief approved?", kind: "condition", x: 430, y: 54 },
      { id: "generate", label: "Generate image", kind: "image", x: 570, y: 54 },
      { id: "asset_review", label: "Review image", kind: "approval", x: 710, y: 54 },
      { id: "asset_gate", label: "Image approved?", kind: "condition", x: 850, y: 54 },
      { id: "approved", label: "Approved image", kind: "end", x: 990, y: 10 },
      { id: "asset_changes", label: "Revise image", kind: "end", x: 990, y: 118 },
      { id: "brief_changes", label: "Revise brief", kind: "end", x: 570, y: 190 },
    ],
    edges: [
      { from: "knowledge", to: "draft" },
      { from: "draft", to: "brief_review" },
      { from: "brief_review", to: "brief_gate" },
      { from: "brief_gate", to: "generate", branch: "approved" },
      { from: "brief_gate", to: "brief_changes", branch: "changes" },
      { from: "generate", to: "asset_review" },
      { from: "asset_review", to: "asset_gate" },
      { from: "asset_gate", to: "approved", branch: "approved" },
      { from: "asset_gate", to: "asset_changes", branch: "changes" },
    ],
  },
  "opc-create-video-from-topic-v1": {
    nodes: [
      { id: "knowledge", label: "Video knowledge", kind: "knowledge", x: 10, y: 54 },
      { id: "draft", label: "Draft video brief", kind: "agent", x: 150, y: 54 },
      { id: "brief_review", label: "Review brief", kind: "approval", x: 290, y: 54 },
      { id: "brief_gate", label: "Brief approved?", kind: "condition", x: 430, y: 54 },
      { id: "generate", label: "Generate video", kind: "video", x: 570, y: 54 },
      { id: "asset_review", label: "Review video", kind: "approval", x: 710, y: 54 },
      { id: "asset_gate", label: "Video approved?", kind: "condition", x: 850, y: 54 },
      { id: "approved", label: "Approved video", kind: "end", x: 990, y: 10 },
      { id: "asset_changes", label: "Revise video", kind: "end", x: 990, y: 118 },
      { id: "brief_changes", label: "Revise brief", kind: "end", x: 570, y: 190 },
    ],
    edges: [
      { from: "knowledge", to: "draft" },
      { from: "draft", to: "brief_review" },
      { from: "brief_review", to: "brief_gate" },
      { from: "brief_gate", to: "generate", branch: "approved" },
      { from: "brief_gate", to: "brief_changes", branch: "changes" },
      { from: "generate", to: "asset_review" },
      { from: "asset_review", to: "asset_gate" },
      { from: "asset_gate", to: "approved", branch: "approved" },
      { from: "asset_gate", to: "asset_changes", branch: "changes" },
    ],
  },
};

const FLOW_TEMPLATE_PREVIEW_FALLBACK =
  FLOW_TEMPLATE_PREVIEW_GRAPHS["opc-write-article-from-topic-v1"];

const FLOW_TEMPLATE_PREVIEW_KIND_LABEL: Record<FlowTemplatePreviewNodeKind, string> = {
  knowledge: "K",
  tool: "WEB",
  agent: "AI",
  approval: "H",
  condition: "IF",
  image: "IMG",
  video: "VID",
  end: "END",
};

function flowTemplatePreviewEdgePath(
  source: FlowTemplatePreviewNode,
  target: FlowTemplatePreviewNode,
) {
  const sourceX = source.x + FLOW_TEMPLATE_PREVIEW_NODE_WIDTH;
  const sourceY = source.y + FLOW_TEMPLATE_PREVIEW_NODE_HEIGHT / 2;
  const targetX = target.x;
  const targetY = target.y + FLOW_TEMPLATE_PREVIEW_NODE_HEIGHT / 2;
  const bend = Math.max(34, (targetX - sourceX) * 0.46);
  return `M ${sourceX} ${sourceY} C ${sourceX + bend} ${sourceY}, ${targetX - bend} ${targetY}, ${targetX} ${targetY}`;
}

function FlowTemplateScreenshot({ template }: { template: WorkflowTemplate }) {
  const graph = FLOW_TEMPLATE_PREVIEW_GRAPHS[template.key] || FLOW_TEMPLATE_PREVIEW_FALLBACK;
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  const patternId = `flow-template-grid-${template.key.replace(/[^a-z0-9]/gi, "-")}`;

  return (
    <div
      className="workspace-flow-template-screenshot"
      role="img"
      aria-label={`${template.name} workflow`}
    >
      <svg viewBox="0 0 1130 636" preserveAspectRatio="xMidYMid meet">
        <defs>
          <pattern id={patternId} width="24" height="24" patternUnits="userSpaceOnUse">
            <circle cx="2" cy="2" r="1.15" className="workspace-flow-template-grid-dot" />
          </pattern>
        </defs>
        <rect width="1130" height="636" className="workspace-flow-template-canvas" />
        <rect width="1130" height="636" fill={`url(#${patternId})`} />
        <g transform="translate(0 168)">
          <g className="workspace-flow-template-edges">
            {graph.edges.map((edge) => {
              const source = nodesById.get(edge.from);
              const target = nodesById.get(edge.to);
              if (!source || !target) return null;
              return (
                <path
                  key={`${edge.from}-${edge.to}`}
                  d={flowTemplatePreviewEdgePath(source, target)}
                  className={edge.branch ? `is-${edge.branch}` : undefined}
                />
              );
            })}
          </g>
          <g className="workspace-flow-template-nodes">
            {graph.nodes.map((node) => (
              <g
                key={node.id}
                className={`workspace-flow-template-node workspace-flow-template-node--${node.kind}`}
                transform={`translate(${node.x} ${node.y})`}
              >
                <rect
                  width={FLOW_TEMPLATE_PREVIEW_NODE_WIDTH}
                  height={FLOW_TEMPLATE_PREVIEW_NODE_HEIGHT}
                  rx="12"
                  className="workspace-flow-template-node-card"
                />
                <rect x="10" y="10" width="34" height="34" rx="10" className="workspace-flow-template-node-icon" />
                <text x="27" y="31" textAnchor="middle" className="workspace-flow-template-node-kind">
                  {FLOW_TEMPLATE_PREVIEW_KIND_LABEL[node.kind]}
                </text>
                <text x="52" y="31" className="workspace-flow-template-node-label">
                  {node.label}
                </text>
                <circle cx="0" cy="27" r="4" className="workspace-flow-template-node-handle" />
                <circle cx="126" cy="27" r="4" className="workspace-flow-template-node-handle" />
              </g>
            ))}
          </g>
        </g>
      </svg>
    </div>
  );
}

function featuredFlowTemplates(
  templates: WorkflowTemplate[],
): WorkflowTemplate[] {
  const preferred = FEATURED_FLOW_TEMPLATE_KEYS.flatMap((key) => {
    const template = templates.find((candidate) => candidate.key === key);
    return template ? [template] : [];
  });
  const preferredIds = new Set(preferred.map((template) => template.id));
  return [
    ...preferred,
    ...templates.filter((template) => !preferredIds.has(template.id)),
  ].slice(0, 4);
}

function FlowTemplateQuickPreview({
  template,
  onClose,
  onUse,
  installing,
  disabled,
  installError,
}: {
  template: WorkflowTemplate | null;
  onClose: () => void;
  onUse: (template: WorkflowTemplate) => void;
  installing: boolean;
  disabled: boolean;
  installError: boolean;
}) {
  const installedWorkflowId = template?.installed_workflow_id || "";
  const isInstalled = Boolean(template?.installed || installedWorkflowId);

  return (
    <Modal
      open={Boolean(template)}
      onClose={onClose}
      title={
        template
          ? `${t("component.embedded_chat.quick_preview")} · ${template.name}`
          : t("component.embedded_chat.quick_preview")
      }
      className="workspace-sample-quick-preview-modal workspace-flow-template-quick-preview-modal"
      bodyClassName="workspace-sample-quick-preview-body"
      maxWidth="980px"
      footer={
        template ? (
          <div className="workspace-sample-quick-preview-footer">
            {installError && (
              <p className="workspace-flow-template-install-error" role="alert">
                {t("component.embedded_chat.flow_templates.install_error")}
              </p>
            )}
            <Button
              size="sm"
              variant={isInstalled ? "outline" : "primary"}
              loading={installing}
              disabled={disabled || (isInstalled && !installedWorkflowId)}
              onClick={() => onUse(template)}
            >
              {t(
                isInstalled
                  ? "component.embedded_chat.flow_templates.open_flow"
                  : "component.embedded_chat.flow_templates.install_template",
              )}
            </Button>
          </div>
        ) : null
      }
    >
      {template && (
        <div className="workspace-sample-quick-preview workspace-flow-template-quick-preview">
          <figure>
            <FlowTemplateScreenshot template={template} />
          </figure>
          <div className="workspace-flow-template-preview-details">
            <div>
              <p>{template.description}</p>
              <div className="workspace-flow-template-preview-facts">
                <span>
                  {t("component.embedded_chat.flow_templates.node_count").replace(
                    "{count}",
                    String(template.node_count),
                  )}
                </span>
                <span>v{template.version}</span>
              </div>
            </div>
            {isInstalled && (
              <span className="workspace-sample-installation-status workspace-sample-installation-status--installed workspace-flow-template-preview-installed">
                <IconCheckCircle size={13} />
                {t("component.embedded_chat.flow_templates.installed_status")}
              </span>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}

function FlowTemplateSamples() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [previewTemplate, setPreviewTemplate] =
    useState<WorkflowTemplate | null>(null);
  const templates = useQuery<WorkflowTemplate[]>({
    queryKey: ["workflow-templates"],
    queryFn: () => api.workflows.templates(),
  });
  const openFlow = useCallback(
    (workflowId: string) => {
      const returnTo = `${location.pathname}${location.search}${location.hash}`;
      navigate(`/flows?workflow=${encodeURIComponent(workflowId)}`, {
        state: { returnTo },
      });
    },
    [location.hash, location.pathname, location.search, navigate],
  );
  const installation = useMutation({
    mutationFn: (template: WorkflowTemplate) =>
      api.workflows.installTemplate(template.id),
    onSuccess: (result: any) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-templates"] });
      const workflowId = String(result?.workflow?.id || "").trim();
      if (workflowId) openFlow(workflowId);
    },
  });
  const visibleTemplates = featuredFlowTemplates(templates.data || []);
  const useTemplate = (template: WorkflowTemplate) => {
    installation.reset();
    const installedWorkflowId = template.installed_workflow_id || "";
    if (installedWorkflowId) {
      openFlow(installedWorkflowId);
      return;
    }
    installation.mutate(template);
  };

  if (templates.isLoading) {
    return (
      <div className="workspace-flow-template-state" role="status">
        <LoadingSpinner size={20} />
        <span>{t("component.embedded_chat.flow_templates.loading")}</span>
      </div>
    );
  }

  if (templates.isError) {
    return (
      <div className="workspace-flow-template-state workspace-flow-template-state--error">
        <EmptyState
          icon={<IconFlow size={22} />}
          title={t("component.embedded_chat.flow_templates.load_error_title")}
          description={t(
            "component.embedded_chat.flow_templates.load_error_description",
          )}
          action={(
            <Button
              size="sm"
              variant="outline"
              loading={templates.isFetching}
              onClick={() => void templates.refetch()}
            >
              {t("component.embedded_chat.flow_templates.retry")}
            </Button>
          )}
        />
      </div>
    );
  }

  if (visibleTemplates.length === 0) {
    return (
      <div className="workspace-flow-template-state">
        <EmptyState
          icon={<IconFlow size={22} />}
          title={t("component.embedded_chat.flow_templates.empty_title")}
          description={t("component.embedded_chat.flow_templates.empty_description")}
        />
      </div>
    );
  }

  return (
    <>
      <div
        className="workspace-sample-grid workspace-sample-grid--previewable workspace-flow-template-grid"
        aria-label={t("component.embedded_chat.flow_templates.gallery_label")}
      >
        {visibleTemplates.map((template) => {
          const installing =
            installation.isPending && installation.variables?.id === template.id;
          return (
            <article
              key={template.id}
              className="workspace-sample-card workspace-sample-card--flow-template workspace-sample-card--previewable"
            >
              <strong>{template.name}</strong>
              <FlowTemplateScreenshot template={template} />
              <div className="workspace-sample-actions">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    installation.reset();
                    setPreviewTemplate(template);
                  }}
                >
                  {t("component.embedded_chat.quick_preview")}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  loading={installing}
                  disabled={installation.isPending && !installing}
                  onClick={() => useTemplate(template)}
                >
                  <IconSparkles size={14} />
                  {t("component.embedded_chat.remix")}
                </Button>
              </div>
            </article>
          );
        })}
      </div>
      {installation.isError && !previewTemplate && (
        <p className="workspace-flow-template-install-error" role="alert">
          {t("component.embedded_chat.flow_templates.install_error")}
        </p>
      )}
      <FlowTemplateQuickPreview
        template={previewTemplate}
        onClose={() => {
          installation.reset();
          setPreviewTemplate(null);
        }}
        onUse={useTemplate}
        installing={
          installation.isPending &&
          installation.variables?.id === previewTemplate?.id
        }
        disabled={
          installation.isPending &&
          installation.variables?.id !== previewTemplate?.id
        }
        installError={installation.isError}
      />
    </>
  );
}

function WorkspaceWelcome({
  activeCapability,
  activeIdeaMode,
  onCapabilityChange,
  onSampleSelect,
  onIdeaQuickAction,
  onIdeaModeChange,
  onValidationStart,
}: {
  activeCapability: WorkspaceCapability;
  activeIdeaMode: IdeaQuickAction["id"] | null;
  onCapabilityChange: (capability: WorkspaceCapability) => void;
  onSampleSelect: (
    sample: WorkspaceSample,
    templateAttachment?: AttachedItem,
  ) => void;
  onIdeaQuickAction: (request: IdeaQuickActionRequest) => void;
  onIdeaModeChange: (mode: IdeaQuickAction["id"] | null) => void;
  onValidationStart: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const flowsAccess = usePreviewFeatureAccess("flows");
  const selected =
    WORKSPACE_CAPABILITIES.find((item) => item.key === activeCapability) ||
    WORKSPACE_CAPABILITIES[0];
  const quickActions = ideaQuickActions();
  const railFocusKey: WorkspaceRailKey = activeIdeaMode || activeCapability;
  const [ideaCandidates, setIdeaCandidates] = useState(() =>
    pickRandomSoloBusinessIdeas(4),
  );
  const [previewSample, setPreviewSample] = useState<WorkspaceSample | null>(null);
  const [sampleCatalogOpen, setSampleCatalogOpen] = useState(false);
  const [remixingSampleTitle, setRemixingSampleTitle] = useState<string | null>(null);
  const [installingSampleSlug, setInstallingSampleSlug] = useState<string | null>(null);
  const hasTemplateCatalog =
    selected.key === "slides" || selected.key === "sheets";
  const featuredSamples = selected.samples.slice(0, 4);
  const previewBlueprintSlug = (
    undefined
  );

  const handleSampleInstall = useCallback(
    async (_sample: WorkspaceSample) => {
    },
    [
    ],
  );

  const prepareSampleRemix = useCallback(
    async (sample: WorkspaceSample): Promise<boolean> => {
      if (remixingSampleTitle) return false;
      setRemixingSampleTitle(sample.title);
      try {
        const templateAttachment = await uploadTemplateRemixSource(sample);
        if (templateAttachment) {
          await invalidateKnowledgeQueries(queryClient);
        }
        onSampleSelect(sample, templateAttachment);

        toast.success(
          t("component.embedded_chat.template_ready").replace(
            "{name}", sample.title,
          ),
        );
        return true;
      } catch (error) {
        toast.error(
          t("component.embedded_chat.template_create_failed"),
          error instanceof Error ? error.message : undefined,
        );
        return false;
      } finally {
        setRemixingSampleTitle(null);
      }
    },
    [
      onSampleSelect,
      queryClient,
      remixingSampleTitle,
      toast,
    ],
  );

  useEffect(() => {
    if (selected.key !== "slides" && selected.key !== "sheets") {
      setSampleCatalogOpen(false);
    }
  }, [selected.key]);

  const focusedIdea = quickActions.find((action) => action.id === railFocusKey);
  const activeIndex = WORKSPACE_RAIL_ORDER.indexOf(railFocusKey);
  const modeRailRef = useRef<HTMLDivElement | null>(null);
  const activePillRef = useRef<HTMLButtonElement | null>(null);
  const wheelSwitchLockedRef = useRef(false);
  const railFocusKeyRef = useRef<WorkspaceRailKey>(railFocusKey);
  const activeRailIndexRef = useRef(activeIndex);
  const dockDragStartXRef = useRef<number | null>(null);
  const dockDragLastXRef = useRef<number | null>(null);
  const dockClickRailKeyRef = useRef<WorkspaceRailKey | null>(null);
  railFocusKeyRef.current = railFocusKey;
  activeRailIndexRef.current = activeIndex;

  const refreshIdeaCandidates = useCallback(() => {
    setIdeaCandidates((current) =>
      pickRandomSoloBusinessIdeas(
        4,
        current.map((idea) => idea.id),
      ),
    );
  }, []);

  useEffect(() => {
    const rail = modeRailRef.current;
    if (!rail) return;

    const centerFocusedItems = (behavior: ScrollBehavior) => {
      if (railFocusKey === "new-idea") {
        const newIdea = rail.querySelector<HTMLElement>(
          '[data-rail-key="new-idea"]',
        );
        const validateIdea = rail.querySelector<HTMLElement>(
          '[data-rail-key="validate-idea"]',
        );
        if (newIdea && validateIdea) {
          const pairCenter =
            (newIdea.offsetLeft +
              newIdea.offsetWidth / 2 +
              validateIdea.offsetLeft +
              validateIdea.offsetWidth / 2) /
            2;
          rail.scrollTo({
            left: pairCenter - rail.clientWidth / 2,
            behavior,
          });
          return;
        }
      }
      activePillRef.current?.scrollIntoView({
        behavior,
        block: "nearest",
        inline: "center",
      });
    };

    centerFocusedItems("smooth");
    const resizeObserver = new ResizeObserver(() => {
      centerFocusedItems("auto");
    });
    resizeObserver.observe(rail);
    return () => resizeObserver.disconnect();
  }, [railFocusKey]);

  /*
   * The keys a gesture can actually land on. A gated preview is removed from
   * the walk here, once, as data — never as a branch inside the step function.
   * Naming a key in there is how the rail ends up stranding everything behind
   * it: the step bounces off the special case instead of passing through.
   */
  const reachableRailOrder = useMemo(
    () =>
      WORKSPACE_RAIL_ORDER.filter(
        (key) => key !== "flows" || flowsAccess.enabled,
      ),
    [flowsAccess.enabled],
  );

  const focusRailByStep = useCallback(
    (step: number) => {
      if (step === 0) return;
      const currentKey = WORKSPACE_RAIL_ORDER[activeRailIndexRef.current];
      const currentIndex = Math.max(0, reachableRailOrder.indexOf(currentKey));
      const nextIndex = Math.max(
        0,
        Math.min(reachableRailOrder.length - 1, currentIndex + step),
      );
      if (nextIndex === currentIndex) return;
      const nextKey = reachableRailOrder[nextIndex];
      if (nextKey === "new-idea" || nextKey === "validate-idea") {
        onIdeaModeChange(nextKey);
        if (nextKey === "new-idea") refreshIdeaCandidates();
      } else {
        onIdeaModeChange(null);
        onCapabilityChange(nextKey);
      }
    },
    [
      onCapabilityChange,
      onIdeaModeChange,
      refreshIdeaCandidates,
      reachableRailOrder,
    ],
  );

  const handleRailItemClick = useCallback(
    (key: WorkspaceRailKey) => {
      if (key === "flows" && !flowsAccess.enabled) return;
      if (key === "new-idea" || key === "validate-idea") {
        onIdeaModeChange(key);
        if (key === "new-idea") {
          refreshIdeaCandidates();
        } else {
          onValidationStart();
        }
        return;
      }
      onIdeaModeChange(null);
      onCapabilityChange(key);
    },
    [
      onCapabilityChange,
      onIdeaModeChange,
      onValidationStart,
      refreshIdeaCandidates,
      flowsAccess.enabled,
    ],
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
      focusRailByStep(delta > 0 ? 1 : -1);
      window.setTimeout(() => {
        wheelSwitchLockedRef.current = false;
      }, 420);
    };
    rail.addEventListener("wheel", handleWheel, { passive: false });
    return () => rail.removeEventListener("wheel", handleWheel);
  }, [focusRailByStep]);

  return (
    <div
      className="workspace-welcome"
      style={
        {
          "--capability-accent": focusedIdea?.accent || selected.accent,
        } as CSSProperties
      }
    >
      <div className="workspace-welcome-kicker">{t("component.embedded_chat.manor_ai_workspace")}</div>
      <h1>{t("component.embedded_chat.start_with_sample_or_prompt")}</h1>

      <div
        ref={modeRailRef}
        className="workspace-mode-rail"
        role="group"
        data-pair-focused={railFocusKey === "new-idea" ? "true" : undefined}
        aria-label={t("component.embedded_chat.workspace_capability_selector")}
        onPointerDown={(event) => {
          const targetPill = (event.target as HTMLElement).closest<HTMLElement>(
            ".workspace-mode-pill",
          );
          const targetKey = targetPill?.dataset.railKey as
            | WorkspaceRailKey
            | undefined;
          dockClickRailKeyRef.current =
            targetKey && WORKSPACE_RAIL_ORDER.includes(targetKey)
              ? targetKey
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
          const clickedKey = dockClickRailKeyRef.current;
          dockDragStartXRef.current = null;
          dockDragLastXRef.current = null;
          dockClickRailKeyRef.current = null;
          event.currentTarget.releasePointerCapture?.(event.pointerId);
          if (startX == null || lastX == null) return;
          const diff = lastX - startX;
          if (Math.abs(diff) < 34) {
            if (clickedKey) handleRailItemClick(clickedKey);
            return;
          }
          if (wheelSwitchLockedRef.current) return;
          focusRailByStep(diff < 0 ? 1 : -1);
        }}
        onPointerCancel={() => {
          dockDragStartXRef.current = null;
          dockDragLastXRef.current = null;
          dockClickRailKeyRef.current = null;
        }}
      >
        {WORKSPACE_RAIL_ORDER.map((railKey, index) => {
          const action = quickActions.find((candidate) => candidate.id === railKey);
          const capability = WORKSPACE_CAPABILITIES.find(
            (candidate) => candidate.key === railKey,
          );
          if (!action && !capability) return null;
          const distance = Math.abs(index - activeIndex);
          const direction =
            index === activeIndex ? 0 : index < activeIndex ? -1 : 1;
          const label = action?.title || capability!.label;
          const description = action?.description || capability!.description;
          const accent = action?.accent || capability!.accent;
          const isActive = railKey === railFocusKey;
          const unavailable = railKey === "flows" && !flowsAccess.enabled;
          const comingSoon =
            railKey === "flows" &&
            flowsAccess.loaded &&
            !flowsAccess.released;
          return (
            <button
              key={railKey}
              ref={isActive ? activePillRef : undefined}
              type="button"
              aria-current={isActive ? "true" : undefined}
              aria-label={
                comingSoon
                  ? `${label}. ${t("component.chat_mode.flows_coming_soon")}`
                  : action
                    ? `${label}. ${description}`
                    : label
              }
              aria-disabled={unavailable}
              disabled={unavailable}
              title={
                comingSoon
                  ? t("component.chat_mode.flows_coming_soon")
                  : description
              }
              className={`workspace-mode-pill ${action ? "workspace-mode-pill--idea" : ""} ${isActive ? "workspace-mode-pill--active" : ""} ${unavailable ? "workspace-mode-pill--disabled" : ""}`}
              data-distance={Math.min(distance, 3)}
              data-direction={direction}
              data-rail-key={railKey}
              style={{ "--capability-accent": accent } as CSSProperties}
              onClick={(event) => {
                if (event.detail === 0) handleRailItemClick(railKey);
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                handleRailItemClick(railKey);
              }}
            >
              <span className="workspace-mode-pill-visual">
                <WorkspaceRailVisual kind={railKey} active={isActive} />
              </span>
              <strong>{label}</strong>
              {comingSoon ? (
                <span className="workspace-mode-pill-badge">
                  {t("component.chat_mode.soon")}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      <p className="workspace-mode-summary">
        <span>
          {focusedIdea
            ? focusedIdea.title
            : `${selected.label} ${t("component.embedded_chat.mode")}`}
        </span>
        {focusedIdea?.description ||
          (selected.key === "flows"
            ? t("component.embedded_chat.flow_templates.helper")
            : selected.description)}
      </p>

      {focusedIdea?.id === "new-idea" ? (
        <>
          <div
            className="workspace-sample-grid workspace-idea-grid"
            aria-label={t(
              "component.embedded_chat.idea_library.candidates_label",
            )}
            aria-live="polite"
          >
            {ideaCandidates.map((idea) => {
              return (
                <article
                  key={`${focusedIdea.id}-${idea.id}`}
                  role="button"
                  tabIndex={0}
                  className="workspace-sample-card workspace-idea-card"
                  onClick={() =>
                    onIdeaQuickAction(ideaCandidateRequest(focusedIdea, idea))
                  }
                  onKeyDown={(event) => {
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    onIdeaQuickAction(ideaCandidateRequest(focusedIdea, idea));
                  }}
                >
                  <span className="workspace-sample-mode">
                    {t(soloBusinessIdeaExecutionKey(idea.manorExecution))}
                  </span>
                  <strong>{ideaField(idea, "title")}</strong>
                  <dl className="workspace-idea-summary">
                    <div>
                      <dt>{t("component.embedded_chat.idea_library.customer_label")}</dt>
                      <dd>{ideaField(idea, "buyer")}</dd>
                    </div>
                    <div>
                      <dt>{t("component.embedded_chat.idea_library.offer_label")}</dt>
                      <dd>{ideaField(idea, "promise")}</dd>
                    </div>
                    <div>
                      <dt>{t("component.embedded_chat.idea_library.revenue_label")}</dt>
                      <dd>{ideaField(idea, "revenue")}</dd>
                    </div>
                  </dl>
                  <span className="workspace-sample-apply">
                    {t("component.embedded_chat.idea_library.explore_cta")}
                    <IconChevronRight size={13} />
                  </span>
                </article>
              );
            })}
          </div>
          <div className="workspace-idea-actions">
            <Button
              size="sm"
              variant="ghost"
              className="workspace-idea-refresh"
              onClick={refreshIdeaCandidates}
            >
              {t("component.embedded_chat.idea_library.show_more")}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => onIdeaQuickAction(freshIdeaRequest())}
            >
              <IconSparkles size={14} />
              {t("component.embedded_chat.idea_library.generate_fresh")}
            </Button>
          </div>
        </>
      ) : focusedIdea?.id === "validate-idea" ? (
        <section
          className="workspace-idea-validation-intake"
          aria-labelledby="workspace-idea-validation-title"
        >
          <div className="workspace-idea-validation-heading">
            <span className="workspace-idea-validation-icon">
              <IconReport size={20} />
            </span>
            <div>
              <h2 id="workspace-idea-validation-title">
                {t("component.embedded_chat.idea_validation.title")}
              </h2>
              <p>{t("component.embedded_chat.idea_validation.description")}</p>
            </div>
          </div>
          <div className="workspace-idea-validation-checklist">
            {[
              "customer",
              "problem",
              "offer",
              "evidence",
            ].map((item, index) => (
              <span key={item}>
                <strong>{index + 1}</strong>
                {t(`component.embedded_chat.idea_validation.${item}`)}
              </span>
            ))}
          </div>
        </section>
      ) : selected.key === "flows" ? (
        <FlowTemplateGallery />
      ) : (
        <>
          <div
            className="workspace-sample-grid workspace-sample-grid--previewable"
          >
            {featuredSamples.map((sample, index) => {
              const isInstallableWorkspace = Boolean(
              );
              const isInstalling =
                isInstallableWorkspace &&
                Boolean(
                );

              return (
                <article
                  key={`${selected.key}-sample-${index}-${sample.title}`}
                  className="workspace-sample-card workspace-sample-card--previewable"
                >
                  <strong>{sample.title}</strong>
                  <WorkspaceSamplePreview
                    kind={sample.preview || selected.key}
                    content={sample.previewContent}
                  />
                  <div className="workspace-sample-actions">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setPreviewSample(sample)}
                    >
                      {t("component.embedded_chat.quick_preview")}
                    </Button>
                    <Button
                      size="sm"
                      variant="primary"
                      loading={
                        isInstallableWorkspace
                          ? isInstalling
                          : remixingSampleTitle === sample.title
                      }
                      disabled={
                        isInstallableWorkspace
                          ? Boolean(
                              installingSampleSlug &&
                                !isInstalling,
                            )
                          : Boolean(
                              remixingSampleTitle &&
                                remixingSampleTitle !== sample.title,
                            )
                      }
                      onClick={() => {
                        if (isInstallableWorkspace) {
                          void handleSampleInstall(sample);
                          return;
                        }
                        void prepareSampleRemix(sample);
                      }}
                    >
                      {isInstallableWorkspace ? (
                        t("component.embedded_chat.install_workspace")
                      ) : (
                        <>
                          <IconSparkles size={14} />
                          {t("component.embedded_chat.remix")}
                        </>
                      )}
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
          {hasTemplateCatalog && selected.samples.length > featuredSamples.length && (
            <div className="workspace-sample-library-link">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSampleCatalogOpen(true)}
              >
                {t(
                  selected.key === "slides"
                    ? "component.embedded_chat.browse_presentation_templates"
                    : "component.embedded_chat.browse_spreadsheet_templates",
                ).replace(
                  "{count}",
                  String(selected.samples.length),
                )}
                <IconChevronRight size={13} />
              </Button>
            </div>
          )}
        </>
      )}
      <Modal
        open={sampleCatalogOpen && hasTemplateCatalog}
        onClose={() => setSampleCatalogOpen(false)}
        title={t(
          selected.key === "slides"
            ? "component.embedded_chat.presentation_template_library"
            : "component.embedded_chat.spreadsheet_template_library",
        )}
        className="workspace-sample-catalog-modal"
        bodyClassName="workspace-sample-catalog-body"
        maxWidth="1120px"
      >
        <div className="workspace-sample-catalog-grid">
            {selected.samples.map((sample, index) => (
              <article
                key={`${selected.key}-catalog-${index}-${sample.title}`}
                className="workspace-sample-card workspace-sample-card--previewable"
              >
                <strong>{sample.title}</strong>
                <WorkspaceSamplePreview
                  kind={sample.preview || selected.key}
                  content={sample.previewContent}
                />
                <div className="workspace-sample-actions">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setSampleCatalogOpen(false);
                      setPreviewSample(sample);
                    }}
                  >
                    {t("component.embedded_chat.quick_preview")}
                  </Button>
                  <Button
                    size="sm"
                    variant="primary"
                    loading={remixingSampleTitle === sample.title}
                    disabled={Boolean(
                      remixingSampleTitle && remixingSampleTitle !== sample.title,
                    )}
                    onClick={() => {
                      void prepareSampleRemix(sample).then((didRemix) => {
                        if (didRemix) setSampleCatalogOpen(false);
                      });
                    }}
                  >
                    <IconSparkles size={14} />
                    {t("component.embedded_chat.remix")}
                  </Button>
                </div>
              </article>
            ))}
        </div>
      </Modal>
      <WorkspaceSampleQuickPreview
        sample={previewSample}
        onClose={() => setPreviewSample(null)}
        onRemix={prepareSampleRemix}
        remixing={Boolean(
          previewSample && remixingSampleTitle === previewSample.title,
        )}
        remixDisabled={Boolean(
          remixingSampleTitle && remixingSampleTitle !== previewSample?.title,
        )}
        blueprint={
          null
        }
        blueprintStatus={
          "idle"
        }
        installedWorkspace={
          null
        }
        installationStatus={
          "idle"
        }
        onInstall={(blueprint) => {
        }}
        onOpenInstalled={(workspace) => navigate(`/workspaces/${workspace.id}`)}
      />
    </div>
  );
}

function isDiagramArtifactReference(value?: unknown) {
  const path = String(value || "").split(/[?#]/)[0].trim().toLowerCase();
  return (
    path.endsWith(".diagram.json") ||
    /\.(mmd|mermaid|drawio|diagram)$/.test(path)
  );
}

function inferArtifactKindFromPath(path: string): OutputArtifact["kind"] {
  const lowerPath = path.toLowerCase();
  if (lowerPath.match(/\.(ppt|pptx)$/)) return "presentation";
  if (lowerPath.match(/\.(pdf)$/)) return "pdf";
  if (lowerPath.match(/\.(xlsx|xls|csv)$/)) return "spreadsheet";
  if (isDiagramArtifactReference(lowerPath)) return "diagram";
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
  const fileType = String(doc.file_type || "")
    .trim()
    .toLowerCase()
    .replace(/^\./, "");
  const persistedKind = fileType ? fileReferenceKind("", undefined, fileType) : "file";
  const ext = persistedKind !== "file" ? fileType : (
    (doc.name || "").split(".").pop()?.toLowerCase() || ""
  );
  const mime = persistedKind !== "file" ? "" : doc.mime_type || "";

  if (fileReferenceKind(doc.name || "", doc.mime_type, doc.file_type) === "diagram") {
    return "diagram";
  }
  if (["md", "markdown"].includes(ext)) return "markdown";
  if (["html", "htm"].includes(ext) || mime === "text/html") return "html";
  if (["json"].includes(ext) || mime === "application/json") return "json";
  if (["csv"].includes(ext) || mime === "text/csv") return "csv";
  if (["mmd", "mermaid", "drawio", "diagram"].includes(ext)) return "diagram";
  if (persistedKind === "page") return "html";
  if (persistedKind === "image") return "image";
  if (persistedKind === "video") return "video";
  if (persistedKind === "audio") return "audio";
  if (persistedKind === "pdf") return "pdf";
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
  if (persistedKind === "code") return "code";
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

function extractFileArtifactsFromText(
  content: string,
  idPrefix: string,
): OutputArtifact[] {
  const matches = extractPlatformFileReferences(content);
  const seen = new Set<string>();
  return matches.flatMap((match, index) => {
    const raw = match.trim();
    const name = fileNameFromPath(raw);
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

function artifactSpreadsheetColumnLabel(column: number): string {
  let value = column + 1;
  let label = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    value = Math.floor((value - 1) / 26);
  }
  return label;
}

interface OutputState {
  artifacts: OutputArtifact[];
  progress: ProgressItem[];
}

function normalizeArtifactPath(value?: string) {
  if (!value) return "";
  return String(value).trim().replace(/\\/g, "/").toLowerCase();
}

export function artifactDedupKey(artifact: OutputArtifact) {
  if (artifact.kind === "workspace" && artifact.data?.draft_id) {
    return `workspace|draft:${String(artifact.data.draft_id)}`;
  }
  const documentId = String(artifact.data?.document_id || "").trim();
  if (documentId) return `${artifact.kind}|document:${documentId}`;
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
      if (existingIndex >= 0 && artifact.kind === "workspace") {
        result[existingIndex] = {
          ...result[existingIndex],
          ...artifact,
          id: result[existingIndex].id,
        };
      } else if (
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
  if (
    artifact.href?.startsWith("/viewer/") ||
    data.document_id
  ) {
    return true;
  }
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

function compactChatRailText(value: unknown) {
  return (toDisplayText(value) || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[#>*_~]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function chatRailPreviewFromText(value: unknown, fallbackTitle: string) {
  const compact = compactChatRailText(value);
  if (!compact) return { title: fallbackTitle, excerpt: "" };
  const titleLength = compact.length > 58 ? 58 : compact.length;
  const title =
    compact.length > titleLength
      ? `${compact.slice(0, titleLength).trim()}...`
      : compact;
  const excerptSource =
    compact.length > titleLength ? compact.slice(titleLength).trim() : compact;
  const excerpt =
    excerptSource.length > 150
      ? `${excerptSource.slice(0, 150).trim()}...`
      : excerptSource;
  return { title, excerpt };
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
    /删除|写入|修改|移动|覆盖|创建|生成|保存|发布|发表|delete|write|modify|move|overwrite|create|generate|save|publish|publicat|post/.test(
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

export function assistantMessageRendersInlineFileSurfaces(
  message: ChatMessage | undefined,
  streaming: boolean,
): boolean {
  if (message?.role !== "assistant") return false;
  if (message.stop_reason === "credit_exhausted") return false;
  return streaming || !isApprovalBoilerplateContent(message);
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

function parseArtifactToolResult(tool: ToolCall): any {
  const preview = parseToolResultJson(tool.result);
  if (preview && typeof preview === "object") return preview;
  return parseToolResultJson(tool.rawResult ?? tool.result);
}

function toolResultRequestsChatArtifact(tool: ToolCall): boolean {
  const parsed = parseArtifactToolResult(tool);
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
  if (!["start_workspace_draft", "continue_workspace_draft", "manor"].includes(normalizedName))
    return false;
  const parsed = parseArtifactToolResult(tool);
  return Boolean(
    parsed?.draft_id &&
    (parsed?.artifact_kind === "workspace_draft" ||
      (typeof parsed?.deep_link === "string" &&
        parsed.deep_link.includes("/workspaces/new?draft="))),
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
  if (["diagram", "diagram.json", "mermaid", "mmd", "drawio"].includes(value))
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
  const diagramReferences = [
    record.name,
    record.filename,
    record.fs_path,
    record.file_path,
    record.path,
    record.output_path,
    record.saved_to,
    reference,
  ];
  const explicit = normalizeArtifactKind(
    record.kind || record.type || record.category,
  );
  const persistedFileType = String(record.file_type || record.fileType || "").trim();
  if (explicitDiagramIdentityOverridesGenericJson(persistedFileType, explicit)) {
    return "diagram";
  }
  if (persistedFileType) {
    const persistedFileKind = fileReferenceKind(
      diagramReferences.find((value) => String(value || "").trim()) || "",
      record.mime_type || record.mime,
      persistedFileType,
    );
    if (persistedFileKind !== "file") {
      if (persistedFileKind === "archive") return "file";
      return persistedFileKind;
    }
  }
  if (record.html || record.component || record.preview_url || record.route)
    return "page";
  if (explicit && explicit !== "file") return explicit;
  if (diagramReferences.some(isDiagramArtifactReference)) return "diagram";
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
    merged.open_url ||
    merged.viewer_url ||
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
  const documentId = String(merged.document_id || "").trim();

  if (!reference && !documentId) return null;

  const kind = artifactKindFromRecord(merged, reference);
  const openHref = generatedFileOpenReference({
    ...merged,
    document_id: documentId,
    open_url: href,
    fs_path: path,
  }) || undefined;
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
    href: openHref,
    body: path || openHref || merged.name || title,
    meta: path || openHref || merged.name || title,
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
  const rawResult: unknown = tool.rawResult ?? tool.result;
  const textResult = toDisplayText(rawResult) || "";
  if (!textResult || tool.status === "pending") return null;
  if (tool.status === "error") return null;

  const parsed = parseToolResultJson(rawResult);

  const normalizedName = (tool.name || "").toLowerCase();

  if (parsed?.draft_id && (
    parsed?.artifact_kind === "workspace_draft" ||
    (typeof parsed?.deep_link === "string" && parsed.deep_link.includes("/workspaces/new?draft="))
  )) {
    return {
      id,
      kind: "workspace",
      title: parsed.title || "Workspace configuration",
      status: "done",
      body:
        parsed.assistant_reply ||
        "Keep chatting to refine this Workspace.",
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

  const parsed = parseArtifactToolResult(tool);
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

export function deriveMessageArtifacts(
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

  msg.attachments?.forEach((attachment, attachmentIndex) => {
    const documentId = String(attachment.document_id || "").trim();
    const previewUrl = String(attachment.previewUrl || "").trim();
    const openUrl = String(attachment.openUrl || "").trim();
    const fsPath = String(attachment.fsPath || "").trim();
    const reference = generatedFileOpenReference({
      document_id: documentId,
      open_url: openUrl,
      result_url: previewUrl,
      fs_path: fsPath,
    });
    const name = String(attachment.name || "").trim();
    if (!name || !reference) return;
    artifacts.push({
      id: `message-attachment-${attachmentIndex}-${documentId || name}`,
      kind: artifactKindFromRecord({
        name,
        file_type: attachment.fileType,
        type: attachment.type,
        mime_type: attachment.mimeType,
      }, name),
      title: friendlyGeneratedAssetTitle(name),
      status: "done",
      href: reference,
      body: reference,
      meta: reference,
      data: {
        document_id: documentId || undefined,
        file_url: previewUrl || undefined,
        open_url: openUrl || undefined,
        fs_path: fsPath || undefined,
        file_type: attachment.fileType || attachment.type,
        mime_type: attachment.mimeType,
        name,
      },
    });
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

export function filterMessageArtifactsAlreadyRepresented(
  message: ChatMessage | undefined,
  artifacts: OutputArtifact[],
  inlineFileSurfacesVisible = true,
  projection: ChatMessageDisplayProjectionOptions = {},
): OutputArtifact[] {
  if (
    message?.role !== "assistant"
    || artifacts.length === 0
    || !inlineFileSurfacesVisible
  ) return artifacts;
  const parsedDisplay = parseUserMessageDisplay(message, projection);
  const renderedContent =
    projection.renderedContent === undefined
      ? parsedDisplay.cleanContent
      : projection.renderedContent;
  const markdown = renderedChatMessageMarkdownForFileDedupe(
    message,
    renderedContent,
    Boolean(projection.streaming),
  );
  const recordByArtifact = new Map<OutputArtifact, Record<string, unknown>>();
  artifacts.forEach((artifact) => {
    if (!FILE_BACKED_ARTIFACT_KINDS.has(artifact.kind)) return;
    const data = artifact.data || {};
    recordByArtifact.set(artifact, {
      ...data,
      name: data.name || artifact.title,
      artifact_url: artifact.href || data.artifact_url,
      output_path: artifact.body || data.output_path,
      output_url: artifact.meta || data.output_url,
    });
  });
  const recordsNotLinkedInMarkdown =
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      markdown,
      Array.from(recordByArtifact.values()),
    );
  const representedReferences = parsedDisplay.references
    .slice(0, CHAT_MESSAGE_REFERENCE_CARD_LIMIT)
    .map((reference) => ({
      ...reference,
      open_url: reference.openUrl,
      result_url: reference.url || reference.previewUrl,
      fs_path: reference.fsPath,
    }));
  const visibleRecords = new Set(
    filterGeneratedFileRecordsAlreadyRepresented(
      recordsNotLinkedInMarkdown,
      representedReferences,
    ),
  );
  return artifacts.filter((artifact) => {
    const record = recordByArtifact.get(artifact);
    return !record || visibleRecords.has(record);
  });
}

export function keepLatestWorkspaceDraftArtifacts(
  artifactGroups: OutputArtifact[][],
): OutputArtifact[][] {
  const latestMessageByDraft = new Map<string, number>();
  artifactGroups.forEach((artifacts, messageIndex) => {
    artifacts.forEach((artifact) => {
      if (artifact.kind !== "workspace" || !artifact.data?.draft_id) return;
      latestMessageByDraft.set(artifactDedupKey(artifact), messageIndex);
    });
  });

  return artifactGroups.map((artifacts, messageIndex) =>
    artifacts.filter((artifact) => {
      if (artifact.kind !== "workspace" || !artifact.data?.draft_id) return true;
      return latestMessageByDraft.get(artifactDedupKey(artifact)) === messageIndex;
    }),
  );
}

function ExecutionStatusDot({ status }: { status: ExecutionStatus }) {
  return (
    <span className={`chat-execution-dot chat-execution-dot--${status}`} />
  );
}

export function ArtifactIcon({ kind }: { kind: OutputArtifact["kind"] }) {
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
  return String(artifact.data?.document_id || "").trim();
}

function artifactDownloadName(artifact: OutputArtifact, doc?: Document | null) {
  const data = artifact.data || {};
  return (
    doc?.name ||
    fileNameFromPath(
      data.fs_path || data.file_path || data.path || data.saved_to || "",
    ) ||
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

async function findDocumentForArtifact(
  artifact: OutputArtifact,
): Promise<Document | null> {
  const documentId = artifactDocumentId(artifact);
  if (!documentId) return null;

  const cacheKey = `id:${documentId}`;
  const now = Date.now();
  const cached = artifactDocumentCache.get(cacheKey);
  if (cached && cached.expiresAt > now) return cached.document;
  if (cached) artifactDocumentCache.delete(cacheKey);

  const inflight = artifactDocumentInflight.get(cacheKey);
  if (inflight) return inflight;

  const lookup = api.documents.get(documentId).catch(() => null);

  artifactDocumentInflight.set(cacheKey, lookup);
  try {
    const document = await lookup;
    const entry = {
      document,
      expiresAt: Date.now() + ARTIFACT_DOCUMENT_CACHE_TTL_MS,
    };
    artifactDocumentCache.set(cacheKey, entry);
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
  if (directUrl && isLocalFsUrl(directUrl)) {
    const resolved = await resolveDisplayMediaUrl(directUrl);
    triggerBrowserDownload(
      resolved.url,
      artifactDownloadName(artifact),
      resolved.revoke,
    );
    return;
  }

  const fsPath =
    generatedFileFsPath(artifact.data) ||
    generatedFileFsPath({ fs_path: artifact.href }) ||
    generatedFileFsPath({ fs_path: artifact.body }) ||
    generatedFileFsPath({ fs_path: artifact.meta });
  if (fsPath) {
    const entityId = useAuthStore.getState().user?.entity_id;
    if (!entityId) throw new Error("Artifact download requires an active entity");
    const encodedPath = fsPath
      .split("/")
      .map((segment) => encodeURIComponent(segment))
      .join("/");
    const response = await fetchProtectedFsResponse(
      `/api/v1/fs/${encodeURIComponent(entityId)}/${encodedPath}`,
    );
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    triggerBrowserDownload(url, artifactDownloadName(artifact), () =>
      URL.revokeObjectURL(url),
    );
    return;
  }

  if (!directUrl) throw new Error("Artifact download source is unavailable");
  triggerBrowserDownload(directUrl, artifactDownloadName(artifact));
}

function withArtifactPreviewTimeout<T>(
  promise: Promise<T>,
  ms: number,
  message: string,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

function PresentationArtifactCanvas({
  url,
  title,
}: {
  url: string;
  title: string;
}) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const viewerRef = useRef<any>(null);
  const resizeTimerRef = useRef<number | null>(null);
  const renderGenerationRef = useRef(0);
  const renderQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const slideAspectRef = useRef(16 / 9);
  const activeSlideRef = useRef(0);
  const [slideCount, setSlideCount] = useState(0);
  const [activeSlide, setActiveSlide] = useState(0);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState("");

  const prepareCanvas = useCallback((aspect = slideAspectRef.current) => {
    const canvas = canvasRef.current;
    const stage = stageRef.current;
    if (!canvas || !stage) return null;
    const stageWidth = Math.max(1, stage.clientWidth);
    const stageHeight = Math.max(1, stage.clientHeight);
    const width = Math.max(
      1,
      Math.floor(Math.min(stageWidth, stageHeight * aspect)),
    );
    const height = Math.max(1, Math.round(width / aspect));
    const pixelRatio = window.devicePixelRatio || 1;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    return canvas;
  }, []);

  const renderSlide = useCallback(
    async (slideIndex: number, announce = true) => {
      const viewer = viewerRef.current;
      if (!viewer) return;
      if (announce && resizeTimerRef.current !== null) {
        window.clearTimeout(resizeTimerRef.current);
        resizeTimerRef.current = null;
      }
      activeSlideRef.current = slideIndex;
      const generation = ++renderGenerationRef.current;
      if (announce) setRendering(true);
      const task = renderQueueRef.current.then(async () => {
        if (
          generation !== renderGenerationRef.current ||
          viewerRef.current !== viewer
        ) {
          return false;
        }
        const canvas = prepareCanvas();
        if (!canvas) return false;
        await withArtifactPreviewTimeout(
          viewer.renderSlide(slideIndex, canvas, { quality: "high" }),
          20000,
          "Presentation preview timed out",
        );
        return true;
      });
      renderQueueRef.current = task.catch(() => undefined);
      try {
        const rendered = await task;
        if (
          rendered &&
          generation === renderGenerationRef.current &&
          viewerRef.current === viewer
        ) {
          setActiveSlide(slideIndex);
        }
      } catch (previewError: any) {
        if (
          generation === renderGenerationRef.current &&
          viewerRef.current === viewer
        ) {
          setError(
            previewError?.message ||
              t("component.embedded_chat.presentation_content_is_not_available_yet"),
          );
        }
      } finally {
        if (generation === renderGenerationRef.current) setRendering(false);
      }
    },
    [prepareCanvas],
  );

  useEffect(() => {
    let cancelled = false;
    const fetchAbortController = new AbortController();

    (async () => {
      setLoading(true);
      setError("");
      setSlideCount(0);
      setActiveSlide(0);
      setRendering(false);
      activeSlideRef.current = 0;
      try {
        const response = await fetch(url, {
          signal: fetchAbortController.signal,
        });
        if (!response.ok) throw new Error("Presentation fetch failed");
        const buffer = await response.arrayBuffer();
        if (cancelled || !canvasRef.current) return;
        const { PPTXViewer } = await import("pptxviewjs");
        if (cancelled || !canvasRef.current) return;
        const viewer = new PPTXViewer({
          canvas: canvasRef.current,
          backgroundColor: "#ffffff",
          slideSizeMode: "fit",
        });
        viewerRef.current = viewer;
        try {
          await withArtifactPreviewTimeout(
            viewer.loadFile(buffer),
            20000,
            "Presentation preview timed out",
          );
        } catch (previewError) {
          if (viewerRef.current === viewer) {
            viewerRef.current = null;
            viewer.destroy();
          }
          throw previewError;
        }
        if (cancelled || viewerRef.current !== viewer) return;
        const viewerInternals = viewer as any;
        const slideSize =
          viewerInternals?.processor?.getSlideDimensions?.() ||
          viewerInternals?.presentation?.slideSize;
        const rawAspect =
          slideSize?.cx && slideSize?.cy
            ? slideSize.cx / slideSize.cy
            : 16 / 9;
        slideAspectRef.current = Number.isFinite(rawAspect)
          ? Math.min(4, Math.max(0.25, rawAspect))
          : 16 / 9;
        const total = viewer.getSlideCount();
        setSlideCount(total);
        if (total > 0) await renderSlide(0, false);
      } catch (previewError: any) {
        if (!cancelled) {
          setError(
            previewError?.message ||
              t("component.embedded_chat.presentation_content_is_not_available_yet"),
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      fetchAbortController.abort();
      renderGenerationRef.current += 1;
      if (resizeTimerRef.current !== null) {
        window.clearTimeout(resizeTimerRef.current);
        resizeTimerRef.current = null;
      }
      const viewer = viewerRef.current;
      viewerRef.current = null;
      viewer?.destroy();
    };
  }, [renderSlide, url]);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || loading) return undefined;
    const observer = new ResizeObserver(() => {
      if (!viewerRef.current) return;
      if (resizeTimerRef.current !== null) {
        window.clearTimeout(resizeTimerRef.current);
      }
      resizeTimerRef.current = window.setTimeout(() => {
        resizeTimerRef.current = null;
        void renderSlide(activeSlideRef.current, false);
      }, 80);
    });
    observer.observe(stage);
    return () => observer.disconnect();
  }, [loading, renderSlide]);

  if (error) {
    return (
      <div className="chat-output-file-missing">
        <p>{t("component.embedded_chat.presentation_content_is_not_available_yet")}</p>
        <span>{title}</span>
      </div>
    );
  }

  return (
    <div className="chat-output-ppt-viewer chat-output-ppt-viewer--canvas">
      <div ref={stageRef} className="chat-output-ppt-stage">
        <canvas
          ref={canvasRef}
          role="img"
          aria-label={`${title} — ${t("page.file_viewer.slide")} ${activeSlide + 1}`}
        />
        {loading && (
          <div className="chat-output-ppt-canvas-loading">
            <span className="chat-tool-spinner" />
          </div>
        )}
      </div>
      {slideCount > 1 && (
        <div className="chat-output-ppt-navigation">
          <button
            type="button"
            onClick={() => void renderSlide(Math.max(0, activeSlide - 1))}
            disabled={activeSlide === 0 || rendering}
            aria-label={t("page.doc_editor.previous_slide")}
          >
            <IconChevronLeft size={16} />
          </button>
          <span aria-live="polite">
            {t("page.file_viewer.slide")} {activeSlide + 1} {t("page.file_viewer.of")} {slideCount}
          </span>
          <button
            type="button"
            onClick={() =>
              void renderSlide(Math.min(slideCount - 1, activeSlide + 1))
            }
            disabled={activeSlide === slideCount - 1 || rendering}
            aria-label={t("page.doc_editor.next_slide")}
          >
            <IconChevronRight size={16} />
          </button>
        </div>
      )}
    </div>
  );
}

function PresentationArtifactViewer({
  artifact,
}: {
  artifact: OutputArtifact;
}) {
  const [slideUrls, setSlideUrls] = useState<string[]>([]);
  const [canvasUrl, setCanvasUrl] = useState<string | null>(null);
  const [activeSlide, setActiveSlide] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const previewAbortController = new AbortController();
    let objectUrls: string[] = [];
    const trackObjectUrl = (url: string) => {
      if (cancelled) URL.revokeObjectURL(url);
      else objectUrls.push(url);
      return url;
    };
    const revokeTrackedObjectUrls = (urls: string[]) => {
      urls.forEach((url) => {
        const index = objectUrls.indexOf(url);
        if (index < 0) return;
        objectUrls.splice(index, 1);
        URL.revokeObjectURL(url);
      });
    };

    async function loadPresentation() {
      setLoading(true);
      setSlideUrls([]);
      setCanvasUrl(null);
      setActiveSlide(0);
      try {
        const doc = await findDocumentForArtifact(artifact);
        if (!doc || cancelled) return;

        try {
          const slideData = await withArtifactPreviewTimeout(
            api.documents.getSlides(doc.id),
            8000,
            "Presentation preview timed out",
          );
          const token = getAuthToken();
          const headers: Record<string, string> = {};
          if (token) headers.Authorization = `Bearer ${token}`;
          const slideResults = await Promise.allSettled(
            (slideData.slides || []).map(async (slide) => {
              const res = await withArtifactPreviewTimeout(
                fetch(`/api/v1${slide.url}`, {
                  headers,
                  signal: previewAbortController.signal,
                }),
                8000,
                "Presentation preview timed out",
              );
              if (!res.ok) throw new Error("Slide fetch failed");
              const blob = await res.blob();
              return trackObjectUrl(URL.createObjectURL(blob));
            }),
          );
          const urls = slideResults.flatMap((result) =>
            result.status === "fulfilled" ? [result.value] : [],
          );
          const failedSlide = slideResults.find(
            (result) => result.status === "rejected",
          );
          if (failedSlide?.status === "rejected") {
            revokeTrackedObjectUrls(urls);
            throw failedSlide.reason;
          }
          if (urls.length === 0) throw new Error("No rendered slides");
          if (!cancelled) setSlideUrls(urls);
        } catch {
          previewAbortController.abort();
          revokeTrackedObjectUrls([...objectUrls]);
          if (cancelled) return;
          const url = trackObjectUrl(await api.documents.download(doc.id));
          if (cancelled) return;
          setCanvasUrl(url);
        }
      } catch {
        if (!cancelled) setCanvasUrl(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    loadPresentation();
    return () => {
      cancelled = true;
      previewAbortController.abort();
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
      objectUrls = [];
    };
  }, [
    artifact.body,
    artifact.data,
    artifact.href,
    artifact.id,
    artifact.meta,
    artifact.title,
  ]);

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

  if (canvasUrl) {
    return <PresentationArtifactCanvas url={canvasUrl} title={artifact.title} />;
  }

  return (
    <div className="chat-output-file-missing">
      <p>{t("component.embedded_chat.presentation_content_is_not_available_yet")}</p>
      <span>{artifact.body || artifact.title}</span>
    </div>
  );
}

function formatJsonArtifactContent(content: string) {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

const DOCX_PAGE_FETCH_CONCURRENCY = 3;

async function loadDocxFallbackRender(
  documentId: string,
  signal?: AbortSignal,
): Promise<ManorDocumentRender> {
  const response = await api.documents.downloadResponse(documentId, { signal });
  const blob = await response.blob();
  const buf = await blob.arrayBuffer();
  const rendered = await renderManorDocument(buf);
  const sanitizeOptions = {
    allowDocxEditorAttributes: true,
    allowDocxLayoutStyles: true,
  };
  return {
    ...rendered,
    html: sanitizeDocumentHtml(rendered.html, sanitizeOptions),
    headerHtml: sanitizeDocumentHtml(rendered.headerHtml, sanitizeOptions),
    footerHtml: sanitizeDocumentHtml(rendered.footerHtml, sanitizeOptions),
    firstHeaderHtml: sanitizeDocumentHtml(rendered.firstHeaderHtml, sanitizeOptions),
    firstFooterHtml: sanitizeDocumentHtml(rendered.firstFooterHtml, sanitizeOptions),
    evenHeaderHtml: sanitizeDocumentHtml(rendered.evenHeaderHtml, sanitizeOptions),
    evenFooterHtml: sanitizeDocumentHtml(rendered.evenFooterHtml, sanitizeOptions),
  };
}

function DocxFallbackArtifactViewer({
  artifact,
  render,
}: {
  artifact: OutputArtifact;
  render: ManorDocumentRender;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    root.innerHTML = render.html;
    paginateManorDocument(root, render);
  }, [render]);

  return (
    <div
      className="chat-output-docx-pages chat-output-docx-pages--native"
      aria-label={artifact.title}
    >
      <div
        ref={rootRef}
        className="docx-viewer-page manor-docx-native manor-docx-readonly"
        style={{
          "--docx-page-width": `${render.layout.pageWidthPx}px`,
          "--docx-page-height": `${render.layout.pageHeightPx}px`,
          "--docx-margin-top": `${render.layout.marginTopPx}px`,
          "--docx-margin-right": `${render.layout.marginRightPx}px`,
          "--docx-margin-bottom": `${render.layout.marginBottomPx}px`,
          "--docx-margin-left": `${render.layout.marginLeftPx}px`,
          "--docx-header-distance": `${render.layout.headerDistancePx}px`,
          "--docx-footer-distance": `${render.layout.footerDistancePx}px`,
        } as CSSProperties}
      />
    </div>
  );
}

function DocumentPageArtifactViewer({
  artifact,
  documentId,
  pages,
}: {
  artifact: OutputArtifact;
  documentId: string;
  pages: ArtifactDocumentPage[];
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [pageUrls, setPageUrls] = useState<Record<number, string>>({});
  const [fallbackRender, setFallbackRender] = useState<ManorDocumentRender | null>(null);
  const [fallbackLoading, setFallbackLoading] = useState(false);
  const [fallbackError, setFallbackError] = useState("");

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    let cancelled = false;
    let fallbackStarted = false;
    let activeFetches = 0;
    const pageController = new AbortController();
    const fallbackController = new AbortController();
    const queued = new Set<number>();
    const loaded = new Set<number>();
    const queue: number[] = [];
    const objectUrls = new Set<string>();
    const pagesByIndex = new Map(pages.map((page) => [page.index, page]));
    let observer: IntersectionObserver | null = null;

    setPageUrls({});
    setFallbackRender(null);
    setFallbackLoading(false);
    setFallbackError("");

    const revokePageUrls = () => {
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
      objectUrls.clear();
    };

    const startFallback = async (reason: unknown) => {
      if (cancelled || fallbackStarted) return;
      fallbackStarted = true;
      observer?.disconnect();
      pageController.abort();
      revokePageUrls();
      setPageUrls({});
      setFallbackLoading(true);
      try {
        const render = await loadDocxFallbackRender(
          documentId,
          fallbackController.signal,
        );
        if (!cancelled) setFallbackRender(render);
      } catch (error: any) {
        if (!cancelled) {
          setFallbackError(
            error?.message || (reason instanceof Error ? reason.message : "File preview failed"),
          );
        }
      } finally {
        if (!cancelled) setFallbackLoading(false);
      }
    };

    const pump = () => {
      if (cancelled || fallbackStarted) return;
      while (activeFetches < DOCX_PAGE_FETCH_CONCURRENCY && queue.length > 0) {
        const pageIndex = queue.shift();
        if (pageIndex === undefined) break;
        queued.delete(pageIndex);
        if (loaded.has(pageIndex)) continue;
        const page = pagesByIndex.get(pageIndex);
        if (!page) continue;
        activeFetches += 1;
        void (async () => {
          try {
            const token = getAuthToken();
            const headers: Record<string, string> = {};
            if (token) headers.Authorization = `Bearer ${token}`;
            const response = await withArtifactPreviewTimeout(
              fetch(`/api/v1${page.url}`, {
                headers,
                signal: pageController.signal,
              }),
              15_000,
              "Word preview timed out",
            );
            if (!response.ok) throw new Error("Word page fetch failed");
            const imageUrl = URL.createObjectURL(await response.blob());
            if (cancelled || fallbackStarted) {
              URL.revokeObjectURL(imageUrl);
              return;
            }
            objectUrls.add(imageUrl);
            loaded.add(pageIndex);
            setPageUrls((current) => ({ ...current, [pageIndex]: imageUrl }));
          } catch (error: any) {
            if (error?.name !== "AbortError") void startFallback(error);
          } finally {
            activeFetches -= 1;
            pump();
          }
        })();
      }
    };

    const enqueue = (pageIndex: number) => {
      if (
        cancelled
        || fallbackStarted
        || loaded.has(pageIndex)
        || queued.has(pageIndex)
      ) return;
      queued.add(pageIndex);
      queue.push(pageIndex);
      pump();
    };

    const pageElements = Array.from(
      root.querySelectorAll<HTMLElement>("[data-docx-page-index]"),
    );
    if (typeof IntersectionObserver === "undefined") {
      pages.forEach((page) => enqueue(page.index));
    } else {
      observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            const pageIndex = Number(
              (entry.target as HTMLElement).dataset.docxPageIndex,
            );
            if (Number.isInteger(pageIndex)) enqueue(pageIndex);
          });
        },
        { root, rootMargin: "900px 0px" },
      );
      pageElements.forEach((element) => observer?.observe(element));
    }

    return () => {
      cancelled = true;
      observer?.disconnect();
      pageController.abort();
      fallbackController.abort();
      revokePageUrls();
    };
  }, [documentId, pages]);

  if (fallbackLoading) {
    return (
      <div className="chat-output-file-loading">
        <span className="chat-tool-spinner" />
        <p>{t("component.embedded_chat.loading_file_preview")}</p>
      </div>
    );
  }

  if (fallbackError) {
    return (
      <div className="chat-output-file-missing">
        <p>{t("component.embedded_chat.file_preview_failed")}</p>
        <span>{fallbackError}</span>
      </div>
    );
  }

  if (fallbackRender) {
    return <DocxFallbackArtifactViewer artifact={artifact} render={fallbackRender} />;
  }

  return (
    <div
      ref={rootRef}
      className="chat-output-docx-pages"
      aria-label={artifact.title}
    >
      {pages.map((page) => {
        const imageUrl = pageUrls[page.index];
        return (
          <figure
            aria-busy={!imageUrl}
            className="chat-output-docx-page"
            data-docx-page-index={page.index}
            key={`${page.index}:${page.url}`}
            style={{
              aspectRatio: page.width && page.height
                ? `${page.width} / ${page.height}`
                : "8.5 / 11",
            }}
          >
            {imageUrl ? (
              <img
                src={imageUrl}
                alt={`${artifact.title} — page ${page.index + 1}`}
                draggable={false}
              />
            ) : (
              <div className="chat-output-docx-page-loading" aria-hidden="true">
                <span className="chat-tool-spinner" />
              </div>
            )}
          </figure>
        );
      })}
    </div>
  );
}

function FileArtifactViewer({ artifact }: { artifact: OutputArtifact }) {
  const [category, setCategory] = useState<ArtifactFileCategory | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [docxRender, setDocxRender] = useState<ManorDocumentRender | null>(null);
  const [docxPages, setDocxPages] = useState<ArtifactDocumentPage[]>([]);
  const [docxDocumentId, setDocxDocumentId] = useState("");
  const [sheets, setSheets] = useState<SpreadsheetSheetModel[]>([]);
  const [activeSheet, setActiveSheet] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const directUrl = artifact.href || artifact.body || "";
  const htmlPreview = useIsolatedHtmlPreview(
    content,
    category === "html" && Boolean(content),
  );

  useEffect(() => {
    let cancelled = false;
    let createdObjectUrl: string | null = null;
    const abortController = new AbortController();
    const docxPageAbortController = new AbortController();

    async function loadFile() {
      setLoading(true);
      setCategory(null);
      setObjectUrl(null);
      setContent("");
      setDocxRender(null);
      setDocxPages([]);
      setDocxDocumentId("");
      setSheets([]);
      setActiveSheet(0);
      setError("");
      try {
        const doc = await findDocumentForArtifact(artifact);
        if (!doc) {
          const fallbackCategory = artifact.kind === "diagram"
            ? "diagram"
            : inferArtifactKindFromPath(directUrl || artifact.title);
          if (fallbackCategory === "diagram") {
            let diagramSourceLoaded = false;
            const fsPath = generatedFileFsPath(artifact.data)
              || generatedFileFsPath({ fs_path: directUrl });
            if (isLocalFsUrl(directUrl)) {
              const response = await fetchProtectedFsResponse(directUrl, {
                signal: abortController.signal,
              });
              const rawContent = await readDiagramPreviewText(response);
              diagramSourceLoaded = true;
              if (!cancelled) setContent(rawContent);
            } else if (fsPath) {
              const info = await api.fs.info(fsPath);
              assertDiagramPreviewFileSize(info?.size);
              const result = await api.fs.read(fsPath);
              const rawContent = diagramPreviewTextFromFsRead(result);
              diagramSourceLoaded = true;
              if (!cancelled) setContent(rawContent);
            }
            if (!diagramSourceLoaded) {
              throw new Error("Artifact preview source is unavailable");
            }
            if (!cancelled) setCategory("diagram");
            return;
          }
          if (isLocalFsUrl(directUrl)) {
            const resolved = await resolveDisplayMediaUrl(directUrl);
            if (cancelled) {
              resolved.revoke();
              return;
            }
            createdObjectUrl = resolved.url;
            setObjectUrl(resolved.url);
          }
          if (!cancelled)
            setCategory(
              fallbackCategory === "presentation"
                ? "unsupported"
                : (fallbackCategory as ArtifactFileCategory),
            );
          return;
        }
        if (cancelled) return;

        const nextCategory = detectArtifactFileCategory(doc);
        if (!cancelled) setCategory(nextCategory);

        if (nextCategory === "diagram") {
          assertDiagramPreviewFileSize(doc.file_size);
          const response = await api.documents.downloadResponse(doc.id, {
            signal: abortController.signal,
          });
          const rawContent = await readDiagramPreviewText(response);
          if (!cancelled) setContent(rawContent);
          return;
        }

        if (
          [
            "text",
            "markdown",
            "code",
            "html",
            "csv",
            "json",
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
          if (nextCategory === "xlsx") {
            const blob = await api.documents.downloadBlob(doc.id);
            if (cancelled) return;
            const buf = await blob.arrayBuffer();
            if (cancelled) return;
            const XLSX = await import("xlsx");
            if (cancelled) return;
            const wb = XLSX.read(buf, {
              type: "array",
              cellFormula: true,
              cellNF: true,
              cellStyles: true,
              cellText: true,
            });
            const chartsBySheet = await spreadsheetChartsFromFile(buf, XLSX, wb);
            if (cancelled) return;
            const parsedSheets = spreadsheetSheetsFromWorkbook(XLSX, wb)
              .map((sheet) => ({ ...sheet, charts: chartsBySheet.get(sheet.name) || [] }))
              .filter((sheet) => !sheet.hidden && sheet.name !== "_manor_charts");
            setSheets(parsedSheets);
            return;
          }

          if (nextCategory === "docx") {
            try {
              const pageData = await api.documents.getPages(
                doc.id,
                docxPageAbortController.signal,
              );
              const pages = pageData.pages || [];
              if (pages.length === 0) throw new Error("No rendered Word pages");
              if (!cancelled) {
                setDocxDocumentId(doc.id);
                setDocxPages(pages);
              }
              return;
            } catch {
              if (cancelled) return;
            }

            const render = await loadDocxFallbackRender(
              doc.id,
              abortController.signal,
            );
            if (!cancelled) setDocxRender(render);
            return;
          }

          const downloadedUrl = await api.documents.download(doc.id);
          if (cancelled) {
            if (downloadedUrl.startsWith("blob:")) URL.revokeObjectURL(downloadedUrl);
            return;
          }
          createdObjectUrl = downloadedUrl;
          setObjectUrl(downloadedUrl);
        }
      } catch (err: any) {
        if (createdObjectUrl?.startsWith("blob:")) {
          URL.revokeObjectURL(createdObjectUrl);
          createdObjectUrl = null;
        }
        if (!cancelled) setError(err?.message || "File preview failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    loadFile();
    return () => {
      cancelled = true;
      abortController.abort();
      docxPageAbortController.abort();
      if (createdObjectUrl) URL.revokeObjectURL(createdObjectUrl);
    };
  }, [
    artifact.body,
    artifact.data,
    artifact.href,
    artifact.id,
    artifact.kind,
    artifact.meta,
    artifact.title,
  ]);

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
        <IsolatedHtmlPreviewFrame
          title={artifact.title}
          preview={htmlPreview}
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

  if (category === "diagram") {
    return (
      <Suspense
        fallback={(
          <div className="chat-output-file-loading">
            <span className="chat-tool-spinner" />
            <p>{t("component.embedded_chat.loading_file_preview")}</p>
          </div>
        )}
      >
        <LazyDiagramArtifactViewer
          content={content}
          title={artifact.title}
          fileType={artifact.data?.file_type || artifact.data?.document?.file_type}
        />
      </Suspense>
    );
  }

  if (
    (category === "text" ||
      category === "code" ||
      category === "json") &&
    content
  ) {
    return (
      <pre className="chat-output-code-block chat-output-code-block--light">
        <code>
          {category === "json" ? formatJsonArtifactContent(content) : content}
        </code>
      </pre>
    );
  }

  if (category === "csv" && content) {
    const rows = parseDelimitedText(content).rows;
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

  if (category === "docx" && docxDocumentId && docxPages.length > 0) {
    return (
      <DocumentPageArtifactViewer
        artifact={artifact}
        documentId={docxDocumentId}
        pages={docxPages}
      />
    );
  }

  if (category === "docx" && docxRender) {
    return <DocxFallbackArtifactViewer artifact={artifact} render={docxRender} />;
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
          <table style={{ width: "max-content", minWidth: "100%", tableLayout: "fixed" }}>
            <colgroup>
              <col style={{ width: 44 }} />
              {sheet.columnWidths.map((width, column) => <col key={column} style={{ width }} />)}
            </colgroup>
            <thead>
              <tr>
                <th aria-label="Row number">#</th>
                {sheet.columnWidths.map((_width, column) => (
                  <th key={column}>{artifactSpreadsheetColumnLabel(column)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sheet.data.map((row, rowIndex) => (
                <tr key={rowIndex} style={{ height: sheet.rowHeights[rowIndex] || 32 }}>
                  <th scope="row" style={{ color: "var(--text-faint)", textAlign: "center", fontWeight: 600 }}>
                    {rowIndex + 1}
                  </th>
                  {sheet.columnWidths.map((_width, cellIndex) => {
                    const merge = spreadsheetMergeAt(sheet.merges, rowIndex, cellIndex);
                    if (merge.covered) return null;
                    const raw = row[cellIndex];
                    const display = sheet.displayData[rowIndex]?.[cellIndex] ?? String(raw ?? "");
                    const style = sheet.styles[`${rowIndex}:${cellIndex}`] || {};
                    return (
                      <td
                        key={cellIndex}
                        rowSpan={merge.rowSpan}
                        colSpan={merge.columnSpan}
                        title={typeof raw === "string" && raw.startsWith("=") ? raw : undefined}
                        style={{
                          color: style.color || "var(--text-default)",
                          background: style.fill || "var(--surface-panel)",
                          fontWeight: style.bold ? 700 : 400,
                          fontStyle: style.italic ? "italic" : "normal",
                          fontFamily: style.fontFamily,
                          fontSize: style.fontSize,
                          textAlign: style.align,
                          whiteSpace: "pre-wrap",
                          overflowWrap: "anywhere",
                        }}
                      >
                        {display}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {sheet.charts.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <SpreadsheetChartPreview charts={sheet.charts} />
          </div>
        )}
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
      try {
        const nextDoc = await findDocumentForArtifact(artifact);
        if (!cancelled) setDoc(nextDoc);
      } catch {
        if (!cancelled) setDoc(null);
      }
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

export function ArtifactViewer({
  artifact,
  updating = false,
}: {
  artifact: OutputArtifact;
  updating?: boolean;
}) {
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
      (Boolean(artifactDocumentId(artifact)) ||
        looksLikeFileReference(artifact.body) ||
        looksLikeFileReference(artifact.meta))) ||
    (artifact.kind === "code" &&
      (Boolean(artifactDocumentId(artifact)) ||
        looksLikeFileReference(artifact.body) ||
        looksLikeFileReference(artifact.meta)));
  const canToggleCode =
    artifact.kind === "page" && Boolean(htmlSource) && !isFileBackedArtifact;
  const htmlPreview = useIsolatedHtmlPreview(htmlSource, canToggleCode);

  if (artifact.kind === "workspace" && artifact.data?.draft_id) {
    return (
      <WorkspaceDraftConfigurationPanel
        draftId={String(artifact.data.draft_id)}
        updating={updating}
        refreshKey={JSON.stringify({
          status: artifact.data.status,
          ready: artifact.data.ready,
          missing: artifact.data.missing,
          fields: artifact.data.fields,
        })}
      />
    );
  }

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
              <IsolatedHtmlPreviewFrame
                title={artifact.title}
                preview={htmlPreview}
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

export function OutputPanel({
  artifact,
  updating,
  onStop,
  returnTo,
}: {
  artifact?: OutputArtifact | null;
  updating: boolean;
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
        <ArtifactViewer artifact={artifact} updating={updating} />
      )}
    </aside>
  );
}

export function ArtifactSummaryCards({
  artifacts,
  onOpen,
}: {
  artifacts: OutputArtifact[];
  onOpen: (artifact: OutputArtifact) => void;
}) {
  const toast = useToastStore();
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
              } catch (error) {
                toast.error(
                  t("component.embedded_chat.artifact_download_failed"),
                  error instanceof Error ? error.message : undefined,
                );
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
  onConversationResolved,
  onNewConversation,
  showWorkspaceIntro = false,
}: EmbeddedChatProps) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const location = useLocation();
  const navigate = useNavigate();
  const currentUser = useAuthStore((s) => s.user);
  const currentUserName =
    currentUser?.display_name ||
    currentUser?.first_name ||
    currentUser?.email ||
    "You";
  const currentUserAvatar = currentUser?.avatar_url;
  const isAgentConversation = Boolean(agentId && !isMasterAgent(agentId));

  const [input, setInput] = useState("");
  const [composerSeed, setComposerSeed] = useState<{
    key: string;
    attachments: AttachedItem[];
  } | null>(null);
  const [remixingChatModeSampleTitle, setRemixingChatModeSampleTitle] =
    useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const composerEditorRef = useRef<HTMLDivElement>(null);
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
  const [ideaComposer, setIdeaComposer] = useState<IdeaComposerSelection | null>(
    { mode: "new-idea", origin: "default" },
  );
  const ideaComposerMode = ideaComposer?.mode ?? null;
  const [chatMode, setChatMode] = useState<ChatBoxMode>("auto");
  const chatModeRef = useRef(chatMode);
  chatModeRef.current = chatMode;
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
  const { data: globalFlowEntrypoints = [] } = useQuery({
    queryKey: ["global-chat-flow-entrypoints"],
    queryFn: () => api.chat.listFlowEntrypoints(),
  });
  const workflowInvokeOptions = useMemo<WorkflowInvokeItem[]>(
    () =>
      globalFlowEntrypoints.map((entrypoint: GlobalChatFlowEntrypoint) => ({
        bindingId: entrypoint.binding_id,
        workflowId: entrypoint.workflow_id,
        title: entrypoint.title,
        description: [entrypoint.workspace_name, entrypoint.description]
          .filter(Boolean)
          .join(" · "),
        placeholder: entrypoint.placeholder,
      })),
    [globalFlowEntrypoints],
  );

  // Persistent per-session stream store — survives route transitions.
  const initialPropConvId =
    conversationId === "manor-ai" ||
    isVirtualAgentConversationId(conversationId) ||
    isNewManorConversationId(conversationId)
      ? undefined
      : conversationId;
  const initialStreamState = useMemo(() => useChatStreamStore.getState(), []);
  const initialSession = isNewManorConversationId(conversationId)
    ? undefined
    : initialPropConvId
      ? initialStreamState.sessions[initialPropConvId]
      : initialStreamState.latestSessionKey
        ? initialStreamState.sessions[initialStreamState.latestSessionKey]
        : undefined;
  const shouldLoadInitialConversation =
    !initialSession?.messages?.length &&
    (Boolean(initialPropConvId) ||
      conversationId === "manor-ai" ||
      (isVirtualAgentConversationId(conversationId) && Boolean(agentId)));
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
  const responseSurfaceSubmissionReceipts = useMemo(
    () => collectResponseSurfaceSubmissionReceipts(messages),
    [messages],
  );
  const responseSurfaceSubmissionFailureMessageIds = useMemo(
    () => collectResponseSurfaceSubmissionFailureMessageIds(
      messages,
      responseSurfaceSubmissionReceipts,
    ),
    [messages, responseSurfaceSubmissionReceipts],
  );
  /*
   * A run this tab only watches. Deliberately NOT the store's `streaming`,
   * which means "this tab owns an SSE connection and an AbortController": that
   * flag gates the stop button, the send guard and every REST reload, and none
   * of those are true here. This one only drives the "still working"
   * affordances.
   */
  const [followedRunActive, setFollowedRunActive] = useState(false);
  /* Bumped by every applied snapshot; restarts the silence watchdog below. */
  const [followedRunSilenceKey, setFollowedRunSilenceKey] = useState(0);
  const lastSnapshotSeqRef = useRef<Record<string, number>>({});
  const snapshotRefetchedRef = useRef<string | undefined>(undefined);
  const followedRunPollsRef = useRef(0);
  const remoteRunInFlight =
    !streaming && (followedRunActive || hasActivePersistedChatStream(messages));
  const assistantWorking = streaming || remoteRunInFlight;
  const activeAgentActivity = inferAgentActivity(
    assistantWorking
      ? [...messages].reverse().find((message) => message.role === "assistant")
      : undefined,
  );
  const streamingConvId = currentSession?.convId;
  const runtimeQueueStatus = currentSession?.runtimeQueue
    ? formatRuntimeQueueStatus(currentSession.runtimeQueue)
    : null;
  const {
    hydrateConversation: hydrateMessageFeedback,
    submit: submitMessageFeedback,
    values: messageFeedback,
  } = useChatMessageFeedback(currentUser?.id);
  useEffect(() => {
    const conversationId = currentConvId || streamingConvId;
    if (!conversationId) return;
    void hydrateMessageFeedback(conversationId).catch(() => {});
  }, [currentConvId, hydrateMessageFeedback, streamingConvId]);
  const [messageHistoryState, setMessageHistoryState] = useState<
    Record<string, { hasMore: boolean; nextCursor: string | null }>
  >({});
  const [loadingOlderMessages, setLoadingOlderMessages] = useState(false);
  const [conversationLoading, setConversationLoading] = useState(shouldLoadInitialConversation);
  const setSessionMessages = useChatStreamStore((s) => s.setSessionMessages);
  const createDraftSession = useChatStreamStore((s) => s.createDraftSession);
  const startStream = useChatStreamStore((s) => s.startStream);
  const stopStream = useChatStreamStore((s) => s.stopStream);
  const streamingRef = useRef(false);
  const sendPreflightInFlightRef = useRef(false);
  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);
  const currentSessionKeyRef = useRef<string | undefined>(currentSessionKey);
  useEffect(() => {
    currentSessionKeyRef.current = currentSessionKey;
  }, [currentSessionKey]);
  const showConversationSkeleton = conversationLoading && messages.length === 0;
  const didInitialScrollRef = useRef(false);
  const suppressNextAutoScrollRef = useRef(false);
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
  const workflowRunGroups = useMemo(
    () => buildWorkspaceWorkflowRunGroups(messages),
    [messages],
  );
  const hostOwnedWorkflowMessageIds = useMemo(
    () => workflowHostOwnedMessageIds(workflowRunGroups),
    [workflowRunGroups],
  );
  const visibleWorkflowMessageEntries = useMemo(
    () => messages.flatMap((message, index) => (
      isResponseSurfaceSubmissionMessage(message, responseSurfaceSubmissionReceipts)
        || Boolean(message.id && responseSurfaceSubmissionFailureMessageIds.has(message.id))
        || (message.id && hostOwnedWorkflowMessageIds.has(message.id))
        ? []
        : [{ message, index }]
    )),
    [
      hostOwnedWorkflowMessageIds,
      messages,
      responseSurfaceSubmissionFailureMessageIds,
      responseSurfaceSubmissionReceipts,
    ],
  );
  const visibleWorkflowMessages = useMemo(
    () => visibleWorkflowMessageEntries.map(({ message }) => message),
    [visibleWorkflowMessageEntries],
  );
  const workflowMessageResolveMutation = useMutation({
    mutationFn: ({
      messageId,
      choice,
      note,
      payload,
      files,
    }: {
      messageId: string;
      choice: string;
      note?: string;
      payload?: Record<string, unknown>;
      files?: File[];
    }) => resolveGlobalWorkflowMessageAction(
      messageId,
      choice,
      note,
      payload,
      files,
    ),
    onSuccess: (resolved, { messageId }) => {
      setMessages((current) => mergeResolvedWorkflowMessage(
        current,
        messageId,
        resolved,
      ));
    },
  });
  const handleWorkflowMessageResolve = (
    messageId: string,
    choice: string,
    note?: string,
    payload?: Record<string, unknown>,
    files?: File[],
  ) => workflowMessageResolveMutation.mutateAsync({
    messageId,
    choice,
    note,
    payload,
    files,
  });
  const outputState = useMemo(
    () => deriveOutputState(messages, assistantWorking),
    [messages, assistantWorking],
  );
  const activeOutputArtifact = useMemo(() => {
    if (!selectedArtifact) return outputState.artifacts[0];
    const selectedKey = artifactDedupKey(selectedArtifact);
    return (
      outputState.artifacts.find(
        (artifact) => artifactDedupKey(artifact) === selectedKey,
      ) || selectedArtifact
    );
  }, [outputState.artifacts, selectedArtifact]);
  const messageArtifacts = useMemo(() => {
    const artifactGroups = keepLatestWorkspaceDraftArtifacts(
      messages.map((message, index) =>
        deriveMessageArtifacts(
          message,
          assistantWorking && index === messages.length - 1,
        ),
      ),
    );
    return artifactGroups.map((artifacts, index) => {
      const message = messages[index];
      const streaming = assistantWorking && index === messages.length - 1;
      const localCodingNotice =
        message?.role === "assistant"
          ? maybeLocalCodingRunNoticeForTools(
              visibleToolCallsForApprovalMessage(message),
            )
          : null;
      return filterMessageArtifactsAlreadyRepresented(
        message,
        artifacts,
        assistantMessageRendersInlineFileSurfaces(message, streaming),
        {
          renderedContent: localCodingNotice ?? undefined,
          streaming,
        },
      );
    });
  }, [messages, assistantWorking]);

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

  useEffect(() => {
    const handleInsertChatPrompt = (event: Event) => {
      const detail =
        (event as CustomEvent<InsertChatComposerDetail>).detail || {};
      const prompt = (detail.prompt || "").trim();
      if (!prompt) return;
      setInput((prev) => {
        const current = prev.trim();
        return current ? `${prev.trimEnd()}\n\n${prompt}` : prompt;
      });
      window.setTimeout(() => textareaRef.current?.focus(), 0);
    };

    window.addEventListener(
      INSERT_CHAT_COMPOSER_EVENT,
      handleInsertChatPrompt as EventListener,
    );
    return () =>
      window.removeEventListener(
        INSERT_CHAT_COMPOSER_EVENT,
        handleInsertChatPrompt as EventListener,
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
    setIdeaComposer(null);
    setChatMode(mode);
    setChatModePayload(getDefaultChatModePayload(mode));
    if (mode === "auto") {
      setActiveCapability("workspace");
    } else if (mode === "document") {
      setActiveCapability("docs");
    } else if (mode === "pdf") {
      setActiveCapability("pdf");
    } else if (mode === "sheet") {
      setActiveCapability("sheets");
    } else if (
      mode === "slides" ||
      mode === "website" ||
      mode === "image" ||
      mode === "video" ||
      mode === "research" ||
      mode === "flows"
    ) {
      setActiveCapability(mode);
    }
  }, []);

  const resetChatModeAfterTurn = useCallback(() => {
    setIdeaComposer(null);
    setChatMode("auto");
    setChatModePayload(getDefaultChatModePayload("auto"));
    setActiveCapability("workspace");
  }, []);

  const handleSampleSelect = useCallback((
    sample: WorkspaceSample | ChatModeTemplateSample,
    templateAttachment?: AttachedItem,
  ) => {
    setIdeaComposer(null);
    const remixPrompt = buildTemplateRemixPrompt(
      sample,
      "",
      templateAttachment,
    );
    setInput(remixPrompt);
    if (templateAttachment) {
      setComposerSeed({
        key: `artifact-template-${templateAttachment.id || Date.now()}-${Date.now()}`,
        attachments: [templateAttachment],
      });
    }
    const sampleChatModePayloadPatch =
      "chatModePayloadPatch" in sample ? sample.chatModePayloadPatch : undefined;
    if (sampleChatModePayloadPatch) {
      setChatModePayload((current) => ({
        ...current,
        ...sampleChatModePayloadPatch,
      }));
    }
    window.setTimeout(() => composerEditorRef.current?.focus(), 0);
  }, []);

  const handleChatModeTemplateSelect = useCallback(
    async (sample: ChatModeTemplateSample): Promise<void> => {
      if (remixingChatModeSampleTitle) return;
      setRemixingChatModeSampleTitle(sample.title);
      try {
        const templateAttachment = await uploadTemplateRemixSource(sample);
        if (templateAttachment) {
          await invalidateKnowledgeQueries(queryClient);
        }
        handleSampleSelect(sample, templateAttachment);
        toast.success(
          t("component.embedded_chat.template_ready").replace(
            "{name}",
            sample.title,
          ),
        );
      } catch (error) {
        toast.error(
          t("component.embedded_chat.template_create_failed"),
          error instanceof Error ? error.message : undefined,
        );
      } finally {
        setRemixingChatModeSampleTitle(null);
      }
    },
    [
      handleSampleSelect,
      queryClient,
      remixingChatModeSampleTitle,
      toast,
    ],
  );

  // Resolved agent ID for stream calls (DM prop takes priority over @mention selection)
  const resolvedAgentId = agentId || mentionedAgent?.id || selectedAgent?.id;

  const chatBodyRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const { autoFollowRef, handleAutoFollowScroll } = useChatAutoFollow();
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

  const chatScrollRailMarkers = useMemo<ChatScrollRailMarker[]>(
    () => {
      const sources = messages.map<ChatScrollRailTurnSource>((msg, index) => {
        const fallbackTitle =
          msg.role === "user" ? currentUserName || "You" : title || "Manor AI";
        const rawContent =
          msg.role === "assistant"
            ? displayContentForAssistantMessage(msg, toDisplayText(msg.content) || "")
            : stripAttachedLine(msg.content);
        const preview = chatRailPreviewFromText(rawContent, fallbackTitle);
        const artifact = messageArtifacts[index]?.[0];
        const attachment =
          Array.isArray(msg.attachments) && msg.attachments.length > 0
            ? msg.attachments[0]
            : null;
        const attachmentRecord = attachment as Record<string, unknown> | null;
        const fileLabel =
          artifact?.title ||
          String(
            attachmentRecord?.name ||
              attachmentRecord?.filename ||
              attachmentRecord?.title ||
              "",
          ) ||
          "";
        const fileKindSource =
          artifact?.kind ||
          (fileLabel.includes(".") ? fileLabel.split(".").pop() : "") ||
          "file";
        return {
          id: chatMessageAnchorId(msg.id, index),
          sourceIndex: index,
          role: msg.role,
          tone: msg.role === "user" ? "user" : "assistant",
          title: preview.title,
          excerpt: preview.excerpt,
          text: rawContent,
          fileKind: String(fileKindSource).slice(0, 4).toUpperCase(),
          fileLabel,
        };
      });
      return buildChatScrollRailTurnMarkers(sources);
    },
    [currentUserName, messageArtifacts, messages, title],
  );

  const closeOutputPanel = useCallback(() => {
    setOutputOpen(false);
    setSelectedArtifactAnchor(null);
  }, []);

  const mapMessages = useCallback(
    (msgs: any[]) =>
      msgs
        .filter(
          (m: any) =>
            !(m.role === "user" && isInternalFilePermissionMessage(m.content)) &&
            !isRedundantApprovalResolutionReceipt(m),
        )
        .map((m: any) => ({
          id: m.id,
          conversation_id: m.conversation_id,
          message_kind: m.message_kind,
          refs: m.refs,
          meta: m.meta,
          pending_action: m.pending_action,
          resolved_at: m.resolved_at,
          resolution: m.resolution,
          // Workflow progress and HITL rows are persisted as system messages.
          // They belong to Manor, never to the signed-in user.
          role: (m.role === "user" ? "user" : "assistant") as "user" | "assistant",
          content: toDisplayText(m.content) || "",
          tool_calls: parseToolCalls(m.tool_calls),
          assistant_blocks: Array.isArray(m.assistant_blocks) ? m.assistant_blocks : undefined,
          hitl_requests: Array.isArray(m.hitl_requests) ? m.hitl_requests : undefined,
          workflow_result:
            m.workflow_result && typeof m.workflow_result === "object"
              ? m.workflow_result
              : undefined,
          attachments: Array.isArray(m.attachments) ? m.attachments : undefined,
          stop_reason: m.stop_reason,
          limit_detail: m.limit_detail,
          timestamp: m.created_at,
          updated_at: m.updated_at,
        })),
    [],
  );

  const mergeOlderMessages = useCallback(
    (olderMessages: ChatMessage[], existingMessages: ChatMessage[]) => {
      const seen = new Set(
        existingMessages
          .map((message, index) => message.id || `${message.role}:${message.timestamp || index}:${message.content}`)
          .filter(Boolean),
      );
      const older = olderMessages.filter((message, index) => {
        const key = message.id || `${message.role}:${message.timestamp || index}:${message.content}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      return [...older, ...existingMessages];
    },
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
    async (
      convId: string,
      options: {
        fallbackToAgent?: boolean;
        allowSettledStreamRefresh?: boolean;
        backgroundRefresh?: boolean;
        /*
         * Background refreshes (snapshot terminal edge, silence watchdog) set
         * this: their success path must not call setCurrentConvId, or a stale
         * response resolving after the user switched conversations yanks the
         * view back while the URL still points at the new one.
         */
        onlyIfStillCurrent?: boolean;
        messageId?: string;
      } = {},
    ) => {
      const staleForCurrentView = () =>
        options.onlyIfStillCurrent && currentSessionKeyRef.current !== convId;
      if (shouldIgnoreLocallyStoppedStreamUpdate(convId, options.messageId)) {
        return;
      }
      if (!options.backgroundRefresh) setConversationLoading(true);
      try {
        const page = await api.chat.getMessagesPage(convId, {
          silent: true,
          limit: CHAT_MESSAGE_PAGE_SIZE,
        });
        const streamState = useChatStreamStore.getState();
        const targetSessionKey = streamState.getSessionKeyForConversation(convId);
        const targetTransportStreaming = Boolean(
          targetSessionKey && streamState.sessions[targetSessionKey]?.streaming,
        );
        if (targetTransportStreaming && !options.allowSettledStreamRefresh) return;
        if (staleForCurrentView()) return;
        if (shouldIgnoreLocallyStoppedStreamUpdate(convId, options.messageId)) return;
        const mappedMessages = mapMessages(page.items || []);
        setSessionMessages(convId, mappedMessages);
        setMessageHistoryState((prev) => ({
          ...prev,
          [convId]: {
            hasMore: Boolean(page.has_more),
            nextCursor: page.next_cursor || null,
          },
        }));
        setCurrentConvId(convId);
        setDraftSessionKey(undefined);
        return mappedMessages;
      } catch (err) {
        const isMissing = err instanceof ApiError && err.status === 404;
        if (options.backgroundRefresh && !isMissing) return;
        if (isMissing && options.fallbackToAgent && agentId) {
          const fallbackConvId = await resolveLatestAgentConversationId().catch(
            () => undefined,
          );
          if (fallbackConvId && fallbackConvId !== convId) {
            try {
              const page = await api.chat.getMessagesPage(fallbackConvId, {
                silent: true,
                limit: CHAT_MESSAGE_PAGE_SIZE,
              });
              const streamState = useChatStreamStore.getState();
              const targetSessionKey =
                streamState.getSessionKeyForConversation(fallbackConvId);
              const targetTransportStreaming = Boolean(
                targetSessionKey && streamState.sessions[targetSessionKey]?.streaming,
              );
              if (targetTransportStreaming && !options.allowSettledStreamRefresh) return;
              if (staleForCurrentView()) return;
              const mappedMessages = mapMessages(page.items || []);
              setSessionMessages(fallbackConvId, mappedMessages);
              setMessageHistoryState((prev) => ({
                ...prev,
                [fallbackConvId]: {
                  hasMore: Boolean(page.has_more),
                  nextCursor: page.next_cursor || null,
                },
              }));
              setCurrentConvId(fallbackConvId);
              setDraftSessionKey(undefined);
              window.dispatchEvent(
                new CustomEvent("manor:dm-conversation-resolved", {
                  detail: { agentId, conversationId: fallbackConvId },
                }),
              );
              return mappedMessages;
            } catch {
              // Fall through to the clean empty DM state below.
            }
          }
        }
        if (streamingRef.current && !options.allowSettledStreamRefresh) return;
        if (staleForCurrentView()) return;
        /*
         * Only a 404 means the conversation is gone. Any other failure is the
         * request, not the data — and the silence watchdog fires precisely
         * when connectivity is broken, so wiping here would blank a transcript
         * the user is actively reading over a transient network error.
         */
        if (!isMissing) return;
        setCurrentConvId(undefined);
        setDraftSessionKey(undefined);
        setMessageHistoryState((prev) => ({
          ...prev,
          [convId]: { hasMore: false, nextCursor: null },
        }));
        if (isMissing && agentId) {
          window.dispatchEvent(
            new CustomEvent("manor:dm-conversation-resolved", {
              detail: { agentId, conversationId: null },
            }),
          );
        }
      } finally {
        if (!options.backgroundRefresh) setConversationLoading(false);
      }
    },
    [
      agentId,
      mapMessages,
      resolveLatestAgentConversationId,
      setSessionMessages,
      setMessageHistoryState,
    ],
  );

  const currentHistoryState = currentConvId
    ? messageHistoryState[currentConvId]
    : undefined;

  /*
   * Follow a turn this tab is not streaming.
   *
   * A personal conversation streams over the SSE body of the POST that started
   * it, so a reloaded page has no connection to a turn still running. The
   * snapshot is the only thing that moves the page after that reload.
   *
   * Ignored when this tab owns the stream (its SSE reducer appends onto the
   * same row, so an outside write duplicates text); or when the publish raced
   * and arrived out of order.
   *
   * A snapshot only projects the live turn. The stored row is the authority for
   * everything that settles at the end — attachments, approval cards, the
   * message kind — so the terminal edge refetches instead of trusting it. A
   * turn this transcript has never seen (sent from another tab) refetches too,
   * to pick up its user message.
   */
  useEffect(() => {
    if (!currentConvId) return;
    const handler = (event: Event) => {
      const snapshot = (event as CustomEvent).detail as ChatStreamSnapshot | undefined;
      if (!snapshot || snapshot.conversation_id !== currentConvId) return;
      const streamState = useChatStreamStore.getState();
      const liveKey = streamState.getSessionKeyForConversation(currentConvId);
      if (liveKey && streamState.sessions[liveKey]?.streaming) return;
      if (hasLocallyStreamedConversation(currentConvId)) return;
      if (
        shouldIgnoreLocallyStoppedStreamUpdate(
          currentConvId,
          snapshot.message_id,
          snapshot.status,
        )
      ) {
        setFollowedRunActive(false);
        followedRunPollsRef.current = 0;
        return;
      }

      const seqKey = snapshot.message_id || currentConvId;
      const seq = typeof snapshot.seq === "number" ? snapshot.seq : 0;
      if (seq && seq <= (lastSnapshotSeqRef.current[seqKey] || 0)) return;
      lastSnapshotSeqRef.current[seqKey] = seq;

      const known = liveKey ? streamState.sessions[liveKey]?.messages || [] : [];
      const needsHistory = streamSnapshotNeedsHistory(known, snapshot);
      setSessionMessages(currentConvId, (prev) =>
        mergeChatStreamSnapshot(prev, snapshot),
      );
      const terminal = isTerminalStreamSnapshot(snapshot);
      setFollowedRunActive(!terminal);
      setFollowedRunSilenceKey((value) => value + 1);
      followedRunPollsRef.current = 0;

      const refetchKey = `${snapshot.message_id || ""}:${terminal ? "final" : "history"}`;
      if ((terminal || needsHistory) && snapshotRefetchedRef.current !== refetchKey) {
        snapshotRefetchedRef.current = refetchKey;
        // No allowSettledStreamRefresh: if the reader starts their own turn
        // while this is in flight, the loader's guard must win over us.
        loadConversationMessages(currentConvId, {
          fallbackToAgent: Boolean(agentId),
          onlyIfStillCurrent: true,
        }).catch(() => {});
      }
    };
    window.addEventListener("manor:chat-stream-snapshot", handler);
    return () =>
      window.removeEventListener("manor:chat-stream-snapshot", handler);
  }, [agentId, currentConvId, loadConversationMessages, setSessionMessages]);

  /*
   * Watchdog for a followed run that goes quiet.
   *
   * The transport has no replay and no sequence recovery: snapshots published
   * during a socket reconnect are gone, and a lost terminal edge would leave
   * the reader watching a spinner over a reply that finished minutes ago. Every
   * silent interval, ask the API instead — the stored row settles the question,
   * and its stream_status re-arms this timer if the turn really is still going.
   */
  useEffect(() => {
    if (!currentConvId || !remoteRunInFlight) return;
    const timer = window.setTimeout(() => {
      if (shouldIgnoreLocallyStoppedStreamUpdate(currentConvId)) {
        setFollowedRunActive(false);
        return;
      }
      if (followedRunPollsRef.current >= FOLLOWED_RUN_MAX_POLLS) {
        // A row can claim "streaming" forever if its API process was
        // hard-killed (the sweeper only runs at startup). Give up on the
        // affordance rather than poll for the life of the tab.
        setFollowedRunActive(false);
        return;
      }
      followedRunPollsRef.current += 1;
      loadConversationMessages(currentConvId, {
        fallbackToAgent: Boolean(agentId),
        onlyIfStillCurrent: true,
      })
        .then(() => {
          setFollowedRunActive(false);
          // Re-arm: if the refetched row still claims to be streaming (the
          // meta path keeps remoteRunInFlight true without any dep changing),
          // the next silent interval must check again. A failed poll lands
          // here too — the loader swallows transient errors and keeps the
          // transcript — which is exactly the retry we want while offline.
          setFollowedRunSilenceKey((value) => value + 1);
        })
        .catch(() => {
          setFollowedRunSilenceKey((value) => value + 1);
        });
    }, FOLLOWED_RUN_SILENCE_MS);
    return () => window.clearTimeout(timer);
  }, [
    agentId,
    currentConvId,
    followedRunSilenceKey,
    loadConversationMessages,
    remoteRunInFlight,
  ]);

  /* This tab streaming for itself ends any followed run. */
  useEffect(() => {
    if (streaming) setFollowedRunActive(false);
  }, [streaming]);

  /* A different conversation is a different run — never inherit the last one's state. */
  useEffect(() => {
    setFollowedRunActive(false);
    lastSnapshotSeqRef.current = {};
    snapshotRefetchedRef.current = undefined;
    followedRunPollsRef.current = 0;
  }, [currentConvId]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (
        detail?.conversation_id === currentConvId &&
        !streamingRef.current
      ) {
        const messageId =
          typeof detail?.message_id === "string" ? detail.message_id : undefined;
        if (shouldIgnoreLocallyStoppedStreamUpdate(currentConvId, messageId)) {
          return;
        }
        loadConversationMessages(detail.conversation_id, {
          fallbackToAgent: Boolean(agentId),
          backgroundRefresh: true,
          messageId,
        }).catch(() => {});
      }
    };
    window.addEventListener("manor:conversation-message", handler);
    return () => window.removeEventListener("manor:conversation-message", handler);
  }, [agentId, currentConvId, loadConversationMessages]);

  const handleLoadOlderMessages = useCallback(async () => {
    if (
      !currentConvId ||
      !currentHistoryState?.hasMore ||
      !currentHistoryState.nextCursor ||
      loadingOlderMessages ||
      streamingRef.current
    ) {
      return;
    }
    const container = chatBodyRef.current;
    const previousHeight = container?.scrollHeight || 0;
    const previousTop = container?.scrollTop || 0;
    setLoadingOlderMessages(true);
    try {
      const page = await api.chat.getMessagesPage(currentConvId, {
        silent: true,
        limit: CHAT_MESSAGE_PAGE_SIZE,
        before: currentHistoryState.nextCursor,
      });
      const olderMessages = mapMessages(page.items || []);
      suppressNextAutoScrollRef.current = true;
      setSessionMessages(currentConvId, (prev) =>
        mergeOlderMessages(olderMessages, prev),
      );
      setMessageHistoryState((prev) => ({
        ...prev,
        [currentConvId]: {
          hasMore: Boolean(page.has_more),
          nextCursor: page.next_cursor || null,
        },
      }));
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          const nextContainer = chatBodyRef.current;
          if (!nextContainer) return;
          const heightDelta = nextContainer.scrollHeight - previousHeight;
          nextContainer.scrollTop = previousTop + heightDelta;
        });
      });
    } catch {
      // Existing request() handling surfaces API errors; keep the chat usable.
    } finally {
      setLoadingOlderMessages(false);
    }
  }, [
    currentConvId,
    currentHistoryState?.hasMore,
    currentHistoryState?.nextCursor,
    loadingOlderMessages,
    mapMessages,
    mergeOlderMessages,
    setSessionMessages,
  ]);

  /* Rebind the visible conversation while other per-session streams continue in the background. */
  useEffect(() => {
    if (isNewManorConversationId(conversationId)) {
      const sessionKey = createDraftSession();
      currentSessionKeyRef.current = sessionKey;
      setCurrentConvId(undefined);
      setDraftSessionKey(sessionKey);
      setSelectedAgent(null);
      setMentionedAgent(null);
      setSelectedMentions([]);
      // Cosmetic re-arm of the empty-state focus only — origin "default" keeps
      // it from attaching a skill to whatever the user types next.
      setIdeaComposer(
        chatModeRef.current === "auto"
          ? { mode: "new-idea", origin: "default" }
          : null,
      );
      setConversationLoading(false);
      resumedRef.current = false;
      return;
    }
    const cid =
      conversationId === "manor-ai" ||
      isVirtualAgentConversationId(conversationId)
        ? undefined
        : conversationId;
    currentSessionKeyRef.current = cid;
    /*
     * A live session owns its transcript. This effect also fires on the
     * conversation we are streaming right now: the first `stream_start` frame
     * mints the id, AppLayout navigates to /chat?conversation=<id>, and the
     * prop lands here. Clearing then would drop the user's own message plus
     * everything streamed so far, bounce the view back to the empty state, and
     * leave it there — `loadConversationMessages` refuses to overwrite a live
     * stream, so nothing refills it until the next SSE frame arrives.
     */
    const streamState = useChatStreamStore.getState();
    const liveKey = cid ? streamState.getSessionKeyForConversation(cid) : undefined;
    const streamingSession = liveKey ? streamState.sessions[liveKey] : undefined;
    const isLiveConversation = Boolean(streamingSession?.streaming);
    setCurrentConvId(cid);
    setDraftSessionKey(undefined);
    if (cid && !isLiveConversation) setSessionMessages(cid, []);
    setSelectedAgent(null);
    setMentionedAgent(null);
    setSelectedMentions([]);
    resumedRef.current = false; // allow auto-resume for new "manor-ai" switch
    if (cid) {
      if (isLiveConversation) {
        setConversationLoading(false);
        return;
      }
      loadConversationMessages(cid, {
        fallbackToAgent: Boolean(agentId),
        onlyIfStillCurrent: true,
      });
      return;
    }
    if (isVirtualAgentConversationId(conversationId) && agentId) {
      setConversationLoading(true);
      resolveLatestAgentConversationId()
        .then((latestId) => {
          if (latestId) {
            return loadConversationMessages(latestId, { fallbackToAgent: false });
          }
          setConversationLoading(false);
          return undefined;
        })
        .catch(() => setConversationLoading(false));
    }
  }, [
    agentId,
    conversationId,
    createDraftSession,
    loadConversationMessages,
    resolveLatestAgentConversationId,
  ]);

  /* Auto-resume most recent conversation when conversationId is "manor-ai" */
  useEffect(() => {
    if (conversationId !== "manor-ai") return;
    if (resumedRef.current || currentConvId) return;
    if (streamingRef.current) return;
    let cancelled = false;
    resumedRef.current = true;
    setConversationLoading(true);
    api.chat.listConversations()
      .then((convs) => {
        if (cancelled) return undefined;
        const latest = (convs || []).find(
          (conv: any) => !conv.agent_id && !conv.workspace_id,
        );
        if (!latest) {
          setConversationLoading(false);
          return undefined;
        }
        currentSessionKeyRef.current = latest.id;
        setCurrentConvId(latest.id);
        return loadConversationMessages(latest.id, {
          fallbackToAgent: false,
          onlyIfStillCurrent: true,
        });
      })
      .catch(() => {
        if (!cancelled) setConversationLoading(false);
      });
    return () => {
      cancelled = true;
    };
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

    if (suppressNextAutoScrollRef.current) {
      suppressNextAutoScrollRef.current = false;
      return;
    }

    if (!didInitialScrollRef.current) {
      didInitialScrollRef.current = true;
      scrollToBottom();
      return;
    }

    // Respect the user's scroll position: only follow new content while they
    // are at the bottom (they resume following by scrolling back down).
    if (!autoFollowRef.current) return;

    if (!assistantWorking) {
      scrollToBottom();
      return;
    }

    const now = Date.now();
    if (now - lastStreamScrollAtRef.current < 240) return;
    lastStreamScrollAtRef.current = now;
    if (streamScrollFrameRef.current != null) return;
    streamScrollFrameRef.current = window.requestAnimationFrame(() => {
      streamScrollFrameRef.current = null;
      scrollToBottom();
    });
  }, [messages, assistantWorking, autoFollowRef]);

  /* ---- Send message (SSE streaming) ---- */
  const handleSend = useCallback(
    async (
      rawText: string,
      attachments: AttachedItem[],
      manualSkills: ManualSkillItem[] = [],
      options: {
        forceAutoMode?: boolean;
        forceOwnerChat?: boolean;
        workflow?: WorkflowInvokeItem | null;
        sendContext?: ChatComposerSendContext;
        responseSurfaceSubmission?: ResponseSurfaceSubmissionReceipt;
      } = {},
    ) => {
      if (sendPreflightInFlightRef.current) return false;
      sendPreflightInFlightRef.current = true;
      const releaseSendPreflight = () => {
        sendPreflightInFlightRef.current = false;
      };
      const isResponseSurfaceSubmission = Boolean(options.responseSurfaceSubmission);

      /*
       * A built-in skill rides along only when the person deliberately armed an
       * idea mode on an empty chat. Anything else — the cosmetic default focus,
       * or a follow-up in a conversation that already has messages — sends no
       * skill, because manual_skill_refs is a force-invoke instruction to the
       * server, not a hint.
       */
      const armedIdeaSkill =
        !isResponseSurfaceSubmission && messages.length === 0 && ideaComposer?.origin === "user"
          ? IDEA_BUILT_IN_SKILLS[ideaComposer.mode]
          : undefined;
      const selectedWorkflow = isResponseSurfaceSubmission ? null : options.workflow || null;
      const effectiveManualSkills =
        !isResponseSurfaceSubmission && !selectedWorkflow && manualSkills.length > 0
          ? manualSkills
          : !selectedWorkflow && armedIdeaSkill
            ? [armedIdeaSkill]
            : [];
      const requestedManualSkillRefs =
        effectiveManualSkills.length > 0
          ? manualSkillReferences(effectiveManualSkills)
          : undefined;
      let manualSkillRefs = requestedManualSkillRefs;
      if (requestedManualSkillRefs?.some((reference) => reference.kind === "slug")) {
        try {
          const availableSkills = await queryClient.fetchQuery({
            queryKey: ["skills", "chat-manual-skill-resolution", "platform"],
            queryFn: () => api.skills.list({ include_platform: true }),
            staleTime: 60_000,
          });
          manualSkillRefs = resolveManualSkillReferenceIds(
            requestedManualSkillRefs,
            availableSkills,
          );
        } catch (error) {
          releaseSendPreflight();
          toast.error(
            t("lib.api.chat_failed"),
            error instanceof Error ? error.message : undefined,
          );
          return false;
        }
      }
      const text = stripWorkflowInvokeToken(
        stripManualSkillTokens(rawText, manualSkills),
        selectedWorkflow,
      );
      if (
        !text &&
        attachments.length === 0 &&
        effectiveManualSkills.length === 0 &&
        !selectedWorkflow
      ) {
        releaseSendPreflight();
        return false;
      }
      let sessionKey = currentSessionKeyRef.current;
      if (!sessionKey) {
        sessionKey = createDraftSession();
        setDraftSessionKey(sessionKey);
      }
      const existingSession = useChatStreamStore.getState().sessions[sessionKey];
      if (
        existingSession?.streaming ||
        hasActivePersistedChatStream(existingSession?.messages || [])
      ) {
        releaseSendPreflight();
        return false;
      }

      // Sending is an explicit jump back to the newest message.
      autoFollowRef.current = true;

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
        avatarUrl: mention.avatarUrl,
      }));
      const mentionContext =
        peopleMentions.length > 0
          ? `\n\n[Referenced people: ${peopleMentions.map((mention) => `${mention.name} <id:${mention.id}>${mention.subtitle ? ` ${mention.subtitle}` : ""}`).join(", ")}]`
          : "";
      const streamText =
        `${text}${mentionContext}`.trim() ||
        (selectedWorkflow
          ? workflowInvokeMessage(selectedWorkflow)
          : "Use the manually selected skill with the current conversation context.");
      if (!isResponseSurfaceSubmission) {
        setInput("");
        setSelectedMentions([]);
        setMentionedAgent(null);
        setIdeaComposer(null);
      }

      const displayContent = [
        text,
        selectedWorkflow ? `[${t("nav.flows")}: ${selectedWorkflow.title}]` : "",
      ].filter(Boolean).join("\n\n");

      // Separate local files and KB document IDs for the API call
      const localFiles = attachments
        .filter((a) => a.type === "file" && a.file)
        .map((a) => a.file!);
      const documentIds = attachments
        .filter((a) => a.type === "knowledge" && a.id)
        .map((a) => a.id!);
      const turnChatMode = options.responseSurfaceSubmission
        ? undefined
        : selectedWorkflow
        ? "flows"
        : options.forceAutoMode
          ? undefined
          : requestChatMode;
      const turnChatModePayload =
        !turnChatMode || selectedWorkflow ? undefined : chatModePayload;
      const turnAgentId = isResponseSurfaceSubmission
        ? agentId
        : options.forceOwnerChat
          ? undefined
          : resolvedAgentId;
      const retryRequest: ChatRetryRequest | undefined = selectedWorkflow ? undefined : {
        message: streamText || text,
        conversationId: currentConvId,
        documentIds: documentIds.length > 0 ? documentIds : undefined,
        agentId: turnAgentId,
        localWorkerId: options.sendContext?.localWorkerId,
        chatMode: turnChatMode,
        chatModePayload: turnChatModePayload,
        manualSkillRefs,
        responseSurfaceSubmission: options.responseSurfaceSubmission,
      };
      if (retryRequest) savePendingChatRetry(retryRequest);

      let attachmentSnapshots: Awaited<
        ReturnType<typeof createChatMessageAttachmentSnapshot>
      >[];
      try {
        attachmentSnapshots = await Promise.all(
          attachments.map(createChatMessageAttachmentSnapshot),
        );
      } catch (error) {
        releaseSendPreflight();
        throw error;
      }

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
            effectiveManualSkills.length > 0
              ? effectiveManualSkills.map((skill) => ({
                  id: skill.id,
                  name: manualSkillLabel(skill),
                  slug: skill.slug || undefined,
                }))
              : undefined,
          chatMode: turnChatMode,
          chatModePayload: turnChatModePayload,
          meta: options.responseSurfaceSubmission
            ? responseSurfaceSubmissionMeta(options.responseSurfaceSubmission)
            : undefined,
        },
        {
          role: "assistant" as const,
          content: "",
          timestamp: now,
          retryRequest,
        },
      ];

      let sendSucceeded = true;
      let responseSurfaceResult: ResponseSurfaceSubmissionResult = {
        status: "succeeded",
        serverAccepted: false,
        terminalObserved: false,
      };
      const completedSessionKey = await startStream(
        () => {
          releaseSendPreflight();
          return selectedWorkflow
            ? api.chat.streamFlowEntrypoint(
                selectedWorkflow.bindingId,
                text || workflowInvokeMessage(selectedWorkflow),
                currentConvId,
                {
                  files: localFiles.length > 0 ? localFiles : undefined,
                  documentIds: documentIds.length > 0 ? documentIds : undefined,
                  agentId: isAgentConversation ? agentId : undefined,
                  localWorkerId: options.sendContext?.localWorkerId,
                },
              )
            : api.chat.stream(streamText, currentConvId, {
                files: localFiles.length > 0 ? localFiles : undefined,
                documentIds: documentIds.length > 0 ? documentIds : undefined,
                agentId: turnAgentId,
                localWorkerId: options.sendContext?.localWorkerId,
                chatMode: turnChatMode,
                chatModePayload: turnChatModePayload,
                manualSkillRefs,
                responseSurfaceSubmission: options.responseSurfaceSubmission,
              });
        },
        currentConvId,
        msgsBeforeSend,
        (newConvId) => {
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
            onConversationResolved?.(newConvId);
          }
        },
        sessionKey,
        (status, details) => {
          sendSucceeded = status === ChatStreamCompletionStatus.Succeeded;
          responseSurfaceResult = {
            status: status === ChatStreamCompletionStatus.Succeeded
              ? "succeeded"
              : status === ChatStreamCompletionStatus.Cancelled
                ? "cancelled"
                : "failed",
            serverAccepted: details.serverAccepted,
            terminalObserved: details.terminalObserved,
          };
        },
      );
      if (!sendSucceeded) {
        return options.responseSurfaceSubmission ? responseSurfaceResult : false;
      }
      const streamState = useChatStreamStore.getState();
      const activeKey = currentSessionKeyRef.current;
      const resolvedActiveKey = activeKey
        ? streamState.sessionAliases[activeKey] || activeKey
        : undefined;
      const completedConversationId =
        streamState.sessions[completedSessionKey]?.convId;
      if (
        completedConversationId &&
        resolvedActiveKey === completedSessionKey
      ) {
        await loadConversationMessages(completedConversationId, {
          fallbackToAgent: Boolean(agentId),
          allowSettledStreamRefresh: true,
          backgroundRefresh: true,
        });
      }
      clearPendingChatRetry();
      if (turnChatMode) resetChatModeAfterTurn();
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      return options.responseSurfaceSubmission
        ? responseSurfaceResult
        : true;
    },
    [
      selectedMentions,
      currentConvId,
      queryClient,
      resolvedAgentId,
      messages,
      startStream,
      createDraftSession,
      loadConversationMessages,
      requestChatMode,
      chatModePayload,
      resetChatModeAfterTurn,
      ideaComposer,
      agentId,
      isAgentConversation,
      onConversationResolved,
      toast,
    ],
  );

  const handleResponseSurfaceSubmit = useCallback(
    async (receipt: ResponseSurfaceSubmissionReceipt) => {
      const rollback = () => {
        setMessages((current) => (
          rollbackResponseSurfaceSubmissionMessages(current, receipt.eventId)
        ));
      };
      try {
        const accepted = await handleSend(
          responseSurfaceSubmissionMessage(receipt),
          [],
          [],
          { responseSurfaceSubmission: receipt },
        );
        if (typeof accepted === "object") {
          if (!accepted.serverAccepted) {
            rollback();
          } else if (accepted.status !== "succeeded" && accepted.terminalObserved) {
            setMessages((current) => settleResponseSurfaceSubmissionFailure(
              current,
              receipt.eventId,
              accepted.status === "cancelled" ? "interrupted" : "failed",
            ));
          }
        } else if (accepted === false) rollback();
        return accepted;
      } catch (error) {
        rollback();
        throw error;
      }
    },
    [handleSend, setMessages],
  );

  const createWorkspaceFromRecommendation = useCallback(
    async (prompt: string) => {
      await handleSend(prompt, [], [], {
        forceAutoMode: true,
        forceOwnerChat: true,
      });
    },
    [handleSend],
  );
  const {
    dismissedWorkspaceRecommendations,
    dismissWorkspaceRecommendation,
    handleWorkspaceRecommendationAdd,
    handleWorkspaceRecommendationCreate,
    handleWorkspaceRecommendationOpen,
    handleWorkspaceRecommendationOptOut,
    workspaceRecommendationBusyKey,
    workspaceRecommendationPreferenceBusy,
    workspaceRecommendationPreferencesReady,
    workspaceRecommendationsSuppressed,
  } = useWorkspaceRecommendationActions({
    createWorkspace: createWorkspaceFromRecommendation,
  });

  const handleIdeaQuickAction = useCallback(
    (request: IdeaQuickActionRequest) => {
      setChatMode("auto");
      setChatModePayload(getDefaultChatModePayload("auto"));
      setActiveCapability("workspace");
      void handleSend(request.message, [], [request.skill], {
        forceAutoMode: true,
      });
    },
    [handleSend],
  );

  /* Rail click / wheel focus: a deliberate gesture, so the mode may arm a skill. */
  const handleIdeaModeChange = useCallback(
    (mode: IdeaQuickAction["id"] | null) => {
      setIdeaComposer(mode ? { mode, origin: "user" } : null);
      if (mode) {
        setChatMode("auto");
        setChatModePayload(getDefaultChatModePayload("auto"));
        setActiveCapability("workspace");
      }
    },
    [],
  );

  const handleValidationStart = useCallback(() => {
    setIdeaComposer({ mode: "validate-idea", origin: "user" });
    window.setTimeout(() => composerEditorRef.current?.focus(), 0);
  }, []);

  const handleStopRequest = useCallback(() => {
    const convId = currentConvId || streamingConvId;
    const hitlIds = pendingHITLIds(messages);
    if (convId) {
      void api.chat.cancelPendingFileApprovals(convId, hitlIds).then(
        () => queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        () => undefined,
      );
    }
    void stopStream(currentSessionKeyRef.current);
  }, [currentConvId, streamingConvId, messages, stopStream, queryClient]);

  /* ---- HITL actions ---- */
  const handleHITLAction = useCallback(
    async (hitlId: string, action: string, review?: unknown) => {
      const markResolved = (items: ChatMessage[]) =>
        items.map((msg) => ({
          ...msg,
          hitl_requests: msg.hitl_requests?.map((h) =>
            h.id === hitlId ? { ...h, resolved: true, resolution: action } : h,
          ),
        }));
      const updatedMessages = markResolved(messages);
      setMessages(updatedMessages);

      const hitlMessage = JSON.stringify({
        hitl_id: hitlId,
        action,
        ...(review !== undefined ? { payload: { review } } : {}),
      });
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
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
            onConversationResolved?.(newConvId);
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
      onConversationResolved,
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
          meta: retryRequest.responseSurfaceSubmission
            ? responseSurfaceSubmissionMeta(retryRequest.responseSurfaceSubmission)
            : fallbackUserMessage?.meta,
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
              manualSkillRefs: retryRequest.manualSkillRefs,
              manualSkillIds: retryRequest.manualSkillIds,
              responseSurfaceSubmission: retryRequest.responseSurfaceSubmission,
            },
          ),
        retryRequest.conversationId || currentConvId,
        msgsBeforeSend,
        (newConvId) => {
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          if (currentSessionKeyRef.current === sessionKey) {
            setCurrentConvId(newConvId);
            setDraftSessionKey(undefined);
            onConversationResolved?.(newConvId);
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
      onConversationResolved,
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
      const fallbackRequestPreview = messages
        .slice(0, index)
        .reverse()
        .find(hasVisibleUserContent);

      try {
        await submitMessageFeedback(key, rating, (queuedRating) =>
          api.chat.feedback(conversationId, message.id!, {
            rating: queuedRating,
            content_preview: contentPreview,
            request_preview: (toDisplayText(fallbackRequestPreview?.content) || "").slice(0, 1000),
          }),
        );
      } catch {}
    },
    [
      currentConvId,
      feedbackKeyForMessage,
      messages,
      submitMessageFeedback,
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
            <div style={{ display: "flex", alignItems: "center" }}>
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
            </div>
            {assistantWorking ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 2,
                }}
              >
                <AgentActivityOrb activity={activeAgentActivity} />
              </div>
            ) : (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  marginTop: 2,
                }}
              >
                <span style={{ fontSize: 11, color: "#78716c" }}>
                  {subtitle || t("page.app_layout.your_ai_chief_of_staff")}
                </span>
              </div>
            )}
          </div>
        </div>
        {(showWorkspaceIntro || onNewConversation) && (
          <div className="embedded-chat-header-actions">
            {showWorkspaceIntro && <WorkspaceIntroDialog />}
            {onNewConversation && (
              <Button
                variant="ghost"
                className="embedded-chat-new-session"
                onClick={onNewConversation}
                title={t("component.session_switcher.new_chat_2")}
                ariaLabel={t("component.session_switcher.new_chat_2")}
              >
                <IconPlus size={22} />
              </Button>
            )}
          </div>
        )}
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
        <ResizablePaneGroup
          panes={[
            {
              id: "chat",
              label: title,
              initialSize: 2,
              minSize: outputOpen ? 360 : undefined,
              className: "embedded-chat-pane embedded-chat-pane--chat",
              children: (
                <div className="embedded-chat-column">
          {/* ---- Chat Body ---- */}
          <div className="embedded-chat-body-wrap">
            <div
              ref={chatBodyRef}
              onScroll={handleAutoFollowScroll}
              className={`embedded-chat-body ${
                messages.length === 0 ? "embedded-chat-body--empty" : ""
              }`}
            >
              {showConversationSkeleton ? (
                <ChatMessagesSkeleton rows={5} />
              ) : messages.length === 0 && (
                <WorkspaceWelcome
                  activeCapability={activeCapability}
                  activeIdeaMode={ideaComposerMode}
                  onCapabilityChange={(capability) => {
                    setIdeaComposer(null);
                    setActiveCapability(capability);
                    const nextMode = chatModeFromCapability(capability);
                    setChatMode(nextMode);
                    setChatModePayload(getDefaultChatModePayload(nextMode));
                  }}
                  onSampleSelect={handleSampleSelect}
                  onIdeaQuickAction={handleIdeaQuickAction}
                  onIdeaModeChange={handleIdeaModeChange}
                  onValidationStart={handleValidationStart}
                />
              )}

              {messages.length > 0 && currentHistoryState?.hasMore && (
                <div className="chat-history-load-more">
                  <button
                    type="button"
                    className="chat-history-load-more-button"
                    disabled={loadingOlderMessages}
                    onClick={handleLoadOlderMessages}
                  >
                    {loadingOlderMessages && <span className="chat-tool-spinner" />}
                    {t("page.chat_history.load_earlier_messages")}
                  </button>
                </div>
              )}

            {!showConversationSkeleton && visibleWorkflowMessageEntries.map(({ message: msg, index: i }) => {
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
              const isLatestStreaming = assistantWorking && i === messages.length - 1;
              const renderedBubbleDisplay = parseUserMessageDisplay(
                { ...msg, content: bubbleContent },
                { streaming: isLatestStreaming },
              );
              const renderedBubbleContent = renderedBubbleDisplay.cleanContent;
              const messageDisplay = localCodingNotice
                ? parseUserMessageDisplay(msg, {
                    renderedContent: renderedBubbleContent,
                    streaming: isLatestStreaming,
                  })
                : renderedBubbleDisplay;
              const suppressApprovalBubble =
                isApprovalBoilerplateContent(msg) && !isLatestStreaming;
              const showCreditLimitNotice =
                msg.role === "assistant" &&
                msg.stop_reason === "credit_exhausted";
              const renderAssistantBubble =
                msg.role !== "assistant" ||
                assistantMessageRendersInlineFileSurfaces(msg, isLatestStreaming);
              const actionCopyText = msg.role === "user"
                ? content
                : chatMessageActionText(msg, rawBubbleContent);
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
              const messageAnchorId = chatMessageAnchorId(msg.id, i);
              const createdResources = msg.role === "assistant"
                ? createdChatResourceReferences(msg.tool_calls)
                : [];
              const workspaceRecommendation = msg.role === "assistant"
                ? normalizeWorkspaceRecommendation(
                    msg.meta?.workspace_recommendation,
                  )
                : null;
              const workspaceRecommendationKey = `${msg.id || currentConvId || "draft"}:${i}`;
              const messageReturnTo = `${location.pathname}${location.search}#${messageAnchorId}`;
              // Only backend HITL cards have an id that can be safely resolved.
              const showInlineApproval = false;
              return (
	                <div
	                  key={i}
                    id={messageAnchorId}
	                  data-chat-message-index={i}
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

                    {isLatestStreaming &&
                      msg.role === "assistant" &&
                      runtimeQueueStatus && (
                        <div
                          className="chat-runtime-queue-status"
                          role="status"
                          aria-live="polite"
                        >
                          <span
                            className="chat-runtime-queue-status-dot"
                            aria-hidden="true"
                          />
                          <span>{runtimeQueueStatus}</span>
                        </div>
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
                                  disabled={assistantWorking || hitl.resolved}
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
                            disabled={assistantWorking}
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
                    {hasAssistantBlocks &&
                      !canRetryFromContent &&
                      renderAssistantBubble && (
                      <div className="chat-bubble chat-bubble--bot">
                        <AssistantMessageBlocks
                          blocks={msg.assistant_blocks}
                          content={renderedBubbleContent}
                          keyPrefix={i}
                          streaming={assistantWorking && i === messages.length - 1}
                          returnTo={messageReturnTo}
                          onResponseSurfaceSubmit={handleResponseSurfaceSubmit}
                          sourceMessageId={msg.id || ""}
                          responseSurfaceSubmissionReceipts={responseSurfaceSubmissionReceipts}
                        />
                        {createdResources.map((resource) => (
                          <CreatedResourceCard
                            key={`${resource.kind}:${resource.id}`}
                            resource={resource}
                            returnTo={messageReturnTo}
                          />
                        ))}
                        {msg.workflow_result && (
                          <WorkflowResultCard
                            result={msg.workflow_result}
                            returnTo={messageReturnTo}
                          />
                        )}
                        <ChatMessageReferenceStrip
                          references={messageDisplay.references}
                          inlineFileCards
                          returnTo={messageReturnTo}
                        />
                      </div>
                    )}

                    {(!hasAssistantBlocks || canRetryFromContent) &&
                      (bubbleContent ||
                        createdResources.length > 0 ||
                        Boolean(msg.workflow_result) ||
                        // Attachment-only turns still need their bubble: the
                        // file card is the message's only visible trace.
                        (msg.attachments?.length ?? 0) > 0) &&
                      renderAssistantBubble && (
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
                                content={renderedBubbleContent}
                                isUser={false}
                                streaming={
                                  assistantWorking &&
                                  i === messages.length - 1 &&
                                  msg.role === "assistant"
                                }
                                returnTo={messageReturnTo}
                              />
                              {createdResources.map((resource) => (
                                <CreatedResourceCard
                                  key={`${resource.kind}:${resource.id}`}
                                  resource={resource}
                                  returnTo={messageReturnTo}
                                />
                              ))}
                              {msg.workflow_result && (
                                <WorkflowResultCard
                                  result={msg.workflow_result}
                                  returnTo={messageReturnTo}
                                />
                              )}
                              <ChatMessageReferenceStrip
                                references={messageDisplay.references}
                                inlineFileCards
                                returnTo={messageReturnTo}
                              />
                            </>
                          )}
                          {assistantWorking &&
                            i === messages.length - 1 &&
                            msg.role === "assistant" &&
                            !runtimeQueueStatus && (
                              <span className="chat-streaming-cursor" />
                            )}
                        </div>
                      )}

                    {/* Compact first-token activity. Tool-only turns already render
                      their processing state above, so avoid a duplicate status. */}
                    {!content &&
                      visibleTools.length === 0 &&
                      !hasAssistantBlocks &&
                      assistantWorking &&
                      i === messages.length - 1 &&
                      msg.role === "assistant" &&
                      !runtimeQueueStatus && (
                        <div className="chat-bubble chat-bubble--bot chat-bubble--activity">
                          <AgentActivityOrb
                            activity={inferAgentActivity(msg)}
                            className="agent-activity-orb--message"
                          />
                        </div>
                      )}

                    {workspaceRecommendation &&
                      workspaceRecommendationPreferencesReady &&
                      !workspaceRecommendationsSuppressed &&
                      !dismissedWorkspaceRecommendations.has(workspaceRecommendationKey) && (
                        <WorkspaceRecommendationCard
                          recommendation={workspaceRecommendation}
                          loading={workspaceRecommendationBusyKey === workspaceRecommendationKey}
                          preferenceLoading={workspaceRecommendationPreferenceBusy}
                          disabled={assistantWorking || workspaceRecommendationPreferenceBusy}
                          onCreate={() => void handleWorkspaceRecommendationCreate(
                            workspaceRecommendation,
                            workspaceRecommendationKey,
                          )}
                          onOpen={() => handleWorkspaceRecommendationOpen(
                            workspaceRecommendation,
                            workspaceRecommendationKey,
                          )}
                          onAdd={() => void handleWorkspaceRecommendationAdd(
                            workspaceRecommendation,
                            workspaceRecommendationKey,
                          )}
                          onContinue={() => dismissWorkspaceRecommendation(
                            workspaceRecommendationKey,
                          )}
                          onDontSuggestAgain={() => void handleWorkspaceRecommendationOptOut()}
                        />
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

                    {(!isLatestStreaming || Boolean(content) || visibleTools.length > 0 || hasAssistantBlocks) && (
                      <div
                        className={`chat-message-meta-row ${
                          msg.role === "user" ? "chat-message-meta-row--user" : ""
                        } ${showMessageActions ? "chat-message-meta-row--actions" : ""}`}
                      >
                        <ChatTimestamp timestamp={msg.timestamp} />
                        {showMessageActions && (
                          <span className="chat-message-meta-actions">
                            <ChatMessageActions
                              align="right"
                              copyText={actionCopyText}
                              speechText={msg.role === "assistant" ? actionCopyText : undefined}
                              voiceScope={{ conversationId: msg.conversation_id || currentConvId || streamingConvId }}
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
                              disabled={assistantWorking}
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
                    )}
                  </div>
                </div>
              );
            })}

              <div ref={messagesEndRef} />
            </div>
            <ChatScrollRail
              containerRef={chatBodyRef}
              markers={chatScrollRailMarkers}
            />
          </div>

          {/* Sticky approval bar — surfaces the oldest unresolved
            approval-style HITL with its human description and
            Approve/Reject. Hidden when nothing is pending. The
            `variant` keeps width / padding in lockstep with the
            composer below — when OutputPanel opens, the composer
            narrows from 920→820px and the bar follows. */}
          <ApprovalActionBar
            messages={visibleWorkflowMessages}
            disabled={assistantWorking}
            onResolve={handleHITLAction}
            variant={outputOpen ? "embedded-output-open" : "embedded"}
          />

          <div className="chat-tip-bar">
            <InlineTips surface="general_chat" placement="composer" />
          </div>

          <WorkflowRunHost
            conversationId={currentConvId || streamingConvId}
            groups={workflowRunGroups}
            onResolveMessage={handleWorkflowMessageResolve}
            resolveLoading={workflowMessageResolveMutation.isPending}
            resolveError={workflowMessageResolveMutation.error}
            resolveMessageId={workflowMessageResolveMutation.variables?.messageId || null}
            invalidationQueryKeys={GLOBAL_WORKFLOW_INVALIDATION_QUERY_KEYS}
            onRunChange={workflowMessageResolveMutation.reset}
          />

          <ChatInputFooter
            voiceScope={{ conversationId: currentConvId || streamingConvId, agentId: resolvedAgentId }}
            onVoiceConversation={(id) => {
              setCurrentConvId(id);
              setDraftSessionKey(undefined);
              void loadConversationMessages(id, { backgroundRefresh: true, onlyIfStillCurrent: true });
              void queryClient.invalidateQueries({ queryKey: ["conversations"] });
            }}
            value={input}
            onChange={handleComposerChange}
            streaming={assistantWorking}
            onSend={(text, attachments, manualSkills, context) => {
              void handleSend(text, attachments, manualSkills, { sendContext: context });
            }}
            onSendWorkflow={(text, attachments, manualSkills, workflow, context) => {
              void handleSend(text, attachments, manualSkills, { workflow, sendContext: context });
            }}
            onStop={handleStopRequest}
            topSlot={
              messages.length > 0 ? (
                <ChatModeTemplateGallery
                  mode={chatMode}
                  disabled={assistantWorking || Boolean(remixingChatModeSampleTitle)}
                  samples={chatModeTemplateSamples(chatMode)}
                  onSelect={handleChatModeTemplateSelect}
                />
              ) : undefined
            }
            placeholder={
              requestChatMode
                ? getChatModeInputPlaceholder(chatMode, chatModePayload)
                : messages.length === 0 && ideaComposerMode === "validate-idea"
                ? t("component.embedded_chat.idea_validation.placeholder")
                : messages.length === 0 && ideaComposerMode === "new-idea"
                  ? t("component.embedded_chat.idea_generation.placeholder")
                : messages.length === 0
                ? activeCapabilityConfig.placeholder
                : `Message ${title}... @ mention, # attach, / skill, % flow`
            }
            modeSlot={
              <ChatModeToolbar
                mode={chatMode}
                payload={chatModePayload}
                onModeChange={handleChatModeChange}
                onPayloadChange={setChatModePayload}
                disabled={assistantWorking}
              />
            }
            replaceActionButtons={
              chatMode !== "auto"
            }
            mentions={mentionOptions}
            workflows={workflowInvokeOptions}
            selectedMentions={selectedMentions}
            onMentionSelect={handleMentionSelect}
            onMentionRemove={handleMentionRemove}
            className={`embedded-chat-footer ${outputOpen ? "embedded-chat-footer--output-open" : ""}`}
            textareaRef={textareaRef}
            editorRef={composerEditorRef}
            seedAttachments={composerSeed?.attachments}
            seedAttachmentsKey={composerSeed?.key}
          />
                </div>
              ),
            },
            ...(outputOpen
              ? [
                  {
                    id: "output",
                    label: t("component.embedded_chat.output_panel"),
                    initialSize: 1,
                    minSize: 360,
                    className: "embedded-chat-pane embedded-chat-pane--output",
                    children: (
                      <OutputPanel
                        artifact={activeOutputArtifact}
                        updating={assistantWorking}
                        onStop={closeOutputPanel}
                        returnTo={selectedArtifactReturnTo}
                      />
                    ),
                  },
                ]
              : []),
          ]}
          storageKey="embedded-chat-output-panes"
          className="embedded-chat-output-panes"
        />
      </div>
    </div>
  );
}
