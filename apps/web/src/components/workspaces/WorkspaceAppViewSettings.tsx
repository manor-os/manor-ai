import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type WorkspaceAppAction, type WorkspaceAppFormField, type WorkspaceAppViewConfig } from "../../lib/api";
import { t } from "../../lib/i18n";
import { workflowPath } from "../../lib/workflowRoutes";
import { useToastStore } from "../../stores/toast";
import { resolveWorkspaceAppTarget, workspaceAppTargetValue } from "../../lib/workspaceAppTargets";
import { IconArrowDown, IconArrowRight, IconArrowUp, IconChatBubble, IconClock, IconEye, IconEyeOff, IconFlow, IconImage, IconSparkles } from "../icons";
import Button from "../ui/Button";
import GlassCard from "../ui/GlassCard";
import Select from "../ui/Select";
import WorkspaceAppDataSettings from "./WorkspaceAppDataSettings";
import WorkspaceAppSectionsSettings from "./WorkspaceAppSectionsSettings";
import WorkspaceAppView from "./WorkspaceAppView";
import "./WorkspaceAppViewSettings.css";

type Visual = WorkspaceAppViewConfig["visual"];
type Layout = WorkspaceAppViewConfig["layout"];

export default function WorkspaceAppViewSettings({ workspaceId, canManage }: { workspaceId: string; canManage: boolean }) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const { data: view, isLoading, isError, refetch } = useQuery({
    queryKey: ["workspace-app-view", workspaceId],
    queryFn: () => api.workspaces.appView(workspaceId),
  });
  const [config, setConfig] = useState<WorkspaceAppViewConfig | null>(null);
  const [dataSettingsValid, setDataSettingsValid] = useState(true);
  useEffect(() => { if (view) setConfig(view.config); }, [view]);

  const actions = useMemo(() => {
    if (!view || !config) return [];
    const position = new Map(config.order.map((key, index) => [key, index]));
    const available = view.available_actions.filter((action) => !action.key.startsWith("action:") || config.entries?.[action.key])
      .map((action) => config.entries?.[action.key] ? { ...action,
        source_title: config.entries[action.key].title, title: config.labels[action.key] || config.entries[action.key].title,
      } : action);
    for (const [key, entry] of Object.entries(config.entries || {})) {
      if (!available.some((action) => action.key === key)) available.push({
        key, kind: "chat", title: entry.title, source_title: entry.title, description: "", source_description: "",
      });
    }
    for (const flow of view.workflow_options) {
      const key = `workflow:${flow.binding_id}`;
      if (!available.some((action) => action.key === key)) available.push({
        key, kind: "workflow", binding_id: flow.binding_id, workflow_id: flow.workflow_id,
        title: flow.title, source_title: flow.title, description: flow.description || "",
        placeholder: flow.placeholder, inputs: flow.inputs,
      });
    }
    const initialPosition = new Map(available.map((action, index) => [action.key, index]));
    return available.sort((a, b) =>
      (position.get(a.key) ?? initialPosition.get(a.key)! + position.size) -
      (position.get(b.key) ?? initialPosition.get(b.key)! + position.size),
    );
  }, [view, config]);
  const visible = actions.filter((action) => !config?.hidden.includes(action.key));
  const pendingPrimary = config?.hero?.startsWith("attach:")
    ? view?.attachable_workflow_options?.find((flow) => flow.workflow_id === config.hero!.slice(7)) : undefined;
  const selectedHero = config?.hero
      ?? (view && config ? visible.find((action) => {
        const resolved = resolveWorkspaceAppTarget(action, view, config);
        return resolved.kind === "workflow" && resolved.available !== false;
      })?.key : null) ?? null;
  const dirty = Boolean(view && config && JSON.stringify(config) !== JSON.stringify(view.config));
  const targetAction = (action: WorkspaceAppAction): WorkspaceAppAction => {
    if (!view || !config) return action;
    return resolveWorkspaceAppTarget(action, view, config);
  };
  const previewView = view && config ? {
    ...view,
    eyebrow: config.eyebrow ?? view.eyebrow,
    title: config.title ?? view.title,
    description: config.description ?? view.description,
    hero: selectedHero,
    actions: [...visible.map((action) => {
      const resolved = targetAction(action);
      return {
        ...resolved,
        title: config.labels[action.key]?.trim() || resolved.source_title,
        description: config.descriptions[action.key] ?? resolved.description,
        inputs: resolved.inputs,
      };
    }), ...(pendingPrimary ? [{
      key: config.hero!, kind: "workflow" as const, title: pendingPrimary.title,
      source_title: pendingPrimary.title, description: pendingPrimary.description || "",
      workflow_id: pendingPrimary.workflow_id, inputs: pendingPrimary.inputs,
    }] : [])],
    sections: config.sections
      ? config.sections.map((section) => ({ ...section, implicit: false, available: true, source_title: null }))
      : view.sections,
    config,
  } : undefined;

  const save = useMutation({
    mutationFn: async () => {
      if (!config || !view) return;
      const labels = Object.fromEntries(Object.entries(config.labels).filter(([, label]) => label.trim()).map(([key, label]) => [key, label.trim()]));
      await api.workspaces.saveAppView(workspaceId, { ...config, hero: selectedHero, labels }, view.config);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["workspace-app-view", workspaceId] });
      toast.success(t("component.workspace_app_view.saved"));
    },
    onError: (error: Error) => toast.error(t("component.workspace_app_view.save_error"), error.message),
  });

  const update = (next: Partial<WorkspaceAppViewConfig>) => setConfig((current) => current ? { ...current, ...next } : current);
  // An unset field follows the platform; an empty theme is no theme.
  const updateTheme = (next: NonNullable<WorkspaceAppViewConfig["theme"]>) => setConfig((current) => {
    if (!current) return current;
    const merged = Object.fromEntries(Object.entries({ ...(current.theme || {}), ...next }).filter(([, value]) => value));
    return { ...current, theme: Object.keys(merged).length ? merged : null };
  });
  const addEntry = () => {
    if (!config) return;
    const key = `action:${crypto.randomUUID()}`;
    update({
      entries: { ...config.entries, [key]: { title: t("component.workspace_app_view.new_action") } },
      targets: { ...config.targets, [key]: "agent:default" },
    });
  };
  const removeEntry = (key: string) => {
    if (!config) return;
    const next = {
      ...config, entries: { ...config.entries }, labels: { ...config.labels },
      descriptions: { ...config.descriptions }, forms: { ...config.forms },
      targets: { ...config.targets }, prompts: { ...config.prompts }, presets: { ...(config.presets || {}) },
      asks: { ...(config.asks || {}) }, choices: { ...(config.choices || {}) },
    };
    delete next.entries[key];
    for (const field of ["labels", "descriptions", "forms", "targets", "prompts", "presets", "asks", "choices"] as const) {
      delete next[field][key];
    }
    if (config.icons?.[key]) {
      next.icons = { ...config.icons };
      delete next.icons[key];
    }
    next.order = config.order.filter((item) => item !== key);
    next.hidden = config.hidden.filter((item) => item !== key);
    if (next.hero === key) next.hero = null;
    setConfig(next);
  };
  // An empty preset removes it, so the Flow's own default shows again.
  const setPreset = (key: string, field: string, value: string) => {
    if (!config) return;
    const presets = { ...(config.presets || {}) };
    const values = { ...(presets[key] || {}) };
    if (value) values[field] = value; else delete values[field];
    if (Object.keys(values).length) presets[key] = values; else delete presets[key];
    update({ presets });
  };
  const setTarget = (key: string, value: string) => {
    if (!config) return;
    const targets = { ...config.targets };
    if (value) targets[key] = value;
    else delete targets[key];
    update({ targets, ...(key === selectedHero && (value.startsWith("agent:") || value.startsWith("automation:")) ? { hero: null } : {}) });
  };
  const setRunWith = (action: WorkspaceAppAction, kind: WorkspaceAppAction["kind"]) => {
    if (targetAction(action).kind === kind) return;
    if (kind === "chat") setTarget(action.key, "agent:default");
    else if (kind === "workflow") {
      const flow = view?.workflow_options[0];
      const saved = view?.attachable_workflow_options?.[0];
      if (flow) setTarget(action.key, `workflow:${flow.binding_id}`);
      else if (saved) setTarget(action.key, `attach:${saved.workflow_id}`);
    } else if (kind === "automation" && view?.automation_options[0]) {
      setTarget(action.key, `automation:${view.automation_options[0].job_id}`);
    }
  };
  const setPrimaryFlow = (key: string) => {
    if (!config) return;
    const targets = { ...config.targets };
    delete targets[key];
    update({ hero: key, hidden: config.hidden.filter((item) => item !== key), targets });
  };
  const move = (key: string, direction: -1 | 1) => {
    const list = actions.map((action) => action.key);
    const index = list.indexOf(key);
    const other = index + direction;
    if (index < 0 || other < 0 || other >= list.length) return;
    [list[index], list[other]] = [list[other], list[index]];
    update({ order: list });
  };
  const toggleVisible = (key: string) => {
    if (!config) return;
    const hidden = config.hidden.includes(key)
      ? config.hidden.filter((item) => item !== key)
      : [...config.hidden, key];
    const nextHero = key === config.hero && hidden.includes(key)
      ? actions.find((action) => !hidden.includes(action.key) && targetAction(action).kind === "workflow")?.key || null
      : config.hero;
    update({ hidden, hero: nextHero });
  };
  const updateForm = (key: string, fields: WorkspaceAppFormField[]) => {
    if (!config) return;
    update({ forms: { ...(config.forms || {}), [key]: fields } });
  };
  const addField = (key: string) => {
    const fields = config?.forms?.[key] || [];
    if (fields.length >= 10) return;
    let number = fields.length + 1;
    while (fields.some((field) => field.key === `field_${number}`)) number += 1;
    updateForm(key, [...fields, { key: `field_${number}`, label: t("component.workspace_app_view.new_question"), type: "text", required: true, placeholder: "", options: [] }]);
  };
  const editField = (key: string, index: number, patch: Partial<WorkspaceAppFormField>) => {
    updateForm(key, (config?.forms?.[key] || []).map((field, position) => position === index ? { ...field, ...patch } : field));
  };

  if (isLoading) return <GlassCard hoverable={false}><p className="workspace-app-settings__state">{t("component.workspace_app_view.loading")}</p></GlassCard>;
  if (isError || !view || !config) return <GlassCard hoverable={false}><p className="workspace-app-settings__state">{t("component.workspace_app_view.error")}</p><Button variant="ghost" size="sm" onClick={() => void refetch()}>{t("component.workspace_app_view.retry")}</Button></GlassCard>;

  const currentPrimary = visible.find((action) => action.key === selectedHero);
  const resolvedPrimary = currentPrimary ? targetAction(currentPrimary) : undefined;
  const primaryFlowKey = pendingPrimary ? config.hero!
    : resolvedPrimary?.kind === "workflow" && resolvedPrimary.binding_id ? `workflow:${resolvedPrimary.binding_id}` : "";

  const visualOptions: { value: Visual; label: string; detail: string }[] = [
    { value: "auto", label: t("component.workspace_app_view.visual_auto"), detail: t("component.workspace_app_view.visual_auto_detail") },
    { value: "image", label: t("component.workspace_app_view.visual_image"), detail: t("component.workspace_app_view.visual_image_detail") },
    { value: "motion", label: t("component.workspace_app_view.visual_motion"), detail: t("component.workspace_app_view.visual_motion_detail") },
    { value: "plain", label: t("component.workspace_app_view.visual_plain"), detail: t("component.workspace_app_view.visual_plain_detail") },
  ];
  const layoutOptions: { value: Layout; label: string; detail: string }[] = [
    { value: "feature", label: t("component.workspace_app_view.layout_feature"), detail: t("component.workspace_app_view.layout_feature_detail") },
    { value: "cover", label: t("component.workspace_app_view.layout_cover"), detail: t("component.workspace_app_view.layout_cover_detail") },
    { value: "list", label: t("component.workspace_app_view.layout_list"), detail: t("component.workspace_app_view.layout_list_detail") },
  ];

  return <div className="workspace-app-settings">
    <div className="workspace-app-settings__heading">
      <div>
        <span className="workspace-app-settings__eyebrow">{t("component.workspace_app_view.title")}</span>
        <h2>{t("component.workspace_app_view.configure_title")}</h2>
        <p>{t("component.workspace_app_view.configure_description")}</p>
      </div>
      <Link className="workspace-app-settings__open" to={`/chat?workspace=${encodeURIComponent(workspaceId)}`}>
        {t("component.workspace_app_view.open_page")} <IconArrowRight size={15} />
      </Link>
    </div>

    <div className="workspace-app-settings__layout">
      <div className="workspace-app-settings__editor">
        <GlassCard hoverable={false}>
          <div className="workspace-app-settings__section-head"><div><h3>{t("component.workspace_app_view.copy_title")}</h3><p>{t("component.workspace_app_view.copy_description")}</p></div></div>
          <div className="workspace-app-settings__copy-fields">
            <label>{t("component.workspace_app_view.page_eyebrow")}
              <input className="manor-input" value={config.eyebrow ?? ""} onChange={(event) => update({ eyebrow: event.target.value })} maxLength={60} disabled={!canManage || save.isPending} />
            </label>
            <label>{t("component.workspace_app_view.page_title")}
              <input className="manor-input" value={config.title ?? view.title} onChange={(event) => update({ title: event.target.value })} maxLength={100} disabled={!canManage || save.isPending} />
            </label>
            <label>{t("component.workspace_app_view.page_description")}
              <textarea className="manor-input" value={config.description ?? view.description} onChange={(event) => update({ description: event.target.value })} maxLength={600} rows={3} disabled={!canManage || save.isPending} />
            </label>
            <label>{t("component.workspace_app_view.main_action")}
              <Select role="combobox" value={primaryFlowKey} onChange={setPrimaryFlow} disabled={!canManage || save.isPending}
                ariaLabel={t("component.workspace_app_view.main_action")} placeholder={t("component.workspace_app_view.choose_main_flow")}
                options={[...view.workflow_options.map((flow) => ({ value: `workflow:${flow.binding_id}`, label: flow.title })),
                  ...(view.attachable_workflow_options || []).map((flow) => ({ value: `attach:${flow.workflow_id}`, label: `${t("component.workspace_app_view.attach_flow")}: ${flow.title}` }))]} />
            </label>
            <p className="workspace-app-settings__hint">{t("component.workspace_app_view.main_flow_hint")} <Link to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=workflows`}>{t("nav.flows")}</Link></p>
          </div>
        </GlassCard>
        <GlassCard hoverable={false}>
          <div className="workspace-app-settings__section-head"><div><h3>{t("component.workspace_app_view.layout_label")}</h3><p>{t("component.workspace_app_view.layout_description")}</p></div></div>
          <div className="workspace-app-settings__layout-choices" role="radiogroup" aria-label={t("component.workspace_app_view.layout_label")}>
            {layoutOptions.map((option) => <label key={option.value} className={`workspace-app-settings__layout-choice${config.layout === option.value ? " workspace-app-settings__layout-choice--selected" : ""}`}>
              <input type="radio" name="workspace-app-layout" value={option.value} checked={config.layout === option.value} onChange={() => update({ layout: option.value })} disabled={!canManage || save.isPending} />
              <span className={`workspace-app-settings__layout-thumbnail workspace-app-settings__layout-thumbnail--${option.value}`} aria-hidden="true"><i /><i /><i /></span>
              <strong>{option.label}</strong><small>{option.detail}</small>
            </label>)}
          </div>
        </GlassCard>
        {Boolean(config.components?.length) && <GlassCard hoverable={false}>
          <WorkspaceAppDataSettings
            components={config.components || []}
            dataSources={config.data_sources || {}}
            onChange={(components) => update({ components })}
            onDataSourcesChange={(data_sources) => update({ data_sources })}
            disabled={!canManage || save.isPending}
            onValidityChange={setDataSettingsValid}
          />
        </GlassCard>}
        <GlassCard hoverable={false}>
          <div className="workspace-app-settings__section-head">
            <div><h3>{t("component.workspace_app_view.entries_title")}</h3><p>{t("component.workspace_app_view.entries_description")}</p></div>
            <div className="workspace-app-settings__order">
              <span className="workspace-app-settings__count">{visible.length + (pendingPrimary ? 1 : 0)}</span>
              <Button variant="ghost" size="sm" onClick={addEntry} disabled={!canManage || save.isPending || Object.keys(config.entries || {}).length >= 50}>{t("component.workspace_app_view.add_action")}</Button>
            </div>
          </div>
          {actions.length === 0 ? <p className="workspace-app-settings__state">{t(pendingPrimary ? "component.workspace_app_view.pending_main_flow" : "component.workspace_app_view.no_entries")}</p> : (
            <div className="workspace-app-settings__entries">
              {actions.map((action, index) => {
                const isHidden = config.hidden.includes(action.key);
                const resolved = targetAction(action);
                const kindLabel = t(`component.workspace_app_view.run_${resolved.kind}`);
                const targetValue = workspaceAppTargetValue(action, config);
                const targetOptions: Array<{ value: string; label: string; description?: string }> = resolved.kind === "chat" ? [
                  { value: "agent:default", label: action.default_agent_name || t("component.workspace_app_view.default_agent"), description: t("component.workspace_app_view.workspace_default") },
                  ...view.agent_options.map((agent) => ({ value: `agent:${agent.id}`, label: agent.name })),
                ] : resolved.kind === "workflow" ? [
                  ...view.workflow_options.map((flow) => ({ value: `workflow:${flow.binding_id}`, label: flow.title, description: t("component.workspace_app_view.flow_connected") })),
                  ...(view.attachable_workflow_options || []).map((flow) => ({ value: `attach:${flow.workflow_id}`, label: flow.title, description: t("component.workspace_app_view.flow_connect_on_save") })),
                ] : view.automation_options.map((job) => ({ value: `automation:${job.job_id}`, label: job.title }));
                if (resolved.available === false) targetOptions.unshift({ value: targetValue, label: t("component.workspace_app_view.target_unavailable") });
                return <div className={`workspace-app-settings__entry${isHidden ? " workspace-app-settings__entry--hidden" : ""}`} key={action.key} role="group" aria-label={action.source_title}>
                  <div className="workspace-app-settings__entry-top">
                    <span className="workspace-app-settings__entry-icon" aria-hidden="true">{resolved.kind === "workflow" ? <IconFlow size={17} /> : resolved.kind === "automation" ? <IconClock size={17} /> : <IconChatBubble size={17} />}</span>
                    <span className="workspace-app-settings__entry-title">{action.source_title}</span>
                    {action.key.startsWith("action:") && <button type="button" className="workspace-app-settings__icon-button" onClick={() => removeEntry(action.key)} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.remove_action")} ${action.title}`} title={t("component.workspace_app_view.remove_action")}>×</button>}
                    <button type="button" className="workspace-app-settings__icon-button" onClick={() => toggleVisible(action.key)} disabled={!canManage || save.isPending} aria-label={`${isHidden ? t("component.workspace_app_view.show") : t("component.workspace_app_view.hide")} ${action.title}`} title={isHidden ? t("component.workspace_app_view.show") : t("component.workspace_app_view.hide")}>{isHidden ? <IconEyeOff size={17} /> : <IconEye size={17} />}</button>
                  </div>
                  <div className="workspace-app-settings__entry-controls">
                    <label className="workspace-app-settings__primary">
                      <input type="radio" name="workspace-app-primary" checked={!isHidden && selectedHero === action.key} onChange={() => update({ hero: action.key })} disabled={isHidden || resolved.kind !== "workflow" || !canManage || save.isPending} />
                      {t("component.workspace_app_view.primary")}
                    </label>
                    <div className="workspace-app-settings__order">
                      <button type="button" className="workspace-app-settings__icon-button" onClick={() => move(action.key, -1)} disabled={index === 0 || !canManage || save.isPending} aria-label={`${t("component.workspace_app_view.move_up")} ${action.title}`}><IconArrowUp size={16} /></button>
                      <button type="button" className="workspace-app-settings__icon-button" onClick={() => move(action.key, 1)} disabled={index === actions.length - 1 || !canManage || save.isPending} aria-label={`${t("component.workspace_app_view.move_down")} ${action.title}`}><IconArrowDown size={16} /></button>
                    </div>
                  </div>
                  <div className="workspace-app-settings__entry-copy">
                    <input className="manor-input workspace-app-settings__label-input" value={config.labels[action.key] ?? action.source_title} onChange={(event) => update({ labels: { ...config.labels, [action.key]: event.target.value }, ...(config.entries?.[action.key] ? { entries: { ...config.entries, [action.key]: { title: event.target.value } } } : {}) })} onBlur={() => { if (!config.labels[action.key]?.trim()) { const labels = { ...config.labels }; delete labels[action.key]; update({ labels }); } }} maxLength={80} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.entry_name")} ${action.source_title}`} />
                    <textarea className="manor-input workspace-app-settings__label-input" value={config.descriptions[action.key] ?? resolved.description} onChange={(event) => update({ descriptions: { ...config.descriptions, [action.key]: event.target.value } })} maxLength={240} rows={2} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.entry_description")} ${action.source_title}`} />
                  </div>
                  <div className="workspace-app-settings__target">
                    <div className="workspace-app-settings__run-with">
                      <div className="workspace-app-settings__target-field">
                        <span>{t("component.workspace_app_view.run_with")}</span>
                        <Select value={resolved.kind} onChange={(value) => setRunWith(action, value as WorkspaceAppAction["kind"])}
                          options={[
                            { value: "chat", label: t("component.workspace_app_view.run_chat"), icon: <IconChatBubble size={15} /> },
                            { value: "workflow", label: t("component.workspace_app_view.run_workflow"), icon: <IconFlow size={15} />, disabled: !view.workflow_options.length && !view.attachable_workflow_options?.length },
                            { value: "automation", label: t("component.workspace_app_view.run_automation"), icon: <IconClock size={15} />, disabled: !view.automation_options.length },
                          ]}
                          showSelectedIcon disabled={!canManage || save.isPending}
                          ariaLabel={`${t("component.workspace_app_view.run_with")} ${action.source_title}`} />
                      </div>
                      <div className="workspace-app-settings__target-field">
                        <span>{kindLabel}</span>
                        <Select value={targetValue} onChange={(value) => setTarget(action.key, value)} options={targetOptions}
                          filterable disabled={!canManage || save.isPending || !targetOptions.length}
                          ariaLabel={`${kindLabel} ${action.source_title}`} />
                      </div>
                    </div>
                    {resolved.kind === "workflow" && <div className="workspace-app-settings__execution-detail">
                      <p>{t("component.workspace_app_view.flow_inputs_hint")}</p>
                      {resolved.workflow_id && <Link to={workflowPath(resolved.workflow_id)}>{t("component.workspace_app_view.configure_flow")} <IconArrowRight size={13} /></Link>}
                    </div>}
                    {resolved.kind === "workflow" && (resolved.inputs || []).some((input) => input.type !== "file") && <div className="workspace-app-settings__presets">
                      <p>{t("component.workspace_app_view.presets_hint")}</p>
                      {(resolved.inputs || []).filter((input) => input.type !== "file").map((input) => <label key={input.key}>
                        <span>{input.label}</span>
                        <textarea className="manor-input" rows={3} maxLength={4000} value={config.presets?.[action.key]?.[input.key] ?? ""}
                          onChange={(event) => setPreset(action.key, input.key, event.target.value)} disabled={!canManage || save.isPending}
                          aria-label={`${t("component.workspace_app_view.presets_label")} ${input.label}`} />
                      </label>)}
                    </div>}
                    {targetValue.startsWith("attach:") && <p className="workspace-app-settings__target-note" role="status">{t("component.workspace_app_view.flow_connect_on_save")}</p>}
                    {resolved.kind === "automation" && <div className="workspace-app-settings__execution-detail">
                      <p>{t("component.workspace_app_view.automation_config_hint")}</p>
                      <Link to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=automations`}>{t("component.workspace_app_view.configure_automation")} <IconArrowRight size={13} /></Link>
                    </div>}
                  </div>
                  {resolved.kind === "chat" && view.agent_options.length === 0 && <Link className="workspace-app-settings__target-help" to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=agents`}>{t("component.workspace_app_view.add_agent_hint")} <IconArrowRight size={13} /></Link>}
                  {resolved.kind === "chat" && <div className="workspace-app-settings__form-editor">
                    <label className="workspace-app-settings__target-field">{t("component.workspace_app_view.agent_command")}
                      <textarea className="manor-input" rows={3} maxLength={8000} value={config.prompts?.[action.key] ?? ""} onChange={(event) => update({ prompts: { ...config.prompts, [action.key]: event.target.value } })} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.agent_command")} ${action.title}`} />
                    </label>
                    <div className="workspace-app-settings__form-head"><strong>{t("component.workspace_app_view.questions")}</strong><button type="button" onClick={() => addField(action.key)} disabled={!canManage || save.isPending || (config.forms?.[action.key]?.length || 0) >= 10}>+ {t("component.workspace_app_view.add_question")}</button></div>
                    {(config.forms?.[action.key] || []).map((field, fieldIndex) => <div className="workspace-app-settings__form-field" key={field.key}>
                      <input className="manor-input" value={field.label} onChange={(event) => editField(action.key, fieldIndex, { label: event.target.value })} maxLength={80} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.question_label")} ${fieldIndex + 1}`} />
                      <div className="workspace-app-settings__form-options">
                        <Select role="combobox" value={field.type} onChange={(value) => editField(action.key, fieldIndex, { type: value as WorkspaceAppFormField["type"], options: value === "select" ? (field.options.length ? field.options : [t("component.workspace_app_view.choice_example")]) : [] })} disabled={!canManage || save.isPending} ariaLabel={`${t("component.workspace_app_view.question_type")} ${fieldIndex + 1}`}
                          style={{ width: "min(180px, 50%)" }} buttonStyle={{ fontSize: 11 }}
                          options={(["text", "long_text", "file", "number", "select"] as const).map((type) => ({ value: type, label: t(`component.workspace_app_view.field_${type}`) }))} />
                        <label><input type="checkbox" checked={field.required} onChange={(event) => editField(action.key, fieldIndex, { required: event.target.checked })} disabled={!canManage || save.isPending} />{t("component.workspace_app_view.required")}</label>
                        <button type="button" onClick={() => updateForm(action.key, (config.forms?.[action.key] || []).filter((_, position) => position !== fieldIndex))} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.remove_question")} ${fieldIndex + 1}`}>×</button>
                      </div>
                      {field.type === "select" ? <input className="manor-input" value={field.options.join(", ")} onChange={(event) => editField(action.key, fieldIndex, { options: event.target.value.split(",").map((option) => option.trim()).filter(Boolean) })} placeholder={t("component.workspace_app_view.choices_hint")} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.choices_hint")} ${fieldIndex + 1}`} /> : field.type !== "file" ? <input className="manor-input" value={field.placeholder} onChange={(event) => editField(action.key, fieldIndex, { placeholder: event.target.value })} maxLength={160} placeholder={t("component.workspace_app_view.question_hint")} disabled={!canManage || save.isPending} aria-label={`${t("component.workspace_app_view.question_hint")} ${fieldIndex + 1}`} /> : null}
                    </div>)}
                    {(config.forms?.[action.key]?.length || 0) === 0 && <p>{t("component.workspace_app_view.questions_empty")}</p>}
                  </div>}
                </div>;
              })}
            </div>
          )}
          <div className="workspace-app-settings__more-links">
            <span>{t("component.workspace_app_view.connect_more")}</span>
            <Link to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=workflows`}>{t("nav.flows")}</Link>
            <Link to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=automations`}>{t("page.scheduled_jobs.automations")}</Link>
            <Link to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=agents`}>{t("nav.agents")}</Link>
          </div>
        </GlassCard>

        <GlassCard hoverable={false}>
          <div className="workspace-app-settings__section-head"><div><h3>{t("component.workspace_app_view.sections_title")}</h3><p>{t("component.workspace_app_view.sections_description")}</p></div></div>
          <WorkspaceAppSectionsSettings view={view} sections={config.sections} disabled={!canManage || save.isPending}
            onChange={(sections) => update({ sections })} />
        </GlassCard>

        <GlassCard hoverable={false}>
          <div className="workspace-app-settings__section-head"><div><h3>{t("component.workspace_app_view.appearance")}</h3><p>{t("component.workspace_app_view.appearance_description")}</p></div><IconSparkles size={17} /></div>
          <div className="workspace-app-settings__visuals" role="radiogroup" aria-label={t("component.workspace_app_view.appearance")}>
            {visualOptions.map((option) => <label key={option.value} className={`workspace-app-settings__visual${config.visual === option.value ? " workspace-app-settings__visual--selected" : ""}`}>
              <input type="radio" name="workspace-app-visual" value={option.value} checked={config.visual === option.value} onChange={() => update({ visual: option.value })} disabled={!canManage || save.isPending} />
              <span className="workspace-app-settings__visual-mark" aria-hidden="true">{option.value === "image" ? <IconImage size={17} /> : option.value === "motion" ? <IconSparkles size={17} /> : option.value === "plain" ? <span className="workspace-app-settings__plain-mark" /> : <IconImage size={17} />}</span>
              <strong>{option.label}</strong><small>{option.detail}</small>
            </label>)}
          </div>
          {(config.visual === "image" || config.visual === "auto" || config.visual === "motion") && <p className="workspace-app-settings__hint">
            {!view.cover_image_url && <span>{t("component.workspace_app_view.no_cover_hint")} </span>}
            <Link to={`/workspaces/${encodeURIComponent(workspaceId)}?tab=settings#workspace-cover-image`}>{view.cover_image_url ? t("component.workspace_app_view.change_cover") : t("component.workspace_app_view.add_cover")}</Link>
          </p>}
          {view.cover_image_url && (config.visual === "image" || config.visual === "auto" || config.visual === "motion") && <div className="workspace-app-settings__cover-preview"><img src={view.cover_image_url} alt="" /><span>{t(view.cover_source === "starter" || view.cover_source === "app" ? "component.workspace_app_view.using_starter_cover" : "component.workspace_app_view.using_workspace_cover")}</span></div>}
          {config.visual === "motion" && view.cover_image_url && <label className="workspace-app-settings__motion-source">
            {t("component.workspace_app_view.motion_cover_label")}
            <input className="manor-input" type="text" value={config.motion_cover_url || ""} onChange={(event) => update({ motion_cover_url: event.target.value || null, motion_cover_source_url: event.target.value ? view.cover_image_url : null })} placeholder="https://…/cover.mp4" disabled={!canManage || save.isPending} />
            <small>{t("component.workspace_app_view.motion_cover_hint")}</small>
            {config.motion_cover_url && config.motion_cover_source_url !== view.cover_image_url && <small role="status">{t("component.workspace_app_view.motion_cover_changed")}</small>}
          </label>}
          <div className="workspace-app-settings__theme">
            <label className="workspace-app-settings__theme-accent">
              {t("component.workspace_app_view.theme_accent")}
              <span>
                <input type="color" value={config.theme?.accent || "#4f7d75"} aria-label={t("component.workspace_app_view.theme_accent")}
                  onChange={(event) => updateTheme({ accent: event.target.value })} disabled={!canManage || save.isPending} />
                {config.theme?.accent
                  ? <Button variant="ghost" size="sm" onClick={() => updateTheme({ accent: undefined })} disabled={!canManage || save.isPending}>{t("component.workspace_app_view.theme_default")}</Button>
                  : <small>{t("component.workspace_app_view.theme_default")}</small>}
              </span>
            </label>
            <Select value={config.theme?.surface || "neutral"} disabled={!canManage || save.isPending}
              ariaLabel={t("component.workspace_app_view.theme_surface")}
              onChange={(value) => updateTheme({ surface: value === "neutral" ? undefined : value as "tinted" | "deep" })}
              options={(["neutral", "tinted", "deep"] as const).map((value) => ({ value, label: t(`component.workspace_app_view.theme_surface_${value}`) }))} />
            <Select value={config.theme?.radius || "soft"} disabled={!canManage || save.isPending}
              ariaLabel={t("component.workspace_app_view.theme_radius")}
              onChange={(value) => updateTheme({ radius: value === "soft" ? undefined : value as "sharp" | "round" })}
              options={(["sharp", "soft", "round"] as const).map((value) => ({ value, label: t(`component.workspace_app_view.theme_radius_${value}`) }))} />
          </div>
        </GlassCard>
        {canManage && <div className="workspace-app-settings__footer">
          {dirty && <span className="workspace-app-settings__unsaved" role="status">{t("component.workspace_app_view.unsaved")}</span>}
          <Button variant="ghost" onClick={() => setConfig(view.config)} disabled={!dirty || save.isPending}>{t("action.cancel")}</Button>
          <Button variant="primary" onClick={() => save.mutate()} disabled={!dirty || save.isPending || (Boolean(config.components?.length) && !dataSettingsValid)} loading={save.isPending}>{t("action.save")}</Button>
        </div>}
      </div>
      <div className="workspace-app-settings__preview-wrap">
        <span className="workspace-app-settings__preview-label">{t("component.workspace_app_view.preview")}</span>
        <WorkspaceAppView view={previewView} loading={false} error={false} busy={false} canStart={true} onRetry={() => undefined} onAction={async () => ({ content: "", files: [] })} onEdit={() => undefined} onClose={() => undefined} preview />
      </div>
    </div>
  </div>;
}
