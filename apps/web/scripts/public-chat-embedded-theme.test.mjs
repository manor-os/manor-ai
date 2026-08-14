import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/pages/PublicChat.tsx", import.meta.url), "utf8");
const nginx = await readFile(new URL("../../../docker/nginx.conf", import.meta.url), "utf8");

test("embedded public chat consumes the website theme safely", () => {
  assert.match(source, /const EMBED_THEME_COLOR =/);
  assert.match(source, /params\.get\("theme"\) === "dark"/);
  assert.match(source, /params\.get\("font"\).*replace\(\/\[;\{\}<>\]\//s);
  assert.match(source, /"--public-chat-accent": accent/);
  assert.match(source, /"--public-chat-surface": surface/);
  assert.match(source, /"--public-chat-font": rawFont/);
});

test("every prominent embedded chat surface uses inherited theme variables", () => {
  assert.match(source, /background: "var\(--public-chat-surface, #fff\)"/);
  assert.match(source, /background: "var\(--public-chat-accent, #5d7f77\)"/);
  assert.match(source, /color: "var\(--public-chat-on-accent, #fff\)"/);
  assert.match(source, /fontFamily: "var\(--public-chat-font,/);
});

test("only the tokenized public chat route is allowed to render in website frames", () => {
  assert.match(nginx, /location \^~ \/chat\/public\//);
  assert.match(nginx, /frame-ancestors http: https:/);
  assert.doesNotMatch(
    nginx.match(/location \^~ \/chat\/public\/[\s\S]*?\n    }/)?.[0] || "",
    /X-Frame-Options/,
  );
});
