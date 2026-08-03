#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const tourSource = await readFile(
  new URL("../src/components/OnboardingTour.tsx", import.meta.url),
  "utf8",
);
const engineSource = await readFile(
  new URL("../src/components/SpotlightTour.tsx", import.meta.url),
  "utf8",
);
const layoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);

test("workspace-only tour steps declare the mode they need", () => {
  // Regression: the chat/workspace sidebar split left nav targets unmounted in
  // chat mode, so the tour rendered centered tooltips pointing at nothing.
  for (const key of [
    "nav-dashboard",
    "nav-tasks",
    "nav-workspaces",
    "nav-knowledge",
    "nav-team",
    "configure-menu",
    "nav-agents",
    "nav-skills",
    "nav-integrations",
  ]) {
    const stepBlock = new RegExp(
      `\\{[^{}]*data-tour='${key}'[^{}]*mode:\\s*"workspace"[^{}]*\\}`,
      "s",
    );
    assert.match(tourSource, stepBlock, `step ${key} must declare mode: "workspace"`);
  }
});

test("configure submenu steps ask the sidebar to open the menu", () => {
  // Agents/Skills/Integrations only mount while Configure is expanded.
  for (const key of ["nav-agents", "nav-skills", "nav-integrations"]) {
    const stepBlock = new RegExp(
      `\\{[^{}]*data-tour='${key}'[^{}]*openConfigure:\\s*true[^{}]*\\}`,
      "s",
    );
    assert.match(tourSource, stepBlock, `step ${key} must set openConfigure: true`);
  }
  assert.match(engineSource, /manor:tour-configure/);
  assert.match(layoutSource, /addEventListener\("manor:tour-configure"/);
  // Every step target must exist as a data-tour anchor in the layout or chat UI.
  for (const key of ["nav-dashboard", "nav-tasks", "nav-integrations"]) {
    assert.match(
      layoutSource,
      new RegExp(`tourKey: "${key}"`),
      `sidebar must expose tourKey ${key}`,
    );
  }
});

test("chat step falls back to the on-page composer selector", () => {
  assert.match(tourSource, /\[data-tour='chat-input'\], \.chat-composer/);
});

test("tour requests mode switches and the app shell listens", () => {
  assert.match(engineSource, /manor:tour-mode/);
  assert.match(layoutSource, /addEventListener\("manor:tour-mode"/);
});

test("missing targets are skipped instead of showing an unanchored tooltip", () => {
  assert.match(engineSource, /MAX_TARGET_ATTEMPTS/);
  assert.match(engineSource, /skipMissingStep/);
  // Only visible elements may be measured — selectors can match hidden duplicates.
  assert.match(engineSource, /getClientRects\(\)\.length > 0/);
});

test("help can replay the product onboarding tour", () => {
  assert.match(tourSource, /startEvent="manor:start-tour"/);
  assert.match(layoutSource, /dispatchEvent\(new Event\("manor:start-tour"\)\)/);
});
