import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const configDirectory = new URL(
  "../../../packages/core/blueprints/configs/solo_company/",
  import.meta.url,
);
const themeAwareImageSource = new URL(
  "../src/components/ui/ThemeAwareImage.tsx",
  import.meta.url,
);
const coverTemplateSource = new URL(
  "../src/services/blueprintCoverTemplate.ts",
  import.meta.url,
);
const coverServiceSource = new URL(
  "../../../packages/core/services/blueprint_cover_service.py",
  import.meta.url,
);
const oilPaintingCoverDirectory = new URL(
  "../public/assets/blueprints/oil-paintings/",
  import.meta.url,
);

const blueprintFiles = [
  "product-video-studio-v1.json",
  "solo-content-distribution-studio-v1.json",
  "solo-digital-product-store-v1.json",
  "solo-faceless-stickman-studio-v1.json",
  "solo-productized-service-os-v1.json",
  "solo-video-account-studio-v1.json",
];

const oilPaintingCoverFiles = [
  "automation-launch.webp",
  "content-distribution.webp",
  "creator-fundraising.webp",
  "creator-partnership.webp",
  "digital-store.webp",
  "product-video.webp",
  "productized-service.webp",
  "stickman-storyboard.webp",
  "video-account.webp",
];

const replacementCoverFiles = [
  "content-distribution.png",
  "digital-store.png",
  "product-video.png",
  "productized-service.png",
  "stickman-storyboard.png",
  "video-account.png",
];

test("built-in Marketplace Blueprints exercise the automatic cover fallback", async () => {
  for (const filename of blueprintFiles) {
    const blueprint = JSON.parse(
      await readFile(new URL(filename, configDirectory), "utf8"),
    );
    const { manifest } = blueprint;

    assert.ok(manifest.description, `${filename} needs a description`);
    assert.equal(
      manifest.showcase_assets?.length ?? 0,
      0,
      `${filename} should exercise the generated-cover fallback`,
    );
    assert.equal(manifest.cover_image_url, null);
  }
});

test("description-driven cover templates render stable light and dark SVG", async () => {
  const [themeAwareImage, coverTemplate, coverService, listPage, detailPage] = await Promise.all([
    readFile(themeAwareImageSource, "utf8"),
    readFile(coverTemplateSource, "utf8"),
    readFile(coverServiceSource, "utf8"),
    readFile(new URL("../src/pages/BlueprintList.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/pages/BlueprintDetail.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(themeAwareImage, /attributeFilter: \["data-theme"\]/);
  assert.match(coverService, /normalized_description} \{normalized_description}/);
  assert.match(coverService, /sha256/);
  assert.match(coverTemplate, /viewBox=\"0 0 1200 800\"/);
  assert.match(coverTemplate, /data:image\/svg\+xml/);
  assert.match(coverTemplate, /white:[\s\S]*background: "#ffffff"/);
  assert.match(coverTemplate, /dark:[\s\S]*background: "#090909"/);
  assert.match(coverTemplate, /const SCENE_SIGNALS/);
  assert.match(coverTemplate, /function sceneArtwork/);
  assert.match(coverTemplate, /function sceneLabel/);
  assert.match(coverTemplate, /linearGradient id="cover-gradient"/);
  assert.match(coverTemplate, /CONTENT-DRIVEN COVER/);
  assert.match(coverTemplate, /case "stickman-storyboard":[\s\S]*case "creator-fundraising"/);
  assert.match(coverTemplate, /scene: resolveCoverScene\(semanticText, motif\)/);
  assert.match(coverTemplate, /function blueprintOilPaintingCoverUrl/);
  assert.match(coverTemplate, /function isAutomaticBlueprintCover/);
  assert.match(coverTemplate, /OIL_PAINTING_COVERS/);
  assert.match(listPage, /blueprint\.showcase_assets\.filter/);
  assert.match(listPage, /!isAutomaticBlueprintCover\(asset\.url\)/);
  assert.match(coverTemplate, /resolveBlueprintCoverTemplate/);
  assert.match(listPage, /blueprintCoverDataUrl\(coverTemplate, "dark"\)/);
  assert.match(listPage, /oilPaintingCover[\s\S]*blueprintCoverDataUrl/);
  assert.doesNotMatch(detailPage, /coverTemplate=\{resolveBlueprintCoverTemplate\(bp\)\}/);
  assert.doesNotMatch(detailPage, /blueprintOilPaintingCoverUrl\(coverTemplate\)/);
  assert.doesNotMatch(detailPage, /id: "generated-cover-template"/);
  assert.match(detailPage, /authoredAssets\.length > 0[\s\S]*: \[\];/);
  assert.match(detailPage, /active\.dark_url \?\?/);
});

test("semantic Workspace scenes ship optimized oil-painting covers", async () => {
  for (const filename of oilPaintingCoverFiles) {
    const asset = await readFile(new URL(filename, oilPaintingCoverDirectory));
    assert.ok(asset.length > 80_000, `${filename} should retain painterly detail`);
    assert.ok(asset.length < 260_000, `${filename} should stay card-friendly`);
    assert.equal(asset.subarray(8, 12).toString("ascii"), "WEBP");
  }
});

test("updated Marketplace covers are packaged and mapped", async () => {
  const coverTemplate = await readFile(coverTemplateSource, "utf8");
  for (const filename of replacementCoverFiles) {
    const asset = await readFile(new URL(filename, oilPaintingCoverDirectory));
    assert.ok(asset.length > 500_000, `${filename} should retain illustration detail`);
    assert.ok(asset.length < 4_000_000, `${filename} should stay bounded for web delivery`);
    assert.equal(asset.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
    assert.match(
      coverTemplate,
      new RegExp(`"${filename.replace(".", "\\.")}"`),
      `${filename} should be selected by the cover scene mapping`,
    );
  }
});
