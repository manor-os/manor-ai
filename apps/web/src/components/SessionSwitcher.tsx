import { useState, useRef, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import { t } from "../lib/i18n";


interface SessionSwitcherProps {
  currentConvId?: string;
  disabled?: boolean;
  onNewChat: () => void;
  onSwitchSession: (convId: string) => void;
}

export default function SessionSwitcher({ currentConvId, disabled, onNewChat, onSwitchSession }: SessionSwitcherProps) {
  const [open, setOpen] = useState(false);
  const [hoveredConvId, setHoveredConvId] = useState<string | null>(null);
  const [editingConvId, setEditingConvId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    if (open) document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const { data: conversations } = useQuery({
    queryKey: ["conversations", "session-switcher"],
    queryFn: () => api.chat.listConversations(),
    enabled: open,
  });

  const deleteConversation = useMutation({
    mutationFn: (id: string) => api.chat.deleteConversation(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      queryClient.invalidateQueries({ queryKey: ["conversations", "session-switcher"] });
      if (id === currentConvId) {
        onNewChat();
      }
    },
  });

  const renameConversation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.chat.renameConversation(id, title),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      queryClient.invalidateQueries({ queryKey: ["conversations", "session-switcher"] });
    },
  });

  const startRename = (convId: string, currentTitle?: string) => {
    setEditingConvId(convId);
    setTitleDraft(currentTitle || "Untitled");
  };

  const finishRename = () => {
    if (!editingConvId) return;
    const nextTitle = titleDraft.trim();
    const conv = conversations?.find((c) => c.id === editingConvId);
    setEditingConvId(null);
    if (!nextTitle || nextTitle === (conv?.title || "Untitled")) return;
    renameConversation.mutate({ id: editingConvId, title: nextTitle });
  };

  const cancelRename = () => {
    setEditingConvId(null);
    setTitleDraft("");
  };

  const handleDeleteConversation = (convId: string, title?: string) => {
    const name = title || "this chat";
    if (!window.confirm(`Delete "${name}"? This cannot be undone.`)) return;
    deleteConversation.mutate(convId);
  };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen(!open)}
        title={t("component.session_switcher.new_chat")}
        disabled={disabled}
        style={{
          width: 32,
          height: 32,
          borderRadius: 8,
          border: "none",
          background: open ? "var(--surface-muted)" : "transparent",
          cursor: disabled ? "default" : "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: open ? "var(--text-default)" : "var(--text-faint)",
          transition: "all 0.15s",
          opacity: disabled ? 0.4 : 1,
        }}
        onMouseEnter={(e) => { if (!disabled) { e.currentTarget.style.background = "var(--surface-muted)"; e.currentTarget.style.color = "var(--text-default)"; } }}
        onMouseLeave={(e) => { if (!open) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text-faint)"; } }}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 5v14" /><path d="M5 12h14" />
        </svg>
      </button>

      {open && (
        <div style={{
          position: "absolute",
          top: "calc(100% + 6px)",
          right: 0,
          width: "min(calc(100vw - 24px), 280px)",
          background: "var(--surface-panel)",
          borderRadius: 14,
          border: "1px solid var(--border-default)",
          boxShadow: "var(--shadow-lg)",
          zIndex: 100,
          overflow: "hidden",
          animation: "dialog-in 0.15s ease-out",
        }}>
          {/* New chat */}
          <button
            onClick={() => { setOpen(false); onNewChat(); }}
            style={{
              width: "100%",
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "12px 16px",
              border: "none",
              background: "transparent",
              cursor: "pointer",
              fontSize: 13,
              fontWeight: 600,
              color: "var(--accent)",
              fontFamily: "inherit",
              transition: "background 0.1s",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--accent-soft)"; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 5v14" /><path d="M5 12h14" />
            </svg>
            {t("component.session_switcher.new_chat_2")}</button>

          {/* Recent sessions */}
          {conversations && conversations.length > 0 && (
            <>
              <div style={{ height: 1, background: "var(--border-subtle)" }} />
              <div style={{ padding: "8px 16px 4px", fontSize: 10, fontWeight: 700, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                {t("page.knowledge.recent")}</div>
              <div style={{ maxHeight: 240, overflowY: "auto", paddingBottom: 6 }}>
                {conversations.slice(0, 10).map((conv) => {
                  const isActive = conv.id === currentConvId;
                  const isEditing = editingConvId === conv.id;
                  return (
                    <div
                      key={conv.id}
                      role="button"
                      tabIndex={0}
                      onClick={() => {
                        if (isEditing) return;
                        setOpen(false);
                        onSwitchSession(conv.id);
                      }}
                      onKeyDown={(e) => {
                        if (isEditing) return;
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setOpen(false);
                          onSwitchSession(conv.id);
                        }
                      }}
                      onFocus={() => setHoveredConvId(conv.id)}
                      onBlur={() => setHoveredConvId(null)}
                      style={{
                        width: "100%",
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                        padding: "8px 16px",
                        border: "none",
                        background: isActive ? "var(--accent-soft)" : "transparent",
                        cursor: "pointer",
                        fontSize: 13,
                        fontWeight: isActive ? 600 : 400,
                        color: isActive ? "var(--accent)" : "var(--text-default)",
                        fontFamily: "inherit",
                        transition: "background 0.1s",
                        textAlign: "left",
                      }}
                      onMouseEnter={(e) => {
                        setHoveredConvId(conv.id);
                        e.currentTarget.style.background = isActive ? "var(--accent-soft)" : "var(--surface-muted)";
                      }}
                      onMouseLeave={(e) => {
                        setHoveredConvId(null);
                        e.currentTarget.style.background = isActive ? "var(--accent-soft)" : "transparent";
                      }}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, opacity: 0.5 }}>
                        <path d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
                      </svg>
                      <div style={{ flex: 1, minWidth: 0 }}>
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
                            style={{
                              width: "100%",
                              border: "1px solid var(--border-default)",
                              borderRadius: 7,
                              padding: "3px 6px",
                              fontSize: 12,
                              fontWeight: 600,
                              color: "var(--text-strong)",
                              background: "var(--surface-muted)",
                              outline: "none",
                            }}
                          />
                        ) : (
                          <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", lineHeight: 1.3 }}>
                            {conv.title || "Untitled"}
                          </div>
                        )}
                        <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 1 }}>
                          {relativeTime(conv.updated_at || conv.created_at)}
                        </div>
                      </div>
                      {(hoveredConvId === conv.id || isActive || isEditing) && !isEditing && (
                        <span
                          role="button"
                          tabIndex={0}
                          title={t("component.session_switcher.rename_chat")}
                          aria-label={t("component.session_switcher.rename_chat_label").replace("{title}", conv.title || t("page.chat_history.untitled"))}
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            startRename(conv.id, conv.title);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              e.stopPropagation();
                              startRename(conv.id, conv.title);
                            }
                          }}
                          style={{
                            width: 24,
                            height: 24,
                            borderRadius: 8,
                            color: "var(--text-faint)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            flexShrink: 0,
                            cursor: renameConversation.isPending ? "wait" : "pointer",
                            opacity: renameConversation.isPending ? 0.5 : 1,
                            transition: "all 0.12s ease",
                          }}
                          onMouseEnter={(e) => {
                            e.currentTarget.style.background = "var(--accent-soft)";
                            e.currentTarget.style.color = "var(--accent)";
                          }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.background = "transparent";
                            e.currentTarget.style.color = "var(--text-faint)";
                          }}
                        >
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round">
                            <path d="M12 20h9" />
                            <path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z" />
                          </svg>
                        </span>
                      )}
                      {(hoveredConvId === conv.id || isActive) && !isEditing && (
                        <span
                          role="button"
                          tabIndex={0}
                          title={t("component.session_switcher.delete_chat")}
                          aria-label={`Delete ${conv.title || "chat"}`}
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            handleDeleteConversation(conv.id, conv.title);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              e.stopPropagation();
                              handleDeleteConversation(conv.id, conv.title);
                            }
                          }}
                          style={{
                            width: 24,
                            height: 24,
                            borderRadius: 8,
                            color: "var(--text-faint)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            flexShrink: 0,
                            cursor: deleteConversation.isPending ? "wait" : "pointer",
                            opacity: deleteConversation.isPending ? 0.5 : 1,
                            transition: "all 0.12s ease",
                          }}
                          onMouseEnter={(e) => {
                            e.currentTarget.style.background = "rgba(241,221,219,0.8)";
                            e.currentTarget.style.color = "#c14a44";
                          }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.background = "transparent";
                            e.currentTarget.style.color = "var(--text-faint)";
                          }}
                        >
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round">
                            <path d="M3 6h18" />
                            <path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2" />
                            <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
                            <path d="M10 11v6M14 11v6" />
                          </svg>
                        </span>
                      )}
                      {isActive && hoveredConvId !== conv.id && (
                        <div style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--accent)", flexShrink: 0, marginLeft: 9 }} />
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
