import { useEffect, useId, useState } from "react";
import { useLocation } from "react-router-dom";
import type { WorkspaceAppAction, WorkspaceAppView as WorkspaceAppViewData } from "../../lib/api";
import { t } from "../../lib/i18n";
import { IconArrowRight, IconChatBubble, IconClock, IconFlow, IconSearch, IconSparkles } from "../icons";
import Button from "../ui/Button";
import WorkspaceAppActionFlow from "./WorkspaceAppActionFlow";
import WorkspaceAppActionChat, { type WorkspaceAppChatResult } from "./WorkspaceAppActionChat";
import WorkspaceAppActionAutomation from "./WorkspaceAppActionAutomation";
import "./WorkspaceAppView.css";

interface Props {
  view?: WorkspaceAppViewData;
  loading: boolean;
  error: boolean;
  busy: boolean;
  canStart: boolean;
  onRetry: () => void;
  onAction: (action: WorkspaceAppAction, request: string, files: File[]) => Promise<WorkspaceAppChatResult>;
  canUseWorkflowChat?: (action: WorkspaceAppAction) => boolean;
  onOpenWorkflow?: (action: WorkspaceAppAction) => void;
  onEdit: () => void;
  onClose: () => void;
  onUse?: () => void;
  preview?: boolean;
}

const MUSIC_COVER = "/blueprints/covers/jr-ai-music-first-release-automatic-goals-v1.webp";

function MusicCoverMotion({ src }: { src: string }) {
  const id = useId().replace(/:/g, "");
  return <svg className="workspace-app-view__cover-layers" viewBox="0 0 1672 941" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">
    <defs>
      <clipPath id={`${id}-record`}><path d="M111 480 209 475 343 477 343 548 113 548Z" /></clipPath>
      <clipPath id={`${id}-parcel`}><path d="M905 491 1060 487 1065 575 909 581Z" /></clipPath>
    </defs>
    <g className="workspace-app-view__record-layer" clipPath={`url(#${id}-record)`}><image href={src} width="1672" height="941" /></g>
    <g className="workspace-app-view__parcel-layer" clipPath={`url(#${id}-parcel)`}><image href={src} width="1672" height="941" /></g>
  </svg>;
}

