#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const statsPanelSource = await readFile(
  new URL("../src/components/workspaces/WorkspaceStatsPanel.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const statsHelpersSource = await readFile(
  new URL("../src/lib/workspaceStats.ts", import.meta.url),
  "utf8",
);
const statsQuickAccessSource = await readFile(
  new URL("../src/components/workspaces/WorkspaceStatsQuickAccess.tsx", import.meta.url),
  "utf8",
);
const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const anchoredPopoverSource = await readFile(
  new URL("../src/components/ui/AnchoredPopover.tsx", import.meta.url),
  "utf8",
);
const tabSwitcherSource = await readFile(
  new URL("../src/components/ui/TabSwitcher.tsx", import.meta.url),
  "utf8",
);
const englishTranslationsSource = await readFile(
  new URL("../src/lib/i18n/en.ts", import.meta.url),
  "utf8",
);
const chineseTranslationsSource = await readFile(
  new URL("../src/lib/i18n/zh.ts", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);

test("healthy workspace stats do not show a Fresh badge", () => {
  assert.match(statsHelpersSource, /if \(status === "fresh"\) return null/);
  assert.doesNotMatch(statsPanelSource, /page\.workspace_stats\.fresh/);
  assert.match(statsPanelSource, /meta=\{attentionStatus \? \(/);
});

test("workspace stat cards still surface states that need attention", () => {
  assert.match(statsHelpersSource, /status === "stale"/);
  assert.match(statsHelpersSource, /status === "collection_error"/);
  assert.match(statsHelpersSource, /status === "paused"/);
  assert.match(statsHelpersSource, /page\.workspace_stats\.no_data/);
  assert.match(statsPanelSource, /<StatusBadge type=\{attentionStatus\.type\} dot>/);
});

test("workspace chat exposes scalable workspace quick access through a shared anchored popover", () => {
  assert.match(workspaceChatSource, /!threadRef && \(/);
  assert.match(workspaceChatSource, /<WorkspaceStatsQuickAccess/);
  assert.match(statsQuickAccessSource, /queryKey: \["workspace-stats", workspaceId\]/);
  assert.match(statsQuickAccessSource, /<AnchoredPopover/);
  assert.match(statsQuickAccessSource, /persistentOnWide/);
  assert.match(statsQuickAccessSource, /persistentBreakpoint=\{1280\}/);
  assert.match(statsQuickAccessSource, /persistentPlacement="below-anchor"/);
  assert.match(statsQuickAccessSource, /persistentInlineInset=\{24\}/);
  assert.match(statsQuickAccessSource, /<IconViewOptions size=\{18\} \/>/);
  assert.doesNotMatch(statsQuickAccessSource, /IconDashboard/);
  assert.match(statsQuickAccessSource, /icon: <IconTrendingUp size=\{14\} \/>/);
  assert.doesNotMatch(statsQuickAccessSource, /openDetail\(\{/);
  assert.match(statsQuickAccessSource, /data-testid="workspace-chat-stats-trigger"/);
  assert.doesNotMatch(statsQuickAccessSource, /workspace-chat-stats-trigger-dot/);
  assert.doesNotMatch(statsQuickAccessSource, /useNavigate/);
  assert.match(statsQuickAccessSource, /setAdding\(true\)/);
  assert.doesNotMatch(statsQuickAccessSource, /IconChevronDown/);
  assert.doesNotMatch(statsQuickAccessSource, /workspace-chat-stats-trigger-label/);
  assert.match(statsQuickAccessSource, /title=\{t\("component\.workspace_chat\.workspace_quick_access"\)\}/);
  assert.doesNotMatch(statsQuickAccessSource, /workspace-chat-stats-trigger-count/);
  assert.match(anchoredPopoverSource, /createPortal/);
  assert.match(anchoredPopoverSource, /event\.key !== "Escape"/);
  assert.match(anchoredPopoverSource, /document\.addEventListener\("mousedown", handleOutsideClick\)/);
  assert.match(anchoredPopoverSource, /if \(persistent\) return/);
  assert.match(anchoredPopoverSource, /anchored-popover-panel--persistent/);
  assert.match(anchoredPopoverSource, /data-popover-persistent/);
  assert.match(anchoredPopoverSource, /persistentPlacement === "below-anchor" \? rect\.bottom \+ gap : rect\.top/);
  assert.match(anchoredPopoverSource, /rect\.right - effectiveWidth - persistentInlineInset/);
  assert.doesNotMatch(stylesSource, /margin-right: 376px/);
  assert.doesNotMatch(statsQuickAccessSource, /workspace-chat-quick-access-heading/);
});

test("workspace quick access places status indicators on their owning tabs", () => {
  assert.match(tabSwitcherSource, /status\?: "warning" \| "danger"/);
  assert.match(tabSwitcherSource, /className=\{`inline-block h-1\.5 w-1\.5 shrink-0 rounded-full/);
  assert.match(tabSwitcherSource, /aria-label=\{tab\.statusLabel/);
  assert.match(statsQuickAccessSource, /status: metricsTabStatus/);
  assert.match(statsQuickAccessSource, /status: tasksTabStatus/);
  assert.match(statsQuickAccessSource, /taskStatusNeedsAttention/);
  assert.match(statsQuickAccessSource, /enabled: Boolean\(workspaceId\),[\s\S]*staleTime: 30_000/);
});

test("workspace quick access includes workspace-scoped generated files and tasks", () => {
  assert.match(statsQuickAccessSource, /type WorkspaceQuickTab = "metrics" \| "files" \| "tasks"/);
  assert.match(statsQuickAccessSource, /api\.workspaces\.documents\(workspaceId\)/);
  assert.match(statsQuickAccessSource, /api\.tasks\.list\(\{ workspace_id: workspaceId, limit: 20 \}\)/);
  assert.match(statsQuickAccessSource, /is_workspace_file_bucket/);
  assert.match(statsQuickAccessSource, /to=\{`\/viewer\/\$\{document\.id\}`\}/);
  assert.match(statsQuickAccessSource, /to=\{`\/tasks\/\$\{task\.id\}`\}/);
  assert.match(statsQuickAccessSource, /returnSearch\.set\("workspace", workspaceId\)/);
  assert.match(statsQuickAccessSource, /return `\$\{location\.pathname\}\$\{serializedSearch \? `\?\$\{serializedSearch\}` : ""\}\$\{location\.hash\}`/);
  assert.match(statsQuickAccessSource, /state=\{\{ returnTo, chatReturnTo: returnTo \}\}/);
  assert.match(statsQuickAccessSource, /preserveReturnToInHistory\(returnTo\)/);
  assert.match(statsQuickAccessSource, /<TabSwitcher/);
  assert.match(statsQuickAccessSource, /label: t\("component\.workspace_chat\.tasks"\)/);
  assert.match(englishTranslationsSource, /"component\.workspace_chat\.tasks": "Tasks"/);
  assert.match(chineseTranslationsSource, /"component\.workspace_chat\.workspace_folders": "Workspace 文件夹"/);
  assert.match(stylesSource, /\.workspace-chat-stats-popover-content \{[\s\S]*max-height: min\(560px, calc\(100dvh - 32px\)\)/);
  assert.match(stylesSource, /\.workspace-chat-quick-folder-list,[\s\S]*\.workspace-chat-quick-task-list \{[\s\S]*overflow-y: auto/);
  assert.match(stylesSource, /\.workspace-chat-quick-task-copy > span \{[\s\S]*-webkit-line-clamp: 2/);
});

test("workspace chat stats are a personal configurable module", () => {
  assert.match(apiSource, /WorkspaceStatsQuickView/);
  assert.match(apiSource, /quickView: \(wsId: string\)/);
  assert.match(apiSource, /updateQuickView: \(wsId: string/);
  assert.match(statsQuickAccessSource, /queryKey: \["workspace-stats-quick-view", workspaceId\]/);
  assert.match(statsQuickAccessSource, /data-testid="workspace-chat-stats-customize"/);
  assert.match(statsQuickAccessSource, /function orderedStats\(/);
  assert.match(statsQuickAccessSource, /const toggleStat = \(statId: string\)/);
  assert.match(statsQuickAccessSource, /const moveStat = \(statId: string, direction: -1 \| 1\)/);
  assert.match(statsQuickAccessSource, /IconEyeOff/);
  assert.match(statsQuickAccessSource, /page\.workspace_stats\.add/);
  assert.match(statsQuickAccessSource, /workspace-chat-stats-popover-header-add/);
  assert.match(statsQuickAccessSource, /workspace-chat-stats-popover-header-arrange/);
  assert.doesNotMatch(statsQuickAccessSource, /workspace-chat-stats-popover-menu-row/);
  assert.doesNotMatch(statsQuickAccessSource, /workspace-chat-stat-quick-icon/);
  assert.match(statsQuickAccessSource, /<IconCheck size=\{16\} \/>/);
  assert.match(statsQuickAccessSource, /data-testid="workspace-chat-stats-library"/);
  assert.match(statsQuickAccessSource, /api\.workspaces\.stats\.create\(workspaceId/);
  assert.match(statsQuickAccessSource, /entry\.collector_type !== "integration" \|\| entry\.available_connections\.length > 0/);
  assert.doesNotMatch(statsQuickAccessSource, /className="mono"/);
});

test("workspace measurements use user-facing metric terminology", () => {
  assert.match(englishTranslationsSource, /"page\.workspace_stats\.title": "Metrics"/);
  assert.match(englishTranslationsSource, /"page\.workspace_stats\.add": "Add metric"/);
  assert.match(englishTranslationsSource, /"component\.workspace_chat\.customize_stats": "Arrange metrics"/);
  assert.match(englishTranslationsSource, /"component\.workspace_chat\.no_stats_selected": "No metrics selected"/);
  assert.match(chineseTranslationsSource, /"page\.workspace_stats\.title": "指标"/);
  assert.match(chineseTranslationsSource, /"component\.workspace_chat\.no_stats_selected": "尚未选择指标"/);
});

test("workspace chat stats use the same value and attention formatting as the full panel", () => {
  assert.match(statsPanelSource, /formatWorkspaceStatValue/);
  assert.match(statsPanelSource, /workspaceStatAttentionPresentation/);
  assert.match(statsQuickAccessSource, /formatWorkspaceStatValue/);
  assert.match(statsQuickAccessSource, /workspaceStatAttentionPresentation/);
});

test("workspace chat omits the aggregate metric status while matching nearby typography", () => {
  assert.doesNotMatch(statsQuickAccessSource, /component\.workspace_chat\.stats_(?:attention|status)_summary/);
  assert.doesNotMatch(statsQuickAccessSource, /workspace-chat-stats-(?:attention|status)-summary/);
  assert.doesNotMatch(englishTranslationsSource, /component\.workspace_chat\.stats_(?:attention|status)_summary/);
  assert.doesNotMatch(chineseTranslationsSource, /component\.workspace_chat\.stats_(?:attention|status)_summary/);
  assert.match(stylesSource, /\.anchored-popover-panel \{[\s\S]*font-family: var\(--font-sans\)/);
  assert.match(stylesSource, /\.workspace-chat-stat-quick-copy > strong \{[\s\S]*font-size: 12px;[\s\S]*font-weight: 600/);
  assert.match(stylesSource, /\.workspace-chat-stat-quick-value > strong \{[\s\S]*font-size: 12px;[\s\S]*font-weight: 600/);
});

test("integration stats require and persist an explicit active connection", () => {
  assert.match(apiSource, /WorkspaceStatIntegrationKey = "twitter_x"/);
  assert.match(apiSource, /integration_key: WorkspaceStatIntegrationKey \| null/);
  assert.match(apiSource, /available_connections: Array</);
  assert.match(apiSource, /integration_key: WorkspaceStatIntegrationKey/);
  assert.match(statsPanelSource, /selectedLibrary\?\.available_connections \|\| \[\]/);
  assert.match(statsPanelSource, /selectedLibrary\.collector_type === "integration" && !effectiveConnectionId/);
  assert.match(statsPanelSource, /collector_config: effectiveConnectionId/);
  assert.match(statsPanelSource, /\{ connection_id: effectiveConnectionId \}/);
  assert.match(statsPanelSource, /entry\.available_connections\.length === 1/);
  assert.match(statsPanelSource, /refetchOnMount: "always"/);
});
