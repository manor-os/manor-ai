import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const page = readFileSync(new URL("../src/admin/pages/Models.tsx", import.meta.url), "utf8");

test("provider keys use an in-page audited dialog instead of a native prompt", () => {
  assert.match(page, /import Input from "\.\.\/\.\.\/components\/ui\/Input"/);
  assert.match(page, /import Modal from "\.\.\/\.\.\/components\/ui\/Modal"/);
  assert.match(page, /setKeyDialogOpen\(true\)/);
  assert.match(page, /type="password"/);
  assert.match(page, /label="Audit reason"/);
  assert.match(page, /adminApi\.models\.setProviderToken/);
  assert.doesNotMatch(page, /window\.prompt\(`New API key for/);
});

test("provider key dialog exposes loading, validation, and accessible errors", () => {
  assert.match(page, /loading=\{setToken\.isPending\}/);
  assert.match(page, /keyReason\.trim\(\)\.length < 3/);
  assert.match(page, /setToken\.error && \(/);
  assert.match(page, /<div role="alert" style=\{providerKeyError\}>/);
});
