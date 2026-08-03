import assert from "node:assert/strict";
import { test } from "node:test";

import {
  matchesWorkspaceTerm,
  matchWorkspacePresentationRule,
  workspaceHaystack,
} from "../src/components/ui/workspace-presentation.mjs";

/**
 * A one-person-company stickman video workspace was classified as
 * "Property ops" — the FIRST rule, so nothing after it got a chance to
 * match — because its description said "the current theme" and matching
 * was plain substring: `haystack.includes("rent")` is true for
 * "cur-RENT". Whole-word matching fixes it.
 */
test("the production case: 'current' does not read as 'rent'", () => {
  const ws = {
    name: "Faceless Stickman Video Studio (One-Person Company)",
    description:
      "Creates a repeatable daily topic-to-video loop. Each day the workspace " +
      "selects one useful topic from the current theme, drafts a concise " +
      "stickman script and storyboard, runs the embedded Stickman Video " +
      "Creator skill, and delivers a finished MP4.",
    kind: "one_person_company",
  };
  const rule = matchWorkspacePresentationRule(ws);
  assert.notEqual(rule?.label, "Property ops");
  assert.equal(rule?.label, "Content studio", "video/creator/content should win instead");
});

test("substring matching is rejected directly", () => {
  assert.equal(matchesWorkspaceTerm("the current theme", "rent"), false);
  assert.equal(matchesWorkspaceTerm("paid monthly rent", "rent"), true);
});

test("other short terms are common substrings too, and must not misfire", () => {
  // "ops" inside "shops" / "drops"; "ai" inside "email" / "maintain" / "explain".
  assert.equal(matchesWorkspaceTerm("running a shopify dropshipping store", "ops"), false);
  assert.equal(matchesWorkspaceTerm("send a confirmation email", "ai"), false);
  assert.equal(matchesWorkspaceTerm("please maintain the record", "ai"), false);
  // The real words still match.
  assert.equal(matchesWorkspaceTerm("daily ops review", "ops"), true);
  assert.equal(matchesWorkspaceTerm("built with ai tools", "ai"), true);
});

test("a genuine property-management workspace still matches Property ops", () => {
  const ws = { description: "Manages tenant leasing and rent collection." };
  assert.equal(matchWorkspacePresentationRule(ws)?.label, "Property ops");
});

test("multi-word terms still match as a phrase", () => {
  const ws = { description: "Runs a social channel for daily updates." };
  assert.equal(matchWorkspacePresentationRule(ws)?.label, "Social channel");
});

test("no match falls through cleanly (caller supplies the fallback)", () => {
  assert.equal(matchWorkspacePresentationRule({ description: "" }), null);
});

test("the haystack lowercases and joins every field, tags included", () => {
  const haystack = workspaceHaystack({
    name: "Acme",
    category: "Store",
    attribute_tags: ["Shopify", "DTC"],
  });
  assert.equal(haystack, "acme store shopify dtc");
});

test("rule order still governs when more than one term matches", () => {
  // "video" (Content studio) and "ops" (Operations) both appear; the first
  // matching RULE in list order wins, not the first matching term.
  const ws = { description: "video production ops team" };
  assert.equal(matchWorkspacePresentationRule(ws)?.label, "Content studio");
});
