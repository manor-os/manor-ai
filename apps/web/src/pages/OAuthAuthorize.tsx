/**
 * OAuth 2.0 Authorize consent page (Manor as IdP).
 *
 * Visually modelled on Google's OAuth consent screen:
 *   - Borderless white card on a soft background
 *   - Official Manor mark + the app's official mark, app name as the title
 *   - Signed-in account row
 *   - Short permission list, then Cancel / Continue
 *
 * Flow:
 *   1. Client app (e.g. PMS) redirects user here with client_id+redirect_uri+state.
 *   2. If unauthenticated → bounce to /login?next=... and come back.
 *   3. Fetch client metadata (public endpoint).
 *   4. Show consent. On Continue → POST /authorize → set window.location to the code URL.
 *      On Cancel → redirect back to client with ?error=access_denied.
 */
import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useAuthStore } from "../stores/auth";
import { IconManorLogo } from "../components/icons";
import IntegrationLogo from "../components/IntegrationLogo";
import { resolveIntegrationBrand } from "../lib/brands/catalog";
import CurrentEmailVerification from "../components/auth/CurrentEmailVerification";
import Button from "../components/ui/Button";
import { isSameAuthIdentity } from "../lib/authToken";

interface ClientInfo {
  client_id: string;
  name: string;
  description?: string | null;
  redirect_uris: string[];
  allowed_scopes: string[];
  require_pkce?: boolean;
  require_verified_email?: boolean;
  /** False for apps that registered themselves (e.g. a local MCP client). */
  verified?: boolean;
  /** Server-side match of the requested redirect URI (loopback ports vary). */
  redirect_uri_allowed?: boolean | null;
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "";

function redirectWithError(redirectUri: string, error: string, state: string | null) {
  if (!redirectUri) return;
  const sep = redirectUri.includes("?") ? "&" : "?";
  const qs = new URLSearchParams({ error });
  if (state) qs.set("state", state);
  window.location.href = `${redirectUri}${sep}${qs.toString()}`;
}

function safeDomain(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

function initialsOf(name?: string | null, email?: string | null): string {
  const src = (name || email || "?").trim();
  const parts = src.split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return src.slice(0, 2).toUpperCase();
}

// ── Small inline icons (no extra dep) ──

function CheckIcon() {
  return (
    <svg className="w-4 h-4 text-emerald-600 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path fillRule="evenodd" d="M16.704 5.29a1 1 0 010 1.42l-7.5 7.5a1 1 0 01-1.42 0l-3.5-3.5a1 1 0 011.42-1.42L8.5 12.07l6.79-6.78a1 1 0 011.414 0z" clipRule="evenodd" />
    </svg>
  );
}

/** "Claude Code (manor-staging)" → title "Claude Code", qualifier "manor-staging". */
function splitClientName(name: string): { title: string; qualifier: string } {
  const match = name.match(/^(.+?)\s*\(([^()]+)\)\s*$/);
  return match ? { title: match[1], qualifier: match[2] } : { title: name, qualifier: "" };
}

// A self-registered app chooses its own name, so it only gets the official
// mark of the coding agents that register this way, never any catalog brand.
const UNVERIFIED_APP_BRANDS = new Set(["claude", "openai", "cursor", "googlegemini", "continue"]);

function appBrandKey(title: string, verified: boolean): string | null {
  const brand = resolveIntegrationBrand(title);
  if (!brand?.icon) return null;
  return verified || UNVERIFIED_APP_BRANDS.has(brand.key) ? brand.key : null;
}

function LogoTile({ children, dark = false }: { children: React.ReactNode; dark?: boolean }) {
  return (
    <div
      className={`w-12 h-12 rounded-[14px] flex items-center justify-center select-none shrink-0 ${
        dark ? "bg-black text-white" : "bg-white text-stone-700 font-semibold text-sm"
      }`}
      style={{ boxShadow: "var(--shadow-md)" }}
      aria-hidden="true"
    >
      {children}
    </div>
  );
}

const Frame = ({ children }: { children: React.ReactNode }) => (
  <div className="min-h-screen flex items-center justify-center bg-stone-50 p-4 sm:p-6">
    <div className="w-full max-w-sm">
      <div
        className="bg-white rounded-[22px] p-7 sm:p-8 text-stone-900"
        style={{ boxShadow: "var(--shadow-lg)" }}
      >
        {children}
      </div>
    </div>
  </div>
);


// ── Page ──

export default function OAuthAuthorize() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  // Capture the consent route for this render. Reading window.location inside
  // a replayed effect can wrap the login URL into its own `next` parameter.
  const backUrl = location.pathname + location.search;
  const { user, token: accountToken, checkAuth } = useAuthStore();
  const accountIdentity = user ? JSON.stringify([user.id, user.entity_id, user.email]) : "";

  const clientId = searchParams.get("client_id") || "";
  const redirectUri = searchParams.get("redirect_uri") || "";
  const state = searchParams.get("state");
  const scope = searchParams.get("scope") || "";
  const responseType = searchParams.get("response_type") || "code";
  const codeChallenge = searchParams.get("code_challenge");
  const codeChallengeMethod = searchParams.get("code_challenge_method");

  const [info, setInfo] = useState<ClientInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const [emailVerified, setEmailVerified] = useState(false);
  const [accountReady, setAccountReady] = useState(false);
  const [accountAttempt, setAccountAttempt] = useState(0);

  // Validate required params up front
  const paramsError = useMemo(() => {
    for (const key of searchParams.keys()) {
      if (searchParams.getAll(key).length > 1) return "Duplicate sign-in parameter";
    }
    if (codeChallenge !== null || codeChallengeMethod !== null) {
      if (codeChallengeMethod !== "S256" || !codeChallenge || !/^[A-Za-z0-9_-]{43}$/.test(codeChallenge)) {
        return "A valid S256 sign-in challenge is required";
      }
    }
    if (!clientId) return "Missing client_id";
    if (!redirectUri) return "Missing redirect_uri";
    if (responseType !== "code") return "Only response_type=code is supported";
    return "";
  }, [clientId, redirectUri, responseType, codeChallenge, codeChallengeMethod, searchParams]);

  // A cold consent-page load has a token but no hydrated user. Confirm the
  // account before showing consent, including remembered-session restoration.
  useEffect(() => {
    if (paramsError) return;
    let cancelled = false;
    setAccountReady(false);
    void checkAuth().then(() => {
      if (cancelled) return;
      const session = useAuthStore.getState();
      if (!session.user) {
        if (!session.token) {
          navigate(`/login?next=${encodeURIComponent(backUrl)}`, { replace: true });
          return;
        }
        setError("Could not confirm your Manor account. Try again before authorizing this app.");
        setLoading(false);
      }
      setAccountReady(true);
    });
    return () => { cancelled = true; };
  }, [paramsError, navigate, backUrl, checkAuth, accountAttempt]);

  // Load client info (with auto-retry on transient 502s during deploy)
  useEffect(() => {
    if (paramsError) {
      setLoading(false);
      setError(paramsError);
      return;
    }
    if (!accountReady || !accountIdentity) return;
    const token = localStorage.getItem("manor_token");
    if (!token) return; // wait for redirect
    let cancelled = false;
    (async () => {
      setLoading(true);
      setInfo(null);
      setEmailVerified(false);
      setError("");
      try {
        const r = await fetch(
          `${API_BASE}/api/v1/oauth/clients/${encodeURIComponent(clientId)}?redirect_uri=${encodeURIComponent(redirectUri)}`,
          { headers: { Authorization: `Bearer ${token}` } }
        );
        if (cancelled) return;
        if (r.status === 502 || r.status === 503) {
          // Service temporarily unavailable (e.g. mid-deploy). Auto-retry up to 3x with backoff.
          if (retryCount < 3) {
            const wait = 1500 * (retryCount + 1);
            setTimeout(() => setRetryCount((c) => c + 1), wait);
            return;
          }
          setError(
            "Manor is briefly unavailable. Try again in a moment, or contact support."
          );
          setLoading(false);
          return;
        }
        if (r.status === 404) {
          setError(`Application not registered with Manor.`);
          setLoading(false);
          return;
        }
        if (!r.ok) {
          setError(`Cannot load app details (HTTP ${r.status}).`);
          setLoading(false);
          return;
        }
        const data = (await r.json()) as ClientInfo;
        if (!(data.redirect_uri_allowed ?? data.redirect_uris.includes(redirectUri))) {
          setError(
            `The redirect URL "${safeDomain(redirectUri)}" is not registered for this app.`
          );
          setLoading(false);
          return;
        }
        if (data.require_pkce && !codeChallenge) {
          setError("This app requires an S256 sign-in challenge. Start sign-in again from the app.");
          setLoading(false);
          return;
        }
        setInfo(data);
        setLoading(false);
      } catch (e: any) {
        if (cancelled) return;
        if (retryCount < 3) {
          const wait = 1500 * (retryCount + 1);
          setTimeout(() => setRetryCount((c) => c + 1), wait);
          return;
        }
        setError(`Network error: ${e?.message || e}`);
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [clientId, redirectUri, paramsError, retryCount, codeChallenge, accountReady, accountIdentity]);

  async function handleApprove() {
    if (paramsError || !accountReady || !user || !info || (info.require_pkce && !codeChallenge)
      || (info.require_verified_email && !emailVerified)) return;
    setSubmitting(true);
    setError("");
    const token = localStorage.getItem("manor_token");
    if (!token) {
      const next = encodeURIComponent(backUrl);
      navigate(`/login?next=${next}`, { replace: true });
      return;
    }
    if (!isSameAuthIdentity(accountToken, token)) {
      // Another tab changed accounts: show that account for a fresh review,
      // never authorize it under the identity previously shown on this page.
      setSubmitting(false);
      setLoading(true);
      setAccountAttempt((attempt) => attempt + 1);
      return;
    }
    try {
      const r = await fetch(`${API_BASE}/api/v1/oauth/authorize`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          client_id: clientId,
          redirect_uri: redirectUri,
          scope,
          state,
          ...(codeChallenge !== null ? { code_challenge: codeChallenge, code_challenge_method: codeChallengeMethod } : {}),
        }),
      });
      if (!r.ok) {
        const text = await r.text();
        setError(`Authorization failed (HTTP ${r.status}): ${text}`);
        setSubmitting(false);
        return;
      }
      const data = await r.json();
      window.location.href = data.redirect_to;
    } catch (e: any) {
      setError(`Authorization error: ${e?.message || e}`);
      setSubmitting(false);
    }
  }

  function handleDeny() {
    redirectWithError(redirectUri, "access_denied", state);
  }

  // ── Render frames ──

  // — Error: missing/invalid params (no redirect possible)
  if (paramsError) {
    return (
      <Frame>
        <h1 className="text-xl font-semibold mb-2">Invalid sign-in request</h1>
        <p className="text-sm text-stone-600">{paramsError}</p>
      </Frame>
    );
  }

  // — Loading + auto-retry
  if (loading || (!paramsError && !accountReady)) {
    return (
      <Frame>
        <div className="flex flex-col items-center py-6">
          <div className="w-10 h-10 rounded-full border-2 border-stone-200 border-t-manor-600 animate-spin mb-4" />
          <p className="text-sm text-stone-600">
            {retryCount > 0 ? `Connecting to Manor… (retry ${retryCount}/3)` : "Loading…"}
          </p>
        </div>
      </Frame>
    );
  }

  // — Loaded but errored
  if (error) {
    return (
      <Frame>
        <h1 className="text-xl font-semibold mb-2">We hit a snag</h1>
        <p className="text-sm text-stone-600 break-words mb-6">{error}</p>
        <div className="flex gap-3">
          <button
            type="button"
            className="flex-1 px-4 py-2.5 rounded-lg border border-stone-300 hover:bg-stone-50 text-sm font-medium text-stone-700"
            onClick={() => {
              setRetryCount(0);
              setAccountAttempt((attempt) => attempt + 1);
              setError("");
              setLoading(true);
            }}
          >
            Try again
          </button>
          <button
            type="button"
            className="flex-1 px-4 py-2.5 rounded-lg bg-stone-900 text-white hover:bg-stone-800 text-sm font-medium"
            onClick={() => redirectWithError(redirectUri, "server_error", state)}
          >
            Back to {info?.name ? info.name.split(" ")[0] : "app"}
          </button>
        </div>
      </Frame>
    );
  }

  // — Loaded OK: consent screen
  const userName = user?.display_name || user?.first_name || user?.email || "your account";
  const userEmail = user?.email || "";
  const appHost = safeDomain(redirectUri);
  const { title: appTitle, qualifier: appQualifier } = splitClientName(info?.name || "");
  const appBrand = appBrandKey(appTitle, info?.verified !== false);

  return (
    <Frame>
      {/* Header: official Manor mark + the app's official mark */}
      <div className="flex items-center justify-center gap-3 mb-6">
        <LogoTile dark>
          <IconManorLogo size={26} />
        </LogoTile>
        <div className="flex gap-1" aria-hidden="true">
          <span className="w-1 h-1 rounded-full bg-stone-300" />
          <span className="w-1 h-1 rounded-full bg-stone-300" />
          <span className="w-1 h-1 rounded-full bg-stone-300" />
        </div>
        <LogoTile>
          {appBrand ? <IntegrationLogo provider={appBrand} size={26} /> : initialsOf(appTitle)}
        </LogoTile>
      </div>

      <h1 className="text-xl font-semibold text-center leading-tight">
        Sign in to {appTitle}
      </h1>
      <p className="text-sm text-stone-500 text-center mt-1 mb-6">
        {appQualifier ? <>{appQualifier} · </> : null}with your Manor account
      </p>

      {/* Signed-in account */}
      {user && (
        <div className="flex items-center gap-3 px-3 py-2.5 mb-5 rounded-xl bg-stone-50">
          <div className="w-8 h-8 rounded-full bg-manor-600 text-white flex items-center justify-center font-medium text-xs select-none shrink-0">
            {initialsOf(userName, userEmail)}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-stone-900 truncate">{userName}</div>
            {userEmail && userEmail !== userName && (
              <div className="text-xs text-stone-500 truncate">{userEmail}</div>
            )}
          </div>
        </div>
      )}

      {info?.description && (
        <p className="text-sm text-stone-600 mb-4">{info.description}</p>
      )}

      {info?.require_verified_email && (
        <CurrentEmailVerification key={`${clientId}:${user?.id}:${user?.email}`} onVerified={setEmailVerified} />
      )}

      {/* Permissions */}
      <ul className="space-y-2 mb-5">
        <li className="flex items-start gap-2.5">
          <CheckIcon />
          <span className="text-sm text-stone-700">See your name and email address</span>
        </li>
        <li className="flex items-start gap-2.5">
          <CheckIcon />
          <span className="text-sm text-stone-700">
            {info?.verified === false
              ? "Build and change Workspaces as you"
              : `Act on your behalf within ${appTitle}`}
          </span>
        </li>
      </ul>

      {info?.verified === false && (
        <p className="flex items-start gap-2 mb-5 text-xs leading-relaxed text-stone-500">
          <span aria-hidden className="mt-1.5 w-1.5 h-1.5 rounded-full shrink-0" style={{ background: "#b27c34" }} />
          <span>
            <span className="font-medium text-stone-700">Unverified app.</span> Manor can't confirm
            “{info.name}”. Continue only if you just started this sign-in from a tool you trust on
            this computer.
          </span>
        </p>
      )}

      <div className="flex gap-3">
        <Button variant="outline" className="flex-1" disabled={submitting} onClick={handleDeny}>
          Cancel
        </Button>
        <Button
          className="flex-1"
          loading={submitting}
          disabled={!!(info?.require_verified_email && !emailVerified)}
          onClick={handleApprove}
        >
          {submitting ? "Signing in…" : "Continue"}
        </Button>
      </div>

      <p className="mt-4 text-center text-xs text-stone-400">
        You'll return to <span className="text-stone-500">{appHost}</span> after signing in.
      </p>
    </Frame>
  );
}
