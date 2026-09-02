(() => {
  document.documentElement.dataset.shellScript = "started";
  const token = new URLSearchParams(window.location.search).get("preview") || "";
  const preview = document.getElementById("manor-generated-preview");
  const parentOrigin = window.location.origin;
  let renderRequested = false;

  const notifyParent = (message) => {
    window.parent.postMessage({ ...message, token }, parentOrigin);
  };

  preview.addEventListener("load", () => {
    if (!renderRequested) return;
    preview.contentWindow?.postMessage({ type: "manor-html-preview:host-ready" }, "*");
    notifyParent({ type: "manor-html-preview:rendered" });
  });

  window.addEventListener("message", (event) => {
    if (event.source === window.parent) {
      if (
        event.origin === parentOrigin
        && event.data?.type === "manor-html-preview:render"
        && event.data?.token === token
        && typeof event.data?.html === "string"
      ) {
        renderRequested = true;
        preview.srcdoc = event.data.html;
        return;
      }
      preview.contentWindow?.postMessage(event.data, "*");
      return;
    }
    if (event.source === preview.contentWindow) {
      window.parent.postMessage(event.data, parentOrigin);
    }
  });

  if (!token) {
    notifyParent({ type: "manor-html-preview:error", error: "missing_preview_token" });
    return;
  }
  document.documentElement.dataset.shellReady = "yes";
  notifyParent({ type: "manor-html-preview:shell-ready" });
})();
