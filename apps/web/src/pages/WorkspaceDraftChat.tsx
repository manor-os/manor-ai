import { startVisiblePolling } from "../lib/visiblePolling";
import { useEffect, useRef, useState, useMemo } from "react";
import type { CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
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
import EmptyState from "../components/ui/EmptyState";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import { ChatMessagesSkeleton } from "../components/ui/Skeleton";
import ChatMarkdown from "../components/ChatMarkdown";
import MessageQuestionOptions from "../components/ui/MessageQuestionOptions";
import AssistantMessageBlocks from "../components/AssistantMessageBlocks";
import type { AssistantBlock, AssistantProcessStep } from "../lib/chatStream";
import MessageBubble from "../components/chat/MessageBubble";
import ChatInputFooter, {
  manualSkillLabel,
  stripManualSkillTokens,
  type AttachedItem,
  type ManualSkillItem,
} from "../components/ChatInputFooter";
import WorkspaceDraftConfigurationPanel from "../components/WorkspaceDraftConfigurationPanel";
import { recommendWorkspaces, type RecommendationReason } from "../lib/workspaceRecommendations";

/* ── Visual tokens ─────────────────────────────────────────────────── */

const LABEL: CSSProperties = {
  fontSize: 10, fontWeight: 800, color: "var(--text-faint)", marginBottom: 6,
};
/* ── Page-scoped CSS ───────────────────────────────────────────────── */

const CHAT_STYLES = `
  .draft-shell {
    flex: 1; min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 380px;
    gap: 20px;
  }
  @media (max-width: 1080px) {
    .draft-shell {
      grid-template-columns: 1fr;
      grid-template-rows: minmax(520px, 1fr) auto;
      overflow-y: auto;
    }
    .draft-side {
      display: flex;
      min-height: auto;
      overflow: visible;
      padding-right: 0;
    }
  }
  .draft-chat {
    display: flex; flex-direction: column; min-height: 0;
    overflow: hidden;
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
    padding: 6px 12px;
    border: none;
    border-radius: 9px;
    background: var(--surface-muted);
    color: var(--text-default);
    font-size: 13px; line-height: 1.25;
    cursor: pointer;
    transition: background 0.15s ease;
  }
  .draft-starter-btn:hover { background: var(--surface-sunken); }
  .draft-recommendations {
    display: flex; flex-direction: column; gap: 2px;
    width: min(100%, var(--chat-thread-max-width, 920px));
    margin: 0 auto;
    box-sizing: border-box;
  }
  .draft-recommendation {
    display: flex; flex-direction: column; gap: 2px;
    min-width: 0;
    padding: 10px 12px;
    border: none;
    border-radius: 10px;
    background: transparent;
    text-align: left;
    cursor: pointer;
    transition: background 0.15s ease;
  }
  .draft-recommendation:hover { background: var(--surface-muted); }
  .draft-recommendation-basis {
    margin: 6px 0 0;
    padding: 0 12px;
    color: var(--text-faint);
    font-size: 12px;
  }
  .draft-recommendation-basis a { color: var(--text-muted); text-decoration: underline; }
  .draft-recommendation-title { color: var(--text-strong); font-size: 14px; font-weight: 500; }
  .draft-recommendation-reason {
    overflow: hidden;
    color: var(--text-muted);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .draft-shell--solo { grid-template-columns: minmax(0, 1fr); }
  .draft-shell--solo .draft-side { display: none; }
  .draft-msg-row.assistant .draft-msg-bubble {
    padding: 0 !important; border: 0 !important; border-radius: 0 !important;
    background: transparent !important; box-shadow: none !important;
  }
  .draft-side  { display: flex; flex-direction: column; gap: 14px; min-height: 0; overflow-y: auto; padding-right: 4px; }
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
  .draft-turn-paused { margin: 0; color: #78716c; font-size: 12px; line-height: 1.4; }

  /* Live activity strip while a turn is streaming */
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
  html[data-theme="dark"] .draft-chat .embedded-chat-footer {
    background: transparent;
    border-top-color: rgba(255,255,255,0.1);
    padding: 14px 28px 16px;
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
const STARTER_PROMPTS = [
  t("page.workspace_draft_chat.starter.coffee_popup"),
  t("page.workspace_draft_chat.starter.client_desk"),
  t("page.workspace_draft_chat.starter.manga_serial"),
];
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
// What Manor is doing, in the reader's words: tool names are not for people.
const DRAFT_STEP_KEYS: Record<string, string> = {
  ws_ask_user: "question",
  ws_mark_ready: "finishing",
  ws_get_draft: "checking",
  ws_lint_draft: "reviewing",
  ws_read_source: "reading",
  ws_search_blueprints: "templates",
  ws_suggest_blueprint: "templates",
  ws_apply_blueprint: "applying_template",
  ws_search_capabilities: "capabilities",
  ws_search_entity_agents: "staff",
  ws_propose_agent_mapping: "staff",
  ws_assign_staff: "staff",
  ws_request_custom_agent: "staff",
  ws_commit_basics: "basics",
  ws_confirm_creation_preferences: "basics",
  ws_propose_service: "services",
  ws_propose_goal: "goals",
  ws_propose_rule: "rules",
  ws_propose_stat: "stats",
  ws_propose_workflow: "workflows",
  ws_design_workflows: "workflows",
  ws_propose_automation: "automations",
  ws_propose_channel: "connections",
  ws_flag_missing_integration: "connections",
  ws_design_app_view: "app",
  ws_accept_app_view: "app",
  ws_attach_knowledge: "knowledge",
  ws_set_autonomy: "operating",
  ws_set_budget: "operating",
  ws_set_evaluation: "operating",
};

function _draftStepKey(name: string): string {
  return `page.workspace_draft_chat.step_${DRAFT_STEP_KEYS[name] || "updating"}`;
}

/** A draft turn as the process block every chat shows. */
function _draftProcess(
  events: WorkspaceArchitectToolEvent[],
  { key, live, startedAtMs, durationMs }: { key: string; live: boolean; startedAtMs?: number; durationMs?: number },
): AssistantBlock {
  const steps: AssistantProcessStep[] = [];
  for (const event of events) {
    const displayKey = _draftStepKey(event.name);
    const status = event.ok === false ? "error" : event.ok === true ? "success" : "running";
    const last = steps[steps.length - 1];
    // Several calls of one kind in a row are one thing to the reader.
    if (last?.display_key === displayKey) {
      last.status = status;
      last.result_preview = _toolSummary(event) || last.result_preview;
    } else {
      steps.push({
        id: `${key}-${event.step}`,
        kind: "tool",
        name: event.name,
        display_key: displayKey,
        status,
        result_preview: _toolSummary(event),
      });
    }
  }
  return {
    id: key,
    type: "process",
    lifecycle_status: live ? "running" : "completed",
    default_collapsed: !live,
    started_at_ms: startedAtMs,
    duration_ms: durationMs,
    steps,
  };
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

function _hasCompletedDraftTurn(
  messages: WorkspaceDraftMessage[],
  userMessage: string,
): boolean {
  if (messages.length < 2) return false;
  const assistant = messages[messages.length - 1];
  const user = messages[messages.length - 2];
  return (
    user.role === "user" &&
    user.content.trim() === userMessage.trim() &&
    assistant.role === "assistant" &&
    assistant.content.trim().length > 0
  );
}

/** The server saved this message and is still answering it. */
function _hasPendingDraftTurn(draft: WorkspaceDraft, userMessage: string): boolean {
  const last = draft.messages[draft.messages.length - 1];
  return Boolean(draft.working) && last?.role === "user" && last.content.trim().startsWith(userMessage.trim());
}

const DRAFT_WORKING_POLL_MS = 5000;

const DRAFT_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

function recommendationBrief(blueprint: { title: string; summary: string | null }): string {
  return t("ui.workspace_recommendation_brief", { title: blueprint.title, summary: blueprint.summary || "" }).trim();
}

// A real reason when there is one; otherwise what the Workspace does.
function recommendationDetail(blueprint: { summary: string | null }, reason: RecommendationReason): string {
  if (reason.kind === "integration") return t("ui.workspace_recommendation_integration", { name: reason.name });
  if (reason.kind === "workspace") return t("ui.workspace_recommendation_workspace", { name: reason.name });
  if (reason.kind === "industry") return t("ui.workspace_recommendation_industry", { name: reason.name });
  return blueprint.summary || t("ui.workspace_recommendation_ready");
}

function draftOpeningMessage(): WorkspaceDraftMessage {
  return { role: "assistant", content: t("page.workspace_draft_chat.opening_question") };
}

function createClientDraftId(): string {
  const random = globalThis.crypto.getRandomValues(new Uint8Array(10));
  let value = BigInt(Date.now());
  for (const byte of random) value = (value << 8n) | BigInt(byte);
  let encoded = "";
  for (let index = 0; index < 26; index += 1) {
    encoded = DRAFT_ID_ALPHABET[Number(value & 31n)] + encoded;
    value >>= 5n;
  }
  return encoded;
}

/* ── Page ──────────────────────────────────────────────────────────── */

function draftHasPlan(draft: WorkspaceDraft | null | undefined): boolean {
  if (!draft) return false;
  const fields = draft.fields as Record<string, any>;
  return Boolean(
    draft.ready
    || draft.applied_blueprint_id
    || fields.name
    || fields.primary_work
    || fields.operating_context
    || fields.services?.length
    || fields.agent_mappings?.length,
  );
}

export default function WorkspaceDraftChat({ embeddedInChat = false }: { embeddedInChat?: boolean }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const exitPath = embeddedInChat ? "/chat" : "/workspaces";

  const draftIdParam = searchParams.get("draft");
  const resumingOpening = searchParams.get("opening") === "1" && !!draftIdParam;
  const initialBriefParam = searchParams.get("brief")?.trim() || "";
  const [draftId, setDraftId] = useState<string | null>(resumingOpening ? null : draftIdParam);
  const [input, setInput] = useState("");
  const msgsRef = useRef<HTMLDivElement>(null);
  const draftChatRef = useRef<HTMLDivElement>(null);
  const startedRef = useRef(false);
  const openingDraftIdRef = useRef<string | null>(resumingOpening ? draftIdParam : null);
  // A blank start asks a fixed question: no model call, and no draft until there is an answer.
  const blankStartRef = useRef(!draftIdParam && !initialBriefParam);

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
  })[]>(() => (blankStartRef.current ? [draftOpeningMessage()] : []));
  const [streaming, setStreaming] = useState(false);
  /** Structured plan-limit detail surfaced when the gate refuses
   *  draft creation upfront. When set, the page renders a dedicated
   *  "limit reached" screen instead of an empty chat. */
  const [planLimit, setPlanLimit] = useState<PlanLimitDetail | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  /** Tool events for the *currently streaming* assistant turn. */
  const liveEventsRef = useRef<WorkspaceArchitectToolEvent[]>([]);
  const [liveEvents, setLiveEvents] = useState<WorkspaceArchitectToolEvent[]>([]);
  /** Meta for the in-flight turn (filled at end-of-turn just before done). */
  const liveMetaRef = useRef<WorkspaceArchitectTurnMeta | null>(null);
  const [liveStartedAtMs, setLiveStartedAtMs] = useState<number>();
  useEffect(() => {
    if (streaming) setLiveStartedAtMs(Date.now());
  }, [streaming]);

  useEffect(() => {
    if (existingDraft) {
      setDraft(existingDraft);
      // The opening question isn't stored on the draft; an empty record must not wipe it.
      if (existingDraft.messages.length > 0) {
        const stored = existingDraft.messages.map((m) => ({ ...m }));
        setMessages(blankStartRef.current && stored[0]?.role === "user" ? [draftOpeningMessage(), ...stored] : stored);
      }
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

  /** The streamed reply has only text; the saved transcript also carries the
   *  question's answer buttons. Copy them onto the last assistant bubble. */
  const attachTurnQuestion = (saved: WorkspaceDraft) => {
    const last = [...(saved.messages || [])].reverse().find((m) => m.role === "assistant");
    if (!last?.question) return;
    setMessages((prev) => {
      const next = [...prev];
      const index = next.length - 1;
      if (next[index]?.role === "assistant") next[index] = { ...next[index], question: last.question };
      return next;
    });
  };

  /* ── Mutations ── */
  const createMutation = useMutation({
    mutationFn: async ({ initial_brief, document_ids, displayText }: { initial_brief?: string; document_ids?: string[]; displayText?: string } = {}) => {
      setStartError(null);
      setStreaming(true);
      liveEventsRef.current = [];
      setLiveEvents([]);
      setMessages([
        ...(blankStartRef.current ? [draftOpeningMessage()] : []),
        ...(initial_brief ? [{ role: "user" as const, content: displayText ?? initial_brief }] : []),
        { role: "assistant", content: "" },
      ]);
      const requestedDraftId = openingDraftIdRef.current || createClientDraftId();
      if (!openingDraftIdRef.current) {
        openingDraftIdRef.current = requestedDraftId;
        const openingParams = new URLSearchParams(searchParams);
        openingParams.set("draft", requestedDraftId);
        openingParams.set("opening", "1");
        setSearchParams(openingParams, { replace: true });
      }
      try {
        return await api.workspaceDrafts.createStream(
          {
            ...(initial_brief ? { initial_brief } : {}),
            ...(document_ids && document_ids.length ? { document_ids } : {}),
            draft_id: requestedDraftId,
          },
          {
            onStart: ({ draft_id: startedDraftId, mode }) => {
              if (mode !== "create" || !startedDraftId) return;
              openingDraftIdRef.current = startedDraftId;
              const openingParams = new URLSearchParams(searchParams);
              openingParams.set("draft", startedDraftId);
              openingParams.set("opening", "1");
              setSearchParams(openingParams, { replace: true });
            },
            onToken: appendToken,
            onReset: resetStreamBuffer,
            onToolStart,
            onToolEnd,
            onTurnMeta,
          },
        );
      } finally {
        setStreaming(false);
      }
    },
    onSuccess: (turn) => {
      setStartError(null);
      flushLiveEventsToLastAssistant();
      setDraft(turn.draft);
      attachTurnQuestion(turn.draft);
      const readyParams = embeddedInChat ? new URLSearchParams({ new: "workspace", draft: turn.draft.id }) : new URLSearchParams({ draft: turn.draft.id });
      setSearchParams(readyParams, { replace: true });
      setDraftId(turn.draft.id);
      openingDraftIdRef.current = null;
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
      liveEventsRef.current = [];
      setLiveEvents([]);
      const failStart = () => {
        setStartError(err.message);
        toast.error(t("page.workspace_draft_chat.could_not_start_workspace_draft"), err.message);
        setMessages([]);
      };
      const openingId = openingDraftIdRef.current;
      if (!openingId) {
        failStart();
        return;
      }
      // The saved opening may still be running, or have committed its reply
      // before the connection lost its done frame. Follow either draft.
      void api.workspaceDrafts.get(openingId).then((saved) => {
        const hasReply = saved.messages.some((entry) => entry.role === "assistant" && entry.content.trim());
        if (!saved.working && !hasReply) {
          failStart();
          return;
        }
        setStartError(null);
        setDraft(saved);
        setMessages(saved.messages.map((entry) => ({ ...entry })));
        setSearchParams(embeddedInChat ? new URLSearchParams({ new: "workspace", draft: saved.id }) : new URLSearchParams({ draft: saved.id }), { replace: true });
        setDraftId(saved.id);
        openingDraftIdRef.current = null;
      }).catch(failStart);
    },
  });

  useEffect(() => {
    if (!draftId && (initialBriefParam || resumingOpening) && !startedRef.current && !createMutation.isPending) {
      // Let StrictMode finish replaying mount effects before starting the
      // mutation; otherwise its observer can remain stuck in pending state.
      const startTimer = window.setTimeout(() => {
        startedRef.current = true;
        createMutation.mutate({ initial_brief: initialBriefParam || undefined });
      }, 0);
      return () => window.clearTimeout(startTimer);
    }
  }, [draftId, initialBriefParam, resumingOpening, createMutation.mutate, createMutation.isPending]);

  const sendAbortRef = useRef<AbortController>(new AbortController());
  const sendMutation = useMutation({
    mutationFn: async ({
      id,
      message,
      document_ids,
      displayText,
    }: { id: string; message: string; document_ids?: string[]; displayText?: string }) => {
      sendAbortRef.current = new AbortController();
      setStreaming(true);
      liveEventsRef.current = [];
      setLiveEvents([]);
      setMessages((prev) => [
        ...prev,
        { role: "user", content: displayText ?? message },
        { role: "assistant", content: "" },
      ]);
      try {
        return await api.workspaceDrafts.sendMessageStream(
          id,
          { message, ...(document_ids && document_ids.length ? { document_ids } : {}) },
          { onToken: appendToken, onReset: resetStreamBuffer, onToolStart, onToolEnd, onTurnMeta, signal: sendAbortRef.current.signal },
        );
      } finally {
        setStreaming(false);
      }
    },
    onSuccess: (turn) => {
      flushLiveEventsToLastAssistant();
      setDraft(turn.draft);
      attachTurnQuestion(turn.draft);
    },
    onError: (err: Error, { id, message }) => {
      setMessages((prev) => prev.slice(0, -2));
      // The server did not acknowledge this turn with ``done``. Put the
      // exact text back in the composer so a transport failure cannot make
      // the user's input disappear, then reconcile any state that committed
      // before the connection ended. A turn the server saved finishes there
      // (it is run again if its server went away): show it, not an error.
      setInput((current) => current.trim() ? current : message);
      void api.workspaceDrafts.get(id).then((freshDraft) => {
        setDraft(freshDraft);
        if (_hasCompletedDraftTurn(freshDraft.messages, message) || _hasPendingDraftTurn(freshDraft, message)) {
          setMessages(freshDraft.messages.map((entry) => ({ ...entry })));
          setInput((current) => current === message ? "" : current);
          return;
        }
        toast.error(t("page.workspace_draft_chat.send_failed"), err.message);
      }).catch(() => {
        // Keep the original send error and retryable composer text visible.
        toast.error(t("page.workspace_draft_chat.send_failed"), err.message);
      });
      liveEventsRef.current = [];
      setLiveEvents([]);
    },
  });

  /* A turn this page is not streaming (its connection or its server went
     away, or another tab sent it) is answered on the server: follow the
     draft until the reply lands. */
  const answeringOnServer = Boolean(draft?.working) && !streaming
    && !createMutation.isPending && !sendMutation.isPending;
  useEffect(() => {
    if (!answeringOnServer || !draftId) return;
    const poll = startVisiblePolling(async (signal) => {
      const fresh = await api.workspaceDrafts.get(draftId, signal);
      if (signal.aborted) return;
      setDraft(fresh);
      if (!fresh.working) setMessages(fresh.messages.map((entry) => ({ ...entry })));
    }, DRAFT_WORKING_POLL_MS);
    return poll.stop;
  }, [answeringOnServer, draftId]);

  /* ── Finalize progress ── */
  const [finalizeSteps, setFinalizeSteps] = useState<FinalizeProgressEvent[]>([]);
  const [finalizeWorkspaceId, setFinalizeWorkspaceId] = useState<string | null>(null);
  const [strategistEta, setStrategistEta] = useState<number | null>(null);
  const [finalizeWarning, setFinalizeWarning] = useState<string | null>(null);
  const finalizeMutation = useMutation({
    mutationFn: async (id: string) => {
      setFinalizeSteps([]);
      setFinalizeWorkspaceId(null);
      setStrategistEta(null);
      setFinalizeWarning(null);
      return await api.workspaceDrafts.finalizeStream(id, {
        onProgress: (e) => {
          if (e.step === "dispatch_warning") {
            const message = typeof e.payload.message === "string" ? e.payload.message.trim() : "";
            setFinalizeWarning(message || "dispatch_warning");
          }
          setFinalizeSteps((prev) => [...prev, e]);
        },
        onDone: (final) => {
          setFinalizeWorkspaceId(final.workspace_id);
          const eta = final.strategist_eta_seconds;
          if (typeof eta === "number") setStrategistEta(eta);
          if (final.dispatch_warning?.trim()) setFinalizeWarning(final.dispatch_warning);
        },
      });
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      toast.success(t("page.workspace_draft_chat.workspace_created"));
      // Hold the user on the progress UI for ~strategistEta seconds so
      // they see the Strategist countdown, then navigate.
      const eta = res.strategist_eta_seconds ?? 5;
      setTimeout(() => {
        navigate(embeddedInChat ? `/chat?workspace=${encodeURIComponent(res.workspace_id)}` : `/workspaces/${res.workspace_id}?created=1`);
      }, Math.max(2000, eta * 1000));
    },
    onError: (err: Error) => {
      toast.error(t("page.workspace_draft_chat.could_not_create_workspace"), err.message);
      if (draftId) {
        void api.workspaceDrafts.get(draftId).then((updated) => {
          queryClient.setQueryData(["workspace-draft", draftId], updated);
          setDraft(updated);
        }).catch(() => {
          // Keep the original finalize failure visible; the Draft query can
          // retry this background refresh later.
        });
      }
    },
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

  function formatComposerMessage(rawText: string, manualSkills: ManualSkillItem[] = []) {
    const text = stripManualSkillTokens(rawText, manualSkills).trim();
    const skillText = manualSkills.length > 0
      ? `[${t("component.chat_input_footer.skill")}: ${manualSkills.map(manualSkillLabel).join(", ")}]`
      : "";
    return [text, skillText].filter(Boolean).join("\n\n").trim();
  }

  /** Attachments reach the architect as document ids: local files are uploaded
   *  first, knowledge items already have ids. Never silently drop a file. */
  async function resolveAttachmentDocumentIds(attachments: AttachedItem[]): Promise<string[]> {
    const ids: string[] = [];
    for (const item of attachments) {
      if (item.type === "knowledge" && item.id) {
        ids.push(item.id);
        continue;
      }
      if (item.file) {
        const uploaded = await api.documents.upload(item.file);
        ids.push(uploaded.id);
        continue;
      }
      if (item.id) ids.push(item.id);
    }
    return Array.from(new Set(ids));
  }

  async function send(
    rawText = input,
    attachments: AttachedItem[] = [],
    manualSkills: ManualSkillItem[] = [],
  ) {
    const v = formatComposerMessage(rawText, manualSkills);
    if (!v || createMutation.isPending || sendMutation.isPending || streaming || answeringOnServer) return;
    let document_ids: string[] = [];
    try {
      document_ids = await resolveAttachmentDocumentIds(attachments);
    } catch (err) {
      toast.error(t("page.workspace_draft_chat.send_failed"), (err as Error).message);
      return;
    }
    const displayText = attachments.length > 0
      ? `${v}\n[${t("component.workspace_chat.attached")}: ${attachments.map((f) => f.name).join(", ")}]`
      : v;
    if (!draftId || !draft) {
      setInput("");
      startedRef.current = true;
      createMutation.mutate({ initial_brief: v, document_ids, displayText });
      return;
    }
    if (draft.status !== "active" && draft.status !== "ready") return;
    setInput("");
    sendMutation.mutate({ id: draftId, message: v, document_ids, displayText });
  }

  /* What a blank start can suggest, from what the company already has. */
  const recommending = blankStartRef.current && !draftId;
  const { data: marketplaceBlueprints } = useQuery({
    queryKey: ["blueprints", "published"],
    queryFn: () => api.blueprints.list("published"),
    enabled: recommending,
    retry: false,
  });
  const { data: companyWorkspaces } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
    enabled: recommending,
    retry: false,
  });
  const { data: connectedIntegrations } = useQuery({
    queryKey: ["integrations"],
    queryFn: () => api.integrations.list(),
    enabled: recommending,
    retry: false,
  });
  const { data: companySettings } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: () => api.admin.getSettings(),
    enabled: recommending,
    retry: false,
  });
  const { data: personalRecommendations } = useQuery({
    queryKey: ["workspace-recommendations"],
    queryFn: () => api.workspaceDrafts.recommendations(),
    enabled: recommending,
    retry: false,
    // The first result is being prepared in the background; it replaces the local one.
    refetchInterval: (query) =>
      query.state.data?.status === "pending" && query.state.dataUpdateCount < 12 ? 5000 : false,
  });
  const localRecommendations = useMemo(() => {
    if (!Array.isArray(marketplaceBlueprints)) return [];
    return recommendWorkspaces(
      marketplaceBlueprints.map((blueprint) => ({
        ...blueprint,
        required_integrations: (blueprint.setup_preview?.required_integrations || [])
          .map((item) => item.key || "")
          .filter(Boolean),
      })),
      {
        workspaces: Array.isArray(companyWorkspaces) ? companyWorkspaces : [],
        connectedIntegrations: (Array.isArray(connectedIntegrations) ? connectedIntegrations : [])
          .map((item) => String(item?.name || item?.provider || item?.type || ""))
          .filter(Boolean),
        industry: companySettings?.industry,
      },
    );
  }, [marketplaceBlueprints, companyWorkspaces, connectedIntegrations, companySettings]);
  const personalItems = personalRecommendations?.items || [];
  const recommendations = [
    ...personalItems.map((item, index) => ({
      key: item.blueprint_id || `suggested-${index}`,
      title: item.title,
      detail: item.reason,
      brief: item.brief,
    })),
    // Fewer reasoned picks than slots: fill with what the Workspace does, claiming no reason.
    ...localRecommendations
      .filter(({ blueprint }) => !personalItems.some((item) => item.blueprint_id === blueprint.id))
      .map(({ blueprint, reason }) => ({
        key: blueprint.id,
        title: blueprint.title,
        detail: personalItems.length > 0
          ? blueprint.summary || t("ui.workspace_recommendation_ready")
          : recommendationDetail(blueprint, reason),
        brief: recommendationBrief(blueprint),
      })),
  ].slice(0, 3);
  const recommendationSources = personalItems.length > 0 ? personalRecommendations?.sources || [] : [];

  /* Derived */
  const lastIsAssistant = messages[messages.length - 1]?.role === "assistant";
  const lastContent = messages[messages.length - 1]?.content || "";
  const isStarting =
    (createMutation.isPending && !lastIsAssistant) ||
    (loadingExisting && !draft);
  const finalized = draft?.status === "finalized";
  const editable = draft?.status === "active" || draft?.status === "ready";
  const showStarterPrompts =
    (editable || (!draftId && !createMutation.isPending)) &&
    !streaming &&
    !sendMutation.isPending &&
    messages.length > 0 &&
    messages.every((m) => m.role !== "user") &&
    // One set of answers at a time: Manor's own options win over the examples.
    !messages[messages.length - 1]?.question?.options?.length;
  /* The latest message's turn ended without a reply and will not be run again. */
  const turnPaused =
    editable &&
    !draft?.working &&
    !streaming &&
    !createMutation.isPending &&
    !sendMutation.isPending &&
    messages[messages.length - 1]?.role === "user";

  // Plan-limit short-circuit -- swap the whole creation UI for a
  // friendly explanation so the user knows up-front WHY they can't
  // start a new draft, plus what to do about it.
  if (planLimit) {
    return (
      <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: 0, overflow: "hidden", gap: 16 }}>
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
                  border: "0.5px solid rgba(29, 29, 31,0.06)",
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
                  border: "0.5px solid rgba(29, 29, 31,0.06)",
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
                <Button variant="ghost" onClick={() => navigate(exitPath)}>
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
    <div className="workspace-draft-page" style={{ height: "100%", display: "flex", flexDirection: "column", padding: 0, overflow: "hidden", gap: 16 }}>
      <style>{CHAT_STYLES}</style>

      <PageHeader
        inline
        title={t("page.workspaces.create_workspace")}
        actions={(
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            if (draft && editable) {
              queryClient.invalidateQueries({ queryKey: ["workspace-drafts"] });
              toast.success(t("page.workspace_draft_chat.draft_saved"), t("page.workspace_draft_chat.resume_from_workspaces_page_anytime"));
            }
            navigate(exitPath);
          }}
        >
          {draft && editable ? t("page.workspace_draft_chat.save_and_exit") : t("action.cancel")}
        </Button>
        )}
      />

      {/* Nothing to show beside the conversation until Manor has something to propose. */}
      <div className={`draft-shell${draftHasPlan(draft) ? "" : " draft-shell--solo"}`}>
        {/* ── Chat ── */}
        <div className="draft-chat" ref={draftChatRef}>
          <div className="draft-msgs" ref={msgsRef}>
            {isStarting ? (
              <div
                style={{ display: "grid", gap: 14, padding: 12 }}
                aria-busy="true"
                aria-live="polite"
              >
                <div style={{ display: "flex", alignItems: "center", gap: 12, color: "var(--text-muted)" }}>
                  <LoadingSpinner size={18} />
                  <span style={{ fontSize: 14 }}>{t("page.workspace_draft_chat.starting_your_workspace_draft")}</span>
                </div>
                <ChatMessagesSkeleton rows={3} />
              </div>
            ) : startError && !draft ? (
              <EmptyState
                title={t("page.workspace_draft_chat.could_not_start_workspace_draft")}
                description={startError}
                action={(
                  <Button
                    variant="outline"
                    disabled={createMutation.isPending}
                    onClick={() => {
                      startedRef.current = true;
                      createMutation.mutate(createMutation.variables ?? { initial_brief: initialBriefParam || undefined });
                    }}
                  >
                    {t("component.chat_message_actions.retry")}
                  </Button>
                )}
              />
            ) : (
              <>
                {messages.map((m, i) => {
                  const isLast = i === messages.length - 1;
                  const isStreamingThis = isLast && streaming && m.role === "assistant";
                  const events = isStreamingThis ? liveEvents : (m.toolEvents || []);
                  return (
                    <div key={i} className={`draft-msg-row ${m.role}`}>
                      <MessageBubble
                        role={m.role === "user" ? "user" : "other"}
                        className="draft-msg-bubble"
                      >
                        {m.role === "assistant" && (isStreamingThis || events.length > 0) && (
                          <AssistantMessageBlocks
                            part="process"
                            keyPrefix={`draft-${i}`}
                            sourceMessageId={`draft-${i}`}
                            streaming={isStreamingThis}
                            blocks={[_draftProcess(events, {
                              key: `draft-process-${i}`,
                              live: isStreamingThis,
                              startedAtMs: isStreamingThis ? liveStartedAtMs : undefined,
                              durationMs: isStreamingThis ? undefined : m.meta?.duration_ms,
                            })]}
                          />
                        )}
                        {m.role === "assistant" && m.content === "" && isStreamingThis ? null
                        : m.role === "assistant" ? (
                          <>
                            <ChatMarkdown content={m.content} />
                            {/* Only the latest question is still open. */}
                            {isLast && !streaming && m.question?.options?.length && draftId
                              && (draft?.status === "active" || draft?.status === "ready") ? (
                              <MessageQuestionOptions
                                question={m.question}
                                disabled={sendMutation.isPending}
                                onAnswer={(answer) => sendMutation.mutate({ id: draftId, message: answer })}
                                onOther={focusComposer}
                              />
                            ) : null}
                          </>
                        ) : (
                          m.content
                        )}
                      </MessageBubble>
                    </div>
                  );
                })}
                {answeringOnServer && (
                  <div className="draft-msg-row assistant">
                    <MessageBubble role="other" className="draft-msg-bubble">
                      <span className="draft-typing"><span /><span /><span /></span>
                    </MessageBubble>
                  </div>
                )}
                {turnPaused && (
                  <div className="draft-msg-row assistant">
                    <p className="draft-turn-paused">{t("page.workspace_draft_chat.turn_paused")}</p>
                  </div>
                )}
                {showStarterPrompts && recommendations.length > 0 && (
                  <div className="draft-recommendations" aria-label={t("ui.workspace_recommendations")}>
                    {recommendations.map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        className="draft-recommendation"
                        onClick={() => void send(item.brief)}
                      >
                        <span className="draft-recommendation-title">{item.title}</span>
                        <span className="draft-recommendation-reason">{item.detail}</span>
                      </button>
                    ))}
                    {recommendationSources.length > 0 && (
                      <p className="draft-recommendation-basis">
                        {recommendationSources.includes("public_web")
                          ? t("ui.workspace_recommendation_basis_public")
                          : t("ui.workspace_recommendation_basis_records")}
                        {" · "}
                        <Link to="/settings?tab=memories">{t("ui.manage")}</Link>
                      </p>
                    )}
                  </div>
                )}
                {showStarterPrompts && recommendations.length === 0 && (
                  <div className="draft-starters" aria-label={t("page.workspace_draft_chat.sample_prompts")}>
                    {STARTER_PROMPTS.map((starter) => (
                      <button
                        key={starter}
                        type="button"
                        className="draft-starter-btn"
                        onClick={() => void send(starter)}
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
            attachments="documents"
            value={input}
            onChange={setInput}
            enterToSend
            streaming={sendMutation.isPending || streaming || answeringOnServer}
            disabled={Boolean(draftId) && (!draft || !editable)}
            showStopButton={sendMutation.isPending}
            onSend={(text, attachments, skills) => {
              void send(text, attachments, skills);
            }}
            onStop={() => sendAbortRef.current.abort()}
            placeholder={
              finalized
                ? t("page.workspace_draft_chat.workspace_already_created")
                : draft?.status === "abandoned"
                  ? t("page.workspace_draft_chat.draft_saved")
                  : t("page.workspace_draft_chat.describe_your_workspace")
            }
          />
        </div>

        {/* ── Sidebar — live preview ── */}
        <div className="draft-side">
          {draft && draftId && draftHasPlan(draft) && (
            <WorkspaceDraftConfigurationPanel
              draftId={draftId}
              draft={draft}
              creating={finalizeMutation.isPending}
              updating={sendMutation.isPending || streaming}
              onCreate={() => finalizeMutation.mutateAsync(draftId).then(() => undefined)}
              onDraftChange={setDraft}
            />
          )}
        </div>
      </div>

      {/* Finalize progress overlay */}
      {(finalizeMutation.isPending || finalizeWorkspaceId) && (
        <FinalizeProgressOverlay
          steps={finalizeSteps}
          finalized={!!finalizeWorkspaceId}
          autonomous={Boolean(draft?.fields.heartbeat_enabled)}
          strategistEta={strategistEta}
          warning={finalizeWarning}
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
  autonomous,
  strategistEta,
  warning,
}: {
  steps: FinalizeProgressEvent[];
  finalized: boolean;
  autonomous: boolean;
  strategistEta: number | null;
  warning: string | null;
}) {
  const seenKeys = new Set(steps
    .filter((step) => step.step !== "agent_bindings_repair_done" || step.payload.ready === true)
    .map((step) => step.step));
  const provisioningSteps = autonomous
    ? FINALIZE_STEPS
    : FINALIZE_STEPS.filter((step) => !["runtime_scheduled", "strategist_dispatched"].includes(step.key));
  const visibleSteps = seenKeys.has("agent_bindings_repair_started")
    ? [{
      key: "agent_bindings_repair_done",
      label: seenKeys.has("agent_bindings_repair_done")
        ? t("page.workspace_draft_chat.agent_bindings_repaired")
        : t("page.workspace_draft_chat.repairing_agent_bindings"),
    }, ...provisioningSteps]
    : provisioningSteps;
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

  const completed = visibleSteps.filter((step) => seenKeys.has(step.key)).length;
  const total = visibleSteps.length;
  const pct = Math.min(100, Math.round((completed / total) * 100));

  return createPortal(
    <div className="manor-dialog-overlay" style={{
      position: "fixed", inset: 0,
      background: "var(--modal-overlay-bg)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 20000,
    }}>
      <div role="dialog" aria-modal="true" className="manor-dialog workspace-finalize-dialog" style={{
        width: "min(92vw, 480px)",
        background: "var(--modal-bg)",
        borderRadius: "var(--modal-radius)",
        padding: "32px 32px 28px",
        boxShadow: "var(--modal-shadow)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 18 }}>
          {finalized ? (
            <div style={{
              width: 36, height: 36, borderRadius: "50%",
              background: "var(--surface-muted)", display: "flex", alignItems: "center",
              justifyContent: "center", color: warning ? "#936027" : "var(--text-strong)", fontSize: 18, fontWeight: 800,
            }}>{warning ? "!" : "✓"}</div>
          ) : (
            <LoadingSpinner size={20} />
          )}
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 17, fontWeight: 700, color: "var(--text-strong)" }}>
              {finalized
                ? warning
                  ? t("page.workspace_draft_chat.workspace_created_with_setup_warning")
                  : t("page.workspace_draft_chat.workspace_ready")
                : t("page.workspace_draft_chat.creating_your_workspace")}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {finalized
                ? warning
                  ? t("page.workspace_draft_chat.workspace_startup_needs_attention")
                  : secondsLeft != null && secondsLeft > 0
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
          {visibleSteps.map((s, idx) => {
            const done = seenKeys.has(s.key);
            // The currently-active step is the first not-done after the last done.
            const prevDone = idx === 0 || seenKeys.has(visibleSteps[idx - 1].key);
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
                  <div style={{ fontSize: 13, color: done ? "#1d1d1f" : active ? "#436b65" : "#8e8e93" }}>
                    {s.key === "complete" && warning
                      ? t("page.workspace_draft_chat.workspace_created_startup_incomplete")
                      : s.label}
                  </div>
                  {subtitle && (
                    <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 1 }}>{subtitle}</div>
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
