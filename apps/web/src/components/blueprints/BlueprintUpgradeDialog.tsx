/**
 * Confirming a blueprint upgrade.
 *
 * An upgrade overwrites content the workspace is running, so what it will do
 * is shown item by item before anything is written — including the items it
 * will NOT touch, because those are the ones an operator most needs to know
 * about. "Update available" alone is not something anyone can act on.
 *
 * Undo is offered afterwards rather than promised in advance: only what the
 * upgrade actually overwrote can be put back.
 */
import { Fragment, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import Modal from "../ui/Modal";
import Input from "../ui/Input";
import LoadingSpinner from "../ui/LoadingSpinner";
import Select from "../ui/Select";
import { api } from "../../lib/api";
import {
  formatBlueprintVariableValue,
  parseBlueprintVariableValue,
} from "../../lib/blueprintVariables.mjs";
import { t } from "../../lib/i18n";

const ACTION_TONE: Record<string, { fg: string; bg: string }> = {
  update: { fg: "#3f6f68", bg: "#eaf1ef" },
  keep_yours: { fg: "#8a6d3b", bg: "#f6efe3" },
  unchanged: { fg: "#78716c", bg: "#f5f5f4" },
  missing: { fg: "#78716c", bg: "#f5f5f4" },
  baseline_unknown: { fg: "#8a6d3b", bg: "#f6efe3" },
  reconfigure: { fg: "#8a6d3b", bg: "#f6efe3" },
};

type ConflictChoice = "keep_yours" | "use_blueprint";

function upgradeItemKey(item: { kind: string; slug: string }) {
  return `${item.kind}:${item.slug}`;
}

export interface BlueprintUpgradeDialogProps {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  workspaceName: string;
}

export default function BlueprintUpgradeDialog({
  open,
  onClose,
  workspaceId,
  workspaceName,
}: BlueprintUpgradeDialogProps) {
  const queryClient = useQueryClient();
  const [applied, setApplied] = useState<{
    updated: number;
    kept: number;
    canRevert: boolean;
    versionOnly: boolean;
    partial: boolean;
  } | null>(null);
  const [conflictChoices, setConflictChoices] = useState<Record<string, ConflictChoice>>({});
  const [variableValues, setVariableValues] = useState<Record<string, string>>({});
  const [channelConfigIds, setChannelConfigIds] = useState<Record<string, string>>({});

  const basePlanQuery = useQuery({
    queryKey: ["blueprint-upgrade-plan", workspaceId],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId),
    enabled: open,
  });
  const installVariables = [
    ...(basePlanQuery.data?.setup_preview.required_variables ?? []),
    ...(basePlanQuery.data?.setup_preview.optional_variables ?? []),
  ].filter((variable) => variable.key);
  const variableByKey = new Map(
    installVariables.map((variable) => [variable.key as string, variable]),
  );
  const variableOverrides: Record<string, unknown> = {};
  const invalidVariableKeys = new Set<string>();
  for (const [key, input] of Object.entries(variableValues)) {
    const parsed = parseBlueprintVariableValue(
      input,
      variableByKey.get(key)?.default,
    );
    if (parsed.ok) variableOverrides[key] = parsed.value;
    else invalidVariableKeys.add(key);
  }
  const hasVariableOverrides = Object.keys(variableValues).length > 0;
  const hasChannelSelections = Object.keys(channelConfigIds).length > 0;
  const overridePlanQuery = useQuery({
    queryKey: ["blueprint-upgrade-plan", workspaceId, variableOverrides, channelConfigIds],
    queryFn: () => api.workspaces.blueprintUpgradePlan(
      workspaceId,
      variableOverrides,
      channelConfigIds,
    ),
    enabled: open && (hasVariableOverrides || hasChannelSelections) && invalidVariableKeys.size === 0,
  });
  const plan = (
    (hasVariableOverrides || hasChannelSelections) && invalidVariableKeys.size === 0
      ? overridePlanQuery.data
      : undefined
  ) ?? basePlanQuery.data;
  const resolvedVariableKeys = new Set(
    basePlanQuery.data?.resolved_variable_keys ?? [],
  );
  const missingRequiredVariable = (
    basePlanQuery.data?.setup_preview.required_variables ?? []
  ).some((variable) => {
    if (!variable.key) return false;
    if (Object.hasOwn(variableValues, variable.key)) {
      return !variableValues[variable.key].trim();
    }
    return !resolvedVariableKeys.has(variable.key)
      && !formatBlueprintVariableValue(variable.default).trim();
  });
  const variablePlanPending = (
    hasVariableOverrides
    && invalidVariableKeys.size === 0
    && overridePlanQuery.isFetching
  );
  const isLoading = basePlanQuery.isLoading;
  const isError = basePlanQuery.isError;
  const error = basePlanQuery.error;
  const refetchPlan = () => {
    void basePlanQuery.refetch();
    if ((hasVariableOverrides || hasChannelSelections) && invalidVariableKeys.size === 0) {
      void overridePlanQuery.refetch();
    }
  };

  const invalidate = () => {
    // These projections have independent query keys; refreshing the Workspace
    // alone leaves Channel bindings and setup/Agent configuration stale.
    return Promise.all([
      queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
      queryClient.invalidateQueries({ queryKey: ["entity-agents-for-mapping"] }),
      queryClient.invalidateQueries({ queryKey: ["agents"] }),
      queryClient.invalidateQueries({ queryKey: ["skills"] }),
      queryClient.invalidateQueries({ queryKey: ["workflows"] }),
      ...[
        "workspace", "blueprint-upgrade-plan", "workspace-channels",
        "workspace-available-channels", "workspace-agents", "workspace-setup-status",
        "workspace-capabilities", "workspace-operating-model", "workspace-dashboard",
        "workspace-documents", "workflow-bindings", "workspace-automation-bindings-summary",
      ].map((key) => queryClient.invalidateQueries({ queryKey: [key, workspaceId] })),
    ]);
  };

  const applyMutation = useMutation({
    mutationFn: async () => {
      if (!plan?.blueprint_fingerprint) {
        throw new Error(t("page.blueprints.upgrade_plan_changed"));
      }
      const result = await api.workspaces.applyBlueprintUpgrade(workspaceId, {
        expected_blueprint_fingerprint: plan.blueprint_fingerprint,
        variable_values: variableOverrides,
        channel_config_ids: resolvedChannelConfigIds,
        conflict_resolutions: keptYours.map((item) => {
          if (
            (item.kind !== "agent" && item.kind !== "skill" && item.kind !== "workflow")
            || typeof item.revision !== "number"
          ) {
            throw new Error(t("page.blueprints.upgrade_plan_changed"));
          }
          return {
            kind: item.kind,
            slug: item.slug,
            resolution: conflictChoices[upgradeItemKey(item)] ?? "keep_yours",
            expected_revision: item.revision,
          };
        }),
      });
      if (result.upgrade_protocol_version !== 2) {
        throw new Error(t("page.blueprints.upgrade_plan_changed"));
      }
      return result;
    },
    onSuccess: async (result) => {
      setApplied({
        updated: result.updated.length,
        kept: result.kept_yours.length,
        canRevert: result.can_revert,
        versionOnly: result.updated.length === 0 && result.kept_yours.length === 0,
        partial: !result.fully_synchronized,
      });
      await invalidate();
    },
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ["blueprint-upgrade-plan", workspaceId] });
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

  const planItems = plan?.items;
  const items = planItems ?? [];
  const toUpdate = items.filter((item) => item.action === "update");
  const keptYours = items.filter((item) => item.action === "keep_yours");
  const missing = items.filter((item) => item.action === "missing");
  const installableMissing = missing.filter((item) => item.kind === "workflow");
  const unavailableMissing = missing.filter((item) => item.kind !== "workflow");
  const visibleItems = items.filter((item) => item.action !== "unchanged");
  const useBlueprintConflicts = keptYours.filter(
    (item) => conflictChoices[upgradeItemKey(item)] === "use_blueprint",
  );
  const conflictsToKeep = keptYours.filter(
    (item) => conflictChoices[upgradeItemKey(item)] !== "use_blueprint",
  );
  const selectedUpdateCount =
    toUpdate.length + useBlueprintConflicts.length + installableMissing.length;
  const keepYoursOnlySync =
    selectedUpdateCount === 0 &&
    conflictsToKeep.length > 0 &&
    unavailableMissing.length === 0;
  const hasCurrentPlan =
    plan?.upgrade_protocol_version === 2 &&
    Boolean(plan.blueprint_fingerprint) &&
    keptYours.every(
      (item) => item.kind !== "knowledge_document" && typeof item.revision === "number",
    );
  const versionOnlySync = hasCurrentPlan && visibleItems.length === 0;
  const resolvedChannelConfigIds = { ...channelConfigIds };
  for (const requirement of plan?.setup_preflight.requirements ?? []) {
    if (
      requirement.kind === "channel"
      && requirement.requirement_key
      && requirement.resource_id
      && !resolvedChannelConfigIds[requirement.requirement_key]
    ) {
      resolvedChannelConfigIds[requirement.requirement_key] = requirement.resource_id;
    }
  }
  const hasBlockingSetupRequirement = (plan?.setup_preflight.requirements ?? []).some(
    (requirement) => requirement.required && !requirement.ready && !(
      requirement.kind === "channel"
      && requirement.requirement_key
      && Boolean(resolvedChannelConfigIds[requirement.requirement_key])
    ),
  );
  const channelPlanPending =
    hasChannelSelections &&
    invalidVariableKeys.size === 0 &&
    overridePlanQuery.isFetching;
  const canApply =
    unavailableMissing.length === 0 &&
    plan?.variables_ready === true &&
    !missingRequiredVariable &&
    invalidVariableKeys.size === 0 &&
    !variablePlanPending &&
    !channelPlanPending &&
    !((hasVariableOverrides || hasChannelSelections) && overridePlanQuery.isError) &&
    !hasBlockingSetupRequirement &&
    (selectedUpdateCount > 0 || keepYoursOnlySync || versionOnlySync);
  const busy = applyMutation.isPending || revertMutation.isPending;
  const canRevert = applied?.canRevert ?? plan?.can_revert ?? false;
  const revertButton = canRevert ? (
    <button
      onClick={() => revertMutation.mutate()}
      disabled={busy}
      style={secondaryButton}
    >
      {t("page.blueprints.upgrade_revert")}
    </button>
  ) : null;
  const revertError = revertMutation.isError ? (
    <p role="alert" style={{ fontSize: 12, color: "var(--danger, #b42318)", lineHeight: 1.55, margin: 0 }}>
      {revertMutation.error instanceof Error && revertMutation.error.message
        ? revertMutation.error.message
        : t("page.blueprints.upgrade_plan_changed")}
    </p>
  ) : null;

  useEffect(() => {
    if (!open) return;
    setApplied(null);
    setVariableValues({});
    setChannelConfigIds({});
  }, [open, workspaceId]);

  useEffect(() => {
    if (!open) return;
    const currentConflicts = (planItems ?? []).filter(
      (item) => item.action === "keep_yours",
    );
    setConflictChoices(Object.fromEntries(
      currentConflicts.map((item) => [upgradeItemKey(item), "keep_yours"]),
    ));
  }, [open, workspaceId, plan?.blueprint_fingerprint, planItems]);

  return (
    <Modal
      open={open}
      onClose={busy ? () => {} : onClose}
      title={t("page.blueprints.upgrade_title").replace("{workspace}", workspaceName)}
    >
      {isLoading ? (
        <LoadingSpinner />
      ) : isError ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <p style={{ fontSize: 13, color: "#b42318", lineHeight: 1.6, margin: 0 }}>
            {t("page.blueprints.upgrade_load_failed")}
          </p>
          {error instanceof Error && error.message ? (
            <p style={{ fontSize: 11.5, color: "#78716c", lineHeight: 1.5, margin: 0 }}>
              {error.message}
            </p>
          ) : null}
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button onClick={onClose} style={secondaryButton}>
              {t("action.close")}
            </button>
          </div>
        </div>
      ) : applied ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <p style={{ fontSize: 13, color: "#44403c", lineHeight: 1.6, margin: 0 }}>
            {applied.versionOnly
              ? t("page.blueprints.upgrade_version_confirmed")
              : t("page.blueprints.upgrade_done")
                .replace("{updated}", String(applied.updated))
                .replace("{kept}", String(applied.kept))}
          </p>
          {applied.partial && (
            <p style={{ fontSize: 12, color: "#8a6d3b", lineHeight: 1.55, margin: 0 }}>
              {t("page.blueprints.upgrade_partial_done")}
            </p>
          )}
          {revertError}
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            {revertButton}
            <button onClick={onClose} disabled={busy} style={primaryButton}>
              {t("action.done")}
            </button>
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {installVariables.length > 0 && (
            <div style={{ padding: 12, borderRadius: 10, background: "var(--surface-muted)" }}>
              <h4 style={{ fontSize: 13, fontWeight: 700, margin: 0, color: "var(--text-strong)" }}>
                {t("component.install_blueprint_modal.workspace_settings")}
              </h4>
              <p style={{ color: "var(--text-muted)", fontSize: 11.5, lineHeight: 1.5, margin: "3px 0 10px" }}>
                {t("component.install_blueprint_modal.workspace_settings_desc")}
              </p>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))", gap: 10 }}>
                {installVariables.map((variable) => (
                  <div key={variable.key}>
                    <Input
                      label={variable.label}
                      value={variableValues[variable.key as string] ?? ""}
                      onChange={(event) => setVariableValues((current) => ({
                        ...current,
                        [variable.key as string]: event.target.value,
                      }))}
                      placeholder={
                        resolvedVariableKeys.has(variable.key as string)
                          ? t("page.blueprints.upgrade_existing_value")
                          : formatBlueprintVariableValue(variable.default) || undefined
                      }
                      required={variable.required}
                      error={
                        variable.required && missingRequiredVariable && (
                          Object.hasOwn(variableValues, variable.key as string)
                            ? !variableValues[variable.key as string]?.trim()
                            : !resolvedVariableKeys.has(variable.key as string)
                              && !formatBlueprintVariableValue(variable.default).trim()
                        )
                          ? t("component.install_blueprint_modal.required_value")
                          : invalidVariableKeys.has(variable.key as string)
                            ? t("component.install_blueprint_modal.invalid_typed_value")
                            : undefined
                      }
                    />
                    {variable.purpose && (
                      <div style={{ color: "var(--text-muted)", fontSize: 11, lineHeight: 1.45, marginTop: 3 }}>
                        {variable.purpose}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              {variablePlanPending && (
                <div aria-live="polite" style={{ color: "var(--text-muted)", fontSize: 11, marginTop: 8 }}>
                  {t("page.blueprints.upgrade_refreshing_preview")}
                </div>
              )}
              {overridePlanQuery.isError && (
                <div role="alert" style={{ color: "rgb(193, 74, 68)", fontSize: 11, marginTop: 8 }}>
                  {t("page.blueprints.upgrade_variable_preview_failed")}
                </div>
              )}
            </div>
          )}

          {items.length === 0 ? (
            <p style={{ fontSize: 13, color: "#78716c", margin: 0 }}>
              {t("page.blueprints.upgrade_nothing")}
            </p>
          ) : versionOnlySync ? (
            <p style={{ fontSize: 13, color: "#57534e", lineHeight: 1.6, margin: 0 }}>
              {t("page.blueprints.upgrade_already_matches")}
            </p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {keptYours.length > 0 && (
                <p style={{ fontSize: 12, color: "#57534e", lineHeight: 1.55, margin: "0 0 2px" }}>
                  {t("page.blueprints.upgrade_conflicts_help")}
                </p>
              )}
              {visibleItems.map((item) => {
                  const itemKey = upgradeItemKey(item);
                  const conflictChoice = conflictChoices[itemKey] ?? "keep_yours";
                  const displayedAction = (
                    item.action === "keep_yours" && conflictChoice === "use_blueprint"
                      ? "update"
                      : item.action
                  );
                  const tone = ACTION_TONE[displayedAction] || ACTION_TONE.unchanged;
                  return (
                    <div
                      key={itemKey}
                      style={{
                        display: "flex", gap: 10, alignItems: "flex-start",
                        padding: "10px 12px", borderRadius: 12, background: "rgba(250,250,249,0.7)",
                      }}
                    >
                      <span style={{
                        flexShrink: 0, fontSize: 9.5, fontWeight: 850, letterSpacing: "0.04em",
                        textTransform: "uppercase", color: tone.fg, background: tone.bg,
                        borderRadius: 999, padding: "3px 8px", marginTop: 1,
                      }}>
                        {t(`page.blueprints.upgrade_action_${displayedAction}`)}
                      </span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 700, color: "#292524" }}>
                          {item.name}
                          <span style={{ color: "#a8a29e", fontWeight: 600 }}> · {item.kind}</span>
                        </div>
                        {item.changes.length > 0 && (
                          <div style={{ fontSize: 11.5, color: "#78716c", marginTop: 2 }}>
                            {item.changes.join(" · ")}
                          </div>
                        )}
                        {item.action === "keep_yours" && (
                          <Select
                            value={conflictChoice}
                            onChange={(value) => setConflictChoices((current) => ({
                              ...current,
                              [itemKey]: value as ConflictChoice,
                            }))}
                            options={[
                              {
                                value: "keep_yours",
                                label: t("page.blueprints.upgrade_resolution_keep_yours"),
                              },
                              {
                                value: "use_blueprint",
                                label: t("page.blueprints.upgrade_resolution_use_blueprint"),
                              },
                            ]}
                            ariaLabel={t("page.blueprints.upgrade_resolution_label").replace(
                              "{item}", item.name,
                            )}
                            style={{ width: "min(100%, 230px)", marginTop: 8 }}
                            buttonStyle={{ height: 32, fontSize: 11.5 }}
                          />
                        )}
                        {Object.keys(item.new_content || {}).length > 0 && (
                          <details style={{ marginTop: 6 }}>
                            <summary style={{
                              cursor: "pointer", fontSize: 11.5, fontWeight: 700,
                              color: "#3f6f68", listStyle: "none",
                            }}>
                              {t("page.blueprints.upgrade_show_new")}
                            </summary>
                            {Object.entries(item.new_content).map(([field, value]) => (
                              <Fragment key={field}>
                                <div style={{
                                  fontSize: 10, fontWeight: 800, color: "#a8a29e",
                                  textTransform: "uppercase", letterSpacing: "0.05em",
                                  margin: "8px 0 3px",
                                }}>
                                  {field}
                                </div>
                                <pre style={{
                                  margin: 0, padding: "8px 10px", borderRadius: 9,
                                  background: "rgba(28,25,23,0.035)", color: "#44403c",
                                  fontSize: 11, lineHeight: 1.55, maxHeight: 220,
                                  overflow: "auto", whiteSpace: "pre-wrap",
                                  wordBreak: "break-word",
                                }}>
                                  {value}
                                </pre>
                              </Fragment>
                            ))}
                          </details>
                        )}
                      </div>
                    </div>
                  );
                })}
            </div>
          )}

          {conflictsToKeep.length > 0 && (
            <p style={{ fontSize: 12, color: "#8a6d3b", lineHeight: 1.55, margin: 0 }}>
              {t("page.blueprints.upgrade_keeps_yours").replace(
                "{count}", String(conflictsToKeep.length),
              )}
            </p>
          )}

          {plan?.setup_preflight.ready === false && (
            <div style={{ padding: 10, borderRadius: 10, background: "var(--surface-muted)" }}>
              <p style={{ fontSize: 12, color: "var(--text-default)", lineHeight: 1.55, margin: 0 }}>
                {t("page.blueprints.upgrade_connections_required")}
              </p>
              <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
                {plan.setup_preflight.requirements
                  .filter((requirement) => requirement.required && !requirement.ready)
                  .map((requirement) => {
                    const selectedChannelId = requirement.requirement_key
                      ? resolvedChannelConfigIds[requirement.requirement_key] || ""
                      : "";
                    return (
                      <div
                        key={`${requirement.kind}:${requirement.provider}:${requirement.label}`}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: 10,
                        }}
                      >
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontSize: 12, color: "var(--text-strong)", fontWeight: 700 }}>
                            {requirement.label}
                          </div>
                          <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginTop: 2 }}>
                            {requirement.purpose || requirement.reason}
                          </div>
                        </div>
                        {requirement.kind === "channel"
                          && requirement.requirement_key
                          && requirement.resource_options.length > 0 ? (
                          <Select
                            value={selectedChannelId}
                            onChange={(value) => setChannelConfigIds((current) => ({
                              ...current,
                              [requirement.requirement_key as string]: value,
                            }))}
                            options={requirement.resource_options.map((option) => ({
                              value: option.id,
                              label: option.label,
                            }))}
                            placeholder={t("page.blueprints.setup_required_short")}
                            ariaLabel={requirement.label}
                            style={{ width: 190 }}
                          />
                        ) : null}
                      </div>
                    );
                  })}
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
                <button
                  onClick={() => window.open("/integrations", "_blank", "noopener,noreferrer")}
                  style={secondaryButton}
                >
                  {t("page.blueprints.open_integrations")}
                </button>
                <button onClick={() => refetchPlan()} style={secondaryButton}>
                  {t("page.blueprints.refresh_status")}
                </button>
              </div>
            </div>
          )}

          {applyMutation.isError && (
            <p role="alert" style={{ fontSize: 12, color: "#b42318", lineHeight: 1.55, margin: 0 }}>
              {applyMutation.error instanceof Error && applyMutation.error.message
                ? applyMutation.error.message
                : t("page.blueprints.upgrade_apply_failed")}
            </p>
          )}

          {unavailableMissing.length > 0 && (
            <p style={{ fontSize: 12, color: "#78716c", lineHeight: 1.55, margin: 0 }}>
              {t("page.blueprints.upgrade_missing_items").replace(
                "{count}", String(unavailableMissing.length),
              )}
            </p>
          )}

          {revertError}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "flex-end" }}>
            <button onClick={onClose} disabled={busy} style={secondaryButton}>
              {t("action.cancel")}
            </button>
            {revertButton}
            <button
              onClick={() => applyMutation.mutate()}
              disabled={busy || !canApply || !hasCurrentPlan}
              style={{ ...primaryButton, opacity: canApply && hasCurrentPlan ? 1 : 0.5 }}
            >
              {selectedUpdateCount > 0
                ? t("page.blueprints.upgrade_confirm").replace(
                  "{count}", String(selectedUpdateCount),
                )
                : keepYoursOnlySync
                  ? t("page.blueprints.upgrade_confirm_keep_yours")
                  : versionOnlySync
                    ? t("page.blueprints.upgrade_confirm_current")
                    : t("page.blueprints.upgrade_no_automatic_updates")}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

const primaryButton: React.CSSProperties = {
  height: 32, padding: "0 14px", borderRadius: 10, border: "none",
  background: "#4f7d75", color: "#fff", fontSize: 12.5, fontWeight: 800, cursor: "pointer",
};

const secondaryButton: React.CSSProperties = {
  height: 32, padding: "0 14px", borderRadius: 10, border: "none",
  background: "#f5f5f4", color: "#57534e",
  fontSize: 12.5, fontWeight: 800, cursor: "pointer",
};
