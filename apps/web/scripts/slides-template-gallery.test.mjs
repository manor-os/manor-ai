import assert from "node:assert/strict";
import { access, readFile, stat } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import JSZip from "jszip";

const webRoot = new URL("../", import.meta.url);
const componentSource = new URL("src/components/EmbeddedChat.tsx", webRoot);
const cssSource = new URL("src/index.css", webRoot);
const localeSources = ["en", "zh", "es"].map(
  (locale) => new URL(`src/lib/i18n/${locale}.ts`, webRoot),
);
const sampleRoot = new URL("public/assets/samples/", webRoot);

/* The one-image-per-slide deck, kept as a fixture for the render-mode test. */
const ADVANCED_DECK = {
  id: "manor_advanced_presentation",
  file: "manor-advanced-presentation-mode.pptx",
  pages: 6,
};

/*
 * The gallery is read out of the source rather than pinned here. It has been
 * rebuilt wholesale once already, and a hardcoded roster turns every content
 * decision into a red test that says nothing about whether the feature works.
 */
function shippedSlidesTemplates(source) {
  const block = source.match(/slides: \[([\s\S]*?)\n  \],\n  docs:/)?.[1];
  assert.ok(block, "Slides sample definitions should be present");
  const templates = block
    .split(/\n    \{\n/)
    .slice(1)
    .map((entry) => ({
      id: entry.match(/id: "([^"]+)"/)?.[1],
      file: entry.match(/sampleSrc: "\/assets\/samples\/artifacts\/slides\/([^"]+)"/)?.[1],
      pages: Number(entry.match(/detailPageCount: (\d+)/)?.[1] ?? 3),
      render: entry.match(/chatModePayloadPatch: \{ render: "([^"]+)" \}/)?.[1],
      hasInlineCopy: /copy: \{/.test(entry),
    }));
  assert.ok(templates.length >= 4, "the Slides gallery should offer a full row of templates");
  return templates;
}

