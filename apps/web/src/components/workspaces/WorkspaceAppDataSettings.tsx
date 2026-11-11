import { useEffect, useMemo, useState } from "react";

import type { WorkspaceAppDataBlock, WorkspaceAppDataComponent, WorkspaceAppDataDisplay, WorkspaceAppDataSource, WorkspaceAppSummaryField } from "../../lib/api";
import { t } from "../../lib/i18n";
import { IconArrowDown, IconArrowUp, IconPlus, IconTrash } from "../icons";
import Button from "../ui/Button";

type Display = Omit<Required<WorkspaceAppDataDisplay>, "calendar"> & Pick<WorkspaceAppDataDisplay, "calendar">;
type Props = {
  components: WorkspaceAppDataComponent[];
  dataSources: Record<string, WorkspaceAppDataSource>;
  onChange: (components: WorkspaceAppDataComponent[]) => void;
  onDataSourcesChange: (dataSources: Record<string, WorkspaceAppDataSource>) => void;
  disabled: boolean;
  onValidityChange?: (valid: boolean) => void;
};

function recordProjectAncestors(dataSources: Record<string, WorkspaceAppDataSource>, sourceName: string): string[] {
  const source = dataSources[sourceName];
  let current = source?.type === "project_records" ? String(source.parent || "") : sourceName;
  const ancestors: string[] = [];
  while (current && !ancestors.includes(current)) {
    const item = dataSources[current];
    if (!item || !["workflow_projects", "related_projects"].includes(item.type)) return [];
    ancestors.push(current);
    if (item.type === "workflow_projects") return ancestors;
    current = String(item.parent || "");
  }
  return [];
}

function eligibleMediaSources(dataSources: Record<string, WorkspaceAppDataSource>, sourceName: string): string[] {
  const ancestors = new Set(recordProjectAncestors(dataSources, sourceName));
  return Object.entries(dataSources).flatMap(([key, source]) =>
    source.type === "project_documents" && ancestors.has(String(source.parent || "")) ? [key] : []);
}

