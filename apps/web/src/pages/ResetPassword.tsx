import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import AuthShell, { AuthBrand } from "../components/auth/AuthShell";
import { IconCheckCircle, IconEye, IconEyeOff, IconInfo, IconWarning } from "../components/icons";
import { api, ApiError } from "../lib/api";
import { t } from "../lib/i18n";

export default function ResetPassword() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") || "";

  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showNewPassword, setShowNewPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [tokenInvalid, setTokenInvalid] = useState(!token);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");

    if (!token) {
      setTokenInvalid(true);
      return;
    }
    if (newPassword !== confirmPassword) {
      setError(t("page.reset_password.passwords_do_not_match"));
      return;
    }
    if (newPassword.length < 12) {
      setError(t("page.reset_password.password_must_be_at_least_12_characters"));
      return;
    }

    setLoading(true);
    try {
      await api.auth.resetPassword(token, newPassword);
      setSuccess(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        setTokenInvalid(true);
      } else {
        setError(t("page.forgot_password.server_error"));
      }
    } finally {
      setLoading(false);
    }
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
        {success ? (
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
              {t("page.reset_password.title")}
            </h1>
            <p style={{ fontSize: 14, color: "var(--text-muted)", lineHeight: 1.6, marginBottom: 28 }}>
              {t("page.reset_password.success")}
            </p>
            <Link
              to="/login"
              className="login-submit-btn"
              style={{ display: "block", textAlign: "center", textDecoration: "none" }}
            >
              {t("page.reset_password.go_login")}
            </Link>
          </div>
        ) : tokenInvalid ? (
          <div role="alert" aria-live="assertive">
            <div
              className="flex items-center justify-center"
              style={{
                width: 52,
                height: 52,
                marginBottom: 20,
                borderRadius: 16,
                background: "var(--surface-sunken)",
                color: "var(--text-muted)",
              }}
            >
              <IconWarning size={24} />
            </div>
            <h1 style={{ fontSize: 30, fontWeight: 900, color: "var(--text-strong)", marginBottom: 8 }}>
              {t("page.reset_password.link_invalid_title")}
            </h1>
            <p style={{ fontSize: 14, color: "var(--text-muted)", lineHeight: 1.6, marginBottom: 28 }}>
              {t("page.reset_password.link_invalid_description")}
            </p>
            <Link
              to="/forgot-password"
              className="login-submit-btn"
              style={{ display: "block", textAlign: "center", textDecoration: "none" }}
            >
              {t("page.reset_password.request_new_link")}
            </Link>
            <div style={{ textAlign: "center", marginTop: 20 }}>
              <Link
                to="/login"
                style={{ fontSize: 14, fontWeight: 600, color: "var(--accent)", textDecoration: "none" }}
              >
                {t("page.forgot_password.back_login")}
              </Link>
            </div>
          </div>
        ) : (
          <>
            <h1 style={{ fontSize: 30, fontWeight: 900, color: "var(--text-strong)", marginBottom: 8 }}>
              {t("page.reset_password.set_new")}
            </h1>
            <p style={{ fontSize: 14, color: "var(--text-muted)", lineHeight: 1.6, marginBottom: 28 }}>
              {t("page.reset_password.subtitle")}
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
              <div style={{ marginBottom: 20 }}>
                <label htmlFor="new-password" className="login-label">
                  {t("page.reset_password.new_password")}
                </label>
                <div className="login-input-wrap">
                  <input
                    id="new-password"
                    type={showNewPassword ? "text" : "password"}
                    value={newPassword}
                    onChange={(event) => setNewPassword(event.target.value)}
                    className="login-input"
                    style={{ paddingRight: 42 }}
                    placeholder={t("page.reset_password.placeholder_min12")}
                    autoComplete="new-password"
                    minLength={12}
                    maxLength={72}
                    required
                  />
                  <button
                    type="button"
                    onClick={() => setShowNewPassword((visible) => !visible)}
                    className="login-pw-toggle"
                    aria-label={showNewPassword ? t("page.login.hide_password") : t("page.login.show_password")}
                  >
                    {showNewPassword ? <IconEyeOff size={18} /> : <IconEye size={18} />}
                  </button>
                </div>
              </div>

              <div style={{ marginBottom: 24 }}>
                <label htmlFor="confirm-password" className="login-label">
                  {t("page.reset_password.confirm_password")}
                </label>
                <div className="login-input-wrap">
                  <input
                    id="confirm-password"
                    type={showConfirmPassword ? "text" : "password"}
                    value={confirmPassword}
                    onChange={(event) => setConfirmPassword(event.target.value)}
                    className="login-input"
                    style={{ paddingRight: 42 }}
                    placeholder={t("page.reset_password.placeholder_reenter")}
                    autoComplete="new-password"
                    minLength={12}
                    maxLength={72}
                    required
                  />
                  <button
                    type="button"
                    onClick={() => setShowConfirmPassword((visible) => !visible)}
                    className="login-pw-toggle"
                    aria-label={showConfirmPassword ? t("page.login.hide_password") : t("page.login.show_password")}
                  >
                    {showConfirmPassword ? <IconEyeOff size={18} /> : <IconEye size={18} />}
                  </button>
                </div>
              </div>

              <button type="submit" disabled={loading} className="login-submit-btn">
                {loading ? t("page.reset_password.resetting") : t("page.reset_password.reset")}
              </button>
            </form>
          </>
        )}
      </main>
    </AuthShell>
  );
}
