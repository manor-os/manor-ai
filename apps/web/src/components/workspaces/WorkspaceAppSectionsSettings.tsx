import type { WorkspaceAppBlockSpec, WorkspaceAppSectionBlock, WorkspaceAppSectionConfig, WorkspaceAppView, WorkspaceAppZone } from "../../lib/api";
import { t } from "../../lib/i18n";
import { IconArrowDown, IconArrowUp, IconTrash } from "../icons";
import Button from "../ui/Button";
import Select from "../ui/Select";

const MAX_SECTIONS = 8;

/** Every block in the contract has a name; the Record type keeps this complete. */
const BLOCK_TITLES: Record<WorkspaceAppSectionBlock, string> = {
  connections: "component.workspace_app_view.connections_title",
  actions: "component.workspace_app_view.all_actions",
  queue: "component.workspace_app_view.queue_title",
  metrics: "component.workspace_app_view.metrics_title",
  outputs: "component.workspace_app_view.outputs_title",
};

/** Zones in page order; blocks only move within their own zone. */
const ZONES: Array<{ zone: WorkspaceAppZone; title: string }> = [
  { zone: "notices", title: "component.workspace_app_view.notices_title" },
  { zone: "functions", title: "component.workspace_app_view.functions_title" },
  { zone: "data", title: "component.workspace_app_view.metrics_title" },
  { zone: "outputs", title: "component.workspace_app_view.outputs_title" },
];

function nextId(block: WorkspaceAppSectionBlock, sections: WorkspaceAppSectionConfig[]): string {
  const taken = new Set(sections.map((section) => section.id));
  if (!taken.has(block)) return block;
  let index = 2;
  while (taken.has(`${block}-${index}`)) index += 1;
  return `${block}-${index}`;
}

/**
 * The blocks the App page shows, in order, edited against the server's block
 * catalog: which blocks exist, their views, sources and whether they repeat.
 * ``null`` is the default page, whose empty blocks hide themselves.
 */
