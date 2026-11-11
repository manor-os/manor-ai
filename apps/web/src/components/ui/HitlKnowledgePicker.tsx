import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api, type DocumentMediaKind } from "../../lib/api";
import { authPrincipalKey, getAuthToken } from "../../lib/authToken";
import { documentFolderLabel, viewerPathForDocumentId } from "../../lib/fileReferences";
import { formatFileSize, relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import type { Document } from "../../lib/types";
import Button from "./Button";
import Input from "./Input";
import Modal from "./Modal";
import TabSwitcher from "./TabSwitcher";
import { useDocumentThumbnail } from "./useDocumentThumbnail";
import { IconExternalLink, IconPause, IconPlay } from "../icons";
import { fileVisual } from "../workspaces/workspaceAppRunDisplay";
import { PageLoading } from "./Skeleton";

const PAGE_SIZE = 30;
const KIND_TABS: Array<"all" | DocumentMediaKind> = ["all", "audio", "video", "image", "document"];

/** Whether a document is a sound file, by its MIME type or extension. */
function isAudio(document: Document): boolean {
  return /^audio\//.test(document.mime_type || "")
    || /\.(mp3|wav|m4a|aac|flac|ogg|oga|opus|aiff?)$/i.test(document.name);
}

/** One track playing at a time across every picker row. */
let playing: { audio: HTMLAudioElement; stop: () => void } | null = null;

/** Play or pause a track right in the row, so a song can be found by ear when its name is forgotten. */
function TrackPreview({ document }: { document: Document }) {
  const [state, setState] = useState<"idle" | "loading" | "playing">("idle");
  const [seconds, setSeconds] = useState<number | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const url = useRef("");
  useEffect(() => () => {
    audio.current?.pause();
    if (url.current) URL.revokeObjectURL(url.current);
  }, []);
  const toggle = async () => {
    if (state === "playing") {
      audio.current?.pause();
      return;
    }
    if (!audio.current) {
      setState("loading");
      try {
        url.current = URL.createObjectURL(await api.documents.previewBlob(document.id, { cache: true }));
      } catch {
        setState("idle");
        return;
      }
      const element = new Audio(url.current);
      element.onloadedmetadata = () => setSeconds(Number.isFinite(element.duration) ? element.duration : null);
      element.onpause = () => setState("idle");
      element.onended = () => setState("idle");
      element.onplay = () => setState("playing");
      audio.current = element;
    }
    if (playing && playing.audio !== audio.current) playing.stop();
    playing = { audio: audio.current, stop: () => audio.current?.pause() };
    void audio.current.play().catch(() => setState("idle"));
  };
  const label = t(state === "playing" ? "component.audio_waveform.pause" : "component.audio_waveform.play", { name: document.name });
  return <span className="chat-hitl-knowledge-track">
    <button type="button" className="chat-hitl-knowledge-play" aria-label={label} title={label}
      aria-busy={state === "loading"} onClick={() => void toggle()}>
      {state === "playing" ? <IconPause size={14} /> : <IconPlay size={14} />}
    </button>
    {seconds !== null && <span className="mono">{Math.floor(seconds / 60)}:{String(Math.floor(seconds % 60)).padStart(2, "0")}</span>}
  </span>;
}

/** The folder a document sits in, from its Knowledge path ("Workspaces/Studio/renders"). */
function documentFolder(document: Document): string {
  const path = String(document.display_path || document.fs_path || "").replace(/^\/+/, "");
  const slash = path.lastIndexOf("/");
  return slash > 0 ? path.slice(0, slash) : "";
}

/**
 * One file to pick: what it looks like, where it lives, when it changed and how
 * big it is, so files with the same name ("brief.json") can be told apart, and
 * an Open link to look inside before choosing it.
 */
function KnowledgePickerRow({ document, selected, onToggle }: {
  document: Document;
  selected: boolean;
  onToggle: () => void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(typeof IntersectionObserver === "undefined");
  useEffect(() => {
    const row = rowRef.current;
    if (visible || !row) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setVisible(true);
    }, { rootMargin: "120px" });
    observer.observe(row);
    return () => observer.disconnect();
  }, [visible]);
  const audio = isAudio(document);
  // Only rows on screen ask for a preview image, so a long list does not render them all at once.
  const thumbnail = useDocumentThumbnail(document.id, document.name, { enabled: visible && !audio });
  const [broken, setBroken] = useState(false);
  const { extension, Icon } = fileVisual(document.name);
  const folder = documentFolder(document);
  const viewer = viewerPathForDocumentId(document.id);
  const changed = document.updated_at || document.created_at;
  const nearFolder = documentFolderLabel(folder);
  const made = document.source === "upload" || document.source === "chat_upload"
    ? t("component.knowledge_picker.uploaded")
    : ["ai_generated", "sandbox", "bash", "agent", "elevenlabs", "mcp"].includes(document.source || "")
      ? t("component.knowledge_picker.generated") : "";
  const meta = [
    changed ? relativeTime(changed) : "",
    made,
    typeof document.file_size === "number" ? formatFileSize(document.file_size) : "",
    nearFolder,
  ].filter(Boolean);
  return (
    <div ref={rowRef} className="chat-hitl-knowledge-row" data-selected={selected || undefined} data-audio={audio || undefined}>
      {audio && <TrackPreview document={document} />}
      <button type="button" className="chat-hitl-knowledge-item" aria-pressed={selected} onClick={onToggle}>
        {!audio && <span className="chat-hitl-knowledge-preview" aria-hidden="true">
          {thumbnail.src && !broken
            ? <img src={thumbnail.src} alt="" loading="lazy" onError={() => setBroken(true)} />
            : <Icon size={16} />}
        </span>}
        <span className="chat-hitl-knowledge-copy">
          <span className="chat-hitl-knowledge-name">{document.name}</span>
          {document.hint && <span className="chat-hitl-knowledge-hint" title={document.hint}>{document.hint}</span>}
          {meta.length > 0 && <span className="chat-hitl-knowledge-meta" title={folder || undefined}>{meta.join(" · ")}</span>}
        </span>
        <span className="chat-hitl-knowledge-type">{extension || document.file_type || document.mime_type}</span>
        <span className="chat-hitl-knowledge-check" aria-hidden="true">{selected ? "✓" : ""}</span>
      </button>
      {viewer && <a
        className="chat-hitl-knowledge-open"
        href={viewer}
        target="_blank"
        rel="noreferrer"
        aria-label={t("component.chat_input_footer.open_document", { name: document.name })}
        title={t("component.chat_input_footer.open_document", { name: document.name })}
      >
        <IconExternalLink size={14} aria-hidden="true" />
      </a>}
    </div>
  );
}

