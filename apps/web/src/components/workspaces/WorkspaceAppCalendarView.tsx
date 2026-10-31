import { useEffect, useId, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";

import { api, type WorkspaceAppDataBlock, type WorkspaceAppDataBlockResult, type WorkspaceAppDataComponent, type WorkspaceAppDataContext, type WorkspaceAppDataItem } from "../../lib/api";
import { formatDateCompact, getPreferredTimeZone } from "../../lib/format";
import { getLocale, t } from "../../lib/i18n";
import { resolveIntegrationBrand } from "../../lib/brands/catalog";
import { IconArrowRight, IconCheck, IconCheckCircle, IconChevronLeft, IconChevronRight, IconChevronDown, IconCircleDot, IconClock, IconClose, IconWarning } from "../icons";
import IntegrationLogo from "../IntegrationLogo";
import { titleHues } from "./workspaceAppRunDisplay";
import MakingMark from "./WorkspaceAppMakingMark";
import type { WorkspaceAppRunOpener } from "./WorkspaceAppDataView";
import { calendarAttentionCount, calendarDateKey, calendarDisplayStage, calendarItemsByDay, calendarMonthDays, calendarRecordProjectId, calendarSummaryCounts, shiftCalendarMonth } from "./workspaceAppCalendar";
import "./WorkspaceAppCalendarView.css";

type Props = {
  workspaceId: string;
  component: WorkspaceAppDataComponent;
  preview?: boolean;
  renderBlock: (block: WorkspaceAppDataBlockResult, config: WorkspaceAppDataBlock | undefined, onMore: (block: WorkspaceAppDataBlockResult) => void, loadingMore: boolean) => ReactNode;
  renderItemDetails: (item: WorkspaceAppDataItem, records: WorkspaceAppDataItem[]) => ReactNode;
  renderItemMetrics: (records: WorkspaceAppDataItem[]) => ReactNode;
  renderContextDetails: (context: WorkspaceAppDataContext) => ReactNode;
  /** What the person can answer on a post right now (a review waiting on them), shown on its row. */
  renderItemResponse?: (item: WorkspaceAppDataItem) => ReactNode;
  onFocusChange?: (componentKey: string, focused: boolean) => void;
  /** Opens a run where its progress shows. */
  openRun?: WorkspaceAppRunOpener;
};
type CalendarSummary = Pick<WorkspaceAppDataItem, "total" | "counts_by_stage" | "counts_include_execution"> & {
  next?: { id: string; status?: string | null; stage?: string; execution?: WorkspaceAppDataItem["execution"] } | null;
};
type CalendarView = { month: string; date: string | null; expanded: string | null; dayOpen: boolean; expandedDays: string[] };
const copy = (key: string, vars?: Record<string, string | number>) => t(`component.workspace_app_view.calendar_${key}`, vars);
// "1 scheduled item", "2 scheduled items": a key with a singular form reads it for one.
const countCopy = (key: "item_count" | "attention", count: number) => copy(count === 1 ? `${key}_one` : key, { count });
const titleOf = (item: WorkspaceAppDataItem) => item.title || item.label || t("component.workspace_app_view.data_untitled");
const readable = (value: string) => value.replace(/[_-]+/g, " ").replace(/^\w/, (letter) => letter.toUpperCase());

type CalendarStatusTone = "processing" | "waiting" | "ready" | "published" | "attention" | "error" | "cancelled" | "neutral";
function CalendarStatusIcon({ tone }: { tone: CalendarStatusTone }) {
  const Icon = { processing: IconCircleDot, waiting: IconClock, ready: IconCheckCircle, published: IconCheck,
    attention: IconWarning, error: IconClose, cancelled: IconClose, neutral: IconCircleDot }[tone];
  return <span className={`workspace-app-calendar__status-icon is-${tone}`} aria-hidden="true">
    {tone === "processing" ? <span className="workspace-app-calendar__spinner" /> : <Icon size={14} />}
  </span>;
}

function ContextFields({ fields, secondary = false }: { fields?: WorkspaceAppDataItem["fields"]; secondary?: boolean }) {
  const dates = (fields || []).filter((field) => field.format === "date" && typeof field.value === "string").map((field) => String(field.value));
  return <span className={`workspace-app-calendar__context-fields${secondary ? " is-secondary" : ""}`}>{(fields || []).filter((field) => field.value != null && field.value !== "").map((field, index) => <span key={`${field.label}:${index}`} title={field.label}>
    <small>{field.label} </small>
    {field.format === "date" && typeof field.value === "string"
      ? <time dateTime={field.value}>{formatDateCompact(field.value, dates)}</time> : String(field.value)}
    {field.secondary && <small> · {field.secondary}</small>}
  </span>)}</span>;
}

export default function WorkspaceAppCalendarView({ workspaceId, component, preview = false, renderBlock, renderItemDetails, renderItemMetrics, renderContextDetails, renderItemResponse, onFocusChange, openRun }: Props) {
  const calendar = component.display!.calendar!;
  const eventConfig = component.blocks.find((block) => block.key === calendar.block);
  const rollupConfig = component.blocks.find((block) => block.key === calendar.rollup_block);
  const resultKeys = component.blocks.filter((block) => block.type === "records" && block.metrics_source
    && !component.display?.hidden_blocks?.includes(block.key)).map((block) => block.key);
  const timeZone = getPreferredTimeZone();
  const locale = getLocale();
  const instagramGradientId = useId();
  const rootRef = useRef<HTMLElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const collectionPosition = useRef<{ element: HTMLElement; top: number } | null>(null);
  const [compact, setCompact] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [views, setViews] = useState<Record<string, CalendarView>>({});
  const [extraBlocks, setExtraBlocks] = useState<Record<string, WorkspaceAppDataBlockResult>>({});
  const [loadingBlock, setLoadingBlock] = useState<string | null>(null);
  const [moreError, setMoreError] = useState(false);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const scope = `${workspaceId}:${component.key}:${selectedId}:${JSON.stringify(calendar)}`;
  const activeScope = useRef(scope);
  activeScope.current = scope;

  useEffect(() => {
    if (!rootRef.current) return;
    const observer = new ResizeObserver(([entry]) => setCompact(entry.contentRect.width < 640));
    observer.observe(rootRef.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => { setExtraBlocks({}); setLoadingBlock(null); setMoreError(false); }, [scope]);
  useEffect(() => { setSelectedId(null); setViews({}); setOpenGroups({}); }, [workspaceId, component.key]);
  useEffect(() => {
    onFocusChange?.(component.key, Boolean(selectedId));
    return () => onFocusChange?.(component.key, false);
  }, [component.key, onFocusChange, selectedId]);
  useEffect(() => {
    if (!selectedId) return;
    const frame = requestAnimationFrame(() => {
      if (collectionPosition.current) collectionPosition.current.element.scrollTop = 0;
      backRef.current?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedId]);

  const contextsQuery = useInfiniteQuery({
    queryKey: ["workspace-app-data-contexts", workspaceId, component.key, component.visibility, calendar],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => api.workspaces.appViewContexts(workspaceId, component.key, { limit: 50, cursor: pageParam, signal }),
    getNextPageParam: (page) => page.next_cursor || undefined,
    enabled: !preview && Boolean(workspaceId), staleTime: 10_000,
    // While one is being prepared its progress moves; otherwise the list rarely changes.
    refetchInterval: (query) => (query.state.data?.pages[0]?.making?.length ? 10_000 : 30_000),
  });
  const contexts = useMemo(() => [...new Map((contextsQuery.data?.pages.flatMap((page) => page.items) || []).map((item) => [item.id, item])).values()], [contextsQuery.data]);
  const making = contextsQuery.data?.pages[0]?.making || [];
  const context = contexts.find((item) => item.id === selectedId);
  const dataQuery = useInfiniteQuery({
    queryKey: ["workspace-app-data", workspaceId, component.key, context?.kind, context?.id, "calendar", calendar, component.visibility],
    initialPageParam: undefined as { block: string; cursor: string } | undefined,
    queryFn: ({ signal, pageParam }) => api.workspaces.appViewComponentData(workspaceId, component.key, {
      contextKind: context!.kind, contextId: context!.id, signal, limit: 100,
      ...pageParam,
    }),
    getNextPageParam: (_page, pages) => {
      const latest = new Map(pages.flatMap((page) => page.blocks).map((block) => [block.key, block]));
      for (const key of [calendar.block, ...resultKeys]) {
        const block = latest.get(key);
        if (block?.next_cursor && block.status !== "error" && !block.error) return { block: key, cursor: block.next_cursor };
      }
      return undefined;
    },
    enabled: !preview && Boolean(context), staleTime: 15_000, refetchInterval: 30_000,
  });
  const firstBlocks = useMemo(() => {
    const merged = new Map<string, WorkspaceAppDataBlockResult>();
    for (const block of dataQuery.data?.pages.flatMap((page) => page.blocks) || []) {
      const previous = merged.get(block.key);
      merged.set(block.key, previous ? { ...block, items: [...new Map([...previous.items, ...block.items].map((item) => [item.id, item])).values()] } : block);
    }
    return [...merged.values()];
  }, [dataQuery.data]);
  const eventPages = useMemo(() => dataQuery.data?.pages.flatMap((page) => page.blocks.filter((block) => block.key === calendar.block)) || [], [dataQuery.data, calendar.block]);
  const eventError = eventPages.find((block) => block.status === "error" || block.error);
  // Finish the existing cursor chain before claiming that a day's count is complete.
  useEffect(() => {
    if (dataQuery.hasNextPage && !dataQuery.isFetching && !dataQuery.isError && !eventError) void dataQuery.fetchNextPage();
  }, [dataQuery.hasNextPage, dataQuery.isFetching, dataQuery.isError, dataQuery.data?.pages.length, eventError, dataQuery.fetchNextPage]);
  const schedule = useMemo(() => calendarItemsByDay(eventPages.flatMap((block) => block.items), timeZone), [eventPages, timeZone]);
  const scheduleReady = Boolean(eventPages.length) && !eventError && !dataQuery.hasNextPage && !dataQuery.isError;
  const rollup = firstBlocks?.find((block) => block.key === calendar.rollup_block);
  const summary = rollup?.status === "ready" ? rollup.items[0] : undefined;
  const contextFields = firstBlocks?.find((block) => block.key === calendar.context_block)?.items.flatMap((item) => item.fields || []) || context?.summary?.fields;
  const completionStages = rollupConfig?.completion?.stages || [];
  const completedSet = new Set(completionStages);
  const today = calendarDateKey(new Date().toISOString(), timeZone)!;
  const firstDay = calendarDateKey(context?.summary?.next?.calendar_at, timeZone)
    || calendarDateKey(schedule.items.find((item) => !completedSet.has(item.status || ""))?.calendar_at, timeZone)
    || [...schedule.days.keys()].at(-1) || today;
  const initialView: CalendarView = { month: firstDay.slice(0, 7), date: firstDay, expanded: null, dayOpen: false, expandedDays: [] };
  const view = (selectedId && views[selectedId]) || initialView;
  useEffect(() => {
    if (selectedId && scheduleReady) setViews((current) => current[selectedId] ? current : {
      ...current, [selectedId]: { month: firstDay.slice(0, 7), date: firstDay, expanded: null, dayOpen: false, expandedDays: [] },
    });
  }, [selectedId, scheduleReady, firstDay]);
  const updateView = (update: Partial<CalendarView>) => {
    if (selectedId) setViews((current) => ({ ...current, [selectedId]: { ...(current[selectedId] || initialView), ...update } }));
  };
  const days = calendarMonthDays(view.month);
  const dayItems = view.date ? schedule.days.get(view.date) || [] : [];
  const toggleDayItems = (day: string) => updateView({ expandedDays: view.expandedDays.includes(day)
    ? view.expandedDays.filter((value) => value !== day) : [...view.expandedDays, day] });
  const showAllDayItems = Boolean(view.date && view.expandedDays.includes(view.date));
  const dateTitle = (day: string, options: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat(locale, { ...options, timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));
  const clock = (item: WorkspaceAppDataItem) => item.calendar_at && !Number.isNaN(Date.parse(item.calendar_at))
    ? new Intl.DateTimeFormat(locale, { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(item.calendar_at)) : "—";
  const executionLabels: Record<string, string> = { failed: "failed", cancelled: "cancelled", paused: "needs_attention", pending: "waiting" };
  const stageLabel = (stage: string) => eventConfig?.stage_labels?.[stage] || rollupConfig?.stage_labels?.[stage]
    || (executionLabels[stage] ? t(`component.workspace_app_view.${executionLabels[stage]}`) : readable(stage));
  const displayStage = (item: WorkspaceAppDataItem) => calendarDisplayStage(item, completionStages);
  const stageTone = (stage: string): CalendarStatusTone => {
    if (completedSet.has(stage)) return "published";
    if (["failed", "expired", "error"].includes(stage)) return "error";
    if (calendar.attention_stages.includes(stage) || stage === "paused") return "attention";
    if (["leased", "running", "in_progress", "processing"].includes(stage)) return "processing";
    if (stage === "ready") return "ready";
    if (["scheduled", "pending", "waiting"].includes(stage)) return "waiting";
    if (["cancelled", "skipped"].includes(stage)) return "cancelled";
    return "neutral";
  };
  const issueCount = (items: WorkspaceAppDataItem[]) => items.filter((item) => ["attention", "error"].includes(stageTone(displayStage(item)))).length;
  const openDay = (day: string, expanded: string | null = null) => {
    triggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    updateView({ date: day, month: day.slice(0, 7), dayOpen: true, expanded });
  };
  const closeDay = () => {
    updateView({ dayOpen: false });
    requestAnimationFrame(() => triggerRef.current?.isConnected && triggerRef.current.focus());
  };
  useEffect(() => {
    if (!view.dayOpen) return;
    panelRef.current?.focus({ preventScroll: true });
    // Beside the month it is already in view; stacked under it, it is brought up.
    panelRef.current?.scrollIntoView({ block: "nearest" });
  }, [view.dayOpen, view.date]);
  // A problem named in the header opens the first post it counts, on its day.
  const firstItemWith = (matches: (stage: string) => boolean) => schedule.items.find((item) => matches(displayStage(item)) && calendarDateKey(item.calendar_at, timeZone));
  const jumpTo = (item: WorkspaceAppDataItem) => {
    openDay(calendarDateKey(item.calendar_at, timeZone)!, item.id);
    requestAnimationFrame(() => rootRef.current?.querySelector(`[data-testid="calendar-entry-${CSS.escape(item.id)}"]`)?.scrollIntoView({ block: "nearest" }));
  };

  // What a campaign's rollup says: how many are done, what the rest are doing, and what needs the person.
  const summarize = (value: CalendarSummary, knownItems?: WorkspaceAppDataItem[]) => {
    const next = value.next;
    const counts = calendarSummaryCounts(value, knownItems || (next
      ? [{ id: next.id, status: next.status ?? next.stage, execution: next.execution }] : []), completionStages);
    const complete = completionStages.reduce((total, stage) => total + (counts[stage] || 0), 0);
    const stages = Object.entries(counts).filter(([stage, count]) => count > 0 && !completedSet.has(stage) && !calendar.attention_stages.includes(stage));
    return { total: value.total || 0, complete, stages, attention: calendarAttentionCount(counts, calendar.attention_stages) };
  };
  const summaryState = (value?: CalendarSummary) => value?.total === undefined
    ? <span className="workspace-app-calendar__loading">{copy("summary_unavailable")}</span>
    : <span className="workspace-app-calendar__empty">{rollupConfig?.empty_message || copy("empty_schedule")}</span>;
  const completionLabel = rollupConfig?.completion?.label || t("component.workspace_app_view.data_total_items");
  const completionFigure = (complete: number, total: number) => rollupConfig?.completion ? `${complete}/${total}` : String(total);
  const pill = (key: string, className: string, content: ReactNode, target?: WorkspaceAppDataItem) => target
    ? <button type="button" key={key} className={`${className} is-jump`} onClick={() => jumpTo(target)}>{content}<IconArrowRight size={12} aria-hidden="true" /></button>
    : <span key={key} className={className}>{content}</span>;
  const stagePill = ([stage, count]: [string, number], jumpable = false) => pill(stage, `is-${stageTone(stage)}`,
    <><CalendarStatusIcon tone={stageTone(stage)} />{count} {stageLabel(stage)}</>,
    jumpable && stageTone(stage) === "error" ? firstItemWith((itemStage) => itemStage === stage) : undefined);
  const attentionPill = (attention: number, jumpable = false) => attention > 0 && pill("attention", "workspace-app-calendar__attention",
    <><CalendarStatusIcon tone="attention" />{countCopy("attention", attention)}</>,
    jumpable ? firstItemWith((itemStage) => calendar.attention_stages.includes(itemStage)) : undefined);
  const showSummary = (value?: CalendarSummary, knownItems?: WorkspaceAppDataItem[]) => {
    if (!value?.total) return summaryState(value);
    // Only the open campaign's header knows its posts, so only there a problem can be opened.
    const jumpable = Boolean(knownItems);
    const { total, complete, stages, attention } = summarize(value, knownItems);
    return <>
      <span className="workspace-app-calendar__completion">
        <span>{rollupConfig?.completion && <CalendarStatusIcon tone={complete > 0 ? "published" : "neutral"} />}{completionLabel} <strong>{completionFigure(complete, total)}</strong></span>
        {rollupConfig?.completion && <progress aria-label={rollupConfig.completion.label} value={complete} max={total} />}
      </span>
      <span className="workspace-app-calendar__stage-counts">{stages.map((entry) => stagePill(entry, jumpable))}</span>
      {attentionPill(attention, jumpable)}
    </>;
  };
  // A campaign card leads with a cover in the campaign's own colours: how far
  // publishing has come in large type, and only what went wrong on top of it.
  const coverStyle = (label: string) => {
    const [hue, accent] = titleHues(label);
    return { "--campaign-hue": hue, "--campaign-accent": accent } as CSSProperties;
  };
  const contextCard = (value?: CalendarSummary) => {
    if (!value?.total) return { cover: summaryState(value), status: null };
    const { total, complete, stages, attention } = summarize(value);
    const problems = stages.filter(([stage]) => stageTone(stage) === "error");
    const others = stages.filter(([stage]) => stageTone(stage) !== "error");
    return {
      cover: <>
        <span className="workspace-app-calendar__cover-figure"><small>{completionLabel}</small><strong>{completionFigure(complete, total)}</strong></span>
        {rollupConfig?.completion && <progress aria-label={rollupConfig.completion.label} value={complete} max={total} />}
        {(problems.length > 0 || attention > 0) && <span className="workspace-app-calendar__cover-flags">{problems.map((entry) => stagePill(entry))}{attentionPill(attention)}</span>}
      </>,
      status: others.length > 0 && <span className="workspace-app-calendar__context-status">{others.map(([stage, count]) => <span key={stage}>{count} {stageLabel(stage)}</span>)}</span>,
    };
  };

  const loadMore = async (block: WorkspaceAppDataBlockResult) => {
    if (!context || !block.next_cursor || loadingBlock) return;
    const requestScope = scope;
    setLoadingBlock(block.key); setMoreError(false);
    try {
      const page = await api.workspaces.appViewComponentData(workspaceId, component.key, { contextKind: context.kind, contextId: context.id, block: block.key, cursor: block.next_cursor, limit: 100 });
      if (activeScope.current !== requestScope) return;
      const next = page.blocks.find((item) => item.key === block.key);
      if (next) setExtraBlocks((current) => ({ ...current, [block.key]: { ...next, items: [...(current[block.key]?.items || []), ...next.items] } }));
    } catch { if (activeScope.current === requestScope) setMoreError(true); }
    finally { if (activeScope.current === requestScope) setLoadingBlock(null); }
  };
  const hiddenBlocks = new Set(component.display?.hidden_blocks || []);
  const consumedBlocks = new Set([calendar.block, calendar.rollup_block, calendar.context_block]);
  const completedIds = new Set(schedule.items.filter((item) => completedSet.has(displayStage(item))).map((item) => item.id));
  const recordsByItem = new Map<string, WorkspaceAppDataItem[]>();
  for (const block of firstBlocks.filter((block) => resultKeys.includes(block.key) && block.status !== "error" && !block.error)) {
    for (const record of block.items) {
      const projectId = calendarRecordProjectId(record);
      if (projectId && completedIds.has(projectId)) recordsByItem.set(projectId, [...(recordsByItem.get(projectId) || []), record]);
    }
    // Only remove a results block once every record is available on its own day.
    if (scheduleReady && !block.next_cursor && block.items.every((record) => completedIds.has(calendarRecordProjectId(record) || ""))) consumedBlocks.add(block.key);
  }
  if (scheduleReady) {
    const stages = new Map(schedule.items.map((item) => [item.id, displayStage(item)]));
    for (const block of firstBlocks) {
      if (block.type === "progress" && !block.error && block.status !== "error" && !block.next_cursor
        && block.items.every((item) => stages.get(item.id) === displayStage(item))) consumedBlocks.add(block.key);
    }
  }
  // Documents grouped with the rollup are the configured plan references.
  const overviewGroup = component.display?.groups?.find((group) => group.blocks.includes(calendar.rollup_block));
  const headerDocuments = (firstBlocks || []).filter((block) => block.type === "documents" && overviewGroup?.blocks.includes(block.key)
    && !hiddenBlocks.has(block.key) && (block.items.length > 0 || block.error));
  for (const block of headerDocuments) consumedBlocks.add(block.key);
  const supplementary = (firstBlocks || []).filter((block) => {
    const config = component.blocks.find((item) => item.key === block.key);
    // Progress is represented by the all-stage calendar, including its issue records.
    return !consumedBlocks.has(block.key) && !hiddenBlocks.has(block.key) && !(block.type === "progress" && config?.source && eventConfig?.source === config.source);
  }).filter((block) => block.type !== "progress" || !component.display?.groups?.some((group) => group.blocks.includes(calendar.block) && group.blocks.includes(block.key)));
  const supplementalGroups = component.display?.groups?.length ? component.display.groups : [{ key: "details", title: "", blocks: supplementary.map((block) => block.key) }];
  const scheduleNotes = contextFields && contextFields.length > 2
    ? <div className="workspace-app-calendar__schedule-notes"><ContextFields fields={contextFields.slice(2)} secondary /></div> : null;

  const itemRow = (item: WorkspaceAppDataItem) => {
    const expanded = view.expanded === item.id;
    const provider = item.fields?.find((field) => field.format === "provider");
    const stage = displayStage(item);
    const tone = stageTone(stage);
    const records = recordsByItem.get(item.id) || [];
    return <li key={item.id} data-testid={`calendar-entry-${item.id}`} className={`workspace-app-calendar__day-item is-${tone}${expanded ? " is-expanded" : ""}`}>
      <button type="button" className="workspace-app-calendar__item-heading" aria-expanded={expanded}
        onClick={() => updateView({ expanded: expanded ? null : item.id })}>
        <time dateTime={item.calendar_at || undefined}>{clock(item)}</time>
        <span className="workspace-app-calendar__brand" title={String(provider?.value || provider?.provider || "")}>
          {provider?.provider && <IntegrationLogo provider={provider.provider} size={22} />}
        </span>
        <span className="workspace-app-calendar__item-copy"><strong>{titleOf(item)}</strong>
          {stage && <span className={`workspace-app-calendar__status is-${tone}`}><CalendarStatusIcon tone={tone} /><span>{stageLabel(stage)}</span></span>}
          {provider && <small className="workspace-app-calendar__provider">{String(provider.value || provider.provider || "")}{provider.secondary ? ` · ${provider.secondary}` : ""}</small>}
        </span>
        <IconChevronDown size={16} className="workspace-app-calendar__item-chevron" aria-hidden="true" />
      </button>
      {renderItemResponse && item.execution?.status === "paused" && <div className="workspace-app-calendar__item-response">{renderItemResponse(item)}</div>}
      {completedSet.has(stage) && resultKeys.length > 0 && <div className="workspace-app-calendar__item-metrics" data-testid={`calendar-metrics-${item.id}`}>
        {renderItemMetrics(records)}
      </div>}
      {expanded && <div className="workspace-app-calendar__item-details">
        {renderItemDetails(item, records)}
      </div>}
    </li>;
  };

  return <section ref={rootRef} className={`workspace-app-calendar${selectedId ? " is-context-open" : ""}${view.dayOpen && selectedId ? " is-day-open" : ""}`} aria-label={component.title || calendar.collection_title}
    style={{ "--calendar-instagram-fill": `url("#${instagramGradientId}")` } as CSSProperties}>
    <svg className="workspace-app-calendar__brand-defs" width="0" height="0" aria-hidden="true" focusable="false"><defs>
      <linearGradient id={instagramGradientId} x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stopColor="#833AB4" /><stop offset="50%" stopColor="#E1306C" /><stop offset="100%" stopColor="#FCAF45" />
      </linearGradient>
    </defs></svg>
    {!selectedId ? <div data-testid="calendar-collection">
      <header className="workspace-app-calendar__collection-heading"><h3>{calendar.collection_title}{contextsQuery.data && !contextsQuery.hasNextPage && !contextsQuery.isError && <small> {contexts.length}</small>}</h3></header>
      {preview ? <p className="workspace-app-calendar__empty">{copy("preview")}</p> : contextsQuery.isPending ? <p role="status" className="workspace-app-calendar__loading">{t("component.workspace_app_view.data_loading")}</p> : null}
      {contextsQuery.isError && <p role="alert" className="workspace-app-calendar__error">{t("component.workspace_app_view.data_error")} <button onClick={() => void contextsQuery.refetch()}>{copy("reload")}</button></p>}
      {!preview && contextsQuery.data && !contextsQuery.isError && contexts.length === 0 && !making.length && <p className="workspace-app-calendar__empty">{copy("empty_collection", { collection: calendar.collection_title })}</p>}
      <div className="workspace-app-calendar__contexts">
      {/* One being prepared shows before it exists, as a campaign card with how far it is; it opens its run. */}
      {making.map((run) => {
        const title = copy(run.status === "paused" ? "making_waiting" : "making", { collection: calendar.collection_title });
        return <button type="button" key={run.run_id} className="workspace-app-calendar__context" data-making style={coverStyle(run.run_id)}
          disabled={!openRun} onClick={() => openRun?.({ id: run.run_id, workflow_id: run.workflow_id, binding_id: run.binding_id })}>
          <span className="workspace-app-calendar__cover"><MakingMark percent={run.progress_percent} /></span>
          <span className="workspace-app-calendar__context-copy">
            <strong title={title}>{title}</strong>
            {run.step && <span className="workspace-app-calendar__context-status"><span>{run.step}</span></span>}
          </span>
        </button>;
      })}
      {contexts.map((item: WorkspaceAppDataContext) => <button type="button" key={item.id} data-context-id={item.id} aria-label={item.label} className="workspace-app-calendar__context" style={coverStyle(item.label)} onClick={(event) => {
        let scroll = event.currentTarget.parentElement;
        while (scroll && scroll.scrollHeight <= scroll.clientHeight) scroll = scroll.parentElement;
        if (scroll === document.body || scroll === document.documentElement) scroll = document.scrollingElement as HTMLElement | null;
        if (scroll) collectionPosition.current = { element: scroll, top: scroll.scrollTop };
        setSelectedId(item.id);
      }}>
        {(() => {
          const card = contextCard(item.summary);
          return <>
            <span className="workspace-app-calendar__cover">{card.cover}<IconArrowRight size={16} className="workspace-app-calendar__cover-open" aria-hidden="true" /></span>
            <span className="workspace-app-calendar__context-copy">
              <strong>{item.label}</strong>
              <ContextFields fields={item.summary?.fields.slice(0, 2)} />
              {card.status}
              {item.summary?.next?.calendar_at && <span className="workspace-app-calendar__next"><span>{Date.parse(item.summary.next.calendar_at) < Date.now() ? copy("pending_since") : copy("next")}</span> <time dateTime={item.summary.next.calendar_at}>{formatDateCompact(item.summary.next.calendar_at)}</time><small>{item.summary.next.label}</small></span>}
            </span>
          </>;
        })()}
      </button>)}</div>
      {contextsQuery.hasNextPage && <button type="button" className="workspace-app-calendar__load-more" onClick={() => void contextsQuery.fetchNextPage()} disabled={contextsQuery.isFetchingNextPage}>{t("component.workspace_app_view.data_more")}</button>}
    </div> : <div data-testid="campaign-calendar">
      <button ref={backRef} type="button" data-testid="calendar-back" className="workspace-app-calendar__back" onClick={() => {
        setSelectedId(null);
        requestAnimationFrame(() => {
          const position = collectionPosition.current;
          if (position) position.element.scrollTop = position.top;
          rootRef.current?.querySelector<HTMLButtonElement>(`[data-context-id="${CSS.escape(selectedId)}"]`)?.focus({ preventScroll: true });
        });
      }}><IconChevronLeft size={16} aria-hidden="true" />{t("component.workspace_app_view.all_actions")}</button>
      <header className="workspace-app-calendar__heading" style={coverStyle(context?.label || "")}><div className="workspace-app-calendar__heading-copy"><h3>{context?.label || t("component.workspace_app_view.data_untitled")}</h3><div className="workspace-app-calendar__metadata"><ContextFields fields={contextFields?.slice(0, 2)} /></div></div>
        {headerDocuments.length > 0 && <div className="workspace-app-calendar__plan-links">{headerDocuments.map((block) => <div key={block.key}>{renderBlock(block, component.blocks.find((item) => item.key === block.key), loadMore, loadingBlock === block.key)}</div>)}</div>}
        <div className="workspace-app-calendar__summary">{showSummary(summary || context?.summary, scheduleReady ? schedule.items : undefined)}</div>
      </header>
      {context && <div className="workspace-app-calendar__context-details">{renderContextDetails(context)}</div>}
      {!context && !contextsQuery.isPending && <p className="workspace-app-calendar__error">{copy("context_unavailable")}</p>}
      {dataQuery.isError || eventError ? <p role="alert" className="workspace-app-calendar__error">{eventError?.error?.message || t("component.workspace_app_view.data_error")} <button type="button" onClick={() => void dataQuery.refetch()}>{copy("reload")}</button></p> : null}
      {context && !scheduleReady && !dataQuery.isError && !eventError && <p className="workspace-app-calendar__loading" role="status">{copy("loading_schedule")}</p>}
      {scheduleReady && <div className="workspace-app-calendar__layout">
        <div className="workspace-app-calendar__month-area">
          <header className="workspace-app-calendar__month">
            <button type="button" aria-label={copy("previous_month")} onClick={() => updateView({ month: shiftCalendarMonth(view.month, -1), dayOpen: false })}><IconChevronLeft size={19} /></button>
            <h4>{dateTitle(`${view.month}-01`, { year: "numeric", month: "long" })}</h4>
            <button type="button" aria-label={copy("next_month")} onClick={() => updateView({ month: shiftCalendarMonth(view.month, 1), dayOpen: false })}><IconChevronRight size={19} /></button>
          </header>
          <div className="workspace-app-calendar__weekdays" aria-hidden="true">{Array.from({ length: 7 }, (_, day) => <span key={day}>{dateTitle(`2024-01-${String(day + 1).padStart(2, "0")}`, { weekday: "short" })}</span>)}</div>
          <div className="workspace-app-calendar__grid" role="group" aria-label={copy("month_calendar")}>{days.map((day) => {
            const items = schedule.days.get(day) || [];
            const issues = issueCount(items);
            const showAll = view.expandedDays.includes(day);
            return <div key={day} className={`workspace-app-calendar__cell${day.startsWith(view.month) ? "" : " is-outside"}${day === view.date ? " is-selected" : ""}${items.length ? " has-items" : ""}`}>
              <button type="button" data-testid={`calendar-day-${day}`} className="workspace-app-calendar__date" aria-pressed={day === view.date}
                aria-label={`${dateTitle(day, { weekday: "long", month: "long", day: "numeric", year: "numeric" })}, ${countCopy("item_count", items.length)}${issues ? `, ${countCopy("attention", issues)}` : ""}`}
                onClick={() => compact ? updateView({ date: day, month: day.slice(0, 7), dayOpen: false, expanded: null }) : openDay(day)}>
                <span className="workspace-app-calendar__date-number">{Number(day.slice(-2))}</span>
                {items.length > 0 && <span className="workspace-app-calendar__date-dots" aria-hidden="true">{items.map((item) => {
                  const provider = item.fields?.find((field) => field.format === "provider");
                  const brand = resolveIntegrationBrand(provider?.provider);
                  return <svg key={item.id} viewBox="0 0 10 10" data-integration-brand={brand?.key} focusable="false" style={{ color: brand?.color || "#78716c" }}><circle cx="5" cy="5" r="5" /></svg>;
                })}</span>}
              </button>
              <div className="workspace-app-calendar__previews">{(showAll ? items : items.slice(0, 3)).map((item) => <button type="button" className={`workspace-app-calendar__event is-${stageTone(displayStage(item))}`} key={item.id} title={`${clock(item)} · ${titleOf(item)} · ${stageLabel(displayStage(item))}`} onClick={() => openDay(day, item.id)}>
                <CalendarStatusIcon tone={stageTone(displayStage(item))} /><time dateTime={item.calendar_at || undefined}>{clock(item)}</time><span>{String(item.fields?.find((field) => field.format === "provider")?.value || titleOf(item))}</span>
              </button>)}
                {items.length > 3 && <button type="button" data-testid={`calendar-more-${day}`} className="workspace-app-calendar__more" aria-expanded={showAll} onClick={() => toggleDayItems(day)}>{showAll ? t("component.workspace_app_view.metrics_show_less") : copy("more", { count: items.length - 3 })}</button>}
                {issues > 0 && <span className="workspace-app-calendar__day-issue"><CalendarStatusIcon tone="attention" />{copy(issues === 1 ? "issue" : "issues", { count: issues })}</span>}
              </div>
            </div>;
          })}</div>
          {schedule.items.length === 0 && <p className="workspace-app-calendar__empty">{eventConfig?.empty_message || copy("empty_schedule")}</p>}
          {compact && view.date && <section data-testid="calendar-day-preview" className="workspace-app-calendar__mobile-day-preview" aria-label={copy("selected_day")}>
            <header><h4>{dateTitle(view.date, { weekday: "short", month: "short", day: "numeric" })}</h4>{dayItems.length > 3 && <button type="button" data-testid="calendar-view-day" aria-expanded={showAllDayItems} onClick={() => toggleDayItems(view.date!)}>{showAllDayItems ? t("component.workspace_app_view.metrics_show_less") : copy("view_all", { count: dayItems.length })}<IconChevronDown size={16} /></button>}</header>
            <p>{countCopy("item_count", dayItems.length)}{issueCount(dayItems) > 0 && <span className="workspace-app-calendar__attention"> · {countCopy("attention", issueCount(dayItems))}</span>}</p>
            <ol className="workspace-app-calendar__day-list">{(showAllDayItems ? dayItems : dayItems.slice(0, 3)).map(itemRow)}</ol>
            {!dayItems.length && <p className="workspace-app-calendar__empty">{copy("empty_day")}</p>}
            {dayItems.length > 3 && <button type="button" className="workspace-app-calendar__more" aria-expanded={showAllDayItems} onClick={() => toggleDayItems(view.date!)}>{showAllDayItems ? t("component.workspace_app_view.metrics_show_less") : copy("more", { count: dayItems.length - 3 })}</button>}
          </section>}
          {scheduleNotes}
        </div>
        {!compact && view.dayOpen && view.date && <aside ref={panelRef} tabIndex={-1} data-testid="calendar-day-panel" className="workspace-app-calendar__day-panel" aria-label={copy("selected_day")}
          onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeDay(); } }}>
          <header><div><h4>{dateTitle(view.date, { weekday: "short", month: "short", day: "numeric" })}</h4><p>{countCopy("item_count", dayItems.length)} · {timeZone}</p></div><button type="button" data-testid="calendar-day-close" className="workspace-app-calendar__day-close" aria-label={copy("close_day")} onClick={closeDay}><IconClose size={19} /></button></header>
          <ol className="workspace-app-calendar__day-list">{dayItems.map(itemRow)}</ol>
          {!dayItems.length && <p className="workspace-app-calendar__empty">{copy("empty_day")}</p>}
        </aside>}
      </div>}
      {!scheduleReady && scheduleNotes}
      {scheduleReady && schedule.undated.length > 0 && <section className="workspace-app-calendar__undated"><h4>{copy("undated")}</h4><ol className="workspace-app-calendar__day-list">{schedule.undated.map(itemRow)}</ol></section>}
      <div className="workspace-app-calendar__supplementary">{supplementalGroups.map((group) => {
        const members = group.blocks.flatMap((key) => supplementary.filter((block) => block.key === key));
        if (!members.length) return null;
        const groupState = `${selectedId}:${group.key}`;
        return <details key={group.key} className="workspace-app-calendar__group" open={Boolean(openGroups[groupState])}><summary onClick={(event) => {
          event.preventDefault();
          setOpenGroups((current) => ({ ...current, [groupState]: !current[groupState] }));
        }}>{group.title || component.title || calendar.collection_title}<IconChevronDown size={16} /></summary><div className="workspace-app-calendar__group-body">
          {members.map((block) => {
          const extra = extraBlocks[block.key];
          const combined = extra ? { ...block, ...extra, items: [...new Map([...block.items, ...extra.items].map((item) => [item.id, item])).values()] } : block;
          return <div key={block.key}>{renderBlock(combined, component.blocks.find((item) => item.key === block.key), loadMore, loadingBlock === block.key)}</div>;
        })}</div></details>;
      })}</div>
      {moreError && <p role="alert" className="workspace-app-calendar__error">{t("component.workspace_app_view.data_error")}</p>}
    </div>}
  </section>;
}
