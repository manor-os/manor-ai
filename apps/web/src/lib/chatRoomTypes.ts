import type { AssistantBlock } from "./chatStream";

export interface ChatRoomMember {
  user_id: string;
  name: string;
  avatar_url?: string | null;
  role: "owner" | "admin" | "member";
}

export interface ChatRoom {
  id: string;
  kind: "dm" | "group";
  title: string;
  owner_scope: "personal" | "entity";
  member_count: number;
  members: ChatRoomMember[];
  online_user_ids?: string[];
  agents: ChatRoomAgentBinding[];
  can_manage_agents: boolean;
  ai_tool_execution_enabled: boolean;
  ai_tool_execution_revision: number;
  can_manage_ai_execution: boolean;
  notifications_muted: boolean;
  last_message_at: string | null;
  unread_count?: number;
  created_at: string;
  invite_token?: string | null;
  recipient_outcome?: "added" | "invited" | null;
  invite_next_step?: "accept" | "sign_up" | "verify_email" | null;
  invite_email_sent?: boolean | null;
}

export interface ChatRoomAgentBinding {
  id: string;
  name: string;
  available: boolean;
  is_default?: boolean;
  context_scope?: "room_only" | "agent_and_group_audience";
  subscription_id?: string;
  workspace_name?: string | null;
  status?: "active" | "disabled";
}

export interface ChatRoomAgentCandidate {
  subscription_id: string;
  name: string;
  workspace_name: string | null;
}

export interface ChatRoomPresence {
  online_user_ids: string[];
}

export interface ChatRoomMessage {
  id: string;
  room_id: string;
  sender_user_id: string | null;
  sender_name: string;
  sender_user_avatar_url?: string | null;
  sender_agent_binding_id?: string | null;
  sender_agent_name?: string | null;
  sender_agent_avatar_url?: string | null;
  content: string;
  deleted_at?: string | null;
  mentioned_user_ids: string[];
  mentioned_agent_binding_ids?: string[];
  agent_runs?: ChatRoomAgentRun[];
  agent_run_status?: "pending" | "running" | "succeeded" | "failed" | "interrupted" | null;
  assistant_blocks?: AssistantBlock[];
  reply_to_message_id?: string | null;
  reply_to?: { id: string; sender_name: string; content: string } | null;
  reactions?: { emoji: string; count: number; reacted_by_me: boolean }[];
  attachments: ChatRoomFile[];
  created_at: string;
}

export interface ChatRoomAgentRun {
  id: string;
  binding_id: string;
  agent_name: string;
  status: "pending" | "running" | "waiting_approval" | "succeeded" | "failed" | "interrupted";
  approval_kind?: "file" | "reply";
  error_code?: string | null;
  assistant_blocks?: AssistantBlock[];
}

export interface ChatRoomReplyApproval {
  run_id: string;
  agent_name: string;
  content: string;
  digest: string;
  created_at: string;
}

export interface ChatRoomToolApproval {
  id: string;
  run_id: string;
  tool_name: string;
  summary: string;
  status: "pending" | "approved" | "rejected" | "claimed" | "succeeded" | "failed" | "invalidated";
  requester_user_id: string;
  decided_by_user_id: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface ChatRoomFile {
  id: string;
  message_id: string;
  name: string;
  size: number;
  mime_type: string;
  sender_name: string;
  created_at: string;
  download_url?: string;
  can_delete?: boolean;
}

export type ChatRoomInvite =
  | { outcome: "added"; invite_token: null; invite_next_step: null; expires_at: null; email: string }
  | { outcome: "invited"; invite_token: string; invite_next_step: "accept" | "sign_up" | "verify_email" | null; email_sent?: boolean | null; expires_at: string; email: string };

export interface ChatRoomInviteSender {
  id: string;
  email: string;
  provider: "email" | "gmail" | "outlook";
}

export interface ChatRoomPendingInvite {
  id: string;
  email: string;
  status: "pending" | "expired";
  expires_at: string;
}
