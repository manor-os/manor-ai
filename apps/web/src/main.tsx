import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { router } from "./router";
import AuthSessionBoundary from "./components/AuthSessionBoundary";
import ErrorBoundary from "./components/ErrorBoundary";
import ToastContainer from "./components/ToastContainer";
import DetailDrawer from "./components/ui/DetailDrawer";
import VersionRefreshManager from "./VersionRefreshManager";
import { initializeClientErrorCapture } from "./lib/clientErrors";
import { applyThemePreference, getStoredThemePreference } from "./lib/theme";
import "./index.css";

initializeClientErrorCapture("web");
applyThemePreference(getStoredThemePreference());

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      gcTime: 10 * 60_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});


ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AuthSessionBoundary />
        <VersionRefreshManager />
        <RouterProvider router={router} />
        <ToastContainer />
        <DetailDrawer />
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>
);
