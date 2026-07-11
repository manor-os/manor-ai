import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { createPortal } from "react-dom";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import { t } from "../lib/i18n";
import type {
  WorkspaceDraft,
  WorkspaceDraftMessage,
  WorkspaceArchitectToolEvent,
  WorkspaceArchitectTurnMeta,
  FinalizeProgressEvent,
  PlanLimitDetail,
} from "../lib/api";
import { ApiError } from "../lib/api";
import PageHeader from "../components/ui/PageHeader";
import GlassCard from "../components/ui/GlassCard";
import Button from "../components/ui/Button";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import StatusBadge from "../components/ui/StatusBadge";
import Chip from "../components/ui/Chip";
import Toggle from "../components/ui/Toggle";
import ChatMarkdown from "../components/ChatMarkdown";
import MessageBubble from "../components/chat/MessageBubble";
import ChatInputFooter, {
  manualSkillLabel,
  stripManualSkillTokens,
  type AttachedItem,
  type ManualSkillItem,
} from "../components/ChatInputFooter";
import { inferRuntimeRuleEnforcement } from "../lib/runtimeRules";

/* ── Visual tokens ─────────────────────────────────────────────────── */

const LABEL: CSSProperties = {
  fontSize: 10, fontWeight: 800, textTransform: "uppercase",
  letterSpacing: "0.12em", color: "var(--text-faint)", marginBottom: 6,
};
const VALUE: CSSProperties = {
  fontSize: 13, fontWeight: 600, color: "var(--text-strong)", wordBreak: "break-word",
};
const SUBTLE: CSSProperties = { fontSize: 11, color: "var(--text-faint)" };

/* ── Page-scoped CSS ───────────────────────────────────────────────── */

const CHAT_STYLES = `
  .draft-shell {
    flex: 1; min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 380px;
    gap: 20px;
  }
  @media (max-width: 1080px) {
    .draft-shell { grid-template-columns: 1fr; }
    .draft-side  { display: none; }
  }
  .draft-chat {
    display: flex; flex-direction: column; min-height: 0;
    background: rgba(255,255,255,0.85);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(231,229,228,0.6);
    border-radius: 20px; overflow: hidden;
  }
  .draft-msgs {
    flex: 1; min-height: 0; overflow-y: auto;
    padding: 24px 28px;
    display: flex; flex-direction: column; gap: 14px;
  }
  .draft-msg-row { display: flex; }
  .draft-msg-row.user { justify-content: flex-end; }
  .draft-msg-bubble {
    max-width: 78%;
    overflow-wrap: anywhere;
  }
  .draft-msg-row.assistant .draft-msg-bubble {
    width: 100%;
    max-width: 100%;
  }
  .draft-msg-row.user .draft-msg-bubble {
    white-space: pre-wrap;
  }
  .draft-msg-row,
  .draft-starters {
    width: min(100%, var(--chat-thread-max-width, 920px));
    margin: 0 auto;
  }
  .draft-starters {
    display: flex; flex-wrap: wrap; gap: 8px;
  }
  .draft-starter-btn {
    min-height: 30px;
    padding: 6px 10px;
    border: 1px solid rgba(120,113,108,0.14);
    border-radius: 8px;
    background: rgba(255,255,255,0.74);
    color: #57534e;
    font-size: 12px; font-weight: 700; line-height: 1.25;
    cursor: pointer;
    transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
  }
  .draft-starter-btn:hover {
    border-color: rgba(67,107,101,0.26);
    color: #2f5f58;
    background: rgba(250,250,249,0.9);
  }
  .draft-side  { display: flex; flex-direction: column; gap: 14px; min-height: 0; overflow-y: auto; padding-right: 4px; }
  .draft-row   {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 6px 0;
    border-bottom: 1px dashed rgba(231,229,228,0.6);
    font-size: 13px;
  }
  .draft-row:last-child { border-bottom: none; }
  .draft-row .label { color: #78716c; flex-shrink: 0; }
  .draft-row .value { color: #1c1917; text-align: right; overflow-wrap: anywhere; font-weight: 600; }
  .draft-typing {
    display: inline-flex; gap: 3px; align-items: center; color: #a8a29e;
  }
  .draft-typing span {
    width: 6px; height: 6px; border-radius: 50%; background: currentColor;
    opacity: 0.4; animation: draftDot 1.2s infinite ease-in-out;
  }
  .draft-typing span:nth-child(2) { animation-delay: 0.15s; }
  .draft-typing span:nth-child(3) { animation-delay: 0.3s; }
  @keyframes draftDot {
    0%, 60%, 100% { opacity: 0.3; transform: scale(0.85); }
    30%           { opacity: 1.0; transform: scale(1.0);  }
  }

  /* Construction log inside assistant message */
  .draft-build-log {
    margin-top: 10px; border-top: 1px dashed rgba(168,162,158,0.4); padding-top: 8px;
  }
  .draft-build-toggle {
    background: none; border: none; padding: 0; cursor: pointer;
    color: #57534e; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
    display: inline-flex; align-items: center; gap: 4px;
  }
  .draft-build-toggle:hover { color: #436b65; }
  .draft-build-list {
    margin: 8px 0 0; padding: 0; list-style: none;
    display: flex; flex-direction: column; gap: 3px;
    font-size: 11px; color: #78716c; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .draft-build-list li { display: flex; gap: 6px; align-items: baseline; }
  .draft-build-list .ok    { color: #436b65; }
  .draft-build-list .err   { color: #c14a44; }
  .draft-build-list .step  { color: #d6d3d1; }
  .draft-build-list .name  { color: #44403c; font-weight: 600; }
  .draft-build-list .summary { color: #78716c; flex: 1; min-width: 0; overflow-wrap: anywhere; }

  /* Live activity strip while a turn is streaming */
  .draft-live-strip {
    display: flex; flex-direction: column; gap: 4px;
    background: rgba(67,107,101,0.05);
    border: 1px solid rgba(67,107,101,0.2);
    border-radius: 10px; padding: 8px 12px;
    font-size: 11px; color: #436b65;
  }
  .draft-live-strip .pulse {
    width: 6px; height: 6px; border-radius: 50%; background: #436b65;
    box-shadow: 0 0 8px #436b65;
    animation: pulseDot 1.4s infinite ease-in-out;
    display: inline-block; margin-right: 8px;
  }
  html[data-theme="dark"] .draft-shell {
    color: var(--text-default);
  }
  html[data-theme="dark"] .draft-chat {
    background:
      linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.028)),
      rgba(14, 15, 14, 0.96);
    border-color: rgba(255,255,255,0.13);
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.06),
      0 18px 52px rgba(0,0,0,0.24);
  }
  html[data-theme="dark"] .draft-msgs {
    background:
      radial-gradient(circle at 50% 12%, rgba(127,208,196,0.075), transparent 32%),
      transparent;
  }
  html[data-theme="dark"] .draft-msg-row.assistant .draft-msg-bubble {
    background: rgba(255,255,255,0.075) !important;
    color: var(--text-strong) !important;
    border-color: rgba(255,255,255,0.14) !important;
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.05),
      0 12px 30px rgba(0,0,0,0.18) !important;
  }
  html[data-theme="dark"] .draft-msg-row.user .draft-msg-bubble {
    color: #ffffff !important;
    border-color: rgba(127,208,196,0.42) !important;
    background: #4f7f76 !important;
  }
  html[data-theme="dark"] .draft-msg-bubble .chat-md,
  html[data-theme="dark"] .draft-msg-bubble .chat-md :where(p, li, td, th, strong, em, h1, h2, h3, h4, code) {
    color: var(--text-strong) !important;
  }
  html[data-theme="dark"] .draft-starter-btn {
    background: rgba(255,255,255,0.07);
    border-color: rgba(255,255,255,0.14);
    color: var(--text-default);
  }
  html[data-theme="dark"] .draft-starter-btn:hover {
    background: rgba(127,208,196,0.12);
    border-color: rgba(127,208,196,0.38);
    color: #dff8f3;
  }
  html[data-theme="dark"] .draft-chat .embedded-chat-footer {
    background: transparent;
    border-top-color: rgba(255,255,255,0.1);
    padding: 14px 28px 16px;
  }
  html[data-theme="dark"] .draft-chat .chat-composer {
    margin: 0 auto 16px;
    width: min(calc(100% - 56px), var(--chat-thread-max-width, 920px));
    background: rgba(18,18,18,0.98);
    border-color: rgba(255,255,255,0.16);
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.07),
      0 12px 40px rgba(0,0,0,0.24);
  }
  html[data-theme="dark"] .draft-chat .chat-composer.chat-composer--focused {
    background: rgba(20,20,20,0.99);
    border-color: rgba(127,208,196,0.42);
    box-shadow:
      0 0 0 3px rgba(127,208,196,0.14),
      inset 0 1px 0 rgba(255,255,255,0.08),
      0 16px 42px rgba(0,0,0,0.28);
  }
  html[data-theme="dark"] .draft-chat .chat-composer-rich-editor,
  html[data-theme="dark"] .draft-chat .chat-composer-inline-render,
  html[data-theme="dark"] .draft-chat .chat-composer-textarea {
    color: #ffffff !important;
  }
  html[data-theme="dark"] .draft-chat .chat-composer-rich-editor:empty::before,
  html[data-theme="dark"] .draft-chat .chat-composer-textarea::placeholder {
    color: rgba(255,255,255,0.58) !important;
  }
  html[data-theme="dark"] .draft-side .glass-card {
    background: rgba(255,255,255,0.055);
    border-color: rgba(255,255,255,0.13);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
  }
  html[data-theme="dark"] .draft-side .glass-card :is(p, li, div, span) {
    color: var(--text-default) !important;
  }
  html[data-theme="dark"] .draft-side .glass-card :is(.inline-flex, span[style*="inline-flex"]) {
    color: #111111 !important;
  }
  html[data-theme="dark"] .draft-side .glass-card button {
    color: #b7eee5 !important;
  }
  .draft-side-surface,
  .draft-meta-panel {
    transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
  }
  html[data-theme="dark"] .draft-side-surface,
  html[data-theme="dark"] .draft-meta-panel {
    background: rgba(255,255,255,0.06) !important;
    border-color: rgba(255,255,255,0.14) !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.045);
  }
  html[data-theme="dark"] .draft-side-surface :where(div, p, span, li, strong),
  html[data-theme="dark"] .draft-meta-panel :where(div, p, span, strong) {
    color: var(--text-strong) !important;
  }
  html[data-theme="dark"] .draft-side-surface .draft-muted,
  html[data-theme="dark"] .draft-meta-panel .draft-muted {
    color: var(--text-muted) !important;
  }
  html[data-theme="dark"] .draft-side-surface .draft-remove,
  html[data-theme="dark"] .draft-side-surface .draft-inline-action {
    color: #b7eee5 !important;
  }
  html[data-theme="dark"] .draft-warning-surface {
    background: rgba(147,96,39,0.13) !important;
    border-color: rgba(229,184,96,0.26) !important;
  }
  html[data-theme="dark"] .draft-side .draft-row {
    border-bottom-color: rgba(255,255,255,0.12);
  }
  html[data-theme="dark"] .draft-side .draft-row .label {
    color: var(--text-muted);
  }
  html[data-theme="dark"] .draft-side .draft-row .value {
    color: var(--text-strong);
  }
  html[data-theme="dark"] .draft-build-log {
    border-top-color: rgba(255,255,255,0.14);
  }
  html[data-theme="dark"] .draft-build-toggle,
  html[data-theme="dark"] .draft-build-list {
    color: var(--text-muted);
  }
  html[data-theme="dark"] .draft-build-list .step {
    color: rgba(255,255,255,0.32);
  }
  html[data-theme="dark"] .draft-build-list .name {
    color: var(--text-strong);
  }
  html[data-theme="dark"] .draft-build-list .summary {
    color: var(--text-muted);
  }
  html[data-theme="dark"] .draft-live-strip {
    background: rgba(127,208,196,0.09);
    border-color: rgba(127,208,196,0.24);
    color: #dff8f3;
  }
  html[data-theme="dark"] .draft-live-strip .pulse {
    background: #7fd0c4;
    box-shadow: 0 0 10px rgba(127,208,196,0.7);
  }
  @keyframes pulseDot {
    0%, 100% { opacity: 0.4; }
    50%      { opacity: 1; }
  }
`;

