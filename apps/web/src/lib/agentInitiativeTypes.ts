export type AgentInitiativeStatus =
  | "draft"
  | "active"
  | "muted"
  | "paused"
  | "needs_setup"
  | "error"
  | "completed"
  | "deleted";

export type AgentInitiativeDecisionMode = "always" | "on_condition" | "on_change";

export type AgentInitiativeTrigger =
  | {
      version: "1";
      kind: "cron";
      cron: string;
      timezone: string;
    }
  | {
      version: "1";
      kind: "interval";
      every_seconds: number;
      timezone?: string;
      anchor_at?: string | null;
    }
  | {
      version: "1";
      kind: "at";
      run_at: string;
      timezone?: string;
    }
  | {
      version: "1";
      kind: "event";
      source: string;
      event: string;
      account_ref?: string | null;
      filter?: Record<string, unknown> | null;
    };

export interface AgentInitiativeCondition {
  version: "1";
  preconditions?: Array<{
    kind: "deterministic";
    field: string;
    op: "eq" | "ne" | "in" | "contains" | "exists";
    value?: unknown;
  }>;
  semantic_condition?: string | null;
}

export interface AgentInitiativeDecisionPolicy {
  version: "1";
  mode: AgentInitiativeDecisionMode | "on_match";
  cooldown_seconds?: number | null;
  max_notifications_per_day?: number | null;
  /** Tolerated legacy/read-model alias; never sent by the current form. */
  max_notifications_per_24h?: number | null;
  dedupe_window_seconds?: number | null;
  max_model_severity?: "info" | "warn";
}

export type AgentInitiativeSurface =
  | {
      kind: "conversation";
      conversation_id: string;
    }
  | {
      kind: "workspace_chat";
      conversation_id: string;
      scope?: "main" | "thread";
    };

export interface AgentInitiativeDeliveryPolicy {
  version: "1";
  surface?: AgentInitiativeSurface | null;
  notification?: {
    enabled: boolean;
    recipient: "owner";
    channels: Array<"inapp" | "email" | "telegram" | "wechat" | "slack" | "discord" | "whatsapp">;
    content_mode?: "summary_and_link" | "full";
  } | null;
}

export interface AgentInitiativeInterruptionPolicy {
  version: "1";
  quiet_hours:
    | { mode: "inherit_user" }
    | { mode: "none" }
    | {
        mode: "custom";
        timezone: string;
        start: string;
        end: string;
        from?: string;
        to?: string;
      };
  during_quiet_hours: "suppress_external";
}

export interface AgentInitiative {
  id: string;
  entity_id?: string;
  owner_user_id?: string;
  agent_subscription_id: string;
  workspace_id?: string | null;
  name: string;
  status: AgentInitiativeStatus | string;
  instruction: string;
  trigger_kind?: string;
  trigger_source?: string | null;
  trigger_event?: string | null;
  /** Canonical API field. */
  trigger?: AgentInitiativeTrigger | null;
  /** Tolerated while the backend read model is being finalized. */
  trigger_config?: AgentInitiativeTrigger | null;
  condition?: AgentInitiativeCondition | null;
  condition_config?: AgentInitiativeCondition | null;
  decision_policy?: AgentInitiativeDecisionPolicy | null;
  delivery_policy?: AgentInitiativeDeliveryPolicy | null;
  interruption_policy?: AgentInitiativeInterruptionPolicy | null;
  revision: number;
  consecutive_errors?: number;
  last_run_at?: string | null;
  last_checked_at?: string | null;
  next_run_at?: string | null;
  last_status?: string | null;
  last_error?: string | null;
  last_delivery_at?: string | null;
  last_notified_at?: string | null;
  last_silent_reason?: string | null;
  can_manage?: boolean;
  created_by_user_id?: string;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
}

export interface AgentInitiativeCreateRequest {
  name: string;
  instruction: string;
  trigger: AgentInitiativeTrigger;
  condition: AgentInitiativeCondition;
  decision_policy: AgentInitiativeDecisionPolicy;
  delivery_policy: AgentInitiativeDeliveryPolicy;
  interruption_policy: AgentInitiativeInterruptionPolicy;
}

export type AgentInitiativeUpdatePatch = Partial<AgentInitiativeCreateRequest>;

export interface AgentInitiativeUpdateRequest {
  expected_revision: number;
  patch: AgentInitiativeUpdatePatch;
}

export interface AgentInitiativeRevisionRequest {
  expected_revision: number;
}

export interface AgentInitiativeDelivery {
  id: string;
  run_id?: string;
  initiative_id?: string;
  kind: "conversation" | "workspace_chat" | "notification" | string;
  target_ref?: string;
  status: string;
  message_id?: string | null;
  notification_id?: string | null;
  recipient_user_id?: string | null;
  attempt_count?: number;
  next_attempt_at?: string | null;
  suppressed_reason?: string | null;
  error?: string | null;
  last_error?: string | null;
  delivered_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AgentInitiativeRun {
  id: string;
  initiative_id: string;
  occurrence_key?: string;
  trigger_kind?: string;
  trigger_type?: string;
  config_revision?: number;
  status: string;
  candidate_decision?: {
    version?: "1";
    decision?: "notify" | "silent";
    message?: string | null;
    reason_code?: string | null;
    reason?: string | null;
    severity?: string | null;
  } | null;
  final_decision?: "notify" | "silent" | string | null;
  decision?: "notify" | "silent" | string | null;
  reason_code?: string | null;
  reason?: string | null;
  severity?: "info" | "warn" | string | null;
  message?: string | null;
  evidence_refs?: string[] | null;
  error_code?: string | null;
  error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  deliveries?: AgentInitiativeDelivery[];
}

export interface AgentInitiativeRunReceipt {
  initiative_id: string;
  queued_at: string;
  idempotency_key: string;
  scheduled_run_id: string;
  published: boolean;
}

export interface AgentInitiativePage<T> {
  items: T[];
  total?: number;
  has_more?: boolean;
  next_cursor?: string | null;
}

export type AgentInitiativeListResponse =
  | AgentInitiative[]
  | AgentInitiativePage<AgentInitiative>;

export interface AgentInitiativeConversationListResponse {
  conversation: {
    id: string;
    title?: string | null;
    workspace_id?: string | null;
    scope: string;
  };
  items: AgentInitiative[];
}

export type AgentInitiativeRunListResponse =
  | AgentInitiativeRun[]
  | AgentInitiativePage<AgentInitiativeRun>;

export type AgentInitiativeActionResponse =
  | AgentInitiative
  | AgentInitiativeRun
  | AgentInitiativeRunReceipt
  | {
      initiative?: AgentInitiative;
      run?: AgentInitiativeRun;
      status?: string;
      next_run_at?: string | null;
    };
