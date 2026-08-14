import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const APP_PAGE_HEADER_CONTRACTS = new Map([
  ["Account.tsx", "<PageHeader"],
  ["Activity.tsx", "<PageHeader"],
  ["AdminOAuthClients.tsx", "<PageHeader"],
  ["AgentDashboard.tsx", "<PageHeader"],
  ["AgentDetail.tsx", "<PageHeader"],
  ["Agents.tsx", "<PageHeader"],
  ["Announcements.tsx", "<PageHeader"],
  ["ApiKeys.tsx", "<PageHeader"],
  ["Apps.tsx", "<PageHeader"],
  ["BlueprintDetail.tsx", "<PageHeader"],
  ["BrowserSessions.tsx", "<PageHeader"],
  ["ChatHistory.tsx", "<PageHeader"],
  ["CustomFields.tsx", "<PageHeader"],
  ["Dashboard.tsx", "<PageHeader"],
  ["DiagramStudio.tsx", "<PageHeader"],
  ["DocEditor.tsx", '<PageHeaderTitle variant="editor"'],
  ["FileViewer.tsx", '<PageHeaderTitle variant="editor"'],
  ["Flows.tsx", "<PageHeader"],
  ["GoalExplorer.tsx", "<PageHeader"],
  ["Integrations.tsx", "<PageHeader"],
  ["JobLogs.tsx", "<PageHeader"],
  ["Knowledge.tsx", "<PageHeader"],
  ["Memories.tsx", "<PageHeader"],
  ["MerchantDashboard.tsx", "<PageHeader"],
  ["Messages.tsx", "<PageHeader"],
  ["Notifications.tsx", "<PageHeader"],
  ["PurchaseSuccess.tsx", "<PageHeader"],
  ["QRCode.tsx", "<PageHeader"],
  ["RemoteCoding.tsx", "<PageHeader"],
  ["Reports.tsx", "<PageHeader"],
  ["ScheduledJobs.tsx", "<PageHeader"],
  ["SearchResults.tsx", "<PageHeader"],
  ["Settings.tsx", "<PageHeader"],
  ["SimulationReport.tsx", "<PageHeader"],
  ["Skills.tsx", "<PageHeader"],
  ["TaskCollections.tsx", "<PageHeader"],
  ["TaskDetail.tsx", "<PageHeader"],
  ["Tasks.tsx", "<PageHeader"],
  ["Users.tsx", "<PageHeader"],
  ["VideoEditor.tsx", '<PageHeaderTitle variant="editor"'],
  ["WebhookManager.tsx", "<PageHeader"],
  ["WorkspaceDetail.tsx", "<PageHeader"],
  ["WorkspaceDraftChat.tsx", "<PageHeader"],
  ["Workspaces.tsx", "<PageHeader"],
  ["commerce/CommerceHome.tsx", "<PageHeader"],
  ["team/TeamLayout.tsx", "<PageHeader"],
]);

const ADMIN_PAGE_FILES = [
  "AdminMarketplace.tsx",
  "AdminTeam.tsx",
  "AffiliateDetail.tsx",
  "Affiliates.tsx",
  "Announcements.tsx",
  "AuditLog.tsx",
  "BlueprintReviews.tsx",
  "ClientErrors.tsx",
  "Commissions.tsx",
  "CreditsUsage.tsx",
  "Flags.tsx",
  "Integrations.tsx",
  "Invites.tsx",
  "Models.tsx",
  "OpsDashboard.tsx",
  "Overview.tsx",
  "Plans.tsx",
  "Roles.tsx",
  "SupportTickets.tsx",
  "SystemHealth.tsx",
  "TenantDetail.tsx",
  "TenantsList.tsx",
  "WaitingList.tsx",
];

