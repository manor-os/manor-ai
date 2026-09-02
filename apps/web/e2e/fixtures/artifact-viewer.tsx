import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import ChatMarkdown from "../../src/components/ChatMarkdown";
import { ChatMessageReferenceStrip, chatMessageReferencesFromAttachments } from "../../src/components/ChatMessageDisplay";
import FileViewer from "../../src/pages/FileViewer";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

// Real completion/attachment renderers and FileViewer; the E2E spec intercepts
// all API traffic, including errors. Nothing is written to a live workspace.
setLocale("en");
const name = "AI_SDE_20小时课程_Agenda与课程内容.md";
const id = "01M1B8KT380NRNZ9DGX8ZVGF7A";
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function Completion() {
  const location = useLocation();
  const variant = new URLSearchParams(location.search).get("case") || "double";
  const target = variant === "once" ? encodeURIComponent(name)
    : variant === "plain" ? name : encodeURIComponent(encodeURIComponent(name));
  const returnTo = `${location.pathname}${location.search}#completion`;
  return <main style={{ maxWidth: 900, margin: "0 auto", padding: 20 }}>
    <section id="completion" className="chat-bubble chat-bubble--bot" style={{ padding: 16 }}>
      <h1>Task complete — AI SDE curriculum</h1>
      <p>Completed 5/5 steps.</p>
      <h2>Files and outputs saved</h2>
      {variant.startsWith("attachment") || variant === "canonical"
        ? <ChatMessageReferenceStrip inlineFileCards returnTo={returnTo}
          references={chatMessageReferencesFromAttachments([{
            name, document_id: variant === "canonical" ? id : encodeURIComponent(name),
            viewer_url: `/viewer/${target}`, file_type: "md",
            ...(variant === "attachment-path" ? { fs_path: `Workspaces/Preparation/${name}` } : {}),
          }])} />
        : <ChatMarkdown returnTo={returnTo} content={`[${name}](/viewer/${target})`} />}
    </section>
  </main>;
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}><BrowserRouter><Routes>
    <Route path="/viewer/:docId" element={<FileViewer />} />
    <Route path="*" element={<Completion />} />
  </Routes></BrowserRouter></QueryClientProvider>,
);
