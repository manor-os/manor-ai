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
const generatedModuleSource = await readFile(
  new URL("../src/components/dashboard/GeneratedDashboardModule.tsx", import.meta.url),
  "utf8",
);
const englishSource = await readFile(
  new URL("../src/lib/i18n/en.ts", import.meta.url),
  "utf8",
);
const chineseSource = await readFile(
  new URL("../src/lib/i18n/zh.ts", import.meta.url),
  "utf8",
);

test("dashboard customization persists widgets and generated modules", () => {
  assert.match(dashboardSource, /function DashboardInlineEditor/);
  assert.match(dashboardSource, /function DashboardWidgetFrame/);
  assert.doesNotMatch(dashboardSource, /DashboardCustomizer|<Modal/);
  assert.match(dashboardSource, /api\.dashboard\.layout\(\)/);
  assert.match(
    dashboardSource,
    /api\.dashboard\.updateLayout\(layout\.widgets, layout\.modules\)/,
  );
  assert.match(
    dashboardSource,
    /api\.dashboard\s*\n\s*\.suggestLayout\(prompt, widgets, modules, \{/,
  );
  assert.match(dashboardSource, /signal: controller\.signal/);
  assert.match(dashboardSource, /DASHBOARD_AI_REQUEST_TIMEOUT_MS/);
  assert.match(dashboardSource, /showInlineGeneratingPlaceholder/);
  assert.match(dashboardSource, /page\.dashboard\.ai_generating_inline/);
  assert.match(dashboardSource, /page\.dashboard\.stop_generation/);
  assert.match(dashboardSource, /page\.dashboard\.ai_update_timeout/);
  assert.match(cssSource, /\.dashboard-ai-layout-status/);
  assert.match(dashboardSource, /onConfirm=\{saveDraftLayout\}/);
  assert.match(dashboardSource, /saveLayoutMutation\.mutate\(\{/);
  assert.match(dashboardSource, /widgets: draftWidgets/);
  assert.match(dashboardSource, /modules: draftModules/);
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
  assert.match(
    cssSource,
    /\.dashboard-generated-module--wide\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/s,
  );
});

test("dashboard customization uses a compact icon-only page action", () => {
  assert.match(
    dashboardSource,
    /<Tooltip content=\{t\("page\.dashboard\.customize_tooltip"\)\} position="left">/,
  );
  assert.match(
    dashboardSource,
    /className="dashboard-customize-trigger"[\s\S]*aria-label=\{t\("page\.dashboard\.customize"\)\}[\s\S]*<IconSettings size=\{16\} \/>[\s\n]*<\/button>/,
  );
  assert.doesNotMatch(
    dashboardSource,
    /<IconSettings size=\{16\} \/>[\s\S]*\{t\("page\.dashboard\.customize"\)\}[\s\S]*<\/button>/,
  );
  assert.match(englishSource, /"page\.dashboard\.customize_tooltip":/);
  assert.match(chineseSource, /"page\.dashboard\.customize_tooltip":/);
  assert.match(
    cssSource,
    /\.dashboard-customize-trigger\s*\{[^}]*width:\s*34px;[^}]*height:\s*34px;/s,
  );
});

test("saved dashboard modules hide AI conversation controls", () => {
  assert.match(
    generatedModuleSource,
    /\{editing && \(\s*<div className="dashboard-generated-module-actions">/,
  );
  assert.match(
    generatedModuleSource,
    /\{editing && conversationOpen && \(\s*<section/,
  );
});

test("generated module readiness resets before iframe load events", () => {
  assert.match(
    generatedModuleSource,
    /import \{[^}]*useLayoutEffect[^}]*\} from "react";/,
  );
  assert.match(
    generatedModuleSource,
    /useLayoutEffect\(\(\) => \{\s*setFrameReady\(false\);[\s\S]*?\}, \[sourceDocument\]\);/,
  );
  assert.match(generatedModuleSource, /onLoad=\{\(\) => setFrameReady\(true\)\}/);
});

test("generated modules inherit the Manor visual contract", () => {
  assert.match(generatedModuleSource, /const MANOR_MODULE_BASE_CSS = `/);
  assert.match(generatedModuleSource, /--module-row-hover:/);
  assert.match(generatedModuleSource, /--module-space-5:/);
  assert.match(generatedModuleSource, /--module-type-metric:/);
  assert.match(generatedModuleSource, /<IconDashboard size=\{15\} \/>/);
  assert.match(
    cssSource,
    /\.dashboard-generated-module\s*\{[^}]*border-radius:\s*var\(--radius-panel\);[^}]*background:\s*var\(--glass-panel\);/s,
  );
});

test("generated module frames measure content instead of their viewport", () => {
  assert.match(generatedModuleSource, /const measuredHeight = \(node\) =>/);
  assert.match(generatedModuleSource, /measuredHeight\(root\), measuredHeight\(errorNode\)/);
  assert.doesNotMatch(
    generatedModuleSource,
    /height: Math\.ceil\(document\.documentElement\.scrollHeight\)/,
  );
});

test("generated modules own public JSON integrations behind generic egress", () => {
  assert.match(generatedModuleSource, /\| "http_json"/);
  assert.match(generatedModuleSource, /api\.dashboard\.httpData\(/);
  assert.match(apiSource, /"\/dashboard\/http-data"/);
  assert.match(generatedModuleSource, /connect-src 'none'/);
  assert.match(generatedModuleSource, /BLOCKED_JAVASCRIPT[\s\S]*fetch/);
});