export default function WorkspaceAppView({
  view, loading, error, busy, canStart, onRetry, onAction, canUseWorkflowChat, onOpenWorkflow, onEdit, onClose, onUse, preview = false,
}: Props) {
  const [search, setSearch] = useState("");
  const location = useLocation();
  const [selectedActionKey, setSelectedActionKey] = useState<string | null>(null);
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
  const selectedAction = actions.find((action) => action.key === selectedActionKey);
  const hero = actions.find((action) => action.key === view?.hero);
  const layout = view?.config.layout || "feature";
  const filtered = actions.filter((action) =>
    (layout === "list" || action.key !== hero?.key) &&
    `${action.title} ${action.description}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
  );
  const scheduledActions = layout === "cover" ? filtered.filter((action) => action.kind === "automation") : [];
  const otherActions = layout === "cover" ? filtered.filter((action) => action.kind !== "automation") : filtered;
  const actionDisabled = preview || !canStart || busy;
  const visual = view?.config.visual || "auto";
  const coverUrl = view?.cover_image_url;
  const showCover = Boolean(coverUrl && failedCoverUrl !== coverUrl && visual !== "plain");
  const configuredMotionUrl = view?.config.motion_cover_url;
  const motionUrl = showCover && visual === "motion" && !reduceMotion && view?.config.motion_cover_source_url === coverUrl
    && configuredMotionUrl && configuredMotionUrl !== failedMotionUrl ? configuredMotionUrl : null;
  const showCoverLayers = showCover && visual === "motion" && !reduceMotion && !motionUrl && coverUrl === MUSIC_COVER;
  const showMotion = !showCover && visual !== "plain";
  const coverMedia = showCover && <>
    <img className="workspace-app-view__cover-image" src={coverUrl!} alt="" onError={() => setFailedCoverUrl(coverUrl!)} />
    {motionUrl && <video className="workspace-app-view__cover-video" src={motionUrl} autoPlay muted loop playsInline preload="metadata" poster={coverUrl!} aria-hidden="true" onError={() => setFailedMotionUrl(motionUrl)} />}
    {showCoverLayers && <MusicCoverMotion src={coverUrl!} />}
  </>;
  const selectAction = (action: WorkspaceAppAction) => {
    onUse?.();
    setSelectedActionKey(action.key);
  };
  const actionKindLabel = (action: WorkspaceAppAction) => {
    if (action.available === false) return t("component.workspace_app_view.target_unavailable");
    if (action.kind === "automation" && action.enabled === false) return t("component.workspace_app_view.kind_paused");
    if (action.kind === "chat") return null;
    return t(`component.workspace_app_view.kind_${action.kind}`);
  };
  const actionButton = (action: WorkspaceAppAction, index: number) => {
    const kindLabel = actionKindLabel(action);
    return <button
      key={action.key}
      type="button"
      className={`workspace-app-view__action${index === 0 && action.kind !== "automation" ? " workspace-app-view__action--spotlight" : ""}`}
      data-kind={action.kind}
      onClick={() => selectAction(action)}
      disabled={actionDisabled || action.available === false}
    >
      <span className="workspace-app-view__action-icon" aria-hidden="true">
        {action.kind === "workflow" ? <IconFlow size={17} /> : action.kind === "automation" ? <IconClock size={17} /> : <IconChatBubble size={17} />}
      </span>
      <span className="workspace-app-view__action-copy">
        {kindLabel && <span className="workspace-app-view__action-kind">{kindLabel}</span>}
        <strong>{action.title}</strong>
        {action.description && <small>{action.description}</small>}
      </span>
      <IconArrowRight size={16} aria-hidden="true" />
    </button>;
  };
  const intro = view && <div className="workspace-app-view__intro">
    {view.eyebrow && <span className="workspace-app-view__eyebrow">{view.eyebrow}</span>}
    <h2>{view.title}</h2>
    {view.description && <p>{view.description}</p>}
    {!preview && !canStart && <p role="status">{t("component.workspace_app_view.read_only")}</p>}
  </div>;

  return (
    <aside className={`workspace-app-view workspace-app-view--${layout}${preview ? " workspace-app-view--preview" : ""}`} aria-label={preview ? t("component.workspace_app_view.preview") : t("component.workspace_app_view.title")}>
      {!preview && <button type="button" className="workspace-app-view__close" onClick={onClose} aria-label={t("component.workspace_app_view.open_chat")}>
        <IconChatBubble size={15} /><span>{t("component.workspace_app_view.chat_intake")}</span>
      </button>}

      <div className="workspace-app-view__scroll" tabIndex={0}>
        {loading ? (
          <div className="workspace-app-view__state" role="status">{t("component.workspace_app_view.loading")}</div>
        ) : error ? (
          <div className="workspace-app-view__state" role="alert">
            <p>{t("component.workspace_app_view.error")}</p>
            <Button variant="ghost" size="sm" onClick={onRetry}>{t("component.workspace_app_view.retry")}</Button>
          </div>
        ) : view && selectedAction?.kind === "workflow" ? (
          <WorkspaceAppActionFlow
            key={`${view.workspace_id}:${selectedAction.key}:${selectedAction.binding_id}`}
            action={selectedAction}
            workspaceId={view.workspace_id}
            canStart={!actionDisabled && selectedAction.available !== false}
            chatAvailable={Boolean(canUseWorkflowChat?.(selectedAction))}
            onUseChat={() => onOpenWorkflow?.(selectedAction)}
            onBack={() => setSelectedActionKey(null)}
            onFollowProgress={onClose}
          />
        ) : view && selectedAction?.kind === "chat" ? (
          <WorkspaceAppActionChat key={`${view.workspace_id}:${selectedAction.key}:${selectedAction.agent_id || "default"}`} action={selectedAction} canStart={!actionDisabled && selectedAction.available !== false} onBack={() => setSelectedActionKey(null)} onRun={(request, files) => onAction(selectedAction, request, files)} />
        ) : view && selectedAction?.kind === "automation" ? (
          <WorkspaceAppActionAutomation key={`${view.workspace_id}:${selectedAction.key}:${selectedAction.job_id}`} action={selectedAction} canStart={!actionDisabled && selectedAction.available !== false} onBack={() => setSelectedActionKey(null)} />
        ) : view && (
          <>
            {layout === "cover" ? <div className={`workspace-app-view__cover${showCover ? " workspace-app-view__cover--image" : ""}`}>
              {coverMedia}
              {showMotion && <span className="workspace-app-view__hero-art" aria-hidden="true"><span /><span /><span /></span>}
              {intro}
              {hero && <button type="button" className="workspace-app-view__cover-action" onClick={() => selectAction(hero)} disabled={actionDisabled || hero.available === false}>
                <span>{hero.title}{hero.available === false && <small>{t("component.workspace_app_view.target_unavailable")}</small>}</span><IconArrowRight size={17} aria-hidden="true" />
              </button>}
            </div> : intro}

            {layout === "feature" && hero ? (
              <button
                type="button"
                className={`workspace-app-view__hero${showCover ? " workspace-app-view__hero--image" : ""}${visual === "plain" ? " workspace-app-view__hero--plain" : ""}`}
                data-kind={hero.kind}
                onClick={() => selectAction(hero)}
                disabled={actionDisabled || hero.available === false}
              >
                {coverMedia}
                {showMotion && <span className="workspace-app-view__hero-art" aria-hidden="true"><span /><span /><span /></span>}
                <span className="workspace-app-view__hero-icon" aria-hidden="true">
                  {hero.kind === "workflow" ? <IconFlow size={20} /> : hero.kind === "automation" ? <IconClock size={20} /> : <IconChatBubble size={20} />}
                </span>
                <span className="workspace-app-view__hero-content">
                  <strong>{hero.title}</strong>
                  {hero.available === false && <small>{t("component.workspace_app_view.target_unavailable")}</small>}
                  {hero.description && <small>{hero.description}</small>}
                </span>
                <span className="workspace-app-view__hero-arrow" aria-hidden="true"><IconArrowRight size={18} /></span>
              </button>
            ) : layout === "feature" && (
              <div className="workspace-app-view__empty">
                <IconSparkles size={20} />
                <p>{t("component.workspace_app_view.empty")}</p>
              </div>
            )}
            {layout !== "feature" && actions.length === 0 && <div className="workspace-app-view__empty">
              <IconSparkles size={20} />
              <p>{t("component.workspace_app_view.empty")}</p>
            </div>}

            {(layout === "list" ? actions.length > 0 : filtered.length > 0) && (
              <section className="workspace-app-view__section" aria-label={t(layout === "list" ? "component.workspace_app_view.all_actions" : "component.workspace_app_view.actions")}>
                <div className="workspace-app-view__section-head">
                  <h3>{t(layout === "list" ? "component.workspace_app_view.all_actions" : "component.workspace_app_view.actions")}</h3>
                  {actions.length > 5 && (
                    <label className="workspace-app-view__search">
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
                    </label>
                  )}
                </div>
                {otherActions.length > 0 && <div className="workspace-app-view__actions">
                  {otherActions.map(actionButton)}
                </div>}
                {scheduledActions.length > 0 && <div className="workspace-app-view__scheduled">
                  <h4>{t("component.workspace_app_view.scheduled_actions")}</h4>
                  <div className="workspace-app-view__scheduled-actions">{scheduledActions.map(actionButton)}</div>
                </div>}
                {filtered.length === 0 && <p className="workspace-app-view__no-results">{t("component.workspace_app_view.no_results")}</p>}
              </section>
            )}

            {!preview && <button type="button" className="workspace-app-view__edit" onClick={onEdit}>
              <IconSparkles size={15} />
              <span>{t("component.workspace_app_view.edit")}</span>
              <IconArrowRight size={15} />
            </button>}
          </>
        )}
      </div>
    </aside>
  );
}
