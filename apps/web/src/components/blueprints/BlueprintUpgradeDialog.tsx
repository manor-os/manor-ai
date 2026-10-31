import { invalidateCatalogQueries } from "../../lib/catalogInvalidation";
/** Review and reconcile a Blueprint inside its existing Workspace. */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Input from "../ui/Input";
import LoadingSpinner from "../ui/LoadingSpinner";
import Select from "../ui/Select";
import StatusPill from "../ui/StatusPill";
import { api } from "../../lib/api";
import { formatBlueprintVariableValue, parseBlueprintVariableValue } from "../../lib/blueprintVariables.mjs";
import { t } from "../../lib/i18n";
import { getPreferredTimeZone } from "../../lib/format";

type ConflictChoice = "keep_yours" | "use_blueprint";
type UpgradePlan = Awaited<ReturnType<typeof api.workspaces.blueprintUpgradePlan>>;
type UpgradeItem = UpgradePlan["items"][number];

function defaultChoice(item: UpgradeItem): ConflictChoice {
  // Edits to items the Blueprint did not change stay yours unless you switch them.
  return item.blocked_reason || item.action === "customized" ? "keep_yours" : "use_blueprint";
}

function compatibilityText(issue: NonNullable<UpgradeItem["compatibility_issues"]>[number]): string {
  const users = issue.required_by.map((dependant) => dependant.name).join(", ")
    || t("page.blueprints.upgrade_issue_blueprint");
  return t(`page.blueprints.upgrade_issue_${issue.code}`)
    .replace("{names}", issue.names.join(", ")).replace("{users}", users);
}

