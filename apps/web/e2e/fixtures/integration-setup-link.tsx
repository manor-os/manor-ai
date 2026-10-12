/** Renderer-only sample. Clicking a setup link opens the real app; no mocked API. */
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, useLocation } from "react-router-dom";
import ChatMarkdown from "../../src/components/ChatMarkdown";
import "../../src/index.css";

function Fixture() {
  const location = useLocation();
  React.useEffect(() => {
    if (location.pathname === "/integrations") window.location.reload();
  }, [location.pathname]);
  return <main style={{ maxWidth: 680, padding: 24 }}>
    <p>回复组件测试文案 · 点击后打开真实配置页，不会自动授权。</p>
    <section className="chat-bubble chat-bubble--bot" style={{ margin: "20px 0" }}>
      <strong>Manor AI</strong>
      <ChatMarkdown content={"Gmail 尚未连接，邮件发送暂不可用。点击 [Gmail 邮箱](/integrations?provider=gmail) 配置；其他工作可以继续。\n\n浏览器操作需要先完成 [Chrome 浏览器](/integrations?provider=chrome) 配置。"} />
    </section>
    <ChatMarkdown content={"普通外部链接不会显示为配置入口：[外部网页](https://example.com/integrations?provider=gmail)。"} />
  </main>;
}
createRoot(document.getElementById("root")!).render(<BrowserRouter><Fixture /></BrowserRouter>);
