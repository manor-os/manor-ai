import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);

test("vendored image-size rejects a zero-length ICNS entry without looping", () => {
  const modulePath = require.resolve("image-size/dist/index.js");
  const program = `
    const { imageSize } = require(${JSON.stringify(modulePath)});
    const input = Buffer.alloc(16);
    input.write("icns", 0, "ascii");
    input.writeUInt32BE(16, 4);
    input.write("ic07", 8, "ascii");
    input.writeUInt32BE(0, 12);
    imageSize(input);
  `;
  const result = spawnSync(process.execPath, ["-e", program], {
    encoding: "utf8",
    timeout: 1_000,
  });

  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.signal, null);
  assert.equal(result.status, 0, result.stderr);
});
