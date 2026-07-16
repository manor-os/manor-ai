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
  BookingLink,
  BookingLinkWrite,
  DailyAgendaResponse,
  ExternalCalendarEventsResponse,
  PublicBookingLink,
  PublicBookingRequest,
  BookingConfirmation,
  ExecutionPlan,
  ExecutionStep,
  PlanRetryResponse,
  StepRetryResponse,
  SearchResult,
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

import { useToastStore } from "../stores/toast";
import { t } from "./i18n";
import {
  getAuthToken,
  USER_TOKEN_KEY,
} from "./authToken";
import { captureClientError } from "./clientErrors";

const API_BASE = "/api/v1";
const BACKEND_UNAVAILABLE_TOAST_ID = "backend-unavailable";

type DocumentListParams = {
  search?: string;
  folder_id?: string;
  scope?: "all";
  workspace_id?: string;
  include_generated_assets?: boolean;
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
};

function listDocuments(params?: DocumentListParams) {
  const q = new URLSearchParams();
  if (params?.search) q.set("search", params.search);
  if (params?.folder_id !== undefined) q.set("folder_id", params.folder_id);
  if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
  if (params?.include_generated_assets !== undefined) q.set("include_generated_assets", String(params.include_generated_assets));
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
  const locale = localStorage.getItem("manor_locale");
  return locale === "zh" || locale === "es" || locale === "en" ? locale : "en";
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

export interface HealthStatus {
  ok: boolean | null;
  detail: string | null;
  latency_ms: number | null;
  checked_at: string | null;
  wiring: WiringStatus | null;
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
    protocol_version?: number;
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
  service_key: string | null;
  custom_prompt: string | null;
  config: Record<string, unknown>;
  status: string;
  workers: SubscriptionWorkerBinding[];
  created_at: string | null;
  updated_at: string | null;
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
}

// ── Workspace Blueprint / Marketplace types (M12) ─────────────────────

export interface BlueprintSetupItem {
  label: string;
  key: string | null;
  kind: string | null;
  required: boolean;
  purpose: string | null;
  default: string | null;
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
  first_week_outputs: string[];
  validation_evidence: string[];
  acceptance_criteria: string[];
  not_included: string[];
  services: BlueprintSetupItem[];
  rules: string[];
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
  source_workspace_id: string | null;
  cover_image_url: string | null;
  setup_preview: BlueprintSetupPreview;
  created_at: string;
  updated_at: string | null;
  published_at: string | null;
  // Server-computed / read-only marketplace fields. Pricing is changed via
  // api.blueprints.setPricing, never via update().
  price_cents?: number | null;
  currency?: string;
  purchase_count?: number;
  has_share_token?: boolean;
  // Whether the current caller owns this blueprint row. False for built-in
  // marketplace blueprints and other tenants' published blueprints.
  is_owner?: boolean;
}

export interface BlueprintDetail extends BlueprintSummary {
  description: string | null;
  payload: Record<string, unknown>;
  purchased?: boolean;
}

// Mirrors backend UpdateBlueprintRequest (apps/api/routers/blueprints.py):
// only these fields are writable via PUT /blueprints/{id}.
export interface UpdateBlueprintRequest {
  title?: string;
  summary?: string;
  description?: string;
  tags?: string[];
  cover_image_url?: string;
}

export interface ExportBlueprintRequest {
  slug: string;
  title: string;
  summary?: string;
  description?: string;
  tags?: string[];
  cover_image_url?: string;
  author_handle?: string;
  author_display_name?: string;
  include_subscriptions?: boolean;
  include_goals?: boolean;
  include_scheduled_jobs?: boolean;
  include_custom_fields?: boolean;
  include_governance?: boolean;
  include_channel_requirements?: boolean;
  include_session_requirements?: boolean;
  include_memory_files?: boolean;
}

export type InstallMode = "simulate" | "live";
export type GovernancePresetKey = "safe" | "standard" | "aggressive";

export interface GovernancePolicy {
  never_allow_actions: string[];
  hitl_required_actions: string[];
  auto_approve_actions: string[];
  never_allow_capabilities: string[];
  hitl_required_capabilities: string[];
  auto_approve_capabilities: string[];
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

export interface InstallBlueprintRequest {
  mode?: InstallMode;
  workspace_name?: string;
  create_missing_agents?: boolean;
  governance_preset?: GovernancePresetKey;
  share_token?: string;
}

// Mirrors backend PurchaseStatusResponse (apps/api/routers/marketplace.py).
export interface PurchaseStatusResponse {
  purchase_id: string;
  blueprint_id: string;
  status: string;
  purchased_at?: string | null;
}

// Mirrors backend MerchantStatusResponse (apps/api/routers/merchant.py).
export interface MerchantStatusResponse {
  exists: boolean;
  onboarding_status?: string;
  charges_enabled: boolean;
  payouts_enabled: boolean;
}

export interface MerchantSaleItem {
  purchase_id: string;
  blueprint_id: string;
  blueprint_title: string;
  amount_cents: number;
  platform_fee_cents: number;
  seller_amount_cents: number;
  currency: string;
  status: string;
  purchased_at?: string | null;
}

export interface MerchantSalesResponse {
  items: MerchantSaleItem[];
  gross_cents: number;
  fees_cents: number;
  net_cents: number;
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
  goal_ids: string[];
  subscription_ids: string[];
  scheduled_job_ids: string[];
  custom_field_ids: string[];
  governance_applied: boolean;
  todos: InstallTodo[];
  notes: string[];
}

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
  activity: ActivitySection;
  cost: CostSection;
  counterfactuals: CounterfactualOutcome[];
  goals: GoalPace[];
  notes: string[];
}

let _sessionRedirecting = false;
/** A 401 from a normal API call means the session expired mid-use. Clear it and
 *  route to /login once — so an expired token never silently dead-ends an
 *  in-app action (e.g. clicking "add node" and nothing happening). The
 *  ``/auth/*`` endpoints legitimately 401 on bad credentials, so they're left to
 *  show their own inline error instead of redirecting. */
function handleSessionExpired(path: string) {
  if (typeof window === "undefined" || _sessionRedirecting) return;
  if (path.startsWith("/auth/")) return;
  if (window.location.pathname.startsWith("/login")) return;
  _sessionRedirecting = true;
  try {
    window.localStorage.removeItem(USER_TOKEN_KEY);
    window.sessionStorage.setItem("manor_session_expired", "1");
  } catch {
    /* storage may be unavailable */
  }
  const next = window.location.pathname + window.location.search;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
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
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
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
export type PlanLimitKind = "credit" | "storage" | "workspaces" | "users" | "generic";

/** Shape of FastAPI's plan-gate 402 response detail. */
export interface PlanLimitDetail {
  message: string;
  limit: number | null;
  current: number | null;
  plan: string;
  /** Limit type; absent on legacy payloads → treated as "credit". */
  kind?: PlanLimitKind;
}

type DocumentDownloadCacheEntry = {
  blob: Blob;
  expiresAt: number;
  lastAccessed: number;
  size: number;
};

type DocumentBlobOptions = { cache?: boolean; force?: boolean };
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
const PERSISTENT_THUMBNAIL_CACHE_TTL_MS = 14 * 24 * 60 * 60 * 1000;
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
    [id, `download:${id}`, `thumbnail:${id}`].forEach((key) => {
      documentDownloadCache.delete(key);
      documentDownloadInflight.delete(key);
    });
    invalidatePersistentThumbnailCache(id);
    return;
  }
  documentDownloadCache.clear();
  documentDownloadInflight.clear();
  invalidatePersistentThumbnailCache();
}

