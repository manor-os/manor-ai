/** Real OAuth/Login pages and auth store under StrictMode with disposable API responses. */
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Login from "../../src/pages/Login";
import OAuthAuthorize from "../../src/pages/OAuthAuthorize";
import { api } from "../../src/lib/api";
import { setLocale } from "../../src/lib/i18n";
import { useAuthStore } from "../../src/stores/auth";
import "../../src/index.css";

await setLocale("en");
localStorage.removeItem("manor_token");
localStorage.removeItem("manor_user");
useAuthStore.setState({ user: null, token: null, isLoading: false });
api.auth.signupConfig = async () => ({ invitation_code_required: false }) as any;
api.auth.login = async () => ({ access_token: "oauth-login-fixture-token" }) as any;
api.auth.me = async () => ({ id: "fixture-owner", entity_id: "fixture-team", role: "owner", email: "owner@example.com", display_name: "Fixture Owner", locale: "en" }) as any;
const params = new URLSearchParams({ client_id: "pms-fixture", redirect_uri: "https://pms.example/auth/callback", response_type: "code", scope: "openid profile email", state: "fixture-state" });
window.history.replaceState(null, "", `/oauth/authorize?${params}`);
createRoot(document.getElementById("root")!).render(<React.StrictMode><BrowserRouter><Routes>
  <Route path="/oauth/authorize" element={<OAuthAuthorize />} />
  <Route path="/login" element={<Login />} />
</Routes></BrowserRouter></React.StrictMode>);
