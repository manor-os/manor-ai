import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EMBED } from "./embed/embedContext";
import { startEmbeddedSession } from "./embed/embeddedSession";
import { installFrameGuards } from "./embed/frameGuards";
import AuthSessionBoundary from "./components/AuthSessionBoundary";
import CatalogCacheBoundary from "./components/CatalogCacheBoundary";
import ErrorBoundary from "./components/ErrorBoundary";
import ToastContainer from "./components/ToastContainer";
import VersionRefreshManager from "./VersionRefreshManager";
import { initializeClientErrorCapture } from "./lib/clientErrors";
import { shouldRetryApiQuery } from "./lib/api";
import { applyThemePreference, getStoredThemePreference } from "./lib/theme";

initializeClientErrorCapture("web");
applyThemePreference(getStoredThemePreference());
// The frame gets its session from the parent page; the app's own routes never load there.
if (EMBED) {
  installFrameGuards();
  void startEmbeddedSession();
}
const { router } = EMBED ? await import("./embed/embeddedRouter") : await import("./router");

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      gcTime: 10 * 60_000,
      refetchOnWindowFocus: false,
      retry: shouldRetryApiQuery,
    },
  },
});


ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AuthSessionBoundary />
        <CatalogCacheBoundary />
        <VersionRefreshManager />
        <RouterProvider router={router} />
        <ToastContainer />
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
