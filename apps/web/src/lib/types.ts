// Auth
export interface User {
  id: string;
  entity_id: string;
  email: string;
  display_name?: string;
  first_name?: string;
  last_name?: string;
  phone?: string;
  avatar_url?: string;
  role: string;
  permissions?: string[];
  staff_role_id?: string | null;
  staff_role_name?: string | null;
  status: string;
  llm_model?: string;
  timezone?: string;
  locale?: string;
  created_at?: string;
  memberships?: {
    entity_id: string;
    entity_name?: string | null;
    role: string;
    status: string;
    staff_id?: string | null;
    is_primary?: boolean;
    is_current?: boolean;
  }[];
}

export interface PeopleGatewayUser {
  id: string;
  email: string;
  display_name?: string | null;
  first_name?: string | null;
  last_name?: string | null;
  phone?: string | null;
  avatar_url?: string | null;
  timezone?: string | null;
  locale?: string | null;
}

export interface PeopleGatewayEntity {
  id: string;
  name: string;
  address?: string | null;
  phone?: string | null;
  email?: string | null;
  logo_url?: string | null;
  plan_id?: string | null;
  plan_name?: string | null;
}

export interface PeopleMembership {
  entity_id: string;
  entity_name?: string | null;
  role: string;
  status: string;
  staff_id?: string | null;
  staff_role_id?: string | null;
  staff_role_name?: string | null;
  is_primary?: boolean;
  is_current?: boolean;
  can_switch?: boolean;
  can_leave?: boolean;
  can_manage_team?: boolean;
  can_manage_billing?: boolean;
}

export interface PeopleInvite {
  invite_id: string;
  invite_token?: string | null;
  entity_id: string;
  entity_name?: string | null;
  email: string;
  name?: string | null;
  role_id?: string | null;
  role_name?: string | null;
  status: string;
  can_accept?: boolean;
  can_decline?: boolean;
}

export interface PeopleBillingContext {
  plan_id?: string | null;
  plan_name?: string | null;
  scope: "member" | "company" | string;
  can_manage_billing: boolean;
  total_credits?: number | null;
  used_credits?: number | null;
  remaining_credits?: number | null;
  own_credits_used: number;
  own_tokens_used: number;
  own_cost_usd: number;
}

export interface PeopleActions {
  can_switch_entity: boolean;
  can_leave_entity: boolean;
  can_manage_team: boolean;
  can_manage_billing: boolean;
  can_accept_invites: boolean;
  can_decline_invites: boolean;
}

export interface PeopleContext {
  user: PeopleGatewayUser;
  active_entity?: PeopleGatewayEntity | null;
  active_membership?: PeopleMembership | null;
  memberships: PeopleMembership[];
  pending_invites: PeopleInvite[];
  declined_invites: PeopleInvite[];
  effective_permissions: string[];
  billing: PeopleBillingContext;
  usage_scope: "company" | "personal" | string;
  actions: PeopleActions;
}

export interface PeopleContextActionResponse {
  access_token?: string | null;
  context: PeopleContext;
}

export interface PeopleDirectoryEntry extends UserSummary {
  membership_status: string;
  staff_id?: string | null;
  staff_name?: string | null;
  staff_role_id?: string | null;
  staff_role_name?: string | null;
}

// Tasks
export interface Task {
  id: string;
  entity_id: string;
  title: string;
  description?: string;
  status: string;
  priority: number;
  task_type: string;
  assignee_id?: string;
  agent_id?: string;
  agent_type?: string;
  owner_service_key?: string | null;
  owner_subscription_id?: string | null;
  category_id?: string;
  workspace_id?: string;
  workspace_name?: string | null;
  creator_id?: string;
  conversation_id?: string;
  parent_task_id?: string;
  required_skills?: string[];
  estimated_hours?: number;
  sla_policy_id?: string;
  sla_breached?: boolean;
  escalation_level?: number;
  tags?: string[];
  scheduled_job_id?: string;
  details: Record<string, any>;
  deadline?: string;
  scheduled_at?: string;
  duration_minutes?: number;
  started_at?: string;
  completed_at?: string;
  created_at?: string;
  actual_output?: Record<string, any> | null;
  // Resolved display fields from API
  assignee_name?: string;
  assignee_avatar?: string;
  agent_name?: string;
  agent_avatar?: string;
  creator_name?: string;
  creator_avatar?: string;
  // ── Permission-v1 fields ──
  visibility?: Visibility;
  owner_id?: string;
  client_visible?: boolean;
}

