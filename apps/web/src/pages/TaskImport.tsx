import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { receiveTaskDraftHandoff, taskDraftOriginAllowed } from "../lib/taskDraftHandoff";

/** Public receiver: transfer drafts before sign-in without putting meeting text
 * or credentials in URLs, server logs, or the login return path. */
export default function TaskImport() {
  const navigate = useNavigate();
  const [error, setError] = useState("");
  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const nonce = params.get("nonce") || "";
    const source = params.get("origin") || "";
    const opener = window.opener;
    if (!opener || !/^[a-f0-9-]{36}$/.test(nonce) || !taskDraftOriginAllowed(source, window.location.origin)) {
      setError("Open this page from a connected app to prepare a task.");
      return;
    }
    let received = false;
    const onMessage = (event: MessageEvent) => {
      if (received || event.origin !== source) return;
      try {
        if (!receiveTaskDraftHandoff(event, opener, nonce, window.location.origin, sessionStorage)) return;
        received = true;
        opener.postMessage({ type: "manor:task-drafts:received", nonce }, source);
        window.opener = null;
        navigate("/tasks?importDrafts=1", { replace: true });
      } catch {
        setError("These task drafts could not be received. Return to the app and try again.");
      }
    };
    const ready = () => { if (!received) opener.postMessage({ type: "manor:task-drafts:ready", nonce }, source); };
    window.addEventListener("message", onMessage);
    ready();
    const interval = window.setInterval(ready, 500);
    const timeout = window.setTimeout(() => {
      window.clearInterval(interval);
      if (!received) setError("The transfer expired. Return to the app and try again.");
    }, 30000);
    return () => { window.removeEventListener("message", onMessage); window.clearInterval(interval); window.clearTimeout(timeout); };
  }, [navigate]);
  return <main className="min-h-screen flex items-center justify-center p-6">
    <section className="max-w-md space-y-4">
      <h1 className="text-xl font-semibold">Create tasks in Manor</h1>
      <p role={error ? "alert" : "status"}>{error || "Receiving your drafts. You’ll review the details before creating each task."}</p>
      {error && <a href="/tasks" className="underline">Open tasks</a>}
    </section>
  </main>;
}