/* ── Helpers ───────────────────────────────────────────────────────── */

function _humanize(key: string | null | undefined): string {
  if (!key) return "";
  return key
    .replace(/[_\-]+/g, " ").trim().split(/\s+/)
    .map((w) => (w.length > 0 ? w[0].toUpperCase() + w.slice(1).toLowerCase() : w))
    .join(" ");
}
const FIELD_LABELS: Record<string, string> = {
  kind: t("page.workspace_draft_chat.field.workspace_type"),
  name: t("page.workspace_draft_chat.field.workspace_name"),
  operating_context: t("page.workspace_draft_chat.field.how_it_will_be_used"),
  primary_work: t("page.workspace_draft_chat.field.main_work"),
  services: t("page.workspace_draft_chat.field.services_to_run"),
  goals: t("page.workspace_draft_chat.field.goals"),
  agent_mappings: t("page.workspace_draft_chat.field.agent_assignments"),
  staff_assignments: t("page.workspace_draft_chat.field.staff"),
  knowledge_attachments: t("page.workspace_draft_chat.field.knowledge_sources"),
  channel_config: t("page.workspace_draft_chat.field.channels"),
  budget_policy: t("page.workspace_draft_chat.field.budget"),
  rules: t("page.workspace_draft_chat.field.rules"),
  automations: t("page.workspace_draft_chat.field.automations"),
  flagged_integrations: t("page.workspace_draft_chat.field.integrations_to_connect"),
  missing_integrations: t("page.workspace_draft_chat.field.integrations_to_connect"),
};
const VALUE_LABELS: Record<string, string> = {
  active: t("page.workspace_draft_chat.value.drafting"),
  ready: t("page.workspace_draft_chat.value.ready"),
  finalized: t("page.workspace_draft_chat.value.created"),
  abandoned: t("page.workspace_draft_chat.value.abandoned"),
  ai_tech_founder: t("page.workspace_draft_chat.value.ai_tech_founder"),
  content_creation: t("page.workspace_draft_chat.value.content_creation"),
  content_scheduling: t("page.workspace_draft_chat.value.content_scheduling"),
  community_engagement: t("page.workspace_draft_chat.value.community_engagement"),
  growth_analytics: t("page.workspace_draft_chat.value.growth_analytics"),
  public_webchat: t("page.workspace_draft_chat.value.public_web_chat"),
  external_message: t("page.workspace_draft_chat.value.customer_message"),
  twitter_x: t("page.workspace_draft_chat.value.x_twitter"),
  x_twitter: t("page.workspace_draft_chat.value.x_twitter"),
  linkedin: t("page.workspace_draft_chat.value.linkedin"),
  wechat: t("page.workspace_draft_chat.value.wechat"),
  xiaohongshu: t("page.workspace_draft_chat.value.xiaohongshu"),
  google_drive: t("page.workspace_draft_chat.value.google_drive"),
  gmail: t("page.workspace_draft_chat.value.gmail"),
  email: t("page.workspace_draft_chat.value.email"),
  slack: t("page.workspace_draft_chat.value.slack"),
};
const ACTION_PATTERN_LABELS: Record<string, string> = {
  "social_post.publish": t("page.workspace_draft_chat.action.publishing_social_posts"),
  "social_post.delete": t("page.workspace_draft_chat.action.deleting_social_posts"),
  "external_message.send": t("page.workspace_draft_chat.action.sending_customer_messages"),
  "email.send": t("page.workspace_draft_chat.action.sending_email"),
  "email.delete": t("page.workspace_draft_chat.action.deleting_email"),
  "file.write": t("page.workspace_draft_chat.action.creating_or_editing_files"),
  "file.delete": t("page.workspace_draft_chat.action.deleting_files"),
  "file.move": t("page.workspace_draft_chat.action.moving_files"),
  "document.export": t("page.workspace_draft_chat.action.exporting_documents"),
};
const STARTER_PROMPTS = [
  t("page.workspace_draft_chat.starter.coffee_popup"),
  t("page.workspace_draft_chat.starter.client_desk"),
  t("page.workspace_draft_chat.starter.manga_serial"),
];
function _friendlyFieldLabel(key: string | null | undefined): string {
  if (!key) return "";
  return FIELD_LABELS[key] || _humanize(key);
}
function _friendlyValue(value: string | null | undefined): string {
  if (!value) return "";
  const raw = String(value);
  return VALUE_LABELS[raw] || _humanize(raw)
    .replace(/\bAi\b/g, "AI")
    .replace(/\bApi\b/g, "API")
    .replace(/\bUrl\b/g, "URL")
    .replace(/\bId\b/g, "ID")
    .replace(/\bSop\b/g, "SOP")
    .replace(/\bCrm\b/g, "CRM");
}
function _friendlyActionPattern(pattern: string): string {
  return ACTION_PATTERN_LABELS[pattern] || _friendlyValue(pattern.replace(/\./g, "_"));
}
function _serviceLabel(svc: any): string {
  return svc?.name || _friendlyValue(svc?.service_key || svc?.key) || t("page.workspace_draft_chat.unnamed_service");
}
function _goalLabel(g: any): string {
  return g?.title || g?.name || _friendlyValue(g?.goal_key || g?.key) || t("page.workspace_draft_chat.goal_fallback");
}
function _inferDraftRuleEnforcement(rule: any): { label: string; tone: "orange" | "red"; patterns: string[] } | null {
  return inferRuntimeRuleEnforcement(rule);
}
function _toolLabel(name: string): string {
  // "ws_propose_service" → "Propose service"
  return _humanize(name.replace(/^ws_/, "")).replace(/^./, (c) => c.toUpperCase());
}
function _toolSummary(e: WorkspaceArchitectToolEvent): string {
  const s = e.summary || {};
  if (e.ok === false) return String(s.error || "rejected");
  for (const k of ["service_key", "goal_key", "rule_key", "automation_key", "channel_type", "agent_name"] as const) {
    if (s[k]) return _friendlyValue(String(s[k]));
  }
  if (s.p0 !== undefined || s.p1 !== undefined) return `P0: ${s.p0 ?? 0}, P1: ${s.p1 ?? 0}`;
  if (s.ready) return t("page.workspace_draft_chat.value.ready");
  return "";
}

