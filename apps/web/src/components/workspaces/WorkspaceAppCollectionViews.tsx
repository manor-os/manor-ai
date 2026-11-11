import { useMemo, useState, type ReactNode } from "react";
import type { WorkspaceAppDataItem } from "../../lib/api";
import { formatDateLong, getPreferredTimeZone } from "../../lib/format";
import { getLocale, t } from "../../lib/i18n";
import { IconChevronDown, IconChevronRight, IconEye, IconPin } from "../icons";
import AnchoredPopover from "../ui/AnchoredPopover";
import Button from "../ui/Button";
import Checkbox from "../ui/Checkbox";
import { ganttLayout, placeGroups, placeMapHref, timelineDays } from "./workspaceAppCollection";
import "./WorkspaceAppCollectionViews.css";

/** The App's other ways to lay a list out: in time, as bars, by place and as a tree. Each row opens the
 *  record's whole card in the global detail drawer. */

type Item = WorkspaceAppDataItem;
export type RowPresenter = {
  nameOf: (item: Item) => string;
  statusOf: (item: Item) => string | null;
  onOpen: (item: Item, from: HTMLElement) => void;
};

function OpenRow({ item, presenter, children, className = "" }: { item: Item; presenter: RowPresenter; children?: ReactNode; className?: string }) {
  const status = presenter.statusOf(item);
  return <button type="button" className={`workspace-app-views__row ${className}`} onClick={(event) => presenter.onOpen(item, event.currentTarget)}>
    <span className="workspace-app-views__row-name">{presenter.nameOf(item)}</span>
    {children}
    {status && <span className="workspace-app-views__row-status">{status}</span>}
  </button>;
}

function timeText(value: string | null | undefined, locale: string, timeZone: string): string {
  if (!value || /^\d{4}-\d{2}-\d{2}$/.test(value)) return "";
  const at = Date.parse(value);
  if (Number.isNaN(at)) return "";
  try {
    return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", timeZone }).format(at);
  } catch {
    return "";
  }
}

