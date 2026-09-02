import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveBlueprintChannelSelections } from "../src/lib/blueprintSetup.mjs";

const requirement = {
  kind: "channel", requirement_key: "channel:0:telegram", resource_id: "one",
  resource_options: [{ id: "one" }, { id: "two" }],
};

test("auto-selection and explicit choices use current eligible accounts", () => {
  assert.deepEqual(resolveBlueprintChannelSelections([requirement]), { "channel:0:telegram": "one" });
  assert.deepEqual(resolveBlueprintChannelSelections([requirement], { "channel:0:telegram": "two" }), {
    "channel:0:telegram": "two",
  });
});

test("disconnected choices and obsolete requirement keys cannot be submitted", () => {
  assert.deepEqual(resolveBlueprintChannelSelections([requirement], {
    "channel:0:telegram": "disconnected", "channel:1:slack": "secret",
  }), {});
  assert.deepEqual(resolveBlueprintChannelSelections([{ ...requirement, resource_options: [] }]), {});
});

test("built-in and older server requirements do not invent account selections", () => {
  assert.deepEqual(resolveBlueprintChannelSelections([
    { kind: "channel", requirement_key: "channel:0:webchat", ready: true },
    { kind: "integration", resource_id: "ignored" },
  ]), {});
});
