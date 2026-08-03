#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const tasksSource = await readFile(
  new URL("../src/pages/Tasks.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);

test("task board preferences synchronize through the authenticated user API", () => {
  assert.match(apiSource, /getBoardPreferences:[\s\S]*"\/tasks\/board-preferences"/);
  assert.match(apiSource, /updateBoardPreferences:[\s\S]*method: "PUT"/);
  assert.match(tasksSource, /queryKey: \["task-board-preferences", currentUser\?\.id\]/);
  assert.match(tasksSource, /api\.tasks\.getBoardPreferences\(\)/);
  assert.match(tasksSource, /api\.tasks\.updateBoardPreferences\(\{/);
  assert.match(tasksSource, /taskBoardPreferencesHydrated/);
});

test("task board browser fallback is isolated by user and migrates legacy values", () => {
  assert.match(tasksSource, /return `\$\{baseKey\}:\$\{userId\}`/);
  assert.match(tasksSource, /loadLocalTaskBoardPreferences\(currentUser\?\.id\)/);
  assert.match(tasksSource, /window\.localStorage\.removeItem\(key\)/);
  assert.match(tasksSource, /Keep the user-scoped browser copy as an offline fallback/);
});