function contentText(value: unknown): string {
  if (value === undefined || value === null) return t("page.blueprints.upgrade_content_absent");
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/** Which release to move to, what kind of step that is, and what changed on the way. */
function VersionChoice({ plan, disabled, onChange }: {
  plan: UpgradePlan; disabled: boolean; onChange: (version: string | undefined) => void;
}) {
  const versions = plan.available_versions ?? [];
  if (versions.length === 0) return null;
  const latest = plan.latest_version;
  const installed = plan.installed_version;
  const target = plan.target_version ?? latest ?? versions[0].version;
  const change = plan.version_change;
  const label = (version: (typeof versions)[number]) => [
    version.version,
    version.version === latest ? t("page.blueprints.upgrade_version_latest") : null,
    version.version === installed ? t("page.blueprints.upgrade_version_installed") : null,
    version.deprecated ? t("page.blueprints.upgrade_version_deprecated") : null,
  ].filter(Boolean).join(" · ");
  // Notes of the releases an upgrade crosses (after the installed one, up to the target), oldest first.
  const toIndex = versions.findIndex((version) => version.version === target);
  const fromIndex = versions.findIndex((version) => version.version === installed);
  const crossed = plan.downgrade || toIndex < 0 || fromIndex < 0 ? []
    : versions.slice(toIndex, fromIndex).filter((version) => version.changelog).reverse();
  return (
    <section aria-label={t("page.blueprints.upgrade_version_title")} className="rounded-xl bg-stone-50 p-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h3 className="font-semibold">{t("page.blueprints.upgrade_version_title")}</h3>
          <p className="mt-1 text-xs text-stone-500">
            <span className="font-mono">{installed || "—"}</span> → <span className="font-mono">{target}</span>
          </p>
        </div>
        <Select value={target} disabled={disabled}
          onChange={(value) => onChange(value === latest ? undefined : value)}
          options={versions.map((version) => ({ value: version.version, label: label(version) }))}
          ariaLabel={t("page.blueprints.upgrade_version_title")}
          style={{ width: "min(100%, 260px)" }} />
      </div>
      {change && <div className="mt-2 space-y-1 text-xs leading-relaxed text-stone-600">
        <p>{t(plan.downgrade ? "page.blueprints.upgrade_version_downgrade" : `page.blueprints.upgrade_version_${change.min_bump}`)}</p>
        {change.reasons.length > 0 && <ul className="list-disc space-y-0.5 pl-4">
          {change.reasons.slice(0, 8).map((reason) => <li key={reason} className="break-words">{reason}</li>)}
        </ul>}
      </div>}
      {crossed.length > 0 && <details className="mt-2">
        <summary className="cursor-pointer text-xs font-semibold text-stone-700">{t("page.blueprints.upgrade_version_notes")}</summary>
        <ul className="mt-2 space-y-2 text-xs leading-relaxed text-stone-600">
          {crossed.map((version) => <li key={version.version}>
            <span className="font-mono font-semibold">{version.version}</span> — <span className="whitespace-pre-wrap break-words">{version.changelog}</span>
          </li>)}
        </ul>
      </details>}
    </section>
  );
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
    /** Set when the review moved to a release other than the latest. */
    version?: { to: string; latest: string };
  } | null>(null);
  const [conflictChoices, setConflictChoices] = useState<Record<string, ConflictChoice>>({});
  const [variableValues, setVariableValues] = useState<Record<string, string>>({});
  const [channelConfigIds, setChannelConfigIds] = useState<Record<string, string>>({});
  // A listed release to move to, newer or older; undefined = the latest.
  const [targetVersion, setTargetVersion] = useState<string | undefined>(undefined);

  const basePlanQuery = useQuery({
    queryKey: ["blueprint-upgrade-plan", workspaceId, targetVersion],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId, undefined, undefined, targetVersion),
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
    queryKey: ["blueprint-upgrade-plan", workspaceId, targetVersion, variableOverrides, channelConfigIds],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId, variableOverrides, channelConfigIds, targetVersion),
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
        ...(targetVersion ? { target_version: targetVersion } : {}),
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
      const to = plan.target_version;
      const latest = plan.latest_version;
      return { ...result, version: to && latest && to !== latest ? { to, latest } : undefined };
    },
    onSuccess: async (result) => {
      setApplied({
        updated: result.updated.length, kept: result.kept_yours.length,
        canRevert: result.can_revert, customized: result.synchronization_status === "customized",
        version: result.version,
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
    setTargetVersion(undefined);
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
          <p className="text-sm text-stone-600">{applied.version
            ? t("page.blueprints.upgrade_synchronized_to_version")
              .replace("{version}", applied.version.to).replace("{latest}", applied.version.latest)
            : t(applied.customized
              ? "page.blueprints.upgrade_customized_done" : "page.blueprints.upgrade_synchronized_done")}</p>
          {revertError}
        </div>
      ) : (
        <div className="space-y-4 text-sm">
          <p className="leading-relaxed text-stone-600">{t("page.blueprints.upgrade_in_place_help")
            .replace("{workspace}", workspaceName)}</p>
          {plan && <VersionChoice plan={plan} disabled={busy} onChange={setTargetVersion} />}
          {plan?.restore_point && <p className="text-xs text-stone-500">
            {t("page.blueprints.upgrade_restore_point")
              .replace("{date}", new Date(plan.restore_point.applied_at).toLocaleString(undefined, { timeZone: getPreferredTimeZone() }))
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
                const action = choice === "keep_yours"
                  ? item.action === "customized" ? "customized" : "keep_yours"
                  : item.action === "keep_yours" || item.action === "customized" ? "update" : item.action;
                const issues = choice === "keep_yours" ? item.compatibility_issues ?? [] : [];
                return <section key={item.key} aria-label={item.name} className="rounded-xl bg-stone-50 p-3">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <h3 className="break-words font-semibold text-stone-800">{item.name}</h3>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs text-stone-500">{t(`page.blueprints.upgrade_action_${action}`)}</span>
                        {issues.length > 0 && <StatusPill status="needs_setup" size="sm"
                          label={t("page.blueprints.upgrade_incompatible")} />}
                      </div>
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
                  {issues.length > 0 && <ul className="mt-2 space-y-1 text-xs leading-relaxed text-stone-700">
                    {issues.map((issue) => <li key={issue.code} className="break-words">{compatibilityText(issue)}</li>)}
                    <li className="text-stone-500">{t("page.blueprints.upgrade_incompatible_help")}</li>
                  </ul>}
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