function dayText(day: string, locale: string): string {
  try {
    return new Intl.DateTimeFormat(locale, { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC" })
      .format(new Date(`${day}T00:00:00Z`));
  } catch {
    return day;
  }
}

/** Events in time order, a heading per day, each at its time. */
export function TimelineView({ items, presenter, title }: { items: Item[]; presenter: RowPresenter; title: string }) {
  const locale = getLocale();
  const timeZone = getPreferredTimeZone();
  const { days, undated } = useMemo(() => timelineDays(items, timeZone), [items, timeZone]);
  return <div className="workspace-app-views__timeline" role="list" aria-label={t("component.workspace_app_view.view_timeline", { list: title })}>
    {days.map(({ day, items: members }) => <section key={day} role="listitem" className="workspace-app-views__day">
      <h5><time dateTime={day}>{dayText(day, locale)}</time></h5>
      <ol>{members.map((item) => <li key={item.id}>
        <span className="workspace-app-views__time mono">{timeText(item.time?.start, locale, timeZone)}</span>
        <OpenRow item={item} presenter={presenter} />
      </li>)}</ol>
    </section>)}
    {undated.length > 0 && <section role="listitem" className="workspace-app-views__day">
      <h5>{t("component.workspace_app_view.view_undated")}</h5>
      <ol>{undated.map((item) => <li key={item.id}><span className="workspace-app-views__time" /><OpenRow item={item} presenter={presenter} /></li>)}</ol>
    </section>}
  </div>;
}

/** Bars from each record's start to its end on one time axis; a record it waits for is named under its bar. */
export function GanttView({ items, presenter, title }: { items: Item[]; presenter: RowPresenter; title: string }) {
  const locale = getLocale();
  const layout = useMemo(() => ganttLayout(items), [items]);
  const names = new Map(items.map((item) => [item.id, presenter.nameOf(item)]));
  const ticks = useMemo(() => {
    if (!layout.bars.length) return [];
    const span = layout.last - layout.first;
    return [0, 0.25, 0.5, 0.75, 1].map((share) => ({ share, at: layout.first + span * share }));
  }, [layout]);
  const dateFormat = new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", timeZone: getPreferredTimeZone() });
  const now = Date.now();
  const today = now >= layout.first && now <= layout.last ? (now - layout.first) / (layout.last - layout.first) : null;
  return <div className="workspace-app-views__gantt" role="list" aria-label={t("component.workspace_app_view.view_gantt", { list: title })}>
    {layout.bars.length > 0 && <div className="workspace-app-views__gantt-axis" aria-hidden="true">
      <span />
      <span className="workspace-app-views__gantt-ticks">{ticks.map((tick) => <span key={tick.share} style={{ left: `${tick.share * 100}%` }}>{dateFormat.format(tick.at)}</span>)}</span>
    </div>}
    {layout.bars.map((bar) => {
      const waits = (bar.item.time?.depends_on || []).map((id) => names.get(id)).filter(Boolean);
      return <div key={bar.item.id} role="listitem" className="workspace-app-views__gantt-row">
        <OpenRow item={bar.item} presenter={presenter} className="workspace-app-views__gantt-name" />
        <span className="workspace-app-views__gantt-lane">
          {today !== null && <span className="workspace-app-views__gantt-today" style={{ left: `${today * 100}%` }} aria-hidden="true" />}
          <span className="workspace-app-views__gantt-bar" style={{ left: `${bar.left * 100}%`, width: `${bar.width * 100}%` }}
            title={`${formatDateLong(bar.item.time?.start || undefined)}${bar.item.time?.end ? ` – ${formatDateLong(bar.item.time.end)}` : ""}`} />
          {waits.length > 0 && <small className="workspace-app-views__gantt-waits" style={{ left: `${bar.left * 100}%` }}>
            {t("component.workspace_app_view.view_waits_for", { names: waits.join(", ") })}</small>}
        </span>
      </div>;
    })}
    {layout.untimed.length > 0 && <div className="workspace-app-views__unplaced">
      <h5>{t("component.workspace_app_view.view_undated")}</h5>
      {layout.untimed.map((item) => <OpenRow key={item.id} item={item} presenter={presenter} />)}
    </div>}
  </div>;
}

/** Records grouped by where they are, each place with a link to see it on a map. No map is drawn here. */
export function PlacesView({ items, presenter, title }: { items: Item[]; presenter: RowPresenter; title: string }) {
  const locale = getLocale();
  const { places, unplaced } = useMemo(() => placeGroups(items), [items]);
  return <div className="workspace-app-views__places" role="list" aria-label={t("component.workspace_app_view.view_places", { list: title })}>
    {places.map((place) => <section key={place.address} role="listitem" className="workspace-app-views__place">
      <header>
        <IconPin size={14} aria-hidden="true" />
        <span className="workspace-app-views__place-name">{place.name || place.address}</span>
        <span className="mono workspace-app-views__count">{place.items.length}</span>
        <a href={placeMapHref(place, locale)} target="_blank" rel="noreferrer">{t("component.workspace_app_view.view_open_map")}</a>
      </header>
      {place.name && <small className="workspace-app-views__place-address">{place.address}</small>}
      <div className="workspace-app-views__place-rows">{place.items.map((item) => <OpenRow key={item.id} item={item} presenter={presenter} />)}</div>
    </section>)}
    {unplaced.length > 0 && <section role="listitem" className="workspace-app-views__place">
      <header><span className="workspace-app-views__place-name">{t("component.workspace_app_view.view_no_place")}</span>
        <span className="mono workspace-app-views__count">{unplaced.length}</span></header>
      <div className="workspace-app-views__place-rows">{unplaced.map((item) => <OpenRow key={item.id} item={item} presenter={presenter} />)}</div>
    </section>}
  </div>;
}

export type TreeLoader = (parent: Item, level: number, cursor?: string | null) => Promise<{ items: Item[]; next_cursor?: string | null }>;

function TreeNode({ item, level, depth, presenter, load }: { item: Item; level: number; depth: number; presenter: RowPresenter; load: TreeLoader }) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<Item[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const count = item.child_count ?? 0;
  const branch = level < depth && count > 0;
  const read = async (after?: string | null) => {
    setBusy(true);
    setFailed(false);
    try {
      const page = await load(item, level + 1, after);
      setChildren((current) => [...(after ? current || [] : []), ...page.items]);
      setCursor(page.next_cursor || null);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && children === null) void read();
  };
  return <li role="treeitem" aria-level={level + 1} aria-expanded={branch ? open : undefined} className="workspace-app-views__node">
    <div className="workspace-app-views__node-row" style={{ paddingInlineStart: level * 18 }}>
      {branch ? <button type="button" className="workspace-app-views__twisty" onClick={toggle}
        aria-label={t(open ? "component.workspace_app_view.view_collapse" : "component.workspace_app_view.view_expand", { name: presenter.nameOf(item) })}>
        {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
      </button> : <span className="workspace-app-views__twisty" aria-hidden="true" />}
      <OpenRow item={item} presenter={presenter}>
        {branch && <span className="mono workspace-app-views__count">{count}</span>}
      </OpenRow>
    </div>
    {open && <ul role="group">
      {(children || []).map((child) => <TreeNode key={child.id} item={child} level={level + 1} depth={depth} presenter={presenter} load={load} />)}
      {busy && <li className="workspace-app-views__tree-note" style={{ paddingInlineStart: (level + 1) * 18 + 24 }}>{t("component.workspace_app_view.data_loading")}</li>}
      {failed && <li className="workspace-app-views__tree-note is-error" style={{ paddingInlineStart: (level + 1) * 18 + 24 }}>{t("component.workspace_app_view.data_error")}</li>}
      {cursor && !busy && <li style={{ paddingInlineStart: (level + 1) * 18 + 24 }}>
        <Button size="sm" variant="ghost" onClick={() => void read(cursor)}>{t("component.workspace_app_view.data_more")}</Button></li>}
    </ul>}
  </li>;
}

/** Each record and the projects inside it (a series, its episodes, their shots), opened one level at a time. */
export function TreeView({ items, depth, presenter, load, title }: { items: Item[]; depth: number; presenter: RowPresenter; load: TreeLoader; title: string }) {
  return <ul className="workspace-app-views__tree" role="tree" aria-label={t("component.workspace_app_view.view_tree", { list: title })}>
    {items.map((item) => <TreeNode key={item.id} item={item} level={0} depth={depth} presenter={presenter} load={load} />)}
  </ul>;
}

/** Which of a table's columns this viewer sees; the name always shows. */
export function ColumnsMenu({ columns, hidden, onChange }: { columns: Array<{ key: string; label: string }>; hidden: ReadonlySet<string>; onChange: (hidden: Set<string>) => void }) {
  if (columns.length < 2) return null;
  return <AnchoredPopover ariaLabel={t("component.workspace_app_view.data_columns")} width={240} align="right"
    trigger={<Button size="sm" variant="ghost" ariaLabel={t("component.workspace_app_view.data_columns")} title={t("component.workspace_app_view.data_columns")}>
      <IconEye size={14} />{hidden.size ? <span className="mono">{columns.length - hidden.size}/{columns.length}</span> : null}
    </Button>}>
    <div className="workspace-app-views__columns">
      {columns.map((column) => <Checkbox key={column.key} size="sm" label={column.label} checked={!hidden.has(column.key)}
        onChange={(shown) => {
          const next = new Set(hidden);
          if (shown) next.delete(column.key); else next.add(column.key);
          onChange(next);
        }} />)}
      {hidden.size > 0 && <Button size="sm" variant="ghost" onClick={() => onChange(new Set())}>{t("component.workspace_app_view.data_columns_all")}</Button>}
    </div>
  </AnchoredPopover>;
}