async function assertPptxRelationshipsResolve(zip, file) {
  const names = new Set(Object.keys(zip.files));
  const relationshipFiles = [...names].filter((name) => name.endsWith(".rels"));

  for (const relationshipFile of relationshipFiles) {
    const xml = await zip.file(relationshipFile)?.async("string");
    assert.ok(xml, `${file}: ${relationshipFile} should be readable`);
    const baseDir = relationshipFile === "_rels/.rels"
      ? ""
      : path.posix.dirname(path.posix.dirname(relationshipFile));

    for (const tag of xml.match(/<Relationship\b[^>]*>/g) || []) {
      if (/TargetMode=["']External["']/.test(tag)) continue;
      const target = tag.match(/Target=["']([^"']+)["']/)?.[1];
      assert.ok(target, `${file}: relationship target should be present`);
      const resolved = decodeURI(
        target.startsWith("/")
          ? target.slice(1)
          : path.posix.normalize(path.posix.join(baseDir, target)),
      );
      assert.ok(
        names.has(resolved),
        `${file}: ${relationshipFile} points to missing ${resolved}`,
      );
    }
  }
}

test("Slides templates expose preview, PPTX, and Remix actions", async () => {
  const [source, css] = await Promise.all([
    readFile(componentSource, "utf8"),
    readFile(cssSource, "utf8"),
  ]);

  assert.match(
    source,
    /className="workspace-sample-grid workspace-sample-grid--previewable"/,
    "Every sample mode should use the previewable gallery path",
  );
  assert.match(
    source,
    /className="workspace-sample-card workspace-sample-card--previewable"/,
    "Every sample card should expose the shared preview actions",
  );
  assert.doesNotMatch(source, /PREVIEWABLE_SAMPLE_CAPABILITIES/);
  assert.match(source, /component\.embedded_chat\.quick_preview/);
  assert.match(source, /component\.embedded_chat\.open_presentation/);
  assert.match(source, /component\.embedded_chat\.remix/);
  const templates = shippedSlidesTemplates(source);
  for (const template of templates) {
    assert.ok(template.id, "every Slides template needs an id");
    assert.ok(
      template.file,
      `${template.id} must point at a packaged deck under artifacts/slides`,
    );
    // Remix sends the deck through Manor with the sample's render parameters.
    // Leaving this off silently inherits whatever the composer was last set
    // to, so an editable template can come back as flat images.
    assert.ok(
      template.render,
      `${template.id} must state the render mode Remix should request`,
    );
  }
  /*
   * The patch may be spread inline or read into a local first. What has to
   * hold is that Remix MERGES the sample's render parameters onto the current
   * payload — replacing it loses the composer's other settings, and dropping
   * it sends the deck through whatever render mode was last selected.
   */
  const payloadMerge = source.match(
    /setChatModePayload\(\(current\) => \(\{\s*\.\.\.current,\s*\.\.\.([\w.]+),?\s*\}\)\)/,
  );
  assert.ok(payloadMerge, "Remix must merge into the existing composer payload");
  assert.match(
    payloadMerge[1],
    /[Cc]hatModePayloadPatch/,
    "Remix must merge the selected sample's render parameters into the Manor request",
  );
  assert.match(css, /workspace-sample-preview--slides[\s\S]*?aspect-ratio: 16 \/ 9/);
  assert.match(
    css,
    /workspace-sample-preview--pdf[\s\S]*?aspect-ratio: 16 \/ 9/,
    "Previewable PDF cards should use the same 16:9 canvas as Slides",
  );
  assert.match(
    css,
    /\.workspace-sample-card--previewable[\s\S]*?\.workspace-sample-preview--browseable[\s\S]*?object-fit: cover/,
  );

});

test("Advanced Slides sample is a one-image-per-slide PPTX", async () => {
  const advanced = ADVANCED_DECK;
  const deck = new URL(`artifacts/slides/${advanced.file}`, sampleRoot);
  const zip = await JSZip.loadAsync(await readFile(deck));
  const presentationXml = await zip.file("ppt/presentation.xml")?.async("string");
  assert.ok(presentationXml);
  assert.match(presentationXml, /<p:sldSz cx="12192000" cy="6858000"/);

  const mediaFiles = Object.keys(zip.files).filter((name) => /^ppt\/media\/image\d+\.(?:png|jpe?g|webp)$/i.test(name));
  assert.equal(mediaFiles.length, advanced.pages);

  for (let page = 1; page <= advanced.pages; page += 1) {
    const slideXml = await zip.file(`ppt/slides/slide${page}.xml`)?.async("string");
    assert.ok(slideXml, `slide ${page} XML should exist`);
    assert.equal((slideXml.match(/<p:pic>/g) || []).length, 1, `slide ${page} should contain exactly one image`);
    assert.doesNotMatch(slideXml, /<p:sp>/, `slide ${page} should not contain editable text or vector shapes`);
    assert.match(slideXml, /<a:off x="0" y="0"\/>/);
    assert.match(slideXml, /<a:ext cx="12192000" cy="6858000"\/>/);
  }
});

test("Slides templates ship complete metadata and preview assets", async () => {
  const [source, ...localeContents] = await Promise.all([
    readFile(componentSource, "utf8"),
    ...localeSources.map((locale) => readFile(locale, "utf8")),
  ]);
  const templates = shippedSlidesTemplates(source);

  for (const locale of localeContents) {
    assert.match(locale, /"component\.embedded_chat\.open_presentation"/);
  }

  const COPY_FIELDS = [
    "title",
    "outcome",
    "prompt",
    "label",
    "preview_title",
    "line_1",
    "line_2",
    "chip_1",
    "chip_2",
  ];

  for (const template of templates) {
    /*
     * `createSideHustleSample` resolves each field as `copy?.[field] ??
     * sideHustleText(...)`, so a template carries its own copy or leans on the
     * side_hustle keys. Either is fine; neither is not — the card would render
     * raw key paths. Inline copy needs no per-field check here because its
     * type makes all nine fields required.
     */
    if (!template.hasInlineCopy) {
      const prefix = `component.embedded_chat.side_hustle.slides.${template.id}`;
      for (const locale of localeContents) {
        for (const field of COPY_FIELDS) {
          assert.ok(
            locale.includes(`"${prefix}.${field}"`),
            `${prefix}.${field} is required for a template with no inline copy`,
          );
        }
      }
    }

    const deck = new URL(`artifacts/slides/${template.file}`, sampleRoot);
    await access(deck);
    assert.ok((await stat(deck)).size > 20_000, `${template.file} should contain a real deck`);
    const zip = await JSZip.loadAsync(await readFile(deck));
    const slideFiles = Object.keys(zip.files).filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name));
    // The card offers `pages` detail previews; a deck shorter than that is
    // advertising slides it does not have.
    assert.ok(
      slideFiles.length >= template.pages,
      `${template.file} has ${slideFiles.length} slides but the card previews ${template.pages} pages`,
    );
    await assertPptxRelationshipsResolve(zip, template.file);
    await access(new URL(`previews/slides/${template.id}.png`, sampleRoot));
    for (let page = 1; page <= template.pages; page += 1) {
      const suffix = String(page).padStart(2, "0");
      await access(new URL(`details/slides/${template.id}-${suffix}.png`, sampleRoot));
    }
  }
});
