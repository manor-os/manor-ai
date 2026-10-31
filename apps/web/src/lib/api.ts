import {
  accessTokenExpired,
  getReadyAuthToken,
  markCollaborationEndedSignIn,
  restorePersistentSession,
  signInPath,
} from "./persistentSession";
import { EMBED } from "../embed/embedContext";
import { endEmbeddedSession } from "../embed/embeddedSession";
import type { KnowledgeContentSearchResponse } from "./knowledgeLayout";
import { approvalResourcePath, type ApprovalScope, type ApprovalSnapshot, type ApprovalWrite, type ApprovalPreview, type ApprovalOverrides, type ApprovalCommandProjection, type ApprovalSaveRuleCommand, type ApprovalCommandResult } from "./approvalTypes";
import type {
  User,
  Task,
  Conversation,
  Message,
  Document,
  Agent,
  Notification,
  Workspace,
  WorkspaceStaff,
  WorkspaceStats,
  WorkspaceActivity,
  RuntimeEvidence,
  AgentLearningCandidate,
  UsageSummary,
  TeamUsageResponse,
  PaginatedResponse,
  TaskCategory,
  TaskConstantsResponse,
  TaskHITLResponse,
  TaskRetryResponse,
  CalendarSettings,
  CalendarSettingsResponse,
  CalendarOptionsResponse,
  BookingLink,
  BookingLinkWrite,
  DailyAgendaResponse,
  ExternalCalendarEventsResponse,
  PublicBookingLink,
  PublicBookingRequest,
  BookingConfirmation,
  Order,
  OrderItem,
  OrderStats,
  DocGenFormat,
  StaffDepartment,
  StaffSchedule,
  StaffScheduleException,
  BrowserSession,
  ApiKey,
  WebhookEndpoint,
  WebhookDelivery,
  CustomField,
  Memory,
  Favorite,
  Tag,
  Report,
  BackupSummary,
  PresenceInfo,
  TaskTemplateDetail,
  DocumentGrant,
  DocumentShare,
  DocumentSignatureRequest,
  DocumentAccessLogRow,
  DocumentFolderInfo,
  AccessRequest,
  ShareApproval,
  UserSummary,
  PeopleContext,
  PeopleContextActionResponse,
  PeopleDirectoryEntry,
  NotificationPreferences,
  NotificationPreferencesUpdate,
  Comment,
  CommentAnchor,
} from "./types";
import type { AiEditTargetKind } from "./editorLiveChat";
import {
  globalSearchPath,
  normalizeGlobalSearchResponse,
  type GlobalSearchResponse,
} from "./globalSearchResponse";
import type { ChatRoom, ChatRoomAgentBinding, ChatRoomAgentCandidate, ChatRoomFile, ChatRoomInvite, ChatRoomInviteSender, ChatRoomPendingInvite, ChatRoomMessage, ChatRoomPresence, ChatRoomReplyApproval, ChatRoomToolApproval } from "./chatRoomTypes";
import type {
  AgentInitiative,
  AgentInitiativeActionResponse,
  AgentInitiativeConversationListResponse,
  AgentInitiativeCreateRequest,
  AgentInitiativeListResponse,
  AgentInitiativeRevisionRequest,
  AgentInitiativeRunListResponse,
  AgentInitiativeRunReceipt,
  AgentInitiativeRun,
  AgentInitiativeUpdateRequest,
} from "./agentInitiativeTypes";

import { useToastStore } from "../stores/toast";
import { normalizeLocale, t } from "./i18n";
import { browserImageBlob, prepareChatUploadFile } from "./browserImage";
import {
  legacyManualSkillIds,
  type ManualSkillReference,
} from "./manualSkillRefs";
import {
  authPrincipalKey,
  decodeAuthTokenClaims,
  isSameAuthIdentity,
  getAuthToken,
  clearAuthBrowserState,
  notifyAuthTokenChanged,
} from "./authToken";
import { captureClientError } from "./clientErrors";
import { downloadRoomFile } from "./roomFileDownload";
import { fetchCachedFile, invalidateCachedFiles } from "./persistentFileCache";

const API_BASE = "/api/v1";

export type ConnectionKind = "integration" | "oauth_account";
/** A coding agent (public OAuth client) signed in as the current user. */
export interface OAuthConnection {
  id: string;
  client_id: string;
  name: string;
  verified: boolean;
  entity_id: string;
  connected_at: string;
  last_used_at: string;
  expires_at: string;
}
export interface ConnectionShareMember {
  user_id: string;
  display_name: string | null;
  email: string | null;
  avatar_url: string | null;
}
export interface ConnectionShareState {
  entity_grant_id: string | null;
  users: Array<ConnectionShareMember & { grant_id: string }>;
  candidates: ConnectionShareMember[];
}
export interface ConnectionShareSummary {
  entity: boolean;
  user_count: number;
  /** The first few named recipients, for an avatar stack. */
  people: Array<{ user_id: string; display_name: string | null; avatar_url: string | null }>;
}
export interface ConnectionUse {
  actor_user_id: string;
  actor_display_name: string | null;
  server_key: string;
  tool_name: string;
  created_at: string;
}


export enum ConversationSurfaceKind {
  OrdinaryChat = "chat",
  AiEdit = "ai_edit",
}
const BACKEND_UNAVAILABLE_TOAST_ID = "backend-unavailable";
const RATE_LIMIT_TOAST_ID = "api-rate-limit";
let rateLimitUntil = 0;
let backendUnavailableUntil = 0;

export type DashboardAppliedModuleChange = {
  action: "create" | "update" | "remove";
  module?: Record<string, unknown> | null;
  module_id?: string | null;
};

export type DashboardSuggestLayoutResult = {
  version: number;
  widgets: Array<{ id: string; visible: boolean }>;
  modules: any[];
  assistant_message?: string | null;
  changed_module_id?: string | null;
  conversation_id?: string | null;
  tool_calls: string[];
  hitl_requests: Array<Record<string, unknown>>;
  preview_created: boolean;
  applied_module_changes?: DashboardAppliedModuleChange[];
  widgets_changed?: boolean;
};

export type DashboardSuggestJobResponse = {
  job_id: string;
  status: "running" | "succeeded" | "failed" | "cancelled";
  error?: string | null;
  error_code?: string | null;
  result?: DashboardSuggestLayoutResult | null;
};

export type TaskBoardPreferences = {
  mode: "kanban" | "list";
  visible_columns: string[];
  column_order: string[];
  configured: boolean;
};

export type WorkspaceAttentionReminder = {
  key: string;
  message_id: string;
  source_id: string;
  kind: string;
  summary: string;
  created_at: string;
};

type PaginatedMessagesResponse<T> = {
  conversation_id?: string | null;
  /** Workspace chat only: durable run state; null uses legacy message markers. */
  has_active_runtime_run?: boolean | null;
  items: T[];
  has_more: boolean;
  next_cursor?: string | null;
  /** Workspace chat only: server-counted actions still waiting on a human. */
  open_action_count?: number;
  unseen_action_keys?: Record<string, Record<string, string>>;
  /** True when `items` carries every open action card, so absent == answered. */
  open_actions_complete?: boolean;
};

export type GlobalChatMessage = Message & {
  message_kind?: string | null;
  refs?: Record<string, unknown>[] | null;
  meta?: Record<string, unknown> | null;
  pending_action?: Record<string, unknown> | null;
  resolved_at?: string | null;
  resolution?: Record<string, unknown> | null;
  updated_at?: string | null;
};

export type WorkspaceChatEntrypoint = {
  binding_id: string;
  workflow_id: string;
  title: string;
  description: string;
  placeholder: string;
  order: number;
  intent_enabled: boolean;
  inputs: Array<{
    key: string;
    label: string;
    type: "string" | "number" | "boolean" | "json";
    required: boolean;
    placeholder: string;
    default?: unknown;
  }>;
};

export type WorkspaceAppActionInput = WorkspaceChatEntrypoint["inputs"][number] & {
  description?: string;
  hidden?: boolean;
  schema?: import("../components/workflows/workflowSchema").WorkflowInputSchema;
  /** The form page this input is on; inputs sharing a step are asked together. */
  step?: string;
  step_label?: string;
};

export type WorkspaceAppFormField = {
  key: string;
  label: string;
  type: "text" | "long_text" | "file" | "number" | "select";
  required: boolean;
  placeholder: string;
  options: string[];
};

export type WorkspaceAppDataContextKind = "project" | "launch_run" | "workspace";
export type WorkspaceAppDataBlockType = "progress" | "records" | "documents" | "metrics" | "summary" | "project_rollup" | "references" | "packs" | "gallery";

export type WorkspaceAppSummaryField = {
  label: string;
  field: string[];
  format?: "text" | "status" | "date" | "link" | "link_list" | "document" | "provider";
  hide_when_empty?: boolean;
  labels?: Record<string, string>;
  /** Fallback paths shown beside the value, e.g. the account of a provider. */
  secondary?: string[][];
};

/**
 * A read-only source declared by a Workspace App configuration.  The server
 * owns the adapter, ACL check, joins, and field projection; the browser never
 * sends a source name or path when it asks for data.
 */
export type WorkspaceAppDataSource = {
  type: string;
  workflow?: string;
  project_type?: string;
  label_fields?: string[][];
  /** Optional state path that pins App execution links to a substantive Workflow run. */
  execution_run_id_path?: string[];
  stages?: string[];
  order?: { field: string[]; type: "datetime"; direction: "asc" | "desc" };
  [key: string]: unknown;
};

export type WorkspaceAppDataBlock = {
  key: string;
  type: WorkspaceAppDataBlockType;
  source: string;
  metrics_source?: string;
  metrics_display?: "declared" | "configured";
  record_display?: {
    title?: { source: string; field: string[] };
    fields?: Array<{ label: string; source: string; field: string[]; labels?: Record<string, string>; format?: "text" | "provider"; secondary?: string[][] }>;
    media?: Array<{ source: string; label?: string }>;
  };
  title?: string | null;
  fields?: string[][] | WorkspaceAppSummaryField[];
  metric_keys?: string[];
  count_by_stage?: boolean;
  first?: { stages: string[] };
  /** Presentation copy retained for an explicitly configured empty block. */
  empty_message?: string;
  /** Completion is derived from the rollup stages; it never changes project state. */
  completion?: { stages: string[]; label: string };
  /** Presentation-only mapping from stored stages to display labels. */
  stage_labels?: Record<string, string>;
  /** A packs block's plan: its cards follow the plan's order, a planned child a card before it starts. */
  plan?: string;
  /** One App action per pack, opened with the pack's project filling ``input``; only in ``stages`` when given. */
  item_action?: { action: string; label: string; input: string; stages?: string[] };
  /** A planned card's App action, its inputs filled from the card's parent project or plan code. */
  plan_action?: { action: string; label: string; inputs: Record<string, "parent" | "code"> };
  limit?: number;
};

export type WorkspaceAppDataDisplay = {
  layout?: "stack" | "tabs";
  groups?: Array<{ key: string; title: string; blocks: string[] }>;
  hidden?: boolean;
  hidden_blocks?: string[];
  /** Opt-in projection for date-based project collections such as schedules. */
  calendar?: {
    block: string;
    date_field: string[];
    rollup_block: string;
    context_block: string;
    attention_stages: string[];
    collection_title: string;
  };
};

export type WorkspaceAppDataComponent = {
  type: "data_view";
  schema_version: 1;
  key: string;
  title?: string | null;
  description?: string | null;
  context: {
    kind: WorkspaceAppDataContextKind;
    source?: string;
    label?: string | null;
    /** Pick projects from cards instead of a list; ``cover`` and ``progress`` name the blocks a card shows. */
    display?: "select" | "cards";
    cover?: string;
    progress?: string;
  };
  blocks: WorkspaceAppDataBlock[];
  display?: WorkspaceAppDataDisplay;
  visibility?: { block: string; when: "has_records" | "has_observations" };
};

export type WorkspaceAppDataContext = {
  kind: WorkspaceAppDataContextKind;
  id: string;
  label: string;
  href?: string | null;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  project_type?: string | null;
  execution?: WorkspaceAppDataItem["execution"];
  summary?: WorkspaceAppDataContextSummary;
  /** On a card: the project's first image reference and how many of its children are done. */
  cover?: { document_id: string; title?: string | null };
  progress?: { done: number; total: number; label: string };
};

export type WorkspaceAppDataContextSummary = {
  fields: Array<{
    label: string;
    value?: string | number | boolean | null;
    format?: "text" | "status" | "date" | "link" | "link_list" | "document" | "provider";
    href?: string | null;
    links?: Array<{ label: string; href: string }>;
    provider?: string | null;
    secondary?: string | null;
  }>;
  total: number;
  counts_by_stage: Record<string, number>;
  /** Unfinished items are already counted by their run (failed, cancelled, paused, pending). */
  counts_include_execution?: boolean;
  next?: {
    id: string;
    label: string;
    status?: string | null;
    detail?: string | null;
    updated_at?: string | null;
    fields: Array<{
      label: string;
      value?: string | number | boolean | null;
      format?: "text" | "status" | "date" | "link" | "link_list" | "document" | "provider";
      href?: string | null;
      links?: Array<{ label: string; href: string }>;
    }>;
    execution?: WorkspaceAppDataItem["execution"];
    calendar_at?: string | null;
  };
};

export type WorkspaceAppDataMetric = {
  id: string;
  metric_key?: string | null;
  label?: string | null;
  value?: number | string | null;
  unit?: string | null;
  value_type?: "number" | "percent" | "currency" | "duration" | null;
  status?: string | null;
  freshness?: string | null;
  collection_error?: string | null;
  delta_reason?: string | null;
  change?: number | null;
  previous_value?: number | null;
  trend?: Array<{ value: number | null; observed_at?: string | null }>;
  updated_at?: string | null;
  detail?: string | null;
  title?: string | null;
  subtitle?: string | null;
  href?: string | null;
  viewer_url?: string | null;
  document_id?: string | null;
};

export type WorkspaceAppDataItem = WorkspaceAppDataMetric & {
  calendar_at?: string | null;
  /** A gallery card's result: the run that made the version shown, which opens it. */
  run_id?: string | null;
  /** How far a result still being made is, 1–99. */
  progress_percent?: number;
  workflow_id?: string | null;
  binding_id?: string | null;
  platform?: string | null;
  /** A project file's folder below the project's own folder ("" for the folder itself). */
  folder?: string;
  /** The child project (an episode) whose results this file is, and which version folder it is in. */
  pack?: {
    id: string; root: string; label: string; stage?: string | null; version?: string; code?: string; synopsis?: string; parent_id?: string;
    /** The child project's latest run: how its making is going, and whether it waits for a person. */
    run?: { id: string; workflow_id: string; binding_id?: string | null; status: string; needs_you?: boolean };
  };
  /** Verified as a result of its production, not a file a newer version is still writing. */
  delivered?: boolean;
  /** A reference a project keeps for everything made in it, e.g. role narrator → voice Puck. */
  role?: string;
  kind?: string;
  reference?: { voice?: string; provider?: string; model?: string; pinned_at?: string };
  /** Held by an ancestor project (the series an episode belongs to), not the selected one. */
  inherited?: boolean;
  holder_project_id?: string | null;
  holder_label?: string | null;
  execution?: {
    run_id: string;
    status: string;
    current_step_id?: string | null;
    error?: string | null;
    href?: string | null;
  } | null;
  total?: number;
  counts_by_stage?: Record<string, number>;
  /** Unfinished items are already counted by their run (failed, cancelled, paused, pending). */
  counts_include_execution?: boolean;
  next?: {
    id: string;
    label: string;
    stage: string;
    fields: Array<{
      label: string;
      value?: string | number | boolean | null;
      format?: "text" | "status" | "date" | "link" | "link_list" | "document" | "provider";
      href?: string | null;
      links?: Array<{ label: string; href: string }>;
    }>;
    execution?: WorkspaceAppDataItem["execution"];
  } | null;
  enabled?: boolean;
  state?: "enabled" | "ended" | string;
  last_status?: string | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  stop_after?: string | null;
  fields?: Array<{
    label: string;
    value?: string | number | boolean | null;
    format?: "text" | "status" | "date" | "link" | "link_list" | "document" | "provider";
    href?: string | null;
    links?: Array<{ label: string; href: string }>;
    /** Raw provider key of a `provider` field; the value is its display label. */
    provider?: string | null;
    secondary?: string | null;
  }>;
  metrics?: WorkspaceAppDataMetric[];
  published_at?: string | null;
  tracking_status?: "pending" | "partial" | "configured" | null;
  conflict?: string | null;
  media?: Array<{
    document_id: string;
    title: string;
    mime_type?: string | null;
    file_size?: number | null;
    updated_at?: string | null;
    viewer_url?: string | null;
    label?: string | null;
  }>;
};

export type WorkspaceAppDataBlockResult = {
  key: string;
  type: WorkspaceAppDataBlockType;
  source: string;
  title?: string | null;
  status: "ready" | "empty" | "waiting" | "stale" | "error" | string;
  items: WorkspaceAppDataItem[];
  summary?: string | null;
  next_cursor?: string | null;
  error?: { code?: string; message?: string } | null;
};

export type WorkspaceAppDataContextsResponse = {
  component: { key: string; title?: string | null; context: WorkspaceAppDataComponent["context"] };
  items: WorkspaceAppDataContext[];
  /** Runs of the producing Flow still making their project (a campaign being prepared), newest first. */
  making?: Array<{
    run_id: string; workflow_id: string; binding_id?: string | null; status: string;
    step?: string | null; started_at?: string | null; progress_percent?: number;
  }>;
  next_cursor?: string | null;
  status: "ready" | "empty" | "waiting" | string;
  errors?: Array<{ code?: string; message?: string; source?: string }>;
};

export type WorkspaceAppDataResponse = {
  component_key: string;
  visible?: boolean;
  context: { kind: WorkspaceAppDataContextKind; id: string };
  status: "ready" | "empty" | "waiting" | "stale" | "error" | string;
  blocks: WorkspaceAppDataBlockResult[];
  errors?: Array<{ code?: string; message?: string; source?: string }>;
};

export type WorkspaceAppAction = {
  key: string;
  kind: "workflow" | "chat" | "automation";
  title: string;
  source_title: string;
  source_description?: string;
  available?: boolean;
  description: string;
  /** The glyph the page config chose for this entry; null lets its name suggest one. */
  icon?: WorkspaceAppEntryIcon | null;
  binding_id?: string;
  workflow_id?: string;
  job_id?: string;
  agent_id?: string;
  agent_name?: string;
  default_agent_id?: string;
  default_agent_name?: string;
  enabled?: boolean;
  last_status?: string | null;
  job_type?: string | null;
  schedule_kind?: string | null;
  every_seconds?: number | null;
  run_at?: string | null;
  cron_expr?: string | null;
  timezone?: string | null;
  instructions?: string | null;
  target_title?: string | null;
  target_workflow_id?: string | null;
  placeholder?: string;
  prompt?: string;
  /** Rules across the Flow's inputs (choose at least one platform), checked before it starts. */
  input_rules?: import("../components/workflows/workflowSchema").WorkflowInputSchema[];
  inputs?: WorkspaceAppActionInput[] | WorkspaceAppFormField[];
};

export type WorkspaceAppViewConfig = {
  version: number;
  eyebrow: string | null;
  title: string | null;
  description: string | null;
  hero: string | null;
  order: string[];
  hidden: string[];
  labels: Record<string, string>;
  descriptions: Record<string, string>;
  forms: Record<string, WorkspaceAppFormField[]>;
  targets: Record<string, string>;
  entries?: Record<string, { title: string }>;
  prompts?: Record<string, string>;
  /** Prefilled Flow inputs per entry, so one Flow can appear as several entries. */
  presets?: Record<string, Record<string, string>>;
  /** Hidden Flow inputs an entry asks for, e.g. the series a "next episode" entry continues. */
  asks?: Record<string, string[]>;
  /** A glyph per entry; absent when the page sets none. */
  icons?: Record<string, WorkspaceAppEntryIcon>;
  /** The Workspace's own templates for an entry's free-text inputs, by action key then input key. */
  choices?: Record<string, Record<string, Array<{
    value: string; label?: string; description?: string; image?: string; presets?: Record<string, string>;
  }>>>;
  visual: "auto" | "image" | "motion" | "plain";
  layout: "feature" | "cover" | "list";
  /** What opens first for someone who has not picked a view of this Workspace; absent = App beside the chat. */
  open_as?: WorkspaceAppDisplayMode;
  /** The App's own cover, shown when the Workspace has none; it travels with the Blueprint. */
  cover_image_url?: string | null;
  /** The App's own look; absent = the platform's (lib/appTheme.ts). */
  theme?: { accent?: string; surface?: "neutral" | "tinted" | "deep"; radius?: "sharp" | "soft" | "round" } | null;
  motion_cover_url: string | null;
  motion_cover_source_url: string | null;
  data_sources?: Record<string, WorkspaceAppDataSource>;
  components?: WorkspaceAppDataComponent[];
  sections?: WorkspaceAppSectionConfig[] | null;
};

/** Must equal the server's APP_OPEN_AS. */
export type WorkspaceAppDisplayMode = "split" | "app" | "chat";

/** Must equal the server's APP_SECTION_BLOCKS keys (guarded by a test). */
export type WorkspaceAppSectionBlock = "connections" | "actions" | "queue" | "metrics" | "outputs";

/** Must equal the server's APP_ENTRY_ICONS (guarded by a test). */
export type WorkspaceAppEntryIcon =
  | "flow" | "chat" | "clock" | "play" | "music" | "audio" | "microphone" | "image" | "palette" | "sparkles"
  | "document" | "report" | "presentation" | "checklist" | "clipboard" | "search" | "megaphone" | "send"
  | "comment" | "mail" | "globe" | "calendar" | "trending" | "rocket" | "academic" | "box" | "cart"
  | "refresh" | "bolt" | "bookmark" | "scale" | "code" | "users" | "dollar" | "heart" | "star";

/** The page's zones, always in this order: notices, functions, data, outputs. */
export type WorkspaceAppZone = "notices" | "functions" | "data" | "outputs";

/** One block's contract as published by the server; clients never hard-code it. */
export type WorkspaceAppBlockSpec = {
  block: WorkspaceAppSectionBlock;
  zone: WorkspaceAppZone;
  /** The first view is the default. */
  views: string[];
  default_source: string | null;
  /** "workflow" means a section may read one flow's runs ("workflow:<binding_id>"). */
  source_kinds: string[];
  multiple: boolean;
  removable: boolean;
  /** How many items the block shows at once. */
  limit: { default: number; max: number } | null;
};

export type WorkspaceAppSectionConfig = {
  id: string;
  block: WorkspaceAppSectionBlock;
  source: string | null;
  view: string;
  limit: number | null;
  /** A metrics section's chosen numbers (stat:<key>, goal:<key>), in order; absent shows them all. */
  items?: string[];
  /** The section's heading instead of its source's name; absent uses the source's. */
  title?: string;
};

export type WorkspaceAppSection = WorkspaceAppSectionConfig & {
  /** True when the page uses the default sections instead of a saved list. */
  implicit: boolean;
  available: boolean;
  source_title: string | null;
};

export type WorkspaceAppOutputFile = {
  document_id?: string;
  name: string;
  fs_path?: string;
  open_url?: string;
  mime?: string;
};

export type WorkspaceAppOutputRun = {
  /** "chat": a request an App entry sent to an Agent; it opens at its reply in chat. */
  kind?: "chat";
  /** For a chat request: the chat message to open (its reply, or the request until one exists). */
  message_id?: string;
  run_id: string;
  workflow_id: string;
  binding_id: string | null;
  /** The App entry the run was started from, when an entry started it. */
  app_action?: string | null;
  /** The action or Flow that made it. */
  title: string;
  /** What came out: the result's own title and line. */
  result_title?: string | null;
  result_summary?: string | null;
  status: string;
  business_outcome: string | null;
  trigger_source: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  files: WorkspaceAppOutputFile[];
  file_count: number;
  /** Person-facing stages from the persisted run summary; absent for older runs. */
  progress?: {
    stages: Array<{ name: string; status: string }>;
    stage_count: number;
    done_count: number;
    current: { id: string | null; name: string | null; index: number | null; status: string; waiting: boolean } | null;
  } | null;
  publications?: Array<{
    platform?: string;
    title?: string;
    published_url?: string;
    verification_status?: string;
    published_at?: string;
    account?: string;
  }>;
};

export type WorkspaceAppQueueItem = WorkspaceAttentionReminder & {
  /** A workflow review's in-place choice and a glimpse of what is waiting. */
  primary_option?: string;
  has_changes_option?: boolean;
  previews?: Array<{ document_id: string; name: string }>;
};

export type WorkspaceAppMetric = {
  kind: "stat" | "goal";
  id: string;
  title: string;
  value_type: string;
  unit: string | null;
  value: number | null;
  updated_at: string | null;
  freshness_status: string;
  /** "agent_review" when an agent assesses the value from evidence. */
  collector_type: string | null;
  /** What the last assessment could not verify. */
  gaps: string[];
  next_assessment_at: string | null;
  /** The agent-review schedule and its latest run, for "assess now" and run links. */
  assessment: {
    next_run_at: string | null;
    job_id: string | null;
    workflow_id: string | null;
    run_id: string | null;
    run_status: string | null;
  } | null;
  goal: {
    id: string;
    title: string;
    target: number | null;
    baseline: number | null;
    deadline: string | null;
    pace_status: string | null;
    status: string;
  } | null;
  series: Array<{ at: string; value: number }>;
};

export type WorkspaceAppSectionData =
  | { id: string; block: "outputs"; items: WorkspaceAppOutputRun[] }
  | { id: string; block: "queue"; items: WorkspaceAppQueueItem[] }
  | { id: string; block: "connections"; items: WorkspaceConnectionRequirement[] }
  | { id: string; block: "metrics"; items: WorkspaceAppMetric[]; suggestions?: WorkspaceAssessmentTemplate[] };

/** An audience metric an agent can assess for a Workspace, named in the user's language. */
export type WorkspaceAssessmentTemplate = { key: string; name: string; unit: string };

export type WorkspaceAppView = {
  workspace_id: string;
  eyebrow: string | null;
  title: string;
  description: string;
  status: string;
  cover_image_url?: string | null;
  cover_source?: "workspace" | "app" | "starter" | "none";
  can_start: boolean;
  hero: string | null;
  actions: WorkspaceAppAction[];
  available_actions: WorkspaceAppAction[];
  /** Absent from older servers; the page then shows only its actions. */
  sections?: WorkspaceAppSection[];
  /** The block catalog the sections follow; absent from older servers. */
  blocks?: WorkspaceAppBlockSpec[];
  workflow_options: Array<{ binding_id: string; workflow_id: string; title: string; description: string; placeholder: string; inputs: WorkspaceAppActionInput[] }>;
  automation_options: Array<{ job_id: string; title: string; description?: string; enabled: boolean } & Pick<WorkspaceAppAction, "last_status" |
    "job_type" | "schedule_kind" | "every_seconds" | "run_at" | "cron_expr" | "timezone" | "instructions" | "target_title" | "target_workflow_id">>;
  agent_options: Array<{ id: string; name: string }>;
  attachable_workflow_options?: Array<{ workflow_id: string; title: string; description: string; inputs: WorkspaceAppActionInput[] }>;
  config: WorkspaceAppViewConfig;
};

export type GlobalChatFlowEntrypoint = WorkspaceChatEntrypoint & {
  workspace_id: string;
  workspace_name: string;
};

export type DocumentMediaKind = "audio" | "video" | "image" | "document";
type DocumentListParams = {
  search?: string;
  folder_id?: string;
  scope?: "all";
  workspace_id?: string;
  include_generated_assets?: boolean;
  /** Only these media kinds, filtered by the server so every page holds matches. */
  kinds?: DocumentMediaKind[];
  /** Leave out files made on the way to a result (QA frames, recipes, timelines). */
  results_only?: boolean;
  /** Skip the folder's total size and count when nothing shows them. */
  include_usage?: boolean;
  limit?: number;
  offset?: number;
};
type DocumentListResponse = PaginatedResponse<Document> & {
  total_files?: number;
  total_size?: number;
  storage_used_mb?: number | null;
  storage_limit_mb?: number | null;
};
type DocumentBrowseResponse = DocumentListResponse & {
  folders: DocumentFolderInfo[];
  documents: Document[];
  total_folders: number;
  total_documents: number;
  direct_total_files?: number;
  direct_total_size?: number;
  max_upload_mb: number;
};
type DocumentBrowsePageResponse = {
  items: Document[];
  folders: DocumentFolderInfo[];
  next_cursor: string | null;
  storage_used_mb: number | null;
  storage_limit_mb: number | null;
  max_upload_mb: number;
};
type DocumentIndexingStatus = Pick<Document, "id" | "vector_status" | "indexing_progress">;

export type VideoEditorFinalizeResult = {
  document: Document;
  render: {
    duration_seconds: number;
    width: number;
    height: number;
    fps: number;
    file_size: number;
    has_audio: boolean;
    video_codec: string;
    audio_codec?: string | null;
  };
};

export type VideoEditorRecipeLinkResult = {
  document: Document;
  recipe: Document;
};

export type FreeMusicTrack = {
  id: string;
  title: string;
  creator: string;
  duration_seconds?: number | null;
  preview_url: string;
  source_url: string;
  license: "pdm" | "cc0" | "by";
  license_name: string;
  license_url: string;
  attribution: string;
  source: string;
  provider: string;
  genres: string[];
};

export type MediaJobPhase = "queued" | "submitting" | "generating" | "saving" | "done" | "failed";

export type MediaJob = {
  id: string;
  status: "pending" | "processing" | "completed" | "failed" | string;
  kind: string;
  prompt: string;
  model?: string | null;
  params: Record<string, any>;
  result_url?: string | null;
  document_id?: string | null;
  error?: string | null;
  duration_seconds?: number | null;
  credits?: number | null;
  byok?: boolean;
  progress?: { phase: MediaJobPhase; at?: string } | null;
  eta_seconds?: number | null;
  regenerated_from?: string | null;
  lineage_root?: string | null;
  run_id?: string | null;
  unit_id?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
};

export type MediaJobRegenerateInput = {
  prompt?: string;
  model?: string;
  duration?: number;
  resolution?: string;
  aspect_ratio?: string;
  keep_seed?: boolean;
};

export type FreeMusicSearchResult = {
  items: FreeMusicTrack[];
  page: number;
  page_count: number;
  total: number;
};

export type FreeMusicImportResult = {
  document: Document;
  track: FreeMusicTrack;
};

function listDocuments(params?: DocumentListParams) {
  const q = new URLSearchParams();
  if (params?.search) q.set("search", params.search);
  if (params?.folder_id !== undefined) q.set("folder_id", params.folder_id);
  if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
  if (params?.include_generated_assets !== undefined) q.set("include_generated_assets", String(params.include_generated_assets));
  for (const kind of params?.kinds || []) q.append("kind", kind);
  if (params?.results_only) q.set("results_only", "true");
  if (params?.include_usage === false) q.set("include_usage", "false");
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  return request<DocumentListResponse>(`/documents?${q}`);
}

function browseDocuments(params?: Pick<DocumentListParams, "search" | "folder_id" | "scope" | "workspace_id" | "include_generated_assets">) {
  const q = new URLSearchParams();
  if (params?.search) q.set("search", params.search);
  if (params?.folder_id !== undefined) q.set("folder_id", params.folder_id);
  if (params?.scope) q.set("scope", params.scope);
  if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
  if (params?.include_generated_assets !== undefined) q.set("include_generated_assets", String(params.include_generated_assets));
  return request<DocumentBrowseResponse>(`/documents/browse?${q}`);
}

function browseDocumentsPage(
  params: Pick<DocumentListParams, "search" | "folder_id" | "scope" | "workspace_id" | "include_generated_assets"> & {
    favorite_only?: boolean;
    file_type_filter?: string;
    sort_by?: "date" | "name" | "size";
    limit?: number;
    cursor?: string;
  },
  signal?: AbortSignal,
) {
  const q = new URLSearchParams();
  if (params.search) q.set("search", params.search);
  if (params.folder_id !== undefined) q.set("folder_id", params.folder_id);
  if (params.scope) q.set("scope", params.scope);
  if (params.workspace_id) q.set("workspace_id", params.workspace_id);
  if (params.include_generated_assets !== undefined) q.set("include_generated_assets", String(params.include_generated_assets));
  if (params.favorite_only) q.set("favorite_only", "true");
  if (params.file_type_filter && params.file_type_filter !== "all") q.set("file_type_filter", params.file_type_filter);
  if (params.sort_by) q.set("sort_by", params.sort_by);
  q.set("limit", String(params.limit ?? 50));
  if (params.cursor) q.set("cursor", params.cursor);
  return request<DocumentBrowsePageResponse>(`/documents/browse-page?${q}`, { signal });
}

/** Documents whose text (or a media file's stored description) matches ``q``. */
function searchDocumentContent(
  params: { q: string; workspace_id?: string; folder_id?: string; limit?: number },
  signal?: AbortSignal,
) {
  const q = new URLSearchParams({ q: params.q, limit: String(params.limit ?? 20) });
  if (params.workspace_id) q.set("workspace_id", params.workspace_id);
  if (params.folder_id) q.set("folder_id", params.folder_id);
  return request<KnowledgeContentSearchResponse>(`/documents/content-search?${q}`, { signal });
}

function listDocumentIndexingStatuses(ids: string[]) {
  const q = new URLSearchParams();
  ids.forEach((id) => q.append("ids", id));
  return request<DocumentIndexingStatus[]>(`/documents/indexing-status?${q}`);
}

async function listAllDocuments(params?: DocumentListParams) {
  const limit = params?.limit || 500;
  const startOffset = params?.offset || 0;
  const first = await listDocuments({ ...params, limit, offset: startOffset });
  const items = [...(first.items || [])];
  const total = first.total ?? items.length;

  while (items.length < total) {
    const next = await listDocuments({ ...params, limit, offset: startOffset + items.length });
    if (!next.items?.length) break;
    items.push(...next.items);
  }

  return { ...first, items, total };
}

function isBackendUnavailableStatus(status: number): boolean {
  return status >= 500;
}

function showBackendUnavailableToast() {
  useToastStore.getState().addToast({
    id: BACKEND_UNAVAILABLE_TOAST_ID,
    type: "warning",
    title: t("lib.api.reconnecting_title"),
    message: t("lib.api.reconnecting_message"),
    duration: 0,
  });
}

function clearBackendUnavailableToast() {
  useToastStore.getState().removeToast(BACKEND_UNAVAILABLE_TOAST_ID);
}

function getStoredLocale(): string {
  return normalizeLocale(localStorage.getItem("manor_locale"));
}

export interface WiringStatus {
  ok: boolean | null;
  detail: string | null;
  mode?: "webhook" | "polling" | null;
  configured_url: string | null;
  expected_url: string | null;
  last_error: string | null;
  pending_update_count: number | null;
}

export interface HealthCheckStatus {
  ok: boolean | null;
  detail: string | null;
  configured_url?: string | null;
  expected_url?: string | null;
}

export interface HealthStatus {
  ok: boolean | null;
  reason_code: string | null;
  detail: string | null;
  latency_ms: number | null;
  checked_at: string | null;
  wiring: WiringStatus | null;
  integration_id: string | null;
  channel_config_id: string | null;
  phone_number_id: string | null;
  waba_id: string | null;
  checks: Record<string, HealthCheckStatus> | null;
}

export interface IntegrationMCPAccount {
  id: string;
  display_name: string | null;
  is_default: boolean;
  kind: "oauth_account" | "integration";
  ownership: "mine" | "shared";
  owner_user_id: string | null;
  owner_display_name: string | null;
  can_manage: boolean;
  can_share: boolean;
  runtime_callable: boolean;
  availability:
    | "callable"
    | "reconnect_required"
    | "permission_denied"
    | "load_failed";
}

export interface IntegrationMCPServer {
  server_key: string;
  server_kind: "managed" | "custom";
  name: string;
  category: string | null;
  description: string | null;
  auth_type: string;
  scopes: string | null;
  tagline: string | null;
  docs_url: string | null;
  app_url?: string | null;
  setup_hint: string | null;
  color_hex: string | null;
  supports_multi_account: boolean;
  capabilities?: string[];
  example_prompts?: string[];
  connections: Array<Omit<IntegrationMCPAccount, "kind"> & {
    kind: "oauth_account";
    provider_user_id: string;
    expires_at: string | null;
    connected_at: string | null;
    health: HealthStatus | null;
  }>;
  entity_accounts: Array<Omit<IntegrationMCPAccount, "kind"> & {
    kind: "integration";
    name: string | null;
    created_at: string | null;
    status: string;
    health: HealthStatus | null;
    nango_backed: boolean;
    whatsapp_readiness_code: string | null;
  }>;
  user_connected: boolean;
  user_expires_at: string | null;
  entity_connected: boolean;
  required_permission: string | null;
  user_has_required_permission: boolean;
  agent_can_use: boolean;
  requires_explicit_account: boolean;
  hint: string;
  setup_pending?: boolean;
  setup_unavailable?: boolean;
  nango_provider_config_key?: string | null;
  oauth_configured?: boolean;
  oauth_client_secret_required?: boolean;
  coming_soon?: boolean;
  plugin_key?: string | null;
  plugin_enabled?: boolean | null;
  plugin_skills?: Array<{
    slug: string;
    display_name: string;
    description: string;
    installed: boolean;
  }>;
  cli_spec?: {
    command_template: string;
    supported_subcommands: string[];
    requires_local_paths: boolean;
    timeout_seconds: number;
    output_format: string;
  } | null;
  browser_spec?: {
    login_url: string;
    session_check_selector: string | null;
    provider_module: string;
    tool_actions: Record<string, any>;
    cookie_ttl_days: number;
  } | null;
}

export interface NangoSyncResponse {
  upserted: number;
  providers: string[];
  integration_id: string | null;
  readiness_code: string | null;
  provisioning_pending: boolean;
  provisioning_detail: string | null;
}

export interface IntegrationOperation {
  name: string;
  tool_name: string;
  label: string;
  resource: string;
  description: string;
  effect: "read" | "write" | "destructive";
  input_schema: {
    type: "object";
    properties: Record<string, Record<string, any>>;
    required: string[];
    [key: string]: any;
  };
  output_schema?: Record<string, any> | null;
  account_input_schemas?: Record<string, {
    type: "object";
    properties: Record<string, Record<string, any>>;
    required: string[];
    [key: string]: any;
  }>;
  account_output_schemas?: Record<string, Record<string, any>>;
  account_ids: string[];
  account_options: Array<{
    id: string;
    display_name?: string;
    provider?: string;
    scope?: string;
    is_default?: boolean;
  }>;
  requires_explicit_account: boolean;
  supports_all_accounts?: boolean | null;
}

export interface IntegrationOperationCatalog {
  server_key: string;
  server_kind: "managed" | "custom";
  source: "builtin" | "cache" | "account_discovery";
  operations: IntegrationOperation[];
}

// ── Workers ─────────────────────────────────────────────────────────


