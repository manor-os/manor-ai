import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const integrationsSource = await readFile(
  new URL("../src/pages/Integrations.tsx", import.meta.url),
  "utf8",
);
const dropdownSource = await readFile(
  new URL("../src/components/ui/Dropdown.tsx", import.meta.url),
  "utf8",
);

test("integration account rows render one combined status indicator", () => {
  assert.equal(
    [...integrationsSource.matchAll(/<ConnectionStatusPip\b/g)].length,
    2,
  );
  assert.doesNotMatch(integrationsSource, /<HealthPip\b|<WiringPip\b/);
});

test("an untested health check does not make a connected account look disconnected", () => {
  assert.match(
    integrationsSource,
    /credOk === false[\s\S]*?!connected[\s\S]*?wiring && wireOk === false[\s\S]*?"#54a176"/,
  );
  assert.match(
    integrationsSource,
    /if \(untested\)[\s\S]*?not_tested_yet_use_menu_test_connection/,
  );
});

// A red "AUTH FAILED" chip tells the user something broke but not what,
// and the health check's detail — which names the actual cause, e.g. a
// username missing its domain or an app password being required — was
// only reachable by hovering for a native tooltip. Tooltips do not exist
// on touch, and nobody hovers a status chip on the off-chance. The
// reason has to be on screen next to the failure.
test("a failed account shows why it failed, not just that it failed", () => {
  assert.match(
    integrationsSource,
    /const failureDetail =[\s\S]*?health[\s\S]*?ok === false[\s\S]*?detail/,
  );
  // Rendered as page content, not only as a title attribute.
  assert.match(
    integrationsSource,
    /\{failureDetail && \([\s\S]*?\{failureDetail\}/,
  );
});

test("both account row kinds surface the reason, not just the entity one", () => {
  // EntityAccountRow (IMAP/SMTP, API keys) and ConnectionRow (OAuth) share
  // the same status pip and had the same blind spot. Fixing one and not
  // the other just moves the dead end.
  assert.equal(
    [...integrationsSource.matchAll(/const failureDetail =/g)].length,
    2,
  );
  assert.equal(
    [...integrationsSource.matchAll(/\{failureDetail && \(/g)].length,
    2,
  );
});

test("the failure reason stays readable instead of being clipped to one line", () => {
  // The row label uses nowrap + ellipsis; the detail must not inherit it
  // or a multi-cause hint becomes "IMAP login failed: b'[AUTHENTICA…".
  assert.match(
    integrationsSource,
    /\{failureDetail && \([\s\S]*?whiteSpace: "normal"/,
  );
});

test("an open integration detail refreshes when account health changes", () => {
  // DetailDrawer stores a React-node snapshot. A successful manual test
  // refreshes mcp-servers, so the detail-rebuild effect must observe the
  // nested account arrays or the drawer keeps showing the previous failure.
  assert.match(
    integrationsSource,
    /useEffect\(\(\) => \{[\s\S]*?currentDetailKey !== detailKey[\s\S]*?openIntegrationDetail\(\)[\s\S]*?server\.connections,[\s\S]*?server\.entity_accounts,/,
  );
});

// The provider badge said "ready" while the account under it said AUTH
// FAILED. "ready" is agent_can_use, which the backend now clears when a
// provider has actually refused the credentials — so the label resolves
// to "needs attention" on its own. The dot has to follow it, or the card
// shows a green light next to a warning.
test("the provider status dot agrees with the label it sits next to", () => {
  assert.match(
    integrationsSource,
    /const statusColor = server\.agent_can_use[\s\S]*?hasPartialConnection[\s\S]*?"#d6d3d1"/,
  );
  assert.doesNotMatch(
    integrationsSource,
    /const statusColor = isReadyConnection \?/,
  );
});

test("the integration row overflow menu uses an accessible button", () => {
  assert.match(
    integrationsSource,
    /<button[\s\S]*?className="integration-row-menu-trigger"[\s\S]*?aria-label=\{t\("action\.more"\)\}/,
  );
});

test("dropdown menus render above detail and modal surfaces", () => {
  assert.match(dropdownSource, /zIndex:\s*20010/);
  assert.match(
    dropdownSource,
    /closest<HTMLElement>\([\s\S]*?\.detail-scrim, \.manor-dialog-overlay[\s\S]*?portalTarget[\s\S]*?createPortal\([\s\S]*?portalTarget/,
  );
});
