import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type BlueprintInstallPreflightRequirement, type WorkspaceDraft } from "../lib/api";
import { t } from "../lib/i18n";
import { resolveBlueprintChannelSelections } from "../lib/blueprintSetup.mjs";
import {
  formatBlueprintVariableValue,
  parseBlueprintVariableValue,
} from "../lib/blueprintVariables.mjs";
import { useToastStore } from "../stores/toast";
import Button from "./ui/Button";
import Input from "./ui/Input";
import LoadingSpinner from "./ui/LoadingSpinner";
import Select from "./ui/Select";
import Toggle from "./ui/Toggle";

type DraftFields = Record<string, any>;

export interface WorkspaceDraftConfigurationPanelProps {
  draftId: string;
  draft?: WorkspaceDraft | null;
  refreshKey?: string;
  creating?: boolean;
  updating?: boolean;
  onCreate?: () => Promise<void>;
  onDraftChange?: (draft: WorkspaceDraft) => void;
}

function humanize(value: unknown) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function labelFor(item: any, fallback: string) {
  return String(
    item?.name || item?.title || item?.description || item?.service_key ||
      item?.metric_key || item?.goal_key || item?.rule_key || item?.automation_key || fallback,
  );
}

function personalizationValuesEqual(
  left: Record<string, string>,
  right: Record<string, string>,
): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  return [...keys].every((key) => (left[key] ?? "") === (right[key] ?? ""));
}

function compactParts(values: unknown[]) {
  return values
    .flatMap((value) => Array.isArray(value) ? value : [value])
    .map((value) => String(value ?? "").trim())
    .filter(Boolean)
    .join(" · ");
}

function asList<T = any>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[];
  return value == null || value === "" ? [] : [value as T];
}

type DetailField = { label: string; value: string };
type DetailItem = { primary: string; meta?: string; details?: DetailField[] };

function detailItem(
  primary: unknown,
  meta: unknown[] = [],
  details: Array<{ label: string; value: unknown }> = [],
): DetailItem {
  return {
    primary: String(primary || "—"),
    meta: compactParts(meta) || undefined,
    details: details.flatMap(({ label, value }) => {
      const normalized = compactParts(asList(value).map(bindingLabel));
      return normalized ? [{ label, value: normalized }] : [];
    }),
  };
}

function bindingLabel(value: any) {
  if (value == null) return "";
  if (typeof value !== "object") return String(value);
  return String(
    value.name || value.title || value.slug || value.tool_name ||
      value.server_slug || value.capability || value.skill_slug || value.description || "",
  );
}

function labeledValue(label: string, value: unknown) {
  return value == null || value === "" ? "" : `${label}: ${String(value)}`;
}

function channelDetails(fields: DraftFields): DetailItem[] {
  const config = fields.channel_config || {};
  const values = [
    ["primary_external", config.primary_external_channel],
    ["internal", config.internal_channel],
    ...asList(config.secondary_external_channels).map((item: any) => ["secondary_external", item]),
    ...asList(config.channels).map((item: any) => [item?.role || "channel", item]),
  ] as Array<[string, any]>;
  const seen = new Set<string>();
  return values.flatMap(([fallbackRole, item]) => {
    if (!item) return [];
    const channelType = humanize(item?.channel_type || item?.type || item);
    const role = humanize(item?.role || fallbackRole);
    const signature = compactParts([role, channelType, item?.purpose]);
    if (!channelType || seen.has(signature)) return [];
    seen.add(signature);
    return [detailItem(channelType, [
      role,
      item?.purpose,
      humanize(item?.linked_service_key || item?.service_key),
      item?.login_required ? t("page.workspace_draft_chat.value.login_required") : "",
    ])];
  });
}

function channelNames(fields: DraftFields) {
  const config = fields.channel_config || {};
  const values = [
    config.primary_external_channel,
    config.internal_channel,
    ...asList(config.secondary_external_channels),
    ...asList(config.channels),
  ];
  return Array.from(new Set(values
    .map((item: any) => humanize(item?.channel_type || item?.type || item))
    .filter(Boolean)));
}