const PRIMARY_PAGE_BODY_GUTTER_CONTRACTS = new Map([
  ["Dashboard.tsx", 'boxSizing: "border-box",\n          padding: 0,'],
  ["Tasks.tsx", 'className="tasks-page-header" style={{ padding: 0 }}'],
  ["ScheduledJobs.tsx", 'gap: 16, minHeight: "100%", padding: 0'],
  ["Workspaces.tsx", 'className="workspaces-page relative z-10 flex h-full min-h-0 flex-col overflow-hidden"'],
  ["Knowledge.tsx", 'className="flex-1 min-w-0 flex flex-col overflow-hidden"'],
  ["team/TeamLayout.tsx", 'className="h-full flex flex-col overflow-hidden"'],
  ["Agents.tsx", 'padding: 0,\n        overflow: "hidden",\n        position: "relative"'],
  ["Flows.tsx", 'flexDirection: "column", padding: 0, overflow: "hidden"'],
  ["Integrations.tsx", 'padding: 0,\n        overflow: "hidden",\n        position: "relative"'],
  ["Skills.tsx", 'padding: 0,\n        overflow: "hidden",\n        position: "relative"'],
  ["Apps.tsx", 'flexDirection: "column", padding: 0, overflow: "hidden"'],
  ["Settings.tsx", 'padding: "8px 24px 24px"'],
]);