/** Selection belongs to this input card, never to a global chat event. */
export default function HitlKnowledgePicker({ selectedIds, onToggle, onClose, restoreFocus, acceptDocument, workspaceId, kinds }: {
  selectedIds: Set<string>;
  onToggle: (document: Document) => void;
  onClose: () => void;
  restoreFocus: () => void;
  acceptDocument?: (document: Document) => boolean;
  /** The Workspace being worked in: its files are shown first, all of Knowledge a tab away. */
  workspaceId?: string;
  /** The only kinds of file this field takes (a song: audio); without it the person picks a kind. */
  kinds?: DocumentMediaKind[];
}) {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  // Ask once typing pauses, not on every key.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);
  const [scope, setScope] = useState<"workspace" | "all">(workspaceId ? "workspace" : "all");
  const [kindTab, setKindTab] = useState<"all" | DocumentMediaKind>("all");
  // Files made on the way to a result (QA frames, recipes) stay out unless asked for.
  const [showWorkFiles, setShowWorkFiles] = useState(false);
  const scopedWorkspaceId = scope === "workspace" ? workspaceId : undefined;
  const requestedKinds = kinds?.length ? kinds : kindTab === "all" ? undefined : [kindTab];
  const documents = useInfiniteQuery({
    queryKey: ["documents", "hitl-picker", authPrincipalKey(getAuthToken()), scopedWorkspaceId || "all", query,
      requestedKinds?.join(",") || "any", showWorkFiles],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.documents.list({
      search: query || undefined,
      ...(scopedWorkspaceId ? { workspace_id: scopedWorkspaceId } : {}),
      include_generated_assets: true,
      kinds: requestedKinds,
      results_only: !showWorkFiles,
      include_usage: false,
      limit: PAGE_SIZE,
      offset: pageParam,
    }),
    getNextPageParam: (lastPage, pages) => {
      const count = pages.reduce((total, page) => total + page.items.length, 0);
      return lastPage.items.length > 0 && count < lastPage.total ? count : undefined;
    },
  });
  const items = (documents.data?.pages.flatMap((page) => page.items) || [])
    .filter((document) => !acceptDocument || acceptDocument(document));
  // Typed but not searched yet: the rows still on screen are the old ones, so a
  // click there would pick a file the person was not looking for.
  const stale = search.trim() !== query;

  return (
    <Modal
      open
      onClose={onClose}
      restoreFocusFallback={restoreFocus}
      title={t("component.chat_input_footer.add_from_knowledge_base")}
      maxWidth="600px"
      bodyClassName="chat-hitl-knowledge-body"
      footer={<Button onClick={onClose}>{t("action.done")}</Button>}
    >
      <Input
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder={t("component.chat_input_footer.search_knowledge_base")}
        ariaLabel={t("component.chat_input_footer.search_knowledge_base")}
        autoFocus
      />
      {workspaceId && <TabSwitcher
        size="sm"
        tabs={[
          { key: "workspace", label: t("component.chat_input_footer.this_workspace") },
          { key: "all", label: t("component.chat_input_footer.all_knowledge") },
        ]}
        value={scope}
        onChange={(key) => setScope(key === "all" ? "all" : "workspace")}
      />}
      {!kinds?.length && <TabSwitcher
        size="sm"
        tabs={KIND_TABS.map((key) => ({ key, label: t(`component.knowledge_picker.kind_${key}`) }))}
        value={kindTab}
        onChange={(key) => setKindTab(KIND_TABS.includes(key as "all") ? key as "all" | DocumentMediaKind : "all")}
      />}
      <div className="chat-hitl-knowledge-list" aria-busy={documents.isFetching || stale} data-stale={stale || undefined}>
        {documents.isPending && <PageLoading label={t("status.loading")} minHeight={96} />}
        {!documents.isPending && !documents.isError && items.length === 0 && !documents.hasNextPage && (
          scope === "workspace" ? (
            <div className="chat-hitl-knowledge-empty" role="status">
              <p>{search.trim() ? t("component.chat_input_footer.no_documents_found") : t("component.chat_input_footer.no_workspace_documents")}</p>
              <Button variant="ghost" size="sm" onClick={() => setScope("all")}>{t("component.chat_input_footer.show_all_knowledge")}</Button>
            </div>
          ) : <p role="status">{t("component.chat_input_footer.no_documents_found")}</p>
        )}
        {items.map((document) => (
          <KnowledgePickerRow
            key={document.id}
            document={document}
            selected={selectedIds.has(document.id)}
            onToggle={() => { if (!stale) onToggle(document); }}
          />
        ))}
        {documents.isError && (
          <div className="chat-hitl-knowledge-error" role="alert">
            <p>{t("component.chat_action_card.knowledge_load_failed")}</p>
            <Button variant="ghost" onClick={() => {
              if (documents.isFetchNextPageError) void documents.fetchNextPage();
              else void documents.refetch();
            }}>{t("page.dashboard.retry")}</Button>
          </div>
        )}
        {documents.hasNextPage && !documents.isError && (
          <Button variant="ghost" loading={documents.isFetchingNextPage} onClick={() => void documents.fetchNextPage()}>
            {t("webchat_page.load_more_documents")}
          </Button>
        )}
        {!documents.isPending && !documents.hasNextPage && (
          <Button variant="ghost" size="sm" onClick={() => setShowWorkFiles((value) => !value)}>
            {t(showWorkFiles ? "component.knowledge_picker.hide_work_files" : "component.knowledge_picker.show_work_files")}
          </Button>
        )}
      </div>
    </Modal>
  );
}
