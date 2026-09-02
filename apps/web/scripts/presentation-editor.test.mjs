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
    contents: 'export { presentationColorWithAlpha, presentationGroupContent, presentationGroupTransform, presentationInverseTransform, presentationMediaMime, presentationObjectGroups, presentationObjectIdsInOrder, presentationRelationshipsPart, presentationResizeRect, presentationShapeFillScope, presentationShapeTransform, presentationTransformBounds, presentationTransformPoint, presentationVideoRelationshipIds, presentationVideoSource, resolvePresentationPartTarget } from "../src/lib/presentationOoxml.ts";',
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
  presentationColorWithAlpha,
  presentationGroupContent,
  presentationGroupTransform,
  presentationInverseTransform,
  presentationMediaMime,
  presentationObjectGroups,
  presentationObjectIdsInOrder,
  presentationRelationshipsPart,
  presentationResizeRect,
  presentationShapeFillScope,
  presentationShapeTransform,
  presentationTransformBounds,
  presentationTransformPoint,
  presentationVideoRelationshipIds,
  presentationVideoSource,
  resolvePresentationPartTarget,
} = await import(ooxmlModuleUrl);
assert.deepEqual(
  presentationObjectIdsInOrder('<p:sp><p:cNvPr id="4"/></p:sp><p:pic><p:cNvPr id="2"/></p:pic><p:cxnSp><p:cNvPr id="7"/></p:cxnSp>'),
  ["4", "2", "7"],
  "mixed presentation objects must keep their DrawingML paint order",
);
const nestedGroupProbe = '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="10"/></p:nvGrpSpPr>'
  + '<p:grpSpPr><a:xfrm><a:off x="100" y="200"/><a:ext cx="200" cy="400"/><a:chOff x="0" y="0"/><a:chExt cx="100" cy="100"/></a:xfrm></p:grpSpPr>'
  + '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="11"/></p:nvGrpSpPr>'
  + '<p:grpSpPr><a:xfrm><a:off x="10" y="20"/><a:ext cx="50" cy="50"/><a:chOff x="0" y="0"/><a:chExt cx="10" cy="10"/></a:xfrm></p:grpSpPr>'
  + '<p:sp><p:nvSpPr><p:cNvPr id="12"/></p:nvSpPr></p:sp></p:grpSp></p:grpSp>';
