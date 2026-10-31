// A Blueprint package (.manor) is a gzip tar: package/manifest.json,
// package/blueprint.json and package/assets/<sha256> (packages/core/blueprints/package.py).
// The browser only reads the payload, to show its install form and preflight;
// the server unpacks and verifies the whole file when it installs it.

const BLOCK = 512;

export function isBlueprintPackageName(name: string): boolean {
  return name.toLowerCase().endsWith(".manor");
}

function text(bytes: Uint8Array): string {
  const end = bytes.indexOf(0);
  return new TextDecoder().decode(end < 0 ? bytes : bytes.subarray(0, end));
}

/** Each regular file in a tar archive, by path. Long-name (PAX) headers are skipped. */
export function tarFiles(archive: Uint8Array): Map<string, Uint8Array> {
  const files = new Map<string, Uint8Array>();
  let offset = 0;
  while (offset + BLOCK <= archive.length) {
    const header = archive.subarray(offset, offset + BLOCK);
    if (header.every((byte) => byte === 0)) break;
    const name = text(header.subarray(0, 100));
    const prefix = text(header.subarray(345, 500));
    const size = parseInt(text(header.subarray(124, 136)).trim() || "0", 8);
    const type = String.fromCharCode(header[156] || 48);
    const start = offset + BLOCK;
    if (type === "0" || type === "\0") {
      files.set(prefix ? `${prefix}/${name}` : name, archive.subarray(start, start + size));
    }
    offset = start + Math.ceil(size / BLOCK) * BLOCK;
  }
  return files;
}

async function gunzip(data: Blob): Promise<Uint8Array> {
  const stream = data.stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** The payload inside a .manor file; carried files stay placeholders until install. */
export async function readBlueprintPackagePayload(file: Blob): Promise<Record<string, unknown>> {
  const blueprint = tarFiles(await gunzip(file)).get("package/blueprint.json");
  if (!blueprint) throw new Error("This file is not a Blueprint package.");
  return JSON.parse(new TextDecoder().decode(blueprint)) as Record<string, unknown>;
}
