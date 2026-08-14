import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import test from "node:test";

const embeddedChatSource = new URL("../src/components/EmbeddedChat.tsx", import.meta.url);
const templateGallerySource = new URL(
  "../src/components/ChatModeTemplateGallery.tsx",
  import.meta.url,
);
const chatSurfaces = ["EmbeddedChat", "FloatingChat", "WorkspaceChat"].map(
  (component) => new URL(`../src/components/${component}.tsx`, import.meta.url),
);
const blueprintDetailSource = new URL("../src/pages/BlueprintDetail.tsx", import.meta.url);
const stylesheetSource = new URL("../src/index.css", import.meta.url);
const localeSources = ["en", "zh", "es"].map(
  (locale) => new URL(`../src/lib/i18n/${locale}.ts`, import.meta.url),
);

test("Workspace welcome reuses four theme-aware Marketplace Blueprint covers", async () => {
  const [source, blueprintDetail] = await Promise.all([
    readFile(embeddedChatSource, "utf8"),
    readFile(blueprintDetailSource, "utf8"),
  ]);
  const workspaceDefinitions = source.slice(
    source.indexOf("  workspace: ["),
    source.indexOf("  slides: ["),
  );

  for (const id of [
    "productized_service_os",
    "product_video_studio",
    "digital_product_store_os",
    "content_distribution_studio",
  ]) {
    assert.match(workspaceDefinitions, new RegExp(`id: "${id}"`));
  }

  assert.match(workspaceDefinitions, /motif: "service"/);
  assert.match(workspaceDefinitions, /motif: "video"/);
  assert.match(workspaceDefinitions, /motif: "commerce"/);
  assert.match(workspaceDefinitions, /motif: "content"/);
  assert.equal((workspaceDefinitions.match(/\bid: "/g) || []).length, 4);
  assert.doesNotMatch(workspaceDefinitions, /sampleSrc:/);
  assert.match(source, /blueprintCoverDataUrl\(coverTemplate, "white"\)/);
  assert.match(source, /blueprintCoverDataUrl\(coverTemplate, "dark"\)/);
  assert.match(source, /<ThemeAwareImage/);
  assert.match(source, /darkSrc=\{activeDetailSrc \? undefined : previewContent\.previewImageDarkSrc\}/);
  assert.match(source, /sourceBlueprintSlug: "solo-content-distribution-studio-v1"/);
  assert.match(source, /workspace\.blueprint_update\?\.blueprint_slug === blueprintSlug/);
  assert.match(source, /api\.blueprints\.list\("published"\)/);
  assert.match(source, /api\.blueprints\.get\(previewBlueprintSummary!\.id\)/);
  assert.match(
    source,
    /navigate\(`\/blueprints\/\$\{encodeURIComponent\(blueprint\.id\)\}\?install=1`\)/,
  );
  assert.match(blueprintDetail, /searchParams\.get\("install"\) !== "1"/);
  assert.match(blueprintDetail, /setInstallOpen\(true\)/);
  assert.match(blueprintDetail, /<InstallBlueprintModal/);
  assert.match(source, /component\.embedded_chat\.install_workspace/);
  assert.match(source, /component\.embedded_chat\.workspace_contents/);
  assert.match(source, /setupPreview\.first_week_outputs/);
  assert.match(source, /component\.embedded_chat\.installed_as/);
  assert.match(source, /component\.embedded_chat\.open_installed_workspace/);
});

test("Workspace cards open the Blueprint install page instead of Remix", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const installHandlerStart = source.indexOf("  const handleSampleInstall = useCallback(");
  const installHandlerEnd = source.indexOf(
    "\n\n  const prepareSampleRemix = useCallback(",
    installHandlerStart,
  );
  const installHandler = source.slice(installHandlerStart, installHandlerEnd);
  const featuredGridStart = source.indexOf(
    'className="workspace-sample-grid workspace-sample-grid--previewable"',
  );
  const featuredGridEnd = source.indexOf(
    "{hasTemplateCatalog && selected.samples.length > featuredSamples.length",
    featuredGridStart,
  );
  const featuredGrid = source.slice(featuredGridStart, featuredGridEnd);
  const quickPreviewStart = source.indexOf("function WorkspaceSampleQuickPreview(");
  const quickPreviewEnd = source.indexOf(
    "\n\nconst FEATURED_FLOW_TEMPLATE_KEYS",
    quickPreviewStart,
  );
  const quickPreview = source.slice(quickPreviewStart, quickPreviewEnd);

  assert.ok(installHandlerStart >= 0 && installHandlerEnd > installHandlerStart);
  assert.match(installHandler, /queryClient\.fetchQuery\(\{/);
  assert.match(installHandler, /api\.blueprints\.list\("published"\)/);
  assert.match(installHandler, /candidate\.slug === blueprintSlug/);
  assert.match(
    installHandler,
    /navigate\(`\/blueprints\/\$\{encodeURIComponent\(blueprint\.id\)\}\?install=1`\)/,
  );
  assert.match(featuredGrid, /selected\.key === "workspace"/);
  assert.match(featuredGrid, /void handleSampleInstall\(sample\)/);
  assert.match(featuredGrid, /component\.embedded_chat\.install_workspace/);
  assert.match(quickPreview, /const isInstallableWorkspace = Boolean\(sample\?\.sourceBlueprintSlug\)/);
  assert.match(quickPreview, /\{!isInstallableWorkspace && \([\s\S]*?component\.embedded_chat\.remix/);
  assert.match(quickPreview, /isInstallableWorkspace && installedWorkspace \?/);
  assert.match(quickPreview, /isInstallableWorkspace && blueprint \?/);
});

test("every sample mode exposes its complete previewable and remixable card set", async () => {
  const [source, stylesheet] = await Promise.all([
    readFile(embeddedChatSource, "utf8"),
    readFile(stylesheetSource, "utf8"),
  ]);
  const modes = [
    "workspace",
    "slides",
    "docs",
    "pdf",
    "sheets",
    "website",
    "image",
    "video",
    "research",
    "agents",
    "automations",
  ];
  const expectedCounts = {
    workspace: 4,
    slides: 10,
    docs: 7,
    pdf: 34,
    sheets: 14,
    website: 11,
    image: 24,
    video: 13,
    research: 4,
    agents: 4,
    automations: 4,
  };

  for (const [index, mode] of modes.entries()) {
    const nextMode = modes[index + 1];
    const start = source.indexOf(`  ${mode}: [`);
    const end = nextMode
      ? source.indexOf(`  ${nextMode}: [`, start)
      : source.indexOf("\n};", start);
    assert.ok(start >= 0 && end > start, `${mode} sample definitions should be present`);
    const block = source.slice(start, end);
    const expectedCount = expectedCounts[mode];
    assert.equal(
      (block.match(/\bid: "/g) || []).length,
      expectedCount,
      `${mode} should expose exactly ${expectedCount} samples`,
    );
  }

  assert.match(source, /pickRandomSoloBusinessIdeas\(4\)/);
  assert.match(source, /pickRandomSoloBusinessIdeas\(\s*4,/);
  assert.match(source, /const featuredSamples = selected\.samples\.slice\(0, 4\);/);
  assert.match(source, /className="workspace-sample-grid workspace-sample-grid--previewable"/);
  assert.match(source, /className="workspace-sample-card workspace-sample-card--previewable"/);
  assert.match(source, /component\.embedded_chat\.quick_preview/);
  assert.match(source, /component\.embedded_chat\.remix/);
  assert.doesNotMatch(source, /PREVIEWABLE_SAMPLE_CAPABILITIES/);
  assert.match(
    stylesheet,
    /\.workspace-sample-actions > button \{[\s\S]*?white-space: nowrap;/,
  );
  assert.match(
    stylesheet,
    /\.workspace-sample-actions > button:first-child \{[\s\S]*?flex-grow: 1\.15;/,
  );
});

test("existing-session template rails only render the complete real sample library", async () => {
  const [gallery, ...surfaces] = await Promise.all([
    readFile(templateGallerySource, "utf8"),
    ...chatSurfaces.map((source) => readFile(source, "utf8")),
  ]);

  assert.match(gallery, /samples: ChatModeTemplateSample\[\];/);
  assert.match(gallery, /samples\.filter\(\(sample\) =>/);
  assert.match(gallery, /Boolean\(previewSrc\(sample\)\)/);
  assert.doesNotMatch(gallery, /CHAT_MODE_TEMPLATES|fallbackSamples|templateText/);
  assert.doesNotMatch(gallery, /IconDocument|template-card-fallback|template-detail-fallback/);

  for (const source of surfaces) {
    assert.match(source, /samples=\{chatModeTemplateSamples\(chatMode\)\}/);
  }
});

test("representative generated samples use real Manor artifacts instead of cover placeholders", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const samples = [
    ["website", "digital_product_launch_site", "/assets/samples/artifacts/website/digital-product-launch-site.html"],
    ["image", "product_campaign_visuals", "/assets/samples/artifacts/image/arcline-product-campaign.png"],
    ["video", "aurora-app-launch", "/assets/samples/artifacts/video/templates/aurora-app-launch.mp4"],
    ["research", "market_opportunity_map", "/assets/samples/artifacts/research/market-opportunity-map.html"],
    ["agents", "customer_success_team", "/assets/samples/artifacts/agents/customer-success-agent-team.html"],
    ["automations", "lead_nurture_system", "/assets/samples/artifacts/automations/lead-nurture-system.html"],
  ];

  for (const [mode, id, artifactPath] of samples) {
    const modeStart = source.indexOf(`  ${mode}: [`);
    const modeEnd = source.indexOf("\n  ],", modeStart) + 5;
    const block = source.slice(modeStart, modeEnd);
    assert.match(block, new RegExp(`id: "${id}"[\\s\\S]*?sampleSrc: "${artifactPath.replaceAll(".", "\\.")}"`));
    assert.doesNotMatch(block, /coverTemplate:/, `${mode} should not fall back to generated cover art`);

    const artifact = await stat(new URL(`../public${artifactPath}`, import.meta.url));
    assert.ok(artifact.size > 1_000, `${mode}.${id} should point to a substantive artifact`);

    const detailPath = `/assets/samples/details/${mode}/${id}-01.png`;
    const detail = await stat(new URL(`../public${detailPath}`, import.meta.url));
    assert.ok(detail.size > 1_000, `${mode}.${id} should include a real detail preview`);

    if (mode !== "image") {
      const previewPath = `/assets/samples/previews/${mode}/${id}.png`;
      const preview = await stat(new URL(`../public${previewPath}`, import.meta.url));
      assert.ok(preview.size > 1_000, `${mode}.${id} should include a real 16:9 card preview`);
    }
  }

  /*
   * The sheets gallery was rebuilt on a different asset convention, so it is
   * checked by shape rather than by a named sample. The invariant is the one
   * above: every card opens a real workbook, and none of them fall back to
   * generated cover art.
   */
  const sheetsStart = source.indexOf("  sheets: [");
  const sheetsBlock = source.slice(sheetsStart, source.indexOf("  website: [", sheetsStart));
  const workbooks = [...sheetsBlock.matchAll(/sampleSrc: "([^"]+\.xlsx)"/g)].map(([, p]) => p);
  assert.ok(workbooks.length >= 4, "sheets should ship real workbooks");
  assert.doesNotMatch(sheetsBlock, /coverTemplate:/, "sheets should not fall back to generated cover art");
  for (const workbook of workbooks) {
    const artifact = await stat(new URL(`../public${workbook}`, import.meta.url));
    assert.ok(artifact.size > 1_000, `${workbook} should be a substantive workbook`);
  }
});

test("Sheets mode offers fourteen distinct workbooks while featuring four", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const sheetsStart = source.indexOf("  sheets: [");
  const sheetsEnd = source.indexOf("  website: [", sheetsStart);
  const sheetsDefinitions = source.slice(sheetsStart, sheetsEnd);
  const samples = [
    ["atlas_product_roadmap", "atlas-product-development.xlsx", 2],
    ["orbit_people_planner", "orbit-people-planner.xlsx", 2],
    ["harbor_crm_pipeline", "harbor-crm-pipeline.xlsx", 2],
    ["relay_project_portfolio", "relay-project-portfolio.xlsx", 2],
    ["northstar_saas_command_center", "northstar-saas-command-center.xlsx", 1],
    ["casa_sol_hotel_planner", "casa-sol-hotel-planner.xlsx", 1],
    ["night_shift_film_control", "night-shift-film-control.xlsx", 1],
    ["verdant_lab_notebook", "verdant-lab-notebook.xlsx", 1],
    ["ember_menu_engineering", "ember-menu-engineering.xlsx", 1],
    ["pulse_athlete_profile", "pulse-athlete-profile.xlsx", 1],
    ["forge_inventory_control", "forge-inventory-control.xlsx", 1],
    ["open_hands_grant_pipeline", "open-hands-grant-pipeline.xlsx", 1],
    ["ironclad_cost_risk", "ironclad-cost-risk.xlsx", 1],
    ["willow_rose_run_of_show", "willow-rose-run-of-show.xlsx", 1],
  ];

  assert.equal((sheetsDefinitions.match(/\bid: "/g) || []).length, samples.length);
  assert.equal((sheetsDefinitions.match(/\bcopy: \{/g) || []).length, samples.length);

  for (const [id, filename, detailPageCount] of samples) {
    const artifactPath = `/assets/reviews/sheets-final/${filename}`;
    assert.match(
      sheetsDefinitions,
      new RegExp(`id: "${id}"[\\s\\S]*?sampleSrc: "${artifactPath.replaceAll(".", "\\.")}"`),
    );

    for (const path of [
      artifactPath,
      `/assets/samples/previews/sheets/${id}.png`,
      ...Array.from({ length: detailPageCount }, (_, index) =>
        `/assets/samples/details/sheets/${id}-${String(index + 1).padStart(2, "0")}.png`,
      ),
    ]) {
      const asset = await stat(new URL(`../public${path}`, import.meta.url));
      assert.ok(asset.size > 1_000, `${id} should include ${path}`);
    }
  }

  const firstFourIds = Array.from(sheetsDefinitions.matchAll(/\bid: "([^"]+)"/g))
    .slice(0, 4)
    .map((match) => match[1]);
  assert.deepEqual(firstFourIds, [
    "atlas_product_roadmap",
    "orbit_people_planner",
    "harbor_crm_pipeline",
    "relay_project_portfolio",
  ]);
  assert.match(source, /const hasTemplateCatalog =\s*selected\.key === "slides" \|\| selected\.key === "sheets"/);
  assert.match(source, /const featuredSamples = selected\.samples\.slice\(0, 4\);/);
  assert.match(source, /component\.embedded_chat\.browse_spreadsheet_templates/);
  assert.match(source, /className="workspace-sample-catalog-grid"/);
  assert.match(source, /selected\.samples\.map\(\(sample, index\) =>/);
  assert.doesNotMatch(source, /SPREADSHEETS_PLUGIN_COMPARISON_SAMPLES/);
  assert.doesNotMatch(source, /generated_by_manor_ai|generated_by_spreadsheets/);
});

test("Slides mode offers ten real source PPTX templates while featuring four", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const slidesStart = source.indexOf("  slides: [", source.indexOf("const SIDE_HUSTLE_SAMPLE_DEFINITIONS"));
  const slidesEnd = source.indexOf("  docs: [", slidesStart);
  const slidesDefinitions = source.slice(slidesStart, slidesEnd);
  const samples = [
    ["aurelia_far_north", "aurelia-far-north.pptx"],
    ["null_signal_incident_manual", "null-signal-incident-manual.pptx"],
    ["moss_moon_story", "moss-and-moon-story.pptx"],
    ["kinetic_autumn_27", "kinetic-autumn-27.pptx"],
    ["blue_commons_impact_report", "blue-commons-impact-report.pptx"],
    ["monolith_house", "monolith-house.pptx"],
    ["orbital_bakery_brand_launch", "orbital-bakery-brand-launch.pptx"],
    ["helio_sx_evidence_review", "helio-sx-evidence-review.pptx"],
    ["line_47_wayfinding", "line-47-wayfinding.pptx"],
    ["cinder_road_film_pitch", "cinder-road-film-pitch.pptx"],
  ];

  assert.equal((slidesDefinitions.match(/\bid: "/g) || []).length, samples.length);
  assert.equal((slidesDefinitions.match(/\bcopy: \{/g) || []).length, samples.length);

  for (const [id, filename] of samples) {
    const artifactPath = `/assets/samples/artifacts/slides/${filename}`;
    assert.match(
      slidesDefinitions,
      new RegExp(`id: "${id}"[\\s\\S]*?sampleSrc: "${artifactPath.replaceAll(".", "\\.")}"`),
    );
    for (const path of [
      artifactPath,
      `/assets/samples/previews/slides/${id}.png`,
      ...[1, 2, 3].map(
        (page) => `/assets/samples/details/slides/${id}-${String(page).padStart(2, "0")}.png`,
      ),
    ]) {
      const asset = await stat(new URL(`../public${path}`, import.meta.url));
      assert.ok(asset.size > 1_000, `${id} should include ${path}`);
    }
  }

  assert.match(source, /component\.embedded_chat\.browse_presentation_templates/);
  assert.match(source, /component\.embedded_chat\.presentation_template_library/);
});

test("Workspace Remix uses the unified source-template workflow", async () => {
  const [source, remix] = await Promise.all([
    readFile(embeddedChatSource, "utf8"),
    readFile(new URL("../src/components/templateRemix.ts", import.meta.url), "utf8"),
  ]);
  const handlerStart = source.indexOf("  const prepareSampleRemix = useCallback(");
  const handlerEnd = source.indexOf("\n  useEffect(() => {", handlerStart);
  const handler = source.slice(handlerStart, handlerEnd);
  const selectionStart = source.indexOf("  const handleSampleSelect = useCallback(");
  const selectionEnd = source.indexOf("\n\n  // Resolved agent ID", selectionStart);
  const selection = source.slice(selectionStart, selectionEnd);

  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart);
  assert.match(handler, /uploadTemplateRemixSource\(sample\)/);
  assert.match(handler, /invalidateKnowledgeQueries\(queryClient\)/);
  assert.match(handler, /onSampleSelect\(sample, templateAttachment\)/);
  assert.doesNotMatch(handler, /remixRequirements/);
  assert.doesNotMatch(handler, /navigate\(`\/editor/);
  assert.match(selection, /buildTemplateRemixPrompt/);
  assert.doesNotMatch(selection, /setPendingSampleRemix/);
  assert.doesNotMatch(source, /pendingSampleRemix|TemplateRemixDialog/);
  assert.doesNotMatch(handler, /setInput\(sample\.prompt\)/);
  assert.match(source, /onRemix=\{prepareSampleRemix\}/);
  assert.match(source, /loading=\{remixingSampleTitle === sample\.title\}/);
  for (const kind of ["document", "presentation", "spreadsheet", "pdf", "website", "image", "video"]) {
    assert.match(remix, new RegExp(`case "${kind}"`));
  }
});

test("Image mode previews and opens the original image assets without page wrappers", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const imageDefinitions = source.slice(
    source.indexOf("  image: ["),
    source.indexOf("  video: ["),
  );
  const originalAssets = [
    "/assets/samples/digital-product-cover.jpg",
    "/assets/samples/coffee-brand-visual.jpg",
    "/assets/samples/ai-manga-poster.jpg",
    "/assets/samples/artifacts/image/arcline-product-campaign.png",
  ];

  for (const assetPath of originalAssets) {
    const escapedAssetPath = assetPath.replaceAll(".", "\\.");
    assert.match(imageDefinitions, new RegExp(`imageSrc: "${escapedAssetPath}"`));
    assert.match(imageDefinitions, new RegExp(`sampleSrc: "${escapedAssetPath}"`));
  }

  assert.doesNotMatch(imageDefinitions, /detailPageCount:/);

  const imagePreviewBranchStart = source.indexOf("\n    : imageSrc\n");
  const imagePreviewBranchEnd = source.indexOf("\n    : {", imagePreviewBranchStart);
  const imagePreviewBranch = source.slice(imagePreviewBranchStart, imagePreviewBranchEnd);
  assert.match(imagePreviewBranch, /imageSrc,/);
  assert.match(imagePreviewBranch, /sampleSrc,/);
  assert.doesNotMatch(imagePreviewBranch, /detailImageSrcs/);
  assert.doesNotMatch(imagePreviewBranch, /detailImageAlt/);
});

test("every previewable sample uses the same full 16:9 canvas as PDF", async () => {
  const stylesheet = await readFile(stylesheetSource, "utf8");

  assert.match(
    stylesheet,
    /\.workspace-sample-card--previewable \.workspace-sample-preview,[\s\S]*?aspect-ratio: 16 \/ 9;/,
  );
  assert.match(
    stylesheet,
    /\.workspace-sample-card--previewable[\s\S]*?\.workspace-sample-preview--browseable[\s\S]*?object-fit: cover;[\s\S]*?object-position: top center;/,
  );
  assert.match(
    stylesheet,
    /\.workspace-sample-card--flow-template \.workspace-flow-template-screenshot \{[\s\S]*?aspect-ratio: 16 \/ 9;/,
  );
});

test("new fourth samples and installation status are localized", async () => {
  const locales = await Promise.all(localeSources.map((source) => readFile(source, "utf8")));
  const samples = [
    ["workspace", "content_distribution_studio"],
    ["website", "digital_product_launch_site"],
    ["image", "product_campaign_visuals"],
    ["video", "feature_walkthrough"],
    ["research", "market_opportunity_map"],
    ["agents", "customer_success_team"],
    ["automations", "lead_nurture_system"],
  ];

  for (const locale of locales) {
    for (const [mode, sample] of samples) {
      for (const field of ["title", "outcome", "prompt", "preview_title"]) {
        assert.ok(
          locale.includes(
            `"component.embedded_chat.side_hustle.${mode}.${sample}.${field}"`,
          ),
          `${mode}.${sample}.${field} should be localized`,
        );
      }
      const previewFields = mode === "workspace"
        ? []
        : mode === "image"
          ? ["image_alt", "image_caption"]
          : ["label", "line_1", "line_2", "chip_1", "chip_2"];
      for (const field of previewFields) {
        assert.ok(
          locale.includes(
            `"component.embedded_chat.side_hustle.${mode}.${sample}.${field}"`,
          ),
          `${mode}.${sample}.${field} should be localized`,
        );
      }
    }
    for (const key of [
      "open_installed_workspace",
      "checking_installation",
      "installation_status_unavailable",
      "installed_as",
      "install_workspace",
      "marketplace_workspace",
      "loading_workspace_details",
      "workspace_details_unavailable",
      "workspace_contents",
      "setup_items",
      "browse_spreadsheet_templates",
      "browse_presentation_templates",
      "spreadsheet_template_library",
      "presentation_template_library",
      "spreadsheet_template_ready",
      "presentation_template_ready",
      "pdf_template_ready",
      "spreadsheet_template_create_failed",
      "presentation_template_create_failed",
      "pdf_template_create_failed",
      "template_ready",
      "template_create_failed",
      "prepare_remix",
      "remix_creates_new_artifact",
      "remix_dialog_description",
      "remix_requirements_label",
      "remix_requirements_placeholder",
      "remix_requirements_error",
      "remix_shortcut_hint",
    ]) {
      assert.ok(locale.includes(`"component.embedded_chat.${key}"`));
    }
    assert.ok(
      locale.includes(
        '"component.embedded_chat.flow_templates.installed_status"',
      ),
    );
  }
});

test("Workspace quick preview prioritizes installation and keeps details visible responsively", async () => {
  const [source, stylesheet] = await Promise.all([
    readFile(embeddedChatSource, "utf8"),
    readFile(stylesheetSource, "utf8"),
  ]);

  assert.match(
    source,
    /isInstallableWorkspace && installedWorkspace \? \([\s\S]*?open_installed_workspace[\s\S]*?: isInstallableWorkspace && blueprint \? \([\s\S]*?install_workspace/,
  );
  assert.match(source, /workspace-blueprint-preview-summary/);
  assert.match(source, /workspace-blueprint-preview-description/);
  assert.match(source, /workspace-blueprint-preview-facts/);
  assert.match(source, /workspace-blueprint-preview-includes/);
  assert.match(source, /const isSpreadsheetPreview = sample\?\.preview === "sheets"/);
  assert.match(source, /workspace-sheet-quick-preview-navigation/);
  assert.match(
    source,
    /isSpreadsheetPreview \? \([\s\S]*?workspace-sheet-quick-preview-navigation[\s\S]*?: \([\s\S]*?<aside className="workspace-blueprint-preview-details">/,
  );
  assert.match(
    stylesheet,
    /\.workspace-sheet-quick-preview \{[\s\S]*?display: flex;[\s\S]*?flex-direction: column;/,
  );
  assert.match(
    stylesheet,
    /\.workspace-blueprint-quick-preview \{[\s\S]*?grid-template-columns:/,
  );
  assert.match(
    stylesheet,
    /@media \(max-width: 720px\)[\s\S]*?\.workspace-blueprint-quick-preview \{[\s\S]*?display: flex;/,
  );
});

test("Marketplace sample covers keep motion subtle and respect reduced motion", async () => {
  const stylesheet = await readFile(stylesheetSource, "utf8");

  assert.match(stylesheet, /\.workspace-sample-preview--marketplace-cover/);
  assert.match(stylesheet, /transform 240ms cubic-bezier\(0\.22, 1, 0\.36, 1\)/);
  assert.match(
    stylesheet,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.workspace-sample-preview--marketplace-cover/,
  );
  assert.match(
    stylesheet,
    /html\[data-theme="dark"\] \.workspace-sample-preview--marketplace-cover/,
  );
  assert.doesNotMatch(
    stylesheet,
    /\.workspace-flow-template-quick-preview\s*>\s*\.workspace-flow-template-preview-details\s*{\s*display:\s*none/,
    "Flow preview details and installation status must remain visible",
  );
});