const nestedGroupProbeGroups = presentationObjectGroups(nestedGroupProbe);
assert.equal(nestedGroupProbeGroups.length, 1, "nested group scanning must return the complete outer group");
assert.deepEqual([...nestedGroupProbeGroups[0].objectIds], ["10", "11", "12"]);
const nestedGroupProbeContent = presentationGroupContent(nestedGroupProbeGroups[0].xml);
assert.equal(nestedGroupProbeContent.nestedGroups.length, 1);
assert.doesNotMatch(nestedGroupProbeContent.directXml, /id="12"/);
assert.deepEqual(
  presentationGroupTransform(
    nestedGroupProbeContent.nestedGroups[0].xml,
    presentationGroupTransform(nestedGroupProbeGroups[0].xml),
  ),
  { a: 10, b: 0, c: 0, d: 20, e: 120, f: 280 },
  "nested group transforms must compose into slide coordinates",
);
const rotatedGroupProbe = '<p:grpSp><p:grpSpPr><a:xfrm rot="5400000" flipH="1"><a:off x="0" y="0"/><a:ext cx="200" cy="100"/><a:chOff x="0" y="0"/><a:chExt cx="200" cy="100"/></a:xfrm></p:grpSpPr></p:grpSp>';
const rotatedGroupTransform = presentationGroupTransform(rotatedGroupProbe);
const rotatedRectSource = {
  x: 20, y: 10, width: 40, height: 20, rotation: 0, flipH: false, flipV: false,
};
const rotatedShapeTransform = presentationShapeTransform(rotatedGroupTransform, rotatedRectSource);
assert.ok(rotatedShapeTransform.a * rotatedShapeTransform.d - rotatedShapeTransform.b * rotatedShapeTransform.c < 0);
const rotatedShapeInverse = presentationInverseTransform(rotatedShapeTransform);
assert.ok(rotatedShapeInverse, "valid grouped shape transforms must be invertible");
const rotatedPoint = presentationTransformPoint(rotatedShapeTransform, 27, 14);
const restoredPoint = presentationTransformPoint(rotatedShapeInverse, rotatedPoint.x, rotatedPoint.y);
assert.ok(Math.abs(restoredPoint.x - 27) < 1e-9 && Math.abs(restoredPoint.y - 14) < 1e-9);
const shearedRectSource = { x: 10, y: 20, width: 100, height: 40, rotation: 45 };
const shearedShapeTransform = presentationShapeTransform(
  { a: 2, b: 0, c: 0, d: 1, e: 0, f: 0 },
  shearedRectSource,
);
assert.ok(
  Math.abs(shearedShapeTransform.a * shearedShapeTransform.c + shearedShapeTransform.b * shearedShapeTransform.d) > 0.1,
  "non-uniform grouped rotations must retain their non-orthogonal axes",
);
const shearedBounds = presentationTransformBounds(shearedShapeTransform, shearedRectSource);
assert.ok(shearedBounds.width > 0 && shearedBounds.height > 0);
const shearedShapeInverse = presentationInverseTransform(shearedShapeTransform);
assert.ok(shearedShapeInverse);
const shearedPoint = presentationTransformPoint(shearedShapeTransform, 33, 29);
const restoredShearedPoint = presentationTransformPoint(shearedShapeInverse, shearedPoint.x, shearedPoint.y);
assert.ok(Math.abs(restoredShearedPoint.x - 33) < 1e-9 && Math.abs(restoredShearedPoint.y - 29) < 1e-9);
const resizedShearedRect = presentationResizeRect(
  shearedRectSource,
  "e",
  {
    x: shearedShapeTransform.a * 20,
    y: shearedShapeTransform.b * 20,
  },
  { a: 2, b: 0, c: 0, d: 1, e: 0, f: 0 },
  { width: 3, height: 3 },
);
assert.ok(Math.abs(resizedShearedRect.width - 120) < 1e-9);
const originalShearedWest = presentationTransformPoint(
  shearedShapeTransform,
  shearedRectSource.x,
  shearedRectSource.y + shearedRectSource.height / 2,
);
const resizedShearedTransform = presentationShapeTransform(
  { a: 2, b: 0, c: 0, d: 1, e: 0, f: 0 },
  resizedShearedRect,
);
const resizedShearedWest = presentationTransformPoint(
  resizedShearedTransform,
  resizedShearedRect.x,
  resizedShearedRect.y + resizedShearedRect.height / 2,
);
assert.ok(
  Math.abs(originalShearedWest.x - resizedShearedWest.x) < 1e-9
    && Math.abs(originalShearedWest.y - resizedShearedWest.y) < 1e-9,
  "resizing a sheared grouped shape must keep the opposite visible handle fixed",
);
const resizeProbeRect = {
  x: 30,
  y: 25,
  width: 110,
  height: 70,
  rotation: 31,
  flipH: true,
  flipV: false,
};
const resizeProbeGroup = { a: 1.7, b: 0.18, c: -0.12, d: 0.85, e: 42, f: -17 };
const resizeProbeTransform = presentationShapeTransform(resizeProbeGroup, resizeProbeRect);
for (const handle of ["n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
  const localPointerDelta = {
    x: handle.includes("e") ? 16 : handle.includes("w") ? -12 : 0,
    y: handle.includes("s") ? 11 : handle.includes("n") ? -9 : 0,
  };
  const resized = presentationResizeRect(
    resizeProbeRect,
    handle,
    {
      x: resizeProbeTransform.a * localPointerDelta.x + resizeProbeTransform.c * localPointerDelta.y,
      y: resizeProbeTransform.b * localPointerDelta.x + resizeProbeTransform.d * localPointerDelta.y,
    },
    resizeProbeGroup,
    { width: 3, height: 3 },
  );
  const oppositePoint = (rect) => ({
    x: handle.includes("e") ? rect.x : handle.includes("w") ? rect.x + rect.width : rect.x + rect.width / 2,
    y: handle.includes("s") ? rect.y : handle.includes("n") ? rect.y + rect.height : rect.y + rect.height / 2,
  });
  const originalOpposite = oppositePoint(resizeProbeRect);
  const resizedOpposite = oppositePoint(resized);
  const originalOppositeOnSlide = presentationTransformPoint(
    resizeProbeTransform,
    originalOpposite.x,
    originalOpposite.y,
  );
  const resizedOppositeOnSlide = presentationTransformPoint(
    presentationShapeTransform(resizeProbeGroup, resized),
    resizedOpposite.x,
    resizedOpposite.y,
  );
  assert.ok(
    Math.abs(originalOppositeOnSlide.x - resizedOppositeOnSlide.x) < 1e-8
      && Math.abs(originalOppositeOnSlide.y - resizedOppositeOnSlide.y) < 1e-8,
    `resize handle ${handle} must keep its opposite visible anchor fixed`,
  );
}
const docEditorSource = await readFile(new URL("../src/pages/DocEditor.tsx", import.meta.url), "utf8");
assert.match(docEditorSource, /const groupMatches = presentationObjectGroups\(xml\)/);
assert.doesNotMatch(docEditorSource, /xml\.match\(\/<p:grpSp\[\\s>\]\[\\s\\S\]\*\?<\\\/p:grpSp>\/g\)/);
const groupParserStart = docEditorSource.indexOf("const parseGroup =");
const groupParserSource = docEditorSource.slice(
  groupParserStart,
  docEditorSource.indexOf("for (const group of groupMatches)", groupParserStart),
);
assert.match(groupParserSource, /\{ part: slidePath, kind, editable: true, mediaParts \}/);
assert.match(docEditorSource, /shape\.source\.groupTransform = transform/);
assert.match(docEditorSource, /shape\.source\.groupPath = groupPath/);
assert.doesNotMatch(docEditorSource, /presentationTransformRect/);
assert.match(docEditorSource, /const sourcePaintOrder = new Map/);
assert.doesNotMatch(docEditorSource, /graphicPreviewCrop/);
assert.match(docEditorSource, /api\.documents\.presentationObjectBlob/);
assert.match(docEditorSource, /src=\{shape\.graphicPreviewUrl\}/);
const styleInheritanceBundle = await build({
  stdin: {
    contents: 'export { officeCompatibleFontFamily } from "../src/lib/officeFonts.ts"; export { presentationInheritedTextStyleLevels, presentationPointsToCqh } from "../src/lib/presentationStyleInheritance.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const styleInheritanceModuleUrl = `data:text/javascript;base64,${Buffer.from(styleInheritanceBundle.outputFiles[0].text).toString("base64")}`;
const { officeCompatibleFontFamily, presentationInheritedTextStyleLevels, presentationPointsToCqh } = await import(styleInheritanceModuleUrl);

const preservationBundle = await build({
  stdin: {
    contents: 'export { preservePresentationFile, preservePresentationFileWithSnapshot } from "../src/lib/presentationOoxmlPatch.ts";',
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
const {
  preservePresentationFile,
  preservePresentationFileWithSnapshot,
} = await import(preservationModuleUrl);
const editabilityBundle = await build({
  stdin: {
    contents: 'export { presentationShapesForDuplicateSlide, presentationSlideEditability } from "../src/lib/presentationEditability.ts";',
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
const { presentationShapesForDuplicateSlide, presentationSlideEditability } = await import(editabilityModuleUrl);
assert.deepEqual(
  presentationShapesForDuplicateSlide([
    { id: "generated" },
    { id: "slide-owned-locked", source: { part: "ppt/slides/slide1.xml" } },
    { id: "layout-decoration", source: { part: "ppt/slideLayouts/slideLayout1.xml" } },
    { id: "master-decoration", source: { part: "ppt/slideMasters/slideMaster1.xml" } },
  ], "ppt/slides/slide1.xml").map((shape) => shape.id),
  ["generated", "slide-owned-locked"],
  "duplicating a slide must keep its own objects and let the copied layout/master supply inherited decorations",
);
const liveEditBundle = await build({
  stdin: {
    contents: 'export { applyPresentationLiveEditContent, buildPresentationLiveEditContent } from "../src/lib/presentationLiveEdit.ts";',
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
} = await import(liveEditModuleUrl);
const textEditsBundle = await build({
  stdin: {
    contents: 'export { presentationTextEditSpansFromSourceMap, rebasePresentationTextSourceMap, reconcilePresentationTextRuns, reconcilePresentationTextSourceMap } from "../src/lib/presentationTextEdits.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const textEditsModuleUrl = `data:text/javascript;base64,${Buffer.from(textEditsBundle.outputFiles[0].text).toString("base64")}`;
const {
  presentationTextEditSpansFromSourceMap,
  rebasePresentationTextSourceMap,
  reconcilePresentationTextRuns,
  reconcilePresentationTextSourceMap,
} = await import(textEditsModuleUrl);
const pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

const inheritedMasterXml = `<p:sldMaster><p:spTree>
  <p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:lstStyle/></p:txBody></p:sp>
  <p:sp><p:nvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:txBody><a:lstStyle/></p:txBody></p:sp>
  </p:spTree><p:txStyles>
  <p:titleStyle><a:lvl1pPr algn="ctr"><a:buNone/><a:defRPr sz="4400"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mj-lt"/></a:defRPr></a:lvl1pPr></p:titleStyle>
  <p:bodyStyle><a:lvl1pPr marL="342900" marR="114300" indent="-342900"><a:spcBef><a:spcPct val="20000"/></a:spcBef><a:buChar char="•"/><a:defRPr sz="3200"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr></a:lvl1pPr></p:bodyStyle>
  </p:txStyles></p:sldMaster>`;
const inheritedLayoutXml = `<p:sldLayout><p:spTree>
  <p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:lstStyle/></p:txBody></p:sp>
  <p:sp><p:nvSpPr><p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr><p:txBody><a:lstStyle/></p:txBody></p:sp>
  </p:spTree></p:sldLayout>`;
const inheritedResolvers = {
  color: (xml) => xml.includes('schemeClr val="tx1"') ? "#111111" : xml.includes('schemeClr val="lt1"') ? "#ffffff" : undefined,
  font: (font) => font === "+mj-lt" ? "Major Theme" : font === "+mn-lt" ? "Minor Theme" : font,
};
const inheritedTitleStyles = presentationInheritedTextStyleLevels(
  '<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:lstStyle/></p:txBody></p:sp>',
  inheritedLayoutXml,
  inheritedMasterXml,
  inheritedResolvers,
);
assert.deepEqual(
  { fontSize: inheritedTitleStyles[0].fontSize, fontFamily: inheritedTitleStyles[0].fontFamily, color: inheritedTitleStyles[0].color, align: inheritedTitleStyles[0].align, bullet: inheritedTitleStyles[0].bullet },
  { fontSize: 44, fontFamily: "Major Theme", color: "#111111", align: "ctr", bullet: null },
  "title placeholders must inherit theme-backed master typography",
);
const inheritedBodyStyles = presentationInheritedTextStyleLevels(
  '<p:sp><p:nvSpPr><p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr><p:txBody><a:lstStyle/></p:txBody></p:sp>',
  inheritedLayoutXml,
  inheritedMasterXml,
  inheritedResolvers,
);
assert.deepEqual(
  { fontSize: inheritedBodyStyles[0].fontSize, fontFamily: inheritedBodyStyles[0].fontFamily, color: inheritedBodyStyles[0].color, indent: inheritedBodyStyles[0].indent, indentRight: inheritedBodyStyles[0].indentRight, hanging: inheritedBodyStyles[0].hanging, spaceBefore: inheritedBodyStyles[0].spaceBefore, bullet: inheritedBodyStyles[0].bullet },
  { fontSize: 32, fontFamily: "Minor Theme", color: "#111111", indent: 27, indentRight: 9, hanging: -27, spaceBefore: 6.4, bullet: "•" },
  "body placeholders must inherit master list styling when slide runs omit formatting",
);
assert.equal(
  presentationPointsToCqh(54),
  "calc(10.000cqh * var(--pptx-point-scale, 1))",
  "PowerPoint point sizes must scale against the editor's canonical slide height",
);
assert.equal(
  officeCompatibleFontFamily("Calibri Light"),
  "Carlito",
  "missing Calibri variants must use the bundled metric-compatible font",
);
const styledTextBox = presentationInheritedTextStyleLevels(
  '<p:sp><p:style><a:fontRef idx="major"><a:schemeClr val="lt1"/></a:fontRef></p:style><p:txBody><a:lstStyle/></p:txBody></p:sp>',
  undefined,
  inheritedMasterXml,
  inheritedResolvers,
);
assert.deepEqual(
  { fontFamily: styledTextBox[0].fontFamily, color: styledTextBox[0].color },
  { fontFamily: "Major Theme", color: "#ffffff" },
  "shape font references must override master text defaults",
);

assert.deepEqual(
  reconcilePresentationTextSourceMap("11", "1", undefined, [{
    originalStart: 0,
    originalEnd: 1,
    editedStart: 0,
    editedEnd: 0,
  }]),
  [1],
  "an explicit deletion range must retain the identity of the second duplicate character",
);
assert.deepEqual(
  presentationTextEditSpansFromSourceMap("11", "1", [1]),
  [{ originalStart: 0, originalEnd: 1, editedStart: 0, editedEnd: 0 }],
  "source-mapped edits must patch the character the user actually deleted",
);
assert.deepEqual(
  reconcilePresentationTextRuns(
    [{ text: "1", color: "#0000ff" }, { text: "1", color: "#ff0000" }],
    "11",
    "1",
    [1],
  ),
  [{ text: "1", color: "#ff0000" }],
  "rich-text reconciliation must retain the formatting of the source-mapped duplicate character",
);
assert.deepEqual(
  rebasePresentationTextSourceMap("11", "1", [0, 1], [1]),
  [1],
  "save completion must rebase exact edit provenance onto the newly saved text",
);
const longSourceText = `${"A".repeat(300)}1${"B".repeat(300)}`;
const longEditedText = `${"X".repeat(300)}1${"Y".repeat(300)}`;
const longSourceMap = reconcilePresentationTextSourceMap(longSourceText, longEditedText);
assert.equal(
  longSourceMap[300],
  300,
  "long replacements must retain a unique unchanged field anchor beyond the bounded Myers diff",
);

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
assert.equal(flattenedLiveState.format, "manor-presentation-edit-v2");
assert.match(flattenedLiveState.instructions.join("\n"), /To add a slide/);
assert.equal(flattenedLiveState.targetShapeId, "full-page-image");
assert.equal(flattenedLiveState.slides[0].shapes[0].fullSlide, true);
assert.equal(flattenedLiveState.slides[0].shapes[0].image.attachedAs, "current-slide-1-image.png");
assert.equal(flattenedLiveContent.includes(pixel), false, "live-edit JSON should not inline image bytes");

const unsavedImageSlides = [{
  id: "unsaved-image-slide",
  shapes: [{
    id: "unsaved-image",
    type: "image",
    x: 10,
    y: 10,
    w: 40,
    h: 40,
    rotation: 0,
    texts: [],
    imgUrl: pixel,
  }],
}];
const unsavedImageState = JSON.parse(buildPresentationLiveEditContent(unsavedImageSlides, {
  documentName: "unsaved-image.pptx",
  activeSlideIndex: 0,
  selectedShapeId: "unsaved-image",
}));
unsavedImageState.slides[0].shapes[0].paragraphs = [{
  index: 0,
  text: "This text cannot be persisted by p:pic",
  edits: [],
}];
assert.equal(
  applyPresentationLiveEditContent(unsavedImageSlides, JSON.stringify(unsavedImageState)),
  null,
  "source-less browser images must reject text that the PPTX picture saver would discard",
);

const structuralLiveState = structuredClone(flattenedLiveState);
structuralLiveState.slides.push({
  id: "ai-slide-core-capabilities",
  slideNumber: 2,
  active: false,
  create: {
    layout: "title-body",
    title: "Core capabilities",
    body: "Plan and execute work\nSearch and synthesize knowledge\nCoordinate specialized agents",
  },
});
const structurallyLiveEdited = applyPresentationLiveEditContent(
  flattenedSlides,
  JSON.stringify(structuralLiveState),
);
assert.ok(structurallyLiveEdited, "AI live edit should create a reviewable slide");
assert.equal(structurallyLiveEdited.length, 2);
assert.equal(structurallyLiveEdited[1].id, "ai-slide-core-capabilities");
assert.equal(structurallyLiveEdited[1].shapes[0].texts[0].text, "Core capabilities");
assert.deepEqual(
  structurallyLiveEdited[1].shapes[1].texts.map((paragraph) => paragraph.text),
  ["Plan and execute work", "Search and synthesize knowledge", "Coordinate specialized agents"],
);
assert.equal(
  applyPresentationLiveEditContent(structurallyLiveEdited, JSON.stringify(structuralLiveState)).length,
  2,
  "reapplying cumulative streamed state must not duplicate AI-created slides",
);

const objectCapabilityState = structuredClone(flattenedLiveState);
objectCapabilityState.slides[0].notes = "Generated and editable speaker notes";
objectCapabilityState.slides[0].background = {
  color: null,
  gradient: {
    angle: 25,
    stops: [
      { pos: 0, color: "#f8fafc", alpha: 1 },
      { pos: 100, color: "#dbeafe", alpha: 1 },
    ],
  },
};
objectCapabilityState.slides[0].shapes.push({
  id: "ai-shape-callout",
  type: "shape",
  editable: true,
  fullSlide: false,
  create: { operation: "shape.insert" },
  x: 8,
  y: 12,
  w: 42,
  h: 20,
  rotation: 0,
  flipH: false,
  flipV: false,
  format: {
    fill: "#0f766e",
    stroke: "#134e4a",
    strokeWidth: 2,
    preset: "roundRect",
    borderRadius: 18,
    verticalAlignment: "middle",
    wordWrap: true,
    padding: { l: 8, t: 4, r: 8, b: 4 },
  },
  paragraphs: [{
    index: 0,
    text: "Native AI-created callout",
    edits: [],
    format: { bold: true, fontSize: 22, color: "#ffffff", align: "center" },
  }],
});
objectCapabilityState.slides[0].shapes.push({
  id: "ai-shape-table",
  type: "table",
  editable: true,
  fullSlide: false,
  create: { operation: "table.insert" },
  x: 8,
  y: 40,
  w: 58,
  h: 28,
  rotation: 0,
  flipH: false,
  flipV: false,
  table: [
    [
      { row: 0, column: 0, text: "Metric", edits: [], format: { bold: true, italic: true, fontSize: 15, fontFamily: "Aptos", color: "#ffffff", fill: "#0f766e" } },
      { row: 0, column: 1, text: "Value", edits: [], format: { bold: true, fill: "#0f766e", color: "#ffffff" } },
    ],
    [
      { row: 1, column: 0, text: "Editability", edits: [] },
      { row: 1, column: 1, text: "Native", edits: [] },
    ],
  ],
});
const objectCapabilitySlides = applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(objectCapabilityState));
assert.ok(objectCapabilitySlides, "AI live edit should support native PPT objects, styling, slide backgrounds, and notes");
assert.equal(objectCapabilitySlides[0].notes, "Generated and editable speaker notes");
assert.equal(objectCapabilitySlides[0].bgGrad.stops.length, 2);
assert.equal(objectCapabilitySlides[0].shapes[1].presetGeom, "roundRect");
assert.equal(objectCapabilitySlides[0].shapes[1].texts[0].fontSize, 22);
assert.equal(objectCapabilitySlides[0].shapes[2].tableRows[0][0].italic, true);
assert.equal(objectCapabilitySlides[0].shapes[2].tableRows[0][0].fontFamily, "Aptos");

const invalidObjectOperationState = structuredClone(objectCapabilityState);
invalidObjectOperationState.slides[0].shapes.at(-1).create.operation = "picture.insert";
assert.equal(
  applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(invalidObjectOperationState)),
  null,
  "the live editor must reject native operations it cannot preview and persist atomically",
);
const invalidTableFrameFormatState = structuredClone(objectCapabilityState);
invalidTableFrameFormatState.slides[0].shapes.at(-1).format = { fill: "#ff0000" };
assert.equal(
  applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(invalidTableFrameFormatState)),
  null,
  "new table frames must reject shape-level formatting that their OOXML saver cannot persist",
);
const invalidDashState = structuredClone(objectCapabilityState);
invalidDashState.slides[0].shapes[1].format.strokeDash = "not-a-drawingml-dash";
assert.equal(
  applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(invalidDashState)),
  null,
  "AI edits must reject unsupported DrawingML dash presets",
);
const invalidPresetState = structuredClone(objectCapabilityState);
invalidPresetState.slides[0].shapes[1].format.preset = "cloud";
assert.equal(
  applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(invalidPresetState)),
  null,
  "AI edits must reject shape presets the editor cannot render consistently",
);

const gradientReplacementBaseline = [{
  id: "gradient-slide",
  bgGrad: { angle: 90, stops: [{ pos: 0, color: "#111827", alpha: 1 }, { pos: 100, color: "#334155", alpha: 1 }] },
  shapes: [{
    id: "gradient-shape",
    type: "shape",
    x: 10, y: 10, w: 30, h: 20,
    gradFill: { angle: 0, stops: [{ pos: 0, color: "#0f172a", alpha: 1 }, { pos: 100, color: "#475569", alpha: 1 }] },
    texts: [],
    source: { part: "ppt/slides/slide1.xml", kind: "sp", objectId: "8", editable: true },
  }],
}];
const gradientReplacementState = JSON.parse(buildPresentationLiveEditContent(gradientReplacementBaseline, {
  documentName: "gradient-to-solid.pptx",
  activeSlideIndex: 0,
  selectedShapeId: "gradient-shape",
}));
gradientReplacementState.slides[0].background.color = "#f8fafc";
gradientReplacementState.slides[0].shapes[0].format.fill = "#ffffff";
const gradientReplacementSlides = applyPresentationLiveEditContent(
  gradientReplacementBaseline,
  JSON.stringify(gradientReplacementState),
);
assert.ok(gradientReplacementSlides, "solid fill replacements should be accepted");
assert.equal(gradientReplacementSlides[0].bg, "#f8fafc");
assert.equal(gradientReplacementSlides[0].bgGrad, undefined, "solid slide backgrounds must clear the old gradient");
assert.equal(gradientReplacementSlides[0].shapes[0].fill, "#ffffff");
assert.equal(gradientReplacementSlides[0].shapes[0].gradFill, undefined, "solid shape fills must clear the old gradient");

const emptyTextShapeState = JSON.parse(buildPresentationLiveEditContent(gradientReplacementBaseline, {
  documentName: "empty-text-shape.pptx",
  activeSlideIndex: 0,
  selectedShapeId: "gradient-shape",
}));
emptyTextShapeState.slides[0].shapes[0].paragraphs = [{
  index: 0,
  text: "Text added to an existing empty shape",
  edits: [],
  format: { bold: true, fontSize: 20 },
}];
const emptyTextShapeSlides = applyPresentationLiveEditContent(
  gradientReplacementBaseline,
  JSON.stringify(emptyTextShapeState),
);
assert.ok(emptyTextShapeSlides, "AI edits should add text to an existing empty native shape");
assert.equal(emptyTextShapeSlides[0].shapes[0].texts[0].text, "Text added to an existing empty shape");
assert.equal(emptyTextShapeSlides[0].shapes[0].texts[0].bold, true);

const reorderedStructuralLiveState = structuredClone(structuralLiveState);
reorderedStructuralLiveState.slides.reverse();
assert.deepEqual(
  applyPresentationLiveEditContent(structurallyLiveEdited, JSON.stringify(reorderedStructuralLiveState))
    .map((slide) => slide.id),
  ["ai-slide-core-capabilities", "slide-13"],
  "slide array order should define the reviewable presentation order",
);

const duplicateStructuralLiveState = structuredClone(structuralLiveState);
duplicateStructuralLiveState.slides.push(structuredClone(duplicateStructuralLiveState.slides[1]));
assert.equal(
  applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(duplicateStructuralLiveState)),
  null,
  "duplicate AI slide IDs must fail closed",
);

const invalidStructuralLiveState = structuredClone(structuralLiveState);
invalidStructuralLiveState.slides[1].id = "existing-looking-id";
assert.equal(
  applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(invalidStructuralLiveState)),
  null,
  "AI-created slides must use the structural ai-slide identity namespace",
);

const invalidStructuralBodyState = structuredClone(structuralLiveState);
invalidStructuralBodyState.slides[1].create.body = ["not", "a", "string"];
assert.equal(
  applyPresentationLiveEditContent(flattenedSlides, JSON.stringify(invalidStructuralBodyState)),
  null,
  "malformed AI slide factory input must fail closed",
);

const persistedAiSlide = {
  ...structurallyLiveEdited[1],
  sourcePart: "ppt/slides/slide2.xml",
};
const persistedAiSlideState = JSON.parse(buildPresentationLiveEditContent([persistedAiSlide], {
  documentName: "accepted-ai-slide.pptx",
  activeSlideIndex: 0,
  selectedShapeId: null,
}));
persistedAiSlideState.slides[0].create = {
  layout: "blank",
};
assert.equal(
  applyPresentationLiveEditContent([persistedAiSlide], JSON.stringify(persistedAiSlideState)),
  null,
  "a later turn must not recreate an accepted source-backed slide through the create contract",
);

assert.equal(resolvePresentationPartTarget("ppt/presentation.xml", "slides/slide3.xml"), "ppt/slides/slide3.xml");
assert.equal(resolvePresentationPartTarget("ppt/slides/slide1.xml", "../media/image.png"), "ppt/media/image.png");
assert.equal(resolvePresentationPartTarget("ppt/slides/slide1.xml", "/ppt/media/image.png"), "ppt/media/image.png");
assert.equal(resolvePresentationPartTarget("ppt/slideLayouts/slideLayout2.xml", "../slideMasters/slideMaster1.xml"), "ppt/slideMasters/slideMaster1.xml");
assert.equal(presentationRelationshipsPart("ppt/slides/custom-slide.xml"), "ppt/slides/_rels/custom-slide.xml.rels");
const filledShapeProperties = '<p:spPr><a:solidFill><a:srgbClr val="05070D"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>';
assert.match(presentationShapeFillScope(filledShapeProperties), /<a:solidFill>/);
assert.doesNotMatch(presentationShapeFillScope(filledShapeProperties), /<a:noFill/);
assert.equal(presentationColorWithAlpha("#08111f", '<a:srgbClr val="08111F"/>'), "#08111f");
assert.equal(presentationColorWithAlpha("#08111f", '<a:srgbClr val="08111F"><a:alpha val="0"/></a:srgbClr>'), "transparent");
assert.equal(presentationColorWithAlpha("#08111f", '<a:srgbClr val="08111F"><a:alpha val="50000"/></a:srgbClr>'), "rgba(8, 17, 31, 0.5)");
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
const originalPackageParts = Object.keys(preservationZip.files).filter((name) => !preservationZip.files[name].dir).sort();
for (const packagePart of originalPackageParts) {
  if (packagePart === mediaPart && !preservedZip.file(packagePart)) continue;
  assert.ok(preservedZip.file(packagePart), `incremental save should retain ${packagePart}`);
}
const preservedSlideXml = await preservedZip.file(slidePart).async("text");
assert.notEqual(preservedSlideXml, slideXmlBefore, "incremental save should patch the edited slide XML");
assert.match(await preservedZip.file(slidePart).async("text"), /<a:srcRect l="0" t="10000" r="0" b="0"\/>/);
const preservedPictureXml = (preservedSlideXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || [])
  .find((element) => element.match(/<p:cNvPr\b[^>]*id="([^"]+)"/)?.[1] === objectId);
const replacementRelationshipId = preservedPictureXml?.match(/<a:blip\b[^>]*r:embed="([^"]+)"/)?.[1];
assert.ok(replacementRelationshipId && replacementRelationshipId !== relationshipId, "edited image should receive an independent relationship");
const preservedSlideRelationships = await preservedZip.file(presentationRelationshipsPart(slidePart)).async("text");
assert.ok(
  !(preservedSlideRelationships.match(/<Relationship\b[^>]*\/>/g) || []).some((relationship) => relationship.includes(`Id="${relationshipId}"`)),
  "image replacement should remove the old unused slide relationship",
);
const preservedOriginalMedia = preservedZip.file(mediaPart);
if (preservedOriginalMedia) {
  assert.deepEqual(
    Buffer.from(await preservedOriginalMedia.async("uint8array")),
    Buffer.from(await preservationZip.file(mediaPart).async("uint8array")),
    "shared original media must remain byte-for-byte intact",
  );
}
const replacementTarget = (preservedSlideRelationships.match(/<Relationship\b[^>]*\/>/g) || [])
  .find((relationship) => relationship.includes(`Id="${replacementRelationshipId}"`))
  ?.match(/Target="([^"]+)"/)?.[1];
assert.ok(replacementTarget, "edited image relationship should resolve to a media target");
const replacementMediaPart = resolvePresentationPartTarget(slidePart, replacementTarget);
assert.notEqual(replacementMediaPart, mediaPart);
assert.deepEqual(
  Buffer.from(await preservedZip.file(replacementMediaPart).async("uint8array")),
  Buffer.from(pixel.split(",")[1], "base64"),
  "edited image bytes should be isolated in the new media part",
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
  opacity: 0.45,
  stroke: "#2563eb",
  strokeWidth: 2,
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
assert.match(insertedMediaSlideXml, /<a:alphaModFix amt="45000"\/>/);
assert.match(insertedMediaSlideXml, /<a:ln w="25400"><a:solidFill><a:srgbClr val="2563EB"><\/a:srgbClr><\/a:solidFill>/);
assert.doesNotMatch(insertedMediaSlideXml, /<a:ln w="25400">[\s\S]*?<a:alpha val="45000"\/>[\s\S]*?<\/a:ln>/);
assert.match(insertedMediaRelationships, /relationships\/image/);
assert.match(insertedMediaRelationships, /Target="http:\/\/localhost:18080\/viewer\/video-document" TargetMode="External"/);

const existingImageOpacitySlides = structuredClone(baselineSlides);
existingImageOpacitySlides[0].shapes[0].opacity = 0.4;
existingImageOpacitySlides[0].shapes[0].stroke = "#2563eb";
existingImageOpacitySlides[0].shapes[0].strokeWidth = 2;
const existingImageOpacityFile = await preservePresentationFile(
  preservationInput,
  baselineSlides,
  existingImageOpacitySlides,
  "existing-image-opacity.pptx",
);
const existingImageOpacityZip = await JSZip.loadAsync(await existingImageOpacityFile.arrayBuffer());
const existingImageOpacityXml = await existingImageOpacityZip.file(slidePart).async("text");
assert.match(existingImageOpacityXml, /<a:alphaModFix amt="40000"\/>/);
assert.match(existingImageOpacityXml, /<a:ln w="25400"><a:solidFill><a:srgbClr val="2563EB"><\/a:srgbClr><\/a:solidFill>/);
assert.doesNotMatch(existingImageOpacityXml, /<a:ln w="25400">[\s\S]*?<a:alpha val="40000"\/>[\s\S]*?<\/a:ln>/);

const backgroundImageZip = await JSZip.loadAsync(preservationInput);
backgroundImageZip.file("ppt/media/manor-bg.png", Buffer.from(pixel.split(",")[1], "base64"));
backgroundImageZip.file(
  presentationRelationshipsPart(slidePart),
  `${slideRelationships.replace(
    /<\/Relationships>\s*$/i,
    '<Relationship Id="rIdBackground" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/manor-bg.png"/></Relationships>',
  )}`,
);
backgroundImageZip.file(
  slidePart,
  slideXmlBefore.replace(
    /<p:cSld\b[^>]*>/i,
    (openTag) => `${openTag}<p:bg><p:bgPr><a:blipFill><a:blip r:embed="rIdBackground"/><a:stretch><a:fillRect/></a:stretch></a:blipFill><a:effectLst/></p:bgPr></p:bg>`,
  ),
);
const backgroundImageInput = await backgroundImageZip.generateAsync({ type: "arraybuffer" });
const backgroundImageBaseline = [{
  id: "slide-bg-image",
  bgImgUrl: pixel,
  aspectRatio: `${width}/${height}`,
  sourcePart: slidePart,
  shapes: [],
}];
const backgroundImageEdited = structuredClone(backgroundImageBaseline);
backgroundImageEdited[0].bg = "#123456";
backgroundImageEdited[0].bgImgUrl = undefined;
const backgroundImageFile = await preservePresentationFile(
  backgroundImageInput,
  backgroundImageBaseline,
  backgroundImageEdited,
  "background-image-replaced.pptx",
);
const backgroundImageSavedZip = await JSZip.loadAsync(await backgroundImageFile.arrayBuffer());
const backgroundImageSavedSlideXml = await backgroundImageSavedZip.file(slidePart).async("text");
const backgroundImageSavedRels = await backgroundImageSavedZip.file(presentationRelationshipsPart(slidePart)).async("text");
assert.match(
  backgroundImageSavedSlideXml,
  /<p:bg><p:bgPr><a:solidFill><a:srgbClr val="123456">/,
  "changing a picture-backed slide background should persist the selected editable color",
);
assert.doesNotMatch(backgroundImageSavedSlideXml, /rIdBackground/);
assert.doesNotMatch(backgroundImageSavedRels, /rIdBackground/);

const unchangedBackgroundImageEdit = structuredClone(backgroundImageBaseline);
unchangedBackgroundImageEdit[0].bg = "#654321";
const unchangedBackgroundImageFile = await preservePresentationFile(
  backgroundImageInput,
  backgroundImageBaseline,
  unchangedBackgroundImageEdit,
  "unchanged-background-image.pptx",
);
const unchangedBackgroundImageZip = await JSZip.loadAsync(await unchangedBackgroundImageFile.arrayBuffer());
const unchangedBackgroundImageSlideXml = await unchangedBackgroundImageZip.file(slidePart).async("text");
const unchangedBackgroundImageRels = await unchangedBackgroundImageZip.file(presentationRelationshipsPart(slidePart)).async("text");
assert.match(
  unchangedBackgroundImageSlideXml,
  /<p:bg><p:bgPr><a:blipFill><a:blip r:embed="rIdBackground"/,
  "an unchanged picture-backed slide background should not be replaced by editable color drift",
);
assert.match(unchangedBackgroundImageRels, /Id="rIdBackground"[\s\S]*?Type="http:\/\/schemas.openxmlformats.org\/officeDocument\/2006\/relationships\/image"/);
assert.doesNotMatch(unchangedBackgroundImageSlideXml, /<a:srgbClr val="654321">/);

const alternateBackgroundPixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
const changedBackgroundImageEdit = structuredClone(backgroundImageBaseline);
changedBackgroundImageEdit[0].bgImgUrl = alternateBackgroundPixel;
const changedBackgroundImageFile = await preservePresentationFile(
  backgroundImageInput,
  backgroundImageBaseline,
  changedBackgroundImageEdit,
  "changed-background-image.pptx",
);
const changedBackgroundImageZip = await JSZip.loadAsync(await changedBackgroundImageFile.arrayBuffer());
const changedBackgroundImageSlideXml = await changedBackgroundImageZip.file(slidePart).async("text");
const changedBackgroundImageRels = await changedBackgroundImageZip.file(presentationRelationshipsPart(slidePart)).async("text");
const changedBackgroundRelationshipId = changedBackgroundImageSlideXml.match(/<a:blip\b[^>]*\br:embed="([^"]+)"/)?.[1];
assert.ok(changedBackgroundRelationshipId && changedBackgroundRelationshipId !== "rIdBackground");
assert.match(changedBackgroundImageSlideXml, /<p:bg><p:bgPr><a:blipFill>/);
assert.match(changedBackgroundImageRels, new RegExp(`Id="${changedBackgroundRelationshipId}"[\\s\\S]*?Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"`));
assert.doesNotMatch(changedBackgroundImageRels, /rIdBackground/);

const newBackgroundImageResult = await preservePresentationFileWithSnapshot(
  backgroundImageInput,
  backgroundImageBaseline,
  [{
    id: "new-slide-with-image-bg",
    bgImgUrl: alternateBackgroundPixel,
    aspectRatio: `${width}/${height}`,
    shapes: [],
  }],
  "new-background-image.pptx",
);
const newBackgroundImageSlidePart = newBackgroundImageResult.slides[0].sourcePart;
assert.ok(newBackgroundImageSlidePart, "new slides with image backgrounds should receive an editable slide part");
const newBackgroundImageZip = await JSZip.loadAsync(await newBackgroundImageResult.file.arrayBuffer());
const newBackgroundImageSlideXml = await newBackgroundImageZip.file(newBackgroundImageSlidePart).async("text");
const newBackgroundImageRels = await newBackgroundImageZip.file(presentationRelationshipsPart(newBackgroundImageSlidePart)).async("text");
const newBackgroundRelationshipId = newBackgroundImageSlideXml.match(/<a:blip\b[^>]*\br:embed="([^"]+)"/)?.[1];
assert.ok(newBackgroundRelationshipId, "new slides with image backgrounds should persist a background image relationship");
assert.match(newBackgroundImageSlideXml, /<p:bg><p:bgPr><a:blipFill>/);
assert.match(newBackgroundImageRels, new RegExp(`Id="${newBackgroundRelationshipId}"[\\s\\S]*?Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"`));

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

const presentationWithoutNotes = await buildPresentationBlob([
  { id: "slide-without-notes", bg: "#ffffff", aspectRatio: "16/9", shapes: [] },
], "Presentation without notes.pptx");
const withoutNotesBaseline = [{
  id: "slide-without-notes",
  sourcePart: "ppt/slides/slide1.xml",
  shapes: [],
}];
const withoutNotesEdited = structuredClone(withoutNotesBaseline);
withoutNotesEdited[0].notes = "Notes added in Manor";
const withoutNotesResult = await preservePresentationFileWithSnapshot(
  await presentationWithoutNotes.arrayBuffer(),
  withoutNotesBaseline,
  withoutNotesEdited,
  "Presentation without notes.pptx",
);
if (process.env.PRESENTATION_NO_NOTES_TEST_OUTPUT) {
  await writeFile(process.env.PRESENTATION_NO_NOTES_TEST_OUTPUT, Buffer.from(await withoutNotesResult.file.arrayBuffer()));
}
const withoutNotesZip = await JSZip.loadAsync(await withoutNotesResult.file.arrayBuffer());
assert.ok(withoutNotesResult.slides[0].notesPart, "saving should return the new durable notes part");
assert.match(
  await withoutNotesZip.file(withoutNotesResult.slides[0].notesPart).async("text"),
  /Notes added in Manor/,
  "slides without a notes part should gain editable speaker notes",
);

const aiNativeObjectState = JSON.parse(buildPresentationLiveEditContent(withoutNotesBaseline, {
  documentName: "AI native objects.pptx",
  activeSlideIndex: 0,
  selectedShapeId: null,
}));
aiNativeObjectState.slides[0].notes = "AI-created notes on a slide that originally had none";
aiNativeObjectState.slides[0].background = {
  color: null,
  gradient: {
    angle: 45,
    stops: [
      { pos: 0, color: "#f0fdfa", alpha: 1 },
      { pos: 100, color: "#ccfbf1", alpha: 1 },
    ],
  },
};
aiNativeObjectState.slides[0].shapes = [
  {
    id: "ai-shape-native-title",
    type: "shape",
    editable: true,
    fullSlide: false,
    create: { operation: "textbox.insert" },
    x: 10, y: 10, w: 80, h: 18, rotation: 0, flipH: false, flipV: false,
    format: {
      fill: "#0f766e", stroke: "#134e4a", strokeWidth: 2, strokeDash: "dash",
      preset: "roundRect", borderRadius: 15, verticalAlignment: "middle", wordWrap: true,
      padding: { l: 8, t: 4, r: 8, b: 4 },
    },
    paragraphs: [{
      index: 0,
      text: "Generated, then edited with the same native structure",
      edits: [],
      format: { bold: true, italic: true, fontSize: 24, fontFamily: "Aptos", color: "#ffffff", align: "center" },
    }],
  },
  {
    id: "ai-shape-native-table",
    type: "table",
    editable: true,
    fullSlide: false,
    create: { operation: "table.insert" },
    x: 15, y: 38, w: 70, h: 32, rotation: 0, flipH: false, flipV: false,
    table: [
      [
        { row: 0, column: 0, text: "Capability", edits: [], format: { bold: true, italic: true, fontSize: 14, fontFamily: "Aptos", color: "#ffffff", fill: "#0f766e" } },
        { row: 0, column: 1, text: "Status", edits: [], format: { bold: true, color: "#ffffff", fill: "#0f766e" } },
      ],
      [
        { row: 1, column: 0, text: "Native OOXML", edits: [] },
        { row: 1, column: 1, text: "Editable", edits: [] },
      ],
    ],
  },
];
const aiNativeObjectSlides = applyPresentationLiveEditContent(
  withoutNotesBaseline,
  JSON.stringify(aiNativeObjectState),
);
assert.ok(aiNativeObjectSlides, "AI live editing should produce source-less native shapes that the OOXML saver can persist");
const aiNativeObjectResult = await preservePresentationFileWithSnapshot(
  await presentationWithoutNotes.arrayBuffer(),
  withoutNotesBaseline,
  aiNativeObjectSlides,
  "AI native objects.pptx",
);
const aiNativeObjectZip = await JSZip.loadAsync(await aiNativeObjectResult.file.arrayBuffer());
const aiNativeObjectXml = await aiNativeObjectZip.file("ppt/slides/slide1.xml").async("text");
assert.match(aiNativeObjectXml, /Generated, then edited with the same native structure/);
assert.match(aiNativeObjectXml, /<p:cNvSpPr txBox="1"\/>/, "textbox.insert must persist a native PowerPoint text box");
assert.match(aiNativeObjectXml, /<a:prstGeom prst="roundRect">/);
assert.match(aiNativeObjectXml, /<a:prstDash val="dash"\/>/);
assert.match(aiNativeObjectXml, /<a:rPr[^>]*sz="2400"[^>]*b="1"[^>]*i="1"/);
assert.match(aiNativeObjectXml, /<a:latin typeface="Aptos"\/>/);
assert.match(aiNativeObjectXml, /<a:tbl>/);
assert.match(aiNativeObjectXml, /<a:gradFill/);
assert.ok(aiNativeObjectResult.slides[0].notesPart, "AI live editing should attach a new native speaker-notes part");
assert.match(
  await aiNativeObjectZip.file(aiNativeObjectResult.slides[0].notesPart).async("text"),
  /AI-created notes on a slide that originally had none/,
);

const weightedTableEdited = structuredClone(withoutNotesBaseline);
weightedTableEdited[0].shapes.push({
  id: "weighted-table",
  type: "table",
  x: 12,
  y: 18,
  w: 40,
  h: 28,
  texts: [],
  tableRows: [
    [{ text: "Narrow" }, { text: "Wide" }],
    [{ text: "1x" }, { text: "3x" }],
  ],
  tableColWidths: [1, 3],
});
const weightedTableResult = await preservePresentationFileWithSnapshot(
  await presentationWithoutNotes.arrayBuffer(),
  withoutNotesBaseline,
  weightedTableEdited,
  "Presentation weighted table.pptx",
);
const weightedTableZip = await JSZip.loadAsync(await weightedTableResult.file.arrayBuffer());
const weightedTableXml = await weightedTableZip.file("ppt/slides/slide1.xml").async("text");
const weightedGridWidths = [...weightedTableXml.matchAll(/<a:gridCol\b[^>]*w="(\d+)"/g)].map((match) => Number(match[1]));
assert.equal(weightedGridWidths.length, 2, "the saved table should expose two column grid widths");
assert.ok(
  Math.abs(weightedGridWidths[1] - (weightedGridWidths[0] * 3)) <= 2,
  "saving source-less PPT tables should preserve the editor's proportional column widths",
);

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

const invalidShapeOpacityState = JSON.parse(buildPresentationLiveEditContent(generatedBaseline, {
  documentName: "invalid-shape-opacity.pptx",
  activeSlideIndex: 0,
  selectedShapeId: "title",
}));
invalidShapeOpacityState.slides[0].shapes[0].format.opacity = 0.35;
assert.equal(
  applyPresentationLiveEditContent(generatedBaseline, JSON.stringify(invalidShapeOpacityState)),
  null,
  "ordinary shape opacity must fail closed instead of disappearing after save",
);
const invalidPersistedShapeOpacity = structuredClone(generatedBaseline);
invalidPersistedShapeOpacity[0].shapes[0].opacity = 0.35;
await assert.rejects(
  async () => preservePresentationFile(
    await blob.arrayBuffer(),
    generatedBaseline,
    invalidPersistedShapeOpacity,
    "invalid-shape-opacity.pptx",
  ),
  /Opacity can only be edited on native presentation pictures/,
  "the OOXML saver must reject non-picture opacity instead of returning a false saved snapshot",
);

const shapeStyleBundle = await build({
  stdin: { contents: 'export * from "../src/lib/presentationShapeStyle.ts";', loader: "ts", resolveDir: new URL(".", import.meta.url).pathname },
  bundle: true, format: "esm", platform: "browser", write: false, logLevel: "silent",
});
const {
  presentationOutlineRectRadius,
  presentationRoundRectRadius,
  presentationStrokeDash,
  presentationStrokeDashArray,
} = await import(
  `data:text/javascript;base64,${Buffer.from(shapeStyleBundle.outputFiles[0].text).toString("base64")}`,
);
assert.equal(presentationRoundRectRadius(50, 20, 0), "calc(min(50cqw, 20cqh) * 0)");
assert.equal(presentationRoundRectRadius(50, 20, 10), "calc(min(50cqw, 20cqh) * 0.1)");
assert.equal(presentationStrokeDash('<a:prstDash val="dashDot"/>'), "dashDot");
assert.equal(presentationStrokeDash('<a:prstDash val="constructor"/>'), undefined);
assert.equal(presentationStrokeDashArray("solid", 2), undefined);
assert.equal(presentationStrokeDashArray("dashDot", 2).split("calc(").length - 1, 4);
assert.equal(
  presentationOutlineRectRadius("flowChartTerminator", 50, 20),
  "999px",
  "flowchart terminator fill and SVG outline must use the same pill geometry",
);
assert.equal(
  presentationOutlineRectRadius("roundRect", 50, 20, 10, "2px"),
  "max(0px, calc(calc(min(50cqw, 20cqh) * 0.1) - 2px))",
  "round-rect outlines must retain the stroke inset adjustment",
);

// A narrow style edit must not rebuild unrelated native properties.
const customGeometry = '<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/><a:pathLst/></a:custGeom>';
const customLine = '<a:ln w="31750" cap="rnd" cmpd="dbl"><a:solidFill><a:schemeClr val="accent2"><a:alpha val="70000"/></a:schemeClr></a:solidFill><a:custDash><a:ds d="200000" sp="150000"/></a:custDash><a:round/><a:headEnd type="triangle"/><a:tailEnd type="oval"/></a:ln>';
const customEffects = '<a:effectLst><a:glow rad="63500"><a:srgbClr val="FFFFFF"/></a:glow><a:outerShdw blurRad="76200"><a:srgbClr val="000000"/></a:outerShdw><a:softEdge rad="12700"/></a:effectLst>';
const customShape = titleShapeXml.replace(/<p:spPr\b[^>]*>[\s\S]*?<\/p:spPr>/, `<p:spPr>${titleTransform}${customGeometry}<a:solidFill><a:srgbClr val="174C46"/></a:solidFill>${customLine}${customEffects}</p:spPr>`);
const customStyleZip = await JSZip.loadAsync(await blob.arrayBuffer());
customStyleZip.file("ppt/slides/slide1.xml", slideXml.replace(titleShapeXml, customShape));
const customStyleInput = await customStyleZip.generateAsync({ type: "arraybuffer" });
const customStyleBaseline = structuredClone(generatedBaseline);
Object.assign(customStyleBaseline[0].shapes[0], { fill: "#174C46", stroke: "rgba(216,155,69,0.7)", strokeWidth: 2.5, shadow: { blur: 6, dist: 0, angle: 0, color: "#000000", alpha: 1 } });
for (const update of [{ fill: "#336699" }, { strokeWidth: 5 }, { strokeDash: "dot" }, { shadow: undefined }]) {
  const edited = structuredClone(customStyleBaseline);
  Object.assign(edited[0].shapes[0], update);
  const output = await preservePresentationFile(customStyleInput, customStyleBaseline, edited, "style-preservation.pptx");
  const outputZip = await JSZip.loadAsync(await output.arrayBuffer());
  const outputXml = await outputZip.file("ppt/slides/slide1.xml").async("string");
  assert.ok(outputXml.includes(customGeometry), "a style edit must retain custom geometry");
  assert.ok(outputXml.includes(titleTransform), "a style edit must retain geometry");
  assert.ok(outputXml.includes(customShape.match(/<a:bodyPr\b[^>]*?(?:\/>|>[\s\S]*?<\/a:bodyPr>)/)[0]), "a style edit must retain text insets/autofit");
  if ("fill" in update || "shadow" in update) assert.ok(outputXml.includes(customLine), "fill/shadow edits must retain native dashes, alpha, compound lines and arrows");
  if ("strokeWidth" in update) assert.ok(outputXml.includes(customLine.replace('w="31750"', 'w="63500"')), "width edits must change only width");
  if ("strokeDash" in update) assert.ok(outputXml.includes(customLine.replace(/<a:custDash>[\s\S]*?<\/a:custDash>/, '<a:prstDash val="dot"/>')), "dash edits must retain arrows and compound lines");
  if (!("shadow" in update)) assert.ok(outputXml.includes(customEffects), "other edits must not rewrite effects");
  else assert.ok(outputXml.includes(customEffects.replace(/<a:outerShdw\b[\s\S]*?<\/a:outerShdw>/, "")), "clearing outer shadow must retain glow and soft edge");
}

const pairedLine = customLine.replace(/<a:solidFill>[\s\S]*?<\/a:solidFill>/, "<a:noFill></a:noFill>")
  .replace(/<a:custDash>[\s\S]*?<\/a:custDash>/, '<a:prstDash val="dash"></a:prstDash>');
const pairedZip = await JSZip.loadAsync(customStyleInput);
pairedZip.file("ppt/slides/slide1.xml", slideXml.replace(titleShapeXml, customShape.replace(customLine, pairedLine)));
const pairedEdited = structuredClone(customStyleBaseline);
Object.assign(pairedEdited[0].shapes[0], { stroke: "#174C46", strokeDash: "dot" });
const pairedResult = await preservePresentationFile(await pairedZip.generateAsync({ type: "arraybuffer" }), customStyleBaseline, pairedEdited, "paired-style.pptx");
const pairedOutput = await JSZip.loadAsync(await pairedResult.arrayBuffer());
const pairedOutputXml = await pairedOutput.file("ppt/slides/slide1.xml").async("string");
assert.ok(pairedOutputXml.includes(pairedLine.replace("<a:noFill></a:noFill>", '<a:solidFill><a:srgbClr val="174C46"></a:srgbClr></a:solidFill>')
  .replace('<a:prstDash val="dash"></a:prstDash>', '<a:prstDash val="dot"/>')), "paired empty fill/dash elements must be replaced, not duplicated");

const textureFill = '<a:blipFill><a:blip><a:extLst><a:ext uri="keep"><a:ln w="12"/></a:ext></a:extLst></a:blip><a:stretch><a:fillRect/></a:stretch></a:blipFill>';
const texturedShape = titleShapeXml.replace(/<p:spPr\b[^>]*>[\s\S]*?<\/p:spPr>/, `<p:spPr>${titleTransform}${customGeometry}${textureFill}${customEffects}</p:spPr>`);
const texturedZip = await JSZip.loadAsync(await blob.arrayBuffer());
texturedZip.file("ppt/slides/slide1.xml", slideXml.replace(titleShapeXml, texturedShape));
const texturedInput = await texturedZip.generateAsync({ type: "arraybuffer" });
for (const update of [{ fill: "#336699" }, { stroke: "#174C46", strokeWidth: 3 }, { wordWrap: false }]) {
  const edited = structuredClone(generatedBaseline);
  Object.assign(edited[0].shapes[0], update);
  const output = await preservePresentationFile(texturedInput, generatedBaseline, edited, "texture-style.pptx");
  const outputZip = await JSZip.loadAsync(await output.arrayBuffer());
  const outputXml = await outputZip.file("ppt/slides/slide1.xml").async("string");
  assert.ok(outputXml.includes(customGeometry));
  assert.ok(outputXml.includes(customEffects));
  if ("fill" in update) assert.ok(!outputXml.includes(textureFill), "changing a texture to a solid fill must replace, not duplicate, the fill");
  else assert.ok(outputXml.includes(textureFill), "line/wrap edits must preserve nested texture extensions");
  if ("stroke" in update) assert.match(outputXml, /<\/a:blipFill><a:ln w="38100">[\s\S]*?<\/a:ln><a:effectLst>/, "new outline belongs after the fill and before effects, not inside a texture extension");
  if ("wordWrap" in update) assert.match(outputXml, /<a:bodyPr\b[^>]*wrap="none"/, "wrap changes must serialize to native body properties");
}

const originalShapeCreationId = "{11111111-2222-4333-8444-555555555555}";
const identityZip = await JSZip.loadAsync(await blob.arrayBuffer());
const identityTitleShape = titleShapeXml.replace(
  /<p:cNvPr\b([^>]*)><\/p:cNvPr>/i,
  `<p:cNvPr$1><a:extLst><a:ext uri="{FF2B5EF4-FFF2-40B4-BE49-F238E27FC236}"><draw16:creationId xmlns:draw16="http://schemas.microsoft.com/office/drawing/2014/main" id='${originalShapeCreationId}'/></a:ext></a:extLst></p:cNvPr>`,
);
assert.notEqual(identityTitleShape, titleShapeXml, "the identity fixture must add a shape creation ID");
identityZip.file("ppt/slides/slide1.xml", slideXml.replace(titleShapeXml, identityTitleShape));
const identityInput = await identityZip.generateAsync({ type: "arraybuffer" });
const identityEdited = structuredClone(generatedBaseline);
const identityClone = structuredClone(identityEdited[0].shapes[0]);
identityClone.id = "title-copy";
identityClone.x += 4;
identityClone.source = {
  ...identityClone.source,
  objectId: "clone-pending",
  cloneOfObjectId: titleObjectId,
};
identityEdited[0].shapes.push(identityClone);
const identityResult = await preservePresentationFileWithSnapshot(
  identityInput,
  generatedBaseline,
  identityEdited,
  "shape-creation-id-copy.pptx",
);
const identitySavedZip = await JSZip.loadAsync(await identityResult.file.arrayBuffer());
const identitySavedXml = await identitySavedZip.file("ppt/slides/slide1.xml").async("text");
const savedCreationIds = Array.from(
  identitySavedXml.matchAll(/<draw16:creationId\b[^>]*\bid=(["'])(.*?)\1/gi),
  (match) => match[2],
);
assert.equal(savedCreationIds.length, 2, "the source and copied shape should each retain a creation ID");
assert.equal(new Set(savedCreationIds).size, 2, "copied shapes must receive a distinct creation ID");
assert.ok(savedCreationIds.includes(originalShapeCreationId), "the source shape creation ID must remain unchanged");

const zOrderZip = await JSZip.loadAsync(await blob.arrayBuffer());
const zOrderSlideXml = await zOrderZip.file("ppt/slides/slide1.xml").async("text");
const secondEditableObjectId = "99";
const lockedSlideObjectId = "98";
const secondEditableShapeXml = titleShapeXml
  .replace(new RegExp(`(<p:cNvPr\\b[^>]*\\bid=")${titleObjectId}(")`), `$1${secondEditableObjectId}$2`)
  .replace("Editable title", "Second editable");
const lockedSlideShapeXml = titleShapeXml
  .replace(new RegExp(`(<p:cNvPr\\b[^>]*\\bid=")${titleObjectId}(")`), `$1${lockedSlideObjectId}$2`)
  .replace("Editable title", "Locked slide object");
zOrderZip.file(
  "ppt/slides/slide1.xml",
  zOrderSlideXml
    .replace(titleShapeXml, `${titleShapeXml}${lockedSlideShapeXml}`)
    .replace("</p:spTree>", `${secondEditableShapeXml}</p:spTree>`),
);
const zOrderInput = await zOrderZip.generateAsync({ type: "arraybuffer" });
const zOrderBaseline = [{
  ...structuredClone(generatedBaseline[0]),
  shapes: [
    {
      id: "master-decoration",
      type: "shape",
      x: 0,
      y: 0,
      w: 100,
      h: 5,
      texts: [],
      source: { part: "ppt/slideMasters/slideMaster1.xml", kind: "sp", objectId: "42", editable: false },
    },
    structuredClone(generatedBaseline[0].shapes[0]),
    {
      ...structuredClone(generatedBaseline[0].shapes[0]),
      id: "locked-slide-object",
      texts: [{ text: "Locked slide object", fontSize: 28, bold: true, color: "#1c1917" }],
      source: { part: "ppt/slides/slide1.xml", kind: "sp", objectId: lockedSlideObjectId, editable: false },
    },
    {
      ...structuredClone(generatedBaseline[0].shapes[0]),
      id: "second-editable",
      texts: [{ text: "Second editable", fontSize: 28, bold: true, color: "#1c1917" }],
      source: { part: "ppt/slides/slide1.xml", kind: "sp", objectId: secondEditableObjectId, editable: true },
    },
  ],
}];
const zOrderEdited = structuredClone(zOrderBaseline);
zOrderEdited[0].shapes = [
  zOrderEdited[0].shapes[0],
  zOrderEdited[0].shapes[3],
  zOrderEdited[0].shapes[2],
  zOrderEdited[0].shapes[1],
];
const zOrderFile = await preservePresentationFile(zOrderInput, zOrderBaseline, zOrderEdited, "z-order.pptx");
const zOrderSavedZip = await JSZip.loadAsync(await zOrderFile.arrayBuffer());
const zOrderSavedXml = await zOrderSavedZip.file("ppt/slides/slide1.xml").async("text");
assert.doesNotMatch(
  zOrderSavedXml,
  /\bshowMasterSp="0"/,
  "reordering editable slide objects must not hide or materialize inherited master objects",
);
assert.deepEqual(
  presentationObjectIdsInOrder(zOrderSavedXml).filter((id) => (
    id === titleObjectId || id === lockedSlideObjectId || id === secondEditableObjectId
  )),
  [secondEditableObjectId, lockedSlideObjectId, titleObjectId],
  "editable and slide-owned locked objects should follow the requested paint order",
);

const inheritedKeyCollisionBaseline = [{
  id: "slide-1",
  sourcePart: "ppt/slides/slide1.xml",
  shapes: [
    {
      id: "layout-object",
      type: "shape",
      x: 0,
      y: 0,
      w: 100,
      h: 5,
      texts: [],
      source: { part: "ppt/slideLayouts/slideLayout1.xml", kind: "sp", objectId: "2", editable: false },
    },
    {
      id: "master-object",
      type: "shape",
      x: 0,
      y: 95,
      w: 100,
      h: 5,
      texts: [],
      source: { part: "ppt/slideMasters/slideMaster1.xml", kind: "sp", objectId: "2", editable: false },
    },
  ],
}];
const inheritedKeyCollisionEdited = structuredClone(inheritedKeyCollisionBaseline);
inheritedKeyCollisionEdited[0].shapes.reverse();
const inheritedKeyCollisionResult = await preservePresentationFileWithSnapshot(
  await blob.arrayBuffer(),
  inheritedKeyCollisionBaseline,
  inheritedKeyCollisionEdited,
  "inherited-key-collision.pptx",
);
const inheritedKeyCollisionZip = await JSZip.loadAsync(await inheritedKeyCollisionResult.file.arrayBuffer());
const inheritedKeyCollisionXml = await inheritedKeyCollisionZip.file("ppt/slides/slide1.xml").async("text");
assert.doesNotMatch(
  inheritedKeyCollisionXml,
  /\bshowMasterSp="0"/,
  "layout and master objects with the same local ID must remain inherited after a pure reorder",
);
assert.deepEqual(
  inheritedKeyCollisionResult.slides[0].shapes.map((shape) => shape.source?.part),
  ["ppt/slideMasters/slideMaster1.xml", "ppt/slideLayouts/slideLayout1.xml"],
  "inherited source identity must include its package part",
);

const groupedGraphicFrame = (slideXml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [])
  .find((frame) => frame.includes("Metric"));
const groupedPictureXml = (slideXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || [])[0];
const groupedGraphicFrameObjectId = groupedGraphicFrame?.match(/<p:cNvPr\b[^>]*\bid="([^"]+)"/)?.[1];
const groupedPictureObjectId = groupedPictureXml?.match(/<p:cNvPr\b[^>]*\bid="([^"]+)"/)?.[1];
assert.ok(groupedGraphicFrame && groupedPictureXml && groupedGraphicFrameObjectId && groupedPictureObjectId);
const groupedObjectXml = '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="100" name="Editable group"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
  + '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/><a:chOff x="0" y="0"/><a:chExt cx="1000000" cy="1000000"/></a:xfrm></p:grpSpPr>'
  + `${titleShapeXml}${groupedGraphicFrame}</p:grpSp>`;
const groupedObjectZip = await JSZip.loadAsync(await blob.arrayBuffer());
groupedObjectZip.file(
  "ppt/slides/slide1.xml",
  slideXml.replace(groupedGraphicFrame, "").replace(titleShapeXml, groupedObjectXml),
);
const groupedObjectInput = await groupedObjectZip.generateAsync({ type: "arraybuffer" });
const groupedObjectBaseline = [{
  id: "slide-1",
  sourcePart: "ppt/slides/slide1.xml",
  shapes: [
    {
      ...structuredClone(generatedBaseline[0].shapes[0]),
      id: "grouped-locked-shape",
      source: { part: "ppt/slides/slide1.xml", kind: "sp", objectId: titleObjectId, editable: false },
    },
    {
      id: "grouped-table",
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
      source: {
        part: "ppt/slides/slide1.xml",
        kind: "graphicFrame",
        objectId: groupedGraphicFrameObjectId,
        editable: true,
      },
    },
    {
      id: "top-level-picture",
      type: "image",
      x: 8,
      y: 32,
      w: 36,
      h: 48,
      texts: [],
      imgUrl: "unchanged-image-data",
      source: {
        part: "ppt/slides/slide1.xml",
        kind: "pic",
        objectId: groupedPictureObjectId,
        editable: true,
      },
    },
  ],
}];
const groupedObjectEdited = structuredClone(groupedObjectBaseline);
groupedObjectEdited[0].shapes = [
  groupedObjectEdited[0].shapes[2],
  groupedObjectEdited[0].shapes[0],
  groupedObjectEdited[0].shapes[1],
];
const groupedObjectResult = await preservePresentationFileWithSnapshot(
  groupedObjectInput,
  groupedObjectBaseline,
  groupedObjectEdited,
  "grouped-graphic-frame.pptx",
);
const groupedObjectSavedZip = await JSZip.loadAsync(await groupedObjectResult.file.arrayBuffer());
const groupedObjectSavedXml = await groupedObjectSavedZip.file("ppt/slides/slide1.xml").async("text");
assert.match(groupedObjectSavedXml, /<p:grpSp>/, "grouped OOXML must remain grouped when its paint order changes");
assert.match(groupedObjectSavedXml, /Metric/, "grouped tables must survive paint-order changes");
assert.match(groupedObjectSavedXml, /Saved/, "grouped table content must survive paint-order changes");
const groupedObjectSavedOrder = presentationObjectIdsInOrder(groupedObjectSavedXml);
assert.ok(
  groupedObjectSavedOrder.indexOf(groupedPictureObjectId) < groupedObjectSavedOrder.indexOf("100"),
  "paint-order changes should move the original group as one fidelity-preserving object",
);

const groupedInternalOrderEdited = structuredClone(groupedObjectBaseline);
groupedInternalOrderEdited[0].shapes = [
  groupedInternalOrderEdited[0].shapes[1],
  groupedInternalOrderEdited[0].shapes[0],
  groupedInternalOrderEdited[0].shapes[2],
];
const groupedInternalOrderResult = await preservePresentationFileWithSnapshot(
  groupedObjectInput,
  groupedObjectBaseline,
  groupedInternalOrderEdited,
  "grouped-internal-order.pptx",
);
const groupedInternalOrderZip = await JSZip.loadAsync(await groupedInternalOrderResult.file.arrayBuffer());
const groupedInternalOrderXml = await groupedInternalOrderZip.file("ppt/slides/slide1.xml").async("text");
const savedInternalGroup = presentationObjectGroups(groupedInternalOrderXml)
  .find((group) => group.objectIds.has(titleObjectId) && group.objectIds.has(groupedGraphicFrameObjectId));
assert.ok(savedInternalGroup);
assert.deepEqual(
  presentationObjectIdsInOrder(savedInternalGroup.xml).filter((objectId) => (
    objectId === titleObjectId || objectId === groupedGraphicFrameObjectId
  )),
  [groupedGraphicFrameObjectId, titleObjectId],
  "arranging one grouped child must persist the group's internal paint order",
);

const groupedCloneEdited = structuredClone(groupedObjectBaseline);
const groupedCloneTemplate = groupedCloneEdited[0].shapes.find((shape) => shape.id === "grouped-table");
assert.ok(groupedCloneTemplate?.source);
const groupedClone = structuredClone(groupedCloneTemplate);
groupedClone.id = "grouped-table-copy";
groupedClone.x += 4;
groupedClone.y += 3;
groupedClone.tableRows[0][0].text = "Metric copy";
groupedClone.source = {
  ...groupedClone.source,
  objectId: "clone-pending",
  cloneOfObjectId: groupedGraphicFrameObjectId,
  groupPath: ["100"],
};
groupedCloneEdited[0].shapes.splice(2, 0, groupedClone);
const groupedCloneResult = await preservePresentationFileWithSnapshot(
  groupedObjectInput,
  groupedObjectBaseline,
  groupedCloneEdited,
  "grouped-object-clone.pptx",
);
const groupedCloneZip = await JSZip.loadAsync(await groupedCloneResult.file.arrayBuffer());
const groupedCloneXml = await groupedCloneZip.file("ppt/slides/slide1.xml").async("text");
const groupedCloneSavedGroup = presentationObjectGroups(groupedCloneXml)
  .find((group) => group.objectIds.has(groupedGraphicFrameObjectId));
assert.ok(groupedCloneSavedGroup);
const groupedCloneFrames = groupedCloneSavedGroup.xml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [];
assert.equal(groupedCloneFrames.length, 2, "duplicating a grouped object must keep both copies in the source group");
assert.match(groupedCloneSavedGroup.xml, /Metric copy/);
assert.notEqual(groupedCloneResult.slides[0].shapes[2].source?.objectId, "clone-pending");
assert.equal(groupedCloneResult.slides[0].shapes[2].source?.cloneOfObjectId, undefined);
assert.deepEqual(groupedCloneResult.slides[0].shapes[2].source?.groupPath, ["100"]);

const groupedFrameTransform = groupedGraphicFrame.match(/<(?:a|p):xfrm\b[^>]*>[\s\S]*?<\/(?:a|p):xfrm>/)?.[0];
const groupedFrameOffset = groupedFrameTransform?.match(/<(?:a|p):off\b[^>]*x="(-?\d+)"[^>]*y="(-?\d+)"/);
const groupedFrameExtent = groupedFrameTransform?.match(/<(?:a|p):ext\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
assert.ok(groupedFrameOffset && groupedFrameExtent, "grouped graphic frames must expose editable transforms");
const editableGroupTransform = { a: 2, b: 0, c: 0, d: 3, e: 100000, f: 200000 };
const transformedGroupXml = groupedObjectXml.replace(
  /<p:grpSpPr>[\s\S]*?<\/p:grpSpPr>/,
  '<p:grpSpPr><a:xfrm><a:off x="100000" y="200000"/><a:ext cx="2000000" cy="3000000"/><a:chOff x="0" y="0"/><a:chExt cx="1000000" cy="1000000"/></a:xfrm></p:grpSpPr>',
);
const transformedGroupZip = await JSZip.loadAsync(await blob.arrayBuffer());
const transformedGroupSlideXml = await transformedGroupZip.file("ppt/slides/slide1.xml").async("text");
transformedGroupZip.file(
  "ppt/slides/slide1.xml",
  transformedGroupSlideXml.replace(groupedGraphicFrame, "").replace(titleShapeXml, transformedGroupXml),
);
const transformedGroupInput = await transformedGroupZip.generateAsync({ type: "arraybuffer" });
const transformedGroupBaseline = structuredClone(groupedObjectBaseline);
const transformedGroupedTitle = transformedGroupBaseline[0].shapes.find((shape) => shape.id === "grouped-locked-shape");
const transformedGroupedTable = transformedGroupBaseline[0].shapes.find((shape) => shape.id === "grouped-table");
assert.ok(transformedGroupedTitle && transformedGroupedTable?.source);
transformedGroupedTitle.source = {
  ...transformedGroupedTitle.source,
  editable: true,
  groupTransform: editableGroupTransform,
};
transformedGroupedTable.source.groupTransform = editableGroupTransform;
transformedGroupedTable.x = Number(groupedFrameOffset[1]) / generatedWidth * 100;
transformedGroupedTable.y = Number(groupedFrameOffset[2]) / generatedHeight * 100;
transformedGroupedTable.w = Number(groupedFrameExtent[1]) / generatedWidth * 100;
transformedGroupedTable.h = Number(groupedFrameExtent[2]) / generatedHeight * 100;
const transformedGroupEdited = structuredClone(transformedGroupBaseline);
const editedGroupedTitle = transformedGroupEdited[0].shapes.find((shape) => shape.id === "grouped-locked-shape");
const editedGroupedTable = transformedGroupEdited[0].shapes.find((shape) => shape.id === "grouped-table");
assert.ok(editedGroupedTitle && editedGroupedTable);
editedGroupedTitle.texts[0].text = "Editable grouped title";
editedGroupedTable.x += 4;
editedGroupedTable.y += 3;
editedGroupedTable.w += 2;
editedGroupedTable.h += 1;
const transformedGroupResult = await preservePresentationFileWithSnapshot(
  transformedGroupInput,
  transformedGroupBaseline,
  transformedGroupEdited,
  "transformed-group-edit.pptx",
);
const transformedGroupSavedZip = await JSZip.loadAsync(await transformedGroupResult.file.arrayBuffer());
const transformedGroupSavedXml = await transformedGroupSavedZip.file("ppt/slides/slide1.xml").async("text");
const transformedGroupSavedFrame = (transformedGroupSavedXml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [])
  .find((frame) => frame.includes(`id="${groupedGraphicFrameObjectId}"`));
assert.ok(transformedGroupSavedFrame);
const expectedLocalX = Math.round((editedGroupedTable.x / 100) * generatedWidth);
const expectedLocalY = Math.round((editedGroupedTable.y / 100) * generatedHeight);
const expectedLocalWidth = Math.round((editedGroupedTable.w / 100) * generatedWidth);
const expectedLocalHeight = Math.round((editedGroupedTable.h / 100) * generatedHeight);
assert.match(transformedGroupSavedFrame, new RegExp(`<a:off x="${expectedLocalX}" y="${expectedLocalY}"/>`));
assert.match(transformedGroupSavedFrame, new RegExp(`<a:ext cx="${expectedLocalWidth}" cy="${expectedLocalHeight}"/>`));
assert.match(transformedGroupSavedXml, /Editable grouped title/, "grouped shapes must remain text-editable without flattening the group");

const groupedObjectDeletionEdited = structuredClone(groupedObjectBaseline);
groupedObjectDeletionEdited[0].shapes = groupedObjectDeletionEdited[0].shapes.filter((shape) => (
  shape.id !== "grouped-table"
));
const groupedObjectDeletionResult = await preservePresentationFileWithSnapshot(
  groupedObjectInput,
  groupedObjectBaseline,
  groupedObjectDeletionEdited,
  "grouped-object-deletion.pptx",
);
const groupedObjectDeletionZip = await JSZip.loadAsync(await groupedObjectDeletionResult.file.arrayBuffer());
const groupedObjectDeletionXml = await groupedObjectDeletionZip.file("ppt/slides/slide1.xml").async("text");
assert.doesNotMatch(
  groupedObjectDeletionXml,
  new RegExp(`<p:cNvPr\\b[^>]*\\bid="${groupedGraphicFrameObjectId}"`),
  "deleting one grouped object must remove it without restoring the complete source group",
);
assert.match(
  groupedObjectDeletionXml,
  new RegExp(`<p:cNvPr\\b[^>]*\\bid="${titleObjectId}"`),
  "deleting one grouped object must retain the other group members",
);

const unsupportedGroupMemberId = "102";
const unsupportedGroupMemberXml = `<p:contentPart r:id="rIdUnsupported"><p:nvContentPartPr><p:cNvPr id="${unsupportedGroupMemberId}" name="Unsupported ink"/><p:cNvContentPartPr/></p:nvContentPartPr><p:xfrm><a:off x="0" y="0"/><a:ext cx="1000" cy="1000"/></p:xfrm></p:contentPart>`;
const unsupportedGroupXml = groupedObjectXml.replace("</p:grpSp>", `${unsupportedGroupMemberXml}</p:grpSp>`);
const unsupportedGroupZip = await JSZip.loadAsync(await blob.arrayBuffer());
const unsupportedGroupSlideXml = await unsupportedGroupZip.file("ppt/slides/slide1.xml").async("text");
unsupportedGroupZip.file(
  "ppt/slides/slide1.xml",
  unsupportedGroupSlideXml.replace(groupedGraphicFrame, "").replace(titleShapeXml, unsupportedGroupXml),
);
const unsupportedGroupInput = await unsupportedGroupZip.generateAsync({ type: "arraybuffer" });
const unsupportedGroupEdited = structuredClone(groupedObjectBaseline);
unsupportedGroupEdited[0].shapes = unsupportedGroupEdited[0].shapes.filter((shape) => (
  shape.id === "top-level-picture"
));
const unsupportedGroupResult = await preservePresentationFileWithSnapshot(
  unsupportedGroupInput,
  groupedObjectBaseline,
  unsupportedGroupEdited,
  "unsupported-group-member.pptx",
);
const unsupportedGroupSavedZip = await JSZip.loadAsync(await unsupportedGroupResult.file.arrayBuffer());
const unsupportedGroupSavedXml = await unsupportedGroupSavedZip.file("ppt/slides/slide1.xml").async("text");
assert.match(
  unsupportedGroupSavedXml,
  new RegExp(`<p:cNvPr\\b[^>]*\\bid="${unsupportedGroupMemberId}"`),
  "deleting every modeled group member must retain unsupported raw members",
);
assert.doesNotMatch(unsupportedGroupSavedXml, new RegExp(`<p:cNvPr\\b[^>]*\\bid="${titleObjectId}"`));
assert.doesNotMatch(unsupportedGroupSavedXml, new RegExp(`<p:cNvPr\\b[^>]*\\bid="${groupedGraphicFrameObjectId}"`));

const nestedGroupObjectZip = await JSZip.loadAsync(await blob.arrayBuffer());
const nestedGroupSlideXml = await nestedGroupObjectZip.file("ppt/slides/slide1.xml").async("text");
const nestedGroupXml = '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="101" name="Outer group"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
  + '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/><a:chOff x="0" y="0"/><a:chExt cx="1000000" cy="1000000"/></a:xfrm></p:grpSpPr>'
  + `${groupedObjectXml}</p:grpSp>`;
nestedGroupObjectZip.file(
  "ppt/slides/slide1.xml",
  nestedGroupSlideXml.replace(groupedGraphicFrame, "").replace(titleShapeXml, nestedGroupXml),
);
const nestedGroupInput = await nestedGroupObjectZip.generateAsync({ type: "arraybuffer" });
const nestedGroupEdited = structuredClone(groupedObjectBaseline);
nestedGroupEdited[0].shapes = [
  nestedGroupEdited[0].shapes[2],
  nestedGroupEdited[0].shapes[0],
  nestedGroupEdited[0].shapes[1],
];
const nestedGroupResult = await preservePresentationFileWithSnapshot(
  nestedGroupInput,
  groupedObjectBaseline,
  nestedGroupEdited,
  "nested-group.pptx",
);
const nestedGroupZip = await JSZip.loadAsync(await nestedGroupResult.file.arrayBuffer());
const nestedGroupSavedXml = await nestedGroupZip.file("ppt/slides/slide1.xml").async("text");
let nestedGroupDepth = 0;
for (const match of nestedGroupSavedXml.matchAll(/<p:grpSp\b[^>]*>|<\/p:grpSp>/g)) {
  if (match[0].startsWith("</")) {
    nestedGroupDepth -= 1;
    assert.ok(nestedGroupDepth >= 0, "nested groups must not leave an orphaned closing tag");
  } else nestedGroupDepth += 1;
}
assert.equal(nestedGroupDepth, 0, "nested groups must remain balanced after paint-order changes");
assert.match(nestedGroupSavedXml, /<p:cNvPr id="101" name="Outer group"\/>/);
assert.match(nestedGroupSavedXml, /<p:cNvPr id="100" name="Editable group"\/>/);
const generatedEdited = structuredClone(generatedBaseline);
generatedEdited[0].shapes[0].texts[0].text = "Preserved editable title";
const textPatchedFile = await preservePresentationFile(await blob.arrayBuffer(), generatedBaseline, generatedEdited, "text-patched.pptx");
const textPatchedZip = await JSZip.loadAsync(await textPatchedFile.arrayBuffer());
const textPatchedXml = await textPatchedZip.file("ppt/slides/slide1.xml").async("text");
assert.match(textPatchedXml, /Preserved editable title/);
assert.match(textPatchedXml, /Metric/);
assert.match(textPatchedXml, /Saved/);

const fieldPreservationZip = await JSZip.loadAsync(await blob.arrayBuffer());
const fieldPreservationSlideXml = await fieldPreservationZip.file("ppt/slides/slide1.xml").async("text");
const fieldPreservationTitleShape = (fieldPreservationSlideXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [])
  .find((shape) => shape.includes("Editable title"));
assert.ok(fieldPreservationTitleShape, "text preservation fixture should expose its title shape");
const linkedFieldParagraph = '<a:p><a:pPr lvl="2" rtl="1"><a:tabLst><a:tab pos="12345" algn="l"/></a:tabLst><a:extLst><a:ext uri="{custom-paragraph-extension}"/></a:extLst></a:pPr><a:r><a:rPr lang="en-US"><a:hlinkClick r:id="rId999"/></a:rPr><a:t>Linked</a:t></a:r><a:br/><a:r><a:rPr lang="en-US"/><a:t xml:space="preserve">page </a:t></a:r><a:fld id="{00000000-0000-0000-0000-000000000001}" type="slidenum"><a:rPr lang="en-US"/><a:t>1</a:t></a:fld><a:endParaRPr lang="en-US"/></a:p>';
fieldPreservationZip.file(
  "ppt/slides/slide1.xml",
  fieldPreservationSlideXml.replace(
    fieldPreservationTitleShape,
    fieldPreservationTitleShape.replace("</p:txBody>", `${linkedFieldParagraph}</p:txBody>`),
  ),
);
const fieldPreservationRelationshipsPart = "ppt/slides/_rels/slide1.xml.rels";
const fieldPreservationRelationships = await fieldPreservationZip.file(fieldPreservationRelationshipsPart).async("text");
fieldPreservationZip.file(
  fieldPreservationRelationshipsPart,
  fieldPreservationRelationships.replace(
    /<\/Relationships>\s*$/,
    '<Relationship Id="rId999" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/linked-page" TargetMode="External"/></Relationships>',
  ),
);
const fieldPreservationInput = await fieldPreservationZip.generateAsync({ type: "arraybuffer" });
const fieldPreservationBaseline = structuredClone(generatedBaseline);
fieldPreservationBaseline[0].shapes[0].texts.push({
  text: "Linked\npage 1",
  runs: [{ text: "Linked" }, { text: "\n" }, { text: "page " }, { text: "1" }],
});
const fieldPreservationEdited = structuredClone(fieldPreservationBaseline);
fieldPreservationEdited[0].shapes[0].texts[0].text = "Edited title only";
fieldPreservationEdited[0].shapes[0].texts[1].text = "Updated\npage 1";
fieldPreservationEdited[0].shapes[0].texts[1].runs = [{ text: "Updated" }, { text: "\n" }, { text: "page " }, { text: "1" }];
const fieldPreservationFile = await preservePresentationFile(
  fieldPreservationInput,
  fieldPreservationBaseline,
  fieldPreservationEdited,
  "field-preservation.pptx",
);
const fieldPreservationSavedZip = await JSZip.loadAsync(await fieldPreservationFile.arrayBuffer());
const fieldPreservationSavedXml = await fieldPreservationSavedZip.file("ppt/slides/slide1.xml").async("text");
assert.match(fieldPreservationSavedXml, /Edited title only/);
assert.match(fieldPreservationSavedXml, /Updated/);
assert.match(fieldPreservationSavedXml, /<a:br\s*\/>/);
assert.doesNotMatch(
  fieldPreservationSavedXml,
  /<a:r[\s>](?:(?!<\/a:r>)[\s\S])*<a:br\b/,
  "PowerPoint line breaks must remain paragraph children rather than being nested inside runs",
);
assert.match(
  fieldPreservationSavedXml,
  /<a:fld\b[^>]*\btype="slidenum"/,
  "editing one paragraph should preserve an untouched PowerPoint field",
);
assert.match(
  fieldPreservationSavedXml,
  /<a:hlinkClick\b[^>]*\br:id="rId999"/,
  "editing one paragraph should preserve hyperlinks in untouched runs",
);
assert.match(
  await fieldPreservationSavedZip.file(fieldPreservationRelationshipsPart).async("text"),
  /\bId="rId999"/,
  "preserved hyperlinks should retain their package relationship",
);

const aiFieldEditState = JSON.parse(buildPresentationLiveEditContent(fieldPreservationBaseline, {
  documentName: "field-preservation.pptx",
  activeSlideIndex: 0,
  selectedShapeId: fieldPreservationBaseline[0].shapes[0].id,
}));
aiFieldEditState.slides[0].shapes[0].paragraphs[1].text = "Reworded\npage 1";
aiFieldEditState.slides[0].shapes[0].paragraphs[1].edits = [{
  start: 0,
  end: 6,
  text: "Reworded",
}];
const aiFieldEdited = applyPresentationLiveEditContent(fieldPreservationBaseline, JSON.stringify(aiFieldEditState));
assert.ok(aiFieldEdited, "AI presentation edits should retain a valid editable snapshot");
assert.ok(aiFieldEdited[0].shapes[0].texts[1].runs?.length, "AI text edits should reconcile rich text runs instead of discarding them");
assert.ok(aiFieldEdited[0].shapes[0].texts[1].sourceMap?.includes(12), "AI text edits should retain the unchanged field's source identity");
const aiFieldEditFile = await preservePresentationFile(
  fieldPreservationInput,
  fieldPreservationBaseline,
  aiFieldEdited,
  "ai-field-edit.pptx",
);
const aiFieldEditZip = await JSZip.loadAsync(await aiFieldEditFile.arrayBuffer());
const aiFieldEditXml = await aiFieldEditZip.file("ppt/slides/slide1.xml").async("text");
const aiFieldEditParagraph = (aiFieldEditXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [])
  .find((paragraph) => paragraph.includes("Reworded"));
assert.ok(aiFieldEditParagraph, "AI-edited rich text should remain in its source paragraph");
assert.match(aiFieldEditParagraph, /<a:fld\b[^>]*\btype="slidenum"/);
assert.match(aiFieldEditParagraph, /<a:hlinkClick\b[^>]*\br:id="rId999"/);

const fieldFormattingEdited = structuredClone(fieldPreservationBaseline);
Object.assign(fieldFormattingEdited[0].shapes[0].texts[1], {
  align: "center",
  bullet: "•",
  bold: true,
  fontSize: 24,
  fontFamily: "Aptos",
  color: "#123456",
});
fieldFormattingEdited[0].shapes[0].texts[1].runs = fieldFormattingEdited[0].shapes[0].texts[1].runs
  .map((run) => ({ ...run, bold: true, fontSize: 24, fontFamily: "Aptos", color: "#123456" }));
const fieldFormattingFile = await preservePresentationFile(
  fieldPreservationInput,
  fieldPreservationBaseline,
  fieldFormattingEdited,
  "field-formatting.pptx",
);
const fieldFormattingZip = await JSZip.loadAsync(await fieldFormattingFile.arrayBuffer());
const fieldFormattingXml = await fieldFormattingZip.file("ppt/slides/slide1.xml").async("text");
const fieldFormattingParagraph = (fieldFormattingXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [])
  .find((paragraph) => paragraph.includes("Linked"));
assert.ok(fieldFormattingParagraph, "formatted rich text should remain in its source paragraph");
assert.match(fieldFormattingParagraph, /<a:pPr\b[^>]*\blvl="2"[^>]*\brtl="1"[^>]*\balgn="ctr"/);
assert.match(fieldFormattingParagraph, /<a:tab\b[^>]*\bpos="12345"/);
assert.match(fieldFormattingParagraph, /<a:ext\b[^>]*\buri="\{custom-paragraph-extension\}"/);
assert.match(fieldFormattingParagraph, /<a:buChar\b[^>]*\bchar="•"/);
assert.match(fieldFormattingParagraph, /<a:fld\b[^>]*\btype="slidenum"/);
assert.match(fieldFormattingParagraph, /<a:hlinkClick\b[^>]*\br:id="rId999"/);
assert.match(fieldFormattingParagraph, /<a:rPr\b[^>]*\bsz="2400"[^>]*\bb="1"/);
assert.match(fieldFormattingParagraph, /<a:solidFill><a:srgbClr val="123456"/);
assert.match(fieldFormattingParagraph, /<a:latin\b[^>]*\btypeface="Aptos"/);
assert.match(
  await fieldFormattingZip.file(fieldPreservationRelationshipsPart).async("text"),
  /\bId="rId999"/,
  "formatting a linked run must preserve its hyperlink relationship",
);

const longFieldZip = await JSZip.loadAsync(fieldPreservationInput);
const longFieldSlideXml = await longFieldZip.file("ppt/slides/slide1.xml").async("text");
const longFieldParagraph = `<a:p><a:r><a:rPr lang="en-US"/><a:t>${"A".repeat(300)}</a:t></a:r><a:fld id="{00000000-0000-0000-0000-000000000004}" type="slidenum"><a:rPr lang="en-US"/><a:t>1</a:t></a:fld><a:r><a:rPr lang="en-US"/><a:t>${"B".repeat(300)}</a:t></a:r><a:endParaRPr lang="en-US"/></a:p>`;
longFieldZip.file("ppt/slides/slide1.xml", longFieldSlideXml.replace(linkedFieldParagraph, longFieldParagraph));
const longFieldInput = await longFieldZip.generateAsync({ type: "arraybuffer" });
const longFieldBaseline = structuredClone(generatedBaseline);
longFieldBaseline[0].shapes[0].texts.push({
  text: longSourceText,
  runs: [{ text: "A".repeat(300) }, { text: "1" }, { text: "B".repeat(300) }],
});
const longFieldEdited = structuredClone(longFieldBaseline);
longFieldEdited[0].shapes[0].texts[1] = {
  ...longFieldEdited[0].shapes[0].texts[1],
  text: longEditedText,
  sourceMap: longSourceMap,
  runs: [{ text: "X".repeat(300) }, { text: "1" }, { text: "Y".repeat(300) }],
};
const longFieldFile = await preservePresentationFile(
  longFieldInput,
  longFieldBaseline,
  longFieldEdited,
  "long-field-edit.pptx",
);
const longFieldSavedZip = await JSZip.loadAsync(await longFieldFile.arrayBuffer());
const longFieldSavedXml = await longFieldSavedZip.file("ppt/slides/slide1.xml").async("text");
assert.match(
  longFieldSavedXml,
  /<a:fld\b[^>]*\btype="slidenum"/,
  "long edits must preserve an unchanged field even when the total edit distance exceeds the bounded diff",
);

const duplicateFieldZip = await JSZip.loadAsync(fieldPreservationInput);
const duplicateFieldSlideXml = await duplicateFieldZip.file("ppt/slides/slide1.xml").async("text");
const duplicateFieldParagraph = '<a:p><a:r><a:rPr lang="en-US"/><a:t>1</a:t></a:r><a:fld id="{00000000-0000-0000-0000-000000000005}" type="slidenum"><a:rPr lang="en-US"/><a:t>1</a:t></a:fld><a:endParaRPr lang="en-US"/></a:p>';
duplicateFieldZip.file("ppt/slides/slide1.xml", duplicateFieldSlideXml.replace(linkedFieldParagraph, duplicateFieldParagraph));
const duplicateFieldInput = await duplicateFieldZip.generateAsync({ type: "arraybuffer" });
const duplicateFieldBaseline = structuredClone(generatedBaseline);
duplicateFieldBaseline[0].shapes[0].texts.push({
  text: "11",
  runs: [{ text: "1", color: "#0000ff" }, { text: "1", color: "#ff0000" }],
});
const duplicateFieldEdited = structuredClone(duplicateFieldBaseline);
duplicateFieldEdited[0].shapes[0].texts[1] = {
  text: "1",
  sourceMap: [1],
  runs: [{ text: "1", color: "#ff0000" }],
};
const duplicateFieldResult = await preservePresentationFileWithSnapshot(
  duplicateFieldInput,
  duplicateFieldBaseline,
  duplicateFieldEdited,
  "duplicate-field-edit.pptx",
);
const duplicateFieldSavedZip = await JSZip.loadAsync(await duplicateFieldResult.file.arrayBuffer());
const duplicateFieldSavedXml = await duplicateFieldSavedZip.file("ppt/slides/slide1.xml").async("text");
const duplicateFieldSavedParagraph = (duplicateFieldSavedXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [])
  .find((paragraph) => paragraph.includes('type="slidenum"'));
assert.ok(duplicateFieldSavedParagraph, "deleting the ordinary duplicate should retain the field with the same visible value");
assert.doesNotMatch(duplicateFieldSavedParagraph, /<a:r[\s>]/, "the explicitly deleted ordinary run should not survive");
assert.equal(duplicateFieldResult.slides[0].shapes[0].texts[1].sourceMap, undefined, "saved snapshots should reset transient edit provenance");

const duplicateAiState = JSON.parse(buildPresentationLiveEditContent(duplicateFieldBaseline, {
  documentName: "duplicate-ai-field.pptx",
  activeSlideIndex: 0,
  selectedShapeId: duplicateFieldBaseline[0].shapes[0].id,
}));
duplicateAiState.slides[0].shapes[0].paragraphs[1].text = "1";
duplicateAiState.slides[0].shapes[0].paragraphs[1].edits = [{ start: 0, end: 1, text: "" }];
const duplicateAiEdited = applyPresentationLiveEditContent(duplicateFieldBaseline, JSON.stringify(duplicateAiState));
assert.ok(duplicateAiEdited, "AI edits with explicit source ranges should produce an editable snapshot");
assert.deepEqual(duplicateAiEdited[0].shapes[0].texts[1].sourceMap, [1]);
assert.deepEqual(
  duplicateAiEdited[0].shapes[0].texts[1].runs,
  [{ text: "1", color: "#ff0000" }],
  "AI range edits must preserve the formatting of the retained field",
);
const invalidDuplicateAiState = structuredClone(duplicateAiState);
delete invalidDuplicateAiState.slides[0].shapes[0].paragraphs[1].edits;
assert.equal(
  applyPresentationLiveEditContent(duplicateFieldBaseline, JSON.stringify(invalidDuplicateAiState)),
  null,
  "ambiguous AI text edits without source ranges must fail closed",
);
const invalidParagraphStructureState = structuredClone(duplicateAiState);
invalidParagraphStructureState.slides[0].shapes[0].paragraphs.pop();
assert.equal(
  applyPresentationLiveEditContent(duplicateFieldBaseline, JSON.stringify(invalidParagraphStructureState)),
  null,
  "AI edits that remove presentation paragraphs must fail closed",
);
const missingShapeState = structuredClone(duplicateAiState);
missingShapeState.slides[0].shapes.pop();
assert.deepEqual(
  applyPresentationLiveEditContent(duplicateFieldBaseline, JSON.stringify(missingShapeState))[0].shapes,
  [],
  "AI edits should be able to remove an editable native presentation shape",
);
const lockedShapeBaseline = structuredClone(duplicateFieldBaseline);
lockedShapeBaseline[0].shapes[0].source.editable = false;
const missingLockedShapeState = JSON.parse(buildPresentationLiveEditContent(lockedShapeBaseline, {
  documentName: "locked-shape.pptx",
  activeSlideIndex: 0,
  selectedShapeId: null,
}));
missingLockedShapeState.slides[0].shapes.pop();
assert.equal(
  applyPresentationLiveEditContent(lockedShapeBaseline, JSON.stringify(missingLockedShapeState)),
  null,
  "AI edits must not remove a presentation object the native editor marked read-only",
);
const lockedOrderBaseline = structuredClone(lockedShapeBaseline);
lockedOrderBaseline[0].shapes.push({
  ...structuredClone(lockedOrderBaseline[0].shapes[0]),
  id: "editable-after-locked",
  source: { ...lockedOrderBaseline[0].shapes[0].source, editable: true },
});
const reorderedLockedShapeState = JSON.parse(buildPresentationLiveEditContent(lockedOrderBaseline, {
  documentName: "locked-shape-order.pptx",
  activeSlideIndex: 0,
  selectedShapeId: null,
}));
reorderedLockedShapeState.slides[0].shapes.reverse();
assert.equal(
  applyPresentationLiveEditContent(lockedOrderBaseline, JSON.stringify(reorderedLockedShapeState)),
  null,
  "AI edits must not move a read-only presentation object across existing slide objects",
);

const fieldDeletionEdited = structuredClone(fieldPreservationBaseline);
fieldDeletionEdited[0].shapes[0].texts[1].text = "Linked\npage ";
fieldDeletionEdited[0].shapes[0].texts[1].runs = [{ text: "Linked" }, { text: "\n" }, { text: "page " }];
const fieldDeletionFile = await preservePresentationFile(
  fieldPreservationInput,
  fieldPreservationBaseline,
  fieldDeletionEdited,
  "field-deletion.pptx",
);
const fieldDeletionZip = await JSZip.loadAsync(await fieldDeletionFile.arrayBuffer());
const fieldDeletionXml = await fieldDeletionZip.file("ppt/slides/slide1.xml").async("text");
const fieldDeletionParagraph = (fieldDeletionXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [])
  .find((paragraph) => paragraph.includes("Linked"));
assert.ok(fieldDeletionParagraph, "field deletion fixture should retain the edited paragraph");
assert.doesNotMatch(
  fieldDeletionParagraph,
  /<a:fld\b[^>]*\btype="slidenum"/,
  "deleting a PowerPoint field's visible text should delete the dynamic field itself",
);
assert.match(fieldDeletionParagraph, /<a:hlinkClick\b[^>]*\br:id="rId999"/);
assert.match(fieldDeletionParagraph, /<a:br\s*\/>/);

const separatedEdits = structuredClone(fieldPreservationBaseline);
separatedEdits[0].shapes[0].texts[1].text = "Updated\nsheet 1";
separatedEdits[0].shapes[0].texts[1].runs = [{ text: "Updated" }, { text: "\n" }, { text: "sheet " }, { text: "1" }];
const separatedEditsFile = await preservePresentationFile(
  fieldPreservationInput,
  fieldPreservationBaseline,
  separatedEdits,
  "separated-field-edits.pptx",
);
const separatedEditsZip = await JSZip.loadAsync(await separatedEditsFile.arrayBuffer());
const separatedEditsXml = await separatedEditsZip.file("ppt/slides/slide1.xml").async("text");
const separatedEditsParagraph = (separatedEditsXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [])
  .find((paragraph) => paragraph.includes("Updated"));
assert.ok(separatedEditsParagraph, "two separated edits should retain their source paragraph");
assert.match(
  separatedEditsParagraph,
  /<a:fld\b[^>]*\btype="slidenum"/,
  "two separated edits must preserve an unchanged dynamic field between them",
);
const separatedEditsHyperlinkRun = separatedEditsParagraph.match(/<a:r[\s>](?:(?!<\/a:r>)[\s\S])*?<a:hlinkClick\b[^>]*\br:id="rId999"(?:(?!<\/a:r>)[\s\S])*?<\/a:r>/)?.[0];
assert.ok(separatedEditsHyperlinkRun, "the edited linked run should retain its hyperlink");
assert.match(separatedEditsHyperlinkRun, /Updated/);
assert.equal(
  (separatedEditsParagraph.match(/<a:hlinkClick\b[^>]*\br:id="rId999"/g) || []).length,
  1,
  "a later edit must not clone an earlier hyperlink onto plain text",
);
assert.doesNotMatch(
  separatedEditsHyperlinkRun,
  /sheet/,
  "editing later plain text must not expand an earlier hyperlink across the paragraph",
);

const styledFieldZip = await JSZip.loadAsync(fieldPreservationInput);
const styledFieldSlideXml = await styledFieldZip.file("ppt/slides/slide1.xml").async("text");
const styledFieldParagraph = '<a:p><a:r><a:rPr lang="en-US"><a:solidFill><a:srgbClr val="0000FF"/></a:solidFill></a:rPr><a:t>A</a:t></a:r><a:fld id="{00000000-0000-0000-0000-000000000003}" type="slidenum"><a:rPr lang="en-US"><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></a:rPr><a:t>1</a:t></a:fld><a:endParaRPr lang="en-US"/></a:p>';
styledFieldZip.file("ppt/slides/slide1.xml", styledFieldSlideXml.replace(linkedFieldParagraph, styledFieldParagraph));
const styledFieldInput = await styledFieldZip.generateAsync({ type: "arraybuffer" });
const styledFieldBaseline = structuredClone(generatedBaseline);
styledFieldBaseline[0].shapes[0].texts.push({
  text: "A1",
  runs: [{ text: "A", color: "#0000ff" }, { text: "1", color: "#ff0000" }],
});
const styledFieldEdited = structuredClone(styledFieldBaseline);
styledFieldEdited[0].shapes[0].texts[1].text = "AX";
styledFieldEdited[0].shapes[0].texts[1].runs = [{ text: "AX", color: "#0000ff" }];
const styledFieldFile = await preservePresentationFile(
  styledFieldInput,
  styledFieldBaseline,
  styledFieldEdited,
  "styled-field-replacement.pptx",
);
const styledFieldSavedZip = await JSZip.loadAsync(await styledFieldFile.arrayBuffer());
const styledFieldSavedXml = await styledFieldSavedZip.file("ppt/slides/slide1.xml").async("text");
const styledFieldSavedParagraph = (styledFieldSavedXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [])
  .find((paragraph) => paragraph.includes(">X<"));
assert.ok(styledFieldSavedParagraph, "field replacement should remain in its source paragraph");
assert.doesNotMatch(styledFieldSavedParagraph, /<a:fld\b/);
assert.match(
  styledFieldSavedParagraph,
  /<a:rPr\b[^>]*>[\s\S]*?<a:srgbClr val="0000FF"\/>[\s\S]*?<\/a:rPr>[\s\S]*?<a:t>X<\/a:t>/,
  "field replacement text should use the style shown by the editor",
);

const tablePreservationZip = await JSZip.loadAsync(await blob.arrayBuffer());
const tablePreservationSlideXml = await tablePreservationZip.file("ppt/slides/slide1.xml").async("text");
const tablePreservationFrame = (tablePreservationSlideXml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [])
  .find((frame) => frame.includes("Metric"));
const tablePreservationObjectId = tablePreservationFrame?.match(/<p:cNvPr\b[^>]*\bid="([^"]+)"/)?.[1];
const tablePreservationFirstCell = tablePreservationFrame?.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(tablePreservationFrame && tablePreservationObjectId && tablePreservationFirstCell);
const tableRichParagraph = '<a:p><a:pPr lvl="0"><a:buChar char="•"/></a:pPr><a:r><a:rPr lang="en-US" i="1"><a:hlinkClick r:id="rId998"/></a:rPr><a:t xml:space="preserve">Linked </a:t></a:r><a:fld id="{00000000-0000-0000-0000-000000000002}" type="slidenum"><a:rPr lang="en-US"/><a:t>1</a:t></a:fld><a:endParaRPr lang="en-US"/></a:p>';
const tablePreservationFrameWithField = tablePreservationFrame.replace(
  tablePreservationFirstCell,
  tablePreservationFirstCell
    .replace("</a:txBody>", `${tableRichParagraph}</a:txBody>`)
    .replace(
      /<a:tcPr\b[^>]*>/,
      '$&<a:lnL><a:solidFill><a:srgbClr val="0A0B0C"/></a:solidFill><a:prstDash val="solid"/></a:lnL>',
    ),
);
tablePreservationZip.file(
  "ppt/slides/slide1.xml",
  tablePreservationSlideXml.replace(tablePreservationFrame, tablePreservationFrameWithField),
);
const tablePreservationRelationshipsPart = "ppt/slides/_rels/slide1.xml.rels";
const tablePreservationRelationships = await tablePreservationZip.file(tablePreservationRelationshipsPart).async("text");
tablePreservationZip.file(
  tablePreservationRelationshipsPart,
  tablePreservationRelationships.replace(
    /<\/Relationships>\s*$/,
    '<Relationship Id="rId998" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/table-link" TargetMode="External"/></Relationships>',
  ),
);
const tablePreservationInput = await tablePreservationZip.generateAsync({ type: "arraybuffer" });
const tablePreservationBaseline = [{
  id: "slide-1",
  sourcePart: "ppt/slides/slide1.xml",
  shapes: [{
    id: "table",
    type: "table",
    x: 50,
    y: 32,
    w: 42,
    h: 48,
    texts: [],
    tableRows: [
      [{ text: "Metric\nLinked 1", bold: true, fill: "#e7e5e4" }, { text: "Value", bold: true, fill: "#e7e5e4" }],
      [{ text: "Saved" }, { text: "Yes" }],
    ],
    source: {
      part: "ppt/slides/slide1.xml",
      kind: "graphicFrame",
      objectId: tablePreservationObjectId,
      editable: true,
    },
  }],
}];
const invalidTableStructureState = JSON.parse(buildPresentationLiveEditContent(tablePreservationBaseline, {
  documentName: "invalid-table-structure.pptx",
  activeSlideIndex: 0,
  selectedShapeId: tablePreservationBaseline[0].shapes[0].id,
}));
invalidTableStructureState.slides[0].shapes[0].table[0].pop();
assert.equal(
  applyPresentationLiveEditContent(tablePreservationBaseline, JSON.stringify(invalidTableStructureState)),
  null,
  "AI edits that remove presentation table cells must fail closed",
);
const invalidExistingTableFrameFormatState = JSON.parse(buildPresentationLiveEditContent(
  tablePreservationBaseline,
  {
    documentName: "invalid-table-frame-format.pptx",
    activeSlideIndex: 0,
    selectedShapeId: tablePreservationBaseline[0].shapes[0].id,
  },
));
invalidExistingTableFrameFormatState.slides[0].shapes[0].format.fill = "#ff0000";
assert.equal(
  applyPresentationLiveEditContent(
    tablePreservationBaseline,
    JSON.stringify(invalidExistingTableFrameFormatState),
  ),
  null,
  "existing table frames must use cell-specific formatting rather than lossy shape formatting",
);
const invalidPersistedTableFrameFormat = structuredClone(tablePreservationBaseline);
invalidPersistedTableFrameFormat[0].shapes[0].fill = "#ff0000";
await assert.rejects(
  () => preservePresentationFile(
    tablePreservationInput,
    tablePreservationBaseline,
    invalidPersistedTableFrameFormat,
    "invalid-table-frame-format.pptx",
  ),
  /dedicated native formatting operations/,
  "the OOXML saver must reject graphic-frame formatting instead of silently discarding it",
);
const tablePreservationEdited = structuredClone(tablePreservationBaseline);
tablePreservationEdited[0].shapes[0].tableRows[0][0].text = "Metric\nUpdated 1";
const tablePreservationFile = await preservePresentationFile(
  tablePreservationInput,
  tablePreservationBaseline,
  tablePreservationEdited,
  "table-preservation.pptx",
);
const tablePreservationSavedZip = await JSZip.loadAsync(await tablePreservationFile.arrayBuffer());
const tablePreservationSavedXml = await tablePreservationSavedZip.file("ppt/slides/slide1.xml").async("text");
assert.match(tablePreservationSavedXml, /Updated/);
const tablePreservationSavedFirstCell = tablePreservationSavedXml.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(tablePreservationSavedFirstCell, "saved presentation should retain the edited table cell");
assert.equal(
  (tablePreservationSavedFirstCell.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || []).length,
  2,
  "editing table text should preserve its original paragraph structure",
);
assert.match(
  tablePreservationSavedFirstCell,
  /<a:fld\b[^>]*\btype="slidenum"/,
  "editing rich table text should preserve an untouched field in the same cell",
);
assert.match(tablePreservationSavedFirstCell, /<a:hlinkClick\b[^>]*\br:id="rId998"/);
assert.match(tablePreservationSavedFirstCell, /<a:buChar\b[^>]*\bchar="•"/);
assert.match(tablePreservationSavedFirstCell, /<a:rPr\b[^>]*\bi="1"/);

const tableRichFormattingEdited = structuredClone(tablePreservationBaseline);
Object.assign(tableRichFormattingEdited[0].shapes[0].tableRows[0][0], {
  italic: true,
  fontSize: 17,
  fontFamily: "Aptos",
  color: "#123456",
  fill: "#fedcba",
});
const tableRichFormattingFile = await preservePresentationFile(
  tablePreservationInput,
  tablePreservationBaseline,
  tableRichFormattingEdited,
  "table-rich-formatting.pptx",
);
const tableRichFormattingZip = await JSZip.loadAsync(await tableRichFormattingFile.arrayBuffer());
const tableRichFormattingXml = await tableRichFormattingZip.file("ppt/slides/slide1.xml").async("text");
const tableRichFormattingCell = tableRichFormattingXml.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(tableRichFormattingCell, "formatted table text should retain its source cell");
assert.equal(
  (tableRichFormattingCell.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || []).length,
  2,
  "formatting a table cell must preserve its paragraph structure",
);
assert.match(tableRichFormattingCell, /<a:fld\b[^>]*\btype="slidenum"/);
assert.match(tableRichFormattingCell, /<a:hlinkClick\b[^>]*\br:id="rId998"/);
assert.match(tableRichFormattingCell, /<a:buChar\b[^>]*\bchar="•"/);
assert.match(tableRichFormattingCell, /<a:rPr\b[^>]*\bsz="1700"[^>]*\bi="1"/);
assert.match(tableRichFormattingCell, /<a:latin\b[^>]*\btypeface="Aptos"/);
assert.match(tableRichFormattingCell, /<a:srgbClr\b[^>]*\bval="123456"/);
const tableRichFormattingProperties = tableRichFormattingCell.match(/<a:tcPr\b[^>]*>[\s\S]*?<\/a:tcPr>/)?.[0];
assert.ok(tableRichFormattingProperties, "formatted table cell should retain its property block");
assert.match(
  tableRichFormattingProperties,
  /<a:lnL>[\s\S]*?<a:srgbClr val="0A0B0C"\/>[\s\S]*?<\/a:lnL>/,
  "changing cell background must not replace a nested border fill",
);
assert.match(
  tableRichFormattingProperties,
  /<\/a:lnB><a:solidFill><a:srgbClr val="FEDCBA"(?:\/>|><\/a:srgbClr>)<\/a:solidFill>/,
  "changing cell background must replace the direct tcPr fill",
);

const tableParagraphMergeEdited = structuredClone(tablePreservationBaseline);
tableParagraphMergeEdited[0].shapes[0].tableRows[0][0].text = "MetricLinked 1";
const tableParagraphMergeFile = await preservePresentationFile(
  tablePreservationInput,
  tablePreservationBaseline,
  tableParagraphMergeEdited,
  "table-paragraph-merge.pptx",
);
const tableParagraphMergeZip = await JSZip.loadAsync(await tableParagraphMergeFile.arrayBuffer());
const tableParagraphMergeXml = await tableParagraphMergeZip.file("ppt/slides/slide1.xml").async("text");
const tableParagraphMergeCell = tableParagraphMergeXml.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(tableParagraphMergeCell, "merged table text should retain its source cell");
assert.equal(
  (tableParagraphMergeCell.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || []).length,
  1,
  "deleting a table paragraph boundary should merge the two source paragraphs",
);
assert.match(tableParagraphMergeCell, /<a:fld\b[^>]*\btype="slidenum"/);
assert.match(tableParagraphMergeCell, /<a:hlinkClick\b[^>]*\br:id="rId998"/);

const sourceMappedTableMergeEdited = structuredClone(tablePreservationBaseline);
sourceMappedTableMergeEdited[0].shapes[0].tableRows[0][0] = {
  ...sourceMappedTableMergeEdited[0].shapes[0].tableRows[0][0],
  text: "XY1",
  sourceMap: [null, null, 14],
};
const sourceMappedTableMergeFile = await preservePresentationFile(
  tablePreservationInput,
  tablePreservationBaseline,
  sourceMappedTableMergeEdited,
  "source-mapped-table-merge.pptx",
);
const sourceMappedTableMergeZip = await JSZip.loadAsync(await sourceMappedTableMergeFile.arrayBuffer());
const sourceMappedTableMergeXml = await sourceMappedTableMergeZip.file("ppt/slides/slide1.xml").async("text");
const sourceMappedTableMergeCell = sourceMappedTableMergeXml.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(sourceMappedTableMergeCell, "source-mapped table edits should retain their source cell");
assert.match(
  sourceMappedTableMergeCell,
  /<a:fld\b[^>]*\btype="slidenum"/,
  "explicit table source mappings must survive paragraph-boundary merge heuristics",
);
assert.doesNotMatch(sourceMappedTableMergeCell, /<a:hlinkClick\b[^>]*\br:id="rId998"/);

const tableReplaceAllEdited = structuredClone(tablePreservationBaseline);
tableReplaceAllEdited[0].shapes[0].tableRows[0][0].text = "Replacement";
const tableReplaceAllFile = await preservePresentationFile(
  tablePreservationInput,
  tablePreservationBaseline,
  tableReplaceAllEdited,
  "table-replace-all.pptx",
);
const tableReplaceAllZip = await JSZip.loadAsync(await tableReplaceAllFile.arrayBuffer());
const tableReplaceAllXml = await tableReplaceAllZip.file("ppt/slides/slide1.xml").async("text");
const tableReplaceAllCell = tableReplaceAllXml.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(tableReplaceAllCell, "replaced table text should retain its source cell");
assert.equal(
  Array.from(tableReplaceAllCell.matchAll(/<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g), (match) => match[1]).join(""),
  "Replacement",
);
assert.equal(
  (tableReplaceAllCell.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || []).length,
  1,
  "replacing all table-cell text should collapse obsolete source paragraphs",
);
assert.doesNotMatch(tableReplaceAllCell, /<a:fld\b|<a:hlinkClick\b/);

const duplicateTableZip = await JSZip.loadAsync(await blob.arrayBuffer());
const duplicateTableSlideXml = await duplicateTableZip.file("ppt/slides/slide1.xml").async("text");
const duplicateTableFrame = (duplicateTableSlideXml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [])
  .find((frame) => frame.includes("Metric"));
const duplicateTableFirstCell = duplicateTableFrame?.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(duplicateTableFrame && duplicateTableFirstCell);
const duplicateTableParagraph = '<a:p><a:r><a:rPr lang="en-US"><a:solidFill><a:srgbClr val="0000FF"/></a:solidFill></a:rPr><a:t>1</a:t></a:r><a:fld id="{00000000-0000-0000-0000-000000000006}" type="slidenum"><a:rPr lang="en-US"><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></a:rPr><a:t>1</a:t></a:fld><a:endParaRPr lang="en-US"/></a:p>';
const duplicateTableCell = duplicateTableFirstCell.replace(
  /<a:txBody\b[^>]*>[\s\S]*?<\/a:txBody>/,
  `<a:txBody><a:bodyPr/><a:lstStyle/>${duplicateTableParagraph}</a:txBody>`,
);
duplicateTableZip.file(
  "ppt/slides/slide1.xml",
  duplicateTableSlideXml.replace(duplicateTableFrame, duplicateTableFrame.replace(duplicateTableFirstCell, duplicateTableCell)),
);
const duplicateTableInput = await duplicateTableZip.generateAsync({ type: "arraybuffer" });
const duplicateTableBaseline = structuredClone(tablePreservationBaseline);
duplicateTableBaseline[0].shapes[0].tableRows[0][0].text = "11";
const duplicateTableEdited = structuredClone(duplicateTableBaseline);
duplicateTableEdited[0].shapes[0].tableRows[0][0] = {
  ...duplicateTableEdited[0].shapes[0].tableRows[0][0],
  text: "1",
  sourceMap: [1],
};
const duplicateTableFile = await preservePresentationFile(
  duplicateTableInput,
  duplicateTableBaseline,
  duplicateTableEdited,
  "duplicate-table-field.pptx",
);
const duplicateTableSavedZip = await JSZip.loadAsync(await duplicateTableFile.arrayBuffer());
const duplicateTableSavedXml = await duplicateTableSavedZip.file("ppt/slides/slide1.xml").async("text");
const duplicateTableSavedCell = duplicateTableSavedXml.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/)?.[0];
assert.ok(duplicateTableSavedCell);
assert.match(duplicateTableSavedCell, /<a:fld\b[^>]*\btype="slidenum"/);
assert.doesNotMatch(
  duplicateTableSavedCell,
  /<a:r[\s>]/,
  "table edits must delete the source-mapped ordinary duplicate and retain the field",
);

const visualRoundTripEdited = structuredClone(generatedBaseline);
visualRoundTripEdited[0].shapes[0].fill = "rgba(219, 234, 254, 0.5)";
visualRoundTripEdited[0].shapes[0].presetGeom = "roundRect";
visualRoundTripEdited[0].shapes[0].borderRadius = 18;
visualRoundTripEdited[0].shapes[0].texts = [{
  text: "First line\nSecond line",
  color: "rgba(29, 78, 216, 0.4)",
  runs: [{
    text: "First line\nSecond line",
    strikethrough: true,
    baseline: 2,
    spacing: 1.5,
    color: "rgba(29, 78, 216, 0.4)",
  }],
}];
const visualRoundTripFile = await preservePresentationFile(
  await blob.arrayBuffer(),
  generatedBaseline,
  visualRoundTripEdited,
  "visual-round-trip.pptx",
);
const visualRoundTripZip = await JSZip.loadAsync(await visualRoundTripFile.arrayBuffer());
const visualRoundTripXml = await visualRoundTripZip.file("ppt/slides/slide1.xml").async("text");
assert.match(visualRoundTripXml, /<a:srgbClr val="DBEAFE"><a:alpha val="50000"\/>/);
assert.match(visualRoundTripXml, /<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 18000"\/><\/a:avLst>/);
assert.match(visualRoundTripXml, /<a:br\/>/);
assert.match(visualRoundTripXml, /strike="sngStrike"/);
assert.match(visualRoundTripXml, /baseline="2000"/);
assert.match(visualRoundTripXml, /spc="150"/);
assert.match(visualRoundTripXml, /<a:srgbClr val="1D4ED8"><a:alpha val="40000"\/>/);

const structurallyEditedExisting = structuredClone(generatedBaseline[0]);
structurallyEditedExisting.bg = "#fef3c7";
structurallyEditedExisting.shapes[0].fill = "#dbeafe";
structurallyEditedExisting.shapes[0].stroke = "#2563eb";
structurallyEditedExisting.shapes[0].strokeWidth = 2;
structurallyEditedExisting.shapes[0].texts = [
  { text: "Styled title", fontSize: 30, bold: true, italic: true, color: "#1d4ed8", align: "center" },
  { text: "Second editable paragraph", fontSize: 16, color: "#334155" },
];
structurallyEditedExisting.shapes.push({
  id: "new-shape",
  type: "shape",
  x: 12,
  y: 72,
  w: 32,
  h: 12,
  fill: "#dcfce7",
  stroke: "#16a34a",
  strokeWidth: 1,
  presetGeom: "roundRect",
  texts: [{ text: "New editable shape", bold: true, color: "#166534", align: "center", indent: 18, indentRight: 36, hanging: 9, spaceBefore: 0, spaceAfter: 0 }],
});
const newSlide = {
  id: "new-slide",
  bg: "#fff7ed",
  aspectRatio: "16/9",
  notes: "Notes on an inserted slide",
  shapes: [{
    id: "new-slide-title",
    type: "shape",
    x: 10,
    y: 16,
    w: 80,
    h: 18,
    fill: "#ffedd5",
    presetGeom: "rect",
    texts: [{ text: "Inserted slide", fontSize: 32, bold: true, color: "#9a3412", align: "center" }],
  }],
};
const structuralFile = await preservePresentationFile(
  await blob.arrayBuffer(),
  generatedBaseline,
  [newSlide, structurallyEditedExisting],
  "structural-edit.pptx",
);
if (process.env.PRESENTATION_STRUCTURAL_TEST_OUTPUT) {
  await writeFile(process.env.PRESENTATION_STRUCTURAL_TEST_OUTPUT, Buffer.from(await structuralFile.arrayBuffer()));
}
const structuralZip = await JSZip.loadAsync(await structuralFile.arrayBuffer());
const structuralPresentationXml = await structuralZip.file("ppt/presentation.xml").async("text");
assert.equal((structuralPresentationXml.match(/<p:sldId\b/g) || []).length, 2, "slide insertion and reordering should update the slide list");
assert.match(await structuralZip.file("ppt/slides/slide2.xml").async("text"), /Inserted slide/);

const acceptedLiveInsertState = JSON.parse(buildPresentationLiveEditContent(generatedBaseline, {
  documentName: "ai-live-insert.pptx",
  activeSlideIndex: 0,
  selectedShapeId: null,
}));
acceptedLiveInsertState.slides.push({
  id: "ai-slide-value",
  slideNumber: 2,
  active: false,
  create: {
    layout: "title-body",
    title: "Value and rollout",
    body: "Automate repeatable work\nKeep context in one place\nStart with a two-week pilot",
  },
});
const acceptedLiveInsertSlides = applyPresentationLiveEditContent(
  generatedBaseline,
  JSON.stringify(acceptedLiveInsertState),
);
assert.ok(acceptedLiveInsertSlides, "the streamed slide factory output should be accepted for save");
const acceptedLiveInsertFile = await preservePresentationFile(
  await blob.arrayBuffer(),
  generatedBaseline,
  acceptedLiveInsertSlides,
  "ai-live-insert.pptx",
);
const acceptedLiveInsertZip = await JSZip.loadAsync(await acceptedLiveInsertFile.arrayBuffer());
const acceptedLiveInsertPresentationXml = await acceptedLiveInsertZip.file("ppt/presentation.xml").async("text");
assert.equal(
  (acceptedLiveInsertPresentationXml.match(/<p:sldId\b/g) || []).length,
  2,
  "Accept should persist an AI-created slide in the PPTX package",
);
assert.match(
  await acceptedLiveInsertZip.file("ppt/slides/slide2.xml").async("text"),
  /Value and rollout/,
);
const structuralOriginalSlideXml = await structuralZip.file("ppt/slides/slide1.xml").async("text");
assert.match(structuralOriginalSlideXml, /Styled title/);
assert.match(structuralOriginalSlideXml, /Second editable paragraph/);
assert.match(structuralOriginalSlideXml, /New editable shape/);
assert.match(structuralOriginalSlideXml, /marL="228600" marR="457200" indent="114300"/);
assert.match(structuralOriginalSlideXml, /<a:spcBef><a:spcPts val="0"\/><\/a:spcBef>/);
assert.match(structuralOriginalSlideXml, /<a:solidFill><a:srgbClr val="DBEAFE"/);
assert.ok(Object.keys(structuralZip.files).some((name) => /^ppt\/notesSlides\/notesSlide\d+\.xml$/.test(name) && name !== "ppt/notesSlides/notesSlide1.xml"), "inserted slides should get their own notes part");
const insertedSlideRelationships = await structuralZip.file("ppt/slides/_rels/slide2.xml.rels").async("text");
const insertedSlideNotesTarget = (insertedSlideRelationships.match(/<Relationship\b[^>]*\/>/g) || [])
  .find((relationship) => /relationships\/notesSlide"/i.test(relationship))
  ?.match(/\bTarget="([^"]+)"/i)?.[1];
assert.ok(insertedSlideNotesTarget, "inserted slide should expose its notes relationship");
const insertedSlideNotesPart = resolvePresentationPartTarget("ppt/slides/slide2.xml", insertedSlideNotesTarget);

const repeatedSaveBaseline = [{ id: "repeat-slide", sourcePart: "ppt/slides/slide1.xml", shapes: [] }];
const repeatedSaveEdited = structuredClone(repeatedSaveBaseline);
repeatedSaveEdited[0].shapes.push({
  id: "repeat-shape",
  type: "shape",
  x: 10,
  y: 10,
  w: 40,
  h: 12,
  texts: [{ text: "Added once" }],
});
const repeatedFirst = await preservePresentationFileWithSnapshot(
  await presentationWithoutNotes.arrayBuffer(),
  repeatedSaveBaseline,
  repeatedSaveEdited,
  "repeated-save.pptx",
);
const repeatedSecondEdited = structuredClone(repeatedFirst.slides);
repeatedSecondEdited[0].shapes[0].texts[0].text = "Edited later";
const repeatedSecond = await preservePresentationFileWithSnapshot(
  await repeatedFirst.file.arrayBuffer(),
  repeatedFirst.slides,
  repeatedSecondEdited,
  "repeated-save.pptx",
);
const repeatedZip = await JSZip.loadAsync(await repeatedSecond.file.arrayBuffer());
const repeatedSlideXml = await repeatedZip.file("ppt/slides/slide1.xml").async("text");
assert.doesNotMatch(repeatedSlideXml, /Added once/, "a later save should replace the previously inserted object");
assert.equal((repeatedSlideXml.match(/Edited later/g) || []).length, 1, "a later save should keep exactly one copy of an inserted object");

const repeatedImageLiveSlides = structuredClone(repeatedSaveBaseline);
repeatedImageLiveSlides[0].shapes.push({
  id: "repeat-image",
  type: "image",
  x: 10,
  y: 10,
  w: 24,
  h: 24,
  texts: [],
  imgUrl: pixel,
  hyperlink: "https://example.com/image",
});
const repeatedImageFirst = await preservePresentationFileWithSnapshot(
  await presentationWithoutNotes.arrayBuffer(),
  repeatedSaveBaseline,
  repeatedImageLiveSlides,
  "repeated-image-save.pptx",
);
const repeatedImageSecondLiveSlides = structuredClone(repeatedImageLiveSlides);
repeatedImageSecondLiveSlides[0].shapes[0].x = 12;
const repeatedImageSecond = await preservePresentationFileWithSnapshot(
  await repeatedImageFirst.file.arrayBuffer(),
  repeatedImageFirst.slides,
  repeatedImageSecondLiveSlides,
  "repeated-image-save.pptx",
);
const repeatedImageFirstZip = await JSZip.loadAsync(await repeatedImageFirst.file.arrayBuffer());
const repeatedImageSecondZip = await JSZip.loadAsync(await repeatedImageSecond.file.arrayBuffer());
const packageFileCount = (packageZip, prefix) => Object.keys(packageZip.files)
  .filter((name) => name.startsWith(prefix) && !packageZip.files[name].dir).length;
const repeatedImageFirstRelationships = await repeatedImageFirstZip.file("ppt/slides/_rels/slide1.xml.rels").async("text");
const repeatedImageSecondRelationships = await repeatedImageSecondZip.file("ppt/slides/_rels/slide1.xml.rels").async("text");
assert.equal(
  packageFileCount(repeatedImageSecondZip, "ppt/media/"),
  packageFileCount(repeatedImageFirstZip, "ppt/media/"),
  "a later save of a newly inserted image must reuse its persisted media identity",
);
assert.equal(
  (repeatedImageSecondRelationships.match(/relationships\/image/g) || []).length,
  (repeatedImageFirstRelationships.match(/relationships\/image/g) || []).length,
  "a later save must not append a duplicate image relationship",
);
assert.equal(
  (repeatedImageSecondRelationships.match(/relationships\/hyperlink/g) || []).length,
  (repeatedImageFirstRelationships.match(/relationships\/hyperlink/g) || []).length,
  "a later save must not append a duplicate hyperlink relationship",
);

const deletionInputZip = await JSZip.loadAsync(await structuralFile.arrayBuffer());
const deletionSlideRelationshipsPart = "ppt/slides/_rels/slide2.xml.rels";
const deletionSlideRelationships = await deletionInputZip.file(deletionSlideRelationshipsPart).async("text");
deletionInputZip.file(
  deletionSlideRelationshipsPart,
  deletionSlideRelationships.replace(
    /<\/Relationships>\s*$/,
    '<Relationship Id="rId999" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart99.xml"/></Relationships>',
  ),
);
deletionInputZip.file(
  "ppt/charts/chart99.xml",
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:autoTitleDeleted val="1"/></c:chart></c:chartSpace>',
);
deletionInputZip.file(
  "ppt/charts/_rels/chart99.xml.rels",
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/package" Target="../embeddings/deleted-chart-data.xlsx"/></Relationships>',
);
deletionInputZip.file("ppt/embeddings/deleted-chart-data.xlsx", new Uint8Array([80, 75, 3, 4]));
let deletionContentTypes = await deletionInputZip.file("[Content_Types].xml").async("text");
deletionContentTypes = deletionContentTypes.replace(
  /<\/Types>\s*$/,
  '<Override PartName="/ppt/charts/chart99.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/><Override PartName="/ppt/embeddings/deleted-chart-data.xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/></Types>',
);
deletionInputZip.file("[Content_Types].xml", deletionContentTypes);
const deletionInput = await deletionInputZip.generateAsync({ type: "arraybuffer" });

const deletedSlideFile = await preservePresentationFile(
  deletionInput,
  [
    { id: "new-slide", sourcePart: "ppt/slides/slide2.xml", shapes: [] },
    { id: "slide-1", sourcePart: "ppt/slides/slide1.xml", shapes: [] },
  ],
  [{ id: "slide-1", sourcePart: "ppt/slides/slide1.xml", shapes: [] }],
  "deleted-slide.pptx",
);
const deletedSlideZip = await JSZip.loadAsync(await deletedSlideFile.arrayBuffer());
assert.equal(((await deletedSlideZip.file("ppt/presentation.xml").async("text")).match(/<p:sldId\b/g) || []).length, 1, "slide deletion should update the presentation order without rebuilding the package");
assert.equal(deletedSlideZip.file("ppt/slides/slide2.xml"), null, "deleted slide content should be removed from the package");
assert.doesNotMatch(
  await deletedSlideZip.file("ppt/_rels/presentation.xml.rels").async("text"),
  /Target="slides\/slide2\.xml"/,
  "deleted slide relationships should be removed from the presentation",
);
assert.equal(deletedSlideZip.file(insertedSlideNotesPart), null, "deleting a slide should remove its speaker-notes content");
assert.equal(deletedSlideZip.file("ppt/charts/chart99.xml"), null, "deleting a slide should remove its orphaned chart part");
assert.equal(
  deletedSlideZip.file("ppt/embeddings/deleted-chart-data.xlsx"),
  null,
  "deleting a slide should remove the chart's orphaned embedded workbook",
);

const deletedShapeSlides = structuredClone(generatedBaseline);
deletedShapeSlides[0].shapes = [];
const deletedShapeFile = await preservePresentationFile(await blob.arrayBuffer(), generatedBaseline, deletedShapeSlides, "deleted-shape.pptx");
const deletedShapeZip = await JSZip.loadAsync(await deletedShapeFile.arrayBuffer());
assert.doesNotMatch(await deletedShapeZip.file("ppt/slides/slide1.xml").async("text"), /Editable title/);

const generatedPictureXml = slideXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/)?.[0];
const generatedPictureId = generatedPictureXml?.match(/<p:cNvPr\b[^>]*id="([^"]+)"/)?.[1];
const generatedPictureRelationshipId = generatedPictureXml?.match(/<a:blip\b[^>]*r:embed="([^"]+)"/)?.[1];
const generatedSlideRelationshipsXml = await zip.file("ppt/slides/_rels/slide1.xml.rels").async("text");
const generatedPictureTarget = (generatedSlideRelationshipsXml.match(/<Relationship\b[^>]*\/>/g) || [])
  .find((relationship) => relationship.includes(`Id="${generatedPictureRelationshipId}"`))
  ?.match(/Target="([^"]+)"/)?.[1];
assert.ok(generatedPictureId && generatedPictureRelationshipId && generatedPictureTarget, "generated image should expose durable package identity");
const generatedPictureMediaPart = resolvePresentationPartTarget("ppt/slides/slide1.xml", generatedPictureTarget);
const deletedPictureFile = await preservePresentationFile(
  await blob.arrayBuffer(),
  [{
    id: "slide-1",
    sourcePart: "ppt/slides/slide1.xml",
    shapes: [{
      id: "image",
      type: "image",
      x: 8,
      y: 32,
      w: 36,
      h: 48,
      imgUrl: pixel,
      texts: [],
      source: {
        part: "ppt/slides/slide1.xml",
        kind: "pic",
        objectId: generatedPictureId,
        editable: true,
        mediaPart: generatedPictureMediaPart,
      },
    }],
  }],
  [{ id: "slide-1", sourcePart: "ppt/slides/slide1.xml", shapes: [] }],
  "deleted-picture.pptx",
);
const deletedPictureZip = await JSZip.loadAsync(await deletedPictureFile.arrayBuffer());
assert.doesNotMatch(
  await deletedPictureZip.file("ppt/slides/_rels/slide1.xml.rels").async("text"),
  new RegExp(`\\bId="${generatedPictureRelationshipId}"`),
  "deleting a picture should remove its slide relationship",
);
assert.equal(
  deletedPictureZip.file(generatedPictureMediaPart),
  null,
  "deleting the last reference to a picture should remove its media bytes",
);

console.log("presentation editor export smoke test passed");
