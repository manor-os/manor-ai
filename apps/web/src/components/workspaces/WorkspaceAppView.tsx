import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, type WorkspaceAppAction, type WorkspaceAppOutputRun, type WorkspaceAppSection, type WorkspaceAppSectionBlock as BlockName, type WorkspaceAppView as WorkspaceAppViewData, type WorkspaceAppZone } from "../../lib/api";
import type { WorkflowDocumentRef } from "../../lib/workflowRunInputs";
import { t } from "../../lib/i18n";
import { IconArrowRight, IconChatBubble, IconClock, IconSearch, IconSparkles } from "../icons";
import Button from "../ui/Button";
import WorkspaceAppActionFlow from "./WorkspaceAppActionFlow";
import { actionIcon } from "./workspaceAppRunDisplay";
import WorkspaceAppActionChat, { type WorkspaceAppChatResult, type WorkspaceAppLiveReply } from "./WorkspaceAppActionChat";
import WorkspaceAppActionAutomation from "./WorkspaceAppActionAutomation";
import WorkspaceAppDataView, { type WorkspaceAppActionOpener, type WorkspaceAppRunOpener } from "./WorkspaceAppDataView";
import { workflowRunPath } from "../../lib/workflowRoutes";
import WorkspaceAppSectionBlock, { NoticesZone, blockRenderKind } from "./WorkspaceAppSections";
import OneLineText from "../ui/OneLineText";
import { useResolvedDocumentTheme } from "../ui/ThemeAwareImage";
import { appThemeStyle } from "../../lib/appTheme";
import { useProtectedMediaUrl } from "../../lib/useProtectedMediaUrl";
import { WorkspaceAppDraftContext } from "./workspaceAppDraft";
import "./WorkspaceAppView.css";

interface Props {
  view?: WorkspaceAppViewData;
  loading: boolean;
  error: boolean;
  busy: boolean;
  canStart: boolean;
  /** Why entries cannot start here, when it is not the viewer's role (a simulation). */
  readOnlyNote?: string;
  onRetry: () => void;
  onAction: (action: WorkspaceAppAction, request: string, documents: WorkflowDocumentRef[]) => Promise<WorkspaceAppChatResult>;
  /** The reply an Agent entry is writing now, shown on the entry's result step. */
  liveReply?: WorkspaceAppLiveReply | null;
  canUseWorkflowChat?: (action: WorkspaceAppAction) => boolean;
  onOpenWorkflow?: (action: WorkspaceAppAction) => void;
  onEdit: () => void;
  onSplit?: () => void;
  onClose: () => void;
  onUse?: () => void;
  preview?: boolean;
  /** The name the surrounding chrome already shows; a title equal to it is not repeated. */
  contextTitle?: string;
  /** The page an open design draft stages, shown with real data before it is approved; nothing starts from it. */
  draftPreview?: { draftId: string; onExit: () => void };
}

function visibleAppDataViews(view: WorkspaceAppViewData | null | undefined) {
  return (view?.components ?? view?.config?.components ?? []).filter((component) =>
    !component.display?.hidden && component.blocks.some((block) => !component.display?.hidden_blocks?.includes(block.key)),
  );
}

/** An App with no functions and no data views has nothing to open, so Chat stays the only view. */
export function workspaceAppHasContent(view: WorkspaceAppViewData) {
  return (view.actions?.length ?? 0) > 0 || visibleAppDataViews(view).length > 0;
}

export default function WorkspaceAppView({ draftPreview, ...props }: Props) {
  return <WorkspaceAppDraftContext.Provider value={draftPreview?.draftId ?? null}>
    <WorkspaceAppPage {...props} draftPreview={draftPreview} />
  </WorkspaceAppDraftContext.Provider>;
}

