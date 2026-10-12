import { invalidateCatalogQueries } from "../../lib/catalogInvalidation";
/** Review and reconcile a Blueprint inside its existing Workspace. */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Input from "../ui/Input";
import LoadingSpinner from "../ui/LoadingSpinner";
import Select from "../ui/Select";
import { api } from "../../lib/api";
import { formatBlueprintVariableValue, parseBlueprintVariableValue } from "../../lib/blueprintVariables.mjs";
import { t } from "../../lib/i18n";

type ConflictChoice = "keep_yours" | "use_blueprint";
type UpgradePlan = Awaited<ReturnType<typeof api.workspaces.blueprintUpgradePlan>>;
type UpgradeItem = UpgradePlan["items"][number];

function defaultChoice(item: UpgradeItem): ConflictChoice {
  return item.blocked_reason ? "keep_yours" : "use_blueprint";
}

function contentText(value: unknown): string {
  if (value === undefined || value === null) return t("page.blueprints.upgrade_content_absent");
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export interface BlueprintUpgradeDialogProps {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  workspaceName: string;
}

export default function BlueprintUpgradeDialog({ open, onClose, workspaceId, workspaceName }: BlueprintUpgradeDialogProps) {
  const queryClient = useQueryClient();
  const [applied, setApplied] = useState<{
    updated: number; kept: number; canRevert: boolean; customized: boolean;
  } | null>(null);
  const [conflictChoices, setConflictChoices] = useState<Record<string, ConflictChoice>>({});
  const [variableValues, setVariableValues] = useState<Record<string, string>>({});
  const [channelConfigIds, setChannelConfigIds] = useState<Record<string, string>>({});

  const basePlanQuery = useQuery({
    queryKey: ["blueprint-upgrade-plan", workspaceId],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId),
    enabled: open,
  });
  // The server only exposes declarations that affect materialized configuration.
  const installVariables = [
    ...(basePlanQuery.data?.setup_preview.required_variables ?? []),
    ...(basePlanQuery.data?.setup_preview.optional_variables ?? []),
  ].filter((variable) => variable.key);
  const variableByKey = new Map(installVariables.map((variable) => [variable.key as string, variable]));
  const variableOverrides: Record<string, unknown> = {};
  const invalidVariableKeys = new Set<string>();
  for (const [key, input] of Object.entries(variableValues)) {
    const parsed = parseBlueprintVariableValue(input, variableByKey.get(key)?.default);
    if (parsed.ok) variableOverrides[key] = parsed.value;
    else invalidVariableKeys.add(key);
  }
  const hasOverrides = Object.keys(variableValues).length > 0 || Object.keys(channelConfigIds).length > 0;
  const overridePlanQuery = useQuery({
    queryKey: ["blueprint-upgrade-plan", workspaceId, variableOverrides, channelConfigIds],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId, variableOverrides, channelConfigIds),
    enabled: open && hasOverrides && invalidVariableKeys.size === 0,
  });
  const currentPlanQuery = hasOverrides ? overridePlanQuery : basePlanQuery;
  const plan = currentPlanQuery.data ?? basePlanQuery.data;
  const savedValues = basePlanQuery.data?.resolved_variable_values ?? {};
  const variableInput = (key: string, defaultValue: unknown) => variableValues[key]
    ?? formatBlueprintVariableValue(Object.hasOwn(savedValues, key) ? savedValues[key] : defaultValue);
  const missingRequiredVariable = (basePlanQuery.data?.setup_preview.required_variables ?? [])
    .some((variable) => variable.key && !variableInput(variable.key, variable.default).trim());
  const isLoading = basePlanQuery.isLoading;
  const isError = basePlanQuery.isError;
  const error = basePlanQuery.error;
  const refetchPlan = () => {
    void basePlanQuery.refetch();
    if (hasOverrides && invalidVariableKeys.size === 0) void overridePlanQuery.refetch();
  };

  const invalidate = () => {
    return Promise.all([
      queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
      invalidateCatalogQueries(queryClient, "agents"),
      queryClient.invalidateQueries({ queryKey: ["agent-deployments"] }),
      invalidateCatalogQueries(queryClient, "skills"),
      queryClient.invalidateQueries({ queryKey: ["workflows"] }),
      queryClient.invalidateQueries({ queryKey: ["goals"] }),
      queryClient.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
      ...[
        "workspace", "blueprint-upgrade-plan", "workspace-channels",
        "workspace-available-channels", "workspace-agents", "workspace-setup-status",
        "workspace-capabilities", "workspace-operating-model", "workspace-dashboard",
        "workspace-documents", "workflow-bindings", "workspace-automation-bindings-summary",
        "workspace-stats", "workspace-stats-library", "workspace-stats-quick-view",
        "workspace-goals", "workspace-goals-graph", "workspace-governance", "workspace-approval-matrix",
        "workspace-budget", "workspace-heartbeat", "workspace-scheduled-job-summary",
        "workspace-automation-health", "workspace-available-documents", "workspace-connection-status",
      ].map((key) => queryClient.invalidateQueries({ queryKey: [key, workspaceId] })),
    ]);
  };

  const items = plan?.items ?? [];
  const visibleItems = items.filter((item) => item.action !== "unchanged");
  const conflictsToKeep = visibleItems.filter((item) => (conflictChoices[item.key] ?? defaultChoice(item)) === "keep_yours");
  const selectedUpdateCount = visibleItems.length - conflictsToKeep.length;
  const hasCurrentPlan = plan?.upgrade_protocol_version === 3
    && Boolean(plan.blueprint_fingerprint && plan.plan_fingerprint)
    && !currentPlanQuery.isFetching && !currentPlanQuery.isError;
  const runtimeItem = items.find((item) => item.key === "runtime_requirements");
  const keepsRuntime = runtimeItem && (runtimeItem.action === "unchanged"
    ? JSON.stringify(runtimeItem.current_content) !== JSON.stringify(runtimeItem.new_content)
    : (conflictChoices[runtimeItem.key] ?? defaultChoice(runtimeItem)) === "keep_yours");
  const setupPreflight = keepsRuntime && plan?.kept_setup_preflight
    ? plan.kept_setup_preflight : plan?.setup_preflight;
  const resolvedChannelConfigIds = { ...channelConfigIds };
  for (const requirement of setupPreflight?.requirements ?? []) {
    if (requirement.kind === "channel" && requirement.requirement_key && requirement.resource_id
      && !resolvedChannelConfigIds[requirement.requirement_key]) {
      resolvedChannelConfigIds[requirement.requirement_key] = requirement.resource_id;
    }
  }
  const activeRuns = plan?.active_runs ?? [];
  const canApply = plan?.variables_ready === true && setupPreflight?.ready === true
    && !missingRequiredVariable && invalidVariableKeys.size === 0 && activeRuns.length === 0;

  const applyMutation = useMutation({
    mutationFn: async () => {
      if (!hasCurrentPlan || !canApply || !plan?.blueprint_fingerprint || !plan.plan_fingerprint) {
        throw new Error(t("page.blueprints.upgrade_plan_changed"));
      }
      const result = await api.workspaces.applyBlueprintUpgrade(workspaceId, {
        expected_blueprint_fingerprint: plan.blueprint_fingerprint,
        expected_plan_fingerprint: plan.plan_fingerprint,
        variable_values: variableOverrides,
        // Keep these inputs identical to preview; the server resolves defaults.
        channel_config_ids: channelConfigIds,
        configuration_resolutions: visibleItems.map((item) => ({
          key: item.key,
          resolution: conflictChoices[item.key] ?? defaultChoice(item),
        })),
      });
      if (result.upgrade_protocol_version !== 3) throw new Error(t("page.blueprints.upgrade_plan_changed"));
      return result;
    },
    onSuccess: async (result) => {
      setApplied({
        updated: result.updated.length, kept: result.kept_yours.length,
        canRevert: result.can_revert, customized: result.synchronization_status === "customized",
      });
      await invalidate();
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["blueprint-upgrade-plan", workspaceId] });
    },
  });
  const revertMutation = useMutation({
    mutationFn: () => api.workspaces.revertBlueprintUpgrade(workspaceId),
    onSuccess: async () => {
      setApplied(null);
      await invalidate();
      onClose();
    },
  });
  const busy = applyMutation.isPending || revertMutation.isPending;
  const canRevert = applied?.canRevert ?? plan?.can_revert ?? false;
  const revertButton = canRevert ? (
    <Button variant="outline" loading={revertMutation.isPending} disabled={busy || activeRuns.length > 0}
      onClick={() => revertMutation.mutate()}>
      {t("page.blueprints.upgrade_revert")}
    </Button>
  ) : null;
  const revertError = revertMutation.isError ? (
    <p role="alert" className="text-xs text-red-700">
      {revertMutation.error instanceof Error && revertMutation.error.message
        ? revertMutation.error.message : t("page.blueprints.upgrade_revert_failed")}
    </p>
  ) : null;

  useEffect(() => {
    if (!open) return;
    setApplied(null);
    setVariableValues({});
    setChannelConfigIds({});
    setConflictChoices({});
    applyMutation.reset();
    revertMutation.reset();
    // Mutation reset functions are stable; opening another Workspace starts a new review.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workspaceId]);
  useEffect(() => {
    // A changed current or target configuration requires reviewing choices again.
    setConflictChoices({});
  }, [plan?.plan_fingerprint]);

  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} maxWidth="900px"
      title={t("page.blueprints.upgrade_title").replace("{workspace}", workspaceName)}
      footer={!isLoading && !isError ? (
        <div className="flex flex-wrap justify-end gap-2">
          {revertButton}
          <Button variant={applied ? "primary" : "outline"} onClick={onClose} disabled={busy}>
            {t(applied ? "action.done" : "action.cancel")}
          </Button>
          {!applied && <Button onClick={() => applyMutation.mutate()} loading={applyMutation.isPending}
            disabled={busy || !canApply || !hasCurrentPlan}>
            {selectedUpdateCount > 0
              ? t("page.blueprints.upgrade_confirm").replace("{count}", String(selectedUpdateCount))
              : conflictsToKeep.length > 0 ? t("page.blueprints.upgrade_confirm_keep_yours")
                : t("page.blueprints.upgrade_confirm_current")}
          </Button>}
        </div>
      ) : undefined}>
      {isLoading ? <LoadingSpinner /> : isError ? (
        <div className="space-y-3">
          <p role="alert" className="text-sm text-red-700">{t("page.blueprints.upgrade_load_failed")}</p>
          {error instanceof Error && <p className="text-xs text-stone-500">{error.message}</p>}
          <Button variant="outline" onClick={refetchPlan}>{t("page.blueprints.refresh_status")}</Button>
        </div>
      ) : applied ? (
        <div className="space-y-3" role="status">
          <p className="text-sm text-stone-800">{t("page.blueprints.upgrade_done")
            .replace("{updated}", String(applied.updated)).replace("{kept}", String(applied.kept))}</p>
          <p className="text-sm text-stone-600">{t(applied.customized
            ? "page.blueprints.upgrade_customized_done" : "page.blueprints.upgrade_synchronized_done")}</p>
          {revertError}
        </div>
      ) : (
        <div className="space-y-4 text-sm">
          <p className="leading-relaxed text-stone-600">{t("page.blueprints.upgrade_in_place_help")
            .replace("{workspace}", workspaceName)}</p>
          {plan?.restore_point && <p className="text-xs text-stone-500">
            {t("page.blueprints.upgrade_restore_point")
              .replace("{date}", new Date(plan.restore_point.applied_at).toLocaleString())
              .replace("{version}", plan.restore_point.blueprint_version || "—")}
          </p>}
          {plan?.variables_ready && plan.baseline_known === false && <p className="rounded-xl bg-stone-100 p-3 text-xs leading-relaxed text-stone-700">
            {t("page.blueprints.upgrade_baseline_review")}
          </p>}
          {activeRuns.length > 0 && <section aria-label={t("page.blueprints.upgrade_active_runs_title")}
            className="rounded-xl bg-stone-100 p-3">
            <h3 className="font-semibold">{t("page.blueprints.upgrade_active_runs_title")}</h3>
            <p className="mt-1 text-xs leading-relaxed text-stone-600">{t("page.blueprints.upgrade_active_runs_help")}</p>
            <ul className="my-3 space-y-1 text-xs">
              {activeRuns.map((run) => <li key={`${run.kind}:${run.id}`} className="break-words">
                {run.name} · {run.status}
              </li>)}
            </ul>
            <Button variant="outline" onClick={refetchPlan} disabled={busy || currentPlanQuery.isFetching}>
              {t("page.blueprints.refresh_status")}
            </Button>
          </section>}
          {installVariables.length > 0 && <section className="rounded-xl bg-stone-50 p-3">
            <h3 className="font-semibold">{t("component.install_blueprint_modal.workspace_settings")}</h3>
            <p className="mb-3 mt-1 text-xs text-stone-500">{t("page.blueprints.upgrade_settings_help")}</p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {installVariables.map((variable) => {
                const key = variable.key as string;
                const input = variableInput(key, variable.default);
                return <Input key={key} label={variable.label} value={input} required={variable.required}
                  disabled={busy} hint={variable.purpose ?? undefined}
                  onChange={(event) => setVariableValues((current) => ({ ...current, [key]: event.target.value }))}
                  error={variable.required && !input.trim()
                    ? t("component.install_blueprint_modal.required_value")
                    : invalidVariableKeys.has(key) ? t("component.install_blueprint_modal.invalid_typed_value") : undefined} />;
              })}
            </div>
          </section>}
          {currentPlanQuery.isFetching && <p aria-live="polite" className="text-xs text-stone-500">
            {t("page.blueprints.upgrade_refreshing_preview")}
          </p>}
          {hasOverrides && overridePlanQuery.isError && <p role="alert" className="text-xs text-red-700">
            {overridePlanQuery.error instanceof Error ? overridePlanQuery.error.message
              : t("page.blueprints.upgrade_variable_preview_failed")}
            <Button variant="ghost" onClick={refetchPlan}>{t("page.blueprints.refresh_status")}</Button>
          </p>}
          {visibleItems.length === 0 ? <p className="text-stone-600">{t(plan?.variables_ready
            ? plan.synchronization_status === "customized" ? "page.blueprints.upgrade_customized_done"
              : "page.blueprints.upgrade_already_matches"
            : "page.blueprints.upgrade_variables_required")}</p> : (
            <div className="space-y-3">
              <p className="text-xs leading-relaxed text-stone-500">{t("page.blueprints.upgrade_conflicts_help")}</p>
              {visibleItems.map((item) => {
                const choice = conflictChoices[item.key] ?? defaultChoice(item);
                const action = choice === "keep_yours" ? "keep_yours" : item.action === "keep_yours" ? "update" : item.action;
                return <section key={item.key} aria-label={item.name} className="rounded-xl bg-stone-50 p-3">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <h3 className="break-words font-semibold text-stone-800">{item.name}</h3>
                      <span className="text-xs text-stone-500">{t(`page.blueprints.upgrade_action_${action}`)}</span>
                    </div>
                    <Select value={choice} disabled={busy || Boolean(item.blocked_reason)}
                      onChange={(value) => setConflictChoices((current) => ({ ...current, [item.key]: value as ConflictChoice }))}
                      options={[
                        { value: "keep_yours", label: t("page.blueprints.upgrade_resolution_keep_yours") },
                        { value: "use_blueprint", label: t("page.blueprints.upgrade_resolution_use_blueprint") },
                      ]}
                      ariaLabel={t("page.blueprints.upgrade_resolution_label").replace("{item}", item.name)}
                      style={{ width: "min(100%, 230px)" }} />
                  </div>
                  {item.changes.length > 0 && <p className="mt-2 break-words text-xs text-stone-500">{item.changes.join(" · ")}</p>}
                  {item.blocked_reason && <p className="mt-2 text-xs leading-relaxed text-stone-600">{item.blocked_reason}</p>}
                  <details className="mt-3">
                    <summary className="cursor-pointer text-xs font-semibold text-stone-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4">
                      {t("page.blueprints.upgrade_show_diff")}
                    </summary>
                    <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
                      {([
                        ["current", contentText(item.current_content)],
                        ["new", contentText(item.new_content)],
                        ["baseline", item.baseline_known ? contentText(item.baseline_content) : t("page.blueprints.upgrade_baseline_unavailable")],
                      ] as const).map(([label, content]) => <div key={label} className="min-w-0">
                        <h4 className="mb-1 text-xs font-semibold text-stone-500">{t(`page.blueprints.upgrade_content_${label}`)}</h4>
                        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-stone-100 p-2 font-mono text-[11px] leading-relaxed text-stone-700">{content}</pre>
                      </div>)}
                    </div>
                  </details>
                </section>;
              })}
            </div>
          )}
          {conflictsToKeep.length > 0 && <p className="text-xs leading-relaxed text-stone-600">
            {t("page.blueprints.upgrade_keeps_yours").replace("{count}", String(conflictsToKeep.length))}
          </p>}
          {setupPreflight?.ready === false && <section className="rounded-xl bg-stone-100 p-3">
            <p className="text-xs leading-relaxed">{t("page.blueprints.upgrade_connections_required")}</p>
            <div className="mt-3 space-y-3">
              {setupPreflight.requirements.filter((requirement) => requirement.required && !requirement.ready).map((requirement) => (
                <div key={`${requirement.kind}:${requirement.requirement_key}:${requirement.label}`} className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0 flex-1"><p className="font-semibold">{requirement.label}</p>
                    <p className="text-xs text-stone-500">{requirement.purpose || requirement.reason}</p></div>
                  {requirement.kind === "channel" && requirement.requirement_key && requirement.resource_options.length > 0 && <Select
                    value={resolvedChannelConfigIds[requirement.requirement_key] || ""} disabled={busy}
                    onChange={(value) => setChannelConfigIds((current) => ({ ...current, [requirement.requirement_key as string]: value }))}
                    options={requirement.resource_options.map((option) => ({ value: option.id, label: option.label }))}
                    placeholder={t("page.blueprints.setup_required_short")} ariaLabel={requirement.label}
                    style={{ width: "min(100%, 230px)" }} />}
                </div>
              ))}
            </div>
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              <Button variant="outline" onClick={() => window.open("/integrations", "_blank", "noopener,noreferrer")}>{t("page.blueprints.open_integrations")}</Button>
              <Button variant="outline" onClick={refetchPlan}>{t("page.blueprints.refresh_status")}</Button>
            </div>
          </section>}
          {applyMutation.isError && <p role="alert" className="text-xs text-red-700">
            {applyMutation.error instanceof Error && applyMutation.error.message
              ? applyMutation.error.message : t("page.blueprints.upgrade_apply_failed")}
          </p>}
          {revertError}
        </div>
      )}
    </Modal>
  );
}
