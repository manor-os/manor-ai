/**
 * SupportPanel — a bottom-right floating panel that lists the user's
 * support tickets, opens an existing thread, or creates a new one.
 *
 * Polls /unread-count for the sidebar badge; on open switches to the
 * thread view for the most recent active ticket, or to a blank "new
 * ticket" form when the user has none.
 *
 * Backed by /api/v1/support — auth via existing JWT.
 *
 * Visual system: shares the bottom-right floating chrome with the Manor
 * AI chat via <FloatingPanel> (rounded, translucent blur, slide-up), and
 * the shared <Button>, <Chip>, <EmptyState> primitives and .manor-input
 * / .manor-label form classes (#436b65 primary, Plus Jakarta Sans).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAuthToken } from "../lib/authToken";
import FloatingPanel from "./FloatingPanel";
import PanelHeader from "./chat/PanelHeader";
import PanelComposer from "./chat/PanelComposer";
import MessageRow from "./chat/MessageRow";
import MessageBubble from "./chat/MessageBubble";
import ManorAvatar from "./ui/ManorAvatar";
import Button from "./ui/Button";
import Input from "./ui/Input";
import Textarea from "./ui/Textarea";
import Chip from "./ui/Chip";
import EmptyState from "./ui/EmptyState";
import { SkeletonLine } from "./ui/Skeleton";


interface TicketSummary {
  id: string;
  subject: string;
  status: "open" | "awaiting_user" | "resolved" | "closed";
  priority: string;
  created_at: string;
  last_message_at: string | null;
  last_admin_message_at: string | null;
  unread_user_count: number;
}

interface TicketMessage {
  id: string;
  sender_kind: "user" | "admin" | "system";
  sender_display_name: string | null;
  body: string;
  created_at: string;
}

interface TicketDetail extends TicketSummary {
  messages: TicketMessage[];
}

type StatusKey = TicketSummary["status"];

const STATUS_LABEL: Record<StatusKey, string> = {
  open: "Open",
  awaiting_user: "Awaiting you",
  resolved: "Resolved",
  closed: "Closed",
};

const STATUS_CHIP: Record<StatusKey, React.ComponentProps<typeof Chip>["variant"]> = {
  open: "orange",
  awaiting_user: "blue",
  resolved: "green",
  closed: "slate",
};

const ACTIVE_STATUSES: StatusKey[] = ["open", "awaiting_user"];

type ListFilter = "all" | "active" | "resolved";


async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAuthToken();
  if (!token) throw new Error("Not signed in.");
  const res = await fetch(`/api/v1/support${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(detail || `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return await res.json();
}

async function fetchSupportUnreadCount(): Promise<{ count: number }> {
  const tickets = await apiFetch<TicketSummary[]>("/tickets");
  return {
    count: tickets.reduce(
      (total, ticket) => total + Math.max(0, ticket.unread_user_count || 0),
      0,
    ),
  };
}


/** "just now" / "5m ago" / "3h ago" / "2d ago", else a short date. */
function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const sec = Math.round((Date.now() - t) / 1000);
  if (sec < 45) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 7) return `${day}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** A "Today" / "Yesterday" / "March 4" label for thread date dividers. */
function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const today = new Date();
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOf(today) - startOf(d)) / 86_400_000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, {
    month: "short", day: "numeric",
    year: d.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}


export default function SupportPanel({
  open, onClose,
}: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [view, setView] = useState<"list" | "thread" | "new">("list");
  const [activeId, setActiveId] = useState<string | null>(null);

  const { data: tickets, isLoading: ticketsLoading, refetch: refetchTickets } = useQuery({
    queryKey: ["support-my-tickets"],
    queryFn: () => apiFetch<TicketSummary[]>("/tickets"),
    enabled: open,
    refetchInterval: open ? 30_000 : false,
  });

  // First time the user opens the panel, jump them straight to the
  // newest active thread if they have one. Otherwise default to the
  // new-ticket form so they don't stare at an empty list.
  const didAutoSelect = useRef(false);
  useEffect(() => {
    if (!open || !tickets || didAutoSelect.current) return;
    didAutoSelect.current = true;
    const active = tickets.find((t) => ACTIVE_STATUSES.includes(t.status));
    if (active) {
      setActiveId(active.id);
      setView("thread");
    } else if (tickets.length === 0) {
      setView("new");
    }
  }, [open, tickets]);

  // Reset to a clean list view when closed, so a hidden panel does no
  // background thread polling and reopening starts fresh (auto-selects).
  useEffect(() => {
    if (!open) {
      didAutoSelect.current = false;
      setView("list");
      setActiveId(null);
    }
  }, [open]);

  // Escape closes the floating panel.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <FloatingPanel open={open} zIndex={1002} ariaLabel="Support">
        <PanelHeader
          avatar={<ManorAvatar size={34} />}
          leading={
            view !== "list" ? (
              <button
                onClick={() => { setView("list"); setActiveId(null); }}
                style={iconBtn}
                aria-label="Back to conversations"
                title="Back"
              >‹</button>
            ) : undefined
          }
          title={
            view === "new" ? "New support request"
              : view === "thread" ? "Support conversation"
              : "Support"
          }
          subtitle={
            view === "list" ? (
              <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 1 }}>
                Chat with the Manor team
              </div>
            ) : undefined
          }
          actions={
            <button onClick={onClose} style={iconBtn} aria-label="Close" title="Close">
              <svg
                width="14" height="14" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth={2.5}
                strokeLinecap="round" strokeLinejoin="round"
              >
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          }
        />

        {view === "list" && (
          <TicketList
            tickets={tickets || []}
            loading={ticketsLoading}
            onOpen={(id) => { setActiveId(id); setView("thread"); }}
            onNew={() => setView("new")}
          />
        )}

        {view === "thread" && activeId && (
          <ThreadView
            ticketId={activeId}
            onSent={() => {
              qc.invalidateQueries({ queryKey: ["support-my-tickets"] });
              qc.invalidateQueries({ queryKey: ["support-unread"] });
            }}
            onClosed={() => {
              refetchTickets();
              setView("list");
              setActiveId(null);
            }}
          />
        )}

        {view === "new" && (
          <NewTicketForm
            onCreated={(t) => {
              qc.invalidateQueries({ queryKey: ["support-my-tickets"] });
              setActiveId(t.id);
              setView("thread");
            }}
            onCancel={() => setView("list")}
          />
        )}
    </FloatingPanel>
  );
}


function TicketList({
  tickets, loading, onOpen, onNew,
}: {
  tickets: TicketSummary[];
  loading: boolean;
  onOpen: (id: string) => void;
  onNew: () => void;
}) {
  const [filter, setFilter] = useState<ListFilter>("all");

  const counts = useMemo(() => ({
    all: tickets.length,
    active: tickets.filter((t) => ACTIVE_STATUSES.includes(t.status)).length,
    resolved: tickets.filter((t) => !ACTIVE_STATUSES.includes(t.status)).length,
  }), [tickets]);

  const visible = useMemo(() => {
    if (filter === "active") return tickets.filter((t) => ACTIVE_STATUSES.includes(t.status));
    if (filter === "resolved") return tickets.filter((t) => !ACTIVE_STATUSES.includes(t.status));
    return tickets;
  }, [tickets, filter]);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{
        padding: "12px 18px 10px",
        display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12,
      }}>
        {tickets.length > 0 ? (
          <div style={{ display: "flex", gap: 4 }}>
            {(["all", "active", "resolved"] as ListFilter[]).map((key) => (
              <button
                key={key}
                onClick={() => setFilter(key)}
                style={{
                  border: "none", cursor: "pointer", fontFamily: "inherit",
                  padding: "5px 10px", borderRadius: 8, fontSize: 12, fontWeight: 600,
                  background: filter === key ? "#f2f6f5" : "transparent",
                  color: filter === key ? "#436b65" : "#a8a29e",
                  transition: "background 0.15s, color 0.15s",
                }}
              >
                {key === "all" ? "All" : key === "active" ? "Active" : "Resolved"}
                <span style={{ opacity: 0.7, marginLeft: 5 }}>{counts[key]}</span>
              </button>
            ))}
          </div>
        ) : <span style={{ fontSize: 12, color: "#a8a29e" }}>Start a conversation</span>}
        <Button variant="primary" size="sm" onClick={onNew}>
          + New
        </Button>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "2px 18px 18px" }}>
        {loading && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {[0, 1, 2].map((i) => <TicketRowSkeleton key={i} />)}
          </div>
        )}

        {!loading && tickets.length === 0 && (
          <div style={{ marginTop: 8 }}>
            <EmptyState
              icon={<span>💬</span>}
              title="No conversations yet"
              description="Open your first request to ask the Manor team anything."
              action={<Button variant="primary" size="sm" onClick={onNew}>+ New request</Button>}
            />
          </div>
        )}

        {!loading && tickets.length > 0 && visible.length === 0 && (
          <div style={{ color: "#a8a29e", fontSize: 13, padding: "24px 0", textAlign: "center" }}>
            No {filter} conversations.
          </div>
        )}

        {!loading && visible.map((t) => (
          <button
            key={t.id}
            onClick={() => onOpen(t.id)}
            className="item-card"
            style={{
              width: "100%", textAlign: "left",
              padding: 14, marginBottom: 8,
              cursor: "pointer", display: "flex", flexDirection: "column",
              gap: 7, fontFamily: "inherit", border: "none",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Chip variant={STATUS_CHIP[t.status]} size="sm">
                {STATUS_LABEL[t.status]}
              </Chip>
              <span
                style={{ fontSize: 11, color: "#a8a29e", marginLeft: "auto" }}
                title={new Date(t.last_message_at || t.created_at).toLocaleString()}
              >
                {relativeTime(t.last_message_at || t.created_at)}
              </span>
              {t.unread_user_count > 0 && (
                <span style={{
                  minWidth: 18, height: 18, padding: "0 5px", borderRadius: 9,
                  background: "#436b65", color: "#fff",
                  fontSize: 10, fontWeight: 700,
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                }}>{t.unread_user_count}</span>
              )}
            </div>
            <div style={{
              fontSize: 13, fontWeight: 600, color: "#292524",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>{t.subject}</div>
          </button>
        ))}
      </div>
    </div>
  );
}


function TicketRowSkeleton() {
  return (
    <div className="item-card" style={{
      padding: 14, display: "flex", flexDirection: "column", gap: 9, border: "none",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <SkeletonLine width={64} height={16} />
        <SkeletonLine width={40} height={12} />
      </div>
      <SkeletonLine width="75%" height={14} />
    </div>
  );
}


function ThreadView({
  ticketId, onSent, onClosed,
}: {
  ticketId: string;
  onSent: () => void;
  onClosed: () => void;
}) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["support-ticket", ticketId],
    queryFn: () => apiFetch<TicketDetail>(`/tickets/${ticketId}`),
    refetchInterval: 20_000,
  });
  const [reply, setReply] = useState("");

  const send = useMutation({
    mutationFn: async () => {
      const body = reply.trim();
      if (!body) return null;
      await apiFetch(`/tickets/${ticketId}/messages`, {
        method: "POST", body: JSON.stringify({ body }),
      });
      return body;
    },
    onSuccess: () => {
      setReply("");
      qc.invalidateQueries({ queryKey: ["support-ticket", ticketId] });
      onSent();
    },
  });

  const close = useMutation({
    mutationFn: () => apiFetch(`/tickets/${ticketId}/close`, { method: "POST" }),
    onSuccess: () => { onClosed(); },
  });

  const scrollerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scrollerRef.current) {
      scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
    }
  }, [data?.messages.length]);

  if (isLoading || !data) {
    return <ThreadSkeleton />;
  }

  const canClose = data.status !== "closed";

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{
        padding: "12px 18px", borderBottom: "1px solid #f5f5f4",
        display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8,
      }}>
        <div style={{ minWidth: 0 }}>
          <div style={{
            fontSize: 13, fontWeight: 600, color: "#292524",
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>
            {data.subject}
          </div>
          <div style={{ marginTop: 4 }}>
            <Chip variant={STATUS_CHIP[data.status]} size="sm">
              {STATUS_LABEL[data.status]}
            </Chip>
          </div>
        </div>
        {canClose && (
          <Button
            variant="outline" size="sm"
            loading={close.isPending}
            onClick={() => {
              if (window.confirm("Close this conversation? You can always start a new one.")) {
                close.mutate();
              }
            }}
          >
            Close
          </Button>
        )}
      </div>

      <div ref={scrollerRef} style={{
        flex: 1, overflow: "auto", padding: 18,
        display: "flex", flexDirection: "column", gap: 4,
      }}>
        {data.messages.map((m, i) => {
          const prev = data.messages[i - 1];
          const showDivider = !prev || dayLabel(prev.created_at) !== dayLabel(m.created_at);
          return (
            <div key={m.id} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {showDivider && <DateDivider label={dayLabel(m.created_at)} />}
              {m.sender_kind === "system" ? (
                <SystemNote body={m.body} createdAt={m.created_at} />
              ) : (
                <Bubble
                  mine={m.sender_kind === "user"}
                  label={
                    m.sender_kind === "admin"
                      ? (m.sender_display_name || "Manor team")
                      : (m.sender_display_name || "You")
                  }
                  body={m.body}
                  createdAt={m.created_at}
                />
              )}
            </div>
          );
        })}
      </div>

      {data.status !== "closed" && (
        <PanelComposer
          value={reply}
          onChange={setReply}
          onSend={() => send.mutate()}
          sending={send.isPending}
          placeholder="Type your reply…  (Enter to send, Shift+Enter for a new line)"
        />
      )}

      {data.status === "closed" && (
        <div style={{
          padding: "14px 18px", borderTop: "1px solid #f5f5f4",
          fontSize: 12, color: "#78716c", textAlign: "center",
        }}>
          This conversation is closed. Open a new request to follow up.
        </div>
      )}
    </div>
  );
}


function ThreadSkeleton() {
  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{ padding: "12px 18px", borderBottom: "1px solid #f5f5f4" }}>
        <SkeletonLine width="55%" height={14} />
        <div style={{ marginTop: 8 }}><SkeletonLine width={64} height={16} /></div>
      </div>
      <div style={{ flex: 1, padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ alignSelf: "flex-start", width: "70%" }}>
          <SkeletonLine height={48} />
        </div>
        <div style={{ alignSelf: "flex-end", width: "60%" }}>
          <SkeletonLine height={40} />
        </div>
        <div style={{ alignSelf: "flex-start", width: "65%" }}>
          <SkeletonLine height={56} />
        </div>
      </div>
    </div>
  );
}


function NewTicketForm({
  onCreated, onCancel,
}: {
  onCreated: (t: TicketDetail) => void;
  onCancel: () => void;
}) {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const valid = subject.trim().length > 0 && body.trim().length > 0;

  const create = useMutation({
    mutationFn: () => apiFetch<TicketDetail>("/tickets", {
      method: "POST",
      body: JSON.stringify({ subject: subject.trim(), body: body.trim() }),
    }),
    onSuccess: (t) => onCreated(t),
  });

  return (
    <div style={{
      flex: 1, display: "flex", flexDirection: "column",
      padding: 18, gap: 14, overflow: "auto",
    }}>
      <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5 }}>
        Tell us what's going on — we'll reply by email and right here in
        Manor AI. Include any error messages or workspace links.
      </div>

      <Input
        label="Subject"
        value={subject}
        onChange={(e) => setSubject(e.target.value.slice(0, 200))}
        placeholder="Quick summary…"
        autoFocus
      />

      <Textarea
        label="Details"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        rows={8}
        placeholder="What happened, what you expected, links if helpful…"
        error={
          create.error
            ? ((create.error as Error).message || "Failed to open the ticket.")
            : undefined
        }
      />

      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="outline" size="sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          variant="primary" size="sm"
          loading={create.isPending}
          disabled={!valid}
          onClick={() => create.mutate()}
        >
          Send to support
        </Button>
      </div>
    </div>
  );
}


function DateDivider({ label }: { label: string }) {
  if (!label) return null;
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 10,
      margin: "10px 0 6px", color: "#d6d3d1",
    }}>
      <div style={{ flex: 1, height: 1, background: "#f5f5f4" }} />
      <span style={{ fontSize: 10, fontWeight: 700, color: "#a8a29e", letterSpacing: "0.04em" }}>
        {label}
      </span>
      <div style={{ flex: 1, height: 1, background: "#f5f5f4" }} />
    </div>
  );
}


function SystemNote({ body, createdAt }: { body: string; createdAt: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "center", padding: "2px 0" }}>
      <span
        title={new Date(createdAt).toLocaleString()}
        style={{
          fontSize: 11, color: "#78716c", background: "#fafaf9",
          border: "1px solid #f5f5f4", borderRadius: 999,
          padding: "4px 12px", textAlign: "center", maxWidth: "90%",
        }}
      >
        {body}
      </span>
    </div>
  );
}


function Bubble({
  mine, label, body, createdAt,
}: { mine: boolean; label: string; body: string; createdAt: string }) {
  return (
    <MessageRow
      role={mine ? "user" : "other"}
      avatar={!mine ? <ManorAvatar size={26} /> : undefined}
    >
      <div style={{ fontSize: 10, color: "#a8a29e", marginBottom: 1, padding: "0 2px" }}>
        {label}
        {createdAt && (
          <span title={new Date(createdAt).toLocaleString()}>
            {" · "}{relativeTime(createdAt)}
          </span>
        )}
      </div>
      <MessageBubble role={mine ? "user" : "other"} style={{ whiteSpace: "pre-wrap" }}>
        {body}
      </MessageBubble>
    </MessageRow>
  );
}


const iconBtn: React.CSSProperties = {
  width: 28, height: 28, border: "none", background: "transparent",
  color: "#a8a29e", fontSize: 18, cursor: "pointer",
  display: "inline-flex", alignItems: "center", justifyContent: "center",
  borderRadius: 8,
};


// Tiny hook callers can import to power a sidebar badge without
// holding the whole panel mounted.
export function useSupportUnreadCount(enabled: boolean = true) {
  return useQuery({
    queryKey: ["support-unread"],
    queryFn: fetchSupportUnreadCount,
    enabled,
    refetchInterval: enabled ? 60_000 : false,
    retry: 0,
  });
}
