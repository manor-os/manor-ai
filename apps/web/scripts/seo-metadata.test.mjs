import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("authenticated web app publishes safe discovery metadata", () => {
  const html = read("../index.html");
  const robots = read("../public/robots.txt");
  const llms = read("../public/llms.txt");

  assert.match(html, /name="description"/);
  assert.match(html, /name="robots" content="noindex, nofollow, noarchive"/);
  assert.match(html, /property="og:title"/);
  assert.match(html, /name="twitter:card"/);

  assert.match(robots, /^User-agent: \*$/m);
  assert.match(robots, /^Disallow: \/$/m);
  assert.match(robots, /^Allow: \/llms\.txt$/m);

  assert.match(llms, /^# Manor AI Application$/m);
  assert.match(llms, /https:\/\/github\.com\/manor-os\/manor-ai/);
  assert.match(llms, /source-available/);
});