export interface WorkerRegisterRequest {
  kind:
    | "openclaw"
    | "paperclip_bridge"
    | "custom_http"
    | "shell_script"
    | "mcp_reverse";
  display_name: string;
  description?: string | null;
  version?: string | null;
  capabilities: {
    supported_kinds?: string[];
    supported_providers?: string[] | null;
    supported_capabilities?: string[] | null;
    max_concurrent_leases?: number;
    max_risk_level?: "low" | "medium" | "high";
    uses_manor_credentials?: boolean;
    deployment?: "local" | "remote" | "cloud";
    protocol_version: 2;
    [key: string]: unknown;
  };
  trust_level?: "high" | "standard" | "low";
  allowed_ips?: string[] | null;
  monthly_budget_usd?: number | null;
  expires_at?: string | null;
}

export interface WorkerRegisterResponse {
  worker_id: string;
  worker_secret: string;
  expires_at: string | null;
  heartbeat_endpoint: string;
  next_heartbeat_in_seconds: number;
  protocol_version: 2;
}

export interface WorkerResponse {
  id: string;
  entity_id: string;
  kind: string;
  display_name: string;
  description: string | null;
  version: string | null;
  capabilities: Record<string, unknown>;
  trust_level: string;
  status: string;
  last_heartbeat_at: string | null;
  last_seen_ip: string | null;
  consecutive_failures: number;
  monthly_budget_usd: number | null;
  monthly_spent_usd: number;
  expires_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface WorkerSummaryResponse {
  id: string;
  kind: string;
  display_name: string;
  description: string | null;
  version: string | null;
  capabilities: Record<string, unknown>;
  trust_level: string;
  status: string;
  last_heartbeat_at: string | null;
  monthly_budget_usd: number | null;
  monthly_spent_usd: number;
  expires_at: string | null;
}

export interface SubscriptionWorkerBinding {
  subscription_id: string;
  worker_id: string;
  priority: number;
  is_preferred: boolean;
  created_at: string | null;
  worker: WorkerSummaryResponse | null;
}

export interface AgentDeploymentResponse {
  id: string;
  entity_id: string;
  agent_id: string;
  workspace_id: string | null;
  workspace_name: string | null;
  workspace_status: string | null;
  name?: string | null;
  owner_user_id?: string | null;
  owner_name?: string | null;
  owner_display_name?: string | null;
  can_manage?: boolean;
  scope_kind?: "personal" | "workspace" | "legacy" | string;
  conversation_id?: string | null;
  default_conversation_id?: string | null;
  service_key: string | null;
  custom_prompt: string | null;
  config: Record<string, unknown>;
  status: string;
  workers: SubscriptionWorkerBinding[];
  created_at: string | null;
  updated_at: string | null;
}

export interface WorkspaceAgentMapping {
  id: string;
  agent_id: string;
  service_key: string | null;
  custom_prompt: string | null;
  config: Record<string, unknown>;
  created_at: string | null;
  agent: {
    id: string;
    name: string;
    avatar_url: string | null;
    avatar_seed: string;
  } | null;
}



// ── Workspace budget (M8) ──────────────────────────────────────────

export interface WorkspaceBudgetStatus {
  monthly_budget_credits: number | null;
  monthly_spent_credits: number;
  monthly_remaining_credits: number | null;
  pct_used: number | null;
  alert_state: string | null;
  auto_pause_on_budget: boolean;
  budget_reset_at: string | null;
  days_until_month_end: number;
  monthly_budget_usd: number | null;
  monthly_spent_usd: number;
  credits_per_usd: number;
}

export interface WorkspaceBudgetUpdate {
  monthly_budget_credits?: number | null;
  monthly_budget_usd?: number | null;
  auto_pause_on_budget?: boolean;
  reset_alert_state?: boolean;
}

export interface WorkspaceCreditUsageSummary {
  today_credits: number;
  week_credits: number;
  all_time_credits: number;
  timezone: string;
  /** A client delivery trial's provider pays this Workspace's AI usage. */
  paid_by_provider?: boolean;
}

/** One row of the per-source usage report. */
export interface UsageBySourceRow {
  /** Agent / feature name, or "customer_trial" for a client trial this company paid for. */
  source: string;
  /** Server label for a customer_trial row: "Customer trial: <customer> / <Workspace>". */
  label?: string;
  /** "<customer> / <Workspace>" of a customer_trial row. */
  trial_name?: string;
  workspace_id?: string;
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  credit_used: number;
  last_used: string | null;
}

export interface WorkspaceSetupStatus {
  ready: boolean;
  status: string;
  summary: string;
  automatic_setup_state?: "pending" | "running" | "retrying" | "failed" | null;
  automatic_setup_task_id?: string | null;
  incomplete_checks: Array<{
    key?: string;
    kind?: string;
    todo_kind?: string;
    reason?: string;
    provider?: string;
    setup_job_id?: string;
    setup_task_key?: string;
    accepts_text?: boolean;
    [key: string]: unknown;
  }>;
}

export interface BlueprintStartupReconcileResponse {
  state: string;
  dispatched_job_ids: string[];
  blocking_check_keys: string[];
}

export interface WorkspaceEvaluationSnapshot {
  workspace_id: string;
  workspace_name: string;
  generated_at: string;
  window: { days: number; start: string; end: string };
  overall: {
    score: number | null;
    confidence: "low" | "medium" | "high" | string;
    summary: string;
    weights: Record<string, number>;
  };
  dimensions: Record<string, any>;
  recommendations: string[];
  evidence_summary: Record<string, number>;
  history?: Array<{
    evidence_id: string;
    recorded_at: string | null;
    source: string;
    overall_score: number | null;
    confidence?: string | null;
    window_days?: number | null;
    dimension_scores?: Record<string, number | null>;
  }>;
  trend?: {
    previous_score?: number | null;
    delta?: number | null;
    direction?: "improving" | "declining" | "flat" | "unknown" | string;
  };
}

// ── Workspace Draft (conversational creation) ─────────────────────────

export interface WorkspaceDraftBlueprintSuggestion {
  id: string;
  title: string;
  summary: string | null;
  tags: string[];
  install_count: number;
}

export interface WorkspaceDraftMessage {
  role: "user" | "assistant";
  content: string;
}

export interface WorkspaceDraft {
  id: string;
  entity_id: string;
  user_id: string | null;
  status: "active" | "ready" | "finalized" | "abandoned";
  fields: Record<string, unknown>;
  messages: WorkspaceDraftMessage[];
  missing: string[];
  ready: boolean;
  suggested_blueprint: WorkspaceDraftBlueprintSuggestion | null;
  applied_blueprint_id: string | null;
  finalized_workspace_id: string | null;
  finalized_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface WorkspaceDraftTurn {
  reply: string;
  draft: WorkspaceDraft;
}

export interface WorkspaceDraftFinalize {
  workspace_id: string;
  draft: WorkspaceDraft;
  strategist_eta_seconds?: number | null;
  dispatch_warning?: string | null;
}

// ── Workspace Blueprint / Marketplace types (M12) ─────────────────────

export type WorkspaceConnectionRequirement = WorkspaceConnectionStatus["requirements"][number];

export interface WorkspaceConnectionStatus {
  workspace_id: string;
  required_issue_count: number;
  requirements: Array<{
    key: string;
    kind: "integration" | "channel" | "browser_session";
    provider: string;
    label: string;
    required: boolean;
    ready: boolean;
    reason: string;
    service_keys: string[];
    setup_kind?: string | null;
    /** Set when another connection (Chrome) already covers this one. */
    covered_by?: string | null;
  }>;
}

export interface BlueprintSetupItem {
  label: string;
  key: string | null;
  kind: string | null;
  required: boolean;
  purpose: string | null;
  default: unknown;
}

export interface BlueprintSetupPreview {
  use_when: string | null;
  maturity_level: string | null;
  validation_summary: string | null;
  primary_work: string | null;
  runnable_in_simulation: boolean;
  blocking_todos_expected: number | null;
  required_variables: BlueprintSetupItem[];
  optional_variables: BlueprintSetupItem[];
  required_channels: BlueprintSetupItem[];
  optional_channels: BlueprintSetupItem[];
  required_sessions: BlueprintSetupItem[];
  optional_sessions: BlueprintSetupItem[];
  required_integrations: BlueprintSetupItem[];
  optional_integrations: BlueprintSetupItem[];
  first_week_outputs: string[];
  validation_evidence: string[];
  acceptance_criteria: string[];
  not_included: string[];
  services: BlueprintSetupItem[];
  rules: string[];
  author_hard_blocks: string[];
}

export interface BlueprintShowcaseAsset {
  id: string;
  kind: "image" | "video";
  url: string;
  caption: string | null;
  alt_text: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  uploaded_at: string | null;
}

export interface BlueprintCoverTemplate {
  motif:
    | "analytics"
    | "commerce"
    | "content"
    | "distribution"
    | "service"
    | "video"
    | "workspace";
  palette: "blue" | "peach" | "sage" | "stone";
  variant: 0 | 1 | 2;
  seed: number;
}

export interface BlueprintSummary {
  id: string;
  slug: string;
  title: string;
  summary: string | null;
  tags: string[];
  status: "draft" | "pending_review" | "published" | "archived";
  install_count: number;
  payload_version: string;
  content_version: string;
  source_workspace_id: string | null;
  cover_image_url: string | null;
  showcase_assets: BlueprintShowcaseAsset[];
  cover_template?: BlueprintCoverTemplate;
  author_handle: string | null;
  author_display_name: string | null;
  author_avatar_url: string | null;
  remixed_from_id: string | null;
  favorite_count: number;
  is_favorited: boolean;
  remix_count: number;
  setup_preview: BlueprintSetupPreview;
  created_at: string;
  updated_at: string | null;
  published_at: string | null;
  has_share_token?: boolean;
  /** Owner-only verified-email allowlist for a private install link. */
  share_allowed_emails?: string[];
  // Whether the current caller owns this blueprint row.
  is_owner?: boolean;
}


export interface BlueprintDetail extends BlueprintSummary {
  description: string | null;
  payload: Record<string, unknown>;
}


// Mirrors backend UpdateBlueprintRequest (apps/api/routers/blueprints.py):
// only these fields are writable via PUT /blueprints/{id}.
export interface UpdateBlueprintRequest {
  title?: string;
  summary?: string;
  description?: string;
  usage_guide?: string;
  tags?: string[];
  cover_image_url?: string;
}

export interface ExportBlueprintPayloadRequest {
  title?: string;
  summary?: string;
  description?: string;
  usage_guide?: string;
  tags?: string[];
  author_handle?: string;
  author_display_name?: string;
  install_variables?: Array<{
    key: string;
    label: string;
    purpose?: string;
    required: boolean;
    default?: unknown;
    materialize?: boolean;
  }>;
  include_subscriptions?: boolean;
  include_goals?: boolean;
  include_stats?: boolean;
  include_scheduled_jobs?: boolean;
  include_workflows?: boolean;
  include_blocking_setup?: boolean;
  include_custom_fields?: boolean;
  include_governance?: boolean;
  include_channel_requirements?: boolean;
  include_session_requirements?: boolean;
  include_embedded_agents?: boolean;
  include_embedded_skills?: boolean;
  include_knowledge_packs?: boolean;
  knowledge_pack_mode?: "skeleton" | "inline_text";
  include_starter_memory?: boolean;
  include_memory_files?: boolean;
  knowledge_document_ids?: string[];
}

export interface ExportBlueprintRequest extends ExportBlueprintPayloadRequest {
  marketplace_blueprint_id?: string;
  slug: string;
  title: string;
  cover_image_url?: string;
  replace_existing?: boolean;
}

export interface BlueprintExportKnowledgeDocument {
  id: string;
  name: string;
  path: string;
  file_size: number;
  groups: Array<{ id: string; name: string }>;
  groups_truncated?: boolean;
  content_source?: "initial_template" | "current_content";
  required_by_workflows?: string[];
}

export type InstallMode = "simulate" | "live";
export type GovernancePresetKey = "automatic" | "follow_default" | "confirm_changes" | "risk_based";

export interface GovernancePolicy {
  max_risk_level: "low" | "medium" | "high";
  budget_caps_per_kind: Record<string, number>;
}

export interface GovernancePolicyResponse {
  workspace_id: string;
  revision: number;
  policy: GovernancePolicy;
  updated_by?: string | null;
  updated_at?: string | null;
}

// ── M14 observability (apps/api/routers/observability.py) ─────────────

export interface TimelineEventDigest {
  id: string;
  event_type: string;
  source_kind: string;
  source_id: string;
  run_id?: string | null;
  status?: string | null;
  actor_kind?: string | null;
  actor_id?: string | null;
  root_execution_id?: string | null;
  causation_id?: string | null;
  correlation_id?: string | null;
  occurred_at?: string | null;
  payload?: Record<string, any>;
  payload_keys?: string[];
}

export interface StrategyReviewDigest {
  id: string;
  trigger_kind: string;
  status: string;
  skip_reason?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  watermark_start?: string | null;
  watermark_end?: string | null;
  workspace_revision?: number | null;
  policy_revision?: number | null;
  created_at?: string | null;
  completed_at?: string | null;
}

export interface StrategyReviewListEntry extends StrategyReviewDigest {
  reports: { complete: number; partial: number; failed: number };
  proposal_id?: string | null;
  item_counts: { kind: string; status: string; count: number }[];
}

export interface StrategyProposalItem {
  id: string;
  item_key: string;
  kind: string;
  status: string;
  risk_level: string;
  action_key: string;
  decision?: { decision?: string; decided_by?: string; reason_code?: string; comment?: string } | null;
  execution_root_id?: string | null;
  created_at?: string | null;
  decided_at?: string | null;
  payload?: Record<string, any>;
  basis?: { report_refs?: string[]; evidence_refs?: string[] } | null;
}

export interface StrategyReviewDetail {
  review: StrategyReviewDigest & { briefing?: Record<string, any> | null; error?: string | null };
  reports: {
    id: string;
    domain: string;
    status: string;
    summary?: string | null;
    metrics?: Record<string, any> | null;
    observations?: any[] | null;
    uncertainties?: any[] | null;
    coverage?: Record<string, any> | null;
    created_at?: string | null;
  }[];
  proposal?: {
    id: string;
    summary: string;
    notes?: string | null;
    status: string;
    created_at?: string | null;
    resolved_at?: string | null;
    items: StrategyProposalItem[];
  } | null;
}

export interface WorkspaceTimelineResponse {
  selector: Record<string, string>;
  events?: TimelineEventDigest[];
  causes?: Record<string, string[]>;
  review?: StrategyReviewDigest;
  reports?: StrategyReviewDetail["reports"];
  proposal?: StrategyReviewDetail["proposal"];
  executions?: Record<string, TimelineEventDigest[]>;
}

export interface AutomationHealthEntry {
  id: string;
  name: string;
  kind: "scheduled_job" | "workflow_binding";
  schedule: {
    kind?: string | null;
    cron_expr?: string | null;
    every_seconds?: number | null;
    run_at?: string | null;
    timezone?: string | null;
  };
  revision: number;
  consecutive_errors: number;
  last_run_at?: string | null;
  last_status?: string | null;
  runs_30d: { dispatched: number; completed: number; failed: number; missed: number };
  active_experiment?: { id: string; status?: string | null } | null;
}

// Mirrors backend HumanParticipationResponse (apps/api/routers/humans.py).
// M9.6 privacy boundary: participants carry declared facts + an open-work
// count only — never a per-person timing / efficiency metric — and
// contributions carry changed FIELD NAMES only.
export interface HumanQueueCommitment {
  id: string;
  request_kind: string;
  participant_id?: string | null;
  role_required?: string | null;
  source_kind: string;
  source_id: string;
  expected_input?: string | null;
  status: string;
  requested_at?: string | null;
  expected_by?: string | null;
  blocking: boolean;
  blocking_execution_ids: string[];
  age_hours?: number | null;
}

export interface HumanBlockingEntry {
  commitment_id: string;
  request_kind: string;
  expected_by?: string | null;
  execution_ids: string[];
  blocked: { id: string; kind: string; title?: string | null; status?: string | null }[];
}

export interface HumanParticipantDigest {
  user_id: string;
  display_name?: string | null;
  roles: string[];
  declared_capabilities: string[];
  availability: { timezone?: string | null; out_of_office: boolean };
  open_commitments_count: number;
}

export interface HumanContributionDigest {
  kind: string;
  target_kind: string;
  target_id: string;
  fields_changed: string[];
  created_at?: string | null;
}

export interface HumanDecisionDigest {
  item_id: string;
  kind?: string | null;
  decision: string;
  reason_code?: string | null;
  decided_at?: string | null;
}

export interface HumanParticipationResponse {
  queue: HumanQueueCommitment[];
  blocking: HumanBlockingEntry[];
  participants: HumanParticipantDigest[];
  recent_contributions: HumanContributionDigest[];
  decisions: HumanDecisionDigest[];
}

// Mirrors the provenance endpoint (apps/api/routers/observability.py).
export interface ProvenanceStep {
  kind: string;
  id?: string | null;
  label?: string | null;
}

export interface TaskProvenanceResponse {
  task: { id: string; title: string; status: string };
  root_execution_id: string;
  trigger: ProvenanceStep;
  causation_chain: ProvenanceStep[];
  config_versions: Record<string, number | string>;
  events: TimelineEventDigest[];
}

export interface InstallBlueprintRequest {
  mode?: InstallMode;
  workspace_name?: string;
  create_missing_agents?: boolean;
  agent_ids_by_service_key?: Record<string, string>;
  variable_values?: Record<string, unknown>;
  channel_config_ids?: Record<string, string>;
  governance_preset?: GovernancePresetKey;
  install_author_hard_blocks?: boolean;
  share_token?: string;
}

export interface BlueprintInstallPreflightRequirement {
  kind: "integration" | "channel" | "browser_session";
  provider: string;
  label: string;
  required: boolean;
  blocking: boolean;
  ready: boolean;
  reason: string;
  purpose: string | null;
  setup_kind: string | null;
  scope: string | null;
  config_fields_to_set: string[];
  requirement_key: string | null;
  resource_id: string | null;
  resource_options: Array<{ id: string; label: string }>;
}

export interface BlueprintInstallPreflight {
  ready: boolean;
  blocking_count: number;
  requirements: BlueprintInstallPreflightRequirement[];
}



export interface InstallTodo {
  kind: "channel" | "browser_session" | "missing_agent" | "note";
  detail: string;
  payload: Record<string, unknown>;
  blocking: boolean;
}

export interface InstallBlueprintResponse {
  workspace_id: string;
  mode: string;
  blueprint_id: string | null;
  blueprint_slug: string | null;
  stat_ids: string[];
  goal_ids: string[];
  subscription_ids: string[];
  scheduled_job_ids: string[];
  workflow_binding_ids: string[];
  custom_field_ids: string[];
  governance_applied: boolean;
  todos: InstallTodo[];
  notes: string[];
}

export type WorkspaceSimulationRun = {
  workspace_id: string;
  enabled: boolean;
  run_id: string | null;
  status: "idle" | "running" | "waiting" | "completed";
  title: string;
  goal_title: string;
  stage_index: number;
  stage_count: number;
  stage_id: string | null;
  stage_title: string;
  waiting_message_id: string | null;
  last_message_id: string | null;
  started_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  decision_count: number;
  runtime_version: string;
};

export interface GovernancePresetSummary {
  key: GovernancePresetKey;
  title: string;
  summary: string;
}

export interface UnmetRequirement {
  kind: "channel" | "browser_session";
  detail: string;
  payload: Record<string, unknown>;
}

export interface PromoteResponse {
  workspace_id: string;
  promoted: boolean;
  unmet: UnmetRequirement[];
  notes: string[];
}

// ── Simulation report types ───────────────────────────────────────────

export interface ActivitySection {
  total_steps: number;
  by_status: Record<string, number>;
  by_kind: Record<string, number>;
  by_action_key: Record<string, number>;
  governance_paused: number;
  governance_denied: number;
}

export interface CostSection {
  total_credits: number;
  total_usd: number;
  by_kind_credits: Record<string, number>;
  simulation_days: number;
  daily_avg_credits: number;
  projected_monthly_credits: number;
  /** Credits the billing ledger charged; projection uses the larger of this and total_credits. */
  billed_credits: number;
  /** Spent credits per completed task or successful workflow run. */
  credits_per_outcome: number | null;
}

export interface RunBreakdown {
  key: string;
  label: string;
  total: number;
  succeeded: number;
  failed: number;
  success_rate: number | null;
  /** succeeded + failed — the runs the rate is computed over. */
  judged: number;
}

export interface RunOutcomes {
  total: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  in_flight: number;
  /** Runs that needed more than one attempt. */
  retried: number;
  /** succeeded / (succeeded + failed); cancellations are reported beside it. Null when nothing was judged. */
  success_rate: number | null;
  avg_duration_seconds: number | null;
  top_errors: { message: string; count: number }[];
  breakdown: RunBreakdown[];
  /** Agents / workflows / jobs with enough runs whose own rate misses the target, worst first. */
  below_target: RunBreakdown[];
  /** Finished runs per UTC day, oldest first. */
  daily: { day: string; succeeded: number; failed: number }[];
}

export interface HumanLoad {
  requested: number;
  approved: number;
  denied: number;
  expired: number;
  pending: number;
  per_day: number;
  median_wait_minutes: number | null;
  oldest_pending_hours: number | null;
  /** Review / authorize requests a person approved or denied. */
  accepted: number;
  rejected: number;
}

export interface OperationsSection {
  tasks: RunOutcomes;
  agents: RunOutcomes;
  workflows: RunOutcomes;
  automations: RunOutcomes;
  human: HumanLoad;
  stalled_tasks: number;
  stalled_workflow_runs: number;
  auto_paused_automations: string[];
  failing_automations: string[];
}

export type LaunchCheckStatus = "pass" | "warn" | "fail" | "na";
export type LaunchVerdict = "ready" | "at_risk" | "blocked" | "insufficient_data";

export interface LaunchCheck {
  key: string;
  status: LaunchCheckStatus;
  /** Stable code; the UI localises it with ``values``. */
  reason: string;
  values: Record<string, string | number | null>;
  details: string[];
}

export interface LaunchReadiness {
  verdict: LaunchVerdict;
  checks: LaunchCheck[];
}

export type ValueVerdict = "on_track" | "concerns" | "below_expectations" | "unproven";

/** Goal pace and output acceptance, judged apart from operational readiness. */
export interface ValueEvidence {
  verdict: ValueVerdict;
  checks: LaunchCheck[];
}

export interface CounterfactualOutcome {
  preset_key: GovernancePresetKey;
  title: string;
  allowed: number;
  paused_for_hitl: number;
  denied: number;
  delta_blocked_vs_actual: number;
}

export interface GoalPace {
  goal_id: string;
  title: string;
  metric_key: string;
  target_value: number | null;
  baseline_value: number | null;
  first_measurement_value: number | null;
  last_measurement_value: number | null;
  measurement_count: number;
  progress_fraction: number | null;
}

export interface SimulationReport {
  workspace_id: string;
  workspace_name: string;
  in_simulation: boolean;
  governance_preset: GovernancePresetKey | null;
  window_start: string | null;
  window_end: string;
  /** Simulation start; window_start is later when recent_days narrowed it. */
  simulation_started_at: string | null;
  /** Set when a simulation was promoted; null for running or never-simulated Workspaces. */
  promoted_at: string | null;
  activity: ActivitySection;
  cost: CostSection;
  counterfactuals: CounterfactualOutcome[];
  goals: GoalPace[];
  operations: OperationsSection;
  readiness: LaunchReadiness;
  value: ValueEvidence;
  notes: string[];
}

let _sessionRedirecting = false;
/** A 401 from a normal API call means the session expired mid-use. Clear it and
 *  route to /login once — so an expired token never silently dead-ends an
 *  in-app action (e.g. clicking "add node" and nothing happening). The
 *  ``/auth/*`` endpoints legitimately 401 on bad credentials, so they're left to
 *  show their own inline error instead of redirecting. */
export function handleSessionExpired(path = "") {
  if (typeof window === "undefined" || _sessionRedirecting) return;
  if (path.startsWith("/auth/") && path !== "/auth/renew") return;
  // The embedded frame has no sign-in of its own: it shows that access ended.
  if (EMBED) {
    endEmbeddedSession();
    return;
  }
  if (window.location.pathname.startsWith("/login")) return;
  // The companion composer stays in its frame and offers an explicit sign-in
  // link. Other app routes (including login) must not become frameable.
  if (window.parent !== window && ["/task-import/embed", "/calendar-picker/embed"].includes(window.location.pathname)) {
    _sessionRedirecting = true;
    window.dispatchEvent(new Event("manor:companion-session-ended"));
    clearAuthBrowserState();
    _sessionRedirecting = false;
    return;
  }
  _sessionRedirecting = true;
  clearAuthBrowserState();
  const next = window.location.pathname + window.location.search + window.location.hash;
  window.location.assign(signInPath(next));
}

function handleUnauthorizedResponse(path: string, requestToken: string | null, detail?: unknown) {
  // A late response from an old account/token must not sign out the new one.
  if (requestToken !== getAuthToken()) {
    if (!getAuthToken()) notifyAuthTokenChanged();
    return;
  }
  // Session recovery could not move an ended collaboration home (no
  // remember-me cookie): the sign-in page explains why and goes home.
  if (requestToken && _extractCodedDetail(detail).code === "collaboration_ended") markCollaborationEndedSignIn();
  if (requestToken) handleSessionExpired(path);
  // Missing credentials can coexist with a stale in-memory signed-in UI.
  // The session boundary reconciles it; truly anonymous public pages stay put.
  else notifyAuthTokenChanged();
}

/**
 * For paths that never replay through session refresh (uploads, streams): a
 * 401 for an ended collaboration moves the session to the person's own
 * company instead of signing them out. True when it did; the request is then
 * abandoned, never replayed in that company.
 */
async function leftEndedCollaboration(detail: unknown, requestToken: string | null): Promise<boolean> {
  if (_extractCodedDetail(detail).code !== "collaboration_ended" || !requestToken || requestToken !== getAuthToken()) return false;
  return restorePersistentSession().catch(() => false);
}

export class ApiError extends Error {
  /** Structured detail body from FastAPI (e.g. require_plan's
   *  ``{message, limit, current, plan}`` dict, or our CodedError's
   *  ``{code, message, vars}``). Set when the server returns ``detail``
   *  as an object instead of a string. */
  detail?: Record<string, unknown>;
  /** Stable i18n key from backend CodedError. When present, prefer
   *  ``t(code, vars)`` over the raw ``message`` for display. See
   *  ``lib/translateApiError.ts``. */
  code?: string;
  /** Variable substitutions for ``code`` interpolation
   *  (e.g. ``{ name: "foo.pdf" }``). */
  vars?: Record<string, string | number>;
  /** Remaining server-directed cooldown for rate limits or unavailable dependencies. */
  retryAfterMs?: number;
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export type DocumentUploadPhase = "uploading" | "processing";

export interface DocumentUploadProgress {
  phase: DocumentUploadPhase;
  loaded: number;
  total: number;
  percent: number;
}

export interface DocumentUploadRequestOptions {
  signal?: AbortSignal;
  onProgress?: (progress: DocumentUploadProgress) => void;
  /** Abort only while the browser has stopped sending bytes for this long. */
  stallTimeoutMs?: number;
  /** Bound the server-side validation/persistence phase after all bytes arrive. */
  processingTimeoutMs?: number;
  /** Stable across retries so an ambiguous completion cannot create a duplicate. */
  idempotencyKey?: string;
  /** Bound receipt reconciliation after the browser times out waiting for processing. */
  receiptReconcileTimeoutMs?: number;
}

export class DocumentUploadStalledError extends Error {
  constructor(public phase: DocumentUploadPhase = "uploading") {
    super(phase === "processing"
      ? "Upload processing did not finish in time"
      : "Upload stopped making progress");
    this.name = "DocumentUploadStalledError";
  }
}

/**
 * A request abandoned because an ended collaboration moved the session to the
 * person's own company. It is a cancellation (name "AbortError", so abort
 * handling applies) with no message: the collaboration-ended notice already
 * tells the person, and inline error displays have nothing to show.
 */
export class SessionMovedError extends Error {
  constructor() {
    super("");
    this.name = "AbortError";
  }
}

export class DocumentUploadAuthChangedError extends Error {
  constructor() {
    super("Upload stopped because the authenticated account changed");
    this.name = "DocumentUploadAuthChangedError";
  }
}

export function shouldShowRequestErrorToast(
  status: number,
  method: string | undefined,
  suppressed: boolean,
): boolean {
  if (suppressed || status === 401 || status === 403) return false;
  const normalizedMethod = (method || "GET").toUpperCase();
  if (normalizedMethod === "GET" && (status === 404 || status === 410)) {
    return false;
  }
  return true;
}

export function shouldRetryApiQuery(failureCount: number, error: unknown): boolean {
  if (
    error instanceof ApiError
    && error.status >= 400
    && error.status < 500
  ) {
    return false;
  }
  return failureCount < 1;
}

/** Pull i18n code + vars out of a FastAPI error response's structured
 *  ``detail`` body. Returns an empty object for string details, missing
 *  bodies, or any shape that doesn't carry a backend CodedError. */
function _extractCodedDetail(detail: unknown): {
  code?: string;
  vars?: Record<string, string | number>;
} {
  if (!detail || typeof detail !== "object") return {};
  const d = detail as Record<string, unknown>;
  const code = typeof d.code === "string" ? d.code : undefined;
  const vars =
    d.vars && typeof d.vars === "object"
      ? (d.vars as Record<string, string | number>)
      : undefined;
  return { code, vars };
}


/** Translate an ApiError (or any error) for display. Prefers the
 *  backend-supplied i18n ``code`` (with optional ``vars``); falls back
 *  to the raw error message; finally falls back to a generic string.
 *
 *  Use this in component-level error handlers so users see localized
 *  text:
 *
 *    catch (e) {
 *      setError(translateApiError(e));
 *    }
 */
export function translateApiError(err: unknown, fallback?: string): string {
  if (err instanceof ApiError) {
    if (err.code) {
      const translated = t(err.code, err.vars);
      // t() returns the key itself when no translation exists — if that
      // happens, fall back to the backend's English message.
      if (translated !== err.code) return translated;
    }
    return err.message || fallback || t("lib.api.request_failed");
  }
  if (err instanceof Error) return err.message || fallback || t("lib.api.request_failed");
  return fallback || t("lib.api.request_failed");
}

/** Which plan limit was hit — drives the reminder's title/CTA. */
export type PlanLimitKind = "credit" | "trial_credit" | "storage" | "workspaces" | "users" | "generic";

/** Shape of FastAPI's plan-gate 402 response detail. */
export interface PlanLimitDetail {
  message: string;
  limit: number | null;
  current: number | null;
  plan: string;
  /** Limit type; absent on legacy payloads → treated as "credit". */
  kind?: PlanLimitKind;
  /** Exact billing-period boundary for the next included-credit refill. */
  resets_at?: string | null;
  /** Safe in-app destination used to continue an interrupted paid action. */
  return_to?: string | null;
}

type DocumentDownloadCacheEntry = {
  blob: Blob;
  expiresAt: number;
  lastAccessed: number;
  size: number;
};

export type ProtectedFileResponseOptions = { signal?: AbortSignal };
type DocumentBlobOptions = ProtectedFileResponseOptions & { cache?: boolean; force?: boolean };
type DocumentResponseOptions = ProtectedFileResponseOptions & { force?: boolean };
type DocumentThumbnailOptions = DocumentBlobOptions & {
  persistent?: boolean;
  version?: string | null;
};

type PersistentThumbnailCacheIndexEntry = {
  documentId: string;
  version: string;
  size: number;
  expiresAt: number;
  lastAccessed: number;
};

const DOCUMENT_DOWNLOAD_CACHE_TTL_MS = 60 * 60 * 1000;
const DOCUMENT_DOWNLOAD_CACHE_MAX_ITEMS = 96;
const DOCUMENT_DOWNLOAD_CACHE_MAX_BYTES = 512 * 1024 * 1024;
const PERSISTENT_THUMBNAIL_CACHE_INDEX_KEY = "manor:document-thumbnail-cache:v1:index";
const PERSISTENT_THUMBNAIL_CACHE_KEY_PREFIX = "manor:document-thumbnail-cache:v1:item:";
const PERSISTENT_THUMBNAIL_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
// Protected blobs must reach the API for current access checks. Redis keeps
// versioned thumbnail bytes hot for a week; browser persistence bypasses ACL
// revocation and can survive an account switch on a shared computer.
const ALLOW_PERSISTENT_PROTECTED_DOCUMENT_CACHE = false;
const PERSISTENT_THUMBNAIL_CACHE_MAX_ITEMS = 48;
const PERSISTENT_THUMBNAIL_CACHE_MAX_BYTES = 4 * 1024 * 1024;
const PERSISTENT_THUMBNAIL_CACHE_MAX_ENTRY_BYTES = 768 * 1024;
const documentDownloadCache = new Map<string, DocumentDownloadCacheEntry>();
const documentDownloadInflight = new Map<string, Promise<Blob>>();

function getDocumentDownloadCacheSize(): number {
  let size = 0;
  documentDownloadCache.forEach((entry) => {
    size += entry.size;
  });
  return size;
}

function getPersistentThumbnailStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function normalizePersistentThumbnailVersion(version?: string | null): string {
  const normalized = String(version || "default").trim();
  return normalized ? normalized.slice(0, 160) : "default";
}

function persistentThumbnailCacheKey(id: string, version?: string | null): string {
  return [
    PERSISTENT_THUMBNAIL_CACHE_KEY_PREFIX,
    encodeURIComponent(id),
    ":",
    encodeURIComponent(normalizePersistentThumbnailVersion(version)),
  ].join("");
}

function readPersistentThumbnailIndex(): Record<string, PersistentThumbnailCacheIndexEntry> {
  const storage = getPersistentThumbnailStorage();
  if (!storage) return {};
  try {
    const raw = storage.getItem(PERSISTENT_THUMBNAIL_CACHE_INDEX_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed as Record<string, PersistentThumbnailCacheIndexEntry> : {};
  } catch {
    return {};
  }
}

function writePersistentThumbnailIndex(index: Record<string, PersistentThumbnailCacheIndexEntry>) {
  const storage = getPersistentThumbnailStorage();
  if (!storage) return;
  try {
    if (Object.keys(index).length === 0) {
      storage.removeItem(PERSISTENT_THUMBNAIL_CACHE_INDEX_KEY);
    } else {
      storage.setItem(PERSISTENT_THUMBNAIL_CACHE_INDEX_KEY, JSON.stringify(index));
    }
  } catch {
    // localStorage can be unavailable or quota-limited. Thumbnail caching is best-effort.
  }
}

function removePersistentThumbnailCacheKey(key: string, index: Record<string, PersistentThumbnailCacheIndexEntry>) {
  const storage = getPersistentThumbnailStorage();
  try {
    storage?.removeItem(key);
  } catch {
    // Best-effort cleanup.
  }
  delete index[key];
}

function getPersistentThumbnailIndexSize(index: Record<string, PersistentThumbnailCacheIndexEntry>): number {
  return Object.values(index).reduce((sum, entry) => sum + (Number(entry.size) || 0), 0);
}

function prunePersistentThumbnailCache(protectedKey?: string) {
  const storage = getPersistentThumbnailStorage();
  if (!storage) return;
  const now = Date.now();
  const index = readPersistentThumbnailIndex();

  Object.entries(index).forEach(([key, entry]) => {
    if (key === protectedKey) return;
    if (!key.startsWith(PERSISTENT_THUMBNAIL_CACHE_KEY_PREFIX) || entry.expiresAt <= now || !storage.getItem(key)) {
      removePersistentThumbnailCacheKey(key, index);
    }
  });

  while (
    Object.keys(index).length > PERSISTENT_THUMBNAIL_CACHE_MAX_ITEMS ||
    getPersistentThumbnailIndexSize(index) > PERSISTENT_THUMBNAIL_CACHE_MAX_BYTES
  ) {
    const [oldestKey] = Object.entries(index)
      .filter(([key]) => key !== protectedKey)
      .sort((a, b) => a[1].lastAccessed - b[1].lastAccessed)[0] || [];
    if (!oldestKey) break;
    removePersistentThumbnailCacheKey(oldestKey, index);
  }

  writePersistentThumbnailIndex(index);
}

function readPersistentThumbnailDataUrl(id: string, version?: string | null): string | null {
  const storage = getPersistentThumbnailStorage();
  if (!storage) return null;
  const key = persistentThumbnailCacheKey(id, version);
  const index = readPersistentThumbnailIndex();
  const entry = index[key];
  if (!entry) return null;

  const now = Date.now();
  const dataUrl = storage.getItem(key);
  if (!dataUrl || entry.expiresAt <= now) {
    removePersistentThumbnailCacheKey(key, index);
    writePersistentThumbnailIndex(index);
    return null;
  }

  index[key] = {
    ...entry,
    expiresAt: now + PERSISTENT_THUMBNAIL_CACHE_TTL_MS,
    lastAccessed: now,
  };
  writePersistentThumbnailIndex(index);
  return dataUrl;
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") resolve(reader.result);
      else reject(new Error("Thumbnail conversion failed"));
    };
    reader.onerror = () => reject(reader.error || new Error("Thumbnail conversion failed"));
    reader.readAsDataURL(blob);
  });
}

function writePersistentThumbnailDataUrl(
  id: string,
  version: string | null | undefined,
  dataUrl: string,
): string | null {
  const storage = getPersistentThumbnailStorage();
  if (!dataUrl || dataUrl.length > PERSISTENT_THUMBNAIL_CACHE_MAX_ENTRY_BYTES) return null;
  if (!storage) return null;

  const key = persistentThumbnailCacheKey(id, version);
  const now = Date.now();
  const index = readPersistentThumbnailIndex();
  const entry: PersistentThumbnailCacheIndexEntry = {
    documentId: id,
    version: normalizePersistentThumbnailVersion(version),
    size: dataUrl.length,
    expiresAt: now + PERSISTENT_THUMBNAIL_CACHE_TTL_MS,
    lastAccessed: now,
  };

  const store = () => {
    storage.setItem(key, dataUrl);
    index[key] = entry;
    writePersistentThumbnailIndex(index);
    prunePersistentThumbnailCache(key);
  };

  try {
    store();
    return dataUrl;
  } catch {
    prunePersistentThumbnailCache();
    try {
      store();
      return dataUrl;
    } catch {
      removePersistentThumbnailCacheKey(key, index);
      writePersistentThumbnailIndex(index);
      return null;
    }
  }
}

async function writePersistentThumbnailBlob(
  id: string,
  version: string | null | undefined,
  blob: Blob,
): Promise<string | null> {
  if (blob.size > PERSISTENT_THUMBNAIL_CACHE_MAX_ENTRY_BYTES) return null;
  const dataUrl = await blobToDataUrl(blob).catch(() => null);
  return dataUrl ? writePersistentThumbnailDataUrl(id, version, dataUrl) : null;
}

function invalidatePersistentThumbnailCache(id?: string) {
  const storage = getPersistentThumbnailStorage();
  if (!storage) return;
  const index = readPersistentThumbnailIndex();
  Object.entries(index).forEach(([key, entry]) => {
    if (!id || entry.documentId === id) {
      removePersistentThumbnailCacheKey(key, index);
    }
  });
  writePersistentThumbnailIndex(index);
}

function pruneDocumentDownloadCache(protectedId?: string) {
  const now = Date.now();
  documentDownloadCache.forEach((entry, id) => {
    if (id !== protectedId && entry.expiresAt <= now) {
      documentDownloadCache.delete(id);
    }
  });

  while (
    documentDownloadCache.size > DOCUMENT_DOWNLOAD_CACHE_MAX_ITEMS ||
    getDocumentDownloadCacheSize() > DOCUMENT_DOWNLOAD_CACHE_MAX_BYTES
  ) {
    const candidates = [...documentDownloadCache.entries()]
      .filter(([id]) => id !== protectedId)
      .sort((a, b) => a[1].lastAccessed - b[1].lastAccessed);
    const oldest = candidates[0]?.[0];
    if (!oldest) break;
    documentDownloadCache.delete(oldest);
  }
}

function invalidateDocumentDownloadCache(id?: string) {
  if (id) {
    const persistedInvalidation = invalidateCachedFiles(`${API_BASE}/documents/${id}`);
    [id, `download:${id}`, `preview:${id}`, `thumbnail:${id}`].forEach((key) => {
      documentDownloadCache.delete(key);
      documentDownloadInflight.delete(key);
    });
    invalidatePersistentThumbnailCache(id);
    return persistedInvalidation;
  }
  documentDownloadCache.clear();
  documentDownloadInflight.clear();
  invalidatePersistentThumbnailCache();
}

/** Retry transient file-read failures within a bounded budget. Never replay writes
 * or retry before a long Retry-After window allows another request. */
export async function fetchWithBusyRetry(
  url: string,
  init: RequestInit,
  { retries = 2, maxDelayMs = 3000 }: { retries?: number; maxDelayMs?: number } = {},
): Promise<Response> {
  const safeRead = !init.method || ["GET", "HEAD"].includes(init.method.toUpperCase());
  for (let attempt = 0; ; attempt += 1) {
    init.signal?.throwIfAborted();
    let res: Response | undefined;
    try {
      res = await fetch(url, init);
    } catch (error) {
      if (!safeRead || attempt >= retries || init.signal?.aborted || !(error instanceof TypeError)) throw error;
    }
    if (res && (!safeRead || ![429, 500, 502, 503, 504].includes(res.status) || attempt >= retries)) return res;
    const header = res?.headers.get("Retry-After");
    const seconds = Number(header);
    const retryAfterMs = header && !Number.isFinite(seconds)
      ? Date.parse(header) - Date.now() : seconds * 1000;
    const delayMs = Number.isFinite(retryAfterMs) && retryAfterMs > 0 ? retryAfterMs : 500 * (attempt + 1);
    if (res && delayMs > maxDelayMs) return res;
    await res?.body?.cancel();
    await new Promise<void>((resolve, reject) => {
      const done = () => {
        init.signal?.removeEventListener("abort", abort);
        resolve();
      };
      const timer = setTimeout(done, delayMs);
      const abort = () => {
        clearTimeout(timer);
        init.signal?.removeEventListener("abort", abort);
        reject(init.signal?.reason ?? new DOMException("Aborted", "AbortError"));
      };
      if (init.signal?.aborted) abort();
      else init.signal?.addEventListener("abort", abort, { once: true });
    });
  }
}

async function fetchProtectedBlob(
  cacheKey: string,
  url: string,
  options: DocumentBlobOptions = {},
): Promise<Blob> {
  // Abortable requests are caller-owned. Keeping them out of the shared
  // in-flight cache prevents one viewer cancellation from aborting another.
  const useCache = options.cache !== false && !options.signal;
  const now = Date.now();
  if (useCache && !options.force) {
    const cached = documentDownloadCache.get(cacheKey);
    if (cached && cached.expiresAt > now) {
      cached.lastAccessed = now;
      // Sliding TTL keeps genuinely hot files in L1 while inactive entries
      // still age out and are pruned by the size/item bounds.
      cached.expiresAt = now + DOCUMENT_DOWNLOAD_CACHE_TTL_MS;
      return cached.blob;
    }
    if (cached) documentDownloadCache.delete(cacheKey);

    const inflight = documentDownloadInflight.get(cacheKey);
    if (inflight) return inflight;
  }

  const token = await getReadyAuthToken();
  let request: Promise<Blob>;
  request = fetchWithBusyRetry(url, {
    cache: "no-store",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal: options.signal,
  }).then(async (res) => {
    if (!res.ok) throw new ApiError(res.status, "Download failed");
    const blob = await res.blob();
    if (useCache && documentDownloadInflight.get(cacheKey) === request) {
      documentDownloadCache.set(cacheKey, {
        blob,
        expiresAt: Date.now() + DOCUMENT_DOWNLOAD_CACHE_TTL_MS,
        lastAccessed: Date.now(),
        size: blob.size,
      });
      pruneDocumentDownloadCache(cacheKey);
    }
    return blob;
  });

  if (!useCache) return request;

  documentDownloadInflight.set(cacheKey, request);
  return request.finally(() => {
    if (documentDownloadInflight.get(cacheKey) === request) {
      documentDownloadInflight.delete(cacheKey);
    }
  });
}

function cacheBustUrl(url: string): string {
  return `${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`;
}

async function fetchProtectedDocumentBlob(
  _cacheKey: string,
  path: string,
  options: DocumentBlobOptions = {},
): Promise<Blob> {
  return (await fetchCachedFile(`${API_BASE}${path}`, { ...options, force: options.force || options.cache === false }, fetchWithBusyRetry)).blob();
}

async function fetchDocumentBlob(id: string, options: DocumentBlobOptions = {}): Promise<Blob> {
  return fetchProtectedDocumentBlob(`download:${id}`, `/documents/${id}/download`, options);
}

async function fetchDocumentResponse(
  id: string,
  options: DocumentResponseOptions = {},
  action = "download",
): Promise<Response> {
  // Editable source reads retain their fresh-download contract. Preview and
  // download share persistent local copies, including response MIME/filename.
  return fetchCachedFile(`${API_BASE}/documents/${id}/${action}`, {
    ...options, force: options.force || action === "editable-file",
  }, fetchWithBusyRetry);
}

async function fetchDocumentThumbnailBlob(id: string, options: DocumentBlobOptions = {}): Promise<Blob> {
  return fetchProtectedDocumentBlob(`thumbnail:${id}`, `/documents/${id}/thumbnail`, options);
}

async function fetchDocumentThumbnailUrl(id: string, options: DocumentThumbnailOptions = {}): Promise<string> {
  const useCache = options.cache !== false;
  const usePersistentCache = ALLOW_PERSISTENT_PROTECTED_DOCUMENT_CACHE && useCache && options.persistent !== false;
  if (usePersistentCache && !options.force) {
    const cached = readPersistentThumbnailDataUrl(id, options.version);
    if (cached) return cached;
  }

  const blob = await fetchDocumentThumbnailBlob(id, options);
  if (usePersistentCache) {
    const dataUrl = await writePersistentThumbnailBlob(id, options.version, blob);
    if (dataUrl) return dataUrl;
  }
  return URL.createObjectURL(blob);
}

function waitForDocumentVideoElement(
  element: HTMLMediaElement,
  events: string[],
  timeoutMs = 8000,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer: number | null = null;
    const cleanup = () => {
      if (timer !== null) window.clearTimeout(timer);
      events.forEach((eventName) => element.removeEventListener(eventName, handleSuccess));
      element.removeEventListener("error", handleError);
    };
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback();
    };
    const handleSuccess = () => finish(resolve);
    const handleError = () => finish(() => reject(new Error("Video thumbnail failed")));
    events.forEach((eventName) => element.addEventListener(eventName, handleSuccess, { once: true }));
    element.addEventListener("error", handleError, { once: true });
    timer = window.setTimeout(() => finish(() => reject(new Error("Video thumbnail timed out"))), timeoutMs);
  });
}

