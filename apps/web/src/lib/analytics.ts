type QueuedAnalyticsFunction = ((...args: unknown[]) => void) & {
  q?: unknown[][];
};

declare global {
  interface Window {
    dataLayer?: unknown[];
    clarity?: QueuedAnalyticsFunction;
  }
}

function isLocalRuntime(): boolean {
  const hostname = window.location.hostname;
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    /^\d+\.\d+\.\d+\.\d+$/.test(hostname) ||
    hostname.endsWith(".local")
  );
}

function loadScript(src: string) {
  const script = document.createElement("script");
  script.async = true;
  script.src = src;
  document.head.appendChild(script);
}

export function initializeAnalytics() {
  if (isLocalRuntime()) return;

  loadScript("https://www.googletagmanager.com/gtag/js?id=G-DBLBSCX1JE");
  window.dataLayer = window.dataLayer || [];
  const gtag = (...args: unknown[]) => window.dataLayer?.push(args);
  gtag("js", new Date());
  gtag("config", "G-DBLBSCX1JE");

  const clarity: QueuedAnalyticsFunction = (...args: unknown[]) => {
    clarity.q = clarity.q || [];
    clarity.q.push(args);
  };
  window.clarity = window.clarity || clarity;
  loadScript("https://www.clarity.ms/tag/uy303tt3ps");
}