function completeness(draft: WorkspaceDraft) {
  if (draft.status === "finalized" || draft.ready) return 100;
  const fields = draft.fields as DraftFields;
  const services = asList(fields.services);
  const mappings = asList(fields.agent_mappings);
  const checks = [
    Boolean(fields.name),
    Boolean(fields.kind),
    Boolean(fields.operating_context),
    Boolean(fields.primary_work),
    services.length > 0,
    services.length > 0 && services.every((service) => {
      const key = service?.service_key || service?.key;
      return mappings.some((mapping) =>
        mapping?.service_key === key &&
        Boolean(mapping?.agent_id || mapping?.recommended_agent_id || mapping?.strategy === "create_custom"));
    }),
    channelNames(fields).length > 0,
  ];
  return Math.min(94, Math.round((checks.filter(Boolean).length / checks.length) * 100));
}

function Tags({ values }: { values: string[] }) {
  if (!values.length) return <span className="workspace-draft-empty">—</span>;
  return (
    <div className="workspace-draft-tags">
      {values.map((value, index) => (
        <span className="workspace-draft-tag" key={`${value}-${index}`}>{value}</span>
      ))}
    </div>
  );
}

function DetailList({ values }: { values: DetailItem[] }) {
  if (!values.length) return <span className="workspace-draft-empty">—</span>;
  return (
    <ul className="workspace-draft-list workspace-draft-list--details">
      {values.map((value, index) => (
        <li key={`${value.primary}-${index}`}>
          <strong>{value.primary}</strong>
          {value.meta && <small>{value.meta}</small>}
          {value.details && value.details.length > 0 && (
            <dl className="workspace-draft-list__configuration">
              {value.details.map((detail) => (
                <div key={`${detail.label}-${detail.value}`}>
                  <dt>{detail.label}</dt>
                  <dd>{detail.value}</dd>
                </div>
              ))}
            </dl>
          )}
        </li>
      ))}
    </ul>
  );
}

