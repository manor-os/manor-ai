import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import type { Document } from "../lib/types";
import {
  documentInsertableMediaKind,
  generateInsertableMedia,
  importOnlineMedia,
  knowledgeInsertableMedia,
  searchOnlineInsertableMedia,
  uploadInsertableMedia,
  type InsertableMediaAsset,
  type InsertableMediaKind,
  type OnlineMediaResult,
} from "../lib/mediaInsertion";
import {
  IconCheck,
  IconFolder,
  IconGlobe,
  IconImage,
  IconLink,
  IconPlay,
  IconSparkles,
  IconUpload,
} from "./icons";
import Button from "./ui/Button";
import EmptyState from "./ui/EmptyState";
import LoadingSpinner from "./ui/LoadingSpinner";
import Modal from "./ui/Modal";
import SearchInput from "./ui/SearchInput";
import TabSwitcher from "./ui/TabSwitcher";

type MediaSource = "upload" | "knowledge" | "online" | "generate";

type MediaInsertDialogProps = {
  open: boolean;
  onClose: () => void;
  onInsert: (asset: InsertableMediaAsset) => void | Promise<void>;
  allowedKinds?: InsertableMediaKind[];
  defaultKind?: InsertableMediaKind;
  title?: string;
};

function mediaAccept(kind: InsertableMediaKind) {
  return kind === "image" ? "image/*" : "video/*";
}

function kindLabel(kind: InsertableMediaKind) {
  return kind === "image"
    ? t("component.media_insert.images")
    : t("component.media_insert.videos");
}

function documentMatchesKind(document: Document, kind: InsertableMediaKind) {
  return documentInsertableMediaKind(document) === kind;
}

function MediaDocumentPreview({ document, kind }: { document: Document; kind: InsertableMediaKind }) {
  const [previewUrl, setPreviewUrl] = useState("");

  useEffect(() => {
    let active = true;
    let localUrl = "";
    const load = async () => {
      try {
        localUrl = kind === "image"
          ? await api.documents.imageThumbnail(document.id)
          : await api.documents.videoThumbnail(document.id);
        if (active) setPreviewUrl(localUrl);
      } catch {
        if (active) setPreviewUrl("");
      }
    };
    void load();
    return () => {
      active = false;
      if (localUrl.startsWith("blob:")) URL.revokeObjectURL(localUrl);
    };
  }, [document.id, kind]);

  if (previewUrl) {
    return <img src={previewUrl} alt="" draggable={false} />;
  }
  return kind === "image" ? <IconImage size={24} /> : <IconPlay size={24} />;
}

function MediaDocumentCard({
  document,
  kind,
  selected,
  onSelect,
}: {
  document: Document;
  kind: InsertableMediaKind;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`media-insert-card${selected ? " is-selected" : ""}`}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="media-insert-card-preview">
        <MediaDocumentPreview document={document} kind={kind} />
        {kind === "video" && <span className="media-insert-video-badge"><IconPlay size={11} /></span>}
      </span>
      <span className="media-insert-card-copy">
        <strong title={document.name}>{document.name}</strong>
        <small>{kindLabel(kind)}</small>
      </span>
      {selected && <span className="media-insert-selected-mark"><IconCheck size={12} /></span>}
    </button>
  );
}

function OnlineMediaCard({
  result,
  selected,
  onSelect,
}: {
  result: OnlineMediaResult;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`media-insert-card${selected ? " is-selected" : ""}`}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="media-insert-card-preview">
        {result.kind === "image" ? (
          <img src={result.url} alt="" draggable={false} referrerPolicy="no-referrer" />
        ) : (
          <span className="media-insert-online-video"><IconPlay size={25} /></span>
        )}
        <span className="media-insert-source-badge"><IconGlobe size={11} /></span>
      </span>
      <span className="media-insert-card-copy">
        <strong title={result.name}>{result.name}</strong>
        <small>{result.attribution || new URL(result.url).hostname}</small>
      </span>
      {selected && <span className="media-insert-selected-mark"><IconCheck size={12} /></span>}
    </button>
  );
}

