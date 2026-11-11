// No imports: shown instead of the app when an embed URL is opened outside
// the provider's own Workspace page (or the frame could not be isolated).
export function renderEmbedNotice(rootId: string) {
  const root = document.getElementById(rootId);
  if (!root) return;
  const zh = (navigator.language || "").toLowerCase().startsWith("zh");
  const panel = document.createElement("main");
  panel.style.cssText = "min-height:100vh;display:grid;place-items:center;padding:24px;font-family:system-ui,sans-serif;color:#2c2c2e;background:#fafafa";
  const content = document.createElement("div");
  content.style.cssText = "max-width:420px;text-align:center";
  const text = document.createElement("p");
  text.textContent = zh ? "请从你的 Workspace 中打开客户试用。" : "Open this customer trial from your Workspace.";
  const link = document.createElement("a");
  link.href = "/workspaces";
  link.target = "_top";
  link.textContent = zh ? "前往 Workspace" : "Go to Workspaces";
  link.style.cssText = "color:#436b65";
  content.append(text, link);
  panel.append(content);
  root.replaceChildren(panel);
}
