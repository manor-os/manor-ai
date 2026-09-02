import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const readWebSource = (path) => readFile(new URL(`../src/${path}`, import.meta.url), "utf8");

const queueBundle = await build({
  stdin: {
    contents: [
      'export { allocateEditorSaveIntent, enqueueAuxiliarySave } from "../src/lib/auxiliarySaveQueue.ts";',
      'export { authEntityKey, authPrincipalKey } from "../src/lib/authToken.ts";',
    ].join("\n"),
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const queueModuleUrl = `data:text/javascript;base64,${Buffer.from(queueBundle.outputFiles[0].text).toString("base64")}`;
const {
  allocateEditorSaveIntent,
  authEntityKey,
  authPrincipalKey,
  enqueueAuxiliarySave,
} = await import(queueModuleUrl);

const authTokenWithClaims = (claims) => (
  `header.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.signature`
);

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    },
  };
}

async function waitForMicrotasks(predicate, message) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  assert.ok(predicate(), message);
}

test("auth queue scope follows filesystem ownership instead of token context", () => {
  const identity = {
    sub: "user-1",
    entity_id: "entity-a",
    role: "owner",
    token_version: 3,
    typ: "access",
    amr: ["password"],
  };
  const original = authTokenWithClaims({ ...identity, iat: 10, exp: 20 });
  const refreshed = authTokenWithClaims({ ...identity, iat: 15, exp: 25 });
  const steppedUp = authTokenWithClaims({
    ...identity,
    role: "admin",
    token_version: 4,
    amr: ["password", "mfa"],
    iat: 15,
    exp: 25,
  });
  const switchedUser = authTokenWithClaims({ ...identity, sub: "user-2", iat: 15, exp: 25 });
  const switched = authTokenWithClaims({ ...identity, entity_id: "entity-b", iat: 15, exp: 25 });

  assert.equal(authEntityKey(original), authEntityKey(refreshed));
  assert.equal(authEntityKey(original), authEntityKey(steppedUp));
  assert.equal(authEntityKey(original), authEntityKey(switchedUser));
  assert.notEqual(authEntityKey(original), authEntityKey(switched));
  assert.equal(authPrincipalKey(original), authPrincipalKey(steppedUp));
  assert.notEqual(authPrincipalKey(original), authPrincipalKey(switchedUser));
});

test("auxiliary writes remain ordered when a workspace generation changes", async () => {
  const events = [];
  let releaseFirst = () => {};
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });

  const firstSave = enqueueAuxiliarySave("entity-a", "project/shared.ts", async ({ isLatest }) => {
    events.push("first:start");
    await firstGate;
    events.push(`first:end:${isLatest()}`);
    return true;
  });
  await waitForMicrotasks(
    () => events.includes("first:start"),
    "the first ordered save should start",
  );
  const latestSave = enqueueAuxiliarySave("entity-a", "project/shared.ts", async ({ isLatest }) => {
    events.push(`latest:start:${isLatest()}`);
    events.push("latest:end");
    return true;
  });

  assert.deepEqual(events, ["first:start"]);
  releaseFirst();
  assert.deepEqual(await Promise.all([firstSave, latestSave]), [true, true]);
  assert.deepEqual(events, ["first:start", "first:end:false", "latest:start:true", "latest:end"]);
});

test("a failed auxiliary write does not block the next save for that path", async () => {
  const failedSave = enqueueAuxiliarySave(
    "entity-a",
    "project/retry.ts",
    async () => { throw new Error("save failed"); },
  );
  const nextSave = enqueueAuxiliarySave("entity-a", "project/retry.ts", async () => true);
  await assert.rejects(
    failedSave,
    /save failed/,
  );
  assert.equal(await nextSave, true);
});

test("the same path saves independently for different entities", async () => {
  const events = [];
  let releaseFirst = () => {};
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });

  const firstSave = enqueueAuxiliarySave("entity-a", "project/shared.ts", async () => {
    events.push("entity-a:start");
    await firstGate;
    events.push("entity-a:end");
    return true;
  });
  const secondSave = enqueueAuxiliarySave("entity-b", "project/shared.ts", async () => {
    events.push("entity-b:start");
    return true;
  });

  await waitForMicrotasks(
    () => events.includes("entity-a:start") && events.includes("entity-b:start"),
    "independent entity queues should both start",
  );
  assert.equal(await secondSave, true);
  assert.deepEqual(events, ["entity-a:start", "entity-b:start"]);
  releaseFirst();
  assert.equal(await firstSave, true);
});

