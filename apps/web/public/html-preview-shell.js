(() => {
  document.documentElement.dataset.shellScript = "started";
  const params = new URLSearchParams(window.location.search);
  const token = params.get("preview") || "";
  const preview = document.getElementById("manor-generated-preview");
  const parentOrigin = window.location.origin;
  let renderRequested = false;

  // frame-src on this document is the only policy that stops the inner frame
  // navigating itself to a URL carrying its data; about:srcdoc stays exempt.
  if (params.get("navigation") === "locked") {
    const policy = document.createElement("meta");
    policy.httpEquiv = "Content-Security-Policy";
    policy.content = "frame-src 'none'";
    document.head.appendChild(policy);
  }

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
      // The inner document can read the token from document.baseURI, so only
      // this script may speak the shell's own protocol to the host.
      if (String(event.data?.type).startsWith("manor-html-preview:")) return;
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
