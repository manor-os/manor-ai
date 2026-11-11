import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { createHash } from "node:crypto";
import { extname, resolve, sep } from "path";
import { cpSync, createReadStream, existsSync, rmSync, statSync } from "fs";

const apiTarget = process.env.VITE_API_TARGET || "http://localhost:8000";
const buildVersion = process.env.BUILD_VERSION || new Date().toISOString();
const releaseAssetsDir = `assets/releases/${createHash("sha256").update(buildVersion).digest("hex").slice(0, 20)}`;


function firstHeader(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function forwardOriginalHost(proxy: any) {
  proxy.on("proxyReq", (proxyReq: any, req: any) => {
    const host = firstHeader(req.headers["x-forwarded-host"]) || firstHeader(req.headers.host);
    if (host) proxyReq.setHeader("x-forwarded-host", host);
    const proto = firstHeader(req.headers["x-forwarded-proto"]) || "http";
    proxyReq.setHeader("x-forwarded-proto", proto);
  });
}

// Published sites in local development: production's reverse proxy sends every
// {slug}.{MANOR_SITES_DOMAIN} request to /api/v1/site-host{uri}. The dev server
// does the same for hosts under the sites domain, so root-absolute links and
// /__manor/runtime.js resolve inside the site. The original Host travels as
// X-Forwarded-Host (forwardOriginalHost), which the API routes by.
const sitesDomain = (process.env.VITE_SITES_DOMAIN || "sites.localhost").trim().toLowerCase().replace(/^\.+|\.+$/g, "");

function isSitesHost(hostHeader: string | undefined): boolean {
  const host = (hostHeader || "").replace(/:\d+$/, "").toLowerCase().replace(/\.$/, "");
  return Boolean(sitesDomain) && host.endsWith(`.${sitesDomain}`);
}

function publishedSitesDevHost(): Plugin {
  return {
    name: "published-sites-dev-host",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (isSitesHost(firstHeader(req.headers.host))) {
          req.url = `/api/v1/site-host${req.url?.startsWith("/") ? req.url : `/${req.url || ""}`}`;
        }
        next();
      });
    },
  };
}

function emitBuildVersion(): Plugin {
  return {
    name: "emit-build-version",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "version.json",
        source: JSON.stringify({ version: buildVersion }, null, 2),
      });
    },
  };
}

function isInsideDir(filePath: string, dir: string): boolean {
  return filePath === dir || filePath.startsWith(`${dir}${sep}`);
}

function contentTypeForFile(filePath: string): string {
  switch (extname(filePath).toLowerCase()) {
    case ".css":
      return "text/css";
    case ".html":
      return "text/html; charset=utf-8";
    case ".ico":
      return "image/x-icon";
    case ".js":
      return "text/javascript";
    case ".json":
      return "application/json";
    case ".png":
      return "image/png";
    case ".svg":
      return "image/svg+xml";
    case ".webp":
      return "image/webp";
    default:
      return "application/octet-stream";
  }
}


function pdfjsAssets(): Plugin {
  const assets = [
    { prefix: "/pdfjs/cmaps/", dir: resolve(__dirname, "node_modules/pdfjs-dist/cmaps"), out: "pdfjs/cmaps" },
    { prefix: "/pdfjs/standard_fonts/", dir: resolve(__dirname, "node_modules/pdfjs-dist/standard_fonts"), out: "pdfjs/standard_fonts" },
  ];

  return {
    name: "pdfjs-assets",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const pathname = decodeURIComponent((req.url || "").split("?")[0] || "");
        const asset = assets.find((entry) => pathname.startsWith(entry.prefix));
        if (!asset) {
          next();
          return;
        }

        const filePath = resolve(asset.dir, pathname.slice(asset.prefix.length));
        if (!isInsideDir(filePath, asset.dir) || !existsSync(filePath) || statSync(filePath).isDirectory()) {
          next();
          return;
        }

        res.setHeader("Content-Type", "application/octet-stream");
        createReadStream(filePath).pipe(res);
      });
    },
    writeBundle(options) {
      const outDir = options.dir || resolve(__dirname, "dist");
      for (const asset of assets) {
        if (existsSync(asset.dir)) {
          const targetDir = resolve(outDir, asset.out);
          rmSync(targetDir, { recursive: true, force: true });
          cpSync(asset.dir, targetDir, { recursive: true });
        }
      }
    },
  };
}

/** Production loads English as its own file; see src/lib/i18n/englishAtStartup.ts. */
function englishOnDemand(): Plugin {
  return {
    name: "manor-english-on-demand",
    apply: "build",
    enforce: "pre",
    resolveId(id) {
      return id === "./i18n/englishAtStartup" ? resolve(__dirname, "src/lib/i18n/englishOnDemand.ts") : null;
    },
  };
}

