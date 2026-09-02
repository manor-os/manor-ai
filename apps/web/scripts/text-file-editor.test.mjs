import assert from "node:assert/strict";
import { build } from "esbuild";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import os from "node:os";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../..");
const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "manor-text-editor-"));

try {
  const entry = path.join(temporaryDirectory, "entry.ts");
  await writeFile(entry, `export * from ${JSON.stringify(path.join(root, "apps/web/src/lib/textFilePreservation.ts"))};\nexport * from ${JSON.stringify(path.join(root, "apps/web/src/lib/delimitedText.ts"))};\n`);
  const output = path.join(temporaryDirectory, "bundle.mjs");
  await build({ entryPoints: [entry], outfile: output, bundle: true, platform: "node", format: "esm" });
  const codec = await import(`${pathToFileURL(output).href}?${Date.now()}`);

  const utf16 = Uint8Array.of(0xff, 0xfe, 0x41, 0x00, 0x0d, 0x00, 0x0a, 0x00, 0x42, 0x00);
  const decoded = codec.decodeTextFile(utf16);
  assert.equal(decoded.text, "A\r\nB");
  assert.equal(codec.textEncodingLabel(decoded.format), "UTF-16 LE BOM");
  assert.deepEqual(Array.from(codec.encodeTextFile("A changed\nB", decoded.text, decoded.format).slice(0, 2)), [0xff, 0xfe]);
  assert.equal(codec.decodeTextFile(codec.encodeTextFile("A changed\nB", decoded.text, decoded.format)).text, "A changed\r\nB");

  const invalidUtf8 = codec.decodeTextFile(Uint8Array.of(0xc3, 0x28));
  assert.equal(invalidUtf8.format.safeToSave, false);
  assert.equal(codec.textFileSaveStrategy(invalidUtf8.format), codec.TextFileSaveStrategy.NormalizeUtf8);
  const normalizedUtf8 = codec.encodeTextFile("editable", invalidUtf8.text, invalidUtf8.format);
  assert.equal(new TextDecoder().decode(normalizedUtf8), "editable");
  assert.equal(codec.decodeTextFile(normalizedUtf8).format.safeToSave, true);

  const csv = 'name;note\r\nAlice;"line 1\r\nline 2"\r\nBob;"said ""hi"""\r\n';
  const parsed = codec.parseDelimitedText(csv);
  assert.equal(parsed.format.delimiter, ";");
  assert.equal(parsed.format.lineEnding, "\r\n");
  assert.equal(parsed.format.finalLineEnding, true);
  assert.deepEqual(parsed.rows, [["name", "note"], ["Alice", "line 1\r\nline 2"], ["Bob", 'said "hi"']]);
  assert.equal(codec.serializeDelimitedText(parsed.rows, parsed.format), csv);
  console.log("text and delimited-file preservation test passed");
} finally {
  await rm(temporaryDirectory, { recursive: true, force: true });
}