test("a permanently stalled save times out and releases the ordered write queue", async () => {
  const events = [];

  const stalledSave = enqueueAuxiliarySave(
    "entity-a",
    "project/stalled.ts",
    async ({ signal }) => {
      events.push("first:start");
      await new Promise(() => {});
      return signal.aborted;
    },
    { timeoutMs: 10, onTimeout: () => events.push("first:timeout") },
  );

  assert.equal(await stalledSave, false);
  assert.deepEqual(events, ["first:start", "first:timeout"]);

  const nextSave = enqueueAuxiliarySave(
    "entity-a",
    "project/stalled.ts",
    async ({ intent }) => {
      events.push("next:start");
      events.push(`next:sequence:${intent.sequence}`);
      return true;
    },
    { timeoutMs: 100 },
  );

  assert.equal(await nextSave, true);
  assert.deepEqual(events.slice(0, 3), ["first:start", "first:timeout", "next:start"]);
  assert.match(events[3], /^next:sequence:\d+$/);
});

test("a superseded stalled save does not emit an obsolete timeout notification", async () => {
  const events = [];
  let releaseFirst = () => {};
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });

  const firstSave = enqueueAuxiliarySave(
    "entity-a",
    "project/superseded.ts",
    async () => {
      await firstGate;
      return true;
    },
    { timeoutMs: 10, onTimeout: () => events.push("first:timeout") },
  );
  const latestSave = enqueueAuxiliarySave(
    "entity-a",
    "project/superseded.ts",
    async () => true,
    { timeoutMs: 20, onTimeout: () => events.push("latest:timeout") },
  );

  assert.equal(await firstSave, false);
  releaseFirst();
  assert.equal(await latestSave, true);
  assert.deepEqual(events, []);
});

test("same-session write intents increase before a timed-out request can finish", async () => {
  const intents = [];
  const firstSave = enqueueAuxiliarySave(
    "entity-a",
    "project/fenced.ts",
    async ({ intent }) => {
      intents.push(intent);
      await new Promise(() => {});
      return true;
    },
    { timeoutMs: 10 },
  );
  assert.equal(await firstSave, false);

  const secondSave = enqueueAuxiliarySave(
    "entity-a",
    "project/fenced.ts",
    async ({ intent }) => {
      intents.push(intent);
      return true;
    },
  );
  assert.equal(await secondSave, true);
  assert.equal(intents[0].sessionId, intents[1].sessionId);
  assert.ok(intents[1].sequence > intents[0].sequence);
});

test("main and auxiliary editor saves allocate from the same intent sequence", async () => {
  const mainIntent = await allocateEditorSaveIntent();
  let auxiliaryIntent;
  await enqueueAuxiliarySave("entity-a", "project/shared.html", async ({ intent }) => {
    auxiliaryIntent = intent;
    return true;
  });

  assert.equal(mainIntent.sessionId, auxiliaryIntent.sessionId);
  assert.ok(auxiliaryIntent.sequence > mainIntent.sequence);
});

