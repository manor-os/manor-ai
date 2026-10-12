import { ApplicationReloadMode, isStaleChunkError, reloadApplication } from "../utils/recoverableErrors";

export function renderBootstrapFailure(rootId: string, appName = "Manor", error?: unknown) {
  const root = document.getElementById(rootId);
  if (!root) return;

  const panel = document.createElement("main");
  panel.style.cssText = "min-height:100vh;display:grid;place-items:center;padding:24px;font-family:system-ui,sans-serif;color:#292524;background:#fafaf9";

  const content = document.createElement("div");
  content.style.cssText = "max-width:420px;text-align:center";
  const title = document.createElement("h1");
  title.textContent = `Could not start ${appName}`;
  const detail = document.createElement("p");
  detail.textContent = "Refresh to try loading the latest version.";
  const reload = document.createElement("button");
  reload.type = "button";
  reload.textContent = "Refresh";
  reload.style.cssText = "padding:10px 16px;border:0;border-radius:6px;background:#436b65;color:#fff;font:inherit;cursor:pointer";
  reload.addEventListener("click", () => reloadApplication(
    isStaleChunkError(error) ? ApplicationReloadMode.RecoverAssets : ApplicationReloadMode.Refresh,
  ));

  content.append(title, detail, reload);
  panel.append(content);
  root.replaceChildren(panel);
}
