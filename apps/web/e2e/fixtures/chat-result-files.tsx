import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ChatMarkdown from "../../src/components/ChatMarkdown";
import { ChatMessageReferenceStrip, filterChatMessageReferencesAlreadyLinked, type ChatMessageDisplayReference } from "../../src/components/ChatMessageDisplay";
import WorkflowResultCard from "../../src/components/WorkflowResultCard";
import AssistantReplyResults from "../../src/components/chat/AssistantReplyResults";
import { PreviewsStartFolded } from "../../src/components/ChatFilePreview";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

// A finished result message: one sentence, then its files shown as themselves.
// The spec serves every document's content; nothing reaches a live workspace.
await setLocale(new URLSearchParams(location.search).get("lang") === "zh" ? "zh" : "en");
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

const FIXTURE_DOCUMENTS = {
  table: "01M5KEWXCEFY6PT79TGQSCHVM1",
  report: "01M5KEWXCEFY6PT79TGQSCHVM2",
  cover: "01M5KEWXCEFY6PT79TGQSCHVM3",
};

const output = (id: string, name: string, fileType: string, kind: ChatMessageDisplayReference["kind"] = "file"): ChatMessageDisplayReference => ({
  key: id, name, document_id: id, kind, fileType, openUrl: `/viewer/${id}`, isOutput: true,
});

const pictureReply = [
  "The promo image is ready:",
  "",
  `![coffee-pop-up-promotion.png](/viewer/${FIXTURE_DOCUMENTS.cover})`,
  "",
  `Same picture again, named in a sentence: [coffee-pop-up-promotion.png](/viewer/${FIXTURE_DOCUMENTS.cover}).`,
  "",
  `![](/viewer/${FIXTURE_DOCUMENTS.cover})`,
].join("\n");

const pathReply = `![bean-pop-latte-poster.png](/viewer/${FIXTURE_DOCUMENTS.cover})\n\n**Bean Pop latte poster** — square, no text.`;
const pathAttachment: ChatMessageDisplayReference = {
  key: "bean-pop-latte-poster", name: "bean-pop-latte-poster.png", kind: "image", fileType: "png", isOutput: true,
  fsPath: "Workspaces/_by_id/01M5XG338918ND9C95MVYCVDKF/bean-pop-latte-poster.png",
  openUrl: "/api/v1/fs/01KXFDNM3C7FBZDMGDY4RW6W5X/Workspaces/_by_id/01M5XG338918ND9C95MVYCVDKF/bean-pop-latte-poster.png",
};

function App() {
  return <QueryClientProvider client={client}>
    <MemoryRouter>
      <main style={{ maxWidth: 760, margin: "0 auto", padding: 16 }}>
        <section id="result" className="chat-bubble chat-bubble--bot" style={{ padding: 16 }}>
          <ChatMarkdown content="Both creators are unchanged this week; the list needs no edits." />
          <ChatMessageReferenceStrip inlineFileCards returnTo="/fixture#result" references={[
            output(FIXTURE_DOCUMENTS.table, "creator-snapshot.csv", "csv"),
            output(FIXTURE_DOCUMENTS.report, "weekly-report.md", "md"),
          ]} />
          <WorkflowResultCard result={{ workflow_name: "Weekly report", workflow_id: "wf_1", run_id: "run_1", status: "completed",
            summary: "Both creators are unchanged this week; the list needs no edits." }} />
        </section>
        <section id="linked-result" className="chat-bubble chat-bubble--bot" style={{ padding: 16, marginTop: 16 }}>
          <ChatMarkdown showFilesInPlace content={[
            `I saved this week's list as [creator-snapshot.csv](/viewer/${FIXTURE_DOCUMENTS.table}).`,
            "",
            `[weekly-report.md](/viewer/${FIXTURE_DOCUMENTS.report})`,
            "",
            "The archive stays a link: [assets.zip](/viewer/01M5KEWXCEFY6PT79TGQSCHVM4).",
          ].join("\n")} />
        </section>
        <section id="picture-in-text" className="chat-bubble chat-bubble--bot" style={{ padding: 16, marginTop: 16 }}>
          {/* The live case: the reply embeds the picture and the same file is attached. */}
          <ChatMarkdown showFilesInPlace content={pictureReply} />
          <ChatMessageReferenceStrip inlineFileCards returnTo="/fixture#picture-in-text"
            references={filterChatMessageReferencesAlreadyLinked(pictureReply, [output(FIXTURE_DOCUMENTS.cover, "coffee-pop-up-promotion.png", "png", "image")])} />
        </section>
        <section id="picture-by-path" className="chat-bubble chat-bubble--bot" style={{ padding: 16, marginTop: 16 }}>
          {/* Live shape: the reply embeds /viewer/<document id>, the attachment only has its storage path. */}
          <ChatMarkdown showFilesInPlace content={pathReply} />
          <ChatMessageReferenceStrip inlineFileCards returnTo="/fixture#picture-by-path"
            references={filterChatMessageReferencesAlreadyLinked(pathReply, [pathAttachment])} />
        </section>
        <section id="picture-outside-reply" className="chat-bubble chat-bubble--bot" style={{ padding: 16, marginTop: 16 }}>
          <ChatMarkdown content={`Outside a chat reply the picture still loads: ![coffee-pop-up-promotion.png](/viewer/${FIXTURE_DOCUMENTS.cover}) and a missing one reads as its name: ![missing-poster.png](/viewer/01M5KEWXCEFY6PT79TGQSCHVM9)`} />
        </section>
        <section id="workspace-result" className="chat-bubble chat-bubble--bot" style={{ padding: 16, marginTop: 16 }}>
          {/* A finished Workflow result in Workspace chat: its files start as rows. */}
          <PreviewsStartFolded.Provider value>
            <ChatMarkdown showFilesInPlace content={`The weekly list is ready. [weekly-report.md](/viewer/${FIXTURE_DOCUMENTS.report})`} />
            <AssistantReplyResults returnTo="/fixture#workspace-result" references={[
              output(FIXTURE_DOCUMENTS.table, "creator-snapshot.csv", "csv"),
              output(FIXTURE_DOCUMENTS.cover, "cover.png", "png", "image"),
            ]} workflowResult={{ workflow_name: "Weekly report", workflow_id: "wf_1", run_id: "run_1", status: "completed" }} />
          </PreviewsStartFolded.Provider>
        </section>
        <section id="image-result" className="chat-bubble chat-bubble--bot" style={{ padding: 16, marginTop: 16 }}>
          <ChatMarkdown content="Here is the cover." />
          <ChatMessageReferenceStrip inlineFileCards returnTo="/fixture#image-result" references={[
            output(FIXTURE_DOCUMENTS.cover, "cover.png", "png", "image"),
          ]} />
        </section>
      </main>
    </MemoryRouter>
  </QueryClientProvider>;
}

createRoot(document.getElementById("root")!).render(<App />);
