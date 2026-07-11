/**
 * OAuth 2.0 Authorize consent page (Manor as IdP).
 *
 * Visually modelled on Google's OAuth consent screen:
 *   - Clean white card on a soft background
 *   - Manor logomark + app name as the title
 *   - "Continuing as {user}" chip
 *   - Bulleted permission list with monochrome icons
 *   - Cancel / Continue at the bottom
 *
 * Flow:
 *   1. Client app (e.g. PMS) redirects user here with client_id+redirect_uri+state.
 *   2. If unauthenticated → bounce to /login?next=... and come back.
 *   3. Fetch client metadata (public endpoint).
 *   4. Show consent. On Continue → POST /authorize → set window.location to the code URL.
 *      On Cancel → redirect back to client with ?error=access_denied.
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuthStore } from "../stores/auth";

interface ClientInfo {
  client_id: string;
  name: string;
  description?: string | null;
  redirect_uris: string[];
  allowed_scopes: string[];
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "";

function buildBackUrl(): string {
  return window.location.pathname + window.location.search;
}

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

function ShieldIcon() {
  return (
    <svg className="w-5 h-5 text-stone-500 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path fillRule="evenodd" d="M10 1.5l7 3v5c0 4.2-2.93 7.83-7 9-4.07-1.17-7-4.8-7-9v-5l7-3z" clipRule="evenodd" />
    </svg>
  );
}

function ArrowSwap() {
  return (
    <svg className="w-6 h-6 text-stone-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M7 7h10M7 7l3-3M7 7l3 3M17 17H7M17 17l-3-3M17 17l-3 3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ManorMark() {
  // A simple "M" badge — matches the brand colour without needing an asset
  return (
    <div className="w-10 h-10 rounded-lg bg-manor-600 text-white flex items-center justify-center font-semibold text-lg select-none shrink-0">
      M
    </div>
  );
}

// ── Page ──

export default function OAuthAuthorize() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { user } = useAuthStore();

  const clientId = searchParams.get("client_id") || "";
  const redirectUri = searchParams.get("redirect_uri") || "";
  const state = searchParams.get("state");
  const scope = searchParams.get("scope") || "";
  const responseType = searchParams.get("response_type") || "code";

  const [info, setInfo] = useState<ClientInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [retryCount, setRetryCount] = useState(0);

  // Validate required params up front
  const paramsError = useMemo(() => {
    if (!clientId) return "Missing client_id";
    if (!redirectUri) return "Missing redirect_uri";
    if (responseType !== "code") return "Only response_type=code is supported";
    return "";
  }, [clientId, redirectUri, responseType]);

  // If unauthenticated, bounce to login and come back
  useEffect(() => {
    if (paramsError) return;
    const token = localStorage.getItem("manor_token");
    if (!token) {
      const next = encodeURIComponent(buildBackUrl());
      navigate(`/login?next=${next}`, { replace: true });
    }
  }, [paramsError, navigate]);

  // Load client info (with auto-retry on transient 502s during deploy)
  useEffect(() => {
    if (paramsError) {
      setLoading(false);
      setError(paramsError);
      return;
    }
    const token = localStorage.getItem("manor_token");
    if (!token) return; // wait for redirect
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const r = await fetch(
          `${API_BASE}/api/v1/oauth/clients/${encodeURIComponent(clientId)}`,
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
        if (!data.redirect_uris.includes(redirectUri)) {
          setError(
            `The redirect URL "${safeDomain(redirectUri)}" is not registered for this app.`
          );
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
  }, [clientId, redirectUri, paramsError, retryCount]);

  async function handleApprove() {
    setSubmitting(true);
    setError("");
    const token = localStorage.getItem("manor_token");
    if (!token) {
      const next = encodeURIComponent(buildBackUrl());
      navigate(`/login?next=${next}`, { replace: true });
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

  const Frame = ({ children }: { children: React.ReactNode }) => (
    <div className="min-h-screen flex items-center justify-center bg-stone-50 p-4 sm:p-6">
      <div className="w-full max-w-md">
        <div className="text-center mb-6 text-xs text-stone-400 select-none">
          MANOR · IDENTITY
        </div>
        <div className="bg-white rounded-2xl shadow-[0_2px_24px_-8px_rgba(28,25,23,0.12)] ring-1 ring-stone-200 p-7 sm:p-9 text-stone-900">
          {children}
        </div>
      </div>
    </div>
  );

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
  if (loading) {
    return (
      <Frame>
        <div className="flex flex-col items-center py-6">
          <div className="w-10 h-10 rounded-full border-2 border-stone-200 border-t-teal-600 animate-spin mb-4" />
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

  return (
    <Frame>
      {/* Header: Manor mark + app icon side-by-side */}
      <div className="flex items-center justify-center gap-4 mb-6">
        <ManorMark />
        <ArrowSwap />
        <div className="w-10 h-10 rounded-lg bg-stone-100 text-stone-700 flex items-center justify-center font-semibold text-sm select-none shrink-0 ring-1 ring-stone-200">
          {initialsOf(info?.name)}
        </div>
      </div>

      <h1 className="text-2xl font-semibold text-center mb-1 leading-tight">
        Sign in to {info?.name}
      </h1>
      <p className="text-sm text-stone-500 text-center mb-6">
        with your Manor account
      </p>

      {/* User account chip */}
      {user && (
        <div className="flex items-center gap-3 p-3 mb-5 rounded-xl bg-stone-50 ring-1 ring-stone-200">
          <div className="w-9 h-9 rounded-full bg-manor-600 text-white flex items-center justify-center font-medium text-xs select-none shrink-0">
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

      {/* Description */}
      {info?.description && (
        <p className="text-sm text-stone-600 mb-4">{info.description}</p>
      )}

      {/* Permissions list */}
      <p className="text-xs font-medium uppercase tracking-wide text-stone-500 mb-2">
        This will allow {info?.name} to
      </p>
      <ul className="space-y-2 mb-5">
        <li className="flex items-start gap-2.5">
          <CheckIcon />
          <span className="text-sm text-stone-700">
            See your name and email address
          </span>
        </li>
        <li className="flex items-start gap-2.5">
          <CheckIcon />
          <span className="text-sm text-stone-700">
            Act on your behalf within {info?.name}
          </span>
        </li>
      </ul>

      {/* Security note */}
      <div className="flex items-start gap-2 p-3 mb-6 rounded-lg bg-stone-50 text-xs text-stone-600">
        <ShieldIcon />
        <span>
          You'll return to <span className="font-medium text-stone-900">{appHost}</span> after signing in. Manor never shares your password.
        </span>
      </div>

      {/* Action buttons */}
      <div className="flex gap-3">
        <button
          type="button"
          disabled={submitting}
          className="flex-1 px-4 py-2.5 rounded-lg border border-stone-300 hover:bg-stone-50 text-sm font-medium text-stone-700 disabled:opacity-50 transition"
          onClick={handleDeny}
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={submitting}
          className="flex-1 px-4 py-2.5 rounded-lg bg-manor-600 text-white hover:bg-manor-500 text-sm font-semibold disabled:opacity-50 transition"
          onClick={handleApprove}
        >
          {submitting ? "Signing in…" : "Continue"}
        </button>
      </div>
    </Frame>
  );
}