export interface TaskRetryResponse {
  task: Task;
  dispatched: boolean;
  mode: string;
  plan_id?: string | null;
  reset_steps: number;
}

export interface TaskHITLResponse {
  task: Task;
  resumed: boolean;
  dispatched: boolean;
  mode?: string | null;
  plan_id?: string | null;
  step_id?: string | null;
}

// Calendar settings / booking links
export interface CalendarWorkingHourWindow {
  day_of_week: number;
  enabled: boolean;
  start: string;
  end: string;
}

export interface CalendarBookingDefaults {
  duration_minutes: number;
  buffer_before_minutes: number;
  buffer_after_minutes: number;
  min_notice_minutes: number;
  rolling_window_days: number;
}

export interface BookingLink {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  duration_minutes: number;
  location_type: "none" | "phone" | "video" | "in_person" | "custom";
  location_detail?: string | null;
  calendar_id?: string | null;
  enabled: boolean;
  color: string;
  buffer_before_minutes: number;
  buffer_after_minutes: number;
  min_notice_minutes: number;
  rolling_window_days: number;
  created_at?: string | null;
  updated_at?: string | null;
  url?: string | null;
}

export interface BookingRecord {
  id: string;
  booking_link_id: string;
  booking_link_slug: string;
  guest_name: string;
  guest_email: string;
  note?: string | null;
  starts_at: string;
  ends_at: string;
  timezone: string;
  status: "confirmed" | "cancelled";
  calendar_provider?: string | null;
  calendar_account_id?: string | null;
  calendar_event_id?: string | null;
  calendar_event_url?: string | null;
  meeting_url?: string | null;
  calendar_event_created: boolean;
  email_sent: boolean;
  created_at?: string | null;
}

export interface CalendarSettings {
  provider: string;
  connection_id?: string | null;
  default_calendar_id: string;
  conflict_calendar_ids: string[];
  visible_calendar_ids: string[];
  timezone: string;
  working_hours: CalendarWorkingHourWindow[];
  booking_defaults: CalendarBookingDefaults;
  booking_links: BookingLink[];
  bookings: BookingRecord[];
  auto_create_events_from_tasks: boolean;
  track_task_deadlines: boolean;
  track_scheduled_tasks: boolean;
}

export interface CalendarConnectionOption {
  id: string;
  provider: string;
  display_name: string;
  provider_user_id: string;
  is_default: boolean;
  expires_at?: string | null;
}

export interface CalendarSettingsResponse {
  settings: CalendarSettings;
  connections: CalendarConnectionOption[];
}

export interface BookingLinkWrite {
  slug?: string | null;
  name: string;
  description?: string | null;
  duration_minutes?: number | null;
  location_type?: BookingLink["location_type"] | null;
  location_detail?: string | null;
  calendar_id?: string | null;
  enabled?: boolean | null;
  color?: string | null;
  buffer_before_minutes?: number | null;
  buffer_after_minutes?: number | null;
  min_notice_minutes?: number | null;
  rolling_window_days?: number | null;
}

export interface DailyAgendaItem {
  id: string;
  source: "task" | "booking";
  title: string;
  starts_at: string;
  ends_at?: string | null;
  status?: string | null;
  priority?: number | null;
  task_id?: string | null;
  workspace_id?: string | null;
  booking_id?: string | null;
  booking_link_id?: string | null;
  booking_link_slug?: string | null;
  guest_name?: string | null;
  guest_email?: string | null;
}

export interface DailyAgendaResponse {
  date: string;
  timezone: string;
  items: DailyAgendaItem[];
}

export interface ExternalCalendarEvent {
  id: string;
  provider: string;
  calendar_id: string;
  calendar_name?: string | null;
  external_event_id: string;
  title: string;
  starts_at: string;
  ends_at?: string | null;
  timezone?: string | null;
  all_day: boolean;
  status?: string | null;
  location?: string | null;
  description?: string | null;
  organizer_email?: string | null;
  attendee_count?: number | null;
  calendar_event_url?: string | null;
  meeting_url?: string | null;
}

export interface ExternalCalendarEventsResponse {
  provider: string;
  connection_id?: string | null;
  timezone: string;
  range_start: string;
  range_end: string;
  synced_at: string;
  events: ExternalCalendarEvent[];
}