async function fetchProtectedBlob(
  cacheKey: string,
  url: string,
  options: DocumentBlobOptions = {},
): Promise<Blob> {
  const useCache = options.cache !== false;
  const now = Date.now();
  if (useCache && !options.force) {
    const cached = documentDownloadCache.get(cacheKey);
    if (cached && cached.expiresAt > now) {
      cached.lastAccessed = now;
      return cached.blob;
    }
    if (cached) documentDownloadCache.delete(cacheKey);

    const inflight = documentDownloadInflight.get(cacheKey);
    if (inflight) return inflight;
  }

  const token = getAuthToken();
  const promise = fetch(url, {
    cache: "no-store",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).then(async (res) => {
    if (!res.ok) throw new ApiError(res.status, "Download failed");
    const blob = await res.blob();
    if (useCache) {
      documentDownloadCache.set(cacheKey, {
        blob,
        expiresAt: Date.now() + DOCUMENT_DOWNLOAD_CACHE_TTL_MS,
        lastAccessed: Date.now(),
        size: blob.size,
      });
      pruneDocumentDownloadCache(cacheKey);
    }
    return blob;
  }).finally(() => {
    documentDownloadInflight.delete(cacheKey);
  });

  if (useCache) documentDownloadInflight.set(cacheKey, promise);
  return promise;
}

function cacheBustUrl(url: string): string {
  return `${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`;
}

async function fetchProtectedDocumentBlob(
  cacheKey: string,
  path: string,
  options: DocumentBlobOptions = {},
): Promise<Blob> {
  return fetchProtectedBlob(cacheKey, cacheBustUrl(`${API_BASE}${path}`), options);
}

async function fetchDocumentBlob(id: string, options: DocumentBlobOptions = {}): Promise<Blob> {
  return fetchProtectedDocumentBlob(`download:${id}`, `/documents/${id}/download`, options);
}

async function fetchDocumentThumbnailBlob(id: string, options: DocumentBlobOptions = {}): Promise<Blob> {
  return fetchProtectedDocumentBlob(`thumbnail:${id}`, `/documents/${id}/thumbnail`, options);
}

async function fetchDocumentThumbnailUrl(id: string, options: DocumentThumbnailOptions = {}): Promise<string> {
  const useCache = options.cache !== false;
  const usePersistentCache = useCache && options.persistent !== false;
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
  const usePersistentCache = useCache && options.persistent !== false;
  if (usePersistentCache && !options.force) {
    const cached = readPersistentThumbnailDataUrl(id, options.version);
    if (cached) return cached;
  }

  const blob = await fetchDocumentBlob(id, options);
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
  const imageUrl = URL.createObjectURL(blob);
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
  const usePersistentCache = useCache && options.persistent !== false;
  if (usePersistentCache && !options.force) {
    const cached = readPersistentThumbnailDataUrl(id, options.version);
    if (cached) return cached;
  }

  const blob = await fetchDocumentBlob(id, options);
  if (usePersistentCache) {
    const captured = await captureDocumentImageThumbnail(blob).catch(() => null);
    const dataUrl = captured
      ? writePersistentThumbnailDataUrl(id, options.version, captured)
      : await writePersistentThumbnailBlob(id, options.version, blob);
    if (dataUrl) return dataUrl;
  }
  return URL.createObjectURL(blob);
}

async function fetchDocumentPresentationThumbnailUrl(id: string, options: DocumentThumbnailOptions = {}): Promise<string> {
  const useCache = options.cache !== false;
  const usePersistentCache = useCache && options.persistent !== false;
  if (usePersistentCache && !options.force) {
    const cached = readPersistentThumbnailDataUrl(id, options.version);
    if (cached) return cached;
  }

  const slideData = await request<{ slides: { index: number; url: string }[]; total: number }>(`/documents/${id}/slides`);
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

const PLAN_LIMIT_KINDS: PlanLimitKind[] = ["credit", "storage", "workspaces", "users", "generic"];

function normalizePlanLimitDetail(detail: unknown, fallback: string): PlanLimitDetail {
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
    };
  }
  return {
    message: typeof detail === "string" && detail ? detail : fallback,
    limit: null,
    current: null,
    plan: "current",
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

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getAuthToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Language": getStoredLocale(),
    ...((options.headers as Record<string, string>) || {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const suppressErrorToast = headers["X-Silent-Error"] === "1";
  if (suppressErrorToast) delete headers["X-Silent-Error"];

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers,
      cache: options.cache ?? "no-store",
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    showBackendUnavailableToast();
    captureClientError(error, {
      handled: true,
      mechanism: "api.network",
      tags: {
        method: options.method || "GET",
        path,
      },
    });
    throw error;
  }

  if (!res.ok) {
    if (isBackendUnavailableStatus(res.status)) {
      showBackendUnavailableToast();
      const err = new ApiError(res.status, t("lib.api.backend_unavailable"));
      captureClientError(err, {
        handled: true,
        mechanism: "api.http",
        tags: {
          method: options.method || "GET",
          path,
          status: res.status,
        },
      });
      throw err;
    }

    const body = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = body.detail;

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
    const coded = _extractCodedDetail(detail);
    const displayMessage = coded.code
      ? t(coded.code, coded.vars) || message
      : message;
    // Expired session mid-use → clear + route to login (once), so an in-app
    // action never silently dead-ends on a stale token.
    if (res.status === 401) handleSessionExpired(path);
    // Show error toast for non-auth failures
    if (!suppressErrorToast && res.status !== 401 && res.status !== 403) {
      useToastStore.getState().error(t("lib.api.request_failed"), displayMessage);
    }
    const err = new ApiError(res.status, message);
    if (coded.code) err.code = coded.code;
    if (coded.vars) err.vars = coded.vars;
    if (typeof detail === "object" && detail !== null) err.detail = detail as Record<string, unknown>;
    throw err;
  }

  clearBackendUnavailableToast();

  if (res.status === 204) return undefined as T;
  return res.json();
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
  const token = getAuthToken();
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
): Promise<any> {
  const token = getAuthToken();
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({}),
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
      if (event === "node") onNode(parsed.id, parsed.status);
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

export async function resolveDisplayMediaUrl(url: string): Promise<{ url: string; revoke: () => void }> {
  if (!isLocalFsUrl(url)) return { url, revoke: () => {} };
  const blob = await fetchProtectedBlob(`media:${url}`, cacheBustUrl(url));
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
  onDone?: (final: WorkspaceDraftFinalize & { strategist_eta_seconds?: number }) => void;
  onError?: (message: string) => void;
}

async function _streamFinalizeSSE(
  path: string,
  handlers: WorkspaceFinalizeStreamHandlers,
): Promise<WorkspaceDraftFinalize> {
  const token = getAuthToken();
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
  let streamError: string | null = null;
  // Steps that flow through ``onProgress`` rather than start/done/error.
  const KNOWN_STEPS = new Set([
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
        streamError = (payload.message as string) || "Unknown finalize error";
        handlers.onError?.(streamError);
      }
    }
  }

  if (streamError) throw new Error(streamError);
  if (!finalPayload) throw new Error("Finalize ended without a final payload");
  return finalPayload;
}


async function _streamDraftSSE(
  path: string,
  body: Record<string, unknown>,
  handlers: WorkspaceDraftStreamHandlers,
): Promise<WorkspaceDraftTurn> {
  const token = getAuthToken();
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

export const api = {
  auth: {
    register: (data: {
      email: string;
      password: string;
      entity_name?: string;
      invitation_code?: string;
      invite_token?: string;
    }) =>
      request<{ access_token: string; user_id: string; entity_id: string; role: string }>("/auth/register", {
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
    verifyEmail: (email: string, code: string) =>
      request<{ access_token: string; user_id: string; entity_id: string; role: string }>("/auth/verify-email", {
        method: "POST",
        body: JSON.stringify({ email, code }),
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
    uploadAvatar: (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      const token = getAuthToken();
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
    saveCustomModel: (data: { role: string; model: string; api_key?: string; use_saved_api_key?: boolean; base_url?: string; test_token?: string | null }) =>
      request<{ detail: string; models: Record<string, string>; masked: string }>("/auth/me/models/custom", {
        method: "PUT", body: JSON.stringify(data),
      }),
    getModelCatalog: () =>
      request<{ catalog: Record<string, any[]>; defaults: Record<string, string> }>("/auth/models/catalog"),
    getMyModels: () =>
      request<{ models: Record<string, string>; user_models: Record<string, string>; entity_models: Record<string, string> }>("/auth/me/models"),
    updateMyModels: (data: { models: Record<string, string> }) =>
      request<{ models: Record<string, string> }>("/auth/me/models", {
        method: "PUT", body: JSON.stringify(data),
      }),
    oauthGoogle: (opts: { code?: string; redirectUri: string; invitationCode?: string; teamInviteToken?: string; oauthSession?: string; publicChatToken?: string }) =>
      request<{ access_token: string; user: User }>("/auth/oauth/google", {
        method: "POST",
        body: JSON.stringify({
          code: opts.code,
          redirect_uri: opts.redirectUri,
          ...(opts.invitationCode ? { invitation_code: opts.invitationCode } : {}),
          ...(opts.teamInviteToken ? { team_invite_token: opts.teamInviteToken } : {}),
          ...(opts.oauthSession ? { oauth_session: opts.oauthSession } : {}),
          ...(opts.publicChatToken ? { public_chat_token: opts.publicChatToken } : {}),
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
    list: (params?: { status?: string; limit?: number; offset?: number; parent_task_id?: string; workspace_id?: string; category_id?: string }) => {
      const q = new URLSearchParams();
      if (params?.status) q.set("status", params.status);
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.offset) q.set("offset", String(params.offset));
      if (params?.parent_task_id) q.set("parent_task_id", params.parent_task_id);
      if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
      if (params?.category_id) q.set("category_id", params.category_id);
      return request<PaginatedResponse<Task>>(`/tasks?${q}`);
    },
    get: (id: string) => request<Task>(`/tasks/${id}`),
    create: (data: Partial<Task>) =>
      request<Task>("/tasks", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<Task>) =>
      request<Task>(`/tasks/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    updateStatus: (id: string, status: string) =>
      request<Task>(`/tasks/${id}/status`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      }),
    delete: (id: string) =>
      request<void>(`/tasks/${id}`, { method: "DELETE" }),
    board: (workspaceId?: string) => {
      const q = workspaceId ? `?workspace_id=${workspaceId}` : "";
      return request<Record<string, Task[]>>(`/tasks/board${q}`);
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
    respondHITL: (taskId: string, data: { response?: string; choice?: string; fields?: Record<string, any>; note?: string }) =>
      request<TaskHITLResponse>(`/tasks/${taskId}/hitl-response`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    decideApproval: (taskId: string, data: { choice: string; note?: string }) =>
      request<Task>(`/tasks/${taskId}/approval`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    escalate: (taskId: string) =>
      request<any>(`/tasks/${taskId}/escalate`, { method: "POST" }),
    logs: (taskId: string) =>
      request<any[]>(`/tasks/${taskId}/logs`),
    addLog: (taskId: string, content: string, logType: string = "comment", attachments?: any[], mentions?: { type: "agent" | "user"; id: string }[]) =>
      request<any>(`/tasks/${taskId}/logs`, { method: "POST", body: JSON.stringify({ content, log_type: logType, attachments: attachments || [], mentions: mentions || [] }) }),
    uploadAttachment: async (taskId: string, file: File) => {
      const token = getAuthToken();
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
    reassign: (taskId: string) =>
      request<any>(`/tasks/${taskId}/reassign`, { method: "POST" }),
    fromTemplate: (templateId: string, overrides?: Partial<Task>) =>
      request<Task>("/tasks/from-template", {
        method: "POST",
        body: JSON.stringify({ template_id: templateId, ...overrides }),
      }),
    checklist: {
      list: (taskId: string) =>
        request<any[]>(`/tasks/${taskId}/checklist`),
      add: (taskId: string, content: string, sortOrder?: number) =>
        request<any>(`/tasks/${taskId}/checklist`, {
          method: "POST",
          body: JSON.stringify({ content, sort_order: sortOrder }),
        }),
      toggle: (taskId: string, itemId: string, isCompleted: boolean) =>
        request<any>(`/tasks/${taskId}/checklist/${itemId}`, {
          method: "PUT",
          body: JSON.stringify({ is_completed: isCompleted }),
        }),
    },
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
    events: (start: string, end: string) => {
      const q = new URLSearchParams({ start, end });
      return request<ExternalCalendarEventsResponse>(`/calendar-settings/events?${q}`);
    },
    publicBookingLink: (slug: string, ownerId?: string) =>
      request<PublicBookingLink>(
        ownerId
          ? `/calendar-settings/public/booking-links/u/${encodeURIComponent(ownerId)}/${encodeURIComponent(slug)}`
          : `/calendar-settings/public/booking-links/${encodeURIComponent(slug)}`,
      ),
    bookPublicBookingLink: (slug: string, data: PublicBookingRequest, ownerId?: string) =>
      request<BookingConfirmation>(
        `${ownerId
          ? `/calendar-settings/public/booking-links/u/${encodeURIComponent(ownerId)}/${encodeURIComponent(slug)}`
          : `/calendar-settings/public/booking-links/${encodeURIComponent(slug)}`}/book`,
        { method: "POST", body: JSON.stringify(data) },
      ),
  },

  chat: {
    stream: async (
      message: string,
      conversationId?: string,
      opts?: {
        files?: File[];
        documentIds?: string[];
        manualSkillIds?: string[];
        agentId?: string;
        workspaceId?: string;
        workspaceContext?: boolean;
        threadRef?: { kind: "task" | "plan" | "goal"; id: string };
        chatMode?: string;
        chatModePayload?: Record<string, unknown>;
        disableTools?: boolean;
        blockedTools?: string[];
        editorContext?: {
          path?: string | null;
          sourcePath?: string | null;
          documentId?: string | null;
          documentName?: string | null;
          fileType?: string | null;
          mimeType?: string | null;
          editorType?: string | null;
          supportsImageGeneration?: boolean | null;
          currentDocumentContent?: string | null;
        };
        ephemeral?: boolean;
      },
    ): Promise<Response> => {
      const token = getAuthToken();
      const form = new FormData();
      form.append("message", message);
      if (conversationId) form.append("conversation_id", conversationId);
      if (opts?.agentId) form.append("agent_id", opts.agentId);
      if (opts?.workspaceContext) form.append("workspace_context", "true");
      if (opts?.workspaceContext && opts?.workspaceId) form.append("workspace_id", opts.workspaceId);
      if (opts?.threadRef?.kind) form.append("thread_ref_kind", opts.threadRef.kind);
      if (opts?.threadRef?.id) form.append("thread_ref_id", opts.threadRef.id);
      if (opts?.chatMode) form.append("chat_mode", opts.chatMode);
      if (opts?.chatModePayload) form.append("chat_mode_payload", JSON.stringify(opts.chatModePayload));
      if (opts?.documentIds?.length) form.append("document_ids", opts.documentIds.join(","));
      if (opts?.manualSkillIds?.length) form.append("manual_skill_ids", opts.manualSkillIds.join(","));
      if (opts?.disableTools) form.append("disable_tools", "true");
      if (opts?.blockedTools?.length) form.append("blocked_tools", opts.blockedTools.join(","));
      if (opts?.editorContext) form.append("editor_context", JSON.stringify(opts.editorContext));
      if (opts?.ephemeral) form.append("ephemeral", "true");
      if (opts?.files) opts.files.forEach((f) => form.append("files", f));
      const response = await fetch(`${API_BASE}/chat/stream`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: form,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({ detail: response.statusText }));
        const detail = body.detail;
        if (response.status === 402) {
          const limitDetail = normalizePlanLimitDetail(detail, body.error || t("component.upgrade_prompt.default_message"));
          const err = new ApiError(response.status, limitDetail.message);
          err.detail = limitDetail as unknown as Record<string, unknown>;
          throw err;
        }
        const message = (typeof detail === "string" ? detail : (detail as any)?.message) || response.statusText;
        const coded = _extractCodedDetail(detail);
        const displayMessage = coded.code ? t(coded.code, coded.vars) || message : message;
        if (response.status !== 401) {
          useToastStore.getState().error(t("lib.api.chat_failed"), displayMessage);
        }
        const err = new ApiError(response.status, message);
        if (coded.code) err.code = coded.code;
        if (coded.vars) err.vars = coded.vars;
        if (typeof detail === "object" && detail !== null) err.detail = detail as Record<string, unknown>;
        throw err;
      }
      return response;
    },
    listConversations: (wsId?: string) =>
      request<Conversation[]>(`/chat/conversations${wsId ? `?workspace_id=${wsId}` : ""}`),
    getMessages: (convId: string, opts?: { silent?: boolean; limit?: number }) =>
      request<Message[]>(`/chat/conversations/${convId}/messages?limit=${encodeURIComponent(String(opts?.limit ?? 500))}`, {
        headers: opts?.silent ? { "X-Silent-Error": "1" } : undefined,
      }),
    feedback: (
      convId: string,
      messageId: string,
      body: { rating: "up" | "down"; content_preview?: string; request_preview?: string },
    ) =>
      request<{ message_id: string; rating: "up" | "down"; updated_at: string | null }>(
        `/chat/conversations/${convId}/messages/${messageId}/feedback`,
        { method: "POST", body: JSON.stringify(body) },
      ),
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
    deleteConversation: (id: string) =>
      request<void>(`/chat/conversations/${id}`, { method: "DELETE" }),
    tts: async (text: string, voice?: string): Promise<ArrayBuffer> => {
      const token = getAuthToken();
      const resp = await fetch(`${API_BASE}/chat/tts`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice: voice || "alloy" }),
      });
      if (!resp.ok) throw new Error(`TTS failed: ${resp.status}`);
      return resp.arrayBuffer();
    },
    voiceSession: async (opts: {
      agent_id?: string;
      voice?: string;
      conversation_id?: string;
      workspace_id?: string;
    }): Promise<{
      ephemeral_key: string;
      expires_at: number;
      model: string;
      conversation_id: string;
      session_id: string;
    }> => {
      const token = getAuthToken();
      const resp = await fetch(`${API_BASE}/chat/voice-session`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify(opts),
      });
      if (!resp.ok) throw new Error(`Voice session failed: ${resp.status}`);
      return resp.json();
    },
    voiceSave: async (conversationId: string, turns: { role: string; content: string }[]) => {
      const token = getAuthToken();
      await fetch(`${API_BASE}/chat/voice-save`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: conversationId, turns }),
      });
    },
  },

  documents: {
    list: listDocuments,
    listAll: listAllDocuments,
    browse: browseDocuments,
    move: (id: string, folderId: string | null) =>
      request<Document>(`/documents/${id}/move`, { method: "POST", body: JSON.stringify({ folder_id: folderId }) }),
    get: (id: string) => request<Document>(`/documents/${id}`),
    upload: (
      file: File,
      folderId?: string | null,
      options?: {
        visibility?: string;
        classification?: string;
        client_visible?: boolean;
      },
    ): Promise<Document> => {
      const token = getAuthToken();
      const form = new FormData();
      form.append("file", file);
      const params = new URLSearchParams();
      if (folderId) params.set("folder_id", folderId);
      if (options?.visibility) params.set("visibility", options.visibility);
      if (options?.classification) params.set("classification", options.classification);
      if (options?.client_visible != null) params.set("client_visible", String(options.client_visible));
      const q = params.toString();
      return fetch(`${API_BASE}/documents/upload${q ? `?${q}` : ""}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` || "" },
        body: form,
      }).then((r) => r.json() as Promise<Document>);
    },
    delete: async (id: string) => {
      const result = await request<void>(`/documents/${id}`, { method: "DELETE" });
      invalidateDocumentDownloadCache(id);
      return result;
    },
    rename: (id: string, name: string) =>
      request<Document>(`/documents/${id}`, { method: "PUT", body: JSON.stringify({ name }) }),
    getContent: (id: string) =>
      request<{ content: string }>(`/documents/${id}/content`),
    saveContent: async (id: string, content: string) => {
      const result = await request<{ saved: boolean }>(`/documents/${id}/content`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      invalidateDocumentDownloadCache(id);
      return result;
    },
    replaceFile: async (id: string, file: File): Promise<Document> => {
      const token = getAuthToken();
      const form = new FormData();
      form.append("file", file);
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
      invalidateDocumentDownloadCache(id);
      return res.json() as Promise<Document>;
    },
    getVersions: (id: string) => request<any[]>(`/documents/${id}/versions`),
    download: async (id: string, options?: DocumentBlobOptions): Promise<string> => {
      const blob = await fetchDocumentBlob(id, options);
      return URL.createObjectURL(blob);
    },
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
    trash: (id: string) => request<void>(`/documents/${id}/trash`, { method: "POST" }),
    restore: (id: string) => request<void>(`/documents/${id}/restore`, { method: "POST" }),
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
    syncGoogleDrive: (docId: string, data: {
      file_id: string; name: string; mime_type?: string;
      file_size?: number; modified_time?: string; access_token: string;
    }) =>
      request<{ status: string }>(`/documents/${docId}/sync-google-drive`, {
        method: "POST", body: JSON.stringify(data),
      }),
  },

  folders: {
    list: () => request<DocumentFolderInfo[]>("/documents/folders"),
    tree: () => request<DocumentFolderInfo[]>("/documents/folder-tree"),
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
        expires_in_days?: number;
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
        expires_in_days?: number;
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

  // ── permissions_v1: classify / visibility / legal-hold / request-access
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
    setLegalHold: (docId: string, enabled: boolean, reason?: string) =>
      request<{ id: string; legal_hold: boolean }>(
        `/permissions/documents/${docId}/legal-hold`,
        { method: "POST", body: JSON.stringify({ enabled, reason }) },
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
        expires_in_days?: number;
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

  executions: {
    list: (params?: { agent_id?: string; task_id?: string; limit?: number }) => {
      const q = new URLSearchParams();
      if (params?.agent_id) q.set("agent_id", params.agent_id);
      if (params?.task_id) q.set("task_id", params.task_id);
      if (params?.limit) q.set("limit", String(params.limit));
      return request<{ items: any[]; total: number }>(`/executions?${q}`);
    },
  },

  plans: {
    list: (params?: { workspace_id?: string; task_id?: string; status?: string; limit?: number }) => {
      const q = new URLSearchParams();
      if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
      if (params?.task_id) q.set("task_id", params.task_id);
      if (params?.status) q.set("status", params.status);
      if (params?.limit) q.set("limit", String(params.limit));
      return request<ExecutionPlan[]>(`/plans?${q}`);
    },
    get: (id: string) => request<ExecutionPlan>(`/plans/${id}`),
    steps: (id: string) => request<ExecutionStep[]>(`/plans/${id}/steps`),
    approve: (id: string) =>
      request<ExecutionPlan>(`/plans/${id}/approve`, { method: "POST" }),
    retryFailedSteps: (id: string, note?: string) =>
      request<PlanRetryResponse>(`/plans/${id}/retry-failed-steps`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),
    retryStep: (stepId: string, note?: string) =>
      request<StepRetryResponse>(`/plans/steps/${stepId}/retry`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),
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

  people: {
    me: () => request<PeopleContext>("/people/me"),
    directory: () => request<PeopleDirectoryEntry[]>("/people/directory"),
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
    get: (id: string) => request<Workspace>(`/workspaces/${id}`),
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
      list: (wsId: string) => request<any[]>(`/workspaces/${wsId}/agents`),
      map: (wsId: string, data: { service_key: string; agent_id: string; custom_prompt?: string }) =>
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
      remove: (wsId: string, staffId: string) =>
        request<void>(`/workspaces/${wsId}/staff/${staffId}`, { method: "DELETE" }),
    },
    channels: (wsId: string) => request<any[]>(`/workspaces/${wsId}/channels`),
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
      removeDocument: (wsId: string, groupId: string, documentId: string) =>
        request<void>(`/workspaces/${wsId}/documents/groups/${groupId}/members/${documentId}`, { method: "DELETE" }),
    },
    pause: (wsId: string) => request<any>(`/workspaces/${wsId}/pause`, { method: "POST" }),
    resume: (wsId: string) => request<any>(`/workspaces/${wsId}/resume`, { method: "POST" }),
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
    setup: {
      turn: (wsId: string, data: { session_id?: string; message: string }) =>
        request<any>(`/workspaces/${wsId}/setup/turn`, { method: "POST", body: JSON.stringify(data) }),
      finalize: (wsId: string, sessionId: string) =>
        request<any>(`/workspaces/${wsId}/setup/finalize`, { method: "POST", body: JSON.stringify({ session_id: sessionId }) }),
    },
    // ── Workspace Blueprint / Marketplace (M12) ──────────────────────
    exportBlueprint: (wsId: string, data: ExportBlueprintRequest) =>
      request<BlueprintDetail>(
        `/workspaces/${wsId}/export-blueprint`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    promote: (wsId: string, force = false) =>
      request<PromoteResponse>(
        `/workspaces/${wsId}/promote`,
        { method: "POST", body: JSON.stringify({ force }) },
      ),
    promotePreflight: (wsId: string) =>
      request<UnmetRequirement[]>(`/workspaces/${wsId}/promote/preflight`),
    simulationReport: (wsId: string) =>
      request<SimulationReport>(`/workspaces/${wsId}/simulation-report`),
    // ── Workspace Chat ──────────────────────────────────────────────
    chat: {
      listMessages: (wsId: string, opts?: { thread_ref_kind?: string; thread_ref_id?: string; limit?: number }) => {
        const q = new URLSearchParams();
        if (opts?.thread_ref_kind) q.set("thread_ref_kind", opts.thread_ref_kind);
        if (opts?.thread_ref_id) q.set("thread_ref_id", opts.thread_ref_id);
        if (opts?.limit) q.set("limit", String(opts.limit));
        return request<any[]>(`/workspaces/${wsId}/chat/messages?${q}`);
      },
      postMessage: (wsId: string, body: string, threadRef?: { kind: string; id: string }) =>
        request<any>(`/workspaces/${wsId}/chat/messages`, {
          method: "POST",
          body: JSON.stringify({ body, thread_ref_kind: threadRef?.kind, thread_ref_id: threadRef?.id }),
        }),
      resolveAction: (wsId: string, msgId: string, choice: string, note?: string, payload?: Record<string, any>) =>
        request<any>(`/workspaces/${wsId}/chat/messages/${msgId}/resolve`, {
          method: "POST",
          body: JSON.stringify({ choice, note, payload }),
        }),
      feedback: (wsId: string, msgId: string, rating: "up" | "down") =>
        request<any>(`/workspaces/${wsId}/chat/messages/${msgId}/feedback`, {
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
      data: { initial_brief?: string } = {},
      handlers: WorkspaceDraftStreamHandlers = {},
    ) =>
      _streamDraftSSE(
        "/workspace-drafts/stream",
        data,
        handlers,
      ),
    /** Stream the assistant's reply to one user message (for an existing draft). */
    sendMessageStream: (
      id: string,
      message: string,
      handlers: WorkspaceDraftStreamHandlers = {},
    ) =>
      _streamDraftSSE(
        `/workspace-drafts/${id}/messages/stream`,
        { message },
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
    submitReview: (id: string, data?: { note?: string }) =>
      request<BlueprintSummary>(
        `/blueprints/${id}/submit-review`,
        { method: "POST", body: JSON.stringify(data ?? {}) },
      ),
    delete: (id: string) =>
      request<void>(`/blueprints/${id}`, { method: "DELETE" }),
    install: (id: string, data: InstallBlueprintRequest) =>
      request<InstallBlueprintResponse>(
        `/blueprints/${id}/install`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    installPayload: (data: {
      payload: Record<string, any>;
      mode?: InstallMode;
      workspace_name?: string;
      create_missing_agents?: boolean;
      governance_preset?: GovernancePresetKey;
    }) =>
      request<InstallBlueprintResponse>(
        "/blueprints/install-payload",
        { method: "POST", body: JSON.stringify(data) },
      ),
    governancePresets: () =>
      request<GovernancePresetSummary[]>("/blueprints/governance-presets"),
    setPricing: (id: string, data: { price_cents: number }) =>
      request<BlueprintSummary>(`/blueprints/${id}/pricing`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    createShareToken: (id: string) =>
      request<{ share_token: string }>(`/blueprints/${id}/share-token`, {
        method: "POST",
      }),
    revokeShareToken: (id: string) =>
      request<void>(`/blueprints/${id}/share-token`, { method: "DELETE" }),
    resolveShared: (token: string) =>
      request<BlueprintDetail>(`/blueprints/shared/${encodeURIComponent(token)}`),
  },

  marketplace: {
    checkout: (blueprintId: string) =>
      request<{ checkout_url: string }>(
        `/marketplace/blueprints/${blueprintId}/checkout`,
        { method: "POST" },
      ),
    purchaseBySession: (sessionId: string) =>
      request<PurchaseStatusResponse>(
        `/marketplace/purchases/by-session/${encodeURIComponent(sessionId)}`,
      ),
  },

  merchant: {
    onboard: () =>
      request<{ onboarding_url: string }>("/merchant/onboard", {
        method: "POST",
      }),
    status: () => request<MerchantStatusResponse>("/merchant/status"),
    sales: () => request<MerchantSalesResponse>("/merchant/sales"),
  },

  notifications: {
    list: (params?: { unread_only?: boolean }) => {
      const q = new URLSearchParams();
      if (params?.unread_only) q.set("unread_only", "true");
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
    exportTasksCsv: (status?: string) => {
      const token = getAuthToken();
      const q = status ? `?status=${status}` : "";
      return fetch(`${API_BASE}/bulk/export/tasks${q}`, {
        headers: { Authorization: `Bearer ${token}` || "" },
      }).then((r) => r.text());
    },
    importTasksCsv: (file: File) => {
      const token = getAuthToken();
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
      request<any[]>(`/usage/by-source?days=${days || 30}&scope=${scope}`),
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
      request<{ enabled: boolean; backup_codes: string[] }>("/auth/2fa/verify", {
        method: "POST",
        body: JSON.stringify({ code }),
      }),
    disable: (code: string) =>
      request<void>("/auth/2fa/disable", { method: "POST", body: JSON.stringify({ code }) }),
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
    list: (params?: { enabled_only?: boolean; workspace_id?: string; limit?: number }) => {
      const q = new URLSearchParams();
      if (params?.enabled_only) q.set("enabled_only", "true");
      if (params?.workspace_id) q.set("workspace_id", params.workspace_id);
      if (params?.limit) q.set("limit", String(params.limit));
      return request<{ items: any[]; total: number }>(`/jobs?${q}`);
    },
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
    runNow: (jobId: string) =>
      request<{ job_id: string; queued_at: string }>(
        `/jobs/${jobId}/run_now`, { method: "POST" },
      ),
  },

  search: {
    global: (query: string, limit?: number) => {
      const q = new URLSearchParams({ query });
      if (limit) q.set("limit", String(limit));
      return request<SearchResult[]>(`/search?${q}`);
    },
  },

  workflows: {
    list: () => request<any[]>("/workflows"),
    create: (data: any) =>
      request<any>("/workflows", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<any>(`/workflows/${id}`),
    update: (id: string, data: any) =>
      request<any>(`/workflows/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/workflows/${id}`, { method: "DELETE" }),
    startRun: (id: string, data?: any) =>
      request<any>(`/workflows/${id}/run`, { method: "POST", body: JSON.stringify(data || {}) }),
    // Streaming run — lights up the canvas node-by-node as it executes.
    runStream: (id: string, onNode: (nodeId: string, status: string) => void) =>
      streamWorkflowRun(`/workflows/${id}/run-stream`, onNode),
    runs: (id: string) => request<any[]>(`/workflows/${id}/runs`),
    getRun: (runId: string) => request<any>(`/workflows/runs/${runId}`),
    executeStep: (runId: string) =>
      request<any>(`/workflows/runs/${runId}/step`, { method: "POST" }),
    // Run a single node standalone (test a node's config without the workflow).
    runNode: (step: any, variables?: Record<string, any>) =>
      request<{ status: string; output: any; error?: string }>("/workflows/run-node", {
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
    listBindings: (params?: { workspace_id?: string; business_line?: string }) => {
      const q = new URLSearchParams(
        Object.entries(params || {}).filter(([, v]) => v) as [string, string][],
      ).toString();
      return request<any[]>(`/workflows/bindings${q ? `?${q}` : ""}`);
    },
    createBinding: (data: any) =>
      request<any>("/workflows/bindings", { method: "POST", body: JSON.stringify(data) }),
    trigger: (data: {
      trigger_type?: string;
      event_name?: string;
      workspace_id?: string;
      trigger_data?: any;
    }) => request<any>("/workflows/trigger", { method: "POST", body: JSON.stringify(data) }),
  },

  skills: {
    list: (params?: { include_platform?: boolean; category?: string }) => {
      const q = new URLSearchParams();
      if (params?.include_platform) q.set("include_platform", "true");
      if (params?.category) q.set("category", params.category);
      const suffix = q.toString() ? `?${q}` : "";
      return request<any[]>(`/skills${suffix}`);
    },
    store: () => request<any[]>("/skills/store"),
    create: (data: any) =>
      request<any>("/skills", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
      request<any>(`/skills/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/skills/${id}`, { method: "DELETE" }),
    invoke: (id: string, input: string | Record<string, unknown>) =>
      request<any>(`/skills/${id}/invoke`, { method: "POST", body: JSON.stringify({ input }) }),
    preview: (id: string, data: { message: string; metadata?: Record<string, unknown> }) =>
      request<any>(`/skills/${id}/preview`, { method: "POST", body: JSON.stringify(data) }),
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
    cancel: (id: string) =>
      request<any>(`/goals/${id}/cancel`, { method: "POST" }),
    getSteps: (id: string) => request<any[]>(`/goals/${id}/steps`),
    getMeasurements: (id: string, limit = 30) =>
      request<any[]>(`/goals/${id}/measurements?limit=${limit}`),
    addMeasurement: (id: string, value: number, source = "manual", note?: string) =>
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
    revoke: (workerId: string) =>
      request<WorkerResponse>(`/workers/${workerId}/revoke`, { method: "POST" }),
    register: (data: WorkerRegisterRequest) =>
      request<WorkerRegisterResponse>("/workers/register", {
        method: "POST",
        body: JSON.stringify(data),
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
    nango: {
      /** Mint a Nango Connect session token + popup URL. */
      startConnect: (provider_config_keys?: string[]) =>
        request<{ session_token: string; nango_connect_url: string }>(
          "/integrations/nango/connect-session",
          {
            method: "POST",
            body: JSON.stringify({ provider_config_keys }),
          },
        ),
      /** After the popup closes, mirror Nango connections into our integrations table. */
      sync: () =>
        request<{ upserted: number; providers: string[] }>(
          "/integrations/nango/connections/sync",
          { method: "POST" },
        ),
    },
    mcpServers: () =>
      request<Array<{
        server_key: string;
        name: string;
        category: string | null;
        description: string | null;
        auth_type: string;
        scopes: string | null;
        tagline: string | null;
        docs_url: string | null;
        setup_hint: string | null;
        color_hex: string | null;
        supports_multi_account: boolean;
        connections: Array<{
          id: string;
          display_name: string | null;
          provider_user_id: string;
          expires_at: string | null;
          is_default: boolean;
          connected_at: string | null;
          health: HealthStatus | null;
        }>;
        entity_accounts: Array<{
          id: string;
          name: string | null;
          display_name: string | null;
          is_default: boolean;
          created_at: string | null;
          status: string;
          health: HealthStatus | null;
        }>;
        user_connected: boolean;       // legacy
        user_expires_at: string | null; // legacy
        entity_connected: boolean;
        required_permission: string | null;
        user_has_required_permission: boolean;
        agent_can_use: boolean;
        hint: string;
        nango_provider_config_key?: string | null;
        oauth_configured?: boolean;
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
      }>>("/integrations/mcp-servers"),
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
    /** Absolute URL for the live QR png. Used as an <img> src; the
     *  browser will include cookies via the same-origin /api path. */
    wechatPersonalQrUrl: (accountId: string) =>
      `/api/v1/integrations/wechat-personal/${accountId}/qr.png`,

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
    wechatPersonalSessionQrUrl: (sessionId: string) =>
      `/api/v1/integrations/wechat-personal/sessions/${sessionId}/qr.png`,
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
        agent_name: string | null;
        binding_status: string | null;
        last_inbound_at: string | null;
        last_outbound_at: string | null;
      }>>("/integrations/channel-bindings"),
    upsertChannelBinding: (data: { channel_config_id: string; agent_id: string | null }) =>
      request<{
        channel_config_id: string;
        channel_type: string;
        provider: string;
        name: string | null;
        display_name: string;
        status: string;
        bound_channel_id: string | null;
        bound_agent_id: string | null;
        agent_name: string | null;
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
    oauthStart: (serverKey: string, opts?: { returnTo?: string }) => {
      const q = new URLSearchParams();
      if (opts?.returnTo) q.set("return_to", opts.returnTo);
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
      const token = getAuthToken();
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
      const token = getAuthToken();
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
      const token = getAuthToken();
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

  health: {
    check: () => request<{ status: string }>("/health"),
    deep: () => request<Record<string, any>>("/health/deep"),
  },

  // ── Entity Filesystem (JuiceFS) ──
  fs: {
    list: (path = ".", showSystem = false) =>
      request<{ items: any[]; path: string; count: number }>(`/fs/list?path=${encodeURIComponent(path)}&show_system=${showSystem}`),
    tree: (maxDepth = 3) =>
      request<{ tree: any[]; entity_id: string }>(`/fs/tree?max_depth=${maxDepth}`),
    read: (path: string) =>
      request<{ content: string; encoding: string; path: string; size: number; mime_type: string }>(`/fs/read?path=${encodeURIComponent(path)}`),
    info: (path: string) =>
      request<any>(`/fs/info?path=${encodeURIComponent(path)}`),
    write: (path: string, content: string) =>
      request<{ status: string; path: string }>("/fs/write", { method: "POST", body: JSON.stringify({ path, content }) }),
    mkdir: (path: string) =>
      request<{ status: string; path: string }>("/fs/mkdir", { method: "POST", body: JSON.stringify({ path }) }),
    move: (src: string, dest: string) =>
      request<{ status: string; src: string; dest: string }>("/fs/move", { method: "POST", body: JSON.stringify({ src, dest }) }),
    delete: (path: string) =>
      request<{ status: string; path: string }>("/fs/delete", { method: "POST", body: JSON.stringify({ path }) }),
    upload: (path: string, file: globalThis.File) => {
      const formData = new FormData();
      formData.append("file", file);
      const token = getAuthToken();
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
    wikiIndex: (opts?: { netId?: string; groupId?: string; workspaceId?: string }) => {
      const params = new URLSearchParams();
      if (opts?.netId) params.set("net_id", opts.netId);
      if (opts?.groupId) params.set("group_id", opts.groupId);
      if (opts?.workspaceId) params.set("workspace_id", opts.workspaceId);
      const qs = params.toString();
      return request<any>(`/fs/wiki-index${qs ? `?${qs}` : ""}`);
    },
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
};
