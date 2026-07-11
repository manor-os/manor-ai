#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const dashboardSource = await readFile(
  new URL("../src/pages/Dashboard.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const cssSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);

test("dashboard customization persists widget visibility and order", () => {
  assert.match(dashboardSource, /function DashboardInlineEditor/);
  assert.match(dashboardSource, /function DashboardWidgetFrame/);
  assert.doesNotMatch(dashboardSource, /DashboardCustomizer|<Modal/);
  assert.match(dashboardSource, /api\.dashboard\.layout\(\)/);
  assert.match(dashboardSource, /api\.dashboard\.updateLayout\(draftWidgets\)/);
  assert.match(dashboardSource, /api\.dashboard\.suggestLayout\(layoutPrompt\.trim\(\), draftWidgets\)/);
  assert.match(apiSource, /\/dashboard\/layout/);
  assert.match(apiSource, /\/dashboard\/layout\/suggest/);
  assert.match(dashboardSource, /page\.dashboard\.restore_defaults/);
});

test("dashboard customization supports accessible and responsive ordering controls", () => {
  assert.match(dashboardSource, /page\.dashboard\.move_up/);
  assert.match(dashboardSource, /page\.dashboard\.move_down/);
  assert.match(dashboardSource, /page\.dashboard\.hide_widget/);
  assert.match(dashboardSource, /dashboard-editable-widget-drag/);
  assert.match(cssSource, /\.dashboard-inline-editor/);
  assert.match(cssSource, /\.dashboard-editable-widget-actions/);
  assert.match(cssSource, /@media \(max-width: 640px\)/);
  assert.match(cssSource, /html\[data-theme="dark"\] \.dashboard-editable-widget/);
});