export interface PublicBookingLink {
  owner_id: string;
  slug: string;
  name: string;
  description?: string | null;
  duration_minutes: number;
  location_type: string;
  location_detail?: string | null;
  owner_name?: string | null;
  timezone: string;
  working_hours: CalendarWorkingHourWindow[];
  available_slots: BookingAvailableSlot[];
}

export interface BookingAvailableSlot {
  starts_at: string;
  ends_at: string;
  label: string;
}

export interface PublicBookingRequest {
  starts_at: string;
  guest_name: string;
  guest_email: string;
  note?: string | null;
}

export interface BookingConfirmation {
  id: string;
  status: string;
  booking_link_slug: string;
  guest_name: string;
  guest_email: string;
  starts_at: string;
  ends_at: string;
  timezone: string;
  calendar_event_created: boolean;
  calendar_event_url?: string | null;
  meeting_url?: string | null;
  email_sent: boolean;
}

export interface ExecutionPlan {
  id: string;
  entity_id: string;
  workspace_id?: string | null;
  task_id?: string | null;
  agent_subscription_id?: string | null;
  status: string;
  execution_mode: string;
  approval_required: boolean;
  plan_dag: Record<string, any>;
  planner_version?: string | null;
  parent_plan_id?: string | null;
  cost_tracking: Record<string, any>;
  started_at?: string | null;
  completed_at?: string | null;
  last_error?: Record<string, any> | null;
  created_at: string;
  updated_at?: string | null;
}