function WorkspaceAppPage({
  view, loading, error, busy, canStart, readOnlyNote, onRetry, onAction, liveReply, canUseWorkflowChat, onOpenWorkflow, onEdit, onSplit, onClose, onUse, preview = false, contextTitle,
  draftPreview,
}: Props) {
  const [search, setSearch] = useState("");
  const location = useLocation();
  const navigate = useNavigate();
  const appearance = useResolvedDocumentTheme();
  const [selectedActionKey, setSelectedActionKey] = useState<string | null>(null);
  const [focusedCalendarKey, setFocusedCalendarKey] = useState<string | null>(null);
  const handleCalendarFocusChange = useCallback((componentKey: string, focused: boolean) => {
    setFocusedCalendarKey((current) => focused ? componentKey : current === componentKey ? null : current);
  }, []);
  // Inputs a data view chose for the opened action (a Remake for one episode).
  const [actionValues, setActionValues] = useState<Record<string, unknown> | undefined>(undefined);
  useEffect(() => {
    const restored = location.state?.workspaceApp;
    if (restored?.workspaceId === view?.workspace_id && typeof restored?.actionKey === "string") {
      setSelectedActionKey(restored.actionKey);
    }
  }, [location.key, location.state, view?.workspace_id]);
  const [failedCoverUrl, setFailedCoverUrl] = useState<string | null>(null);
  const [failedMotionUrl, setFailedMotionUrl] = useState<string | null>(null);
  const [reduceMotion, setReduceMotion] = useState(() => typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches));
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!media) return;
    const onChange = () => setReduceMotion(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  const actions = view?.actions || [];
  const dataComponents = visibleAppDataViews(view);
  const calendarComponentKeys = dataComponents.filter((component) => Boolean(component.display?.calendar)).map((component) => component.key).join("\u0000");
  useEffect(() => {
    setFocusedCalendarKey(null);
  }, [view?.workspace_id, calendarComponentKeys]);
  // A data view can open an entry that is not a tile of its own (Make this episode).
  const openable = view?.available_actions?.length ? view.available_actions : actions;
  const selectedAction = openable.find((action) => action.key === selectedActionKey);
  // A link to one entry (?app_action=<key>) opens it once the page has loaded.
  const linkedActionKey = new URLSearchParams(location.search).get("app_action");
  const linkedActionAvailable = Boolean(linkedActionKey && openable.some((action) => action.key === linkedActionKey));
  useEffect(() => {
    if (linkedActionKey && linkedActionAvailable) setSelectedActionKey(linkedActionKey);
  }, [linkedActionKey, linkedActionAvailable, view?.workspace_id]);
  const hero = actions.find((action) => action.key === view?.hero);
  const layout = view?.config.layout || "feature";
  // Functions: one main function, the rest as an icon grid, automations as one line.
  const matches = (action: WorkspaceAppAction) =>
    `${action.title} ${action.description}`.toLocaleLowerCase().includes(search.toLocaleLowerCase());
  const tiles = actions.filter((action) => action.kind !== "automation" && (layout === "list" || action.key !== hero?.key) && matches(action));
  const automations = actions.filter((action) => action.kind === "automation" && action.key !== hero?.key);
  // A reply in progress holds the one chat stream, which only Agent entries use;
  // Flows and automations start through their own runs and stay available.
  const agentBusy = busy && !preview && canStart;
  const blocked = (action: WorkspaceAppAction) => preview || Boolean(draftPreview) || !canStart || action.available === false
    || (busy && action.kind === "chat");
  // The page is four zones in a fixed order: notices, functions, data,
  // outputs. Each block's zone comes from the server's block catalog; older
  // servers without one send no sections, which leaves the functions only.
  const sections = view?.sections?.length ? view.sections : [];
  const zones = new Map<BlockName, WorkspaceAppZone>((view?.blocks || []).map((spec) => [spec.block, spec.zone]));
  const zoneOf = (section: WorkspaceAppSection): WorkspaceAppZone => zones.get(section.block)
    ?? ({ notice: "notices", page: "functions", section: "data" } as const)[blockRenderKind(section.block)];
  const inZone = (zone: WorkspaceAppZone) => sections.filter((section) => zoneOf(section) === zone);
  const showActions = !sections.length || sections.some((section) => section.block === "actions");
  // A run reopens in the entry that started it, or in its Flow's own entry.
  const workflowActionKeys = new Set(actions.filter((action) => action.kind === "workflow").map((action) => action.key));
  // A run started elsewhere (from chat, with no binding) still opens in the one entry of its Flow.
  const runAction = (run: WorkspaceAppOutputRun) => [run.app_action, run.binding_id ? `workflow:${run.binding_id}` : null]
    .find((key): key is string => Boolean(key && workflowActionKeys.has(key)))
    || flowAction(run.workflow_id);
  const flowAction = (workflowId?: string | null) => {
    const entries = actions.filter((action) => action.kind === "workflow" && workflowId && action.workflow_id === workflowId);
    return entries.length === 1 ? entries[0].key : null;
  };
  // A data view's run (an episode being made) opens in its entry's progress, else on its run page.
  const openRun: WorkspaceAppRunOpener = (run) => {
    const actionKey = runAction({ run_id: run.id, workflow_id: run.workflow_id, binding_id: run.binding_id } as WorkspaceAppOutputRun);
    if (actionKey && view) {
      navigate(`${location.pathname}${location.search}`, {
        state: { ...location.state, workspaceApp: { workspaceId: view.workspace_id, actionKey, runId: run.id } },
      });
    } else {
      // The run page's back returns here, to the App this run was opened from.
      navigate(workflowRunPath(run.workflow_id, run.id, view?.workspace_id), {
        state: { returnTo: `${location.pathname}${location.search}${location.hash}`, returnState: location.state },
      });
    }
  };
  const zoneSections = (zone: WorkspaceAppZone) => view && !preview ? inZone(zone).map((section) =>
    <WorkspaceAppSectionBlock key={section.id} workspaceId={view.workspace_id} section={section} canStart={canStart}
      runAction={runAction} />) : null;
  const visual = view?.config.visual || "auto";
  const coverUrl = view?.cover_image_url;
  const showCover = Boolean(coverUrl && failedCoverUrl !== coverUrl && visual !== "plain");
  const configuredMotionUrl = view?.config.motion_cover_url;
  const motionUrl = showCover && visual === "motion" && !reduceMotion && view?.config.motion_cover_source_url === coverUrl
    && configuredMotionUrl && configuredMotionUrl !== failedMotionUrl ? configuredMotionUrl : null;
  const showMotion = !showCover && visual !== "plain";
  // A cover or loop in the Workspace's own files (an upload, or one a Blueprint package
  // carried in) loads with the user's token; platform pictures load as they are.
  const cover = useProtectedMediaUrl(showCover ? coverUrl : null);
  const motion = useProtectedMediaUrl(motionUrl);
  useEffect(() => {
    if (cover.failed && coverUrl) setFailedCoverUrl(coverUrl);
  }, [cover.failed, coverUrl]);
  useEffect(() => {
    if (motion.failed && motionUrl) setFailedMotionUrl(motionUrl);
  }, [motion.failed, motionUrl]);
  const coverMedia = showCover && <>
    {cover.src && <img className="workspace-app-view__cover-image" src={cover.src} alt="" onError={() => setFailedCoverUrl(coverUrl!)} />}
    {motionUrl && motion.src && <video className="workspace-app-view__cover-video" src={motion.src} autoPlay muted loop playsInline preload="metadata" poster={cover.src || undefined} aria-hidden="true" onError={() => setFailedMotionUrl(motionUrl)} />}
  </>;
  // A result opened from a shelf is remembered in history so its run page can come back to it; leaving
  // the entry forgets it, so the next visit to that entry starts a new run instead of showing the old one.
  const forgetOpenedRun = () => {
    const { workspaceApp: _opened, ...rest } = (location.state || {}) as Record<string, unknown>;
    if (_opened) navigate(`${location.pathname}${location.search}${location.hash}`, { replace: true, state: rest });
  };
  const closeAction = () => {
    forgetOpenedRun();
    setSelectedActionKey(null);
  };
  const selectAction = (action: WorkspaceAppAction, values?: Record<string, unknown>) => {
    onUse?.();
    forgetOpenedRun();
    setActionValues(values);
    setSelectedActionKey(action.key);
  };
  const openAction: WorkspaceAppActionOpener = {
    has: (key) => openable.some((action) => action.key === key && action.kind === "workflow" && !blocked(action)),
    open: (key, values) => {
      const action = openable.find((item) => item.key === key);
      if (action) selectAction(action, values);
    },
    // The same run the entry's own Start makes, without leaving the page (refresh this post).
    start: async (key, values) => {
      const action = openable.find((item) => item.key === key && item.kind === "workflow" && !blocked(item));
      if (!action?.binding_id) throw new Error("This entry cannot start here.");
      onUse?.();
      return api.workflows.runBinding(action.binding_id, {
        variables: values,
        trigger_data: { workspace_manual: true, _workspace_app_action: action.key },
        execute: false,
      }) as Promise<{ id: string }>;
    },
  };
  // The icon already says what kind of entry it is; words only for exceptions.
  const actionKindLabel = (action: WorkspaceAppAction) => {
    if (action.available === false) return t("component.workspace_app_view.target_unavailable");
    if (action.kind === "automation" && action.enabled === false) return t("component.workspace_app_view.kind_paused");
    return null;
  };
  // A function is an app icon: what it does is on the screen it opens.
  const actionTile = (action: WorkspaceAppAction) => {
    const kindLabel = actionKindLabel(action);
    const Icon = actionIcon(action);
    return <button
      key={action.key}
      type="button"
      className="workspace-app-view__tile"
      data-kind={action.kind}
      title={action.description || undefined}
      onClick={() => selectAction(action)}
      disabled={blocked(action)}
    >
      <span className="workspace-app-view__tile-icon" aria-hidden="true"><Icon size={20} /></span>
      <span className="workspace-app-view__tile-name">{action.title}</span>
      {kindLabel && <small>{kindLabel}</small>}
    </button>;
  };
  // One title and one line under it: the eyebrow repeats what the title says,
  // and the description is one line, written to fit.
  const introTitle = view?.title || view?.eyebrow;
  const intro = view && <div className="workspace-app-view__intro">
    {introTitle && introTitle.trim() !== contextTitle?.trim() && <h2>{introTitle}</h2>}
    {view.description && <OneLineText as="p" text={view.description} />}
    {!preview && !draftPreview && !canStart && <p role="status">{readOnlyNote || t("component.workspace_app_view.read_only")}</p>}
  </div>;

  return (
    <aside className={`workspace-app-view workspace-app-view--${layout}${preview ? " workspace-app-view--preview" : ""}`}
      style={appThemeStyle(view?.config.theme, appearance === "dark") as CSSProperties}
      data-app-surface={view?.config.theme?.surface && view.config.theme.surface !== "neutral" ? view.config.theme.surface : undefined}
      data-calendar-focused={focusedCalendarKey && !selectedAction && !loading && !error ? "true" : undefined} aria-label={preview ? t("component.workspace_app_view.preview") : t("component.workspace_app_view.title")}>
      {!preview && onSplit && <button type="button" className="workspace-app-view__split" onClick={onSplit}>
        {t("component.workspace_app_view.display_split")}
      </button>}
      {!preview && <button type="button" className="workspace-app-view__close" onClick={onClose} aria-label={t("component.workspace_app_view.open_chat")}>
        <IconChatBubble size={15} /><span>{t("component.workspace_app_view.chat_intake")}</span>
      </button>}

      <div className="workspace-app-view__scroll" tabIndex={0}>
        {draftPreview && <div className="workspace-app-view__draft" role="status">
          <IconSparkles size={15} aria-hidden="true" />
          <p>{t("component.workspace_app_view.draft_preview")}</p>
          <Button variant="ghost" size="sm" onClick={draftPreview.onExit}>{t("component.workspace_app_view.draft_preview_exit")}</Button>
        </div>}
        {loading ? (
          <div className="workspace-app-view__state" role="status">{t("component.workspace_app_view.loading")}</div>
        ) : error ? (
          <div className="workspace-app-view__state" role="alert">
            <p>{t("component.workspace_app_view.error")}</p>
            <Button variant="ghost" size="sm" onClick={onRetry}>{t("component.workspace_app_view.retry")}</Button>
          </div>
        ) : view && selectedAction?.kind === "workflow" ? (
          <WorkspaceAppActionFlow
            key={`${view.workspace_id}:${selectedAction.key}:${selectedAction.binding_id}:${JSON.stringify(actionValues || {})}`}
            action={selectedAction}
            initialValues={actionValues}
            workspaceId={view.workspace_id}
            canStart={!blocked(selectedAction)}
            chatAvailable={Boolean(canUseWorkflowChat?.(selectedAction))}
            onUseChat={() => onOpenWorkflow?.(selectedAction)}
            onBack={closeAction}
            onFollowProgress={onClose}
          />
        ) : view && selectedAction?.kind === "chat" ? (
          <WorkspaceAppActionChat key={`${view.workspace_id}:${selectedAction.key}:${selectedAction.agent_id || "default"}`} action={selectedAction} workspaceId={view.workspace_id} canStart={!blocked(selectedAction)} onBack={closeAction} onRun={(request, documents) => onAction(selectedAction, request, documents)} live={liveReply} />
        ) : view && selectedAction?.kind === "automation" ? (
          <WorkspaceAppActionAutomation key={`${view.workspace_id}:${selectedAction.key}:${selectedAction.job_id}`} action={selectedAction} canStart={!blocked(selectedAction)} onBack={closeAction} />
        ) : view && (
          <>
            {/* A cover holds the main function, so notices go above it to stay before the functions. */}
            {layout === "cover" && !preview && <NoticesZone workspaceId={view.workspace_id} sections={inZone("notices")} canConnect={view.can_start} />}
            {layout === "cover" ? <div className={`workspace-app-view__cover${showCover ? " workspace-app-view__cover--image" : visual !== "plain" ? " workspace-app-view__cover--color" : ""}`}>
              {coverMedia}
              {showMotion && <span className="workspace-app-view__hero-art" aria-hidden="true"><span /><span /><span /></span>}
              {intro}
              {showActions && hero && <button type="button" className="workspace-app-view__cover-action" onClick={() => selectAction(hero)} disabled={blocked(hero)}>
                <span>{hero.title}{hero.available === false && <small>{t("component.workspace_app_view.target_unavailable")}</small>}</span><IconArrowRight size={17} aria-hidden="true" />
              </button>}
            </div> : intro}

            {/* ① Notices: what needs the person. */}
            {!preview && layout !== "cover" && <NoticesZone workspaceId={view.workspace_id} sections={inZone("notices")} canConnect={view.can_start} />}

            {/* ② Functions: what the person can start. */}
            {showActions && <section className="workspace-app-view__section workspace-app-zone workspace-app-zone--functions" aria-label={t("component.workspace_app_view.functions_title")}>
              {layout === "feature" && (hero ? <button
                type="button"
                className={`workspace-app-view__hero${showCover ? " workspace-app-view__hero--image" : ""}`}
                data-kind={hero.kind}
                onClick={() => selectAction(hero)}
                disabled={blocked(hero)}
              >
                {coverMedia}
                {showMotion && <span className="workspace-app-view__hero-art" aria-hidden="true"><span /><span /><span /></span>}
                <span className="workspace-app-view__hero-content">
                  <small className="workspace-app-view__hero-kicker" aria-hidden="true">{t("component.workspace_app_view.hero_kicker")}</small>
                  <strong>{hero.title}</strong>
                  {hero.available === false && <small className="workspace-app-view__hero-unavailable">{t("component.workspace_app_view.target_unavailable")}</small>}
                  {hero.description && <small className="workspace-app-view__hero-description">{hero.description}</small>}
                </span>
                <span className="workspace-app-view__hero-start" aria-hidden="true">{t("component.workspace_app_view.hero_start")}<IconArrowRight size={14} /></span>
              </button> : dataComponents.length === 0 && <div className="workspace-app-view__empty">
                <p>{t("component.workspace_app_view.empty")}</p>
              </div>)}
              {layout !== "feature" && actions.length === 0 && dataComponents.length === 0 && <div className="workspace-app-view__empty">
                <p>{t("component.workspace_app_view.empty")}</p>
              </div>}
              {actions.length > 12 && <label className="workspace-app-view__search">
                <IconSearch size={14} aria-hidden="true" />
                <input
                  className="workspace-app-view__search-input"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder={t("component.workspace_app_view.search")}
                  aria-label={t("component.workspace_app_view.search")}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>}
              {tiles.length > 0 && <div className="workspace-app-view__grid">{tiles.map(actionTile)}</div>}
              {agentBusy && actions.some((action) => action.kind === "chat") && <p className="workspace-app-view__busy" role="status">{t("component.workspace_app_view.agent_busy")}</p>}
              {search && tiles.length === 0 && <p className="workspace-app-view__no-results">{t("component.workspace_app_view.no_results")}</p>}
              {automations.length > 0 && <div className="workspace-app-view__automations" role="group" aria-label={t("component.workspace_app_view.scheduled_actions")}>
                <span className="workspace-app-view__automations-label"><IconClock size={14} aria-hidden="true" />{t("component.workspace_app_view.scheduled_actions")}</span>
                {automations.map((action) => <button key={action.key} type="button" className="workspace-app-view__automation"
                  onClick={() => selectAction(action)} disabled={blocked(action)}>
                  {action.title}{actionKindLabel(action) && <small> · {actionKindLabel(action)}</small>}
                </button>)}
              </div>}
            </section>}

            {/* ③ Data: how things stand, key numbers before configured data views.
                ④ Outputs: what was made. Both are read, not pressed, so they share one flat zone. */}
            {(dataComponents.length > 0 || (!preview && (inZone("data").length > 0 || inZone("outputs").length > 0))) && <div className="workspace-app-zone workspace-app-zone--records">
              {zoneSections("data")}
              {dataComponents.map((component) => <WorkspaceAppDataView
                key={component.key}
                workspaceId={view.workspace_id}
                component={component}
                preview={preview}
                onFocusChange={handleCalendarFocusChange}
                openRun={openRun}
                openAction={openAction}
              />)}
              {zoneSections("outputs")}
            </div>}

            {!preview && !draftPreview && <button type="button" className="workspace-app-view__edit" onClick={onEdit}>
              <span>{t("component.workspace_app_view.edit")}</span>
            </button>}
          </>
        )}
      </div>
    </aside>
  );
}