interface AssistantTurn {
  id: string;
  content: string;
  toolEvents: WorkspaceArchitectToolEvent[];
  meta?: WorkspaceArchitectTurnMeta;
}

function _formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}m`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
}
function _formatCredits(n: number): string {
  return t("page.workspace_draft_chat.credit_amount", { count: Math.max(0, Math.round(n)).toLocaleString() });
}
function _optionalCreditValue(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(String(value).replace(/,/g, ""));
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return Math.floor(parsed);
}
function _formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${(s - m * 60).toFixed(0)}s`;
}

/* ── Page ──────────────────────────────────────────────────────────── */

export default function WorkspaceDraftChat() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const draftIdParam = searchParams.get("draft");
  const initialBriefParam = searchParams.get("brief")?.trim() || "";
  const [draftId, setDraftId] = useState<string | null>(draftIdParam);
  const [input, setInput] = useState("");
  const msgsRef = useRef<HTMLDivElement>(null);
  const draftChatRef = useRef<HTMLDivElement>(null);
  const startedRef = useRef(false);

  /* Existing-draft fetch */
  const { data: existingDraft, isLoading: loadingExisting } = useQuery({
    queryKey: ["workspace-draft", draftId],
    queryFn: () => api.workspaceDrafts.get(draftId!),
    enabled: !!draftId,
  });

  const [draft, setDraft] = useState<WorkspaceDraft | null>(null);
  /** Visible chat messages — rendered as bubbles. Assistant messages
   *  also carry their construction log (tool events) + turn metadata
   *  (tokens / duration). */
  const [messages, setMessages] = useState<(WorkspaceDraftMessage & {
    toolEvents?: WorkspaceArchitectToolEvent[];
    meta?: WorkspaceArchitectTurnMeta;
  })[]>([]);
  const [streaming, setStreaming] = useState(false);
  /** Structured plan-limit detail surfaced when the gate refuses
   *  draft creation upfront. When set, the page renders a dedicated
   *  "limit reached" screen instead of an empty chat. */
  const [planLimit, setPlanLimit] = useState<PlanLimitDetail | null>(null);
  /** Tool events for the *currently streaming* assistant turn. */
  const liveEventsRef = useRef<WorkspaceArchitectToolEvent[]>([]);
  const [liveEvents, setLiveEvents] = useState<WorkspaceArchitectToolEvent[]>([]);
  /** Meta for the in-flight turn (filled at end-of-turn just before done). */
  const liveMetaRef = useRef<WorkspaceArchitectTurnMeta | null>(null);
  const [showAllLogs, setShowAllLogs] = useState<Record<string, boolean>>({});
  const [budgetEditing, setBudgetEditing] = useState(false);

  useEffect(() => {
    if (existingDraft) {
      setDraft(existingDraft);
      setMessages(existingDraft.messages.map((m) => ({ ...m })));
    }
  }, [existingDraft]);

  /* ── Streaming handlers ── */
  const appendToken = (chunk: string) => {
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (last && last.role === "assistant") {
        updated[updated.length - 1] = { ...last, content: last.content + chunk };
      }
      return updated;
    });
  };
  const resetStreamBuffer = () => {
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (last && last.role === "assistant") {
        updated[updated.length - 1] = { ...last, content: "" };
      }
      return updated;
    });
  };
  const onToolStart = (e: WorkspaceArchitectToolEvent) => {
    liveEventsRef.current = [...liveEventsRef.current, e];
    setLiveEvents([...liveEventsRef.current]);
  };
  const onToolEnd = (e: WorkspaceArchitectToolEvent) => {
    // Merge by step number — replace the matching tool_start with the
    // completed event so the log doesn't double up.
    liveEventsRef.current = liveEventsRef.current.map((prev) =>
      prev.step === e.step && prev.name === e.name ? { ...prev, ...e } : prev,
    );
    setLiveEvents([...liveEventsRef.current]);
  };
  const onTurnMeta = (m: WorkspaceArchitectTurnMeta) => {
    liveMetaRef.current = m;
  };
  const flushLiveEventsToLastAssistant = () => {
    const collected = liveEventsRef.current;
    const meta = liveMetaRef.current;
    liveEventsRef.current = [];
    liveMetaRef.current = null;
    setLiveEvents([]);
    if (collected.length === 0 && !meta) return;
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (last && last.role === "assistant") {
        updated[updated.length - 1] = {
          ...last,
          toolEvents: collected.length > 0 ? collected : last.toolEvents,
          meta: meta || last.meta,
        };
      }
      return updated;
    });
  };

  /* ── Mutations ── */
  const createMutation = useMutation({
    mutationFn: async (initial_brief?: string) => {
      setStreaming(true);
      liveEventsRef.current = [];
      setLiveEvents([]);
      setMessages([{ role: "assistant", content: "" }]);
      try {
        return await api.workspaceDrafts.createStream(
          initial_brief ? { initial_brief } : {},
          { onToken: appendToken, onReset: resetStreamBuffer, onToolStart, onToolEnd, onTurnMeta },
        );
      } finally {
        setStreaming(false);
      }
    },
    onSuccess: (turn) => {
      flushLiveEventsToLastAssistant();
      setDraft(turn.draft);
      setSearchParams({ draft: turn.draft.id }, { replace: true });
      setDraftId(turn.draft.id);
    },
    onError: (err: Error) => {
      // 402 with structured detail = plan limit reached. Don't toast --
      // the page will swap to a dedicated plan-limit screen below.
      if (err instanceof ApiError && err.status === 402 && err.detail) {
        setPlanLimit(err.detail as unknown as PlanLimitDetail);
        setMessages([]);
        liveEventsRef.current = [];
        setLiveEvents([]);
        return;
      }
      toast.error(t("page.workspace_draft_chat.could_not_start_workspace_draft"), err.message);
      setMessages([]);
      liveEventsRef.current = [];
      setLiveEvents([]);
    },
  });

  useEffect(() => {
    if (!draftId && !startedRef.current && !createMutation.isPending) {
      startedRef.current = true;
      createMutation.mutate(initialBriefParam || undefined);
    }
  }, [draftId, initialBriefParam, createMutation]);

  const sendMutation = useMutation({
    mutationFn: async ({ id, message }: { id: string; message: string }) => {
      setStreaming(true);
      liveEventsRef.current = [];
      setLiveEvents([]);
      setMessages((prev) => [
        ...prev,
        { role: "user", content: message },
        { role: "assistant", content: "" },
      ]);
      try {
        return await api.workspaceDrafts.sendMessageStream(id, message, {
          onToken: appendToken, onReset: resetStreamBuffer, onToolStart, onToolEnd, onTurnMeta,
        });
      } finally {
        setStreaming(false);
      }
    },
    onSuccess: (turn) => {
      flushLiveEventsToLastAssistant();
      setDraft(turn.draft);
    },
    onError: (err: Error) => {
      toast.error(t("page.workspace_draft_chat.send_failed"), err.message);
      setMessages((prev) => prev.slice(0, -2));
      liveEventsRef.current = [];
      setLiveEvents([]);
    },
  });

  const applyMutation = useMutation({
    mutationFn: ({ id, blueprint_id }: { id: string; blueprint_id: string }) =>
      api.workspaceDrafts.applyBlueprint(id, blueprint_id),
    onSuccess: (updated) => {
      setDraft(updated);
      setMessages(updated.messages.map((m) => ({ ...m })));
      toast.success(t("page.workspace_draft_chat.blueprint_applied"));
    },
    onError: (err: Error) => toast.error(t("page.workspace_draft_chat.could_not_apply_blueprint"), err.message),
  });

  /* ── Finalize progress ── */
  const [finalizeSteps, setFinalizeSteps] = useState<FinalizeProgressEvent[]>([]);
  const [finalizeWorkspaceId, setFinalizeWorkspaceId] = useState<string | null>(null);
  const [strategistEta, setStrategistEta] = useState<number | null>(null);
  const finalizeMutation = useMutation({
    mutationFn: async (id: string) => {
      setFinalizeSteps([]);
      setFinalizeWorkspaceId(null);
      setStrategistEta(null);
      return await api.workspaceDrafts.finalizeStream(id, {
        onProgress: (e) => {
          setFinalizeSteps((prev) => [...prev, e]);
        },
        onDone: (final) => {
          setFinalizeWorkspaceId(final.workspace_id);
          const eta = (final as any).strategist_eta_seconds;
          if (typeof eta === "number") setStrategistEta(eta);
        },
      });
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      toast.success(t("page.workspace_draft_chat.workspace_created"));
      // Hold the user on the progress UI for ~strategistEta seconds so
      // they see the Strategist countdown, then navigate.
      const eta = strategistEta ?? 5;
      setTimeout(() => {
        navigate(`/workspaces/${res.workspace_id}?created=1`);
      }, Math.max(2000, eta * 1000));
    },
    onError: (err: Error) => toast.error(t("page.workspace_draft_chat.could_not_create_workspace"), err.message),
  });

  /* Auto-scroll */
  useEffect(() => {
    if (msgsRef.current) {
      msgsRef.current.scrollTop = msgsRef.current.scrollHeight;
    }
  }, [messages.length, messages[messages.length - 1]?.content, liveEvents.length, streaming]);

  function focusComposer() {
    requestAnimationFrame(() => {
      const editor = draftChatRef.current?.querySelector<HTMLElement>(".chat-composer-rich-editor");
      editor?.focus();
    });
  }

  function setComposerPrompt(prompt: string) {
    setInput(prompt);
    focusComposer();
  }

  function formatComposerMessage(
    rawText: string,
    attachments: AttachedItem[] = [],
    manualSkills: ManualSkillItem[] = [],
  ) {
    const text = stripManualSkillTokens(rawText, manualSkills).trim();
    const attachmentText = attachments.length > 0
      ? `[${t("component.workspace_chat.attached")}: ${attachments.map((f) => f.name).join(", ")}]`
      : "";
    const skillText = manualSkills.length > 0
      ? `[${t("component.chat_input_footer.skill")}: ${manualSkills.map(manualSkillLabel).join(", ")}]`
      : "";
    return [text, attachmentText, skillText].filter(Boolean).join("\n\n").trim();
  }

  function send(
    rawText = input,
    attachments: AttachedItem[] = [],
    manualSkills: ManualSkillItem[] = [],
  ) {
    if (!draftId) return;
    const v = formatComposerMessage(rawText, attachments, manualSkills);
    if (!v || sendMutation.isPending || streaming) return;
    setInput("");
    sendMutation.mutate({ id: draftId, message: v });
  }

  /* ── Sidebar field helpers ─────────────────────────────────────── */
  const patchFields = async (patch: Record<string, any>) => {
    if (!draftId) return;
    try {
      await api.workspaceDrafts.updateFields(draftId, patch);
      const fresh = await api.workspaceDrafts.get(draftId);
      setDraft(fresh);
    } catch {}
  };

  const removeFromArray = (key: string, idx: number) => {
    const arr = ((draft?.fields as any)?.[key] || []) as any[];
    patchFields({ [key]: arr.filter((_: any, i: number) => i !== idx) });
  };

  /* Derived */
  const lastIsAssistant = messages[messages.length - 1]?.role === "assistant";
  const lastContent = messages[messages.length - 1]?.content || "";
  const isStarting =
    (createMutation.isPending && !lastIsAssistant) ||
    (loadingExisting && !draft);
  const finalized = draft?.status === "finalized";
  const showStarterPrompts =
    !finalized &&
    !streaming &&
    !sendMutation.isPending &&
    messages.length > 0 &&
    messages.every((m) => m.role !== "user");

  const fields = (draft?.fields || {}) as Record<string, any>;
  const services = (fields.services as any[]) || [];
  const goals = (fields.goals as any[]) || [];
  const agentMappings = (fields.agent_mappings as any[]) || [];
  const rules = (fields.rules as any[]) || [];
  const automations = (fields.automations as any[]) || [];
  const channelConfig = (fields.channel_config as Record<string, any>) || {};
  const budgetPolicy = (fields.budget_policy as Record<string, any>) || {};
  const monthlyBudgetCredits = _optionalCreditValue(budgetPolicy.monthly_budget_credits);
  const autoPauseOnBudget = budgetPolicy.auto_pause_on_budget !== false;
  const identityDetails = [
    fields.kind ? _friendlyValue(fields.kind) : null,
    fields.primary_work ? String(fields.primary_work) : null,
  ].filter(Boolean).join(" · ");
  const budgetSummary = monthlyBudgetCredits
    ? _formatCredits(monthlyBudgetCredits)
    : t("page.workspace_draft_chat.no_monthly_credit_cap");

  const mappingByService = new Map<string, any>();
  for (const m of agentMappings) {
    if (m?.service_key) mappingByService.set(m.service_key, m);
  }

  // Plan-limit short-circuit -- swap the whole creation UI for a
  // friendly explanation so the user knows up-front WHY they can't
  // start a new draft, plus what to do about it.
  if (planLimit) {
    return (
      <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "1rem", overflow: "hidden", gap: 16 }}>
        <style>{CHAT_STYLES}</style>
        <PageHeader title={t("page.workspaces.create_workspace")} subtitle={t("page.workspace_draft_chat.plan_limit_reached")} />
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
          <GlassCard hoverable={false} className="!p-8" >
            <div style={{ maxWidth: 480 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
                <div style={{
                  width: 44, height: 44, borderRadius: 14,
                  background: "rgba(207, 155, 68, 0.15)",
                  color: "#936027",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 22, fontWeight: 800,
                }}>!</div>
                <div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: "var(--text-strong)" }}>
                    {t("page.workspace_draft_chat.workspace_limit_reached")}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    {t("page.workspace_draft_chat.on_the")} {planLimit.plan} {t("page.workspace_draft_chat.plan")}
                  </div>
                </div>
              </div>

              {/* Numbers */}
              <div style={{
                display: "flex", gap: 10, marginBottom: 18,
              }}>
                <div className="draft-side-surface" style={{
                  flex: 1,
                  padding: "12px 14px",
                  background: "rgba(250, 250, 249, 0.6)",
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 12,
                }}>
                  <div style={LABEL}>{t("page.workspace_draft_chat.used")}</div>
                  <div style={{ fontSize: 24, fontWeight: 800, color: "var(--text-strong)" }}>
                    {planLimit.current ?? "?"}
                  </div>
                </div>
                <div className="draft-side-surface" style={{
                  flex: 1,
                  padding: "12px 14px",
                  background: "rgba(250, 250, 249, 0.6)",
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 12,
                }}>
                  <div style={LABEL}>{t("page.workspace_draft_chat.plan_limit")}</div>
                  <div style={{ fontSize: 24, fontWeight: 800, color: "var(--text-strong)" }}>
                    {planLimit.limit ?? "∞"}
                  </div>
                </div>
              </div>

              <p style={{ fontSize: 14, color: "var(--text-default)", lineHeight: 1.6, margin: "0 0 20px" }}>
                {planLimit.message ||
                  `You've reached the ${planLimit.plan} plan limit of ${planLimit.limit ?? "?"} workspaces.`}
              </p>

              <p style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.5, margin: "0 0 20px" }}>
                {t("page.workspace_draft_chat.you_can_either_upgrade_your_plan_or_archive_an_e")}
              </p>

              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                <Button variant="primary" onClick={() => navigate("/account")}>
                  {t("page.workspace_draft_chat.upgrade_plan")}
                </Button>
                <Button variant="outline" onClick={() => navigate("/workspaces")}>
                  {t("page.workspace_draft_chat.manage_workspaces")}
                </Button>
                <Button variant="ghost" onClick={() => navigate("/workspaces")}>
                  {t("action.cancel")}
                </Button>
              </div>
            </div>
          </GlassCard>
        </div>
      </div>
    );
  }

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "1rem", overflow: "hidden", gap: 16 }}>
      <style>{CHAT_STYLES}</style>

      <PageHeader
        title={t("page.workspaces.create_workspace")}
        subtitle={t("page.workspace_draft_chat.tell_me_what_you_want_to_build_i_ll_draft_the_op")}
      >
        <Button
          variant="outline"
          onClick={() => {
            if (draft && !finalized) {
              queryClient.invalidateQueries({ queryKey: ["workspace-drafts"] });
              toast.success(t("page.workspace_draft_chat.draft_saved"), t("page.workspace_draft_chat.resume_from_workspaces_page_anytime"));
            }
            navigate("/workspaces");
          }}
        >
          {draft && !finalized ? t("page.workspace_draft_chat.save_and_exit") : t("action.cancel")}
        </Button>
      </PageHeader>

      <div className="draft-shell">
        {/* ── Chat ── */}
        <div className="draft-chat" ref={draftChatRef}>
          <div className="draft-msgs" ref={msgsRef}>
            {isStarting ? (
              <div style={{ display: "flex", alignItems: "center", gap: 12, color: "#a8a29e", padding: 12 }}>
                <LoadingSpinner size={18} />
                <span style={{ fontSize: 14 }}>{t("page.workspace_draft_chat.starting_your_workspace_draft")}</span>
              </div>
            ) : (
              <>
                {messages.map((m, i) => {
                  const isLast = i === messages.length - 1;
                  const isStreamingThis = isLast && streaming && m.role === "assistant";
                  const showLog = !!showAllLogs[`m-${i}`];
                  const events = isStreamingThis ? liveEvents : (m.toolEvents || []);
                  return (
                    <div key={i} className={`draft-msg-row ${m.role}`}>
                      <MessageBubble
                        role={m.role === "user" ? "user" : "other"}
                        className="draft-msg-bubble"
                      >
                        {m.role === "assistant" && m.content === "" && isStreamingThis ? (
                          <span className="draft-typing"><span /><span /><span /></span>
                        ) : m.role === "assistant" ? (
                          <ChatMarkdown content={m.content} />
                        ) : (
                          m.content
                        )}

                        {/* Live strip while streaming */}
                        {isStreamingThis && events.length > 0 && (
                          <div className="draft-live-strip">
                            <div>
                              <span className="pulse" />
                              <strong>{_toolLabel(events[events.length - 1].name)}</strong>
                              {(() => {
                                const sum = _toolSummary(events[events.length - 1]);
                                return sum ? <span> · {sum}</span> : null;
                              })()}
                            </div>
                            <div style={{ color: "#436b65", opacity: 0.7 }}>
                              {events.length} {t("page.flows.step")}{events.length === 1 ? "" : "s"} · {events.filter(e => e.ok === false).length} {t("page.workspace_draft_chat.error")}{events.filter(e => e.ok === false).length === 1 ? "" : "s"}
                            </div>
                          </div>
                        )}

                        {/* Past turn's collapsed construction log */}
                        {!isStreamingThis && (events.length > 0 || m.meta) && (
                          <div className="draft-build-log">
                            <button
                              className="draft-build-toggle"
                              onClick={() => setShowAllLogs((s) => ({ ...s, [`m-${i}`]: !s[`m-${i}`] }))}
                            >
                              {showLog ? "▾" : "▸"}
                              {events.length > 0 && (
                                <> {t("page.workspace_draft_chat.construction")} {events.length} {t("page.flows.step")}{events.length === 1 ? "" : "s"}</>
                              )}
                              {m.meta && (
                                <span style={{ color: "#a8a29e", fontWeight: 600, marginLeft: 8 }}>
                                  {events.length === 0 ? t("page.workspace_draft_chat.turn") : "·"} {_formatTokens(m.meta.total_tokens)} {t("page.workspace_draft_chat.tok")} {_formatMs(m.meta.duration_ms)}
                                  {m.meta.rounds > 1 && (
                                    <> · {m.meta.rounds} {t("page.workspace_draft_chat.rounds")}</>
                                  )}
                                </span>
                              )}
                            </button>
                            {showLog && (
                              <>
                                {m.meta && (
                                  <div className="draft-meta-panel" style={{
                                    marginTop: 6, padding: "6px 8px",
                                    background: "rgba(67,107,101,0.04)",
                                    border: "1px solid rgba(67,107,101,0.12)",
                                    borderRadius: 8,
                                    fontSize: 11, color: "#57534e",
                                    display: "flex", flexWrap: "wrap", gap: 12,
                                  }}>
                                    <span><strong style={{ color: "#1c1917" }}>{_formatTokens(m.meta.total_tokens)}</strong> {t("page.workspace_draft_chat.tokens")}</span>
                                    <span><strong style={{ color: "#1c1917" }}>{_formatTokens(m.meta.prompt_tokens)}</strong> {t("page.workspace_draft_chat.prompt")}</span>
                                    <span><strong style={{ color: "#1c1917" }}>{_formatTokens(m.meta.completion_tokens)}</strong> {t("page.workspace_draft_chat.completion")}</span>
                                    <span><strong style={{ color: "#1c1917" }}>{_formatMs(m.meta.duration_ms)}</strong></span>
                                    <span><strong style={{ color: "#1c1917" }}>{m.meta.rounds}</strong> {t("page.workspace_draft_chat.round")}{m.meta.rounds === 1 ? "" : "s"}</span>
                                    {m.meta.tool_calls > 0 && (
                                      <span><strong style={{ color: "#1c1917" }}>{m.meta.tool_calls}</strong> {t("page.workspace_draft_chat.tool_calls")}</span>
                                    )}
                                    {m.meta.model && (
                                      <span style={{ color: "#a8a29e" }}>· {m.meta.model}</span>
                                    )}
                                  </div>
                                )}
                                {events.length > 0 && (
                                  <ul className="draft-build-list" style={{ marginTop: m.meta ? 6 : 8 }}>
                                    {events.map((e, idx) => {
                                      const sum = _toolSummary(e);
                                      return (
                                        <li key={`${e.step}-${idx}`}>
                                          <span className="step">{String(e.step).padStart(2, " ")}</span>
                                          <span className={e.ok === false ? "err" : "ok"}>{e.ok === false ? "✗" : "✓"}</span>
                                          <span className="name">{_toolLabel(e.name)}</span>
                                          {sum && <span className="summary">· {sum}</span>}
                                        </li>
                                      );
                                    })}
                                  </ul>
                                )}
                              </>
                            )}
                          </div>
                        )}
                      </MessageBubble>
                    </div>
                  );
                })}
                {showStarterPrompts && (
                  <div className="draft-starters" aria-label={t("page.workspace_draft_chat.sample_prompts")}>
                    {STARTER_PROMPTS.map((starter) => (
                      <button
                        key={starter}
                        type="button"
                        className="draft-starter-btn"
                        onClick={() => setComposerPrompt(starter)}
                      >
                        {starter}
                      </button>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          <ChatInputFooter
            value={input}
            onChange={setInput}
            enterToSend
            streaming={sendMutation.isPending || streaming}
            disabled={!draft || finalized}
            showStopButton={false}
            onSend={send}
            onStop={() => {}}
            placeholder={
              finalized ? t("page.workspace_draft_chat.workspace_already_created")
                : t("page.workspace_draft_chat.describe_your_workspace")
            }
          />
        </div>

        {/* ── Sidebar — live preview ── */}
        <div className="draft-side">
          {/* Status + summary */}
          <GlassCard hoverable={false}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <span style={LABEL}>{t("page.workspace_draft_chat.draft_summary")}</span>
              {draft && (
                <StatusBadge
                  type={
                    draft.status === "finalized" ? "purple"
                      : draft.status === "ready" ? "success"
                        : draft.status === "abandoned" ? "danger"
                          : "info"
                  }
                  dot
                  pulse={draft.status === "active"}
                >
                  {_friendlyValue(draft.status)}
                </StatusBadge>
              )}
            </div>

            {fields.name || fields.kind || fields.operating_context || fields.primary_work ? (
              <div
                className="draft-side-surface"
                style={{
                  border: "1px solid rgba(231,229,228,0.75)",
                  borderRadius: 12,
                  background: "rgba(250,250,249,0.55)",
                  padding: "12px 12px 11px",
                }}
              >
                <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ ...VALUE, fontSize: 15, fontWeight: 800 }}>
                      {fields.name || t("page.workspace_draft_chat.identity")}
                    </div>
                    {identityDetails && (
                      <div className="draft-muted" style={{ fontSize: 12, color: "#78716c", marginTop: 3, lineHeight: 1.45 }}>
                        {identityDetails}
                      </div>
                    )}
                  </div>
                  {!finalized && (
                    <button
                      onClick={() => { setComposerPrompt(t("page.workspace_draft_chat.prompt.change_workspace_name")); }}
                      className="draft-inline-action"
                      style={{ background: "none", border: "none", cursor: "pointer", color: "#78716c", fontSize: 11, fontWeight: 700, padding: "1px 2px" }}
                      title={t("page.workspace_draft_chat.edit_identity")}
                    >{t("page.workspace_draft_chat.edit")}</button>
                  )}
                </div>
                {fields.operating_context && (
                  <div className="draft-muted" style={{ fontSize: 12, color: "#57534e", lineHeight: 1.45, marginTop: 8 }}>
                    {fields.operating_context}
                  </div>
                )}
              </div>
            ) : (
              <p style={{ fontSize: 13, color: "#57534e", margin: 0, lineHeight: 1.5 }}>
                {t("page.workspace_draft_chat.tell_me_what_you_re_building_i_ll_fill_these_in")}
              </p>
            )}

            {/* Missing chips */}
            {draft?.missing && draft.missing.length > 0 && !finalized && (
              <div style={{ marginTop: 14 }}>
                <div style={LABEL}>{t("page.workspace_draft_chat.still_needed")}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {draft.missing.map((m) => (
                    <Chip key={m} variant="orange" size="sm">{_friendlyFieldLabel(m)}</Chip>
                  ))}
                </div>
              </div>
            )}
          </GlassCard>

          {/* Credit budget */}
          {draft && (
          <GlassCard hoverable={false}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <span style={LABEL}>{t("page.workspace_draft_chat.budget")}</span>
              {!finalized && (
                <button
                  onClick={() => setBudgetEditing((open) => !open)}
                  className="draft-inline-action"
                  style={{ background: "none", border: "none", cursor: "pointer", color: "#78716c", fontSize: 11, fontWeight: 700, padding: "0 4px" }}
                  title={t("page.workspace_draft_chat.edit_budget")}
                >{budgetEditing ? t("common.done") : t("page.workspace_draft_chat.edit")}</button>
              )}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              <Chip variant="slate" size="sm">{budgetSummary}</Chip>
              <Chip variant="slate" size="sm">
                {autoPauseOnBudget
                  ? t("page.workspace_draft_chat.auto_pause_on")
                  : t("page.workspace_draft_chat.auto_pause_off")}
              </Chip>
            </div>
            {!finalized && budgetEditing && (
              <div style={{ marginTop: 10, display: "grid", gap: 8 }}>
                <input
                  className="manor-input"
                  type="number"
                  min="0"
                  step="1"
                  placeholder={t("page.workspace_draft_chat.monthly_credit_cap_placeholder")}
                  defaultValue={monthlyBudgetCredits ?? ""}
                  key={`draft-budget-${monthlyBudgetCredits ?? "none"}`}
                  onBlur={(e) => {
                    const value = e.currentTarget.value.trim();
                    const parsed = value === "" ? null : Number(value);
                    if (parsed !== null && (!Number.isFinite(parsed) || parsed < 0)) {
                      toast.error(t("page.workspace_detail.invalid_amount"), t("page.workspace_detail.enter_non_negative_number_or_leave_empty"));
                      e.currentTarget.value = monthlyBudgetCredits?.toString() ?? "";
                      return;
                    }
                    const next = parsed === null || parsed <= 0 ? null : Math.floor(parsed);
                    if (next === monthlyBudgetCredits) return;
                    patchFields({
                      budget_policy: {
                        ...budgetPolicy,
                        monthly_budget_credits: next,
                        auto_pause_on_budget: autoPauseOnBudget,
                      },
                    });
                  }}
                />
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, fontSize: 12, color: "#57534e" }}>
                  <span>{t("page.workspace_draft_chat.auto_pause_at_cap")}</span>
                  <Toggle
                    checked={autoPauseOnBudget}
                    size="sm"
                    aria-label={t("page.workspace_draft_chat.auto_pause_at_cap")}
                    onChange={() => patchFields({
                      budget_policy: {
                        ...budgetPolicy,
                        monthly_budget_credits: monthlyBudgetCredits,
                        auto_pause_on_budget: !autoPauseOnBudget,
                      },
                    })}
                  />
                </div>
              </div>
            )}
          </GlassCard>
          )}

          {/* Services & Agents */}
          {services.length > 0 && (
            <GlassCard hoverable={false}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <span style={LABEL}>{t("page.workspace_draft_chat.services_matched_agents")} {services.length}</span>
                {!finalized && (
                  <button
                    onClick={() => { setComposerPrompt(t("page.workspace_draft_chat.prompt.add_new_service")); }}
                    style={{ background: "none", border: "none", cursor: "pointer", color: "#436b65", fontSize: 18, fontWeight: 700, lineHeight: 1, padding: "0 4px" }}
                    title={t("page.workspace_draft_chat.add_service")}
                  >+</button>
                )}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {services.map((svc, i) => {
                  const sk = svc.service_key || svc.key;
                  const m = sk ? mappingByService.get(sk) : null;
                  const agentName = m?.recommended_agent_name || m?.agent_name;
                  const isCustom = m?.strategy === "create_custom";
                  return (
                    <div
                      className="draft-side-surface"
                      key={sk || i}
                      style={{
                        padding: "10px 12px",
                        borderRadius: 10,
                        background: "rgba(250,250,249,0.6)",
                        border: "1px solid rgba(28,25,23,0.06)",
                        position: "relative",
                      }}
                    >
                      {!finalized && (
                        <button
                          onClick={() => removeFromArray("services", i)}
                          className="draft-remove"
                          style={{ position: "absolute", top: 6, right: 8, background: "none", border: "none", cursor: "pointer", color: "#a8a29e", fontSize: 14, lineHeight: 1 }}
                          title={t("page.workspace_draft_chat.remove_service")}
                        >×</button>
                      )}
                      <div style={{ ...VALUE, fontWeight: 700, fontSize: 13 }}>{_serviceLabel(svc)}</div>
                      {svc.description && (
                        <div className="draft-muted" style={{ fontSize: 11, color: "#78716c", marginTop: 2, lineHeight: 1.4 }}>
                          {svc.description}
                        </div>
                      )}
                      <div style={{ marginTop: 6, display: "flex", alignItems: "center", gap: 6 }}>
                        {agentName ? (
                          <Chip variant={isCustom ? "purple" : "teal"} size="sm">
                            {isCustom ? "↟ " : ""}{agentName}
                          </Chip>
                        ) : isCustom ? (
                          <Chip variant="purple" size="sm">{t("page.workspace_draft_chat.custom_agent")}</Chip>
                        ) : (
                          <StatusBadge type="warning" dot>{t("page.workspace_draft_chat.unmapped")}</StatusBadge>
                        )}
                        {svc.autonomy_level && (
                          <Chip variant="slate" size="sm">{_friendlyValue(svc.autonomy_level)}</Chip>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </GlassCard>
          )}

          {/* Staff assigned */}
          {(() => {
            const staff = ((fields.staff_assignments as any[]) || []).filter(Boolean);
            if (staff.length === 0) return null;
            return (
              <GlassCard hoverable={false}>
                <div style={LABEL}>{t("page.workspace_draft_chat.staff")} {staff.length}</div>
                <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
                  {staff.map((s: any, i: number) => (
                    <li key={i} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-strong)" }}>
                        {s.staff_name || s.staff_id}
                      </span>
                      <span style={{ display: "flex", gap: 4 }}>
                        {s.service_key && <Chip variant="slate" size="sm">{_friendlyValue(s.service_key)}</Chip>}
                        <Chip variant="teal" size="sm">{_friendlyValue(s.role || "member")}</Chip>
                      </span>
                    </li>
                  ))}
                </ul>
              </GlassCard>
            );
          })()}

          {/* Knowledge groups — toggleable approval */}
          {(() => {
            const ks = ((fields.knowledge_attachments as any[]) || []).filter(Boolean);
            if (ks.length === 0) return null;

            const toggleKnowledge = async (idx: number) => {
              const updated = ks.map((k: any, i: number) =>
                i === idx ? { ...k, approved: !(k.approved !== false) } : k
              );
              try {
                await api.workspaceDrafts.updateFields(draftId!, { knowledge_attachments: updated });
                // Refetch draft to update sidebar
                const fresh = await api.workspaceDrafts.get(draftId!);
                setDraft(fresh);
              } catch {}
            };

            return (
              <GlassCard hoverable={false}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div style={LABEL}>{t("page.workspace_draft_chat.knowledge")} {ks.filter((k: any) => k.approved !== false).length}/{ks.length} {t("page.onboarding.selected")}</div>
                  {!finalized && (
                    <button
                      onClick={() => { setComposerPrompt(t("page.workspace_draft_chat.prompt.add_knowledge_source")); }}
                      style={{ background: "none", border: "none", cursor: "pointer", color: "#436b65", fontSize: 18, fontWeight: 700, lineHeight: 1, padding: "0 4px" }}
                      title={t("page.workspace_draft_chat.add_knowledge")}
                    >+</button>
                  )}
                </div>
                <p style={{ ...SUBTLE, margin: "0 0 8px", lineHeight: 1.45 }}>
                  {t("page.workspace_draft_chat.selected_sources_become_default_runtime_knowledg")}
                </p>
                <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
                  {ks.map((k: any, i: number) => {
                    const approved = k.approved !== false;
                    return (
                      <li key={i}
                        className="draft-side-surface"
                        onClick={() => toggleKnowledge(i)}
                        style={{
                          padding: "6px 8px", borderRadius: 8, cursor: "pointer",
                          background: approved ? "rgba(242,246,245,0.6)" : "rgba(250,250,249,0.4)",
                          border: approved ? "1px solid rgba(67,107,101,0.2)" : "1px solid rgba(231,229,228,0.4)",
                          opacity: approved ? 1 : 0.6,
                          transition: "all 0.15s",
                          display: "flex", alignItems: "flex-start", gap: 8,
                        }}
                      >
                        <div style={{
                          width: 18, height: 18, borderRadius: 4, flexShrink: 0, marginTop: 1,
                          border: approved ? "2px solid #436b65" : "2px solid #d6d3d1",
                          background: approved ? "#436b65" : "transparent",
                          display: "flex", alignItems: "center", justifyContent: "center",
                        }}>
                          {approved && (
                            <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="#fff" strokeWidth={3}><path strokeLinecap="round" d="M20 6L9 17l-5-5" /></svg>
                          )}
                        </div>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-strong)" }}>{k.name}</div>
                          {k.purpose && (
                            <div className="draft-muted" style={{ fontSize: 11, color: "#78716c", marginTop: 2, lineHeight: 1.4 }}>{k.purpose}</div>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </GlassCard>
            );
          })()}

          {/* Goals */}
          {goals.length > 0 && (
            <GlassCard hoverable={false}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div style={LABEL}>{t("page.workspace_draft_chat.goals")} {goals.length}</div>
                {!finalized && (
                  <button
                    onClick={() => { setComposerPrompt(t("page.workspace_draft_chat.prompt.add_goal")); }}
                    style={{ background: "none", border: "none", cursor: "pointer", color: "#436b65", fontSize: 18, fontWeight: 700, lineHeight: 1, padding: "0 4px" }}
                    title={t("page.workspace_draft_chat.add_goal")}
                  >+</button>
                )}
              </div>
              <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 8 }}>
                {goals.map((g, i) => (
                  <li key={i} className="draft-side-surface" style={{
                    padding: "8px 10px",
                    borderRadius: 10,
                    background: "rgba(250,250,249,0.6)",
                    border: "1px solid rgba(28,25,23,0.06)",
                    position: "relative",
                  }}>
                    {!finalized && (
                      <button
                        onClick={() => removeFromArray("goals", i)}
                        className="draft-remove"
                        style={{ position: "absolute", top: 6, right: 8, background: "none", border: "none", cursor: "pointer", color: "#a8a29e", fontSize: 14, lineHeight: 1 }}
                        title={t("page.workspace_draft_chat.remove_goal")}
                      >×</button>
                    )}
                    <div style={{ ...VALUE, fontWeight: 700, fontSize: 13 }}>{_goalLabel(g)}</div>
                    {g.description && _goalLabel(g) !== g.description && (
                      <div className="draft-muted" style={{ fontSize: 11, color: "#78716c", marginTop: 2, lineHeight: 1.4 }}>
                        {g.description}
                      </div>
                    )}
                    <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 6 }}>
                      {g.target && <Chip variant="green" size="sm">{t("page.workspace_draft_chat.target")} {g.target}</Chip>}
                      {g.cadence && <Chip variant="blue" size="sm">{g.cadence}</Chip>}
                      {g.metric_key && <Chip variant="slate" size="sm">{_friendlyValue(g.metric_key)}</Chip>}
                    </div>
                  </li>
                ))}
              </ul>
            </GlassCard>
          )}

          {/* Missing integrations — surfaced from architect's missing_integrations / flagged_integrations */}
          {(() => {
            const flagged = ((fields.flagged_integrations as any[]) || []).filter(Boolean);
            if (flagged.length === 0) return null;
            return (
              <GlassCard hoverable={false} className="border-amber-200">
                <div style={{ ...LABEL, color: "#936027" }}>
                  {t("page.workspace_draft_chat.needs_setup")} {flagged.length} {t("page.apps.integration")}{flagged.length === 1 ? "" : "s"}
                </div>
                <p style={{ ...SUBTLE, margin: "4px 0 10px", lineHeight: 1.5, color: "#76502c" }}>
                  {t("page.workspace_draft_chat.these_integrations_weren_t_found_in_your_account")}
                </p>
                <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 8 }}>
                  {flagged.map((f: any, i: number) => (
                    <li key={i} className="draft-side-surface draft-warning-surface" style={{
                      padding: "8px 10px",
                      borderRadius: 10,
                      background: "rgba(243, 236, 214, 0.4)",
                      border: "1px solid rgba(207, 155, 68, 0.3)",
                    }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "space-between" }}>
                        <span style={{ ...VALUE, fontSize: 13 }}>{_humanize(f.provider)}</span>
                        {f.required && <Chip variant="red" size="sm">{t("page.login.required")}</Chip>}
                      </div>
                      {f.purpose && (
                        <div className="draft-muted" style={{ fontSize: 11, color: "#76502c", marginTop: 4, lineHeight: 1.4 }}>
                          {f.purpose}
                        </div>
                      )}
                      {Array.isArray(f.linked_service_keys) && f.linked_service_keys.length > 0 && (
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
                          {f.linked_service_keys.map((sk: string) => (
                            <Chip key={sk} variant="orange" size="sm">{_friendlyValue(sk)}</Chip>
                          ))}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </GlassCard>
            );
          })()}

          {/* Channels */}
          {(channelConfig.primary_external_channel?.channel_type ||
            channelConfig.internal_channel?.channel_type) && (
            <GlassCard hoverable={false}>
              <div style={LABEL}>{t("page.workspace_draft_chat.channels")}</div>
              {channelConfig.primary_external_channel?.channel_type && (
                <div className="draft-row">
                  <span className="label">{t("page.workspace_draft_chat.primary")}</span>
                  <span className="value">{_friendlyValue(channelConfig.primary_external_channel.channel_type)}</span>
                </div>
              )}
              {channelConfig.internal_channel?.channel_type && (
                <div className="draft-row">
                  <span className="label">{t("page.workspace_draft_chat.internal")}</span>
                  <span className="value">{_friendlyValue(channelConfig.internal_channel.channel_type)}</span>
                </div>
              )}
              {Array.isArray(channelConfig.secondary_external_channels) && channelConfig.secondary_external_channels.length > 0 && (
                <div className="draft-row">
                  <span className="label">{t("page.workspace_draft_chat.also")}</span>
                  <span className="value" style={{ display: "flex", flexWrap: "wrap", gap: 4, justifyContent: "flex-end" }}>
                    {channelConfig.secondary_external_channels.map((c: any, i: number) => (
                      <Chip key={i} variant="teal" size="sm">{_friendlyValue(c.channel_type)}</Chip>
                    ))}
                  </span>
                </div>
              )}
            </GlassCard>
          )}

          {/* Rules + automations as compact lists */}
          {(rules.length > 0 || automations.length > 0) && (
            <GlassCard hoverable={false}>
              {rules.length > 0 && (
                <div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={LABEL}>{t("page.workspace_draft_chat.rules")} {rules.length}</div>
                    {!finalized && (
                      <button
                        onClick={() => { setComposerPrompt(t("page.workspace_draft_chat.prompt.add_rule")); }}
                        style={{ background: "none", border: "none", cursor: "pointer", color: "#436b65", fontSize: 18, fontWeight: 700, lineHeight: 1, padding: "0 4px" }}
                        title={t("page.workspace_draft_chat.add_rule")}
                      >+</button>
                    )}
                  </div>
                  <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
                    {rules.map((r, i) => {
                      const enforcement = _inferDraftRuleEnforcement(r);
                      return (
                        <li key={i} style={{ fontSize: 12, color: "var(--text-strong)", lineHeight: 1.4, display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div>• {r.description || _friendlyValue(r.rule_key)}</div>
                            {enforcement ? (
                              <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 5 }}>
                                <Chip variant={enforcement.tone} size="sm">{enforcement.label}</Chip>
                                {enforcement.patterns.map((pattern) => (
                                  <Chip key={pattern} variant="slate" size="sm">{_friendlyActionPattern(pattern)}</Chip>
                                ))}
                              </div>
                            ) : (
                              <div style={{ ...SUBTLE, marginTop: 3 }}>
                                {t("page.workspace_draft_chat.agent_visible_rule_no_direct_runtime_action_patt")}
                              </div>
                            )}
                          </div>
                          {!finalized && (
                            <button
                              onClick={() => removeFromArray("rules", i)}
                              className="draft-remove"
                              style={{ background: "none", border: "none", cursor: "pointer", color: "#a8a29e", fontSize: 13, lineHeight: 1, flexShrink: 0 }}
                              title={t("page.workspace_draft_chat.remove_rule")}
                            >×</button>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
              {automations.length > 0 && (
                <div style={{ marginTop: rules.length > 0 ? 14 : 0 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={LABEL}>{t("page.workspace_draft_chat.automations")} {automations.length}</div>
                    {!finalized && (
                      <button
                        onClick={() => { setComposerPrompt(t("page.workspace_draft_chat.prompt.add_automation")); }}
                        style={{ background: "none", border: "none", cursor: "pointer", color: "#436b65", fontSize: 18, fontWeight: 700, lineHeight: 1, padding: "0 4px" }}
                        title={t("page.workspace_draft_chat.add_automation")}
                      >+</button>
                    )}
                  </div>
                  <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 4 }}>
                    {automations.map((a, i) => (
                      <li key={i} style={{ fontSize: 12, color: "var(--text-strong)", lineHeight: 1.4, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 4 }}>
                        <span>• {a.description || _friendlyValue(a.automation_key)}{a.trigger && <span className="draft-muted" style={{ color: "#a8a29e", marginLeft: 4 }}>· {_friendlyValue(a.trigger)}</span>}</span>
                        {!finalized && (
                          <button
                            onClick={() => removeFromArray("automations", i)}
                            className="draft-remove"
                            style={{ background: "none", border: "none", cursor: "pointer", color: "#a8a29e", fontSize: 13, lineHeight: 1, flexShrink: 0 }}
                            title={t("page.workspace_draft_chat.remove_automation")}
                          >×</button>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </GlassCard>
          )}

          {/* Blueprint suggestion */}
          {draft?.suggested_blueprint && !draft.applied_blueprint_id && (
            <GlassCard hoverable={false}>
              <div style={LABEL}>{t("page.workspace_draft_chat.suggested_template")}</div>
              <div style={{ ...VALUE, fontSize: 14, fontWeight: 700 }}>{draft.suggested_blueprint.title}</div>
              {draft.suggested_blueprint.summary && (
                <div className="draft-muted" style={{ fontSize: 12, color: "#57534e", marginTop: 4, lineHeight: 1.5 }}>
                  {draft.suggested_blueprint.summary}
                </div>
              )}
              {draft.suggested_blueprint.tags?.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 8 }}>
                  {draft.suggested_blueprint.tags.slice(0, 4).map((t) => (
                    <Chip key={t} variant="purple" size="sm">{t}</Chip>
                  ))}
                </div>
              )}
              <div style={{ marginTop: 12 }}>
                <Button
                  variant="primary"
                  size="sm"
                  loading={applyMutation.isPending}
                  onClick={() => applyMutation.mutate({ id: draft.id, blueprint_id: draft.suggested_blueprint!.id })}
                >
                  {t("page.workspace_draft_chat.use_template")}
                </Button>
              </div>
            </GlassCard>
          )}

          {/* Finalize CTA */}
          <Button
            variant="primary"
            size="lg"
            onClick={() => draft && finalizeMutation.mutate(draft.id)}
            disabled={!draft || !draft.ready || finalizeMutation.isPending || finalized}
            loading={finalizeMutation.isPending}
          >
            {finalized ? t("page.workspace_draft_chat.workspace_created")
              : draft?.ready ? t("page.workspaces.create_workspace")
                : t("page.workspace_draft_chat.keep_chatting_until_ready")}
          </Button>
        </div>
      </div>

      {/* Finalize progress overlay */}
      {(finalizeMutation.isPending || finalizeWorkspaceId) && (
        <FinalizeProgressOverlay
          steps={finalizeSteps}
          finalized={!!finalizeWorkspaceId}
          strategistEta={strategistEta}
        />
      )}
    </div>
  );
}

/* ── Finalize progress overlay ─────────────────────────────────────── */

const FINALIZE_STEPS: { key: string; label: string }[] = [
  { key: "workspace_created",          label: t("page.workspace_draft_chat.creating_workspace_record") },
  { key: "provisioning_agents_started", label: t("page.workspace_draft_chat.provisioning_agents") },
  { key: "agents_done",                label: t("page.workspace_draft_chat.agents_ready") },
  { key: "provisioning_team_and_knowledge", label: t("page.workspace_draft_chat.setting_up_team_and_knowledge") },
  { key: "team_and_knowledge_done",    label: t("page.workspace_draft_chat.team_and_knowledge_ready") },
  { key: "default_skills_seeded",      label: t("page.workspace_draft_chat.seeding_default_skills") },
  { key: "memory_seeded",              label: t("page.workspace_draft_chat.initializing_memory") },
  { key: "runtime_scheduled",          label: t("page.workspace_draft_chat.scheduling_runtime") },
  { key: "strategist_dispatched",      label: t("page.workspace_draft_chat.strategist_dispatched") },
  { key: "complete",                   label: t("page.workspace_draft_chat.workspace_fully_provisioned") },
];

function FinalizeProgressOverlay({
  steps,
  finalized,
  strategistEta,
}: {
  steps: FinalizeProgressEvent[];
  finalized: boolean;
  strategistEta: number | null;
}) {
  const seenKeys = new Set(steps.map((s) => s.step));
  const lastAgent = [...steps].reverse().find((s) => s.step === "agent_provisioned");
  const teamPayload = steps.find((s) => s.step === "team_and_knowledge_done")?.payload as
    | { staff?: number; knowledge?: number; channels?: number }
    | undefined;

  // Strategist countdown
  const [secondsLeft, setSecondsLeft] = useState<number | null>(strategistEta);
  useEffect(() => {
    if (strategistEta == null) {
      setSecondsLeft(null);
      return;
    }
    setSecondsLeft(strategistEta);
    const t = setInterval(() => {
      setSecondsLeft((s) => (s == null ? null : Math.max(0, s - 1)));
    }, 1000);
    return () => clearInterval(t);
  }, [strategistEta]);

  const completed = seenKeys.size;
  const total = FINALIZE_STEPS.length;
  const pct = Math.min(100, Math.round((completed / total) * 100));

  return createPortal(
    <div className="manor-dialog-overlay" style={{
      position: "fixed", inset: 0,
      background: "var(--modal-overlay-bg)",
      backdropFilter: "blur(5px)",
      WebkitBackdropFilter: "blur(5px)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 20000,
    }}>
      <div role="dialog" aria-modal="true" className="manor-dialog workspace-finalize-dialog" style={{
        width: "min(92vw, 480px)",
        background: "var(--modal-bg)",
        backdropFilter: "blur(20px) saturate(1.08)",
        WebkitBackdropFilter: "blur(20px) saturate(1.08)",
        borderRadius: 24,
        padding: "32px 32px 28px",
        border: "1px solid var(--modal-border)",
        boxShadow: "var(--modal-shadow)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 18 }}>
          {finalized ? (
            <div style={{
              width: 36, height: 36, borderRadius: "50%",
              background: "#e4efe8", display: "flex", alignItems: "center",
              justifyContent: "center", color: "#3d7351", fontSize: 18, fontWeight: 800,
            }}>✓</div>
          ) : (
            <LoadingSpinner size={20} />
          )}
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 17, fontWeight: 700, color: "var(--text-strong)" }}>
              {finalized ? t("page.workspace_draft_chat.workspace_ready") : t("page.workspace_draft_chat.creating_your_workspace")}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {finalized
                ? secondsLeft != null && secondsLeft > 0
                  ? `Strategist starts proposing tasks in ${secondsLeft}s — taking you in…`
                  : t("page.workspace_draft_chat.opening_workspace")
                : t("page.workspace_draft_chat.provisioning_agents_channels_knowledge_and_runtime")}
            </div>
          </div>
        </div>

        {/* Progress bar */}
        <div style={{
          height: 6, borderRadius: 999, background: "var(--modal-muted-bg)", overflow: "hidden", marginBottom: 16,
        }}>
          <div style={{
            height: "100%",
            width: `${pct}%`,
            background: "linear-gradient(90deg, #436b65, #4f9c84)",
            transition: "width 0.4s ease",
          }} />
        </div>

        {/* Step checklist */}
        <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
          {FINALIZE_STEPS.map((s, idx) => {
            const done = seenKeys.has(s.key);
            // The currently-active step is the first not-done after the last done.
            const prevDone = idx === 0 || seenKeys.has(FINALIZE_STEPS[idx - 1].key);
            const active = !done && prevDone && !finalized;
            const subtitle = (() => {
              if (s.key === "agent_provisioned" || s.key === "agents_done") {
                if (lastAgent) {
                  const p = (lastAgent.payload || {}) as Record<string, unknown>;
                  return `${p.index ?? "?"}/${p.total ?? "?"} · ${p.agent_name ?? ""}`;
                }
              }
              if (s.key === "team_and_knowledge_done" && teamPayload) {
                return [
                  `${teamPayload.staff ?? 0} staff`,
                  `${teamPayload.knowledge ?? 0} knowledge`,
                  `${teamPayload.channels ?? 0} channels`,
                ].join(" · ");
              }
              return null;
            })();
            return (
              <li key={s.key} style={{
                display: "flex", alignItems: "center", gap: 10,
                padding: "5px 0",
                opacity: done || active ? 1 : 0.45,
              }}>
                <span style={{
                  width: 18, height: 18, borderRadius: "50%",
                  background: done ? "var(--accent)" : active ? "var(--accent-soft)" : "var(--modal-muted-bg)",
                  color: done ? "#fff" : active ? "var(--accent)" : "var(--text-faint)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 11, fontWeight: 800, flexShrink: 0,
                }}>
                  {done ? "✓" : active ? "•" : ""}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, color: done ? "#1c1917" : active ? "#436b65" : "#78716c" }}>
                    {s.label}
                  </div>
                  {subtitle && (
                    <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 1 }}>{subtitle}</div>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>,
    document.body,
  );
}