async function captureDocumentVideoFrame(videoUrl: string): Promise<string> {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.src = videoUrl;
  video.load();

  if (video.readyState < HTMLMediaElement.HAVE_METADATA) {
    await waitForDocumentVideoElement(video, ["loadedmetadata", "loadeddata"]);
  }

  const duration = Number.isFinite(video.duration) ? video.duration : 0;
  const targetTime = duration > 0.2 ? Math.min(0.8, Math.max(0.08, duration * 0.12)) : 0;
  if (targetTime > 0) {
    await new Promise<void>((resolve) => {
      let timer: number | null = null;
      const cleanup = () => {
        if (timer !== null) window.clearTimeout(timer);
        video.removeEventListener("seeked", finish);
        video.removeEventListener("error", finish);
      };
      const finish = () => {
        cleanup();
        resolve();
      };
      video.addEventListener("seeked", finish, { once: true });
      video.addEventListener("error", finish, { once: true });
      timer = window.setTimeout(finish, 2500);
      try {
        video.currentTime = targetTime;
      } catch {
        finish();
      }
    });
  }

  if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !video.videoWidth || !video.videoHeight) {
    await waitForDocumentVideoElement(video, ["loadeddata", "canplay"], 5000);
  }

  const width = video.videoWidth || 640;
  const height = video.videoHeight || 360;
  const maxWidth = 640;
  const scale = Math.min(1, maxWidth / width);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(2, Math.round(width * scale));
  canvas.height = Math.max(2, Math.round(height * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas is not available");
  context.drawImage(video, 0, 0, canvas.width, canvas.height);

  video.removeAttribute("src");
  video.load();
  return canvas.toDataURL("image/jpeg", 0.82);
}

async function fetchDocumentVideoThumbnailUrl(id: string, options: DocumentThumbnailOptions = {}): Promise<string> {
  const useCache = options.cache !== false;
  const usePersistentCache = ALLOW_PERSISTENT_PROTECTED_DOCUMENT_CACHE && useCache && options.persistent !== false;
  if (usePersistentCache && !options.force) {
    const cached = readPersistentThumbnailDataUrl(id, options.version);
    if (cached) return cached;
  }

  // Ask the server for its 640px thumbnail first. The server keeps generated
  // previews in Redis + its disk cache, so a Knowledge card never needs the
  // full video unless the user actually opens it.
  try {
    return await fetchDocumentThumbnailUrl(id, options);
  } catch {
    // Remote/legacy videos without a server-renderable source retain the
    // browser capture fallback below.
  }

  const blob = await fetchProtectedDocumentBlob(`preview:${id}`, `/documents/${id}/preview/content`, options);
  const videoUrl = URL.createObjectURL(blob);
  try {
    const dataUrl = await captureDocumentVideoFrame(videoUrl);
    if (usePersistentCache) {
      const cached = writePersistentThumbnailDataUrl(id, options.version, dataUrl);
      if (cached) return cached;
    }
    return dataUrl;
  } finally {
    URL.revokeObjectURL(videoUrl);
  }
}

function loadDocumentImageElement(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("Image thumbnail failed"));
    image.src = url;
  });
}

async function captureDocumentImageThumbnail(blob: Blob): Promise<string> {
  const imageUrl = URL.createObjectURL(await browserImageBlob(blob));
  try {
    const image = await loadDocumentImageElement(imageUrl);
    const width = image.naturalWidth || image.width || 640;
    const height = image.naturalHeight || image.height || 360;
    const maxWidth = 640;
    const scale = Math.min(1, maxWidth / width);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(2, Math.round(width * scale));
    canvas.height = Math.max(2, Math.round(height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas is not available");
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.82);
  } finally {
    URL.revokeObjectURL(imageUrl);
  }
}

async function fetchDocumentImageThumbnailUrl(id: string, options: DocumentThumbnailOptions = {}): Promise<string> {
  const useCache = options.cache !== false;
  const usePersistentCache = ALLOW_PERSISTENT_PROTECTED_DOCUMENT_CACHE && useCache && options.persistent !== false;
  if (usePersistentCache && !options.force) {
    const cached = readPersistentThumbnailDataUrl(id, options.version);
    if (cached) return cached;
  }

  // Prefer the server's bounded JPEG preview instead of downloading an
  // original multi-megabyte image just to paint a small Knowledge card.
  try {
    return await fetchDocumentThumbnailUrl(id, options);
  } catch {
    // SVG/remote formats that the server cannot rasterize use the existing
    // client-side fallback.
  }

  const blob = await fetchProtectedDocumentBlob(`preview:${id}`, `/documents/${id}/preview/content`, options);
  if (usePersistentCache) {
    const captured = await captureDocumentImageThumbnail(blob).catch(() => null);
    const dataUrl = captured
      ? writePersistentThumbnailDataUrl(id, options.version, captured)
      : await writePersistentThumbnailBlob(id, options.version, blob);
    if (dataUrl) return dataUrl;
  }
  return URL.createObjectURL(blob);
}

const SLIDE_LIST_BUSY_BUDGET_MS = 60_000;

/** `/slides` answers 503 (+ Retry-After) while render slots are busy; wait it out within a budget. */
async function requestSlideListWhileRenderBusy(id: string) {
  const deadline = Date.now() + SLIDE_LIST_BUSY_BUDGET_MS;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await request<{ slides: { index: number; url: string }[]; total: number }>(`/documents/${id}/slides`);
    } catch (error) {
      const retryable = error instanceof ApiError
        && (error.status === 503 || (error.status === 409 && attempt < 3));
      const delay = Math.min(8_000, 1_000 * 2 ** attempt);
      if (!retryable || Date.now() + delay >= deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
}

async function fetchDocumentPresentationThumbnailUrl(id: string, options: DocumentThumbnailOptions = {}): Promise<string> {
  const useCache = options.cache !== false;
  const usePersistentCache = ALLOW_PERSISTENT_PROTECTED_DOCUMENT_CACHE && useCache && options.persistent !== false;
  if (usePersistentCache && !options.force) {
    const cached = readPersistentThumbnailDataUrl(id, options.version);
    if (cached) return cached;
  }

  try {
    return await fetchDocumentThumbnailUrl(id, options);
  } catch {
    // Keep the slide endpoint fallback for legacy presentations.
  }

  const slideData = await requestSlideListWhileRenderBusy(id);
  const firstSlide = slideData.slides?.[0];
  if (!firstSlide?.url) throw new ApiError(404, "Presentation thumbnail unavailable");
  const blob = await fetchProtectedDocumentBlob(
    `slide-thumbnail:${id}:${firstSlide.index}`,
    firstSlide.url,
    options,
  );
  if (usePersistentCache) {
    const dataUrl = await writePersistentThumbnailBlob(id, options.version, blob);
    if (dataUrl) return dataUrl;
  }
  return URL.createObjectURL(blob);
}

const PLAN_LIMIT_KINDS: PlanLimitKind[] = ["credit", "trial_credit", "storage", "workspaces", "users", "generic"];

export function normalizePlanLimitDetail(detail: unknown, fallback: string): PlanLimitDetail {
  if (typeof detail === "object" && detail !== null) {
    const d = detail as Record<string, unknown>;
    const kind = typeof d.kind === "string" && (PLAN_LIMIT_KINDS as string[]).includes(d.kind)
      ? (d.kind as PlanLimitKind)
      : undefined;
    return {
      message: String(d.message || fallback),
      limit: typeof d.limit === "number" ? d.limit : null,
      current: typeof d.current === "number" ? d.current : null,
      plan: String(d.plan || "current"),
      kind,
      resets_at: typeof d.resets_at === "string" ? d.resets_at : null,
      return_to: typeof d.return_to === "string" ? d.return_to : null,
    };
  }
  return {
    message: typeof detail === "string" && detail ? detail : fallback,
    limit: null,
    current: null,
    plan: "current",
    resets_at: null,
    return_to: null,
  };
}

function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (typeof error === "object" &&
      error !== null &&
      "name" in error &&
      (error as { name?: unknown }).name === "AbortError")
  );
}

function responseRetryAfterMs(response: Response, fallbackMs: number): number {
  const now = Date.now();
  const header = response.headers.get("Retry-After")?.trim();
  const seconds = header && /^\d+(\.\d+)?$/.test(header) ? Number(header) : NaN;
  const delay = Number.isFinite(seconds)
    ? seconds * 1000
    : header ? Date.parse(header) - now : NaN;
  return Number.isFinite(delay) && delay > 0 ? delay : fallbackMs;
}

function recordRateLimit(response: Response, silent: boolean) {
  const now = Date.now();
  rateLimitUntil = Math.max(rateLimitUntil, now + responseRetryAfterMs(response, 60_000));
  if (!silent && !useToastStore.getState().toasts.some((toast) => toast.id === RATE_LIMIT_TOAST_ID)) {
    useToastStore.getState().addToast({
      id: RATE_LIMIT_TOAST_ID,
      type: "warning",
      title: t("lib.api.rate_limit_title"),
      message: t("lib.api.rate_limit_message"),
      duration: rateLimitUntil - now,
    });
  }
}

async function waitForRateLimit(signal?: AbortSignal | null) {
  // Spread reads resuming after a backend outage; do not shorten Retry-After.
  const jitterMs = backendUnavailableUntil > Date.now() ? Math.floor(Math.random() * 500) : 0;
  // Other in-flight responses may extend the shared cooldown while we wait.
  while (Math.max(rateLimitUntil, backendUnavailableUntil) + jitterMs > Date.now()) {
    if (signal?.aborted) throw new DOMException("Request aborted", "AbortError");
    await new Promise<void>((resolve, reject) => {
      const abort = () => {
        clearTimeout(timer);
        reject(new DOMException("Request aborted", "AbortError"));
      };
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", abort);
        resolve();
      }, Math.min(Math.max(rateLimitUntil, backendUnavailableUntil) + jitterMs - Date.now(), 2_147_483_647));
      signal?.addEventListener("abort", abort, { once: true });
    });
  }
}

// A page load fires many reads at once and several components ask for the same
// resource under different query keys. Identical concurrent reads share one
// network round trip; each caller gets its own copy so mutation stays local.
const inflightReads = new Map<string, Promise<unknown>>();

function request<T>(
  path: string,
  options: RequestInit = {},
  authTokenOverride?: string | null,
  rateLimitAttempt = 0,
): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = (options.headers as Record<string, string> | undefined) || {};
  const coalescible =
    method === "GET"
    && !options.signal
    && !options.body
    && authTokenOverride === undefined
    && rateLimitAttempt === 0
    && Object.keys(headers).every((name) => name.toLowerCase() === "x-silent-error");
  if (!coalescible) return requestUncoalesced<T>(path, options, authTokenOverride, rateLimitAttempt);

  const key = [getAuthToken() || "", getStoredLocale(), headers["X-Silent-Error"] || "", path].join("\n");
  let shared = inflightReads.get(key) as Promise<T> | undefined;
  if (!shared) {
    shared = requestUncoalesced<T>(path, options).finally(() => {
      if (inflightReads.get(key) === shared) inflightReads.delete(key);
    });
    inflightReads.set(key, shared);
  }
  return shared.then((value) =>
    value !== null && typeof value === "object" ? structuredClone(value) : value,
  );
}