test("routed app pages use the shared page-title contract", async () => {
  for (const [file, contract] of APP_PAGE_HEADER_CONTRACTS) {
    const source = await readFile(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.ok(source.includes(contract), `${file} must render ${contract}`);
    assert.doesNotMatch(source, /<PageHeader(?:\s|>)[\s\S]{0,180}\bflush\b/, `${file} must not override the app header gutter`);
  }
});

test("admin pages use the same shared page header", async () => {
  for (const file of ADMIN_PAGE_FILES) {
    const source = await readFile(new URL(`../src/admin/pages/${file}`, import.meta.url), "utf8");
    assert.ok(source.includes("<AdminPageHeader"), `${file} must render AdminPageHeader`);
    assert.ok(!source.includes("<h1"), `${file} must not define page-local h1 styles`);
  }
});

test("primary page bodies reuse the app-shell gutter", async () => {
  for (const [file, contract] of PRIMARY_PAGE_BODY_GUTTER_CONTRACTS) {
    const source = await readFile(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.ok(source.includes(contract), `${file} must not add a second outer page gutter`);
  }
});

test("PageHeader owns typography, row placement, and app-shell positioning", async () => {
  const source = await readFile(
    new URL("../src/components/ui/PageHeader.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes("page-header-title"));
  assert.ok(source.includes("md:text-[28px]"));
  assert.match(
    source,
    /EDITOR_HEADER_TITLE_CLASS\s*=\s*[\s\S]{0,360}\bmanor-editor-title\b[\s\S]{0,360}\btext-base\b/,
    "editor titles must carry their 16px typography in the shared component instead of relying on purgeable global CSS",
  );
  assert.match(
    source,
    /EDITOR_HEADER_TITLE_CLASS\s*=\s*[\s\S]{0,360}\btext-ellipsis\b[\s\S]{0,360}\bwhitespace-nowrap\b/,
    "editor titles must preserve single-line truncation in the shared component",
  );
  assert.ok(source.includes('variant?: "page" | "editor"'));
  assert.ok(source.includes("tracking-[-0.014em]"));
  assert.ok(source.includes("page-header-subtitle"));
  assert.ok(source.includes("page-header-meta"));
  assert.ok(source.includes("page-header-title m-0 flex h-10"));
  assert.ok(source.includes("page-header-subtitle mt-1 h-5"));
  // min-h-6, not h-6: the breadcrumb-slot change let the meta row grow and
  // wrap instead of scrolling sideways, so the guard pins the floor height.
  assert.ok(source.includes("page-header-meta mt-1 flex min-h-6"));
  assert.ok(source.includes("{meta && ("));
  assert.ok(source.includes("2xl:flex-row"));
  assert.ok(source.includes("2xl:flex-nowrap"));
  assert.doesNotMatch(
    source,
    /groupClass\s*=\s*[\s\S]{0,180}md:flex-1/,
    "toolbar groups must not expand away from neighboring tabs",
  );
  assert.ok(source.includes("text-[13px]"));
  assert.ok(source.includes("text-[color:var(--text-muted)]"));
  assert.ok(source.includes("PageHeaderBoundary"));
  assert.ok(source.includes("createPortal(header, portalContext.target)"));
  assert.ok(source.includes('data-page-header-layout={portalContext && !inline ? "app" : "inline"}'));
  assert.ok(source.includes("px-3 pb-0 pt-3"));
  assert.ok(!source.includes("flush?:"));
  assert.ok(!source.includes("compactControls?:"));
});

test("Flow editor uses the shared compact editor title", async () => {
  const [flowsSource, cssSource] = await Promise.all([
    readFile(new URL("../src/pages/Flows.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/index.css", import.meta.url), "utf8"),
  ]);

  assert.match(
    flowsSource,
    /<PageHeaderTitle variant="editor">[\s\S]*?workflow-editor-identity-edit/,
  );
  assert.doesNotMatch(
    cssSource,
    /\.workflow-editor-heading h1\s*\{[\s\S]*?font-size:/,
    "Flow editor must not override the shared 16px editor title",
  );
});

test("Workspaces aligns its body with the shared header gutter", async () => {
  const source = await readFile(
    new URL("../src/pages/Workspaces.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes('className="workspaces-page relative z-10 flex h-full min-h-0 flex-col overflow-hidden"'));
  assert.doesNotMatch(
    source,
    /<TabSwitcher[\s\S]{0,180}\s+wrap(?:\s|\/>)/,
    "workspace tabs must remain a compact single-row control",
  );
});

test("Tasks and Knowledge explain their purpose before live page stats", async () => {
  for (const file of ["Tasks.tsx", "Knowledge.tsx"]) {
    const source = await readFile(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.match(source, /<PageHeader[\s\S]{0,180}subtitle=\{t\("page\.(?:tasks|knowledge)\.subtitle"\)\}[\s\S]{0,180}meta=/);
  }
});

test("Blueprint detail keeps its aligned content wide on large screens", async () => {
  const source = await readFile(
    new URL("../src/pages/BlueprintDetail.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes('width: "100%", maxWidth: 1600'));
  assert.ok(!source.includes("maxWidth: 1240"));
  assert.match(source, /<PageHeader[\s\S]{0,320}breadcrumb=/);
  assert.doesNotMatch(source, /<PageHeader[\s\S]{0,260}meta=/);
  assert.doesNotMatch(source, /bp\.tags\.slice/);
});

test("AppLayout provides one canonical header slot before routed page content", async () => {
  const source = await readFile(
    new URL("../src/layouts/AppLayout.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes("<PageHeaderBoundary>"));
  assert.ok(source.includes("app-route-content min-h-0 min-w-0 flex-1"));
  assert.ok(source.includes("app-route-content--settings overflow-auto p-0"));
  assert.ok(source.includes("overflow-auto px-6 pb-6 pt-2"));
});

test("editor routes use a full-bleed content contract without clipping their headers", async () => {
  const [layoutSource, styleSource] = await Promise.all([
    readFile(new URL("../src/layouts/AppLayout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/index.css", import.meta.url), "utf8"),
  ]);

  assert.ok(layoutSource.includes("const isEditorShellRoute ="));
  assert.ok(layoutSource.includes('location.pathname.startsWith("/viewer/")'));
  assert.ok(layoutSource.includes('location.pathname.startsWith("/editor/")'));
  assert.ok(layoutSource.includes('location.pathname === "/diagram-canvas"'));
  assert.ok(layoutSource.includes("app-route-content--editor overflow-hidden p-0"));

  const editorShellRules = [...styleSource.matchAll(/\.manor-editor-shell\s*\{([^}]*)\}/g)];
  assert.ok(editorShellRules.length > 0);
  for (const [, declarations] of editorShellRules) {
    assert.doesNotMatch(
      declarations,
      /margin:\s*-/,
      "editor shells must not use negative margins to escape the app content gutter",
    );
  }
  assert.ok(
    styleSource.includes(
      ".app-route-content:not(.app-route-content--settings):not(.app-route-content--editor)",
    ),
    "mobile page gutters must not override the editor route's full-bleed layout",
  );
});
