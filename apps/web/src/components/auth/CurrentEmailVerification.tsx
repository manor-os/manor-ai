import { useEffect, useRef, useState } from "react";
import Button from "../ui/Button";
import Input from "../ui/Input";
import { isSameAuthIdentity } from "../../lib/authToken";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "";
const endpoint = `${API_BASE}/api/v1/auth/me/email-verification`;
const REQUEST_TIMEOUT_MS = 30_000;
const timeoutMessage = "The request timed out. Check verification status before trying again.";

function failure(status: number): string {
  if (status === 429) return "Too many attempts. Wait a few minutes before trying again.";
  if (status === 401) return "Your sign-in has changed. Sign in again from the app.";
  if (status >= 500) return "Email verification is temporarily unavailable. Please try again.";
  return "The code is invalid or expired. Check the code or request a new one.";
}

export default function CurrentEmailVerification({ onVerified }: {
  onVerified: (verified: boolean) => void;
}) {
  const [email, setEmail] = useState("");
  const [verified, setVerified] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [signInChanged, setSignInChanged] = useState(false);
  const generation = useRef(0);
  const pendingPost = useRef<AbortController | null>(null);

  function recoverChangedToken(previousToken: string) {
    setLoaded(false);
    setBusy(false);
    setVerified(false);
    setEmail("");
    setSent(false);
    setCode("");
    onVerified(false);
    const nextToken = localStorage.getItem("manor_token") || "";
    if (nextToken && isSameAuthIdentity(previousToken, nextToken)) {
      // Do not reuse the old response, even for a refresh of the same account.
      setRetry((value) => value + 1);
    } else {
      setSignInChanged(true);
      setError("Your sign-in has changed. Reload this sign-in request.");
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    generation.current += 1;
    const currentGeneration = generation.current;
    const token = localStorage.getItem("manor_token") || "";
    const current = () => generation.current === currentGeneration;
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    setLoaded(false);
    setBusy(false);
    setVerified(false);
    setSignInChanged(false);
    setEmail("");
    setSent(false);
    setCode("");
    setError("");
    onVerified(false);
    void (async () => {
      try {
        const response = await fetch(endpoint, {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error(failure(response.status));
        const result = await response.json();
        if (typeof result.email !== "string" || typeof result.email_verified !== "boolean") {
          throw new Error("Email verification is temporarily unavailable. Please try again.");
        }
        if (!current()) return;
        if (controller.signal.aborted) throw new Error(timeoutMessage);
        if ((localStorage.getItem("manor_token") || "") !== token) {
          recoverChangedToken(token);
          return;
        }
        setEmail(result.email);
        setVerified(result.email_verified);
        onVerified(result.email_verified);
        setLoaded(true);
      } catch (cause) {
        if (!current()) return;
        if ((localStorage.getItem("manor_token") || "") !== token) recoverChangedToken(token);
        else setError(controller.signal.aborted ? timeoutMessage : cause instanceof Error ? cause.message : failure(503));
      } finally {
        window.clearTimeout(timer);
      }
    })();
    return () => {
      generation.current += 1;
      window.clearTimeout(timer);
      controller.abort();
      pendingPost.current?.abort();
    };
  }, [onVerified, retry]);

  async function submit(action: "send" | "confirm") {
    if (busy || !loaded || verified) return;
    setBusy(true);
    setError("");
    const currentGeneration = generation.current;
    const token = localStorage.getItem("manor_token") || "";
    const current = () => generation.current === currentGeneration;
    const controller = new AbortController();
    pendingPost.current = controller;
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`${endpoint}/${action}`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        ...(action === "confirm" ? { body: JSON.stringify({ code }) } : {}),
      });
      if (!response.ok) throw new Error(failure(response.status));
      const result = await response.json();
      if (!current()) return;
      if (controller.signal.aborted) throw new Error(timeoutMessage);
      if ((localStorage.getItem("manor_token") || "") !== token) {
        recoverChangedToken(token);
        return;
      }
      if (result.email !== email || typeof result.email_verified !== "boolean") {
        throw new Error("Your email has changed. Reload this sign-in request.");
      }
      if (action === "confirm" && result.email_verified !== true) throw new Error(failure(400));
      setVerified(result.email_verified);
      onVerified(result.email_verified);
      setSent(action === "send" && result.sent === true);
      setCode("");
    } catch (cause) {
      if (!current()) return;
      if ((localStorage.getItem("manor_token") || "") !== token) recoverChangedToken(token);
      else if (controller.signal.aborted) {
        // The server may already have consumed the code. Recovery only GETs status.
        setLoaded(false);
        setCode("");
        setError(timeoutMessage);
      } else setError(cause instanceof Error ? cause.message : failure(503));
    } finally {
      window.clearTimeout(timer);
      if (pendingPost.current === controller) pendingPost.current = null;
      // Request ownership, not token equality, controls releasing the spinner.
      if (current()) setBusy(false);
    }
  }

  return (
    <section className="mb-5 rounded-xl bg-stone-50 p-4" aria-label="Email verification">
      <h2 className="text-sm font-semibold text-stone-900">
        {verified ? "Email verified" : "Verify your email to continue"}
      </h2>
      {email && <p className="mt-1 text-sm text-stone-600 break-words">{email}</p>}
      {!loaded && !error && <p role="status" className="mt-2 text-sm text-stone-600">Checking verification…</p>}
      {error && <p role="alert" className="mt-2 text-sm text-stone-700">{error}</p>}
      {!loaded && error && (
        signInChanged
          ? <Button variant="outline" className="mt-3" onClick={() => window.location.reload()}>Reload sign-in request</Button>
          : <Button variant="outline" className="mt-3" onClick={() => setRetry((value) => value + 1)}>Retry verification check</Button>
      )}
      {loaded && !verified && (
        <form className="mt-3 space-y-3" onSubmit={(event) => { event.preventDefault(); void submit("confirm"); }}>
          <p className="text-xs text-stone-600">This app requires proof that you can receive email at this address.</p>
          <Button variant="outline" disabled={busy} onClick={() => void submit("send")}>
            {sent ? "Resend code" : "Send verification code"}
          </Button>
          {sent && <p role="status" className="text-xs text-stone-600">Code sent. It expires in 10 minutes.</p>}
          <Input label="Verification code" hint="Enter the 8-character code from your email."
            value={code} onChange={(event) => setCode(event.target.value.toUpperCase())}
            maxLength={8} autoComplete="one-time-code" disabled={busy} required />
          <Button type="submit" loading={busy} disabled={code.trim().length !== 8}>Verify email</Button>
        </form>
      )}
    </section>
  );
}
