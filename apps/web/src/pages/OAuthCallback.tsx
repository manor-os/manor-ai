import { useState, useEffect, useRef } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { api } from "../lib/api";
import { useAuthStore } from "../stores/auth";
import LoadingSpinner from "../components/ui/LoadingSpinner";

import { t } from "../lib/i18n";

function consumeOAuthNext(): string {
  const next = sessionStorage.getItem("oauth_next");
  sessionStorage.removeItem("oauth_next");
  if (!next || !next.startsWith("/") || next.startsWith("//")) return "/chat";
  return next;
}

export default function OAuthCallback() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [needsInvite, setNeedsInvite] = useState(false);
  const [inviteCode, setInviteCode] = useState("");
  const [inviteLoading, setInviteLoading] = useState(false);
  const [inviteError, setInviteError] = useState("");
  const { checkAuth } = useAuthStore();

  // Stored from the 403 response so we can retry without the Google auth code
  const oauthSessionRef = useRef<string>("");
  const redirectUriRef = useRef<string>("");
  const publicChatTokenRef = useRef<string | null>(null);

  useEffect(() => {
    const code = searchParams.get("code");
    const state = searchParams.get("state");
    if (!code) {
      setError(t("page.oauth_callback.no_authorization_code_received_from_google"));
      return;
    }

    const savedState = sessionStorage.getItem("oauth_state");
    if (savedState && state !== savedState) {
      setError(t("page.oauth_callback.oauth_state_mismatch_possible_csrf_attack_please"));
      return;
    }
    sessionStorage.removeItem("oauth_state");

    const savedInvite = sessionStorage.getItem("oauth_invitation_code") || undefined;
    sessionStorage.removeItem("oauth_invitation_code");
    const savedTeamInvite = sessionStorage.getItem("oauth_team_invite") || undefined;
    sessionStorage.removeItem("oauth_team_invite");
    publicChatTokenRef.current = sessionStorage.getItem("oauth_public_chat_token");

    const redirectUri = window.location.origin + "/oauth/callback";
    redirectUriRef.current = redirectUri;

    api.auth
      .oauthGoogle({
        code,
        redirectUri,
        invitationCode: savedInvite,
        teamInviteToken: savedTeamInvite,
        publicChatToken: publicChatTokenRef.current || undefined,
      })
      .then((res) => {
        localStorage.setItem("manor_token", res.access_token);
        sessionStorage.removeItem("oauth_public_chat_token");
        return checkAuth();
      })
      .then(() => {
        navigate(consumeOAuthNext(), { replace: true });
      })
      .catch((err: any) => {
        const status = err?.status;
        let session = err?.detail?.oauth_session;
        // Fallback: if message contains oauth_session, try to parse it
        if (!session && status === 403 && err?.message) {
          try {
            const parsed = JSON.parse(err.message.replace(/'/g, '"'));
            session = parsed?.oauth_session;
          } catch { /* not parseable */ }
        }
        if (status === 403 && session) {
          oauthSessionRef.current = session;
          setNeedsInvite(true);
        } else if (err?.message?.includes("invite-only")) {
          // 403 but couldn't extract session — still show invite form
          // (shouldn't happen, but safety net)
          setNeedsInvite(true);
        } else {
          setError(err?.message || "OAuth sign-in failed. Please try again.");
        }
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleInviteSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!inviteCode.trim() || !oauthSessionRef.current) return;

    setInviteLoading(true);
    setInviteError("");

    try {
      const res = await api.auth.oauthGoogle({
        redirectUri: redirectUriRef.current,
        oauthSession: oauthSessionRef.current,
        invitationCode: inviteCode.trim(),
        publicChatToken: publicChatTokenRef.current || undefined,
      });
      localStorage.setItem("manor_token", res.access_token);
      sessionStorage.removeItem("oauth_public_chat_token");
      await checkAuth();
      navigate(consumeOAuthNext(), { replace: true });
    } catch (err: any) {
      setInviteLoading(false);
      if (err?.status === 403) {
        setInviteError("Invalid or expired invitation code.");
      } else {
        setInviteError(err?.message || "Sign-in failed. Please try again.");
      }
    }
  };

  const cardStyle = {
    maxWidth: 440,
    width: "100%",
    borderRadius: 40,
    background: "rgba(255,255,255,0.7)",
    backdropFilter: "blur(24px)",
    WebkitBackdropFilter: "blur(24px)",
    boxShadow: "0 25px 50px -12px rgba(0,0,0,0.15), 0 0 0 1px rgba(255,255,255,0.5)",
    padding: "48px 40px",
    textAlign: "center" as const,
  };

  return (
    <div className="min-h-screen flex items-center justify-center relative overflow-hidden">
      <div className="aurora-bg">
        <div className="aurora-blob aurora-blob-1" />
        <div className="aurora-blob aurora-blob-2" />
        <div className="aurora-blob aurora-blob-3" />
      </div>

      <div className="relative z-10 animate-fade-in" style={cardStyle}>
        {needsInvite ? (
          <form onSubmit={handleInviteSubmit}>
            <div style={{
              width: 64, height: 64, margin: "0 auto 24px", borderRadius: "50%",
              background: "linear-gradient(135deg, #ece9f5, #e6e9f3)",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}>
              <svg style={{ width: 32, height: 32, color: "#6d6fb2" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
              </svg>
            </div>
            <h2 style={{ fontSize: 22, fontWeight: 900, color: "#292524", marginBottom: 8 }}>
              {t("page.oauth_callback.invitation_required")}
            </h2>
            <p style={{ fontSize: 14, color: "#78716c", marginBottom: 24, lineHeight: 1.5 }}>
              {t("page.oauth_callback.we_re_currently_in_early_access_enter_your_invit")}
            </p>

            <input
              type="text"
              value={inviteCode}
              onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
              placeholder={t("page.oauth_callback.enter_invitation_code")}
              autoFocus
              style={{
                width: "100%", padding: "12px 16px", fontSize: 16, fontWeight: 600,
                letterSpacing: "0.1em", textAlign: "center",
                border: inviteError ? "2px solid #d65f59" : "2px solid #e7e5e4",
                borderRadius: 16, outline: "none", background: "rgba(255,255,255,0.8)",
                transition: "border-color 0.2s",
              }}
              onFocus={(e) => { if (!inviteError) e.target.style.borderColor = "#6d6fb2"; }}
              onBlur={(e) => { if (!inviteError) e.target.style.borderColor = "#e7e5e4"; }}
            />

            {inviteError && (
              <p style={{ fontSize: 13, color: "#d65f59", marginTop: 8 }}>{inviteError}</p>
            )}

            <button
              type="submit"
              disabled={!inviteCode.trim() || inviteLoading}
              style={{
                width: "100%", marginTop: 16, padding: "12px 24px",
                fontSize: 15, fontWeight: 700, color: "#fff",
                background: !inviteCode.trim() ? "#d6d3d1" : "linear-gradient(135deg, #6d6fb2, #9079c2)",
                border: "none", borderRadius: 16, cursor: !inviteCode.trim() ? "not-allowed" : "pointer",
                transition: "all 0.2s",
              }}
            >
              {inviteLoading ? t("page.login.verifying") : t("page.oauth_callback.continue")}
            </button>

            <Link
              to="/login"
              style={{
                display: "inline-block", marginTop: 16,
                fontSize: 13, color: "#a8a29e", textDecoration: "none",
              }}
            >
              {t("page.oauth_callback.back_to_login")}
            </Link>
          </form>
        ) : error ? (
          <>
            <div style={{
              width: 64, height: 64, margin: "0 auto 24px", borderRadius: "50%",
              background: "#f8f0ef",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}>
              <svg style={{ width: 32, height: 32, color: "#c14a44" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
              </svg>
            </div>
            <h2 style={{ fontSize: 22, fontWeight: 900, color: "#292524", marginBottom: 8 }}>{t("page.oauth_callback.sign_in_failed")}</h2>
            <p style={{ fontSize: 14, color: "#78716c", marginBottom: 28 }}>{error}</p>
            <Link
              to="/login"
              className="btn-manor-outline"
              style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
            >
              <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" />
              </svg>
              {t("page.oauth_callback.try_again")}
            </Link>
          </>
        ) : (
          <>
            <div style={{ margin: "0 auto 24px", display: "flex", justifyContent: "center" }}>
              <LoadingSpinner size={40} />
            </div>
            <p style={{ fontSize: 16, fontWeight: 600, color: "#292524", marginBottom: 4 }}>
              {t("page.oauth_callback.completing_sign_in")}
            </p>
            <p style={{ fontSize: 14, color: "#78716c" }}>
              {t("page.oauth_callback.please_wait_while_we_verify_your_account")}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
