import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  commentActionLabel,
  layoutCommentRanges,
  markdownCommentMarkerPlugin,
} from "../src/lib/markdownCommentMarkers.mjs";

const viewerSource = readFileSync(new URL("../src/pages/FileViewer.tsx", import.meta.url), "utf8");
const commentThreadSource = readFileSync(new URL("../src/components/CommentThread.tsx", import.meta.url), "utf8");
const markerSource = readFileSync(new URL("../src/lib/markdownCommentMarkers.mjs", import.meta.url), "utf8");
const tailwindSource = readFileSync(new URL("../tailwind.config.ts", import.meta.url), "utf8");
const uiSource = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

test("file comment anchors use the selected source occurrence and reject stale ranges", () => {
  assert.match(viewerSource, /viewerSourceSelectionOffsets\([\s\S]*?content\.slice\(start, end\) === range\.toString\(\)/);
  assert.match(viewerSource, /function viewerSelectionQuoteOccurrence\(/);
  assert.match(viewerSource, /closest<HTMLElement>\("\[data-comment-content-surface\]"\)/);
  assert.match(viewerSource, /data-comment-selection-ignore="true"/);
  assert.match(viewerSource, /start !== content\.lastIndexOf\(selectedText\)/);
  assert.match(viewerSource, /quote_occurrence: quoteOccurrence \?\? undefined/);
  assert.match(viewerSource, /contentRangeMatchesCommentQuote\(content, start, end, quote\)/);
  assert.match(viewerSource, /matchStart !== searchText\.lastIndexOf\(quoteKey\)/);
  assert.match(viewerSource, /firstMatch === searchableText\.lastIndexOf\(key\)/);
  assert.match(markerSource, /function markdownCommentMarkerPlugin\(/);
  assert.match(tailwindSource, /\.\/src\/\*\*\/\*\.\{ts,tsx,mjs\}/);
  assert.match(uiSource, /button\.document-comment-anchor-action\s*\{[\s\S]*?width: 24px;[\s\S]*?height: 24px;/);
  assert.match(viewerSource, /remarkPlugins=\{\[remarkGfm, remarkBreaks\]\}/);
  assert.match(viewerSource, /rehypePlugins=\{\[commentMarkerPlugin\]\}/);
  assert.match(viewerSource, /commentAnchors=\{viewerAnchoredComments\}/);
  assert.match(viewerSource, /if \(mark\?\.closest\("a"\)\) return false/);
  assert.match(viewerSource, /<button\s*\n\s*type="button"\s*\n\s*key=\{`\$\{range\.id\}-/);
  assert.match(viewerSource, /aria-label=\{range\.accessibleLabel\}/);
  assert.match(viewerSource, /aria-pressed=\{activeCommentId === range\.id\}/);
  assert.match(viewerSource, /data-comment-source-mode="text"/);
  assert.match(viewerSource, /data-comment-source-mode="code"/);
  assert.match(viewerSource, /data-comment-source-mode="json"/);
  assert.match(viewerSource, /preferSelectablePreview=\{commentsOpen\}/);
  assert.match(viewerSource, /docId && !preferSelectablePreview && !useClientFallback/);
  assert.match(
    viewerSource,
    /updateCommentMarkActiveState\(root, activeCommentId\);[\s\S]{0,500}document-comment-mark[\s\S]{0,250}scrollIntoView/,
  );
});

function markdownTree() {
  return {
    type: "root",
    children: [
      {
        type: "element",
        tagName: "p",
        children: [
          {
            type: "element",
            tagName: "code",
            children: [{ type: "text", value: "repeat" }],
          },
          { type: "text", value: " repeat" },
        ],
      },
      { type: "text", value: "\n" },
      {
        type: "element",
        tagName: "pre",
        children: [
          {
            type: "element",
            tagName: "code",
            children: [{ type: "text", value: "fenced" }],
          },
        ],
      },
      { type: "text", value: "\n" },
      {
        type: "element",
        tagName: "p",
        children: [
          {
            type: "element",
            tagName: "a",
            properties: { href: "https://example.com" },
            children: [{ type: "text", value: "linked" }],
          },
        ],
      },
    ],
  };
}

function markFor(tree, commentId) {
  let found = null;
  const visit = (node) => {
    if (node.properties?.["data-comment-id"] === commentId) found = node;
    node.children?.forEach(visit);
  };
  visit(tree);
  return found;
}

function elementsFor(tree, predicate) {
  const found = [];
  const visit = (node) => {
    if (node.type === "element" && predicate(node)) found.push(node);
    node.children?.forEach(visit);
  };
  visit(tree);
  return found;
}

function applyMarker(tree, id, quote, quoteOccurrence = 0) {
  markdownCommentMarkerPlugin([
    {
      id,
      anchor: { mode: "markdown", quote, quote_occurrence: quoteOccurrence },
    },
  ], [], null)()(tree);
  return markFor(tree, id);
}

test("Markdown comment occurrence mapping includes inline and fenced code", () => {
  const inlineTree = markdownTree();
  const inlineMark = applyMarker(inlineTree, "inline", "repeat", 0);
  assert.equal(inlineMark?.tagName, "button");
  assert.equal(inlineMark?.properties?.type, "button");
  assert.equal(inlineMark?.children?.[0]?.value, "repeat");
  assert.equal(inlineTree.children[0].children[0].tagName, "code");

  const proseTree = markdownTree();
  const proseMark = applyMarker(proseTree, "prose", "repeat", 1);
  assert.equal(proseMark?.children?.[0]?.value, "repeat");

  const fencedTree = markdownTree();
  const fencedMark = applyMarker(fencedTree, "fenced-comment", "fenced", 0);
  assert.equal(fencedMark?.children?.[0]?.value, "fenced");
  assert.equal(fencedTree.children[2].children[0].tagName, "code");
});

test("Markdown link comment marks avoid nested interactive semantics", () => {
  const tree = markdownTree();
  applyMarker(tree, "link-comment", "linked", 0);
  const marks = elementsFor(
    tree,
    (node) => node.properties?.["data-comment-id"] === "link-comment",
  );
  const mark = marks.find((node) => node.tagName === "span");
  const action = marks.find((node) => node.tagName === "button");
  assert.equal(mark?.tagName, "span");
  assert.equal(mark?.properties?.role, undefined);
  assert.equal(mark?.properties?.tabIndex, undefined);
  assert.equal(action?.properties?.type, "button");
  assert.equal(action?.properties?.["aria-label"], "Comments 1: linked");
  assert.equal(action?.properties?.["aria-pressed"], false);
  assert.equal(tree.children[4].children[0].tagName, "a");
  assert.equal(tree.children[4].children[0].properties.href, "https://example.com");
  assert.equal(tree.children[4].children[1], action);
});

test("one Markdown comment spanning formatting creates one focus target", () => {
  const tree = {
    type: "root",
    children: [{
      type: "element",
      tagName: "p",
      children: [
        { type: "text", value: "one " },
        {
          type: "element",
          tagName: "em",
          children: [{ type: "text", value: "two" }],
        },
        { type: "text", value: " three" },
      ],
    }],
  };

  applyMarker(tree, "formatted-comment", "one two three", 0);
  const marks = elementsFor(
    tree,
    (node) => node.properties?.["data-comment-id"] === "formatted-comment",
  );
  assert.equal(marks.length, 3);
  assert.equal(marks.filter((node) => node.tagName === "button").length, 1);
  assert.equal(marks.filter((node) => node.tagName === "span").length, 2);
});

test("every comment in one Markdown link has its own keyboard action", () => {
  const tree = {
    type: "root",
    children: [{
      type: "element",
      tagName: "p",
      children: [{
        type: "element",
        tagName: "a",
        properties: { href: "https://example.com" },
        children: [{ type: "text", value: "alpha beta" }],
      }],
    }],
  };
  markdownCommentMarkerPlugin([
    { id: "first", anchor: { mode: "markdown", quote: "alpha", quote_occurrence: 0 } },
    { id: "second", anchor: { mode: "markdown", quote: "beta", quote_occurrence: 0 } },
  ], [], null, "Comments")()(tree);

  const actions = elementsFor(
    tree,
    (node) => node.tagName === "button" && node.properties?.["data-comment-id"],
  );
  assert.deepEqual(
    actions.map((node) => node.properties["data-comment-id"]),
    ["first", "second"],
  );
});

test("overlapping comment ranges preserve an action for every comment", () => {
  const ranges = layoutCommentRanges([
    { id: "first", start: 0, end: 13, quote: "same sentence" },
    { id: "second", start: 0, end: 13, quote: "same sentence" },
    { id: "third", start: 5, end: 17, quote: "sentence end" },
  ]);

  assert.deepEqual(ranges.map((range) => range.id), ["first", "second", "third"]);
  assert.equal(ranges[0].actionOffset, undefined);
  assert.equal(ranges[1].actionOffset, 0);
  assert.equal(ranges[2].actionOffset, 5);

  const tree = {
    type: "root",
    children: [{
      type: "element",
      tagName: "p",
      children: [{ type: "text", value: "same sentence end" }],
    }],
  };
  markdownCommentMarkerPlugin([
    {
      id: "first",
      display_name: "Alice",
      content: "First review note",
      anchor: { mode: "markdown", quote: "same sentence", quote_occurrence: 0 },
    },
    {
      id: "second",
      display_name: "Bob",
      content: "Second review note",
      anchor: { mode: "markdown", quote: "same sentence", quote_occurrence: 0 },
    },
    {
      id: "third",
      display_name: "Carol",
      content: "Third review note",
      anchor: { mode: "markdown", quote: "sentence end", quote_occurrence: 0 },
    },
  ], [], "third", "Comments")()(tree);

  const actions = elementsFor(
    tree,
    (node) => node.tagName === "button" && node.properties?.["data-comment-id"],
  );
  assert.deepEqual(
    actions.map((node) => node.properties["data-comment-id"]),
    ["first", "second", "third"],
  );
  assert.equal(new Set(actions.map((node) => node.properties["aria-label"])).size, 3);
  assert.deepEqual(
    actions.map((node) => node.properties["aria-pressed"]),
    [false, false, true],
  );
  assert.equal(
    markFor(tree, "second")?.properties?.["aria-label"],
    "Comments 2: Bob — Second review note",
  );
  assert.equal(markFor(tree, "third")?.properties?.className.includes("is-active"), true);
});

test("comment action labels remain unique when author and content match", () => {
  const comment = { display_name: "Alex", content: "Please revise" };
  assert.equal(commentActionLabel(comment, "quote", "Comments", 1), "Comments 1: Alex — Please revise");
  assert.equal(commentActionLabel(comment, "quote", "Comments", 2), "Comments 2: Alex — Please revise");
  assert.equal(
    commentActionLabel({ ...comment, content: " " }, "quote", "Comments", 3),
    "Comments 3: Alex — quote",
  );
});

test("Word preview uses one selectable native-layout comment surface", () => {
  assert.match(viewerSource, /manor-docx-native manor-docx-readonly/);
  assert.match(viewerSource, /markQuoteCommentsInElement\(root, visibleCommentAnchors\)/);
  assert.match(
    viewerSource,
    /markQuoteCommentsInElement\(root, visibleCommentAnchors\);[\s\S]*?\}, \[html, legacyHtml, render, visibleCommentAnchors\]\);/,
  );
  assert.match(viewerSource, /updateCommentMarkActiveState\(root, activeCommentId\)/);
  assert.match(viewerSource, /ref=\{previewRef\}/);
  assert.doesNotMatch(viewerSource, /import\("mammoth"\)/);
  assert.doesNotMatch(viewerSource, /className="docx-semantic-layer"/);
  assert.match(viewerSource, /const commentAnchorBar = visibleCommentAnchors\.length > 0/);
  assert.match(viewerSource, /comment\.status === "deleted" \? t\("component\.comment_thread\.deleted"\) : comment\.content/);
  assert.match(viewerSource, /action\.setAttribute\("aria-label", segment\.accessibleLabel\)/);
  assert.match(viewerSource, /action\.setAttribute\("aria-pressed", String\(segment\.active\)\)/);
  assert.match(viewerSource, /if \(legacyHtml\)[\s\S]*?\{commentAnchorBar\}[\s\S]*?ref=\{legacyPreviewRef\}/);
});

test("comment thread exposes update, delete confirmation, and reactions", () => {
  assert.match(commentThreadSource, /api\.comments\.update\(id, content\)/);
  assert.match(commentThreadSource, /api\.comments\.delete\(id\)/);
  assert.match(commentThreadSource, /api\.comments\.react\(id, "thumbsup"\)/);
  assert.match(commentThreadSource, /<ConfirmDialog[\s\S]*?closeOnConfirm=\{false\}/);
  assert.match(commentThreadSource, /const openDeleteDialog[\s\S]*?deleteComment\.reset\(\)[\s\S]*?setError\(""\)/);
  assert.match(commentThreadSource, /const closeDeleteDialog[\s\S]*?deleteComment\.reset\(\)[\s\S]*?setError\(""\)/);
  assert.match(commentThreadSource, /onRequestDelete: openDeleteDialog/);
  assert.match(commentThreadSource, /onClose=\{closeDeleteDialog\}/);
  assert.match(commentThreadSource, /aria-pressed=\{hasReacted\}/);
  assert.match(commentThreadSource, /const reactionLabel = reactionUsers\.length > 0[\s\S]*?reactionUsers\.length/);
  assert.match(commentThreadSource, /aria-label=\{reactionLabel\}[\s\S]*?comment-thread-reaction-count[\s\S]*?reactionUsers\.length/);
  assert.match(uiSource, /\.comment-thread-inline-action\s*\{[\s\S]*?min-width: 28px;[\s\S]*?height: 28px;/);
  assert.match(uiSource, /\.comment-thread-actions\s*\{[\s\S]*?flex-wrap: nowrap;/);
  assert.match(commentThreadSource, /if \(anchor\.quote\) return "";/);
  assert.match(commentThreadSource, /aria-label=\{t\("component\.comment_thread\.reply"\)\}[\s\S]*?<IconComment size=\{14\} \/>/);
  assert.match(commentThreadSource, /aria-label=\{t\("action\.more"\)\}[\s\S]*?aria-haspopup="menu"/);
  assert.match(commentThreadSource, /<ContextMenu[\s\S]*?label: t\("action\.edit"\), icon: <IconEdit/);
  assert.match(commentThreadSource, /label: t\("action\.delete"\), icon: <IconTrash/);
  assert.match(commentThreadSource, /isDeleted \? t\("component\.comment_thread\.deleted"\) : comment\.content/);
  assert.match(commentThreadSource, /!isEditing && !isDeleted/);
});

test("comment quotes suppress saved generic labels across locales without hiding location labels", () => {
  assert.match(commentThreadSource, /SUPPORTED_LOCALES\.map\([\s\S]*?tForLocale\("component.comment_thread.selected_text", code\)/);
  assert.match(commentThreadSource, /!legacySelectionLabels\.has\(anchor\.label\.trim\(\)\)/);
  assert.match(commentThreadSource, /<blockquote className=\{`comment-thread-quote/);
  assert.doesNotMatch(viewerSource, /label: t\("component.comment_thread.selected_text"\)/);
});
