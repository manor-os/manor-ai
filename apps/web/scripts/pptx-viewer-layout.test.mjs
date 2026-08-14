import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const fileViewerSource = await readFile(
  new URL("../src/pages/FileViewer.tsx", import.meta.url),
  "utf8",
);
const styles = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const pptxViewerSource = fileViewerSource.slice(
  fileViewerSource.indexOf("function PptxViewer("),
  fileViewerSource.indexOf("// ── Details drawer helper"),
);
const audioViewerSource = fileViewerSource.slice(
  fileViewerSource.indexOf("function AudioViewer("),
  fileViewerSource.indexOf("// ── Details drawer helper"),
);

test("PPTX viewer fits slides and keeps thumbnails in one scrollable row", () => {
  assert.match(pptxViewerSource, /className="pptx-document-stage"/);
  assert.match(pptxViewerSource, /className="pptx-document-thumbnail-track" role="tablist"/);
  assert.match(pptxViewerSource, /scrollIntoView\(\{ block: "nearest", inline: "nearest" \}\)/);
  assert.doesNotMatch(pptxViewerSource, /flexWrap:\s*"wrap"/);

  assert.match(styles, /\.pptx-document-slide-image\s*\{[\s\S]*?max-height:\s*100%/);
  assert.match(styles, /\.pptx-document-thumbnail-strip\s*\{[\s\S]*?overflow-x:\s*auto/);
  assert.match(styles, /\.pptx-document-thumbnail-track\s*\{[\s\S]*?width:\s*max-content/);
});

test("PPTX viewer keeps rendered fidelity while exposing native video playback", () => {
  assert.match(pptxViewerSource, /Promise\.allSettled\(\[serverSlidesTask, parsedSlidesTask\]\)/);
  assert.match(fileViewerSource, /presentationVideoSource\(spXml, relsMap\)/);
  assert.match(pptxViewerSource, /className="pptx-native-video"/);
  assert.match(pptxViewerSource, /controls\s+playsInline\s+preload="metadata"/);
  assert.match(pptxViewerSource, /Math\.min\(1200, stageSize\.width \|\| 1200, \(stageSize\.height \|\| 675\) \* aspect\)/);
  assert.match(styles, /\.pptx-document-rendered-slide\s*\{[\s\S]*?max-width:\s*100%/);
  assert.match(styles, /\.pptx-native-video\s*\{[\s\S]*?position:\s*absolute/);
});

test("mobile app shell reserves the remaining height for viewer content", () => {
  assert.match(styles, /@media \(max-width:\s*768px\)\s*\{[\s\S]*?\.app-main-shell\s*\{[\s\S]*?display:\s*flex/);
  assert.match(styles, /@media \(max-width:\s*768px\)\s*\{[\s\S]*?\.app-content-panel\s*\{[\s\S]*?flex:\s*1 1 0/);
});

test("audio viewer centers the player and renders the live audio frequency spectrum", () => {
  assert.match(audioViewerSource, /className="manor-editor-audio-waveform"/);
  assert.match(audioViewerSource, /createAnalyser\(\)/);
  assert.match(audioViewerSource, /getByteFrequencyData\(graph\.frequencyData\)/);
  assert.match(audioViewerSource, /requestAnimationFrame\(drawFrequencyFrame\)/);
  assert.match(audioViewerSource, /onPlay=\{\(\) => \{ void startAudioAnalysis\(\); \}\}/);
  assert.match(audioViewerSource, /onTimeUpdate=\{syncAudioTime\}/);
  assert.doesNotMatch(audioViewerSource, /AUDIO_WAVEFORM_HEIGHTS|animationDelay/);
  assert.match(styles, /\.manor-editor-audio-stage\s*\{[\s\S]*?flex:\s*1/);
  assert.match(styles, /\.manor-editor-audio-waveform\s*\{[\s\S]*?align-items:\s*center/);
  assert.doesNotMatch(styles, /@keyframes manor-audio-wave-pulse/);
  assert.match(styles, /@media \(prefers-reduced-motion:\s*reduce\)\s*\{[\s\S]*?\.manor-editor-audio-waveform > span\s*\{[\s\S]*?transition:\s*none/);
});