// Deployment-specific entries share node_modules but ship separate bundles.
export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(buildVersion),
  },
  // ``build.target`` (set lower in this file) only affects production
  // builds. The dev server runs deps through esbuild's separate
  // ``optimizeDeps`` step, which has its own target — left at the
  // default of chrome87/edge88/es2020/firefox78/safari14 it rejects
  // top-level await. @novnc/novnc 1.7.0's core/util/browser.js uses
  // TLA, so dev would fail with the same "Top-level await is not
  // available" error the prod build had before #35 was merged.
  // Mirror the production target here so dev and build behave
  // identically.
  optimizeDeps: {
    esbuildOptions: {
      target: "es2022",
    },
  },
  plugins: [
    // First, so a site host never reaches the app's own middlewares.
    publishedSitesDevHost(),
    englishOnDemand(),
    react(),
    pdfjsAssets(),
    emitBuildVersion(),
  ],
  server: {
    port: 3000,
    host: true,
    // Vite refuses unknown Host headers; published sites arrive as {slug}.{sites domain}.
    allowedHosts: sitesDomain ? [`.${sitesDomain}`] : [],
    proxy: {
      // SSE streaming endpoint — must be listed before the generic /api rule
      // so it matches first and gets the buffering-disabled proxy config
      "/api/v1/chat/stream": {
        target: apiTarget,
        changeOrigin: true,
        configure: (proxy) => {
          forwardOriginalHost(proxy);
          proxy.on("proxyRes", (proxyRes) => {
            // Prevent proxy from buffering the SSE stream
            proxyRes.headers["x-accel-buffering"] = "no";
            proxyRes.headers["cache-control"] = "no-cache, no-transform";
          });
        },
      },
      "^/api(/|$)": {
        target: apiTarget,
        changeOrigin: true,
        configure: forwardOriginalHost,
        // Headed-login streams a CDP screencast over WS at
        // /api/v1/integrations/headed-login/{sid}/stream. Upgrade
        // requests on /api paths need ws:true here; SSE doesn't use
        // Upgrade so it's unaffected.
        ws: true,
      },
      "/ws": {
        target: apiTarget,
        ws: true,
        changeOrigin: true,
        // Swallow expected disconnect noise from API restarts / closed
        // browser tabs. There are TWO separate code paths to silence:
        //
        //  1. ``proxy.on('error')`` — fires for HTTP-side errors (e.g.
        //     upstream unreachable on initial connect).
        //  2. The raw socket pipe between upgraded WebSocket sockets —
        //     these errors bypass the proxy event bus entirely and
        //     surface as uncaught socket errors. Need to attach error
        //     listeners directly on the sockets we get from the
        //     ``proxyReqWs`` and ``open`` events.
        //
        // Real proxy failures (ECONNREFUSED, ETIMEDOUT, target down)
        // still surface because they don't match the expected-disconnect
        // patterns below.
        configure: (proxy) => {
          const isExpectedDisconnect = (err: unknown) => {
            const e = err as NodeJS.ErrnoException;
            const code = e?.code;
            const msg = e?.message || "";
            return (
              code === "ECONNRESET" ||
              code === "EPIPE" ||
              code === "ECONNREFUSED" ||  // API restart in flight
              code === "ETIMEDOUT" ||      // brief network blip
              msg.includes("socket has been ended") ||
              msg.includes("write after end") ||
              msg.includes("Premature close")
            );
          };

          // Vite calls ``configure(proxy, opts)`` BEFORE it registers
          // its own ``proxy.on('error', ...)`` listener (verified
          // against vite/dist/node/chunks). So ``removeAllListeners``
          // here would run before Vite's listener exists — useless.
          //
          // Instead, intercept the EventEmitter ``emit`` itself: when
          // the proxy is about to fire an ``error`` event for one of
          // the expected-disconnect codes, return early so neither
          // Vite's logger nor any other listener sees it. Real errors
          // pass through untouched.
          const origEmit = proxy.emit.bind(proxy);
          (proxy as any).emit = function (event: string, ...args: unknown[]) {
            if (event === "error" && isExpectedDisconnect(args[0])) {
              return false;
            }
            return origEmit(event as any, ...(args as any[]));
          };

          // Fired with the upstream-bound request + the client socket.
          proxy.on("proxyReqWs", (_proxyReq, _req, clientSocket) => {
            clientSocket.on("error", (err) => {
              if (isExpectedDisconnect(err)) return;
              // eslint-disable-next-line no-console
              console.error("[vite] ws client socket error:", err);
            });
          });

          // Fired when the upstream WebSocket connection is open.
          proxy.on("open", (proxySocket) => {
            proxySocket.on("error", (err) => {
              if (isExpectedDisconnect(err)) return;
              // eslint-disable-next-line no-console
              console.error("[vite] ws upstream socket error:", err);
            });
          });
        },
      },
      "/health": {
        target: apiTarget,
        changeOrigin: true,
      },
      "/config": {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    assetsDir: releaseAssetsDir,
    chunkSizeWarningLimit: 600,
    // Vite's default target (chrome87/edge88/es2020/firefox78/safari14)
    // predates top-level await. @novnc/novnc 1.7.0 uses TLA at module
    // scope (`await _checkWebCodecsH264DecodeSupport()` in core/rfb.js),
    // which makes esbuild fail with:
    //   ERROR: Top-level await is not available in the configured
    //   target environment ("chrome87" + ...)
    // es2022 covers TLA (Chrome 89+, Firefox 89+, Safari 15+ — all
    // shipped 2021). Manor users run modern browsers; the ~4-year-old
    // baseline drop is acceptable.
    target: "es2022",
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
      },
      output: {
        // Use the standard JavaScript MIME mapping and a new URL so clients
        // recover from cached .mjs responses served as application/octet-stream.
        // Keep the original PDF.js module intact, including fallback exports.
        assetFileNames: (asset) => asset.names.includes("pdf.worker.min.mjs")
          ? `${releaseAssetsDir}/[name]-[hash].js`
          : `${releaseAssetsDir}/[name]-[hash][extname]`,
        manualChunks: {
          vendor: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
          state: ["zustand"],
        },
      },
    },
  },
});
