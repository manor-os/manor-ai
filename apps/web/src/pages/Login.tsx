import { useEffect, useState, useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuthStore } from "../stores/auth";
import { api, ApiError } from "../lib/api";
import { IconEye, IconEyeOff, IconInfo } from "../components/icons";
import AuthShell, { AuthBrand } from "../components/auth/AuthShell";
import { navigateAfterAuth, safeAuthRedirect } from "../lib/authNavigation";
import { t } from "../lib/i18n";

type Tab = "login" | "register";

export default function Login() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const safeNextPath = safeAuthRedirect(searchParams.get("next"));
  const teamInviteToken = searchParams.get("team_invite") || searchParams.get("invite_token") || "";
  const inviteRedirectPath = teamInviteToken
    ? `/account?team_invite=${encodeURIComponent(teamInviteToken)}`
    : safeNextPath;
  const finishAuthNavigation = useCallback(() => {
    navigateAfterAuth(inviteRedirectPath, navigate);
  }, [inviteRedirectPath, navigate]);
  const defaultTab = searchParams.get("tab") === "register" ? "register" : "login";
  const [tab, setTab] = useState<Tab>(defaultTab);
  const [email, setEmail] = useState(() => searchParams.get("email") || "");
  const [password, setPassword] = useState("");
  const [entityName, setEntityName] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);
  const [rememberMe, setRememberMe] = useState(true);
  const [verificationCode, setVerificationCode] = useState("");
  const [resendCooldown, setResendCooldown] = useState(0);
  const [showForgotPassword, setShowForgotPassword] = useState(false);
  const [forgotEmail, setForgotEmail] = useState("");
  const [forgotSent, setForgotSent] = useState(false);
  const [totpCode, setTotpCode] = useState("");
  const [invitationCode, setInvitationCode] = useState(
    () => searchParams.get("invite") || "",
  );
  const [inviteRequired, setInviteRequired] = useState(false);

  // Probe the public signup-config endpoint to know whether invite is
  // mandatory. Falls back to false (open signup) on any error so a
  // misconfigured deploy never blocks signups.
  useEffect(() => {
    let cancelled = false;
    api.auth.signupConfig()
      .then((cfg) => { if (!cancelled) setInviteRequired(!!cfg.invitation_code_required); })
      .catch(() => { /* keep default false */ });
    return () => { cancelled = true; };
  }, []);

  const handleGoogleSignIn = useCallback(async () => {
    setError("");
    setGoogleLoading(true);
    try {
      const cfg = await api.auth.googleOAuthConfig();
      const clientId = (cfg.client_id || "").trim();
      if (!cfg.enabled || !clientId) {
        setError(t("page.login.google_sign_in_is_not_configured_for_this_deploy"));
        setGoogleLoading(false);
        return;
      }
      const redirectUri = encodeURIComponent(window.location.origin + "/oauth/callback");
      const scope = encodeURIComponent("openid email profile");
      const state = crypto.randomUUID();
      sessionStorage.setItem("oauth_state", state);
      sessionStorage.setItem("oauth_next", inviteRedirectPath);
      sessionStorage.setItem("oauth_remember_me", rememberMe ? "1" : "0");
      if (teamInviteToken) {
        sessionStorage.setItem("oauth_team_invite", teamInviteToken);
        sessionStorage.removeItem("oauth_invitation_code");
      } else {
        sessionStorage.removeItem("oauth_team_invite");
        const trimmedInvite = invitationCode.trim();
        if (trimmedInvite) {
          sessionStorage.setItem("oauth_invitation_code", trimmedInvite);
        } else {
          sessionStorage.removeItem("oauth_invitation_code");
        }
      }
      window.location.href = `https://accounts.google.com/o/oauth2/v2/auth?client_id=${encodeURIComponent(clientId)}&redirect_uri=${redirectUri}&response_type=code&scope=${scope}&access_type=offline&prompt=consent&state=${state}`;
    } catch {
      setError(t("page.login.could_not_start_google_sign_in_please_check_the"));
      setGoogleLoading(false);
    }
  }, [invitationCode, inviteRedirectPath, rememberMe, teamInviteToken]);

  const { login, login2fa, register, verifyEmail, resendVerification, pendingVerificationEmail, pending2fa } = useAuthStore();

  /* ---------- submit ---------- */
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      if (tab === "login") {
        await login(email, password, rememberMe);
        const state = useAuthStore.getState();
        if (state.pendingVerificationEmail || state.pending2fa) return;
        finishAuthNavigation();
      } else {
        await register(
          email, password,
          entityName || undefined,
          invitationCode.trim() || undefined,
          teamInviteToken || undefined,
        );
        const state = useAuthStore.getState();
        if (!state.pendingVerificationEmail && !state.pending2fa) {
          finishAuthNavigation();
        }
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError(t("page.login.unexpected_error"));
      }
    } finally {
      setLoading(false);
    }
  };

  const handleVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await verifyEmail(pendingVerificationEmail!, verificationCode);
      finishAuthNavigation();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError(t("page.login.invalid_verification_code"));
      }
    } finally {
      setLoading(false);
    }
  };

  const handle2fa = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login2fa(totpCode);
      finishAuthNavigation();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("page.login.invalid_2fa_code"));
    } finally {
      setLoading(false);
    }
  };

  const handleResend = async () => {
    if (resendCooldown > 0) return;
    try {
      await resendVerification(pendingVerificationEmail!);
      setResendCooldown(60);
      const timer = setInterval(() => {
        setResendCooldown((prev) => {
          if (prev <= 1) { clearInterval(timer); return 0; }
          return prev - 1;
        });
      }, 1000);
    } catch {
      setError(t("page.login.failed_resend"));
    }
  };

  return (
    <AuthShell>
      {/* Sticky header */}
      <div className="sticky top-0 z-20 flex items-center justify-between p-8 bg-transparent">
        <AuthBrand />
        <button
          type="button"
          onClick={() => { setTab(tab === "login" ? "register" : "login"); setError(""); useAuthStore.setState({ pendingVerificationEmail: null }); setVerificationCode(""); setShowForgotPassword(false); }}
          style={{ fontSize: 13, fontWeight: 600, color: "#4f7d75", background: "transparent", border: "none", cursor: "pointer" }}
        >
          {tab === "login" ? t("page.login.create_account") : t("page.login.sign_in")}
        </button>
      </div>

      {/* Form body */}
      <div className="flex-1 flex flex-col justify-center px-8 pb-8" style={{ maxWidth: 420 }}>
            <h1 style={{ fontSize: 30, fontWeight: 900, color: "#292524", marginBottom: 8 }}>
              {pending2fa ? t("page.login.two_factor_auth") : pendingVerificationEmail ? t("page.login.verify_email") : showForgotPassword ? t("page.login.reset_password") : tab === "login" ? t("page.login.sign_in") : t("page.login.create_account")}
            </h1>
            <p style={{ fontSize: 14, color: "#78716c", marginBottom: 28 }}>
              {pending2fa
                ? t("page.login.two_factor_desc")
                : pendingVerificationEmail
                ? t("page.login.verify_email_desc")
                : showForgotPassword
                ? t("page.login.reset_desc")
                : tab === "login"
                ? t("page.login.sign_in_desc")
                : t("page.login.create_desc")}
            </p>

            {/* Error */}
            {error && (
              <div
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

            {/* Email Verification Step */}
            {pending2fa ? (
              /* 2FA Code Input */
              <form onSubmit={handle2fa}>
                <div style={{ textAlign: "center", marginBottom: 24 }}>
                  <div style={{ width: 56, height: 56, borderRadius: 16, background: "linear-gradient(135deg, #9079c2, #a78bfa)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
                    <svg width="24" height="24" fill="none" viewBox="0 0 24 24" stroke="white" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
                    </svg>
                  </div>
                  <p style={{ fontSize: 14, color: "#78716c" }}>
                    {t("page.login.enter_2fa_code")}
                  </p>
                </div>
                <div style={{ marginBottom: 22 }}>
                  <label className="login-label">{t("page.login.authentication_code")}</label>
                  <div className="login-input-wrap">
                    <input
                      type="text"
                      required
                      maxLength={6}
                      value={totpCode}
                      onChange={(e) => setTotpCode(e.target.value.replace(/\D/g, ""))}
                      className="login-input"
                      style={{ textAlign: "center", fontSize: 24, letterSpacing: 8, fontWeight: 700 }}
                      placeholder="000000"
                      autoFocus
                    />
                  </div>
                </div>
                <button type="submit" disabled={loading || totpCode.length !== 6} className="login-submit-btn">
                  {loading ? t("page.login.verifying") : t("page.login.verify_and_sign_in")}
                </button>
                <div style={{ textAlign: "center", marginTop: 16 }}>
                  <button type="button" onClick={() => { useAuthStore.setState({ pending2fa: null }); setTotpCode(""); setError(""); }}
                    style={{ fontSize: 13, fontWeight: 600, color: "#78716c", background: "transparent", border: "none", cursor: "pointer" }}>
                    {t("page.login.back_to_sign_in")}
                  </button>
                </div>
              </form>
            ) : pendingVerificationEmail ? (
              <form onSubmit={handleVerify} key="verify">
                <div style={{ textAlign: "center", marginBottom: 24 }}>
                  <div style={{ width: 56, height: 56, borderRadius: 16, background: "linear-gradient(135deg, #4f7d75, #5f928a)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
                    <svg width="24" height="24" fill="none" viewBox="0 0 24 24" stroke="white" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
                    </svg>
                  </div>
                  <p style={{ fontSize: 14, color: "#78716c" }}>
                    {t("page.login.sent_code_to")} <strong style={{ color: "#292524" }}>{pendingVerificationEmail}</strong>
                  </p>
                </div>

                <div style={{ marginBottom: 22 }}>
                  <label className="login-label">{t("page.login.verification_code")}</label>
                  <div className="login-input-wrap">
                    <input
                      type="text"
                      required
                      maxLength={8}
                      value={verificationCode}
                      onChange={(e) => setVerificationCode(
                        e.target.value.replace(/[^A-Za-z0-9]/g, "").toUpperCase(),
                      )}
                      className="login-input"
                      style={{ textAlign: "center", fontSize: 22, letterSpacing: 5, fontWeight: 700 }}
                      placeholder="AB12CD34"
                      autoComplete="one-time-code"
                      autoFocus
                    />
                  </div>
                </div>

                <button type="submit" disabled={loading || verificationCode.length !== 8} className="login-submit-btn">
                  {loading ? t("page.login.verifying") : t("page.login.verify_email")}
                </button>

                <div style={{ textAlign: "center", marginTop: 16 }}>
                  <button
                    type="button"
                    onClick={handleResend}
                    disabled={resendCooldown > 0}
                    style={{
                      fontSize: 13, fontWeight: 600,
                      color: resendCooldown > 0 ? "#a8a29e" : "#4f7d75",
                      background: "transparent", border: "none", cursor: resendCooldown > 0 ? "default" : "pointer",
                    }}
                  >
                    {resendCooldown > 0 ? `${t("page.login.resend_code_in")} ${resendCooldown}s` : t("page.login.resend_code")}
                  </button>
                  <span style={{ color: "#d6d3d1", margin: "0 4px" }}>|</span>
                  <button
                    type="button"
                    onClick={() => { useAuthStore.setState({ pendingVerificationEmail: null }); setVerificationCode(""); setError(""); }}
                    style={{ fontSize: 13, fontWeight: 600, color: "#78716c", background: "transparent", border: "none", cursor: "pointer" }}
                  >
                    {t("page.login.back_to_sign_in")}
                  </button>
                </div>
              </form>
            ) : showForgotPassword ? (
              /* Forgot Password Form */
              <div>
                <div style={{ textAlign: "center", marginBottom: 24 }}>
                  <div style={{ width: 56, height: 56, borderRadius: 16, background: "linear-gradient(135deg, #4f7d75, #5f928a)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
                    <svg width="24" height="24" fill="none" viewBox="0 0 24 24" stroke="white" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1121.75 8.25z" />
                    </svg>
                  </div>
                  <p style={{ fontSize: 14, color: "#78716c" }}>
                    {forgotSent ? t("page.login.reset_link_sent") : t("page.login.enter_email_reset_link")}
                  </p>
                </div>
                {!forgotSent && (
                  <>
                    <div style={{ marginBottom: 16 }}>
                      <label className="login-label">{t("page.login.email_address")}</label>
                      <input type="email" value={forgotEmail} onChange={(e) => setForgotEmail(e.target.value)} className="login-input" placeholder={t("page.login.name_company_com")} />
                    </div>
                    <button
                      type="button"
                      className="login-submit-btn"
                      onClick={async () => {
                        if (!forgotEmail) return;
                        try {
                          await api.auth.forgotPassword(forgotEmail);
                          setForgotSent(true);
                        } catch { setError(t("page.login.failed_send_reset_email")); }
                      }}
                    >
                      {t("page.login.send_reset_link")}
                    </button>
                  </>
                )}
                <div style={{ textAlign: "center", marginTop: 16 }}>
                  <button type="button" onClick={() => setShowForgotPassword(false)} style={{ fontSize: 13, fontWeight: 600, color: "#4f7d75", background: "transparent", border: "none", cursor: "pointer" }}>
                    {t("page.login.back_to_sign_in")}
                  </button>
                </div>
              </div>
            ) : (
            <>
            <form onSubmit={handleSubmit}>
              {teamInviteToken && (
                <div style={{
                  marginBottom: 18,
                  padding: "10px 12px",
                  borderRadius: 12,
                  background: "rgba(242,246,245,0.85)",
                  border: "1px solid rgba(79,125,117,0.18)",
                  color: "#5d7f77",
                  fontSize: 12,
                  lineHeight: 1.5,
                  fontWeight: 600,
                }}>
                  {t("page.login.team_invite_hint")}
                </div>
              )}
              {tab === "login" ? (
                <>
                  {/* Email field */}
                  <div style={{ marginBottom: 18 }}>
                    <label className="login-label">{t("page.login.email_address")}</label>
                    <div className="login-input-wrap">
                      <svg className="login-input-icon" viewBox="0 0 20 20" fill="currentColor">
                        <path d="M2.003 5.884L10 9.882l7.997-3.998A2 2 0 0016 4H4a2 2 0 00-1.997 1.884z" />
                        <path d="M18 8.118l-8 4-8-4V14a2 2 0 002 2h12a2 2 0 002-2V8.118z" />
                      </svg>
                      <input
                        type="email"
                        required
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        className="login-input"
                        style={{ paddingLeft: 40 }}
                        placeholder={t("page.login.name_company_com")}
                      />
                    </div>
                  </div>

                  {/* Password field */}
                  <div style={{ marginBottom: 10 }}>
                    <label className="login-label">{t("page.login.password")}</label>
                    <div className="login-input-wrap">
                      <svg className="login-input-icon" viewBox="0 0 20 20" fill="currentColor">
                        <path fillRule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clipRule="evenodd" />
                      </svg>
                      <input
                        type={showPassword ? "text" : "password"}
                        required
                        maxLength={72}
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        className="login-input"
                        style={{ paddingLeft: 40, paddingRight: 42 }}
                        placeholder="••••••••"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="login-pw-toggle"
                        tabIndex={-1}
                      >
                        {showPassword ? (
                          <IconEyeOff size={16} />
                        ) : (
                          <IconEye size={16} />
                        )}
                      </button>
                    </div>
                  </div>

                  {/* Remember me + Forgot password */}
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 22 }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, color: "#57534e", fontWeight: 500 }}>
                      <input
                        type="checkbox"
                        checked={rememberMe}
                        onChange={(e) => setRememberMe(e.target.checked)}
                        style={{ width: 16, height: 16, accentColor: "#4f7d75", cursor: "pointer" }}
                      />
                      {t("page.login.remember_me")}
                    </label>
                    <button
                      type="button"
                      onClick={() => { setShowForgotPassword(true); setForgotEmail(email); setForgotSent(false); setError(""); }}
                      style={{ fontSize: 13, fontWeight: 600, color: "#4f7d75", background: "transparent", border: "none", cursor: "pointer" }}
                    >
                      {t("page.login.forgot_password")}
                    </button>
                  </div>
                </>
              ) : (
                <>
                  {/* Invitation code — required when the platform gate is on */}
                  {inviteRequired && !teamInviteToken && (
                    <div style={{ marginBottom: 18 }}>
                      <label className="login-label">
                        {t("page.login.invitation_code")}
                        <span style={{ fontWeight: 400, textTransform: "none", color: "#c14a44", marginLeft: 6 }}>({t("page.login.required")})</span>
                      </label>
                      <div className="login-input-wrap">
                        <svg className="login-input-icon" viewBox="0 0 20 20" fill="currentColor">
                          <path fillRule="evenodd" d="M5 5a3 3 0 015-2.236A3 3 0 0114.83 6H16a2 2 0 110 4h-5V9a1 1 0 10-2 0v1H4a2 2 0 110-4h1.17C5.06 5.687 5 5.35 5 5zm4 1V5a1 1 0 10-1 1h1zm3 0a1 1 0 10-1-1v1h1z" clipRule="evenodd" />
                          <path d="M9 11H3v5a2 2 0 002 2h4v-7zM11 18h4a2 2 0 002-2v-5h-6v7z" />
                        </svg>
                        <input
                          type="text"
                          required={inviteRequired}
                          value={invitationCode}
                          onChange={(e) => setInvitationCode(e.target.value.toUpperCase())}
                          className="login-input"
                          style={{ paddingLeft: 40, textTransform: "uppercase", fontFamily: "ui-monospace, monospace" }}
                          placeholder={t("page.login.enter_invitation_code")}
                          autoComplete="off"
                        />
                      </div>
                      <p style={{ fontSize: 11, color: "#78716c", margin: "6px 0 0" }}>
                        {t("page.login.invite_only")}{" "}
                        <a
                          href="https://app.manorai.xyz"
                          target="_blank"
                          rel="noopener noreferrer"
                          style={{ color: "#4f7d75" }}
                        >
                          {t("page.login.join_waitlist")}
                        </a>.
                      </p>
                    </div>
                  )}
                  {/* Company Name (optional) */}
                  <div style={{ marginBottom: 18 }}>
                    <label className="login-label">
                      {teamInviteToken ? t("page.login.full_name") : t("page.login.company_name")}
                      <span style={{ fontWeight: 400, textTransform: "none", color: "#a8a29e", marginLeft: 4 }}>({t("page.login.optional")})</span>
                    </label>
                    <div className="login-input-wrap">
                      <svg className="login-input-icon" viewBox="0 0 20 20" fill="currentColor">
                        <path fillRule="evenodd" d="M4 4a2 2 0 012-2h8a2 2 0 012 2v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4zm3 1h2v2H7V5zm2 4H7v2h2V9zm2-4h2v2h-2V5zm2 4h-2v2h2V9z" clipRule="evenodd" />
                      </svg>
                      <input
                        type="text"
                        value={entityName}
                        onChange={(e) => setEntityName(e.target.value)}
                        className="login-input"
                        style={{ paddingLeft: 40 }}
                        placeholder={teamInviteToken ? t("page.team_people.jane_doe") : t("page.login.company_placeholder")}
                      />
                    </div>
                  </div>
                  {/* Email */}
                  <div style={{ marginBottom: 18 }}>
                    <label className="login-label">{t("page.login.email_address")}</label>
                    <div className="login-input-wrap">
                      <svg className="login-input-icon" viewBox="0 0 20 20" fill="currentColor">
                        <path d="M2.003 5.884L10 9.882l7.997-3.998A2 2 0 0016 4H4a2 2 0 00-1.997 1.884z" />
                        <path d="M18 8.118l-8 4-8-4V14a2 2 0 002 2h12a2 2 0 002-2V8.118z" />
                      </svg>
                      <input
                        type="email"
                        required
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        className="login-input"
                        style={{ paddingLeft: 40 }}
                        placeholder={t("page.login.name_company_com")}
                      />
                    </div>
                  </div>
                  {/* Password */}
                  <div style={{ marginBottom: 22 }}>
                    <label className="login-label">{t("page.login.password")}</label>
                    <div className="login-input-wrap">
                      <svg className="login-input-icon" viewBox="0 0 20 20" fill="currentColor">
                        <path fillRule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clipRule="evenodd" />
                      </svg>
                      <input
                        type={showPassword ? "text" : "password"}
                        required
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        className="login-input"
                        style={{ paddingLeft: 40, paddingRight: 42 }}
                        placeholder="••••••••"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="login-pw-toggle"
                        tabIndex={-1}
                      >
                        {showPassword ? (
                          <IconEyeOff size={16} />
                        ) : (
                          <IconEye size={16} />
                        )}
                      </button>
                    </div>
                  </div>
                </>
              )}

              {/* Submit */}
              <button
                type="submit"
                disabled={loading}
                className="login-submit-btn"
              >
                {loading ? (
                  <span style={{ display: "flex", alignItems: "center", gap: 8, opacity: 0.8 }}>
                    <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="31.4 31.4" strokeLinecap="round" />
                    </svg>
                    {t("page.login.please_wait")}
                  </span>
                ) : (
                  teamInviteToken
                    ? t("page.login.accept_invite")
                    : tab === "login"
                      ? t("page.login.sign_in")
                      : t("page.login.create_account")
                )}
              </button>
            </form>

            <>
              {/* Divider */}
              <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "24px 0" }}>
                <div style={{ flex: 1, height: 1, background: "rgba(214,211,209,0.5)" }} />
                <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", color: "#a8a29e", letterSpacing: "0.05em" }}>
                  {t("page.login.or_continue_with")}
                </span>
                <div style={{ flex: 1, height: 1, background: "rgba(214,211,209,0.5)" }} />
              </div>

              {/* Google button */}
              <button
                type="button"
                onClick={handleGoogleSignIn}
                disabled={googleLoading}
                className="login-google-btn"
              >
                <svg style={{ width: 20, height: 20 }} viewBox="0 0 24 24">
                  <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" />
                  <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
                  <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
                  <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
                </svg>
                {googleLoading ? t("page.login.connecting_to_google") : t("page.login.sign_in_with_google")}
              </button>
            </>

            </>
            )}
      </div>
    </AuthShell>
  );
}
