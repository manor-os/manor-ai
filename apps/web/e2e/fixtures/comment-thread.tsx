import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import CommentThread from "../../src/components/CommentThread";
import { api } from "../../src/lib/api";
import { setLocale } from "../../src/lib/i18n";
import type { Comment, User } from "../../src/lib/types";
import { useAuthStore } from "../../src/stores/auth";
import "../../src/index.css";

// Isolated UI fixture: no login, comment, or mutation reaches the backend.
setLocale("en");
useAuthStore.setState({ user: { id: "reviewer" } as User });
const makeComment = (id: string, content: string, anchor: Comment["anchor"]): Comment => ({
  id, content, anchor, user_id: "reviewer", user_display_name: "Alex Chen",
  entity_id: "fixture", resource_type: "document", resource_id: "fixture",
  status: "active", mentions: [], reactions: {}, is_edited: false,
  created_at: "2026-07-18T19:15:00Z",
});
let comments = [
  makeComment("groups", "这个很重要 key = tuple((ord(s[i]) - ord(s[i-1])) % 26 for i in range(1,len(s)))", { label: "选中文字", quote: "groups" }),
  makeComment("isalnum", "isalnum 是判断是否是字母或者数字的", { label: "Selected text", quote: "isalnum" }),
  makeComment("location", "保留具体的位置提示。", { label: "Slide 3", quote: "lower" }),
  makeComment("line", "保留行号。", { line: 12, line_end: 14, quote: "code" }),
  { ...makeComment("foreign", "另一位作者的评论。", { label: "Texto seleccionado", quote: "selection" }), user_id: "other" },
];
const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
let failMutation = false;
api.comments.list = async () => comments;
api.comments.update = async (id, content) => {
  if (failMutation) throw new Error("Fixture save failed");
  comments = comments.map((comment) => comment.id === id ? { ...comment, content, is_edited: true } : comment);
  return comments.find((comment) => comment.id === id)!;
};
api.comments.create = async (data) => {
  const comment = makeComment("reply", data.content, data.anchor);
  comments = data.parent_id
    ? comments.map((parent) => parent.id === data.parent_id ? { ...parent, replies: [...(parent.replies || []), comment] } : parent)
    : [...comments, comment];
  return comment;
};
api.comments.react = async (id) => {
  comments = comments.map((comment) => comment.id === id
    ? { ...comment, reactions: { thumbsup: comment.reactions.thumbsup?.length ? [] : ["reviewer"] } }
    : comment);
  return comments.find((comment) => comment.id === id)!;
};
api.comments.delete = async (id) => {
  comments = comments.filter((comment) => comment.id !== id);
};

function Fixture() {
  const [selected, setSelected] = useState("");
  const [dark, setDark] = useState(false);
  const [wide, setWide] = useState(false);
  const [readOnly, setReadOnly] = useState(false);
  const [locale, updateLocale] = useState("en");
  return <main style={{ padding: 20, minHeight: "100vh", background: "var(--surface-app)", color: "var(--text-default)" }}>
    <header style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 16 }}>
      <button onClick={() => { document.documentElement.dataset.theme = dark ? "light" : "dark"; setDark(!dark); }}>Theme</button>
      <button onClick={() => setWide(!wide)}>Width</button>
      <button onClick={() => setReadOnly(!readOnly)}>Permissions</button>
      <button onClick={() => { const next = locale === "en" ? "zh" : "en"; setLocale(next); updateLocale(next); }}>Language</button>
      <button onClick={() => { failMutation = !failMutation; }}>Fail save</button>
      <output aria-label="Selected comment">{selected || "none"}</output>
    </header>
    <section data-testid="comments" style={{ width: wide ? 340 : 214, maxWidth: "100%" }}>
      <CommentThread resourceType="document" resourceId="fixture" canComment={!readOnly}
        anchor={{ label: "选中文字", quote: "new selection" }}
        activeCommentId={selected} onSelectComment={(comment) => setSelected(comment.id)} />
    </section>
  </main>;
}
createRoot(document.getElementById("root")!).render(<QueryClientProvider client={client}><Fixture /></QueryClientProvider>);
