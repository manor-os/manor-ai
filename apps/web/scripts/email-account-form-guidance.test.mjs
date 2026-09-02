#!/usr/bin/env node
// The email account form has to say what to type BEFORE the user types it.
//
// Gmail, Outlook, iCloud and Yahoo all reject a bare local part as the
// IMAP/SMTP username, and they answer with the same "Invalid credentials"
// they use for a wrong password — so a user who fills in "alice" instead
// of "alice@gmail.com" has no way to tell the two apart. The same goes
// for pasting a normal account password where an app password is
// required. Both are cheap to prevent with a line of guidance under the
// field, and expensive to debug without it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

function readSource(path) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const integrations = readSource("../src/pages/Integrations.tsx");
const input = readSource("../src/components/ui/Input.tsx");
const en = readSource("../src/lib/i18n/en.ts");
const zh = readSource("../src/lib/i18n/zh.ts");
const es = readSource("../src/lib/i18n/es.ts");

const GUIDANCE_KEYS = [
  "page.integrations.email_username_hint",
  "page.integrations.email_password_hint",
];

test("the shared Input primitive can render guidance under a field", () => {
  assert.match(input, /hint\?: string/);
  assert.match(input, /\{hint &&/);
  assert.match(input, /<label htmlFor=\{inputId\}/);
  assert.match(input, /aria-describedby=\{error \? errorId : hint \? hintId : undefined\}/);
});

test("the email form explains the username must be a full address", () => {
  // The username Input carries the hint prop, not just a placeholder —
  // placeholders vanish the moment the user starts typing.
  assert.match(
    integrations,
    /hint=\{t\("page\.integrations\.email_username_hint"\)\}/,
  );
});

test("the email form explains an app password is required", () => {
  assert.match(
    integrations,
    /hint=\{t\("page\.integrations\.email_password_hint"\)\}/,
  );
});

test("the username guidance names the full-address requirement", () => {
  const line = en.match(
    /"page\.integrations\.email_username_hint":\s*\n?\s*"([^"]+)"/,
  );
  assert.ok(line, "english username hint should exist");
  assert.match(line[1], /full email address/i);
});

test("the password guidance distinguishes app password from account password", () => {
  const line = en.match(
    /"page\.integrations\.email_password_hint":\s*\n?\s*"([^"]+)"/,
  );
  assert.ok(line, "english password hint should exist");
  assert.match(line[1], /app password/i);
});

test("every locale defines the guidance strings", () => {
  for (const [name, locale] of [["zh", zh], ["es", es], ["en", en]]) {
    for (const key of GUIDANCE_KEYS) {
      assert.ok(
        locale.includes(`"${key}"`),
        `${name} is missing ${key}`,
      );
    }
  }
});