export interface ExecutionStep {
  id: string;
  plan_id: string;
  step_key: string;
  kind: string;
  service_key?: string | null;
  resolved_subscription_id?: string | null;
  resolved_agent_id?: string | null;
  resolved_subscription_name?: string | null;
  resolved_agent_name?: string | null;
  resolved_agent_avatar?: string | null;
  provider?: string | null;
  action_key?: string | null;
  integration_id?: string | null;
  params: Record<string, any>;
  result?: Record<string, any> | null;
  depends_on: string[];
  step_status: string;
  risk_level: string;
  requires_approval: boolean;
  attempt_count: number;
  max_attempts: number;
  cost: Record<string, any>;
  error?: Record<string, any> | null;
  human_input_prompt?: string | null;
  human_input_response?: Record<string, any> | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface PlanRetryResponse {
  plan: ExecutionPlan;
  reset_steps: number;
  dispatched: boolean;
}

export interface StepRetryResponse {
  plan: ExecutionPlan;
  step: ExecutionStep;
  dispatched: boolean;
}

export interface TaskConstantsResponse {
  statuses: Record<string, { label: string; color: string; order: number }>;
  status_transitions: Record<string, string[]>;
  priorities: Record<string, { label: string; color: string }>;
  categories: any[];
  types: string[];
  notification_channels?: Record<
    string,
    {
      label: string;
      status: "active" | "available" | "planned";
      description: string;
    }
  >;
  notification_events?: Record<
    string,
    {
      label: string;
      notification_type: string;
      severity: "success" | "error" | "warning" | "info";
      description: string;
      default_channels: string[];
      configurable_channels?: string[];
      user_action?: string | null;
    }
  >;
}

export interface TaskLog {
  id: string;
  task_id: string;
  log_type: string;
  content?: string;
  created_by?: string;
  created_at?: string;
  attachments?: any[];
  /** Resolved agent context — present when an agent posted this log. */
  author_agent_id?: string | null;
  author_agent_name?: string | null;
  meta?: Record<string, any> | null;
}

export interface TaskCategory {
  id: string;
  entity_id: string;
  name: string;
  description?: string;
  color?: string;
  created_at?: string;
}

// Chat
export interface Conversation {
  id: string;
  entity_id: string;
  user_id?: string;
  agent_id?: string;
  workspace_id?: string;
  title?: string;
  summary?: string;
  channel: string;
  status: string;
  message_count?: number;
  created_at?: string;
  updated_at?: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: string;
  content?: string;
  tool_calls?: Record<string, any> | Array<{ name: string; result?: string }>;
  assistant_blocks?: Record<string, any>[] | null;
  token_usage?: Record<string, any>;
  stop_reason?: string | null;
  error?: string | null;
  limit_detail?: Record<string, any> | null;
  hitl_requests?: Record<string, any>[] | null;
  created_at?: string;
}

// ── Permission-v1 (see docs/PERMISSIONS_DESIGN_ZH.md) ─────────────────
export type Visibility = "private" | "workspace" | "entity" | "public";
export type Classification =
  | "public"
  | "internal"
  | "confidential"
  | "restricted";
export type QuarantineStatus =
  | "clean"
  | "pending_scan"
  | "quarantined"
  | "rejected";
export type WorkspaceRole =
  | "owner"
  | "editor"
  | "contributor"
  | "viewer"
  | "external_client";

// Documents
export interface Document {
  id: string;
  entity_id: string;
  name: string;
  fs_path?: string;
  file_size?: number;
  file_type?: string;
  mime_type?: string;
  source: string;
  vector_status: string;
  folder_id?: string | null;
  indexing_progress?: {
    step?: string;
    progress?: number;
    total_chunks?: number;
    current_chunk?: number;
  } | null;
  created_by?: string;
  created_at?: string;
  // ── Permission-v1 fields ──────────────────────────────────────────────
  visibility?: Visibility;
  classification?: Classification;
  owner_id?: string;
  client_visible?: boolean;
  legal_hold?: boolean;
  legal_hold_reason?: string;
  legal_hold_set_by?: string;
  legal_hold_set_at?: string;
  pii_detected?: boolean;
  quarantine_status?: QuarantineStatus;
  editor_recipe_document_id?: string | null;
  editor_recipe_path?: string | null;
  editor_recipe_name?: string | null;
  current_user_capabilities?: string[];
}

// Agents
export interface Agent {
  id: string;
  entity_id?: string;
  name: string;
  slug?: string;
  description?: string;
  description_i18n?: Record<string, string> | null;
  avatar_url?: string;
  system_prompt?: string;
  config: Record<string, any>;
  is_template: boolean;
  is_public: boolean;
  category?: string;
  tags: string[];
  source: string;
  status: string;
  tool_count?: number;
  skill_count?: number;
}

// Notifications
export interface Notification {
  id: string;
  entity_id: string;
  user_id: string;
  type: string;
  title?: string;
  content?: string;
  /** Structured extras. For daily briefings this carries kind, stats,
   *  alerts and action_items so the UI can render a rich card instead
   *  of the plain-text body. */
  metadata?: Record<string, unknown>;
  read_at?: string;
  created_at?: string;
}

export interface NotificationEventDescriptor {
  kind: string;
  category: "task" | "agent" | "media" | "system" | "billing" | "calendar";
  severity: "info" | "warn" | "critical";
  label: string;
  description: string;
}

export interface NotificationConnectedChannel {
  channel_type: string;
  channel_config_id: string;
  contact_id: string;
  display_name?: string | null;
  source_id: string;
  last_seen_at?: string | null;
}

export interface NotificationKindOverride {
  channels?: string[];
  enabled?: boolean;
  bypass_quiet_hours?: boolean;
}

export interface NotificationQuietHours {
  tz?: string;
  from?: string;
  to?: string;
}

export interface NotificationPreferences {
  default_channels: string[];
  by_kind: Record<string, NotificationKindOverride>;
  quiet_hours: NotificationQuietHours | null;
  supported_channels: string[];
  configured_channels?: string[];
  event_catalog: NotificationEventDescriptor[];
  connected_channels: NotificationConnectedChannel[];
}

export interface NotificationPreferencesUpdate {
  default_channels?: string[];
  /** Send `null` for a kind to drop the override. */
  by_kind?: Record<string, NotificationKindOverride | null>;
  quiet_hours?: NotificationQuietHours | null;
}

// Workspace
export interface Workspace {
  id: string;
  entity_id: string;
  name: string;
  description?: string;
  category?: string;
  address?: string;
  kind?: string;
  operating_context?: string;
  primary_work?: string;
  operating_model?: Record<string, any>;
  settings?: Record<string, any>;
  longitude?: number;
  latitude?: number;
  cover_image_url?: string;
  attribute_tags?: string[];
  identity_label?: string;
  property_type?: string;
  occupancy_status?: string;
  pms_property_id?: string;
  pms_unit_id?: string;
  heartbeat_enabled?: boolean;
  heartbeat_cadence?: string;
  last_heartbeat_at?: string;
  stats?: Record<string, any>;
  status: string;
  created_at?: string;
  updated_at?: string;
  created_by_user_id?: string;
  created_by_name?: string;
  created_by_email?: string;
  created_by_avatar_url?: string;
  deleted_at?: string | null;
}

export interface WorkspaceStaff {
  id: string;
  workspace_id: string;
  staff_id?: string;
  user_id?: string;
  /** Workspace-level role (Permission-v1, see RFC §5.2). */
  role?: WorkspaceRole | string;
  added_by?: string;
  added_at?: string;
  expires_at?: string;
  status?: string;
  created_at?: string;
}

export interface WorkspaceStats {
  total_tasks: number;
  tasks_by_status: Record<string, number>;
  total_documents: number;
  total_agents: number;
  recent_tasks: any[];
}

export interface WorkspaceActivity {
  id: string;
  workspace_id: string;
  event_type: string;
  summary: string;
  details?: Record<string, any>;
  user_id?: string;
  user_name?: string;
  user_email?: string;
  user_avatar_url?: string;
  agent_id?: string;
  agent_name?: string;
  agent_avatar_url?: string;
  actor_type?: "user" | "agent" | string;
  actor_id?: string;
  actor_name?: string;
  created_at?: string;
}

export interface RuntimeEvidence {
  id: string;
  workspace_id?: string | null;
  agent_id?: string | null;
  user_id?: string | null;
  conversation_id?: string | null;
  message_id?: string | null;
  task_id?: string | null;
  trace_id?: string | null;
  evidence_type: string;
  source: string;
  status: string;
  summary: string;
  details: Record<string, any>;
  metrics: Record<string, any>;
  created_at?: string;
}

export interface AgentLearningCandidate {
  id: string;
  workspace_id?: string | null;
  agent_id?: string | null;
  user_id?: string | null;
  candidate_type: "memory" | "skill" | "agent_profile_patch" | "profile_patch" | "rule" | "tool_experience" | string;
  scope: "agent" | "workspace" | "user" | "entity" | string;
  title: string;
  summary: string;
  payload: Record<string, any>;
  evidence_ids: string[];
  risk_level: "low" | "medium" | "high" | string;
  status: "proposed" | "accepted" | "rejected" | "applied" | "archived" | string;
  confidence: number;
  created_by: string;
  resolution: Record<string, any>;
  applied_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

// Usage
export interface UsageSummary {
  total_tokens: number;
  input_tokens?: number;
  output_tokens?: number;
  total_cost: number;
  by_model: { model: string; tokens: number; cost: number }[];
  by_source: { source: string; tokens: number; cost: number }[];
}

export interface TeamUsageTotals {
  credits_used: number;
  tokens_used: number;
  cost_usd: number;
  request_count: number;
  llm_calls: number;
  task_count: number;
  active_seconds: number;
  active_users: number;
  active_now: number;
}

export interface TeamActivityItem {
  id: string;
  user_id?: string | null;
  workspace_id?: string | null;
  workspace_name?: string | null;
  event_type: string;
  summary: string;
  details: Record<string, unknown>;
  created_at?: string | null;
}

export interface TeamMemberUsage {
  credits_used: number;
  tokens_used: number;
  cost_usd: number;
  request_count: number;
  llm_calls: number;
  task_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  last_used_at?: string | null;
}

export interface TeamMemberActivity {
  session_count: number;
  active_seconds: number;
  avg_session_seconds: number;
  active_session_count: number;
  active_now: boolean;
  last_seen_at?: string | null;
  recent: TeamActivityItem[];
}

export interface TeamUsageMember {
  staff_id: string;
  user_id?: string | null;
  membership_status?: string | null;
  kind: string;
  status: string;
  name: string;
  email?: string | null;
  avatar_url?: string | null;
  title?: string | null;
  role_id?: string | null;
  role_name?: string | null;
  usage: TeamMemberUsage;
  activity: TeamMemberActivity;
}

export interface TeamUsageResponse {
  entity_id: string;
  scope: "company" | string;
  days: number;
  generated_at: string;
  totals: TeamUsageTotals;
  members: TeamUsageMember[];
  recent_activity: TeamActivityItem[];
}

// Paginated response
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
}

// Search
export interface SearchResult {
  type: string;
  id: string;
  title: string;
  snippet?: string;
  score?: number;
}

// Comments
export interface CommentAnchor {
  type?: string;
  mode?: string;
  label?: string;
  source?: string;
  line?: number;
  line_end?: number;
  start?: number;
  end?: number;
  quote?: string;
  [key: string]: any;
}

export interface Comment {
  id: string;
  entity_id: string;
  resource_type: string;
  resource_id: string;
  parent_id?: string;
  user_id: string;
  user_email?: string;
  display_name?: string;
  user_display_name?: string;
  user_avatar_url?: string;
  content: string;
  mentions: string[];
  anchor?: CommentAnchor | null;
  reactions: Record<string, string[]>;
  is_edited: boolean;
  status: string;
  replies?: Comment[];
  created_at?: string;
  updated_at?: string;
}

// Billing
export interface BillingBalance {
  entity_id?: string;
  total_credits?: number;
  used_credits?: number;
  reserved_credits?: number;
  remaining_credits?: number;
  plan?: string;
  // Legacy fields from the older billing endpoint shape.
  token_balance?: number;
  credit_balance?: number;
  auto_recharge_enabled?: boolean;
}

export interface Payment {
  id: string;
  entity_id: string;
  amount: number;
  currency: string;
  status: string;
  stripe_session_id?: string;
  created_at?: string;
}

// Features
export interface Feature {
  id: string;
  name: string;
  key: string;
  category?: string;
  description?: string;
  is_visible: boolean;
  enabled: boolean;
}

export interface FeaturePackage {
  id: string;
  name: string;
  plan_type?: string;
  max_tokens?: number | null;
  max_users?: number | null;
  max_credit?: number | null;
  price_monthly?: number | null;
  features: Record<string, any> | string[];
}

// Task automation
export interface ChecklistItem {
  id: string;
  task_id: string;
  content: string;
  is_completed: boolean;
  sort_order: number;
  created_at?: string;
}

export interface TaskTemplate {
  id: string;
  entity_id: string;
  name: string;
  title_template: string;
  description_template?: string;
  default_priority?: number;
  category_id?: string;
  status: string;
}

// Skills
export interface Skill {
  id: string;
  entity_id?: string;
  name: string;
  slug?: string;
  display_name?: string;
  description?: string;
  system_prompt: string;
  tools: string[];
  category?: string;
  tags: string[];
  is_public: boolean;
  version: string;
  status: string;
  bindings?: Record<string, any>[];
}

// Orders
export interface Order {
  id: string;
  entity_id: string;
  order_number: string;
  title: string;
  description?: string;
  client_id?: string;
  assignee_id?: string;
  status: string;
  order_type: string;
  amount: number;
  currency: string;
  paid_amount: number;
  payment_status: string;
  details: Record<string, any>;
  notes?: string;
  due_date?: string;
  completed_at?: string;
  created_at?: string;
  updated_at?: string;
}

export interface OrderItem {
  id: string;
  order_id: string;
  name: string;
  description?: string;
  quantity: number;
  unit_price: number;
  total_price: number;
  details: Record<string, any>;
  created_at?: string;
}

export interface OrderStats {
  total: number;
  by_status: Record<string, number>;
  total_revenue: number;
  total_paid: number;
}

// Document Generation
export interface DocGenFormat {
  format: string;
  name: string;
  available: boolean;
}

// Staff Departments
export interface StaffDepartment {
  id: string;
  entity_id: string;
  name: string;
  parent_id?: string;
  description?: string;
  sort_order: number;
  status: string;
  created_at?: string;
}

// Staff Schedule
export interface StaffSchedule {
  id: string;
  staff_id: string;
  day_of_week: number;
  start_time: string;
  end_time: string;
  is_available: boolean;
}

export interface StaffScheduleException {
  id: string;
  staff_id: string;
  date: string;
  is_available: boolean;
  reason?: string;
  start_time?: string;
  end_time?: string;
}

// Browser Sessions
export interface BrowserSession {
  session_id: string;
  entity_id: string;
  status: string;
  current_url?: string;
  created_at?: string;
}

// API Keys
export interface ApiKey {
  id: string;
  entity_id: string;
  name: string;
  provider: string;
  key_prefix: string;
  base_url?: string;
  default_model?: string;
  is_default: boolean;
  status: string;
  usage_count: number;
  last_used_at?: string;
  created_at?: string;
}

// Webhooks
export interface WebhookEndpoint {
  id: string;
  entity_id: string;
  url: string;
  events: string[];
  secret?: string;
  status: string;
  created_at?: string;
}

export interface WebhookDelivery {
  id: string;
  endpoint_id: string;
  event: string;
  status: string;
  response_code?: number;
  created_at?: string;
}

// Custom Fields
export interface CustomField {
  id: string;
  entity_id: string;
  name: string;
  field_type: string;
  resource_type: string;
  options?: Record<string, any>;
  required: boolean;
  sort_order: number;
  created_at?: string;
}

// Memories
export interface Memory {
  id: string;
  entity_id: string;
  agent_id?: string;
  content: string;
  context?: string;
  status: string;
  created_at?: string;
  // ── Permission-v1 fields ──
  visibility?: Visibility;
  classification?: Classification;
  owner_id?: string;
}

// Favorites
export interface Favorite {
  id: string;
  entity_id: string;
  user_id: string;
  resource_type: string;
  resource_id: string;
  created_at?: string;
}

// Tags
export interface Tag {
  id: string;
  entity_id: string;
  name: string;
  color?: string;
  created_at?: string;
}

// Reports
export interface Report {
  title: string;
  data: Record<string, any>;
  generated_at: string;
}

// Backup
export interface BackupSummary {
  entity_id: string;
  users: number;
  workspaces: number;
  tasks: number;
  conversations: number;
  documents: number;
  agents: number;
  clients: number;
  staff_members: number;
}


// Presence
export interface PresenceInfo {
  user_id: string;
  status: string;
  resource_type?: string;
  resource_id?: string;
  last_seen?: string;
}

// Templates (detailed)
export interface TaskTemplateDetail {
  id: string;
  entity_id: string;
  name: string;
  title_template: string;
  description_template?: string;
  default_priority?: number;
  category_id?: string;
  status: string;
  created_at?: string;
}

// WebSocket events
export interface WSEvent {
  event: string;
  data: Record<string, any>;
}

// ── Permission-v1 wire types (mirror packages/core/models/permission.py) ──

export interface UserSummary {
  id: string;
  email: string;
  display_name?: string;
  avatar_url?: string;
}

export interface DocumentGrant {
  id: string;
  resource_type: string;
  resource_id: string;
  subject_type: "user" | "staff_role" | "workspace_role" | "team" | string;
  subject_id: string;
  subject_user_id?: string | null;
  subject_email?: string | null;
  subject_display_name?: string | null;
  subject_avatar_url?: string | null;
  capabilities: string[];
  granted_by?: string;
  granted_at?: string;
  expires_at?: string;
  status: string;
}

export interface DocumentShare {
  id: string;
  audience?: string; // 'anonymous' | 'email:foo@x.com' | 'domain:acme.com'
  capabilities: string[];
  watermark: boolean;
  require_otp: boolean;
  allow_download: boolean;
  expires_at?: string;
  max_uses?: number;
  use_count: number;
  last_used_at?: string;
  status: string;
  created_at?: string;
}

export interface DocumentAccessLogRow {
  ts: string;
  actor_type: string;
  actor_id?: string;
  action: string;
  classification_at_access?: string;
  ip?: string;
  redacted?: boolean;
  share_id?: string;
}

// Folder list/get response. Mirrors backend FolderResponse incl. the
// permission-v1 fields added in Phase B (RFC §13.3).
export interface DocumentFolderInfo {
  id: string;
  entity_id: string;
  name: string;
  parent_id?: string | null;
  document_count?: number;
  created_at?: string;
  visibility?: Visibility;
  classification?: Classification;
  owner_id?: string;
  client_visible?: boolean;
  current_user_capabilities?: string[];
}

export interface AccessRequest {
  id: string;
  resource_type: string;
  resource_id: string;
  requester_user_id: string;
  requested_capabilities: string[];
  reason?: string;
  status: "pending" | "approved" | "denied" | "expired" | string;
  decided_by?: string;
  decided_at?: string;
  decision_note?: string;
  created_at?: string;
}

// Share-approval request — Confidential external-share approval workflow.
// `config` is the snapshotted CreateShareRequest body so admins can review
// the exact audience/capabilities/expiry the requester picked.
export interface ShareApproval {
  id: string;
  document_id: string;
  requester_user_id: string;
  reason?: string;
  status: "pending" | "approved" | "denied" | "expired" | string;
  config: {
    audience_type?: "anonymous" | "email" | "domain";
    audience_value?: string;
    capabilities?: string[];
    expires_in_days?: number;
    watermark?: boolean;
    require_otp?: boolean;
    allow_download?: boolean;
  };
  decided_by?: string;
  decided_at?: string;
  decision_note?: string;
  approved_share_id?: string;
  created_at?: string;
}
