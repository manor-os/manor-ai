import { useState, useMemo, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import { t } from "../lib/i18n";
import { isInternalFilePermissionMessage, parseToolCalls } from "../lib/chatStream";
import { useAuthStore } from "../stores/auth";
import { useWorkspaceFilter } from "../stores/workspace";
import SmartToolbar from "../components/ui/SmartToolbar";
import Avatar from "../components/ui/Avatar";
import AgentAvatar from "../components/ui/AgentAvatar";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import TabSwitcher from "../components/ui/TabSwitcher";
import PageHeader from "../components/ui/PageHeader";
import { SkeletonLine, SkeletonCircle } from "../components/ui/Skeleton";
import ChatMarkdown from "../components/ChatMarkdown";
import AssistantMessageBlocks from "../components/AssistantMessageBlocks";
import CreditLimitNotice from "../components/ui/CreditLimitNotice";
import ToolCallList from "../components/ui/ToolCallList";
import {
  IconClose, IconChat,
  IconGlobe, IconEmail, IconTelegram, IconWhatsApp, IconWeChat, IconSMS, IconChatBubble, IconEdit,
} from "../components/icons";
import ManorAvatar from "../components/ui/ManorAvatar";
import { MANOR_AGENT_NAME } from "../lib/constants";

/* ── Channel config ─────────────────────────────────────────────── */

type IconFn = (props: { size?: number; style?: React.CSSProperties }) => JSX.Element;

const CHANNEL_META: Record<string, { labelKey: string; Icon: IconFn; badgeColor: string }> = {
  web:      { labelKey: "page.chat_history.channel_web",      Icon: IconGlobe,      badgeColor: "#5f928a" },
  webchat:  { labelKey: "page.chat_history.channel_webchat",  Icon: IconChatBubble, badgeColor: "#4f7d75" },
  email:    { labelKey: "page.chat_history.channel_email",    Icon: IconEmail,       badgeColor: "#5f84bd" },
  wechat:   { labelKey: "page.chat_history.channel_wechat",   Icon: IconWeChat,      badgeColor: "#54a176" },
  whatsapp: { labelKey: "page.chat_history.channel_whatsapp", Icon: IconWhatsApp,    badgeColor: "#54a176" },
  telegram: { labelKey: "page.chat_history.channel_telegram", Icon: IconTelegram,    badgeColor: "#5f84bd" },
  sms:      { labelKey: "page.chat_history.channel_sms",      Icon: IconSMS,         badgeColor: "#9079c2" },
  inapp:    { labelKey: "page.chat_history.channel_inapp",    Icon: IconChatBubble,  badgeColor: "#9079c2" },
};

function normalizedChannel(channel: string) {
  return channel?.toLowerCase().replace(/[_\s-]/g, "") || "web";
}

function getChannelMeta(channel: string) {
  const normalized = normalizedChannel(channel);
  if (normalized === "webchat") return CHANNEL_META.webchat;
  if (normalized === "gmail" || normalized.includes("email")) return CHANNEL_META.email;
  if (normalized === "twilio" || normalized === "twiliosms" || normalized === "twiliovoice" || normalized.includes("sms")) return CHANNEL_META.sms;
  if (normalized.startsWith("wechat")) return CHANNEL_META.wechat;
  if (normalized.includes("whatsapp")) return CHANNEL_META.whatsapp;
  if (normalized.includes("telegram")) return CHANNEL_META.telegram;
  if (normalized === "webclient" || normalized === "webclientsource") return CHANNEL_META.web;
  if (normalized === "webapp" || normalized === "portal" || normalized === "inapp") return CHANNEL_META.inapp;
  return CHANNEL_META[normalized] || CHANNEL_META.web;
}

function isExternalConversationChannel(channel: string) {
  const normalized = normalizedChannel(channel);
  return [
    "discord",
    "email",
    "facebook",
    "gmail",
    "inapp",
    "slack",
    "sms",
    "telegram",
    "twiliosms",
    "twiliovoice",
    "webchat",
    "wechat",
    "wechatpersonal",
    "whatsapp",
  ].includes(normalized);
}

function customerNameFromConversation(conversation: any) {
  const title = String(conversation?.title || "");
  const sep = title.indexOf(":");
  const fromTitle = sep >= 0 ? title.slice(sep + 1).trim() : "";
  return fromTitle || t("page.chat_history.customer");
}

/* ── Message detail panel (right side) ──────────────────────────── */

interface DetailMessage {
  role: string;
  content: string;
  tool_calls?: any[];
  assistant_blocks?: any[] | null;
  stop_reason?: string | null;
  limit_detail?: any;
  timestamp?: string;
}

function MessagePanel({
  conversation,
  agentInfo,
  onClose,
  onRename,
}: {
  conversation: any;
  agentInfo?: { id?: string; name: string; avatar_url?: string } | null;
  onClose: () => void;
  onRename: (id: string, title: string) => void;
}) {
  const currentUser = useAuthStore((s) => s.user);
  const [messages, setMessages] = useState<DetailMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(conversation.title || "");
  const bodyRef = useRef<HTMLDivElement>(null);
  const meta = getChannelMeta(conversation.channel);
  const isExternalConversation = isExternalConversationChannel(conversation.channel);

  const userName = isExternalConversation
    ? customerNameFromConversation(conversation)
    : currentUser?.display_name || currentUser?.first_name || currentUser?.email || t("page.chat_history.you");
  const userAvatar = isExternalConversation ? undefined : currentUser?.avatar_url;
  const assistantName = agentInfo?.name || MANOR_AGENT_NAME;
  const isManorDefault = !agentInfo;

  useEffect(() => {
    setEditingTitle(false);
    setTitleDraft(conversation.title || "");
    setLoading(true);
    api.chat.getMessages(conversation.id).then((msgs) => {
      setMessages(
        msgs
          .filter((m) => !(m.role === "user" && isInternalFilePermissionMessage(m.content)))
          .map((m) => ({
            role: m.role,
            content: m.content || "",
            tool_calls: parseToolCalls(m.tool_calls),
            assistant_blocks: Array.isArray((m as any).assistant_blocks) ? (m as any).assistant_blocks : undefined,
            stop_reason: m.stop_reason,
            limit_detail: m.limit_detail,
            timestamp: m.created_at,
          })),
      );
      setLoading(false);
      setTimeout(() => bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight }), 100);
    }).catch(() => { setMessages([]); setLoading(false); });
  }, [conversation.id, conversation.title]);

  const commitTitle = () => {
    const nextTitle = titleDraft.trim();
    setEditingTitle(false);
    if (!nextTitle || nextTitle === (conversation.title || "")) return;
    onRename(conversation.id, nextTitle);
  };

  const formatTime = (ts?: string) => {
    if (!ts) return "";
    try { return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); } catch { return ""; }
  };

  return (
    <div className="chat-history-panel flex flex-col h-full">
      {/* Header — like old repo: agent avatar + conversation title + agent name tag */}
      <div className="chat-header chat-history-detail-header">
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <div className="relative shrink-0">
            {isManorDefault ? (
              <ManorAvatar size={40} />
            ) : (
              <AgentAvatar name={assistantName} avatarUrl={agentInfo?.avatar_url} seed={agentInfo?.id} size={40} />
            )}
            {/* Channel badge overlay — like old repo */}
            <div
              className="chat-history-channel-badge absolute -bottom-0.5 -right-0.5 rounded-full flex items-center justify-center"
              style={{
                width: 18, height: 18,
                background: meta.badgeColor,
              }}
            >
              <meta.Icon size={9} style={{ color: "#fff" }} />
            </div>
          </div>
          <div className="min-w-0 flex-1">
            {editingTitle ? (
              <input
                autoFocus
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={commitTitle}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitTitle();
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setTitleDraft(conversation.title || "");
                    setEditingTitle(false);
                  }
                }}
                className="chat-history-title-input text-[15px] font-semibold"
                style={{
                  width: "100%",
                }}
              />
            ) : (
              <div className="flex items-center gap-1.5 min-w-0">
                <h2 className="chat-history-detail-title text-[15px] font-semibold truncate">
                  {conversation.title || t("page.chat_history.untitled")}
                </h2>
                <button
                  onClick={() => setEditingTitle(true)}
                  className="chat-history-icon-button p-1 rounded-lg transition-all shrink-0"
                  title={t("component.session_switcher.rename_chat")}
                >
                  <IconEdit size={13} />
                </button>
              </div>
            )}
            <div className="flex items-center gap-2 mt-0.5">
              <span className="chat-history-agent-badge text-[11px] font-medium px-1.5 py-0.5 rounded">
                {assistantName}
              </span>
              <span className="chat-history-detail-meta text-[11px]">
                {conversation.message_count || 0} {t("page.chat_history.messages")}
              </span>
              <span className="chat-history-detail-meta text-[11px]">
                {relativeTime(conversation.updated_at || conversation.created_at)}
              </span>
            </div>
          </div>
        </div>
        <button onClick={onClose} className="chat-history-close-button p-2 rounded-xl transition-all">
          <IconClose size={18} />
        </button>
      </div>

      {/* Messages — old repo layout: user LEFT, agent RIGHT */}
      <div ref={bodyRef} className="chat-history-message-body flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center h-full">
            <LoadingSpinner size={24} />
          </div>
        ) : messages.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <p className="chat-history-empty-copy text-sm">{t("page.chat_history.no_messages_in_conversation")}</p>
          </div>
        ) : (
          <div className="flex flex-col gap-5">
            {messages.map((msg, i) => {
              const isUser = msg.role === "user";
              const hasAssistantBlocks =
                !isUser && Array.isArray(msg.assistant_blocks) && msg.assistant_blocks.length > 0;
              const showCreditLimitNotice =
                !isUser && msg.stop_reason === "credit_exhausted";
              return (
                <div
                  key={i}
                  className={`flex gap-3 ${isUser ? "" : "flex-row-reverse"}`}
                >
                  {/* Avatar */}
                  <div className="shrink-0 mt-1">
                    {isUser ? (
                      <Avatar name={userName} src={userAvatar} size={36} />
                    ) : isManorDefault ? (
                      <ManorAvatar size={36} />
                    ) : (
                      <AgentAvatar name={assistantName} avatarUrl={agentInfo?.avatar_url} seed={agentInfo?.id} size={36} />
                    )}
                  </div>

                  {/* Message content */}
                  <div
                    className={`flex flex-col min-w-0 ${
                      isUser ? "max-w-[75%]" : "w-full flex-1 max-w-none"
                    }`}
                  >
                    <span className={`chat-history-message-meta text-[11px] mb-1 ${isUser ? "" : "text-right"}`}>
                      {isUser ? userName : assistantName}, {formatTime(msg.timestamp)}
                    </span>

                    {/* Tool calls */}
                    {!hasAssistantBlocks && msg.tool_calls && msg.tool_calls.length > 0 && (
                      <ToolCallList tools={msg.tool_calls} keyPrefix={`history-${i}`} />
                    )}

                    {showCreditLimitNotice && (
                      <CreditLimitNotice detail={msg.limit_detail} compact />
                    )}

                    {hasAssistantBlocks && !showCreditLimitNotice && (
                      <div className="chat-bubble chat-bubble--bot w-full">
                        <AssistantMessageBlocks
                          blocks={msg.assistant_blocks as any}
                          content={msg.content}
                          keyPrefix={`history-${i}`}
                        />
                      </div>
                    )}

                    {!hasAssistantBlocks && msg.content && !showCreditLimitNotice && (
                      <div
                        className={`chat-bubble ${
                          isUser ? "chat-bubble--user" : "chat-bubble--bot w-full"
                        }`}
                      >
                        <ChatMarkdown content={msg.content} isUser={isUser} />
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

    </div>
  );
}

/* ── Skeleton ───────────────────────────────────────────────────── */

function ConversationSkeleton() {
  return (
    <div className="flex flex-col gap-1">
      {[1, 2, 3, 4, 5, 6].map((i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <SkeletonCircle size={36} />
          <div className="flex-1 flex flex-col gap-1.5">
            <SkeletonLine width="60%" height={13} />
            <SkeletonLine width="40%" height={11} />
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Main component — split layout ──────────────────────────────── */

export default function ChatHistory() {
  const queryClient = useQueryClient();

  const [search, setSearch] = useState("");
  const [channelFilter, setChannelFilter] = useState("all");
  const [selectedConvId, setSelectedConvId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [editingConvId, setEditingConvId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");

  const wsId = useWorkspaceFilter((s) => s.activeWorkspaceId);
  const wsFilter = wsId !== "all" ? wsId : undefined;

  const { data: conversations = [], isLoading } = useQuery({
    queryKey: ["conversations", wsFilter],
    queryFn: () => api.chat.listConversations(wsFilter),
  });

  /* Fetch agents to resolve avatars & names in the list */
  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
  });
  const agentMap = useMemo(() => {
    const map: Record<string, { id: string; name: string; avatar_url?: string }> = {};
    agents.forEach((a) => { map[a.id] = { id: a.id, name: a.name, avatar_url: a.avatar_url }; });
    return map;
  }, [agents]);

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.chat.deleteConversation(id),
    onSuccess: (_data, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      setDeleteTarget(null);
      if (selectedConvId === deletedId) setSelectedConvId(null);
    },
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.chat.renameConversation(id, title),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });

  const startRename = (id: string, title?: string) => {
    setEditingConvId(id);
    setTitleDraft(title || t("page.chat_history.untitled"));
  };

  const cancelRename = () => {
    setEditingConvId(null);
    setTitleDraft("");
  };

  const finishRename = () => {
    if (!editingConvId) return;
    const nextTitle = titleDraft.trim();
    const conv = conversations.find((c) => c.id === editingConvId);
    setEditingConvId(null);
    if (!nextTitle || nextTitle === (conv?.title || t("page.chat_history.untitled"))) return;
    renameMutation.mutate({ id: editingConvId, title: nextTitle });
  };

  const renameConversation = (id: string, title: string) => {
    const nextTitle = title.trim();
    if (!nextTitle) return;
    renameMutation.mutate({ id, title: nextTitle });
  };

  /* Unique channels for tab switcher */
  const channelTabs = useMemo(() => {
    const counts: Record<string, number> = {};
    conversations.forEach((c) => {
      const ch = c.channel || "web";
      counts[ch] = (counts[ch] || 0) + 1;
    });
    const tabs: { key: string; label: string; icon?: React.ReactNode; count?: number }[] = [
      { key: "all", label: t("page.chat_history.all"), count: conversations.length },
    ];
    Object.entries(counts).forEach(([ch, count]) => {
      const meta = getChannelMeta(ch);
      tabs.push({ key: ch, label: t(meta.labelKey), icon: <meta.Icon size={13} />, count });
    });
    return tabs;
  }, [conversations]);

  const filtered = useMemo(() => {
    let list = [...conversations];
    if (channelFilter !== "all") list = list.filter((c) => c.channel === channelFilter);
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(
        (c) =>
          c.title?.toLowerCase().includes(q) ||
          c.summary?.toLowerCase().includes(q) ||
          c.channel?.toLowerCase().includes(q),
      );
    }
    list.sort((a, b) => {
      const da = new Date(a.updated_at || a.created_at || 0).getTime();
      const db = new Date(b.updated_at || b.created_at || 0).getTime();
      return db - da;
    });
    return list;
  }, [conversations, channelFilter, search]);

  const selectedConv = conversations.find((c) => c.id === selectedConvId);

  /* ================================================================ */
  /*  Render — full-height split layout                                */
  /* ================================================================ */

  return (
    <div className="chat-history-page flex flex-col h-full -m-6">
      <div className="px-6 pt-6">
        <PageHeader
          title={t("nav.chatHistory")}
          compactControls
          tabs={<TabSwitcher tabs={channelTabs} value={channelFilter} onChange={setChannelFilter} />}
          toolbar={(
            <SmartToolbar
              searchValue={search}
              onSearchChange={setSearch}
              searchPlaceholder={t("page.chat_history.search_placeholder")}
              className="w-full sm:w-64"
            />
          )}
        />
      </div>

      <div className="flex flex-1 min-h-0">
      {/* ============================================================ */}
      {/* LEFT — Conversation list                                      */}
      {/* ============================================================ */}
      <div className="chat-history-list-panel chat-sidebar-left flex flex-col overflow-hidden" style={{ width: selectedConv ? 360 : "100%", maxWidth: selectedConv ? 360 : undefined, transition: "width 0.2s ease" }}>

        {/* List */}
        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {isLoading ? (
            <ConversationSkeleton />
          ) : filtered.length === 0 ? (
            <div className="py-12">
              <EmptyState
                icon={<IconChatBubble size={40} />}
                title={t("page.chat_history.no_conversations")}
                description={t("page.chat_history.no_conversations_desc")}
              />
            </div>
          ) : (
            <div className="flex flex-col">
              {filtered.map((conv) => {
                const meta = getChannelMeta(conv.channel);
                const isSelected = selectedConvId === conv.id;
                const convAgent = conv.agent_id ? agentMap[conv.agent_id] : null;
                const isEditing = editingConvId === conv.id;

                return (
                  <div
                    key={conv.id}
                    onClick={() => { if (!isEditing) setSelectedConvId(conv.id); }}
                    className={`chat-history-row group flex items-center gap-3 px-4 py-3 cursor-pointer transition-colors${isSelected ? " chat-history-row--selected" : ""}`}
                  >
                    <div className="relative shrink-0">
                      {convAgent ? (
                        <AgentAvatar name={convAgent.name} avatarUrl={convAgent.avatar_url} seed={convAgent.id} size={40} />
                      ) : (
                        <ManorAvatar size={40} />
                      )}
                      {/* Channel badge overlay */}
                      <div
                        className="chat-history-channel-badge absolute -bottom-0.5 -right-0.5 rounded-full flex items-center justify-center"
                        style={{
                          width: 16, height: 16,
                          background: meta.badgeColor,
                        }}
                      >
                        <meta.Icon size={8} style={{ color: "#fff" }} />
                      </div>
                    </div>

                    <div className="flex-1 min-w-0">
                      {/* Title + time */}
                      <div className="flex items-center gap-2">
                        {isEditing ? (
                          <input
                            autoFocus
                            value={titleDraft}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) => setTitleDraft(e.target.value)}
                            onBlur={finishRename}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.preventDefault();
                                finishRename();
                              }
                              if (e.key === "Escape") {
                                e.preventDefault();
                                cancelRename();
                              }
                            }}
                            className="chat-history-title-input text-[13px] font-semibold flex-1"
                            style={{
                              minWidth: 0,
                            }}
                          />
                        ) : (
                          <p className={`chat-history-row-title text-[13px] font-semibold truncate flex-1${isSelected ? " chat-history-row-title--selected" : ""}`}>
                            {conv.title || t("page.chat_history.untitled")}
                          </p>
                        )}
                        <span className="chat-history-row-time text-[10px] shrink-0">
                          {relativeTime(conv.updated_at || conv.created_at)}
                        </span>
                      </div>

                      {/* Channel + agent name subtitle — like old repo */}
                      <p className="chat-history-row-subtitle text-[11px] truncate mt-0.5">
                        <span className="chat-history-channel-label" style={{ color: meta.badgeColor }}>{t(meta.labelKey)}</span>
                        <span className="mx-1">·</span>
                        {convAgent?.name || t("page.chat_history.manor_ai")}
                      </p>
                    </div>

                    {/* Rename */}
                    {!isEditing && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          startRename(conv.id, conv.title);
                        }}
                        className="chat-history-row-action opacity-0 group-hover:opacity-100 p-1.5 rounded-lg transition-all shrink-0"
                        title={t("component.session_switcher.rename_chat")}
                      >
                        <IconEdit size={12} />
                      </button>
                    )}

                    {/* Delete */}
                    <button
                      onClick={(e) => { e.stopPropagation(); setDeleteTarget(conv.id); }}
                      className="chat-history-row-action chat-history-row-action--danger opacity-0 group-hover:opacity-100 p-1.5 rounded-lg transition-all shrink-0"
                    >
                      <IconClose size={12} />
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* ============================================================ */}
      {/* RIGHT — Message detail panel                                  */}
      {/* ============================================================ */}
      <div className="chat-history-detail-panel flex-1 flex flex-col overflow-hidden">
        {selectedConv ? (
          <MessagePanel
            key={selectedConv.id}
            conversation={selectedConv}
            agentInfo={selectedConv.agent_id ? agentMap[selectedConv.agent_id] : null}
            onClose={() => setSelectedConvId(null)}
            onRename={renameConversation}
          />
        ) : (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <div className="chat-history-empty-icon w-16 h-16 rounded-2xl flex items-center justify-center mx-auto mb-4">
                <IconChat size={28} />
              </div>
              <p className="chat-history-empty-title text-[15px] font-semibold">{t("page.chat_history.select_conversation")}</p>
              <p className="chat-history-empty-copy text-sm mt-1">{t("page.chat_history.select_conversation_desc")}</p>
            </div>
          </div>
        )}
      </div>

      {/* Delete confirmation */}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}
        title={t("page.chat_history.delete_title")}
        message={t("page.chat_history.delete_message")}
        confirmLabel={deleteMutation.isPending ? t("page.chat_history.deleting") : t("action.delete")}
        danger
      />
      </div>
    </div>
  );
}