export default function WorkspaceAppSectionsSettings({ view, sections, disabled, onChange }: {
  view: WorkspaceAppView;
  sections: WorkspaceAppSectionConfig[] | null | undefined;
  disabled: boolean;
  onChange: (sections: WorkspaceAppSectionConfig[] | null) => void;
}) {
  const catalog = new Map<WorkspaceAppSectionBlock, WorkspaceAppBlockSpec>((view.blocks || []).map((spec) => [spec.block, spec]));
  // Editing the list keeps what a section names beyond its place (its numbers, its heading).
  const current: WorkspaceAppSectionConfig[] = sections ?? (view.sections || []).map(({ id, block, source, view: shown, limit, items, title }) => ({
    id, block, source, view: shown, limit, ...(items ? { items } : {}), ...(title ? { title } : {}),
  }));
  const isDefault = sections == null;
  const flowTitle = (source: string | null) => source?.startsWith("workflow:")
    ? view.workflow_options.find((flow) => `workflow:${flow.binding_id}` === source)?.title ?? t("component.workspace_app_view.section_unavailable")
    : null;
  const zoneOf = (block: WorkspaceAppSectionBlock): WorkspaceAppZone => catalog.get(block)?.zone ?? "data";
  const zoneIndex = (block: WorkspaceAppSectionBlock) => ZONES.findIndex((item) => item.zone === zoneOf(block));
  // Saved in zone order, the order the page always shows them in.
  const edit = (next: WorkspaceAppSectionConfig[]) => onChange(
    next.map((section, index) => ({ section, index }))
      .sort((a, b) => zoneIndex(a.section.block) - zoneIndex(b.section.block) || a.index - b.index)
      .map(({ section }) => section),
  );
  const move = (index: number, delta: number) => {
    const next = [...current];
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    edit(next);
  };
  // A block can swap only with a neighbour in the same zone.
  const canMove = (index: number, delta: number) => {
    const neighbour = current[index + delta];
    return Boolean(neighbour) && zoneOf(neighbour.block) === zoneOf(current[index].block);
  };
  const patch = (index: number, change: Partial<WorkspaceAppSectionConfig>) =>
    edit(current.map((section, position) => position === index ? { ...section, ...change } : section));
  const addable = [...catalog.values()].filter((spec) => spec.multiple || !current.some((section) => section.block === spec.block));
  const add = (spec: WorkspaceAppBlockSpec) => edit([...current, {
    id: nextId(spec.block, current), block: spec.block, source: spec.default_source, view: spec.views[0], limit: null,
  }]);
  const flowSources = view.workflow_options.map((flow) => ({ value: `workflow:${flow.binding_id}`, label: flow.title }));

  return <div className="workspace-app-settings__sections">
    {isDefault && <p className="workspace-app-settings__hint">{t("component.workspace_app_view.sections_default_hint")}</p>}
    {ZONES.map(({ zone, title: zoneTitle }) => {
      const members = current.map((section, index) => ({ section, index })).filter(({ section }) => zoneOf(section.block) === zone);
      if (!members.length) return null;
      return <div key={zone} className="workspace-app-settings__zone" role="group" aria-label={t(zoneTitle)}>
        <span className="workspace-app-settings__zone-title">{t(zoneTitle)}</span>
    <ol className="workspace-app-settings__section-list">
      {members.map(({ section, index }) => {
        const spec = catalog.get(section.block);
        const title = flowTitle(section.source) || t(BLOCK_TITLES[section.block]);
        const scopes = spec?.source_kinds.includes("workflow") ? [
          ...(spec.default_source ? [{ value: spec.default_source, label: t("component.workspace_app_view.sections_source_workspace") }] : []),
          ...flowSources,
        ] : [];
        return <li key={section.id} className="workspace-app-settings__block" role="group" aria-label={title}>
          <div className="workspace-app-settings__block-top">
            <strong>{title}</strong>
            {section.source?.startsWith("workflow:") && <small>{t(BLOCK_TITLES[section.block])}</small>}
            <div className="workspace-app-settings__order">
              <button type="button" className="workspace-app-settings__icon-button" onClick={() => move(index, -1)} disabled={disabled || !canMove(index, -1)}
                aria-label={`${t("component.workspace_app_view.move_up")} ${title}`}><IconArrowUp size={16} /></button>
              <button type="button" className="workspace-app-settings__icon-button" onClick={() => move(index, 1)} disabled={disabled || !canMove(index, 1)}
                aria-label={`${t("component.workspace_app_view.move_down")} ${title}`}><IconArrowDown size={16} /></button>
              {spec?.removable !== false && <button type="button" className="workspace-app-settings__icon-button" disabled={disabled}
                onClick={() => edit(current.filter((_, position) => position !== index))}
                aria-label={`${t("component.workspace_app_view.sections_remove")} ${title}`}><IconTrash size={16} /></button>}
            </div>
          </div>
          {(scopes.length > 1 || (spec?.views.length || 0) > 1) && <div className="workspace-app-settings__block-fields">
            {scopes.length > 1 && <Select value={section.source || spec?.default_source || ""} onChange={(value) => patch(index, { source: value })}
              disabled={disabled} ariaLabel={`${t("component.workspace_app_view.sections_source")} ${title}`} options={scopes} />}
            {spec && spec.views.length > 1 && <Select value={section.view} onChange={(value) => patch(index, { view: value })} disabled={disabled}
              ariaLabel={`${t("component.workspace_app_view.sections_view")} ${title}`}
              options={spec.views.map((value) => ({ value, label: t(`component.workspace_app_view.sections_view_${value}`) }))} />}
          </div>}
        </li>;
      })}
    </ol>
      </div>;
    })}
    <div className="workspace-app-settings__sections-actions">
      {addable.length > 0 && current.length < MAX_SECTIONS && <Select value="" disabled={disabled}
        onChange={(value) => { const spec = catalog.get(value as WorkspaceAppSectionBlock); if (spec) add(spec); }}
        ariaLabel={t("component.workspace_app_view.sections_add")} placeholder={t("component.workspace_app_view.sections_add")}
        options={addable.map((spec) => ({ value: spec.block, label: t(BLOCK_TITLES[spec.block]) }))} />}
      {!isDefault && <Button variant="ghost" size="sm" onClick={() => onChange(null)} disabled={disabled}>
        {t("component.workspace_app_view.sections_reset")}
      </Button>}
    </div>
  </div>;
}
