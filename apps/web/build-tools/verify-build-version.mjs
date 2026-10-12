import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export function verifyBuildVersion(dist) {
  const { version } = JSON.parse(readFileSync(resolve(dist, "version.json"), "utf8"));
  if (typeof version !== "string" || !version.trim()) {
    throw new Error("version.json must contain a nonempty build version");
  }
  const assetsDir = `assets/releases/${createHash("sha256").update(version).digest("hex").slice(0, 20)}`;
  for (const entry of ["index.html", "admin.html"]) {
    const file = resolve(dist, entry);
    if (entry === "admin.html" && !existsSync(file)) continue;
    const html = readFileSync(file, "utf8");
    const scripts = [...html.matchAll(/<script\b[^>]*>/gi)]
      .map(([tag]) => /\btype=["']module["']/i.test(tag) && /\bsrc=["']([^"']+)["']/i.exec(tag)?.[1])
      .filter(Boolean);
    if (!scripts.length) throw new Error(`${entry} has no built module entry`);
    for (const src of scripts) {
      const pathname = new URL(src, "https://build.invalid/").pathname;
      if (!pathname.startsWith(`/${assetsDir}/`)) {
        throw new Error(`${entry} loads assets from a different version than version.json (${version})`);
      }
      if (!existsSync(resolve(dist, `.${pathname}`))) {
        throw new Error(`${entry} references a missing module: ${src}`);
      }
    }
  }
  const directory = resolve(dist, assetsDir);
  const versionLiteral = JSON.stringify(version);
  const hasCompiledVersion = readdirSync(directory)
    .filter((name) => name.endsWith(".js"))
    .some((name) => readFileSync(resolve(directory, name), "utf8").includes(versionLiteral));
  if (!hasCompiledVersion) {
    throw new Error(`JavaScript does not contain the version.json build version (${version}); rebuild Web instead of retagging it`);
  }
  return version;
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    console.log(`Web build version verified: ${verifyBuildVersion(resolve(process.argv[2] || "dist"))}`);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
