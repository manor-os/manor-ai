import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => readFile(path.join(webRoot, relativePath), "utf8");

const [
  composer,
  integrations,
  apiClient,
  embedded,
  floating,
  workspace,
  english,
  chinese,
] =
  await Promise.all([
    read("src/components/ChatInputFooter.tsx"),
    read("src/pages/Integrations.tsx"),
    read("src/lib/api.ts"),
    read("src/components/EmbeddedChat.tsx"),
    read("src/components/FloatingChat.tsx"),
    read("src/components/WorkspaceChat.tsx"),
    read("src/lib/i18n/en.ts"),
    read("src/lib/i18n/zh.ts"),
  ]);

test("chat composer leaves automatic computer routing invisible", () => {
  assert.doesNotMatch(composer, /api\.workers\.list\(\{ kind: "custom_http" \}\)/);
  assert.doesNotMatch(composer, /chat-composer-computer-status/);
  assert.doesNotMatch(composer, /computer_status/);
  assert.doesNotMatch(composer, /chat-composer-machine-select/);
  assert.doesNotMatch(composer, /CLOUD_LOCAL_WORKER_TARGET/);
  assert.doesNotMatch(composer, /selectedLocalWorker/);
  assert.doesNotMatch(composer, /localWorkerContextKey/);
});

test("legacy chat paths retain optional immutable worker-id compatibility", () => {
  assert.match(apiClient, /form\.append\("local_worker_id", opts\.localWorkerId\)/);
  for (const source of [embedded, floating, workspace]) {
    assert.match(source, /localWorkerId:/);
  }
});

test("configured computers support clean unique display names and rename UI", () => {
  assert.match(integrations, /key: "rename" as const/);
  assert.match(integrations, /<CliWorkerRenameModal/);
  assert.match(integrations, /api\.workers\.rename\(worker!\.id, cleanName\)/);
  assert.doesNotMatch(
    integrations,
    /display_name: `CLI · \$\{machineLabel\.trim\(\)/,
  );
});

test("local computers stay in the main integrations surface", () => {
  assert.match(integrations, /<LocalCodeReadinessPanel/);
  assert.match(integrations, /type Tab = "agents" \| "channels"/);
  assert.doesNotMatch(integrations, /AgentEnginesPanel/);
  assert.doesNotMatch(integrations, /key: "engines"/);
});

test("active computers use the semantic success dot", () => {
  assert.match(
    integrations,
    /cli_worker_status_active[\s\S]*?color: "var\(--success, #437f6b\)"/,
  );
});

test("computer navigation and readiness counts describe computers", () => {
  assert.match(english, /"page\.integrations\.workers_count": "Computers \(\{count\}\)"/);
  assert.match(english, /"page\.integrations\.category_local_tools": "Computers"/);
  assert.match(chinese, /"page\.integrations\.workers_count": "电脑（\{count\}）"/);
  assert.match(integrations, /readyCount=\{activeWorkers\.length\}/);
  assert.match(integrations, /totalCount=\{configuredWorkers\.length\}/);
  assert.match(integrations, /countLabel=\{t\("page\.integrations\.online"\)\}/);
});
