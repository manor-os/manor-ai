/** Provider-free fixture for the shared chat composer's attachment interactions. */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ChatInputFooter, { type AttachedItem } from "../../src/components/ChatInputFooter";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

declare global {
  interface Window {
    attachmentFixtureSent?: { text: string; attachments: AttachedItem[] };
  }
}

await setLocale("en");
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function Fixture() {
  const [draft, setDraft] = useState("Review these files");
  const [disabled, setDisabled] = useState(false);
  const [streaming, setStreaming] = useState(false);
  return <main style={{ maxWidth: 720, margin: "24px auto", padding: 16 }}>
    <h1>Chat attachments</h1>
    <ChatInputFooter value={draft} onChange={setDraft} disabled={disabled} streaming={streaming}
      onSend={(text, attachments) => {
        window.attachmentFixtureSent = { text, attachments };
        setDraft("");
      }}
      onStop={() => setStreaming(false)} />
    <output data-testid="draft">{draft}</output>
    <div style={{ display: "flex", gap: 16, marginTop: 24 }}>
      <label><input type="checkbox" checked={disabled} onChange={event => setDisabled(event.target.checked)} /> Disabled</label>
      <label><input type="checkbox" checked={streaming} onChange={event => setStreaming(event.target.checked)} /> Streaming</label>
    </div>
  </main>;
}

createRoot(document.getElementById("root")!).render(<QueryClientProvider client={queryClient}>
  <MemoryRouter><Fixture /></MemoryRouter>
</QueryClientProvider>);
