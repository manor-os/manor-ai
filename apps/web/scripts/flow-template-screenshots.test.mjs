import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const embeddedChat = readFileSync(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const gallery = readFileSync(
  new URL("../src/components/workflows/FlowTemplateSamples.tsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../src/components/workflows/FlowTemplateSamples.css", import.meta.url),
  "utf8",
);

test("Flows mode renders the server-backed template gallery", () => {
  assert.match(
    embeddedChat,
    /import FlowTemplateGallery from "\.\/workflows\/FlowTemplateSamples";/,
  );
  assert.match(embeddedChat, /selected\.key === "flows"[\s\S]*?<FlowTemplateGallery \/>/);
  assert.match(gallery, /api\.workflows\.templates\(\)/);
  assert.match(gallery, /api\.workflows\.installTemplate\(template\.id\)/);
});

test("featured cards use distinct screenshots for real Flow templates", () => {
  for (const key of [
    "opc-generate-topic-from-knowledge-v1",
    "opc-write-article-from-topic-v1",
    "opc-create-image-from-topic-v1",
    "opc-create-video-from-topic-v1",
  ]) {
    assert.match(gallery, new RegExp(`"${key}"`));
  }

  assert.match(gallery, /function FlowScreenshot/);
  assert.match(gallery, /<FlowScreenshot template=\{template\} \/>/);
  assert.match(styles, /\.flow-template-screenshot/);
  assert.match(styles, /\.flow-template-preview-modal/);
});

test("cards and quick preview keep the concise Flow catalogue treatment", () => {
  assert.match(gallery, /flow-template-actions/);
  assert.match(gallery, /flow_templates\.quick_preview/);
  assert.match(gallery, /flow_templates\.remix/);
  assert.match(gallery, /flow_templates\.install_template/);
  assert.doesNotMatch(gallery, /template\.description/);
  assert.doesNotMatch(gallery, /template\.node_count/);
  assert.doesNotMatch(gallery, /template\.version/);
  assert.doesNotMatch(gallery, /workspace-flow-template-preview-details/);
});
