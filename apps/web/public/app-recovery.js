// A full document navigation also drops the failed ES-module map. The response
// clears only HTTP cache; never delete cookies, localStorage or sessionStorage.
(() => {
  const params = new URLSearchParams(window.location.search);
  let next = new URL("/chat", window.location.origin);
  try {
    const candidate = new URL(params.get("next") || "/chat", window.location.origin);
    if (candidate.origin === window.location.origin && decodeURIComponent(candidate.pathname) !== "/app-recovery.html") next = candidate;
  } catch { /* Keep the safe same-origin default. */ }
  const button = document.getElementById("continue");
  button.href = next.href;
  if ((navigator.language || "").startsWith("zh")) {
    document.documentElement.lang = "zh";
    document.getElementById("title").textContent = "正在恢复应用";
    document.getElementById("detail").textContent = "正在重新加载应用文件，登录状态和已保存的数据会保留。";
    button.textContent = "继续";
  }
  window.setTimeout(() => window.location.replace(next.href), 250);
})();
