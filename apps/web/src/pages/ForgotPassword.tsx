import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { t } from "../lib/i18n";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      await api.auth.forgotPassword(email);
    } catch (err) {
      if (err instanceof ApiError && err.status >= 500) {
        setError(t("page.forgot_password.server_error"));
        setLoading(false);
        return;
      }
    }

    setSent(true);
    setLoading(false);
  };

  return (
    <div className="min-h-screen flex relative overflow-hidden">
      {/* Aurora background */}
      <div className="aurora-bg">
        <div className="aurora-blob aurora-blob-1" />
        <div className="aurora-blob aurora-blob-2" />
        <div className="aurora-blob aurora-blob-3" />
      </div>

      <div className="flex-1 flex items-center justify-center relative z-10 px-4">
        <div
          className="w-full animate-fade-in"
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
          {/* Logo */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10, marginBottom: 32 }}>
            <div style={{
              width: 40,
              height: 40,
              borderRadius: 12,
              background: "linear-gradient(135deg, #436b65, #4f7d75)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}>
              <svg style={{ width: 22, height: 22, color: "#fff" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
              </svg>
            </div>
            <span style={{ fontSize: 20, fontWeight: 800, color: "#292524" }}>{t("page.chat_history.manor_ai")}</span>
          </div>

          <h2 style={{ fontSize: 24, fontWeight: 900, color: "#292524", textAlign: "center", marginBottom: 4 }}>
            {t("page.forgot_password.title")}
          </h2>
          <p style={{ fontSize: 14, color: "#78716c", textAlign: "center", marginBottom: 28 }}>
            {t("page.forgot_password.subtitle")}
          </p>

          {sent ? (
            <div style={{ textAlign: "center" }}>
              <div style={{
                width: 52,
                height: 52,
                margin: "0 auto 16px",
                borderRadius: "50%",
                background: "#e4efe8",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}>
                <svg style={{ width: 24, height: 24, color: "#44895f" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                </svg>
              </div>
              <p style={{ fontSize: 15, fontWeight: 600, color: "#292524", marginBottom: 4 }}>{t("page.forgot_password.check_inbox")}</p>
              <p style={{ fontSize: 13, color: "#a8a29e", marginBottom: 24 }}>
                {t("page.forgot_password.if_this_email_exists_a_reset_link_has_been_sent")}
              </p>
              <Link
                to="/login"
                style={{ fontSize: 14, fontWeight: 600, color: "#4f7d75", textDecoration: "none" }}
              >
                {t("page.forgot_password.back_login")}
              </Link>
            </div>
          ) : (
            <form onSubmit={handleSubmit}>
              <div style={{ marginBottom: 20 }}>
                <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
                  {t("page.users.email")}
                </label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="manor-input"
                  placeholder={t("page.forgot_password.you_example_com")}
                  required
                />
              </div>

              {error && (
                <p style={{ fontSize: 13, color: "#c14a44", textAlign: "center", marginBottom: 16 }}>{error}</p>
              )}

              <button
                type="submit"
                disabled={loading}
                className="btn-manor"
                style={{
                  width: "100%",
                  padding: "10px 0",
                  fontSize: 14,
                  fontWeight: 700,
                  justifyContent: "center",
                  opacity: loading ? 0.6 : 1,
                }}
              >
                {loading ? t("page.messages.sending") : t("page.forgot_password.send_link")}
              </button>

              <div style={{ textAlign: "center", marginTop: 20 }}>
                <Link
                  to="/login"
                  style={{ fontSize: 14, fontWeight: 600, color: "#4f7d75", textDecoration: "none" }}
                >
                  {t("page.forgot_password.back_login")}
                </Link>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