test("duplicated tabs allocate distinct sequences for their inherited save session", async () => {
  const savedDescriptors = Object.fromEntries(
    ["localStorage", "sessionStorage", "navigator"].map((key) => [
      key,
      Object.getOwnPropertyDescriptor(globalThis, key),
    ]),
  );
  const sharedLocalStorage = memoryStorage();
  const inheritedSession = {
    "manor:editor-save-session": "inherited-editor-session",
    "manor:editor-save-sequence": "12",
  };
  let lockTail = Promise.resolve();
  const locks = {
    request(_name, callback) {
      const result = lockTail.then(callback);
      lockTail = result.catch(() => undefined);
      return result;
    },
  };

  try {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: sharedLocalStorage,
    });
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { locks },
    });
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: memoryStorage(inheritedSession),
    });
    const firstTab = await import(`${queueModuleUrl}#duplicated-tab-a`);

    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: memoryStorage(inheritedSession),
    });
    const secondTab = await import(`${queueModuleUrl}#duplicated-tab-b`);
    const intents = [];

    await Promise.all([
      firstTab.enqueueAuxiliarySave("entity-a", "project/duplicated.ts", async ({ intent }) => {
        intents.push(intent);
        return true;
      }),
      secondTab.enqueueAuxiliarySave("entity-a", "project/duplicated.ts", async ({ intent }) => {
        intents.push(intent);
        return true;
      }),
    ]);

    assert.equal(intents.length, 2);
    assert.equal(intents[0].sessionId, intents[1].sessionId);
    assert.equal(new Set(intents.map((intent) => intent.sequence)).size, 2);
    assert.ok(Math.min(...intents.map((intent) => intent.sequence)) > 12);
  } finally {
    for (const [key, descriptor] of Object.entries(savedDescriptors)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("independently opened tabs share one browser save session", async () => {
  const savedDescriptors = Object.fromEntries(
    ["localStorage", "sessionStorage", "navigator"].map((key) => [
      key,
      Object.getOwnPropertyDescriptor(globalThis, key),
    ]),
  );
  const sharedLocalStorage = memoryStorage();
  let lockTail = Promise.resolve();
  const locks = {
    request(_name, callback) {
      const result = lockTail.then(callback);
      lockTail = result.catch(() => undefined);
      return result;
    },
  };

  try {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: sharedLocalStorage,
    });
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { locks },
    });
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: memoryStorage({ "manor:editor-save-session": "independent-a" }),
    });
    const firstTab = await import(`${queueModuleUrl}#independent-tab-a`);

    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: memoryStorage({ "manor:editor-save-session": "independent-b" }),
    });
    const secondTab = await import(`${queueModuleUrl}#independent-tab-b`);

    const [firstIntent, secondIntent] = await Promise.all([
      firstTab.allocateEditorSaveIntent(),
      secondTab.allocateEditorSaveIntent(),
    ]);

    assert.equal(firstIntent.sessionId, secondIntent.sessionId);
    assert.notEqual(firstIntent.sequence, secondIntent.sequence);
  } finally {
    for (const [key, descriptor] of Object.entries(savedDescriptors)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("shared pane splitter supports pointer, keyboard, reset, and persisted sizing", async () => {
  const source = await readWebSource("components/ui/ResizablePaneGroup.tsx");

  assert.match(source, /role="separator"/);
  assert.match(source, /aria-orientation="vertical"/);
  assert.match(source, /setPointerCapture/);
  assert.match(source, /ArrowLeft/);
  assert.match(source, /ArrowRight/);
  assert.match(source, /onDoubleClick=\{resetSizes\}/);
  assert.match(source, /window\.localStorage\.setItem/);
});

test("project tree lazily lists folders and opens only code-like files", async () => {
  const source = await readWebSource("components/code/CodeProjectExplorer.tsx");

  assert.match(source, /api\.fs\.list\(path\)/);
  assert.match(source, /enabled: expanded/);
  assert.match(source, /role="tree"/);
  assert.match(source, /role="treeitem"/);
  assert.match(source, /isCodeLikeFile/);
  assert.doesNotMatch(source, />\s*EXPLORER\s*</);
});

test("code workspace keeps linked tabs editable, autosaved, and recoverable", async () => {
  const source = await readWebSource("lib/useCodeProjectWorkspace.ts");
  const apiSource = await readWebSource("lib/api.ts");
  const authSource = await readWebSource("lib/authToken.ts");
  const queueSource = await readWebSource("lib/auxiliarySaveQueue.ts");

  assert.match(source, /const authContext = captureAuxiliaryAuthContext\(\)/);
  assert.match(source, /api\.fs\.read\(path, authContext\.authToken\)/);
  assert.match(source, /authEntityKey\(authToken\)/);
  assert.match(source, /authPrincipalKey\(authToken\)/);
  assert.match(source, /currentContext\.principalKey === boundContext\.principalKey/);
  assert.match(source, /return currentContext\.principalKey === boundContext\.principalKey \? currentContext : null/);
  assert.doesNotMatch(source, /\? currentContext : boundContext/);
  assert.match(source, /account or workspace changed/i);
  assert.match(source, /activeAuthPrincipalKey/);
  assert.match(source, /window\.addEventListener\("storage", refreshAuthScope\)/);
  assert.match(
    source,
    /generation !== generationRef\.current[\s\S]*?!refreshAuxiliaryAuthContext\(authContext\)/,
  );
  assert.match(
    source,
    /if \(!refreshAuxiliaryAuthContext\(pending\.authContext\)\)[\s\S]*?onAuxiliarySaveError\(path, AUXILIARY_AUTH_CHANGED_MESSAGE\)/,
  );
  assert.match(source, /authContext: AuxiliaryAuthContext/);
  assert.match(source, /api\.fs\.write\(path, contentToSave, \{/);
  assert.match(source, /authToken: authContext\.authToken/);
  assert.match(source, /saveIntent: intent/);
  assert.match(source, /signal/);
  assert.match(source, /AUXILIARY_AUTOSAVE_DELAY = 3000/);
  assert.match(source, /AUXILIARY_SAVE_STALL_TIMEOUT = 30_000/);
  assert.match(source, /saveAll/);
  assert.match(source, /enqueueAuxiliarySave\([\s\S]*?authContext\.entityKey,[\s\S]*?path/);
  assert.doesNotMatch(source, /saveQueuesRef/);
  assert.match(source, /pending\.flush\(\)[\s\S]*?generationRef\.current \+= 1/);
  assert.doesNotMatch(source, /if \(generation !== generationRef\.current\) return false/);
  assert.match(
    source,
    /await api\.fs\.write\(path, contentToSave,[\s\S]*?if \(generation === generationRef\.current/,
  );
  assert.match(source, /onAuxiliarySaveError\(path, message\)/);
  assert.match(source, /timeoutMs: AUXILIARY_SAVE_STALL_TIMEOUT/);
  assert.match(source, /readError: true/);
  assert.match(source, /tab\.loading \|\| tab\.readError/);
  assert.match(source, /status: "error"/);
  assert.match(source, /previewTextOverrides/);
  assert.match(
    apiSource,
    /write: \(path: string, content: string, options: FilesystemWriteOptions = \{\}\)[\s\S]*?options\.authToken,/,
  );
  assert.match(apiSource, /const tokenIsCurrent = \(\) => token === getAuthToken\(\)/);
  assert.match(
    apiSource,
    /if \([\s\S]*?res\.status === 401 &&[\s\S]*?authTokenOverride === undefined &&[\s\S]*?Boolean\(token\) &&[\s\S]*?tokenIsCurrent\(\)[\s\S]*?\) handleSessionExpired\(path\);/,
  );
  assert.match(authSource, /export function authEntityKey/);
  assert.match(authSource, /claims\.entity_id/);
  assert.match(queueSource, /auxiliarySessionStorage\?\.getItem\(AUXILIARY_SAVE_SESSION_STORAGE_KEY\)/);
  assert.match(queueSource, /readStoredSequence\(auxiliarySharedSequenceStorage\)/);
  assert.match(queueSource, /navigator\?\.locks/);
  assert.match(queueSource, /export async function allocateEditorSaveIntent/);
  assert.match(queueSource, /return nextAuxiliarySaveIntent\(\)/);
  assert.match(queueSource, /auxiliarySharedSequenceStorage\.getItem\([\s\S]*?AUXILIARY_SAVE_SESSION_STORAGE_KEY/);
});

test("document code mode composes files, tabs, editor, and preview as resizable panes", async () => {
  const source = await readWebSource("pages/DocEditor.tsx");

  assert.match(source, /useCodeProjectWorkspace/);
  assert.match(source, /<CodeProjectExplorer/);
  assert.match(source, /<ResizablePaneGroup/);
  assert.match(source, /id: "files"/);
  assert.match(source, /id: "source"/);
  assert.match(source, /id: "preview"/);
  assert.match(source, /role="tablist"/);
  assert.match(source, /codeWorkspace\.previewTextOverrides/);
  assert.match(source, /Preview ready/);
  assert.match(source, /onAuxiliarySaveError: handleAuxiliarySaveError/);
  assert.match(source, /allocateEditorSaveIntent\(\)/);
  assert.match(
    source,
    /api\.documents\.saveContent\(\s*documentId,\s*text,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(
    source,
    /api\.documents\.replaceFile\(\s*[^,]+,\s*file,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(source, /showSaveError\(t\("page\.blueprint_detail\.save_failed"\), `\$\{path\}: \$\{message\}`\)/);
});

test("IDE workspace keeps the tree dark and stacks panes without horizontal overflow on narrow screens", async () => {
  const source = await readWebSource("components/code/CodeProjectWorkspace.css");

  assert.match(source, /\.doc-editor-project-pane,[\s\S]*\.code-project-explorer[\s\S]*background: #1c1917/);
  assert.match(source, /@media \(max-width: 900px\)/);
  assert.match(source, /grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(source, /\.doc-editor-ide-workspace \.resizable-pane-group__handle \{[\s\S]*display: none/);
});

test("HTML preview can resolve unsaved linked text from the code workspace", async () => {
  const source = await readWebSource("lib/useHtmlPreviewDocument.ts");

  assert.match(source, /textOverrides: Record<string, string>/);
  assert.match(source, /hasOwnProperty\.call\(textOverrides, path\)/);
  assert.match(source, /readPreviewAsset\(path, textOverrides\)/);
  assert.match(source, /overrideKey/);
});
