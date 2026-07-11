import { useState } from "react";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import Modal from "../components/ui/Modal";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import { IconPlus } from "../components/icons";
import { PermissionBanner } from "../components/permissions";

type PortalView = "login" | "tickets";

interface Ticket {
  id: string;
  title: string;
  description?: string;
  status: string;
  created_at: string;
}

function PortalLogin({ onLogin }: { onLogin: (token: string) => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!token.trim()) {
      setError(t("page.client_portal.enter_token_error"));
      return;
    }
    api.portal.login(token.trim());
    onLogin(token.trim());
  };

  return (
    <div className="flex items-center justify-center min-h-screen relative overflow-hidden">
      {/* Aurora background */}
      <div className="aurora-bg">
        <div className="aurora-blob aurora-blob-1" />
        <div className="aurora-blob aurora-blob-2" />
        <div className="aurora-blob aurora-blob-3" />
      </div>

      <div
        className="relative z-10 w-full animate-fade-in"
        style={{
          maxWidth: 440,
          borderRadius: 40,
          background: "rgba(255,255,255,0.7)",
          backdropFilter: "blur(24px)",
          WebkitBackdropFilter: "blur(24px)",
          boxShadow: "0 25px 50px -12px rgba(0,0,0,0.15), 0 0 0 1px rgba(255,255,255,0.5)",
          padding: "40px 36px",
        }}
      >
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <div style={{
            width: 56,
            height: 56,
            margin: "0 auto 16px",
            borderRadius: 18,
            background: "linear-gradient(135deg, #436b65, #4f7d75)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}>
            <svg style={{ width: 28, height: 28, color: "#fff" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
            </svg>
          </div>
          <h1 style={{ fontSize: 22, fontWeight: 900, color: "#292524", marginBottom: 4 }}>{t("page.client_portal.title")}</h1>
          <p style={{ fontSize: 14, color: "#78716c" }}>{t("page.client_portal.login_subtitle")}</p>
        </div>

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 20 }}>
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
              {t("page.client_portal.portal_token")}
            </label>
            <input
              type="text"
              value={token}
              onChange={(e) => { setToken(e.target.value); setError(""); }}
              placeholder={t("page.client_portal.enter_token_placeholder")}
              className="manor-input"
              autoFocus
            />
            {error && <p style={{ fontSize: 12, color: "#c14a44", marginTop: 6 }}>{error}</p>}
          </div>
          <button
            type="submit"
            className="btn-manor"
            style={{ width: "100%", padding: "10px 0", justifyContent: "center", fontSize: 14, fontWeight: 700 }}
          >
            {t("page.client_portal.access_portal")}
          </button>
        </form>
      </div>
    </div>
  );
}

function PortalTickets({ token }: { token: string }) {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [selectedTicket, setSelectedTicket] = useState<Ticket | null>(null);

  // Fetch tickets on mount
  useState(() => {
    api.portal.listTickets(token)
      .then((data: any) => {
        setTickets(Array.isArray(data) ? data : data?.items || []);
      })
      .catch(() => setTickets([]))
      .finally(() => setLoading(false));
  });

  const handleCreateTicket = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTitle.trim()) return;
    setSubmitting(true);
    try {
      const ticket = await api.portal.submitTicket(token, {
        title: newTitle.trim(),
        description: newDescription.trim() || undefined,
      });
      setTickets((prev) => [ticket, ...prev]);
      setNewTitle("");
      setNewDescription("");
      setShowCreate(false);
    } catch {
      // silently fail for now
    } finally {
      setSubmitting(false);
    }
  };

  const statusColor = (status: string) => {
    switch (status) {
      case "open": return { bg: "#f3f6fa", color: "#1e40af" };
      case "in_progress": return { bg: "#faf7ef", color: "#76502c" };
      case "resolved": return { bg: "#f1f6f3", color: "#065f46" };
      case "closed": return { bg: "#f5f5f4", color: "#57534e" };
      default: return { bg: "#f5f5f4", color: "#57534e" };
    }
  };

  return (
    <div className="min-h-screen relative overflow-hidden">
      {/* Aurora background */}
      <div className="aurora-bg">
        <div className="aurora-blob aurora-blob-1" />
        <div className="aurora-blob aurora-blob-2" />
        <div className="aurora-blob aurora-blob-3" />
      </div>

      <div className="relative z-10 max-w-3xl mx-auto p-6 pt-12 animate-fade-in">
        <div style={{ marginBottom: 16 }}>
          <PermissionBanner reason="client_view" />
        </div>

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 900, color: "#292524" }}>{t("page.client_portal.my_tickets")}</h1>
            <p style={{ fontSize: 13, color: "#a8a29e", marginTop: 4 }}>{t("page.client_portal.tickets_subtitle")}</p>
          </div>
          <button onClick={() => setShowCreate(true)} className="btn-manor" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <IconPlus size={16} />
            {t("page.client_portal.submit_ticket")}
          </button>
        </div>

        {/* Ticket list */}
        <div className="glass-panel" style={{ overflow: "hidden" }}>
          {loading ? (
            <div style={{ padding: 48, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <LoadingSpinner size={28} />
            </div>
          ) : tickets.length === 0 ? (
            <div style={{ padding: 48 }}>
              <EmptyState
                icon={
                  <svg style={{ width: 32, height: 32, color: "#d6d3d1" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 6v.75m0 3v.75m0 3v.75m0 3V18m-9-5.25h5.25M7.5 15h3M3.375 5.25c-.621 0-1.125.504-1.125 1.125v3.026a2.999 2.999 0 010 5.198v3.026c0 .621.504 1.125 1.125 1.125h17.25c.621 0 1.125-.504 1.125-1.125v-3.026a2.999 2.999 0 010-5.198V6.375c0-.621-.504-1.125-1.125-1.125H3.375z" />
                  </svg>
                }
                title={t("page.client_portal.no_tickets")}
                action={
                  <button
                    onClick={() => setShowCreate(true)}
                    style={{ fontSize: 14, fontWeight: 600, color: "#436b65", background: "none", border: "none", cursor: "pointer" }}
                  >
                    {t("page.client_portal.submit_first_ticket")}
                  </button>
                }
              />
            </div>
          ) : (
            <div>
              {tickets.map((ticket, idx) => {
                const sc = statusColor(ticket.status);
                return (
                  <button
                    key={ticket.id}
                    onClick={() => setSelectedTicket(ticket)}
                    style={{
                      width: "100%",
                      padding: "12px 16px",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      background: "transparent",
                      border: "none",
                      borderTop: idx > 0 ? "1px solid rgba(245,245,244,0.8)" : "none",
                      cursor: "pointer",
                      textAlign: "left" as const,
                      transition: "background 0.15s",
                    }}
                    onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "rgba(250,250,249,0.6)"; }}
                    onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "transparent"; }}
                  >
                    <div style={{ minWidth: 0, flex: 1, marginRight: 12 }}>
                      <p style={{ fontSize: 14, fontWeight: 600, color: "#292524", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {ticket.title}
                      </p>
                      {ticket.description && (
                        <p style={{ fontSize: 12, color: "#a8a29e", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginTop: 2 }}>
                          {ticket.description}
                        </p>
                      )}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
                      <span style={{
                        padding: "2px 10px",
                        fontSize: 11,
                        fontWeight: 600,
                        borderRadius: 6,
                        background: sc.bg,
                        color: sc.color,
                      }}>
                        {ticket.status?.replace("_", " ") || t("page.client_portal.open")}
                      </span>
                      <span style={{ fontSize: 12, color: "#a8a29e" }}>
                        {ticket.created_at ? new Date(ticket.created_at).toLocaleDateString() : ""}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Ticket detail modal */}
        <Modal
          open={!!selectedTicket}
          onClose={() => setSelectedTicket(null)}
          title={t("page.client_portal.ticket_details")}
        >
          {selectedTicket && (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div>
                <span style={{ fontSize: 12, color: "#a8a29e", display: "block" }}>{t("page.client_portal.field_title")}</span>
                <span style={{ fontSize: 14, fontWeight: 600, color: "#292524" }}>{selectedTicket.title}</span>
              </div>
              {selectedTicket.description && (
                <div>
                  <span style={{ fontSize: 12, color: "#a8a29e", display: "block" }}>{t("page.client_portal.field_description")}</span>
                  <p style={{ fontSize: 14, color: "#57534e", whiteSpace: "pre-wrap" }}>{selectedTicket.description}</p>
                </div>
              )}
              <div style={{ display: "flex", gap: 24 }}>
                <div>
                  <span style={{ fontSize: 12, color: "#a8a29e", display: "block" }}>{t("page.client_portal.field_status")}</span>
                  <span style={{
                    display: "inline-block",
                    marginTop: 4,
                    padding: "2px 10px",
                    fontSize: 11,
                    fontWeight: 600,
                    borderRadius: 6,
                    background: statusColor(selectedTicket.status).bg,
                    color: statusColor(selectedTicket.status).color,
                  }}>
                    {selectedTicket.status?.replace("_", " ") || t("page.client_portal.open")}
                  </span>
                </div>
                <div>
                  <span style={{ fontSize: 12, color: "#a8a29e", display: "block" }}>{t("page.client_portal.field_created")}</span>
                  <span style={{ fontSize: 14, color: "#57534e" }}>
                    {selectedTicket.created_at ? new Date(selectedTicket.created_at).toLocaleString() : t("page.client_portal.na")}
                  </span>
                </div>
              </div>
            </div>
          )}
        </Modal>

        {/* Create ticket modal */}
        <Modal
          open={showCreate}
          onClose={() => setShowCreate(false)}
          title={t("page.client_portal.submit_a_ticket")}
          footer={
            <>
              <Button variant="outline" onClick={() => setShowCreate(false)}>
                {t("action.cancel")}
              </Button>
              <button
                type="submit"
                form="create-ticket-form"
                disabled={!newTitle.trim() || submitting}
                className="btn-manor"
                style={{ opacity: !newTitle.trim() || submitting ? 0.5 : 1 }}
              >
                {submitting ? t("page.client_portal.submitting") : t("page.client_portal.submit_ticket")}
              </button>
            </>
          }
        >
          <form id="create-ticket-form" onSubmit={handleCreateTicket} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Input
              label={t("page.client_portal.field_title")}
              type="text"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder={t("page.client_portal.title_placeholder")}
            />
            <Textarea
              label={t("page.client_portal.description_optional")}
              value={newDescription}
              onChange={(e) => setNewDescription(e.target.value)}
              placeholder={t("page.client_portal.description_placeholder")}
              rows={4}
            />
          </form>
        </Modal>
      </div>
    </div>
  );
}

export default function ClientPortal() {
  const [portalToken, setPortalToken] = useState<string | null>(
    () => localStorage.getItem("manor_portal_token")
  );
  const [view, setView] = useState<PortalView>(portalToken ? "tickets" : "login");

  const handleLogin = (token: string) => {
    setPortalToken(token);
    setView("tickets");
  };

  if (view === "login" || !portalToken) {
    return <PortalLogin onLogin={handleLogin} />;
  }

  return <PortalTickets token={portalToken} />;
}
