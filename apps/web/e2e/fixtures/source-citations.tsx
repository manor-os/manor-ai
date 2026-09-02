import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, useLocation } from "react-router-dom";
import ChatMarkdown from "../../src/components/ChatMarkdown";
import SourceCitation from "../../src/components/SourceCitation";
import "../../src/index.css";

const samples = [
  ["named-web", '可以从官方资料中查看概念、用法与示例。 [MDN 官方文档](https://developer.mozilla.org/en-US/docs/Web/JavaScript "source")\n\n组件的 API 说明与示例也可以直接查阅。 [React 官方文档](https://react.dev/reference/react "source")'],
  ["legacy", "请像真实面试一样回答，先讲思路。\nCREDIT: The problem is sourced from the user provided file `Pinterest_Adobe_LeetCode_面试真题整理.md`, TikTok Hard / Minimum Window Substring.\n接下来我会继续追问。"],
  ["files", "评估基于上传的笔记。\n\nSources:\n- [面试真题整理_非常长的文件名用于移动端换行测试.md](/viewer/citation-doc-id)\n- [Official documentation](https://example.test/docs?lang=zh&section=1)"],
  ["web", "Web search evidence supports this claim [Source](https://example.test/research)."],
  ["named-file", '面试练习参考上传的题目笔记。 [面试真题整理.md](/viewer/named-citation-doc-id "source")'],
  ["normal", "Visit the [website](https://example.test/home). Open [report.md](/viewer/another-doc-id)."],
  ["code", '```text\nCREDIT: `report.md`\n```'],
];

function Fixture() {
  const location = useLocation();
  const [dark, setDark] = useState(false);
  useEffect(() => { document.documentElement.dataset.theme = dark ? "dark" : "light"; }, [dark]);
  const renderSample = ([id, content]: string[]) => <section key={id} data-testid={id}
    className="chat-bubble chat-bubble--bot" style={{ margin: "18px 0", padding: 16 }}>
    <ChatMarkdown content={content} />
  </section>;
  return <main style={{ maxWidth: 720, margin: "0 auto", padding: 20 }}>
    <header className="flex items-center justify-between gap-4">
      <h1>Source citations</h1>
      <button type="button" className="btn-outline" onClick={() => setDark(!dark)}>{dark ? "Light mode" : "Dark mode"}</button>
    </header>
    {samples.filter(([id]) => !["normal", "code"].includes(id)).map(renderSample)}
    <details>
      <summary>非引用原文示例</summary>
      {samples.filter(([id]) => ["normal", "code"].includes(id)).map(renderSample)}
      <section data-testid="user"><ChatMarkdown isUser content={"CREDIT: `report.md`\n[Source](https://example.test/user)"} /></section>
      <section data-testid="stream"><ChatMarkdown streaming content="Sources:" /></section>
      <section data-testid="loaded-icon" className="chat-md">
        <SourceCitation label="Loaded site icon" kind="web" iconUrl="https://example.test/source-icon.svg">
          Source icon fixture
        </SourceCitation>
      </section>
      <output data-testid="destination">{location.pathname}</output>
    </details>
  </main>;
}
createRoot(document.getElementById("root")!).render(<BrowserRouter><Fixture /></BrowserRouter>);
