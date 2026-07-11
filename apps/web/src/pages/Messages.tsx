import { useState, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { UserSummary } from "../lib/types";
import PageHeader from "../components/ui/PageHeader";
import SearchInput from "../components/ui/SearchInput";
import Modal from "../components/ui/Modal";
import EmptyState from "../components/ui/EmptyState";
import InlineTips from "../components/ui/InlineTips";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import { IconEdit } from "../components/icons";
import { t } from "../lib/i18n";

interface MessageThread {
  id: string;
  participant_id: string;
  participant_name: string;
  last_message?: string;
  last_message_at?: string;
  unread: boolean;
}

interface ThreadMessage {
  id: string;
  sender_id: string;
  sender_name: string;
  content: string;
  created_at: string;
  is_own: boolean;
}

export default function Messages() {
  const queryClient = useQueryClient();
  const [selectedThread, setSelectedThread] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [messageInput, setMessageInput] = useState("");
  const [showCompose, setShowCompose] = useState(false);
  const [composeRecipient, setComposeRecipient] = useState("");
  const [composeMessage, setComposeMessage] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const { data: threads } = useQuery({
    queryKey: ["message-threads"],
    queryFn: () => api.messages.listThreads(),
  });

  const { data: threadMessages } = useQuery({
    queryKey: ["message-thread", selectedThread],
    queryFn: () => api.messages.getThread(selectedThread!),
    enabled: !!selectedThread,
  });

  const { data: users } = useQuery({
    queryKey: ["users", "directory"],
    queryFn: () => api.users.directory(),
  });

  const sendMutation = useMutation({
    mutationFn: (data: { recipient_id: string; content: string; thread_id?: string }) =>
      api.messages.send(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["message-threads"] });
      if (selectedThread) {
        queryClient.invalidateQueries({ queryKey: ["message-thread", selectedThread] });
      }
      setMessageInput("");
    },
  });

  const composeMutation = useMutation({
    mutationFn: (data: { recipient_id: string; content: string }) =>
      api.messages.send(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["message-threads"] });
      setShowCompose(false);
      setComposeRecipient("");
      setComposeMessage("");
    },
  });

  // Mark thread as read when selected
  useEffect(() => {
    if (selectedThread) {
      api.messages.markRead(selectedThread).then(() => {
        queryClient.invalidateQueries({ queryKey: ["message-threads"] });
      }).catch(() => {});
    }
  }, [selectedThread, queryClient]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [threadMessages]);

  const allThreads: MessageThread[] = threads || [];
  const filteredThreads = allThreads.filter((t) => {
    if (!search) return true;
    return t.participant_name.toLowerCase().includes(search.toLowerCase());
  });

  const allMessages: ThreadMessage[] = threadMessages || [];
  const currentThread = allThreads.find((t) => t.id === selectedThread);

  function handleSendReply() {
    if (!messageInput.trim() || !currentThread) return;
    sendMutation.mutate({
      recipient_id: currentThread.participant_id,
      content: messageInput.trim(),
      thread_id: selectedThread || undefined,
    });
  }

  function handleCompose() {
    if (!composeRecipient || !composeMessage.trim()) return;
    composeMutation.mutate({
      recipient_id: composeRecipient,
      content: composeMessage.trim(),
    });
  }

  function closeCompose() {
    setShowCompose(false);
    setComposeRecipient("");
    setComposeMessage("");
  }

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "clamp(12px, 3vw, 24px) clamp(12px, 3vw, 24px) 0" }}>
        <PageHeader
          title={t("page.messages.title")}
          subtitle={`${allThreads.length} ${t("page.messages.conversations_count")}`}
          actions={
            <button onClick={() => setShowCompose(true)} className="btn-manor" style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <IconEdit size={16} />
              {t("page.messages.compose")}
            </button>
          }
        />
      </div>

      {/* Compose Modal - 40px radius */}
      <Modal
        open={showCompose}
        onClose={closeCompose}
        title={t("page.messages.new_message")}
        footer={
          <>
            <Button variant="outline" onClick={closeCompose}>{t("action.cancel")}</Button>
            <Button
              variant="primary"
              disabled={!composeRecipient || !composeMessage.trim() || composeMutation.isPending}
              onClick={handleCompose}
            >
              {composeMutation.isPending ? t("page.messages.sending") : t("page.messages.send")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#44403c", marginBottom: 4 }}>{t("page.messages.recipient")}</label>
            <select
              value={composeRecipient}
              onChange={(e) => setComposeRecipient(e.target.value)}
              className="manor-input"
            >
              <option value="">{t("page.messages.select_recipient")}</option>
              {(users || []).map((u: UserSummary) => (
                <option key={u.id} value={u.id}>
                  {u.display_name || u.email} ({u.email})
                </option>
              ))}
            </select>
          </div>
          <Textarea
            label={t("page.messages.message")}
            value={composeMessage}
            onChange={(e) => setComposeMessage(e.target.value)}
            rows={4}
            placeholder={t("page.messages.type_message")}
          />
          {composeMutation.isError && (
            <p style={{ fontSize: 13, color: "#c14a44", margin: 0 }}>{(composeMutation.error as Error).message}</p>
          )}
        </div>
      </Modal>

      {/* Split Layout */}
      <div style={{ flex: 1, display: "flex", flexWrap: "wrap", gap: 16, padding: "16px clamp(12px, 3vw, 24px) clamp(12px, 3vw, 24px)", minHeight: 0 }}>
        {/* Conversation Sidebar */}
        <div
          className="glass-panel"
          style={{ width: "min(100%, 320px)", flex: "1 1 280px", display: "flex", flexDirection: "column", overflow: "hidden" }}
        >
          <div style={{ padding: 12, borderBottom: "1px solid rgba(28,25,23,0.06)" }}>
            <SearchInput value={search} onChange={setSearch} placeholder={t("page.messages.search_conversations")} />
          </div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {filteredThreads.length > 0 ? (
              filteredThreads.map((thread) => (
                <button
                  key={thread.id}
                  onClick={() => setSelectedThread(thread.id)}
                  style={{
                    width: "100%",
                    textAlign: "left",
                    padding: "12px 16px",
                    borderBottom: "1px solid rgba(28,25,23,0.06)",
                    transition: "background 0.2s",
                    background: selectedThread === thread.id ? "rgba(79,125,117,0.08)" : "transparent",
                    border: "none",
                    cursor: "pointer",
                    borderBottomWidth: 1,
                    borderBottomStyle: "solid",
                    borderBottomColor: "rgba(231,229,228,0.3)",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div
                      style={{
                        width: 32,
                        height: 32,
                        borderRadius: "50%",
                        background: "#e3ebe8",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        color: "#436b65",
                        fontSize: 13,
                        fontWeight: 800,
                        flexShrink: 0,
                      }}
                    >
                      {thread.participant_name.charAt(0).toUpperCase()}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                        <span
                          style={{
                            fontSize: 13,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                            fontWeight: thread.unread ? 700 : 600,
                            color: thread.unread ? "#292524" : "#44403c",
                          }}
                        >
                          {thread.participant_name}
                        </span>
                        {thread.last_message_at && (
                          <span style={{ fontSize: 11, color: "#a8a29e", flexShrink: 0, marginLeft: 8 }}>
                            {new Date(thread.last_message_at).toLocaleDateString()}
                          </span>
                        )}
                      </div>
                      {thread.last_message && (
                        <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{thread.last_message}</p>
                      )}
                    </div>
                    {thread.unread && (
                      <span
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: "50%",
                          background: "#4f7d75",
                          flexShrink: 0,
                          boxShadow: "0 0 0 3px rgba(79,125,117,0.15)",
                        }}
                      />
                    )}
                  </div>
                </button>
              ))
            ) : (
              <div style={{ padding: 24, textAlign: "center", fontSize: 13, color: "#a8a29e" }}>
                {search ? t("page.messages.no_conversations_found") : t("page.messages.no_messages_yet")}
              </div>
            )}
          </div>
        </div>

        {/* Message Thread */}
        <div
          className="glass-panel"
          style={{ flex: "999 1 320px", minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}
        >
          {selectedThread && currentThread ? (
            <>
              {/* Thread Header */}
              <div style={{ padding: "12px 20px", borderBottom: "1px solid rgba(28,25,23,0.06)", display: "flex", alignItems: "center", gap: 12 }}>
                <div
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    background: "#e3ebe8",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "#436b65",
                    fontSize: 13,
                    fontWeight: 800,
                    flexShrink: 0,
                  }}
                >
                  {currentThread.participant_name.charAt(0).toUpperCase()}
                </div>
                <span style={{ fontSize: 14, fontWeight: 700, color: "#292524" }}>{currentThread.participant_name}</span>
              </div>

              {/* Messages */}
              <div style={{ flex: 1, overflowY: "auto", padding: 20, display: "flex", flexDirection: "column", gap: 16 }}>
                {allMessages.length > 0 ? (
                  allMessages.map((msg) => (
                    <div
                      key={msg.id}
                      style={{
                        display: "flex",
                        justifyContent: msg.is_own ? "flex-end" : "flex-start",
                      }}
                    >
                      <div
                        style={{
                          maxWidth: "70%",
                          borderRadius: 20,
                          padding: "10px 16px",
                          fontSize: 13,
                          background: msg.is_own
                            ? "#436b65"
                            : "rgba(255,255,255,0.8)",
                          color: msg.is_own ? "#fff" : "#44403c",
                          border: msg.is_own
                            ? "none"
                            : "1px solid rgba(231,229,228,0.5)",
                        }}
                      >
                        <p style={{ margin: 0 }}>{msg.content}</p>
                        <p
                          style={{
                            fontSize: 11,
                            marginTop: 4,
                            color: msg.is_own ? "rgba(255,255,255,0.6)" : "#a8a29e",
                          }}
                        >
                          {new Date(msg.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                        </p>
                      </div>
                    </div>
                  ))
                ) : (
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", fontSize: 13, color: "#a8a29e" }}>
                    {t("page.messages.no_messages_thread")}
                  </div>
                )}
                <div ref={messagesEndRef} />
              </div>

              {/* Reply Input */}
              <div style={{ padding: 16, borderTop: "1px solid rgba(28,25,23,0.06)" }}>
                <div style={{ display: "flex", gap: 12 }}>
                  <input
                    value={messageInput}
                    onChange={(e) => setMessageInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        handleSendReply();
                      }
                    }}
                    className="manor-input"
                    style={{ flex: 1 }}
                    placeholder={t("page.messages.type_message")}
                  />
                  <button
                    onClick={handleSendReply}
                    disabled={!messageInput.trim() || sendMutation.isPending}
                    className="btn-manor"
                    style={{ flexShrink: 0, opacity: !messageInput.trim() ? 0.5 : 1 }}
                  >
                    {sendMutation.isPending ? "..." : t("page.messages.send")}
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <EmptyState
                icon={
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 01-.825-.242m9.345-8.334a2.126 2.126 0 00-.476-.095 48.64 48.64 0 00-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0011.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
                  </svg>
                }
                title={t("page.messages.select_conversation")}
                description={t("page.messages.choose_or_compose")}
                action={<InlineTips surface="inbox" placement="empty_state" />}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
