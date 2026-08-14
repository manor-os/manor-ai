import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import test from "node:test";

const componentSource = new URL(
  "../src/components/ChatModeTemplateGallery.tsx",
  import.meta.url,
);
const embeddedChatSource = new URL(
  "../src/components/EmbeddedChat.tsx",
  import.meta.url,
);
const templateRemixSource = new URL(
  "../src/components/templateRemix.ts",
  import.meta.url,
);
const chatSurfaceSources = ["EmbeddedChat", "FloatingChat", "WorkspaceChat"].map(
  (component) => new URL(`../src/components/${component}.tsx`, import.meta.url),
);

test("existing-session template rail only renders real previewable samples", async () => {
  const [component, embeddedChat, ...surfaces] = await Promise.all([
    readFile(componentSource, "utf8"),
    readFile(embeddedChatSource, "utf8"),
    ...chatSurfaceSources.map((source) => readFile(source, "utf8")),
  ]);

  assert.match(component, /samples: ChatModeTemplateSample\[\];/);
  assert.match(component, /samples\s*\.filter\(\(sample\) =>/);
  assert.match(component, /Boolean\(previewSrc\(sample\)\)/);
  assert.doesNotMatch(component, /const templates = samples[\s\S]*?\.slice\(0, 4\);/);
  assert.doesNotMatch(component, /CHAT_MODE_TEMPLATES|fallbackSamples|templateText/);
  assert.doesNotMatch(component, /IconDocument|template-card-fallback|template-detail-fallback/);
  assert.match(embeddedChat, /const featuredSamples = selected\.samples\.slice\(0, 4\);/);

  for (const source of surfaces) {
    assert.match(source, /samples=\{chatModeTemplateSamples\(chatMode\)\}/);
  }
});

test("existing-session templates stay in the original horizontal preview rail", async () => {
  const [component, stylesheet] = await Promise.all([
    readFile(componentSource, "utf8"),
    readFile(new URL("../src/index.css", import.meta.url), "utf8"),
  ]);

  assert.doesNotMatch(component, /if \(mode === "video"\)/);
  assert.doesNotMatch(component, /chat-mode-template-card--tiled/);
  assert.doesNotMatch(component, /chat-mode-template-card-actions/);
  assert.match(component, /className="chat-mode-template-gallery-rail"/);
  assert.match(component, /className="chat-mode-template-card"/);
  assert.match(component, /className="chat-mode-template-card-preview"/);
  assert.match(component, /className="chat-mode-template-card-title"/);
  assert.match(component, /onClick=\{\(\) => setPreviewSample\(item\)\}/);
  assert.match(component, /component\.embedded_chat\.quick_preview/);
  assert.match(component, /component\.embedded_chat\.remix/);
  assert.doesNotMatch(stylesheet, /\.chat-mode-template-gallery\[data-mode="video"\]/);
  assert.match(stylesheet, /\.chat-mode-template-gallery-rail \{[\s\S]*?display: flex;[\s\S]*?overflow-x: auto;/);
  assert.match(stylesheet, /\.chat-mode-template-card \{[\s\S]*?flex: 0 0 calc\(\(100% - 27px\) \/ 4\);/);
  assert.match(stylesheet, /\.chat-mode-template-gallery-rail::-webkit-scrollbar \{[\s\S]*?display: none;/);
  assert.match(stylesheet, /@media \(max-width: 640px\)[\s\S]*?\.chat-mode-template-card \{[\s\S]*?flex-basis: 132px;/);
  assert.doesNotMatch(stylesheet, /\.chat-mode-template-gallery-grid/);
});

test("Video mode exposes thirteen distinct playable MP4 templates", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const start = source.indexOf("  video: [", source.indexOf("SIDE_HUSTLE_SAMPLE_DEFINITIONS"));
  const end = source.indexOf("  research: [", start);
  const block = source.slice(start, end);
  const samples = [
    "aurora-app-launch",
    "morrow-fragrance-film",
    "gridline-conference",
    "fieldnote-data-dispatch",
    "casa-sombra-property",
    "pulse-fm-visualizer",
    "orbit-science-explainer",
    "unscripted-podcast-quote",
    "northline-coffee-story",
    "common-ground-impact",
    "classic-cinema-parody",
    "liubang-marketplace-drama",
    "stickman-reset-retry",
  ];

  assert.equal((block.match(/\bid: "/g) || []).length, samples.length);
  assert.equal((block.match(/Code-generated video template/g) || []).length, 10);
  assert.equal((block.match(/label: "Video template"/g) || []).length, 3);

  for (const id of samples) {
    const artifactPath = `/assets/samples/artifacts/video/templates/${id}.mp4`;
    assert.match(
      block,
      new RegExp(`id: "${id}"[\\s\\S]*?sampleSrc: "${artifactPath.replaceAll(".", "\\.")}"[\\s\\S]*?copy: \\{`),
    );
    const paths = [
      artifactPath,
      `/assets/samples/previews/video/${id}.png`,
      ...[1, 2, 3].map((page) => `/assets/samples/details/video/${id}-${String(page).padStart(2, "0")}.png`),
    ];
    for (const path of paths) {
      const asset = await stat(new URL(`../public${path}`, import.meta.url));
      assert.ok(asset.size > 10_000, `${id} should include real generated asset ${path}`);
    }
  }
});

test("Slides, Docs, and Sheets expose every packaged real template", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const modes = [
    {
      key: "slides",
      next: "docs",
      samples: [
        ["aurelia_far_north", "/assets/samples/artifacts/slides/aurelia-far-north.pptx", 3],
        ["null_signal_incident_manual", "/assets/samples/artifacts/slides/null-signal-incident-manual.pptx", 3],
        ["moss_moon_story", "/assets/samples/artifacts/slides/moss-and-moon-story.pptx", 3],
        ["kinetic_autumn_27", "/assets/samples/artifacts/slides/kinetic-autumn-27.pptx", 3],
        ["blue_commons_impact_report", "/assets/samples/artifacts/slides/blue-commons-impact-report.pptx", 3],
        ["monolith_house", "/assets/samples/artifacts/slides/monolith-house.pptx", 3],
        ["orbital_bakery_brand_launch", "/assets/samples/artifacts/slides/orbital-bakery-brand-launch.pptx", 3],
        ["helio_sx_evidence_review", "/assets/samples/artifacts/slides/helio-sx-evidence-review.pptx", 3],
        ["line_47_wayfinding", "/assets/samples/artifacts/slides/line-47-wayfinding.pptx", 3],
        ["cinder_road_film_pitch", "/assets/samples/artifacts/slides/cinder-road-film-pitch.pptx", 3],
      ],
    },
    {
      key: "docs",
      next: "pdf",
      samples: [
        ["shelter_photo_report", "/assets/samples/artifacts/docs/shelter-photo-report.docx", 2],
        ["adaptive_daylight_experiment", "/assets/samples/artifacts/docs/adaptive-daylight-experiment-report.docx", 2],
        ["circular_timber_investment_memo", "/assets/samples/artifacts/docs/circular-timber-investment-memo.docx", 1],
        ["partnership_pilot_letter", "/assets/samples/artifacts/docs/partnership-pilot-letterhead.docx", 1],
        ["personal_company_copy", "/assets/samples/artifacts/docs/personal-company-copy.docx", 3],
        ["side_hustle_product_manual", "/assets/samples/artifacts/docs/side-hustle-product-manual.docx", 3],
        ["travel_ebook_plan", "/assets/samples/artifacts/docs/travel-ebook-plan.docx", 3],
      ],
    },
    {
      key: "sheets",
      next: "website",
      samples: [
        ["atlas_product_roadmap", "/assets/reviews/sheets-final/atlas-product-development.xlsx", 2],
        ["orbit_people_planner", "/assets/reviews/sheets-final/orbit-people-planner.xlsx", 2],
        ["harbor_crm_pipeline", "/assets/reviews/sheets-final/harbor-crm-pipeline.xlsx", 2],
        ["relay_project_portfolio", "/assets/reviews/sheets-final/relay-project-portfolio.xlsx", 2],
        ["northstar_saas_command_center", "/assets/reviews/sheets-final/northstar-saas-command-center.xlsx", 1],
        ["casa_sol_hotel_planner", "/assets/reviews/sheets-final/casa-sol-hotel-planner.xlsx", 1],
        ["night_shift_film_control", "/assets/reviews/sheets-final/night-shift-film-control.xlsx", 1],
        ["verdant_lab_notebook", "/assets/reviews/sheets-final/verdant-lab-notebook.xlsx", 1],
        ["ember_menu_engineering", "/assets/reviews/sheets-final/ember-menu-engineering.xlsx", 1],
        ["pulse_athlete_profile", "/assets/reviews/sheets-final/pulse-athlete-profile.xlsx", 1],
        ["forge_inventory_control", "/assets/reviews/sheets-final/forge-inventory-control.xlsx", 1],
        ["open_hands_grant_pipeline", "/assets/reviews/sheets-final/open-hands-grant-pipeline.xlsx", 1],
        ["ironclad_cost_risk", "/assets/reviews/sheets-final/ironclad-cost-risk.xlsx", 1],
        ["willow_rose_run_of_show", "/assets/reviews/sheets-final/willow-rose-run-of-show.xlsx", 1],
      ],
    },
  ];

  for (const mode of modes) {
    const start = source.indexOf(`  ${mode.key}: [`, source.indexOf("SIDE_HUSTLE_SAMPLE_DEFINITIONS"));
    const end = source.indexOf(`  ${mode.next}: [`, start);
    const block = source.slice(start, end);
    assert.equal((block.match(/\bid: "/g) || []).length, mode.samples.length);

    for (const [id, artifactPath, detailPageCount] of mode.samples) {
      assert.match(block, new RegExp(`id: "${id}"[\\s\\S]*?sampleSrc: "${artifactPath.replaceAll(".", "\\.")}"`));
      const previewPath = mode.key === "docs"
        ? `/assets/samples/details/docs/${id}-01.png`
        : `/assets/samples/previews/${mode.key}/${id}.png`;
      const paths = [artifactPath, previewPath];
      for (let page = 1; page <= detailPageCount; page += 1) {
        paths.push(`/assets/samples/details/${mode.key}/${id}-${String(page).padStart(2, "0")}.png`);
      }
      for (const path of new Set(paths)) {
        const asset = await stat(new URL(`../public${path}`, import.meta.url));
        assert.ok(asset.size > 1_000, `${id} should include real asset ${path}`);
      }
    }
  }
});

test("Image mode exposes twenty additional real reusable templates", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const start = source.indexOf("  image: [", source.indexOf("SIDE_HUSTLE_SAMPLE_DEFINITIONS"));
  const end = source.indexOf("  video: [", start);
  const block = source.slice(start, end);
  const samples = [
    ["luxury_skincare_campaign", "luxury-skincare-campaign.png"],
    ["seasonal_restaurant_campaign", "seasonal-restaurant-campaign.png"],
    ["brutalist_fashion_editorial", "brutalist-fashion-editorial.png"],
    ["modern_home_campaign", "modern-home-campaign.png"],
    ["fitness_wellness_campaign", "fitness-wellness-campaign.png"],
    ["night_music_festival", "night-music-festival.png"],
    ["independent_podcast_cover", "independent-podcast-cover.png"],
    ["coastal_travel_editorial", "coastal-travel-editorial.png"],
    ["mobile_app_launch", "mobile-app-launch.png"],
    ["saas_data_hero", "saas-data-hero.png"],
    ["botanical_book_cover", "botanical-book-cover.png"],
    ["modern_wedding_stationery", "modern-wedding-stationery.png"],
    ["electric_vehicle_campaign", "electric-vehicle-campaign.png"],
    ["pet_care_lifestyle", "pet-care-lifestyle.png"],
    ["woodland_storybook", "woodland-storybook.png"],
    ["fantasy_game_key_art", "fantasy-game-key-art.png"],
    ["artisan_packaging_system", "artisan-packaging-system.png"],
    ["founder_editorial_portrait", "founder-editorial-portrait.png"],
    ["winter_gift_campaign", "winter-gift-campaign.png"],
    ["climate_science_visual", "climate-science-visual.png"],
  ];

  assert.equal((block.match(/\bid: "/g) || []).length, 24);

  for (const [id, filename] of samples) {
    const assetPath = `/assets/samples/artifacts/image/${filename}`;
    assert.match(
      block,
      new RegExp(
        `id: "${id}"[\\s\\S]*?imageSrc: "${assetPath.replaceAll(".", "\\.")}"[\\s\\S]*?sampleSrc: "${assetPath.replaceAll(".", "\\.")}"[\\s\\S]*?copy: \\{`,
      ),
    );
    const asset = await stat(new URL(`../public${assetPath}`, import.meta.url));
    assert.ok(asset.size > 100_000, `${id} should include a real high-resolution image`);
  }
});

test("PDF mode exposes thirty additional rendered templates", async () => {
  const source = await readFile(embeddedChatSource, "utf8");
  const start = source.indexOf("  pdf: [", source.indexOf("SIDE_HUSTLE_SAMPLE_DEFINITIONS"));
  const end = source.indexOf("  sheets: [", start);
  const block = source.slice(start, end);
  const samples = [
    ["northstar_quarterly_review", "northstar-quarterly-business-review.pdf", 2, false],
    ["open_harbor_impact_report", "open-harbor-community-impact-report.pdf", 2, false],
    ["lumen_festival_sponsorship", "lumen-festival-sponsorship-proposal.pdf", 2, false],
    ["relay_product_launch_brief", "relay-product-launch-brief.pdf", 2, false],
    ["casa_lumen_investment_brief", "casa-lumen-real-estate-investment-brief.pdf", 2, false],
    ["morrow_brand_foundations", "morrow-brand-foundations-guide.pdf", 2, false],
    ["ember_table_tasting_menu", "ember-table-seasonal-tasting-menu.pdf", 2, false],
    ["fieldwork_conference_program", "fieldwork-design-conference-program.pdf", 2, false],
    ["good_company_handbook", "good-company-employee-handbook.pdf", 2, false],
    ["verdant_sustainability_scorecard", "verdant-sustainability-scorecard.pdf", 2, false],
    ["orbit_project_status", "orbit-project-status-report.pdf", 2, false],
    ["signal_ux_research_summary", "signal-ux-research-summary.pdf", 2, false],
    ["bright_steps_grant_proposal", "bright-steps-youth-grant-proposal.pdf", 2, false],
    ["clear_path_patient_guide", "clear-path-patient-preparation-guide.pdf", 2, false],
    ["monolith_architecture_portfolio", "monolith-architecture-portfolio.pdf", 2, false],
    ["stillwater_photography_guide", "stillwater-photography-pricing-guide.pdf", 2, false],
    ["decisive_workshop_book", "decisive-team-workshop-workbook.pdf", 2, false],
    ["street_safety_policy_brief", "street-safety-policy-brief.pdf", 2, false],
    ["harbor_financial_summary", "harbor-annual-financial-summary.pdf", 2, false],
    ["slow_coast_itinerary", "slow-coast-seven-day-itinerary.pdf", 2, false],
    ["atelier_north_company_profile", "atelier-north-company-profile.pdf", 3, true],
    ["cascade_growth_business_plan", "cascade-growth-business-plan.pdf", 3, true],
    ["arcline_enterprise_sales_proposal", "arcline-enterprise-sales-proposal.pdf", 3, true],
    ["luma_skincare_product_catalog", "luma-skincare-product-catalog.pdf", 3, true],
    ["studio_kind_services_pricing", "studio-kind-services-pricing-guide.pdf", 3, true],
    ["relay_customer_case_study", "relay-customer-case-study.pdf", 3, true],
    ["earthline_annual_report", "earthline-annual-report.pdf", 3, true],
    ["sol_house_property_brochure", "sol-house-property-brochure.pdf", 3, true],
    ["nightshift_festival_sponsorship", "nightshift-festival-sponsorship-deck.pdf", 3, true],
    ["table_olive_menu_brand_book", "table-olive-menu-brand-book.pdf", 3, true],
  ];

  assert.equal((block.match(/\bid: "/g) || []).length, 34);

  for (const [id, filename, detailPageCount, imageLed] of samples) {
    const artifactPath = `/assets/samples/artifacts/pdf/${filename}`;
    assert.match(
      block,
      new RegExp(
        `id: "${id}"[\\s\\S]*?sampleSrc: "${artifactPath.replaceAll(".", "\\.")}"[\\s\\S]*?detailPageCount: ${detailPageCount}[\\s\\S]*?copy: \\{`,
      ),
    );
    const artifactUrl = new URL(`../public${artifactPath}`, import.meta.url);
    const [artifact, pdfBytes] = await Promise.all([stat(artifactUrl), readFile(artifactUrl)]);
    assert.ok(artifact.size > 2_500, `${id} should include a real PDF`);
    assert.equal(pdfBytes.subarray(0, 4).toString(), "%PDF");
    if (imageLed) {
      assert.ok(artifact.size > 500_000, `${id} should include high-resolution embedded imagery`);
      assert.ok(pdfBytes.includes(Buffer.from("/Subtype /Image")), `${id} should embed real raster images`);
    }
    const renderedPaths = [`/assets/samples/previews/pdf/${id}.png`];
    for (let page = 1; page <= detailPageCount; page += 1) {
      renderedPaths.push(`/assets/samples/details/pdf/${id}-${String(page).padStart(2, "0")}.png`);
    }
    for (const path of renderedPaths) {
      const image = await stat(new URL(`../public${path}`, import.meta.url));
      assert.ok(image.size > 10_000, `${id} should include rendered preview ${path}`);
    }
  }
});

test("every file type uses the same source-template Remix workflow", async () => {
  const [source, gallery, remix, ...surfaces] = await Promise.all([
    readFile(embeddedChatSource, "utf8"),
    readFile(componentSource, "utf8"),
    readFile(templateRemixSource, "utf8"),
    ...chatSurfaceSources.map((surface) => readFile(surface, "utf8")),
  ]);

  assert.doesNotMatch(gallery, /TemplateRemixDialog|requirements: string|setRemixSample/);
  assert.match(gallery, /void onSelect\(sample\)/);
  assert.match(gallery, /onClick=\{\(\) => setPreviewSample\(item\)\}/);

  for (const extension of ["docx", "pptx", "xlsx", "pdf", "html", "png", "mp4"]) {
    assert.match(remix, new RegExp(`\\b${extension}: \\{`));
  }
  assert.match(remix, /sample\.previewContent\?\.sampleSrc/);
  assert.match(remix, /sample\.previewContent\?\.videoSrc/);
  assert.match(remix, /fetch\(source\.sourceUrl, \{ credentials: "same-origin" \}\)/);
  assert.match(remix, /api\.documents\.upload\(templateFile\)/);
  assert.match(remix, /type: "knowledge"/);
  assert.match(remix, /Create a separate new artifact/);
  assert.match(remix, /User requirements for the new artifact/);
  assert.match(remix, /Invoke the document creation skill/);
  assert.match(remix, /Invoke the presentation skill/);
  assert.match(remix, /spreadsheet creation workflow/);
  assert.match(remix, /Invoke the PDF skill/);
  assert.match(remix, /Use the image generation workflow/);
  assert.match(remix, /Use the video creation workflow/);
  assert.match(remix, /responsive website artifact/);

  assert.match(source, /const handleChatModeTemplateSelect = useCallback/);
  assert.match(source, /uploadTemplateRemixSource\(sample\)/);
  assert.match(source, /buildTemplateRemixPrompt/);
  assert.match(source, /onSelect=\{handleChatModeTemplateSelect\}/);
  for (const surface of surfaces.slice(1)) {
    assert.match(surface, /prepareTemplateRemix\(sample\)/);
    assert.match(surface, /setComposerSeed\(/);
    assert.doesNotMatch(surface, /handleSend\(remix\.prompt, remix\.attachments\)/);
  }
});
