import { defineConfig } from "@playwright/test";

/**
 * Browser E2E config for the web app.
 *
 * Auto-starts the Vite dev server (port 3100) which proxies /api → the backend
 * at VITE_API_TARGET (default http://localhost:8000). The backend must be
 * running separately — see e2e/README.md.
 */
const e2ePort = Number(process.env.E2E_PORT || 3100);
const e2eBase = process.env.E2E_BASE ?? `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: e2eBase,
    trace: "on-first-retry",
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${e2ePort} --strictPort`,
    url: e2eBase,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { VITE_API_TARGET: process.env.E2E_API ?? "http://localhost:8000" },
  },
});
