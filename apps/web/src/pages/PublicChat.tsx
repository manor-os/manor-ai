import { useState, useEffect, useRef, useCallback } from "react";
import { useParams } from "react-router-dom";

import ChatMarkdown from "../components/ChatMarkdown";
import AgentAvatar from "../components/ui/AgentAvatar";
import { api, ApiError } from "../lib/api";
import { tForLocale } from "../lib/i18n";
const API_BASE = "/api/v1/public/chat";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string | null;
  local_status?: "pending" | "notice";
  attachments?: { name?: string; filename?: string; original_name?: string }[] | null;
}

interface ChatInfo {
  channel_name: string;
  workspace_name: string | null;
  agent_name: string | null;
  agent_avatar: string | null;
  language?: string | null;
  welcome_message: string | null;
  purpose: string | null;
  login_required?: boolean;
  login_url?: string | null;
  signup_url?: string | null;
  auth_hint?: string | null;
}

type CustomerAuthMode = "login" | "register" | "verify";

interface PublicChatUser {
  email?: string | null;
  display_name?: string | null;
  first_name?: string | null;
  last_name?: string | null;
}

function authHeaders(): HeadersInit {
  let token: string | null = null;
  try {
    token = localStorage.getItem("manor_token");
  } catch {
    token = null;
  }
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

function authBearerHeaders(): HeadersInit {
  let token: string | null = null;
  try {
    token = localStorage.getItem("manor_token");
  } catch {
    token = null;
  }
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function publicMessageContentMatches(localContent: string, serverContent: string): boolean {
  const normalize = (value: string) =>
    value
      .replace(/\n\n\[Attached:[\s\S]*?\]$/i, "")
      .replace(/\n\[Image:[\s\S]*?\]$/i, "")
      .trim();
  const local = normalize(localContent || "");
  const server = normalize(serverContent || "");
  if (local === server) return true;
  if (local && server && (local.startsWith(server) || server.startsWith(local))) return true;
  return local.startsWith("[Attached:") && server.startsWith("Attached file");
}

const ASSISTANT_STREAM_STARTED_CONTENT =
  "The assistant started this response and is still working. If this remains after a reload, the stream was interrupted before it could finish.";

function hasAssistantStreamPlaceholder(content: string): boolean {
  return (content || "").startsWith(ASSISTANT_STREAM_STARTED_CONTENT);
}

function removeAssistantStreamPlaceholder(content: string): string {
  if (!hasAssistantStreamPlaceholder(content)) return content || "";
  return (content || "").slice(ASSISTANT_STREAM_STARTED_CONTENT.length).replace(/^\s+/, "");
}

function displayNameForUser(user: PublicChatUser | null): string {
  if (!user) return "";
  const fullName = [user.first_name, user.last_name].filter(Boolean).join(" ").trim();
  return user.display_name || fullName || user.email?.split("@")[0] || "";
}

const PUBLIC_CHAT_SESSION_PREFIX = "manor_public_chat_session:";

function publicChatSessionKey(token: string): string {
  return `${PUBLIC_CHAT_SESSION_PREFIX}${token}`;
}

function getStoredSessionId(token?: string | null): string | null {
  if (!token) return null;
  try {
    return localStorage.getItem(publicChatSessionKey(token));
  } catch {
    return null;
  }
}

function saveStoredSessionId(token: string, sessionId: string) {
  try {
    localStorage.setItem(publicChatSessionKey(token), sessionId);
  } catch {
    // Browser storage can be disabled; chat still works for the current tab.
  }
}

function forgetStoredSessionId(token?: string | null) {
  if (!token) return;
  try {
    localStorage.removeItem(publicChatSessionKey(token));
  } catch {
    // Ignore storage failures.
  }
}

export default function PublicChat() {
  const { token } = useParams<{ token: string }>();
  const [info, setInfo] = useState<ChatInfo | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visitorName, setVisitorName] = useState("");
  const [started, setStarted] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [currentUser, setCurrentUser] = useState<PublicChatUser | null>(null);
  const [customerAuthMode, setCustomerAuthMode] = useState<CustomerAuthMode>("login");
  const [customerEmail, setCustomerEmail] = useState("");
  const [customerPassword, setCustomerPassword] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [customerVerificationCode, setCustomerVerificationCode] = useState("");
  const [customerPendingEmail, setCustomerPendingEmail] = useState("");
  const [customerAuthError, setCustomerAuthError] = useState("");
  const [customerAuthLoading, setCustomerAuthLoading] = useState(false);
  const [customerGoogleLoading, setCustomerGoogleLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastServerMessageIdRef = useRef("");
  const lastActivityAtRef = useRef(Date.now());
  const sendingRef = useRef(false);
  const resumeAttemptedForTokenRef = useRef<string | null>(null);
  const isEmbedded = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("embed") === "1";
  const shellStyle = isEmbedded ? styles.embedContainer : styles.container;
  const cardStyle = isEmbedded ? { ...styles.card, ...styles.embedCard } : styles.card;
  const chatCardStyle = isEmbedded
    ? { ...styles.card, ...styles.embedCard, display: "flex", flexDirection: "column" as const }
    : { ...styles.card, display: "flex", flexDirection: "column" as const, height: "min(600px, 85vh)" };
  const brandingStyle = isEmbedded ? styles.embedBranding : styles.branding;
  const customerText = useCallback(
    (key: string, vars?: Record<string, string | number>) => tForLocale(key, info?.language || "en", vars),
    [info?.language],
  );

  // Fetch chat info
  useEffect(() => {
    if (!token) return;
    fetch(`${API_BASE}/${token}`)
      .then((r) => {
        if (!r.ok) throw new Error("Chat not found");
        return r.json();
      })
      .then(setInfo)
      .catch(() => setError(tForLocale("page.public_chat.this_chat_link_is_invalid_or_has_expired", "en")));
  }, [token]);

  useEffect(() => {
    let tokenValue: string | null = null;
    try {
      tokenValue = localStorage.getItem("manor_token");
    } catch {
      tokenValue = null;
    }
    if (!tokenValue) {
      setCurrentUser(null);
      setAuthChecked(true);
      return;
    }
    let cancelled = false;
    fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${tokenValue}` },
      cache: "no-store",
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((user) => {
        if (cancelled) return;
        setCurrentUser(user || null);
        setAuthChecked(true);
      })
      .catch(() => {
        if (cancelled) return;
        setCurrentUser(null);
        setAuthChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Start session
  const startChat = useCallback(async (userOverride?: PublicChatUser | null) => {
    if (!token) return;
    const effectiveUser = userOverride ?? currentUser;
    const storedSessionId = getStoredSessionId(token);
    const effectiveName = info?.login_required
      ? displayNameForUser(effectiveUser)
      : visitorName || undefined;
    const sessionPayload = {
      session_id: storedSessionId || undefined,
      visitor_name: effectiveName,
      visitor_email: info?.login_required ? effectiveUser?.email || undefined : undefined,
    };
    let res = await fetch(`${API_BASE}/${token}/session`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(sessionPayload),
    });
    if (res.status === 401) {
      setCurrentUser(null);
      return;
    }
    if (res.status === 403 && storedSessionId) {
      // Same browser, different signed-in customer: do not expose the old chat.
      forgetStoredSessionId(token);
      res = await fetch(`${API_BASE}/${token}/session`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ ...sessionPayload, session_id: undefined }),
      });
    }
    if (!res.ok) { setError(customerText("page.public_chat.failed_to_start_chat")); return; }
    const data = await res.json();
    setSessionId(data.session_id);
    saveStoredSessionId(token, data.session_id);
    setStarted(true);
  }, [token, info?.login_required, currentUser, visitorName, customerText]);

  useEffect(() => {
    if (!token || !info || started || sessionId) return;
    if (!getStoredSessionId(token)) return;
    if (resumeAttemptedForTokenRef.current === token) return;
    if (info.login_required && (!authChecked || !currentUser)) return;

    resumeAttemptedForTokenRef.current = token;
    startChat();
  }, [token, info, started, sessionId, authChecked, currentUser, startChat]);

  useEffect(() => {
    if (!token || !info?.login_required || !currentUser || started || sessionId) return;
    const key = `manor_public_chat_autostart:${token}`;
    if (sessionStorage.getItem(key) !== "1") return;
    sessionStorage.removeItem(key);
    startChat(currentUser);
  }, [token, info?.login_required, currentUser, started, sessionId, startChat]);

  useEffect(() => {
    sendingRef.current = sending;
  }, [sending]);

  // Poll for messages
  useEffect(() => {
    if (!token || !sessionId) return;
    let active = true;
    lastServerMessageIdRef.current = "";
    lastActivityAtRef.current = Date.now();
    const nextDelay = () => {
      if (document.visibilityState === "hidden") return 15_000;
      return Date.now() - lastActivityAtRef.current < 60_000 ? 2_000 : 8_000;
    };
    const poll = async () => {
      try {
        const lastId = lastServerMessageIdRef.current;
        const res = await fetch(
          `${API_BASE}/${token}/messages?session_id=${sessionId}&after=${lastId}`,
          { headers: authHeaders(), cache: "no-store" }
        );
        if (res.status === 401) {
          setCurrentUser(null);
          setStarted(false);
          return;
        }
        if (res.status === 403) {
          forgetStoredSessionId(token);
          setSessionId(null);
          setStarted(false);
          return;
        }
        if (!res.ok) return;
        const data = await res.json();
        if (data.messages && data.messages.length > 0) {
          const serverMessages = (data.messages as ChatMessage[]).flatMap((msg) => {
            if (sendingRef.current && msg.role === "assistant") return [];
            if (msg.role !== "assistant" || !hasAssistantStreamPlaceholder(msg.content || "")) return msg;
            const strippedContent = removeAssistantStreamPlaceholder(msg.content || "");
            if (strippedContent) return { ...msg, content: strippedContent };
            return msg;
          });
          if (serverMessages.length === 0) return;
          const hasServerAssistant = serverMessages.some((msg) => msg.role === "assistant");
          setMessages((prev) => {
            let changed = false;
            const next = hasServerAssistant
              ? prev.filter((m) => !(m.id.startsWith("tmp-assistant-") && m.local_status))
              : [...prev];
            if (next.length !== prev.length) changed = true;
            const ids = new Set(next.map((m) => m.id));
            const newMsgs: ChatMessage[] = [];
            let newestServerMsg: ChatMessage | null = null;
            for (const msg of serverMessages) {
              if (ids.has(msg.id)) continue;
              const optimisticIndex = next.findIndex((m) =>
                m.id.startsWith("tmp-") &&
                m.role === msg.role &&
                publicMessageContentMatches(m.content, msg.content)
              );
              if (optimisticIndex >= 0) {
                next[optimisticIndex] = msg;
                ids.add(msg.id);
                changed = true;
                newestServerMsg = msg;
              } else {
                newMsgs.push(msg);
                ids.add(msg.id);
                newestServerMsg = msg;
              }
            }
            if (!changed && newMsgs.length === 0) return prev;
            lastActivityAtRef.current = Date.now();
            const lastServer = newestServerMsg || [...newMsgs].reverse().find((m) => !m.id.startsWith("tmp-"));
            if (lastServer) lastServerMessageIdRef.current = lastServer.id;
            return [...next, ...newMsgs];
          });
        }
      } finally {
        if (active) pollRef.current = setTimeout(poll, nextDelay());
      }
    };
    poll();
    return () => {
      active = false;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [token, sessionId]);

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (files.length) {
      setAttachedFiles((prev) => [...prev, ...files]);
    }
    e.target.value = "";
  };

  const removeAttachedFile = (index: number) => {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  // Send message with SSE streaming
  const send = async () => {
    if ((!input.trim() && attachedFiles.length === 0) || !token || !sessionId || sending) return;
    const text = input.trim();
    const filesToSend = [...attachedFiles];
    const displayContent = text || (filesToSend.length ? `[Attached: ${filesToSend.map((file) => file.name).join(", ")}]` : "");
    setInput("");
    setAttachedFiles([]);
    setSending(true);

    // Optimistic add
    const tempStamp = Date.now();
    const tempId = `tmp-user-${tempStamp}`;
    const assistantTempId = `tmp-assistant-${tempStamp}`;
    lastActivityAtRef.current = Date.now();
    setMessages((prev) => [
      ...prev,
      {
        id: tempId,
        role: "user",
        content: displayContent || text,
        created_at: null,
        attachments: filesToSend.map((file) => ({ name: file.name })),
      },
      { id: assistantTempId, role: "assistant", content: "", created_at: null, local_status: "pending" },
    ]);

    let assistantMessageId = assistantTempId;
    const updateLocalAssistant = (
      content: string,
      localStatus?: ChatMessage["local_status"],
      nextId?: string,
      replace = true,
    ) => {
      lastActivityAtRef.current = Date.now();
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === assistantTempId || msg.id === assistantMessageId || (nextId && msg.id === nextId)
            ? {
                ...msg,
                id: nextId || msg.id,
                content: replace ? content : `${removeAssistantStreamPlaceholder(msg.content || "")}${content}`,
                local_status: localStatus,
              }
            : msg
        )
      );
    };

    try {
      const form = new FormData();
      form.append("session_id", sessionId);
      form.append("message", text);
      filesToSend.forEach((file) => form.append("files", file));
      const res = await fetch(`${API_BASE}/${token}/message/stream`, {
        method: "POST",
        headers: authBearerHeaders(),
        body: form,
      });
      if (res.status === 401) {
        updateLocalAssistant("", "notice");
        setCurrentUser(null);
        setStarted(false);
        return;
      }
      if (res.status === 403) {
        updateLocalAssistant("", "notice");
        forgetStoredSessionId(token);
        setSessionId(null);
        setStarted(false);
        return;
      }
      if (!res.ok || !res.body) {
        updateLocalAssistant(customerText("page.public_chat.send_failed"), "notice");
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let currentEvent = "";
      let receivedText = false;

      const applyAssistantId = (messageId?: string) => {
        if (!messageId || messageId === assistantMessageId) return;
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMessageId ? { ...msg, id: messageId } : msg
          )
        );
        assistantMessageId = messageId;
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("event: ")) {
            currentEvent = line.slice(7).trim();
            continue;
          }
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6).trim();
          if (!raw || raw === "[DONE]") continue;
          let parsed: any;
          try {
            parsed = JSON.parse(raw);
          } catch {
            continue;
          }

          if (parsed.message_id) applyAssistantId(String(parsed.message_id));
          if (currentEvent === "text_reset") {
            updateLocalAssistant("", "pending", assistantMessageId);
            continue;
          }
          if (currentEvent === "error") {
            updateLocalAssistant(
              parsed.message || parsed.error || customerText("page.public_chat.send_failed"),
              "notice",
              parsed.message_id ? String(parsed.message_id) : assistantMessageId,
            );
            continue;
          }
          const delta = parsed.text_delta ?? parsed.token ?? parsed.content;
          if (typeof delta === "string" && delta) {
            receivedText = true;
            const deltaStatus = typeof parsed.status === "string" ? parsed.status : "";
            const isNoticeDelta = ["approval_required", "blocked_by_governance", "no_reply", "error"].includes(deltaStatus);
            updateLocalAssistant(delta, isNoticeDelta ? "notice" : undefined, assistantMessageId, false);
          }
          if (currentEvent === "stream_end") {
            const streamStatus = typeof parsed.status === "string" ? parsed.status : "";
            const noticeStatus = ["approval_required", "blocked_by_governance", "no_reply", "error"].includes(streamStatus);
            updateLocalAssistant(
              receivedText ? "" : customerText("page.public_chat.no_immediate_reply"),
              noticeStatus || !receivedText ? "notice" : undefined,
              parsed.message_id ? String(parsed.message_id) : assistantMessageId,
              false,
            );
          }
        }
      }
    } catch {
      updateLocalAssistant(customerText("page.public_chat.send_failed"), "notice");
    } finally {
      setSending(false);
    }
  };

  const finishCustomerAuth = async (accessToken: string) => {
    localStorage.setItem("manor_token", accessToken);
    const user = await api.auth.me();
    setCurrentUser(user);
    setAuthChecked(true);
    setCustomerAuthError("");
    setCustomerPassword("");
    await startChat(user);
  };

  const publicAuthErrorMessage = (err: unknown) => {
    if (err instanceof ApiError) return err.message;
    return err instanceof Error ? err.message : customerText("page.public_chat.customer_auth_failed");
  };

  const handleCustomerAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token || customerAuthLoading) return;
    setCustomerAuthError("");
    setCustomerAuthLoading(true);
    try {
      const email = customerEmail.trim().toLowerCase();
      let result: any;
      if (customerAuthMode === "register") {
        const res = await fetch(`${API_BASE}/${token}/auth/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email,
            password: customerPassword,
            display_name: customerName.trim() || undefined,
          }),
        });
        result = await res.json().catch(() => null);
        if (!res.ok) {
          const detail = result?.detail;
          throw new Error(typeof detail === "string" ? detail : customerText("page.public_chat.customer_auth_failed"));
        }
      } else {
        result = await api.auth.login({
          email,
          password: customerPassword,
          remember_me: true,
        });
      }

      if (result?.requires_verification) {
        setCustomerPendingEmail(result.email || email);
        setCustomerVerificationCode("");
        setCustomerAuthMode("verify");
        return;
      }
      if (result?.requires_2fa) {
        setCustomerAuthError(customerText("page.public_chat.customer_2fa_not_supported"));
        return;
      }
      if (result?.access_token) {
        await finishCustomerAuth(result.access_token);
      } else {
        setCustomerAuthError(customerText("page.public_chat.customer_auth_failed"));
      }
    } catch (err) {
      setCustomerAuthError(publicAuthErrorMessage(err));
    } finally {
      setCustomerAuthLoading(false);
    }
  };

  const handleCustomerVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!customerPendingEmail || customerVerificationCode.length !== 6) return;
    setCustomerAuthError("");
    setCustomerAuthLoading(true);
    try {
      const result = await api.auth.verifyEmail(customerPendingEmail, customerVerificationCode);
      await finishCustomerAuth(result.access_token);
    } catch (err) {
      setCustomerAuthError(publicAuthErrorMessage(err));
    } finally {
      setCustomerAuthLoading(false);
    }
  };

  const handleCustomerGoogle = async () => {
    if (!token) return;
    setCustomerAuthError("");
    setCustomerGoogleLoading(true);
    try {
      const cfg = await api.auth.googleOAuthConfig();
      const clientId = (cfg.client_id || "").trim();
      if (!cfg.enabled || !clientId) {
        setCustomerAuthError(customerText("page.login.google_sign_in_is_not_configured_for_this_deploy"));
        setCustomerGoogleLoading(false);
        return;
      }
      const redirectUri = encodeURIComponent(window.location.origin + "/oauth/callback");
      const scope = encodeURIComponent("openid email profile");
      const state = crypto.randomUUID();
      const nextPath = `${window.location.pathname}${window.location.search}`;
      sessionStorage.setItem("oauth_state", state);
      sessionStorage.setItem("oauth_next", nextPath);
      sessionStorage.setItem("oauth_public_chat_token", token);
      sessionStorage.setItem(`manor_public_chat_autostart:${token}`, "1");
      window.location.href = `https://accounts.google.com/o/oauth2/v2/auth?client_id=${encodeURIComponent(clientId)}&redirect_uri=${redirectUri}&response_type=code&scope=${scope}&access_type=offline&prompt=consent&state=${state}`;
    } catch {
      setCustomerAuthError(customerText("page.login.could_not_start_google_sign_in_please_check_the"));
      setCustomerGoogleLoading(false);
    }
  };

  const switchCustomerAuthMode = (mode: Exclude<CustomerAuthMode, "verify">) => {
    setCustomerAuthMode(mode);
    setCustomerAuthError("");
    setCustomerVerificationCode("");
  };

  if (error) {
    return (
      <div style={styles.container}>
        <div style={styles.card}>
          <div style={styles.errorState}>
            <div style={styles.errorIcon}>
              <svg width="24" height="24" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V7.5a4.5 4.5 0 10-9 0v3m-.75 0h10.5A1.75 1.75 0 0119 12.25v6A1.75 1.75 0 0117.25 20H6.75A1.75 1.75 0 015 18.25v-6a1.75 1.75 0 011.75-1.75z" />
              </svg>
            </div>
            <h1 style={styles.errorTitle}>{customerText("page.public_chat.link_unavailable_title")}</h1>
            <p style={styles.errorCopy}>{error}</p>
            <p style={styles.errorHint}>{customerText("page.public_chat.link_unavailable_hint")}</p>
          </div>
        </div>
      </div>
    );
  }

  if (!info) {
    return (
      <div style={shellStyle}>
        <div style={cardStyle}>
          <div style={{ textAlign: "center", padding: 40, color: "#a8a29e" }}>
            {customerText("status.loading")}
          </div>
        </div>
      </div>
    );
  }

  if (info.login_required && !authChecked) {
    return (
      <div style={shellStyle}>
        <div style={cardStyle}>
          <div style={{ textAlign: "center", padding: 40, color: "#a8a29e" }}>
            {customerText("status.loading")}
          </div>
        </div>
      </div>
    );
  }

  if (info.login_required && !currentUser) {
    const isRegister = customerAuthMode === "register";
    const isVerify = customerAuthMode === "verify";
    const publicAgentSeed = `${info.channel_name}::${info.workspace_name || ""}::${info.agent_name || ""}`;
    return (
      <div style={shellStyle}>
        <div style={cardStyle}>
          <div style={{ padding: "30px 24px" }}>
            <AgentAvatar
              name={info.agent_name || info.channel_name}
              avatarUrl={info.agent_avatar}
              seed={publicAgentSeed}
              size={56}
              shape="rounded"
              style={{ margin: "0 auto 14px" }}
            />
            <div style={{ textAlign: "center" }}>
              <div style={styles.authPill}>{customerText("page.public_chat.secure_chat")}</div>
            </div>
            <h1 style={{ fontSize: 22, fontWeight: 850, color: "#1c1917", margin: "10px 0 6px", textAlign: "center" }}>
              {isVerify
                ? customerText("page.public_chat.verify_customer_email")
                : isRegister
                ? customerText("page.public_chat.create_customer_account")
                : customerText("page.public_chat.customer_sign_in")}
            </h1>
            <p style={{ fontSize: 13, color: "#78716c", margin: "0 auto 18px", lineHeight: 1.6, maxWidth: 320, textAlign: "center" }}>
              {isVerify
                ? customerText("page.public_chat.verify_customer_email_desc", { email: customerPendingEmail })
                : info.auth_hint || customerText("page.public_chat.sign_in_required_desc")}
            </p>

            {customerAuthError && (
              <div style={styles.customerAuthError}>{customerAuthError}</div>
            )}

            {isVerify ? (
              <form onSubmit={handleCustomerVerify} style={styles.customerAuthForm}>
                <input
                  type="text"
                  inputMode="numeric"
                  maxLength={6}
                  value={customerVerificationCode}
                  onChange={(e) => setCustomerVerificationCode(e.target.value.replace(/\D/g, ""))}
                  placeholder="000000"
                  style={{ ...styles.customerInput, ...styles.customerCodeInput }}
                  autoFocus
                />
                <button
                  type="submit"
                  disabled={customerAuthLoading || customerVerificationCode.length !== 6}
                  style={{
                    ...styles.startBtn,
                    opacity: customerAuthLoading || customerVerificationCode.length !== 6 ? 0.55 : 1,
                  }}
                >
                  {customerAuthLoading ? customerText("page.login.verifying") : customerText("page.login.verify_email")}
                </button>
                <button
                  type="button"
                  onClick={() => switchCustomerAuthMode("login")}
                  style={styles.customerTextButton}
                >
                  {customerText("page.login.back_to_sign_in")}
                </button>
              </form>
            ) : (
              <>
                <div style={styles.customerAuthTabs}>
                  <button
                    type="button"
                    onClick={() => switchCustomerAuthMode("login")}
                    style={{
                      ...styles.customerAuthTab,
                      ...(customerAuthMode === "login" ? styles.customerAuthTabActive : {}),
                    }}
                  >
                    {customerText("page.public_chat.sign_in_to_chat")}
                  </button>
                  <button
                    type="button"
                    onClick={() => switchCustomerAuthMode("register")}
                    style={{
                      ...styles.customerAuthTab,
                      ...(customerAuthMode === "register" ? styles.customerAuthTabActive : {}),
                    }}
                  >
                    {customerText("page.public_chat.create_customer_account")}
                  </button>
                </div>
                <form onSubmit={handleCustomerAuth} style={styles.customerAuthForm}>
                  {isRegister && (
                    <input
                      type="text"
                      value={customerName}
                      onChange={(e) => setCustomerName(e.target.value)}
                      placeholder={customerText("page.public_chat.customer_name_optional")}
                      style={styles.customerInput}
                    />
                  )}
                  <input
                    type="email"
                    required
                    value={customerEmail}
                    onChange={(e) => setCustomerEmail(e.target.value)}
                    placeholder={customerText("page.login.name_company_com")}
                    style={styles.customerInput}
                    autoComplete="email"
                  />
                  <input
                    type="password"
                    required
                    value={customerPassword}
                    onChange={(e) => setCustomerPassword(e.target.value)}
                    placeholder={customerText("page.login.password")}
                    style={styles.customerInput}
                    autoComplete={isRegister ? "new-password" : "current-password"}
                  />
                  <button
                    type="submit"
                    disabled={customerAuthLoading || !customerEmail.trim() || !customerPassword}
                    style={{
                      ...styles.startBtn,
                      opacity: customerAuthLoading || !customerEmail.trim() || !customerPassword ? 0.55 : 1,
                    }}
                  >
                    {customerAuthLoading
                      ? customerText("page.login.please_wait")
                      : isRegister
                      ? customerText("page.public_chat.create_customer_account")
                      : customerText("page.public_chat.sign_in_to_chat")}
                  </button>
                </form>
                <div style={styles.customerAuthDivider}>
                  <span>{customerText("page.login.or_continue_with")}</span>
                </div>
                <button
                  type="button"
                  onClick={handleCustomerGoogle}
                  disabled={customerGoogleLoading}
                  style={{
                    ...styles.googleAuthBtn,
                    opacity: customerGoogleLoading ? 0.65 : 1,
                  }}
                >
                  <span style={styles.googleMark}>G</span>
                  {customerGoogleLoading
                    ? customerText("page.login.connecting_to_google")
                    : customerText("page.login.sign_in_with_google")}
                </button>
              </>
            )}

            {info.workspace_name && (
              <p style={{ fontSize: 12, color: "#a8a29e", margin: "18px 0 0", textAlign: "center" }}>
                {info.workspace_name}
              </p>
            )}
          </div>
        </div>
        <div style={brandingStyle}>{customerText("page.public_chat.powered_by_manor_ai")}</div>
      </div>
    );
  }

  // Pre-chat screen: visitor enters name
  if (!started) {
    const currentUserName = displayNameForUser(currentUser);
    const publicAgentSeed = `${info.channel_name}::${info.workspace_name || ""}::${info.agent_name || ""}`;
    return (
      <div style={shellStyle}>
        <div style={cardStyle}>
          <div style={{ textAlign: "center", padding: "32px 24px" }}>
            <AgentAvatar
              name={info.agent_name || info.channel_name}
              avatarUrl={info.agent_avatar}
              seed={publicAgentSeed}
              size={56}
              shape="rounded"
              style={{ margin: "0 auto 12px" }}
            />
            <h1 style={{ fontSize: 20, fontWeight: 800, color: "#1c1917", margin: "0 0 4px" }}>
              {info.channel_name}
            </h1>
            {info.workspace_name && (
              <p style={{ fontSize: 13, color: "#78716c", margin: "0 0 8px" }}>{info.workspace_name}</p>
            )}
            {info.purpose && (
              <p style={{ fontSize: 12, color: "#a8a29e", margin: "0 0 20px", lineHeight: 1.5 }}>{info.purpose}</p>
            )}
            <div style={{ maxWidth: 280, margin: "0 auto" }}>
              {info.login_required ? (
                <div style={styles.signedInBox}>
                  <span style={{ color: "#78716c" }}>{customerText("page.public_chat.continue_as")}</span>
                  <strong style={{ color: "#1c1917" }}>{currentUserName || currentUser?.email}</strong>
                </div>
              ) : (
                <input
                  type="text"
                  placeholder={customerText("page.public_chat.your_name_optional")}
                  value={visitorName}
                  onChange={(e) => setVisitorName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && startChat()}
                  style={styles.nameInput}
                />
              )}
              <button onClick={() => startChat()} style={styles.startBtn}>
                {customerText("page.public_chat.start_chat")}
              </button>
            </div>
          </div>
        </div>
        <div style={brandingStyle}>{customerText("page.public_chat.powered_by_manor_ai")}</div>
      </div>
    );
  }

  // Chat screen
  const publicAgentSeed = `${info.channel_name}::${info.workspace_name || ""}::${info.agent_name || ""}`;
  return (
    <div style={shellStyle}>
      <div style={chatCardStyle}>
        {/* Header */}
        <div style={styles.header}>
          <AgentAvatar
            name={info.agent_name || info.channel_name}
            avatarUrl={info.agent_avatar}
            seed={publicAgentSeed}
            size={32}
            shape="rounded"
          />
          <div>
            <div style={{ fontSize: 14, fontWeight: 700, color: "#1c1917" }}>
              {info.agent_name || info.channel_name}
            </div>
            {sending ? (
              <div style={styles.replyingStatus}>
                <span className="chat-typing-dots">
                  <span />
                  <span />
                  <span />
                </span>
                <span>{customerText("component.embedded_chat.replying")}</span>
              </div>
            ) : info.workspace_name && (
              <div style={{ fontSize: 11, color: "#a8a29e" }}>{info.workspace_name}</div>
            )}
          </div>
        </div>

        {/* Messages */}
        <div style={styles.messageArea}>
          {info.welcome_message && (
            <div style={styles.systemMsg}>{info.welcome_message}</div>
          )}
          {messages.map((m) => {
            const isUser = m.role === "user";
            const isSystemNotice = m.local_status === "notice";
            const isPendingAssistant = m.role === "assistant" && m.local_status === "pending" && !m.content;
            const isStreamingAssistant = sending && m.role === "assistant" && messages[messages.length - 1]?.id === m.id;
            const visibleContent = isUser ? m.content : removeAssistantStreamPlaceholder(m.content);
            return (
              <div
                key={m.id}
                style={{
                  display: "flex",
                  flexDirection: isUser ? "row-reverse" : "row",
                  alignItems: isSystemNotice ? "center" : "flex-end",
                  justifyContent: isSystemNotice ? "center" : undefined,
                  gap: 8,
                  marginBottom: isSystemNotice ? 10 : 8,
                }}
              >
                {!isUser && !isSystemNotice && (
                  <AgentAvatar
                    name={info.agent_name || info.channel_name}
                    avatarUrl={info.agent_avatar}
                    seed={publicAgentSeed}
                    size={32}
                    shape="rounded"
                    style={styles.messageAvatar}
                  />
                )}
                <div style={{
                  ...styles.bubble,
                  ...(isSystemNotice ? styles.noticeBubble : isUser ? styles.userBubble : styles.agentBubble),
                }}>
                  {isPendingAssistant ? (
                    <span style={styles.replyingStatus}>
                      <span className="chat-typing-dots">
                        <span />
                        <span />
                        <span />
                      </span>
                      <span>{customerText("component.embedded_chat.replying")}</span>
                    </span>
                  ) : visibleContent.startsWith("[Attached:") && m.attachments?.length ? (
                    null
                  ) : (
                    <>
                      <ChatMarkdown content={visibleContent} isUser={isUser} streaming={isStreamingAssistant} />
                      {isStreamingAssistant && <span className="chat-streaming-cursor" />}
                    </>
                  )}
                  {m.attachments && m.attachments.length > 0 && (
                    <div style={styles.messageAttachmentList}>
                      {m.attachments.map((attachment, idx) => (
                        <span key={`${attachment.name || attachment.filename || idx}-${idx}`} style={styles.messageAttachmentChip}>
                          {attachment.name || attachment.original_name || attachment.filename || customerText("page.public_chat.attachment")}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div style={styles.composer}>
          {attachedFiles.length > 0 && (
            <div style={styles.attachmentTray}>
              {attachedFiles.map((file, idx) => (
                <span key={`${file.name}-${idx}`} style={styles.attachmentChip}>
                  <span style={styles.attachmentName}>{file.name}</span>
                  <button
                    type="button"
                    onClick={() => removeAttachedFile(idx)}
                    aria-label={customerText("page.public_chat.remove_attachment")}
                    title={customerText("page.public_chat.remove_attachment")}
                    style={styles.removeAttachmentBtn}
                    disabled={sending}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <div style={styles.inputBar}>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={handleFileSelect}
            style={{ display: "none" }}
          />
          <button
            type="button"
            aria-label={customerText("page.public_chat.attach_files")}
            title={customerText("page.public_chat.attach_files")}
            onClick={() => fileInputRef.current?.click()}
            disabled={sending}
            style={{
              ...styles.attachBtn,
              opacity: sending ? 0.45 : 1,
            }}
          >
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M21.44 11.05 12.25 20.24a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 1 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <input
            type="text"
            placeholder={customerText("page.public_chat.type_a_message")}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
            style={styles.chatInput}
            disabled={sending}
          />
          <button
            aria-label={customerText("page.public_chat.send_message")}
            title={customerText("page.public_chat.send_message")}
            onClick={send}
            disabled={(!input.trim() && attachedFiles.length === 0) || sending}
            style={{
              ...styles.sendBtn,
              opacity: (!input.trim() && attachedFiles.length === 0) || sending ? 0.4 : 1,
            }}
          >
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
            </svg>
          </button>
          </div>
        </div>
      </div>
      <div style={brandingStyle}>{customerText("page.public_chat.powered_by_manor_ai")}</div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    minHeight: "100vh",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    padding: 16,
    background: "linear-gradient(135deg, #f2f6f5 0%, #f4f7fa 50%, #f7f4fa 100%)",
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  },
  embedContainer: {
    minHeight: "100vh",
    height: "100vh",
    display: "flex",
    flexDirection: "column",
    padding: 0,
    background: "transparent",
    overflow: "hidden",
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  },
  card: {
    width: "100%",
    maxWidth: 420,
    background: "rgba(255,255,255,0.85)",
    backdropFilter: "blur(20px)",
    borderRadius: 20,
    border: "1px solid rgba(28,25,23,0.06)",
    boxShadow: "0 8px 32px rgba(0,0,0,0.06)",
    overflow: "hidden",
  },
  embedCard: {
    maxWidth: "none",
    height: "100%",
    borderRadius: 0,
    border: "none",
    boxShadow: "none",
    background: "#fff",
    backdropFilter: "none",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "14px 18px",
    borderBottom: "1px solid rgba(28,25,23,0.06)",
    background: "rgba(250,250,249,0.6)",
  },
  replyingStatus: {
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    color: "#78716c",
    fontSize: 11,
    lineHeight: 1.2,
  },
  messageArea: {
    flex: 1,
    overflowY: "auto" as const,
    padding: "16px 18px",
  },
  systemMsg: {
    textAlign: "center" as const,
    fontSize: 12,
    color: "#a8a29e",
    margin: "0 0 16px",
    lineHeight: 1.5,
  },
  bubble: {
    maxWidth: "80%",
    minWidth: 18,
    minHeight: 18,
    padding: "10px 14px",
    borderRadius: 16,
    fontSize: 13,
    lineHeight: 1.5,
    wordBreak: "break-word" as const,
  },
  userBubble: {
    background: "#5d7f77",
    color: "#fff",
    borderBottomRightRadius: 4,
  },
  agentBubble: {
    background: "#f5f5f4",
    color: "#1c1917",
    borderBottomLeftRadius: 4,
  },
  noticeBubble: {
    maxWidth: "86%",
    background: "#fafaf9",
    color: "#78716c",
    border: "1px solid rgba(28,25,23,0.06)",
    borderRadius: 12,
    fontSize: 12,
    textAlign: "center" as const,
    padding: "8px 12px",
  },
  messageAvatar: {
    width: 26,
    height: 26,
    borderRadius: 8,
    objectFit: "cover" as const,
    flex: "0 0 auto",
  },
  messageAvatarFallback: {
    width: 26,
    height: 26,
    borderRadius: 8,
    background: "#1c1917",
    color: "#fff",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 11,
    fontWeight: 900,
    flex: "0 0 auto",
  },
  messageAttachmentList: {
    display: "flex",
    flexWrap: "wrap" as const,
    gap: 6,
    marginTop: 8,
  },
  messageAttachmentChip: {
    display: "inline-flex",
    alignItems: "center",
    maxWidth: 190,
    padding: "4px 7px",
    borderRadius: 8,
    background: "rgba(255,255,255,0.18)",
    border: "1px solid rgba(28,25,23,0.06)",
    fontSize: 11,
    lineHeight: 1.2,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap" as const,
  },
  composer: {
    borderTop: "1px solid rgba(28,25,23,0.06)",
    background: "rgba(250,250,249,0.4)",
  },
  attachmentTray: {
    display: "flex",
    flexWrap: "wrap" as const,
    gap: 6,
    padding: "10px 14px 0",
  },
  attachmentChip: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    maxWidth: "100%",
    padding: "5px 7px 5px 9px",
    borderRadius: 9,
    background: "#f1f6f5",
    border: "1px solid #bae6fd",
    color: "#1c1917",
    fontSize: 11,
    fontWeight: 650,
  },
  attachmentName: {
    maxWidth: 220,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap" as const,
  },
  removeAttachmentBtn: {
    width: 16,
    height: 16,
    borderRadius: 999,
    border: "none",
    background: "#cffafe",
    color: "#5d7f77",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    cursor: "pointer",
    fontSize: 13,
    lineHeight: 1,
    padding: 0,
  },
  inputBar: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "12px 14px",
  },
  attachBtn: {
    width: 38,
    height: 38,
    borderRadius: 12,
    border: "1px solid rgba(28,25,23,0.06)",
    background: "#fff",
    color: "#78716c",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    cursor: "pointer",
    flex: "0 0 auto",
  },
  chatInput: {
    flex: 1,
    padding: "10px 14px",
    border: "1px solid rgba(28,25,23,0.06)",
    borderRadius: 12,
    fontSize: 13,
    outline: "none",
    background: "#fff",
  },
  sendBtn: {
    width: 38,
    height: 38,
    borderRadius: 12,
    border: "none",
    background: "#5d7f77",
    color: "#fff",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    cursor: "pointer",
  },
  nameInput: {
    width: "100%",
    padding: "12px 16px",
    border: "1px solid rgba(28,25,23,0.06)",
    borderRadius: 12,
    fontSize: 14,
    outline: "none",
    marginBottom: 12,
    textAlign: "center" as const,
    boxSizing: "border-box" as const,
  },
  startBtn: {
    width: "100%",
    padding: "12px 0",
    borderRadius: 12,
    border: "none",
    background: "#5d7f77",
    color: "#fff",
    fontSize: 14,
    fontWeight: 700,
    cursor: "pointer",
  },
  errorState: {
    textAlign: "center" as const,
    padding: "44px 28px",
  },
  errorIcon: {
    width: 54,
    height: 54,
    borderRadius: 18,
    margin: "0 auto 16px",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    color: "#5d7f77",
    background: "linear-gradient(135deg, rgba(79,125,117,0.12), rgba(90,142,166,0.1))",
    border: "1px solid rgba(79,125,117,0.18)",
  },
  errorTitle: {
    margin: "0 0 8px",
    color: "#1c1917",
    fontSize: 20,
    lineHeight: 1.25,
    fontWeight: 800,
  },
  errorCopy: {
    margin: 0,
    color: "#57534e",
    fontSize: 14,
    lineHeight: 1.55,
  },
  errorHint: {
    margin: "10px 0 0",
    color: "#a8a29e",
    fontSize: 12,
    lineHeight: 1.5,
  },
  signedInBox: {
    width: "100%",
    padding: "11px 14px",
    border: "1px solid #e5eeeb",
    borderRadius: 12,
    fontSize: 13,
    outline: "none",
    marginBottom: 12,
    background: "#f2f6f5",
    boxSizing: "border-box" as const,
    display: "flex",
    flexDirection: "column" as const,
    gap: 2,
  },
  authIcon: {
    width: 56,
    height: 56,
    borderRadius: 16,
    margin: "0 auto 14px",
    background: "linear-gradient(135deg, #5d7f77, #5f928a)",
    color: "#fff",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontWeight: 900,
    fontSize: 22,
  },
  authPill: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    padding: "5px 10px",
    borderRadius: 999,
    background: "#f1f6f3",
    color: "#3f7361",
    fontSize: 11,
    fontWeight: 800,
    letterSpacing: "0.02em",
  },
  authBtn: {
    minWidth: 128,
    padding: "11px 14px",
    borderRadius: 12,
    fontSize: 13,
    fontWeight: 800,
    textDecoration: "none",
    boxSizing: "border-box" as const,
  },
  primaryAuthBtn: {
    background: "#5d7f77",
    color: "#fff",
    border: "1px solid #5d7f77",
  },
  secondaryAuthBtn: {
    background: "#fff",
    color: "#5d7f77",
    border: "1px solid #ccded9",
  },
  customerAuthTabs: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 6,
    padding: 4,
    borderRadius: 12,
    background: "#f5f5f4",
    marginBottom: 14,
  },
  customerAuthTab: {
    border: "none",
    borderRadius: 9,
    background: "transparent",
    color: "#78716c",
    padding: "9px 8px",
    fontSize: 12,
    fontWeight: 800,
    cursor: "pointer",
  },
  customerAuthTabActive: {
    background: "#fff",
    color: "#5d7f77",
    boxShadow: "0 1px 3px rgba(28,25,23,0.08)",
  },
  customerAuthForm: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 10,
  },
  customerInput: {
    width: "100%",
    padding: "11px 13px",
    border: "1px solid rgba(28,25,23,0.06)",
    borderRadius: 12,
    fontSize: 13,
    outline: "none",
    background: "#fff",
    boxSizing: "border-box" as const,
  },
  customerCodeInput: {
    textAlign: "center" as const,
    fontSize: 22,
    fontWeight: 800,
    letterSpacing: 8,
  },
  customerAuthError: {
    padding: "10px 12px",
    borderRadius: 12,
    background: "#f8f0ef",
    border: "1px solid #ecc8c5",
    color: "#a23e38",
    fontSize: 12,
    lineHeight: 1.45,
    marginBottom: 12,
  },
  customerAuthDivider: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    color: "#a8a29e",
    fontSize: 11,
    fontWeight: 700,
    margin: "14px 0 10px",
    textTransform: "uppercase" as const,
  },
  googleAuthBtn: {
    width: "100%",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    padding: "11px 14px",
    borderRadius: 12,
    border: "1px solid rgba(28,25,23,0.06)",
    background: "#fff",
    color: "#1c1917",
    fontSize: 13,
    fontWeight: 800,
    cursor: "pointer",
  },
  googleMark: {
    width: 18,
    height: 18,
    borderRadius: "50%",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    border: "1px solid rgba(28,25,23,0.06)",
    color: "#4285f4",
    fontWeight: 900,
    fontSize: 12,
  },
  customerTextButton: {
    border: "none",
    background: "transparent",
    color: "#78716c",
    fontSize: 12,
    fontWeight: 700,
    cursor: "pointer",
    padding: "4px 0",
  },
  branding: {
    marginTop: 12,
    fontSize: 11,
    color: "#a8a29e",
    textAlign: "center" as const,
  },
  embedBranding: {
    display: "none",
  },
};