async function requestUncoalesced<T>(
  path: string,
  options: RequestInit = {},
  authTokenOverride?: string | null,
  rateLimitAttempt = 0,
  sessionRecoveryAttempted = false,
): Promise<T> {
  const sessionToken = getAuthToken();
  const refreshablePath = !path.startsWith("/auth/") || path.startsWith("/auth/me")
    || path === "/auth/renew" || path === "/auth/entities/switch";
  if (authTokenOverride === undefined && refreshablePath && accessTokenExpired(sessionToken)) {
    await getReadyAuthToken();
    sessionRecoveryAttempted = true;
  }
  const token = authTokenOverride === undefined ? getAuthToken() : authTokenOverride;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Manor-Session": "1",
    "X-Language": getStoredLocale(),
    ...((options.headers as Record<string, string>) || {}),
  };
  if (typeof FormData !== "undefined" && options.body instanceof FormData) {
    delete headers["Content-Type"];
  }
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const suppressErrorToast = headers["X-Silent-Error"] === "1";
  if (suppressErrorToast) delete headers["X-Silent-Error"];

  const isRead = ["GET", "HEAD"].includes((options.method || "GET").toUpperCase());
  if (isRead && Math.max(rateLimitUntil, backendUnavailableUntil) > Date.now()) {
    await waitForRateLimit(options.signal);
    if (!isSameAuthIdentity(sessionToken, getAuthToken())) {
      throw new DOMException("Session changed while waiting", "AbortError");
    }
    // Rebuild headers after a normal token renewal during the waiting period.
    return requestUncoalesced<T>(path, options, authTokenOverride, rateLimitAttempt, sessionRecoveryAttempted);
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers,
      cache: options.cache ?? "no-store",
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    if (!suppressErrorToast) showBackendUnavailableToast();
    captureClientError(error, {
      handled: true,
      mechanism: "api.network",
      tags: {
        method: options.method || "GET",
        path,
      },
    });
    // The browser's own wording ("Failed to fetch", "Load failed") is not for people; callers that
    // show the message get a plain one, and ones that retry still see a TypeError.
    throw new TypeError(t("lib.api.connection_lost"), { cause: error });
  }
  const tokenIsCurrent = () => token === getAuthToken();

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = body.detail;
    const coded = _extractCodedDetail(detail);
    if (isBackendUnavailableStatus(res.status)) {
      if (!suppressErrorToast && !(res.status === 503 && coded.code === "database_busy")) showBackendUnavailableToast();
      const err = new ApiError(res.status, t("lib.api.backend_unavailable"));
      err.code = coded.code;
      err.vars = coded.vars;
      if (typeof detail === "object" && detail !== null) err.detail = detail;
      if ([502, 503, 504].includes(res.status) || res.headers.has("Retry-After")) {
        backendUnavailableUntil = Math.max(backendUnavailableUntil, Date.now() + responseRetryAfterMs(res, 2_000));
        err.retryAfterMs = Math.max(0, backendUnavailableUntil - Date.now());
      }
      // Server/edge metrics own availability failures. Posting a client error
      // for every failed read adds writes to the dependency already overloaded.
      if (![502, 503, 504].includes(res.status)) {
        captureClientError(err, {
          handled: true,
          mechanism: "api.http",
          tags: {
            method: options.method || "GET",
            path,
            status: res.status,
          },
        });
      }
      throw err;
    }

    // Middleware uses this machine marker; route-specific limits (such as
    // failed login attempts) must not pause unrelated application reads.
    const sharedRateLimit = res.status === 429 && body.error === "Too Many Requests";
    if (sharedRateLimit) {
      recordRateLimit(res, suppressErrorToast);
      // Only replay safe reads, once. Writes remain explicit user actions.
      if (isRead && rateLimitAttempt === 0) {
        if (!isSameAuthIdentity(sessionToken, getAuthToken())) {
          throw new DOMException("Session changed while waiting", "AbortError");
        }
        return requestUncoalesced<T>(path, options, authTokenOverride, 1, sessionRecoveryAttempted);
      }
    }

    // 402 with structured plan-gate detail.
    if (res.status === 402) {
      const limitDetail = normalizePlanLimitDetail(detail, body.error || t("component.upgrade_prompt.default_message"));
      const err = new ApiError(res.status, limitDetail.message);
      err.detail = limitDetail as unknown as Record<string, unknown>;
      throw err;
    }

    const message = (typeof detail === "string" ? detail : (detail as any)?.message) || res.statusText;
    // Pull i18n code + vars out of structured CodedError responses so we
    // can show the user a translated toast instead of raw English.
    const displayMessage = coded.code
      ? t(coded.code, coded.vars) || message
      : message;
    if (!sessionRecoveryAttempted && res.status === 401 && authTokenOverride === undefined
      && (!path.startsWith("/auth/") || path === "/auth/renew" || path === "/auth/me")
      && !path.startsWith("/auth/session/")) {
      const recovered = tokenIsCurrent() ? await restorePersistentSession() : isSameAuthIdentity(token, getAuthToken());
      if (recovered) {
        // An ended collaboration recovers into the person's own company; a
        // request addressed to the customer's Workspace is not replayed there.
        const company = decodeAuthTokenClaims(token)?.entity_id;
        if (company && company !== decodeAuthTokenClaims(getAuthToken())?.entity_id) {
          throw new SessionMovedError();
        }
        return requestUncoalesced<T>(path, options, undefined, rateLimitAttempt, true);
      }
    }
    // Expired session mid-use → clear + route to login (once), so an in-app
    // action never silently dead-ends on a stale token.
    if (
      res.status === 401 &&
      authTokenOverride === undefined
    ) handleUnauthorizedResponse(path, token, detail);
    // Background reads own their loading/empty/error state. A missing resource
    // should not produce a global toast for every query that referenced it.
    if (!sharedRateLimit && shouldShowRequestErrorToast(res.status, options.method, suppressErrorToast)) {
      useToastStore.getState().error(t("lib.api.request_failed"), displayMessage);
    }
    const err = new ApiError(res.status, message);
    if (sharedRateLimit) err.retryAfterMs = Math.max(0, rateLimitUntil - Date.now());
    if (coded.code) err.code = coded.code;
    if (coded.vars) err.vars = coded.vars;
    if (typeof detail === "object" && detail !== null) err.detail = detail as Record<string, unknown>;
    throw err;
  }

  clearBackendUnavailableToast();
  if (rateLimitUntil > 0 && Date.now() >= rateLimitUntil) {
    rateLimitUntil = 0;
    useToastStore.getState().removeToast(RATE_LIMIT_TOAST_ID);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

const DEFAULT_DOCUMENT_UPLOAD_STALL_TIMEOUT_MS = 120_000;
const DEFAULT_DOCUMENT_UPLOAD_PROCESSING_TIMEOUT_MS = 600_000;
const DEFAULT_DOCUMENT_UPLOAD_RECEIPT_RECONCILE_TIMEOUT_MS = 15_000;

function createDocumentUploadIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `document-upload-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function documentUploadResponseBody(xhr: XMLHttpRequest): Record<string, any> {
  let body: Record<string, any> = {};
  try {
    body = xhr.responseText ? JSON.parse(xhr.responseText) : {};
  } catch {
    body = {};
  }
  return body;
}

function documentUploadHttpError(
  xhr: XMLHttpRequest,
  path: string,
  requestToken: string | null,
  body = documentUploadResponseBody(xhr),
): ApiError {
  const detail = body.detail;
  const message =
    (typeof detail === "string" ? detail : detail?.message)
    || xhr.statusText
    || "Upload failed";
  const coded = _extractCodedDetail(detail);

  if (xhr.status === 402) {
    const limitDetail = normalizePlanLimitDetail(
      detail,
      body.error || t("component.upgrade_prompt.default_message"),
    );
    const error = new ApiError(xhr.status, limitDetail.message);
    error.detail = limitDetail as unknown as Record<string, unknown>;
    return error;
  }

  if (xhr.status === 401) {
    handleUnauthorizedResponse(path, requestToken, detail);
  }
  if (isBackendUnavailableStatus(xhr.status)) {
    // A server that explains itself (the virus check timed out, ...) is up;
    // only a bare gateway failure reads as the backend being unavailable.
    const explained = typeof detail === "string" ? detail : typeof detail?.message === "string" ? detail.message : "";
    const error = new ApiError(xhr.status, explained || t("lib.api.backend_unavailable"));
    if (coded.code) error.code = coded.code;
    if (coded.vars) error.vars = coded.vars;
    if (typeof detail === "object" && detail !== null) {
      error.detail = detail as Record<string, unknown>;
    }
    captureClientError(error, {
      handled: true,
      mechanism: "api.http",
      tags: { method: "POST", path, status: xhr.status },
    });
    return error;
  }

  const error = new ApiError(xhr.status, message);
  if (coded.code) error.code = coded.code;
  if (coded.vars) error.vars = coded.vars;
  if (typeof detail === "object" && detail !== null) {
    error.detail = detail as Record<string, unknown>;
  }
  return error;
}

function documentUploadReceiptPause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Upload cancelled", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, ms);
    const abort = () => {
      clearTimeout(timer);
      reject(new DOMException("Upload cancelled", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}

async function reconcileDocumentUploadReceipt(
  idempotencyKey: string,
  timeoutMs: number,
  signal?: AbortSignal,
  expectedPrincipalKey = authPrincipalKey(getAuthToken()),
): Promise<Document> {
  const deadline = Date.now() + timeoutMs;
  let pauseMs = 250;
  while (true) {
    if (signal?.aborted) throw new DOMException("Upload cancelled", "AbortError");
    if (authPrincipalKey(getAuthToken()) !== expectedPrincipalKey) {
      throw new DocumentUploadAuthChangedError();
    }
    const receipt = await lookupDocumentUploadReceipt(
      idempotencyKey,
      signal,
      expectedPrincipalKey,
    );
    if (receipt) return receipt;
    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) throw new DocumentUploadStalledError("processing");
    await documentUploadReceiptPause(Math.min(pauseMs, remainingMs), signal);
    pauseMs = Math.min(pauseMs * 2, 4_000);
  }
}

async function lookupDocumentUploadReceipt(
  idempotencyKey: string,
  signal?: AbortSignal,
  expectedPrincipalKey = authPrincipalKey(getAuthToken()),
): Promise<Document | null> {
  const receiptToken = getAuthToken();
  if (authPrincipalKey(receiptToken) !== expectedPrincipalKey) {
    throw new DocumentUploadAuthChangedError();
  }
  try {
    return await request<Document>(
      `/documents/upload-receipts/${encodeURIComponent(idempotencyKey)}`,
      {
        signal,
        headers: { "X-Silent-Error": "1" },
      },
      receiptToken,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

async function uploadDocumentRequest(
  file: File,
  folderId?: string | null,
  options?: {
    visibility?: string;
    classification?: string;
    client_visible?: boolean;
  },
  requestOptions: DocumentUploadRequestOptions = {},
  requestAttempt = 0,
  expectedPrincipalKey?: string,
): Promise<Document> {
  const form = new FormData();
  form.append("file", file);
  const params = new URLSearchParams();
  if (folderId) params.set("folder_id", folderId);
  if (options?.visibility) params.set("visibility", options.visibility);
  if (options?.classification) params.set("classification", options.classification);
  if (options?.client_visible != null) params.set("client_visible", String(options.client_visible));
  const query = params.toString();
  const path = `/documents/upload${query ? `?${query}` : ""}`;
  const stallTimeoutMs = Math.max(
    1_000,
    requestOptions.stallTimeoutMs ?? DEFAULT_DOCUMENT_UPLOAD_STALL_TIMEOUT_MS,
  );
  const processingTimeoutMs = Math.max(
    1_000,
    requestOptions.processingTimeoutMs ?? DEFAULT_DOCUMENT_UPLOAD_PROCESSING_TIMEOUT_MS,
  );
  const receiptReconcileTimeoutMs = Math.max(
    0,
    requestOptions.receiptReconcileTimeoutMs
      ?? DEFAULT_DOCUMENT_UPLOAD_RECEIPT_RECONCILE_TIMEOUT_MS,
  );
  const idempotencyKey = requestOptions.idempotencyKey || createDocumentUploadIdempotencyKey();
  let token = getAuthToken();
  const requestPrincipalKey = expectedPrincipalKey ?? authPrincipalKey(token);
  if (accessTokenExpired(token)) token = await getReadyAuthToken();

  return new Promise<Document>((resolve, reject) => {
    if (authPrincipalKey(token) !== requestPrincipalKey) {
      reject(new DocumentUploadAuthChangedError());
      return;
    }
    if (requestOptions.signal?.aborted) {
      reject(new DOMException("Upload cancelled", "AbortError"));
      return;
    }

    const xhr = new XMLHttpRequest();
    let settled = false;
    let stalledPhase: DocumentUploadPhase | null = null;
    let uploadBytesSent = false;
    let lastLoaded = 0;
    let stallTimer: ReturnType<typeof setTimeout> | null = null;
    let processingTimer: ReturnType<typeof setTimeout> | null = null;

    const clearStallTimer = () => {
      if (stallTimer !== null) {
        clearTimeout(stallTimer);
        stallTimer = null;
      }
    };
    const armStallTimer = () => {
      clearStallTimer();
      stallTimer = setTimeout(() => {
        stalledPhase = "uploading";
        xhr.abort();
      }, stallTimeoutMs);
    };
    const clearProcessingTimer = () => {
      if (processingTimer !== null) {
        clearTimeout(processingTimer);
        processingTimer = null;
      }
    };
    const armProcessingTimer = () => {
      clearProcessingTimer();
      processingTimer = setTimeout(() => {
        stalledPhase = "processing";
        xhr.abort();
      }, processingTimeoutMs);
    };
    const cleanup = () => {
      clearStallTimer();
      clearProcessingTimer();
      requestOptions.signal?.removeEventListener("abort", abortRequest);
    };
    const finishResolve = (document: Document) => {
      if (settled) return;
      if (authPrincipalKey(getAuthToken()) !== requestPrincipalKey) {
        finishReject(new DocumentUploadAuthChangedError());
        return;
      }
      settled = true;
      cleanup();
      resolve(document);
    };
    const finishReject = (error: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const finishRetry = () => {
      if (settled) return;
      if (authPrincipalKey(getAuthToken()) !== requestPrincipalKey) {
        finishReject(new DocumentUploadAuthChangedError());
        return;
      }
      settled = true;
      cleanup();
      uploadDocumentRequest(
        file,
        folderId,
        options,
        { ...requestOptions, idempotencyKey },
        requestAttempt + 1,
        requestPrincipalKey,
      ).then(resolve, reject);
    };
    const finishProcessingReconciliation = () => {
      if (settled) return;
      settled = true;
      cleanup();
      reconcileDocumentUploadReceipt(
        idempotencyKey,
        receiptReconcileTimeoutMs,
        requestOptions.signal,
        requestPrincipalKey,
      ).then(resolve, reject);
    };
    const finishHttpFailureReconciliation = (originalError: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      // A proxy can synthesize a 5xx, or corrupt a successful response, before
      // a committed receipt is visible. Reconcile for a bounded window; one
      // negative read is not proof that the server rolled the upload back.
      reconcileDocumentUploadReceipt(
        idempotencyKey,
        receiptReconcileTimeoutMs,
        requestOptions.signal,
        requestPrincipalKey,
      ).then(
        resolve,
        (reconciliationError) => {
          if (
            reconciliationError instanceof DocumentUploadAuthChangedError
            || reconciliationError instanceof DOMException
            || (originalError instanceof ApiError
              && originalError.code === "document_upload_commit_uncertain")
          ) {
            reject(reconciliationError);
          } else {
            reject(originalError);
          }
        },
      );
    };
    const abortRequest = () => xhr.abort();

    xhr.open("POST", `${API_BASE}${path}`);
    xhr.setRequestHeader("X-Language", getStoredLocale());
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.setRequestHeader("Idempotency-Key", idempotencyKey);

    xhr.upload.addEventListener("loadstart", () => {
      requestOptions.onProgress?.({ phase: "uploading", loaded: 0, total: file.size, percent: 0 });
      armStallTimer();
    });
    xhr.upload.addEventListener("progress", (event) => {
      const total = event.lengthComputable && event.total > 0 ? event.total : file.size;
      const loaded = Math.min(event.loaded, total || event.loaded);
      if (loaded > lastLoaded) {
        lastLoaded = loaded;
        armStallTimer();
      }
      const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
      requestOptions.onProgress?.({ phase: "uploading", loaded, total, percent });
    });
    xhr.upload.addEventListener("load", () => {
      uploadBytesSent = true;
      clearStallTimer();
      armProcessingTimer();
      requestOptions.onProgress?.({
        phase: "processing",
        loaded: file.size,
        total: file.size,
        percent: 100,
      });
    });

    xhr.addEventListener("load", () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        const body = documentUploadResponseBody(xhr);
        if (xhr.status === 401 && requestAttempt === 0) {
          const latestToken = getAuthToken();
          if (latestToken && latestToken !== token) {
            if (authPrincipalKey(latestToken) === requestPrincipalKey) {
              finishRetry();
            } else {
              finishReject(new DocumentUploadAuthChangedError());
            }
            return;
          }
        }
        if (xhr.status === 401 && _extractCodedDetail(body.detail).code === "collaboration_ended") {
          void leftEndedCollaboration(body.detail, token).then((left) => finishReject(
            left ? new DocumentUploadAuthChangedError() : documentUploadHttpError(xhr, path, token, body),
          ));
          return;
        }
        const error = documentUploadHttpError(xhr, path, token, body);
        if (uploadBytesSent && xhr.status >= 500) {
          finishHttpFailureReconciliation(error);
          return;
        }
        finishReject(error);
        return;
      }
      try {
        const document = JSON.parse(xhr.responseText) as Document;
        clearBackendUnavailableToast();
        finishResolve(document);
      } catch (error) {
        const invalidResponse = new ApiError(
          500,
          error instanceof Error ? `Invalid upload response: ${error.message}` : "Invalid upload response",
        );
        if (uploadBytesSent) {
          finishHttpFailureReconciliation(invalidResponse);
          return;
        }
        finishReject(invalidResponse);
      }
    });
    xhr.addEventListener("error", () => {
      // Once every byte left the browser, a transport error can mean the
      // server committed but its response was lost. Resolve the durable
      // receipt before presenting a retryable failure, so a retry cannot
      // create an ambiguous duplicate.
      if (uploadBytesSent) {
        finishProcessingReconciliation();
        return;
      }
      const error = new TypeError("Network request failed");
      captureClientError(error, {
        handled: true,
        mechanism: "api.network",
        tags: { method: "POST", path },
      });
      finishReject(error);
    });
    xhr.addEventListener("abort", () => {
      if (stalledPhase === "processing") {
        finishProcessingReconciliation();
        return;
      }
      finishReject(
        stalledPhase
          ? new DocumentUploadStalledError(stalledPhase)
          : new DOMException("Upload cancelled", "AbortError"),
      );
    });

    requestOptions.signal?.addEventListener("abort", abortRequest, { once: true });
    xhr.send(form);
  });
}

async function requestStreamResponse(
  path: string,
  body: FormData,
  signal?: AbortSignal,
): Promise<Response> {
  let token = await getReadyAuthToken();
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let response: Response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body,
        signal,
      });
    } catch (error) {
      if (isAbortError(error)) throw error;
      showBackendUnavailableToast();
      captureClientError(error, {
        handled: true,
        mechanism: "api.network",
        tags: { method: "POST", path },
      });
      throw error;
    }

    if (response.ok) {
      clearBackendUnavailableToast();
      return response;
    }

    const latestToken = getAuthToken();
    if (
      response.status === 401
      && attempt === 0
      && Boolean(latestToken)
      && latestToken !== token
      && isSameAuthIdentity(token, latestToken)
    ) {
      token = latestToken;
      continue;
    }

    if (isBackendUnavailableStatus(response.status)) {
      showBackendUnavailableToast();
      const err = new ApiError(response.status, t("lib.api.backend_unavailable"));
      captureClientError(err, {
        handled: true,
        mechanism: "api.http",
        tags: { method: "POST", path, status: response.status },
      });
      throw err;
    }

    const responseBody = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = responseBody.detail;
    if (response.status === 402) {
      const limitDetail = normalizePlanLimitDetail(
        detail,
        responseBody.error || t("component.upgrade_prompt.default_message"),
      );
      const err = new ApiError(response.status, limitDetail.message);
      err.detail = limitDetail as unknown as Record<string, unknown>;
      throw err;
    }

    const message =
      (typeof detail === "string" ? detail : (detail as any)?.message)
      || response.statusText;
    const coded = _extractCodedDetail(detail);
    const displayMessage = coded.code ? t(coded.code, coded.vars) || message : message;
    if (response.status === 401 && await leftEndedCollaboration(detail, token)) {
      throw new SessionMovedError();
    }
    if (response.status === 401) {
      handleUnauthorizedResponse(path, token, detail);
    } else {
      useToastStore.getState().error(t("lib.api.chat_failed"), displayMessage);
    }
    const err = new ApiError(response.status, message);
    if (coded.code) err.code = coded.code;
    if (coded.vars) err.vars = coded.vars;
    if (typeof detail === "object" && detail !== null) {
      err.detail = detail as Record<string, unknown>;
    }
    throw err;
  }
  throw new ApiError(401, "Unauthorized");
}

/**
 * POST `body` to an SSE endpoint that narrates progress via `step` frames and
 * ends with a terminal `done` (result) or `error` frame. Calls `onStep(label)`
 * for each step; resolves with the `done` payload, rejects on `error`.
 *
 * Streaming keeps the connection alive past Cloudflare's 100s origin timeout
 * (524), which plain JSON generation endpoints can blow through.
 */
async function streamSseResult<T>(
  path: string,
  body: unknown,
  onStep?: (label: string) => void,
): Promise<T> {
  const token = await getReadyAuthToken();
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    const errBody = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = (errBody as any).detail;
    const message = (typeof detail === "string" ? detail : (detail as any)?.message) || response.statusText;
    throw new ApiError(response.status, message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: T | null = null;
  let errorMessage: string | null = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let parsed: any;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "step") onStep?.(parsed.label);
      else if (event === "done") result = parsed as T;
      else if (event === "error") errorMessage = parsed.message || "Generation failed";
    }
  }
  if (errorMessage) throw new ApiError(500, errorMessage);
  if (result === null) throw new ApiError(500, "Generation did not return a result");
  return result;
}

/**
 * Drive a streaming workflow run. Calls `onNode(id, status)` for each `node`
 * SSE frame (canvas lights up live), resolves with the terminal `done` run.
 */
async function streamWorkflowRun(
  path: string,
  onNode: (id: string, status: string) => void,
  onRun?: (runId: string) => void,
  data?: Record<string, unknown>,
): Promise<any> {
  const token = await getReadyAuthToken();
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(data || {}),
  });
  if (!response.ok || !response.body) {
    const errBody = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, (errBody as any).detail || response.statusText);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: any = null;
  let errorMessage: string | null = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let parsed: any;
      try { parsed = JSON.parse(data); } catch { continue; }
      if (event === "run") onRun?.(parsed.run_id);
      else if (event === "node") onNode(parsed.id, parsed.status);
      else if (event === "done") result = parsed;
      else if (event === "error") errorMessage = parsed.message || "Run failed";
    }
  }
  if (errorMessage && !result) throw new ApiError(500, errorMessage);
  return result;
}

export function isLocalFsUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  if (url.startsWith(`${API_BASE}/fs/`) || url.startsWith("/api/v1/fs/")) return true;
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.origin === window.location.origin && parsed.pathname.startsWith("/api/v1/fs/");
  } catch {
    return false;
  }
}

/** Fetch a protected raw entity file without buffering it into a Blob first. */
export async function fetchProtectedFsResponse(
  url: string,
  options: ProtectedFileResponseOptions = {},
): Promise<Response> {
  if (!isLocalFsUrl(url)) {
    throw new Error("Protected filesystem response requires a local filesystem URL");
  }
  const token = await getReadyAuthToken();
  const response = await fetch(cacheBustUrl(url), {
    cache: "no-store",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal: options.signal,
  });
  if (!response.ok) throw new ApiError(response.status, "Download failed");
  return response;
}

export function isTikTokApprovalPreviewUrl(url: string): boolean {
  return /^\/api\/v1\/chat\/approvals\/[A-Za-z0-9_-]+\/tiktok-preview\?/.test(url);
}

export async function resolveDisplayMediaUrl(url: string): Promise<{ url: string; revoke: () => void }> {
  const approvalPreview = isTikTokApprovalPreviewUrl(url);
  if (!isLocalFsUrl(url) && !approvalPreview) return { url, revoke: () => {} };
  const blob = await fetchProtectedBlob(`media:${url}`, cacheBustUrl(url), { cache: !approvalPreview });
  const objectUrl = URL.createObjectURL(blob);
  return { url: objectUrl, revoke: () => URL.revokeObjectURL(objectUrl) };
}

// ── SSE helper for workspace draft streaming ──────────────────────────

export interface WorkspaceArchitectToolEvent {
  step: number;
  name: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  summary?: Record<string, unknown>;
}

export interface WorkspaceArchitectTurnMeta {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  duration_ms: number;
  rounds: number;
  tool_calls: number;
  model?: string;
}

export interface WorkspaceDraftStreamHandlers {
  onStart?: (info: { draft_id?: string; mode?: string }) => void;
  /** Called for every token chunk the LLM emits. */
  onToken?: (chunk: string) => void;
  /** Called when the model emits a text_reset (clears partial buffer). */
  onReset?: () => void;
  /** Architect kicks off a tool call (commit_basics, propose_service, ...). */
  onToolStart?: (e: WorkspaceArchitectToolEvent) => void;
  /** Tool call returned. ``ok=false`` means the call was rejected. */
  onToolEnd?: (e: WorkspaceArchitectToolEvent) => void;
  /** Turn-level usage metrics (tokens + duration + rounds) emitted once per turn just before ``done``. */
  onTurnMeta?: (m: WorkspaceArchitectTurnMeta) => void;
  /** Called once when the stream ends successfully. */
  onDone?: (final: WorkspaceDraftTurn) => void;
  /** Called on stream error (after which the promise rejects). */
  onError?: (message: string) => void;
}

// ── SSE helper for workspace finalize streaming ───────────────────────

export interface FinalizeProgressEvent {
  step: string;
  payload: Record<string, unknown>;
}

export interface WorkspaceFinalizeStreamHandlers {
  onStart?: (info: { draft_id?: string }) => void;
  /** Each progress checkpoint emitted by finalize_setup. */
  onProgress?: (e: FinalizeProgressEvent) => void;
  onDone?: (final: WorkspaceDraftFinalize) => void;
  onError?: (message: string) => void;
}

async function _streamFinalizeSSE(
  path: string,
  handlers: WorkspaceFinalizeStreamHandlers,
): Promise<WorkspaceDraftFinalize> {
  const token = await getReadyAuthToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      cache: "no-store",
    });
  } catch (error) {
    showBackendUnavailableToast();
    throw error;
  }

  if (!res.ok) {
    if (isBackendUnavailableStatus(res.status)) {
      showBackendUnavailableToast();
      throw new ApiError(res.status, t("lib.api.backend_unavailable"));
    }

    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    const msg = errBody.detail || res.statusText;
    if (res.status !== 401) useToastStore.getState().error(t("lib.api.finalize_failed"), msg);
    throw new ApiError(res.status, msg);
  }

  clearBackendUnavailableToast();

  const reader = res.body?.getReader();
  if (!reader) throw new Error("Stream not supported by runtime");
  const decoder = new TextDecoder();
  let buffer = "";
  let finalPayload: WorkspaceDraftFinalize | null = null;
  let streamError: Error | null = null;
  // Steps that flow through ``onProgress`` rather than start/done/error.
  const KNOWN_STEPS = new Set([
    "agent_bindings_repair_started",
    "agent_bindings_repair_done",
    "workspace_created",
    "provisioning_agents_started",
    "agent_provisioned",
    "agents_done",
    "provisioning_team_and_knowledge",
    "team_and_knowledge_done",
    "default_skills_seeded",
    "memory_seeded",
    "runtime_scheduled",
    "strategist_dispatched",
    "dispatch_warning",
    "complete",
  ]);

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let eventName = "message";
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length === 0) continue;
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(dataLines.join("\n"));
      } catch {
        continue;
      }
      if (eventName === "start") {
        handlers.onStart?.(payload as { draft_id?: string });
      } else if (KNOWN_STEPS.has(eventName)) {
        handlers.onProgress?.({ step: eventName, payload });
      } else if (eventName === "done") {
        finalPayload = payload as unknown as WorkspaceDraftFinalize;
        handlers.onDone?.(finalPayload);
      } else if (eventName === "error") {
        const message = (payload.message as string) || "Unknown finalize error";
        if (payload.kind === "workspaces") {
          const limitDetail = normalizePlanLimitDetail(payload, message);
          const error = new ApiError(402, limitDetail.message);
          error.detail = limitDetail as unknown as Record<string, unknown>;
          streamError = error;
        } else {
          streamError = new Error(message);
        }
        handlers.onError?.(message);
      }
    }
  }

  if (streamError) throw streamError;
  if (!finalPayload) throw new Error("Finalize ended without a final payload");
  return finalPayload;
}


async function _streamDraftSSE(
  path: string,
  body: Record<string, unknown>,
  handlers: WorkspaceDraftStreamHandlers,
): Promise<WorkspaceDraftTurn> {
  const token = await getReadyAuthToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch (error) {
    showBackendUnavailableToast();
    throw error;
  }

  if (!res.ok) {
    if (isBackendUnavailableStatus(res.status)) {
      showBackendUnavailableToast();
      throw new ApiError(res.status, t("lib.api.backend_unavailable"));
    }

    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = errBody.detail;
    // require_plan returns a structured detail dict {message, limit, current, plan}.
    // Preserve the structure on the thrown error so the caller can
    // render a real "limit reached" UI instead of a vague toast.
    if (typeof detail === "object" && detail !== null) {
      const err = new ApiError(res.status, (detail as any).message || res.statusText);
      (err as any).detail = detail;
      // Don't toast 401/402 — handled by auth redirect / upgrade overlay.
      if (res.status !== 401 && res.status !== 402) {
        useToastStore.getState().error(t("lib.api.stream_failed"), (err as any).message);
      }
      throw err;
    }
    const msg = detail || res.statusText;
    if (res.status !== 401) useToastStore.getState().error(t("lib.api.stream_failed"), msg);
    throw new ApiError(res.status, msg);
  }

  clearBackendUnavailableToast();

  const reader = res.body?.getReader();
  if (!reader) throw new Error("Stream not supported by runtime");

  const decoder = new TextDecoder();
  let buffer = "";
  let finalTurn: WorkspaceDraftTurn | null = null;
  let streamError: string | null = null;

  // Each SSE frame is "event: <name>\ndata: <json>\n\n".
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      let eventName = "message";
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (dataLines.length === 0) continue;

      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(dataLines.join("\n"));
      } catch {
        continue;
      }

      if (eventName === "start") {
        handlers.onStart?.(payload as { draft_id?: string; mode?: string });
      } else if (eventName === "token") {
        const c = (payload.content as string) || "";
        if (c) handlers.onToken?.(c);
      } else if (eventName === "text_reset") {
        handlers.onReset?.();
      } else if (eventName === "tool_start") {
        handlers.onToolStart?.(payload as unknown as WorkspaceArchitectToolEvent);
      } else if (eventName === "tool_end") {
        handlers.onToolEnd?.(payload as unknown as WorkspaceArchitectToolEvent);
      } else if (eventName === "turn_meta") {
        handlers.onTurnMeta?.(payload as unknown as WorkspaceArchitectTurnMeta);
      } else if (eventName === "done") {
        finalTurn = payload as unknown as WorkspaceDraftTurn;
        handlers.onDone?.(finalTurn);
      } else if (eventName === "error") {
        streamError = (payload.message as string) || "Unknown stream error";
        handlers.onError?.(streamError);
      }
    }
  }

  if (streamError) throw new Error(streamError);
  if (!finalTurn) throw new Error("Stream ended without a final turn");
  return finalTurn;
}

export type WorkspaceStatIntegrationKey = "twitter_x";

export interface WorkspaceStatLibraryEntry {
  key: string;
  name: string;
  description: string;
  category: string;
  value_type: "number" | "percent" | "currency" | "duration";
  unit: string | null;
  default_window: string;
  collector_type: "manual" | "workspace_internal" | "integration" | "agent_review";
  integration_key: WorkspaceStatIntegrationKey | null;
  collector_config: Record<string, unknown>;
  default_cadence: string | null;
  freshness_limit_seconds: number | null;
  goal_eligible: boolean;
  recommended_goal_role: string | null;
  available_connections: Array<{
    id: string;
    integration_key: WorkspaceStatIntegrationKey;
    label: string;
    is_default: boolean;
  }>;
}

export interface WorkspaceStatDefinition {
  id: string;
  workspace_id: string;
  key: string;
  name: string;
  description: string | null;
  value_type: "number" | "percent" | "currency" | "duration";
  unit: string | null;
  window: string;
  collector_type: "manual" | "workspace_internal" | "integration" | "agent_review";
  collector_config: Record<string, unknown>;
  collection_cadence: string | null;
  freshness_limit_seconds: number | null;
  origin: string;
  library_key: string | null;
  status: string;
  goal_eligible: boolean;
  current_value: number | null;
  current_value_updated_at: string | null;
  last_collection_status: string | null;
  last_collection_error: string | null;
  last_assessment?: { observation_id: string; status: string; observed_at: string; evidence?: { gaps?: string[]; reasoning?: string } } | null;
  assessment_runtime?: { workflow_id: string; binding_id: string; job_id?: string; run_id: string | null; run_status: string | null; next_run_at: string | null };
  freshness_status: "fresh" | "stale" | "no_data" | "collection_error" | "paused" | string;
  revision: number;
  created_at: string;
  updated_at: string | null;
}

export interface WorkspaceStatObservation {
  id: string;
  stat_id: string;
  value: number | null;
  observed_at: string;
  window_start: string | null;
  window_end: string | null;
  source: string;
  evidence: Record<string, unknown> | null;
  collector_revision: number;
}

export interface WorkspaceStatsQuickView {
  ordered_stat_ids: string[];
  hidden_stat_ids: string[];
  configured: boolean;
}

export interface WorkspaceLedgerBreakdown {
  key: string;
  count: number;
}

export interface WorkspaceLedgerOverviewItem {
  contract_id: string;
  kind: "content" | "finance" | "recruiting" | "relationship" | string;
  title: string;
  directory: string;
  schema_version: number;
  projection_kind: "current" | "event_stream" | string;
  record_count: number;
  event_count: number;
  updated_at: string | null;
  status_counts: WorkspaceLedgerBreakdown[];
  stage_counts: WorkspaceLedgerBreakdown[];
  subject_counts?: WorkspaceLedgerBreakdown[];
  totals: Array<{
    currency: string;
    inflow_minor: number;
    outflow_minor: number;
  }>;
}

export interface WorkspaceLedgerOverview {
  workspace_id: string;
  ledger_count: number;
  record_count: number;
  event_count: number;
  ledgers: WorkspaceLedgerOverviewItem[];
}

export type WorkspaceLedgerVisualizationScalar = string | number | boolean | null;

export interface WorkspaceLedgerPresentationSection {
  type: "metrics" | "chart" | "records" | "profile" | "projection";
  title?: string;
  chart?: "bar" | "line" | "area" | "pie" | "donut";
  metric?: string;
  metrics?: string[];
  columns?: string[];
  forecast_periods?: number;
}

export interface WorkspaceLedgerPresentation {
  version: 1;
  title?: string;
  subtitle?: string;
  sections: WorkspaceLedgerPresentationSection[];
}

export interface WorkspaceLedgerQueryVisualization {
  contract_id: string;
  view: "current" | "events" | string;
  layout: "metrics" | "bar" | "timeline" | "table" | "empty";
  matched_count: number;
  as_of: WorkspaceLedgerVisualizationScalar;
  group_by: string[];
  date_granularity?: "day" | "week" | "month" | null;
  metrics: string[];
  aggregates: Record<string, number>;
  groups: Array<{
    key: Record<string, WorkspaceLedgerVisualizationScalar>;
    aggregates: Record<string, number>;
  }>;
  group_count?: number;
  groups_truncated?: boolean;
  columns: string[];
  rows: Array<Record<string, WorkspaceLedgerVisualizationScalar>>;
  has_more: boolean;
  currency: string | null;
  presentation?: WorkspaceLedgerPresentation | null;
  query_fingerprint?: string;
  coalesce_previous_visualization?: boolean;
}

export interface WorkspaceStatCreate {
  library_key?: string;
  key?: string;
  name?: string;
  description?: string;
  value_type?: WorkspaceStatDefinition["value_type"];
  unit?: string;
  window?: string;
  collector_type?: WorkspaceStatDefinition["collector_type"];
  collector_config?: Record<string, unknown>;
  collection_cadence?: string | null;
  freshness_limit_seconds?: number | null;
  goal_eligible?: boolean;
}

interface EditorSaveIntent {
  sessionId: string;
  sequence: number;
}

interface FilesystemWriteOptions {
  authToken?: string | null;
  signal?: AbortSignal;
  saveIntent?: EditorSaveIntent;
}

export interface OwnedApprovalRequest {
  id: string;
  status: string;
  action_key: string | null;
  risk_level: string;
  reason: string | null;
  hitl_type: string;
  payload: { review?: { summary?: string; review_artifacts?: Array<{ kind?: string; url?: string; title?: string }> } } & Record<string, unknown>;
  step_id: string | null;
  created_at: string | null;
  decided_at: string | null;
}


export const api = {
  approvalRequests: {
    get: (requestId: string) => request<OwnedApprovalRequest>(`/approval-requests/${encodeURIComponent(requestId)}`),
    decide: (requestId: string, decision: "approve" | "reject") =>
      request<OwnedApprovalRequest>(`/approval-requests/${encodeURIComponent(requestId)}/decision`, {
        method: "POST", body: JSON.stringify({ decision }),
      }),
  },
  approvalCommands: {
    get: (requestId: string) => request<ApprovalCommandProjection>(`/approval-commands/hitl/${encodeURIComponent(requestId)}`),
    saveRule: (command: ApprovalSaveRuleCommand) => request<ApprovalCommandResult>("/approval-commands", { method: "POST", body: JSON.stringify(command) }),
  },
  approvalSettings: {
    get: (scope: ApprovalScope) => request<ApprovalSnapshot>(approvalResourcePath(scope)),
    update: (scope: ApprovalScope, data: ApprovalWrite) => request<ApprovalSnapshot>(approvalResourcePath(scope), { method: "PUT", body: JSON.stringify(data) }),
    preview: (scope: ApprovalScope, data: ApprovalWrite) => request<ApprovalPreview>(`${approvalResourcePath(scope)}/preview`, { method: "POST", body: JSON.stringify(data) }),
    overrides: (entityId: string, offset = 0) => request<ApprovalOverrides>(`/approval-settings/accounts/${encodeURIComponent(entityId)}/overrides?limit=50&offset=${offset}`),
  },
  chatRooms: {
    publicInvite: (id: string) => request<{ token: string | null }>(`/chat/rooms/${encodeURIComponent(id)}/public-invite`),
    enablePublicInvite: (id: string, reset = false) => request<{ token: string }>(`/chat/rooms/${encodeURIComponent(id)}/public-invite`, {
      method: "PUT", body: JSON.stringify({ reset }),
    }),
    disablePublicInvite: (id: string) => request<void>(`/chat/rooms/${encodeURIComponent(id)}/public-invite`, { method: "DELETE" }),
    joinPublicInvite: (token: string) => request<ChatRoom>("/chat/rooms/public-invites/join", {
      method: "POST", body: JSON.stringify({ token }),
    }),
    list: () => request<ChatRoom[]>("/chat/rooms"),
    groupUsage: () => request<{ used: boolean }>("/chat/rooms/group-usage"),
    get: (id: string) => request<ChatRoom>(`/chat/rooms/${encodeURIComponent(id)}`),
    rename: (id: string, title: string) => request<ChatRoom>(`/chat/rooms/${encodeURIComponent(id)}`, {
      method: "PATCH", body: JSON.stringify({ title }),
    }),
    updateAiExecution: (id: string, enabled: boolean, expectedRevision: number) =>
      request<ChatRoom>(`/chat/rooms/${encodeURIComponent(id)}/ai-execution`, {
        method: "PATCH", body: JSON.stringify({ enabled, expected_revision: expectedRevision }),
      }),
    toolApprovals: (id: string) =>
      request<ChatRoomToolApproval[]>(`/chat/rooms/${encodeURIComponent(id)}/ai-tool-approvals`),
    decideToolApproval: (id: string, approvalId: string, approve: boolean) =>
      request<ChatRoomToolApproval>(`/chat/rooms/${encodeURIComponent(id)}/ai-tool-approvals/${encodeURIComponent(approvalId)}/decision`, {
        method: "POST", body: JSON.stringify({ approve }),
      }),
    replyApprovals: (id: string) =>
      request<ChatRoomReplyApproval[]>(`/chat/rooms/${encodeURIComponent(id)}/ai-reply-approvals`),
    decideReplyApproval: (id: string, runId: string, expectedDigest: string, approve: boolean) =>
      request<{ run_id: string; status: "approved" | "rejected" }>(`/chat/rooms/${encodeURIComponent(id)}/ai-reply-approvals/${encodeURIComponent(runId)}/decision`, {
        method: "POST", body: JSON.stringify({ approve, expected_digest: expectedDigest }),
      }),
    close: (id: string) => request<void>(`/chat/rooms/${encodeURIComponent(id)}`, { method: "DELETE" }),
    leave: (id: string) => request<void>(`/chat/rooms/${encodeURIComponent(id)}/membership`, { method: "DELETE" }),
    presence: (id: string) => request<ChatRoomPresence>(`/chat/rooms/${encodeURIComponent(id)}/presence`),
    create: (data: {
      kind: "dm" | "group";
      recipient_email: string;
      title?: string;
      owner_scope?: "personal" | "entity";
      sender_integration_id?: string;
    }) => request<ChatRoom>("/chat/rooms", {
      method: "POST", body: JSON.stringify(data),
    }),
    messages: (id: string, options: { before?: string; after?: string; q?: string; limit?: number } = {}) => {
      const params = new URLSearchParams();
      if (options.before) params.set("before", options.before);
      if (options.after) params.set("after", options.after);
      if (options.q) params.set("q", options.q);
      if (options.limit) params.set("limit", String(options.limit));
      const query = params.toString();
      return request<ChatRoomMessage[]>(`/chat/rooms/${encodeURIComponent(id)}/messages${query ? `?${query}` : ""}`);
    },
    markRead: (id: string, throughMessageId: string) => request<void>(`/chat/rooms/${encodeURIComponent(id)}/read`, {
      method: "POST", body: JSON.stringify({ through_message_id: throughMessageId }),
    }),
    updateNotifications: (id: string, muted: boolean) => request<{ muted: boolean }>(`/chat/rooms/${encodeURIComponent(id)}/notifications`, {
      method: "PATCH", body: JSON.stringify({ muted }),
    }),
    retractMessage: (id: string, messageId: string) => request<void>(
      `/chat/rooms/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}`, { method: "DELETE" },
    ),
    retractions: (id: string, messageIds: string[]) => request<{ message_ids: string[] }>(
      `/chat/rooms/${encodeURIComponent(id)}/message-retractions`, {
        method: "POST", body: JSON.stringify({ message_ids: messageIds }),
      },
    ),
    files: (id: string, options: { before?: string; limit?: number; q?: string } = {}) => {
      const params = new URLSearchParams();
      if (options.before) params.set("before", options.before);
      if (options.limit) params.set("limit", String(options.limit));
      if (options.q) params.set("q", options.q);
      const query = params.toString();
      return request<ChatRoomFile[]>(`/chat/rooms/${encodeURIComponent(id)}/files${query ? `?${query}` : ""}`);
    },
    uploadFile: (id: string, file: File, content = "", mentionedUserIds: string[] = [], replyToMessageId?: string, mentionedAgentBindingIds: string[] = []) => {
      const body = new FormData();
      body.append("file", file);
      body.append("content", content);
      body.append("mentioned_user_ids", JSON.stringify(mentionedUserIds));
      body.append("mentioned_agent_binding_ids", JSON.stringify(mentionedAgentBindingIds));
      if (replyToMessageId) body.append("reply_to_message_id", replyToMessageId);
      return request<ChatRoomMessage>(`/chat/rooms/${encodeURIComponent(id)}/files`, { method: "POST", body });
    },
    attachKnowledgeFile: (id: string, documentId: string, content = "", mentionedUserIds: string[] = [], replyToMessageId?: string, mentionedAgentBindingIds: string[] = []) =>
      request<ChatRoomMessage>(`/chat/rooms/${encodeURIComponent(id)}/files/from-knowledge`, {
        method: "POST",
        body: JSON.stringify({ document_id: documentId, content, mentioned_user_ids: mentionedUserIds, reply_to_message_id: replyToMessageId, mentioned_agent_binding_ids: mentionedAgentBindingIds }),
      }),
    prepareFileDownload: (id: string, fileIds: string[], archive: boolean, signal?: AbortSignal) =>
      request<{ download_url: string }>(`/chat/rooms/${encodeURIComponent(id)}/file-downloads`, {
        method: "POST", body: JSON.stringify({ file_ids: fileIds, archive }), signal,
      }),
    downloadFile: (id: string, fileId: string, options: DocumentBlobOptions = {}) => downloadRoomFile(
      `${API_BASE}/chat/rooms/${encodeURIComponent(id)}/files/${encodeURIComponent(fileId)}`,
      options,
      fetchWithBusyRetry,
    ),
    deleteFile: (id: string, fileId: string) => request<void>(
      `/chat/rooms/${encodeURIComponent(id)}/files/${encodeURIComponent(fileId)}`,
      { method: "DELETE" },
    ),
    send: (id: string, content: string, mentionedUserIds: string[], mentionedAgentBindingIds: string[] = [], replyToMessageId?: string) =>
      request<ChatRoomMessage>(`/chat/rooms/${encodeURIComponent(id)}/messages`, {
        method: "POST", body: JSON.stringify({ content, mentioned_user_ids: mentionedUserIds, mentioned_agent_binding_ids: mentionedAgentBindingIds, reply_to_message_id: replyToMessageId }),
      }),
    addReaction: (id: string, messageId: string, emoji: string) => request<void>(
      `/chat/rooms/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}/reactions`,
      { method: "PUT", body: JSON.stringify({ emoji }) },
    ),
    removeReaction: (id: string, messageId: string, emoji: string) => request<void>(
      `/chat/rooms/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}/reactions/${encodeURIComponent(emoji)}`,
      { method: "DELETE" },
    ),
    inviteSenders: () => request<ChatRoomInviteSender[]>("/chat/rooms/invite-senders"),
    invite: (id: string, email: string, senderIntegrationId?: string) => request<ChatRoomInvite>(`/chat/rooms/${encodeURIComponent(id)}/invites`, {
      method: "POST", body: JSON.stringify({ email, sender_integration_id: senderIntegrationId }),
    }),
    pendingInvites: (id: string) => request<ChatRoomPendingInvite[]>(`/chat/rooms/${encodeURIComponent(id)}/invites`),
    cancelInvite: (id: string, inviteId: string) => request<void>(`/chat/rooms/${encodeURIComponent(id)}/invites/${encodeURIComponent(inviteId)}`, {
      method: "DELETE",
    }),
    accept: (token: string) => request<ChatRoom>("/chat/rooms/invites/accept", {
      method: "POST", body: JSON.stringify({ token }),
    }),
    removeMember: (id: string, userId: string) =>
      request<void>(`/chat/rooms/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`, { method: "DELETE" }),
    updateMemberRole: (id: string, userId: string, role: "admin" | "member") =>
      request<ChatRoom>(`/chat/rooms/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`, {
        method: "PATCH", body: JSON.stringify({ role }),
      }),
    agentCandidates: (id: string) => request<ChatRoomAgentCandidate[]>(`/chat/rooms/${encodeURIComponent(id)}/agent-candidates`),
    bindAgent: (id: string, subscriptionId: string) => request<ChatRoomAgentBinding>(`/chat/rooms/${encodeURIComponent(id)}/agents`, {
      method: "POST", body: JSON.stringify({ subscription_id: subscriptionId }),
    }),
    removeAgent: (id: string, bindingId: string) => request<void>(`/chat/rooms/${encodeURIComponent(id)}/agents/${encodeURIComponent(bindingId)}`, { method: "DELETE" }),
  },
  platform: {
    flags: () => request<{ flags: Record<string, boolean> }>("/platform/flags"),
  },
  auth: {
    currentEmailVerification: () => request<{ email: string; email_verified: boolean }>("/auth/me/email-verification"),
    sendCurrentEmailVerification: () => request<{ email: string; email_verified: boolean; sent: boolean }>("/auth/me/email-verification/send", { method: "POST" }),
    confirmCurrentEmailVerification: (code: string) => request<{ email: string; email_verified: boolean }>("/auth/me/email-verification/confirm", { method: "POST", body: JSON.stringify({ code }) }),
    register: (data: {
      email: string;
      password: string;
      entity_name?: string;
      invitation_code?: string;
      invite_token?: string;
      chat_invite_token?: string;
      timezone?: string;
    }) =>
      request<{ access_token: string; user_id: string; entity_id: string; role: string; is_new?: boolean }>("/auth/register", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    /** Public read of the signup gate — no auth required. */
    signupConfig: () =>
      request<{ invitation_code_required: boolean }>(
        "/platform/signup-config",
      ),
    login: (data: { email: string; password: string; remember_me?: boolean; totp_code?: string }) =>
      request<{ access_token: string; user: User }>("/auth/login", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    renew: () =>
      request<{ access_token: string; user_id: string; entity_id: string; role: string }>("/auth/renew", {
        method: "POST",
      }),
    logout: () => request<void>("/auth/logout", { method: "POST" }),
    verifyEmail: (email: string, code: string, rememberMe = false) =>
      request<{ access_token: string; user_id: string; entity_id: string; role: string }>("/auth/verify-email", {
        method: "POST",
        body: JSON.stringify({ email, code, remember_me: rememberMe }),
      }),
    resendVerification: (email: string) =>
      request<{ message: string }>("/auth/resend-verification", {
        method: "POST",
        body: JSON.stringify({ email }),
      }),
    acceptInvite: (data: { token: string; name?: string; phone?: string }) =>
      request<{ access_token?: string; user_id: string; entity_id: string; staff_id: string; role: string }>("/auth/accept-invite", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    me: () => request<User>("/auth/me"),
    switchEntity: (entityId: string) =>
      request<{ access_token: string; user_id: string; entity_id: string; role: string }>(
        "/auth/entities/switch",
        { method: "POST", body: JSON.stringify({ entity_id: entityId }) },
      ),
    updateProfile: (data: {
      display_name?: string; first_name?: string; last_name?: string;
      phone?: string; timezone?: string; locale?: string; llm_model?: string;
      /** "device": sync the device's zone unless the user chose one. */
      timezone_source?: "user" | "device";
    }) =>
      request<User>("/auth/me", { method: "PUT", body: JSON.stringify(data) }),
    deleteAccount: () =>
      request<{ user_id: string; entity_cascaded: boolean; oauth_revoked: number; grace_days: number }>(
        "/auth/me", { method: "DELETE" },
      ),
    restoreAccount: (data: { email: string; password: string }) =>
      request<{ access_token: string; user_id: string }>(
        "/auth/me/restore", { method: "POST", body: JSON.stringify(data) },
      ),
    accountGraceDays: () =>
      request<{ grace_days: number }>("/auth/me/grace-days"),
    uploadAvatar: async (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      const token = await getReadyAuthToken();
      return fetch("/api/v1/auth/me/avatar", {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
      }).then((r) => r.json() as Promise<{ avatar_url: string }>);
    },
    getLlmConfig: () =>
      request<{
        has_api_key: boolean;
        llm_api_key: string;
        llm_base_url: string;
        role_api_keys?: Record<string, string>;
        role_base_urls?: Record<string, string>;
        role_api_key_models?: Record<string, string>;
        role_model_limits?: Record<string, { model: string; context_window_tokens?: number; max_output_tokens?: number }>;
        byok_allowed?: boolean;
        byok_effective?: boolean;
      }>("/auth/me/llm-config"),
    saveLlmApiKey: (llmApiKey: string, role?: string) =>
      request<{ detail: string; masked: string }>("/auth/me/llm-api-key", {
        method: "PUT", body: JSON.stringify({ llm_api_key: llmApiKey, role }),
      }),
    saveLlmBaseUrl: (llmBaseUrl: string, role?: string) =>
      request<{ detail: string }>("/auth/me/llm-base-url", {
        method: "PUT", body: JSON.stringify({ llm_base_url: llmBaseUrl, role }),
      }),
    testMyModel: (data: { role: string; model: string; api_key?: string; use_saved_api_key?: boolean; base_url?: string }) =>
      request<{ ok: boolean; detail: string; provider?: string | null; latency_ms?: number | null; test_token?: string | null }>("/auth/me/models/test", {
        method: "POST", body: JSON.stringify(data),
      }),
    saveCustomModel: (data: { role: string; model: string; api_key?: string; use_saved_api_key?: boolean; base_url?: string; test_token?: string | null; context_window_tokens?: number; max_output_tokens?: number }) =>
      request<{ detail: string; models: Record<string, string>; masked: string }>("/auth/me/models/custom", {
        method: "PUT", body: JSON.stringify(data),
      }),
    saveCatalogModel: (data: { role: string; model: string; api_key?: string; use_saved_api_key?: boolean; clear_api_key?: boolean; base_url?: string; test_token?: string | null }) =>
      request<{ detail: string; models: Record<string, string>; masked: string }>("/auth/me/models/catalog", {
        method: "PUT", body: JSON.stringify(data),
      }),
    getModelCatalog: () =>
      request<{ catalog: Record<string, any[]>; defaults: Record<string, string> }>("/auth/models/catalog"),
    getMyModels: () =>
      request<{ models: Record<string, string>; user_models: Record<string, string>; entity_models: Record<string, string> }>("/auth/me/models"),
    updateMyModels: (data: { models: Record<string, string> }) =>
      request<{
        models: Record<string, string>;
        changed: Record<string, { old?: string; new: string }>;
      }>("/auth/me/models", {
        method: "PUT", body: JSON.stringify(data),
      }),
    oauthGoogle: (opts: {
      code?: string;
      redirectUri: string;
      invitationCode?: string;
      teamInviteToken?: string;
      chatInviteToken?: string;
      oauthSession?: string;
      publicChatToken?: string;
      rememberMe?: boolean;
    }) =>
      request<{ access_token: string; user: User; is_new?: boolean }>("/auth/oauth/google", {
        method: "POST",
        body: JSON.stringify({
          code: opts.code,
          redirect_uri: opts.redirectUri,
          ...(opts.invitationCode ? { invitation_code: opts.invitationCode } : {}),
          ...(opts.teamInviteToken ? { team_invite_token: opts.teamInviteToken } : {}),
          ...(opts.chatInviteToken ? { chat_invite_token: opts.chatInviteToken } : {}),
          ...(opts.oauthSession ? { oauth_session: opts.oauthSession } : {}),
          ...(opts.publicChatToken ? { public_chat_token: opts.publicChatToken } : {}),
          remember_me: Boolean(opts.rememberMe),
        }),
      }),
    googleOAuthConfig: () =>
      request<{ enabled: boolean; client_id: string | null }>("/auth/oauth/google/config"),
    forgotPassword: (email: string) =>
      request<any>("/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify({ email }),
      }),
    resetPassword: (token: string, newPassword: string) =>
      request<any>("/auth/reset-password", {
        method: "POST",
        headers: { "X-Silent-Error": "1" },
        body: JSON.stringify({ token, new_password: newPassword }),
      }),
    inviteInfo: (token: string) =>
      request<{ email: string; name?: string | null; entity_name: string }>(
        `/auth/invite-info?${new URLSearchParams({ token }).toString()}`,
      ),
    changePassword: (oldPassword: string, newPassword: string) =>
      request<any>("/auth/password", {
        method: "PUT",
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      }),
  },

  tasks: {
    constants: () => request<TaskConstantsResponse>("/tasks/constants"),
    getBoardPreferences: () =>
      request<TaskBoardPreferences>("/tasks/board-preferences"),
    updateBoardPreferences: (data: Omit<TaskBoardPreferences, "configured">) =>
      request<TaskBoardPreferences>("/tasks/board-preferences", {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    list: (params?: { status?: string; attention?: boolean; limit?: number; offset?: number; parent_task_id?: string; workspace_id?: string; category_id?: string }) => {
      const q = new URLSearchParams();
      if (params?.status) q.set("status", params.status);
      if (params?.attention) q.set("attention", "true");
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.offset) q.set("offset", String(params.offset));
      if (params?.parent_task_id) q.set("parent_task_id", params.parent_task_id);
      if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
      if (params?.category_id) q.set("category_id", params.category_id);
      return request<PaginatedResponse<Task>>(`/tasks?${q}`);
    },
    listAttention: async (workspaceId: string) => {
      const items: Task[] = [];
      let total = 1;
      while (items.length < total) {
        const q = new URLSearchParams({
          workspace_id: workspaceId,
          attention: "true",
          limit: "200",
          offset: String(items.length),
        });
        const page = await request<PaginatedResponse<Task>>(`/tasks?${q}`);
        total = page.total;
        if (page.items.length === 0) break;
        items.push(...page.items);
      }
      return items;
    },
    get: (id: string) => request<Task>(`/tasks/${id}`),
    create: (data: Partial<Task>) =>
      request<Task>("/tasks", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<Task>) =>
      request<Task>(`/tasks/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      request<void>(`/tasks/${id}`, { method: "DELETE" }),
    board: (workspaceId?: string, offset = 0, limit = 50, signal?: AbortSignal) => {
      const q = new URLSearchParams({ offset_per_status: String(offset), limit_per_status: String(limit) });
      if (workspaceId) q.set("workspace_id", workspaceId);
      return request<Record<string, Task[]> & { _counts: Record<string, number> }>(`/tasks/board?${q}`, { signal });
    },
    move: (taskId: string, status: string) =>
      request<Task>(`/tasks/${taskId}/move`, {
        method: "POST",
        body: JSON.stringify({ status }),
      }),
    retry: (taskId: string, note?: string) =>
      request<TaskRetryResponse>(`/tasks/${taskId}/retry`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),
    respondHITL: (taskId: string, data: { request_id?: string; response?: string; choice?: string; fields?: Record<string, any>; note?: string }) =>
      request<TaskHITLResponse>(`/tasks/${taskId}/hitl-response`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    decideApproval: (taskId: string, data: { choice: string; note?: string }) =>
      request<Task>(`/tasks/${taskId}/approval`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    logs: (taskId: string) =>
      request<any[]>(`/tasks/${taskId}/logs`),
    addLog: (taskId: string, content: string, logType: string = "comment", attachments?: any[], mentions?: { type: "agent" | "user"; id: string }[], requestId?: string) =>
      request<any>(`/tasks/${taskId}/logs`, { method: "POST", body: JSON.stringify({ content, log_type: logType, attachments: attachments || [], mentions: mentions || [], request_id: requestId }) }),
    uploadAttachment: async (taskId: string, file: File) => {
      const token = await getReadyAuthToken();
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`/api/v1/tasks/${taskId}/attachments`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
      });
      if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
      return res.json() as Promise<{ filename: string; original_name: string; size: number; content_type: string; url: string }>;
    },
    fromTemplate: (templateId: string, overrides?: Partial<Task>) =>
      request<Task>("/tasks/from-template", {
        method: "POST",
        body: JSON.stringify({ template_id: templateId, ...overrides }),
      }),
    categories: {
      list: () => request<TaskCategory[]>("/tasks/categories"),
      create: (data: Partial<TaskCategory>) =>
        request<TaskCategory>("/tasks/categories", {
          method: "POST",
          body: JSON.stringify(data),
        }),
      update: (id: string, data: Partial<TaskCategory>) =>
        request<TaskCategory>(`/tasks/categories/${id}`, {
          method: "PUT",
          body: JSON.stringify(data),
        }),
      delete: (id: string) =>
        request<void>(`/tasks/categories/${id}`, { method: "DELETE" }),
    },
    slaPolicies: {
      list: () =>
        request<Array<{
          id: string; name: string;
          response_seconds: number; resolution_seconds: number;
          priority: string | null; category_id: string | null;
          status: string;
        }>>("/tasks/sla-policies"),
      create: (data: {
        name: string;
        response_seconds?: number;
        resolution_seconds?: number;
        priority?: string;
        category_id?: string;
      }) => request<any>("/tasks/sla-policies", {
        method: "POST", body: JSON.stringify(data),
      }),
      update: (id: string, data: Record<string, any>) =>
        request<any>(`/tasks/sla-policies/${id}`, {
          method: "PUT", body: JSON.stringify(data),
        }),
      delete: (id: string) =>
        request<void>(`/tasks/sla-policies/${id}`, { method: "DELETE" }),
    },
  },

  calendarSettings: {
    get: () => request<CalendarSettingsResponse>("/calendar-settings"),
    calendars: (provider?: string, connectionId?: string) => {
      const q = new URLSearchParams();
      if (provider) q.set("provider", provider);
      if (connectionId !== undefined) q.set("connection_id", connectionId);
      return request<CalendarOptionsResponse>(
        `/calendar-settings/calendars${q.toString() ? `?${q}` : ""}`,
      );
    },
    update: (data: Partial<Omit<CalendarSettings, "booking_links" | "bookings">>) =>
      request<CalendarSettingsResponse>("/calendar-settings", {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    createBookingLink: (data: BookingLinkWrite) =>
      request<BookingLink>("/calendar-settings/booking-links", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    updateBookingLink: (id: string, data: BookingLinkWrite) =>
      request<BookingLink>(`/calendar-settings/booking-links/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    deleteBookingLink: (id: string) =>
      request<void>(`/calendar-settings/booking-links/${id}`, { method: "DELETE" }),
    day: (day?: string) => {
      const q = new URLSearchParams();
      if (day) q.set("day", day);
      return request<DailyAgendaResponse>(`/calendar-settings/day${q.toString() ? `?${q}` : ""}`);
    },
    events: (start: string, end: string, source?: { provider: string; connectionId: string }) => {
      const q = new URLSearchParams({ start, end });
      if (source) {
        q.set("provider", source.provider);
        q.set("connection_id", source.connectionId);
        q.set("include_bookings", "true");
      }
      return request<ExternalCalendarEventsResponse>(`/calendar-settings/events?${q}`);
    },
    publicBookingLink: (slug: string, ownerId?: string, month?: string, timezone?: string) => {
      const q = new URLSearchParams();
      if (month) q.set("month", month);
      if (timezone) q.set("timezone", timezone);
      const suffix = q.toString() ? `?${q}` : "";
      return request<PublicBookingLink>(
        `${ownerId
          ? `/calendar-settings/public/booking-links/u/${encodeURIComponent(ownerId)}/${encodeURIComponent(slug)}`
          : `/calendar-settings/public/booking-links/${encodeURIComponent(slug)}`}${suffix}`,
        {},
        null,
      );
    },
    bookPublicBookingLink: (slug: string, data: PublicBookingRequest, ownerId?: string) =>
      request<BookingConfirmation>(
        `${ownerId
          ? `/calendar-settings/public/booking-links/u/${encodeURIComponent(ownerId)}/${encodeURIComponent(slug)}`
          : `/calendar-settings/public/booking-links/${encodeURIComponent(slug)}`}/book`,
        { method: "POST", body: JSON.stringify(data) },
        null,
      ),
  },

  chat: {
    refreshTikTokPublishReview: (hitlId: string, signal?: AbortSignal) =>
      request<unknown>(`/chat/approvals/${encodeURIComponent(hitlId)}/tiktok-review`, {
        method: "POST", body: "{}", signal,
      }),
    listComposerSkills: (scope: {
      conversation_id?: string; workspace_id?: string; agent_id?: string;
      thread_ref_kind?: string; thread_ref_id?: string;
    }) => {
      const query = new URLSearchParams();
      Object.entries(scope).forEach(([key, value]) => { if (value) query.set(key, value); });
      return request<any[]>(`/chat/composer/skills?${query}`);
    },
    listFlowEntrypoints: (workspaceId?: string) => {
      const query = new URLSearchParams();
      if (workspaceId) query.set("workspace_id", workspaceId);
      const suffix = query.toString() ? `?${query}` : "";
      return request<GlobalChatFlowEntrypoint[]>(`/chat/flow-entrypoints${suffix}`);
    },
    streamFlowEntrypoint: async (
      bindingId: string,
      message: string,
      conversationId?: string,
      opts?: {
        files?: File[];
        documentIds?: string[];
        agentId?: string;
        workspaceId?: string;
        localWorkerId?: string;
      },
    ): Promise<Response> => {
      const form = new FormData();
      form.append("message", message);
      if (conversationId) form.append("conversation_id", conversationId);
      if (opts?.agentId) form.append("agent_id", opts.agentId);
      if (opts?.workspaceId) form.append("workspace_id", opts.workspaceId);
      if (opts?.localWorkerId) form.append("local_worker_id", opts.localWorkerId);
      if (opts?.documentIds?.length) form.append("document_ids", opts.documentIds.join(","));
      for (const file of opts?.files || []) form.append("files", await prepareChatUploadFile(file));
      return requestStreamResponse(
        `/chat/flow-entrypoints/${encodeURIComponent(bindingId)}/stream`,
        form,
      );
    },
    stream: async (
      message: string,
      conversationId?: string,
      opts?: {
        clientMessageId?: string;
        files?: File[];
        documentIds?: string[];
        manualSkillIds?: string[];
        manualSkillRefs?: ManualSkillReference[];
        agentId?: string;
        localWorkerId?: string;
        workspaceId?: string;
        workspaceContext?: boolean;
        /** The Workspace App entry sending this request, so the App lists it under that entry. */
        workspaceAppAction?: string;
        threadRef?: { kind: "task" | "plan" | "goal"; id: string };
        chatMode?: string;
        chatModePayload?: Record<string, unknown>;
        responseSurfaceSubmission?: object;
        disableTools?: boolean;
        blockedTools?: string[];
        editorContext?: {
          target_kind?: AiEditTargetKind | null;
          target_id?: string | null;
          path?: string | null;
          sourcePath?: string | null;
          document_id?: string | null;
          documentName?: string | null;
          fileType?: string | null;
          mimeType?: string | null;
          editorType?: string | null;
          supportsImageGeneration?: boolean | null;
          supportsNativeFilePatch?: boolean | null;
          currentDocumentContent?: string | null;
        };
        conversationSurface?: ConversationSurfaceKind;
        ephemeral?: boolean;
        signal?: AbortSignal;
      },
    ): Promise<Response> => {
      const form = new FormData();
      form.append("message", message);
      if (opts?.clientMessageId) form.append("client_message_id", opts.clientMessageId);
      if (conversationId) form.append("conversation_id", conversationId);
      if (opts?.agentId) form.append("agent_id", opts.agentId);
      if (opts?.localWorkerId) form.append("local_worker_id", opts.localWorkerId);
      if (opts?.workspaceContext) form.append("workspace_context", "true");
      if (opts?.workspaceContext && opts?.workspaceId) form.append("workspace_id", opts.workspaceId);
      if (opts?.workspaceContext && opts?.workspaceId && opts?.workspaceAppAction) {
        form.append("workspace_app_action", opts.workspaceAppAction);
      }
      if (opts?.threadRef?.kind) form.append("thread_ref_kind", opts.threadRef.kind);
      if (opts?.threadRef?.id) form.append("thread_ref_id", opts.threadRef.id);
      if (opts?.chatMode) form.append("chat_mode", opts.chatMode);
      if (opts?.chatModePayload) form.append("chat_mode_payload", JSON.stringify(opts.chatModePayload));
      if (opts?.responseSurfaceSubmission) {
        form.append("response_surface_submission", JSON.stringify(opts.responseSurfaceSubmission));
      }
      if (opts?.documentIds?.length) form.append("document_ids", opts.documentIds.join(","));
      const compatibleManualSkillIds = Array.from(
        new Set([
          ...(opts?.manualSkillIds || []),
          ...legacyManualSkillIds(opts?.manualSkillRefs),
        ]),
      );
      if (compatibleManualSkillIds.length) {
        form.append("manual_skill_ids", compatibleManualSkillIds.join(","));
      }
      if (opts?.manualSkillRefs?.length) form.append("manual_skill_refs", JSON.stringify(opts.manualSkillRefs));
      if (opts?.disableTools) form.append("disable_tools", "true");
      if (opts?.blockedTools?.length) form.append("blocked_tools", opts.blockedTools.join(","));
      if (opts?.editorContext) form.append("editor_context", JSON.stringify(opts.editorContext));
      if (opts?.conversationSurface) form.append("conversation_surface", opts.conversationSurface);
      if (opts?.ephemeral) form.append("ephemeral", "true");
      for (const file of opts?.files || []) form.append("files", await prepareChatUploadFile(file));
      return requestStreamResponse("/chat/stream", form, opts?.signal);
    },
    listConversations: (wsId?: string) =>
      request<Conversation[]>(`/chat/conversations${wsId ? `?workspace_id=${wsId}` : ""}`),
    startAgentConversation: (agentId: string, title: string) =>
      request<Conversation>("/chat/conversations/agent-draft", {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, title }),
      }),
    getMessages: (convId: string, opts?: { silent?: boolean; limit?: number }) =>
      request<GlobalChatMessage[]>(`/chat/conversations/${convId}/messages?limit=${encodeURIComponent(String(opts?.limit ?? 500))}`, {
        headers: opts?.silent ? { "X-Silent-Error": "1" } : undefined,
      }),
    getMessagesPage: (
      convId: string,
      opts?: { silent?: boolean; limit?: number; before?: string | null },
    ) => {
      const q = new URLSearchParams();
      q.set("limit", String(opts?.limit ?? 75));
      if (opts?.before) q.set("before", opts.before);
      return request<PaginatedMessagesResponse<GlobalChatMessage>>(
        `/chat/conversations/${convId}/messages/page?${q}`,
        {
          headers: opts?.silent ? { "X-Silent-Error": "1" } : undefined,
        },
      );
    },
    resolveAction: (
      msgId: string,
      choice: string,
      note?: string,
      payload?: Record<string, unknown>,
    ) => request<GlobalChatMessage>(`/chat/messages/${msgId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ choice, note, payload }),
    }),
    feedback: (
      convId: string,
      messageId: string,
      body: {
        rating: "up" | "down";
        content_preview?: string;
        request_preview?: string;
      },
    ) =>
      request<{
        message_id: string;
        rating: "up" | "down";
        mutation_sequence: number;
        mutation_status: "accepted";
        updated_at: string | null;
        target_kind: string;
        target_id: string;
        task_id: string | null;
        plan_id: string | null;
      }>(
        `/chat/conversations/${convId}/messages/${messageId}/feedback`,
        { method: "POST", body: JSON.stringify(body) },
      ),
    listFeedback: (convId: string) =>
      request<Array<{
        message_id: string;
        rating: "up" | "down";
        mutation_sequence: number;
        target_kind: string;
        target_id: string;
        task_id: string | null;
        plan_id: string | null;
      }>>(`/chat/conversations/${convId}/feedback`, {
        headers: { "X-Silent-Error": "1" },
      }),
    cancelPendingFileApprovals: (convId: string, hitlIds?: string[]) =>
      request<{ cancelled: number }>(`/chat/conversations/${convId}/file-approvals/cancel`, {
        method: "POST",
        body: JSON.stringify({
          hitl_ids: hitlIds && hitlIds.length > 0 ? hitlIds : undefined,
          reason: "request_stopped",
        }),
        headers: { "X-Silent-Error": "1" },
      }),
    renameConversation: (id: string, title: string) =>
      request<Conversation>(`/chat/conversations/${id}`, {
        method: "PUT",
        body: JSON.stringify({ title }),
      }),
    deleteConversation: (id: string, opts?: { silent?: boolean }) =>
      request<void>(`/chat/conversations/${id}`, {
        method: "DELETE",
        headers: opts?.silent ? { "X-Silent-Error": "1" } : undefined,
      }),
    tts: async (text: string, voice?: string): Promise<ArrayBuffer> => {
      const token = await getReadyAuthToken();
      const resp = await fetch(`${API_BASE}/chat/tts`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice: voice || "alloy" }),
      });
      if (!resp.ok) throw new Error(`TTS failed: ${resp.status}`);
      return resp.arrayBuffer();
    },
  },

  mediaJobs: {
    get: (id: string) => request<MediaJob>(`/media/jobs/${encodeURIComponent(id)}`),
    /** Every version of one clip, oldest first. */
    versions: (lineageRoot: string) => request<MediaJob[]>(
      `/media/jobs?lineage_root=${encodeURIComponent(lineageRoot)}&limit=50`,
    ),
    /** Every job a Workflow run started, newest first, regenerated versions included. */
    forRun: (runId: string) => request<MediaJob[]>(
      `/media/jobs?run_id=${encodeURIComponent(runId)}&limit=50`,
    ),
    regenerate: (id: string, input: MediaJobRegenerateInput) => request<MediaJob>(
      `/media/jobs/${encodeURIComponent(id)}/regenerate`,
      { method: "POST", body: JSON.stringify(input) },
    ),
  },

  videoEditor: {
    searchFreeMusic: (query: string, page = 1, pageSize = 18): Promise<FreeMusicSearchResult> => {
      const params = new URLSearchParams({
        q: query,
        page: String(page),
        page_size: String(pageSize),
      });
      return request<FreeMusicSearchResult>(`/media/free-music/search?${params}`);
    },
    importFreeMusic: (trackId: string, sourceDocumentId: string): Promise<FreeMusicImportResult> => (
      request<FreeMusicImportResult>("/media/free-music/import", {
        method: "POST",
        body: JSON.stringify({ track_id: trackId, source_document_id: sourceDocumentId }),
      })
    ),
    finalizePreview: (
      file: File,
      sourceDocumentId: string,
      outputName: string,
      options?: { fps?: number; crf?: number; preset?: string; targetDurationSeconds?: number },
    ): Promise<VideoEditorFinalizeResult> => {
      const form = new FormData();
      form.append("preview", file);
      form.append("source_document_id", sourceDocumentId);
      form.append("output_name", outputName);
      form.append("fps", String(options?.fps ?? 30));
      form.append("crf", String(options?.crf ?? 18));
      form.append("preset", options?.preset ?? "veryfast");
      if (typeof options?.targetDurationSeconds === "number" && options.targetDurationSeconds > 0) {
        form.append("target_duration_seconds", String(options.targetDurationSeconds));
      }
      return request<VideoEditorFinalizeResult>("/media/video-editor/finalize", {
        method: "POST",
        body: form,
      });
    },
    linkRecipe: (finalDocumentId: string, recipeDocumentId: string): Promise<VideoEditorRecipeLinkResult> => {
      const form = new FormData();
      form.append("final_document_id", finalDocumentId);
      form.append("recipe_document_id", recipeDocumentId);
      return request<VideoEditorRecipeLinkResult>("/media/video-editor/link-recipe", {
        method: "POST",
        body: form,
      });
    },
  },

  documents: {
    list: listDocuments,
    listAll: listAllDocuments,
    browse: browseDocuments,
    browsePage: browseDocumentsPage,
    contentSearch: searchDocumentContent,
    indexingStatuses: listDocumentIndexingStatuses,
    move: async (id: string, folderId: string | null, filingOnly = false) => {
      const result = await request<Document>(`/documents/${id}/move`, { method: "POST", body: JSON.stringify({ folder_id: folderId, filing_only: filingOnly }) });
      await invalidateDocumentDownloadCache(id);
      return result;
    },
    get: (id: string, signal?: AbortSignal) => request<Document>(`/documents/${id}`, { signal }),
    // Preview metadata is a local snapshot, not authorization for server writes.
    getForPreview: async (id: string, options: DocumentResponseOptions = {}): Promise<Document> => {
      const response = await fetchCachedFile(`${API_BASE}/documents/${id}`, options, async (url, init) => {
        const result = await fetchWithBusyRetry(url, init);
        if (!result.ok) throw new ApiError(result.status, result.status >= 500 ? t("lib.api.backend_unavailable") : "Failed to load document");
        return result;
      });
      return response.json();
    },
    upload: (
      file: File,
      folderId?: string | null,
      options?: {
        visibility?: string;
        classification?: string;
        client_visible?: boolean;
      },
      requestOptions?: DocumentUploadRequestOptions,
    ): Promise<Document> => uploadDocumentRequest(file, folderId, options, requestOptions),
    reconcileUploadReceipt: (
      idempotencyKey: string,
      options?: Pick<DocumentUploadRequestOptions, "signal" | "receiptReconcileTimeoutMs">,
    ): Promise<Document> => reconcileDocumentUploadReceipt(
      idempotencyKey,
      Math.max(
        0,
        options?.receiptReconcileTimeoutMs
          ?? DEFAULT_DOCUMENT_UPLOAD_RECEIPT_RECONCILE_TIMEOUT_MS,
      ),
      options?.signal,
      authPrincipalKey(getAuthToken()),
    ),
    delete: async (id: string) => {
      const result = await request<void>(`/documents/${id}`, { method: "DELETE" });
      await invalidateDocumentDownloadCache(id);
      return result;
    },
    rename: async (id: string, name: string) => {
      const result = await request<Document>(`/documents/${id}`, { method: "PUT", body: JSON.stringify({ name }) });
      await invalidateDocumentDownloadCache(id);
      return result;
    },
    getContent: (id: string, signal?: AbortSignal) =>
      request<{ content: string }>(`/documents/${id}/content`, { signal }),
    saveContent: async (
      id: string,
      content: string,
      saveIntent?: EditorSaveIntent,
      authTokenOverride?: string | null,
    ) => {
      const result = await request<{ saved: boolean }>(`/documents/${id}/content`, {
        method: "PUT",
        body: JSON.stringify({
          content,
          save_session_id: saveIntent?.sessionId,
          save_sequence: saveIntent?.sequence,
        }),
      }, authTokenOverride);
      await invalidateDocumentDownloadCache(id);
      return result;
    },
    replaceFile: async (
      id: string,
      file: File,
      saveIntent?: EditorSaveIntent,
      authTokenOverride?: string | null,
      expectedSourceSha256?: string | null,
    ): Promise<Document> => {
      const token = authTokenOverride === undefined ? await getReadyAuthToken() : authTokenOverride;
      const form = new FormData();
      form.append("file", file);
      if (saveIntent) {
        form.append("save_session_id", saveIntent.sessionId);
        form.append("save_sequence", String(saveIntent.sequence));
      }
      if (expectedSourceSha256) {
        form.append("expected_source_sha256", expectedSourceSha256);
      }
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`${API_BASE}/documents/${id}/file`, {
        method: "PUT",
        headers,
        body: form,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }));
        const detail = body.detail;
        const message = (typeof detail === "string" ? detail : (detail as any)?.message) || res.statusText;
        throw new ApiError(res.status, message);
      }
      await invalidateDocumentDownloadCache(id);
      return res.json() as Promise<Document>;
    },
    getVersions: (id: string) => request<any[]>(`/documents/${id}/versions`),
    download: async (id: string, options?: DocumentBlobOptions): Promise<string> => {
      const blob = await fetchDocumentBlob(id, options);
      return URL.createObjectURL(blob);
    },
    downloadBlob: (id: string, options?: DocumentBlobOptions): Promise<Blob> =>
      fetchDocumentBlob(id, options),
    editableResponse: (id: string): Promise<Response> =>
      fetchDocumentResponse(id, {}, "editable-file"),
    downloadResponse: (id: string, options?: DocumentResponseOptions): Promise<Response> =>
      fetchDocumentResponse(id, options),
    preview: async (id: string, options?: DocumentBlobOptions): Promise<string> =>
      URL.createObjectURL(await fetchProtectedDocumentBlob(`preview:${id}`, `/documents/${id}/preview/content`, options)),
    previewBlob: (id: string, options?: DocumentBlobOptions): Promise<Blob> =>
      fetchProtectedDocumentBlob(`preview:${id}`, `/documents/${id}/preview/content`, options),
    previewResponse: (id: string, options?: DocumentResponseOptions): Promise<Response> =>
      fetchDocumentResponse(id, options, "preview/content"),
    thumbnail: (id: string, options?: DocumentThumbnailOptions): Promise<string> => fetchDocumentThumbnailUrl(id, options),
    imageThumbnail: (id: string, options?: DocumentThumbnailOptions): Promise<string> => fetchDocumentImageThumbnailUrl(id, options),
    localImageThumbnail: (file: Blob): Promise<string> => captureDocumentImageThumbnail(file),
    presentationThumbnail: (id: string, options?: DocumentThumbnailOptions): Promise<string> =>
      fetchDocumentPresentationThumbnailUrl(id, options),
    videoThumbnail: (id: string, options?: DocumentThumbnailOptions): Promise<string> => fetchDocumentVideoThumbnailUrl(id, options),
    thumbnailCache: {
      get: (id: string, options?: Pick<DocumentThumbnailOptions, "version">): string | null =>
        readPersistentThumbnailDataUrl(id, options?.version),
      setDataUrl: (id: string, dataUrl: string, options?: Pick<DocumentThumbnailOptions, "version">): string | null =>
        writePersistentThumbnailDataUrl(id, options?.version, dataUrl),
    },
    clearDownloadCache: (id?: string) => invalidateDocumentDownloadCache(id),
    /** Protected media is fetched as a blob so raw FS URLs are not public. */
    streamUrl: (doc: { entity_id: string; fs_path?: string | null; id: string }): string | null => {
      return null;
    },
    getSlides: (id: string) =>
      request<{ slides: { index: number; url: string }[]; total: number }>(`/documents/${id}/slides`),
    presentationObjectBlob: (
      id: string,
      slideIndex: number,
      objectId: string,
      signal?: AbortSignal,
    ): Promise<Blob> =>
      fetchProtectedDocumentBlob(
        `presentation-object:${id}:${slideIndex}:${objectId}`,
        `/documents/${id}/slides/${slideIndex}/objects/${encodeURIComponent(objectId)}`,
        { signal },
      ),
    getPages: (id: string, signal?: AbortSignal) =>
      request<{
        pages: { index: number; url: string; width: number | null; height: number | null }[];
        total: number;
        version: string;
      }>(
        `/documents/${id}/pages`,
        { signal },
      ),
    listGroups: () => request<any[]>("/documents/groups"),
    createGroup: (data: { name: string; workspace_id?: string }) =>
      request<any>("/documents/groups", { method: "POST", body: JSON.stringify(data) }),
    addToGroup: (docId: string, groupId: string) =>
      request<any>(`/documents/${docId}/groups/${groupId}`, { method: "POST" }),
    getWorkspaces: (docId: string) =>
      request<string[]>(`/documents/${docId}/workspaces`),
    batchAddToGroup: (documentIds: string[], groupId: string) =>
      request<{ added: number; total: number }>("/documents/groups/batch-add", {
        method: "POST",
        body: JSON.stringify({ document_ids: documentIds, group_id: groupId }),
      }),
    createBlank: (data: { name: string; file_type?: string }) =>
      request<Document>("/documents/create-blank", { method: "POST", body: JSON.stringify(data) }),
    aiDraft: (data: { prompt: string; file_type?: string; name?: string }) =>
      request<Document>("/documents/ai-draft", { method: "POST", body: JSON.stringify(data) }),
    createFromUrl: (data: { url: string; name?: string }) =>
      request<Document>("/documents/from-url", { method: "POST", body: JSON.stringify(data) }),
    trash: async (id: string) => {
      await request<void>(`/documents/${id}/trash`, { method: "POST" });
      await invalidateDocumentDownloadCache(id);
    },
    restore: async (id: string) => {
      await request<void>(`/documents/${id}/restore`, { method: "POST" });
      await invalidateDocumentDownloadCache(id);
    },
    listTrash: () => request<Document[]>("/documents/trash"),
    emptyTrash: () => request<void>("/documents/trash/empty", { method: "POST" }),
    reindex: () => request<{ count: number }>("/documents/reindex", { method: "POST" }),
    reindexOne: (id: string) => request<{ status: string }>(`/documents/${id}/reindex`, { method: "POST" }),
    cancelIndex: (id: string) => request<{ status: string }>(`/documents/${id}/cancel-index`, { method: "POST" }),
    uploadFromGoogleDrive: (data: {
      file_id: string; name: string; mime_type?: string;
      file_size?: number; modified_time?: string; access_token: string;
      folder_id?: string | null;
    }) =>
      request<Document>("/documents/from-google-drive", {
        method: "POST", body: JSON.stringify(data),
      }),
    syncGoogleDrive: async (docId: string, data: {
      file_id: string; name: string; mime_type?: string;
      file_size?: number; modified_time?: string; access_token: string;
    }) => {
      const result = await request<{ status: string }>(`/documents/${docId}/sync-google-drive`, {
        method: "POST", body: JSON.stringify(data),
      });
      await invalidateDocumentDownloadCache(docId);
      return result;
    },
  },

  folders: {
    list: () => request<DocumentFolderInfo[]>("/documents/folders"),
    tree: (includeCounts = true) => request<DocumentFolderInfo[]>(`/documents/folder-tree?include_counts=${includeCounts}`),
    create: (data: { name: string; parent_id?: string }) =>
      request<DocumentFolderInfo>("/documents/folders", { method: "POST", body: JSON.stringify(data) }),
    rename: (id: string, name: string) =>
      request<DocumentFolderInfo>(`/documents/folders/${id}`, { method: "PUT", body: JSON.stringify({ name }) }),
    move: (id: string, parentId: string | null) =>
      request<DocumentFolderInfo>(`/documents/folders/${id}/move`, { method: "POST", body: JSON.stringify({ parent_id: parentId }) }),
    delete: (id: string) =>
      request<void>(`/documents/folders/${id}`, { method: "DELETE" }),
  },

  // ── Document permissions (RFC §13, P3) ────────────────────────────────
  // Top-level namespace — siblings of `documents`/`folders` because the
  // existing api object is flat. Frontend calls these as
  // `api.docPermissions.listGrants(...)`. Backend at
  // apps/api/routers/document_permissions.py + permissions_v1.py.
  docPermissions: {
    // Internal grants
    listGrants: (docId: string) =>
      request<DocumentGrant[]>(`/documents/${docId}/grants`),
    createGrant: (
      docId: string,
      data: {
        subject_type?: "user" | "staff_role" | "workspace_role" | "team";
        subject_id: string;
        capabilities: string[];
        expires_at?: string;
      },
    ) =>
      request<DocumentGrant>(`/documents/${docId}/grants`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    revokeGrant: (docId: string, grantId: string) =>
      request<void>(`/documents/${docId}/grants/${grantId}`, { method: "DELETE" }),

    // External shares
    listShares: (docId: string) =>
      request<DocumentShare[]>(`/documents/${docId}/shares`),
    createShare: (
      docId: string,
      data: {
        audience_type: "anonymous" | "email" | "domain";
        audience_value?: string;
        capabilities: ("view" | "comment" | "download")[];
        expires_in_days?: number | null;
        watermark?: boolean;
        require_otp?: boolean;
        allow_download?: boolean;
      },
    ) =>
      request<DocumentShare & { token: string; url: string }>(
        `/documents/${docId}/shares`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    revokeShare: (docId: string, shareId: string) =>
      request<void>(`/documents/${docId}/shares/${shareId}`, { method: "DELETE" }),

    // External PDF signature requests
    listSignatureSenders: (docId: string) =>
      request<Array<import("./emailAccounts").EmailAccount>>(`/documents/${docId}/signature-senders`),
    listSignatureRequests: (docId: string) =>
      request<DocumentSignatureRequest[]>(`/documents/${docId}/signature-requests`),
    createSignatureRequest: (
      docId: string,
      data: {
        recipient_name: string;
        recipient_email: string;
        recipient_emails?: string[];
        message?: string;
        expires_in_days?: number;
        placement?: DocumentSignatureRequest["placement"];
        fields?: DocumentSignatureRequest["fields"];
        additional_recipients?: Array<{
          recipient_name: string;
          recipient_email: string;
          recipient_emails?: string[];
          fields: NonNullable<DocumentSignatureRequest["fields"]>;
        }>;
        sender_integration_id?: string;
      },
    ) => request<DocumentSignatureRequest>(`/documents/${docId}/signature-requests`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
    resendSignatureRequest: (docId: string, requestId: string) =>
      request<DocumentSignatureRequest>(
        `/documents/${docId}/signature-requests/${requestId}/resend`,
        { method: "POST" },
      ),
    revokeSignatureRequest: (docId: string, requestId: string) =>
      request<void>(`/documents/${docId}/signature-requests/${requestId}`, { method: "DELETE" }),

    // Access log
    accessLog: (docId: string, limit = 50) =>
      request<DocumentAccessLogRow[]>(`/documents/${docId}/access-log?limit=${limit}`),

    // Access requests — list/decide are doc-scoped here; CREATE lives
    // under /permissions/access-requests (see permissionsV1 below).
    listAccessRequests: (docId: string, status?: string) =>
      request<AccessRequest[]>(
        `/documents/${docId}/access-requests${status ? `?status=${encodeURIComponent(status)}` : ""}`,
      ),
    decideAccessRequest: (
      docId: string,
      requestId: string,
      data: { decision: "approve" | "deny"; approved_capabilities?: string[]; expires_at?: string; note?: string },
    ) =>
      request<AccessRequest>(
        `/documents/${docId}/access-requests/${requestId}/decision`,
        { method: "POST", body: JSON.stringify(data) },
      ),

    // Share-approval flow (Confidential external share, RFC §13.6)
    listShareApprovals: (docId: string, status?: string) =>
      request<ShareApproval[]>(
        `/documents/${docId}/share-approvals${status ? `?status=${encodeURIComponent(status)}` : ""}`,
      ),
    requestShareApproval: (
      docId: string,
      data: {
        audience_type: "anonymous" | "email" | "domain";
        audience_value?: string;
        capabilities: ("view" | "comment" | "download")[];
        expires_in_days?: number | null;
        watermark?: boolean;
        require_otp?: boolean;
        allow_download?: boolean;
        reason: string;
      },
    ) =>
      request<ShareApproval>(`/documents/${docId}/share-approvals`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    decideShareApproval: (
      docId: string,
      approvalId: string,
      data: { decision: "approve" | "deny"; note?: string },
    ) =>
      request<{ approval: ShareApproval; token?: string; url?: string }>(
        `/documents/${docId}/share-approvals/${approvalId}/decision`,
        { method: "POST", body: JSON.stringify(data) },
      ),
  },

  // ── permissions_v1: classify / visibility / request-access
  // Lives at /api/v1/permissions/... (separate router; uses authorize()).
  permissionsV1: {
    classify: (docId: string, classification: string, note?: string) =>
      request<{ id: string; classification: string; previous: string }>(
        `/permissions/documents/${docId}/classify`,
        { method: "POST", body: JSON.stringify({ classification, note }) },
      ),
    setVisibility: (docId: string, visibility: string) =>
      request<{ id: string; visibility: string }>(
        `/permissions/documents/${docId}/visibility`,
        { method: "POST", body: JSON.stringify({ visibility }) },
      ),
    setClientVisible: (docId: string, clientVisible: boolean) =>
      request<{ id: string; client_visible: boolean }>(
        `/permissions/documents/${docId}/client-visible`,
        { method: "POST", body: JSON.stringify({ client_visible: clientVisible }) },
      ),
    requestAccess: (data: {
      resource_type: string;
      resource_id: string;
      requested_capabilities?: string[];
      reason?: string;
    }) =>
      request<{ id: string; status: string; resource_type: string; resource_id: string }>(
        "/permissions/access-requests",
        { method: "POST", body: JSON.stringify(data) },
      ),
    listAccessRequests: (status?: string) =>
      request<{ items: AccessRequest[] }>(
        `/permissions/access-requests${status ? `?status=${encodeURIComponent(status)}` : ""}`,
      ),
  },

  // ── Folder permissions (Phase B) ──────────────────────────────────────
  // Mirrors `docPermissions` for folders. Folder grants/shares cascade to
  // every document inside via authorize() walk-up.
  folderPermissions: {
    setProperties: (
      folderId: string,
      data: {
        visibility?: "private" | "workspace" | "entity" | "public";
        classification?: "public" | "internal" | "confidential" | "restricted";
        client_visible?: boolean;
        cascade?: boolean;
      },
    ) =>
      request<{
        id: string;
        visibility?: string;
        classification?: string;
        client_visible?: boolean;
        cascade_summary: { docs_updated: number; subfolders_updated: number };
      }>(`/folders/${folderId}/properties`, {
        method: "POST",
        body: JSON.stringify(data),
      }),

    listGrants: (folderId: string) =>
      request<DocumentGrant[]>(`/folders/${folderId}/grants`),
    createGrant: (
      folderId: string,
      data: {
        subject_type?: "user" | "staff_role" | "workspace_role" | "team";
        subject_id: string;
        capabilities: string[];
        expires_at?: string;
      },
    ) =>
      request<DocumentGrant>(`/folders/${folderId}/grants`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    revokeGrant: (folderId: string, grantId: string) =>
      request<void>(`/folders/${folderId}/grants/${grantId}`, { method: "DELETE" }),

    listShares: (folderId: string) =>
      request<DocumentShare[]>(`/folders/${folderId}/shares`),
    createShare: (
      folderId: string,
      data: {
        audience_type: "anonymous" | "email" | "domain";
        audience_value?: string;
        capabilities: ("view" | "comment" | "download")[];
        expires_in_days?: number | null;
        watermark?: boolean;
        require_otp?: boolean;
        allow_download?: boolean;
      },
    ) =>
      request<DocumentShare & { token: string; url: string }>(
        `/folders/${folderId}/shares`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    revokeShare: (folderId: string, shareId: string) =>
      request<void>(`/folders/${folderId}/shares/${shareId}`, { method: "DELETE" }),
  },

  portal: {
    login: (token: string) => {
      localStorage.setItem("manor_portal_token", token);
      return Promise.resolve({ ok: true });
    },
    submitTicket: (token: string, data: { title: string; description?: string }) =>
      fetch(`${API_BASE}/portal/tickets`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Portal-Token": token },
        body: JSON.stringify(data),
      }).then((r) => r.json()),
    listTickets: (token: string) =>
      fetch(`${API_BASE}/portal/tickets`, {
        headers: { "X-Portal-Token": token },
      }).then((r) => r.json()),
  },

  agents: {
    list: () => request<Agent[]>("/agents"),
    catalog: (knownVersion?: string) =>
      request<{ version: string; items: Agent[] | null }>(
        `/agents/catalog${knownVersion ? `?known_version=${encodeURIComponent(knownVersion)}` : ""}`,
      ),
    get: (id: string) => request<Agent>(`/agents/${id}`),
    create: (data: Partial<Agent>) =>
      request<Agent>("/agents", { method: "POST", body: JSON.stringify(data) }),
    generate: (prompt: string) =>
      request<Agent>("/agents/generate", { method: "POST", body: JSON.stringify({ prompt }) }),
    generateStream: (prompt: string, onStep?: (label: string) => void) =>
      streamSseResult<Agent>("/agents/generate-stream", { prompt }, onStep),
    generateDraftStream: (prompt: string, onStep?: (label: string) => void) =>
      streamSseResult<Agent>("/agents/generate-draft-stream", { prompt }, onStep),
    draftQuestions: (prompt: string) =>
      request<{ questions: string[]; ready: boolean }>("/agents/draft-questions", {
        method: "POST",
        body: JSON.stringify({ prompt }),
      }),
    aiUpdate: (id: string, prompt: string) =>
      request<Agent>(`/agents/${id}/ai-update`, { method: "POST", body: JSON.stringify({ prompt }) }),
    update: (id: string, data: Partial<Agent>) =>
      request<Agent>(`/agents/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      request<void>(`/agents/${id}`, { method: "DELETE" }),
    getTools: (agentId: string) =>
      request<any[]>(`/agents/${agentId}/tools`),
    bindTools: (agentId: string, toolIds: string[]) =>
      request<any>(`/agents/${agentId}/tools`, {
        method: "POST",
        body: JSON.stringify({ tool_ids: toolIds }),
      }),
    unbindTools: (agentId: string, toolIds: string[]) =>
      request<any>(`/agents/${agentId}/tools`, {
        method: "DELETE",
        body: JSON.stringify({ tool_ids: toolIds }),
      }),
    toolCatalog: (includeInactive = false) =>
      request<any[]>(`/agents/tools/catalog${includeInactive ? "?include_inactive=true" : ""}`),
    allToolsForCreate: () => request<any[]>("/agents/tools/all"),
    subscriptions: () => request<any[]>("/agents/subscriptions/mine"),
    subscribe: (agentId: string, customPrompt?: string) =>
      request<any>("/agents/subscriptions", {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, custom_prompt: customPrompt || "" }),
      }),
    unsubscribeById: (subscriptionId: string) =>
      request<void>(`/agents/subscriptions/${subscriptionId}`, { method: "DELETE" }),
    deployments: (agentId: string) =>
      request<AgentDeploymentResponse[]>(`/agents/${agentId}/deployments`),
    allDeployments: () =>
      request<Record<string, AgentDeploymentResponse[]>>("/agents/deployments"),
    runtimeEvidence: (agentId: string, params?: { limit?: number; evidence_type?: string; status?: string }) => {
      const q = new URLSearchParams();
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.evidence_type) q.set("evidence_type", params.evidence_type);
      if (params?.status) q.set("status", params.status);
      return request<RuntimeEvidence[]>(`/agents/${agentId}/runtime/evidence?${q}`);
    },
    learningCandidates: (agentId: string, params?: { limit?: number; status?: string | null; candidate_type?: string }) => {
      const q = new URLSearchParams();
      if (params?.limit) q.set("limit", String(params.limit));
      if (params && params.status !== undefined) q.set("status", params.status === null ? "" : params.status);
      if (params?.candidate_type) q.set("candidate_type", params.candidate_type);
      return request<AgentLearningCandidate[]>(`/agents/${agentId}/learning-candidates?${q}`);
    },
    subscriptionWorkers: (subscriptionId: string) =>
      request<SubscriptionWorkerBinding[]>(`/agents/subscriptions/${subscriptionId}/workers`),
    bindSubscriptionWorker: (
      subscriptionId: string,
      data: { worker_id: string; priority?: number; is_preferred?: boolean },
    ) =>
      request<SubscriptionWorkerBinding>(`/agents/subscriptions/${subscriptionId}/workers`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    unbindSubscriptionWorker: (subscriptionId: string, workerId: string) =>
      request<void>(`/agents/subscriptions/${subscriptionId}/workers/${workerId}`, {
        method: "DELETE",
      }),
    previewPrompt: (systemPrompt: string, testMessage: string) =>
      request<{ response: string }>("/agents/preview", {
        method: "POST",
        body: JSON.stringify({ system_prompt: systemPrompt, test_message: testMessage }),
      }),
  },

  agentInitiatives: {
    listForConversation: (conversationId: string) =>
      request<AgentInitiativeConversationListResponse>(
        `/agent-initiatives/conversation/${encodeURIComponent(conversationId)}`,
      ),
    list: (subscriptionId: string) =>
      request<AgentInitiativeListResponse>(
        `/agent-subscriptions/${subscriptionId}/initiatives`,
      ),
    create: (subscriptionId: string, data: AgentInitiativeCreateRequest) =>
      request<AgentInitiative>(`/agent-subscriptions/${subscriptionId}/initiatives`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    get: (initiativeId: string) =>
      request<AgentInitiative>(`/agent-initiatives/${initiativeId}`),
    update: (initiativeId: string, data: AgentInitiativeUpdateRequest) =>
      request<AgentInitiative>(`/agent-initiatives/${initiativeId}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      }),
    delete: (initiativeId: string, data: AgentInitiativeRevisionRequest) =>
      request<AgentInitiativeActionResponse | void>(`/agent-initiatives/${initiativeId}`, {
        method: "DELETE",
        body: JSON.stringify(data),
      }),
    mute: (initiativeId: string, data: AgentInitiativeRevisionRequest) =>
      request<AgentInitiativeActionResponse>(`/agent-initiatives/${initiativeId}/mute`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    unmute: (initiativeId: string, data: AgentInitiativeRevisionRequest) =>
      request<AgentInitiativeActionResponse>(`/agent-initiatives/${initiativeId}/unmute`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    pause: (initiativeId: string, data: AgentInitiativeRevisionRequest) =>
      request<AgentInitiativeActionResponse>(`/agent-initiatives/${initiativeId}/pause`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    resume: (initiativeId: string, data: AgentInitiativeRevisionRequest) =>
      request<AgentInitiativeActionResponse>(`/agent-initiatives/${initiativeId}/resume`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    run: (initiativeId: string, idempotencyKey: string, confirmCost = false) =>
      request<AgentInitiativeRunReceipt>(`/agent-initiatives/${initiativeId}/run`, {
        method: "POST",
        body: JSON.stringify({
          idempotency_key: idempotencyKey,
          confirm_cost: confirmCost,
        }),
      }),
    runs: (initiativeId: string, params?: { beforeId?: string | null; cursor?: string | null; limit?: number }) => {
      const q = new URLSearchParams();
      const beforeId = params?.beforeId || params?.cursor;
      if (beforeId) q.set("before_id", beforeId);
      if (params?.limit) q.set("limit", String(params.limit));
      const suffix = q.size ? `?${q}` : "";
      return request<AgentInitiativeRunListResponse>(
        `/agent-initiatives/${initiativeId}/runs${suffix}`,
      );
    },
    getRun: (runId: string) =>
      request<AgentInitiativeRun>(`/agent-initiative-runs/${runId}`),
  },

  executions: {
    list: (params?: { agent_id?: string; task_id?: string; limit?: number }) => {
      const q = new URLSearchParams();
      if (params?.agent_id) q.set("agent_id", params.agent_id);
      if (params?.task_id) q.set("task_id", params.task_id);
      if (params?.limit) q.set("limit", String(params.limit));
      return request<{ items: any[]; total: number }>(`/executions?${q}`);
    },
  },

  users: {
    list: () => request<User[]>("/auth/users"),
    directory: () => request<UserSummary[]>("/auth/users/directory"),
    invite: (email: string, role: string = "member") =>
      request<User>("/auth/users/invite", {
        method: "POST",
        body: JSON.stringify({ email, role }),
      }),
    updateRole: (userId: string, role: string) =>
      request<User>(`/auth/users/${userId}/role`, {
        method: "PUT",
        body: JSON.stringify({ role }),
      }),
    deactivate: (userId: string) =>
      request<void>(`/auth/users/${userId}`, { method: "DELETE" }),

    // ── Permission-v1 directory lookups (no admin role required) ──
    // Used by ShareDialog to resolve email <-> user_id and display
    // grant subjects with email/name. Returns minimal info only.
    lookupByEmail: (email: string) =>
      request<UserSummary>("/auth/users/lookup-by-email", {
        method: "POST",
        body: JSON.stringify({ email }),
      }),
    batchByIds: (ids: string[]) =>
      request<UserSummary[]>("/auth/users/batch", {
        method: "POST",
        body: JSON.stringify({ ids }),
      }),
  },

  crm: {
    clients: (query = "", signal?: AbortSignal) => request<CrmPage<ClientRecord>>(`/crm/clients?${query}`, { signal }),
    create: (data: CrmProfile) => request<ClientRecord>("/crm/clients", { method: "POST", body: JSON.stringify(data) }),
    profile: (id: string, data: CrmProfile) => request<ClientRecord>(`/clients/${id}/crm`, { method: "PUT", body: JSON.stringify(data) }),
    overview: (signal?: AbortSignal) => request<CrmOverview>("/crm/overview", { signal }),
    pipeline: (query = "", signal?: AbortSignal) => request<CrmPage<CrmOpportunity>>(`/crm/opportunities?${query}`, { signal }),
    queue: (query = "", signal?: AbortSignal) => request<CrmPage<CrmFollowup>>(`/crm/followups?${query}`, { signal }),
    activities: (id: string, offset = 0, signal?: AbortSignal) => request<CrmPage<CrmActivity>>(`/clients/${id}/activities?limit=25&offset=${offset}`, { signal }),
    opportunities: (id: string, offset = 0, signal?: AbortSignal) => request<CrmPage<CrmOpportunity>>(`/clients/${id}/opportunities?limit=25&offset=${offset}`, { signal }),
    followups: (id: string, offset = 0, signal?: AbortSignal) => request<CrmPage<CrmFollowup>>(`/clients/${id}/followups?limit=25&offset=${offset}`, { signal }),
    saveActivity: (client: string, data: CrmActivityInput, id?: string) => request<CrmActivity>(`/clients/${client}/activities${id ? `/${id}` : ""}`, { method: id ? "PUT" : "POST", body: JSON.stringify(data) }),
    saveOpportunity: (client: string, data: CrmOpportunityInput, id?: string) => request<CrmOpportunity>(`/clients/${client}/opportunities${id ? `/${id}` : ""}`, { method: id ? "PUT" : "POST", body: JSON.stringify(data) }),
    saveFollowup: (client: string, data: CrmFollowupInput, id?: string) => request<CrmFollowup>(`/clients/${client}/followups${id ? `/${id}` : ""}`, { method: id ? "PUT" : "POST", body: JSON.stringify(data) }),
    remove: (client: string, kind: "activities" | "opportunities" | "followups", id: string) => request<void>(`/clients/${client}/${kind}/${id}`, { method: "DELETE" }),
  },
  collaboration: {
    management: (workspaceId: string, signal?: AbortSignal) => request<CollaborationManagement>(`/workspaces/${workspaceId}/collaboration`, { signal }),
    invite: (workspaceId: string, data: CollaborationInvite) => request<CollaborationGrant>(`/workspaces/${workspaceId}/collaboration`, { method: "POST", body: JSON.stringify(data) }),
    change: (workspaceId: string, id: string, action: "authorize" | "revoke" | "update", data?: CollaborationInvite) => request<CollaborationGrant>(`/workspaces/${workspaceId}/collaboration/${id}/${action}`, { method: "POST", ...(data ? { body: JSON.stringify(data) } : {}) }),
    linkGroup: (workspaceId: string, groupId: string | null) => request<CollaborationCommunication>(`/workspaces/${workspaceId}/collaboration-group`, { method: "PUT", body: JSON.stringify({ group_id: groupId }) }),
    // `provider` is required on accept while the provider's trial grant is live.
    phase: (workspaceId: string, action: "request_acceptance" | "accept", note: string, provider?: "keep" | "remove") => request(`/workspaces/${workspaceId}/delivery-phase`, { method: "POST", body: JSON.stringify({ action, note, ...(provider ? { provider } : {}) }) }),
    portal: (id: string, signal?: AbortSignal) => request<CollaborationPortal>(`/workspace-collaborations/${id}`, { signal }),
    accept: (id: string) => request<CollaborationGrant>(`/workspace-collaborations/${id}/accept`, { method: "POST" }),
    /** A scoped session for the embedded frame of a granted Workspace; the caller stays in their own company. */
    session: (id: string) => request<CollaboratorSession>(`/workspace-collaborations/${id}/session`, { method: "POST", headers: { "X-Silent-Error": "1" } }),
  },
  clientTrials: {
    /** Deliveries of this source Workspace the provider team can open now. */
    list: (workspaceId: string, signal?: AbortSignal) => request<ClientTrial[]>(`/workspaces/${workspaceId}/client-trials`, { signal }),
    /** A scoped session for the embedded customer-trial frame; the caller stays in their own company. */
    session: (workspaceId: string, deliveryId: string) => request<CollaboratorSession>(`/workspaces/${workspaceId}/client-trials/${deliveryId}/session`, { method: "POST", headers: { "X-Silent-Error": "1" } }),
  },
  clients: {
    list: () => request<{ items: ClientRecord[]; total: number }>("/clients"),
    get: (id: string, signal?: AbortSignal) => request<ClientRecord>(`/clients/${id}`, { signal }),
    create: (data: { name: string; email?: string }) => request<ClientRecord>("/clients", { method: "POST", body: JSON.stringify(data) }),
    contacts: (id: string, signal?: AbortSignal) => request<ClientContactRecord[]>(`/clients/${id}/contacts`, { signal }),
    addContact: (id: string, data: ClientContactInput) => request<ClientContactRecord>(`/clients/${id}/contacts`, { method: "POST", body: JSON.stringify(data) }),
    editContact: (client: string, id: string, data: ClientContactInput) => request<ClientContactRecord>(`/clients/${client}/contacts/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    removeContact: (client: string, id: string) => request<void>(`/clients/${client}/contacts/${id}`, { method: "DELETE" }),
    deliveries: (id: string, signal?: AbortSignal) => request<ClientDeliveryReceipt[]>(`/clients/${id}/deliveries`, { signal }),
    prepareDelivery: (id: string, data: { contact_id: string; workspace_id: string; phase?: "trial" | "delivered"; knowledge_document_ids?: string[] }) => request<ClientDeliveryReceipt>(`/clients/${id}/deliveries`, { method: "POST", body: JSON.stringify(data) }),
    requestMaintenance: (clientId: string, deliveryId: string) => request<CollaborationGrant>(`/clients/${clientId}/deliveries/${deliveryId}/maintenance/request`, { method: "POST" }),
    publishDeliveryUpdate: (clientId: string, deliveryId: string, data: { release_notes?: string }) => request<ClientDeliveryReceipt & { published: boolean }>(`/clients/${clientId}/deliveries/${deliveryId}/publish-update`, { method: "POST", body: JSON.stringify(data) }),
    changeInvite: (clientId: string, deliveryId: string, action: "rotate" | "revoke") => request<ClientDeliveryReceipt>(`/clients/${clientId}/deliveries/${deliveryId}/${action}`, { method: "POST" }),
    invitation: (token: string) => request<ClientDeliveryReceipt>(`/client-deliveries/invites/${encodeURIComponent(token)}`),
    acceptDelivery: (token: string) => request<ClientDeliveryReceipt>(`/client-deliveries/invites/${encodeURIComponent(token)}/accept`, { method: "POST", body: JSON.stringify({}) }),
  },

  people: {
    me: () => request<PeopleContext>("/people/me"),
    directory: (signal?: AbortSignal) => request<PeopleDirectoryEntry[]>("/people/directory", { signal }),
    acceptInvite: (inviteId: string) =>
      request<PeopleContextActionResponse>(`/people/invites/${inviteId}/accept`, {
        method: "POST",
      }),
    declineInvite: (inviteId: string) =>
      request<PeopleContextActionResponse>(`/people/invites/${inviteId}/decline`, {
        method: "POST",
      }),
    switchMembership: (entityId: string) =>
      request<PeopleContextActionResponse>(`/people/memberships/${entityId}/switch`, {
        method: "POST",
      }),
    leaveMembership: (entityId: string) =>
      request<PeopleContextActionResponse>(`/people/memberships/${entityId}/leave`, {
        method: "POST",
      }),
  },

  workspaces: {
    list: () => request<Workspace[]>("/workspaces"),
    get: (id: string, signal?: AbortSignal) => request<Workspace>(`/workspaces/${id}`, { signal }),
    appView: (id: string, options?: { includeCatalog?: boolean; signal?: AbortSignal }) => request<WorkspaceAppView>(
      `/workspaces/${id}/app-view${options?.includeCatalog === false ? "?include_catalog=false" : ""}`,
      { signal: options?.signal },
    ),
    appViewContexts: (id: string, componentKey: string, options?: { cursor?: string; limit?: number; signal?: AbortSignal }) => {
      const params = new URLSearchParams();
      if (options?.cursor) params.set("cursor", options.cursor);
      if (options?.limit) params.set("limit", String(options.limit));
      const suffix = params.size ? `?${params}` : "";
      return request<WorkspaceAppDataContextsResponse>(
        `/workspaces/${id}/app-view/components/${encodeURIComponent(componentKey)}/contexts${suffix}`,
        { signal: options?.signal },
      );
    },
    appViewComponentData: (id: string, componentKey: string, options: {
      contextKind: WorkspaceAppDataContextKind;
      contextId: string;
      block?: string;
      window?: string;
      cursor?: string;
      limit?: number;
      signal?: AbortSignal;
    }) => {
      const params = new URLSearchParams({ context_kind: options.contextKind, context_id: options.contextId });
      if (options.block) params.set("block", options.block);
      if (options.window) params.set("window", options.window);
      if (options.cursor) params.set("cursor", options.cursor);
      if (options.limit) params.set("limit", String(options.limit));
      return request<WorkspaceAppDataResponse>(
        `/workspaces/${id}/app-view/components/${encodeURIComponent(componentKey)}/data?${params}`,
        { signal: options.signal },
      );
    },
    appViewSection: (id: string, sectionId: string, signal?: AbortSignal) => request<WorkspaceAppSectionData>(
      `/workspaces/${id}/app-view/sections/${encodeURIComponent(sectionId)}`,
      { signal },
    ),
    saveAppView: async (id: string, config: WorkspaceAppViewConfig, previous: WorkspaceAppViewConfig) => {
      const path = `/workspaces/${id}/operation/drafts`;
      const entries = Object.fromEntries(
        Array.from(new Set([...Object.keys(previous.entries || {}), ...Object.keys(config.entries || {})])).map((key) => [key, config.entries?.[key] ?? null]),
      );
      const prompts = Object.fromEntries(
        Array.from(new Set([...Object.keys(previous.prompts || {}), ...Object.keys(config.prompts || {})])).map((key) => [key, config.prompts?.[key] ?? null]),
      );
      const labels = Object.fromEntries(
        Array.from(new Set([...Object.keys(previous.labels), ...Object.keys(config.labels)])).map((key) => [key, config.labels[key] ?? null]),
      );
      const descriptions = Object.fromEntries(
        Array.from(new Set([...Object.keys(previous.descriptions), ...Object.keys(config.descriptions)])).map((key) => [key, config.descriptions[key] ?? null]),
      );
      const forms = Object.fromEntries(
        Array.from(new Set([...Object.keys(previous.forms || {}), ...Object.keys(config.forms || {})])).map((key) => [key, config.forms?.[key] ?? null]),
      );
      const presets = Object.fromEntries(
        Array.from(new Set([...Object.keys(previous.presets || {}), ...Object.keys(config.presets || {})])).map((key) => [key, config.presets?.[key] ?? null]),
      );
      const targets = Object.fromEntries(
        Array.from(new Set([...Object.keys(previous.targets || {}), ...Object.keys(config.targets || {})])).map((key) => [key,
          config.targets?.[key]?.startsWith("attach:") ? null : config.targets?.[key] ?? null]),
      );
      const attachments: Array<{ op: string; payload: { workflow_id: string; service_key?: string; action_key?: string; primary?: boolean } }> = Object.entries(config.targets).filter(([, target]) => target.startsWith("attach:")).map(([key, target]) => ({
        op: "workflow_binding.attach", payload: { workflow_id: target.slice(7), ...(key.startsWith("service:") ? { service_key: key.slice(8) } : { action_key: key }) },
      }));
      const attachPrimary = config.hero?.startsWith("attach:");
      if (attachPrimary) attachments.push({
        op: "workflow_binding.attach", payload: { workflow_id: config.hero!.slice(7), primary: true },
      });
      const draft = await request<{ id: string; validation: { valid: boolean; errors?: Array<{ message?: string }> } }>(path, {
        method: "POST",
        body: JSON.stringify({ patches: [{ op: "app_view.update", payload: {
          eyebrow: config.eyebrow?.trim() || null,
          title: config.title?.trim() || null, description: config.description?.trim() ?? null,
          hero: attachPrimary ? null : config.hero, order: config.order, hidden: config.hidden,
          labels, descriptions, forms, targets, entries, prompts, presets, visual: config.visual, layout: config.layout,
          data_sources: config.data_sources || {}, components: config.components || [],
          ...(config.motion_cover_url !== previous.motion_cover_url || config.motion_cover_source_url !== previous.motion_cover_source_url
            ? { motion_cover_url: config.motion_cover_url, motion_cover_source_url: config.motion_cover_source_url }
            : {}),
          ...(JSON.stringify(config.sections ?? null) !== JSON.stringify(previous.sections ?? null)
            ? { sections: config.sections ?? null }
            : {}),
          ...(JSON.stringify(config.theme ?? null) !== JSON.stringify(previous.theme ?? null)
            ? { theme: config.theme ?? null }
            : {}),
        } }, ...attachments] }),
      });
      if (!draft.validation.valid) {
        await request(`${path}/${draft.id}/discard`, { method: "POST" }).catch(() => undefined);
        throw new Error(draft.validation.errors?.map((error) => error.message).filter(Boolean).join("; ") || "Invalid page configuration");
      }
      try {
        return await request(`${path}/${draft.id}/apply`, {
          method: "POST",
          body: JSON.stringify({ user_confirmation: true }),
        });
      } catch (error) {
        await request(`${path}/${draft.id}/discard`, { method: "POST" }).catch(() => undefined);
        throw error;
      }
    },
    setupStatus: (id: string, signal?: AbortSignal) =>
      request<WorkspaceSetupStatus>(`/workspaces/${id}/setup-status`, { signal }),
    submitSetupInput: (id: string, checkKey: string, text: string) =>
      request<{ accepted: boolean }>(`/workspaces/${id}/setup-input`, {
        method: "POST",
        body: JSON.stringify({ check_key: checkKey, text }),
      }),
    reconcileBlueprintStartup: (id: string) =>
      request<BlueprintStartupReconcileResponse>(
        `/workspaces/${id}/blueprint-startup/reconcile`,
        { method: "POST" },
      ),
    blueprintUpgradePlan: (
      id: string,
      variableValues?: Record<string, unknown>,
      channelConfigIds?: Record<string, string>,
      targetVersion?: string,
    ) =>
      request<{
        upgrade_protocol_version: number;
        workspace_id: string;
        workspace_name: string;
        blueprint_slug: string | null;
        blueprint_fingerprint: string | null;
        plan_fingerprint: string | null;
        baseline_known: boolean;
        synchronization_status?: "current" | "customized";
        can_revert: boolean;
        restore_point?: { applied_at: string; blueprint_version?: string | null } | null;
        /** The release this Workspace runs, the one the preview moves it to, and the latest. */
        installed_version?: string | null;
        current_version?: string | null;
        latest_version?: string | null;
        target_version?: string | null;
        downgrade?: boolean;
        /** Listed releases it can move to, newest first. */
        available_versions?: Array<{
          version: string;
          published_at: string | null;
          changelog: string | null;
          min_bump: "major" | "minor" | "patch" | null;
          deprecated: string | null;
          prerelease: boolean;
        }>;
        /** The semver step from the installed release to the target, and why. */
        version_change?: {
          from: string;
          to: string;
          min_bump: "major" | "minor" | "patch";
          reasons: string[];
        } | null;
        /** Releases newer than the installed one, oldest first. */
        changelog?: Array<{ version: string; changelog: string | null; published_at: string | null }>;
        active_runs: Array<{ id: string; name: string; kind: string; status: string }>;
        setup_preflight: BlueprintInstallPreflight;
        kept_setup_preflight?: BlueprintInstallPreflight;
        setup_preview: BlueprintSetupPreview;
        variables_ready: boolean;
        resolved_variable_keys: string[];
        resolved_variable_values: Record<string, unknown>;
        items: Array<{
          key: string;
          kind: string;
          slug: string;
          name: string;
          id?: string;
          revision?: number;
          // customized: your edit to an item the Blueprint did not change; kept unless you pick use_blueprint.
          action: "update" | "keep_yours" | "customized" | "unchanged" | "missing" | "remove";
          changes: string[];
          current_content: unknown;
          new_content: unknown;
          baseline_content?: unknown;
          baseline_known: boolean;
          blocked_reason?: string;
          compatibility_issues?: Array<{
            code: "missing_tools" | "missing_skills" | "missing_inputs";
            names: string[];
            required_by: Array<{ key: string; kind: string; name: string }>;
            contract_required: string[];
          }>;
        }>;
      }>(
        variableValues === undefined && channelConfigIds === undefined && !targetVersion
          ? `/workspaces/${id}/blueprint/upgrade/v3`
          : `/workspaces/${id}/blueprint/upgrade/v3/preview`,
        variableValues === undefined && channelConfigIds === undefined && !targetVersion
          ? undefined
          : {
              method: "POST",
              body: JSON.stringify({
                variable_values: variableValues ?? {},
                channel_config_ids: channelConfigIds ?? {},
                ...(targetVersion ? { target_version: targetVersion } : {}),
              }),
            },
      ),
    applyBlueprintUpgrade: (id: string, data: {
      /** The release the reviewed preview targeted; omitted = latest. */
      target_version?: string;
      expected_blueprint_fingerprint: string;
      expected_plan_fingerprint: string;
      variable_values?: Record<string, unknown>;
      channel_config_ids?: Record<string, string>;
      configuration_resolutions: Array<{
        key: string;
        resolution: "keep_yours" | "use_blueprint";
      }>;
    }) =>
      request<{
        upgrade_protocol_version: number;
        workspace_id: string;
        updated: Array<{ kind: string; name: string; changes: string[] }>;
        kept_yours: Array<{ kind: string; name: string }>;
        can_revert: boolean;
        fully_synchronized: boolean;
        synchronization_status: "current" | "customized";
      }>(`/workspaces/${id}/blueprint/upgrade/v3`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    revertBlueprintUpgrade: (id: string) =>
      request<{ workspace_id: string; reverted: Array<{ kind: string; name: string }> }>(
        `/workspaces/${id}/blueprint/revert`, { method: "POST" },
      ),
    create: (data: Partial<Workspace>) => request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify(data) }),
    sandbox: (data?: { kind?: string; name?: string; seed_task_title?: string }) =>
      request<{
        workspace_id: string;
        agent_id: string;
        subscription_id: string;
        goal_id: string;
        task_id: string;
        chat_url: string;
      }>("/workspaces/sandbox", { method: "POST", body: JSON.stringify(data || {}) }),
    update: (id: string, data: Partial<Workspace>) => request<Workspace>(`/workspaces/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) => request<void>(`/workspaces/${id}`, { method: "DELETE" }),
    restore: (id: string) => request<Workspace>(`/workspaces/${id}/restore`, { method: "POST" }),
    trash: () => request<Workspace[]>("/workspaces/trash/list"),
    graceDays: () => request<{ grace_days: number }>("/workspaces/trash/grace-days"),
    dashboard: (id: string) => request<WorkspaceStats>(`/workspaces/${id}/dashboard`),
    ledgerOverview: (id: string) =>
      request<WorkspaceLedgerOverview>(`/workspaces/${id}/ledgers/overview`),
    configureLedgers: (id: string, ledgerContracts: Array<string | Record<string, unknown>>) =>
      request<{ workspace_id: string; ledger_contracts: Record<string, unknown>[]; source: string }>(
        `/workspaces/${id}/ledgers/configuration`,
        { method: "PUT", body: JSON.stringify({ ledger_contracts: ledgerContracts }) },
      ),
    stats: {
      library: () => request<{ items: WorkspaceStatLibraryEntry[] }>("/workspaces/stats/library"),
      list: (wsId: string) => request<{ items: WorkspaceStatDefinition[] }>(`/workspaces/${wsId}/stats`),
      quickView: (wsId: string) =>
        request<WorkspaceStatsQuickView>(`/workspaces/${wsId}/stats/quick-view`),
      updateQuickView: (wsId: string, data: Omit<WorkspaceStatsQuickView, "configured">) =>
        request<WorkspaceStatsQuickView>(`/workspaces/${wsId}/stats/quick-view`, {
          method: "PUT",
          body: JSON.stringify(data),
        }),
      create: (wsId: string, data: WorkspaceStatCreate) =>
        request<WorkspaceStatDefinition>(`/workspaces/${wsId}/stats`, {
          method: "POST",
          body: JSON.stringify(data),
        }),
      update: (wsId: string, statId: string, data: Partial<WorkspaceStatCreate> & { status?: string }) =>
        request<WorkspaceStatDefinition>(`/workspaces/${wsId}/stats/${statId}`, {
          method: "PATCH",
          body: JSON.stringify(data),
        }),
      delete: (wsId: string, statId: string) =>
        request<void>(`/workspaces/${wsId}/stats/${statId}`, { method: "DELETE" }),
      collect: (wsId: string, statId: string) =>
        request<{ stat: WorkspaceStatDefinition; observation: WorkspaceStatObservation }>(
          `/workspaces/${wsId}/stats/${statId}/collect`,
          { method: "POST" },
        ),
      addAssessedMetrics: (wsId: string, data: { templates: string[]; cadence?: "daily" | "weekly"; service_key?: string }) =>
        request<{ items: WorkspaceStatDefinition[]; job_id: string; schedule_enabled: boolean; service_key: string }>(
          `/workspaces/${wsId}/stats/assessed-metrics`,
          { method: "POST", body: JSON.stringify(data) },
        ),
      observations: (wsId: string, statId: string, limit = 50) =>
        request<{ items: WorkspaceStatObservation[] }>(
          `/workspaces/${wsId}/stats/${statId}/observations?limit=${limit}`,
        ),
      record: (wsId: string, statId: string, data: { value: number; note?: string; observed_at?: string }) =>
        request<{ stat: WorkspaceStatDefinition; observation: WorkspaceStatObservation }>(
          `/workspaces/${wsId}/stats/${statId}/observations`,
          { method: "POST", body: JSON.stringify(data) },
        ),
    },
    operatingModel: (id: string) => request<any>(`/workspaces/${id}/operating-model`),
    updateOperatingModel: (id: string, model: Record<string, any>) => request<any>(`/workspaces/${id}/operating-model`, { method: "PUT", body: JSON.stringify(model) }),
    evaluation: (id: string, days = 30) => request<WorkspaceEvaluationSnapshot>(`/workspaces/${id}/evaluation?days=${days}`),
    services: {
      add: (wsId: string, data: { key: string; name: string; description?: string; config?: Record<string, any> }) =>
        request<any>(`/workspaces/${wsId}/services`, { method: "POST", body: JSON.stringify(data) }),
      remove: (wsId: string, serviceKey: string) =>
        request<any>(`/workspaces/${wsId}/services/${serviceKey}`, { method: "DELETE" }),
    },
    agents: {
      list: (wsId: string) =>
        request<WorkspaceAgentMapping[]>(`/workspaces/${wsId}/agents`),
      assignable: (wsId: string) =>
        request<{
          agent_ids: string[];
          subscriptions: Array<{
            id: string;
            agent_id: string;
            role_label?: string | null;
          }>;
        }>(`/workspaces/${wsId}/agents/assignable`),
      map: (wsId: string, data: { service_key: string; agent_id: string; custom_prompt?: string; new_service?: { key: string; name: string } }) =>
        request<any>(`/workspaces/${wsId}/agents`, { method: "POST", body: JSON.stringify(data) }),
      unmap: (wsId: string, serviceKey: string) =>
        request<any>(`/workspaces/${wsId}/agents/${serviceKey}`, { method: "DELETE" }),
    },
    governance: {
      get: (wsId: string) => request<GovernancePolicyResponse>(`/workspaces/${wsId}/governance`),
      update: (wsId: string, policy: GovernancePolicy, change_summary?: string) =>
        request<GovernancePolicyResponse>(`/workspaces/${wsId}/governance`, {
          method: "PUT",
          body: JSON.stringify({ policy, change_summary }),
        }),
      revisions: (wsId: string, limit = 20) =>
        request<any[]>(`/workspaces/${wsId}/governance/revisions?limit=${limit}`),

    },
    observability: {
      timeline: (wsId: string, selector: { root_execution_id?: string; review_id?: string; correlation_id?: string }, limit = 100) => {
        const q = new URLSearchParams();
        if (selector.root_execution_id) q.set("root_execution_id", selector.root_execution_id);
        if (selector.review_id) q.set("review_id", selector.review_id);
        if (selector.correlation_id) q.set("correlation_id", selector.correlation_id);
        q.set("limit", String(limit));
        return request<WorkspaceTimelineResponse>(`/workspaces/${wsId}/timeline?${q}`);
      },
      strategyReviews: (wsId: string, limit = 10) =>
        request<{ reviews: StrategyReviewListEntry[] }>(`/workspaces/${wsId}/strategy/reviews?limit=${limit}`),
      strategyReviewDetail: (wsId: string, reviewId: string) =>
        request<StrategyReviewDetail>(`/workspaces/${wsId}/strategy/reviews/${reviewId}`),
      automationHealth: (wsId: string) =>
        request<{ automations: AutomationHealthEntry[] }>(`/workspaces/${wsId}/automation-health`),
      taskProvenance: (wsId: string, taskId: string) =>
        request<TaskProvenanceResponse>(`/workspaces/${wsId}/tasks/${taskId}/provenance`),
    },
    humanParticipation: (wsId: string) =>
      request<HumanParticipationResponse>(`/workspaces/${wsId}/human-participation`),
    goals: (wsId: string, goals: any[]) => request<any>(`/workspaces/${wsId}/goals`, { method: "PUT", body: JSON.stringify({ goals }) }),
    rules: (wsId: string, rules: any[]) => request<any>(`/workspaces/${wsId}/rules`, { method: "PUT", body: JSON.stringify({ rules }) }),
    activity: (wsId: string, params?: { limit?: number; event_type?: string }) => {
      const q = new URLSearchParams();
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.event_type) q.set("event_type", params.event_type);
      return request<WorkspaceActivity[]>(`/workspaces/${wsId}/activity?${q}`);
    },
    runtimeEvidence: (wsId: string, params?: { limit?: number; evidence_type?: string; status?: string }) => {
      const q = new URLSearchParams();
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.evidence_type) q.set("evidence_type", params.evidence_type);
      if (params?.status) q.set("status", params.status);
      return request<RuntimeEvidence[]>(`/workspaces/${wsId}/runtime/evidence?${q}`);
    },
    learningCandidates: (wsId: string, params?: { limit?: number; status?: string | null; candidate_type?: string }) => {
      const q = new URLSearchParams();
      if (params?.limit) q.set("limit", String(params.limit));
      if (params && params.status !== undefined) q.set("status", params.status === null ? "" : params.status);
      if (params?.candidate_type) q.set("candidate_type", params.candidate_type);
      return request<AgentLearningCandidate[]>(`/workspaces/${wsId}/learning-candidates?${q}`);
    },
    resolveLearningCandidate: (wsId: string, candidateId: string, data: { status: "proposed" | "accepted" | "rejected" | "archived"; note?: string }) =>
      request<AgentLearningCandidate>(`/workspaces/${wsId}/learning-candidates/${candidateId}/resolve`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    applyLearningCandidate: (wsId: string, candidateId: string) =>
      request<AgentLearningCandidate>(`/workspaces/${wsId}/learning-candidates/${candidateId}/apply`, {
        method: "POST",
      }),
    staff: {
      list: (wsId: string) => request<WorkspaceStaff[]>(`/workspaces/${wsId}/staff`),
      // Permission-v1 extensions: expires_at (ISO date), optional direct
      // user_id link. Backend P3 will honor them; legacy backend ignores
      // unknown fields, so safe to send today.
      assign: (wsId: string, data: { staff_id: string; role?: string; expires_at?: string; user_id?: string }) =>
        request<WorkspaceStaff>(`/workspaces/${wsId}/staff`, { method: "POST", body: JSON.stringify(data) }),
      // `id` is the Staff row, or the seat (or user) id of a seat without one.
      remove: (wsId: string, id: string) =>
        request<void>(`/workspaces/${wsId}/staff/${encodeURIComponent(id)}`, { method: "DELETE" }),
    },
    channels: (wsId: string) => request<any[]>(`/workspaces/${wsId}/channels`),
    residentPmsAccounts: (wsId: string) => request<{ accounts: { id: string; display_name: string }[] }>(`/workspaces/${wsId}/resident-pms/accounts`),
    webchatResources: (wsId: string, documentCursor = 0) => request<import("./webchatPage").WebchatWorkspaceResources>(`/workspaces/${wsId}/webchat/resources?document_cursor=${documentCursor}`),
    reviewWebchatPage: (wsId: string, page: import("./webchatPage").WebchatPage) =>
      request<import("./webchatPage").WebchatPage>(`/workspaces/${wsId}/webchat/review`, {
        method: "POST",
        body: JSON.stringify(page),
      }),
    reviewSavedWebchatPage: (wsId: string, channelBindingId: string) =>
      request<import("./webchatPage").WebchatPage>(`/workspaces/${wsId}/channels/${channelBindingId}/webchat/review`),
    availableChannels: (wsId: string) => request<any[]>(`/workspaces/${wsId}/channels/available`),
    capabilities: (wsId: string) => request<any>(`/workspaces/${wsId}/capabilities`),
    attachChannel: (wsId: string, data: {
      channel_config_id?: string | null;
      channel_type?: string;
      name?: string;
      purpose?: string;
      role?: string;
      linked_service_key?: string;
      agent_subscription_id?: string;
      agent_id?: string;
      config?: Record<string, any>;
    }) => request<any>(`/workspaces/${wsId}/channels`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
    updateChannel: (wsId: string, channelBindingId: string, data: {
      channel_config_id?: string;
      name?: string;
      purpose?: string;
      role?: string;
      linked_service_key?: string;
      agent_subscription_id?: string;
      agent_id?: string;
      config?: Record<string, any>;
    }) => request<any>(`/workspaces/${wsId}/channels/${channelBindingId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
    removeChannel: (wsId: string, channelBindingId: string) =>
      request<void>(`/workspaces/${wsId}/channels/${channelBindingId}`, { method: "DELETE" }),
    connectionStatus: (wsId: string, signal?: AbortSignal) =>
      request<WorkspaceConnectionStatus>(
        `/workspaces/${wsId}/connection-status`,
        { signal },
      ),
    resolveIntegrations: (wsId: string) => request<{ resolved: string[]; remaining: string[] }>(`/workspaces/${wsId}/resolve-integrations`, { method: "POST" }),
    documents: (wsId: string) => request<any[]>(`/workspaces/${wsId}/documents`),
    knowledge: {
      createGroup: (wsId: string, data: { name: string; purpose?: string; kind?: string }) =>
        request<any>(`/workspaces/${wsId}/documents/groups`, {
          method: "POST",
          body: JSON.stringify(data),
        }),
      updateGroup: (wsId: string, groupId: string, data: { name?: string; purpose?: string; kind?: string }) =>
        request<any>(`/workspaces/${wsId}/documents/groups/${groupId}`, {
          method: "PUT",
          body: JSON.stringify(data),
        }),
      deleteGroup: (wsId: string, groupId: string) =>
        request<void>(`/workspaces/${wsId}/documents/groups/${groupId}`, { method: "DELETE" }),
      addDocuments: (wsId: string, groupId: string, documentIds: string[]) =>
        request<{ added: number; skipped: string[]; total: number }>(`/workspaces/${wsId}/documents/groups/${groupId}/members`, {
          method: "POST",
          body: JSON.stringify({ document_ids: documentIds }),
        }),
      addFolder: (wsId: string, folderId: string) =>
        request<{ group_id: string; group_name: string; created: boolean; added: number; existing: number; total: number }>(
          `/workspaces/${wsId}/documents/folders/${folderId}`,
          { method: "POST" },
        ),
      removeDocument: (wsId: string, groupId: string, documentId: string) =>
        request<void>(`/workspaces/${wsId}/documents/groups/${groupId}/members/${documentId}`, { method: "DELETE" }),
    },
    pause: (wsId: string, transitionId?: string) => request<any>(
      `/workspaces/${wsId}/pause${transitionId ? `?transition_id=${encodeURIComponent(transitionId)}` : ""}`,
      { method: "POST" },
    ),
    resume: (
      wsId: string,
      transitionId?: string,
      options?: {
        goal?: { title: string; target_value: number };
      },
    ) => request<any>(
      `/workspaces/${wsId}/resume${transitionId ? `?transition_id=${encodeURIComponent(transitionId)}` : ""}`,
      {
        method: "POST",
        body: options ? JSON.stringify(options) : undefined,
      },
    ),
    heartbeat: {
      enable: (wsId: string) => request<any>(`/workspaces/${wsId}/heartbeat/enable`, { method: "POST" }),
      disable: (wsId: string) => request<any>(`/workspaces/${wsId}/heartbeat/disable`, { method: "POST" }),
      status: (wsId: string) => request<any>(`/workspaces/${wsId}/heartbeat/status`),
    },
    budget: {
      get: (wsId: string) => request<WorkspaceBudgetStatus>(`/workspaces/${wsId}/budget`),
      update: (wsId: string, data: WorkspaceBudgetUpdate) =>
        request<WorkspaceBudgetStatus>(`/workspaces/${wsId}/budget`, {
          method: "PUT",
          body: JSON.stringify(data),
        }),
    },
    creditUsageSummary: (wsId: string) =>
      request<WorkspaceCreditUsageSummary>(`/workspaces/${wsId}/credit-usage-summary`),
    setup: {
      turn: (wsId: string, data: { session_id?: string; message: string }) =>
        request<any>(`/workspaces/${wsId}/setup/turn`, { method: "POST", body: JSON.stringify(data) }),
      finalize: (wsId: string, sessionId: string) =>
        request<any>(`/workspaces/${wsId}/setup/finalize`, { method: "POST", body: JSON.stringify({ session_id: sessionId }) }),
    },
    // ── Workspace Blueprint / Marketplace (M12) ──────────────────────
    blueprintExportKnowledgeDocuments: (wsId: string, limit = 50, offset = 0) =>
      request<BlueprintExportKnowledgeDocument[]>(
        `/workspaces/${wsId}/blueprint-export/knowledge-documents?limit=${limit}&offset=${offset}`,
      ),
    exportBlueprint: (wsId: string, data: ExportBlueprintRequest) =>
      request<BlueprintDetail>(
        `/workspaces/${wsId}/export-blueprint`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    exportBlueprintPayload: (wsId: string, data: ExportBlueprintPayloadRequest = {}) =>
      request<Record<string, unknown>>(
        `/workspaces/${wsId}/export-blueprint-payload`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    /** The Workspace as a .manor package: its payload plus the files its App
     *  and Skills point at, so another account sees the same App. */
    exportBlueprintPackage: async (wsId: string, data: ExportBlueprintPayloadRequest = {}) => {
      const token = await getReadyAuthToken();
      const res = await fetch(`${API_BASE}/workspaces/${wsId}/export-blueprint-package`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify(data),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new ApiError(res.status, typeof body?.detail === "string" ? body.detail : `Export failed (${res.status})`);
      }
      const match = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
      return { blob: await res.blob(), filename: match?.[1] || "blueprint.manor" };
    },
    promote: (wsId: string, force = false) =>
      request<PromoteResponse>(
        `/workspaces/${wsId}/promote`,
        { method: "POST", body: JSON.stringify({ force }) },
      ),
    promotePreflight: (wsId: string) =>
      request<UnmetRequirement[]>(`/workspaces/${wsId}/promote/preflight`),
    simulationReport: (wsId: string, recentDays?: number) =>
      request<SimulationReport>(
        `/workspaces/${wsId}/simulation-report${recentDays ? `?recent_days=${recentDays}` : ""}`,
      ),
    // ── Workspace Chat ──────────────────────────────────────────────
    chat: {
      listEntrypoints: (wsId: string) =>
        request<WorkspaceChatEntrypoint[]>(`/workspaces/${wsId}/chat/entrypoints`),
      streamEntrypoint: async (
        wsId: string,
        bindingId: string,
        message: string,
        conversationId?: string,
        opts?: {
          clientMessageId?: string;
          files?: File[];
          documentIds?: string[];
          localWorkerId?: string;
          threadRef?: { kind: string; id: string };
        },
      ): Promise<Response> => {
        const form = new FormData();
        form.append("message", message);
        if (opts?.clientMessageId) form.append("client_message_id", opts.clientMessageId);
        if (conversationId) form.append("conversation_id", conversationId);
        if (opts?.localWorkerId) form.append("local_worker_id", opts.localWorkerId);
        if (opts?.threadRef?.kind) form.append("thread_ref_kind", opts.threadRef.kind);
        if (opts?.threadRef?.id) form.append("thread_ref_id", opts.threadRef.id);
        if (opts?.documentIds?.length) form.append("document_ids", opts.documentIds.join(","));
        for (const file of opts?.files || []) form.append("files", await prepareChatUploadFile(file));
        return requestStreamResponse(
          `/workspaces/${wsId}/chat/entrypoints/${encodeURIComponent(bindingId)}/stream`,
          form,
        );
      },
      listMessages: (wsId: string, opts?: { thread_ref_kind?: string; thread_ref_id?: string; limit?: number }) => {
        const q = new URLSearchParams();
        if (opts?.thread_ref_kind) q.set("thread_ref_kind", opts.thread_ref_kind);
        if (opts?.thread_ref_id) q.set("thread_ref_id", opts.thread_ref_id);
        if (opts?.limit) q.set("limit", String(opts.limit));
        return request<any[]>(`/workspaces/${wsId}/chat/messages?${q}`);
      },
      listMessagesPage: (
        wsId: string,
        opts?: {
          thread_ref_kind?: string;
          thread_ref_id?: string;
          limit?: number;
          before?: string | null;
        },
        signal?: AbortSignal,
      ) => {
        const q = new URLSearchParams();
        if (opts?.thread_ref_kind) q.set("thread_ref_kind", opts.thread_ref_kind);
        if (opts?.thread_ref_id) q.set("thread_ref_id", opts.thread_ref_id);
        q.set("limit", String(opts?.limit ?? 75));
        if (opts?.before) q.set("before", opts.before);
        return request<PaginatedMessagesResponse<any>>(
          `/workspaces/${wsId}/chat/messages/page?${q}`,
          { signal },
        );
      },
      getMessage: (wsId: string, messageId: string, signal?: AbortSignal) =>
        request<any>(`/workspaces/${wsId}/chat/messages/${messageId}`, { signal }),
      postMessage: (
        wsId: string,
        body: string,
        threadRef?: { kind: string; id: string },
        audience: "ai" | "team" = "ai",
      ) =>
        request<any>(`/workspaces/${wsId}/chat/messages`, {
          method: "POST",
          body: JSON.stringify({
            body,
            audience,
            thread_ref_kind: threadRef?.kind,
            thread_ref_id: threadRef?.id,
          }),
        }),
      listPendingAttention: (wsId: string, signal?: AbortSignal) =>
        request<{ items: WorkspaceAttentionReminder[] }>(`/workspaces/${wsId}/chat/attention?scope=open`, { signal }),
      markAttentionSeen: (wsId: string, keys: string[]) =>
        request<{ unseen_actions: number; unseen_action_keys: Record<string, Record<string, string>> }>(
          `/workspaces/${wsId}/chat/attention/seen`,
          { method: "POST", body: JSON.stringify({ keys }) },
        ),
      resolveAction: (wsId: string, msgId: string, choice: string, note?: string, payload?: Record<string, any>) =>
        request<any>(`/workspaces/${wsId}/chat/messages/${msgId}/resolve`, {
          method: "POST",
          body: JSON.stringify({ choice, note, payload }),
        }),
      simulationRun: (wsId: string) =>
        request<WorkspaceSimulationRun>(`/workspaces/${wsId}/chat/simulation-run`),
      startSimulationRun: (wsId: string) =>
        request<WorkspaceSimulationRun>(`/workspaces/${wsId}/chat/simulation-run/start`, {
          method: "POST",
        }),
      restartSimulationRun: (wsId: string) =>
        request<WorkspaceSimulationRun>(`/workspaces/${wsId}/chat/simulation-run/restart`, {
          method: "POST",
        }),
      feedback: (wsId: string, msgId: string, rating: "up" | "down") =>
        request<{
          id: string;
          rating: "up" | "down";
          mutation_sequence: number;
          mutation_status: "accepted";
          feedback_updated_at: string;
          feedback_target_kind: "task_completion" | "plan_completion";
          feedback_target_id: string;
          feedback_task_id: string | null;
          feedback_plan_id: string | null;
        }>(`/workspaces/${wsId}/chat/messages/${msgId}/feedback`, {
          method: "POST",
          body: JSON.stringify({ rating }),
        }),
      listThreads: (wsId: string) => request<any[]>(`/workspaces/${wsId}/chat/threads`),
    },
  },

  workspaceDrafts: {
    list: (status?: "active" | "ready" | "finalized" | "abandoned") => {
      const q = new URLSearchParams();
      if (status) q.set("status", status);
      return request<WorkspaceDraft[]>(
        `/workspace-drafts${q.toString() ? "?" + q.toString() : ""}`,
      );
    },
    get: (id: string) => request<WorkspaceDraft>(`/workspace-drafts/${id}`),
    updateFields: (id: string, fields: Record<string, any>) =>
      request<WorkspaceDraft>(`/workspace-drafts/${id}/fields`, { method: "PATCH", body: JSON.stringify(fields) }),
    create: (data: { initial_brief?: string } = {}) =>
      request<WorkspaceDraftTurn>("/workspace-drafts", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    sendMessage: (id: string, message: string) =>
      request<WorkspaceDraftTurn>(`/workspace-drafts/${id}/messages`, {
        method: "POST",
        body: JSON.stringify({ message }),
      }),
    /**
     * Stream the opening turn of a brand-new draft. Tokens arrive via
     * `onToken`; the final hydrated draft + reply via `onDone`.
     */
    createStream: (
      data: { initial_brief?: string; draft_id?: string; document_ids?: string[] } = {},
      handlers: WorkspaceDraftStreamHandlers = {},
    ) =>
      _streamDraftSSE(
        "/workspace-drafts/stream",
        data,
        handlers,
      ),
    /** Stream the assistant's reply to one user message (for an existing draft).
     *  `document_ids` are uploaded/knowledge documents the architect should read
     *  as sources for this turn. */
    sendMessageStream: (
      id: string,
      payload: { message: string; document_ids?: string[] },
      handlers: WorkspaceDraftStreamHandlers = {},
    ) =>
      _streamDraftSSE(
        `/workspace-drafts/${id}/messages/stream`,
        payload,
        handlers,
      ),
    applyBlueprint: (id: string, blueprint_id: string) =>
      request<WorkspaceDraft>(`/workspace-drafts/${id}/apply-blueprint`, {
        method: "POST",
        body: JSON.stringify({ blueprint_id }),
      }),
    finalize: (id: string) =>
      request<WorkspaceDraftFinalize>(`/workspace-drafts/${id}/finalize`, {
        method: "POST",
      }),
    /** SSE-streamed finalize: progress events arrive as the backend
     *  provisions Workspace + agents + staff + knowledge + channels +
     *  memory + scheduler, then a ``done`` payload with workspace_id. */
    finalizeStream: (id: string, handlers: WorkspaceFinalizeStreamHandlers = {}) =>
      _streamFinalizeSSE(`/workspace-drafts/${id}/finalize/stream`, handlers),
    abandon: (id: string) =>
      request<void>(`/workspace-drafts/${id}`, { method: "DELETE" }),
  },

  blueprints: {
    list: (status?: "draft" | "pending_review" | "published" | "archived") => {
      const q = new URLSearchParams();
      if (status) q.set("status", status);
      return request<BlueprintSummary[]>(
        `/blueprints${q.toString() ? "?" + q.toString() : ""}`,
      );
    },
    get: (id: string, shareToken?: string) =>
      request<BlueprintDetail>(
        `/blueprints/${id}${shareToken ? `?share_token=${encodeURIComponent(shareToken)}` : ""}`,
      ),
    update: (id: string, data: UpdateBlueprintRequest) =>
      request<BlueprintSummary>(
        `/blueprints/${id}`,
        { method: "PUT", body: JSON.stringify(data) },
      ),
    uploadShowcaseAsset: (
      id: string,
      file: File,
      metadata: { caption?: string; alt_text?: string } = {},
    ) => {
      const form = new FormData();
      form.append("file", file);
      if (metadata.caption) form.append("caption", metadata.caption);
      if (metadata.alt_text) form.append("alt_text", metadata.alt_text);
      return request<BlueprintSummary>(`/blueprints/${id}/showcase-assets`, {
        method: "POST",
        body: form,
      });
    },
    deleteShowcaseAsset: (id: string, assetId: string) =>
      request<BlueprintSummary>(
        `/blueprints/${id}/showcase-assets/${assetId}`,
        { method: "DELETE" },
      ),
    toggleFavorite: (id: string) =>
      request<{ is_favorited: boolean; favorite_count: number }>(
        `/blueprints/${id}/favorite`,
        { method: "POST" },
      ),
    delete: (id: string) =>
      request<void>(`/blueprints/${id}`, { method: "DELETE" }),
    installPreflight: (
      id: string,
      mode: InstallMode,
      shareToken?: string,
      variableValues: Record<string, unknown> = {},
    ) => {
      const params = new URLSearchParams();
      params.set("mode", mode);
      if (shareToken) params.set("share_token", shareToken);
      params.set("variable_values", JSON.stringify(variableValues));
      return (
      request<BlueprintInstallPreflight>(
        `/blueprints/${id}/install-preflight?${params.toString()}`,
      )
      );
    },
    install: (id: string, data: InstallBlueprintRequest) =>
      request<InstallBlueprintResponse>(
        `/blueprints/${id}/install`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    installPayload: (data: {
      payload: Record<string, any>;
      workspace_source?: {
        workspace_id: string;
        export_options?: ExportBlueprintPayloadRequest;
      };
      mode?: InstallMode;
      workspace_name?: string;
      create_missing_agents?: boolean;
      agent_ids_by_service_key?: Record<string, string>;
      governance_preset?: GovernancePresetKey;
      variable_values?: Record<string, unknown>;
      channel_config_ids?: Record<string, string>;
    }) =>
      request<InstallBlueprintResponse>(
        "/blueprints/install-payload",
        { method: "POST", body: JSON.stringify(data) },
      ),
    /** Install a .manor package: the server stores its files, then installs
     *  its payload exactly as installPayload does. */
    installPackage: (file: File, options: {
      mode?: InstallMode;
      workspace_name?: string;
      create_missing_agents?: boolean;
      agent_ids_by_service_key?: Record<string, string>;
      governance_preset?: GovernancePresetKey;
      variable_values?: Record<string, unknown>;
      channel_config_ids?: Record<string, string>;
    }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("options", JSON.stringify(options));
      return request<InstallBlueprintResponse>("/blueprints/install-package", { method: "POST", body: form });
    },
    installPayloadPreflight: (data: {
      payload: Record<string, unknown>;
      mode?: InstallMode;
      variable_values?: Record<string, unknown>;
      channel_config_ids?: Record<string, string>;
    }) =>
      request<BlueprintInstallPreflight>(
        "/blueprints/install-payload-preflight",
        { method: "POST", body: JSON.stringify(data) },
      ),
    governancePresets: () =>
      request<GovernancePresetSummary[]>("/blueprints/governance-presets"),
    createShareToken: (id: string, allowedEmails?: string[]) =>
      request<{ share_token: string }>(`/blueprints/${id}/share-token`, {
        method: "POST",
        body: allowedEmails === undefined ? undefined : JSON.stringify({ allowed_emails: allowedEmails }),
      }),
    revokeShareToken: (id: string) =>
      request<void>(`/blueprints/${id}/share-token`, { method: "DELETE" }),
    resolveShared: (token: string) =>
      request<BlueprintDetail>(`/blueprints/shared/${encodeURIComponent(token)}`),
  },



  notifications: {
    list: (params?: { unread_only?: boolean; limit?: number }) => {
      const q = new URLSearchParams();
      if (params?.unread_only) q.set("unread_only", "true");
      if (params?.limit) q.set("limit", String(params.limit));
      return request<
        PaginatedResponse<Notification> & { unread_count: number }
      >(`/notifications?${q}`);
    },
    markRead: (id: string) =>
      request<void>(`/notifications/${id}/read`, { method: "POST" }),
    markAllRead: () =>
      request<{ count: number }>("/notifications/read-all", {
        method: "POST",
      }),
    delete: (id: string) =>
      request<void>(`/notifications/${id}`, { method: "DELETE" }),
    getPreferences: () =>
      request<NotificationPreferences>("/notifications/preferences"),
    updatePreferences: (data: Partial<NotificationPreferencesUpdate>) =>
      request<NotificationPreferences>("/notifications/preferences", {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    startChannelLink: (channel_type: string) =>
      request<{
        token: string;
        channel_type: string;
        expires_at: string;
        deep_link: string | null;
        bot_username: string | null;
        instructions: string;
      }>("/notifications/preferences/link/start", {
        method: "POST",
        body: JSON.stringify({ channel_type }),
      }),
    getChannelLinkStatus: (token: string) =>
      request<{
        status: "pending" | "claimed" | "expired" | "not_found";
        contact_id?: string | null;
        claimed_at?: string | null;
      }>(`/notifications/preferences/link/${encodeURIComponent(token)}`),
  },


  admin: {
    // Settings/preferences have dynamic shape per entity, so we use Record<string, any>
    getSettings: () => request<Record<string, any>>("/admin/settings"),
    updateSettings: (data: Record<string, any>) =>
      request<Record<string, any>>("/admin/settings", {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    getPreferences: () =>
      request<Record<string, any>>("/admin/preferences"),
    updatePreferences: (data: Record<string, any>) =>
      request<Record<string, any>>("/admin/preferences", {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    // OAuth client credential management — admin/owner only.
    // Page lives at /__admin/oauth in the frontend; not linked from nav.
    oauthClients: {
      list: () =>
        request<Array<{
          server_key: string;
          name: string;
          client_id: string | null;
          has_secret: boolean;
          client_secret_required: boolean;
          source: "env" | "ui" | "db" | "none";
          scopes: string | null;
          configured: boolean;
          client_id_env_var: string;
          client_secret_env_var: string;
          redirect_uri: string;
        }>>("/admin/oauth-clients"),
      update: (
        serverKey: string,
        body: { client_id: string; client_secret: string; scopes?: string },
      ) =>
        request<{ ok: true }>(`/admin/oauth-clients/${serverKey}`, {
          method: "PUT",
          body: JSON.stringify(body),
        }),
      reset: (serverKey: string) =>
        request<{ ok: true }>(`/admin/oauth-clients/${serverKey}`, {
          method: "DELETE",
        }),
      checkHealth: (serverKey: string) =>
        request<{
          server_key: string;
          ok: boolean;
          status_code: number | null;
          detail: string;
        }>(`/admin/oauth-clients/${serverKey}/health`),
    },
  },

  dashboard: {
    layout: () =>
      request<{
        version: number;
        widgets: Array<{ id: string; visible: boolean }>;
        modules: Array<{
          id: string;
          title: string;
          description?: string | null;
          visible: boolean;
          size: "compact" | "wide";
          conversation_id?: string | null;
          code: any;
        }>;
      }>("/dashboard/layout"),
    updateLayout: (
      widgets: Array<{ id: string; visible: boolean }>,
      modules: Array<{
        id: string;
        title: string;
        description?: string | null;
        visible: boolean;
        size: "compact" | "wide";
        conversation_id?: string | null;
        code: any;
      }>,
    ) =>
      request<{
        version: number;
        widgets: Array<{ id: string; visible: boolean }>;
        modules: any[];
      }>("/dashboard/layout", {
        method: "PUT",
        body: JSON.stringify({ widgets, modules }),
      }),
    suggestLayout: (
      prompt: string,
      widgets: Array<{ id: string; visible: boolean }>,
      modules: Array<{
        id: string;
        title: string;
        description?: string | null;
        visible: boolean;
        size: "compact" | "wide";
        conversation_id?: string | null;
        code: any;
      }>,
      options?: {
        targetModuleId?: string;
        conversationId?: string;
        signal?: AbortSignal;
      },
    ) =>
      request<{
        version: number;
        widgets: Array<{ id: string; visible: boolean }>;
        modules: any[];
        assistant_message?: string | null;
        changed_module_id?: string | null;
        conversation_id?: string | null;
        tool_calls: string[];
        hitl_requests: Array<Record<string, unknown>>;
        preview_created: boolean;
      }>("/dashboard/layout/suggest", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          widgets,
          modules,
          target_module_id: options?.targetModuleId,
          conversation_id: options?.conversationId,
        }),
        signal: options?.signal,
      }),
    suggestLayoutJobStart: (
      prompt: string,
      widgets: Array<{ id: string; visible: boolean }>,
      modules: Array<{
        id: string;
        title: string;
        description?: string | null;
        visible: boolean;
        size: "compact" | "wide";
        conversation_id?: string | null;
        code: any;
      }>,
      options?: {
        targetModuleId?: string;
        conversationId?: string;
        signal?: AbortSignal;
      },
    ) =>
      request<DashboardSuggestJobResponse>("/dashboard/layout/suggest/jobs", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          widgets,
          modules,
          target_module_id: options?.targetModuleId,
          conversation_id: options?.conversationId,
        }),
        signal: options?.signal,
      }),
    suggestLayoutJobStatus: (jobId: string, signal?: AbortSignal) =>
      request<DashboardSuggestJobResponse>(
        `/dashboard/layout/suggest/jobs/${encodeURIComponent(jobId)}`,
        { signal },
      ),
    suggestLayoutJobCancel: (jobId: string) =>
      request<DashboardSuggestJobResponse>(
        `/dashboard/layout/suggest/jobs/${encodeURIComponent(jobId)}/cancel`,
        { method: "POST" },
      ),
    moduleConversation: (moduleId: string) =>
      request<{
        conversation_id?: string | null;
        messages: Array<{
          role: "user" | "assistant";
          content: string;
          tool_calls: string[];
        }>;
      }>(`/dashboard/modules/${encodeURIComponent(moduleId)}/conversation`),
    httpData: (url: string, refreshSeconds = 300) =>
      request<{
        url: string;
        result: unknown;
        cached: boolean;
      }>("/dashboard/http-data", {
        method: "POST",
        body: JSON.stringify({
          url,
          refresh_seconds: refreshSeconds,
        }),
      }),
    toolData: (
      toolName: string,
      args: Record<string, unknown>,
      conversationId?: string | null,
      refreshSeconds = 300,
    ) =>
      request<{
        tool_name: string;
        result: unknown;
        cached: boolean;
      }>("/dashboard/tool-data", {
        method: "POST",
        body: JSON.stringify({
          tool_name: toolName,
          arguments: args,
          conversation_id: conversationId,
          refresh_seconds: refreshSeconds,
        }),
      }),
    news: (query?: string, days = 1, limit = 8) => {
      const params = new URLSearchParams({
        days: String(days),
        limit: String(limit),
      });
      if (query) params.set("query", query);
      return request<Array<{
        id: string;
        title: string;
        url: string;
        source?: string | null;
        published_at?: string | null;
        language?: string | null;
      }>>(`/dashboard/news?${params}`);
    },
    stocks: (symbols: string[]) => {
      const params = new URLSearchParams({ symbols: symbols.join(",") });
      return request<Array<{
        symbol: string;
        price?: number | null;
        change?: number | null;
        change_percent?: number | null;
        open?: number | null;
        high?: number | null;
        low?: number | null;
        previous_close?: number | null;
        currency?: string | null;
        updated_at?: string | null;
        status: "ok" | "unavailable";
        provider?: string | null;
      }>>(`/dashboard/stocks?${params}`);
    },
    stats: (wsId?: string) => request<any>(`/dashboard/stats${wsId ? `?workspace_id=${wsId}` : ""}`),
    taskTrends: (days = 30, wsId?: string) =>
      request<any[]>(`/dashboard/task-trends?days=${days}${wsId ? `&workspace_id=${wsId}` : ""}`),
    usageTrends: (days = 30) =>
      request<any[]>(`/dashboard/usage-trends?days=${days}`),
    recentActivity: (limit = 10, wsId?: string, since?: string | null) => {
      const params = new URLSearchParams({ limit: String(limit) });
      if (wsId) params.set("workspace_id", wsId);
      if (since) params.set("since", since);
      return request<any[]>(`/dashboard/recent-activity?${params.toString()}`);
    },
    activeGoals: (limit = 5, wsId?: string) =>
      request<any[]>(`/dashboard/active-goals?limit=${limit}${wsId ? `&workspace_id=${wsId}` : ""}`),
  },

  activity: {
    feed: (limit = 20) => request<any[]>(`/activity/feed?limit=${limit}`),
    events: (params?: {
      event_type?: string;
      limit?: number;
      offset?: number;
    }) => {
      const q = new URLSearchParams();
      if (params?.event_type) q.set("event_type", params.event_type);
      if (params?.limit) q.set("limit", String(params.limit));
      return request<{ items: any[]; total: number }>(
        `/activity/events?${q}`,
      );
    },
  },

  bulk: {
    updateTaskStatus: (taskIds: string[], status: string) =>
      request<{ count: number }>("/bulk/tasks/status", {
        method: "POST",
        body: JSON.stringify({ task_ids: taskIds, status }),
      }),
    exportTasksCsv: async (status?: string) => {
      const token = await getReadyAuthToken();
      const q = status ? `?status=${status}` : "";
      return fetch(`${API_BASE}/bulk/export/tasks${q}`, {
        headers: { Authorization: `Bearer ${token}` || "" },
      }).then((r) => r.text());
    },
    importTasksCsv: async (file: File) => {
      const token = await getReadyAuthToken();
      const form = new FormData();
      form.append("file", file);
      return fetch(`${API_BASE}/bulk/import/tasks`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` || "" },
        body: form,
      }).then((r) => r.json());
    },
  },

  usage: {
    summary: (days?: number) =>
      request<UsageSummary>(`/usage/summary?days=${days || 30}`),
    daily: (days?: number, scope: "company" | "member" = "company") =>
      request<{ date: string; input: number; output: number; total: number }[]>(`/usage/daily?days=${days || 30}&scope=${scope}`),
    bySource: (days?: number, scope: "company" | "member" = "company") =>
      request<UsageBySourceRow[]>(`/usage/by-source?days=${days || 30}&scope=${scope}`),
    team: (params?: { days?: number; activity_limit?: number }) => {
      const q = new URLSearchParams();
      q.set("days", String(params?.days || 30));
      if (params?.activity_limit) q.set("activity_limit", String(params.activity_limit));
      return request<TeamUsageResponse>(`/usage/team?${q}`);
    },
  },

  comments: {
    list: (resourceType: string, resourceId: string) =>
      request<Comment[]>(`/comments?resource_type=${resourceType}&resource_id=${resourceId}`),
    create: (data: { resource_type: string; resource_id: string; content: string; parent_id?: string; anchor?: CommentAnchor | null }) =>
      request<Comment>("/comments", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, content: string) =>
      request<Comment>(`/comments/${id}`, { method: "PUT", body: JSON.stringify({ content }) }),
    delete: (id: string) => request<void>(`/comments/${id}`, { method: "DELETE" }),
    react: (id: string, reaction: string) =>
      request<any>(`/comments/${id}/reactions`, { method: "POST", body: JSON.stringify({ reaction }) }),
    count: (resourceType: string, resourceId: string) =>
      request<{ count: number }>(`/comments/count?resource_type=${resourceType}&resource_id=${resourceId}`),
  },

  twoFactor: {
    status: () => request<{ enabled: boolean }>("/auth/2fa/status"),
    setup: () => request<{ secret: string; uri: string }>("/auth/2fa/setup", { method: "POST" }),
    verify: (code: string) =>
      request<{
        enabled: boolean;
        backup_codes: string[];
        access_token: string;
        token_type: string;
        user_id: string;
        entity_id: string;
        role: string;
      }>("/auth/2fa/verify", {
        method: "POST",
        body: JSON.stringify({ code }),
      }),
    disable: (code: string) =>
      request<void>("/auth/2fa/disable", { method: "POST", body: JSON.stringify({ code }) }),
  },

  oauthConnections: {
    list: () => request<OAuthConnection[]>("/oauth/connections"),
    revoke: (id: string) =>
      request<void>(`/oauth/connections/${encodeURIComponent(id)}`, { method: "DELETE" }),
  },

  staff: {
    list: (params?: { department?: string; role?: string; kind?: string }) => {
      const q = new URLSearchParams();
      if (params?.department) q.set("department", params.department);
      if (params?.role) q.set("role", params.role);
      if (params?.kind) q.set("kind", params.kind);
      return request<any[]>(`/staff?${q}`);
    },
    invite: (data: { email: string; role_id?: string | null; workspace_ids?: string[]; name?: string }) =>
      request<any>("/staff/invite", { method: "POST", body: JSON.stringify(data) }),
    roles: {
      list: () => request<any[]>("/staff/roles"),
      create: (data: { name: string; permissions: string[]; is_default?: boolean }) =>
        request<any>("/staff/roles", { method: "POST", body: JSON.stringify(data) }),
      update: (id: string, data: { name?: string; permissions?: string[]; is_default?: boolean }) =>
        request<any>(`/staff/roles/${id}`, { method: "PUT", body: JSON.stringify(data) }),
      delete: (id: string, reassign_to_role_id?: string) =>
        request<void>(`/staff/roles/${id}`, {
          method: "DELETE",
          body: JSON.stringify({ reassign_to_role_id: reassign_to_role_id ?? null }),
        }),
    },
    permissionsCatalog: () =>
      request<{ name: string; permissions: { key: string; label: string }[] }[]>(`/permissions`),
    create: (data: any) =>
      request<any>("/staff", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
      request<any>(`/staff/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      request<void>(`/staff/${id}`, { method: "DELETE" }),
    transferKnowledgeToEntity: (staffId: string) =>
      request<{
        departed_user_id: string;
        documents_transferred: number;
        folders_transferred: number;
      }>(`/staff/${staffId}/transfer-knowledge-to-entity`, { method: "POST" }),
    leaveTeam: () =>
      request<{ staff_id: string; status: string }>("/staff/me/leave", { method: "POST" }),
    createAccount: (staffId: string) =>
      request<{ staff_id: string; user_id: string; email: string; password: string }>(
        `/staff/${staffId}/create-account`,
        { method: "POST" },
      ),
    resendInvite: (staffId: string) =>
      request<{
        staff_id: string;
        invite_token: string;
        invite_url: string;
        email: string;
        status: string;
        email_sent: boolean;
      }>(`/staff/${staffId}/resend-invite`, { method: "POST" }),
    resetAccountPassword: (staffId: string) =>
      request<{ staff_id: string; user_id: string; email: string; password: string }>(
        `/staff/${staffId}/reset-password`,
        { method: "POST" },
      ),
    departments: {
      list: () => request<StaffDepartment[]>("/staff/departments"),
      create: (data: Partial<StaffDepartment>) =>
        request<StaffDepartment>("/staff/departments", {
          method: "POST",
          body: JSON.stringify(data),
        }),
      update: (id: string, data: Partial<StaffDepartment>) =>
        request<StaffDepartment>(`/staff/departments/${id}`, {
          method: "PUT",
          body: JSON.stringify(data),
        }),
      delete: (id: string) =>
        request<void>(`/staff/departments/${id}`, { method: "DELETE" }),
    },
    schedule: {
      get: (staffId: string) =>
        request<StaffSchedule[]>(`/staff/${staffId}/schedule`),
      set: (staffId: string, schedules: Partial<StaffSchedule>[]) =>
        request<any>(`/staff/${staffId}/schedule`, {
          method: "PUT",
          body: JSON.stringify({ schedules }),
        }),
      addException: (staffId: string, data: Partial<StaffScheduleException>) =>
        request<any>(`/staff/${staffId}/schedule/exceptions`, {
          method: "POST",
          body: JSON.stringify(data),
        }),
    },
    availability: (date?: string) => {
      const q = new URLSearchParams();
      if (date) q.set("date", date);
      return request<any[]>(`/staff/availability?${q}`);
    },
  },

  messages: {
    listThreads: () => request<any[]>("/messages/threads"),
    getThread: (threadId: string) => request<any[]>(`/messages/threads/${threadId}`),
    send: (data: { recipient_id: string; content: string; thread_id?: string }) =>
      request<any>("/messages", { method: "POST", body: JSON.stringify(data) }),
    markRead: (threadId: string) =>
      request<void>(`/messages/threads/${threadId}/read`, { method: "POST" }),
  },

  jobs: {
    list: (params?: {
      enabled_only?: boolean;
      workspace_id?: string;
      limit?: number;
      offset?: number;
      search?: string;
      status?: "all" | "enabled" | "paused" | "attention";
      agent_id?: string;
      include_workflows?: boolean;
    }) => {
      const q = new URLSearchParams();
      if (params?.enabled_only) q.set("enabled_only", "true");
      if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.offset) q.set("offset", String(params.offset));
      if (params?.search) q.set("search", params.search);
      if (params?.status && params.status !== "all") q.set("status", params.status);
      if (params?.agent_id) q.set("agent_id", params.agent_id);
      if (params?.include_workflows !== undefined) {
        q.set("include_workflows", String(params.include_workflows));
      }
      return request<{
        items: any[];
        total: number;
        summary_total: number;
        enabled_total: number;
        attention_total: number;
      }>(`/jobs?${q}`);
    },
    get: (jobId: string) => request<any>(`/jobs/${encodeURIComponent(jobId)}`),
    create: (data: any) =>
      request<any>("/jobs", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
      request<any>(`/jobs/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/jobs/${id}`, { method: "DELETE" }),
    toggle: (id: string, enabled: boolean) =>
      request<any>(`/jobs/${id}/toggle`, {
        method: "POST",
        body: JSON.stringify({ enabled }),
      }),
    runs: (jobId: string) =>
      request<any[]>(`/jobs/${jobId}/runs`),
    runDetail: (jobId: string, runId: string) =>
      request<{ run: any; task: any | null; agent_execution: any | null }>(
        `/jobs/${jobId}/runs/${runId}`,
      ),
    runNow: (jobId: string, idempotencyKey = crypto.randomUUID()) =>
      request<{ job_id: string; queued_at: string; idempotency_key: string }>(
        `/jobs/${jobId}/run_now`, {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
        },
      ),
  },

  search: {
    global: (query: string, limit?: number) =>
      request<GlobalSearchResponse>(globalSearchPath(query, limit))
        .then(normalizeGlobalSearchResponse),
  },

  workflows: {
    list: () => request<any[]>("/workflows"),
    listProjectCandidates: (params: {
      workspace_id: string;
      project_type: string;
      stages: string[];
      limit?: number;
    }) => {
      const q = new URLSearchParams({
        workspace_id: params.workspace_id,
        project_type: params.project_type,
      });
      for (const stage of params.stages) q.append("stage", stage);
      if (params.limit) q.set("limit", String(params.limit));
      return request<Array<{
        id: string;
        project_type: string;
        current_stage: string;
        title: string;
        youtube_video_id?: string | null;
        created_at: string;
        updated_at?: string | null;
      }>>(`/workflows/projects/candidates?${q}`);
    },
    listRecordChoices: (params: { workflow_id: string; input_key: string; workspace_id: string }) =>
      request<Array<{ value: string; label: string; description?: string | null }>>(
        `/workflows/${encodeURIComponent(params.workflow_id)}/run-inputs/${encodeURIComponent(params.input_key)}/choices?${new URLSearchParams({ workspace_id: params.workspace_id })}`,
      ),
    templates: () => request<any[]>("/workflows/templates"),
    installTemplate: (templateId: string, workspaceId?: string) =>
      request<any>(`/workflows/templates/${encodeURIComponent(templateId)}/install`, {
        method: "POST",
        body: JSON.stringify({ workspace_id: workspaceId || null }),
      }),
    create: (data: any) =>
      request<any>("/workflows", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<any>(`/workflows/${id}`),
    metadata: (id: string) => request<any>(`/workflows/${id}/metadata`),
    update: (id: string, data: any) =>
      request<any>(`/workflows/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/workflows/${id}`, { method: "DELETE" }),
    startRun: (id: string, data?: any) =>
      request<any>(`/workflows/${id}/run`, { method: "POST", body: JSON.stringify(data || {}) }),
    // Streaming run — lights up the canvas node-by-node as it executes.
    runStream: (
      id: string,
      onNode: (nodeId: string, status: string) => void,
      onRun?: (runId: string) => void,
      data?: { variables?: Record<string, any>; trigger_data?: Record<string, any> },
    ) => streamWorkflowRun(`/workflows/${id}/run-stream`, onNode, onRun, data),
    runs: (id: string) => request<any[]>(`/workflows/${id}/runs`),
    listRuns: (params?: { workspace_id?: string; binding_id?: string; workflow_id?: string; include_unbound?: boolean; app_action?: string; include_unlabelled?: boolean; status?: string; limit?: number }, signal?: AbortSignal) => {
      const q = new URLSearchParams();
      if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
      if (params?.binding_id) q.set("binding_id", params.binding_id);
      if (params?.workflow_id) q.set("workflow_id", params.workflow_id);
      if (params?.include_unbound) q.set("include_unbound", "true");
      if (params?.app_action) q.set("app_action", params.app_action);
      if (params?.include_unlabelled) q.set("include_unlabelled", "true");
      if (params?.status) q.set("status", params.status);
      if (params?.limit) q.set("limit", String(params.limit));
      return request<any[]>(`/workflows/runs${q.toString() ? `?${q}` : ""}`, {
        headers: { "X-Silent-Error": "1" },
        signal,
      });
    },
    getRun: (runId: string, detail = true, signal?: AbortSignal) => request<any>(
      `/workflows/runs/${runId}${detail ? "" : "?detail=false"}`,
      { signal },
    ),
    getRunFamily: (runId: string, signal?: AbortSignal) => request<any[]>(
      `/workflows/runs/${runId}/family`,
      { signal },
    ),
    cancelRun: (runId: string) => request<any>(`/workflows/runs/${runId}/cancel`, { method: "POST" }),
    pauseRun: (runId: string) => request<any>(`/workflows/runs/${runId}/pause`, { method: "POST" }),
    resolveRuntimeApproval: (runId: string, requestId: string, choice: "approve" | "reject", review?: unknown) =>
      request<any>(`/workflows/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(requestId)}/resolve`, {
        method: "POST",
        body: JSON.stringify({ choice, ...(review !== undefined ? { review } : {}) }),
      }),
    retryRun: (runId: string, data: {
      from_step_id?: string;
      variables?: Record<string, any>;
      execute?: boolean;
      effect_review?: { step_id: string; effect_fingerprint: string; decision: "not_applied" | "applied"; confirmed: true; note: string; result?: Record<string, unknown>; project_revisions?: Record<string, number> };
    }) => request<any>(`/workflows/runs/${runId}/retry`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
    /** The next version of a finished run's result, from its Workflow's production.revise. */
    reviseRun: (runId: string, data: { notes?: string; unit_versions?: Record<string, string> }) =>
      request<{ id: string; status: string }>(`/workflows/runs/${runId}/revise`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    resumeRun: (runId: string, variables?: Record<string, any>, expectedStepId?: string) =>
      request<any>(`/workflows/runs/${runId}/resume`, {
        method: "POST",
        body: JSON.stringify({ variables: variables ?? null, expected_step_id: expectedStepId }),
      }),
    executeStep: (runId: string) =>
      request<any>(`/workflows/runs/${runId}/step`, { method: "POST" }),
    // Run a single node standalone (test a node's config without the workflow).
    runNode: (step: any, variables?: Record<string, any>) =>
      request<{
        status: string;
        output?: any;
        error?: string;
        step_id?: string;
        inputs?: Record<string, any>;
        duration_ms?: number;
        cached?: boolean;
        skipped?: boolean;
        [key: string]: any;
      }>("/workflows/run-node", {
        method: "POST",
        body: JSON.stringify({ step, variables: variables ?? null }),
      }),
    // AI edit — generate or modify the graph from a natural-language prompt.
    // Streams progress; resolves with the proposed {name, steps, variables}.
    aiEdit: (
      prompt: string,
      currentSteps?: any[] | null,
      onStep?: (label: string) => void,
    ) =>
      streamSseResult<{ name: string; steps: any[]; variables: Record<string, any> }>(
        "/workflows/ai-edit",
        { prompt, current_steps: currentSteps ?? null },
        onStep,
      ),
    // Import ComfyUI / n8n / Dify exports
    importPreview: (content: string, name?: string) =>
      request<any>("/workflows/import", {
        method: "POST",
        body: JSON.stringify({ content, name, dry_run: true }),
      }),
    import: (data: {
      content: string;
      name?: string;
      workspace_id?: string;
      business_line?: string;
      create_binding?: boolean;
    }) =>
      request<any>("/workflows/import", { method: "POST", body: JSON.stringify(data) }),
    // Bindings (deploy a workflow into a workspace / business line) + triggers
    listBindings: (params?: { workspace_id?: string; business_line?: string; workflow_id?: string }) => {
      const q = new URLSearchParams(
        Object.entries(params || {}).filter(([, v]) => v) as [string, string][],
      ).toString();
      return request<any[]>(`/workflows/bindings${q ? `?${q}` : ""}`);
    },
    createBinding: (data: any) =>
      request<any>("/workflows/bindings", { method: "POST", body: JSON.stringify(data) }),
    updateBinding: (bindingId: string, data: any) =>
      request<any>(`/workflows/bindings/${bindingId}`, { method: "PUT", body: JSON.stringify(data) }),
    deleteBinding: (bindingId: string) =>
      request<void>(`/workflows/bindings/${bindingId}`, { method: "DELETE" }),
    runBinding: (bindingId: string, data?: any) =>
      request<any>(`/workflows/bindings/${bindingId}/run`, {
        method: "POST",
        headers: { "X-Silent-Error": "1" },
        body: JSON.stringify(data || {}),
      }),
    trigger: (data: {
      trigger_type?: string;
      event_name?: string;
      workspace_id?: string;
      trigger_data?: any;
    }) => request<any>("/workflows/trigger", { method: "POST", body: JSON.stringify(data) }),
  },

  skills: {
    catalog: (params?: { include_platform?: boolean; category?: string; known_version?: string }) => {
      const q = new URLSearchParams();
      if (params?.include_platform) q.set("include_platform", "true");
      if (params?.category) q.set("category", params.category);
      if (params?.known_version) q.set("known_version", params.known_version);
      const suffix = q.toString() ? `?${q}` : "";
      return request<{ version: string; items: any[] | null }>(`/skills/catalog${suffix}`);
    },
    list: (params?: { include_platform?: boolean; category?: string }) => {
      const q = new URLSearchParams();
      if (params?.include_platform) q.set("include_platform", "true");
      if (params?.category) q.set("category", params.category);
      const suffix = q.toString() ? `?${q}` : "";
      return request<any[]>(`/skills${suffix}`);
    },
    store: () => request<any[]>("/skills/store"),
    getFiles: (id: string) =>
      request<{
        skill_id: string;
        prompt: string;
        extra_files: Record<string, string>;
        requirements: string;
        files: Record<string, string>;
        skipped_files: { path: string; reason: string; size?: number }[];
      }>(`/skills/${id}/files`),
    create: (data: any) =>
      request<any>("/skills", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
      request<any>(`/skills/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/skills/${id}`, { method: "DELETE" }),
    invoke: (id: string, input: string | Record<string, unknown>) =>
      request<any>(`/skills/${id}/invoke`, { method: "POST", body: JSON.stringify({ input }) }),
    generate: (prompt: string, category?: string) =>
      request<any>("/skills/generate", {
        method: "POST",
        body: JSON.stringify({ prompt, category }),
      }),
    generateStream: (prompt: string, onStep?: (label: string) => void, category?: string) =>
      streamSseResult<any>("/skills/generate-stream", { prompt, category }, onStep),
    draftQuestions: (prompt: string) =>
      request<{ questions: string[]; ready: boolean }>("/skills/draft-questions", {
        method: "POST",
        body: JSON.stringify({ prompt }),
      }),
    installGithub: (githubUrl: string) =>
      request<any>("/skills/install-github", {
        method: "POST",
        body: JSON.stringify({ github_url: githubUrl }),
      }),
    aiUpdate: (id: string, prompt: string) =>
      request<any>(`/skills/${id}/ai-update`, {
        method: "POST",
        body: JSON.stringify({ prompt }),
      }),
    batchImport: (skills: any[]) =>
      request<{ imported: number; skipped: number; failed: number }>(
        "/skills/batch-import",
        { method: "POST", body: JSON.stringify({ skills }) },
      ),
    // Agent skill bindings
    listAgentBindings: (agentId: string) =>
      request<any[]>(`/skills/agents/${agentId}/bindings`),
    listAgentAvailable: (agentId: string) =>
      request<any[]>(`/skills/agents/${agentId}/available`),
    bindSkill: (agentId: string, skillId: string) =>
      request<any>(`/skills/agents/${agentId}/bindings/${skillId}`, { method: "POST" }),
    unbindSkill: (agentId: string, skillId: string) =>
      request<void>(`/skills/agents/${agentId}/bindings/${skillId}`, { method: "DELETE" }),
    saveCredentials: (skillId: string, values: Record<string, string>) =>
      request<any>(`/skills/${skillId}/credentials`, {
        method: "PUT",
        body: JSON.stringify({ values }),
      }),
  },

  goals: {
    list: async (params?: { status?: string; limit?: number; workspace_id?: string }) => {
      const q = new URLSearchParams();
      if (params?.status) q.set("status", params.status);
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
      const response = await request<any>(`/goals?${q}`);
      return Array.isArray(response)
        ? { items: response, total: response.length }
        : response;
    },
    get: (id: string) => request<any>(`/goals/${id}`),
    create: (data: any) =>
      request<any>("/goals", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
      request<any>(`/goals/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/goals/${id}`, { method: "DELETE" }),
    cancel: (id: string) =>
      request<any>(`/goals/${id}/cancel`, { method: "POST" }),
    getSteps: (id: string) => request<any[]>(`/goals/${id}/steps`),
    getMeasurements: (id: string, limit = 30) =>
      request<any[]>(`/goals/${id}/measurements?limit=${limit}`),
    addMeasurement: (id: string, value: number | string, source = "manual", note?: string) =>
      request<any>(`/goals/${id}/measurements`, {
        method: "POST",
        body: JSON.stringify({ value, source, note }),
      }),
  },

  workers: {
    list: (params?: { status?: string; kind?: string }) => {
      const q = new URLSearchParams();
      if (params?.status) q.set("status", params.status);
      if (params?.kind) q.set("kind", params.kind);
      const suffix = q.toString() ? `?${q}` : "";
      return request<WorkerResponse[]>(`/workers${suffix}`);
    },
    pause: (workerId: string) =>
      request<WorkerResponse>(`/workers/${workerId}/pause`, { method: "POST" }),
    resume: (workerId: string) =>
      request<WorkerResponse>(`/workers/${workerId}/resume`, { method: "POST" }),
    rename: (workerId: string, displayName: string) =>
      request<WorkerResponse>(`/workers/${workerId}`, {
        method: "PATCH",
        body: JSON.stringify({ display_name: displayName }),
      }),
    revoke: (workerId: string) =>
      request<WorkerResponse>(`/workers/${workerId}/revoke`, { method: "POST" }),
    register: (data: WorkerRegisterRequest) =>
      request<WorkerRegisterResponse>("/workers/register", {
        method: "POST",
        body: JSON.stringify(data),
      }),
  },


  emailAccounts: {
    list: () => request<import("./emailAccounts").EmailAccountSettings>("/integrations/email-accounts"),
    setDefault: (senderId: string) => request<void>("/integrations/email-accounts/default", {
      method: "PUT", body: JSON.stringify({ sender_id: senderId }),
    }),
  },

  integrations: {
    list: () => request<any[]>("/integrations"),
    create: (data: any) =>
      request<any>("/integrations", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
      request<any>(`/integrations/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/integrations/${id}`, { method: "DELETE" }),
    getConnectionShares: (kind: ConnectionKind, connectionId: string) =>
      request<ConnectionShareState>(`/integrations/connections/${kind}/${connectionId}/grants`),
    shareConnectionWithUsers: (kind: ConnectionKind, connectionId: string, userIds: string[]) =>
      request<ConnectionShareState>(
        `/integrations/connections/${kind}/${connectionId}/grants`,
        { method: "POST", body: JSON.stringify({ user_ids: userIds }) },
      ),
    setConnectionEntityShare: (kind: ConnectionKind, connectionId: string, enabled: boolean) =>
      request<ConnectionShareState>(
        `/integrations/connections/${kind}/${connectionId}/entity-share`,
        { method: "PUT", body: JSON.stringify({ enabled }) },
      ),
    listConnectionUsage: (kind: ConnectionKind, connectionId: string) =>
      request<ConnectionUse[]>(`/integrations/connections/${kind}/${connectionId}/usage`),
    deleteConnectionGrant: (
      kind: ConnectionKind,
      connectionId: string,
      grantId: string,
    ) => request<void>(
      `/integrations/connections/${kind}/${connectionId}/grants/${grantId}`,
      { method: "DELETE" },
    ),
    nango: {
      /** Mint a Nango Connect session token + popup URL. */
      startConnect: (
        provider_config_keys?: string[],
        replace_integration_id?: string,
      ) =>
        request<{
          session_token: string;
          nango_connect_url: string;
          connection_id: string;
          provider_config_key: string;
        }>(
          "/integrations/nango/connect-session",
          {
            method: "POST",
            body: JSON.stringify({ provider_config_keys, replace_integration_id }),
          },
        ),
      /** After the popup closes, mirror Nango connections into our integrations table. */
      sync: (data: {
        expected_connection_id: string;
        expected_provider_config_key: string;
        replace_integration_id?: string;
        whatsapp_waba_id?: string;
        whatsapp_phone_number_id?: string;
      }) =>
        request<NangoSyncResponse>(
          "/integrations/nango/connections/sync",
          { method: "POST", body: JSON.stringify(data) },
        ),
    },
    mcpServers: (options?: { snapshot?: boolean }) =>
      request<IntegrationMCPServer[]>(
        `/integrations/mcp-servers${options?.snapshot ? "?snapshot=true" : ""}`,
        { headers: { "X-Silent-Error": "1" } },
      ),
    setBuiltinPluginEnabled: (pluginKey: string, enabled: boolean) =>
      request<{ plugin_key: string; enabled: boolean }>(
        `/integrations/plugins/${encodeURIComponent(pluginKey)}`,
        { method: "PUT", body: JSON.stringify({ enabled }) },
      ),
    operations: (serverKey: string) =>
      request<IntegrationOperationCatalog>(
        `/integrations/mcp-servers/${encodeURIComponent(serverKey)}/tools`,
        { headers: { "X-Silent-Error": "1" } },
      ),
    setDefaultConnection: (serverKey: string, connectionId: string) =>
      request<void>(
        `/integrations/mcp-servers/${serverKey}/connections/${connectionId}/set-default`,
        { method: "POST" },
      ),
    disconnectAccount: (serverKey: string, connectionId: string) =>
      request<void>(
        `/integrations/mcp-servers/${serverKey}/connections/${connectionId}`,
        { method: "DELETE" },
      ),
    setDefaultEntityAccount: (serverKey: string, accountId: string) =>
      request<void>(
        `/integrations/mcp-servers/${serverKey}/entity-accounts/${accountId}/set-default`,
        { method: "POST" },
      ),
    retryWhatsAppProvisioning: (accountId: string, registration_pin: string) =>
      request<{
        ok: boolean;
        integration_id: string;
        readiness_code: string;
        detail: string;
      }>(
        `/integrations/entity-accounts/${accountId}/whatsapp/provisioning/retry`,
        {
          method: "POST",
          body: JSON.stringify({ registration_pin }),
        },
      ),
    deleteEntityAccount: (serverKey: string, accountId: string) =>
      request<void>(
        `/integrations/mcp-servers/${serverKey}/entity-accounts/${accountId}`,
        { method: "DELETE" },
      ),
    testEntityAccount: (accountId: string) =>
      request<HealthStatus>(
        `/integrations/health-check/entity-accounts/${accountId}`,
        { method: "POST" },
      ),
    testOAuthConnection: (connectionId: string) =>
      request<HealthStatus>(
        `/integrations/health-check/connections/${connectionId}`,
        { method: "POST" },
      ),
    registerWebhook: (accountId: string) =>
      request<{ registered: boolean; url?: string; reason?: string; detail?: string }>(
        `/integrations/wiring/entity-accounts/${accountId}/register`,
        { method: "POST" },
      ),
    wechatPersonalStatus: (accountId: string) =>
      request<{
        online: boolean;
        qr_pending: boolean;
        account?: { user_name?: string | null; nick_name?: string | null } | null;
        last_error?: string | null;
        callback_configured?: boolean;
      }>(`/integrations/wechat-personal/${accountId}/status`),
    wechatPersonalQrBlob: (accountId: string) =>
      fetchProtectedBlob(
        `wechat-personal-qr:${accountId}`,
        `${API_BASE}/integrations/wechat-personal/${accountId}/qr.png`,
        { cache: false, force: true },
      ),

    // ── Multi-session WeChat scan flow ──────────────────────────
    // The Integration row is created on /finish, AFTER the runner says
    // status.online === true. Until then the session lives only on
    // the runner (in-memory) and these endpoints poll/proxy by sid.
    wechatPersonalStartSession: () =>
      request<{ session_id: string; qr_path: string }>(
        `/integrations/wechat-personal/sessions`,
        { method: "POST" },
      ),
    wechatPersonalSessionStatus: (sessionId: string) =>
      request<{
        online: boolean;
        qr_pending: boolean;
        account?: { user_name?: string | null; nick_name?: string | null } | null;
        last_error?: string | null;
        callback_configured?: boolean;
      }>(`/integrations/wechat-personal/sessions/${sessionId}/status`),
    wechatPersonalSessionQrBlob: (sessionId: string) =>
      fetchProtectedBlob(
        `wechat-personal-session-qr:${sessionId}`,
        `${API_BASE}/integrations/wechat-personal/sessions/${sessionId}/qr.png`,
        { cache: false, force: true },
      ),
    wechatPersonalFinishSession: (sessionId: string, name?: string) =>
      request<{ id: string; provider: string; status: string }>(
        `/integrations/wechat-personal/sessions/${sessionId}/finish`,
        {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId, name }),
        },
      ),
    wechatPersonalCancelSession: (sessionId: string) =>
      request<void>(
        `/integrations/wechat-personal/sessions/${sessionId}`,
        { method: "DELETE" },
      ),
    channelBindings: () =>
      request<Array<{
        channel_config_id: string;
        channel_type: string;
        provider: string;
        name: string | null;
        display_name: string;
        status: string;
        bound_channel_id: string | null;
        bound_agent_id: string | null;
        bound_agent_subscription_id: string | null;
        bound_workspace_id: string | null;
        agent_name: string | null;
        workspace_name: string | null;
        binding_status: string | null;
        last_inbound_at: string | null;
        last_outbound_at: string | null;
      }>>("/integrations/channel-bindings"),
    upsertChannelBinding: (data: {
      channel_config_id: string;
      agent_id?: string | null;
      agent_subscription_id?: string | null;
    }) =>
      request<{
        channel_config_id: string;
        channel_type: string;
        provider: string;
        name: string | null;
        display_name: string;
        status: string;
        bound_channel_id: string | null;
        bound_agent_id: string | null;
        bound_agent_subscription_id: string | null;
        bound_workspace_id: string | null;
        agent_name: string | null;
        workspace_name: string | null;
        binding_status: string | null;
        last_inbound_at: string | null;
        last_outbound_at: string | null;
      }>("/integrations/channel-bindings", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    deleteChannelBinding: (channelId: string) =>
      request<void>(
        `/integrations/channel-bindings/${channelId}`,
        { method: "DELETE" },
      ),
    logs: (params?: { channel_type?: string; direction?: string; limit?: number; offset?: number }) => {
      const q = new URLSearchParams();
      if (params?.channel_type) q.set("channel_type", params.channel_type);
      if (params?.direction) q.set("direction", params.direction);
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.offset) q.set("offset", String(params.offset));
      return request<Array<{
        id: string;
        channel_type: string;
        channel_config_id: string | null;
        direction: "inbound" | "outbound";
        from_address: string | null;
        to_address: string | null;
        subject: string | null;
        content: string | null;
        status: string;
        error_message: string | null;
        external_id: string | null;
        created_at: string;
      }>>(`/integrations/logs?${q}`);
    },
    oauthStart: (
      serverKey: string,
      opts?: { returnTo?: string; connectionId?: string },
    ) => {
      const q = new URLSearchParams();
      if (opts?.returnTo) q.set("return_to", opts.returnTo);
      if (opts?.connectionId) q.set("connection_id", opts.connectionId);
      return request<{
        authorize_url: string;
        state: string;
        server_key: string;
        source: "db" | "env";
      }>(`/integrations/oauth/${serverKey}/start${q.toString() ? `?${q}` : ""}`);
    },
    setOAuthConfig: (
      serverKey: string,
      data: { client_id: string; client_secret: string; scopes?: string },
    ) =>
      request<void>(
        `/integrations/mcp-servers/${serverKey}/oauth-config`,
        { method: "POST", body: JSON.stringify(data) },
      ),
  },

  browser: {
    createSession: () =>
      request<BrowserSession>("/browser/sessions", {
        method: "POST",
        body: JSON.stringify({}),
      }),
    listSessions: () => request<BrowserSession[]>("/browser/sessions"),
    getSession: (id: string) => request<BrowserSession>(`/browser/sessions/${id}`),
    navigate: (id: string, url: string) =>
      request<any>(`/browser/sessions/${id}/navigate`, {
        method: "POST",
        body: JSON.stringify({ url }),
      }),
    screenshot: async (id: string) => {
      const token = await getReadyAuthToken();
      const res = await fetch(`${API_BASE}/browser/sessions/${id}/screenshot`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` || "" },
      });
      if (!res.ok) throw new ApiError(res.status, "Screenshot failed");
      return res.blob();
    },
    action: (id: string, actionType: string, params: Record<string, any>) =>
      request<any>(`/browser/sessions/${id}/action`, {
        method: "POST",
        body: JSON.stringify({ action: actionType, ...params }),
      }),
    close: (id: string) =>
      request<void>(`/browser/sessions/${id}`, { method: "DELETE" }),
  },

  channels: {
    list: () => request<any[]>("/integrations/channels"),
    create: (data: any) =>
      request<any>("/integrations/channels", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    update: (id: string, data: any) =>
      request<any>(`/integrations/channels/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      request<void>(`/integrations/channels/${id}`, { method: "DELETE" }),
  },


  orders: {
    list: (params?: { status?: string; client_id?: string; limit?: number; offset?: number }) => {
      const q = new URLSearchParams();
      if (params?.status) q.set("status", params.status);
      if (params?.client_id) q.set("client_id", params.client_id);
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.offset) q.set("offset", String(params.offset));
      return request<PaginatedResponse<Order>>(`/orders?${q}`);
    },
    get: (id: string) => request<Order>(`/orders/${id}`),
    create: (data: Partial<Order>) =>
      request<Order>("/orders", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<Order>) =>
      request<Order>(`/orders/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      request<void>(`/orders/${id}`, { method: "DELETE" }),
    updateStatus: (id: string, status: string) =>
      request<Order>(`/orders/${id}/status`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      }),
    stats: () => request<OrderStats>("/orders/stats"),
    items: {
      list: (orderId: string) =>
        request<OrderItem[]>(`/orders/${orderId}/items`),
      add: (orderId: string, data: Partial<OrderItem>) =>
        request<OrderItem>(`/orders/${orderId}/items`, {
          method: "POST",
          body: JSON.stringify(data),
        }),
      update: (orderId: string, itemId: string, data: Partial<OrderItem>) =>
        request<OrderItem>(`/orders/${orderId}/items/${itemId}`, {
          method: "PUT",
          body: JSON.stringify(data),
        }),
      remove: (orderId: string, itemId: string) =>
        request<void>(`/orders/${orderId}/items/${itemId}`, { method: "DELETE" }),
    },
  },

  docgen: {
    formats: () => request<DocGenFormat[]>("/docgen/formats"),
    generate: async (title: string, content: string, format: string, options?: Record<string, any>) => {
      const token = await getReadyAuthToken();
      const res = await fetch(`${API_BASE}/docgen/generate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ title, content, format, options }),
      });
      return res.blob();
    },
    aiGenerate: async (prompt: string, format: string, options?: Record<string, any>) => {
      const token = await getReadyAuthToken();
      const res = await fetch(`${API_BASE}/docgen/ai-generate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ prompt, format, options }),
      });
      return res.blob();
    },
    preview: (title: string, content: string) =>
      request<{ html: string }>("/docgen/preview", {
        method: "POST",
        body: JSON.stringify({ title, content }),
      }),
  },

  apiKeys: {
    list: () => request<ApiKey[]>("/api-keys"),
    create: (data: { name: string; provider: string; key: string; base_url?: string; default_model?: string }) =>
      request<ApiKey>("/api-keys", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<ApiKey>) =>
      request<ApiKey>(`/api-keys/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/api-keys/${id}`, { method: "DELETE" }),
    rotate: (id: string, newKey: string) =>
      request<ApiKey>(`/api-keys/${id}/rotate`, { method: "POST", body: JSON.stringify({ key: newKey }) }),
    test: (id: string) =>
      request<{ success: boolean; model?: string }>(`/api-keys/${id}/test`, { method: "POST", body: JSON.stringify({}) }),
    resolve: () =>
      request<{ provider: string; model: string; has_key: boolean }>("/api-keys/resolve"),
  },

  webhooks: {
    list: () => request<WebhookEndpoint[]>("/webhooks"),
    create: (data: Partial<WebhookEndpoint>) =>
      request<WebhookEndpoint>("/webhooks", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<WebhookEndpoint>(`/webhooks/${id}`),
    update: (id: string, data: Partial<WebhookEndpoint>) =>
      request<WebhookEndpoint>(`/webhooks/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/webhooks/${id}`, { method: "DELETE" }),
    test: (id: string) =>
      request<any>(`/webhooks/${id}/test`, { method: "POST", body: JSON.stringify({}) }),
    deliveries: (id: string) =>
      request<WebhookDelivery[]>(`/webhooks/${id}/deliveries`),
  },

  customFields: {
    list: (resourceType?: string) => {
      const q = new URLSearchParams();
      if (resourceType) q.set("resource_type", resourceType);
      return request<CustomField[]>(`/custom-fields?${q}`);
    },
    create: (data: Partial<CustomField>) =>
      request<CustomField>("/custom-fields", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<CustomField>) =>
      request<CustomField>(`/custom-fields/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/custom-fields/${id}`, { method: "DELETE" }),
  },

  memories: {
    list: (agentId?: string) => {
      const q = new URLSearchParams();
      if (agentId) q.set("agent_id", agentId);
      return request<Memory[]>(`/memories?${q}`);
    },
    create: (data: { content: string; agent_id?: string; context?: string }) =>
      request<Memory>("/memories", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<Memory>) =>
      request<Memory>(`/memories/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/memories/${id}`, { method: "DELETE" }),
    archive: (id: string) =>
      request<any>(`/memories/${id}/archive`, { method: "POST", body: JSON.stringify({}) }),
    context: (agentId?: string) => {
      const q = new URLSearchParams();
      if (agentId) q.set("agent_id", agentId);
      return request<{ context: string }>(`/memories/context?${q}`);
    },
    extract: (text: string) =>
      request<Memory[]>("/memories/extract", { method: "POST", body: JSON.stringify({ text }) }),
  },

  favorites: {
    toggle: (resourceType: string, resourceId: string) =>
      request<any>("/favorites/toggle", {
        method: "POST",
        body: JSON.stringify({ resource_type: resourceType, resource_id: resourceId }),
      }),
    list: (resourceType?: string) => {
      const q = new URLSearchParams();
      if (resourceType) q.set("resource_type", resourceType);
      return request<Favorite[]>(`/favorites?${q}`);
    },
    check: (resourceType: string, resourceId: string) =>
      request<{ is_favorited: boolean }>(`/favorites/check?resource_type=${resourceType}&resource_id=${resourceId}`),
    pinned: () => request<Favorite[]>("/favorites/pinned"),
    counts: () => request<Record<string, number>>("/favorites/counts"),
  },

  tags: {
    list: () => request<Tag[]>("/tags"),
    create: (name: string, color?: string) =>
      request<Tag>("/tags", { method: "POST", body: JSON.stringify({ name, color }) }),
    delete: (id: string) =>
      request<void>(`/tags/${id}`, { method: "DELETE" }),
    apply: (tagId: string, resourceType: string, resourceId: string) =>
      request<any>("/tags/apply", {
        method: "POST",
        body: JSON.stringify({ tag_id: tagId, resource_type: resourceType, resource_id: resourceId }),
      }),
    remove: (tagId: string, resourceType: string, resourceId: string) =>
      request<any>("/tags/remove", {
        method: "POST",
        body: JSON.stringify({ tag_id: tagId, resource_type: resourceType, resource_id: resourceId }),
      }),
    forResource: (resourceType: string, resourceId: string) =>
      request<Tag[]>(`/tags/resource?resource_type=${resourceType}&resource_id=${resourceId}`),
    findByName: (name: string) =>
      request<any[]>(`/tags/find/${encodeURIComponent(name)}`),
    popular: () => request<Tag[]>("/tags/popular"),
  },

  reports: {
    tasks: (params?: Record<string, any>) => {
      const q = new URLSearchParams(params as Record<string, string>);
      return request<Report>(`/reports/tasks?${q}`);
    },
    usage: (params?: Record<string, any>) => {
      const q = new URLSearchParams(params as Record<string, string>);
      return request<Report>(`/reports/usage?${q}`);
    },
    activity: (params?: Record<string, any>) => {
      const q = new URLSearchParams(params as Record<string, string>);
      return request<Report>(`/reports/activity?${q}`);
    },
    tasksHtml: (params?: Record<string, any>) => {
      const q = new URLSearchParams(params as Record<string, string>);
      return request<string>(`/reports/tasks/html?${q}`);
    },
    usageHtml: (params?: Record<string, any>) => {
      const q = new URLSearchParams(params as Record<string, string>);
      return request<string>(`/reports/usage/html?${q}`);
    },
    email: (reportType: string, recipients: string[]) =>
      request<any>("/reports/email", {
        method: "POST",
        body: JSON.stringify({ report_type: reportType, recipients }),
      }),
  },

  backup: {
    summary: () => request<BackupSummary>("/backup/summary"),
    export: () => request<any>("/backup/export"),
    download: (format?: string) =>
      request<any>("/backup/export/download", {
        method: "POST",
        body: JSON.stringify({ format }),
      }),
  },


  presence: {
    get: (resourceType?: string, resourceId?: string) => {
      const q = new URLSearchParams();
      if (resourceType) q.set("resource_type", resourceType);
      if (resourceId) q.set("resource_id", resourceId);
      return request<PresenceInfo[]>(`/presence?${q}`);
    },
    viewers: (resourceType: string, resourceId: string) =>
      request<PresenceInfo[]>(`/presence/viewers?resource_type=${resourceType}&resource_id=${resourceId}`),
    typing: (resourceType: string, resourceId: string) =>
      request<PresenceInfo[]>(`/presence/typing?resource_type=${resourceType}&resource_id=${resourceId}`),
  },

  entities: {
    me: () => request<any>("/entities/me"),
    update: (data: Record<string, any>) =>
      request<any>("/entities/me", { method: "PUT", body: JSON.stringify(data) }),
  },

  // ── Entity Filesystem (JuiceFS) ──
  fs: {
    list: (path = ".", showSystem = false) =>
      request<{ items: any[]; path: string; count: number }>(`/fs/list?path=${encodeURIComponent(path)}&show_system=${showSystem}`),
    tree: (maxDepth = 3) =>
      request<{ tree: any[]; entity_id: string }>(`/fs/tree?max_depth=${maxDepth}`),
    read: (path: string, authToken?: string | null) =>
      request<{ content: string; encoding: string; path: string; size: number; mime_type: string }>(
        `/fs/read?path=${encodeURIComponent(path)}`,
        {},
        authToken,
      ),
    info: (path: string) =>
      request<any>(`/fs/info?path=${encodeURIComponent(path)}`),
    write: (path: string, content: string, options: FilesystemWriteOptions = {}) =>
      request<{ status: string; path: string }>(
        "/fs/write",
        {
          method: "POST",
          body: JSON.stringify({
            path,
            content,
            save_session_id: options.saveIntent?.sessionId,
            save_sequence: options.saveIntent?.sequence,
          }),
          signal: options.signal,
        },
        options.authToken,
      ),
    mkdir: (path: string) =>
      request<{ status: string; path: string }>("/fs/mkdir", { method: "POST", body: JSON.stringify({ path }) }),
    move: (src: string, dest: string) =>
      request<{ status: string; src: string; dest: string }>("/fs/move", { method: "POST", body: JSON.stringify({ src, dest }) }),
    delete: (path: string) =>
      request<{ status: string; path: string }>("/fs/delete", { method: "POST", body: JSON.stringify({ path }) }),
    upload: async (path: string, file: globalThis.File) => {
      const formData = new FormData();
      formData.append("file", file);
      const token = await getReadyAuthToken();
      return fetch(`${API_BASE}/fs/upload?path=${encodeURIComponent(path)}`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
      }).then(r => r.json());
    },
    search: (query: string, glob = "*.md", maxResults = 20) =>
      request<{ results: any[]; query: string; count: number }>(`/fs/search?query=${encodeURIComponent(query)}&glob=${encodeURIComponent(glob)}&max_results=${maxResults}`),
    wikiLinks: (path: string) =>
      request<{ links: any[]; file: string; count: number }>(`/fs/wiki-links?path=${encodeURIComponent(path)}`),
    lint: () =>
      request<any>("/fs/lint"),
  },

  templates: {
    list: () => request<TaskTemplateDetail[]>("/tasks/templates"),
    create: (data: Partial<TaskTemplateDetail>) =>
      request<TaskTemplateDetail>("/tasks/templates", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<TaskTemplateDetail>(`/tasks/templates/${id}`),
    update: (id: string, data: Partial<TaskTemplateDetail>) =>
      request<TaskTemplateDetail>(`/tasks/templates/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/tasks/templates/${id}`, { method: "DELETE" }),
    instantiate: (id: string, overrides?: Record<string, any>) =>
      request<any>(`/tasks/templates/${id}/instantiate`, {
        method: "POST",
        body: JSON.stringify(overrides || {}),
      }),
    recurring: (id: string, schedule: Record<string, any>) =>
      request<any>(`/tasks/templates/${id}/recurring`, {
        method: "POST",
        body: JSON.stringify(schedule),
      }),
  },
  sites: {
    forPath: (path: string) =>
      request<{
        publishable: boolean;
        target: string | null;
        site: SiteInfo | null;
        hosting_configured: boolean;
        auto_connection_plan: SiteAutoConnectionPlan | null;
        publish_snapshot_hash: string | null;
      }>(`/sites/for-path?path=${encodeURIComponent(path)}`),
    publish: (data: {
      path: string;
      name: string;
      auto_connect?: boolean;
      expected_snapshot_hash?: string;
    }) =>
      request<SitePublishResult>("/sites/publish", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    setStatus: (id: string, status: "active" | "offline") =>
      request<SiteInfo>(`/sites/${id}/status`, {
        method: "POST",
        body: JSON.stringify({ status }),
      }),
    bindDomain: (id: string, domain: string) =>
      request<SiteInfo>(`/sites/${id}/domain`, {
        method: "POST",
        body: JSON.stringify({ domain }),
      }),
    checkDomain: (id: string) =>
      request<SiteInfo>(`/sites/${id}/domain/check`, { method: "POST" }),
    unbindDomain: (id: string) =>
      request<SiteInfo>(`/sites/${id}/domain`, { method: "DELETE" }),
    getConnections: (id: string) =>
      request<SiteConnections>(`/sites/${id}/connections`),
    updateConnections: (id: string, data: Omit<SiteConnections, "site_id">) =>
      request<SiteInfo>(`/sites/${id}/connections`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    analytics: (id: string, days = 30) =>
      request<SiteAnalytics>(`/sites/${id}/analytics?days=${days}`),
  },
};

export interface SiteConnections {
  site_id: string;
  workspace_id: string | null;
  customer_service_channel_config_id: string | null;
  subscription_workflow_binding_id: string | null;
  lead_workflow_binding_id: string | null;
  analytics_enabled: boolean;
}

export interface SiteAutoConnectionPlan {
  eligible: boolean;
  can_auto_connect: boolean;
  workspace_id: string | null;
  workspace_name: string | null;
  features: {
    customer_service: boolean;
    lead_forms: number;
    subscription_forms: number;
    tracked_interactions: number;
    analytics: boolean;
  };
  actions: {
    customer_service: "create" | "reuse" | "not_available";
    lead_flow: "create" | "reuse" | "not_detected";
    subscription_flow: "create" | "reuse" | "not_detected";
    analytics: "enable";
  };
  reason: "no_workspace_origin" | "workspace_manage_required" | null;
}

export interface SiteAnalytics {
  site_id: string;
  days: number;
  analytics_enabled: boolean;
  page_views: number;
  unique_sessions: number;
  interactions: number;
  form_submissions: number;
  chat_opens: number;
  top_pages: Array<{ path: string; views: number }>;
}

export interface SiteInfo {
  id: string;
  name: string;
  slug: string;
  status: "active" | "offline";
  revision: number;
  url: string;
  platform_url: string;
  sites_domain: string;
  custom_domain: string | null;
  domain_status: "pending" | "active" | null;
  published_at: string | null;
  source_path: string;
  workspace_id: string | null;
  connections: Omit<SiteConnections, "site_id" | "workspace_id">;
}

export interface SitePublishResult extends SiteInfo {
  excluded: { path: string; sensitive: boolean }[];
  auto_connected: boolean;
}

export interface ClientRecord { id: string; name: string; email?: string; phone?: string; address?: string; status: string; metadata?: { crm?: Partial<CrmProfile> }; }
export interface ClientContactRecord extends Omit<ClientContactInput, "phone" | "job_title"> { id: string; user_id?: string | null; phone: string | null; job_title: string | null; }
export interface ClientDeliveryReceipt {
  id: string; client_id: string; contact_id: string; source_workspace_id: string; title: string;
  recipient_email: string; status: string; expires_at: string; invite_path?: string;
  target_workspace_id?: string; target_entity_id?: string; provider_access: "none" | "trial" | "maintainer"; can_manage?: boolean;
  /** The viewer can open this delivery's customer trial from the source Workspace. */
  can_open_trial?: boolean;
  phase?: string; acceptance?: Record<string, unknown>; maintenance?: CollaborationGrant | null;
  /** The release the delivery holds; accepted Workspaces upgrade toward it. */
  content_version?: string; published_at?: string | null;
  email_delivery?: { status?: "pending" | "sent" | "failed" | "disabled" | "unconfigured"; sent_at?: string; attempted_at?: string; locale?: string };
}

export type CrmLifecycle = "lead" | "prospect" | "customer" | "partner";
export type CrmStage = "qualified" | "discovery" | "proposal" | "negotiation" | "won" | "lost";
export interface CrmProfile {
  name: string; email: string; phone: string; address: string; industry: string; website: string;
  source: string; company_size: string; lifecycle: CrmLifecycle; owner_id: string | null;
  tags: string[]; notes: string; status: "active" | "inactive";
}
export interface CrmPage<T> { items: T[]; total: number; }
export interface CrmActivityInput { kind: "note" | "call" | "email" | "meeting"; subject: string; body: string; occurred_at: string; }
export interface CrmActivity extends Omit<CrmActivityInput, "kind"> { id: string; client_id: string; kind: CrmActivityInput["kind"] | "deal"; }
export interface CrmOpportunityInput { title: string; stage: CrmStage; amount: string; currency: string; probability: number; expected_close: string | null; owner_id: string | null; description: string; loss_reason: string; }
export interface CrmOpportunity extends CrmOpportunityInput { id: string; client_id: string; client_name?: string; }
export interface CrmFollowupInput { title: string; body: string; due_at: string; owner_id: string | null; priority: "low" | "normal" | "high"; status: "open" | "done"; }
export interface CrmFollowup extends CrmFollowupInput { id: string; client_id: string; client_name?: string; completed_at: string | null; }
export interface CrmOverview { clients: Partial<Record<CrmLifecycle, number>>; overdue: number; pipeline: {stage: CrmStage; currency: string; count: number; amount: string; weighted: string}[]; followups: CrmFollowup[]; }
export interface ClientContactInput { name: string; email: string; phone: string; job_title: string; is_primary: boolean; }


// days null keeps a grant without expiry (a kept maintainer) without one; updates only.
export interface CollaborationInvite { email: string; role: "tester" | "maintainer"; days: number | null; share_configuration: boolean; }
// A null expires_at never expires: the provider's trial grant and a kept maintainer.
// `team`: one provider-team member's grant of the delivery; the customer decides on the team as a whole.
export interface CollaborationGrant { id: string; workspace_id: string; delivery_id?: string; email: string; role: "tester" | "maintainer" | "provider_trial"; status: string; expires_at: string | null; share_configuration: boolean; path: string; team?: boolean; }
export interface ProviderTeamMember { grant_id: string; email: string; user_id?: string | null; role: CollaborationGrant["role"]; accepted_at?: string | null; }
export interface ProviderTeam { delivery_id: string; access: "trial" | "maintainer" | "none"; members: ProviderTeamMember[]; }
export interface CollaborationManagement { workspace_id: string; title: string; status: string; phase?: string; acceptance?: Record<string, unknown>; grants: CollaborationGrant[]; communication: CollaborationCommunication; group_options: CollaborationGroup[]; provider_team?: ProviderTeam | null; }
/** A delivery the provider team can open from its source Workspace (GET /workspaces/{id}/client-trials). */
export interface ClientTrial {
  delivery_id: string; client_id: string; client_name: string | null; customer_name: string | null; title: string | null;
  phase: string; access: "trial" | "maintainer"; target_workspace_id: string; target_entity_id: string; accepted_at: string | null;
}
/** A short-lived token for one customer Workspace, held only by the embedded frame. */
export interface CollaboratorSession {
  access_token: string; expires_at: string; entity_id: string; workspace_id: string; role: string;
  customer_name: string | null; phase: string | null; access: string;
}
export interface CollaborationPortal { grant: CollaborationGrant; title: string; configuration?: {revision: number; services: Record<string, unknown>[]; processes: Record<string, unknown>[]}; communication: CollaborationCommunication; }

export interface CollaborationGroup { id: string; title: string; path: string; }
export interface CollaborationCommunication { linked: boolean; group: CollaborationGroup | null; }
