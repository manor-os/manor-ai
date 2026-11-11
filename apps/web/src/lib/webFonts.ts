const MONO_FONT_URL = "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap";

/** Added after startup so a slow or blocked font host never holds up the first paint. */
export function loadWebFonts(): void {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = MONO_FONT_URL;
  document.head.appendChild(link);
}
