import { useState } from "react";
import { Link } from "react-router-dom";
import AuthShell, { AuthBrand } from "../components/auth/AuthShell";
import { IconCheckCircle, IconInfo } from "../components/icons";
import { api, ApiError } from "../lib/api";
import { t } from "../lib/i18n";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
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
    <AuthShell>
      <div className="sticky top-0 z-20 flex items-center justify-between p-8 bg-transparent">
        <AuthBrand />
        <Link
          to="/login"
          style={{ fontSize: 13, fontWeight: 600, color: "var(--accent)", textDecoration: "none" }}
        >
          {t("page.forgot_password.back_login")}
        </Link>
      </div>

      <main
        className="flex-1 flex flex-col justify-center px-8 pb-8"
        style={{ maxWidth: 420, width: "100%", margin: "0 auto" }}
      >
        {sent ? (
          <div role="status" aria-live="polite">
            <div
              className="flex items-center justify-center"
              style={{
                width: 52,
                height: 52,
                marginBottom: 20,
                borderRadius: 16,
                background: "var(--accent-soft)",
                color: "var(--accent)",
              }}
            >
              <IconCheckCircle size={24} />
            </div>
            <h1 style={{ fontSize: 30, fontWeight: 900, color: "var(--text-strong)", marginBottom: 8 }}>
              {t("page.forgot_password.check_inbox")}
            </h1>
            <p style={{ fontSize: 14, color: "var(--text-muted)", lineHeight: 1.6, marginBottom: 28 }}>
              {t("page.forgot_password.if_this_email_exists_a_reset_link_has_been_sent")}
            </p>
            <Link
              to="/login"
              className="login-submit-btn"
              style={{ display: "block", textAlign: "center", textDecoration: "none" }}
            >
              {t("page.forgot_password.back_login")}
            </Link>
          </div>
        ) : (
          <>
            <h1 style={{ fontSize: 30, fontWeight: 900, color: "var(--text-strong)", marginBottom: 8 }}>
              {t("page.forgot_password.title")}
            </h1>
            <p style={{ fontSize: 14, color: "var(--text-muted)", lineHeight: 1.6, marginBottom: 28 }}>
              {t("page.forgot_password.subtitle")}
            </p>

            {error && (
              <div
                role="alert"
                style={{
                  marginBottom: 20,
                  padding: 12,
                  borderRadius: 12,
                  background: "rgba(248,240,239,0.8)",
                  border: "1px solid rgba(214,95,89,0.2)",
                  color: "#a23e38",
                  fontSize: 13,
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <IconInfo size={16} className="shrink-0" />
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit}>
              <div style={{ marginBottom: 24 }}>
                <label htmlFor="reset-email" className="login-label">
                  {t("page.users.email")}
                </label>
                <input
                  id="reset-email"
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  className="login-input"
                  placeholder={t("page.forgot_password.you_example_com")}
                  autoComplete="email"
                  required
                />
              </div>

              <button type="submit" disabled={loading} className="login-submit-btn">
                {loading ? t("page.messages.sending") : t("page.forgot_password.send_link")}
              </button>
            </form>
          </>
        )}
      </main>
    </AuthShell>
  );
}
