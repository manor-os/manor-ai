import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(webRoot, "src/lib/manualSkillRefs.ts");
const source = await readFile(sourcePath, "utf8");
const embeddedChatSource = await readFile(
  path.join(webRoot, "src/components/EmbeddedChat.tsx"),
  "utf8",
);
const workspaceChatSource = await readFile(
  path.join(webRoot, "src/components/WorkspaceChat.tsx"),
  "utf8",
);
const floatingChatSource = await readFile(
  path.join(webRoot, "src/components/FloatingChat.tsx"),
  "utf8",
);
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
  fileName: sourcePath,
}).outputText;
const skillRefs = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

test("builtin slug resolution cannot be shadowed by an entity skill", () => {
  const availableSkills = [
    { id: "entity-id", slug: "same-slug", entity_id: "entity-1" },
    { id: "builtin-id", slug: "same-slug", entity_id: null },
  ];

  assert.deepEqual(
    skillRefs.resolveManualSkillReferenceIds(
      [{ kind: "slug", value: "same-slug", source: "builtin" }],
      availableSkills,
    ),
    [{ kind: "id", value: "builtin-id" }],
  );
  assert.deepEqual(
    skillRefs.resolveManualSkillReferenceIds(
      [{ kind: "slug", value: "same-slug", source: "entity" }],
      availableSkills,
    ),
    [{ kind: "id", value: "entity-id" }],
  );
});

test("legacy compatibility contains only unique database IDs", () => {
  assert.deepEqual(
    skillRefs.legacyManualSkillIds([
      { kind: "id", value: "skill-1" },
      { kind: "slug", value: "portable-skill" },
      { kind: "id", value: "skill-1" },
      { kind: "id", value: "skill-2" },
    ]),
    ["skill-1", "skill-2"],
  );
});

test("missing source-scoped skill fails before the chat request", () => {
  assert.throws(
    () =>
      skillRefs.resolveManualSkillReferenceIds(
        [{ kind: "slug", value: "missing", source: "builtin" }],
        [],
      ),
    /Skill not found or not available: missing/,
  );
});

test("manual Skill ID resolution blocks a second send while lookup is pending", () => {
  const guard = embeddedChatSource.indexOf(
    "if (sendPreflightInFlightRef.current) return false;",
  );
  const lock = embeddedChatSource.indexOf(
    "sendPreflightInFlightRef.current = true;",
    guard,
  );
  const lookup = embeddedChatSource.indexOf(
    "await queryClient.fetchQuery",
    lock,
  );
  const handoff = embeddedChatSource.indexOf(
    "const completedSessionKey = await startStream(",
    lookup,
  );
  const release = embeddedChatSource.indexOf(
    "releaseSendPreflight();",
    handoff,
  );

  assert.ok(guard >= 0);
  assert.ok(lock > guard);
  assert.ok(lookup > lock);
  assert.ok(handoff > lookup);
  assert.ok(release > handoff);
});

test("workspace chat surfaces resolve source-scoped slug refs before sending", () => {
  for (const chatSource of [workspaceChatSource, floatingChatSource]) {
    assert.match(chatSource, /resolveManualSkillReferenceIds/);
    assert.match(chatSource, /chat-manual-skill-resolution/);
    assert.match(chatSource, /api\.skills\.list\(\{ include_platform: true \}\)/);
  }
});
