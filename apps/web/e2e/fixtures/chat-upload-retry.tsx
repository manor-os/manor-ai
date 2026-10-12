import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import EmbeddedChat, { ArtifactSummaryCards, ArtifactViewer, type OutputArtifact } from "../../src/components/EmbeddedChat";
import ChatInputFooter, { createChatMessageAttachmentSnapshot, type AttachedItem } from "../../src/components/ChatInputFooter";
import { useAuthStore } from "../../src/stores/auth";
import { useDisplayPreferences } from "../../src/stores/displayPreferences";
import { setLocale } from "../../src/lib/i18n";
import { api } from "../../src/lib/api";
import fixtureUrl from "./chat-upload.heic?url";
import fixtureUrl2 from "./chat-upload-2.heic?url";
import "../../src/index.css";

await setLocale("zh");
useAuthStore.setState({ token: "synthetic-upload-test-token", isLoading: false,
  user: { id: "upload-user", entity_id: "upload-entity", name: "Upload Test", role: "owner" } as any });
useDisplayPreferences.setState({ interfaceMode: "professional", hasSelectedInterfaceMode: true });
const originalFetch = window.fetch.bind(window);
const composerMode = new URLSearchParams(window.location.search).has("composer");
const downloadMode = new URLSearchParams(window.location.search).has("download");
const attachments = composerMode ? [] : await Promise.all([fixtureUrl, fixtureUrl2].map(async (url, index) => {
  const name = `IMG_${9358 + index}.png`;
  const blob = await (await originalFetch(url)).blob();
  return createChatMessageAttachmentSnapshot({ name, type: "file", file: new File([blob], name, { type: "image/png" }) });
}));
const created_at = new Date().toISOString();
const history = [
  { id: "fixture-user-message", role: "user", content: "合并成一个pdf", created_at, attachments },
  { id: "fixture-failed-message", role: "assistant", content: "Error: test upload failed", created_at },
];
// Files stay in live Chat memory; JSON history cannot carry their bytes.
api.chat.getMessagesPage = async () => ({ items: history, has_more: false }) as any;
let notifyRequest: (value: string) => void = () => {};
const stream = api.chat.stream;
api.chat.stream = async (...args) => {
  try { return await stream(...args); }
  catch (error) { notifyRequest(`请求失败：${String(error)}`); throw error; }
};
window.fetch = async (input, init) => {
  const path = String(input);
  if (!path.startsWith("/api/v1/")) return originalFetch(input, init);
  if (path === "/api/v1/chat/stream") {
    const files = (init?.body as FormData).getAll("files") as File[];
    const signatures = await Promise.all(files.map(async (file) => {
      const header = new Uint8Array(await file.slice(0, 8).arrayBuffer());
      return `${file.name}: ${file.type}, PNG=${header[0] === 137 && header[1] === 80}`;
    }));
    notifyRequest(`收到 ${files.length} 个文件；${signatures.join("；")}`);
    return new Response('data: {"type":"text","content":"测试接口已收到两张 PNG。"}\n\ndata: [DONE]\n\n', {
      headers: { "Content-Type": "text/event-stream" },
    });
  }
  if (path.includes("/messages/page")) return Response.json({ items: history, has_more: false });
  if (path.includes("/agent-initiatives/conversation/")) return Response.json({ items: [], conversation: { id: "upload-fixture", title: "Upload test" } });
  return Response.json([]);
};
const queries = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } });
const brokenHeader = new Uint8Array(16);
new DataView(brokenHeader.buffer).setUint32(0, 16);
brokenHeader.set(new TextEncoder().encode("ftypheic"), 4);
const brokenFile = new File([brokenHeader], "broken.heic", { type: "image/heic" });
function ComposerFixture() {
  const [draft, setDraft] = useState("转换这张图片");
  const [seed, setSeed] = useState({ key: "broken", items: [{ name: brokenFile.name, type: "file", file: brokenFile, fileType: "heic", mimeType: brokenFile.type }] as AttachedItem[] });
  const replaceFile = async () => {
    const blob = await (await originalFetch(fixtureUrl)).blob();
    const file = new File([blob], "valid.heic", { type: "image/heic" });
    setSeed({ key: "valid", items: [{ name: file.name, type: "file", file, fileType: "heic", mimeType: file.type }] });
  };
  return <>
    <button onClick={() => void replaceFile()}>换成有效图片</button>
    <ChatInputFooter value={draft} onChange={setDraft} attachments="documents" streaming={false}
      seedAttachments={seed.items} seedAttachmentsKey={seed.key} onStop={() => {}}
      onSend={(text, items) => {
        setDraft("");
        void api.chat.stream(text, "upload-fixture", { files: items.flatMap((item) => item.file ? [item.file] : []) });
      }} />
  </>;
}
function DownloadFixture() {
  const artifacts: OutputArtifact[] = attachments.map((item, index) => ({
    id: `download-${index}`, title: item.name, kind: "image", status: "done",
    blob: item.file, href: item.previewUrl, body: item.previewUrl, meta: item.mimeType,
    data: { file_type: item.fileType, mime_type: item.mimeType },
  }));
  const [selected, setSelected] = useState(artifacts[0]);
  return <><ArtifactSummaryCards artifacts={artifacts} onOpen={setSelected} /><ArtifactViewer artifact={selected} /></>;
}
function Fixture() {
  const [receipt, setReceipt] = useState("等待点击重试");
  notifyRequest = setReceipt;
  return <MemoryRouter initialEntries={["/chat?conversation=upload-fixture"]}>
    <QueryClientProvider client={queries}>
      <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
        <output data-testid="upload-receipt" style={{ padding: 12 }}>{receipt}</output>
        <div style={{ flex: 1, minHeight: 0 }}>{composerMode ? <ComposerFixture /> : downloadMode ? <DownloadFixture /> : <EmbeddedChat conversationId="upload-fixture" title="Manor AI" />}</div>
      </div>
    </QueryClientProvider>
  </MemoryRouter>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
