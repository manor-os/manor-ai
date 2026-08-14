import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) =>
  readFile(path.join(webRoot, relativePath), "utf8");

const [chatSource, visualSource, visualCssSource, indexCssSource] =
  await Promise.all([
    read("src/components/EmbeddedChat.tsx"),
    read("src/components/ui/WorkspaceRailVisual.tsx"),
    read("src/components/ui/WorkspaceRailVisual.css"),
    read("src/index.css"),
  ]);

const WORKSPACE_RAIL_KEYS = [
  "research",
  "agents",
  "automations",
  "flows",
  "new-idea",
  "validate-idea",
  "workspace",
  "slides",
  "docs",
  "pdf",
  "sheets",
  "website",
  "image",
  "video",
];

test("workspace mode rail uses polished animated image visuals", async () => {
  assert.ok(
    chatSource.includes(
      "<WorkspaceRailVisual kind={railKey} active={isActive} />",
    ),
  );
  assert.ok(visualCssSource.includes("workspace-rail-star-glint"));
  assert.ok(visualCssSource.includes('html[data-theme="dark"]'));
  assert.ok(visualCssSource.includes("prefers-reduced-motion: reduce"));
  assert.ok(indexCssSource.includes(".workspace-mode-pill-visual"));

  for (const kind of [
    "research",
    "agents",
    "automations",
    "flows",
    "new-idea",
    "validate-idea",
    "workspace",
    "slides",
    "docs",
    "sheets",
    "website",
    "image",
    "video",
  ]) {
    assert.ok(visualSource.includes(`"/assets/workspace-rail-v2/${kind}.png"`));
    await access(
      path.join(webRoot, "public/assets/workspace-rail-v2", `${kind}.png`),
    );
  }
});

test("wheel and drag walk the whole rail in one direction, with no per-key special case", () => {
  assert.ok(chatSource.includes("activeIdeaMode || activeCapability"));
  assert.ok(chatSource.includes("const activeRailIndexRef = useRef"));
  // Scroll down/right and drag left both mean "next". Pinning one key to a
  // fixed step (this was `railFocusKeyRef.current === "flows" ? -1 : …`) makes
  // the gesture bounce off that key and strands every item behind it.
  assert.ok(chatSource.includes("focusRailByStep(delta > 0 ? 1 : -1)"));
  assert.ok(chatSource.includes("focusRailByStep(diff < 0 ? 1 : -1)"));

  // The step function itself must stay key-blind, or the same "strands
  // everything past this key" bug just moves one level down where the two
  // assertions above cannot see it. It may only clamp at the ends of the rail.
  const stepFn = chatSource.slice(
    chatSource.indexOf("const focusRailByStep = useCallback("),
    chatSource.indexOf("const handleRailItemClick = useCallback("),
  );
  assert.ok(stepFn.length > 0 && stepFn.length < 2000, "focusRailByStep moved");
  for (const railKey of WORKSPACE_RAIL_KEYS) {
    assert.ok(
      !stepFn.includes(`"${railKey}"`) || railKey === "new-idea" || railKey === "validate-idea",
      `focusRailByStep must not branch on the "${railKey}" key`,
    );
  }
  assert.ok(stepFn.includes("Math.max(") && stepFn.includes("Math.min("));
  assert.ok(chatSource.includes("wheelSwitchLockedRef.current) return"));
  assert.ok(chatSource.includes("}, 420)"));
  assert.ok(chatSource.includes("const chatModeRef = useRef(chatMode)"));
  // The rail still lands on Ideas for a fresh auto-mode chat, but the re-arm
  // carries origin "default" so the focus stays cosmetic and attaches no skill.
  assert.ok(
    chatSource.includes(
      'chatModeRef.current === "auto"\n          ? { mode: "new-idea", origin: "default" }\n          : null,',
    ),
  );
});

test("Flow sample cards come from the verified server catalogue", () => {
  assert.ok(chatSource.includes("function FlowTemplateSamples()"));
  assert.ok(chatSource.includes("api.workflows.templates()"));
  assert.ok(chatSource.includes("api.workflows.installTemplate(template.id)"));
  assert.ok(chatSource.includes("FEATURED_FLOW_TEMPLATE_KEYS"));
});