export default function WorkspaceDraftConfigurationPanel({
  draftId,
  draft: controlledDraft,
  refreshKey,
  creating = false,
  updating = false,
  onCreate,
  onDraftChange,
}: WorkspaceDraftConfigurationPanelProps) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToastStore();
  const operationRef = useRef<"blueprint" | "create" | null>(null);
  const previousUpdatingRef = useRef(updating);
  const panelBodyRef = useRef<HTMLDivElement>(null);
  const [operation, setOperation] = useState<"blueprint" | "create" | null>(null);
  const [personalizationValues, setPersonalizationValues] = useState<Record<string, string>>({});
  const query = useQuery({
    queryKey: ["workspace-draft", draftId],
    queryFn: () => api.workspaceDrafts.get(draftId),
    enabled: Boolean(draftId) && !controlledDraft,
    refetchOnMount: "always",
  });
  const settledRefreshRequired = Boolean(
    !controlledDraft && draftId && previousUpdatingRef.current && !updating,
  );

  useEffect(() => {
    if (panelBodyRef.current) panelBodyRef.current.scrollTop = 0;
  }, [draftId]);

  useEffect(() => {
    if (!controlledDraft && draftId && refreshKey) {
      void query.refetch();
    }
  }, [controlledDraft, draftId, query.refetch, refreshKey]);

  useEffect(() => {
    previousUpdatingRef.current = updating;
    if (settledRefreshRequired) {
      void query.refetch();
    }
  }, [query.refetch, settledRefreshRequired, updating]);

  const finalizeMutation = useMutation({
    mutationFn: () => api.workspaceDrafts.finalize(draftId),
    onSuccess: (result) => {
      queryClient.setQueryData(["workspace-draft", draftId], result.draft);
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      onDraftChange?.(result.draft);
      toast.success(t("page.workspace_draft_chat.workspace_created"));
    },
    onError: (error: Error) => {
      toast.error(t("page.workspace_draft_chat.could_not_create_workspace"), error.message);
      void api.workspaceDrafts.get(draftId).then((updated) => {
        queryClient.setQueryData(["workspace-draft", draftId], updated);
        onDraftChange?.(updated);
      }).catch(() => {
        // Keep the finalize error as the actionable message. A later query
        // refresh can retry if this background state read also fails.
      });
    },
  });

  const applyMutation = useMutation({
    mutationFn: (blueprintId: string) => api.workspaceDrafts.applyBlueprint(draftId, blueprintId),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workspace-draft", draftId], updated);
      onDraftChange?.(updated);
      toast.success(t("page.workspace_draft_chat.blueprint_applied"));
    },
    onError: (error: Error) => {
      toast.error(t("page.workspace_draft_chat.could_not_apply_blueprint"), error.message);
    },
  });

  const autonomyMutation = useMutation({
    mutationFn: (enabled: boolean) => api.workspaceDrafts.updateFields(draftId, {
      heartbeat_enabled: enabled,
    }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workspace-draft", draftId], updated);
      onDraftChange?.(updated);
      toast.success(
        updated.fields.heartbeat_enabled
          ? t("page.workspace_detail.workspace_runtime_enabled_toast")
          : t("page.workspace_detail.workspace_runtime_disabled_toast"),
      );
    },
    onError: (error: Error) => {
      toast.error(t("page.workspace_detail.could_not_update_workspace"), error.message);
    },
  });

  const draft = controlledDraft || query.data;
  const measurementLibrary = useQuery({
    queryKey: ["workspace-stat-library"],
    queryFn: () => api.workspaces.stats.library(),
    enabled: Boolean(draft && (
      asList(draft.fields.goals).some((goal) => goal.measurement?.library_key)
      || asList(draft.fields.stats).some((stat) => stat.library_key)
    )),
  });
  const channelRequirements = asList<BlueprintInstallPreflightRequirement>(
    draft?.fields._blueprint_channel_requirements,
  ).filter((requirement) => (requirement.resource_options ?? []).length > 0);
  const channelMutation = useMutation({
    mutationFn: ({ key, id }: { key: string; id: string }) => api.workspaceDrafts.updateFields(draftId, {
      blueprint_channel_config_ids: {
        ...resolveBlueprintChannelSelections(channelRequirements),
        [key]: id,
      },
    }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workspace-draft", draftId], updated);
      onDraftChange?.(updated);
    },
    onError: (error: Error) => {
      toast.error(t("page.workspace_detail.could_not_update_workspace"), error.message);
    },
  });
  const personalizationSchema = asList<Record<string, any>>(
    (draft?.fields as DraftFields | undefined)?._blueprint_variable_declarations,
  );
  const storedPersonalization = (
    (draft?.fields as DraftFields | undefined)?.blueprint_personalization || {}
  ) as Record<string, unknown>;
  const storedPersonalizationValues = Object.fromEntries(
    personalizationSchema.map((field) => {
      const key = String(field.key || "");
      return [key, formatBlueprintVariableValue(storedPersonalization[key])];
    }),
  );
  const personalizationContextKey = `${draft?.id || ""}:${draft?.applied_blueprint_id || ""}`;
  const storedPersonalizationSnapshot = JSON.stringify(storedPersonalizationValues);
  const personalizationBaselineRef = useRef<{
    contextKey: string;
    values: Record<string, string>;
  }>({ contextKey: "", values: {} });
  useEffect(() => {
    const previous = personalizationBaselineRef.current;
    const nextStored = JSON.parse(storedPersonalizationSnapshot) as Record<string, string>;
    setPersonalizationValues((current) => {
      const contextChanged = previous.contextKey !== personalizationContextKey;
      if (contextChanged) return nextStored;
      return Object.fromEntries(Object.keys(nextStored).map((key) => {
        const locallyEdited = (
          (current[key] ?? "") !== (previous.values[key] ?? "")
        );
        return [key, locallyEdited ? (current[key] ?? "") : nextStored[key]];
      }));
    });
    personalizationBaselineRef.current = {
      contextKey: personalizationContextKey,
      values: nextStored,
    };
  }, [personalizationContextKey, storedPersonalizationSnapshot]);

  const personalizationDirty = !personalizationValuesEqual(
    personalizationValues,
    storedPersonalizationValues,
  );

  const parsedPersonalization: Record<string, unknown> = {};
  const invalidPersonalizationKeys = new Set<string>();
  for (const field of personalizationSchema) {
    const key = String(field.key || "");
    const input = personalizationValues[key] ?? "";
    const parsed = parseBlueprintVariableValue(input, field.default);
    if (parsed.ok) parsedPersonalization[key] = parsed.value;
    else invalidPersonalizationKeys.add(key);
    if (field.required && !input.trim()) invalidPersonalizationKeys.add(key);
  }

  const personalizationMutation = useMutation({
    mutationFn: () => api.workspaceDrafts.updateFields(draftId, {
      blueprint_personalization: parsedPersonalization,
    }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workspace-draft", draftId], updated);
      onDraftChange?.(updated);
      const updatedStored = (
        (updated.fields as DraftFields).blueprint_personalization || {}
      ) as Record<string, unknown>;
      setPersonalizationValues(Object.fromEntries(
        personalizationSchema.map((field) => {
          const key = String(field.key || "");
          return [key, formatBlueprintVariableValue(updatedStored[key])];
        }),
      ));
      toast.success(t("page.workspace_draft_chat.personalization_saved"));
    },
    onError: (error: Error) => {
      toast.error(t("page.workspace_draft_chat.personalization_save_failed"), error.message);
    },
  });

  const view = useMemo(() => {
    if (!draft) return null;
    const fields = draft.fields as DraftFields;
    const services = asList(fields.services);
    const mappings = asList(fields.agent_mappings);
    const goals = asList(fields.goals);
    const staff = asList(fields.staff_assignments);
    const knowledge = asList(fields.knowledge_attachments);
    const rules = asList(fields.rules);
    const automations = asList(fields.automations);
    const integrations = [
      ...asList(fields.flagged_integrations),
      ...asList(fields.missing_integrations),
    ];
    const evaluation = fields.evaluation || fields.evaluation_config || {};
    const budget = fields.budget_policy || {};
    const budgetAmount = Number(budget.monthly_budget_credits || 0);
    const hasEffectiveBudgetCap = Number.isFinite(budgetAmount) && budgetAmount > 0;
    return {
      fields,
      services,
      mappings,
      goals,
      staff,
      knowledge,
      rules,
      automations,
      integrations,
      autonomousEnabled: Boolean(fields.heartbeat_enabled),
      serviceDetails: services.map((item, index) => detailItem(
        labelFor(item, `Service ${index + 1}`),
        [humanize(item.autonomy_level), item.owner_role, item.description],
      )),
      mappingDetails: mappings.map((item) => {
        const service = humanize(item.service_key) || t("page.workspace_draft_chat.unnamed_service");
        const customAgent = item.create_agent_draft || {};
        const agent = item.agent_name || item.recommended_agent_name || customAgent.agent_name || humanize(item.strategy) || t("page.workspace_draft_chat.unmapped");
        return detailItem(
          `${service} → ${agent}`,
          [humanize(item.strategy), item.rationale, customAgent.agent_description],
          [
            { label: t("page.workspace_draft_chat.agent_config.prompt"), value: customAgent.system_prompt },
            { label: t("page.workspace_draft_chat.agent_config.tools"), value: customAgent.tool_bindings },
            { label: t("page.workspace_draft_chat.agent_config.capabilities"), value: customAgent.business_capabilities },
            { label: t("page.workspace_draft_chat.agent_config.skills"), value: customAgent.skill_bindings },
            { label: t("page.workspace_draft_chat.agent_config.mcp"), value: customAgent.mcp_bindings },
            { label: t("page.workspace_draft_chat.agent_config.missing_skills"), value: customAgent.missing_skill_specs },
          ],
        );
      }),
      goalDetails: goals.map((item, index) => {
        const configured = item.measurement || asList(fields.stats).find(
          (stat) => (stat.key || stat.library_key) === item.stat_key,
        );
        const entry = measurementLibrary.data?.items.find((candidate) => candidate.key === configured?.library_key);
        const measurement = configured ? { ...entry, window: entry?.default_window, ...configured } : null;
        const automatic = Boolean(measurement?.library_key
          || (measurement?.collector_type && measurement.collector_type !== "manual"));
        return detailItem(
          labelFor(item, `Goal ${index + 1}`),
          [item.target ?? item.target_value, humanize(item.cadence || item.measurement_cadence || measurement?.collection_cadence || entry?.default_cadence)],
          [
            { label: t("page.workspace_stats.measurement"), value: measurement?.name || humanize(measurement?.key || measurement?.library_key || item.metric_key) },
            { label: t("page.workspace_draft_chat.measurement.definition"), value: measurement?.description },
            { label: t("page.workspace_draft_chat.measurement.source"), value: measurement?.source || measurement?.collector_config?.source || measurement?.library_key },
            { label: t("page.workspace_draft_chat.measurement.unit"), value: measurement?.unit },
            { label: t("page.workspace_draft_chat.measurement.window"), value: humanize(measurement?.window) },
            { label: t("page.workspace_draft_chat.measurement.collection"), value: !measurement
              ? t("page.workspace_draft_chat.measurement.missing")
              : t(automatic ? "page.workspace_draft_chat.measurement.automatic" : "page.workspace_draft_chat.measurement.manual") },
          ],
        );
      }),
      staffDetails: staff.map((item, index) => detailItem(
        item.staff_name || item.name || item.staff_id || `Member ${index + 1}`,
        [item.role, humanize(item.service_key), item.rationale],
        item.staff_name && item.staff_id
          ? [{ label: t("page.workspace_draft_chat.staff_id"), value: item.staff_id }]
          : [],
      )),
      knowledgeDetails: knowledge.map((item, index) => {
        const mode = String(item.mode || "create_new");
        const starterDocument = item.generate_starter_doc == null
          ? mode === "create_new"
          : Boolean(item.generate_starter_doc);
        return detailItem(
          labelFor(item, `Source ${index + 1}`),
          [item.purpose],
          [
            {
              label: t("page.workspace_draft_chat.field.status"),
              value: item.approved === false
                ? t("page.workspace_draft_chat.value.excluded")
                : t("page.workspace_draft_chat.value.included"),
            },
            { label: t("page.workspace_draft_chat.field.mode"), value: humanize(mode) },
            {
              label: t("page.workspace_draft_chat.field.linked_services"),
              value: asList(item.linked_service_keys).map(humanize),
            },
            {
              label: t("page.workspace_draft_chat.field.starter_document"),
              value: starterDocument
                ? t("page.workspace_draft_chat.value.enabled")
                : t("page.workspace_draft_chat.value.disabled"),
            },
          ],
        );
      }),
      ruleDetails: rules.map((item, index) => detailItem(
        labelFor(item, `Rule ${index + 1}`),
        [humanize(item.rule_type), humanize(item.severity), humanize(item.scope), asList(item.action_patterns).map(humanize)],
      )),
      automationDetails: automations.map((item, index) => detailItem(
        labelFor(item, `Automation ${index + 1}`),
        [
          humanize(item.trigger || item.schedule_kind),
          item.cron_expr,
          item.every_seconds ? `${item.every_seconds}s` : "",
          item.run_at,
          item.timezone,
          humanize(item.service_key),
        ],
      )),
      integrationDetails: integrations.map((item, index) => detailItem(
        humanize(item.provider || item.name || `Integration ${index + 1}`),
        [item.purpose, item.required === false ? t("page.workspace_draft_chat.value.optional") : t("page.workspace_draft_chat.value.required")],
      )),
      channelDetails: channelDetails(fields),
      budgetDetails: [
        detailItem(
          hasEffectiveBudgetCap
            ? t("page.workspace_draft_chat.credit_amount", { count: Math.round(budgetAmount).toLocaleString() })
            : t("page.workspace_draft_chat.no_monthly_credit_cap"),
          [
            hasEffectiveBudgetCap && budget.auto_pause_on_budget !== false
              ? t("page.workspace_draft_chat.auto_pause_on")
              : t("page.workspace_draft_chat.auto_pause_off"),
            budget.notes,
          ],
        ),
      ],
      evaluationDetails: Object.keys(evaluation).length ? [detailItem(
          evaluation.enabled === false
            ? t("page.workspace_draft_chat.value.disabled")
            : t("page.workspace_draft_chat.value.enabled"),
          [
            humanize(evaluation.cadence),
            labeledValue(t("page.workspace_draft_chat.target"), evaluation.target_score ?? evaluation.target),
            labeledValue(t("page.workspace_draft_chat.warning_score"), evaluation.warning_score ?? evaluation.warning_threshold),
            evaluation.notes,
          ],
        ), ...asList(evaluation.scorecard || evaluation.metrics).map((item: any, index: number) => detailItem(
          item.name || item.title || humanize(item.metric_key) || `Metric ${index + 1}`,
          [
            labeledValue(t("page.workspace_draft_chat.target"), item.target),
            labeledValue(t("page.workspace_draft_chat.weight"), item.weight),
            labeledValue(t("page.workspace_draft_chat.goal"), humanize(item.goal_key)),
            humanize(item.cadence),
          ],
        ))] : [],
      notes: String(fields.notes || "").trim(),
      percent: completeness(draft),
    };
  }, [draft, measurementLibrary.data]);

  if (!draft || !view) {
    return (
      <div className="workspace-draft-panel workspace-draft-panel--loading">
        {query.isError ? <p>{(query.error as Error).message}</p> : <LoadingSpinner size={22} />}
      </div>
    );
  }

  const finalized = draft.status === "finalized";
  const statusLabel = draft.status === "ready"
    ? t("page.workspace_draft_chat.value.ready")
    : draft.status === "finalized"
      ? t("page.workspace_draft_chat.value.created")
      : draft.status === "abandoned"
        ? t("page.workspace_draft_chat.value.abandoned")
        : t("page.workspace_draft_chat.value.drafting");
  const editable = draft.status === "active" || draft.status === "ready";
  const statusDescription = draft.status === "abandoned"
    ? t("page.workspace_draft_chat.draft_saved")
    : draft.ready
      ? t("page.workspace_draft_chat.workspace_ready")
      : t("page.workspace_draft_chat.keep_chatting_until_ready");
  const busy = creating || updating || settledRefreshRequired || query.isFetching || operation !== null || applyMutation.isPending || autonomyMutation.isPending || personalizationMutation.isPending || channelMutation.isPending || finalizeMutation.isPending;
  const applyBlueprint = async (blueprintId: string) => {
    if (!editable || busy || operationRef.current) return;
    operationRef.current = "blueprint";
    setOperation("blueprint");
    try {
      await applyMutation.mutateAsync(blueprintId);
    } catch {
      // The mutation's onError handler already presents the actionable error.
    } finally {
      operationRef.current = null;
      setOperation(null);
    }
  };
  const createWorkspace = async () => {
    if (
      !editable
      || busy
      || operationRef.current
      || invalidPersonalizationKeys.size > 0
    ) return;
    operationRef.current = "create";
    setOperation("create");
    try {
      if (personalizationDirty) {
        await personalizationMutation.mutateAsync();
      }
      if (onCreate) await onCreate();
      else await finalizeMutation.mutateAsync();
    } catch {
      // The owning mutation presents the error; keep the panel available to retry.
    } finally {
      operationRef.current = null;
      setOperation(null);
    }
  };

  return (
    <section className="workspace-draft-panel" aria-label={t("page.workspace_draft_chat.draft_summary")}>
      <div className="workspace-draft-panel__body" ref={panelBodyRef}>
        <header className="workspace-draft-panel__header">
          <div>
            <span className="workspace-draft-eyebrow">{statusLabel}</span>
            <h2>{view.fields.name || t("page.workspace_draft_chat.identity")}</h2>
            <p>{view.fields.operating_context || view.fields.initial_brief || t("page.workspace_draft_chat.tell_me_what_you_re_building_i_ll_fill_these_in")}</p>
          </div>
          <div
            className="workspace-draft-progress"
            style={{ "--workspace-draft-progress": `${view.percent * 3.6}deg` } as CSSProperties}
            role="progressbar"
            aria-label={t("page.workspace_draft_chat.draft_summary")}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={view.percent}
          >
            <span>{view.percent}<small>%</small></span>
          </div>
        </header>

        <div className="workspace-draft-statusline">
          <span
            className={`workspace-draft-statusdot workspace-draft-statusdot--${draft.status}`}
            aria-hidden="true"
          />
          <span>{statusLabel}</span>
          <span>·</span>
          <span>{statusDescription}</span>
        </div>

        <div className="workspace-draft-details">
          <section>
            <span>{t("page.workspace_draft_chat.field.workspace_type")}</span>
            <strong>{humanize(view.fields.kind) || "—"}</strong>
          </section>
          <section>
            <span>{t("page.workspace_draft_chat.field.how_it_will_be_used")}</span>
            <strong>{view.fields.operating_context || "—"}</strong>
          </section>
          <section>
            <span>{t("page.workspace_draft_chat.field.main_work")}</span>
            <strong>{view.fields.primary_work || "—"}</strong>
          </section>
          <section className="workspace-draft-runtime">
            <div className="workspace-draft-runtime__copy">
              <span>{t("page.workspace_draft_chat.runtime_mode")}</span>
              <strong>
                {view.autonomousEnabled
                  ? t("page.workspace_draft_chat.runtime_automatic")
                  : t("page.workspace_draft_chat.runtime_manual")}
              </strong>
              <small>{t("page.workspace_draft_chat.runtime_mode_desc")}</small>
            </div>
            <Toggle
              checked={view.autonomousEnabled}
              disabled={!editable || busy}
              aria-label={t("page.workspace_draft_chat.runtime_mode")}
              onChange={() => autonomyMutation.mutate(!view.autonomousEnabled)}
            />
          </section>
          <section>
            <span>{t("page.workspace_draft_chat.field.services_to_run")}</span>
            <DetailList values={view.serviceDetails} />
          </section>
          <section>
            <span>{t("page.workspace_draft_chat.field.agent_assignments")}</span>
            <DetailList values={view.mappingDetails} />
          </section>
          <section>
            <span>{t("page.workspace_draft_chat.field.channels")}</span>
            <DetailList values={view.channelDetails} />
            {channelRequirements.map((requirement) => (
              <div key={requirement.requirement_key} style={{ marginTop: 8 }}>
                <small style={{ display: "block", color: "var(--text-muted)", marginBottom: 4 }}>
                  {requirement.label}
                </small>
                <Select
                  ariaLabel={requirement.label}
                  value={requirement.resource_id || ""}
                  placeholder={requirement.reason}
                  disabled={!editable || busy}
                  options={requirement.resource_options.map((option) => ({
                    value: option.id,
                    label: option.label,
                  }))}
                  onChange={(id) => channelMutation.mutate({ key: requirement.requirement_key!, id })}
                />
              </div>
            ))}
          </section>
          <section>
            <span>{t("page.workspace_draft_chat.field.budget")}</span>
            <DetailList values={view.budgetDetails} />
          </section>
          <section>
            <span>{t("page.workspace_draft_chat.field.evaluation")}</span>
            <DetailList values={view.evaluationDetails} />
          </section>
          {view.goals.length > 0 && <section>
            <span>{t("page.workspace_draft_chat.field.goals")}</span>
            <DetailList values={view.goalDetails} />
          </section>}
          {view.staff.length > 0 && <section>
            <span>{t("page.workspace_draft_chat.field.staff")}</span>
            <DetailList values={view.staffDetails} />
          </section>}
          <section>
            <span>{t("page.workspace_draft_chat.field.knowledge_sources")}</span>
            <DetailList values={view.knowledgeDetails} />
          </section>
          {view.rules.length > 0 && <section>
            <span>{t("page.workspace_draft_chat.field.rules")}</span>
            <DetailList values={view.ruleDetails} />
          </section>}
          {view.automations.length > 0 && <section>
            <span>{t("page.workspace_draft_chat.field.automations")}</span>
            <DetailList values={view.automationDetails} />
          </section>}
          {personalizationSchema.length > 0 && <section>
            <span>{t("page.workspace_draft_chat.blueprint_personalization")}</span>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
              {personalizationSchema.map((field) => {
                const key = String(field.key || "");
                return (
                  <div key={key}>
                    <Input
                      label={String(field.label || key)}
                      value={personalizationValues[key] ?? ""}
                      required={Boolean(field.required)}
                      error={invalidPersonalizationKeys.has(key)
                        ? t("page.workspace_draft_chat.personalization_invalid_required_value")
                        : undefined}
                      onChange={(event) => setPersonalizationValues((current) => ({
                        ...current,
                        [key]: event.target.value,
                      }))}
                    />
                    {field.purpose && (
                      <small style={{ display: "block", color: "var(--text-muted)", marginTop: 3 }}>
                        {String(field.purpose)}
                      </small>
                    )}
                  </div>
                );
              })}
              <Button
                size="sm"
                variant="outline"
                disabled={!editable || busy || !personalizationDirty || invalidPersonalizationKeys.size > 0}
                loading={personalizationMutation.isPending}
                onClick={() => personalizationMutation.mutate()}
              >
                {t("page.workspace_draft_chat.save_personalization")}
              </Button>
            </div>
          </section>}
          {view.integrations.length > 0 && <section className="workspace-draft-details__warning">
            <span>{t("page.workspace_draft_chat.field.integrations_to_connect")}</span>
            <DetailList values={view.integrationDetails} />
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <Button
                size="sm"
                variant="outline"
                onClick={() => window.open("/integrations", "_blank", "noopener,noreferrer")}
              >
                {t("page.blueprints.connect_integrations")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => query.refetch()}>
                {t("page.blueprints.refresh_status")}
              </Button>
            </div>
          </section>}
          {view.notes && <section>
            <span>{t("page.workspace_draft_chat.field.notes")}</span>
            <strong>{view.notes}</strong>
          </section>}
          {!finalized && draft.missing.length > 0 && <section>
            <span>{t("page.workspace_draft_chat.still_needed")}</span>
            <Tags values={draft.missing.map(humanize)} />
          </section>}
        </div>

        {draft.suggested_blueprint && !draft.applied_blueprint_id && editable && (
          <div className="workspace-draft-blueprint">
            <div>
              <span>{t("page.workspace_draft_chat.suggested_template")}</span>
              <strong>{draft.suggested_blueprint.title}</strong>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              loading={operation === "blueprint" || applyMutation.isPending}
              ariaBusy={operation === "blueprint" || applyMutation.isPending}
              onClick={() => applyBlueprint(draft.suggested_blueprint!.id)}
            >
              {t("page.workspace_draft_chat.use_template")}
            </Button>
          </div>
        )}
      </div>

      {(finalized || editable) && <footer className="workspace-draft-panel__footer">
        {finalized ? (
          <Button size="lg" variant="primary" onClick={() => navigate(`/workspaces/${draft.finalized_workspace_id}`)}>
            {t("page.blueprint_detail.open_workspace")}
          </Button>
        ) : (
          <Button
            size="lg"
            variant="primary"
            disabled={!draft.ready || busy || invalidPersonalizationKeys.size > 0}
            loading={operation === "create" || creating || finalizeMutation.isPending}
            ariaBusy={busy}
            onClick={createWorkspace}
          >
            {draft.ready ? t("page.workspaces.create_workspace") : t("page.workspace_draft_chat.keep_chatting_until_ready")}
          </Button>
        )}
        {editable && <p>{t("page.workspace_draft_chat.resume_from_workspaces_page_anytime")}</p>}
      </footer>}
    </section>
  );
}
