// Manor v1 worker HTTP protocol — TypeScript types.
// Mirrors `packages/worker_sdk/types.py`. Drift caught by smoke test.

export type LeaseKind = "action" | "llm" | "subagent" | "code";
export type RiskLevel = "low" | "medium" | "high";
export type ExecutionMode = "live" | "dry_run" | "sandbox";
export type CredentialType = "oauth_token" | "api_key" | "basic_auth" | "browser_session";
export type WorkerState = "idle" | "busy" | "shutting_down";

export interface CredentialBundle {
  sublease_id: string;
  integration_id: string;
  provider: string;
  type: CredentialType;
  value: Record<string, unknown>;
  expires_at: string; // ISO-8601
}

export interface Lease {
  lease_id: string;
  step_id: string;
  plan_id: string;
  workspace_id: string | null;
  subscription_id?: string | null;
  service_key?: string | null;
  agent?: Record<string, unknown> | null;
  bindings?: Record<string, unknown>;

  kind: LeaseKind;
  provider: string | null;
  action_key: string | null;
  capability_id?: string | null;
  integration_id: string | null;

  params: Record<string, unknown>;
  expected_input_schema?: Record<string, unknown> | null;
  expected_output_schema?: Record<string, unknown> | null;

  risk_level: RiskLevel;
  lease_until: string; // ISO-8601
  budget_limit_usd?: number | null;
  execution_mode: ExecutionMode;

  credentials: CredentialBundle[];

  // Server may add fields; we don't strip extras.
  [k: string]: unknown;
}

export interface LeaseCost {
  llm_tokens_input?: number;
  llm_tokens_output?: number;
  api_calls?: number;
  usd?: number;
  [k: string]: unknown;
}

export interface LeaseResult {
  result?: Record<string, unknown> | null;
  cost?: LeaseCost | null;
  evidence_refs?: string[] | null;
}

export interface HeartbeatActiveLease {
  lease_id: string;
  progress?: number;
}

export interface HeartbeatCompletedLease {
  lease_id: string;
  status: "done" | "failed";
  result?: Record<string, unknown> | null;
  error?: Record<string, unknown> | null;
  cost?: LeaseCost | null;
  evidence_refs?: string[] | null;
}

export interface HeartbeatCapacity {
  can_accept_leases: number;
  filters?: Record<string, unknown> | null;
}

export interface HeartbeatRequest {
  state: WorkerState;
  timestamp?: string;
  budget_remaining_usd?: number | null;
  active_leases: HeartbeatActiveLease[];
  completed_since_last: HeartbeatCompletedLease[];
  capacity: HeartbeatCapacity;
  capabilities?: Record<string, unknown> | null;
}

export interface HeartbeatInstruction {
  type: string;
  payload?: Record<string, unknown> | null;
}

export interface HeartbeatResponse {
  server_time: string;
  next_heartbeat_in_seconds: number;
  new_leases: Lease[];
  instructions: HeartbeatInstruction[];
}

// ── Errors / control-flow exceptions ─────────────────────────────────

/**
 * Throw from a handler when the lease requires user attention
 * (CAPTCHA, 2FA, ambiguous input, manual approval). Lease pauses;
 * Manor surfaces `prompt` to the operator. When answered, Manor
 * re-leases with the response in `params.human_input_response`.
 */
export class NeedHumanInput extends Error {
  readonly prompt: string;
  readonly kind: string;

  constructor(prompt: string, opts?: { kind?: string }) {
    super(prompt);
    this.name = "NeedHumanInput";
    this.prompt = prompt;
    this.kind = opts?.kind ?? "ambiguous_input";
  }
}

export class WorkerClientError extends Error {
  readonly statusCode?: number;
  readonly body?: unknown;

  constructor(message: string, opts?: { statusCode?: number; body?: unknown }) {
    super(message);
    this.name = "WorkerClientError";
    this.statusCode = opts?.statusCode;
    this.body = opts?.body;
  }
}

export class NoHandlerError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NoHandlerError";
  }
}
