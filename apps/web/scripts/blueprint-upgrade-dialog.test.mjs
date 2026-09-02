import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

const source = await readFile(
  new URL("../src/components/blueprints/BlueprintUpgradeDialog.tsx", import.meta.url),
  "utf8",
);
const workspaceDetail = await readFile(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const workspaceApiSource = await readFile(
  new URL("../../api/routers/workspaces.py", import.meta.url),
  "utf8",
);

test("version-only blueprint updates can be acknowledged instead of showing Update 0", () => {
  assert.match(
    source,
    /const versionOnlySync = hasCurrentPlan && visibleItems\.length === 0/,
  );
  assert.ok(source.includes('t("page.blueprints.upgrade_confirm_current")'));
  assert.ok(source.includes("disabled={busy || !canApply || !hasCurrentPlan}"));
});

test("settings-only personalization updates can be confirmed with no component items", () => {
  assert.ok(source.includes("const versionOnlySync = hasCurrentPlan && visibleItems.length === 0"));
  assert.ok(source.includes("selectedUpdateCount > 0 || keepYoursOnlySync || versionOnlySync"));
});

test("clearing a saved required personalization value shows a field error", () => {
  assert.ok(source.includes("Object.hasOwn(variableValues, variable.key as string)"));
  assert.ok(source.includes("? !variableValues[variable.key as string]?.trim()"));
});

test("an upgrade that only keeps user edits can still be confirmed", () => {
  assert.match(
    source,
    /const keepYoursOnlySync =\s+selectedUpdateCount === 0 &&\s+conflictsToKeep\.length > 0 &&\s+unavailableMissing\.length === 0/,
  );
  assert.match(
    source,
    /const canApply =\s+unavailableMissing\.length === 0 &&/,
  );
  assert.ok(source.includes("plan?.variables_ready === true"));
  assert.ok(source.includes("!((hasVariableOverrides || hasChannelSelections) && overridePlanQuery.isError)"));
  assert.ok(source.includes('t("page.blueprints.upgrade_confirm_keep_yours")'));
});

test("each edited Blueprint item exposes an explicit conflict choice", () => {
  assert.ok(source.includes('import Select from "../ui/Select"'));
  assert.ok(source.includes('type ConflictChoice = "keep_yours" | "use_blueprint"'));
  assert.ok(source.includes('item.action === "keep_yours" && ('));
  assert.ok(source.includes('t("page.blueprints.upgrade_resolution_keep_yours")'));
  assert.ok(source.includes('t("page.blueprints.upgrade_resolution_use_blueprint")'));
  assert.ok(source.includes('ariaLabel={t("page.blueprints.upgrade_resolution_label")'));
});

test("conflict decisions carry the reviewed Blueprint and item revisions", () => {
  assert.ok(source.includes("expected_blueprint_fingerprint: plan.blueprint_fingerprint"));
  assert.ok(source.includes("conflict_resolutions: keptYours.map"));
  assert.ok(source.includes("expected_revision: item.revision"));
  assert.ok(source.includes('resolution: conflictChoices[upgradeItemKey(item)] ?? "keep_yours"'));
  assert.ok(source.includes('role="alert"'));
});

test("rolling deployments cannot silently ignore conflict choices", () => {
  assert.ok(source.includes("plan?.upgrade_protocol_version === 2"));
  assert.ok(source.includes("result.upgrade_protocol_version !== 2"));
  assert.ok(apiSource.includes("`/workspaces/${id}/blueprint/upgrade/v2`"));
  assert.ok(workspaceApiSource.includes('@router.post("/{workspace_id}/blueprint/upgrade/v2")'));
  assert.ok(source.includes('t("page.blueprints.upgrade_plan_changed")'));
});

test("upgrade personalization rebuilds the preview and is applied with the reviewed raw fingerprint", () => {
  assert.ok(source.includes("variable_values: variableOverrides"));
  assert.ok(source.includes("api.workspaces.blueprintUpgradePlan("));
  assert.ok(apiSource.includes("/blueprint/upgrade/preview"));
  assert.ok(workspaceApiSource.includes("resolve_install_variables"));
  assert.ok(workspaceApiSource.includes("source_payload=source_payload"));
  assert.match(workspaceApiSource, /if variables_ready\s+else \{"ready": True/);
});

test("upgrade channel requirements can be selected and submitted", () => {
  assert.ok(source.includes("const [channelConfigIds, setChannelConfigIds]"));
  assert.ok(source.includes("channel_config_ids: resolvedChannelConfigIds"));
  assert.ok(source.includes("requirement.resource_options.map"));
  assert.ok(source.includes("const channelPlanPending ="));
  assert.ok(source.includes("!channelPlanPending"));
  assert.ok(apiSource.includes("channelConfigIds?: Record<string, string>"));
  assert.ok(apiSource.includes("channel_config_ids: channelConfigIds ?? {}"));
  assert.ok(workspaceApiSource.includes("selected_channel_config_ids=req.channel_config_ids"));
  assert.ok(workspaceApiSource.includes("channel_config_ids=req.channel_config_ids"));
});

test("missing blueprint items remain visible and are not treated as version-only", () => {
  assert.match(
    source,
    /const visibleItems = items\.filter\(\(item\) => item\.action !== "unchanged"\)/,
  );
  assert.ok(source.includes('t("page.blueprints.upgrade_missing_items")'));
  assert.ok(source.includes('t("page.blueprints.upgrade_no_automatic_updates")'));
  assert.match(
    source,
    /const unavailableMissing = missing\.filter\(\(item\) => item\.kind !== "workflow"\)/,
  );
  assert.match(
    source,
    /const canApply =\s+unavailableMissing\.length === 0 &&/,
  );
});

test("legacy installs can apply safe component updates without claiming full synchronization", () => {
  assert.ok(source.includes("baseline_unknown"));
  assert.ok(source.includes("reconfigure"));
  assert.ok(source.includes("partial: !result.fully_synchronized"));
  assert.ok(source.includes('t("page.blueprints.upgrade_partial_done")'));
  assert.ok(apiSource.includes('"baseline_unknown"'));
  assert.ok(apiSource.includes('"reconfigure"'));
  assert.ok(apiSource.includes("fully_synchronized: boolean"));
});

test("a missing Blueprint Flow is counted as an installable update", () => {
  assert.match(
    source,
    /const installableMissing = missing\.filter\(\(item\) => item\.kind === "workflow"\)/,
  );
  assert.match(
    source,
    /toUpdate\.length \+ useBlueprintConflicts\.length \+ installableMissing\.length/,
  );
  assert.ok(source.includes("{unavailableMissing.length > 0 && ("));
});

test("version-only confirmation does not offer an undo that has no restore point", () => {
  assert.ok(source.includes("canRevert: result.can_revert"));
  assert.ok(source.includes("const canRevert = applied?.canRevert ?? plan?.can_revert ?? false"));
});

test("reopening a dialog exposes persisted undo without another apply", () => {
  assert.ok(source.includes("const revertButton = canRevert ? ("));
  assert.equal(source.match(/\{revertButton\}/g)?.length, 2);
  assert.equal(source.match(/revertMutation\.mutate\(\)/g)?.length, 1);
  assert.ok(source.includes("const revertError = revertMutation.isError ? ("));
  assert.equal(source.match(/\{revertError\}/g)?.length, 2);
});

test("an undo rejected after later edits shows the server error", () => {
  assert.ok(source.includes("revertMutation.isError"));
  assert.ok(source.includes("revertMutation.error instanceof Error"));
});

test("a blueprint-installed workspace can check real installed items even without a stale badge", () => {
  assert.ok(workspaceDetail.includes("ws.blueprint_update?.blueprint_id"));
  assert.ok(workspaceDetail.includes('t("page.workspaces.blueprint_check_updates")'));
  assert.ok(workspaceDetail.includes("setShowBlueprintUpgrade(true)"));
});

test("a failed upgrade-plan request is never presented as nothing to update", () => {
  assert.ok(source.includes("const isError = basePlanQuery.isError"));
  assert.ok(source.includes("const error = basePlanQuery.error"));
  assert.ok(source.includes('t("page.blueprints.upgrade_load_failed")'));
  assert.match(source, /isLoading \? \([\s\S]+\) : isError \? \(/);
});

test("apply and undo refresh active Channel/configuration queries without a page reload", async () => {
  const body = source.match(/const invalidate = \(\) => \{([\s\S]*?)\n  \};/)?.[1];
  assert.ok(body, "Exercise the dialog's real invalidation callback");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = new Function("queryClient", "workspaceId", body);
  const keys = [
    "workspace-channels", "workspace-available-channels", "workspace-agents",
    "workspace-setup-status", "workspace-capabilities", "workspace-operating-model",
    "workspace-dashboard",
    "workspace-documents", "workflow-bindings", "workspace-automation-bindings-summary",
  ];
  let serverRevision = "before";
  const unsubscribers = [];
  try {
    for (const key of keys) {
      const queryKey = [key, "target"];
      await queryClient.fetchQuery({ queryKey, queryFn: async () => serverRevision });
      const observer = new QueryObserver(queryClient, {
        queryKey, queryFn: async () => serverRevision, staleTime: Infinity,
      });
      unsubscribers.push(observer.subscribe(() => {}));
      queryClient.setQueryData([key, "other-workspace"], "untouched");
    }
    for (const revision of ["applied", "reverted"]) {
      serverRevision = revision;
      await invalidate(queryClient, "target");
      for (const key of keys) {
        assert.equal(queryClient.getQueryData([key, "target"]), revision, key);
        assert.equal(queryClient.getQueryData([key, "other-workspace"]), "untouched");
        assert.equal(queryClient.getQueryState([key, "other-workspace"]).isInvalidated, false);
      }
    }
  } finally {
    unsubscribers.forEach((unsubscribe) => unsubscribe());
    queryClient.clear();
  }
});

test("both mutations await refreshing before leaving their pending state or closing undo", async () => {
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  for (const kind of ["apply", "revert"]) {
    const callback = source.slice(source.indexOf(`const ${kind}Mutation = useMutation`))
      .match(/onSuccess: async \((?:result)?\) => \{([\s\S]*?)\n    \},/)?.[1];
    assert.ok(callback, `${kind} must await its success callback`);
    let finishRefresh;
    const refreshing = new Promise((resolve) => { finishRefresh = resolve; });
    const calls = [];
    const onSuccess = new AsyncFunction("result", "setApplied", "invalidate", "onClose", callback);
    let settled = false;
    const work = onSuccess(
      { updated: [], kept_yours: [], can_revert: true, fully_synchronized: true },
      () => {}, () => refreshing, () => calls.push("closed"),
    ).then(() => { settled = true; });
    await Promise.resolve();
    assert.equal(settled, false);
    assert.deepEqual(calls, []);
    finishRefresh();
    await work;
    assert.deepEqual(calls, kind === "revert" ? ["closed"] : []);
  }
});