function mediaSourceLabel(dataSources: Record<string, WorkspaceAppDataSource>, components: WorkspaceAppDataComponent[], source: string): string {
  const documentBlock = components.flatMap((component) => component.blocks).find((block) => block.type === "documents" && block.source === source);
  const label = documentBlock?.title || String(dataSources[source]?.document_role || source);
  return label.replace(/[_-]+/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function displayOf(component: WorkspaceAppDataComponent): Display {
  return { ...component.display, layout: component.display?.layout || "stack", groups: component.display?.groups || [],
    hidden: component.display?.hidden || false, hidden_blocks: component.display?.hidden_blocks || [] };
}
function move<T>(items: T[], index: number, direction: -1 | 1): T[] {
  const next = [...items];
  const destination = index + direction;
  if (destination < 0 || destination >= next.length) return next;
  [next[index], next[destination]] = [next[destination], next[index]];
  return next;
}
function groupKey(): string { return `group_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`; }
function optionalText(value: string): string | null { return value.trim() ? value : null; }
function parseStageLabels(value: string): Record<string, string> {
  return value.split("\n").reduce<Record<string, string>>((labels, line) => {
    const separator = line.indexOf(":");
    if (separator <= 0) return labels;
    const stage = line.slice(0, separator).trim();
    const label = line.slice(separator + 1).trim();
    if (stage && label) labels[stage] = label;
    return labels;
  }, {});
}
function formatStageLabels(labels?: Record<string, string>): string {
  return Object.entries(labels || {}).map(([stage, label]) => `${stage}: ${label}`).join("\n");
}
function blockDescription(block: WorkspaceAppDataBlock): string {
  return t(`component.workspace_app_view.data_kind_${block.type === "records" && block.metrics_source ? "publication" : block.type}`);
}

export default function WorkspaceAppDataSettings({ components, dataSources, onChange, onDataSourcesChange, disabled, onValidityChange }: Props) {
  const [newGroupBlock, setNewGroupBlock] = useState<Record<string, string>>({});
  const isValid = useMemo(() => components.every((component) => {
    const display = displayOf(component);
    return display.groups.every((group) => group.title.trim()) && (display.layout !== "tabs" || Boolean(display.groups.length));
  }), [components]);
  useEffect(() => onValidityChange?.(isValid), [isValid, onValidityChange]);
  const updateComponent = (index: number, update: (component: WorkspaceAppDataComponent) => WorkspaceAppDataComponent) => {
    onChange(components.map((component, position) => position === index ? update(component) : component));
  };
  const updateDataSource = (key: string, update: (source: WorkspaceAppDataSource) => WorkspaceAppDataSource) => {
    const source = dataSources[key];
    if (source) onDataSourcesChange({ ...dataSources, [key]: update(source) });
  };
  const updateDisplay = (index: number, update: (display: Display, component: WorkspaceAppDataComponent) => WorkspaceAppDataDisplay) => {
    updateComponent(index, (component) => ({ ...component, display: update(displayOf(component), component) }));
  };
  const setBlockVisible = (index: number, key: string, visible: boolean) => updateDisplay(index, (display) => ({
    ...display, hidden_blocks: visible ? display.hidden_blocks.filter((item) => item !== key) : [...display.hidden_blocks, key],
  }));
  const setLayout = (index: number, layout: "stack" | "tabs") => updateDisplay(index, (display, component) => ({
    ...display, layout, groups: layout === "tabs" && !display.groups.length
      ? [{ key: groupKey(), title: t("component.workspace_app_view.data_tab_default"), blocks: component.blocks.map((block) => block.key) }]
      : display.groups,
  }));
  const setBlockGroup = (index: number, blockKey: string, destination: string) => updateDisplay(index, (display) => ({
    ...display, groups: display.groups.map((group) => ({ ...group,
      blocks: group.key === destination ? [...group.blocks.filter((key) => key !== blockKey), blockKey] : group.blocks.filter((key) => key !== blockKey),
    })).filter((group) => group.blocks.length),
  }));
  const addGroup = (index: number, component: WorkspaceAppDataComponent) => {
    const blockKey = newGroupBlock[component.key] || component.blocks[0]?.key;
    if (!blockKey) return;
    updateDisplay(index, (display, current) => {
      const groups = display.groups.length ? display.groups : [{
        key: groupKey(), title: t("component.workspace_app_view.data_group_default"), blocks: current.blocks.map((block) => block.key),
      }];
      return { ...display, groups: [
        ...groups.map((group) => ({ ...group, blocks: group.blocks.filter((key) => key !== blockKey) })).filter((group) => group.blocks.length),
        { key: groupKey(), title: t(display.layout === "tabs" ? "component.workspace_app_view.data_tab_default" : "component.workspace_app_view.data_group_default"), blocks: [blockKey] },
      ] };
    });
  };
  const removeGroup = (index: number, groupIndex: number) => updateComponent(index, (component) => {
    const display = displayOf(component);
    if (display.groups.length === 1) return { ...component,
      blocks: display.groups[0].blocks.map((key) => component.blocks.find((block) => block.key === key)!),
      display: { ...display, layout: "stack", groups: [] },
    };
    const removed = display.groups[groupIndex];
    const destination = groupIndex === 0 ? 0 : groupIndex - 1;
    return { ...component, display: { ...display, groups: display.groups.filter((_, position) => position !== groupIndex).map((group, position) =>
      position === destination ? { ...group, blocks: [...group.blocks, ...removed.blocks] } : group) } };
  });
  const moveBlock = (index: number, key: string, direction: -1 | 1) => updateComponent(index, (component) => {
    const display = displayOf(component);
    const group = display.groups.find((item) => item.blocks.includes(key));
    const visibleKeys = (group ? group.blocks : component.blocks.map((block) => block.key))
      .filter((item) => !display.hidden_blocks.includes(item));
    const other = visibleKeys[visibleKeys.indexOf(key) + direction];
    if (!other) return component;
    const swap = (keys: string[]) => keys.map((item) => item === key ? other : item === other ? key : item);
    if (group) return { ...component, display: { ...display,
      groups: display.groups.map((item) => item.key === group.key ? { ...item, blocks: swap(item.blocks) } : item) } };
    const order = swap(component.blocks.map((block) => block.key));
    return { ...component, blocks: order.map((item) => component.blocks.find((block) => block.key === item)!) };
  });

  if (!components.length) return null;
  return <section className="workspace-app-data-settings" aria-labelledby="workspace-app-data-settings-title">
    <div className="workspace-app-settings__section-head"><div>
      <h3 id="workspace-app-data-settings-title">{t("component.workspace_app_view.data_sections_title")}</h3>
      <p>{t("component.workspace_app_view.data_sections_description")}</p>
    </div></div>
    <div className="workspace-app-data-settings__components">{components.map((component, componentIndex) => {
      const display = displayOf(component);
      const usesGroups = display.groups.length > 0;
      const blockOrder = usesGroups ? display.groups.flatMap((group) => group.blocks) : component.blocks.map((block) => block.key);
      const visibleBlocks = blockOrder.filter((key) => !display.hidden_blocks.includes(key)).map((key) => component.blocks.find((block) => block.key === key)!);
      const hiddenBlocks = component.blocks.filter((block) => display.hidden_blocks.includes(block.key));
      const recordBlocks = component.context.kind === "project" ? component.blocks.filter((block) =>
        block.type === "records" && recordProjectAncestors(dataSources, block.source).includes(component.context.source || "")) : [];
      const observationBlocks = recordBlocks.filter((block) => block.metrics_source && dataSources[block.source]?.record_schema === "publication-receipt/v1");
      const renderBlock = (block: WorkspaceAppDataBlock) => {
        const group = display.groups.find((item) => item.blocks.includes(block.key));
        const order = (group ? group.blocks : component.blocks.map((item) => item.key))
          .filter((key) => !display.hidden_blocks.includes(key));
        const mediaSources = block.type === "records" ? eligibleMediaSources(dataSources, block.source) : [];
        const media = block.record_display?.media || [];
        const source = dataSources[block.source];
        const projectSource = source && (source.type === "workflow_projects" || source.type === "related_projects") ? source : null;
        const sourceFields = Array.isArray(projectSource?.fields) ? projectSource.fields.filter((field): field is string[] => Array.isArray(field) && field.every((part) => typeof part === "string")) : [];
        const progressFields = (block.type === "progress" ? block.fields || [] : []).map((field) => Array.isArray(field)
          ? { label: field.join("."), field, format: "text" as const }
          : field);
        const updateBlock = (update: (current: WorkspaceAppDataBlock) => WorkspaceAppDataBlock) => updateComponent(componentIndex, (current) => ({ ...current,
          blocks: current.blocks.map((item) => item.key === block.key ? update(item) : item),
        }));
        const stageList = (value: string) => value.split(",").map((stage) => stage.trim()).filter(Boolean);
        const updateStageLabels = (value: string) => updateBlock((current) => {
          const stage_labels = parseStageLabels(value);
          const { stage_labels: _stageLabels, ...rest } = current;
          return Object.keys(stage_labels).length ? { ...rest, stage_labels } : rest;
        });
        const updateMedia = (next: NonNullable<NonNullable<WorkspaceAppDataBlock["record_display"]>["media"]>) => updateComponent(componentIndex, (current) => ({ ...current,
          blocks: current.blocks.map((item) => {
            if (item.key !== block.key) return item;
            const { media: _media, ...display } = item.record_display || {};
            return { ...item, record_display: { ...display, ...(next.length ? { media: next } : {}) } };
          }),
        }));
        return <div className="workspace-app-data-settings__block" key={block.key}>
          <div className="workspace-app-data-settings__block-copy">
            <input className="manor-input" value={block.title || ""} placeholder={block.key} maxLength={100} disabled={disabled}
              aria-label={`${t("component.workspace_app_view.data_content_name")} ${block.title || block.key}`}
              onChange={(event) => updateComponent(componentIndex, (current) => ({ ...current,
                blocks: current.blocks.map((item) => item.key === block.key ? { ...item, title: optionalText(event.target.value) } : item),
              }))} />
            <small>{blockDescription(block)}</small>
          </div>
          <div className="workspace-app-data-settings__block-controls">
            {usesGroups && <select value={group?.key || ""} disabled={disabled}
              aria-label={`${t(display.layout === "tabs" ? "component.workspace_app_view.data_layout_tabs" : "component.workspace_app_view.data_group_assignment")} ${block.title || block.key}`}
              onChange={(event) => setBlockGroup(componentIndex, block.key, event.target.value)}>
              {display.groups.map((item) => <option key={item.key} value={item.key}>{item.title || t(display.layout === "tabs" ? "component.workspace_app_view.data_tab_default" : "component.workspace_app_view.data_group_default")}</option>)}
            </select>}
            {([-1, 1] as const).map((direction) => <button key={direction} type="button" className="workspace-app-settings__icon-button"
              disabled={disabled || !order[order.indexOf(block.key) + direction]} onClick={() => moveBlock(componentIndex, block.key, direction)}
              aria-label={`${t(`component.workspace_app_view.move_${direction === -1 ? "up" : "down"}`)} ${block.title || block.key}`}>
              {direction === -1 ? <IconArrowUp size={15} /> : <IconArrowDown size={15} />}
            </button>)}
            <button type="button" className="workspace-app-data-settings__text-button" disabled={disabled}
              aria-label={`${t("component.workspace_app_view.hide")} ${block.title || block.key}`}
              onClick={() => setBlockVisible(componentIndex, block.key, false)}>{t("component.workspace_app_view.hide")}</button>
          </div>
          {/* A page that shows every counter as 0 until read has no "configured only" choice to make. */}
          {block.metrics_source && block.metrics_display !== "zero" && <label className="workspace-app-data-settings__toggle">
            <input type="checkbox" disabled={disabled} checked={block.metrics_display === "configured"}
              onChange={(event) => updateComponent(componentIndex, (current) => ({ ...current,
                blocks: current.blocks.map((item) => item.key === block.key
                  ? { ...item, metrics_display: event.target.checked ? "configured" : "declared" } : item),
              }))} />
            {t("component.workspace_app_view.data_configured_metrics_only")}
          </label>}
          <details className="workspace-app-data-settings__disclosure">
            <summary>{t("component.workspace_app_view.data_configuration")}</summary>
            <div className="workspace-app-data-settings__copy">
              {projectSource && <>
                <label>{t("component.workspace_app_view.data_source_stages")}<input className="manor-input" value={(projectSource.stages || []).join(", ")} disabled={disabled}
                  aria-label={`${t("component.workspace_app_view.data_source_stages")} ${block.title || block.key}`}
                  onChange={(event) => updateDataSource(block.source, (current) => {
                    const stages = stageList(event.target.value);
                    const { stages: _stages, ...rest } = current;
                    return stages.length ? { ...rest, stages } : rest;
                  })} /></label>
                <label>{t("component.workspace_app_view.data_source_execution_run_id_path")}<input className="manor-input" value={projectSource.execution_run_id_path?.join(".") || ""} disabled={disabled}
                  aria-label={`${t("component.workspace_app_view.data_source_execution_run_id_path")} ${block.title || block.key}`}
                  placeholder={t("component.workspace_app_view.data_source_execution_run_id_path_hint")}
                  onChange={(event) => updateDataSource(block.source, (current) => {
                    const execution_run_id_path = event.target.value.trim().split(".").map((part) => part.trim()).filter(Boolean);
                    const { execution_run_id_path: _executionRunIdPath, ...rest } = current;
                    return execution_run_id_path.length ? { ...rest, execution_run_id_path } : rest;
                  })} /></label>
                <label>{t("component.workspace_app_view.data_source_order")}<select value={projectSource.order?.field.join(".") || ""} disabled={disabled}
                  aria-label={`${t("component.workspace_app_view.data_source_order")} ${block.title || block.key}`}
                  onChange={(event) => updateDataSource(block.source, (current) => {
                    const field = event.target.value ? event.target.value.split(".") : null;
                    const { order: _order, ...rest } = current;
                    return field ? { ...rest, order: { field, type: "datetime", direction: current.order?.direction || "asc" } } : rest;
                  })}>
                  <option value="">{t("component.workspace_app_view.data_source_order_none")}</option>
                  {sourceFields.map((field) => <option key={field.join(".")} value={field.join(".")}>{field.join(".")}</option>)}
                </select></label>
                {projectSource.order && <label>{t("component.workspace_app_view.data_source_direction")}<select value={projectSource.order.direction} disabled={disabled}
                  aria-label={`${t("component.workspace_app_view.data_source_direction")} ${block.title || block.key}`}
                  onChange={(event) => updateDataSource(block.source, (current) => ({ ...current, order: { ...current.order!, direction: event.target.value as "asc" | "desc" } }))}>
                  <option value="asc">{t("component.workspace_app_view.data_source_ascending")}</option><option value="desc">{t("component.workspace_app_view.data_source_descending")}</option>
                </select></label>}
              </>}
              {block.type === "project_rollup" && <>
                <label className="workspace-app-data-settings__toggle"><input type="checkbox" checked={Boolean(block.count_by_stage)} disabled={disabled}
                  onChange={(event) => updateBlock((current) => {
                    if (event.target.checked) return { ...current, count_by_stage: true };
                    const { completion: _completion, ...rest } = current;
                    return { ...rest, count_by_stage: false };
                  })} />{t("component.workspace_app_view.data_rollup_count")}</label>
                <label>{t("component.workspace_app_view.data_rollup_pending")}<input className="manor-input" value={(block.first?.stages || []).join(", ")} disabled={disabled}
                  aria-label={`${t("component.workspace_app_view.data_rollup_pending")} ${block.title || block.key}`}
                  onChange={(event) => updateBlock((current) => {
                    const stages = stageList(event.target.value);
                    const { first: _first, ...rest } = current;
                    return stages.length ? { ...rest, first: { stages } } : rest;
                  })} /></label>
                <label>{t("component.workspace_app_view.data_rollup_completion_stages")}<input className="manor-input" value={(block.completion?.stages || []).join(", ")} disabled={disabled || !block.count_by_stage}
                  aria-label={`${t("component.workspace_app_view.data_rollup_completion_stages")} ${block.title || block.key}`}
                  onChange={(event) => updateBlock((current) => {
                    const stages = stageList(event.target.value);
                    const { completion: _completion, ...rest } = current;
                    return stages.length ? { ...rest, count_by_stage: true, completion: { stages, label: current.completion?.label || "" } } : rest;
                  })} /></label>
                <label>{t("component.workspace_app_view.data_rollup_completion_label")}<input className="manor-input" value={block.completion?.label || ""} maxLength={120} disabled={disabled || !block.count_by_stage}
                  aria-label={`${t("component.workspace_app_view.data_rollup_completion_label")} ${block.title || block.key}`}
                  onChange={(event) => updateBlock((current) => current.completion?.stages.length
                    ? { ...current, completion: { ...current.completion, label: event.target.value } }
                    : current)} /></label>
              </>}
              {(block.type === "project_rollup" || block.type === "progress") && <label>{t("component.workspace_app_view.data_stage_labels")}
                <textarea className="manor-input" value={formatStageLabels(block.stage_labels)} rows={3} maxLength={1200} disabled={disabled}
                  aria-label={`${t("component.workspace_app_view.data_stage_labels")} ${block.title || block.key}`}
                  placeholder={t("component.workspace_app_view.data_stage_labels_hint")}
                  onChange={(event) => updateStageLabels(event.target.value)} />
              </label>}
              {block.type === "progress" && projectSource && <div className="workspace-app-data-settings__media"><strong>{t("component.workspace_app_view.data_progress_fields")}</strong>
                {progressFields.map((field, fieldIndex) => <div key={`${field.field.join(".")}:${fieldIndex}`}>
                  <input className="manor-input" value={field.label} maxLength={80} disabled={disabled} aria-label={`${t("component.workspace_app_view.data_field_label")} ${fieldIndex + 1}`}
                    onChange={(event) => updateBlock((current) => ({ ...current, fields: progressFields.map((item, index) => index === fieldIndex ? { ...item, label: event.target.value } : item) }))} />
                  <select value={field.field.join(".")} disabled={disabled} aria-label={`${t("component.workspace_app_view.data_field_source")} ${fieldIndex + 1}`}
                    onChange={(event) => updateBlock((current) => ({ ...current, fields: progressFields.map((item, index) => index === fieldIndex ? { ...item, field: event.target.value.split(".") } : item) }))}>
                    {sourceFields.map((path) => <option key={path.join(".")} value={path.join(".")}>{path.join(".")}</option>)}
                  </select>
                  <select value={field.format || "text"} disabled={disabled} aria-label={`${t("component.workspace_app_view.data_field_format")} ${fieldIndex + 1}`}
                    onChange={(event) => updateBlock((current) => ({ ...current, fields: progressFields.map((item, index) => index === fieldIndex ? { ...item, format: event.target.value as WorkspaceAppSummaryField["format"] } : item) }))}>
                    {(["text", "status", "date"] as const).map((format) => <option key={format} value={format}>{t(`component.workspace_app_view.data_format_${format}`)}</option>)}
                  </select>
                  <button type="button" className="workspace-app-settings__icon-button" disabled={disabled} aria-label={`${t("component.workspace_app_view.data_field_remove")} ${fieldIndex + 1}`}
                    onClick={() => updateBlock((current) => ({ ...current, fields: progressFields.filter((_, index) => index !== fieldIndex) }))}><IconTrash size={15} /></button>
                </div>)}
                {progressFields.length < 12 && sourceFields.length > 0 && <Button size="sm" variant="ghost" disabled={disabled} onClick={() => updateBlock((current) => ({ ...current,
                  fields: [...progressFields, { label: sourceFields[0].join("."), field: sourceFields[0], format: "text" }],
                }))}><IconPlus size={14} /> {t("component.workspace_app_view.data_field_add")}</Button>}
              </div>}
              <label>{t("component.workspace_app_view.data_empty_message")}<input className="manor-input" value={block.empty_message || ""} maxLength={240} disabled={disabled}
                aria-label={`${t("component.workspace_app_view.data_empty_message")} ${block.title || block.key}`}
                placeholder={t("component.workspace_app_view.data_empty_message_hint")}
                onChange={(event) => updateBlock((current) => {
                  const empty_message = optionalText(event.target.value);
                  const { empty_message: _emptyMessage, ...rest } = current;
                  return empty_message ? { ...rest, empty_message } : rest;
                })} /></label>
            </div>
          </details>
          {mediaSources.length > 0 && <div className="workspace-app-data-settings__media">
            <strong>{t("component.workspace_app_view.data_media")}</strong>
            {media.map((binding) => <div key={binding.source}>
              <span>{mediaSourceLabel(dataSources, components, binding.source)}</span>
              <input className="manor-input" value={binding.label || ""} maxLength={80} disabled={disabled}
                aria-label={`${t("component.workspace_app_view.data_media_label")} ${mediaSourceLabel(dataSources, components, binding.source)}`}
                placeholder={t("component.workspace_app_view.data_media_label")}
                onChange={(event) => updateMedia(media.map((item) => item.source === binding.source ? { ...item, label: optionalText(event.target.value) || undefined } : item))} />
              <button type="button" className="workspace-app-settings__icon-button" disabled={disabled}
                aria-label={`${t("component.workspace_app_view.data_media_remove")} ${mediaSourceLabel(dataSources, components, binding.source)}`}
                onClick={() => updateMedia(media.filter((item) => item.source !== binding.source))}><IconTrash size={15} /></button>
            </div>)}
            {media.length < 4 && <label>{t("component.workspace_app_view.data_media_add")}
              <select value="" disabled={disabled || !mediaSources.some((source) => !media.some((item) => item.source === source))}
                aria-label={t("component.workspace_app_view.data_media_add")}
                onChange={(event) => { if (event.target.value) updateMedia([...media, { source: event.target.value }]); }}>
                <option value="">{t("component.workspace_app_view.data_media_select")}</option>
                {mediaSources.filter((source) => !media.some((item) => item.source === source)).map((source) => <option key={source} value={source}>{mediaSourceLabel(dataSources, components, source)}</option>)}
              </select>
            </label>}
          </div>}
        </div>;
      };
      return <section key={component.key} className="workspace-app-data-settings__component" aria-label={component.title || component.key}>
        <header>
          <div><strong>{component.title || component.key}</strong>
            <small>{t("component.workspace_app_view.data_visible_count", { count: String(visibleBlocks.length), total: String(component.blocks.length) })}</small>
          </div>
          <div className="workspace-app-data-settings__actions">
            <label className="workspace-app-data-settings__toggle"><input type="checkbox" checked={!display.hidden} disabled={disabled}
              onChange={(event) => updateDisplay(componentIndex, (current) => ({ ...current, hidden: !event.target.checked }))} />
              {t("component.workspace_app_view.data_show_on_page")}</label>
            {components.length > 1 && ([-1, 1] as const).map((direction) => <button key={direction} type="button" className="workspace-app-settings__icon-button"
              disabled={disabled || !components[componentIndex + direction]} onClick={() => onChange(move(components, componentIndex, direction))}
              aria-label={`${t(`component.workspace_app_view.move_${direction === -1 ? "up" : "down"}`)} ${component.title || component.key}`}>
              {direction === -1 ? <IconArrowUp size={16} /> : <IconArrowDown size={16} />}
            </button>)}
          </div>
        </header>
        <div className="workspace-app-data-settings__copy">
          <label>{t("component.workspace_app_view.data_section_name")}<input className="manor-input" value={component.title || ""} maxLength={100} disabled={disabled}
            onChange={(event) => updateComponent(componentIndex, (current) => ({ ...current, title: optionalText(event.target.value) }))} /></label>
          {recordBlocks.length > 0 && <label>{t("component.workspace_app_view.data_visibility")}
            <select value={component.visibility ? `${component.visibility.when}:${component.visibility.block}` : ""} disabled={disabled} aria-label={t("component.workspace_app_view.data_visibility")}
              aria-describedby={`data-visibility-hint-${component.key}`}
              onChange={(event) => updateComponent(componentIndex, (current) => {
                const [when, block] = event.target.value.split(":", 2);
                return { ...current, visibility: block && (when === "has_records" || when === "has_observations") ? { block, when } : undefined };
              })}>
              <option value="">{t("component.workspace_app_view.data_visibility_always")}</option>
              {recordBlocks.map((block) => <option key={`records:${block.key}`} value={`has_records:${block.key}`}>
                {t("component.workspace_app_view.data_visibility_records")}{recordBlocks.length > 1 ? ` · ${block.title || block.key}` : ""}
              </option>)}
              {observationBlocks.map((block) => <option key={`observed:${block.key}`} value={`has_observations:${block.key}`}>
                {t("component.workspace_app_view.data_visibility_observed")}{observationBlocks.length > 1 ? ` · ${block.title || block.key}` : ""}
              </option>)}
            </select>
            <small id={`data-visibility-hint-${component.key}`}>{t(component.visibility ? component.visibility.when === "has_records" ? "component.workspace_app_view.data_visibility_records_hint" : "component.workspace_app_view.data_visibility_hint" : "component.workspace_app_view.data_visibility_always_hint")}</small>
          </label>}
        </div>
        <fieldset className="workspace-app-data-settings__content"><legend>{t("component.workspace_app_view.data_content_to_show")}</legend>
          <div className="workspace-app-data-settings__blocks">{visibleBlocks.map(renderBlock)}</div>
          {!visibleBlocks.length && <p className="workspace-app-data-settings__notice">{t("component.workspace_app_view.data_all_hidden")}</p>}
          {hiddenBlocks.length > 0 && <details className="workspace-app-data-settings__disclosure">
            <summary>{t("component.workspace_app_view.data_hidden_content", { count: String(hiddenBlocks.length) })}</summary>
            {hiddenBlocks.map((block) => <div className="workspace-app-data-settings__hidden-item" key={block.key}>
              <div><span>{block.title || block.key}</span><small>{blockDescription(block)}</small></div>
              <Button variant="ghost" size="sm" disabled={disabled} ariaLabel={`${t("component.workspace_app_view.show")} ${block.title || block.key}`}
                onClick={() => setBlockVisible(componentIndex, block.key, true)}><IconPlus size={14} />{t("component.workspace_app_view.show")}</Button>
            </div>)}
          </details>}
        </fieldset>
        <fieldset className="workspace-app-data-settings__arrangement"><legend>{t("component.workspace_app_view.data_layout")}</legend>
          <div className="workspace-app-data-settings__layout">
            {(["stack", "tabs"] as const).map((layout) => <label key={layout}>
              <input type="radio" name={`data-layout-${component.key}`} checked={display.layout === layout} disabled={disabled} onChange={() => setLayout(componentIndex, layout)} />
              <span>{t(`component.workspace_app_view.data_layout_${layout}`)}<small>{t(`component.workspace_app_view.data_layout_${layout}_hint`)}</small></span>
            </label>)}
          </div>
          <div className="workspace-app-data-settings__tabs">
            {display.groups.map((group, groupIndex) => <div className="workspace-app-data-settings__tab" key={group.key}>
              <div className="workspace-app-data-settings__tab-head">
                <input className="manor-input" value={group.title} maxLength={80} disabled={disabled}
                  aria-label={`${t(display.layout === "tabs" ? "component.workspace_app_view.data_layout_tabs" : "component.workspace_app_view.data_groups")} ${groupIndex + 1}`}
                  onChange={(event) => updateDisplay(componentIndex, (current) => ({ ...current,
                    groups: current.groups.map((item) => item.key === group.key ? { ...item, title: event.target.value } : item),
                  }))} />
                {([-1, 1] as const).map((direction) => <button key={direction} type="button" className="workspace-app-settings__icon-button"
                  disabled={disabled || !display.groups[groupIndex + direction]} onClick={() => updateDisplay(componentIndex, (current) => ({ ...current, groups: move(current.groups, groupIndex, direction) }))}
                  aria-label={`${t(`component.workspace_app_view.move_${direction === -1 ? "up" : "down"}`)} ${group.title || groupIndex + 1}`}>
                  {direction === -1 ? <IconArrowUp size={15} /> : <IconArrowDown size={15} />}
                </button>)}
                <button type="button" className="workspace-app-settings__icon-button" disabled={disabled} onClick={() => removeGroup(componentIndex, groupIndex)}
                  aria-label={`${t(display.layout === "tabs" ? "component.workspace_app_view.data_remove_tab" : "component.workspace_app_view.data_remove_group")} ${group.title || groupIndex + 1}`}><IconTrash size={15} /></button>
              </div>
              <small>{group.blocks.filter((key) => !display.hidden_blocks.includes(key)).map((key) => component.blocks.find((block) => block.key === key)?.title || key).join(" · ") || t("component.workspace_app_view.data_all_hidden")}</small>
            </div>)}
            <div className="workspace-app-data-settings__tab-add">
              <select value={newGroupBlock[component.key] || component.blocks[0]?.key || ""} disabled={disabled} aria-label={t(display.layout === "tabs" ? "component.workspace_app_view.data_add_tab" : "component.workspace_app_view.data_add_group")}
                onChange={(event) => setNewGroupBlock((current) => ({ ...current, [component.key]: event.target.value }))}>
                {component.blocks.map((block) => <option key={block.key} value={block.key}>{block.title || block.key}</option>)}
              </select>
              <Button size="sm" variant="ghost" disabled={disabled} onClick={() => addGroup(componentIndex, component)}><IconPlus size={14} /> {t(display.layout === "tabs" ? "component.workspace_app_view.data_add_tab" : "component.workspace_app_view.data_add_group")}</Button>
            </div>
          </div>
        </fieldset>
        {display.groups.some((group) => !group.title.trim()) && <p className="workspace-app-data-settings__error" role="alert">{t("component.workspace_app_view.data_groups_invalid")}</p>}
        <details className="workspace-app-data-settings__disclosure"><summary>{t("component.workspace_app_view.data_more_options")}</summary>
          <div className="workspace-app-data-settings__copy">
            <label>{t("component.workspace_app_view.data_section_description")}<textarea className="manor-input" rows={2} value={component.description || ""} maxLength={300} disabled={disabled}
              onChange={(event) => updateComponent(componentIndex, (current) => ({ ...current, description: optionalText(event.target.value) }))} /></label>
            {component.context.kind !== "workspace" && <label>{t("component.workspace_app_view.data_context_label")}<input className="manor-input" value={component.context.label || ""} maxLength={80} disabled={disabled}
              onChange={(event) => updateComponent(componentIndex, (current) => ({ ...current, context: { ...current.context, label: optionalText(event.target.value) } }))} />
              <small>{t("component.workspace_app_view.data_context_hint")}</small></label>}
          </div>
          <p className="workspace-app-data-settings__notice">{t("component.workspace_app_view.data_binding_hint")}</p>
        </details>
      </section>;
    })}</div>
  </section>;
}
