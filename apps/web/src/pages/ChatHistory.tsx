import { agentListQueryOptions, useCatalogScope } from "../lib/catalogQueries";
import { useState, useMemo, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { getPreferredTimeZone, relativeTime } from "../lib/format";
import { t } from "../lib/i18n";
import {
  isInternalFilePermissionMessage,
  parseToolCalls,
  type ChatMessage,
} from "../lib/chatStream";
import {
  collectResponseSurfaceSubmissionFailureMessageIds,
  collectResponseSurfaceSubmissionReceipts,
  isResponseSurfaceSubmissionMessage,
} from "../lib/responseSurface";
import { useAuthStore } from "../stores/auth";
import { useWorkspaceFilter } from "../stores/workspace";
import SmartToolbar from "../components/ui/SmartToolbar";
import Avatar from "../components/ui/Avatar";
import AgentAvatar from "../components/ui/AgentAvatar";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import EmptyState from "../components/ui/EmptyState";
import TabSwitcher from "../components/ui/TabSwitcher";
import { useSearchParams } from "react-router-dom";
import Button from "../components/ui/Button";
import { ConfigGroup, ConfigPage, ConfigRow } from "../components/ui/GroupedList";
import { ChatMessagesSkeleton, PageLoading } from "../components/ui/Skeleton";
import ChatMarkdown from "../components/ChatMarkdown";
import CollapsibleSentMessage from "../components/chat/CollapsibleSentMessage";
import { isRetryableAssistantMessage } from "../components/chat/ChatMessageActions";
import AssistantMessageBlocks from "../components/AssistantMessageBlocks";
import CreditLimitNotice from "../components/ui/CreditLimitNotice";
import ToolCallList from "../components/ui/ToolCallList";
import {
  IconGlobe, IconEmail, IconTelegram, IconWhatsApp, IconWeChat, IconSMS, IconChatBubble, IconChevronLeft,
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

function conversationTitle(conversation: any) {
  const title = conversation.title || "";
  if (isExternalConversationChannel(conversation.channel)) {
    const prefix = `${conversation.channel}:`;
    if (title.toLowerCase().startsWith(prefix.toLowerCase())) return customerNameFromConversation(conversation);
  }
  return title || t("page.chat_history.untitled");
}

/* ── Message detail panel ──────────────────────────── */

interface DetailMessage {
  id: string;
  role: string;
  content: string;
  tool_calls?: any[];
  assistant_blocks?: any[] | null;
  hitl_requests?: Array<Record<string, any>> | null;
  stop_reason?: string | null;
  limit_detail?: any;
  timestamp?: string;
  meta?: Record<string, unknown> | null;
}

function MessagePanel({
  conversation,
  agentInfo,
  onClose,
  onRename,
  onDelete,
}: {
  conversation: any;
  agentInfo?: { id?: string; name: string; avatar_url?: string } | null;
  onClose: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: () => void;
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
            id: m.id,
            role: m.role,
            content: m.content || "",
            tool_calls: parseToolCalls(m.tool_calls),
            assistant_blocks: Array.isArray((m as any).assistant_blocks) ? (m as any).assistant_blocks : undefined,
            hitl_requests: m.hitl_requests,
            stop_reason: m.stop_reason,
            limit_detail: m.limit_detail,
            timestamp: m.created_at,
            meta: m.meta,
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
    try { return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: getPreferredTimeZone() }); } catch { return ""; }
  };
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
  const visibleMessages = useMemo(
    () => messages.filter((message) => (
      !isResponseSurfaceSubmissionMessage(message, responseSurfaceSubmissionReceipts)
      && !responseSurfaceSubmissionFailureMessageIds.has(message.id)
    )),
    [
      messages,
      responseSurfaceSubmissionFailureMessageIds,
      responseSurfaceSubmissionReceipts,
    ],
  );

  return (
    <div className="chat-history-panel flex flex-col h-full">
      <div className="chat-history-detail-head">
        <button type="button" className="chat-history-back" onClick={onClose}>
          <IconChevronLeft size={14} />
          {t("nav.chatHistory")}
        </button>
        <div className="chat-history-detail-row">
          {editingTitle ? (
            <input
              autoFocus
              aria-label={t("component.session_switcher.rename_chat")}
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
              className="config-input chat-history-title-field"
            />
          ) : (
            <h2 className="config-page-title chat-history-detail-title">{conversationTitle(conversation)}</h2>
          )}
          <span className="chat-history-detail-actions">
            <Button variant="ghost" size="sm" onClick={() => setEditingTitle(true)}>{t("component.session_switcher.rename_chat")}</Button>
            <Button variant="ghost" size="sm" onClick={onDelete}>{t("action.delete")}</Button>
          </span>
        </div>
        <p className="chat-history-detail-meta">
          {[assistantName, t(meta.labelKey), relativeTime(conversation.updated_at || conversation.created_at)].join(" · ")}
        </p>
      </div>

      {/* Messages — old repo layout: user LEFT, agent RIGHT */}
      <div ref={bodyRef} className="chat-history-message-body flex-1 overflow-y-auto">
        {loading ? (
          <div className="h-full p-5" aria-busy="true" aria-live="polite">
            <ChatMessagesSkeleton rows={5} />
          </div>
        ) : visibleMessages.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <p className="chat-history-empty-copy text-sm">{t("page.chat_history.no_messages_in_conversation")}</p>
          </div>
        ) : (
          <div className="flex flex-col gap-5">
            {visibleMessages.map((msg, i) => {
              const isUser = msg.role === "user";
              const retryableAssistant = !isUser && isRetryableAssistantMessage({
                role: "assistant",
                content: msg.content,
                stop_reason: msg.stop_reason || undefined,
                stream_error: msg.meta?.stream_status === "error",
              } satisfies ChatMessage, msg.content);
              const hasAssistantBlocks =
                !isUser &&
                !retryableAssistant &&
                Array.isArray(msg.assistant_blocks) &&
                msg.assistant_blocks.length > 0;
              const showCreditLimitNotice =
                !isUser && msg.stop_reason === "credit_exhausted";
              return (
                <div
                  key={msg.id}
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
                          sourceMessageId={msg.id}
                          hitlRequests={msg.hitl_requests}
                          responseSurfaceSubmissionReceipts={responseSurfaceSubmissionReceipts}
                          collapseLongFinal
                        />
                      </div>
                    )}

                    {!hasAssistantBlocks && msg.content && !showCreditLimitNotice && (
                      <div
                        className={`chat-bubble ${
                          isUser ? "chat-bubble--user" : "chat-bubble--bot w-full"
                        }`}
                      >
                        {isUser ? (
                          <CollapsibleSentMessage text={msg.content}>
                            <ChatMarkdown content={msg.content} isUser />
                          </CollapsibleSentMessage>
                        ) : (
                          <CollapsibleSentMessage text={msg.content} tone="assistant">
                            <ChatMarkdown content={msg.content} isUser={false} showFilesInPlace />
                          </CollapsibleSentMessage>
                        )}
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

export default function ChatHistory() {
  const catalogScope = useCatalogScope();
  const queryClient = useQueryClient();

  const [search, setSearch] = useState("");
  const [searchParams] = useSearchParams();
  const linkedChannel = searchParams.get("channel")?.toLowerCase();
  const [channelFilter, setChannelFilter] = useState(linkedChannel || "all");
  const [selectedConvId, setSelectedConvId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const wsId = useWorkspaceFilter((s) => s.activeWorkspaceId);
  // A Workspace's entry point links here with its own Workspace.
  const wsFilter = searchParams.get("workspace") || (wsId !== "all" ? wsId : undefined);
  const requestChannel = channelFilter !== "all" && channelFilter === linkedChannel ? linkedChannel : undefined;

  const { data: conversations = [], isLoading } = useQuery({
    queryKey: requestChannel ? ["conversations", wsFilter, requestChannel] : ["conversations", wsFilter],
    queryFn: () => api.chat.listConversations(wsFilter, requestChannel),
  });

  /* Fetch agents to resolve avatars & names in the list */
  const { data: agents = [] } = useQuery({
    ...agentListQueryOptions(catalogScope),
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

  const renameConversation = (id: string, title: string) => {
    const nextTitle = title.trim();
    if (!nextTitle) return;
    renameMutation.mutate({ id, title: nextTitle });
  };

  /* Unique channels for tab switcher */
  const channelTabs = useMemo(() => {
    if (requestChannel) {
      const meta = getChannelMeta(requestChannel);
      return [
        { key: "all", label: t("page.chat_history.all") },
        { key: requestChannel, label: t(meta.labelKey), icon: <meta.Icon size={13} /> },
      ];
    }
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
  }, [conversations, requestChannel]);

  const filtered = useMemo(() => {
    let list = [...conversations];
    if (!requestChannel && channelFilter !== "all") list = list.filter((c) => c.channel === channelFilter);
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
  }, [conversations, channelFilter, requestChannel, search]);

  const selectedConv = conversations.find((c) => c.id === selectedConvId);

  const renderRows = (list: any[]) => list.map((conv) => {
    const meta = getChannelMeta(conv.channel);
    const convAgent = conv.agent_id ? agentMap[conv.agent_id] : null;
    const external = isExternalConversationChannel(conv.channel);
    return (
      <ConfigRow
        key={conv.id}
        leading={convAgent
          ? <AgentAvatar name={convAgent.name} avatarUrl={convAgent.avatar_url} seed={convAgent.id} size={28} />
          : <ManorAvatar size={28} />}
        label={conversationTitle(conv)}
        note={[convAgent?.name || t("page.chat_history.manor_ai"), external ? t(meta.labelKey) : ""].filter(Boolean).join(" · ")}
        meta={relativeTime(conv.updated_at || conv.created_at)}
        chevron
        onClick={() => setSelectedConvId(conv.id)}
      />
    );
  });
  const customerConversations = filtered.filter((c) => isExternalConversationChannel(c.channel));
  const ownConversations = filtered.filter((c) => !isExternalConversationChannel(c.channel));
  // Customers and your own chats are already apart; channels matter once there are several.
  const showChannelTabs = channelFilter !== "all"
    || new Set(conversations.filter((c) => isExternalConversationChannel(c.channel)).map((c) => c.channel)).size > 1;

  return (
    <>
      {selectedConv ? (
        <MessagePanel
          key={selectedConv.id}
          conversation={selectedConv}
          agentInfo={selectedConv.agent_id ? agentMap[selectedConv.agent_id] : null}
          onClose={() => setSelectedConvId(null)}
          onRename={renameConversation}
          onDelete={() => setDeleteTarget(selectedConv.id)}
        />
      ) : (
        <ConfigPage title={t("nav.chatHistory")}>
          {(showChannelTabs || conversations.length > 8) && (
            <div className="chat-history-filters">
              {showChannelTabs && <TabSwitcher tabs={channelTabs} value={channelFilter} onChange={setChannelFilter} />}
              {conversations.length > 8 && (
                <SmartToolbar
                  searchValue={search}
                  onSearchChange={setSearch}
                  searchPlaceholder={t("page.chat_history.search_placeholder")}
                  className="w-full sm:w-64"
                />
              )}
            </div>
          )}
          {isLoading ? (
            <PageLoading />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<IconChatBubble size={40} />}
              title={t("page.chat_history.no_conversations")}
              description={t("page.chat_history.no_conversations_desc")}
            />
          ) : (
            <>
              {customerConversations.length > 0 && (
                <ConfigGroup title={t("page.chat_history.group_customers")}>{renderRows(customerConversations)}</ConfigGroup>
              )}
              {ownConversations.length > 0 && (
                <ConfigGroup title={t("page.chat_history.group_yours")}>{renderRows(ownConversations)}</ConfigGroup>
              )}
            </>
          )}
        </ConfigPage>
      )}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}
        title={t("page.chat_history.delete_title")}
        message={t("page.chat_history.delete_message")}
        confirmLabel={deleteMutation.isPending ? t("page.chat_history.deleting") : t("action.delete")}
        danger
      />
    </>
  );
}
