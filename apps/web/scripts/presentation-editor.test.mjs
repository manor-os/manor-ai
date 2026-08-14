#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";
import JSZip from "jszip";

const bundled = await build({
  stdin: {
    contents: 'export { buildPresentationBlob } from "../src/lib/presentationPptx.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString("base64")}`;
const { buildPresentationBlob } = await import(moduleUrl);

const ooxmlBundle = await build({
  stdin: {
    contents: 'export { presentationMediaMime, presentationRelationshipsPart, presentationShapeFillScope, presentationVideoRelationshipIds, presentationVideoSource, resolvePresentationPartTarget } from "../src/lib/presentationOoxml.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const ooxmlModuleUrl = `data:text/javascript;base64,${Buffer.from(ooxmlBundle.outputFiles[0].text).toString("base64")}`;
const {
  presentationMediaMime,
  presentationRelationshipsPart,
  presentationShapeFillScope,
  presentationVideoRelationshipIds,
  presentationVideoSource,
  resolvePresentationPartTarget,
} = await import(ooxmlModuleUrl);

const preservationBundle = await build({
  stdin: {
    contents: 'export { preservePresentationFile } from "../src/lib/presentationOoxmlPatch.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const preservationModuleUrl = `data:text/javascript;base64,${Buffer.from(preservationBundle.outputFiles[0].text).toString("base64")}`;
const { preservePresentationFile } = await import(preservationModuleUrl);
const editabilityBundle = await build({
  stdin: {
    contents: 'export { presentationSlideEditability } from "../src/lib/presentationEditability.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const editabilityModuleUrl = `data:text/javascript;base64,${Buffer.from(editabilityBundle.outputFiles[0].text).toString("base64")}`;
const { presentationSlideEditability } = await import(editabilityModuleUrl);
const liveEditBundle = await build({
  stdin: {
    contents: 'export { applyPresentationLiveEditContent, buildPresentationLiveEditContent, localPresentationLiveEditContent } from "../src/lib/presentationLiveEdit.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const liveEditModuleUrl = `data:text/javascript;base64,${Buffer.from(liveEditBundle.outputFiles[0].text).toString("base64")}`;
const {
  applyPresentationLiveEditContent,
  buildPresentationLiveEditContent,
  localPresentationLiveEditContent,
} = await import(liveEditModuleUrl);
const pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

assert.deepEqual(
  presentationSlideEditability([{
    id: "flattened-slide",
    type: "image",
    x: 0,
    y: 0,
    w: 100,
    h: 100,
    imgUrl: pixel,
    texts: [],
    source: { editable: true },
  }]),
  {
    editableShapeCount: 1,
    lockedShapeCount: 0,
    editableTextCount: 0,
    editableTableCount: 0,
    editableImageCount: 1,
    flattenedImageShapeId: "flattened-slide",
  },
  "full-slide image decks should be identified before users try to edit text inside the image",
);

assert.equal(
  presentationSlideEditability([
    { id: "title", type: "shape", x: 5, y: 5, w: 90, h: 15, texts: [{ text: "Editable title" }], source: { editable: true } },
    { id: "master", type: "shape", x: 0, y: 0, w: 100, h: 100, texts: [], source: { editable: false } },
  ]).lockedShapeCount,
  1,
  "master and layout objects should be reported as read-only",
);

const flattenedSlides = [{
  id: "slide-13",
  sourcePart: "ppt/slides/slide13.xml",
  shapes: [{
    id: "full-page-image",
    type: "image",
    x: 0,
    y: 0,
    w: 100,
    h: 100,
    texts: [],
    imgUrl: pixel,
    source: {
      part: "ppt/slides/slide13.xml",
      kind: "pic",
      objectId: "2",
      editable: true,
      mediaPart: "ppt/media/image13.png",
    },
  }],
}];
const flattenedLiveContent = buildPresentationLiveEditContent(flattenedSlides, {
  documentName: "flattened.pptx",
  activeSlideIndex: 0,
  selectedShapeId: null,
});
const flattenedLiveState = JSON.parse(flattenedLiveContent);
assert.equal(flattenedLiveState.format, "manor-presentation-edit-v1");
assert.equal(flattenedLiveState.targetShapeId, "full-page-image");
assert.equal(flattenedLiveState.slides[0].shapes[0].fullSlide, true);
assert.equal(flattenedLiveState.slides[0].shapes[0].image.attachedAs, "current-slide-1-image.png");
assert.equal(flattenedLiveContent.includes(pixel), false, "live-edit JSON should not inline image bytes");

const marginEdit = localPresentationLiveEditContent("图片缩小并保留 5% 页边距", flattenedLiveContent);
assert.ok(marginEdit, "common full-page image adjustments should have a deterministic fallback");
const marginSlides = applyPresentationLiveEditContent(flattenedSlides, marginEdit);
assert.deepEqual(
  [marginSlides[0].shapes[0].x, marginSlides[0].shapes[0].y, marginSlides[0].shapes[0].w, marginSlides[0].shapes[0].h],
  [5, 5, 90, 90],
);
const cropEdit = localPresentationLiveEditContent("裁掉图片顶部 10%", flattenedLiveContent);
const cropSlides = applyPresentationLiveEditContent(flattenedSlides, cropEdit);
assert.deepEqual(cropSlides[0].shapes[0].imgCrop, { l: 0, t: 10, r: 0, b: 0 });

assert.equal(resolvePresentationPartTarget("ppt/presentation.xml", "slides/slide3.xml"), "ppt/slides/slide3.xml");
assert.equal(resolvePresentationPartTarget("ppt/slides/slide1.xml", "../media/image.png"), "ppt/media/image.png");
assert.equal(resolvePresentationPartTarget("ppt/slides/slide1.xml", "/ppt/media/image.png"), "ppt/media/image.png");
assert.equal(resolvePresentationPartTarget("ppt/slideLayouts/slideLayout2.xml", "../slideMasters/slideMaster1.xml"), "ppt/slideMasters/slideMaster1.xml");
assert.equal(presentationRelationshipsPart("ppt/slides/custom-slide.xml"), "ppt/slides/_rels/custom-slide.xml.rels");
const filledShapeProperties = '<p:spPr><a:solidFill><a:srgbClr val="05070D"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>';
assert.match(presentationShapeFillScope(filledShapeProperties), /<a:solidFill>/);
assert.doesNotMatch(presentationShapeFillScope(filledShapeProperties), /<a:noFill/);
assert.equal(presentationMediaMime("ppt/media/media1.mp4"), "video/mp4");
assert.equal(presentationMediaMime("ppt/media/poster.jpg"), "image/jpeg");
const nativeVideoShape = '<p:pic><p:nvPr><a:videoFile r:link="rId5"/><p14:media r:embed="rId4"/></p:nvPr></p:pic>';
assert.deepEqual(presentationVideoRelationshipIds(nativeVideoShape), ["rId5", "rId4"]);
assert.equal(presentationVideoSource(nativeVideoShape, new Map([["rId4", "blob:media"], ["rId5", "blob:video"]])), "blob:video");

function sourcePartForRelationshipsPart(relationshipsPart) {
  return relationshipsPart
    .replace(/(^|\/)\_rels\//, "$1")
    .replace(/\.rels$/, "");
}

const sampleDirectory = new URL("../public/assets/samples/artifacts/slides/", import.meta.url);
const sampleFiles = (await readdir(sampleDirectory)).filter((name) => name.endsWith(".pptx")).sort();
assert.ok(sampleFiles.length >= 3, "built-in PPTX samples should be available for relationship regression coverage");

// Resolving every non-external target is a property of any valid package, so it is asserted
// per sample. Carrying absolute ("/ppt/...") targets and embedded media are NOT: ECMA-376
// allows both target forms, and a text-only deck legitimately has no media. Those two are
// corpus-coverage assertions — they exist so the resolver keeps end-to-end coverage of both
// branches — so they are asserted across the sample set, not against each individual deck.
let corpusAbsoluteTargets = 0;
let corpusResolvedMediaTargets = 0;
let preservationSampleFile = null;

for (const sampleFile of sampleFiles) {
  const sampleZip = await JSZip.loadAsync(await readFile(new URL(sampleFile, sampleDirectory)));

  for (const relationshipsPart of Object.keys(sampleZip.files).filter((name) => name.endsWith(".rels"))) {
    const sourcePart = sourcePartForRelationshipsPart(relationshipsPart);
    const relationshipsXml = await sampleZip.file(relationshipsPart).async("text");
    for (const relationship of relationshipsXml.match(/<Relationship\b[^>]*\/>/g) || []) {
      if (/TargetMode="External"/i.test(relationship)) continue;
      const target = relationship.match(/Target="([^"]+)"/i)?.[1];
      if (!target) continue;
      if (target.startsWith("/")) corpusAbsoluteTargets += 1;
      const resolved = resolvePresentationPartTarget(sourcePart, target);
      assert.ok(sampleZip.file(resolved), `${sampleFile}: ${target} should resolve to ${resolved}`);
      if (resolved.startsWith("ppt/media/")) corpusResolvedMediaTargets += 1;
    }
  }

  // The preservation round-trip below needs a deck whose first slide carries a picture.
  // Pick it by inspection instead of assuming the alphabetically first sample has one.
  if (!preservationSampleFile) {
    const firstSlide = sampleZip.file("ppt/slides/slide1.xml");
    if (firstSlide && /<p:pic[\s>]/.test(await firstSlide.async("text"))) {
      preservationSampleFile = sampleFile;
    }
  }
}

assert.ok(corpusAbsoluteTargets > 0, "built-in PPTX samples should cover absolute package targets");
assert.ok(corpusResolvedMediaTargets >= 3, "built-in PPTX samples should cover resolvable media relationships");
assert.ok(preservationSampleFile, "at least one built-in PPTX sample should embed an image on its first slide");

const preservationSource = await readFile(new URL(preservationSampleFile, sampleDirectory));
const preservationZip = await JSZip.loadAsync(preservationSource);
preservationZip.file("customXml/manor-preservation-test.xml", "<preserve>unknown OOXML</preserve>");
const preservationInput = await preservationZip.generateAsync({ type: "arraybuffer" });
const presentationXml = await preservationZip.file("ppt/presentation.xml").async("text");
const slideSize = presentationXml.match(/<p:sldSz\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
assert.ok(slideSize, "sample presentation should expose slide dimensions");
const slidePart = "ppt/slides/slide1.xml";
const slideXmlBefore = await preservationZip.file(slidePart).async("text");
const pictureXml = slideXmlBefore.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/)?.[0];
assert.ok(pictureXml, "sample first slide should contain an image object");
const objectId = pictureXml.match(/<p:cNvPr\b[^>]*id="([^"]+)"/)?.[1];
const relationshipId = pictureXml.match(/<a:blip\b[^>]*r:embed="([^"]+)"/)?.[1];
const transform = pictureXml.match(/<a:xfrm\b[^>]*>[\s\S]*?<\/a:xfrm>/)?.[0];
const offset = transform?.match(/<a:off\b[^>]*x="(\d+)"[^>]*y="(\d+)"/);
const extent = transform?.match(/<a:ext\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
assert.ok(objectId && relationshipId && offset && extent, "sample image should expose source identity and transform");
const slideRelationships = await preservationZip.file(presentationRelationshipsPart(slidePart)).async("text");
const mediaTarget = (slideRelationships.match(/<Relationship\b[^>]*\/>/g) || [])
  .find((relationship) => relationship.includes(`Id="${relationshipId}"`))
  ?.match(/Target="([^"]+)"/)?.[1];
assert.ok(mediaTarget, "sample image relationship should expose its media target");
const mediaPart = resolvePresentationPartTarget(slidePart, mediaTarget);
const width = Number(slideSize[1]);
const height = Number(slideSize[2]);
const baselineSlides = [{
  id: "slide-1",
  aspectRatio: `${width}/${height}`,
  sourcePart: slidePart,
  shapes: [{
    id: "image-1",
    type: "image",
    x: (Number(offset[1]) / width) * 100,
    y: (Number(offset[2]) / height) * 100,
    w: (Number(extent[1]) / width) * 100,
    h: (Number(extent[2]) / height) * 100,
    texts: [],
    imgUrl: "unchanged-image-data",
    source: { part: slidePart, kind: "pic", objectId, editable: true, mediaPart },
  }],
}];
const editedSlides = structuredClone(baselineSlides);
editedSlides[0].shapes[0].x += 1;
editedSlides[0].shapes[0].w -= 1;
editedSlides[0].shapes[0].imgCrop = { l: 0, t: 10, r: 0, b: 0 };
editedSlides[0].shapes[0].imgUrl = pixel;
const preservedFile = await preservePresentationFile(preservationInput, baselineSlides, editedSlides, "preserved.pptx");
const preservedZip = await JSZip.loadAsync(await preservedFile.arrayBuffer());
if (process.env.PRESENTATION_TEST_OUTPUT) {
  await writeFile(process.env.PRESENTATION_TEST_OUTPUT, Buffer.from(await preservedFile.arrayBuffer()));
}
assert.equal(await preservedZip.file("customXml/manor-preservation-test.xml").async("text"), "<preserve>unknown OOXML</preserve>");
assert.equal(await preservedZip.file("docProps/core.xml").async("text"), await preservationZip.file("docProps/core.xml").async("text"));
assert.deepEqual(
  Object.keys(preservedZip.files).filter((name) => !preservedZip.files[name].dir).sort(),
  Object.keys(preservationZip.files).filter((name) => !preservationZip.files[name].dir).sort(),
  "incremental save should retain every original package part",
);
assert.notEqual(await preservedZip.file(slidePart).async("text"), slideXmlBefore, "incremental save should patch the edited slide XML");
assert.match(await preservedZip.file(slidePart).async("text"), /<a:srcRect l="0" t="10000" r="0" b="0"\/>/);
assert.deepEqual(
  Buffer.from(await preservedZip.file(mediaPart).async("uint8array")),
  Buffer.from(pixel.split(",")[1], "base64"),
  "incremental save should replace image bytes without rebuilding relationships",
);

const insertedMediaSlides = structuredClone(baselineSlides);
insertedMediaSlides[0].shapes.push({
  id: "inserted-media",
  type: "image",
  x: 60,
  y: 62,
  w: 28,
  h: 22,
  texts: [],
  imgUrl: pixel,
  imageFit: "cover",
  hyperlink: "http://localhost:18080/viewer/video-document",
});
const insertedMediaFile = await preservePresentationFile(
  preservationInput,
  baselineSlides,
  insertedMediaSlides,
  "inserted-media.pptx",
);
const insertedMediaZip = await JSZip.loadAsync(await insertedMediaFile.arrayBuffer());
const insertedMediaSlideXml = await insertedMediaZip.file(slidePart).async("text");
const insertedMediaRelationships = await insertedMediaZip.file(presentationRelationshipsPart(slidePart)).async("text");
const originalMediaParts = Object.keys(preservationZip.files).filter((name) => name.startsWith("ppt/media/") && !preservationZip.files[name].dir);
const updatedMediaParts = Object.keys(insertedMediaZip.files).filter((name) => name.startsWith("ppt/media/") && !insertedMediaZip.files[name].dir);
assert.equal(updatedMediaParts.length, originalMediaParts.length + 1, "fidelity save should add a media package part");
assert.match(insertedMediaSlideXml, /name="Inserted image \d+"/);
assert.match(insertedMediaSlideXml, /<a:hlinkClick r:id="rId\d+"\/>/);
assert.match(insertedMediaRelationships, /relationships\/image/);
assert.match(insertedMediaRelationships, /Target="http:\/\/localhost:18080\/viewer\/video-document" TargetMode="External"/);

const blob = await buildPresentationBlob([
  {
    id: "slide-1",
    bg: "#ffffff",
    aspectRatio: "16/9",
    notes: "Explain the saved editor workflow.",
    shapes: [
      {
        id: "title",
        type: "shape",
        x: 8,
        y: 8,
        w: 84,
        h: 18,
        fill: "#f5f5f4",
        stroke: "#4f7d75",
        strokeWidth: 1,
        presetGeom: "roundRect",
        texts: [{ text: "Editable title", fontSize: 28, bold: true, color: "#1c1917" }],
      },
      {
        id: "image",
        type: "image",
        x: 8,
        y: 32,
        w: 36,
        h: 48,
        imgUrl: pixel,
        imageFit: "cover",
        texts: [],
      },
      {
        id: "table",
        type: "table",
        x: 50,
        y: 32,
        w: 42,
        h: 48,
        texts: [],
        tableRows: [
          [{ text: "Metric", bold: true, fill: "#e7e5e4" }, { text: "Value", bold: true, fill: "#e7e5e4" }],
          [{ text: "Saved" }, { text: "Yes" }],
        ],
        tableColWidths: [1, 1],
      },
    ],
  },
], "Editor smoke test.pptx");

assert.equal(blob.type, "application/vnd.openxmlformats-officedocument.presentationml.presentation");
assert.ok(blob.size > 5_000, "generated presentation should contain an OOXML package");

const zip = await JSZip.loadAsync(await blob.arrayBuffer());
assert.ok(zip.file("ppt/presentation.xml"), "presentation.xml should exist");
assert.ok(zip.file("ppt/slides/slide1.xml"), "first slide should exist");
assert.ok(Object.keys(zip.files).some((name) => name.startsWith("ppt/media/")), "image media should be embedded");

const slideXml = await zip.file("ppt/slides/slide1.xml").async("text");
assert.match(slideXml, /Editable title/);
assert.match(slideXml, /Metric/);
assert.match(slideXml, /Saved/);
assert.ok(Object.keys(zip.files).some((name) => name.startsWith("ppt/notesSlides/notesSlide")), "speaker notes should be embedded");

const titleShapeXml = (slideXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []).find((shape) => shape.includes("Editable title"));
const titleObjectId = titleShapeXml?.match(/<p:cNvPr\b[^>]*id="([^"]+)"/)?.[1];
const titleTransform = titleShapeXml?.match(/<a:xfrm\b[^>]*>[\s\S]*?<\/a:xfrm>/)?.[0];
const titleOffset = titleTransform?.match(/<a:off\b[^>]*x="(\d+)"[^>]*y="(\d+)"/);
const titleExtent = titleTransform?.match(/<a:ext\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
assert.ok(titleObjectId && titleOffset && titleExtent, "generated text should expose editable OOXML identity");
const generatedPresentationXml = await zip.file("ppt/presentation.xml").async("text");
const generatedSlideSize = generatedPresentationXml.match(/<p:sldSz\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
assert.ok(generatedSlideSize, "generated presentation should expose slide dimensions");
const generatedWidth = Number(generatedSlideSize[1]);
const generatedHeight = Number(generatedSlideSize[2]);
const generatedBaseline = [{
  id: "slide-1",
  sourcePart: "ppt/slides/slide1.xml",
  shapes: [{
    id: "title",
    type: "shape",
    x: (Number(titleOffset[1]) / generatedWidth) * 100,
    y: (Number(titleOffset[2]) / generatedHeight) * 100,
    w: (Number(titleExtent[1]) / generatedWidth) * 100,
    h: (Number(titleExtent[2]) / generatedHeight) * 100,
    texts: [{ text: "Editable title", fontSize: 28, bold: true, color: "#1c1917" }],
    source: { part: "ppt/slides/slide1.xml", kind: "sp", objectId: titleObjectId, editable: true },
  }],
}];
const generatedEdited = structuredClone(generatedBaseline);
generatedEdited[0].shapes[0].texts[0].text = "Preserved editable title";
const textPatchedFile = await preservePresentationFile(await blob.arrayBuffer(), generatedBaseline, generatedEdited, "text-patched.pptx");
const textPatchedZip = await JSZip.loadAsync(await textPatchedFile.arrayBuffer());
const textPatchedXml = await textPatchedZip.file("ppt/slides/slide1.xml").async("text");
assert.match(textPatchedXml, /Preserved editable title/);
assert.match(textPatchedXml, /Metric/);
assert.match(textPatchedXml, /Saved/);

console.log("presentation editor export smoke test passed");
