import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspacesSource = readFileSync(
  new URL("../src/pages/Workspaces.tsx", import.meta.url),
  "utf8",
);

test("workspace titles expose a semantic navigation control", () => {
  assert.match(
    workspacesSource,
    /<h3[\s\S]*?<button[\s\S]*?className="workspace-card-title-button"[\s\S]*?navigate\(`\/workspaces\/\$\{ws\.id\}`\)[\s\S]*?\{ws\.name\}[\s\S]*?<\/button>[\s\S]*?<\/h3>/,
  );
});