export default function MediaInsertDialog({
  open,
  onClose,
  onInsert,
  allowedKinds = ["image", "video"],
  defaultKind = allowedKinds[0] || "image",
  title = t("component.media_insert.title"),
}: MediaInsertDialogProps) {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [kind, setKind] = useState<InsertableMediaKind>(defaultKind);
  const [source, setSource] = useState<MediaSource>("knowledge");
  const [knowledgeSearch, setKnowledgeSearch] = useState("");
  const [selectedDocumentId, setSelectedDocumentId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [onlineQuery, setOnlineQuery] = useState("");
  const [directUrl, setDirectUrl] = useState("");
  const [onlineResults, setOnlineResults] = useState<OnlineMediaResult[]>([]);
  const [selectedOnlineId, setSelectedOnlineId] = useState("");
  const [busy, setBusy] = useState<"upload" | "generate" | "search" | "insert" | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      abortRef.current?.abort();
      abortRef.current = null;
      setBusy(null);
      setError("");
      return;
    }
    if (!allowedKinds.includes(kind)) setKind(defaultKind);
  }, [allowedKinds, defaultKind, kind, open]);

  useEffect(() => {
    setSelectedDocumentId("");
    setSelectedOnlineId("");
    setOnlineResults([]);
    setError("");
  }, [kind]);

  const knowledgeQuery = useQuery({
    queryKey: ["media-insert-knowledge", kind, knowledgeSearch.trim()],
    queryFn: async () => {
      const response = await api.documents.list({
        search: knowledgeSearch.trim() || undefined,
        include_generated_assets: true,
        limit: 120,
      });
      return response.items
        .filter((document) => documentMatchesKind(document, kind))
        .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    },
    enabled: open && source === "knowledge",
    staleTime: 20_000,
  });

  const selectedDocument = useMemo(
    () => knowledgeQuery.data?.find((document) => document.id === selectedDocumentId) || null,
    [knowledgeQuery.data, selectedDocumentId],
  );
  const selectedOnline = useMemo(
    () => onlineResults.find((result) => result.id === selectedOnlineId) || null,
    [onlineResults, selectedOnlineId],
  );

  const finishInsert = async (asset: InsertableMediaAsset) => {
    setBusy("insert");
    setError("");
    try {
      await onInsert(asset);
      onClose();
    } catch (insertError) {
      setError(insertError instanceof Error ? insertError.message : t("component.media_insert.insert_failed"));
    } finally {
      setBusy(null);
    }
  };

  const handleFile = async (file: File) => {
    setBusy("upload");
    setError("");
    try {
      const asset = await uploadInsertableMedia(file);
      await queryClient.invalidateQueries({ queryKey: ["media-insert-knowledge"] });
      await finishInsert(asset);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : t("component.media_insert.upload_failed"));
      setBusy(null);
    }
  };

  const handleGenerate = async () => {
    if (!prompt.trim()) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy("generate");
    setError("");
    try {
      const asset = await generateInsertableMedia(kind, prompt, controller.signal);
      await queryClient.invalidateQueries({ queryKey: ["media-insert-knowledge"] });
      await finishInsert(asset);
    } catch (generationError) {
      if ((generationError as Error)?.name !== "AbortError") {
        setError(generationError instanceof Error ? generationError.message : t("component.media_insert.generation_failed"));
      }
      setBusy(null);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const handleOnlineSearch = async () => {
    if (!onlineQuery.trim()) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy("search");
    setError("");
    setOnlineResults([]);
    setSelectedOnlineId("");
    try {
      const results = await searchOnlineInsertableMedia(kind, onlineQuery, controller.signal);
      setOnlineResults(results);
      if (results.length === 0) setError(t("component.media_insert.no_online_results"));
    } catch (searchError) {
      if ((searchError as Error)?.name !== "AbortError") {
        setError(searchError instanceof Error ? searchError.message : t("component.media_insert.search_failed"));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setBusy(null);
    }
  };

  const insertSelected = async () => {
    if (source === "knowledge" && selectedDocument) {
      const asset = knowledgeInsertableMedia(selectedDocument);
      if (asset) await finishInsert(asset);
      return;
    }
    if (source === "online") {
      const result = selectedOnline || (directUrl.trim() ? {
        id: directUrl.trim(),
        kind,
        name: onlineQuery.trim() || `${kind}-from-web`,
        url: directUrl.trim(),
      } satisfies OnlineMediaResult : null);
      if (!result) return;
      setBusy("insert");
      setError("");
      try {
        const asset = await importOnlineMedia(result);
        await queryClient.invalidateQueries({ queryKey: ["media-insert-knowledge"] });
        await onInsert(asset);
        onClose();
      } catch (importError) {
        setError(importError instanceof Error ? importError.message : t("component.media_insert.import_failed"));
      } finally {
        setBusy(null);
      }
    }
  };

  const primaryDisabled = busy != null || (
    source === "knowledge" ? !selectedDocument
      : source === "online" ? !selectedOnline && !directUrl.trim()
        : true
  );

  return (
    <Modal
      open={open}
      onClose={() => {
        abortRef.current?.abort();
        onClose();
      }}
      title={title}
      className="media-insert-dialog"
      bodyClassName="media-insert-dialog-body"
      width="min(980px, calc(100vw - 32px))"
      maxWidth="980px"
      height="min(720px, calc(100dvh - 48px))"
      footer={(source === "knowledge" || source === "online") ? (
        <div className="media-insert-footer">
          <span>{selectedDocument?.name || selectedOnline?.name || t("component.media_insert.select_one")}</span>
          <div>
            <Button variant="ghost" onClick={onClose}>{t("action.cancel")}</Button>
            <Button loading={busy === "insert"} disabled={primaryDisabled} onClick={() => { void insertSelected(); }}>
              {t("component.media_insert.insert")}
            </Button>
          </div>
        </div>
      ) : undefined}
    >
      <div className="media-insert-shell">
        <div className="media-insert-topline">
          {allowedKinds.length > 1 && (
            <TabSwitcher
              size="sm"
              ariaLabel={t("component.media_insert.media_type")}
              value={kind}
              onChange={(value) => setKind(value as InsertableMediaKind)}
              tabs={allowedKinds.map((value) => ({
                key: value,
                label: kindLabel(value),
                icon: value === "image" ? <IconImage size={14} /> : <IconPlay size={14} />,
              }))}
            />
          )}
          <span>{t("component.media_insert.saved_to_knowledge")}</span>
        </div>
        <TabSwitcher
          wrap
          size="sm"
          ariaLabel={t("component.media_insert.source")}
          value={source}
          onChange={(value) => setSource(value as MediaSource)}
          tabs={[
            { key: "knowledge", label: t("component.media_insert.knowledge"), icon: <IconFolder size={14} /> },
            { key: "upload", label: t("component.media_insert.upload"), icon: <IconUpload size={14} /> },
            { key: "online", label: t("component.media_insert.online"), icon: <IconGlobe size={14} /> },
            { key: "generate", label: t("component.media_insert.generate"), icon: <IconSparkles size={14} /> },
          ]}
        />

        {error && <div className="media-insert-error" role="alert">{error}</div>}

        {source === "knowledge" && (
          <div className="media-insert-panel">
            <SearchInput
              value={knowledgeSearch}
              onChange={setKnowledgeSearch}
              placeholder={t("component.media_insert.search_knowledge", { kind: kindLabel(kind).toLowerCase() })}
            />
            {knowledgeQuery.isLoading ? (
              <div className="media-insert-loading"><LoadingSpinner size={24} /> {t("status.loading")}</div>
            ) : knowledgeQuery.isError ? (
              <EmptyState
                icon={<IconFolder size={24} />}
                title={t("component.media_insert.knowledge_failed")}
                action={<Button variant="outline" onClick={() => { void knowledgeQuery.refetch(); }}>{t("action.retry")}</Button>}
              />
            ) : (knowledgeQuery.data?.length || 0) === 0 ? (
              <EmptyState
                icon={kind === "image" ? <IconImage size={24} /> : <IconPlay size={24} />}
                title={t("component.media_insert.no_knowledge_media", { kind: kindLabel(kind).toLowerCase() })}
                description={t("component.media_insert.no_knowledge_media_hint")}
              />
            ) : (
              <div className="media-insert-grid">
                {knowledgeQuery.data?.map((document) => (
                  <MediaDocumentCard
                    key={document.id}
                    document={document}
                    kind={kind}
                    selected={selectedDocumentId === document.id}
                    onSelect={() => setSelectedDocumentId(document.id)}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        {source === "upload" && (
          <div className="media-insert-upload-panel">
            <button
              type="button"
              className="media-insert-dropzone"
              disabled={busy != null}
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                const file = event.dataTransfer.files?.[0];
                if (file) void handleFile(file);
              }}
            >
              {busy === "upload" ? <LoadingSpinner size={28} /> : <IconUpload size={28} />}
              <strong>{busy === "upload" ? t("component.media_insert.uploading") : t("component.media_insert.choose_or_drop")}</strong>
              <span>{kind === "image" ? t("component.media_insert.image_formats") : t("component.media_insert.video_formats")}</span>
            </button>
            <input
              ref={fileInputRef}
              hidden
              type="file"
              accept={mediaAccept(kind)}
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                if (file) void handleFile(file);
                event.currentTarget.value = "";
              }}
            />
          </div>
        )}

        {source === "generate" && (
          <div className="media-insert-compose-panel">
            <div className="media-insert-compose-heading">
              <span className="media-insert-compose-icon"><IconSparkles size={19} /></span>
              <div>
                <strong>{t("component.media_insert.generate_kind", { kind: kindLabel(kind).toLowerCase() })}</strong>
                <p>{t("component.media_insert.uses_chat_mode")}</p>
              </div>
            </div>
            <textarea
              className="manor-input media-insert-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.currentTarget.value)}
              placeholder={kind === "image"
                ? t("component.media_insert.image_prompt")
                : t("component.media_insert.video_prompt")}
            />
            <div className="media-insert-compose-actions">
              {busy === "generate" && <span><LoadingSpinner size={14} /> {kind === "video" ? t("component.media_insert.video_generating") : t("component.media_insert.image_generating")}</span>}
              <Button loading={busy === "generate"} disabled={!prompt.trim() || busy != null} onClick={() => { void handleGenerate(); }}>
                <IconSparkles size={15} /> {t("component.media_insert.generate_and_insert")}
              </Button>
            </div>
          </div>
        )}

        {source === "online" && (
          <div className="media-insert-online-panel">
            <div className="media-insert-online-search">
              <SearchInput
                value={onlineQuery}
                onChange={setOnlineQuery}
                placeholder={t("component.media_insert.search_online", { kind: kindLabel(kind).toLowerCase() })}
              />
              <Button
                loading={busy === "search"}
                disabled={!onlineQuery.trim() || busy != null}
                onClick={() => { void handleOnlineSearch(); }}
              >
                <IconGlobe size={15} /> {t("action.search")}
              </Button>
            </div>
            <label className="media-insert-url-field">
              <span><IconLink size={13} /> {t("component.media_insert.direct_url")}</span>
              <input
                className="manor-input"
                type="url"
                value={directUrl}
                onChange={(event) => {
                  setDirectUrl(event.currentTarget.value);
                  if (event.currentTarget.value) setSelectedOnlineId("");
                }}
                placeholder="https://…"
              />
            </label>
            {busy === "search" ? (
              <div className="media-insert-loading"><LoadingSpinner size={24} /> {t("component.media_insert.agent_searching")}</div>
            ) : onlineResults.length > 0 ? (
              <div className="media-insert-grid">
                {onlineResults.map((result) => (
                  <OnlineMediaCard
                    key={result.id}
                    result={result}
                    selected={selectedOnlineId === result.id}
                    onSelect={() => {
                      setSelectedOnlineId(result.id);
                      setDirectUrl("");
                    }}
                  />
                ))}
              </div>
            ) : (
              <EmptyState
                icon={<IconGlobe size={24} />}
                title={t("component.media_insert.online_empty")}
                description={t("component.media_insert.online_hint")}
              />
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
