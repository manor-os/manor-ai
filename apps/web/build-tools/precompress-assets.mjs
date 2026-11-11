// Writes a .br file beside each compressible release file. nginx's brotli_static
// sends it to browsers that accept brotli, so the server spends no CPU on
// compression and the strongest level is affordable. Keep the extensions in step
// with gzip_types: a file here without a .br copy would be sent uncompressed.
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";
import { brotliCompress, constants } from "node:zlib";

const compress = promisify(brotliCompress);
const MIN_BYTES = 1024;
const WORKERS = 8;

async function releaseFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return releaseFiles(path);
    return /\.(?:js|mjs|css|wasm|svg)$/.test(entry.name) ? [path] : [];
  }));
  return nested.flat();
}

const root = new URL("../dist/assets/releases/", import.meta.url).pathname;
const queue = await releaseFiles(root);
let written = 0;
let before = 0;
let after = 0;

await Promise.all(Array.from({ length: WORKERS }, async () => {
  for (let file = queue.pop(); file; file = queue.pop()) {
    if ((await stat(file)).size < MIN_BYTES) continue;
    const source = await readFile(file);
    const compressed = await compress(source, {
      params: {
        [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY,
        [constants.BROTLI_PARAM_SIZE_HINT]: source.length,
      },
    });
    if (compressed.length >= source.length) continue;
    await writeFile(`${file}.br`, compressed);
    written += 1;
    before += source.length;
    after += compressed.length;
  }
}));

console.log(`Precompressed ${written} assets: ${Math.round(before / 1024)}K -> ${Math.round(after / 1024)}K`);
