import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = resolve(fileURLToPath(new URL(".", import.meta.url)));
const apiSource = readFileSync(resolve(here, "../src/lib/api.ts"), "utf8");
const pageSource = readFileSync(resolve(here, "../src/pages/Integrations.tsx"), "utf8");

assert.match(apiSource, /createConnectionGrant/);
assert.match(apiSource, /owner_display_name/);
assert.match(apiSource, /can_share/);
assert.match(pageSource, /Shared by/);
assert.match(pageSource, /connection\.can_share/);
assert.match(pageSource, /api\.integrations\.createConnectionGrant/);
