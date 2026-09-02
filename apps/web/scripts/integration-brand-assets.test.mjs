import assert from "node:assert/strict";
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { build } from "vite";

const root = new URL("../", import.meta.url);
const brands = new URL("src/lib/brands/", root);
const brandFiles = [...readFileSync(new URL("marks.ts", brands), "utf8")
  .matchAll(/from "\.\/assets\/([a-z-]+\.(?:svg|png|webp))\?no-inline"/g)]
  .map(match => match[1]);

test("Vite fingerprints every external brand image and changes its URL only when artwork changes", async () => {
  assert.ok(brandFiles.length >= 23, "include the Google family as well as existing SVG assets");
  for (const file of brandFiles) {
    assert.ok(existsSync(new URL(`assets/${file}`, brands)), `${file} must be a build-managed asset`);
    assert.ok(!existsSync(new URL(`public/assets/brands/${file}`, root)), `${file} must not retain an immutable unversioned URL`);
  }

  // Exercise Vite's real asset pipeline on disposable copies, without touching
  // source artwork, app output, or the running dev server.
  const fixture = mkdtempSync(join(tmpdir(), "manor-brand-assets-"));
  try {
    cpSync(new URL("marks.ts", brands), join(fixture, "marks.ts"));
    cpSync(new URL("assets/", brands), join(fixture, "assets"), { recursive: true });
    const compile = async () => {
      const result = await build({
        root: fixture, configFile: false, envFile: false, publicDir: false, logLevel: "silent",
        build: {
          write: false, emptyOutDir: false,
          rollupOptions: { input: join(fixture, "marks.ts"), preserveEntrySignatures: "strict", output: { assetFileNames: "assets/[name]-[hash:8][extname]" } },
        },
      });
      const emitted = result.output.filter(item => item.type === "asset" && /\.(svg|png|webp)$/.test(item.fileName));
      const code = result.output.filter(item => item.type === "chunk").map(item => item.code).join("\n");
      assert.equal(emitted.length, brandFiles.length);
      assert.ok(!code.includes("/assets/brands/"));
      return brandFiles.map(file => {
        const [name, ext] = file.split(".");
        const asset = emitted.find(item => new RegExp(`^assets/${name}-[\\w-]{8}\\.${ext}$`).test(item.fileName));
        assert.ok(asset, `missing fingerprinted asset: ${file}`);
        assert.ok(code.includes(`/${asset.fileName}`), `${file} URL must be used by the catalog`);
        return asset.fileName;
      });
    };

    const original = await compile();
    assert.deepEqual(await compile(), original, "unchanged artwork must keep its cacheable URL");
    for (const file of brandFiles) {
      const assetPath = join(fixture, "assets", file);
      // Change bytes only in the disposable build fixture, including raster files.
      writeFileSync(assetPath, Buffer.concat([readFileSync(assetPath), Buffer.from("\nCache invalidation regression")]));
    }
    const updated = await compile();
    for (const [index, file] of brandFiles.entries()) {
      assert.notEqual(updated[index], original[index], `${file} must invalidate the immutable cache after an artwork update`);
      assert.equal(readFileSync(new URL(`assets/${file}`, brands)).includes(Buffer.from("Cache invalidation regression")), false);
    }
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});
