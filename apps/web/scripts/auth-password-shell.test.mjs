#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

function readSource(path) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const authShell = readSource("../src/components/auth/AuthShell.tsx");
const login = readSource("../src/pages/Login.tsx");
const oauthCallback = readSource("../src/pages/OAuthCallback.tsx");
const authNavigation = readSource("../src/lib/authNavigation.ts");
const api = readSource("../src/lib/api.ts");
const forgotPassword = readSource("../src/pages/ForgotPassword.tsx");
const resetPassword = readSource("../src/pages/ResetPassword.tsx");

test("password pages share the login authentication shell", () => {
  assert.match(authShell, /login-shell-panel/);
  assert.match(authShell, /login-showcase-wash/);

  for (const source of [login, forgotPassword, resetPassword]) {
    assert.match(source, /<AuthShell>/);
  }

  assert.doesNotMatch(forgotPassword, /aurora-bg/);
  assert.doesNotMatch(resetPassword, /aurora-bg/);
});

test("an invalid reset token hides the password form and offers recovery", () => {
  assert.match(resetPassword, /const \[tokenInvalid, setTokenInvalid\] = useState\(!token\)/);
  assert.match(resetPassword, /err instanceof ApiError && err\.status === 400/);
  assert.match(resetPassword, /tokenInvalid \? \(/);
  assert.match(resetPassword, /to="\/forgot-password"/);
  assert.match(resetPassword, /page\.reset_password\.request_new_link/);
});

test("Google sign-in preserves the user's remember-me choice", () => {
  assert.match(login, /sessionStorage\.setItem\("oauth_remember_me", rememberMe \? "1" : "0"\)/);
  assert.match(oauthCallback, /rememberMe: rememberMeRef\.current/);
  assert.match(oauthCallback, /sessionStorage\.removeItem\("oauth_remember_me"\)/);
  assert.match(api, /remember_me: Boolean\(opts\.rememberMe\)/);
});

