/** Provider-free fixture for recording lifecycle and chat playback. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ChatInputFooter from "../../src/components/ChatInputFooter";
import ChatMessageActions from "../../src/components/chat/ChatMessageActions";
import PublicChat from "../../src/pages/PublicChat";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

const params = new URLSearchParams(location.search);
setLocale(params.get("locale") === "zh" ? "zh" : "en");
const isPublic = params.get("surface") === "public";
if (isPublic) localStorage.setItem("manor_public_chat_session:voice-demo", "voice-session");
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function ComposerFixture() {
  const [input, setInput] = useState("Draft");
  const [conversationId, setConversationId] = useState(params.get("empty") === "1" ? "" : "conversation-one");
  const [sent, setSent] = useState("");
  return <main style={{ maxWidth: 720, margin: "24px auto", padding: 16 }}>
    <h1>AI voice · Chat / Workspace Chat</h1>
    <p>Record a message, review the transcript, then send. AI replies can be read aloud.</p>
    <section className="chat-message-shell" style={{ padding: "32px 0" }}>
      <p>Hello! How can I help?</p>
      <ChatMessageActions copyText="Hello! How can I help?" speechText="Hello! How can I help?" voiceScope={{ conversationId }} />
      <p>Here is another reply.</p>
      <ChatMessageActions copyText="Here is another reply." speechText="Here is another reply." voiceScope={{ conversationId }} />
    </section>
    <ChatInputFooter value={input} onChange={setInput} streaming={false}
      voiceScope={{ conversationId, workspaceId: params.get("surface") === "workspace" ? "workspace" : undefined }}
      onVoiceConversation={setConversationId}
      onSend={(text) => { setSent(text); setInput(""); }} onStop={() => {}} />
    <output data-testid="conversation">{conversationId}</output>
    <output data-testid="draft">{input}</output>
    <output data-testid="sent">{sent}</output>
    <button onClick={() => { setConversationId("conversation-two"); setInput("New draft"); }}>Switch conversation</button>
  </main>;
}

createRoot(document.getElementById("root")!).render(<QueryClientProvider client={queryClient}>
  <MemoryRouter initialEntries={isPublic ? ["/chat/public/voice-demo"] : ["/"]}>
    <Routes><Route path="/chat/public/:token" element={<PublicChat />} /><Route path="/" element={<ComposerFixture />} /></Routes>
  </MemoryRouter>
</QueryClientProvider>);
