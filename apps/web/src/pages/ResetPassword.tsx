import { useState } from "react";
import { Link, useSearchParams, useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { t } from "../lib/i18n";

export default function ResetPassword() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const token = searchParams.get("token") || "";

  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (!token) {
      setError(t("page.reset_password.invalid_or_missing_reset_token"));
      return;
    }
    if (newPwd !== confirmPwd) {
      setError(t("page.reset_password.passwords_do_not_match"));
      return;
    }
    if (newPwd.length < 8) {
      setError(t("page.reset_password.password_must_be_at_least_8_characters"));
      return;
    }

    setLoading(true);
    try {
      await api.auth.resetPassword(token, newPwd);
      setSuccess(true);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError(t("page.reset_password.failed_to_reset_password_the_link_may_have_expir"));
      }
    } finally {
      setLoading(false);
    }
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

          {success ? (
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
              <h2 style={{ fontSize: 20, fontWeight: 900, color: "#292524", marginBottom: 4 }}>{t("page.reset_password.title")}</h2>
              <p style={{ fontSize: 14, color: "#78716c", marginBottom: 24 }}>
                {t("page.reset_password.success")}
              </p>
              <button
                onClick={() => navigate("/login")}
                className="btn-manor"
                style={{ padding: "10px 24px", fontSize: 14, fontWeight: 700 }}
              >
                {t("page.reset_password.go_login")}
              </button>
            </div>
          ) : (
            <>
              <h2 style={{ fontSize: 24, fontWeight: 900, color: "#292524", textAlign: "center", marginBottom: 4 }}>
                {t("page.reset_password.set_new")}
              </h2>
              <p style={{ fontSize: 14, color: "#78716c", textAlign: "center", marginBottom: 28 }}>
                {t("page.reset_password.subtitle")}
              </p>

              {!token && (
                <div style={{
                  marginBottom: 20,
                  padding: "10px 16px",
                  borderRadius: 12,
                  background: "rgba(243,236,214,0.8)",
                  border: "1px solid rgba(207,155,68,0.2)",
                  fontSize: 13,
                  color: "#76502c",
                  textAlign: "center" as const,
                }}>
                  {t("page.reset_password.no_token")}
                </div>
              )}

              <form onSubmit={handleSubmit}>
                <div style={{ marginBottom: 20 }}>
                  <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
                    {t("page.reset_password.new_password")}
                  </label>
                  <input
                    type="password"
                    value={newPwd}
                    onChange={(e) => setNewPwd(e.target.value)}
                    className="manor-input"
                    placeholder={t("page.reset_password.placeholder_min8")}
                    required
                  />
                </div>
                <div style={{ marginBottom: 20 }}>
                  <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
                    {t("page.reset_password.confirm_password")}
                  </label>
                  <input
                    type="password"
                    value={confirmPwd}
                    onChange={(e) => setConfirmPwd(e.target.value)}
                    className="manor-input"
                    placeholder={t("page.reset_password.placeholder_reenter")}
                    required
                  />
                </div>

                {error && (
                  <p style={{ fontSize: 13, color: "#c14a44", textAlign: "center", marginBottom: 16 }}>{error}</p>
                )}

                <button
                  type="submit"
                  disabled={loading || !token}
                  className="btn-manor"
                  style={{
                    width: "100%",
                    padding: "10px 0",
                    fontSize: 14,
                    fontWeight: 700,
                    justifyContent: "center",
                    opacity: loading || !token ? 0.5 : 1,
                  }}
                >
                  {loading ? t("page.reset_password.resetting") : t("page.reset_password.reset")}
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
            </>
          )}
        </div>
      </div>
    </div>
  );
}
